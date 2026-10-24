"""Tests for the power-path check — `ampacity`.

A two-layer fake board small enough to reason about by hand: a 2 mm strip on
the top layer between a source pad and a load pad, a plane-less bottom, and a
dissipating part off to one side. Every number the check reports is either
closed form here or ordered in a way that has to hold whatever the constants.

The slow test at the bottom runs the real thing on any registry board that
carries a `<board>.powerpaths.yaml`, and skips cleanly on one that does not.
"""

import math
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import MultiPolygon, Point, box

from pcbreview import ampacity, boardheat, dcflow, powerpaths

RHO = dcflow.RHO_OHM_M
OZ1 = 0.035


class _Stackup:
    layer_map = {}
    layers = [types.SimpleNamespace(name="F.Cu", is_copper=True, thickness_mm=OZ1),
              types.SimpleNamespace(name="B.Cu", is_copper=True, thickness_mm=OZ1)]

    @staticmethod
    def copper_z_positions():
        return {"F.Cu": (0.0, OZ1), "B.Cu": (1.6 - OZ1, 1.6)}


def _pad(ref, number, net, geom, layer="F.Cu"):
    c = geom.centroid
    return types.SimpleNamespace(ref=ref, number=str(number), net=net,
                                 designator=f"{ref}.{number}", copper=geom,
                                 copper_layers=[layer], is_plated_hole=False,
                                 drill_mm=(0.0, 0.0), pos=(c.x, c.y))


class FakeBoard:
    """Strip net P from U1.1 (x 2-3) to J1.1 (x 21-22) along y 9-11, a
    detached island J2.1 on the same net, and a part U2 that dissipates."""
    copper_layers = ["F.Cu", "B.Cu"]
    stackup = _Stackup()
    outline = box(0, 0, 30, 20)
    vias = []
    zones = []

    def __init__(self):
        self._nets = {1: types.SimpleNamespace(code=1, name="P"),
                      2: types.SimpleNamespace(code=2, name="Q")}
        self.nets = self._nets
        self.pads = [_pad("U1", 1, 1, box(2, 9, 3, 11)),
                     _pad("J1", 1, 1, box(21, 9, 22, 11)),
                     _pad("J2", 1, 1, box(26, 2, 27, 3)),
                     _pad("U2", 1, 2, box(10, 2, 12, 4))]
        self.footprints = [types.SimpleNamespace(ref=r, pos=p.pos, layer="F.Cu")
                           for r, p in (("U1", self.pads[0]), ("J1", self.pads[1]),
                                        ("J2", self.pads[2]), ("U2", self.pads[3]))]
        self.copper = {"P": MultiPolygon([box(2, 9, 22, 11), box(26, 2, 27, 3)]),
                       "Q": box(10, 2, 12, 4)}

    def net(self, key):
        for n in self._nets.values():
            if key in (n.code, n.name):
                return n
        raise KeyError(key)

    def copper_on_layer(self, layer, net=None):
        if layer != "F.Cu":
            return MultiPolygon()
        if net is None:
            return MultiPolygon([*self.copper["P"].geoms, self.copper["Q"]])
        return self.copper[self.net(net).name]


def _manifest(tmp_path, text):
    path = tmp_path / "b.powerpaths.yaml"
    path.write_text(text, encoding="utf-8")
    return powerpaths.load(path)


PATH = """\
paths:
  - name: strip
    net: P
    from: [U1.1]
    to: [{pads: [J1.1], current_a: 3.0}]
"""


def _find(result, words):
    return [f for f in result.findings if all(w in f.title for w in words)]


def test_the_drop_is_the_strips_resistance_times_the_current(tmp_path):
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH),
                               thermal_cell_mm=0.5)
    sol = result.paths[0].solution
    exact = RHO * 18e-3 / (2e-3 * OZ1 * 1e-3) * 3.0
    assert sol.worst_drop_v == pytest.approx(exact, rel=0.03)
    assert sol.loss_w == pytest.approx(3.0 * sol.worst_drop_v, rel=0.01)


def test_temperatures_order_with_the_surroundings(tmp_path):
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH),
                               thermal_cell_mm=0.5)
    by_env = result.scenarios[0].by_env
    rises = [by_env[e].copper_max[0] for e in ("enclosed", "still_air", "forced_air")]
    assert rises[0] > rises[1] > rises[2] > 0
    assert result.verdict_env == "enclosed" and not result.verdict_declared


def test_own_rise_is_judged_in_still_air_and_the_board_at_the_worst_case(tmp_path):
    """IPC-2152 defines a conductor's rise in still air, and the default +10 C
    allowance comes from there; an absolute board limit is judged at the
    worst case. A declared environment overrides both."""
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH),
                               thermal_cell_mm=0.5)
    assert result.rise_env == "still_air" and result.verdict_env == "enclosed"
    own, = _find(result, ["its own current heats"])
    board, = _find(result, ["the board reaches"])
    still = result.scenarios[0].by_env["still_air"].path_rise["strip"]
    assert own.measurement["environment"] == "still_air"
    assert own.measurement["rise_c"] == pytest.approx(still, abs=0.01)
    assert board.measurement["environment"] == "enclosed"

    declared = ampacity.evaluate(
        FakeBoard(), _manifest(tmp_path, "environment: forced_air\n" + PATH),
        thermal_cell_mm=0.5)
    assert declared.rise_env == declared.verdict_env == "forced_air"
    own, = _find(declared, ["its own current heats"])
    assert own.measurement["environment"] == "forced_air"


