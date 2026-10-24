"""How hot the board gets: copper I^2R and part dissipation, steady and pulsed.

`heatsink.py` declined to turn copper area into a temperature, for a reason
that has not gone away: **the answer is dominated by how the board loses heat
to its surroundings, and nothing in a board file says that.** The same 0.6 W
on the same copper is a few degrees in a draught and tens of degrees in a
sealed bay. A model that quietly picks one convection coefficient produces a
confident number whose error is the size of the number.

So this module computes temperatures **only as a bracket over named
environments** (`ENVIRONMENTS`), and every consumer reports the bracket. The
user's declared environment, if there is one, picks the verdict; without one
the verdict is taken at the *worst* case, under the standing ruling that a
false positive is preferred to a silent pass.

What it is, and what it deliberately is not
-------------------------------------------
A 2.5D steady/transient conduction model on a square grid over the board
outline. **One sheet per copper layer.** Each sheet conducts sideways through
the copper actually present in each cell (all nets — the planes are what
spread heat) plus half of the laminate above and below it. Neighbouring sheets
are joined through the laminate between them, and through every plated barrel
(vias of every net, and through-hole pins) at the barrel's cell. The two outer
sheets lose heat to ambient with a linear coefficient — natural convection
and radiation lumped together — and optional mounting points add a stated
conductance to a structure at ambient.

Heat goes in where it is made:

* **copper loss** from `dcflow`, on the layer and in the cell it was solved
  in, barrels split between their two layers;
* **part dissipation** from the thermal manifest — the same `dissipation_w`
  `check_thermal` reads, never a new number — spread over the part's own pads
  on its own side.

It is **not** a component model. Packages are not solids here: a part's heat
enters the board at its pads, which is how a bottom-cooled package actually
behaves and is optimistic for a leaded part that also sheds heat from its
body. Junction temperatures stay `check_thermal`'s job; what this adds there is
the one thing `theta_JA` cannot know — how much **everything else** on the
board raises the local ambient a part sits in (`neighbour_rise`).

Validation
----------
`tests/test_boardheat.py`, against closed form:

* energy balance: any uniform load's mean surface rise is ``P / (h A)`` —
  exact;
* two sheets, top heated: the 1-D through-thickness network, solved by hand —
  exact;
* a point source on a uniform sheet against ``P K0(r/l) / (2 pi k t)`` —
  within 0.1% at 5, 10 and 20 mm;
* the lumped transient ``dT (1 - exp(-t/tau))`` at ``t = tau`` — 0.07%;
* IPC-2221's own working point: a 1 mm, 1 oz trace at 2.39 A on a bare
  76 mm laminate card in still air. IPC-2221 says +10 C and is known to be
  conservative; this model says +8.7 C, and the test pins the band 6-10 C.

What those cannot establish, and nothing here can: that a real board's
surroundings match any one environment. Hence the bracket.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import shapely

#: Linear surface coefficients per face, W/m^2K, convection and radiation
#: together. Textbook ranges, and declared as judgement (§9): natural
#: convection from a small board is ~4-8, grey-body radiation near room
#: temperature ~5-6 at emissivity 0.9, so ~12 in open still air. A closed bay
#: stops most convection and its walls warm up, which is why `enclosed` is
#: well below that. `forced_air` is ~1-2 m/s across the board.
ENVIRONMENTS = {
    "enclosed": 5.0,
    "still_air": 12.0,
    "forced_air": 30.0,
}

#: The environment a verdict is taken at when nobody has said. The worst one:
#: a false positive is preferred to a silent pass.
DEFAULT_VERDICT_ENVIRONMENT = "enclosed"

K_COPPER_W_MK = 385.0
#: FR-4-class laminate. In-plane is roughly twice through-plane because of the
#: glass weave; datasheets put z at 0.25-0.40 and xy at 0.6-1.0.
K_LAMINATE_XY_W_MK = 0.8
K_LAMINATE_Z_W_MK = 0.3
#: Volumetric heat capacities, rho * c_p.
C_COPPER_J_M3K = 8960 * 385
C_LAMINATE_J_M3K = 1850 * 1100

#: Grid pitch. Heat spreads over millimetres, so this can be much coarser
#: than `dcflow`'s; copper is resolved below the pitch by supersampling each
#: cell (`SUPERSAMPLE`^2 points), so a 0.2 mm track still conducts heat.
CELL_MM = 0.25
SUPERSAMPLE = 4


class HeatError(Exception):
    """Raised when a heat model cannot be built or solved."""


@dataclass
class Sheets:
    """The model's physical inputs as plain arrays — what the tests build."""
    layers: list[str]
    cell_mm: float
    origin: tuple[float, float]
    inside: np.ndarray                    # (ny, nx) bool
    #: lateral conductance per cell, k*t summed over copper and laminate, W/K
    kt: dict[str, np.ndarray]
    #: conductance between layer i and i+1 at each cell, W/K
    vertical: list[np.ndarray]
    #: heat capacity per cell, J/K
    capacity: dict[str, np.ndarray]
    #: conductance to ambient per cell (outer layers and mounts), W/K
    surface: dict[str, np.ndarray]
    #: the two parts `surface` is made of, kept so another environment can be
    #: applied without re-sampling the copper: exposed face area per cell, m^2,
    #: and mount conductance per cell, W/K
    face_area_m2: dict[str, np.ndarray] | None = None
    mount_w_k: dict[str, np.ndarray] | None = None


