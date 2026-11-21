"""Generate the minimalist Windows 11 desktop application icon (engine/ui/app.ico).

Renders a 4x supersampled analytical signed-distance-field (SDF) dark charcoal
rounded tile (#111317) with a subtle inner hairline border and pure-white
(#f3f4f6) Fluent Face ID viewfinder glyph across all standard Windows 11 icon
resolutions (16, 24, 32, 48, 64, 128, 256).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

ICON_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

_TILE_RGB = np.array([17.0, 19.0, 23.0], dtype=np.float64)      # #111317
_BORDER_RGB = np.array([255.0, 255.0, 255.0], dtype=np.float64)
_BORDER_ALPHA = 0.14
_GLYPH_RGB = np.array([243.0, 244.0, 246.0], dtype=np.float64)   # #f3f4f6


def _sd_rounded_box(
    px: np.ndarray,
    py: np.ndarray,
    half_w: float,
    half_h: float,
    radius: float,
) -> np.ndarray:
    """Signed distance to an axis-aligned rounded rectangle centered at the origin."""
    qx = np.abs(px) - (half_w - radius)
    qy = np.abs(py) - (half_h - radius)
    outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    inside = np.minimum(np.maximum(qx, qy), 0.0)
    return outside + inside - radius


def _sd_segment(
    px: np.ndarray,
    py: np.ndarray,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> np.ndarray:
    """Distance to line segment AB (capsule centerline)."""
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


def render_app_icon_size(size: int) -> Image.Image:
    """Render a single square RGBA icon frame at ``size`` using 4x supersampled SDF."""
    scale = 4
    hi_size = size * scale
    coords = (np.arange(hi_size, dtype=np.float64) + 0.5) / hi_size
    x_grid, y_grid = np.meshgrid(coords, coords)
    aa_px = 1.0 / hi_size

    # Rounded dark charcoal tile (~22% corner radius, small outer margin for clean AA)
    tile_inset = 0.03
    tile_half = 0.5 - tile_inset
    tile_size = 2.0 * tile_half
    tile_radius = 0.22 * tile_size

    d_tile = _sd_rounded_box(x_grid - 0.5, y_grid - 0.5, tile_half, tile_half, tile_radius)
    tile_cov = np.clip(0.5 - d_tile / aa_px, 0.0, 1.0)

    # Subtle inner hairline border so the dark tile separates cleanly on both dark & light bars
    border_w = max(1.0 / size, 0.011)
    border_inner = np.clip(0.5 + (d_tile + border_w) / aa_px, 0.0, 1.0)
    border_cov = border_inner * _BORDER_ALPHA

    # Slightly heavier stroke weight at 16px/24px so small titlebar icons stay razor-sharp
    stroke_r = max(0.021, 0.45 / size + 0.011)
    dist_glyph = np.full((hi_size, hi_size), 1e6, dtype=np.float64)

    def add_stroke(d: np.ndarray, radius: float = stroke_r) -> None:
        nonlocal dist_glyph
        dist_glyph = np.minimum(dist_glyph, d - radius)

    # Viewfinder corner brackets (~20% inset from tile edge)
    lo = 0.22
    hi = 0.78
    cr = 0.095
    arm = 0.072

    # Top-left bracket
    add_stroke(_sd_arc(x_grid, y_grid, lo + cr, lo + cr, cr, 180.0, 270.0))
    add_stroke(_sd_segment(x_grid, y_grid, lo + cr, lo, lo + cr + arm, lo))
    add_stroke(_sd_segment(x_grid, y_grid, lo, lo + cr, lo, lo + cr + arm))

    # Top-right bracket
    add_stroke(_sd_arc(x_grid, y_grid, hi - cr, lo + cr, cr, 270.0, 360.0))
    add_stroke(_sd_segment(x_grid, y_grid, hi - cr - arm, lo, hi - cr, lo))
    add_stroke(_sd_segment(x_grid, y_grid, hi, lo + cr, hi, lo + cr + arm))

    # Bottom-right bracket
    add_stroke(_sd_arc(x_grid, y_grid, hi - cr, hi - cr, cr, 0.0, 90.0))
    add_stroke(_sd_segment(x_grid, y_grid, hi - cr - arm, hi, hi - cr, hi))
    add_stroke(_sd_segment(x_grid, y_grid, hi, hi - cr - arm, hi, hi - cr))

    # Bottom-left bracket
    add_stroke(_sd_arc(x_grid, y_grid, lo + cr, hi - cr, cr, 90.0, 180.0))
    add_stroke(_sd_segment(x_grid, y_grid, lo + cr, hi, lo + cr + arm, hi))
    add_stroke(_sd_segment(x_grid, y_grid, lo, hi - cr - arm, lo, hi - cr))

    # Minimalist vertical pill eyes
    add_stroke(_sd_segment(x_grid, y_grid, 0.395, 0.385, 0.395, 0.445))
    add_stroke(_sd_segment(x_grid, y_grid, 0.605, 0.385, 0.605, 0.445))

    # Subtle center nose stroke
    add_stroke(_sd_segment(x_grid, y_grid, 0.500, 0.452, 0.500, 0.528), radius=stroke_r * 0.85)

    # Gentle curved smile arc
    add_stroke(_sd_arc(x_grid, y_grid, 0.500, 0.510, 0.132, 42.0, 138.0))

    glyph_cov = np.clip(0.5 - dist_glyph / aa_px, 0.0, 1.0)

    # Composite tile -> inner hairline border -> glyph in float RGB, masked by tile_cov
    rgb = np.empty((hi_size, hi_size, 3), dtype=np.float64)
    rgb[..., :] = _TILE_RGB
    rgb = rgb * (1.0 - border_cov[..., None]) + _BORDER_RGB * border_cov[..., None]
    rgb = rgb * (1.0 - glyph_cov[..., None]) + _GLYPH_RGB * glyph_cov[..., None]

    rgba_hi = np.empty((hi_size, hi_size, 4), dtype=np.uint8)
    rgba_hi[..., :3] = np.clip(rgb + 0.5, 0.0, 255.0).astype(np.uint8)
    rgba_hi[..., 3] = np.clip(tile_cov * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)

    img_hi = Image.fromarray(rgba_hi, mode="RGBA")
    return img_hi.resize((size, size), resample=Image.Resampling.LANCZOS)


def generate_app_icon(output_path: Path | None = None) -> Path:
    """Render and save the multi-resolution Windows 11 app.ico file."""
    target = Path(output_path) if output_path is not None else (Path(__file__).resolve().parent / "app.ico")
    target.parent.mkdir(parents=True, exist_ok=True)

    frames = [render_app_icon_size(size) for size in ICON_SIZES]
    frames[-1].save(
        target,
        format="ICO",
        sizes=[(s, s) for s in ICON_SIZES],
        append_images=frames[:-1],
    )
    return target


if __name__ == "__main__":
    out = generate_app_icon()
    print(f"Generated {out}")
