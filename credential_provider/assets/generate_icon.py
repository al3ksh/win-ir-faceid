"""Generate the native Windows 11 white transparent Face ID PNG icon.

Renders a crisp, 4x supersampled (512x512) Fluent-style Face ID line-art mask
using analytical signed distance fields and downsamples to 128x128 RGBA with
``Image.LANCZOS`` for smooth anti-aliased edges on a 100% transparent background.

To customize the icon, replace ``ir_faceid_icon.png`` with any 128x128 (or
high-DPI) white-on-transparent RGBA PNG and rebuild ``WinIRFaceIDProvider.dll``.
"""

from __future__ import annotations

import math
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw


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
    if denom <= 1e-12:
        return np.hypot(pax, pay)
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


def _brackets_centerline_sdf(px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """Centerline SDF for the outer Fluent viewfinder corner brackets in 512x512 space."""
    lo = 84.0
    hi = 428.0
    cr = 52.0
    arm = 44.0

    d = np.full(px.shape, 1e6, dtype=np.float64)
    # Top-left corner
    d = np.minimum(d, _sd_arc(px, py, lo + cr, lo + cr, cr, 180.0, 270.0))
    d = np.minimum(d, _sd_segment(px, py, lo + cr, lo, lo + cr + arm, lo))
    d = np.minimum(d, _sd_segment(px, py, lo, lo + cr, lo, lo + cr + arm))
    # Top-right corner
    d = np.minimum(d, _sd_arc(px, py, hi - cr, lo + cr, cr, 270.0, 360.0))
    d = np.minimum(d, _sd_segment(px, py, hi - cr - arm, lo, hi - cr, lo))
    d = np.minimum(d, _sd_segment(px, py, hi, lo + cr, hi, lo + cr + arm))
    # Bottom-right corner
    d = np.minimum(d, _sd_arc(px, py, hi - cr, hi - cr, cr, 0.0, 90.0))
    d = np.minimum(d, _sd_segment(px, py, hi - cr - arm, hi, hi - cr, hi))
    d = np.minimum(d, _sd_segment(px, py, hi, hi - cr - arm, hi, hi - cr))
    # Bottom-left corner
    d = np.minimum(d, _sd_arc(px, py, lo + cr, hi - cr, cr, 90.0, 180.0))
    d = np.minimum(d, _sd_segment(px, py, lo + cr, hi, lo + cr + arm, hi))
    d = np.minimum(d, _sd_segment(px, py, lo, hi - cr - arm, lo, hi - cr))
    return d


def _render_brackets_alpha(
    x_grid: np.ndarray,
    y_grid: np.ndarray,
    scale: float = 1.0,
    ox: float = 0.0,
    oy: float = 0.0,
    stroke_r: float = 9.0,
) -> np.ndarray:
    """Render anti-aliased 512x512 coverage for scaled/shifted corner brackets."""
    lx = (x_grid - 256.0 - ox) / scale + 256.0
    ly = (y_grid - 256.0 - oy) / scale + 256.0
    dist = _brackets_centerline_sdf(lx, ly) * scale - stroke_r
    return np.clip(0.5 - dist, 0.0, 1.0)


def _render_face_alpha(
    x_grid: np.ndarray,
    y_grid: np.ndarray,
    ox: float = 0.0,
    oy: float = 0.0,
    eye_scale_y: float = 1.0,
    frown: bool = False,
    smile_lift: float = 0.0,
    stroke_r: float = 9.0,
) -> np.ndarray:
    """Render anti-aliased 512x512 coverage for eyes, nose, and smile/frown."""
    dist = np.full(x_grid.shape, 1e6, dtype=np.float64)

    # Eyes centered vertically at y=213 with base half-height 15
    eye_hh = max(0.0, 15.0 * eye_scale_y)
    eye_cy = 213.0 + oy
    dist = np.minimum(
        dist,
        _sd_segment(x_grid, y_grid, 200.0 + ox, eye_cy - eye_hh, 200.0 + ox, eye_cy + eye_hh)
        - stroke_r,
    )
    dist = np.minimum(
        dist,
        _sd_segment(x_grid, y_grid, 312.0 + ox, eye_cy - eye_hh, 312.0 + ox, eye_cy + eye_hh)
        - stroke_r,
    )

    # Minimalist subtle center nose stroke (bottom edge at 266 + 7.5 = 273.5)
    dist = np.minimum(
        dist,
        _sd_segment(x_grid, y_grid, 256.0 + ox, 232.0 + oy, 256.0 + ox, 266.0 + oy) - 7.5,
    )

    if frown:
        # Softer, wider gentle frown arc centered at (256, 378) radius 72 (top apex y=306,
        # stroke top 297), leaving a ~23.5px gap below the nose at 512px (~5.9px at 128px).
        dist = np.minimum(
            dist,
            _sd_arc(x_grid, y_grid, 256.0 + ox, 378.0 + oy, 72.0, 232.0, 308.0) - stroke_r,
        )
    else:
        dist = np.minimum(
            dist,
            _sd_arc(x_grid, y_grid, 256.0 + ox, 262.0 + oy - smile_lift, 76.0, 40.0, 140.0)
            - stroke_r,
        )

    return np.clip(0.5 - dist, 0.0, 1.0)


def _render_checkmark_alpha(
    x_grid: np.ndarray,
    y_grid: np.ndarray,
    progress: float,
    stroke_r: float = 9.0,
) -> np.ndarray:
    """Render anti-aliased 512x512 coverage for an animated rounded checkmark."""
    if progress <= 0.0:
        return np.zeros(x_grid.shape, dtype=np.float64)

    p0x, p0y = 256.0 - 68.0, 256.0 + 4.0
    p1x, p1y = 256.0 - 20.0, 256.0 + 52.0
    p2x, p2y = 256.0 + 76.0, 256.0 - 48.0

    len1 = math.hypot(p1x - p0x, p1y - p0y)
    len2 = math.hypot(p2x - p1x, p2y - p1y)
    total_len = len1 + len2
    drawn = min(1.0, max(0.0, progress)) * total_len

    dist = np.full(x_grid.shape, 1e6, dtype=np.float64)
    if drawn <= len1:
        frac = drawn / len1
        ex = p0x + (p1x - p0x) * frac
        ey = p0y + (p1y - p0y) * frac
        dist = np.minimum(dist, _sd_segment(x_grid, y_grid, p0x, p0y, ex, ey) - stroke_r)
    else:
        dist = np.minimum(dist, _sd_segment(x_grid, y_grid, p0x, p0y, p1x, p1y) - stroke_r)
        frac = (drawn - len1) / len2
        ex = p1x + (p2x - p1x) * frac
        ey = p1y + (p2y - p1y) * frac
        dist = np.minimum(dist, _sd_segment(x_grid, y_grid, p1x, p1y, ex, ey) - stroke_r)

    return np.clip(0.5 - dist, 0.0, 1.0)


def _save_alpha_mask_as_white_png(alpha_hi: np.ndarray, output_path: Path, size: int = 128) -> Image.Image:
    """Downsample a 512x512 float coverage mask to 128x128 pure-white transparent PNG."""
    alpha_clamped = np.clip(alpha_hi, 0.0, 1.0)
    mask_hi = Image.fromarray((alpha_clamped * 255.0 + 0.5).astype(np.uint8), mode="L")
    mask = mask_hi.resize((size, size), resample=Image.Resampling.LANCZOS)
    alpha = np.array(mask, dtype=np.uint8)

    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    fg = alpha > 0
    rgba[fg, 0] = 255
    rgba[fg, 1] = 255
    rgba[fg, 2] = 255
    rgba[..., 3] = alpha

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(rgba, mode="RGBA")
    img.save(output_path, format="PNG", optimize=True)
    return img


def generate_ir_faceid_icon(output_path: Path, size: int = 128) -> None:
    """Render and save a 128x128 RGBA pure-white transparent Face ID icon."""
    scale = 4
    hi_size = size * scale  # 512x512 supersampling canvas
    y_grid, x_grid = np.mgrid[0:hi_size, 0:hi_size].astype(np.float64) + 0.5

    alpha_hi = np.maximum(
        _render_brackets_alpha(x_grid, y_grid),
        _render_face_alpha(x_grid, y_grid),
    )
    _save_alpha_mask_as_white_png(alpha_hi, output_path, size=size)


def generate_animation_frames(
    frames_dir: Path,
    contact_sheet_path: Path | None = None,
    size: int = 128,
) -> list[Path]:
    """Generate all 86 analytical SDF animation frames into ``frames_dir``."""
    scale = 4
    hi_size = size * scale
    y_grid, x_grid = np.mgrid[0:hi_size, 0:hi_size].astype(np.float64) + 0.5

    frames_dir.mkdir(parents=True, exist_ok=True)
    for old_png in frames_dir.glob("*.png"):
        old_png.unlink()

    generated: list[tuple[str, Image.Image]] = []

    def emit(name: str, alpha_hi: np.ndarray) -> None:
        out_path = frames_dir / name
        img = _save_alpha_mask_as_white_png(alpha_hi, out_path, size=size)
        generated.append((name, img))

    # 1. Entrance (enter_00..enter_11, 12 frames)
    for i in range(12):
        t = i / 11.0
        ease = 1.0 - (1.0 - t) ** 3
        b_scale = 0.84 + 0.16 * ease
        b_alpha = 0.2 + 0.8 * ease
        f_oy = 16.0 * (1.0 - ease)
        f_alpha = ease

        a_brackets = _render_brackets_alpha(x_grid, y_grid, scale=b_scale) * b_alpha
        a_face = _render_face_alpha(x_grid, y_grid, oy=f_oy) * f_alpha
        emit(f"enter_{i:02d}.png", np.maximum(a_brackets, a_face))

    # 2. Scanning loop (scan_00..scan_47, 48 frames, seamless 2pi loop)
    blink_scale_y = {
        36: 0.55,
        37: 0.20,
        38: 0.08,
        39: 0.20,
        40: 0.55,
    }
    for i in range(48):
        theta = 2.0 * math.pi * i / 48.0
        ox = math.sin(theta) * 26.0
        oy = math.sin(2.0 * theta) * 4.5
        b_scale = 1.0 + math.sin(theta) * 0.024
        eye_sy = blink_scale_y.get(i, 1.0)

        a_brackets = _render_brackets_alpha(x_grid, y_grid, scale=b_scale)
        a_face = _render_face_alpha(x_grid, y_grid, ox=ox, oy=oy, eye_scale_y=eye_sy)
        emit(f"scan_{i:02d}.png", np.maximum(a_brackets, a_face))

    # 3. Unlock checkmark morph (unlock_00..unlock_11, 12 frames)
    for i in range(12):
        t = i / 11.0
        b_scale = 1.0 + 0.04 * math.sin(math.pi * t)

        if i <= 4:
            u = i / 4.0
            face_ease = 1.0 - (1.0 - u) ** 2
            face_alpha = 1.0 - face_ease
            face_oy = -14.0 * face_ease
            smile_lift = 10.0 * face_ease
        else:
            face_alpha = 0.0
            face_oy = -14.0
            smile_lift = 10.0

        if i < 4:
            check_progress = 0.0
        else:
            c = (i - 3) / 8.0
            check_progress = 1.0 - (1.0 - c) ** 2

        a_brackets = _render_brackets_alpha(x_grid, y_grid, scale=b_scale)
        a_face = (
            _render_face_alpha(
                x_grid,
                y_grid,
                oy=face_oy,
                smile_lift=smile_lift,
            )
            * face_alpha
        )
        a_check = _render_checkmark_alpha(x_grid, y_grid, check_progress)
        emit(f"unlock_{i:02d}.png", np.maximum.reduce([a_brackets, a_face, a_check]))

    # 4. Failed / Not recognized shake (fail_00..fail_13, 14 frames)
    for i in range(14):
        if i == 13:
            ox = 0.0
        else:
            t = i / 13.0
            ox = -28.0 * ((1.0 - t) ** 1.4) * math.cos(3.5 * math.pi * t)
        a_brackets = _render_brackets_alpha(x_grid, y_grid, ox=ox) * 0.80
        a_face = _render_face_alpha(x_grid, y_grid, ox=ox, frown=True)
        emit(f"fail_{i:02d}.png", np.maximum(a_brackets, a_face))

    if contact_sheet_path is not None:
        _save_contact_sheet(generated, contact_sheet_path, cell_size=size)

    return [frames_dir / name for name, _ in generated]


def _save_contact_sheet(
    frames: list[tuple[str, Image.Image]],
    output_path: Path,
    cell_size: int = 128,
) -> None:
    """Save a dark-charcoal contact sheet preview of all generated frames."""
    cols = 8
    pad = 16
    label_h = 22
    rows = math.ceil(len(frames) / cols)
    sheet_w = cols * (cell_size + pad) + pad
    sheet_h = rows * (cell_size + label_h + pad) + pad

    sheet = Image.new("RGBA", (sheet_w, sheet_h), (28, 30, 34, 255))
    draw = ImageDraw.Draw(sheet)

    for idx, (name, img) in enumerate(frames):
        r = idx // cols
        c = idx % cols
        x0 = pad + c * (cell_size + pad)
        y0 = pad + r * (cell_size + label_h + pad)
        draw.rectangle([x0 - 1, y0 - 1, x0 + cell_size, y0 + cell_size], outline=(62, 66, 74, 255))
        sheet.alpha_composite(img, (x0, y0))
        draw.text((x0 + 4, y0 + cell_size + 4), name, fill=(210, 214, 220, 255))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")


if __name__ == "__main__":
    assets_dir = Path(__file__).resolve().parent
    out = assets_dir / "ir_faceid_icon.png"
    generate_ir_faceid_icon(out)
    print(f"Generated {out}")

    frames_out = assets_dir / "frames"
    sheet_out = Path(
        r"C:\Users\DELL\.gemini\antigravity\brain\244d7b33-015e-4a5f-92b4-ab5b415fc107\scratch\anim_contact_sheet.png"
    )
    frame_paths = generate_animation_frames(frames_out, contact_sheet_path=sheet_out)
    print(f"Generated {len(frame_paths)} frames in {frames_out}")
    print(f"Saved contact sheet to {sheet_out}")
