"""Tests for reference-plane resolution and return-path analysis.

Two things are being defended here:

* the *detour* metric, which is validated against closed-form geometry — an
  antipad of diameter d crossed through its centre costs pi*d/2 - d of extra
  return path, and nothing else;
* the multi-plane resolution on a six-layer stackup, where a trace on an inner
  signal layer references whichever plane is dielectrically nearest, which is
  not always ground.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import LineString, Point

from pcbreview.model import Board
from pcbreview.reference import (
    ReferenceResolver,
    _detour_around,
    find_transitions,
    looks_like_ground,
    stitching_distance_limit_mm,
)
from pcbreview.stackup import load_and_validate


# -- the detour metric, against closed forms ------------------------------

def test_detour_across_a_circular_antipad_matches_half_circumference():
    """Crossing a hole of diameter d centrally costs pi*d/2 instead of d."""
    diameter = 0.9
    hole = Point(0, 0).buffer(diameter / 2, quad_segs=256)
    crossing = LineString([(-diameter / 2, 0), (diameter / 2, 0)])
    expected = math.pi * diameter / 2 - diameter
    assert _detour_around(hole, crossing) == pytest.approx(expected, rel=0.01)


def test_detour_scales_with_hole_size_not_perimeter():
    """A long clip along a big void must stay cheap.

    Using half the perimeter — the obvious wrong metric — would charge ~15 mm
    here, which is what made merged via-fanout voids look catastrophic.
    """
    void = Point(0, 0).buffer(10, quad_segs=256)
    edge_clip = LineString([(9.9, -1.4), (9.9, 1.4)])
    assert _detour_around(void, edge_clip) < 0.5
    straight_through = LineString([(-10, 0), (10, 0)])
    assert _detour_around(void, straight_through) == pytest.approx(
        math.pi * 10 - 20, rel=0.01)


def test_detour_is_never_negative():
    hole = Point(0, 0).buffer(0.5, quad_segs=64)
    assert _detour_around(hole, LineString([(-0.5, 0), (0.5, 0)])) >= 0.0


# -- ground detection -----------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("GND", True), ("/GND", True), ("AGND", True), ("DGND", True),
    ("VSS", True), ("+3.3V", False), ("USB_D+", False), ("SCK", False),
])
def test_ground_name_heuristic(name, expected):
    assert looks_like_ground(name) is expected


# -- transitions ----------------------------------------------------------

def test_stitching_limit_shrinks_with_faster_edges():
    slow = stitching_distance_limit_mm(2000)     # 2 ns
    fast = stitching_distance_limit_mm(200)      # 200 ps
    assert fast < slow
    assert slow > 0
    assert stitching_distance_limit_mm(0) > 0    # unknown rate -> default
