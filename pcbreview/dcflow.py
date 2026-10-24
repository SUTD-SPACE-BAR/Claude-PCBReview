"""DC current flow through a net's whole copper — every layer at once, at a stated current.

`current.py` answers *"is one feed of this net worse than its siblings"*, on one
layer, with no current at all, and that is the right question when nobody has
said what the net carries. This module answers the question that follows once
somebody has: **at 6 A from these pads to those pads, what does the copper
drop, what does it dissipate, where, and how much goes down each via?**

That needs all the copper together. A real power path leaves a regulator on
the top layer, dives through a handful of vias into a half-ounce plane, comes
back up beside the load, and the vias carry very unequal shares of it — on one
six-layer board a single via under a buck's ground pin carried 4.9 A of a 6 A
return while its neighbour carried 1.1 A. No single-layer solve, and no
erosion bottleneck, can see that.

The model
---------
Each copper layer of the net is rasterised onto one shared square grid and
becomes a sheet of resistors: neighbouring cells are joined by the conductance
of one square of that layer's copper, ``t / rho``. Thickness comes from the
**bound stackup**, never from an assumed ounce weight. Layers are joined by
**barrels** — vias and plated holes — each a plated tube of conductance
``sigma * pi * d * plating / L`` between consecutive layers it passes, with
``L`` the distance between those layers' mid-planes in the stackup.

Two kinds of cell group are **merged into single nodes by Galerkin projection**
(``A' = P^T A P``), exactly as `current.plane_resistance` merges an electrode:

* **a terminal** — the pads current enters or leaves by. A through-hole
  terminal is merged across every layer it is on, because the pin and its
  solder join them far better than the plating alone does;
* **a via land on one layer** — the barrel's contact with that layer's copper.

Rewriting rows in place instead of projecting silently shrinks an electrode to
one cell and adds a spreading resistance that is not on the board.

Everything not joined to the source terminal (floating islands of the same
net, pours cut off by an antipad ring) is dropped before the solve rather than
pinned by a leak conductance, and a sink that ends up in a different island is
**refused by name**: "these pads are not joined by copper" is a finding about
the board, not a numerical accident.

What comes out
--------------
* the potential drop from the source to every sink, and the two-terminal
  resistance when there is one sink;
* the I^2R loss, total and **per cell per layer** — the heat source map that
  `boardheat` turns into a temperature;
* the current in every barrel, against its IPC-2152 rating;
* the sheet current density, and the worst **dissipation hotspots** averaged
  over a 1 mm window, so a single corner cell cannot dominate.

Resistivity is copper at 20 C unless a temperature field is supplied, in which
case each cell gets ``rho * (1 + alpha (T - 20))``. That is how `ampacity`
couples this to `boardheat`: solve cold, heat the board, re-solve hot.

Validation
----------
In `tests/test_dcflow.py`, all against closed form, at two grid pitches so the
error is seen to fall with the grid:

* a straight strip against ``rho L / (W t)`` — +1.02%, +0.51%, +0.26% at 0.2,
  0.1 and 0.05 mm;
* an annulus against ``rho ln(r2/r1) / (2 pi t)`` — a spreading problem, which
  is what a plane under a via actually is — +1.67% and +0.76% at 0.1 and
  0.05 mm;
* two layers joined by one via against strip + barrel + strip in series;
* N identical vias in parallel share the current equally, to 1%;
* energy: the solved loss equals ``I * V`` at the terminals.

Measured on a six-layer board, twelve declared paths: halving the grid from
0.1 to 0.05 mm moved the drops by 0.3% to 7.1%, **always downward** — a coarse
grid can only overstate resistance, because it cannot resolve the narrowest
copper. Energy balanced on every path to 2% (I^2R against drop x current at
each sink). `ampacity --check-grid` reports the grid change for every path
rather than asking anyone to take it on trust.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
import scipy.sparse.csgraph as csgraph
import scipy.sparse.linalg as spla
import shapely
from scipy.ndimage import uniform_filter
from shapely.geometry import Point
from shapely.ops import unary_union

from pcbreview import current, physics

#: Copper resistivity at 20 C, and its temperature coefficient. Both shared
#: with `physics` so a resistance here and a resistance there agree.
RHO_OHM_M = physics.COPPER_RESISTIVITY_OHM_M
TEMPCO_PER_C = physics.COPPER_TEMPCO_PER_C

#: Default grid pitch. 0.1 mm resolves a 0.2 mm track as two cells and
#: reproduces a strip to 0.5% and an annulus to 1.7%; 0.05 mm halves both
#: errors (0.26%, 0.76%) at four times the cells. Coarser is used automatically only when the net is too big
#: for `MAX_CELLS`, and the pitch actually used is always reported.
CELL_MM = 0.1

#: Above this many cells the pitch is coarsened. A whole-board ground net on
#: six layers is the case this exists for: 0.1 mm would be several million
#: cells for a sparse solve that buys almost nothing over 0.15 mm on copper
#: that wide.
MAX_CELLS = 1_500_000

#: Window the dissipation density is averaged over when hotspots are ranked.
#: Without it the hottest "spot" is always one corner cell of one pad, which is
#: a property of the grid and not of the board.
HOTSPOT_WINDOW_MM = 1.0

#: How many distinct hotspots to report, and how far apart they must be.
HOTSPOT_COUNT = 5
HOTSPOT_SEPARATION_MM = 2.0

#: How far from a terminal pad the solve will look for a cell when a pad is
#: smaller than one cell. Matches `current.ELECTRODE_RADIUS_MM`.
TERMINAL_REACH_MM = current.ELECTRODE_RADIUS_MM


class FlowError(Exception):
    """Raised when a path cannot be solved — and the message says which part."""


# ---------------------------------------------------------------------------
# What the solver sees
# ---------------------------------------------------------------------------

@dataclass
class Barrel:
    """A plated hole joining copper layers: a via, or a through-hole pad."""
    label: str
    x: float
    y: float
    drill_mm: float
    layers: list[str]
    #: the copper the barrel touches on each layer — its land
    land: dict[str, object]
    kind: str = "via"            # via | hole


@dataclass
class Conductor:
    """One net's copper, layer by layer, with the stackup facts it needs."""
    net: str
    #: board copper layer names, top to bottom
    layers: list[str]
    copper: dict[str, object]
    thickness_mm: dict[str, float]
    #: depth of each layer's mid-plane below the top surface
    z_mm: dict[str, float]
    barrels: list[Barrel] = field(default_factory=list)
    plating_um: float = current.VIA_PLATING_UM


