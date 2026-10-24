"""A scalar field over the board as a picture, with no plotting library.

`render.py` draws copper, and draws it through KiCad; a temperature or current
density map is a grid of numbers and needs neither. Adding matplotlib to the
requirements for one picture was the alternative, and the requirements file
records why every megabyte in it is there.

So: a colour-mapped PNG written with `zlib`, wrapped in an SVG that carries
what the PNG cannot — the title, the colour scale with its numbers, the board
outline, and markers on the places the text names. The SVG opens in any
browser and is the file to show someone.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import numpy as np

#: A perceptual dark-to-bright ramp (black, purple, red, orange, pale yellow),
#: interpolated. Close to matplotlib's "inferno" at the stops, which is the
#: point: it reads in greyscale and to the common colour deficiencies.
_STOPS = np.array([
    [0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99],
    [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164],
], dtype=float)


def colour(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    span = hi - lo if hi > lo else 1.0
    t = np.clip((np.nan_to_num(values, nan=lo) - lo) / span, 0, 1)
    pos = t * (len(_STOPS) - 1)
    i = np.minimum(pos.astype(int), len(_STOPS) - 2)
    f = (pos - i)[..., None]
    rgb = _STOPS[i] * (1 - f) + _STOPS[i + 1] * f
    out = rgb.astype(np.uint8)
    out[np.isnan(values)] = (18, 22, 30)
    return out


def png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an (h, w, 3) uint8 array as PNG."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[row].tobytes() for row in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write(path, grid: np.ndarray, origin: tuple[float, float], cell_mm: float,
          title: str, unit: str, outline=None, markers=(), scale: int = 4,
          lo: float | None = None, hi: float | None = None) -> Path:
    """Write `grid` (rows = y, NaN = off the board) as an SVG with an embedded
    PNG. ``markers`` are ``(x_mm, y_mm, label)``. Returns the SVG path."""
    path = Path(path).with_suffix(".svg")
    values = np.asarray(grid, dtype=float)
    lo = float(np.nanmin(values)) if lo is None else lo
    hi = float(np.nanmax(values)) if hi is None else hi
    rgb = colour(values, lo, hi)
    rgb = np.repeat(np.repeat(rgb, scale, axis=0), scale, axis=1)
    image = base64.b64encode(png_bytes(rgb)).decode("ascii")
    ny, nx = values.shape
    px = scale / cell_mm                       # pixels per mm
    w, h = nx * scale, ny * scale
    pad_top, bar_w = 56, 70
    total_w, total_h = w + bar_w + 20, h + pad_top + 10

    def to_px(x, y):
        return ((x - origin[0]) * px, pad_top + (y - origin[1]) * px)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" '
             f'height="{total_h}" font-family="sans-serif" font-size="12">',
             f'<rect width="100%" height="100%" fill="#0e1117"/>',
             f'<text x="6" y="18" fill="#e6e6e6" font-size="13">{_esc(title)}</text>',
             f'<text x="6" y="38" fill="#a0a0a0">{lo:.3g} to {hi:.3g} {_esc(unit)}'
             f' — {cell_mm:g} mm cells</text>',
             f'<image x="0" y="{pad_top}" width="{w}" height="{h}" '
             f'href="data:image/png;base64,{image}"/>']
    if outline is not None and not outline.is_empty:
        for poly in getattr(outline, "geoms", [outline]):
            pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in
                           (to_px(x, y) for x, y in poly.exterior.coords))
            parts.append(f'<polygon points="{pts}" fill="none" stroke="#7a8599" '
                         f'stroke-width="1"/>')
    for x, y, label in markers:
        cx, cy = to_px(x, y)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="none" '
                     f'stroke="#38d6ff" stroke-width="2"/>')
        parts.append(f'<text x="{cx + 9:.1f}" y="{cy - 6:.1f}" fill="#38d6ff">'
                     f'{_esc(label)}</text>')
    bar = np.linspace(hi, lo, 64)[:, None]
    bar_png = base64.b64encode(png_bytes(np.repeat(np.repeat(
        colour(bar, lo, hi), 4, axis=0), 16, axis=1))).decode("ascii")
    bx = w + 14
    parts.append(f'<image x="{bx}" y="{pad_top}" width="16" height="{min(h, 256)}" '
                 f'preserveAspectRatio="none" href="data:image/png;base64,{bar_png}"/>')
    parts.append(f'<text x="{bx + 20}" y="{pad_top + 10}" fill="#e6e6e6">{hi:.3g}</text>')
    parts.append(f'<text x="{bx + 20}" y="{pad_top + min(h, 256)}" fill="#e6e6e6">'
                 f'{lo:.3g}</text>')
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
