"""
detect.py – End-card detection and game app-icon region detection.

Icon detection: use Canny edges + morphological fill + contour scoring to
locate the rounded-square app icon within the settled end-card frame.

Onset detection: binary-search backward from the last (settled) frame
to find the exact timestamp when the icon first appeared.  This is
completely independent of "scene cut" heuristics and works for any video.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class IconRegion:
    cx: int               # icon center x
    cy: int               # icon center y
    size: int             # settled icon size (pixels, square)
    corner_ratio: float   # corner_radius / size  (e.g. 0.07)
    confidence: float     # detection confidence 0-1


@dataclass
class ZoomTrack:
    """Per-frame geometry of an end-card logo that zooms out (huge → settled)."""
    times: list           # absolute timestamps (s) per animation frame
    sizes: list           # measured original-logo size (px) per frame
    centers: list         # (cx, cy) per frame
    settle_time: float    # time at which the logo is fully settled
    settle_size: int      # settled size (px)
    settle_center: tuple  # (cx, cy) settled


@dataclass
class PersistentLogo:
    """A logo/watermark that stays fixed throughout the whole video."""
    cx: int               # bbox center x (full-res px)
    cy: int               # bbox center y (full-res px)
    w: int                # bbox width  (full-res px)
    h: int                # bbox height (full-res px)
    corner_ratio: float   # corner_radius / size for the replacement logo
    confidence: float     # 0-1


# ---------------------------------------------------------------------------
# Persistent-logo detection (logo present THROUGHOUT the video, any shape)
# ---------------------------------------------------------------------------

def detect_persistent_logo(
    video_path: str | Path,
    duration: float,
    video_w: int,
    video_h: int,
    n_samples: int = 20,
    std_thresh: float = 12.0,
    t_start: float = 0.0,
    t_end: Optional[float] = None,
) -> Optional[PersistentLogo]:
    """
    Detect a logo/branding watermark that persists (nearly unchanged) for the
    entire video by finding a region that is BOTH:
      * temporally stable  – its pixels barely change across the whole clip
        (an overlay composited on top of ever-changing footage), and
      * textured           – it contains real detail/edges (so we ignore flat
        stable areas such as sky, walls or letterbox bars),
    and is located near a frame corner (where branding logos live).

    This is completely shape-agnostic: it finds square app-icons, wide text
    badges, circular emblems, etc.  Returns None when no confident persistent
    logo is found, so the caller can fall back to end-card detection.

    ``t_start``/``t_end`` restrict the temporal analysis window.  For a hybrid
    video (persistent corner logo during gameplay + a big animated end-card
    logo at the very end), pass ``t_end = end_card_onset`` so the corner logo
    is still classified as *persistent* over the gameplay portion even though
    it disappears during the end-card.
    """
    import logging
    import subprocess
    log = logging.getLogger("logoswap.detect")

    win_start = max(0.0, t_start)
    win_end = duration if t_end is None else min(duration, t_end)
    if win_end - win_start < 1.0:               # window too short to be useful
        return None

    sw = min(video_w, 384)                       # working width
    with tempfile.TemporaryDirectory(prefix="logoswap_pl_") as _tmp:
        tmp = Path(_tmp)
        frames = []
        for i in range(n_samples):
            t = win_start + (win_end - win_start) * (i + 0.5) / n_samples
            fp = tmp / f"s{i:02d}.png"
            cmd = [
                "ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(video_path),
                "-frames:v", "1", "-vf", f"scale={sw}:-1", str(fp),
            ]
            subprocess.run(cmd, capture_output=True)
            img = cv2.imread(str(fp))
            if img is not None:
                frames.append(img)

    if len(frames) < max(6, n_samples // 2):
        return None

    stack = np.stack(frames).astype(np.float32)   # (N,h,w,3)
    _, h, w, _ = stack.shape
    fx = video_w / float(w)
    fy = video_h / float(h)

    # Temporal std per pixel (avg over colour) – low where an overlay sits.
    std = stack.std(axis=0).mean(axis=2)
    stable = (std < std_thresh).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    stable = cv2.morphologyEx(stable, cv2.MORPH_OPEN, kernel, iterations=1)
    stable = cv2.morphologyEx(stable, cv2.MORPH_CLOSE, kernel, iterations=2)

    # Texture map from the mean (ghost) frame – logos have edges; flat walls don't.
    meanf = stack.mean(axis=0).astype(np.uint8)
    gray = cv2.cvtColor(meanf, cv2.COLOR_BGR2GRAY)
    grad = cv2.magnitude(
        cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    )
    textured = (grad > 40).astype(np.uint8) * 255
    textured = cv2.dilate(textured, kernel, iterations=2)

    cand_mask = cv2.bitwise_and(stable, textured)
    cand_mask = cv2.morphologyEx(cand_mask, cv2.MORPH_CLOSE, kernel, iterations=3)

    contours, _ = cv2.findContours(
        cand_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    frame_area = w * h
    best = None
    best_conf = 0.0
    for cnt in contours:
        x, y, ww, hh = cv2.boundingRect(cnt)
        area = ww * hh
        # Floor at ~0.3 % of frame so tiny persistent UI (score/gear icons)
        # can't be mistaken for a branding logo; cap at 18 %.
        if area < frame_area * 0.003 or area > frame_area * 0.18:
            continue
        # ── NEW: reject extreme aspect ratios (text bars, thin border strips) ──
        if min(ww, hh) / max(ww, hh) < 0.55:
            continue
        # ── NEW: reject regions too wide/tall to be a corner watermark logo ──
        # A genuine app-icon watermark is at most ~20 % of the smaller frame dim.
        if ww > w * 0.22 or hh > h * 0.22:
            continue
        # Corner proximity: 0 = exactly in a corner, larger = toward centre.
        # Tightened from 0.38 → 0.30 to exclude things near the frame centre.
        cxr, cyr = (x + ww / 2) / w, (y + hh / 2) / h
        corner_score = min(cxr, 1 - cxr) + min(cyr, 1 - cyr)
        if corner_score > 0.30:
            continue
        fill = cv2.contourArea(cnt) / max(1.0, area)
        region_std = float(std[y:y + hh, x:x + ww].mean())

        conf_corner = max(0.0, 1.0 - corner_score / 0.5)
        conf_stab = max(0.0, 1.0 - region_std / std_thresh)
        confidence = 0.5 * conf_corner + 0.3 * conf_stab + 0.2 * min(1.0, fill)
        if confidence > best_conf:
            best_conf = confidence
            best = (x, y, ww, hh)

    if best is None or best_conf < 0.45:
        return None

    x, y, ww, hh = best
    # Pad ~12 % so soft edges of the original are fully covered, then scale up.
    px, py = int(ww * 0.12), int(hh * 0.12)
    x = max(0, x - px); y = max(0, y - py)
    ww = min(w - x, ww + 2 * px); hh = min(h - y, hh + 2 * py)

    fw = int(round(ww * fx)); fh = int(round(hh * fy))
    fcx = int(round((x + ww / 2) * fx))
    fcy = int(round((y + hh / 2) * fy))

    log.debug(
        f"detect_persistent_logo: bbox=({fcx},{fcy}) {fw}x{fh} conf={best_conf:.2f}"
    )
    return PersistentLogo(fcx, fcy, fw, fh, corner_ratio=0.16, confidence=best_conf)


# ---------------------------------------------------------------------------
# Corner-logo detection (hybrid: same-brand watermark in a frame corner)
# ---------------------------------------------------------------------------

def detect_corner_logo(
    video_path: str | Path,
    settled_frame_rgb: np.ndarray,
    region: "IconRegion",
    onset: float,
    n_samples: int = 6,
    min_score: float = 0.70,
) -> Optional[PersistentLogo]:
    """
    Detect a small brand watermark sitting in a frame corner throughout
    gameplay (a *hybrid* video that also has a big end-card logo).

    Temporal-stability detection fails on these because the whole scene
    backdrop (e.g. a desk) is often static too — not just the logo.  Instead
    we exploit the fact that the corner watermark is the SAME brand asset as
    the end-card icon: we build a template from the detected settled icon and
    multi-scale template-match it in each of the four corners across several
    gameplay frames.  A corner with a strong, consistent match is the logo.

    Returns a PersistentLogo (square bbox) or None.
    """
    import logging
    import tempfile as _tmpmod
    from .probe import extract_single_frame, load_frame_rgb
    log = logging.getLogger("logoswap.detect")

    half = region.size // 2
    H, W = settled_frame_rgb.shape[:2]
    ix0 = max(0, region.cx - half); iy0 = max(0, region.cy - half)
    ix1 = min(W, region.cx + half); iy1 = min(H, region.cy + half)
    if ix1 - ix0 < 20 or iy1 - iy0 < 20:
        return None
    tmpl_full = cv2.cvtColor(
        settled_frame_rgb[iy0:iy1, ix0:ix1], cv2.COLOR_RGB2GRAY
    )

    cw, ch = int(W * 0.35), int(H * 0.30)
    corner_boxes = {
        "TL": (0, 0, cw, ch),
        "TR": (W - cw, 0, W, ch),
        "BL": (0, H - ch, cw, H),
        "BR": (W - cw, H - ch, W, H),
    }

    # Sample gameplay frames well before the end-card.
    t_lo, t_hi = 1.0, max(1.5, onset - 0.8)
    if t_hi - t_lo < 0.5:
        return None

    hits: dict[str, list] = {k: [] for k in corner_boxes}
    with _tmpmod.TemporaryDirectory(prefix="logoswap_cl_") as _tt:
        tt = Path(_tt)
        for i in range(n_samples):
            t = t_lo + (t_hi - t_lo) * (i + 0.5) / n_samples
            fp = tt / f"c{i:02d}.png"
            try:
                extract_single_frame(video_path, t, fp)
                frame = load_frame_rgb(fp)
            except Exception:
                continue
            g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            for name, (x0, y0, x1, y1) in corner_boxes.items():
                roi = g[y0:y1, x0:x1]
                best = (-1.0, None)
                # Corner watermarks are SMALL (< 10 % of frame width = ~108 px
                # for 1080p).  The previous range up to 22 % (237 px) was
                # matching large game-branding art and top-banner logos.
                for s in np.linspace(0.03, 0.10, 16):
                    tp = int(W * s)
                    if tp < 20 or tp >= roi.shape[0] or tp >= roi.shape[1]:
                        continue
                    tmpl = cv2.resize(tmpl_full, (tp, tp))
                    res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
                    _, mv, _, ml = cv2.minMaxLoc(res)
                    if mv > best[0]:
                        best = (mv, (x0 + ml[0], y0 + ml[1], tp))
                if best[0] >= min_score and best[1] is not None:
                    # The matched watermark must be SMALLER than the end-card
                    # icon (a genuine corner watermark is a scaled-down version
                    # of the brand).  Reject if it's ≥ 70 % of icon size.
                    tp_found = best[1][2]
                    if tp_found >= region.size * 0.70:
                        continue
                    hits[name].append((best[0], *best[1]))

    # Pick the corner with the most consistent strong matches.
    # Require at least 2/3 of samples to agree.
    min_hits = max(3, n_samples * 2 // 3)
    best_corner, best_list = None, []
    for name, lst in hits.items():
        if len(lst) >= min_hits and len(lst) > len(best_list):
            best_corner, best_list = name, lst
    if best_corner is None:
        return None

    arr = np.array([[h[1], h[2], h[3], h[0]] for h in best_list], dtype=np.float32)
    mx = int(np.median(arr[:, 0]))
    my = int(np.median(arr[:, 1]))
    sz = int(np.median(arr[:, 2]))
    score = float(np.median(arr[:, 3]))
    cx = mx + sz // 2
    cy = my + sz // 2

    log.info(
        f"detect_corner_logo: {best_corner} corner logo at ({cx},{cy}) "
        f"{sz}x{sz}  score={score:.2f}  ({len(best_list)}/{n_samples} frames)"
    )
    return PersistentLogo(cx, cy, sz, sz, corner_ratio=region.corner_ratio, confidence=score)


# ---------------------------------------------------------------------------
# End-card zoom-out measurement
# ---------------------------------------------------------------------------

def measure_endcard_zoom(
    video_path: str | Path,
    settled_frame_rgb: np.ndarray,
    cx: int,
    cy: int,
    settled_size: int,
    onset: float,
    fps: float,
    video_w: int,
    video_h: int,
    window: float = 1.5,
) -> Optional[ZoomTrack]:
    """
    Detect and measure a *zoom-out* end-card logo entrance: the logo appears
    huge (often near full-screen) and shrinks down to its settled size.

    A plain scale-pop tracker (which assumes the logo grows from small to
    settled) cannot cover this: while the original is still larger than
    settled, a settled-size replacement leaves the original poking out around
    the edges.  This routine measures the original logo's bounding box in
    every frame from ``onset`` forward by isolating the central, non-background
    blob (works for any flat-background end-card, light or dark), so the
    replacement can be rendered at the exact per-frame size and centre.

    Returns a ``ZoomTrack`` when a genuine zoom-out is detected, else None so
    the caller falls back to the normal pop/slide/static logic.
    """
    import logging
    from .probe import extract_single_frame, load_frame_rgb
    log = logging.getLogger("logoswap.detect")

    H, W = settled_frame_rgb.shape[:2]
    half = settled_size // 2
    ix0 = max(0, cx - half); iy0 = max(0, cy - half)
    ix1 = min(W, cx + half); iy1 = min(H, cy + half)
    if ix1 - ix0 < 20 or iy1 - iy0 < 20:
        return None
    icon_gray = cv2.cvtColor(settled_frame_rgb[iy0:iy1, ix0:ix1], cv2.COLOR_RGB2GRAY)

    # Work at reduced resolution for speed; measurements are scaled back up.
    work_w = min(W, 540)
    sc = work_w / float(W)
    work_h = int(round(H * sc))
    roi_y1 = int(work_h * 0.80)         # exclude the bottom (PLAY NOW / badges)
    ACCEPT = 0.45                       # template-match score to trust a frame

    scales = np.linspace(0.6, 2.6, 21)

    def _measure(frame_rgb: np.ndarray):
        """Return (size, cx, cy, score) of the best template match, or None."""
        g = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
        g = cv2.resize(g, (work_w, work_h))
        roi = g[:roi_y1, :]
        best = (-1.0, None)
        for s in scales:
            tp = int(settled_size * s * sc)
            if tp < 24 or tp >= roi.shape[0] or tp >= roi.shape[1]:
                continue
            tmpl = cv2.resize(icon_gray, (tp, tp))
            res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv > best[0]:
                size_full = int(settled_size * s)
                fcx = int((ml[0] + tp / 2) / sc)
                fcy = int((ml[1] + tp / 2) / sc)
                best = (mv, (size_full, fcx, fcy))
        if best[1] is None or best[0] < ACCEPT:
            return None
        size_full, fcx, fcy = best[1]
        return (size_full, fcx, fcy, float(best[0]))

    # Scan from before the reported onset (the logo appears huge before the
    # background settles) through to the settled state.
    look_back = 0.7
    t_scan0 = max(0.0, onset - look_back)
    n_frames = int((look_back + window) * fps)
    times, sizes, centers = [], [], []
    import tempfile as _tmpmod
    with _tmpmod.TemporaryDirectory(prefix="logoswap_zoom_") as _tt:
        tt = Path(_tt)
        for i in range(n_frames):
            t = t_scan0 + i / fps
            fp = tt / f"z{i:03d}.png"
            try:
                extract_single_frame(video_path, t, fp)
                frame = load_frame_rgb(fp)
            except Exception:
                break
            m = _measure(frame)
            times.append(t)
            if m is None:
                sizes.append(None); centers.append(None)
            else:
                sizes.append(m[0]); centers.append((m[1], m[2]))

    good = [s for s in sizes if s is not None]
    # Require a DRAMATIC zoom-out (logo ≥1.8× settled).  Normal burst-pop
    # end-cards peak around 1.5× and must keep using the pop path, so this
    # gate avoids regressing them.
    if len(good) < 4 or max(good) < settled_size * 1.80:
        return None

    # Settle index: first accepted frame at/under 1.12× settled that stays low.
    settle_idx = None
    for i in range(len(sizes) - 1):
        if (sizes[i] is not None and sizes[i + 1] is not None
                and sizes[i] <= settled_size * 1.12
                and sizes[i + 1] <= settled_size * 1.12):
            settle_idx = i
            break
    if settle_idx is None:
        for i in range(len(sizes) - 1, -1, -1):
            if sizes[i] is not None:
                settle_idx = i
                break
    if settle_idx is None:
        return None

    # Entrance: walk backward from settle across the contiguous run of accepted
    # (logo-present) frames.  Stops at the gameplay/transition discontinuity.
    ent = settle_idx
    while ent - 1 >= 0 and sizes[ent - 1] is not None:
        ent -= 1

    if sizes[ent] is None or sizes[ent] < settled_size * 1.40:
        return None

    out_times, out_sizes, out_centers = [], [], []
    last_size = sizes[ent]
    last_center = centers[ent] if centers[ent] is not None else (cx, cy)
    for i in range(ent, settle_idx + 1):
        s, c = sizes[i], centers[i]
        if s is None:
            s, c = last_size, last_center
        else:
            last_size, last_center = s, c
        out_times.append(times[i])
        out_sizes.append(int(s))
        out_centers.append((int(c[0]), int(c[1])))

    # Prepend 3 frames sized larger than the entrance to cover the 1-3 heavily
    # motion-blurred frames just before the logo becomes matchable (where it is
    # even bigger).  These sit at the entrance centre.
    ecx, ecy = out_centers[0]
    big = int(out_sizes[0] * 1.32)
    for k in range(3, 0, -1):
        out_times.insert(0, out_times[0] - 1.0 / fps)
        out_sizes.insert(0, big)
        out_centers.insert(0, (ecx, ecy))

    # Enforce a non-increasing (zoom-OUT) profile.
    for i in range(len(out_sizes) - 2, -1, -1):
        if out_sizes[i] < out_sizes[i + 1]:
            out_sizes[i] = out_sizes[i + 1]

    log.info(
        f"measure_endcard_zoom: ZOOM-OUT detected  max={max(out_sizes)}px → "
        f"settled≈{settled_size}px  frames={len(out_sizes)}  "
        f"entrance@{out_times[0]:.3f}s  settle@{times[settle_idx]:.3f}s"
    )
    return ZoomTrack(
        times=out_times,
        sizes=out_sizes,
        centers=out_centers,
        settle_time=times[settle_idx],
        settle_size=settled_size,
        settle_center=(cx, cy),
    )


# ---------------------------------------------------------------------------
# Settled-frame extraction  (replaces the old find_end_card)
# ---------------------------------------------------------------------------

def get_settled_frame(
    video_path: str | Path,
    duration: float,
) -> np.ndarray:
    """
    Extract the very last frame of the video (icon is guaranteed settled).
    Returns an (H,W,3) RGB numpy array.
    """
    from .probe import extract_single_frame, load_frame_rgb

    with tempfile.TemporaryDirectory(prefix="logoswap_sf_") as _tmp:
        out = Path(_tmp) / "settled.png"
        extract_single_frame(video_path, max(0.0, duration - 0.2), out)
        return load_frame_rgb(out)


# ---------------------------------------------------------------------------
# Icon onset detection
# ---------------------------------------------------------------------------

def find_icon_onset(
    video_path: str | Path,
    cx: int,
    cy: int,
    icon_size: int,
    duration: float,
    look_back: float = 10.0,
) -> float:
    """
    Binary-search backward from the last frame to find the timestamp when
    the end-card FIRST appears (which is when the icon also appears).

    Strategy
    --------
    Compare the FULL FRAME (excluding the icon center area) to the settled
    end-card frame.  The end-card background (autumn countryside, dark room,
    etc.) is completely different from the gameplay scene that precedes it.
    This is robust even when the icon has dynamic interior content (animated
    tiles, particle effects) that would fool a crop-only comparison.

    Returns
    -------
    onset_time : float
        Earliest time at which the end-card background is visible.
        The replacement logo will be shown from this time onward,
        guaranteeing the original icon is always covered.
    """
    from .probe import extract_single_frame, load_frame_rgb

    with tempfile.TemporaryDirectory(prefix="logoswap_on_") as _tmp:
        tmp = Path(_tmp)

        # ── Settled template (full frame) ────────────────────────────────
        settled_path = tmp / "settled.png"
        extract_single_frame(video_path, max(0.0, duration - 0.2), settled_path)
        settled = load_frame_rgb(settled_path)
        H, W = settled.shape[:2]

        # Build a mask that EXCLUDES the icon region so dynamic icon
        # content does not affect the comparison.
        # Exclude a generous 1.3× icon-size box around the icon centre.
        excl = int(icon_size * 0.65)
        iy0 = max(0, cy - excl);  iy1 = min(H, cy + excl)
        ix0 = max(0, cx - excl);  ix1 = min(W, cx + excl)

        settled_f = settled.astype(np.float32)

        def _diff_bg(frame_rgb: np.ndarray) -> float:
            """Mean absolute diff in the background region (outside icon)."""
            diff = np.abs(frame_rgb.astype(np.float32) - settled_f)
            # Zero out the icon exclusion zone so tiles don't pollute score
            diff[iy0:iy1, ix0:ix1] = 0.0
            n_px = diff.size - (iy1 - iy0) * (ix1 - ix0) * 3
            if n_px <= 0:
                return 1.0
            return float(diff.sum()) / (n_px * 255.0)

        def _present(t: float) -> bool:
            """True if the end-card background is visible at time t."""
            fp = tmp / "chk.png"
            try:
                extract_single_frame(video_path, max(0.0, t), fp)
                frame = load_frame_rgb(fp)
            except Exception:
                return False
            return _diff_bg(frame) < 0.12   # <12 % mean diff → end-card background

        # ── Verify settled frame itself passes the check ─────────────────
        if not _present(duration - 0.2):
            return max(0.0, duration - 3.0)   # safe fallback

        # ── Binary search ───────────────────────────────────────────────
        t_lo = max(0.0, duration - look_back)
        t_hi = duration - 0.2

        for _ in range(10):   # 10 iters → ≈ look_back/1024 ≈ 0.008 s precision
            t_mid = (t_lo + t_hi) / 2.0
            if _present(t_mid):
                t_hi = t_mid   # end-card present → search even earlier
            else:
                t_lo = t_mid   # end-card absent  → search later

        return t_hi   # earliest confirmed end-card presence


def find_icon_end(
    video_path: str | Path,
    cx: int,
    cy: int,
    icon_size: int,
    onset_time: float,
    duration: float,
    diff_thresh: float = 0.10,
) -> Optional[float]:
    """
    Detect when the end-card icon DISAPPEARS (fade-out, cut, or wipe) so the
    replacement overlay can be bounded in time.

    Scans forward from ``onset_time`` using the settled (last-frame) icon crop
    as a reference.  Returns the timestamp of the last frame that still
    matches the settled icon (within ``diff_thresh``), or None if the icon
    stays visible until the very end of the video (most common case – caller
    should not pass end_time and let the overlay run to EOS).

    Parameters
    ----------
    onset_time   : the detected end-card onset (overlay start)
    duration     : total video duration in seconds
    diff_thresh  : icon-region mean abs diff that signals the icon is gone
    """
    import logging
    log = logging.getLogger("logoswap.detect")
    from .probe import extract_single_frame, load_frame_rgb

    with tempfile.TemporaryDirectory(prefix="logoswap_ie_") as _tmp:
        tmp = Path(_tmp)

        # Extract the settled (reference) icon crop from the last frame.
        ref_path = tmp / "ref.png"
        extract_single_frame(video_path, max(0.0, duration - 0.2), ref_path)
        ref_frame = load_frame_rgb(ref_path)
        H, W = ref_frame.shape[:2]

        half = max(16, icon_size // 2)
        y0 = max(0, cy - half); y1 = min(H, cy + half)
        x0 = max(0, cx - half); x1 = min(W, cx + half)
        ref_crop = ref_frame[y0:y1, x0:x1].astype(np.float32)

        def _icon_diff(t: float) -> float:
            fp = tmp / "chk.png"
            try:
                extract_single_frame(video_path, t, fp)
                f = load_frame_rgb(fp)
                return float(np.abs(f[y0:y1, x0:x1].astype(np.float32) - ref_crop).mean()) / 255.0
            except Exception:
                return 1.0

        # Check 0.3s before end: if the icon is still there the overlay runs
        # to EOS and we return None (no bounded end_time needed).
        if _icon_diff(duration - 0.3) < diff_thresh:
            return None

        # Binary search for the last frame where the icon is present.
        t_lo = onset_time
        t_hi = duration - 0.3
        for _ in range(8):   # 8 iters → precision ~(duration - onset) / 256
            t_mid = (t_lo + t_hi) / 2.0
            if _icon_diff(t_mid) < diff_thresh:
                t_lo = t_mid   # icon still present → search later
            else:
                t_hi = t_mid   # icon gone → search earlier

        end_time = t_lo + 0.1   # add small buffer past last confirmed presence
        log.info(
            f"find_icon_end: end-card icon gone at ~{end_time:.3f}s "
            f"(video ends at {duration:.3f}s; adding end_time bound)"
        )
        return min(end_time, duration)


def find_icon_region_onset(
    video_path: str | Path,
    cx: int,
    cy: int,
    icon_size: int,
    bg_onset: float,
    look_back: float = 1.5,
    forward_look: float = 8.0,
) -> float:
    """
    Scan from ``bg_onset - look_back`` to ``bg_onset + forward_look`` to find
    the EARLIEST frame where the icon region contains an actual rounded-square
    app icon – NOT just random game tiles flying through during a transition.

    This is the primary timing fix: ``find_icon_onset`` returns when the
    end-card BACKGROUND first appears (which can be several seconds before the
    icon pops in).  This function pins the overlay start to the actual icon
    first-appearance, not the background appearance.

    Strategy
    --------
    Sample frames every 100 ms.  For each frame, crop the icon region and
    run contour-based icon detection (the same routine used on the settled
    frame).  The first sample where a solid, roughly square candidate is
    found marks the icon's first appearance.

    This is far more discriminating than pixel-diff alone: flying game
    tiles typically have irregular outlines and low solidity and are
    rejected by the contour filter, while an actual app icon (rounded
    rectangle, high solidity) is accepted.

    Returns
    -------
    float
        Earliest time a real icon is detected in the region.
        Returns ``bg_onset`` unchanged if nothing is found (safe fallback).
    """
    import logging
    log = logging.getLogger("logoswap.detect")
    from .probe import extract_single_frame, load_frame_rgb

    with tempfile.TemporaryDirectory(prefix="logoswap_iro_") as _tmp:
        tmp = Path(_tmp)

        step = 0.1   # 100 ms between probes – fast enough, cheap enough
        # The icon cannot appear before the end-card background transitions in,
        # so scan starts at bg_onset (not look_back seconds earlier).
        # look_back is kept as a parameter for backwards compatibility but is
        # NOT used to start the scan earlier (that was causing gameplay frames
        # to be mistaken for the icon during transition animations).
        t_start = bg_onset
        t_end   = bg_onset + forward_look   # scan well past bg_onset

        # Crop a box ±70 % of icon_size around the expected centre.
        # We pass video_w/h as the *crop* dimensions so size heuristics
        # inside _find_candidates stay calibrated.
        pad = int(icon_size * 0.70)

        t = t_start
        while t <= t_end:
            fp = tmp / "probe.png"
            try:
                extract_single_frame(video_path, max(0.0, t), fp)
                frame = load_frame_rgb(fp)
            except Exception:
                t += step
                continue

            H, W = frame.shape[:2]
            y0 = max(0, cy - pad); y1 = min(H, cy + pad)
            x0 = max(0, cx - pad); x1 = min(W, cx + pad)
            crop_bgr = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_RGB2BGR)
            ch, cw = crop_bgr.shape[:2]

            if ch > 0 and cw > 0:
                # Pass FULL FRAME dimensions so the size filter is calibrated
                # to the real video resolution (not the smaller crop).  This
                # raises min_size from ~44 px (crop-based) to ~130 px (frame-
                # based), filtering out small game tiles / transition particles.
                candidates = _find_candidates(crop_bgr, W, H, loose=False)

                # Require the candidate to be NEAR the crop centre (within
                # ±35 % of icon_size in each axis).  This rejects off-centre
                # elements that happen to pass the shape filter.
                near_centre = []
                cx_crop = cw // 2
                cy_crop = ch // 2
                tol = max(20, int(icon_size * 0.35))
                for c in candidates:
                    if abs(c["cx"] - cx_crop) <= tol and abs(c["cy"] - cy_crop) <= tol:
                        near_centre.append(c)

                if near_centre:
                    log.debug(
                        f"find_icon_region_onset: icon detected at {t:.3f}s "
                        f"(bg_onset={bg_onset:.3f}s, Δ={t - bg_onset:+.3f}s)"
                    )
                    return t

            t += step

        return bg_onset


# ---------------------------------------------------------------------------
# Icon detection
# ---------------------------------------------------------------------------

def detect_icon(
    settled_frame_rgb: np.ndarray,
    video_w: int,
    video_h: int,
    region_override: Optional[tuple[int, int, int, int]] = None,
    debug_path: Optional[Path] = None,
) -> IconRegion:
    """
    Detect the game app-icon (rounded square) in the settled end-card frame.

    Parameters
    ----------
    settled_frame_rgb : np.ndarray
        End-card frame as (H,W,3) RGB.
    video_w, video_h : int
        Video dimensions (for size-range heuristics).
    region_override : (x, y, w, h) | None
        Skip detection; treat this box as the icon.
    debug_path : Path | None
        If given, write a detection-overlay PNG here for visual inspection.

    Returns
    -------
    IconRegion
        Detected (or overridden) region with confidence score.
    """
    if region_override is not None:
        x, y, w, h = region_override
        size = (w + h) // 2
        cx, cy = x + w // 2, y + h // 2
        cr = _measure_corner_ratio(settled_frame_rgb, cx, cy, size)
        region = IconRegion(cx, cy, size, cr, confidence=1.0)
        _write_debug(settled_frame_rgb, region, debug_path)
        return region

    bgr = cv2.cvtColor(settled_frame_rgb, cv2.COLOR_RGB2BGR)
    candidates = _find_candidates(bgr, video_w, video_h, loose=False)

    if not candidates:
        candidates = _find_candidates(bgr, video_w, video_h, loose=True)

    if not candidates:
        # Last-resort: centre of frame at 35 % of min dimension
        size = int(min(video_w, video_h) * 0.35)
        cx, cy = video_w // 2, video_h // 3
        region = IconRegion(cx, cy, size, corner_ratio=0.05, confidence=0.0)
        _write_debug(settled_frame_rgb, region, debug_path)
        return region

    best = max(candidates, key=lambda c: c["score"])
    cx, cy = best["cx"], best["cy"]
    size = (best["w"] + best["h"]) // 2
    cr = _measure_corner_ratio(settled_frame_rgb, cx, cy, size)
    confidence = min(1.0, best["score"] / 6.0)

    region = IconRegion(cx, cy, size, cr, confidence)
    _write_debug(settled_frame_rgb, region, debug_path)
    return region


# ---------------------------------------------------------------------------
# Internals – candidate detection
# ---------------------------------------------------------------------------

def _find_candidates(
    bgr: np.ndarray,
    video_w: int,
    video_h: int,
    loose: bool = False,
) -> list[dict]:
    """
    Run multi-threshold Canny + morphological fill + contour scoring to
    find rounded-square app-icon candidates.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    min_dim = min(video_w, video_h)
    min_size = min_dim * (0.07 if loose else 0.10)
    # Allow icons up to 40 % of frame dimension so that prominent end-card
    # icons (e.g. Crossword GO!, ~380 px on 1080 p) are not cut.  The
    # size_score below provides a SOFT penalty for elements above 28 %, so
    # genuine large icons still score well while noisy game-art regions (low
    # border contrast, poor squareness) lose to a compact icon.
    max_size = min_dim * (0.45 if loose else 0.40)
    min_aspect = 0.72 if loose else 0.83
    min_solidity = 0.72 if loose else 0.85

    candidates: list[dict] = []

    # Try a few Canny threshold pairs to be robust to contrast differences
    for lo, hi in [(20, 60), (30, 90), (50, 150)]:
        edges = cv2.Canny(blurred, lo, hi)

        # Morphologically close + dilate to fill the icon body
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=4)
        dilated = cv2.dilate(closed, k, iterations=4)

        contours, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)

            # ── Correct for morphological inflation ──────────────────────────
            # MORPH_CLOSE (k=7, iter=4) + dilate (k=7, iter=4) each expand
            # the mask by (k//2)*iters = 12 px per side → 24 px per side total
            # → each dimension of the bbox is ~48 px larger than the true icon.
            # Subtracting this gives an accurate icon size for logo sizing.
            _MORPH_INFLATE = 48  # px per dimension (24 px per side × both ops)
            x_raw, y_raw, w_raw, h_raw = x, y, w, h
            w = max(8, w_raw - _MORPH_INFLATE)
            h = max(8, h_raw - _MORPH_INFLATE)
            # Centre stays the same (inflation is symmetric)
            cx = x_raw + w_raw // 2
            cy_ = y_raw + h_raw // 2

            # Size filter (on corrected dimensions)
            if not (min_size <= w <= max_size and min_size <= h <= max_size):
                continue

            # Aspect-ratio filter
            aspect = min(w, h) / max(w, h)
            if aspect < min_aspect:
                continue

            # Solidity (area / convex-hull area)
            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            if hull_area < 100:
                continue
            solidity = cv2.contourArea(cnt) / hull_area
            if solidity < min_solidity:
                continue

            # cx, cy_ already computed from raw (un-inflated) bbox centre above

            # ── Score components ──────────────────────────────────────────────
            squareness = aspect                                  # 0–1, want 1
            rel_size = min(w, h) / min_dim                      # 0–1

            # Size score: peak at 18 % of frame (typical small icon range).
            # For larger elements (>28 %) a gentle linear penalty applies;
            # they are not zeroed out so that genuinely prominent icons like
            # a 380 px Crossword-GO end-card icon can still win on their
            # overall quality (border contrast, solidity, position).
            # Very large game-art (>55 % = 594 px) scores 0.
            if rel_size > 0.28:
                size_score = max(0.0, 0.60 - (rel_size - 0.28) * 2.2)
            else:
                size_score = max(0.0, 1.0 - abs(rel_size - 0.18) / 0.14)

            border_score = _border_contrast(bgr, x, y, w, h)
            interior_var = np.std(gray[y : y + h, x : x + w]) / 255.0
            int_score = min(1.0, interior_var * 3.0)

            # ── Vertical position bonus ────────────────────────────────────────
            # Game-ad end-cards put the app icon in the LOWER 60 % of the frame.
            # Elements in the top 25 % (game title, score, hooks) receive a
            # penalty so they lose to a genuine bottom-area icon.
            vert_ratio = cy_ / video_h   # 0 = top, 1 = bottom
            if vert_ratio < 0.25:
                pos_score = max(0.0, vert_ratio / 0.25 - 0.5)   # 0 at top, 0.5 at 25 %
            elif vert_ratio < 0.45:
                pos_score = 0.5 + (vert_ratio - 0.25) / 0.20 * 0.5  # 0.5→1.0
            else:
                pos_score = 1.0   # lower 55 % of frame: full bonus

            score = (
                squareness * 2.0
                + size_score * 2.0      # raised weight – size matters most
                + border_score * 1.5
                + int_score * 1.0
                + solidity * 1.0
                + pos_score * 1.5       # new: vertical position preference
            )

            candidates.append(
                {
                    "x": x, "y": y, "w": w, "h": h,
                    "cx": cx, "cy": cy_,
                    "score": score,
                }
            )

    return _deduplicate(candidates, iou_thresh=0.40)