@dataclass
class Terminal:
    """Where current enters or leaves: named, with its copper on each layer."""
    name: str
    copper: dict[str, object]
    #: pad designators it was built from, for messages
    pads: list[str] = field(default_factory=list)


@dataclass
class BarrelCurrent:
    label: str
    x: float
    y: float
    drill_mm: float
    current_a: float
    loss_w: float
    #: the layer pair carrying the largest segment current
    span: tuple[str, str]


@dataclass
class Hotspot:
    layer: str
    x: float
    y: float
    #: dissipation density averaged over `HOTSPOT_WINDOW_MM`
    density_mw_mm2: float
    #: sheet current density there, averaged the same way
    sheet_a_per_mm: float


@dataclass
class FlowSolution:
    net: str
    current_a: float
    cell_mm: float
    cells: int
    nodes: int
    #: potential drop from the source terminal to each sink terminal, volts
    drop_v: dict[str, float]
    loss_w: float
    barrels: list[BarrelCurrent]
    hotspots: list[Hotspot]
    #: grid description, so a heat model can take the loss map
    origin: tuple[float, float]
    shape: tuple[int, int]
    layer_loss_w: dict[str, np.ndarray]      # W per cell
    layer_sheet_a_per_mm: dict[str, np.ndarray]
    layer_mask: dict[str, np.ndarray]
    #: barrel loss as point sources: (x, y, upper layer, lower layer, watts)
    barrel_loss: list[tuple[float, float, str, str, float]]
    islands_dropped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def resistance_ohm(self) -> float | None:
        if len(self.drop_v) != 1 or self.current_a <= 0:
            return None
        return next(iter(self.drop_v.values())) / self.current_a

    @property
    def worst_drop_v(self) -> float:
        return max(self.drop_v.values()) if self.drop_v else 0.0

    def worst_barrel(self) -> BarrelCurrent | None:
        return max(self.barrels, key=lambda b: b.current_a, default=None)


