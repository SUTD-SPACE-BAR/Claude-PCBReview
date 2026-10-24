"""Tests for the dependency-free map writer — `heatmap`."""

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from shapely.geometry import box

from pcbreview import heatmap


def test_the_png_is_a_valid_png_of_the_right_size():
    rgb = heatmap.colour(np.arange(12, dtype=float).reshape(3, 4), 0, 11)
    data = heatmap.png_bytes(rgb)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (4, 3)
    # every chunk's CRC is right, so a viewer will not reject it
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind_and_body = data[pos + 4:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert zlib.crc32(kind_and_body) & 0xFFFFFFFF == crc
        pos += 12 + length


def test_off_board_cells_are_drawn_dark_not_as_the_minimum():
    rgb = heatmap.colour(np.array([[np.nan, 0.0]]), 0, 1)
    assert tuple(rgb[0, 0]) == (18, 22, 30)
    assert tuple(rgb[0, 1]) != (18, 22, 30)


def test_the_svg_carries_title_scale_and_markers(tmp_path):
    grid = np.linspace(0, 1, 50).reshape(5, 10)
    out = heatmap.write(tmp_path / "m", grid, (0.0, 0.0), 1.0, "a <title>",
                        "C", outline=box(0, 0, 10, 5), markers=[(2, 2, "hot")])
    text = out.read_text(encoding="utf-8")
    assert out.suffix == ".svg"
    assert "a &lt;title&gt;" in text and "hot" in text and "<polygon" in text
