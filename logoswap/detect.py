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


def find_icon_region_onset(
    video_path: str | Path,
    cx: int,
    cy: int,
    icon_size: int,
    bg_onset: float,
    look_back: float = 1.5,
) -> float:
    """
    Scan forward from ``bg_onset - look_back`` to find the EARLIEST frame
    where the icon region contains an actual rounded-square app icon –
    NOT just random game tiles flying through during a transition.

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
        Returns ``bg_onset`` unchanged if nothing is found.
    """
    import logging
    log = logging.getLogger("logoswap.detect")
    from .probe import extract_single_frame, load_frame_rgb

    with tempfile.TemporaryDirectory(prefix="logoswap_iro_") as _tmp:
        tmp = Path(_tmp)

        step = 0.1   # 100 ms between probes – fast enough, cheap enough
        t_start = max(0.0, bg_onset - look_back)

        # Crop a box ±70 % of icon_size around the expected centre.
        # We pass video_w/h as the *crop* dimensions so size heuristics
        # inside _find_candidates stay calibrated.
        pad = int(icon_size * 0.70)

        t = t_start
        while t < bg_onset + step * 0.5:
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
                # Loose=False: strict solidity (≥0.85) and aspect (≥0.83) –
                # these thresholds reject irregular game-tile shapes.
                candidates = _find_candidates(crop_bgr, cw, ch, loose=False)
                if candidates:
                    log.debug(
                        f"find_icon_region_onset: icon detected at {t:.3f}s "
                        f"(bg_onset={bg_onset:.3f}s, Δ={bg_onset - t:.3f}s)"
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
    min_size = min_dim * (0.08 if loose else 0.12)
    max_size = min_dim * (0.70 if loose else 0.65)
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

            # Size filter
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

            cx = x + w // 2
            cy_ = y + h // 2

            # Score components
            squareness = aspect                                  # 0–1, want 1
            rel_size = min(w, h) / min_dim                      # 0–1
            size_score = max(0.0, 1.0 - abs(rel_size - 0.30) / 0.28)
            border_score = _border_contrast(bgr, x, y, w, h)
            interior_var = np.std(gray[y : y + h, x : x + w]) / 255.0
            int_score = min(1.0, interior_var * 3.0)

            score = (
                squareness * 2.0
                + size_score * 1.5
                + border_score * 1.5
                + int_score * 1.0
                + solidity * 1.0
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