# ---------------------------------------------------------------------------
# Building a conductor from a board
# ---------------------------------------------------------------------------

def stack_geometry(board) -> tuple[dict[str, float], dict[str, float]]:
    """Copper thickness and mid-plane depth for every board copper layer.

    From the bound stackup and nothing else. A board without one is refused
    upstream (Gate 1); a copper layer the stackup does not describe is refused
    here, because a guessed thickness is a guessed resistance.
    """
    stack = board.stackup
    if stack is None:
        raise FlowError("current flow needs the bound fabrication stackup — "
                        "copper thickness is half of every resistance")
    to_board = {name: stack.layer_map.get(name, name)
                for name in (l.name for l in stack.layers if l.is_copper)}
    thickness, depth = {}, {}
    for label, (top, bottom) in stack.copper_z_positions().items():
        name = to_board.get(label, label)
        thickness[name] = bottom - top
        depth[name] = (top + bottom) / 2.0
    missing = [l for l in board.copper_layers if l not in thickness]
    if missing:
        raise FlowError(f"the stackup does not describe copper layer(s) "
                        f"{missing}; bind the stackup the board is built on")
    return thickness, depth


def conductor_for(board, net: str,
                  plating_um: float = current.VIA_PLATING_UM) -> Conductor:
    """Every piece of one net's copper, with its barrels."""
    layers = list(board.copper_layers)
    thickness, depth = stack_geometry(board)
    code = board.net(net).code
    copper = {}
    for layer in layers:
        geom = board.copper_on_layer(layer, net)
        if not geom.is_empty:
            copper[layer] = geom
    barrels: list[Barrel] = []
    for via in board.vias:
        if via.net != code:
            continue
        spans = [l for l in layers if l in via.layers]
        if len(spans) < 2:
            continue
        barrels.append(Barrel(
            label=f"via at ({via.pos[0]:.2f}, {via.pos[1]:.2f})",
            x=via.pos[0], y=via.pos[1], drill_mm=via.drill_mm, layers=spans,
            land={l: via.pad_on(l) for l in spans}, kind="via"))
    for pad in board.pads:
        if pad.net != code or not pad.is_plated_hole:
            continue
        spans = [l for l in layers if l in pad.copper_layers]
        drill = max(pad.drill_mm) if pad.drill_mm else 0.0
        if len(spans) < 2 or drill <= 0:
            continue
        barrels.append(Barrel(
            label=pad.designator, x=pad.pos[0], y=pad.pos[1], drill_mm=drill,
            layers=spans, land={l: pad.copper for l in spans}, kind="hole"))
    return Conductor(net=net, layers=layers, copper=copper,
                     thickness_mm=thickness, z_mm=depth, barrels=barrels,
                     plating_um=plating_um)


def expand_pads(board, net: str, specs: list[str]) -> list:
    """Pads named by `U3.17`, `U3.17-24` (a numeric range) or `U3` (every pad
    of U3 on this net). A spec that matches nothing on the net is refused and
    the message lists what that part does have here."""
    code = board.net(net).code
    on_net = [p for p in board.pads if p.net == code]
    chosen, seen = [], set()
    for spec in specs:
        spec = str(spec).strip()
        hits = []
        if "." not in spec:
            hits = [p for p in on_net if p.ref == spec]
        else:
            ref, number = spec.rsplit(".", 1)
            if "-" in number and all(s.isdigit() for s in number.split("-", 1)):
                lo, hi = (int(s) for s in number.split("-", 1))
                hits = [p for p in on_net if p.ref == ref and p.number.isdigit()
                        and lo <= int(p.number) <= hi]
            else:
                hits = [p for p in on_net if p.ref == ref and p.number == number]
        if not hits:
            ref = spec.split(".", 1)[0]
            here = sorted(p.designator for p in on_net if p.ref == ref)
            raise FlowError(
                f"{spec!r} names no pad on {net}"
                + (f"; {ref} has {', '.join(here)} on this net" if here
                   else f"; {ref} has no pad on this net at all"))
        for p in hits:
            if p.designator not in seen:
                seen.add(p.designator)
                chosen.append(p)
    return chosen


