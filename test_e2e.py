"""Quick end-to-end regression test for the quality overhaul."""
import zipfile, tempfile, logging
from pathlib import Path
from logoswap.__main__ import _process_one
from logoswap.probe import probe_video
import argparse

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')

ZIP   = Path(r'c:\Users\ishankraj\Downloads\Video_Logo.zip')
LOGO  = Path(r'c:\Users\ishankraj\Downloads\1000019871.png')

tmpdir = Path(tempfile.mkdtemp(prefix='logoswap_e2e_'))
outdir = tmpdir / 'output'
outdir.mkdir()

# Extract VIDKTJ260817L0012 – the "7s early onset" worst-case video
test_video = None
with zipfile.ZipFile(ZIP) as zf:
    for n in zf.namelist():
        if 'VIDKTJ260817L0012' in n and n.lower().endswith('.mp4'):
            test_video = tmpdir / Path(n).name
            with open(test_video, 'wb') as f:
                f.write(zf.read(n))
            break

assert test_video is not None, "Test video not found in zip"

args = argparse.Namespace(
    margin=0.08, region=None, start=None, settle=None, preview=False,
    contact_sheet=False, keep_temp=False, end_window=8.0, track_window=2.5,
    persistent='auto', verbose=True,
)

result = _process_one(test_video, LOGO, outdir, args, 'crossword_go')
if result:
    in_info  = probe_video(str(test_video))
    out_info = probe_video(str(result))
    print(f'\nSUCCESS: {result.name}')
    print(f'  Input  duration: {in_info["duration"]:.3f}s   size: {test_video.stat().st_size//1024}KB')
    print(f'  Output duration: {out_info["duration"]:.3f}s   size: {result.stat().st_size//1024}KB')
    drift = abs(out_info['duration'] - in_info['duration'])
    print(f'  Duration drift: {drift:.3f}s  {"OK" if drift < 0.1 else "WARNING"}')
else:
    print('\nFAILED: no output generated')

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)
