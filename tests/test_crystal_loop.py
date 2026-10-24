"""Tests for the oscillator check — and for it never lying about coverage.

The load-bearing property here is not any measurement. It is that **every path
out of `check_crystal_loop` emits a finding**. The first real crystal the check
ever met was a 3-pin ceramic resonator with integrated load capacitors; the
discrete-load-capacitor search found nothing, the loop `continue`d, and nothing
recorded that it had declined. `coverage()` meanwhile reported `crystals=1`,
which a reader correctly takes as proof the check ran.

A zero that means "did not look" is the failure the coverage lines exist to
prevent. A **non-zero** that means "did not look" is the same failure, harder
to catch, and it is what these tests pin.

Board-free: the checks take a `PlacementContext`, so a fabricated one exercises
them without KiCad.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.placement import PlacementContext, check_crystal_loop


class _Board:
    def __init__(self, lengths):
        self._nets = {i + 1: SimpleNamespace(code=i + 1, name=name)
                      for i, name in enumerate(lengths)}
        self._by_name = {n.name: n for n in self._nets.values()}
        self._lengths = lengths
        self.copper_layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

    def net(self, key):
        if isinstance(key, int):
            return self._nets[key]
        return self._by_name[key]

    def tracks_on_net(self, code):
        return [SimpleNamespace(length_mm=self._lengths[self._nets[code].name])]


def _resonator(terminals, ground_via_mm=0.5, value="CSTCE16M0V53-R0",
               plane=True):
    """A 3-pin part: two terminals and a centre ground pad."""
    names = list(terminals)
    pads = [SimpleNamespace(number="1", net_name=names[0], pos=(0.0, 0.0),
                            copper_layers=["F.Cu"]),
            SimpleNamespace(number="2", net_name="GND", pos=(1.0, 0.0),
                            copper_layers=["F.Cu"]),
            SimpleNamespace(number="3", net_name=names[1], pos=(2.0, 0.0),
                            copper_layers=["F.Cu"])]
    context = PlacementContext(
        board=_Board({**terminals, "GND": 0.0}),
        schematic=SimpleNamespace(components=[
            SimpleNamespace(reference="XTAL2", value=value)]),
        ground_nets={"GND"}, supply_nets=set(),
        plane_layers=["In1.Cu"] if plane else [],
        layer_depth={"F.Cu": 0.0, "In1.Cu": 0.2},
        pads_by_ref={"XTAL2": pads})
    context.nearest_via = lambda net, point: (
        (ground_via_mm, None) if ground_via_mm is not None else None)
    return context


# -- the coverage property -------------------------------------------------

def test_a_resonator_the_check_cannot_loop_still_produces_a_finding():
    """The bug, as a property. Silence plus `crystals=1` was indistinguishable
    from a pass."""
    findings = check_crystal_loop(_resonator({"/XIN": 2.2, "/XOUT": 7.8}))
    assert findings, "a counted crystal must never produce nothing"
    assert findings[0].check == "crystal_loop"


def test_an_unrouted_crystal_says_it_was_not_measured():
    context = _resonator({"/XIN": 2.2, "/XOUT": 2.3})
    context.pads_by_ref["XTAL2"] = [
        SimpleNamespace(number="2", net_name="GND", pos=(1.0, 0.0),
                        copper_layers=["F.Cu"])]
    finding, = check_crystal_loop(context)
    assert "NOT measured" in finding.title
    assert finding.measurement["measured"] is False
    assert any("not measured" in line and "not 'measured and clean'" in line
               for line in finding.detail)


def test_every_finding_records_whether_it_measured_anything():
    """`coverage()` counts on this key, so it must be present on every path."""
    for context in (_resonator({"/XIN": 2.2, "/XOUT": 7.8}),
                    _resonator({"/XIN": 2.2, "/XOUT": 2.3})):
        for finding in check_crystal_loop(context):
            assert "measured" in finding.measurement


# -- what a 3-pin integrated-load resonator does have ----------------------

def test_the_reported_board_reproduces():
    """Murata CSTCE16M0V53-R0 on a CubeSat payload board, measured by hand in
    the session that found this: 2.20 mm and 7.77 mm terminals, ground pad
    5.48 mm from the nearest plane via."""
    finding, = check_crystal_loop(
        _resonator({"Net-(U9-PH0)": 2.20, "Net-(U9-PH1)": 7.77},
                   ground_via_mm=5.48))
    assert finding.severity == "minor"
    assert finding.measurement["length_ratio"] == pytest.approx(3.532, abs=1e-3)
    assert finding.measurement["ground_pad_to_via_mm"] == 5.48
    assert finding.measurement["kind"] == "integrated_load"
    assert "3.5x" in finding.title


def test_a_well_matched_resonator_is_info_not_a_finding_to_act_on():
    """The counterpart zero. It still reports, because "measured and clean" is
    a different statement from "not measured" and both must be visible."""
    finding, = check_crystal_loop(
        _resonator({"/XIN": 2.20, "/XOUT": 2.35}, ground_via_mm=0.4))
    assert finding.severity == "info"
    assert finding.measurement["measured"] is True


def test_a_badly_matched_resonator_is_ranked_above_a_mild_one():
    mild, = check_crystal_loop(_resonator({"/A": 2.0, "/B": 5.0}))
    bad, = check_crystal_loop(_resonator({"/A": 2.0, "/B": 20.0}))
    assert bad.score > mild.score


def test_the_thresholds_are_declared_as_judgement():
    """§9's rule: a number nobody measured must say so on the finding."""
    finding, = check_crystal_loop(_resonator({"/A": 2.0, "/B": 8.0}))
    assert "judgement, not" in (finding.assumption or "")


def test_no_via_on_the_ground_pad_is_reported_rather_than_omitted():
    finding, = check_crystal_loop(
        _resonator({"/A": 2.0, "/B": 8.0}, ground_via_mm=None))
    assert any("no via at all" in line for line in finding.detail)
    assert finding.measurement["ground_pad_to_via_mm"] is None
