"""Generate the native Windows 11 white transparent Face ID PNG icon.

Renders a crisp, 4x supersampled (512x512) Fluent-style Face ID line-art mask
using analytical signed distance fields and downsamples to 128x128 RGBA with
``Image.LANCZOS`` for smooth anti-aliased edges on a 100% transparent background.

To customize the icon, replace ``ir_faceid_icon.png`` with any 128x128 (or
high-DPI) white-on-transparent RGBA PNG and rebuild ``WinIRFaceIDProvider.dll``.
"""

from __future__ import annotations

from pathlib import Path
import numpy as np
from PIL import Image


def _sd_segment(
    px: np.ndarray,
    py: np.ndarray,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> np.ndarray:
    """Signed distance to line segment AB (capsule centerline)."""
    pax = px - ax
    pay = py - ay
    bax = bx - ax
    bay = by - ay
    denom = bax * bax + bay * bay
    h = np.clip((pax * bax + pay * bay) / denom, 0.0, 1.0)
    dx = pax - bax * h
    dy = pay - bay * h
    return np.hypot(dx, dy)


def _sd_arc(
    px: np.ndarray,
    py: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    start_deg: float,
    end_deg: float,
) -> np.ndarray:
    """Distance to a circular arc centered at (cx, cy), angles in degrees (0=+X, 90=+Y)."""
    dx = px - cx
    dy = py - cy
    ang = np.degrees(np.arctan2(dy, dx)) % 360.0
    start = start_deg % 360.0
    end = end_deg % 360.0

    if start <= end:
        in_arc = (ang >= start) & (ang <= end)
    else:
        in_arc = (ang >= start) | (ang <= end)

    dist_ring = np.abs(np.hypot(dx, dy) - radius)

    s_rad = np.radians(start_deg)
    e_rad = np.radians(end_deg)
    sx, sy = cx + radius * np.cos(s_rad), cy + radius * np.sin(s_rad)
    ex, ey = cx + radius * np.cos(e_rad), cy + radius * np.sin(e_rad)
    dist_ends = np.minimum(np.hypot(px - sx, py - sy), np.hypot(px - ex, py - ey))

    return np.where(in_arc, dist_ring, dist_ends)


def generate_ir_faceid_icon(output_path: Path, size: int = 128) -> None:
    """Render and save a 128x128 RGBA pure-white transparent Face ID icon."""
    scale = 4
    hi_size = size * scale  # 512x512 supersampling canvas
    y_grid, x_grid = np.mgrid[0:hi_size, 0:hi_size].astype(np.float64) + 0.5

    # Stroke half-width (~4.5px at 128px => 18px stroke width at 512px)
    stroke_r = 9.0
    dist = np.full((hi_size, hi_size), 1e6, dtype=np.float64)

    def add_stroke(d: np.ndarray, radius: float = stroke_r) -> None:
        nonlocal dist
        dist = np.minimum(dist, d - radius)

    # Outer Fluent viewfinder corner brackets: bounds [84, 428], corner radius 52
    lo = 84.0
    hi = 428.0
    cr = 52.0
    arm = 44.0  # straight extension past the corner arc

    # Top-left corner (arc 180..270 around (lo+cr, lo+cr))
    add_stroke(_sd_arc(x_grid, y_grid, lo + cr, lo + cr, cr, 180.0, 270.0))
    add_stroke(_sd_segment(x_grid, y_grid, lo + cr, lo, lo + cr + arm, lo))
    add_stroke(_sd_segment(x_grid, y_grid, lo, lo + cr, lo, lo + cr + arm))

    # Top-right corner (arc 270..360 around (hi-cr, lo+cr))
    add_stroke(_sd_arc(x_grid, y_grid, hi - cr, lo + cr, cr, 270.0, 360.0))
    add_stroke(_sd_segment(x_grid, y_grid, hi - cr - arm, lo, hi - cr, lo))
    add_stroke(_sd_segment(x_grid, y_grid, hi, lo + cr, hi, lo + cr + arm))

    # Bottom-right corner (arc 0..90 around (hi-cr, hi-cr))
    add_stroke(_sd_arc(x_grid, y_grid, hi - cr, hi - cr, cr, 0.0, 90.0))
    add_stroke(_sd_segment(x_grid, y_grid, hi - cr - arm, hi, hi - cr, hi))
    add_stroke(_sd_segment(x_grid, y_grid, hi, hi - cr - arm, hi, hi - cr))

    # Bottom-left corner (arc 90..180 around (lo+cr, hi-cr))
    add_stroke(_sd_arc(x_grid, y_grid, lo + cr, hi - cr, cr, 90.0, 180.0))
    add_stroke(_sd_segment(x_grid, y_grid, lo + cr, hi, lo + cr + arm, hi))
    add_stroke(_sd_segment(x_grid, y_grid, lo, hi - cr - arm, lo, hi - cr))

    # Two crisp vertical pill eyes
    add_stroke(_sd_segment(x_grid, y_grid, 200.0, 198.0, 200.0, 228.0))
    add_stroke(_sd_segment(x_grid, y_grid, 312.0, 198.0, 312.0, 228.0))

    # Minimalist subtle center nose stroke
    add_stroke(_sd_segment(x_grid, y_grid, 256.0, 232.0, 256.0, 266.0), radius=7.5)

    # Gentle curved smile arc
    add_stroke(_sd_arc(x_grid, y_grid, 256.0, 262.0, 76.0, 40.0, 140.0))

    # Anti-aliased coverage in [0, 1] at 512x512, then downsample with LANCZOS
    alpha_hi = np.clip(0.5 - dist, 0.0, 1.0)
    mask_hi = Image.fromarray((alpha_hi * 255.0 + 0.5).astype(np.uint8), mode="L")
    mask = mask_hi.resize((size, size), resample=Image.Resampling.LANCZOS)
    alpha = np.array(mask, dtype=np.uint8)

    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    fg = alpha > 0
    rgba[fg, 0] = 255
    rgba[fg, 1] = 255
    rgba[fg, 2] = 255
    rgba[..., 3] = alpha

    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(output_path, format="PNG", optimize=True)


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "ir_faceid_icon.png"
    generate_ir_faceid_icon(out)
    print(f"Generated {out}")