def _border_contrast(
    bgr: np.ndarray, x: int, y: int, w: int, h: int, ring: int = 8
) -> float:
    """Mean gradient magnitude in a ring just outside the candidate bbox."""
    H, W = bgr.shape[:2]
    x0, y0 = max(0, x - ring), max(0, y - ring)
    x1, y1 = min(W, x + w + ring), min(H, y + h + ring)

    region = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(region, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(region, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx ** 2 + gy ** 2)

    # Mask: outer ring only (exclude interior)
    mask = np.ones(mag.shape, dtype=bool)
    ix0 = x - x0 + ring
    iy0 = y - y0 + ring
    ix1 = ix0 + w - ring * 2
    iy1 = iy0 + h - ring * 2
    if ix1 > ix0 and iy1 > iy0:
        mask[max(0, iy0) : max(0, iy1), max(0, ix0) : max(0, ix1)] = False

    ring_vals = mag[mask]
    return min(1.0, float(np.mean(ring_vals)) / 120.0) if len(ring_vals) else 0.0


def _iou(a: dict, b: dict) -> float:
    """Intersection-over-union for two dicts with x, y, w, h."""
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["w"], ay1 + a["h"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["w"], by1 + b["h"]

    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _deduplicate(candidates: list[dict], iou_thresh: float = 0.40) -> list[dict]:
    """Non-maximum suppression: drop lower-scoring duplicates."""
    by_score = sorted(candidates, key=lambda c: -c["score"])
    kept: list[dict] = []
    for c in by_score:
        if not any(_iou(c, k) > iou_thresh for k in kept):
            kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# Internals – corner radius measurement
# ---------------------------------------------------------------------------

def _measure_corner_ratio(
    frame_rgb: np.ndarray, cx: int, cy: int, size: int
) -> float:
    """
    Estimate corner_radius / size by scanning horizontal insets near the
    top of the icon crop and finding where the first "filled" row starts.
    """
    half = size // 2
    H, W = frame_rgb.shape[:2]
    x0 = max(0, cx - half)
    y0 = max(0, cy - half)
    x1 = min(W, cx + half)
    y1 = min(H, cy + half)

    if x1 <= x0 or y1 <= y0:
        return 0.05

    crop = frame_rgb[y0:y1, x0:x1].astype(np.float32)
    gray = crop.mean(axis=2)
    crop_h, crop_w = gray.shape

    # Estimate background from the four corners
    csz = max(3, size // 30)
    bg = float(
        np.mean([
            gray[:csz, :csz],
            gray[:csz, -csz:],
            gray[-csz:, :csz],
            gray[-csz:, -csz:],
        ])
    )

    diff = np.abs(gray - bg)
    thr = max(10.0, float(diff.max()) * 0.15)
    inside = diff > thr

    # Scan top rows: find left inset (= corner radius in pixels)
    for dy in range(min(crop_h // 3, 80)):
        row = inside[dy]
        filled = np.where(row)[0]
        if len(filled) > crop_w * 0.25:
            inset = int(filled[0])
            if inset <= 0:
                return 0.03
            return min(0.20, max(0.02, inset / crop_w))

    return 0.05


# ---------------------------------------------------------------------------
# Internals – debug preview
# ---------------------------------------------------------------------------

def _write_debug(
    frame_rgb: np.ndarray, region: IconRegion, debug_path: Optional[Path]
) -> None:
    """Write a detection-overlay PNG for visual inspection."""
    if debug_path is None:
        return

    vis = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    half = region.size // 2
    color = (0, 220, 0) if region.confidence >= 0.5 else (0, 140, 255)

    cv2.rectangle(
        vis,
        (region.cx - half, region.cy - half),
        (region.cx + half, region.cy + half),
        color, 3,
    )
    cv2.circle(vis, (region.cx, region.cy), 6, color, -1)

    label = (
        f"conf={region.confidence:.2f}  "
        f"size={region.size}  "
        f"corner={region.corner_ratio:.3f}"
    )
    cv2.putText(
        vis, label,
        (max(0, region.cx - half), max(24, region.cy - half - 10)),
        cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2,
    )

    debug_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(debug_path), vis)
