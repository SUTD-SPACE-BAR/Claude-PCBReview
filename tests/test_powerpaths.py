"""Tests for the power-path manifest loader — `powerpaths`.

The loader refuses rather than half-loads. Every refusal here is a typo that,
silently accepted, would have produced a path that *reads* as checked: a
`current:` that should have been `current_a:`, a scenario naming a path that
is not there, an environment the heat model cannot place.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import powerpaths

GOOD = """\
environment: still_air
ambient_c: 40
mounts: {H1: 0.05}
paths:
  - name: rail
    net: "+5V"
    from: [U1.3-5]
    to:
      - {pads: [J1.1], current_a: 2.0}
      - {pads: [U2], current_a: 0.5, name: logic}
    rise_c: 20
    max_drop_mv: 100
  - name: kick
    net: "/OUT"
    from: [Q1.5]
    to: [{pads: [J2.1], current_a: 5}]
    pulse_s: 0.5
scenarios:
  - name: flight
    paths: {rail: 0.5}
    parts: false
"""


def _load(tmp_path, text):
    path = tmp_path / "b.powerpaths.yaml"
    path.write_text(text, encoding="utf-8")
    return powerpaths.load(path)


def test_a_complete_manifest_loads_as_written(tmp_path):
    m = _load(tmp_path, GOOD)
    assert m.environment == "still_air" and m.surface_w_m2k == 12.0
    assert m.ambient_c == 40 and m.mounts == {"H1": 0.05}
    rail = m.path("rail")
    assert rail.current_a == pytest.approx(2.5)
    assert rail.rise_c == 20 and rail.rise_stated and rail.max_drop_mv == 100
    assert [s.name for s in rail.sinks] == ["sink1", "logic"]
    kick = m.path("kick")
    assert kick.is_pulse and kick.rise_c == powerpaths.DEFAULT_RISE_C
    assert not kick.rise_stated
    assert m.scenarios[0].paths == {"rail": 0.5} and m.scenarios[0].parts is False


def test_a_numeric_environment_is_a_surface_coefficient(tmp_path):
    m = _load(tmp_path, GOOD.replace("still_air", "8"))
    assert m.surface_w_m2k == 8.0 and m.environment == "8 W/m^2K"


def test_an_unknown_environment_is_refused(tmp_path):
    with pytest.raises(powerpaths.PowerPathError, match="environment"):
        _load(tmp_path, GOOD.replace("still_air", "breezy"))


def test_a_misspelt_key_is_refused_not_ignored(tmp_path):
    with pytest.raises(powerpaths.PowerPathError, match="unknown key"):
        _load(tmp_path, GOOD.replace("current_a: 2.0", "current: 2.0"))


def test_a_sink_with_no_current_is_refused(tmp_path):
    text = GOOD.replace("{pads: [J1.1], current_a: 2.0}", "{pads: [J1.1]}")
    with pytest.raises(powerpaths.PowerPathError, match="no `current_a`"):
        _load(tmp_path, text)


def test_a_unit_in_a_number_is_refused(tmp_path):
    with pytest.raises(powerpaths.PowerPathError, match="not a number"):
        _load(tmp_path, GOOD.replace("current_a: 5}", "current_a: 5A}"))


def test_a_scenario_naming_a_missing_path_is_refused(tmp_path):
    with pytest.raises(powerpaths.PowerPathError, match="not declared"):
        _load(tmp_path, GOOD.replace("{rail: 0.5}", "{rial: 0.5}"))


def test_a_path_declared_twice_is_refused(tmp_path):
    text = GOOD.replace("name: kick", "name: rail")
    with pytest.raises(powerpaths.PowerPathError, match="declared twice"):
        _load(tmp_path, text)


def test_without_scenarios_everything_continuous_runs_together(tmp_path):
    text = GOOD.split("scenarios:")[0]
    m = _load(tmp_path, text)
    names = [s.name for s in m.scenarios]
    assert names == ["every continuous path together",
                     "kick pulse over every continuous path"]
    assert m.scenarios[0].paths == {"rail": 1.0}
    assert m.scenarios[1].paths == {"rail": 1.0, "kick": 1.0}
    assert all(s.implied for s in m.scenarios)


def test_the_manifest_lives_beside_the_board():
    assert powerpaths.default_path("/x/board.kicad_pcb") == Path("/x/board.powerpaths.yaml")


def test_the_shipped_example_loads():
    example = Path(__file__).resolve().parents[1] / "fixtures" / "example.powerpaths.yaml"
    m = powerpaths.load(example)
    assert [p.name for p in m.paths] == ["main_input", "motor_return", "solenoid"]
    assert m.path("solenoid").is_pulse and m.environment == "still_air"
