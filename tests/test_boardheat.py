"""Tests for the board heat model — `boardheat`.

Closed form only, built from plain arrays so no board is needed. What these
establish is that the *solver* is right; what they cannot establish is that a
real board's surroundings match the environment it was solved in, which is
why `boardheat` reports a bracket and never one temperature.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from scipy.special import k0

from pcbreview import boardheat

CU = boardheat.K_COPPER_W_MK


def sheets(n_layers, ny, nx, cell_mm, kt, vertical=None, h_top=10.0,
           h_bottom=10.0, capacity=1.0):
    inside = np.ones((ny, nx), bool)
    area = (cell_mm * 1e-3) ** 2
    layers = [f"L{k}" for k in range(n_layers)]
    kts = {l: np.full((ny, nx), kt[k] if isinstance(kt, list) else kt)
           for k, l in enumerate(layers)}
    surface = {l: np.zeros((ny, nx)) for l in layers}
    surface[layers[0]] += h_top * area
    surface[layers[-1]] += h_bottom * area
    vert = [np.full((ny, nx), vertical) for _ in range(n_layers - 1)]
    cap = {l: np.full((ny, nx), capacity) for l in layers}
    return boardheat.Sheets(layers=layers, cell_mm=cell_mm, origin=(0.0, 0.0),
                            inside=inside, kt=kts, vertical=vert, capacity=cap,
                            surface=surface)


def test_uniform_heating_balances_against_the_surface():
    s = sheets(1, 40, 40, 1.0, kt=CU * 35e-6, h_top=6.0, h_bottom=6.0)
    model = boardheat.HeatModel(s)
    q = model.zeros() + 0.5 / model.n_cells
    result = model.steady(q)
    area = (40e-3) ** 2
    assert np.nanmean(result.rise_c["L0"]) == pytest.approx(0.5 / (12.0 * area), rel=1e-9)


def test_two_sheets_heated_on_top_match_the_1d_network():
    """q = h T1 + G (T1 - T2) and G (T1 - T2) = h T2, per cell."""
    cell = 1.0
    area = (cell * 1e-3) ** 2
    g = area * 0.3 / 1.5e-3                       # 1.5 mm of laminate
    s = sheets(2, 20, 20, cell, kt=1e-4, vertical=g, h_top=10.0, h_bottom=10.0)
    model = boardheat.HeatModel(s)
    q = model.zeros()
    per_cell = 1e-3
    q[:model.n_cells] = per_cell
    r = model.steady(q)
    h = 10.0 * area
    t2 = per_cell * g / ((h + g) * h + g * h)
    t1 = t2 * (h + g) / g
    assert np.nanmean(r.rise_c["L0"]) == pytest.approx(t1, rel=1e-6)
    assert np.nanmean(r.rise_c["L1"]) == pytest.approx(t2, rel=1e-6)


def test_a_point_source_on_a_sheet_matches_k0():
    """K0 is the infinite-plate answer, so the plate must be several heat
    lengths ``l = sqrt(kt / 2h)`` wide or its insulated edges trap heat: at
    h = 10 the 200 mm plate is two lengths across and reads 6% hot at 20 mm.
    h = 50 makes it over eight."""
    cell = 0.5
    n = 401
    kt = CU * 4 * 35e-6
    s = sheets(1, n, n, cell, kt=kt, h_top=50.0, h_bottom=50.0)
    model = boardheat.HeatModel(s)
    q = model.zeros()
    centre = (n // 2) * n + n // 2
    q[centre] = 0.2
    rise = model.steady(q).rise_c["L0"]
    lam = math.sqrt(kt / 100.0) * 1e3             # mm
    for r_mm in (5.0, 10.0, 20.0):
        k = int(round(r_mm / cell))
        got = rise[n // 2, n // 2 + k]
        exact = 0.2 / (2 * math.pi * kt) * k0(r_mm / lam)
        assert got == pytest.approx(exact, rel=0.01), r_mm


def test_a_lumped_plate_reaches_one_minus_e_at_tau():
    cell = 1.0
    area = (cell * 1e-3) ** 2
    s = sheets(1, 10, 10, cell, kt=1.0, h_top=10.0, h_bottom=10.0, capacity=0.02)
    model = boardheat.HeatModel(s)
    q = model.zeros() + 1e-3
    steady = model.steady(q).rise_c["L0"][0, 0]
    tau = 0.02 / (20.0 * area)
    pulsed = model.pulse(model.zeros(), q, seconds=tau, steps=400).rise_c["L0"][0, 0]
    assert pulsed == pytest.approx(steady * (1 - math.exp(-1)), rel=0.005)


# -- IPC-2152: the one measured reference --------------------------------
#
# IPC-2152's baseline: one trace on a 1.78 mm board with no copper planes, in
# still air, its rise taken as the trace's average from its resistance over
# the middle 6 in of a 12 in run. Modelled as an infinitely long trace
# (insulated ends, uniform heating) on a board wide enough that its edges do
# not matter, with copper resistance following the trace's temperature as it
# does on the bench. The reference is Brooks's fits to IPC-2152's charts
# ("Trace Currents and Temperatures Revisited", Eq. 3-2 and Table 3-1).

MIL = 0.0254
IPC2152_BOARD_MM = 1.778


def _ipc2152_reference(current, width_mil, oz, internal):
    t_mil = oz * 0.0348 / MIL
    if not internal:
        return 215.3 * current ** 2 * width_mil ** -1.15 * t_mil ** -1.0
    if oz == 1:
        return 200 * current ** 1.9 * width_mil ** -1.10 * t_mil ** -1.52
    return 300 * current ** 2 * width_mil ** -1.15 * t_mil ** -1.52


def _current_for(rise, width_mil, oz, internal):
    lo, hi = 1e-3, 500.0
    for _ in range(80):
        mid = (lo * hi) ** 0.5
        lo, hi = ((mid, hi) if _ipc2152_reference(mid, width_mil, oz, internal)
                  < rise else (lo, mid))
    return (lo * hi) ** 0.5


def _ipc_trace_rise(current, width_mil, oz, internal=False,
                    h=None, cells_across=6, half_width_mm=50.0):
    """The model's average rise of a long trace on IPC-2152's test board."""
    h = boardheat.ENVIRONMENTS["still_air"] if h is None else h
    w_mm = width_mil * MIL
    t_m = oz * 0.0348e-3
    cell = w_mm / cells_across
    ny = int(round(2 * half_width_mm / cell))
    ny += (ny - cells_across) % 2
    nx, area = 4, (cell * 1e-3) ** 2
    n_layers = 3 if internal else 2
    gap = IPC2152_BOARD_MM * 1e-3 / (n_layers - 1)
    s = sheets(n_layers, ny, nx, cell, kt=0.0,
               vertical=area * boardheat.K_LAMINATE_Z_W_MK / gap,
               h_top=h, h_bottom=h)
    for k, name in enumerate(s.layers):
        share = gap / 2 if k in (0, n_layers - 1) else gap
        s.kt[name][:] = boardheat.K_LAMINATE_XY_W_MK * share
    trace = s.layers[1 if internal else 0]
    rows = slice((ny - cells_across) // 2, (ny + cells_across) // 2)
    s.kt[trace][rows, :] += CU * t_m
    model = boardheat.HeatModel(s)
    offset = s.layers.index(trace) * model.n_cells
    idx = np.arange(model.n_cells).reshape(ny, nx)[rows, :].ravel() + offset
    rise = 0.0
    for _ in range(40):
        rho = 1.72e-8 * (1 + 0.00393 * (25.0 + rise - 20.0))
        q = model.zeros()
        q[idx] = current ** 2 * rho / (w_mm * 1e-3 * t_m) * cell * 1e-3 / cells_across
        new = float(np.mean(model.steady(q).rise_c[trace][rows, :]))
        if abs(new - rise) < 1e-5:
            return new
        rise = new
    return rise


def _ratios(internal):
    weights = (1, 2) if internal else (1, 2, 3)
    out = []
    for oz in weights:
        for width in (10, 20, 50, 100, 200, 400):
            for rise in (10, 20, 45):
                current = _current_for(rise, width, oz, internal)
                out.append(_ipc_trace_rise(current, width, oz, internal) / rise)
    return np.array(out)


def test_still_air_reproduces_ipc_2152_on_outer_traces():
    """54 cases, 1-3 oz, 10-400 mil, +10 to +45 C. `still_air` is fitted to
    these, so on average it lands on IPC-2152; no case may read more than 20%
    cooler (the unsafe side) or 25% hotter. The first version of this model
    read about a third cooler on every one."""
    ratios = _ratios(internal=False)
    assert ratios.min() >= 0.80
    assert ratios.max() <= 1.25
    assert np.exp(np.log(ratios).mean()) == pytest.approx(1.0, abs=0.05)


def test_still_air_errs_hot_on_inner_traces():
    """36 cases. Fitted on outer traces, the model reads inner ones hotter
    than IPC-2152 on average — the safe side — and never more than 20% cooler.
    IPC-2152's own inner-trace data disagree between copper weights by more
    than this (Brooks, Table 3-1), so the band is wider."""
    ratios = _ratios(internal=True)
    assert ratios.min() >= 0.80
    assert ratios.max() <= 1.60
    assert np.exp(np.log(ratios).mean()) >= 1.0


def test_ipc_2221s_working_point_now_lands_on_ipc_2152():
    """IPC-2221: 1 mm of 1 oz outer copper, 2.39 A, +10 C. IPC-2152 measures
    +13.1 C for that trace. This model used to say +8.7 C and a test pinned it
    at or below IPC-2221 — which enforced the optimism this recalibration
    removes."""
    width_mil = 1.0 / MIL
    ipc2152 = _ipc2152_reference(2.39, width_mil, 1, False)
    assert ipc2152 == pytest.approx(13.1, abs=0.1)
    model = _ipc_trace_rise(2.39, width_mil, 1)
    assert model >= 10.0
    assert model == pytest.approx(ipc2152, rel=0.20)


def test_the_comparison_does_not_depend_on_the_grid_or_the_board_width():
    current = _current_for(10, 100, 2, False)
    base = _ipc_trace_rise(current, 100, 2)
    assert _ipc_trace_rise(current, 100, 2, cells_across=12) == pytest.approx(base, rel=0.01)
    assert _ipc_trace_rise(current, 100, 2, half_width_mm=100.0) == pytest.approx(base, rel=0.01)


def test_enclosed_stays_hotter_than_ipc_2152_on_every_case():
    """The worst-case end of the bracket has to be worse than IPC-2152's still
    air everywhere, or it is not a worst case."""
    for oz, width, rise in ((1, 10, 10), (2, 100, 45), (3, 400, 10)):
        current = _current_for(rise, width, oz, False)
        assert _ipc_trace_rise(current, width, oz,
                               h=boardheat.ENVIRONMENTS["enclosed"]) > rise


def test_a_board_with_no_way_to_lose_heat_is_refused():
    s = sheets(1, 5, 5, 1.0, kt=1.0, h_top=0.0, h_bottom=0.0)
    with pytest.raises(boardheat.HeatError, match="no path to ambient"):
        boardheat.HeatModel(s)


def test_superposition_matches_a_combined_solve():
    s = sheets(2, 30, 30, 0.5, kt=[CU * 35e-6, CU * 35e-6],
               vertical=(0.5e-3) ** 2 * 0.3 / 1.5e-3)
    model = boardheat.HeatModel(s)
    a = model.zeros(); a[10] = 0.1
    b = model.zeros(); b[model.n_cells + 500] = 0.2
    ta, tb = model.steady_many([a, b])
    both = model.steady(a + b).raw
    assert np.allclose(ta + tb, both)
