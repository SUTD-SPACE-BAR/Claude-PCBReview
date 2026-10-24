"""Tests for multi-layer DC current flow — `dcflow`.

Every number here is checked against closed form, at two grid pitches where
the grid matters, so the error is seen to *fall* with the pitch rather than
merely to sit inside a tolerance. That is the only reason the solver is
trusted on a real board, where nothing is closed form.

Pinned on purpose:

* `test_parallel_vias_share_equally` — the per-barrel currents are the
  headline number on a real board (one via carrying 4.9 A of a 6 A return
  while its neighbour carried 1.1 A), so a symmetric case must split evenly.
* `test_a_sink_on_another_island_is_refused_by_name` — an unconnected sink is
  a finding about the board, never a singular matrix.
* `test_two_terminals_on_shared_copper_are_refused` — they are one node, and
  solving them would report a zero-ohm path that does not exist.
"""

import math
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import MultiPolygon, Point, box

from pcbreview import dcflow

RHO = dcflow.RHO_OHM_M
OZ1 = 0.035
OZ_HALF = 0.0152


def strip_conductor(length=20.0, width=2.0, t=OZ1):
    return dcflow.Conductor(net="T", layers=["F.Cu"],
                            copper={"F.Cu": box(0, 0, length, width)},
                            thickness_mm={"F.Cu": t}, z_mm={"F.Cu": t / 2})


def term(name, layer, geom):
    return dcflow.Terminal(name=name, copper={layer: geom}, pads=[name])


# -- one layer -------------------------------------------------------------

@pytest.mark.parametrize("cell, tol", [(0.1, 0.01), (0.05, 0.005)])
def test_a_strip_matches_rho_l_over_wt(cell, tol):
    c = strip_conductor()
    sol = dcflow.solve(c, term("a", "F.Cu", box(0, 0, 0.2, 2)),
                       [(term("b", "F.Cu", box(19.8, 0, 20, 2)), 1.0)], cell_mm=cell)
    exact = RHO * 19.6e-3 / (2e-3 * OZ1 * 1e-3)
    assert sol.resistance_ohm == pytest.approx(exact, rel=tol)


def test_the_error_falls_with_the_grid():
    c = strip_conductor()
    exact = RHO * 19.6e-3 / (2e-3 * OZ1 * 1e-3)
    errs = []
    for cell in (0.2, 0.1, 0.05):
        sol = dcflow.solve(c, term("a", "F.Cu", box(0, 0, 0.2, 2)),
                           [(term("b", "F.Cu", box(19.8, 0, 20, 2)), 1.0)],
                           cell_mm=cell)
        errs.append(abs(sol.resistance_ohm / exact - 1))
    assert errs[0] > errs[1] > errs[2]


@pytest.mark.parametrize("cell, tol", [(0.1, 0.025), (0.05, 0.012)])
def test_an_annulus_matches_the_spreading_formula(cell, tol):
    """A plane fed at a via is a spreading problem; this is its closed form."""
    disc = Point(0, 0).buffer(10.3, quad_segs=128)
    c = dcflow.Conductor(net="T", layers=["In1.Cu"], copper={"In1.Cu": disc},
                         thickness_mm={"In1.Cu": OZ_HALF}, z_mm={"In1.Cu": 0.5})
    inner = Point(0, 0).buffer(1.0, quad_segs=128)
    outer = disc.difference(Point(0, 0).buffer(10.0, quad_segs=128))
    sol = dcflow.solve(c, term("a", "In1.Cu", inner),
                       [(term("b", "In1.Cu", outer), 1.0)], cell_mm=cell)
    exact = RHO * math.log(10.0) / (2 * math.pi * OZ_HALF * 1e-3)
    assert sol.resistance_ohm == pytest.approx(exact, rel=tol)


def test_loss_equals_current_times_drop():
    c = strip_conductor()
    sol = dcflow.solve(c, term("a", "F.Cu", box(0, 0, 0.2, 2)),
                       [(term("b", "F.Cu", box(19.8, 0, 20, 2)), 3.0)])
    assert sol.loss_w == pytest.approx(3.0 * sol.worst_drop_v, rel=0.005)


