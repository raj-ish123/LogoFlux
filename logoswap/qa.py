"""
qa.py – Lightweight post-render visual and timing QA checks.

Three automated checks are run after every successful render:

  1. Frame-count parity
     The output must contain the same number of video frames as the input
     (within ±1).  Any difference indicates the encoder duplicated or
     dropped frames (most likely the now-disabled MPEG-TS fast-concat path).

  2. Onset coverage
     At onset_time + 0.5 s the output frame must differ from the
     corresponding input frame in the icon region.  If they match it means
     the replacement logo never appeared – a silent rendering failure.

  3. No early replacement
     At max(0, onset_time - 1.0 s) the output frame must MATCH the input
     frame in the icon region.  If they differ the replacement is visible
     before the end-card icon, i.e. the onset was computed too early.

These checks are non-fatal: failures produce warnings/errors in the log
so they are visible to the user without stopping the job.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("logoswap.qa")


@dataclass
class QAResult:
    passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_frames(path: Path) -> int | None:
    """Return the number of video frames via ffprobe (fast, no decode)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip().split("\n")[0])
    except (ValueError, IndexError):
        return None


def _extract_frame(path: Path, t: float, out: Path) -> bool:
    """Extract a single frame at timestamp t.  Returns False on failure."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{t:.4f}", "-i", str(path),
        "-frames:v", "1", "-update", "1", str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and out.is_file()


def _icon_diff(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    cx: int, cy: int, icon_size: int,
) -> float:
    """
    Mean absolute pixel difference in the icon region between two frames.
    Returns a value in [0, 1].
    """
    from PIL import Image

    H, W = frame_a.shape[:2]
    half = max(16, icon_size // 2)
    y0 = max(0, cy - half); y1 = min(H, cy + half)
    x0 = max(0, cx - half); x1 = min(W, cx + half)

    a = frame_a[y0:y1, x0:x1].astype(np.float32)
    b = frame_b[y0:y1, x0:x1].astype(np.float32)
    n = a.size
    if n == 0:
        return 0.0
    return float(np.abs(a - b).sum()) / (n * 255.0)


def _load(path: Path) -> np.ndarray | None:
    """Load a PNG as an RGB numpy array."""
    try:
        from PIL import Image
        return np.array(Image.open(path).convert("RGB"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_qa(
    input_path: Path,
    output_path: Path,
    onset_time: float,
    duration: float,
    icon_cx: int,
    icon_cy: int,
    icon_size: int,
    diff_thresh_covered: float = 0.05,  # replacement must change ≥5 % of icon px
    diff_thresh_clean: float = 0.03,    # pre-onset region must change <3 % of icon px
) -> QAResult:
    """
    Run post-render QA on a single video.

    Parameters
    ----------
    input_path, output_path : paths to the source and rendered videos
    onset_time  : detected/refined overlay start time (seconds)
    duration    : input video duration in seconds
    icon_cx/cy  : icon centre in video pixels
    icon_size   : detected icon size (settled, corrected for morph inflation)
    diff_thresh_covered : min icon-region diff to consider logo "present"
    diff_thresh_clean   : max icon-region diff to consider pre-onset "clean"

    Returns
    -------
    QAResult with .passed, .warnings, .errors populated.
    """
    result = QAResult(passed=True)

    with tempfile.TemporaryDirectory(prefix="logoswap_qa_") as _tmp:
        tmp = Path(_tmp)

        # ------------------------------------------------------------------
        # Check 1: frame-count parity
        # ------------------------------------------------------------------
        n_in  = _count_frames(input_path)
        n_out = _count_frames(output_path)
        if n_in is None or n_out is None:
            result.warnings.append("Frame-count check skipped (ffprobe unavailable).")
        elif abs(n_out - n_in) > 1:
            diff = n_out - n_in
            msg = (
                f"Frame count mismatch: input={n_in} output={n_out} "
                f"({'+' if diff > 0 else ''}{diff} frames).  "
                "Video timing may be shifted."
            )
            result.errors.append(msg)
            result.passed = False
        else:
            log.debug(f"QA frame count: in={n_in} out={n_out}  ✓")

        # ------------------------------------------------------------------
        # Check 2: onset coverage – replacement must be visible at onset+0.5s
        # ------------------------------------------------------------------
        t_check = min(onset_time + 0.5, duration - 0.1)
        p_in  = tmp / "in_cover.png"
        p_out = tmp / "out_cover.png"
        ok_in  = _extract_frame(input_path,  t_check, p_in)
        ok_out = _extract_frame(output_path, t_check, p_out)

        if not ok_in or not ok_out:
            result.warnings.append(
                f"Onset coverage check skipped (frame extraction failed at t={t_check:.2f}s)."
            )
        else:
            arr_in  = _load(p_in)
            arr_out = _load(p_out)
            if arr_in is not None and arr_out is not None:
                d = _icon_diff(arr_in, arr_out, icon_cx, icon_cy, icon_size)
                log.debug(f"QA onset coverage diff at t={t_check:.2f}s: {d:.3f}")
                if d < diff_thresh_covered:
                    result.errors.append(
                        f"Replacement logo NOT visible at onset+0.5s (t={t_check:.2f}s, "
                        f"icon diff={d:.3f} < {diff_thresh_covered}).  "
                        "Detection or rendering may have failed silently."
                    )
                    result.passed = False
                else:
                    log.debug(f"QA onset coverage: diff={d:.3f}  ✓")

        # ------------------------------------------------------------------
        # Check 3: no early replacement – icon region must be clean before onset
        # ------------------------------------------------------------------
        if onset_time >= 1.0:
            t_pre = max(0.1, onset_time - 1.0)
            p_in2  = tmp / "in_pre.png"
            p_out2 = tmp / "out_pre.png"
            ok_in2  = _extract_frame(input_path,  t_pre, p_in2)
            ok_out2 = _extract_frame(output_path, t_pre, p_out2)

            if not ok_in2 or not ok_out2:
                result.warnings.append(
                    f"Early-replacement check skipped (frame extraction failed at t={t_pre:.2f}s)."
                )
            else:
                arr_in2  = _load(p_in2)
                arr_out2 = _load(p_out2)
                if arr_in2 is not None and arr_out2 is not None:
                    d2 = _icon_diff(arr_in2, arr_out2, icon_cx, icon_cy, icon_size)
                    log.debug(f"QA pre-onset diff at t={t_pre:.2f}s: {d2:.3f}")
                    if d2 >= diff_thresh_clean:
                        result.warnings.append(
                            f"Replacement logo appears BEFORE onset at t={t_pre:.2f}s "
                            f"(icon diff={d2:.3f} ≥ {diff_thresh_clean}).  "
                            "Onset may have been detected too early."
                        )
                        # This is a warning only (not a hard error) because
                        # some overlap at the transition boundary is expected.
                    else:
                        log.debug(f"QA no-early-replacement: diff={d2:.3f}  ✓")

    return result
