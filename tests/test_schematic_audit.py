"""Tests for the schematic-only entry point.

Two boards that were fabricated and work are the ground truth here, and the
measurement that matters is the same one that fixed `crosstalk` and `audit`:
read the output and check that what it reports is real. A schematic review that
fires on a working board is worse than none.

Several of these tests assert a *zero* — no floating pins, no output conflicts,
no undecoupled rails. Those are load-bearing, because each of those checks had
a naive form that produced tens of false findings on these boards, and the zero
is what says the correction held.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.findings import AuditError
from pcbreview.schematic import Component, Net, PinRef, Schematic, netlist
from pcbreview.schematic_audit import (CHECKS, audit_schematic,
                                       audit_schematic_file,
                                       check_decoupling, check_floating_pins,
                                       check_output_conflicts,
                                       pin_type_coverage)


def _synthetic(components, nets) -> Schematic:
    return Schematic(root=Path("t.kicad_sch"), sheets=[],
                     components=components, nets=nets, diagnostics=[])


def _pin(reference, number, name, type_):
    return PinRef(reference=reference, number=number, name=name,
                  type=type_, sheet=0)


def test_an_empty_netlist_is_refused_rather_than_reported_clean():
    with pytest.raises(AuditError):
        audit_schematic(_synthetic([], []))


def test_coverage_counts_unspecified_as_unseen_not_as_benign():
    nets = [Net(name="N", pins=[_pin("U1", "1", "A", "unspecified"),
                                _pin("U2", "1", "B", "input")], source="local")]
    coverage = pin_type_coverage(_synthetic([], nets))
    assert coverage["unspecified"] == 1
    assert coverage["typed_fraction"] == 0.5


def test_a_pin_the_symbol_types_no_connect_is_not_floating():
    nets = [Net(name="unconnected-(U1-Reserved-Pad10)",
                pins=[_pin("U1", "10", "Reserved", "no_connect")],
                source="auto")]
    assert check_floating_pins(_synthetic([], nets)) == []


def test_a_pin_with_a_no_connect_marker_is_not_floating():
    nets = [Net(name="n", pins=[_pin("U1", "3", "SPARE", "input")],
                source="auto", has_no_connect=True)]
    assert check_floating_pins(_synthetic([], nets)) == []


def test_a_genuinely_floating_input_is_reported():
    nets = [Net(name="n", pins=[_pin("U1", "3", "RESET", "input")],
                source="auto")]
    found = check_floating_pins(_synthetic([], nets))
    assert len(found) == 1
    assert found[0].severity == "minor"
    assert found[0].measurement["pin"] == "U1.3"


def test_a_lone_passive_pin_is_info_not_a_defect():
    """A test point or a mounting hole. Real, and not a problem."""
    nets = [Net(name="n", pins=[_pin("TP1", "1", "", "passive")],
                source="auto")]
    found = check_floating_pins(_synthetic([], nets))
    assert [f.severity for f in found] == ["info"]


def test_two_devices_driving_one_net_is_a_conflict():
    nets = [Net(name="CLK", pins=[_pin("U1", "5", "Y", "output"),
                                  _pin("U2", "7", "OUT", "output")],
                source="local")]
    found = check_output_conflicts(_synthetic([], nets))
    assert len(found) == 1
    assert found[0].severity == "major"
    assert found[0].measurement["devices"] == ["U1", "U2"]


def test_a_rail_with_no_capacitor_to_ground_is_reported():
    ground = Net(name="GND", pins=[_pin("C1", "2", "", "passive"),
                                   _pin("C2", "2", "", "passive"),
                                   _pin("U1", "8", "VSS", "power_in")],
                 source="power")
    supply_a = Net(name="+3V3", pins=[_pin("C1", "1", "", "passive"),
                                      _pin("U1", "1", "VDD", "power_in")],
                   source="power")
    supply_b = Net(name="+1V8", pins=[_pin("U1", "2", "VDDIO", "power_in"),
                                      _pin("U2", "3", "VOUT", "output")],
                   source="power")
    other = Net(name="+5V", pins=[_pin("C2", "1", "", "passive"),
                                  _pin("U2", "1", "VIN", "power_in")],
                source="power")

    def cap(reference, pins):
        return Component(reference=reference, value="100n", footprint="",
                         lib_id="Device:C", unit=1, dnp=False, sheet=0,
                         uuid=None, pins=pins)

    components = [
        cap("C1", [_pin("C1", "1", "", "passive"), _pin("C1", "2", "", "passive")]),
        cap("C2", [_pin("C2", "1", "", "passive"), _pin("C2", "2", "", "passive")]),
    ]
    schematic = _synthetic(components, [ground, supply_a, supply_b, other])
    found = check_decoupling(schematic)
    assert [f.measurement["net"] for f in found] == ["+1V8"]
    assert found[0].measurement["power_pins"] == ["U1.2"]


def test_an_enable_pin_typed_power_in_is_not_an_undecoupled_rail():
    """Libraries type enable pins `power_in`; a capacitor there would be wrong.

    Reported as `info` rather than dropped, because the mistyping is itself a
    small defect and a reader needs to know the net was skipped.
    """
    nets = [
        Net(name="GND", pins=[_pin("C1", "2", "", "passive")], source="power"),
        Net(name="/RADIO EN", pins=[_pin("U5", "3", "EN", "power_in"),
                                    _pin("U1", "7", "GPIO", "bidirectional")],
            source="local"),
    ]
    found = check_decoupling(_synthetic([], nets))
    assert len(found) == 1
    assert found[0].severity == "info"
    assert found[0].measurement["control_pins"] == ["U5.3"]


def test_the_control_pin_test_is_the_whole_name_not_a_substring():
    """`VCC_EN` is a rail's enable line; `EN` is the pin itself."""
    from pcbreview.schematic_audit import is_control_pin
    assert is_control_pin(_pin("U1", "1", "EN", "power_in"))
    assert is_control_pin(_pin("U1", "1", "~{SHDN}", "power_in"))
    assert not is_control_pin(_pin("U1", "1", "VCC_EN", "power_in"))
    assert not is_control_pin(_pin("U1", "1", "VS", "power_in"))
    assert not is_control_pin(_pin("U1", "1", "VDD", "power_in"))


