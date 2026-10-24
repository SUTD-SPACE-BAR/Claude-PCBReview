"""Plane resistance and dissipation for a board's power rails.

    python3 prototypes/current_capacity/run_planes.py <board.kicad_pcb>

Takes the board on the command line rather than a hardcoded path, so it can
be pointed at whatever you are looking at.
"""
import pathlib
import sys, collections, time
sys.path.insert(0, '.')
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import shapely
from shapely.geometry import Point
from pcbreview.model import Board
from plane_solve import sheet_ohm_per_square

if len(sys.argv) < 2:
    sys.stderr.write("usage: run_planes.py <board.kicad_pcb>\n")
    raise SystemExit(2)
BOARD = sys.argv[1]
CELL = 0.25

b = Board.from_kicad(BOARD)


def nn(c):
    try: return b.net(c).name
    except KeyError: return None


pads = collections.defaultdict(dict)
for p in b.pads:
    pads[p.ref][str(p.number)] = p


def rasterise(poly, cell):
    minx, miny, maxx, maxy = poly.bounds
    nx = int(np.ceil((maxx - minx) / cell)); ny = int(np.ceil((maxy - miny) / cell))
    xs = minx + (np.arange(nx) + 0.5) * cell
    ys = miny + (np.arange(ny) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    mask = shapely.contains_xy(poly, X.ravel(), Y.ravel()).reshape(ny, nx)
    return mask, (minx, miny)


def build(mask, rs):
    ny, nx = mask.shape
    idx = -np.ones((ny, nx), dtype=np.int64)
    cells = np.argwhere(mask)
    idx[mask] = np.arange(len(cells))
    n = len(cells); g = 1.0 / rs
    rows, cols, vals = [], [], []
    for k, (j, i) in enumerate(cells):
        diag = 0.0
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < ny and 0 <= ii < nx and mask[jj, ii]:
                rows.append(k); cols.append(int(idx[jj, ii])); vals.append(-g); diag += g
        rows.append(k); cols.append(k); vals.append(diag)
    return sp.csr_matrix((vals, (rows, cols)), shape=(n, n)), cells, idx


def electrode(idx, mask, origin, cell, pts, r_mm):
    ny, nx = mask.shape
    out = set()
    rr = max(int(np.ceil(r_mm / cell)), 1)
    for (x, y) in pts:
        ci = int((x - origin[0]) / cell); cj = int((y - origin[1]) / cell)
        for dj in range(-rr, rr + 1):
            for di in range(-rr, rr + 1):
                j, i = cj + dj, ci + di
                if 0 <= j < ny and 0 <= i < nx and mask[j, i]:
                    out.add(int(idx[j, i]))
    return sorted(out)


def terminal_network(layer, net, terminals, oz, label):
    """terminals: list of (name, [(x,y)...], current_a). Positive = into the plane."""
    t0 = time.time()
    cu = b.copper_on_layer(layer, net)
    poly = max((g for g in (cu.geoms if cu.geom_type != 'Polygon' else [cu])),
               key=lambda g: g.area)
    rs = sheet_ohm_per_square(oz)
    mask, origin = rasterise(poly, CELL)
    A, cells, idx = build(mask, rs)
    n = A.shape[0]
    print(f"\n=== {label}  [{layer}, {oz} oz, Rsheet {rs*1e3:.3f} mOhm/sq, "
          f"{poly.area:.0f} mm2, {n} cells]")
    groups = []
    for name, pts, cur in terminals:
        e = electrode(idx, mask, origin, CELL, pts, 0.45)
        if not e:
            print(f"   !! {name}: electrode off copper"); return
        groups.append((name, e, cur))

    # Merge each electrode's cells into one node by Galerkin projection:
    # P maps reduced nodes -> cells, A' = P^T A P, and the merged node keeps
    # every face conductance of every cell in the group. (Rewriting the rows
    # in place instead silently shrinks the electrode to one cell, which is a
    # 300x error -- it was the first thing this got wrong.)
    owner = np.arange(n)
    for gi, (name, e, cur) in enumerate(groups):
        owner[e] = -(gi + 1)
    keep = owner >= 0
    reduced = -np.ones(n, dtype=np.int64)
    m = len(groups)
    reduced[keep] = m + np.arange(keep.sum())
    for gi in range(m):
        reduced[owner == -(gi + 1)] = gi
    P = sp.csr_matrix((np.ones(n), (np.arange(n), reduced)),
                      shape=(n, m + int(keep.sum())))
    Ar = (P.T @ A @ P).tolil()
    rhs = np.zeros(Ar.shape[0])
    for gi, (name, e, cur) in enumerate(groups):
        rhs[gi] = cur
    gref = m - 1                       # gauge: last terminal at 0 V
    Ar.rows[gref] = [gref]; Ar.data[gref] = [1.0]; rhs[gref] = 0.0
    V = spla.spsolve(Ar.tocsc(), rhs)

    total_p = 0.0
    base = V[gref]
    for gi, (name, e, cur) in enumerate(groups):
        v = V[gi] - base
        total_p += v * cur
        print(f"   {name:26s} I={cur:+7.2f} A   V={v*1e3:+8.2f} mV")
    print(f"   plane dissipation {total_p:.3f} W   ({time.time()-t0:.0f} s)")
    return total_p


def selftest():
    """Two-terminal rectangle against V = I * Rsheet * L / W."""
    from shapely.geometry import box
    L, W, oz, I = 40.0, 12.0, 0.5, 10.0
    rs = sheet_ohm_per_square(oz)
    mask, origin = rasterise(box(0, 0, L, W), CELL)
    A, cells, idx = build(mask, rs)
    a = electrode(idx, mask, origin, CELL, [(0.2, y) for y in np.arange(0.2, W, 0.2)], 0.2)
    z = electrode(idx, mask, origin, CELL, [(L - 0.2, y) for y in np.arange(0.2, W, 0.2)], 0.2)
    n = A.shape[0]
    owner = np.arange(n); owner[a] = -1; owner[z] = -2
    keep = owner >= 0
    reduced = -np.ones(n, dtype=np.int64); reduced[keep] = 2 + np.arange(keep.sum())
    reduced[owner == -1] = 0; reduced[owner == -2] = 1
    P = sp.csr_matrix((np.ones(n), (np.arange(n), reduced)), shape=(n, 2 + int(keep.sum())))
    Ar = (P.T @ A @ P).tolil()
    rhs = np.zeros(Ar.shape[0]); rhs[0] = I
    Ar.rows[1] = [1]; Ar.data[1] = [1.0]; rhs[1] = 0.0
    V = spla.spsolve(Ar.tocsc(), rhs)
    got = V[0]
    # electrodes occupy ~0.45 mm at each end, so the conducting span is L-0.9
    exact = I * rs * (L - 0.9) / W
    print(f"selftest: solved {got*1e3:.3f} mV, closed form {exact*1e3:.3f} mV, "
          f"{100*(got-exact)/exact:+.2f}%   (Rsheet {rs*1e3:.3f} mOhm/sq)")
    assert abs(got - exact) / exact < 0.05, (got, exact)


selftest()

# ---- +BATT on In2.Cu -------------------------------------------------------
batt_vias = [v for v in b.vias if nn(v.net) == "+BATT"]
q3_pour = max((p for p in (b.copper_on_layer("F.Cu", "+BATT").geoms)), key=lambda p: p.area)
src = [v.pos for v in batt_vias if q3_pour.contains(Point(*v.pos))]
print("Q3-side +BATT vias:", len(src))

efuses = ["U7", "U10", "U13", "U16", "U19", "U22", "U25", "U28"]
terms = [("Q3 tab (source)", src, 40.0)]
for ef in efuses:
    grp = pads[ef]["2"].copper.union(pads[ef]["3"].copper).union(
        pads[ef]["4"].copper).union(pads[ef]["5"].copper).buffer(1.2)
    vs = [v.pos for v in batt_vias if grp.contains(Point(*v.pos))]
    terms.append((f"{ef} VIN ({len(vs)} vias)", vs, -5.0))
terminal_network("In2.Cu", "+BATT", terms, 0.5, "+BATT plane, 8 x 5 A out of Q3")

# ---- GND on In1.Cu ---------------------------------------------------------
gnd_vias = [v for v in b.vias if nn(v.net) == "GND"]
j1 = pads["J1"]["2"]
gsrc = [v.pos for v in gnd_vias if j1.copper.buffer(3.0).contains(Point(*v.pos))]
print("\nGND vias within 3 mm of J1.- :", len(gsrc))
hb = ["U9", "U12", "U15", "U18", "U21", "U24", "U27", "U30"]
gterms = []
for h in hb:
    grp = pads[h]["3"].copper.buffer(4.0)
    vs = [v.pos for v in gnd_vias if grp.contains(Point(*v.pos))]
    gterms.append((f"{h} GND ({len(vs)} vias)", vs or [pads[h]["3"].pos], +5.0))
gterms.append(("J1 GND (return)", gsrc or [j1.pos], -40.0))
terminal_network("In1.Cu", "GND", gterms, 0.5, "GND plane, 8 x 5 A back to XT60")
