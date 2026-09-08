"""
logoswap/__main__.py – CLI entry point and batch orchestrator.

Usage
-----
  python -m logoswap --logo NEW_LOGO.png VIDEO1.mp4 VIDEO2.mp4 ...
  python -m logoswap --logo NEW_LOGO.png --videos-dir ./my_videos/

Run  python -m logoswap --help  for the full option list.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("logoswap")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m logoswap",
        description=(
            "Automatically replace a game app-icon in video end-cards "
            "with a new logo, matching shape, size, and pop-in animation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  # Replace icon in three videos:
  python -m logoswap --logo crossword_go.png video1.mp4 video2.mp4 video3.mp4

  # Process a whole folder, 2 parallel jobs:
  python -m logoswap --logo logo.png --videos-dir ./ads/ --jobs 2

  # Preview detection only (no render), then override if needed:
  python -m logoswap --logo logo.png video.mp4 --preview
  python -m logoswap --logo logo.png video.mp4 --region 400 300 380 380

  # Manual timing override when tracking gives wrong results:
  python -m logoswap --logo logo.png video.mp4 --start 27.30 --settle 27.80
""",
    )

    p.add_argument(
        "videos", nargs="*",
        help="Input video file(s). Can also use --videos-dir.",
    )
    p.add_argument(
        "--logo", required=True,
        help="Path to the replacement logo image (PNG recommended).",
    )
    p.add_argument(
        "--videos-dir", metavar="DIR",
        help="Folder of input videos (*.mp4 *.mov *.avi *.mkv *.webm).",
    )
    p.add_argument(
        "--output-dir", metavar="DIR",
        help=(
            "Output folder. Default: <base_folder>/logoswap_output/ "
            "next to the input videos."
        ),
    )
    p.add_argument(
        "--margin", type=float, default=0.16,
        help="Fractional oversize margin vs detected icon (default 0.16 = 16%%).",
    )
    p.add_argument(
        "--region", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
        help=(
            "Skip auto-detection and use this bounding box for the icon. "
            "Applied to ALL videos; use --preview first to calibrate."
        ),
    )
    p.add_argument(
        "--start", type=float, metavar="T",
        help="Manual pop onset time in seconds (overrides tracking, applies to ALL videos).",
    )
    p.add_argument(
        "--settle", type=float, metavar="T",
        help="Manual settle time in seconds (overrides tracking, applies to ALL videos).",
    )
    p.add_argument(
        "--preview", action="store_true",
        help=(
            "Write detection-overlay PNGs (<name>_detect.png) and exit "
            "without rendering. Use to verify icon detection."
        ),
    )
    p.add_argument(
        "--contact-sheet", action="store_true",
        help=(
            "Save a transition contact sheet (<name>_contact.png) per video "
            "after rendering for quick QA."
        ),
    )
    p.add_argument(
        "--jobs", type=int, default=1,
        help="Number of parallel render jobs (default 1).",
    )
    p.add_argument(
        "--keep-temp", action="store_true",
        help="Keep the temp frame directories (useful for debugging).",
    )
    p.add_argument(
        "--end-window", type=float, default=8.0,
        help="Seconds from the end of the video to scan for the end-card (default 8).",
    )
    p.add_argument(
        "--track-window", type=float, default=2.5,
        help="Seconds after the scene cut to scan for pop-in animation (default 2.5).",
    )
    p.add_argument(
        "--persistent", choices=["auto", "off"], default="auto",
        help=(
            "Detect a logo that stays fixed THROUGHOUT the whole video (a corner "
            "watermark of any shape) and replace it for the entire duration. "
            "'auto' (default) tries this first and falls back to end-card mode; "
            "'off' disables it."
        ),
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging.",
    )
    return p


# ---------------------------------------------------------------------------
# Helper: collect videos
# ---------------------------------------------------------------------------

