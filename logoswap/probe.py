"""
probe.py – Video metadata (ffprobe) and frame-extraction helpers.
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def probe_video(path: str | Path) -> dict:
    """
    Return a dict with keys:
      fps (float), width (int), height (int), duration (float),
      has_audio (bool), fps_frac (str e.g. "30000/1001")
    Raises RuntimeError if ffprobe fails.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {Path(path).name}:\n{result.stderr.strip()}"
        )

    data = json.loads(result.stdout)

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise ValueError(f"No video stream found in {path}")

    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )

    # Parse fps as a fraction for ffmpeg -framerate
    fps_str = video_stream.get("r_frame_rate", "30/1")
    n_str, d_str = fps_str.split("/")
    fps = float(n_str) / float(d_str)

    fps_frac = str(Fraction(fps).limit_denominator(1001))

    return {
        "fps": fps,
        "fps_frac": fps_frac,
        "width": int(video_stream["width"]),
        "height": int(video_stream["height"]),
        "duration": float(data["format"]["duration"]),
        "has_audio": audio_stream is not None,
        "vcodec": video_stream.get("codec_name"),
        "acodec": audio_stream.get("codec_name") if audio_stream else None,
    }


def stream_codecs(path: str | Path) -> tuple[str | None, str | None]:
    """Return (video_codec_name, audio_codec_name|None) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return (None, None)
    data = json.loads(result.stdout or "{}")
    vcodec = acodec = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and vcodec is None:
            vcodec = s.get("codec_name")
        elif s.get("codec_type") == "audio" and acodec is None:
            acodec = s.get("codec_name")
    return (vcodec, acodec)


def find_keyframe_before(path: str | Path, t: float) -> float | None:
    """
    Return the largest video keyframe presentation time <= t, or None.

    Reads packet flags only (no decode), so it is fast even for long videos.
    Used to pick a seam-safe cut point for segmented (tail-only) encoding.
    """
    if t is None or t <= 0:
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=print_section=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None

    best: float | None = None
    for line in result.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        pts_str, flags = parts[0], parts[1]
        if "K" not in flags:          # not a keyframe packet
            continue
        try:
            pts = float(pts_str)
        except ValueError:            # pts_time can be "N/A"
            continue
        if pts <= t and (best is None or pts > best):
            best = pts
    return best


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def extract_frames_at_rate(
    video_path: str | Path,
    start: float,
    duration: float,
    fps: float,
    out_dir: Path,
    prefix: str = "f",
) -> list[Path]:
    """
    Extract frames in [start, start+duration] at the given fps into out_dir.
    Returns a sorted list of written PNG paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / f"{prefix}%05d.png")

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.4f}",
        "-i", str(video_path),
        "-t", f"{duration:.4f}",
        "-vf", f"fps={fps:.6f}",
        "-q:v", "2",
        pattern,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(
            f"Frame extraction failed [{Path(video_path).name}]:\n{stderr[-1500:]}"
        )

    return sorted(out_dir.glob(f"{prefix}*.png"))


def extract_single_frame(
    video_path: str | Path,
    timestamp: float,
    out_path: Path,
) -> Path:
    """Extract one frame at the given timestamp."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.4f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-update", "1",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Single-frame extraction failed: "
            f"{result.stderr.decode(errors='replace')[-800:]}"
        )
    return out_path


# ---------------------------------------------------------------------------
# Frame loading
# ---------------------------------------------------------------------------

def load_frame_rgb(path: Path) -> np.ndarray:
    """Load a PNG/JPG frame as an (H, W, 3) uint8 RGB numpy array."""
    return np.array(Image.open(path).convert("RGB"))


def load_frame_bgr(path: Path) -> np.ndarray:
    """Load a frame as BGR (for OpenCV)."""
    import cv2
    img = cv2.imread(str(path))
    if img is None:
        raise IOError(f"Could not read frame: {path}")
    return img
