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


def test_ipc_2221s_own_working_point_lands_below_it():
    """IPC-2221: a 1 mm, 1 oz external trace carries 2.39 A at a 10 C rise.
    On a bare 76 mm laminate card in still air this model gives 7-9 C — below
    IPC-2221, which is the conservative chart, and in the right place."""
    cell = 0.25
    n = int(76.2 / cell)
    gap = 1.53e-3
    area = (cell * 1e-3) ** 2
    s = sheets(2, n, n, cell, kt=0.0, vertical=area * 0.3 / gap,
               h_top=12.0, h_bottom=12.0)
    lam = boardheat.K_LAMINATE_XY_W_MK * gap / 2
    s.kt["L0"][:] = lam
    s.kt["L1"][:] = lam
    rows = slice(n // 2 - 2, n // 2 + 2)          # 4 cells = 1 mm wide
    s.kt["L0"][rows, :] += CU * 35e-6
    model = boardheat.HeatModel(s)
    q = model.zeros()
    r_per_m = 1.72e-8 / (1e-3 * 35e-6)
    per_cell = 2.39 ** 2 * r_per_m * cell * 1e-3 / 4
    idx = np.arange(model.n_cells).reshape(n, n)
    q[idx[rows, :].ravel()] = per_cell
    peak = np.nanmax(model.steady(q).rise_c["L0"])
    assert 6.0 <= peak <= 10.0


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