def test_the_mean_board_rise_balances_the_loss_against_the_surface(tmp_path):
    """Energy has nowhere else to go, so area-weighted mean rise of the two
    faces times the surface conductance must equal the copper loss."""
    board = FakeBoard()
    result = ampacity.evaluate(board, _manifest(tmp_path, PATH), thermal_cell_mm=0.5)
    field = result.maps[result.scenarios[0].scenario.name]
    import numpy as np
    top, bottom = field.rise_c["F.Cu"], field.rise_c["B.Cu"]
    area = (0.5e-3) ** 2
    h = boardheat.ENVIRONMENTS["enclosed"]
    lost = h * area * (np.nansum(top) + np.nansum(bottom))
    assert lost == pytest.approx(result.paths[0].solution.loss_w, rel=0.01)


def test_a_drop_over_the_declared_limit_is_a_finding(tmp_path):
    text = PATH + "    max_drop_mv: 1\n"
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, text),
                               thermal_cell_mm=0.5)
    path_finding = _find(result, ["strip:", "drops"])[0]
    assert path_finding.severity in ("minor", "major", "critical")


def test_a_sink_on_detached_copper_is_a_critical_finding(tmp_path):
    text = PATH.replace("[J1.1]", "[J2.1]")
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, text),
                               thermal_cell_mm=0.5)
    f = _find(result, ["does not exist in copper"])
    assert f and f[0].severity == "critical"


def test_a_pad_the_net_does_not_have_refuses_the_manifest(tmp_path):
    text = PATH.replace("[J1.1]", "[J1.7]")
    with pytest.raises(powerpaths.PowerPathError, match="J1 has J1.1"):
        ampacity.evaluate(FakeBoard(), _manifest(tmp_path, text), thermal_cell_mm=0.5)


def test_a_pulse_reports_its_rise_and_the_adiabatic_bound(tmp_path):
    text = PATH + "    pulse_s: 0.5\n    rise_c: 60\n"
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, text),
                               thermal_cell_mm=0.5)
    f = _find(result, ["strip:", "pulse"])
    assert f, [x.title for x in result.findings]
    assert any("would rise" in line for line in f[0].detail)
    # the transient rise is below the steady rise of the same current
    steady = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH),
                               thermal_cell_mm=0.5)
    pulsed = result.scenarios[0].by_env["enclosed"].pulse_conductor["strip"]
    held = steady.scenarios[0].by_env["enclosed"].path_rise["strip"]
    assert 0 < pulsed < held


def test_a_part_raises_the_board_its_neighbours_sit_on(tmp_path):
    part = types.SimpleNamespace(dissipation_w=0.5, dissipation_stated=True)
    thermal = types.SimpleNamespace(parts={"U2": part}, ambient_c=30.0)
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH), thermal,
                               thermal_cell_mm=0.5)
    sc = result.scenarios[0].by_env["enclosed"]
    assert sc.total_max[0] > sc.copper_max[0]
    assert result.ambient_c == 30.0 and "thermal manifest" in result.ambient_source
    assert "U2" in result.neighbour


def test_power_nets_without_a_path_are_named(tmp_path):
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH),
                               thermal_cell_mm=0.5, power_nets={"P", "Q"})
    f = _find(result, ["no declared path"])
    assert f and f[0].nets == ["Q"]


def test_findings_carry_basis_and_assumption(tmp_path):
    result = ampacity.evaluate(FakeBoard(), _manifest(tmp_path, PATH),
                               thermal_cell_mm=0.5)
    for f in result.findings:
        assert f.check == ampacity.CHECK and f.basis


# -- a real board, when the registry has one with a manifest --------------

@pytest.mark.slow
def test_a_registry_board_with_power_paths_balances_its_energy():
    """On a real board nothing is closed form, but energy still has to
    balance: every path's I^2R must equal the sum of drop x current over its
    sinks. Runs on the first installed board with a manifest beside it."""
    from tests import boards
    from pcbreview import pipeline
    chosen = None
    for role in boards.any_installed():
        pcb = boards.board_path(role)
        manifest = powerpaths.default_path(pcb)
        if manifest.exists():
            chosen = (role, pcb, manifest)
            break
    if chosen is None:
        pytest.skip("no installed registry board has a <board>.powerpaths.yaml "
                    "beside it — write one with `python -m pcbreview.ampacity "
                    "<project> --template` to switch this on")
    role, pcb, manifest = chosen
    board = pipeline._load_board(pcb, boards.stackup_path(role))
    result = ampacity.evaluate(board, powerpaths.load(manifest))
    solved = [o for o in result.paths if o.solution is not None]
    assert solved
    for o in solved:
        delivered = sum(o.solution.drop_v[f"sink{k + 1}"] * s.current_a
                        for k, s in enumerate(o.path.sinks))
        assert o.solution.loss_w == pytest.approx(delivered, rel=0.02), o.path.name
