"""
render.py – Animation-sequence generation and ffmpeg composite encode.

Mirrors the exact rendering technique proven in this project:

  filter_complex:
    [2:v]scale=L:L[stat];
    [1:v]setpts=PTS+TS/TB[anim];
    [0:v][stat]overlay=X:Y:enable='gte(t,TSET)'[a];
    [a][anim]overlay=0:0:enable='between(t,TS,TSET)'[outv]

Where:
  - Input 0 = original video
  - Input 1 = animated PNG sequence (transparent canvases, 1 per curve frame)
  - Input 2 = static rounded-logo PNG (settled position)
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger("logoswap.render")

# ---------------------------------------------------------------------------
# Encoder settings (tunable via environment for constrained servers)
# ---------------------------------------------------------------------------
# `-preset slow` is far too slow on a single-vCPU container (e.g. CAP free
# tier): a 60 s 1080x1920 encode can take 30+ minutes.  Default to the fastest
# preset — for flat marketing videos the quality difference at the same CRF is
# negligible, but it is dramatically faster on weak CPUs.  Override with env
# vars (LOGOSWAP_X264_PRESET / LOGOSWAP_X264_CRF) for higher quality on beefier
# machines, e.g. PRESET=slow CRF=18.
_X264_PRESET = os.environ.get("LOGOSWAP_X264_PRESET", "ultrafast")
_X264_CRF = os.environ.get("LOGOSWAP_X264_CRF", "21")


def _run_ffmpeg(cmd: list, output_path: Path) -> None:
    """Run an ffmpeg command, logging start + elapsed time, raising on error."""
    log.info(f"Encoding {output_path.name} (preset={_X264_PRESET}, crf={_X264_CRF})…")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg encode failed for {output_path.name}:\n"
            f"{result.stderr.decode(errors='replace')[-2500:]}"
        )
    log.info(f"Encoded {output_path.name} in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------
# Sequence generation
# ---------------------------------------------------------------------------

def generate_sequence(
    logo_rgba: Image.Image,
    logo_size: int,
    cx: int,
    cy: int,
    video_w: int,
    video_h: int,
    curve: list[float],
    out_dir: Path,
) -> Path:
    """
    Write one transparent PNG per curve frame into out_dir.

    Each frame is a video_w × video_h RGBA canvas with the logo scaled by
    curve[f] and centred on (cx, cy).  The sequence is numbered f0000.png …

    Returns out_dir.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    for f, scale in enumerate(curve):
        px_size = max(1, round(logo_size * scale))
        scaled = logo_rgba.resize((px_size, px_size), Image.LANCZOS)

        canvas = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
        paste_x = round(cx - px_size / 2)
        paste_y = round(cy - px_size / 2)
        canvas.paste(scaled, (paste_x, paste_y), scaled)
        canvas.save(str(out_dir / f"f{f:04d}.png"))

    return out_dir


# ---------------------------------------------------------------------------
# ffmpeg composite render
# ---------------------------------------------------------------------------

