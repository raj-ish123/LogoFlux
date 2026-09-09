"""
track.py – Pop-animation tracking.

Given the settled icon region and a window of frames extracted at full fps
immediately after the scene cut, this module:
  1. Estimates the icon's current size in each frame using multi-scale
     template matching against the settled-frame crop.
  2. Finds onset (TS) – first frame where the icon is reliably detectable.
  3. Finds settle (TSET) – first frame where the scale has stabilised near 1.0.
  4. Returns the per-frame scale curve for use by render.py.

Fallback: if tracking confidence is too low the well-known fixed pop curve
measured from this project is used and a warning is logged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("logoswap.track")


def _make_fixed_curve() -> list[float]:
    """
    Build the standard pop-in curve used as fallback when tracking fails.

    Shape: icon bursts in at peak scale (1.53×), holds briefly, then eases
    out to settled (1.0×) via a cubic ease-out over ~37 frames (~1.23 s at
    30 fps).  This matches the "burst" animation style seen in game ads.
    """
    peak = 1.53
    hold = 9    # frames held at peak before easing
    ease = 28   # frames to ease from peak to 1.0

    curve: list[float] = [peak] * hold
    for i in range(1, ease + 1):
        t = i / ease
        eased = 1.0 - (1.0 - t) ** 3          # cubic ease-out
        curve.append(round(peak + (1.0 - peak) * eased, 3))
    curve[-1] = 1.0
    return curve


def _make_ease_in_curve(n_frames: int = 5) -> list[float]:
    """
    Short ease-in curve for cases where the tracker detects no burst.

    The replacement icon ramps from 0.80× to 1.0× using a cubic ease-out
    over `n_frames` frames, matching the ~3-5 frame gentle pop-in that
    videos typically use when the icon simply 'appears' on an end-card.
    """
    curve: list[float] = []
    for i in range(n_frames):
        t = (i + 1) / n_frames           # 0 < t <= 1
        eased = 1.0 - (1.0 - t) ** 3    # cubic ease-out: fast then slow
        scale = round(0.80 + 0.20 * eased, 3)
        curve.append(scale)
    curve[-1] = 1.0
    return curve


FIXED_CURVE: list[float] = _make_fixed_curve()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PopResult:
    ts: float               # pop onset timestamp
    tset: float             # settle timestamp (animation done)
    curve: list[float]      # normalised scale per frame (1.0 = settled size)
    tracking_ok: bool       # False → fixed-curve fallback was used


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def track_pop(
    frames_rgb: list[np.ndarray],
    frame_times: list[float],
    region,                          # IconRegion
    settled_frame_rgb: np.ndarray,
    conf_threshold: float = 0.35,
    stable_window: int = 4,          # frames that must look settled to call TSET
) -> PopResult:
    """
    Derive the pop-in animation timing and scale curve.

    Parameters
    ----------
    frames_rgb : list of (H,W,3) RGB arrays, chronological
        Frames from [end_card_time, end_card_time + track_window].
    frame_times : list of float
        Corresponding timestamps.
    region : IconRegion
        Settled icon region (centre + size) from detect.detect_icon().
    settled_frame_rgb : np.ndarray
        The stable end-card frame used to build the matching template.
    """
    if len(frames_rgb) < 3:
        log.warning("Track window too short; using fixed curve.")
        return _fixed_fallback(frame_times)

    # Build template from settled frame (used as fallback to contour method)
    template_gray = _crop_template(settled_frame_rgb, region)
    if template_gray is None:
        log.warning("Could not crop template from settled frame; using fixed curve.")
        return _fixed_fallback(frame_times)

    settled_size = region.size
    H, W = settled_frame_rgb.shape[:2]

    # ── Per-frame scale estimation ──────────────────────────────────────────
    # Primary:  contour-based measurement – robust against burst decorations
    #           (flying flowers/fruits etc.) that confuse template matching.
    # Fallback: multi-scale template matching when background isn't dark
    #           enough for reliable contour thresholding.
    scales: list[float] = []
    confs: list[float] = []
    for frm in frames_rgb:
        s_c, c_c = _estimate_scale_contour(frm, settled_size, region.cx, region.cy, W, H)
        if c_c >= 0.55:
            scales.append(s_c)
            confs.append(c_c)
        else:
            s_t, c_t = _estimate_scale(frm, template_gray, region.cx, region.cy, settled_size)
            scales.append(s_t)
            confs.append(c_t)

    scales_arr = np.array(scales, dtype=np.float64)
    confs_arr = np.array(confs, dtype=np.float64)

    # ---- Find onset: first run of frames with STRONG confidence ----
    # Using a higher threshold than the general quality check so that low-
    # confidence template-matching noise (from wrong icons / partial overlaps)
    # doesn't trigger onset prematurely.
    ONSET_CONF = max(conf_threshold, 0.55)
    onset_idx = 0
    for i in range(len(confs_arr)):
        window_end = min(i + 3, len(confs_arr))
        if np.mean(confs_arr[i:window_end]) >= ONSET_CONF:
            onset_idx = i
            break
    else:
        log.warning("No reliable onset found; using fixed curve.")
        return _fixed_fallback(frame_times)

    # ---- Find settle: first run of stable_window frames near scale 1.0 ----
    settle_idx = len(frames_rgb) - 1
    for i in range(onset_idx, len(scales_arr) - stable_window + 1):
        window = scales_arr[i : i + stable_window]
        if np.all(np.abs(window - 1.0) < 0.06) and np.mean(confs_arr[i : i + stable_window]) >= conf_threshold:
            settle_idx = i + stable_window - 1
            break

    ts = frame_times[onset_idx]
    tset = frame_times[settle_idx]

    # ts should not precede end_card_time; add a small buffer before onset
    ts = max(frame_times[0], ts - _frame_dt(frame_times) * 2)
    tset = tset + _frame_dt(frame_times)  # one frame past last animated frame

    # Build curve from onset to settle (inclusive)
    raw_curve = scales_arr[onset_idx : settle_idx + 1].tolist()
    if len(raw_curve) < 2:
        log.warning("Curve too short; using fixed curve.")
        return _fixed_fallback(frame_times)

    raw_curve = _smooth(raw_curve, window=3)
    raw_curve = [max(0.88, min(1.65, s)) for s in raw_curve]
    raw_curve[-1] = 1.0  # ensure clean end

    mean_conf = float(np.mean(confs_arr[onset_idx : settle_idx + 1]))
    tracking_ok = mean_conf >= conf_threshold

    if not tracking_ok:
        log.warning(
            f"Low tracking confidence ({mean_conf:.2f}); "
            "injecting fixed curve but keeping detected timing."
        )
        raw_curve = FIXED_CURVE.copy()
    elif max(raw_curve) < 1.10:
        # No meaningful burst detected – use a short ease-in curve instead of
        # jumping instantly to full size.
        n = max(4, len(raw_curve))
        log.warning(
            f"Curve has no burst (max={max(raw_curve):.2f}); "
            f"injecting ease-in curve ({n} frames)."
        )
        raw_curve = _make_ease_in_curve(n)

    log.debug(
        f"track_pop: onset={ts:.3f}s settle={tset:.3f}s "
        f"frames={len(raw_curve)} conf={mean_conf:.2f} ok={tracking_ok}"
    )

    return PopResult(ts=ts, tset=tset, curve=raw_curve, tracking_ok=tracking_ok)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _crop_template(
    frame_rgb: np.ndarray, region
) -> Optional[np.ndarray]:
    """Crop the settled icon into a grayscale template for matchTemplate."""
    half = region.size // 2
    H, W = frame_rgb.shape[:2]
    x0 = max(0, region.cx - half)
    y0 = max(0, region.cy - half)
    x1 = min(W, region.cx + half)
    y1 = min(H, region.cy + half)
    if x1 <= x0 or y1 <= y0:
        return None
    crop = frame_rgb[y0:y1, x0:x1]
    return cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)


def _estimate_scale_contour(
    frame_rgb: np.ndarray,
    settled_size: int,
    cx: int,
    cy: int,
    frame_w: int,
    frame_h: int,
    bg_threshold: int = 28,
) -> tuple[float, float]:
    """
    Estimate icon scale by finding its bounding contour on a dark background.

    Works best after the scene has faded to black.  Returns (scale, confidence)
    where confidence ≥ 0.55 is considered reliable.
    """
    bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    half = int(settled_size * 1.15)
    x0 = max(0, cx - half); y0 = max(0, cy - half)
    x1 = min(frame_w, cx + half); y1 = min(frame_h, cy + half)
    region = gray[y0:y1, x0:x1]

    # Check if background is dark enough for reliable contour isolation.
    # When still bright (early burst / gameplay transition), the icon is at its
    # peak scale – return that assumption at medium confidence so the onset
    # detection triggers and the curve starts at the correct peak value.
    background_dark = float(np.percentile(region, 15)) < 50
    if not background_dark:
        # Use the FIXED_CURVE peak as the assumed scale for bright-background frames.
        return FIXED_CURVE[0], 0.50

    _, thresh = cv2.threshold(region, bg_threshold, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k, iterations=3)

    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best: Optional[tuple[int, int, float]] = None   # (w, h, asp)
    min_sz = settled_size * 0.65
    max_sz = settled_size * 2.10   # allow up to 2.1× settled for burst animations

    for cnt in cnts:
        x, y, w, h = cv2.boundingRect(cnt)
        if not (min_sz < w < max_sz and min_sz < h < max_sz):
            continue
        asp = min(w, h) / max(w, h)
        if asp < 0.72:
            continue

        # Require the contour to be centred near the expected icon location.
        # This rejects other objects (e.g. a burst-phase icon at a different
        # screen position) that share a similar size.
        abs_cx_cnt = x0 + x + w // 2
        abs_cy_cnt = y0 + y + h // 2
        pos_tol = int(settled_size * 0.12)   # allow ±12 % positional drift
        if abs(abs_cx_cnt - cx) > pos_tol or abs(abs_cy_cnt - cy) > pos_tol:
            continue

        if best is None or asp > best[2]:
            best = (w, h, asp)

    if best is None:
        return 1.0, 0.0

    w, h, asp = best
    scale = ((w + h) / 2) / settled_size
    scale = max(0.85, min(1.70, scale))
    conf = min(0.92, 0.35 + asp * 0.60)   # perfect square → 0.95
    return scale, conf


def _estimate_scale(
    frame_rgb: np.ndarray,
    template_gray: np.ndarray,
    cx: int,
    cy: int,
    settled_size: int,
    scale_lo: float = 0.85,
    scale_hi: float = 1.60,
    n_scales: int = 20,
) -> tuple[float, float]:
    """
    Multi-scale template matching to estimate the icon's current scale.
    Returns (scale, confidence) where scale = current_size / settled_size.
    """
    frame_gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
    H, W = frame_gray.shape

    # Search window centred on expected icon location
    search_half = settled_size  # ±1× settled_size gives 2× window
    sx0 = max(0, cx - search_half)
    sy0 = max(0, cy - search_half)
    sx1 = min(W, cx + search_half)
    sy1 = min(H, cy + search_half)
    search = frame_gray[sy0:sy1, sx0:sx1]

    best_score = -1.0
    best_scale = 1.0

    for s in np.linspace(scale_lo, scale_hi, n_scales):
        target_px = int(round(settled_size * s))
        if target_px < 20:
            continue
        if target_px >= search.shape[0] or target_px >= search.shape[1]:
            continue

        try:
            tmpl = cv2.resize(template_gray, (target_px, target_px))
        except Exception:
            continue

        result = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)

        if max_val > best_score:
            best_score = max_val
            best_scale = s

    return best_scale, max(0.0, best_score)


def _smooth(values: list[float], window: int = 3) -> list[float]:
    """Simple symmetric moving-average."""
    if len(values) <= window:
        return list(values)
    half = window // 2
    out = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(float(np.mean(values[lo:hi])))
    return out


def _frame_dt(frame_times: list[float]) -> float:
    """Approximate inter-frame duration."""
    if len(frame_times) >= 2:
        return abs(frame_times[1] - frame_times[0])
    return 1 / 30.0


def _fixed_fallback(frame_times: list[float]) -> PopResult:
    """Return a PopResult using the hard-coded fixed curve."""
    ts = frame_times[0] if frame_times else 0.0
    n = len(FIXED_CURVE)
    dt = _frame_dt(frame_times)
    tset = ts + n * dt
    return PopResult(ts=ts, tset=tset, curve=FIXED_CURVE.copy(), tracking_ok=False)