def test_resistance_follows_copper_temperature():
    c = strip_conductor()
    args = (term("a", "F.Cu", box(0, 0, 0.2, 2)),
            [(term("b", "F.Cu", box(19.8, 0, 20, 2)), 1.0)])
    cold = dcflow.solve(c, *args)
    hot = dcflow.solve(c, *args, temperature_c=lambda l, x, y: 0 * x + 70.0)
    assert hot.resistance_ohm / cold.resistance_ohm == pytest.approx(
        1 + dcflow.TEMPCO_PER_C * 50.0, rel=1e-6)


def test_a_neck_is_the_hotspot():
    """Dissipation density goes as K^2, so the narrow stretch must rank first."""
    copper = box(0, 0, 30, 3).difference(box(14, 0.8, 16, 3))
    c = dcflow.Conductor(net="T", layers=["F.Cu"], copper={"F.Cu": copper},
                         thickness_mm={"F.Cu": OZ1}, z_mm={"F.Cu": OZ1 / 2})
    sol = dcflow.solve(c, term("a", "F.Cu", box(0, 0, 0.2, 3)),
                       [(term("b", "F.Cu", box(29.8, 0, 30, 3)), 2.0)])
    worst = sol.hotspots[0]
    assert 13.5 <= worst.x <= 16.5 and worst.y <= 1.2
    assert worst.sheet_a_per_mm == pytest.approx(2.0 / 0.8, rel=0.25)


# -- several layers --------------------------------------------------------

def test_two_layers_through_one_barrel_add_in_series():
    """Each via land covers its strip's full width, so the strips' ends are
    equipotential and the answer is strip + barrel + strip exactly."""
    copper = {"F.Cu": box(0, 0, 10, 2), "B.Cu": box(9, 0, 19, 2)}
    land = box(9, 0, 10, 2)
    barrel = dcflow.Barrel(label="v", x=9.5, y=1.0, drill_mm=0.3,
                           layers=["F.Cu", "B.Cu"],
                           land={"F.Cu": land, "B.Cu": land})
    c = dcflow.Conductor(net="T", layers=["F.Cu", "B.Cu"], copper=copper,
                         thickness_mm={"F.Cu": OZ1, "B.Cu": OZ1},
                         z_mm={"F.Cu": 0.0175, "B.Cu": 1.5825},
                         barrels=[barrel], plating_um=25.0)
    sol = dcflow.solve(c, term("a", "F.Cu", box(0, 0, 0.2, 2)),
                       [(term("b", "B.Cu", box(18.8, 0, 19, 2)), 1.0)],
                       cell_mm=0.05)
    strip = RHO * 8.8e-3 / (2e-3 * OZ1 * 1e-3)
    tube = RHO * 1.565e-3 / (math.pi * 0.3e-3 * 25e-6)
    assert sol.resistance_ohm == pytest.approx(2 * strip + tube, rel=0.02)
    assert sol.barrels[0].current_a == pytest.approx(1.0, rel=1e-6)


def test_parallel_vias_share_equally():
    plate = box(0, 0, 12, 12)
    vias = []
    for k, (x, y) in enumerate([(3, 3), (9, 3), (3, 9), (9, 9)]):
        land = Point(x, y).buffer(0.3, quad_segs=32)
        vias.append(dcflow.Barrel(label=f"v{k}", x=x, y=y, drill_mm=0.3,
                                  layers=["F.Cu", "B.Cu"],
                                  land={"F.Cu": land, "B.Cu": land}))
    c = dcflow.Conductor(net="T", layers=["F.Cu", "B.Cu"],
                         copper={"F.Cu": plate, "B.Cu": plate},
                         thickness_mm={"F.Cu": OZ1, "B.Cu": OZ1},
                         z_mm={"F.Cu": 0.0175, "B.Cu": 1.5825}, barrels=vias)
    centre = Point(6, 6).buffer(0.6, quad_segs=32)
    sol = dcflow.solve(c, term("a", "F.Cu", centre),
                       [(term("b", "B.Cu", centre), 4.0)])
    shares = sorted(b.current_a for b in sol.barrels)
    assert shares[0] == pytest.approx(1.0, rel=0.01)
    assert shares[-1] == pytest.approx(1.0, rel=0.01)