def render_video(
    video_path: str | Path,
    output_path: str | Path,
    logo_rgba_path: str | Path,
    logo_size: int,
    cx: int,
    cy: int,
    ts: float,
    tset: float,
    seq_dir: Path,
    fps_frac: str,
    has_audio: bool,
    onset_time: float | None = None,
) -> None:
    """
    Composite and encode the final video.

    Parameters
    ----------
    video_path    : source video
    output_path   : destination mp4
    logo_rgba_path: pre-built rounded RGBA logo (for the static overlay)
    logo_size     : settled logo size in video pixels
    cx, cy        : icon centre in video pixels
    ts            : pop onset time (start of animation sequence)
    tset          : settle time (static overlay takes over after this)
    seq_dir       : directory containing f0000.png … (animated sequence)
    fps_frac      : framerate fraction string e.g. "30000/1001"
    has_audio     : whether to copy the audio stream
    onset_time    : if provided and < ts, show static logo from onset_time
                    so the replacement appears before the animation begins
                    (prevents any flash of the original icon during transition)
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Effective start of the static logo layer
    early = onset_time if (onset_time is not None and onset_time < ts) else ts

    # Static overlay top-left corner (for settled phase after tset)
    ox = round(cx - logo_size / 2)
    oy = round(cy - logo_size / 2)

    # Pre-roll: from `early` to `ts` we need to show the logo BEFORE the
    # pop animation starts.  Using the full-size logo for this would cause a
    # visible jump because the animation curve starts at a smaller scale
    # (typically 0.5–0.8×).  Instead we reuse the first animation frame
    # (f0000.png) which is already rendered at curve[0] scale and centred on
    # (cx, cy), so the transition from pre-roll into the animated sequence is
    # completely seamless.
    has_preroll = early < ts
    f0000 = seq_dir / "f0000.png"

    if has_preroll and f0000.exists():
        # 4-input filter:
        #   [0] video  [1] anim sequence  [2] static settled logo  [3] f0000 pre-roll
        # Phase timeline:
        #   [early, ts)  → overlay f0000 (pre-roll at starting scale)
        #   [ts, tset]   → overlay animation sequence
        #   (tset, ∞)    → overlay static full-size logo
        filter_complex = (
            f"[2:v]scale={logo_size}:{logo_size}[stat];"
            f"[1:v]setpts=PTS+{ts:.3f}/TB[anim];"
            f"[0:v][3:v]overlay=0:0:enable='between(t,{early:.3f},{ts:.3f})'[pre];"
            f"[pre][anim]overlay=0:0:enable='between(t,{ts:.3f},{tset:.3f})'[a];"
            f"[a][stat]overlay={ox}:{oy}:enable='gte(t,{tset:.3f})'[outv]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-framerate", fps_frac, "-start_number", "0",
            "-i", str(seq_dir / "f%04d.png"),
            "-i", str(logo_rgba_path),
            "-i", str(f0000),
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ]
    else:
        # No pre-roll (or f0000 missing): original 3-input path
        static_enable = (
            f"gte(t,{early:.3f})*(1-between(t,{ts:.3f},{tset:.3f}))"
        )
        filter_complex = (
            f"[2:v]scale={logo_size}:{logo_size}[stat];"
            f"[1:v]setpts=PTS+{ts:.3f}/TB[anim];"
            f"[0:v][stat]overlay={ox}:{oy}:enable='{static_enable}'[a];"
            f"[a][anim]overlay=0:0:enable='between(t,{ts:.3f},{tset:.3f})'[outv]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-framerate", fps_frac, "-start_number", "0",
            "-i", str(seq_dir / "f%04d.png"),
            "-i", str(logo_rgba_path),
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ]

    if has_audio:
        cmd += ["-map", "0:a", "-c:a", "copy"]

    cmd += [
        "-c:v", "libx264",
        "-crf", _X264_CRF,
        "-preset", _X264_PRESET,
        "-threads", "0",
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]

    _run_ffmpeg(cmd, output_path)


# ---------------------------------------------------------------------------
# Slide-in from top  (position animation, no scale change)
# ---------------------------------------------------------------------------

def render_slide_in(
    video_path: str | Path,
    output_path: str | Path,
    logo_rgba_path: str | Path,
    logo_size: int,
    cx: int,
    cy: int,
    onset_time: float,
    dur: float,
    has_audio: bool,
    start_oy: int | None = None,
) -> None:
    """
    Overlay logo with a slide-in-from-above animation.

    The logo slides linearly from start_oy (default: -logo_size = fully
    off-screen) to its settled position (cx, cy) over `dur` seconds, exactly
    tracking the whole-scene scroll velocity so the replacement always covers
    the original icon throughout the transition.

    Parameters
    ----------
    onset_time : float
        When the logo first starts moving (typically = detected end-card onset).
    dur : float
        Total slide duration in seconds (e.g. 0.10–0.14 s for a 2–4 frame slide).
    start_oy : int | None
        Top-left Y of the logo at onset.  When None defaults to -logo_size
        (logo's bottom just at y=0).  Pass the detected value from
        _detect_slide_start_oy() for a pixel-perfect match with the original.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    final_ox = round(cx - logo_size / 2)   # settled top-left X
    final_oy = round(cy - logo_size / 2)   # settled top-left Y

    # Effective start position
    _start_oy = int(start_oy) if start_oy is not None else -logo_size
    slide_dist = final_oy - _start_oy      # total travel in pixels

    # Linear slide that exactly tracks the whole-scene scroll velocity.
    # Using linear (not ease-out) ensures the replacement always sits at the
    # same y as the original icon — the original is never exposed anywhere.
    #
    #   progress = clip((t - onset) / dur, 0, 1)
    #   y        = _start_oy + slide_dist * progress
    #            = final_oy - slide_dist * (1 - progress)
    y_expr = (
        f"{final_oy}-{slide_dist:.1f}*(1-clip((t-{onset_time:.4f})/{dur:.4f},0,1))"
    )

    filter_complex = (
        f"[1:v]scale={logo_size}:{logo_size}[logo];"
        f"[0:v][logo]overlay={final_ox}:y='{y_expr}':"
        f"enable='gte(t,{onset_time:.4f})'[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(logo_rgba_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ]

    if has_audio:
        cmd += ["-map", "0:a", "-c:a", "copy"]

    cmd += [
        "-c:v", "libx264",
        "-crf", _X264_CRF,
        "-preset", _X264_PRESET,
        "-threads", "0",
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]

    _run_ffmpeg(cmd, output_path)


# ---------------------------------------------------------------------------
# Simple static overlay (no animation sequence)
# ---------------------------------------------------------------------------

def render_simple(
    video_path: str | Path,
    output_path: str | Path,
    logo_rgba_path: str | Path,
    logo_size: int,
    cx: int,
    cy: int,
    onset_time: float,
    has_audio: bool,
) -> None:
    """
    Overlay the (already-scaled RGBA) logo starting at onset_time with
    NO animation – the logo just appears instantly at settled size.

    Much faster than render_video because no PNG sequence is needed.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ox = round(cx - logo_size / 2)
    oy = round(cy - logo_size / 2)

    filter_complex = (
        f"[1:v]scale={logo_size}:{logo_size}[logo];"
        f"[0:v][logo]overlay={ox}:{oy}:enable='gte(t,{onset_time:.3f})'[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(logo_rgba_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ]

    if has_audio:
        cmd += ["-map", "0:a", "-c:a", "copy"]

    cmd += [
        "-c:v", "libx264",
        "-crf", _X264_CRF,
        "-preset", _X264_PRESET,
        "-threads", "0",
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]

    _run_ffmpeg(cmd, output_path)


# ---------------------------------------------------------------------------
# Contact-sheet QA
# ---------------------------------------------------------------------------

def save_contact_sheet(
    output_video_path: str | Path,
    ts: float,
    tset: float,
    icon_cx: int,
    icon_cy: int,
    icon_size: int,
    fps_frac: str,
    out_path: Path,
    n_thumbs: int = 16,
) -> None:
    """
    Extract n_thumbs frames spanning [ts-0.1, tset+0.5], crop around the
    icon, and tile them into a contact-sheet PNG.
    """
    import tempfile
    from .probe import extract_frames_at_rate, load_frame_rgb

    start = max(0.0, ts - 0.1)
    duration = (tset + 0.5) - start
    if duration <= 0:
        return

    sheet_fps = n_thumbs / duration

    with tempfile.TemporaryDirectory(prefix="logoswap_cs_") as tmp:
        tmp_path = Path(tmp)
        try:
            frames_paths = extract_frames_at_rate(
                output_video_path, start, duration,
                fps=sheet_fps, out_dir=tmp_path, prefix="cs",
            )
        except Exception:
            return

        if not frames_paths:
            return

        crop_size = int(icon_size * 1.5)
        x0 = max(0, icon_cx - crop_size // 2)
        y0 = max(0, icon_cy - crop_size // 2)
        thumb_px = 160

        thumbs = []
        for fp in frames_paths[:n_thumbs]:
            img = Image.open(fp).convert("RGB")
            w, h = img.size
            x1 = min(w, x0 + crop_size)
            y1 = min(h, y0 + crop_size)
            cropped = img.crop((x0, y0, x1, y1))
            thumbs.append(cropped.resize((thumb_px, thumb_px), Image.LANCZOS))

    if not thumbs:
        return

    n_cols = min(8, len(thumbs))
    n_rows = (len(thumbs) + n_cols - 1) // n_cols
    sheet = Image.new("RGB", (n_cols * thumb_px, n_rows * thumb_px), (25, 25, 25))
    for i, thumb in enumerate(thumbs):
        r, c = divmod(i, n_cols)
        sheet.paste(thumb, (c * thumb_px, r * thumb_px))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(out_path))
