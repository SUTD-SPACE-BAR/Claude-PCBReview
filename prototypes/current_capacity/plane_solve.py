"""DC spreading resistance of a poured plane, by solving Laplace on it.

The erosion bottleneck answers "how narrow does the copper get", which is the
right question for a *track* and the wrong one for a *plane*: current flows
around a via antipad rather than through it, so a field of holes reads as a
constriction and is not one. The truth is the resistance, so solve for it.

    div(sigma grad V) = 0   on the copper, with the vias as electrodes

Rasterise the polygon, build the 5-point Laplacian over copper cells only,
pin the source vias to 1 V and the sink vias to 0 V, and read the current out
of the boundary reaction. R = 1 / I.

Two independent checks in `selftest`, both against closed form:
  a plain rectangle L x W   ->  R = Rsheet * L / W
  a 2:1 series of rectangles ->  the sum of the two
"""
import sys
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from shapely.geometry import Point, box
from shapely.prepared import prep

RHO_CU = 1.72e-8          # ohm m at 20 C
COPPER_MM_PER_OZ = 0.0348


def rasterise(poly, cell_mm, bounds=None):
    minx, miny, maxx, maxy = bounds or poly.bounds
    nx = int(np.ceil((maxx - minx) / cell_mm))
    ny = int(np.ceil((maxy - miny) / cell_mm))
    pr = prep(poly)
    xs = minx + (np.arange(nx) + 0.5) * cell_mm
    ys = miny + (np.arange(ny) + 0.5) * cell_mm
    mask = np.zeros((ny, nx), dtype=bool)
    for j, y in enumerate(ys):
        row = mask[j]
        for i, x in enumerate(xs):
            if pr.contains(Point(x, y)):
                row[i] = True
    return mask, (minx, miny), cell_mm


def solve_resistance(mask, origin, cell_mm, sheet_ohm_sq, sources, sinks,
                     electrode_r_mm=0.4):
    """sources/sinks are lists of (x, y) in board mm. Returns (R_ohm, V field)."""
    ny, nx = mask.shape
    idx = -np.ones((ny, nx), dtype=np.int64)
    cells = np.argwhere(mask)
    idx[mask] = np.arange(len(cells))
    n = len(cells)
    if n == 0:
        raise ValueError("no copper cells")

    def stamp(pts):
        out = set()
        rr = max(int(np.ceil(electrode_r_mm / cell_mm)), 1)
        for (x, y) in pts:
            ci = int((x - origin[0]) / cell_mm)
            cj = int((y - origin[1]) / cell_mm)
            for dj in range(-rr, rr + 1):
                for di in range(-rr, rr + 1):
                    j, i = cj + dj, ci + di
                    if 0 <= j < ny and 0 <= i < nx and mask[j, i]:
                        out.add(int(idx[j, i]))
        return out

    src, snk = stamp(sources), stamp(sinks)
    src -= snk
    if not src or not snk:
        raise ValueError(f"electrode landed off copper (src {len(src)}, snk {len(snk)})")

    # 5-point Laplacian; conductance of each face is sigma*t = 1/Rsheet
    # (square cells -> the cell size cancels)
    g = 1.0 / sheet_ohm_sq
    rows, cols, vals = [], [], []
    fixed = np.zeros(n, dtype=bool)
    rhs = np.zeros(n)
    fixed[list(src)] = True
    fixed[list(snk)] = True
    volt = np.zeros(n)
    volt[list(src)] = 1.0

    for k, (j, i) in enumerate(cells):
        if fixed[k]:
            rows.append(k); cols.append(k); vals.append(1.0)
            rhs[k] = volt[k]
            continue
        diag = 0.0
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < ny and 0 <= ii < nx and mask[jj, ii]:
                m = int(idx[jj, ii])
                rows.append(k); cols.append(m); vals.append(-g)
                diag += g
        rows.append(k); cols.append(k); vals.append(diag if diag else 1.0)

    A = sp.csr_matrix((vals, (rows, cols)), shape=(n, n))
    V = spla.spsolve(A.tocsc(), rhs)

    # current out of the source electrode = sum of face currents leaving it
    I = 0.0
    for k in src:
        j, i = cells[k]
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < ny and 0 <= ii < nx and mask[jj, ii]:
                m = int(idx[jj, ii])
                if m not in src:
                    I += g * (V[k] - V[m])
    return 1.0 / I, V, cells, idx


def sheet_ohm_per_square(oz, temp_c=20.0):
    t = oz * COPPER_MM_PER_OZ * 1e-3
    return RHO_CU * (1 + 0.00393 * (temp_c - 20.0)) / t


def selftest():
    rs = sheet_ohm_per_square(1.0)
    L, W, cell = 40.0, 10.0, 0.25
    rect = box(0, 0, L, W)
    mask, origin, c = rasterise(rect, cell)
    # full-height electrodes at each end
    src = [(0.4, y) for y in np.arange(0.3, W, cell / 2)]
    snk = [(L - 0.4, y) for y in np.arange(0.3, W, cell / 2)]
    R, *_ = solve_resistance(mask, origin, c, rs, src, snk, electrode_r_mm=0.25)
    exact = rs * (L - 0.8) / W
    print(f"selftest rectangle: solved {R*1e3:.4f} mOhm, closed form {exact*1e3:.4f} mOhm, "
          f"{100*(R-exact)/exact:+.2f}%")
    assert abs(R - exact) / exact < 0.03


if __name__ == "__main__":
    selftest()