def with_environment(sheets: Sheets, top_w_m2k: float,
                     bottom_w_m2k: float | None = None) -> Sheets:
    """The same board in different surroundings. Only `surface` changes."""
    if sheets.face_area_m2 is None:
        raise HeatError("these sheets were built without face areas, so a new "
                        "environment cannot be applied to them")
    bottom = top_w_m2k if bottom_w_m2k is None else bottom_w_m2k
    first, last = sheets.layers[0], sheets.layers[-1]
    surface = {}
    for layer in sheets.layers:
        coeff = (top_w_m2k if layer == first else 0.0) + (bottom if layer == last else 0.0)
        surface[layer] = (sheets.face_area_m2[layer] * coeff
                          + (sheets.mount_w_k[layer] if sheets.mount_w_k else 0.0))
    return Sheets(layers=sheets.layers, cell_mm=sheets.cell_mm, origin=sheets.origin,
                  inside=sheets.inside, kt=sheets.kt, vertical=sheets.vertical,
                  capacity=sheets.capacity, surface=surface,
                  face_area_m2=sheets.face_area_m2, mount_w_k=sheets.mount_w_k)


@dataclass
class HeatResult:
    """Temperature rise above ambient, per layer, plus where the worst is."""
    layers: list[str]
    cell_mm: float
    origin: tuple[float, float]
    rise_c: dict[str, np.ndarray]          # NaN outside the board
    environment: str = ""
    #: the solved vector, in the model's own node order
    raw: np.ndarray | None = field(default=None, repr=False)

    def max_rise(self) -> tuple[float, str, float, float]:
        best = (-math.inf, "", 0.0, 0.0)
        for layer, grid in self.rise_c.items():
            if np.all(np.isnan(grid)):
                continue
            k = int(np.nanargmax(grid))
            j, i = np.unravel_index(k, grid.shape)
            value = float(grid[j, i])
            if value > best[0]:
                best = (value, layer, *self._xy(i, j))
        return best

    def max_in(self, layer: str, geometry) -> float | None:
        """Hottest cell whose centre lies in `geometry` on `layer`."""
        grid = self.rise_c.get(layer)
        if grid is None or geometry is None or geometry.is_empty:
            return None
        gx, gy = self._grid()
        inside = shapely.contains_xy(geometry, gx, gy)
        if not inside.any():
            c = geometry.centroid
            return self.at(layer, c.x, c.y)
        values = grid[inside]
        values = values[~np.isnan(values)]
        return float(values.max()) if values.size else None

    def at(self, layer: str, x: float, y: float) -> float | None:
        grid = self.rise_c.get(layer)
        if grid is None:
            return None
        i = int((x - self.origin[0]) / self.cell_mm)
        j = int((y - self.origin[1]) / self.cell_mm)
        if not (0 <= j < grid.shape[0] and 0 <= i < grid.shape[1]):
            return None
        value = grid[j, i]
        return None if np.isnan(value) else float(value)

    def sampler(self):
        """``f(layer, xs, ys)`` → temperature *rise* at those points.

        `dcflow.solve(temperature_c=...)` wants absolute copper temperature,
        so the caller adds its ambient: ``lambda l, x, y: amb + f(l, x, y)``.
        """
        def lookup(layer, xs, ys):
            grid = self.rise_c.get(layer)
            if grid is None:
                return np.zeros_like(np.asarray(xs, dtype=float))
            i = np.clip(((np.asarray(xs) - self.origin[0]) / self.cell_mm).astype(int),
                        0, grid.shape[1] - 1)
            j = np.clip(((np.asarray(ys) - self.origin[1]) / self.cell_mm).astype(int),
                        0, grid.shape[0] - 1)
            return np.nan_to_num(grid[j, i])
        return lookup

    def _grid(self):
        ny, nx = next(iter(self.rise_c.values())).shape
        xs = self.origin[0] + (np.arange(nx) + 0.5) * self.cell_mm
        ys = self.origin[1] + (np.arange(ny) + 0.5) * self.cell_mm
        return np.meshgrid(xs, ys)

    def _xy(self, i, j):
        return (self.origin[0] + (i + 0.5) * self.cell_mm,
                self.origin[1] + (j + 0.5) * self.cell_mm)


