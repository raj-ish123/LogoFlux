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


def _run_plain(cmd: list, what: str) -> None:
    """Run an ffmpeg helper command (no encode logging), raising on error."""
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg {what} failed:\n{result.stderr.decode(errors='replace')[-2000:]}"
        )


def _x264_args() -> list:
    return [
        "-c:v", "libx264",
        "-crf", _X264_CRF,
        "-preset", _X264_PRESET,
        "-threads", "0",
        "-pix_fmt", "yuv420p",
    ]


# ---------------------------------------------------------------------------
# Encode orchestration: full re-encode  vs  tail-only (copy head + concat)
# ---------------------------------------------------------------------------
# The logo only ever appears near the END of the video (the end-card), so
# re-encoding the whole clip is wasteful.  When the source is h264 (+ aac
# audio) we copy the untouched head with stream-copy and re-encode only the
# short tail that contains the overlay, then concatenate them via MPEG-TS
# (seam-safe for h264).  Falls back to a full encode whenever segmentation is
# not clearly safe.  Disable with LOGOSWAP_FAST_CONCAT=0.

_FAST_CONCAT = os.environ.get("LOGOSWAP_FAST_CONCAT", "1") not in ("0", "false", "False")
_TAIL_MARGIN = 0.5   # start the re-encoded tail this many seconds before overlay
_MIN_HEAD = 5.0      # only segment if the copied head is at least this long


def _seg_cut(video_path, overlay_start: float, has_audio: bool) -> float | None:
    """Return a seam-safe keyframe cut time for tail-only encoding, or None."""
    if not _FAST_CONCAT or overlay_start is None:
        return None
    try:
        from .probe import find_keyframe_before, stream_codecs
        vcodec, acodec = stream_codecs(video_path)
        if vcodec != "h264":
            return None
        if has_audio and acodec != "aac":
            return None
        cut = find_keyframe_before(video_path, overlay_start - _TAIL_MARGIN)
        if cut is None or cut < _MIN_HEAD:
            return None
        return cut
    except Exception:
        return None