def terminal_for(board, net: str, name: str, specs: list[str]) -> Terminal:
    layers = set(board.copper_layers)
    copper: dict[str, list] = {}
    pads = expand_pads(board, net, specs)
    for pad in pads:
        for layer in pad.copper_layers:
            if layer in layers:
                copper.setdefault(layer, []).append(pad.copper)
    return Terminal(name=name, copper={l: unary_union(v) for l, v in copper.items()},
                    pads=[p.designator for p in pads])


# ---------------------------------------------------------------------------
# The solve
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self):
        self.parent: list[int] = []

    def add(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _pitch(conductor: Conductor, cell_mm: float) -> float:
    area = sum(g.area for g in conductor.copper.values())
    if area / (cell_mm * cell_mm) <= MAX_CELLS:
        return cell_mm
    return math.ceil(math.sqrt(area / MAX_CELLS) * 100) / 100.0


def solve(conductor: Conductor, source: Terminal,
          sinks: list[tuple[Terminal, float]], cell_mm: float = CELL_MM,
          temperature_c=None) -> FlowSolution:
    """Inject the sinks' total at `source`; draw each sink's current out.

    ``temperature_c``, if given, is called as ``temperature_c(layer, xs, ys)``
    with arrays of cell centres and returns each cell's copper temperature;
    resistivity follows it. Without it every cell is at 20 C.
    """
    if not sinks:
        raise FlowError("a path needs at least one sink")
    if any(amps < 0 for _, amps in sinks):
        raise FlowError("sink currents are magnitudes; give them as positive")
    names = [source.name] + [t.name for t, _ in sinks]
    if len(set(names)) != len(names):
        raise FlowError(f"terminal names must differ: {names}")
    for terminal in [source] + [t for t, _ in sinks]:
        if not terminal.copper:
            raise FlowError(f"terminal {terminal.name!r} has no copper on any "
                            f"layer of {conductor.net}")
    layers = [l for l in conductor.layers if l in conductor.copper]
    if not layers:
        raise FlowError(f"{conductor.net} has no copper")
    h = _pitch(conductor, cell_mm)
    notes = []
    if h != cell_mm:
        notes.append(f"grid coarsened to {h:.2f} mm: {conductor.net} has "
                     f"more copper than {MAX_CELLS:,} cells at {cell_mm} mm")

    x0, y0, x1, y1 = unary_union([conductor.copper[l] for l in layers]).bounds
    x0 -= h; y0 -= h; x1 += h; y1 += h
    nx = int(math.ceil((x1 - x0) / h)); ny = int(math.ceil((y1 - y0) / h))
    xs = x0 + (np.arange(nx) + 0.5) * h
    ys = y0 + (np.arange(ny) + 0.5) * h
    gx, gy = np.meshgrid(xs, ys)

    masks, index, offset = {}, {}, 0
    for layer in layers:
        geom = conductor.copper[layer]
        shapely.prepare(geom)
        mask = shapely.contains_xy(geom, gx, gy)
        masks[layer] = mask
        idx = -np.ones(mask.shape, dtype=np.int64)
        idx[mask] = offset + np.arange(int(mask.sum()))
        index[layer] = idx
        offset += int(mask.sum())
    n_cells = offset
    if n_cells == 0:
        raise FlowError(f"{conductor.net}: no copper cell at a {h} mm pitch")

    # per-cell sheet conductance, t / rho(T)
    g_cell = np.empty(n_cells)
    for layer in layers:
        idx = index[layer]; m = masks[layer]
        rho = np.full(int(m.sum()), RHO_OHM_M)
        if temperature_c is not None:
            temps = np.asarray(temperature_c(layer, gx[m], gy[m]), dtype=float)
            rho = RHO_OHM_M * (1.0 + TEMPCO_PER_C * (temps - 20.0))
        g_cell[idx[m]] = conductor.thickness_mm[layer] * 1e-3 / rho

    rows, cols, vals = [], [], []
    faces = []        # (a, b, g) per layer, kept for losses and currents
    for layer in layers:
        idx = index[layer]
        for a, b in ((idx[:, :-1], idx[:, 1:]), (idx[:-1, :], idx[1:, :])):
            ok = (a >= 0) & (b >= 0)
            ca, cb = a[ok], b[ok]
            g = 2.0 * g_cell[ca] * g_cell[cb] / (g_cell[ca] + g_cell[cb])
            faces.append((layer, ca, cb, g))
            rows += [ca, cb, ca, cb]; cols += [ca, cb, cb, ca]
            vals += [g, g, -g, -g]
    A = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows),
                                              np.concatenate(cols))),
                      shape=(n_cells, n_cells))

    # groups: terminals first (they win a contested cell), then via lands
    uf = _UnionFind()
    label = -np.ones(n_cells, dtype=np.int64)

    def cells_under(geom, layer, reach=0.0):
        if layer not in masks:
            return np.empty(0, dtype=np.int64)
        bx0, by0, bx1, by1 = geom.bounds
        i0 = max(int((bx0 - x0) / h) - 1, 0); i1 = min(int((bx1 - x0) / h) + 2, nx)
        j0 = max(int((by0 - y0) / h) - 1, 0); j1 = min(int((by1 - y0) / h) + 2, ny)
        if i0 >= i1 or j0 >= j1:
            return np.empty(0, dtype=np.int64)
        sub = shapely.contains_xy(geom, gx[j0:j1, i0:i1], gy[j0:j1, i0:i1])
        sub &= masks[layer][j0:j1, i0:i1]
        found = index[layer][j0:j1, i0:i1][sub]
        if found.size == 0 and reach > 0:
            c = geom.centroid
            d2 = (gx - c.x) ** 2 + (gy - c.y) ** 2
            d2 = np.where(masks[layer], d2, np.inf)
            j = int(np.argmin(d2))
            if math.sqrt(d2.flat[j]) <= reach:
                found = np.array([index[layer].flat[j]])
        return found

    def paint(group, cells):
        if cells.size == 0:
            return
        for other in np.unique(label[cells][label[cells] >= 0]):
            uf.union(group, int(other))
        label[cells] = group

    terminal_group = {}
    for terminal in [source] + [t for t, _ in sinks]:
        g = uf.add()
        terminal_group[terminal.name] = g
        touched = 0
        for layer, geom in terminal.copper.items():
            cells = cells_under(geom, layer, reach=TERMINAL_REACH_MM)
            paint(g, cells)
            touched += cells.size
        if touched == 0:
            raise FlowError(f"terminal {terminal.name!r} ({', '.join(terminal.pads)}) "
                            f"touches no copper of {conductor.net} on the grid")
    # two terminals that ended up merged are one node, and the path is a short
    roots = {name: uf.find(g) for name, g in terminal_group.items()}
    if len(set(roots.values())) != len(roots):
        raise FlowError("two terminals share copper cells, so they are one node: "
                        + ", ".join(f"{n}" for n in roots))

    barrel_group = []
    for barrel in conductor.barrels:
        per_layer = {}
        for layer in barrel.layers:
            g = uf.add()
            per_layer[layer] = g
            if layer in masks:
                paint(g, cells_under(barrel.land[layer], layer))
        barrel_group.append(per_layer)
    roots = {name: uf.find(g) for name, g in terminal_group.items()}
    if len(set(roots.values())) != len(roots):
        raise FlowError("a via joins two terminals directly, so they are one "
                        "node: " + ", ".join(roots))

    # node numbering: one node per group root, then one per ungrouped cell
    n_groups = len(uf.parent)
    group_root = np.array([uf.find(g) for g in range(n_groups)], dtype=np.int64)
    used_roots = np.unique(group_root)
    root_node = -np.ones(n_groups, dtype=np.int64)
    root_node[used_roots] = np.arange(used_roots.size)
    node_of_cell = np.empty(n_cells, dtype=np.int64)
    grouped = label >= 0
    node_of_cell[grouped] = root_node[group_root[label[grouped]]]
    free = np.flatnonzero(~grouped)
    node_of_cell[free] = used_roots.size + np.arange(free.size)
    n_nodes = used_roots.size + free.size
    P = sp.csr_matrix((np.ones(n_cells), (np.arange(n_cells), node_of_cell)),
                      shape=(n_cells, n_nodes))
    Ar = (P.T @ A @ P).tocoo()

    # barrels between consecutive layers they pass
    b_rows, b_cols, b_vals, segments = [], [], [], []
    for k, barrel in enumerate(conductor.barrels):
        spans = barrel.layers
        for upper, lower in zip(spans[:-1], spans[1:]):
            length_m = abs(conductor.z_mm[lower] - conductor.z_mm[upper]) * 1e-3
            if length_m <= 0:
                continue
            g = (math.pi * barrel.drill_mm * 1e-3 * conductor.plating_um * 1e-6
                 / (RHO_OHM_M * length_m))
            a = root_node[group_root[barrel_group[k][upper]]]
            b = root_node[group_root[barrel_group[k][lower]]]
            if a == b:
                continue
            segments.append((k, upper, lower, a, b, g))
            b_rows += [a, b, a, b]; b_cols += [a, b, b, a]; b_vals += [g, g, -g, -g]
    K = sp.csr_matrix((np.concatenate([Ar.data, np.array(b_vals, dtype=float)]),
                       (np.concatenate([Ar.row, np.array(b_rows, dtype=np.int64)]),
                        np.concatenate([Ar.col, np.array(b_cols, dtype=np.int64)]))),
                      shape=(n_nodes, n_nodes))

    # keep only what is joined to the source
    _, component = csgraph.connected_components(K, directed=False)
    src_node = root_node[group_root[terminal_group[source.name]]]
    keep = component == component[src_node]
    apart = [t.name for t, _ in sinks
             if not keep[root_node[group_root[terminal_group[t.name]]]]]
    if apart:
        raise FlowError(f"{conductor.net}: {', '.join(apart)} not joined to "
                        f"{source.name} by copper or vias")
    islands = int(len(np.unique(component)) - 1)

    total = float(sum(a for _, a in sinks))
    rhs = np.zeros(n_nodes)
    rhs[src_node] = total
    ref_node = root_node[group_root[terminal_group[sinks[0][0].name]]]
    for terminal, amps in sinks[1:]:
        rhs[root_node[group_root[terminal_group[terminal.name]]]] -= amps
    solve_nodes = np.flatnonzero(keep & (np.arange(n_nodes) != ref_node))
    Ks = K[solve_nodes][:, solve_nodes].tocsc()
    V = np.full(n_nodes, np.nan)
    V[ref_node] = 0.0
    V[solve_nodes] = spla.spsolve(Ks, rhs[solve_nodes])
    V_cell = V[node_of_cell]

    # losses and sheet current per layer
    loss_cell = np.zeros(n_cells)
    ix = np.zeros(n_cells); iy = np.zeros(n_cells)
    horizontal = True
    for layer, ca, cb, g in faces:
        dv = np.nan_to_num(V_cell[ca] - V_cell[cb])
        p = g * dv * dv
        np.add.at(loss_cell, ca, p / 2); np.add.at(loss_cell, cb, p / 2)
        flow = g * dv
        target = ix if horizontal else iy
        np.add.at(target, ca, flow / 2); np.add.at(target, cb, flow / 2)
        horizontal = not horizontal
    loss = float(loss_cell.sum())

    barrel_currents, barrel_loss = [], []
    per_barrel: dict[int, list] = {}
    for k, upper, lower, a, b, g in segments:
        dv = 0.0 if (np.isnan(V[a]) or np.isnan(V[b])) else V[a] - V[b]
        per_barrel.setdefault(k, []).append((abs(g * dv), g * dv * dv, upper, lower))
        loss += g * dv * dv
    for k, segs in per_barrel.items():
        barrel = conductor.barrels[k]
        top = max(segs, key=lambda s: s[0])
        barrel_currents.append(BarrelCurrent(
            label=barrel.label, x=barrel.x, y=barrel.y, drill_mm=barrel.drill_mm,
            current_a=top[0], loss_w=sum(s[1] for s in segs),
            span=(top[2], top[3])))
        for s in segs:
            if s[1] > 0:
                barrel_loss.append((barrel.x, barrel.y, s[2], s[3], s[1]))
    barrel_currents.sort(key=lambda b: -b.current_a)

    layer_loss, layer_sheet, layer_mask = {}, {}, {}
    window = max(1, int(round(HOTSPOT_WINDOW_MM / h)))
    candidates = []
    contact = label >= 0
    for layer in layers:
        idx = index[layer]; m = masks[layer]
        grid_loss = np.zeros(m.shape); grid_loss[m] = loss_cell[idx[m]]
        sheet = np.zeros(m.shape)
        sheet[m] = np.hypot(ix[idx[m]], iy[idx[m]]) / h      # A per mm
        layer_loss[layer] = grid_loss
        layer_sheet[layer] = sheet
        layer_mask[layer] = m
        # density over free copper only: inside a terminal or a via land the
        # cells are one node and carry no current of their own
        free_m = m.copy()
        free_m[m] = ~contact[idx[m]]
        density = np.where(free_m, grid_loss / (h * h) * 1e3, 0.0)   # mW/mm2
        count = uniform_filter(free_m.astype(float), window)
        avg = np.where(count > 0.4, uniform_filter(density, window)
                       / np.maximum(count, 1e-9), 0.0)
        avg_sheet = np.where(count > 0.4, uniform_filter(
            np.where(free_m, sheet, 0.0), window) / np.maximum(count, 1e-9), 0.0)
        candidates.append((layer, avg, avg_sheet))
    hotspots = _rank_hotspots(candidates, xs, ys, h)

    drops = {t.name: float(V[src_node] - V[root_node[group_root[terminal_group[t.name]]]])
             for t, _ in sinks}
    return FlowSolution(
        net=conductor.net, current_a=total, cell_mm=h, cells=n_cells,
        nodes=int(keep.sum()), drop_v=drops, loss_w=loss,
        barrels=barrel_currents, hotspots=hotspots, origin=(x0, y0),
        shape=(ny, nx), layer_loss_w=layer_loss,
        layer_sheet_a_per_mm=layer_sheet, layer_mask=layer_mask,
        barrel_loss=barrel_loss, islands_dropped=islands, notes=notes)


