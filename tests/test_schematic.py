"""Tests for the schematic netlist parser fixes.

Correctness is measured against the PCB's own net assignment, which KiCad
produced, rather than by inspection. Names are ignored — what matters is the
partition of pins into nets, since that is what every downstream intent
decision depends on.

Scope note: these cover *single-sheet* parsing — one file, in isolation. Whole
designs are resolved by `pcbreview.schematic` and scored in `test_hierarchy.py`;
a sheet placed more than once cannot be scored standalone at all, because its
designators depend on which placement you mean.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import pcbreview._schematic_base as parser
from pcbreview.model import Board

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_schematic import schematic_groups, score  # noqa: E402


# -- geometry primitive ---------------------------------------------------

def test_point_on_segment_accepts_the_interior():
    assert parser._point_on_segment(5.0, 0.0, 0.0, 0.0, 10.0, 0.0)


def test_point_on_segment_accepts_endpoints():
    assert parser._point_on_segment(0.0, 0.0, 0.0, 0.0, 10.0, 0.0)
    assert parser._point_on_segment(10.0, 0.0, 0.0, 0.0, 10.0, 0.0)


def test_point_on_segment_rejects_beyond_the_ends():
    assert not parser._point_on_segment(11.0, 0.0, 0.0, 0.0, 10.0, 0.0)
    assert not parser._point_on_segment(-1.0, 0.0, 0.0, 0.0, 10.0, 0.0)


def test_point_on_segment_rejects_points_off_the_line():
    assert not parser._point_on_segment(5.0, 1.0, 0.0, 0.0, 10.0, 0.0)


def test_point_on_segment_handles_a_degenerate_segment():
    assert parser._point_on_segment(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    assert not parser._point_on_segment(2.0, 1.0, 1.0, 1.0, 1.0, 1.0)


# -- the symbol transform -------------------------------------------------

def test_a_mirror_is_applied_after_rotation_not_before():
    """Order matters, and the intuitive order is the wrong one.

    KiCad composes the mirror onto the left of the orientation matrix
    (`SCH_SYMBOL::SetOrientation`), so it acts on already-rotated screen
    coordinates. Mirroring first in the library frame instead exchanges the
    two ends of every rotated symbol.

    Geometry below is `Device:R` — pin 1 at lib (0, +3.81), pin 2 at (0, -3.81)
    — placed at 90 degrees with `(mirror x)`, which is how a series resistor
    sits on one sheet here. KiCad puts pin 2 on the right, joining the module;
    the old convention put pin 1 there and silently swapped the resistor's
    nets.
    """
    pin1 = parser.transform_pin(0.0, 3.81, 0.0, 0.0, 90.0, "x")
    pin2 = parser.transform_pin(0.0, -3.81, 0.0, 0.0, 90.0, "x")
    assert pin1 == pytest.approx((-3.81, 0.0), abs=1e-9)
    assert pin2 == pytest.approx((3.81, 0.0), abs=1e-9)


def test_an_unmirrored_symbol_is_only_y_flipped():
    """Library frame is Y-up, schematic frame is Y-down."""
    assert parser.transform_pin(0.0, 3.81, 10.0, 20.0, 0.0, None) == \
        pytest.approx((10.0, 16.19), abs=1e-9)