def _render(
    video_path,
    output_path: Path,
    overlay_start: float,
    has_audio: bool,
    aux_inputs: list,
    make_filter,
    disable_seg: bool = False,
) -> None:
    """
    Encode the composited video.

    aux_inputs : ffmpeg input args that follow the main video input (image
                 sequence, logo images) — input indices [1], [2], …
    make_filter: callable(t0) -> filter_complex string, where every absolute
                 time reference is shifted by t0 (used when the tail is seeked
                 with -ss so its timeline starts at 0).
    disable_seg: force a full re-encode (used when an overlay spans the head of
                 the video, e.g. a persistent corner logo, so the head cannot
                 be stream-copied).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cut = None if disable_seg else _seg_cut(video_path, overlay_start, has_audio)
    if cut is not None:
        try:
            _render_tail_concat(video_path, output_path, cut, has_audio, aux_inputs, make_filter)
            return
        except Exception as exc:
            log.warning(f"Tail-segment encode failed ({exc}); using full encode.")

    _render_full(video_path, output_path, has_audio, aux_inputs, make_filter)


def _render_full(video_path, output_path: Path, has_audio: bool, aux_inputs: list, make_filter) -> None:
    cmd = ["ffmpeg", "-y", "-i", str(video_path)] + list(aux_inputs)
    cmd += ["-filter_complex", make_filter(0.0), "-map", "[outv]"]
    if has_audio:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    cmd += _x264_args() + ["-movflags", "+faststart", str(output_path)]
    _run_ffmpeg(cmd, output_path)


def _render_tail_concat(video_path, output_path: Path, cut: float, has_audio: bool, aux_inputs: list, make_filter) -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="logoswap_seg_") as td:
        td = Path(td)
        head_ts = td / "head.ts"
        tail_ts = td / "tail.ts"

        # Head: stream-copy [0, cut] → MPEG-TS (annexb so it concatenates cleanly)
        head_cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-t", f"{cut:.4f}",
            "-c", "copy",
            "-bsf:v", "h264_mp4toannexb",
            "-f", "mpegts", str(head_ts),
        ]
        _run_plain(head_cmd, "head copy")

        # Tail: re-encode [cut, end] with the overlay; timeline shifted by cut.
        tail_cmd = ["ffmpeg", "-y", "-ss", f"{cut:.4f}", "-i", str(video_path)] + list(aux_inputs)
        tail_cmd += ["-filter_complex", make_filter(cut), "-map", "[outv]"]
        if has_audio:
            tail_cmd += ["-map", "0:a", "-c:a", "aac", "-b:a", "192k"]
        tail_cmd += _x264_args() + ["-bsf:v", "h264_mp4toannexb", "-f", "mpegts", str(tail_ts)]
        _run_ffmpeg(tail_cmd, output_path)

        # Concat head + tail (both h264/aac in TS) → final mp4
        concat_cmd = [
            "ffmpeg", "-y",
            "-i", f"concat:{head_ts}|{tail_ts}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        if has_audio:
            concat_cmd += ["-bsf:a", "aac_adtstoasc"]
        concat_cmd += ["-movflags", "+faststart", str(output_path)]
        _run_plain(concat_cmd, "concat")
        log.info(f"Tail-only encode: copied head [0,{cut:.2f}s], re-encoded tail → {output_path.name}")


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


def generate_zoom_sequence(
    logo_rgba: Image.Image,
    sizes: list[int],
    centers: list[tuple[int, int]],
    video_w: int,
    video_h: int,
    out_dir: Path,
    cover_margin: float = 0.20,
) -> Path:
    """
    Write one transparent PNG per frame for a *zoom-out* end-card entrance.

    Unlike ``generate_sequence`` (fixed centre, scale curve) this places the
    logo at an explicit per-frame pixel size AND centre so the replacement
    exactly tracks an original logo that starts huge and shrinks to settled.
    Each frame's logo is oversized by ``cover_margin`` to guarantee the
    original is fully hidden underneath.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    for f, (size, (cx, cy)) in enumerate(zip(sizes, centers)):
        px_size = max(1, round(size * (1.0 + cover_margin)))
        scaled = logo_rgba.resize((px_size, px_size), Image.LANCZOS)
        canvas = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
        canvas.paste(scaled, (round(cx - px_size / 2), round(cy - px_size / 2)), scaled)
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
    corner: dict | None = None,
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
    corner        : optional persistent-corner-logo overlay to composite for
                    the whole gameplay portion, dict with keys
                    {path, size, cx, cy, end}.  When present a full re-encode
                    is forced (the overlay spans the video head).
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
        aux_inputs = [
            "-framerate", fps_frac, "-start_number", "0",
            "-i", str(seq_dir / "f%04d.png"),
            "-i", str(logo_rgba_path),
            "-i", str(f0000),
        ]
        next_idx = 4                      # [1]=seq [2]=logo [3]=f0000

        def core_filter(base: str, t0: float) -> str:
            e, s, se = early - t0, ts - t0, tset - t0
            return (
                f"[2:v]scale={logo_size}:{logo_size}[stat];"
                f"[1:v]setpts=PTS+{s:.3f}/TB[anim];"
                f"[{base}][3:v]overlay=0:0:enable='between(t,{e:.3f},{s:.3f})'[pre];"
                f"[pre][anim]overlay=0:0:enable='between(t,{s:.3f},{se:.3f})'[a];"
                f"[a][stat]overlay={ox}:{oy}:enable='gte(t,{se:.3f})'[outv]"
            )
    else:
        aux_inputs = [
            "-framerate", fps_frac, "-start_number", "0",
            "-i", str(seq_dir / "f%04d.png"),
            "-i", str(logo_rgba_path),
        ]
        next_idx = 3                      # [1]=seq [2]=logo

        def core_filter(base: str, t0: float) -> str:
            e, s, se = early - t0, ts - t0, tset - t0
            static_enable = f"gte(t,{e:.3f})*(1-between(t,{s:.3f},{se:.3f}))"
            return (
                f"[2:v]scale={logo_size}:{logo_size}[stat];"
                f"[1:v]setpts=PTS+{s:.3f}/TB[anim];"
                f"[{base}][stat]overlay={ox}:{oy}:enable='{static_enable}'[a];"
                f"[a][anim]overlay=0:0:enable='between(t,{s:.3f},{se:.3f})'[outv]"
            )

    corner_idx = None
    if corner is not None:
        corner_idx = next_idx
        aux_inputs = aux_inputs + ["-i", str(corner["path"])]

    def make_filter(t0: float) -> str:
        if corner_idx is None:
            return core_filter("0:v", t0)
        cs = int(corner["size"])
        cox = round(corner["cx"] - cs / 2)
        coy = round(corner["cy"] - cs / 2)
        cend = corner["end"] - t0
        pre = (
            f"[{corner_idx}:v]scale={cs}:{cs}[cornr];"
            f"[0:v][cornr]overlay={cox}:{coy}:enable='between(t,{-t0:.3f},{cend:.3f})'[base];"
        )
        return pre + core_filter("base", t0)

    _render(
        video_path, output_path, early, has_audio, aux_inputs, make_filter,
        disable_seg=(corner is not None),
    )


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

    aux_inputs = ["-i", str(logo_rgba_path)]

    def make_filter(t0: float) -> str:
        # Linear slide that exactly tracks the whole-scene scroll velocity.
        # Using linear (not ease-out) ensures the replacement always sits at
        # the same y as the original icon — the original is never exposed.
        #
        #   progress = clip((t - onset) / dur, 0, 1)
        #   y        = _start_oy + slide_dist * progress
        #            = final_oy - slide_dist * (1 - progress)
        onset = onset_time - t0
        y_expr = (
            f"{final_oy}-{slide_dist:.1f}*(1-clip((t-{onset:.4f})/{dur:.4f},0,1))"
        )
        return (
            f"[1:v]scale={logo_size}:{logo_size}[logo];"
            f"[0:v][logo]overlay={final_ox}:y='{y_expr}':"
            f"enable='gte(t,{onset:.4f})'[outv]"
        )

    _render(video_path, output_path, onset_time, has_audio, aux_inputs, make_filter)


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
    end_time: float | None = None,
    corner: dict | None = None,
) -> None:
    """
    Overlay the (already-scaled RGBA) logo starting at onset_time with
    NO animation – the logo just appears instantly at settled size.

    When ``end_time`` is given the overlay is only shown in
    [onset_time, end_time] (used for a persistent corner logo that must stop
    before the end-card takes over).  A full re-encode is then forced because
    the overlay spans the video head.

    ``corner`` optionally composites a persistent corner logo (dict with keys
    {path, size, cx, cy, end}) across the gameplay portion, on top of which the
    static end-card logo is drawn.

    Much faster than render_video because no PNG sequence is needed.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ox = round(cx - logo_size / 2)
    oy = round(cy - logo_size / 2)

    aux_inputs = ["-i", str(logo_rgba_path)]
    corner_idx = None
    if corner is not None:
        corner_idx = 2
        aux_inputs = aux_inputs + ["-i", str(corner["path"])]

    def make_filter(t0: float) -> str:
        onset = onset_time - t0
        if end_time is None:
            enable = f"gte(t,{onset:.3f})"
        else:
            enable = f"between(t,{onset:.3f},{end_time - t0:.3f})"
        base = "0:v"
        pre = ""
        if corner_idx is not None:
            cs = int(corner["size"])
            cox = round(corner["cx"] - cs / 2)
            coy = round(corner["cy"] - cs / 2)
            pre = (
                f"[{corner_idx}:v]scale={cs}:{cs}[cornr];"
                f"[0:v][cornr]overlay={cox}:{coy}:"
                f"enable='between(t,{-t0:.3f},{corner['end'] - t0:.3f})'[base];"
            )
            base = "base"
        return (
            pre
            + f"[1:v]scale={logo_size}:{logo_size}[logo];"
            + f"[{base}][logo]overlay={ox}:{oy}:enable='{enable}'[outv]"
        )

    _render(
        video_path, output_path, onset_time, has_audio, aux_inputs, make_filter,
        disable_seg=(end_time is not None or corner is not None),
    )


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