def _collect_videos(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []

    if args.videos_dir:
        d = Path(args.videos_dir)
        if not d.is_dir():
            log.error(f"--videos-dir is not a directory: {d}")
            sys.exit(1)
        for ext in ("*.mp4", "*.mov", "*.avi", "*.mkv", "*.webm"):
            paths.extend(sorted(d.glob(ext)))

    for v in args.videos or []:
        p = Path(v)
        if not p.is_file():
            log.warning(f"Video not found, skipping: {p}")
        else:
            paths.append(p)

    if not paths:
        log.error(
            "No input videos found. "
            "Pass video paths or use --videos-dir <folder>."
        )
        sys.exit(1)

    return paths


def _output_dir(video_paths: list[Path], args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    bases = {p.parent for p in video_paths}
    base = bases.pop() if len(bases) == 1 else video_paths[0].parent
    return base / "logoswap_output"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_output_name(video_stem: str, logo_stem: str, output_dir: Path) -> str:
    """
    Build an output filename that won't exceed Windows MAX_PATH (260 chars).
    Truncates the video stem if the full path would be too long.
    """
    suffix = f"_{logo_stem}.mp4"
    max_stem = 240 - len(str(output_dir)) - len(suffix) - 1  # 1 for path sep
    max_stem = max(20, max_stem)
    safe = video_stem[:max_stem]
    return safe + suffix


# ---------------------------------------------------------------------------
# Per-video pipeline
# ---------------------------------------------------------------------------

def _check_animation(
    video_path: Path,
    cx: int,
    cy: int,
    settled_size: int,
    onset_time: float,
    fps: float,
) -> bool:
    """
    Return True if the icon has a pop-in animation in the first 0.5 s
    after onset.  Uses contour-based size measurement; if the icon is
    detectably larger than the settled size it must be animating.
    """
    from .probe import extract_frames_at_rate, load_frame_rgb
    import cv2

    check_dur = min(0.5, 1.0)
    try:
        with tempfile.TemporaryDirectory(prefix="logoswap_ac_") as _tmp:
            paths = extract_frames_at_rate(
                video_path, onset_time, check_dur,
                fps=fps, out_dir=Path(_tmp) / "ac", prefix="ac",
            )
            if not paths:
                return False

            half = int(settled_size * 1.20)
            for p in paths[:15]:
                frame = load_frame_rgb(p)
                H, W = frame.shape[:2]
                bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                y0 = max(0, cy - half); y1 = min(H, cy + half)
                x0 = max(0, cx - half); x1 = min(W, cx + half)
                region = gray[y0:y1, x0:x1]
                _, thresh = cv2.threshold(region, 25, 255, cv2.THRESH_BINARY)
                k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
                closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k, iterations=3)
                cnts, _ = cv2.findContours(
                    closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                for cnt in cnts:
                    _, _, w, h = cv2.boundingRect(cnt)
                    if w > settled_size * 1.10 and h > settled_size * 1.10:
                        return True
    except Exception:
        pass
    return False


def _detect_slide_start_oy(
    video_path: Path,
    onset_time: float,
    fps: float,
    cx: int,
    cy: int,
    icon_size: int,
    logo_size: int,
    slide_dur: float,
) -> int:
    """
    Estimate the logo's start_oy (top-left y at onset) for a slide animation.

    Builds a bottom-strip template from the SLIDING ICON itself (sampled just
    after it settles, not from the last video frame), then matches it in early
    frames to detect the icon's y-position and extrapolate back to onset.

    Returns start_oy = cy_onset - logo_size // 2, clamped to valid range.
    Falls back to -logo_size if detection fails.
    """
    import cv2, tempfile as _tmpmod
    from .probe import extract_single_frame, load_frame_rgb

    final_oy = cy - logo_size // 2

    try:
        # Sample a frame just after the icon settles (the ORIGINAL sliding icon,
        # not the last-frame icon which may be a completely different graphic)
        t_icon_settled = onset_time + slide_dur + 0.08
        with _tmpmod.TemporaryDirectory(prefix="logoswap_sst_") as _tt:
            fp = Path(_tt) / "si.png"
            extract_single_frame(video_path, t_icon_settled, fp)
            icon_settled_frame = load_frame_rgb(fp)

        H_video, W_video = icon_settled_frame.shape[:2]

        strip_h = max(20, icon_size // 4)
        icon_half = icon_size // 2
        ty0 = cy + icon_half - strip_h
        ty1 = cy + icon_half
        tx0 = max(0, cx - icon_half + icon_half // 4)
        tx1 = min(W_video, cx + icon_half - icon_half // 4)

        template_rgb = icon_settled_frame[ty0:ty1, tx0:tx1]
        if template_rgb.size == 0 or template_rgb.shape[0] < 4:
            return -logo_size

        template_bgr = cv2.cvtColor(template_rgb, cv2.COLOR_RGB2BGR)

        def _match_at(t: float) -> "int | None":
            with _tmpmod.TemporaryDirectory(prefix="logoswap_soy_") as _t:
                fp = Path(_t) / "f.png"
                extract_single_frame(video_path, t, fp)
                frame = load_frame_rgb(fp)

                search_y_max = min(H_video, cy + 20)
                search_rgb = frame[:search_y_max, tx0:tx1]
                if search_rgb.shape[0] < template_bgr.shape[0]:
                    return None

                search_bgr = cv2.cvtColor(search_rgb, cv2.COLOR_RGB2BGR)
                res = cv2.matchTemplate(search_bgr, template_bgr, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)

                if max_val < 0.40:
                    return None

                match_y = max_loc[1]
                icon_bottom_y = match_y + strip_h
                log.debug(
                    f"  _match_at t={t:.3f}: conf={max_val:.2f} "
                    f"match_y={match_y} icon_bottom_y={icon_bottom_y}"
                )
                return icon_bottom_y

        oy1 = _match_at(onset_time + 1.0 / fps)
        oy2 = _match_at(onset_time + 2.0 / fps)

        if oy1 is not None and oy2 is not None:
            velocity = oy2 - oy1
            onset_icon_bottom = oy1 - velocity
            onset_cy = onset_icon_bottom - icon_size + icon_size // 2
            start_oy = onset_cy - logo_size // 2
            log.debug(
                f"  slide start_oy (2-pt): oy1={oy1} oy2={oy2} vel={velocity} "
                f"onset_cy={onset_cy} → start_oy={start_oy}"
            )
            return max(-logo_size * 2, min(start_oy, final_oy - 1))

        if oy1 is not None:
            settled_bottom = cy + icon_size // 2
            onset_bottom = oy1 - (settled_bottom - oy1) / 3.0
            onset_cy = int(onset_bottom) - icon_size + icon_size // 2
            start_oy = onset_cy - logo_size // 2
            log.debug(
                f"  slide start_oy (1-pt): oy1={oy1} → onset_cy={onset_cy} "
                f"start_oy={start_oy}"
            )
            return max(-logo_size * 2, min(start_oy, final_oy - 1))

    except Exception as exc:
        log.debug(f"  _detect_slide_start_oy failed: {exc}")

    return -logo_size


def _detect_slide_animation(
    video_path: Path,
    cx: int,
    cy: int,
    settled_size: int,
    onset_time: float,
    fps: float,
    settled_frame: "np.ndarray",
) -> tuple[bool, float]:
    """
    Determine whether the icon slides in from above rather than popping in place.

    Extracts 8 frames starting from onset and measures the icon's Y centre
    position in each via template matching against the settled icon crop.
    If the icon centre is significantly ABOVE the settled cy in early frames
    and converges to cy by the end of the window, it's a top-slide animation.

    Returns
    -------
    (is_slide, slide_dur)
        is_slide  : True if the icon descends from above
        slide_dur : estimated duration of the slide in seconds (0 if not a slide)
    """
    from .probe import extract_single_frame, load_frame_rgb
    import tempfile as _tmpmod

    try:
        half = settled_size // 2
        H, W = settled_frame.shape[:2]
        y0 = max(0, cy - half); y1 = min(H, cy + half)
        x0 = max(0, cx - half); x1 = min(W, cx + half)

        def _icon_diff(ta: float, tb: float) -> float:
            """Mean absolute icon-region diff between two timestamps."""
            from PIL import Image
            with tempfile.TemporaryDirectory(prefix="logoswap_sd_") as _t:
                pa = Path(_t) / "a.png"
                pb = Path(_t) / "b.png"
                extract_single_frame(video_path, ta, pa)
                extract_single_frame(video_path, tb, pb)
                fa = np.array(Image.open(pa)).astype(np.float32) / 255.0
                fb = np.array(Image.open(pb)).astype(np.float32) / 255.0
                return float(np.mean(np.abs(fb[y0:y1, x0:x1] - fa[y0:y1, x0:x1])))

        # 1. Check there is meaningful motion at onset (initial diff > 0.10)
        early_diff = _icon_diff(onset_time, onset_time + 1.0 / fps)
        if early_diff < 0.10:
            return False, 0.0

        # 2. Check whether motion has SETTLED by onset + 4 frames.
        #    Slide animations (whole-scene scroll) complete in ~4 frames → diff drops.
        #    Burst/pop animations continue beyond 4 frames → diff stays high.
        slide_dur = max(0.100, 4.0 / fps)
        late_diff = _icon_diff(
            onset_time + slide_dur,
            onset_time + slide_dur + 1.0 / fps,
        )

        # Slide: large motion at onset, settled 4 frames later.
        SETTLED_THRESHOLD = 0.05
        if late_diff >= SETTLED_THRESHOLD:
            return False, 0.0

        # Sanity check: for a real top-slide the end-card background must
        # already be present AT ONSET (the whole scene scrolls into view,
        # so the background is visible from the very first frame of the slide).
        # If the background is absent at onset the "settling" was caused by a
        # scene cut / transition rather than an actual slide animation.
        try:
            from .probe import extract_single_frame as _esf, load_frame_rgb as _lfr
            with tempfile.TemporaryDirectory(prefix="logoswap_sdchk_") as _tc:
                p_onset = Path(_tc) / "onset.png"
                _esf(video_path, onset_time, p_onset)
                frame_at_onset = _lfr(p_onset)
            H2, W2 = frame_at_onset.shape[:2]
            excl = settled_size // 2
            ey0 = max(0, cy - excl); ey1 = min(H2, cy + excl)
            ex0 = max(0, cx - excl); ex1 = min(W2, cx + excl)
            d = np.abs(frame_at_onset.astype(np.float32) - settled_frame.astype(np.float32))
            d[ey0:ey1, ex0:ex1] = 0.0
            n_px = d.size - (ey1 - ey0) * (ex1 - ex0) * 3
            bg_diff_at_onset = float(d.sum()) / (n_px * 255.0) if n_px > 0 else 1.0
            if bg_diff_at_onset > 0.12:
                # End-card background not yet visible → not a true slide
                return False, 0.0
        except Exception:
            pass

        return True, slide_dur

    except Exception:
        return False, 0.0


def _process_one(
    video_path: Path,
    logo_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
    logo_stem: str,
) -> Optional[Path]:
    """Full pipeline for a single video. Returns output path or None on failure."""
    from .probe import probe_video, extract_frames_at_rate, load_frame_rgb
    from .detect import (
        get_settled_frame, detect_icon, find_icon_onset,
        detect_persistent_logo, detect_corner_logo, measure_endcard_zoom,
    )
    from .track import track_pop, FIXED_CURVE, PopResult
    from .logo import build_rounded_logo, save_rounded_logo
    from .render import (
        generate_sequence, generate_zoom_sequence,
        render_video, render_simple, render_slide_in, save_contact_sheet,
    )

    name = video_path.stem

    try:
        # ------------------------------------------------------------------ #
        # 1. Probe
        # ------------------------------------------------------------------ #
        info = probe_video(video_path)
        fps: float = info["fps"]
        fps_frac: str = info["fps_frac"]
        vw: int = info["width"]
        vh: int = info["height"]
        duration: float = info["duration"]
        has_audio: bool = info["has_audio"]
        log.info(
            f"[{name}] {vw}x{vh}  {fps:.3f} fps  "
            f"{duration:.1f}s  audio={has_audio}"
        )

        with tempfile.TemporaryDirectory(prefix=f"logoswap_{name}_") as _tmp:
            tmp = Path(_tmp)

            # -------------------------------------------------------------- #
            # 1b. Persistent-logo mode: if a logo is present THROUGHOUT the
            #     whole video (a fixed corner watermark of any shape), replace
            #     it for the entire duration instead of only at the end-card.
            # -------------------------------------------------------------- #
            persistent_mode = getattr(args, "persistent", "auto")
            manual_override = (args.region is not None) or (args.start is not None)
            if persistent_mode != "off" and not manual_override and not args.preview:
                from .detect import detect_persistent_logo

                log.info(f"[{name}] Checking for a persistent (throughout-video) logo…")
                persist = detect_persistent_logo(video_path, duration, vw, vh)
                if persist is not None:
                    log.info(
                        f"[{name}] Persistent logo found: center=({persist.cx},{persist.cy}) "
                        f"{persist.w}x{persist.h}  conf={persist.confidence:.2f} "
                        f"→ replacing across the whole video"
                    )
                    cover = max(persist.w, persist.h)
                    logo_rgba, logo_size = build_rounded_logo(
                        logo_path,
                        corner_ratio=persist.corner_ratio,
                        settled_size=cover,
                        margin=args.margin,
                    )
                    logo_rgba_path = tmp / "logo_rgba.png"
                    save_rounded_logo(logo_rgba, logo_rgba_path)

                    out_name = _safe_output_name(name, logo_stem, output_dir)
                    output_path = output_dir / out_name
                    output_dir.mkdir(parents=True, exist_ok=True)
                    log.info(
                        f"[{name}] Rendering (persistent overlay, {logo_size}px, "
                        f"full video) → {out_name}"
                    )
                    render_simple(
                        video_path, output_path,
                        logo_rgba_path, logo_size,
                        persist.cx, persist.cy,
                        0.0, has_audio,
                    )
                    size_kb = output_path.stat().st_size // 1024
                    log.info(f"[{name}] Done → {output_path}  ({size_kb} KB)")
                    return output_path
                log.info(f"[{name}] No persistent logo; using end-card detection.")

            # -------------------------------------------------------------- #
            # 2. Get settled frame (last frame – icon guaranteed present)
            # -------------------------------------------------------------- #
            log.info(f"[{name}] Extracting settled frame…")
            settled_frame = get_settled_frame(video_path, duration)

            # -------------------------------------------------------------- #
            # 3. Icon detection in settled frame
            # -------------------------------------------------------------- #
            region_override = tuple(args.region) if args.region else None  # type: ignore[arg-type]

            preview_path: Optional[Path] = (
                (output_dir / f"{name}_detect.png") if args.preview else None
            )
            if preview_path:
                output_dir.mkdir(parents=True, exist_ok=True)

            region = detect_icon(
                settled_frame, vw, vh,
                region_override=region_override,
                debug_path=preview_path,
            )
            log.info(
                f"[{name}] Icon: center=({region.cx},{region.cy})  "
                f"size={region.size}  corner={region.corner_ratio:.3f}  "
                f"conf={region.confidence:.2f}"
            )
            if region.confidence < 0.3 and region_override is None:
                log.warning(
                    f"[{name}] Low detection confidence ({region.confidence:.2f}). "
                    "Run with --preview to inspect, then use --region X Y W H to override."
                )

            if args.preview:
                log.info(f"[{name}] Preview saved → {preview_path}  (skipping render)")
                return None

            # -------------------------------------------------------------- #
            # 4. Find icon onset via binary search
            # -------------------------------------------------------------- #
            if args.start is not None:
                onset_time = float(args.start)
                log.info(f"[{name}] Manual onset: {onset_time:.3f}s")
            else:
                log.info(f"[{name}] Searching for icon onset…")
                onset_time = find_icon_onset(
                    video_path,
                    region.cx, region.cy, region.size,
                    duration,
                    look_back=min(args.end_window, duration * 0.6),
                )
                # Nudge back by 6 frames as a safety buffer so the
                # replacement logo is guaranteed to appear before the
                # original icon becomes visible.  6 frames (~0.2 s at 30 fps)
                # is enough to cover game icons that pop in just before the
                # end-card background transition.
                onset_time = max(0.0, onset_time - 6.0 / fps)
                log.info(f"[{name}] Icon onset: {onset_time:.3f}s (with 6-frame buffer)")

            # -------------------------------------------------------------- #
            # 4b. Zoom-out end-card: logo appears huge and shrinks to settled.
            #     Measure its per-frame size/centre so the replacement tracks
            #     it exactly (a plain scale-pop leaves the original exposed).
            # -------------------------------------------------------------- #
            zoom = None
            if args.start is None and args.settle is None:
                zoom = measure_endcard_zoom(
                    video_path, settled_frame,
                    region.cx, region.cy, region.size,
                    onset_time, fps, vw, vh,
                )
            # The logo's true entrance (huge frame) precedes the background
            # onset; use it so the replacement covers the whole zoom.
            entrance_time = zoom.times[0] if zoom is not None else onset_time

            # -------------------------------------------------------------- #
            # 4c. Hybrid: a small same-brand CORNER watermark present during
            #     gameplay that vanishes at the end-card.  Template-match the
            #     detected icon in the frame corners and, if found, replace it
            #     for [0, entrance] beneath the end-card logo.
            # -------------------------------------------------------------- #
            corner_overlay = None
            if persistent_mode != "off" and not manual_override:
                corner = detect_corner_logo(
                    video_path, settled_frame, region, entrance_time,
                )
                if corner is not None:
                    dist = ((corner.cx - region.cx) ** 2 +
                            (corner.cy - region.cy) ** 2) ** 0.5
                    if dist > region.size * 0.6:
                        cover = max(corner.w, corner.h)
                        c_rgba, c_size = build_rounded_logo(
                            logo_path, corner_ratio=corner.corner_ratio,
                            settled_size=cover, margin=args.margin,
                        )
                        c_path = tmp / "corner_logo.png"
                        save_rounded_logo(c_rgba, c_path)
                        corner_overlay = {
                            "path": str(c_path), "size": c_size,
                            "cx": corner.cx, "cy": corner.cy,
                            "end": entrance_time,
                        }
                        log.info(
                            f"[{name}] Hybrid corner logo: center=({corner.cx},"
                            f"{corner.cy}) {corner.w}x{corner.h} "
                            f"conf={corner.confidence:.2f} → replacing during "
                            f"gameplay [0,{entrance_time:.2f}s]"
                        )

            # -------------------------------------------------------------- #
            # 5. Detect animation type: slide-from-top OR scale-pop OR static
            # -------------------------------------------------------------- #
            # Priority: slide detection first (more specific), then scale pop,
            # then static (no animation).

            is_slide, slide_dur = False, 0.0
            has_anim = False
            if zoom is None:
                is_slide, slide_dur = _detect_slide_animation(
                    video_path, region.cx, region.cy, region.size,
                    onset_time, fps, settled_frame,
                )
                log.info(f"[{name}] Slide-from-top: {is_slide}" +
                         (f"  dur={slide_dur:.3f}s" if is_slide else ""))

                if not is_slide:
                    has_anim = _check_animation(
                        video_path, region.cx, region.cy, region.size,
                        onset_time, fps,
                    )
                    log.info(f"[{name}] Pop animation detected: {has_anim}")

            # -------------------------------------------------------------- #
            # 6. Build logo
            # -------------------------------------------------------------- #
            logo_rgba, logo_size = build_rounded_logo(
                logo_path,
                corner_ratio=region.corner_ratio,
                settled_size=region.size,
                margin=args.margin,
            )
            logo_rgba_path = tmp / "logo_rgba.png"
            save_rounded_logo(logo_rgba, logo_rgba_path)
            log.info(
                f"[{name}] Logo: {logo_size}px  "
                f"corner_radius~{region.corner_ratio * logo_size:.1f}px"
            )

            # -------------------------------------------------------------- #
            # 7. Render
            # -------------------------------------------------------------- #
            out_name = _safe_output_name(name, logo_stem, output_dir)
            output_path = output_dir / out_name
            output_dir.mkdir(parents=True, exist_ok=True)

            if zoom is not None:
                # -- Zoom-out render: replacement tracks the shrinking logo ──
                seq_dir = tmp / "zseq"
                generate_zoom_sequence(
                    logo_rgba, zoom.sizes, zoom.centers, vw, vh, seq_dir,
                )
                log.info(
                    f"[{name}] Rendering (zoom-out, {len(zoom.sizes)} frames, "
                    f"{entrance_time:.3f}→{zoom.settle_time:.3f}s) → {out_name}"
                )
                render_video(
                    video_path, output_path,
                    logo_rgba_path, logo_size,
                    region.cx, region.cy,
                    entrance_time, zoom.settle_time,
                    seq_dir, fps_frac, has_audio,
                    onset_time=entrance_time,
                    corner=corner_overlay,
                )
            elif is_slide:
                # -- Slide-from-top render ─────────────────────────────────
                # Duration: match original (measured from frame data; cap at 0.25s)
                # Apply a 2% speed reduction so the animation feels a touch smoother.
                actual_dur = max(0.067, min(slide_dur, 0.25)) * 1.02

                # Detect where the original icon actually is at onset so the
                # replacement slide exactly tracks it (no original exposed).
                slide_start_oy = _detect_slide_start_oy(
                    video_path, onset_time, fps,
                    region.cx, region.cy, region.size, logo_size,
                    slide_dur,
                )
                log.info(
                    f"[{name}] Rendering (slide-in from top, "
                    f"dur={actual_dur:.3f}s at {onset_time:.3f}s"
                    f"  start_oy={slide_start_oy}) → {out_name}"
                )
                slide_src = video_path
                if corner_overlay is not None:
                    inter = tmp / "corner_inter.mp4"
                    render_simple(
                        video_path, inter,
                        corner_overlay["path"], corner_overlay["size"],
                        corner_overlay["cx"], corner_overlay["cy"],
                        0.0, has_audio, end_time=onset_time,
                    )
                    slide_src = inter
                render_slide_in(
                    slide_src, output_path,
                    logo_rgba_path, logo_size,
                    region.cx, region.cy,
                    onset_time, actual_dur, has_audio,
                    start_oy=slide_start_oy,
                )
            elif has_anim:
                # -- Animated render: track the pop curve ──────────────────
                tr_dur = min(args.track_window, duration - onset_time)
                if tr_dur >= 0.1:
                    tr_paths = extract_frames_at_rate(
                        video_path, onset_time, tr_dur,
                        fps=fps, out_dir=tmp / "tr", prefix="tr",
                    )
                    tr_frames = [load_frame_rgb(p) for p in tr_paths]
                    tr_times = [onset_time + i / fps for i in range(len(tr_frames))]
                else:
                    tr_frames = []
                    tr_times = [onset_time]

                pop = track_pop(tr_frames, tr_times, region, settled_frame)
                if args.settle is not None:
                    pop = PopResult(
                        ts=onset_time, tset=float(args.settle),
                        curve=pop.curve, tracking_ok=pop.tracking_ok,
                    )
                status = "OK" if pop.tracking_ok else "FALLBACK"
                log.info(
                    f"[{name}] Pop tracking {status}: "
                    f"TS={pop.ts:.3f}  TSET={pop.tset:.3f}  "
                    f"frames={len(pop.curve)}"
                )

                seq_dir = tmp / "seq"
                generate_sequence(
                    logo_rgba, logo_size,
                    region.cx, region.cy,
                    vw, vh, pop.curve, seq_dir,
                )
                log.info(f"[{name}] Rendering (animated, {len(pop.curve)} frames) → {out_name}")
                render_video(
                    video_path, output_path,
                    logo_rgba_path, logo_size,
                    region.cx, region.cy,
                    pop.ts, pop.tset,
                    seq_dir, fps_frac, has_audio,
                    onset_time=onset_time,
                    corner=corner_overlay,
                )
            else:
                # -- Static render: logo appears instantly at onset ─────────
                log.info(f"[{name}] Rendering (static overlay at {onset_time:.3f}s) → {out_name}")
                render_simple(
                    video_path, output_path,
                    logo_rgba_path, logo_size,
                    region.cx, region.cy,
                    onset_time, has_audio,
                    corner=corner_overlay,
                )

            size_kb = output_path.stat().st_size // 1024
            log.info(f"[{name}] Done → {output_path}  ({size_kb} KB)")

            # -------------------------------------------------------------- #
            # 8. Contact sheet (optional)
            # -------------------------------------------------------------- #
            if args.contact_sheet:
                cs_path = output_dir / f"{name}_contact.png"
                ts_cs = onset_time
                tset_cs = onset_time + (
                    slide_dur if is_slide else
                    ((pop.tset - pop.ts) if has_anim else 0.5)
                )
                save_contact_sheet(
                    output_path,
                    ts_cs, tset_cs,
                    region.cx, region.cy, logo_size,
                    fps_frac, cs_path,
                )
                log.info(f"[{name}] Contact sheet → {cs_path}")

            # -------------------------------------------------------------- #
            # 9. Optionally preserve temp directory
            # -------------------------------------------------------------- #
            if args.keep_temp:
                keep = output_dir / f"{name}_temp"
                if keep.exists():
                    shutil.rmtree(keep)
                shutil.copytree(_tmp, str(keep))
                log.info(f"[{name}] Temp frames kept → {keep}")

        return output_path

    except Exception as exc:
        log.error(f"[{name}] FAILED: {exc}", exc_info=args.verbose)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
    )

    logo_path = Path(args.logo)
    if not logo_path.is_file():
        log.error(f"Logo file not found: {logo_path}")
        sys.exit(1)

    video_paths = _collect_videos(args)
    output_dir = _output_dir(video_paths, args)
    logo_stem = logo_path.stem

    n = len(video_paths)
    log.info(f"logoswap: {n} video(s)  logo={logo_path.name}  output={output_dir}")

    results: dict[Path, Optional[Path]] = {}

    if args.jobs > 1 and n > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(_process_one, vp, logo_path, output_dir, args, logo_stem): vp
                for vp in video_paths
            }
            for fut in as_completed(futures):
                vp = futures[fut]
                try:
                    results[vp] = fut.result()
                except Exception as e:
                    log.error(f"[{vp.name}] Unexpected error: {e}", exc_info=args.verbose)
                    results[vp] = None
    else:
        for vp in video_paths:
            results[vp] = _process_one(vp, logo_path, output_dir, args, logo_stem)

    # Summary
    ok = [v for v in results.values() if v is not None]
    failed = [k for k, v in results.items() if v is None and not args.preview]

    log.info("─" * 55)
    log.info(f"Finished: {len(ok)}/{n} rendered successfully")
    for p in ok:
        log.info(f"  ✓  {p}")
    for p in failed:
        log.warning(f"  ✗  {p.name}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
