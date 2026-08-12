"""
logo.py – Load the replacement logo and apply a rounded-rectangle alpha mask.

Key design principles inherited from this project:
  - NO flood-fill: flood-fill punches holes through interior black pixels.
  - Explicit rounded-rect mask based on corner_ratio from the detected old icon.
  - Logo sized at settled_size × (1 + margin) so it covers the old icon at
    every frame of the pop animation (peak is always larger than settled).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_rounded_logo(
    logo_path: str | Path,
    corner_ratio: float,
    settled_size: int,
    margin: float = 0.08,
) -> tuple[Image.Image, int]:
    """
    Load the logo, apply a rounded-rect alpha mask, and size it to cover
    the old icon completely.

    Parameters
    ----------
    logo_path : path to logo image (PNG/JPG)
    corner_ratio : corner_radius / settled_size measured from the old icon
    settled_size : detected old-icon size in pixels (square)
    margin : fractional oversize margin (default 0.08 = 8 %)

    Returns
    -------
    logo_rgba : PIL Image in RGBA mode, source resolution
    logo_size  : target render size in video pixels
    """
    logo_size = int(round(settled_size * (1.0 + margin)))

    img = Image.open(logo_path).convert("RGB")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]

    # Full-opacity alpha channel
    alpha = np.full((h, w), 255, dtype=np.uint8)

    # Compute the corner-arc radius on the source image.
    # We want: effective_r / logo_size = corner_ratio
    # effective_r = R_src * (logo_size / w)
    # => R_src = corner_ratio * logo_size * w / logo_size = corner_ratio * w
    R = int(round(corner_ratio * w))
    R = max(2, min(R, w // 4))   # clamp to sane range

    # Build the rounded-rect mask via vectorised corner tests
    yy, xx = np.mgrid[0:h, 0:w]

    # Top-left corner
    tl_mask = (xx < R) & (yy < R) & ((xx - R) ** 2 + (yy - R) ** 2 > R * R)
    # Top-right
    tr_mask = (xx > w - 1 - R) & (yy < R) & ((xx - (w - 1 - R)) ** 2 + (yy - R) ** 2 > R * R)
    # Bottom-left
    bl_mask = (xx < R) & (yy > h - 1 - R) & ((xx - R) ** 2 + (yy - (h - 1 - R)) ** 2 > R * R)
    # Bottom-right
    br_mask = (xx > w - 1 - R) & (yy > h - 1 - R) & (
        (xx - (w - 1 - R)) ** 2 + (yy - (h - 1 - R)) ** 2 > R * R
    )

    outside = tl_mask | tr_mask | bl_mask | br_mask
    alpha[outside] = 0

    rgba = np.dstack([arr, alpha])
    return Image.fromarray(rgba, "RGBA"), logo_size


def save_rounded_logo(logo_rgba: Image.Image, path: Path) -> Path:
    """Save the prepared RGBA logo to disk and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    logo_rgba.save(str(path))
    return path
