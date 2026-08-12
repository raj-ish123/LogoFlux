# LogoFlux

**Automated game icon replacement for marketing videos.**

LogoFlux detects and replaces the app icon in the end-card of a video — matching the original pop-in or slide-in animation — with your new logo, producing a clean, frame-perfect output.

---

## Features

- Auto-detects the icon region and its animation (pop or slide-from-top)
- Tracks the original animation curve and replicates it with the new logo
- Processes multiple videos in one batch
- Real-time progress and log stream in the browser UI
- Preview mode — detect the icon without rendering, to verify placement first
- Optional QA contact sheet per video

---

## Quick start

### 1. Install dependencies

```bash
pip install flask numpy Pillow opencv-python
```

You also need **ffmpeg** and **ffprobe** on your `PATH`:
- Windows: https://www.gyan.dev/ffmpeg/builds/ (add `bin/` folder to PATH)
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

### 2. Run the app

```bash
python logoswap_app.py
```

The browser opens automatically at **http://localhost:5000**

### 3. Use the UI

1. Drop one or more **videos** into the left zone
2. Drop your **new logo** (PNG with transparent background recommended) into the right zone
3. Click **Run logoswap**
4. Download the processed videos when done

---

## Advanced settings

| Setting | Default | Description |
|---|---|---|
| Size margin | 16% | How much larger the new logo is vs the detected icon (ensures full coverage) |
| Region override | auto | Manually specify icon area as `X Y W H` (pixels). Use Preview mode first to measure. |
| Pop onset (TS) | auto | Force the animation start time in seconds |
| Settle time (TSET) | auto | Force the animation end time in seconds |
| End-card scan window | 8 s | How many seconds from the end to scan for the end-card |
| Pop tracking window | 2.5 s | How many seconds after the cut to track the animation |
| Preview only | off | Saves a detection PNG instead of rendering the full video |
| Contact sheet | off | Saves a QA strip of key frames alongside the output |

---

## Project layout

```
logoswap_app.py      ← single-file launcher (share this + logoswap/)
logoswap/
  __main__.py        ← CLI entry point & pipeline orchestrator
  detect.py          ← end-card and icon region detection
  track.py           ← animation curve tracking
  render.py          ← FFmpeg rendering (pop & slide)
  probe.py           ← ffprobe / ffmpeg helpers
  logo.py            ← logo loading & pre-processing
requirements.txt
```

---

## Requirements

- Python 3.9+
- ffmpeg ≥ 4.4
- flask, numpy, Pillow, opencv-python