class HeatModel:
    """The assembled conductance and capacity, factorised on demand."""

    def __init__(self, sheets: Sheets):
        self.sheets = sheets
        inside = sheets.inside
        self.n_cells = int(inside.sum())
        self.layers = list(sheets.layers)
        n = self.n_cells * len(self.layers)
        index = -np.ones(inside.shape, dtype=np.int64)
        index[inside] = np.arange(self.n_cells)
        self.index = index
        rows, cols, vals = [], [], []

        def couple(a, b, g):
            rows.extend([a, b, a, b]); cols.extend([a, b, b, a])
            vals.extend([g, g, -g, -g])

        for k, layer in enumerate(self.layers):
            base = k * self.n_cells
            kt = sheets.kt[layer]
            for a, b, ka, kb in ((index[:, :-1], index[:, 1:], kt[:, :-1], kt[:, 1:]),
                                 (index[:-1, :], index[1:, :], kt[:-1, :], kt[1:, :])):
                ok = (a >= 0) & (b >= 0) & (ka > 0) & (kb > 0)
                g = 2.0 * ka[ok] * kb[ok] / (ka[ok] + kb[ok])
                couple(base + a[ok], base + b[ok], g)
        for k in range(len(self.layers) - 1):
            g = sheets.vertical[k][inside]
            cells = np.arange(self.n_cells)
            ok = g > 0
            couple(k * self.n_cells + cells[ok], (k + 1) * self.n_cells + cells[ok], g[ok])
        core = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows),
                                                     np.concatenate(cols))),
                             shape=(n, n))
        surface = np.concatenate([sheets.surface[l][inside] for l in self.layers])
        if not np.any(surface > 0):
            raise HeatError("no path to ambient: every surface conductance is "
                            "zero, so any heat raises the board without limit")
        self.matrix = (core + sp.diags(surface)).tocsc()
        self.capacity = np.concatenate([sheets.capacity[l][inside]
                                        for l in self.layers])
        self._steady = None
        self._stepper: dict[float, object] = {}

    # -- sources ---------------------------------------------------------

    def zeros(self) -> np.ndarray:
        return np.zeros(self.n_cells * len(self.layers))

    def deposit(self, q: np.ndarray, layer: str, xs, ys, watts) -> None:
        """Add `watts` at points (xs, ys) on `layer`. Points off the board are
        moved to the nearest board cell rather than lost, so energy balances."""
        if layer not in self.layers:
            return
        xs = np.atleast_1d(np.asarray(xs, dtype=float))
        ys = np.atleast_1d(np.asarray(ys, dtype=float))
        watts = np.broadcast_to(np.asarray(watts, dtype=float), xs.shape)
        s = self.sheets
        ny, nx = s.inside.shape
        i = np.clip(((xs - s.origin[0]) / s.cell_mm).astype(int), 0, nx - 1)
        j = np.clip(((ys - s.origin[1]) / s.cell_mm).astype(int), 0, ny - 1)
        cell = self.index[j, i]
        lost = cell < 0
        if lost.any():
            jj, ii = np.nonzero(s.inside)
            for k in np.flatnonzero(lost):
                d = (ii - i[k]) ** 2 + (jj - j[k]) ** 2
                m = int(np.argmin(d))
                cell[k] = self.index[jj[m], ii[m]]
        np.add.at(q, self.layers.index(layer) * self.n_cells + cell, watts)

    def cells_in(self, layer: str, geometry) -> np.ndarray:
        """Indices, in this model's vector, of the cells whose centres lie in
        `geometry` on `layer` — or the one cell under its centroid when it is
        smaller than a cell. Heat put into a single cell is a point source,
        and a point source's spreading resistance grows without bound as the
        cell shrinks, so anything with an area is spread over that area."""
        if layer not in self.layers or geometry is None or geometry.is_empty:
            return np.empty(0, dtype=np.int64)
        s = self.sheets
        ny, nx = s.inside.shape
        x0, y0, x1, y1 = geometry.bounds
        i0 = max(int((x0 - s.origin[0]) / s.cell_mm), 0)
        i1 = min(int((x1 - s.origin[0]) / s.cell_mm) + 1, nx)
        j0 = max(int((y0 - s.origin[1]) / s.cell_mm), 0)
        j1 = min(int((y1 - s.origin[1]) / s.cell_mm) + 1, ny)
        base = self.layers.index(layer) * self.n_cells
        if i0 < i1 and j0 < j1:
            xs = s.origin[0] + (np.arange(i0, i1) + 0.5) * s.cell_mm
            ys = s.origin[1] + (np.arange(j0, j1) + 0.5) * s.cell_mm
            gx, gy = np.meshgrid(xs, ys)
            hit = shapely.contains_xy(geometry, gx, gy)
            cells = self.index[j0:j1, i0:i1][hit]
            cells = cells[cells >= 0]
            if cells.size:
                return base + cells
        c = geometry.centroid
        i = min(max(int((c.x - s.origin[0]) / s.cell_mm), 0), nx - 1)
        j = min(max(int((c.y - s.origin[1]) / s.cell_mm), 0), ny - 1)
        cell = self.index[j, i]
        return np.array([base + cell] if cell >= 0 else [], dtype=np.int64)

    # -- solves ----------------------------------------------------------

    def _factor(self, matrix):
        """Sparse LU, ordered for what this matrix is.

        Two choices, measured on a six-layer board (337k unknowns): SuperLU's
        default column ordering took 50 s to factorise; a symmetric
        minimum-degree ordering took 14 s, and interleaving the layers so a
        cell's six sheets are adjacent took it to 11.6 s — identical answers to
        1e-14. The matrix is symmetric, so telling SuperLU so is free.
        """
        if not hasattr(self, "_perm"):
            layers, n = len(self.layers), self.n_cells
            self._perm = np.arange(layers * n).reshape(layers, n).T.ravel()
        perm = self._perm
        lu = spla.splu(matrix[perm][:, perm].tocsc(), permc_spec="MMD_AT_PLUS_A",
                       options=dict(SymmetricMode=True))

        def solve(q):
            out = np.empty_like(q)
            out[perm] = lu.solve(q[perm])
            return out
        return solve

    def steady(self, q: np.ndarray, environment: str = "") -> HeatResult:
        if self._steady is None:
            self._steady = self._factor(self.matrix)
        return self._result(self._steady(q), environment)

    def steady_many(self, qs: list[np.ndarray]) -> list[np.ndarray]:
        """Several loads against one factorisation — superposition is exact
        because the model is linear."""
        if self._steady is None:
            self._steady = self._factor(self.matrix)
        return [self._steady(q) for q in qs]

    def pulse_rise(self, extra: np.ndarray, seconds: float,
                   steps: int = 30) -> np.ndarray:
        """The field `extra` adds by the end of a `seconds` pulse, starting
        from nothing. Implicit Euler, unconditionally stable.

        Because the model is linear, the end-of-pulse field on top of any
        steady background is that background's steady field **plus this** —
        so one transient per pulse serves every background it is run on.
        For a rectangular pulse the end is the peak.
        """
        if seconds <= 0:
            raise HeatError("a pulse needs a positive duration")
        dt = seconds / steps
        if dt not in self._stepper:
            lhs = (sp.diags(self.capacity / dt) + self.matrix).tocsc()
            self._stepper[dt] = self._factor(lhs)
        step = self._stepper[dt]
        t = self.zeros()
        for _ in range(steps):
            t = step(self.capacity / dt * t + extra)
        return t

    def pulse(self, background: np.ndarray, extra: np.ndarray, seconds: float,
              steps: int = 30, environment: str = "") -> HeatResult:
        """Start at the steady state of `background`; add `extra` for
        `seconds`; return the field at the end of the pulse."""
        base = self.steady(background).raw if np.any(background) else self.zeros()
        return self._result(base + self.pulse_rise(extra, seconds, steps), environment)

    def _result(self, t: np.ndarray, environment: str) -> HeatResult:
        out = {}
        for k, layer in enumerate(self.layers):
            grid = np.full(self.sheets.inside.shape, np.nan)
            grid[self.sheets.inside] = t[k * self.n_cells:(k + 1) * self.n_cells]
            out[layer] = grid
        return HeatResult(layers=self.layers, cell_mm=self.sheets.cell_mm,
                          origin=self.sheets.origin, rise_c=out,
                          environment=environment, raw=t)