def test_the_decoupling_check_says_proximity_is_not_its_question():
    """It is a topological check on purpose; loop area needs the layout."""
    def cap(reference):
        return Component(reference=reference, value="100n", footprint="",
                         lib_id="Device:C", unit=1, dnp=False, sheet=0,
                         uuid=None,
                         pins=[_pin(reference, "1", "", "passive"),
                               _pin(reference, "2", "", "passive")])

    finding = check_decoupling(_synthetic([cap("C1"), cap("C9")], [
        Net(name="GND", pins=[_pin("C1", "2", "", "passive"),
                              _pin("C9", "2", "", "passive")], source="power"),
        Net(name="V1", pins=[_pin("C1", "1", "", "passive"),
                             _pin("U9", "1", "VDD", "power_in")], source="power"),
        Net(name="V2", pins=[_pin("C9", "1", "", "passive"),
                             _pin("U9", "2", "VDD", "power_in")], source="power"),
        Net(name="V3", pins=[_pin("U9", "3", "VDD", "power_in"),
                             _pin("U8", "1", "O", "output")], source="power"),
    ]))
    assert len(finding) == 1
    assert any("layout question" in line for line in finding[0].detail)


def test_an_open_drain_pin_with_no_pullup_is_reported():
    nets = [
        Net(name="GND", pins=[_pin("C1", "2", "", "passive"),
                              _pin("C2", "2", "", "passive")], source="power"),
        Net(name="+3V3", pins=[_pin("C1", "1", "", "passive"),
                               _pin("U1", "8", "VDD", "power_in")],
            source="power"),
        Net(name="+5V", pins=[_pin("C2", "1", "", "passive"),
                              _pin("U2", "8", "VDD", "power_in")],
            source="power"),
        Net(name="ALERT", pins=[_pin("U1", "3", "ALERT", "open_collector"),
                                _pin("U2", "4", "IRQ", "input")],
            source="local"),
    ]

    def cap(reference):
        return Component(reference=reference, value="100n", footprint="",
                         lib_id="Device:C", unit=1, dnp=False, sheet=0,
                         uuid=None,
                         pins=[_pin(reference, "1", "", "passive"),
                               _pin(reference, "2", "", "passive")])

    from pcbreview.schematic_audit import check_missing_pullups
    found = check_missing_pullups(_synthetic([cap("C1"), cap("C2")], nets))
    assert [f.measurement["net"] for f in found] == ["ALERT"]


def test_an_open_drain_pin_pulled_to_a_supply_is_not_reported():
    nets = [
        Net(name="GND", pins=[_pin("C1", "2", "", "passive"),
                              _pin("C2", "2", "", "passive")], source="power"),
        Net(name="+3V3", pins=[_pin("C1", "1", "", "passive"),
                               _pin("R1", "1", "", "passive"),
                               _pin("U1", "8", "VDD", "power_in")],
            source="power"),
        Net(name="+5V", pins=[_pin("C2", "1", "", "passive"),
                              _pin("U2", "8", "VDD", "power_in")],
            source="power"),
        Net(name="ALERT", pins=[_pin("U1", "3", "ALERT", "open_collector"),
                                _pin("R1", "2", "", "passive"),
                                _pin("U2", "4", "IRQ", "input")],
            source="local"),
    ]

    def two_pin(reference, value, lib):
        return Component(reference=reference, value=value, footprint="",
                         lib_id=lib, unit=1, dnp=False, sheet=0, uuid=None,
                         pins=[_pin(reference, "1", "", "passive"),
                               _pin(reference, "2", "", "passive")])

    from pcbreview.schematic_audit import check_missing_pullups
    components = [two_pin("C1", "100n", "Device:C"),
                  two_pin("C2", "100n", "Device:C"),
                  two_pin("R1", "4k7", "Device:R")]
    assert check_missing_pullups(_synthetic(components, nets)) == []


def test_a_missing_file_is_reported_by_name(capsys, tmp_path):
    from pcbreview.schematic_audit import main

    assert main(["prog", str(tmp_path / "absent.kicad_sch")]) == 2
    assert "could not be read" in capsys.readouterr().err


def test_usage_is_printed_with_no_arguments(capsys):
    from pcbreview.schematic_audit import main

    assert main(["prog"]) == 2
    assert "usage" in capsys.readouterr().err
