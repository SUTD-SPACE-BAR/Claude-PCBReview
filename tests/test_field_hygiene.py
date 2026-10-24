"""Tests for field-shape analysis and for tracing through series parts.

Two capabilities that turned out to be the same kind of problem: the data a
human typed does not sit where a lookup expects it, and the circuit a human
drew is not visible from net names alone.

Both are measured against real boards, because both were designed from what
those boards actually contain rather than from what a schema would like them
to contain — 39 LCSC codes in `Description`, a current-sense amplifier
reaching its shunt through two filter resistors.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import placement
from pcbreview.intent import classify_rails
from pcbreview.model import Board
from pcbreview.schematic import (Component, Net, PinRef, Schematic, netlist,
                                 value_shape)
from pcbreview.schematic_audit import check_field_hygiene
from pcbreview.stackup import load_and_validate


# -- value shapes ----------------------------------------------------------

@pytest.mark.parametrize("text,shape", [
    ("C9100001", "lcsc_code"),
    ("C9100002", "lcsc_code"),
    ("https://example.com/x.pdf", "url"),
    ("~/docs/part.pdf", "path"),
    ("XYZ123456-50", "part_number"),
    ("CL21A106KAYNNNE", "part_number"),
    ("4.7uF", "value"),
    ("3.16kR", "value"),
    ("10k", "value"),
    ("Unpolarized capacitor", "text"),
    ("", "empty"),
    ("~", "empty"),
])
def test_value_shape_classifies_by_content_not_by_field_name(text, shape):
    assert value_shape(text) == shape


def test_a_package_code_is_not_mistaken_for_an_lcsc_code():
    """`C_0402` starts with C and has digits. An LCSC code is C then digits
    only, which is what keeps a footprint fragment out of the sourcing data."""
    assert value_shape("C_0402_1005Metric") != "lcsc_code"
    assert value_shape("C1206") == "lcsc_code"   # genuinely ambiguous, and rare


def test_missing_value_and_footprint_are_reported():
    def part(reference, value, footprint):
        return Component(reference=reference, value=value, footprint=footprint,
                         lib_id="Device:R", unit=1, dnp=False, sheet=0,
                         uuid=None,
                         properties={"Reference": reference, "Value": value,
                                     "Footprint": footprint})

    schematic = Schematic(root=Path("t.kicad_sch"), sheets=[], nets=[],
                          diagnostics=[],
                          components=[part("R1", "10k", "R:0402"),
                                      part("R2", "", "R:0402"),
                                      part("R3", "10k", "")])
    survey = schematic.field_survey()
    assert [a.reference for a in survey.by_kind("missing_value")] == ["R2"]
    assert [a.reference for a in survey.by_kind("missing_footprint")] == ["R3"]


def test_a_walk_stops_at_the_depth_limit():
    def link(reference, a, b):
        return Component(reference=reference, value="0R", footprint="",
                         lib_id="Device:R", unit=1, dnp=False, sheet=0,
                         uuid=None,
                         pins=[PinRef(reference, "1", "", "passive", 0),
                               PinRef(reference, "2", "", "passive", 0)])

    nets = [Net(name=f"N{i}", source="local",
                pins=[PinRef(f"R{i}", "2", "", "passive", 0),
                      PinRef(f"R{i + 1}", "1", "", "passive", 0)])
            for i in range(1, 5)]
    nets.insert(0, Net(name="N0", source="local",
                       pins=[PinRef("R1", "1", "", "passive", 0)]))
    schematic = Schematic(root=Path("t.kicad_sch"), sheets=[], nets=nets,
                          diagnostics=[],
                          components=[link(f"R{i}", None, None)
                                      for i in range(1, 6)])
    assert set(schematic.reachable_through_series("N0", max_depth=1)) == {"N0", "N1"}
    assert set(schematic.reachable_through_series("N0", max_depth=2)) == \
        {"N0", "N1", "N2"}


# -- loop inductance -------------------------------------------------------

def test_loop_inductance_is_mu0_times_area_over_width():
    """A 1 mm² loop with a 1 mm ribbon is mu0, i.e. 1.2566 nH. Getting the
    H/m to H/mm conversion backwards put every answer out by 1e6."""
    assert placement.loop_inductance_nh(1.0, 1.0) == pytest.approx(1.2566, abs=1e-3)
    assert placement.MU0_H_PER_MM == pytest.approx(1.2566e-9, rel=1e-4)


def test_loop_inductance_scales_linearly_with_both_terms():
    assert placement.loop_inductance_nh(2.0, 1.0) == \
        pytest.approx(2 * placement.loop_inductance_nh(1.0, 1.0))
    assert placement.loop_inductance_nh(1.0, 0.5) == \
        pytest.approx(2 * placement.loop_inductance_nh(1.0, 1.0))


def test_a_zero_width_ribbon_is_refused_rather_than_infinite():
    with pytest.raises(ValueError):
        placement.loop_inductance_nh(1.0, 0.0)