# ---------------------------------------------------------------------------
# From a board
# ---------------------------------------------------------------------------

def _layer_gaps(board) -> tuple[dict[str, float], dict[str, float], list[float]]:
    """Copper thickness, mid-plane depth, and laminate between neighbours."""
    from pcbreview.dcflow import stack_geometry
    thickness, depth = stack_geometry(board)
    layers = list(board.copper_layers)
    gaps = []
    for upper, lower in zip(layers[:-1], layers[1:]):
        gap = ((depth[lower] - thickness[lower] / 2)
               - (depth[upper] + thickness[upper] / 2))
        gaps.append(max(gap, 1e-3))
    return thickness, depth, gaps


def sheets_for(board, surface_w_m2k: float, cell_mm: float = CELL_MM,
               bottom_w_m2k: float | None = None,
               mounts: dict[str, float] | None = None,
               plating_um: float | None = None) -> Sheets:
    """Every layer's conducting copper and laminate, from the board itself.

    ``mounts`` maps a footprint reference to a conductance, W/K, from its
    position to a structure at ambient — a standoff into an airframe, a
    screw into a chassis. Nothing is assumed for them by default; an
    unmounted board is the conservative case.
    """
    from pcbreview import current as current_module
    plating_um = current_module.VIA_PLATING_UM if plating_um is None else plating_um
    layers = list(board.copper_layers)
    thickness, depth, gaps = _layer_gaps(board)
    outline = board.outline
    if outline.is_empty:
        raise HeatError("the board has no outline, so there is nothing to "
                        "lose heat from")
    x0, y0, x1, y1 = outline.bounds
    nx = int(math.ceil((x1 - x0) / cell_mm)); ny = int(math.ceil((y1 - y0) / cell_mm))
    xs = x0 + (np.arange(nx) + 0.5) * cell_mm
    ys = y0 + (np.arange(ny) + 0.5) * cell_mm
    gx, gy = np.meshgrid(xs, ys)
    shapely.prepare(outline)
    inside = shapely.contains_xy(outline, gx, gy)
    area_m2 = (cell_mm * 1e-3) ** 2

    # copper fraction per cell, supersampled so thin copper still counts
    offs = (np.arange(SUPERSAMPLE) + 0.5) / SUPERSAMPLE - 0.5
    fraction = {}
    for layer in layers:
        cu = board.copper_on_layer(layer)
        shapely.prepare(cu)
        acc = np.zeros(inside.shape)
        for dx in offs:
            for dy in offs:
                acc += shapely.contains_xy(cu, gx + dx * cell_mm, gy + dy * cell_mm)
        fraction[layer] = acc / (SUPERSAMPLE * SUPERSAMPLE)

    kt, capacity = {}, {}
    for k, layer in enumerate(layers):
        above = gaps[k - 1] / 2 if k > 0 else 0.0
        below = gaps[k] / 2 if k < len(gaps) else 0.0
        lam_mm = above + below
        t_mm = thickness[layer]
        kt[layer] = np.where(inside, K_COPPER_W_MK * t_mm * 1e-3 * fraction[layer]
                             + K_LAMINATE_XY_W_MK * lam_mm * 1e-3, 0.0)
        capacity[layer] = np.where(inside, area_m2 * (
            C_COPPER_J_M3K * t_mm * 1e-3 * fraction[layer]
            + C_LAMINATE_J_M3K * lam_mm * 1e-3), 0.0)
    # one face's area on each outer layer; `with_environment` applies the top
    # coefficient to the first and the bottom one to the last, so a one-layer
    # board correctly gets both
    face = {l: np.zeros(inside.shape) for l in layers}
    face[layers[0]] = np.where(inside, area_m2, 0.0)
    face[layers[-1]] = np.where(inside, area_m2, 0.0)
    mount_g = {l: np.zeros(inside.shape) for l in layers}

    vertical = []
    for k, gap in enumerate(gaps):
        vertical.append(np.where(inside, area_m2 * K_LAMINATE_Z_W_MK / (gap * 1e-3), 0.0))

    def add_barrel(x, y, drill_mm, spans):
        i = int((x - x0) / cell_mm); j = int((y - y0) / cell_mm)
        if not (0 <= j < ny and 0 <= i < nx) or not inside[j, i]:
            return
        for upper, lower in zip(spans[:-1], spans[1:]):
            ku, kl = layers.index(upper), layers.index(lower)
            length = (depth[lower] - depth[upper]) * 1e-3
            g = K_COPPER_W_MK * math.pi * drill_mm * 1e-3 * plating_um * 1e-6 / length
            # a barrel spanning several gaps conducts across each in turn
            for k in range(ku, kl):
                share = (depth[layers[k + 1]] - depth[layers[k]]) * 1e-3
                vertical[k][j, i] += g * length / share

    for via in board.vias:
        spans = [l for l in layers if l in via.layers]
        if len(spans) >= 2:
            add_barrel(via.pos[0], via.pos[1], via.drill_mm, [spans[0], spans[-1]])
    for pad in board.pads:
        spans = [l for l in layers if l in pad.copper_layers]
        drill = max(pad.drill_mm) if pad.drill_mm else 0.0
        if pad.is_plated_hole and len(spans) >= 2 and drill > 0:
            add_barrel(pad.pos[0], pad.pos[1], drill, [spans[0], spans[-1]])

    for ref, conductance in (mounts or {}).items():
        fps = [f for f in board.footprints if f.ref == ref]
        if not fps:
            raise HeatError(f"mount {ref!r} is not a footprint on this board")
        x, y = fps[0].pos
        i = min(max(int((x - x0) / cell_mm), 0), nx - 1)
        j = min(max(int((y - y0) / cell_mm), 0), ny - 1)
        mount_g[layers[0]][j, i] += conductance / 2
        mount_g[layers[-1]][j, i] += conductance / 2

    base = Sheets(layers=layers, cell_mm=cell_mm, origin=(x0, y0), inside=inside,
                  kt=kt, vertical=vertical, capacity=capacity, surface={},
                  face_area_m2=face, mount_w_k=mount_g)
    return with_environment(base, surface_w_m2k, bottom_w_m2k)


