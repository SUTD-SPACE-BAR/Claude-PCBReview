"""Tests for the placement checks.

Both fabricated boards are quiet here, which is the right answer and also a
problem for testing: a check that has never fired proves nothing. So each check
is covered twice — once against a real board, asserting the *zero* and the
coverage that makes the zero meaningful, and once against a constructed board
that makes it fire.

Three of these tests exist because the first version of the check was wrong on
real hardware:

* `test_the_shunt_must_be_a_resistor` — the two parts bridging U15's IN+/IN- on
  The six-layer board are both capacitors, and taking any bridging part reported them as
  badly-tapped shunts.
* `test_pin_pitch_proximity_is_not_a_feedback_finding` — measuring closest
  approach reported U10 at 0.292 mm, which is the package pin pitch.
* `test_supply_pins_are_found_by_type_not_by_being_on_a_rail` — selecting pads
  on a rail belonging to a multi-pad part pulled in connector shells and MOSFET
  drains, 49 candidates for 11 real ones.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import placement
from pcbreview.model import Board
from pcbreview.schematic import netlist
from pcbreview.stackup import load_and_validate


def test_a_capacitor_with_a_distant_ground_via_is_reported():
    """Constructed, because neither real board has one.

    Thirteen identical well-placed loops and one whose ground via is 20 mm
    away. Same capacitor-to-pin distance in every case, so only the via
    position can produce the finding.
    """
    context = _synthetic_context(
        [(f"U{i}", 1.0, 0.2) for i in range(1, 14)] + [("U14", 1.0, 20.0)])
    found = placement.check_decoupling_loops(context)
    assert [f.measurement["pin"] for f in found] == ["U14.1"]
    assert found[0].severity == "major"
    assert found[0].measurement["cap_to_ground_via_mm"] == pytest.approx(20.0)


def test_a_board_with_too_few_loops_says_so_rather_than_going_quiet():
    """Two loops give no median worth comparing against — and that must be
    *stated*, not expressed as an empty list.

    This test asserted silence until 2026-08-06, which made it a pin on the
    coverage lie rather than a guard against it: an outside session could not
    tell "measured and within limits" from "not measured" for twelve loops on
    a real board, and the empty return was why."""
    context = _synthetic_context([("U1", 1.0, 0.2), ("U2", 1.0, 20.0)])
    found = placement.check_decoupling_loops(context)
    assert len(found) == 1
    assert found[0].severity == "info"
    assert "NOT compared" in found[0].title
    assert found[0].measurement["loops"] == 2


def test_the_absolute_backstop_catches_a_uniformly_bad_board():
    """If every loop is equally awful the relative test sees nothing, so the
    absolute threshold has to carry it."""
    context = _synthetic_context([(f"U{i}", 1.0, 130.0) for i in range(1, 6)])
    found = placement.check_decoupling_loops(context)
    assert len(found) == 5
    assert all(f.severity == "major" for f in found)
    assert all(f.measurement["area_mm2"] >= placement.DECOUPLING_ABSOLUTE_MAJOR_MM2
               for f in found)


# -- ESD clamp position ----------------------------------------------------

def _esd_context(clamp_pads, victim_x, clamp_ref="U9",
                 clamp_value="USBLC6-2SC6", connector=True):
    """A connector at x=0 on `/D+`, a clamp, and a victim, all on one line.

    `clamp_pads` is the list of x positions the clamp lands on that net — a
    two-pin diode gives one, a USB array gives two.

    `connector=False` drops J2, which is the case that made this check report
    `esd_clamps: 11` and nothing else on a real board: with no entry point the
    clamp's position on the line cannot be measured at all.
    """
    from pcbreview.model import Pad
    from pcbreview.schematic import Component, Net, PinRef, Schematic

    def pad(ref, number, net, net_name, x):
        return Pad(ref=ref, number=number, net=net, net_name=net_name,
                   shape="rect", attribute="smd", pos=(x, 0.0),
                   size_mm=(0.5, 0.5), orientation_deg=0.0,
                   drill_mm=(0.0, 0.0), layers=["F.Cu"], copper_layers=["F.Cu"])

    pads_by_ref = {
        clamp_ref: ([pad(clamp_ref, str(i + 1), 5, "/D+", x)
                     for i, x in enumerate(clamp_pads)]
                    + [pad(clamp_ref, "9", 1, "GND", 0.0)]),
        "U1": [pad("U1", "14", 5, "/D+", victim_x)],
    }
    if connector:
        pads_by_ref["J2"] = [pad("J2", "3", 5, "/D+", 0.0)]
    every = [p for ps in pads_by_ref.values() for p in ps]
    components = [
        Component(reference=clamp_ref, value=clamp_value, footprint="",
                  lib_id=f"Power_Protection:{clamp_value}", unit=1, dnp=False,
                  sheet=0, uuid=None,
                  pins=[PinRef(clamp_ref, "1", "IO1", "passive", 0)]),
        Component(reference="U1", value="MCU", footprint="", lib_id="x:y",
                  unit=1, dnp=False, sheet=0, uuid=None,
                  pins=[PinRef("U1", "14", "USB_D+", "bidirectional", 0)]),
    ]
    if connector:
        components.insert(0, Component(
            reference="J2", value="USB_C", footprint="",
            lib_id="Connector:USB_C", unit=1, dnp=False, sheet=0, uuid=None,
            pins=[PinRef("J2", "3", "D+", "passive", 0)]))
    schematic = Schematic(root=Path("t.kicad_sch"), sheets=[],
                          components=components,
                          nets=[Net(name="/D+", source="local", pins=[])],
                          diagnostics=[])

    class _Board:
        copper_layers = ["F.Cu", "In1.Cu", "B.Cu"]

        def pads_on_net(self, key):
            return [p for p in every if p.net == 5]

    return placement.PlacementContext(
        board=_Board(), schematic=schematic, ground_nets={"GND"},
        supply_nets=set(), plane_layers=["In1.Cu"],
        layer_depth={"F.Cu": 0.0, "In1.Cu": 0.1, "B.Cu": 0.2},
        pads_by_ref=pads_by_ref)


def _above_info(findings):
    return [f for f in findings if f.severity != "info"]


def test_a_clamp_downstream_of_what_it_protects_is_reported():
    """Constructed: no board here has a misplaced clamp (§32.C)."""
    found = _above_info(
        placement.check_esd_position(_esd_context([8.0], victim_x=3.0)))
    assert len(found) == 1
    assert found[0].measurement["clamp_mm"] == pytest.approx(8.0)
    assert found[0].measurement["protected_mm"] == pytest.approx(3.0)
    assert found[0].severity == "minor"


def test_a_clamp_upstream_of_what_it_protects_says_so_rather_than_going_quiet():
    """A correctly placed clamp raises nothing to act on — **and still says it
    was measured.**

    This asserted `== []` until the coverage-lie audit. Silence was the bug:
    one real board reported `esd_clamps: 11` and no findings at all while
    thirteen of its fourteen clamped lines had never been compared, and a
    reader cannot tell that apart from eleven clean clamps. The clean case now
    carries `measured: True` and the declining paths carry `measured: False`.
    """
    found = placement.check_esd_position(_esd_context([1.0], victim_x=3.0))
    assert _above_info(found) == []
    assert [f.measurement["measured"] for f in found] == [True]
    assert found[0].measurement["lines_measured"] == 1


def test_a_multi_line_array_is_examined_and_not_skipped_for_its_pin_count():
    """§40. The check bailed on `len(pads) != 2`, so `USBLC6-2SC6` — the part
    `ESD_HINT` names by name, and the usual ESD part on a USB port — was thrown
    away on every board. Two pads land on this line; the one the strike meets
    first is the one that counts."""
    found = _above_info(placement.check_esd_position(
        _esd_context([8.0, 9.0], victim_x=3.0)))
    assert [f.measurement["clamp_mm"] for f in found] == [pytest.approx(8.0)]
    clean = placement.check_esd_position(_esd_context([1.0, 9.0], victim_x=3.0))
    assert _above_info(clean) == []
    assert all(f.measurement["measured"] for f in clean)


def test_a_clamped_line_with_no_connector_is_reported_as_not_measured():
    """The §52.1 pattern, on the check where it cost the most.

    Without a connector pad on the line there is no entry point, so "is the
    clamp upstream of the victim" has no answer. Returning nothing made that
    indistinguishable from "yes, it is".
    """
    context = _esd_context([8.0], victim_x=3.0, connector=False)
    found = placement.check_esd_position(context)
    assert _above_info(found) == []
    assert [f.measurement["measured"] for f in found] == [False]
    assert "no connector pad" in found[0].measurement["reason"]
    assert "NOT measured" in found[0].title


#: Synthetic rows are this far apart in y, so one row's vias are never the
#: nearest via to another row's pads.
_ROW_PITCH_MM = 1000.0


def _synthetic_context(parts) -> placement.PlacementContext:
    """A context with one supply pin and one capacitor per entry.

    `parts` is `(reference, pin_to_cap_mm, via_distance_mm)`, one row each.
    Everything sits on F.Cu over a plane 0.1 mm below.

    `via_distance_mm` moves *both* ground vias away from their pads, which is
    the realistic version of a bad return: a region of the board with sparse
    stitching. Moving only the capacitor's via does nothing, because the via in
    the IC's own ground pad is then the nearest one to the capacitor as well —
    which is a correct answer, and is why the first version of this fixture
    could not make the check fire.

    So the loop is exactly
    `0.1 * (pin_to_cap + 0.5 + 2 * via_distance)`, checkable by hand.
    """
    from pcbreview.model import Pad, Via
    from pcbreview.schematic import Component, Net, PinRef, Schematic

    def pad(reference, number, net_code, net_name, x, y):
        return Pad(ref=reference, number=number, net=net_code,
                   net_name=net_name, shape="rect", attribute="smd",
                   pos=(x, y), size_mm=(0.5, 0.5), orientation_deg=0.0,
                   drill_mm=(0.0, 0.0), layers=["F.Cu"], copper_layers=["F.Cu"])

    pads_by_ref: dict[str, list[Pad]] = {}
    components: list[Component] = []
    nets: list[Net] = []
    vias: list[Via] = []
    ground_pins: list[PinRef] = []
    y = 0.0

    for index, (reference, to_cap, via_distance) in enumerate(parts):
        y = index * _ROW_PITCH_MM
        capacitor = f"C{index + 1}"
        rail = f"+3V3_{index}"
        # IC: pin 1 on the rail at x=0, pin 2 on ground at x=0
        pads_by_ref[reference] = [
            pad(reference, "1", 10 + index, rail, 0.0, y),
            pad(reference, "2", 1, "GND", 0.0, y),
        ]
        # Capacitor: supply pad at x=to_cap, ground pad 0.5 mm further
        pads_by_ref[capacitor] = [
            pad(capacitor, "1", 10 + index, rail, to_cap, y),
            pad(capacitor, "2", 1, "GND", to_cap + 0.5, y),
        ]
        # One ground via `via_distance` outboard of each ground pad, so both
        # returns travel that far and neither is shortcut by the other.
        for offset in (-via_distance, to_cap + 0.5 + via_distance):
            vias.append(Via(id=len(vias), net=1, pos=(offset, y), drill_mm=0.3,
                            via_type="through", top_layer="F.Cu",
                            bottom_layer="B.Cu",
                            layers=["F.Cu", "In1.Cu", "B.Cu"],
                            width_by_layer={"F.Cu": 0.6}))

        supply_pin = PinRef(reference, "1", "VDD", "power_in", 0)
        components.append(Component(
            reference=reference, value="IC", footprint="", lib_id="x:y",
            unit=1, dnp=False, sheet=0, uuid=None,
            pins=[supply_pin, PinRef(reference, "2", "VSS", "power_in", 0)]))
        components.append(Component(
            reference=capacitor, value="100n", footprint="", lib_id="Device:C",
            unit=1, dnp=False, sheet=0, uuid=None,
            pins=[PinRef(capacitor, "1", "", "passive", 0),
                  PinRef(capacitor, "2", "", "passive", 0)]))
        nets.append(Net(name=rail, source="power", pins=[
            supply_pin, PinRef(capacitor, "1", "", "passive", 0)]))
        ground_pins += [PinRef(reference, "2", "VSS", "power_in", 0),
                        PinRef(capacitor, "2", "", "passive", 0)]

    nets.append(Net(name="GND", source="power", pins=ground_pins))
    schematic = Schematic(root=Path("t.kicad_sch"), sheets=[],
                          components=components, nets=nets, diagnostics=[])

    class _Board:
        copper_layers = ["F.Cu", "In1.Cu", "B.Cu"]

        def net(self, key):
            from pcbreview.model import Net as BoardNet
            return BoardNet(code=1 if key in (1, "GND") else 10, name="GND")

        def vias_on_net(self, key):
            return [v for v in vias if v.net == (1 if key in (1, "GND") else 10)]

        def pads_on_net(self, key):
            return []

    return placement.PlacementContext(
        board=_Board(), schematic=schematic,
        ground_nets={"GND"},
        supply_nets={f"+3V3_{i}" for i in range(len(parts))},
        plane_layers=["In1.Cu"],
        layer_depth={"F.Cu": 0.0, "In1.Cu": 0.1, "B.Cu": 0.2},
        pads_by_ref=pads_by_ref)


def test_the_synthetic_loop_arithmetic_is_what_it_claims():
    """Guards the synthetic tests above: 0.1 mm depth, so a 1.0 + 0.5 + 0.2 + 0
    mm surface run must be exactly 0.17 mm²."""
    context = _synthetic_context([("U1", 1.0, 0.2)] * 3)
    loop = placement.decoupling_loops(context)[0]
    assert loop.depth_mm == pytest.approx(0.1)
    # 1.0 pin->cap + 0.5 cap body + 0.2 + 0.2 for the two ground vias
    assert loop.surface_mm == pytest.approx(1.9)
    assert loop.area_mm2 == pytest.approx(0.19)


# -- the coverage lie, as a property ---------------------------------------
#
# `check_crystal_loop` was fixed for this in §52.1: a candidate counted in
# `coverage()` and then skipped is indistinguishable from one examined and
# found clean, because the count itself reads as evidence the check ran.
#
# Auditing the rest of `placement.py` with `sys.settrace` over five real boards
# found three more, and the crystal was not the worst of them. One board
# reported `esd_clamps: 11` and no findings at all while **thirteen of its
# fourteen clamped lines had never been compared** — no connector pad sat on
# them, so "is the clamp upstream of the victim" had no answer at all.
#
# These tests are board-free on purpose, so they ship (§50.9).

def _sense_amp_context(value, pin_names, reference="U8"):
    """A part with the given pin names, and pads for each."""
    from types import SimpleNamespace
    pads = [SimpleNamespace(number=str(i + 1), net_name=f"/N{i}",
                            net=i + 1, pos=(float(i), 0.0),
                            copper_layers=["F.Cu"])
            for i in range(len(pin_names))]
    component = SimpleNamespace(
        reference=reference, value=value, lib_id=f"lib:{value}",
        description="", dnp=False,
        pins=[SimpleNamespace(number=str(i + 1), name=name, type="passive")
              for i, name in enumerate(pin_names)])
    return placement.PlacementContext(
        board=None, schematic=SimpleNamespace(components=[component]),
        ground_nets=set(), supply_nets=set(), plane_layers=[],
        layer_depth={}, pads_by_ref={reference: pads})


def test_bare_plus_minus_on_a_sense_amp_family_is_recognised():
    """The measured miss: a board with **eight INA180A3 shunt amplifiers**
    reported `sense_pins: 0` — the exact topology `check_kelvin_sense` exists
    for — because the symbol names its inputs bare `+` and `-`."""
    found = placement.sense_pins(
        _sense_amp_context("INA180A3", ["+", "-", "GND", "REF", "OUT"]))
    assert sorted(name for _, _, name, _, _ in found) == ["+", "-"]
    assert {how for *_, how in found} == {"sense_amp_value"}


def test_a_connector_with_plus_and_minus_pins_is_not_a_sense_amp():
    """The reason this is a value-and-pin-count test and not a pin-name one.
    An XT60 power connector has exactly one `+` pin and one `-` pin, and is the
    only other part on any of the eight designs here that does."""
    assert placement.sense_pins(
        _sense_amp_context("XT60_PW-M", ["+", "-"], reference="J1")) == []


def test_a_two_pin_part_named_like_a_sense_amp_is_still_rejected():
    """Belt and braces on the pin count: a real current-sense amplifier has a
    supply, a reference and an output besides its two inputs."""
    assert placement.sense_pins(
        _sense_amp_context("INA180A3", ["+", "-"], reference="J9")) == []


def test_the_named_pin_route_still_wins_and_is_labelled():
    found = placement.sense_pins(
        _sense_amp_context("INA219", ["IN+", "IN-", "GND", "SDA", "SCL"]))
    assert {how for *_, how in found} == {"pin_name"}


def test_a_feedback_pin_nothing_could_measure_says_so():
    """The one an AST scan for `continue` cannot find: this path is a falsy
    `if divider:` block that simply does not execute. Two of the five boards
    here have a feedback pin in exactly that state."""
    from types import SimpleNamespace
    pad = SimpleNamespace(ref="U2", number="3", name="FB", net=7,
                          net_name="/FB", pos=(0.0, 0.0),
                          copper_layers=["F.Cu"])
    component = SimpleNamespace(
        reference="U2", value="XL1509-3.3", lib_id="lib:XL1509", description="",
        dnp=False, pins=[SimpleNamespace(number="3", name="FB", type="input")])

    class _Board:
        def pads_on_net(self, net):
            return [pad]           # only the feedback pin itself

    context = placement.PlacementContext(
        board=_Board(), schematic=SimpleNamespace(components=[component]),
        ground_nets=set(), supply_nets=set(), plane_layers=[],
        layer_depth={}, pads_by_ref={"U2": [pad]})

    finding, = placement.check_regulator_feedback(context)
    assert finding.severity == "info"
    assert "NOT measured" in finding.title
    assert finding.measurement["measured"] is False
    assert "no resistor sits on its net" in finding.measurement["reason"]
    assert "no pin on this board is named as a switch node" in \
        finding.measurement["reason"]


def test_every_placement_finding_says_whether_it_measured():
    """The contract `coverage()` counts on. A finding from one of these checks
    that carries no `measured` key is invisible to the found/measured pair, and
    the pair is the whole mechanism."""
    contexts = [_esd_context([1.0], victim_x=3.0),
                _esd_context([8.0], victim_x=3.0),
                _esd_context([8.0], victim_x=3.0, connector=False)]
    for context in contexts:
        for finding in placement.check_esd_position(context):
            assert "measured" in finding.measurement, finding.title