def test_two_sinks_draw_their_own_currents():
    c = strip_conductor(length=30.0)
    sol = dcflow.solve(c, term("mid", "F.Cu", box(14.9, 0, 15.1, 2)),
                       [(term("left", "F.Cu", box(0, 0, 0.2, 2)), 1.0),
                        (term("right", "F.Cu", box(29.8, 0, 30, 2)), 3.0)])
    one_side = RHO * 14.7e-3 / (2e-3 * OZ1 * 1e-3)
    assert sol.drop_v["left"] == pytest.approx(1.0 * one_side, rel=0.03)
    assert sol.drop_v["right"] == pytest.approx(3.0 * one_side, rel=0.03)
    assert sol.current_a == pytest.approx(4.0)


# -- refusals ----------------------------------------------------------------

def test_a_sink_on_another_island_is_refused_by_name():
    copper = MultiPolygon([box(0, 0, 10, 2), box(20, 0, 30, 2)])
    c = dcflow.Conductor(net="T", layers=["F.Cu"], copper={"F.Cu": copper},
                         thickness_mm={"F.Cu": OZ1}, z_mm={"F.Cu": OZ1 / 2})
    with pytest.raises(dcflow.FlowError, match="far.*not joined"):
        dcflow.solve(c, term("near", "F.Cu", box(0, 0, 0.2, 2)),
                     [(term("far", "F.Cu", box(29.8, 0, 30, 2)), 1.0)])


def test_a_floating_island_is_dropped_not_pinned():
    copper = MultiPolygon([box(0, 0, 10, 2), box(20, 0, 30, 2)])
    c = dcflow.Conductor(net="T", layers=["F.Cu"], copper={"F.Cu": copper},
                         thickness_mm={"F.Cu": OZ1}, z_mm={"F.Cu": OZ1 / 2})
    sol = dcflow.solve(c, term("a", "F.Cu", box(0, 0, 0.2, 2)),
                       [(term("b", "F.Cu", box(9.8, 0, 10, 2)), 1.0)])
    assert sol.islands_dropped == 1
    assert sol.resistance_ohm == pytest.approx(
        RHO * 9.6e-3 / (2e-3 * OZ1 * 1e-3), rel=0.02)


def test_two_terminals_on_shared_copper_are_refused():
    c = strip_conductor()
    with pytest.raises(dcflow.FlowError, match="one node"):
        dcflow.solve(c, term("a", "F.Cu", box(0, 0, 1, 2)),
                     [(term("b", "F.Cu", box(0.5, 0, 2, 2)), 1.0)])


# -- pad naming -------------------------------------------------------------

def _fake_board(pads):
    net = types.SimpleNamespace(code=7, name="N")
    return types.SimpleNamespace(pads=pads, net=lambda name: net)


def _pad(ref, number):
    return types.SimpleNamespace(ref=ref, number=str(number), net=7,
                                 designator=f"{ref}.{number}")


def test_pad_ranges_and_whole_parts_expand():
    board = _fake_board([_pad("U3", n) for n in range(15, 26)] + [_pad("J1", 2)])
    got = [p.designator for p in dcflow.expand_pads(board, "N", ["U3.17-19", "J1"])]
    assert got == ["U3.17", "U3.18", "U3.19", "J1.2"]


def test_a_pad_not_on_the_net_is_refused_with_what_is_there():
    board = _fake_board([_pad("U3", 17), _pad("U3", 18)])
    with pytest.raises(dcflow.FlowError, match="U3 has U3.17, U3.18"):
        dcflow.expand_pads(board, "N", ["U3.4"])


def test_the_adiabatic_bound_is_j_squared_rho_t_over_c():
    rise = dcflow.adiabatic_rise_c(5.0, OZ1, 0.01)
    j = 5.0 / OZ1 * 1e6
    assert rise == pytest.approx(j * j * RHO * 0.01 / dcflow.COPPER_HEAT_CAPACITY_J_M3K)