def deposit_flow(model: HeatModel, q: np.ndarray, solution, scale: float = 1.0) -> None:
    """Put a `dcflow.FlowSolution`'s loss where it was dissipated.

    ``scale`` multiplies the loss — ``(I / I_solved)^2`` re-uses one solve at
    another current, which is exact while resistivity is held fixed.
    """
    ny, nx = solution.shape
    h = solution.cell_mm
    xs = solution.origin[0] + (np.arange(nx) + 0.5) * h
    ys = solution.origin[1] + (np.arange(ny) + 0.5) * h
    gx, gy = np.meshgrid(xs, ys)
    for layer, grid in solution.layer_loss_w.items():
        m = grid > 0
        if m.any():
            model.deposit(q, layer, gx[m], gy[m], grid[m] * scale)
    for x, y, upper, lower, watts in solution.barrel_loss:
        model.deposit(q, upper, x, y, watts * scale / 2)
        model.deposit(q, lower, x, y, watts * scale / 2)


def deposit_part(model: HeatModel, q: np.ndarray, board, ref: str, watts: float) -> None:
    """A part's dissipation, spread over its own pads' copper on its own side,
    shared between pads by area — a bottom-cooled package's heat really does
    enter there. Spread over the pad *area*, never put at a pad's centre: a
    point source on a fine grid reads tens of degrees hot (`cells_in`)."""
    fps = [f for f in board.footprints if f.ref == ref]
    if not fps:
        raise HeatError(f"{ref} is in the thermal manifest but not on the board")
    side = getattr(fps[0], "layer", None) or model.layers[0]
    if side not in model.layers:
        side = model.layers[0]
    pads = [p for p in board.pads if p.ref == ref and side in p.copper_layers]
    if not pads:
        x, y = fps[0].pos
        model.deposit(q, side, x, y, watts)
        return
    total = sum(p.copper.area for p in pads) or 1.0
    for pad in pads:
        cells = model.cells_in(side, pad.copper)
        if cells.size:
            np.add.at(q, cells, watts * pad.copper.area / total / cells.size)


def part_cells(model: HeatModel, board, ref: str) -> np.ndarray:
    """Every cell under a part's pads on its own side."""
    fps = [f for f in board.footprints if f.ref == ref]
    if not fps:
        return np.empty(0, dtype=np.int64)
    side = getattr(fps[0], "layer", None) or model.layers[0]
    side = side if side in model.layers else model.layers[0]
    found = [model.cells_in(side, p.copper) for p in board.pads
             if p.ref == ref and side in p.copper_layers]
    return np.unique(np.concatenate(found)) if found else np.empty(0, dtype=np.int64)