def _rank_hotspots(candidates, xs, ys, h) -> list[Hotspot]:
    """The worst few windows, at least `HOTSPOT_SEPARATION_MM` apart."""
    out: list[Hotspot] = []
    pool = []
    for layer, avg, sheet in candidates:
        flat = np.argsort(avg, axis=None)[::-1][:400]
        for k in flat:
            j, i = np.unravel_index(k, avg.shape)
            if avg[j, i] <= 0:
                break
            pool.append((float(avg[j, i]), layer, float(xs[i]), float(ys[j]),
                         float(sheet[j, i])))
    pool.sort(key=lambda t: -t[0])
    for density, layer, x, y, sheet in pool:
        if any(math.hypot(x - o.x, y - o.y) < HOTSPOT_SEPARATION_MM for o in out):
            continue
        out.append(Hotspot(layer=layer, x=x, y=y, density_mw_mm2=density,
                           sheet_a_per_mm=sheet))
        if len(out) >= HOTSPOT_COUNT:
            break
    return out


def solve_path(board, net: str, source_pads: list[str],
               sinks: list[tuple[list[str], float]], cell_mm: float = CELL_MM,
               plating_um: float = current.VIA_PLATING_UM,
               temperature_c=None) -> FlowSolution:
    """`solve` for a board net, terminals named by pad designators."""
    conductor = conductor_for(board, net, plating_um)
    source = terminal_for(board, net, "source", source_pads)
    terminals = [(terminal_for(board, net, f"sink{k + 1}", pads), amps)
                 for k, (pads, amps) in enumerate(sinks)]
    return solve(conductor, source, terminals, cell_mm=cell_mm,
                 temperature_c=temperature_c)


def barrel_rating_a(drill_mm: float, rise_c: float,
                    plating_um: float = current.VIA_PLATING_UM) -> float:
    """IPC-2152 rating of one barrel at `rise_c` — `current.via_capacity_a`."""
    return current.via_capacity_a(drill_mm, plating_um, rise_c)


def adiabatic_rise_c(sheet_a_per_mm: float, thickness_mm: float,
                     seconds: float) -> float:
    """Upper bound on a copper sheet's temperature rise over a short pulse.

    No heat leaves: ``dT = J^2 rho t / (rho_m c)``. Real copper sheds heat
    sideways within milliseconds, so beyond ~10 ms this overstates the rise by
    a large factor. It is reported as a bound, never as the temperature.
    """
    j = sheet_a_per_mm / thickness_mm * 1e6              # A/m^2
    return j * j * RHO_OHM_M * seconds / COPPER_HEAT_CAPACITY_J_M3K


#: Volumetric heat capacity of copper, rho * c_p = 8960 kg/m^3 * 385 J/kgK.
COPPER_HEAT_CAPACITY_J_M3K = 8960 * 385
