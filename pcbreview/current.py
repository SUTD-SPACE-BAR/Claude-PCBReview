"""How much current the copper can actually carry, and where it pinches.

§32.B9, and the largest hole this tool had. `conductor_neck` looks at *tracks*
and is blind to poured copper, so a board whose 40 A bus is a plane got no
conducted-current analysis at all.

Three measurements, and the first thing to know is which to use when.

**The narrowest channel in a conductor** — `bottleneck_mm`. Binary search on
erosion radius: the largest ``r`` for which the copper, eroded by ``r``, still
joins source to sink. The channel width is ``2r``. This is
``getMinimumTraceWidth()`` from the original brief, and it is what feeds
IPC-2152 (`physics.current_capacity_a`).

**A plane's real DC resistance** — `plane_resistance`. Rasterise the pour,
build the five-point Laplacian over copper cells, merge each via cluster into
one node and solve.

**Via capacity** — `via_capacity_a`. A barrel is an IPC-2152 conductor whose
width is its circumference and whose thickness is the plating.

Erosion is the wrong measure for a plane, and this is not a subtlety
---------------------------------------------------------------------
Current flows *around* a via antipad rather than through it, so a field of
holes reads as a constriction and is not one. Measured: on a 40 A battery plane
the erosion bottleneck read 5.5 mm, which taken at face value says 3.4 A. The
Laplace solve on the same copper says 2.0 mOhm end to end and 3.1 W. **The
erosion number would have been a false `critical`.**

So `worst_feed` uses resistance for poured nets, erosion for tracked ones, and
`check_current_capacity` says which it used on every finding. Where the two
agree — as they did on the one real constriction found so far, 0.49 mm and
40.4 mOhm against 6.9-15.0 mOhm for its seven siblings — the finding is solid.

Why the headline is a *relative* comparison
-------------------------------------------
An absolute capacity needs a current, and a current is intent. But a board that
feeds eight identical channels from one source states its own expectation:
every channel should look like every other channel. So the primary finding is
**a terminal whose feed resistance is an outlier against its own net's median**,
which needs no current at all and is exactly how the one real defect surfaced.
The absolute IPC-2152 capacity is reported beside it, carrying `intent_needed`.

Validation
----------
Both solvers self-test against closed form, in `tests/test_current.py`:
a plain strip reads its own width; a notched strip reads the notch; and a
rectangle's resistance matches ``Rsheet * L / W``. `plane_resistance` returns a
true two-terminal resistance (one factorisation, one back-substitution per
terminal), so it does not depend on an assumed load distribution.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

import numpy as np
import scipy.ndimage as ndimage
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import shapely
from shapely.geometry import Point

from pcbreview import physics
from pcbreview.model import Board

#: Copper resistivity at 20 C. The same constant `physics` uses, restated here
#: only because the sheet-resistance form is what a plane wants.
RHO_COPPER_OHM_M = physics.COPPER_RESISTIVITY_OHM_M

#: Rasterisation pitch for the plane solve. 0.25 mm reproduces the closed-form
#: rectangle to 0.4%; 0.5 mm is 4x faster and reproduces it to about 2%, which
#: is not enough for a ratio between two feeds on the same net.
PLANE_CELL_MM = 0.25

#: How far from a contact point the solve will look for copper to inject into.
#: Shared by `_solve_region`, which decides which region of a pour a terminal
#: belongs to, and `plane_resistance`, which turns that point into an electrode.
#: **These two have to agree.** If region membership were decided by strict
#: containment while the electrode search reached half a millimetre, a via
#: sitting in its own thermal relief would be ruled off the region it is plainly
#: connected to, and the terminal would vanish before the solve ever saw it.
ELECTRODE_RADIUS_MM = 0.45

#: Plated barrel thickness assumed for a via when the fab has not said. IPC-6012
#: class 2 requires 20 um average, 18 um minimum; JLCPCB quote 25 um typical.
#: Stated on every finding as an assumption because it scales capacity directly.
VIA_PLATING_UM = 25.0

#: Temperature rise the reported capacity is quoted at. 10 C is the IPC
#: charts' conventional working point and the conservative one; 20 C is also
#: quoted because power designs routinely allow it.
RISE_C = 10.0

#: Terminals a net needs before it is worth solving at all — **four, not
#: three**, and the difference was a coverage lie of the §52.1 shape.
#:
#: The headline is a feed that is an outlier against its own net's median.
#: One terminal is the source, so a net with `n` terminals yields at most
#: `n - 1` feeds, and `audit.FEED_MIN_TERMINALS = 3` is a floor on the number
#: of **comparable feeds** — which needs four terminals, not three.
#:
#: The arithmetic is why three can never work rather than merely usually not.
#: With two feeds `a <= b` the median is `(a + b) / 2`, so the worst ratio is
#: `2b / (a + b)`, which tends to **2 as b/a tends to infinity** and never
#: reaches it. `audit.FEED_RATIO_MINOR` is 3.0. **A two-feed net therefore
#: cannot produce a finding at any severity, however bad the imbalance is**,
#: and lowering the ratio threshold to reach them would flag every net whose
#: two feeds differ at all.
#:
#: This floor used to be 3, so every 3-terminal net was counted in
#: `coverage()['nets_examined']` and then declined by construction. Measured
#: over the five boards: **11 of 33 candidates** were in that state — a third
#: of the population, counted as looked at and guaranteed never to be. Every
#: one of them is now in `nets_with_too_few_terminals`, where it belongs, and
#: the honest comparison rate goes from 9/33 to 9/22 without a single net
#: being measured differently.
MIN_TERMINALS = 4

#: How far from a terminal's own pads a via counts as that terminal's way down
#: to the plane. **Measured rather than tuned**: over the five boards, raising
#: it from 2.0 mm to 3.0 or 4.0 rescues **no** declined net, and 6.0 rescues
#: exactly one — at a radius where a via belonging to a neighbouring part is
#: being claimed. §54.5 item 4 asked whether this buffer was the reason only
#: 27% of candidates are compared. It is not: 2 of the 33 candidates decline
#: because too few terminals reach the plane, and both are still short at
#: 6.0 mm.
VIA_SEARCH_MM = 2.0


# ---------------------------------------------------------------------------
# The narrowest channel
# ---------------------------------------------------------------------------

def _pieces(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in geom.geoms if g.geom_type == "Polygon"]


def _joins(geom, a, b) -> bool:
    for piece in _pieces(geom):
        if piece.intersects(a) and piece.intersects(b):
            return True
    return False


def _solve_region(pieces, contacts: dict,
                  radius_mm: float = ELECTRODE_RADIUS_MM) -> tuple:
    """The copper region to solve on, and each terminal's contacts on it.

    A net's pour on one layer is usually several disjoint polygons — a main
    plane, a corner fenced off by a keep-out, a strip under a connector —
    joined to each other only through vias to *another* layer.
    `plane_resistance` solves one sheet, so a resistance is only comparable
    between terminals that sit on the **same** region, and the region has to be
    chosen before the source is.

    **Choosing the largest region is the obvious rule and it is the wrong one.**
    On the V3 board's `In1.Cu` `GND` pour it picked the 3838 mm2 region holding
    34 of the 40 terminals that reach the pour — correctly — while the source
    `U18` sat with all 167 of its contact points on the 759 mm2 one. The source
    then had no electrode, `plane_resistance` returned before solving anything,
    and the best-instrumented net on the board produced **zero** feeds. The
    same shape lost `+3.3V_NAV` on `F.Cu`.

    Choosing the region carrying the **most terminals** makes the source a
    consequence of that choice instead of a constraint on it, and maximises
    what can be compared. Area breaks a tie, because between two regions with
    equal terminal counts the larger is the bus.

    Membership is `distance <= radius_mm`, not strict containment, because that
    is what `plane_resistance` does when it turns the point into an electrode —
    a via sitting in its own thermal relief is not inside the pour polygon and
    is obviously on it.
    """
    best = None
    for piece in pieces:
        on = {}
        for ref, points in contacts.items():
            mine = [p for p in points
                    if piece.distance(Point(*p)) <= radius_mm]
            if mine:
                on[ref] = mine
        if not on:
            continue
        key = (len(on), piece.area)
        if best is None or key > best[0]:
            best = (key, piece, on)
    if best is None:
        return None, {}
    return best[1], best[2]


def bottleneck_mm(copper, a: Point, b: Point, hi: float = 8.0,
                  tol: float = 0.002) -> float | None:
    """Narrowest copper channel joining `a` to `b`, or None if they are apart.

    The largest ``r`` for which erosion by ``r`` keeps them connected; the
    channel is ``2r`` wide. Exact on a rectangle and on a notched rectangle —
    see `tests/test_current.py`.

    **Not valid on a poured plane** (see the module docstring). Use
    `plane_resistance` there.
    """
    if not _joins(copper, a, b):
        return None
    lo = 0.0
    while hi - lo > tol:
        mid = (lo + hi) / 2
        eroded = copper.buffer(-mid, join_style=2)
        # the eroded copper no longer reaches the pad centres, so grow the
        # probes rather than shrinking the test
        if _joins(eroded, a.buffer(mid + 0.01), b.buffer(mid + 0.01)):
            lo = mid
        else:
            hi = mid
    return 2 * lo


def via_capacity_a(drill_mm: float, plating_um: float = VIA_PLATING_UM,
                   rise_c: float = RISE_C) -> float:
    """A via barrel as an IPC-2152 conductor: width = circumference, t = plating.

    The same still-air chart as a track. Conservative: the chart describes a
    long conductor in steady state, and a barrel is 1.6 mm long between two
    lands that sink its heat into the copper either side, so a real via
    tolerates more than this says. IPC-2152's appendix treats vias at length;
    nothing here uses those curves, so this stays the conservative reading.
    """
    return physics.current_capacity_a(math.pi * drill_mm, plating_um / 1000.0,
                                      rise_c, internal=False)


def sheet_resistance_ohm_sq(copper_oz: float, temp_c: float = 20.0) -> float:
    thickness_m = copper_oz * physics.COPPER_THICKNESS_PER_OZ_MM * 1e-3
    if thickness_m <= 0:
        raise physics.PhysicsError("copper weight must be positive")
    return (RHO_COPPER_OHM_M
            * (1 + physics.COPPER_TEMPCO_PER_C * (temp_c - 20.0))
            / thickness_m)


# ---------------------------------------------------------------------------
# The plane solve
# ---------------------------------------------------------------------------

@dataclass
class PlaneSolution:
    """Two-terminal resistances from one source terminal to each other one."""
    layer: str
    net: str
    sheet_ohm_sq: float
    cells: int
    #: terminal name -> resistance from the source terminal, ohms
    resistance_ohm: dict = field(default_factory=dict)
    source: str = ""

    def median_ohm(self) -> float | None:
        values = [v for v in self.resistance_ohm.values() if v is not None]
        return statistics.median(values) if values else None


def _rasterise(polygon, cell_mm):
    minx, miny, maxx, maxy = polygon.bounds
    nx = int(np.ceil((maxx - minx) / cell_mm))
    ny = int(np.ceil((maxy - miny) / cell_mm))
    xs = minx + (np.arange(nx) + 0.5) * cell_mm
    ys = miny + (np.arange(ny) + 0.5) * cell_mm
    grid_x, grid_y = np.meshgrid(xs, ys)
    mask = shapely.contains_xy(polygon, grid_x.ravel(),
                               grid_y.ravel()).reshape(ny, nx)
    return mask, (minx, miny)


def _laplacian(mask, sheet_ohm_sq):
    ny, nx = mask.shape
    index = -np.ones((ny, nx), dtype=np.int64)
    cells = np.argwhere(mask)
    index[mask] = np.arange(len(cells))
    conductance = 1.0 / sheet_ohm_sq
    rows, cols, vals = [], [], []
    for k, (j, i) in enumerate(cells):
        diagonal = 0.0
        for dj, di in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < ny and 0 <= ii < nx and mask[jj, ii]:
                rows.append(k)
                cols.append(int(index[jj, ii]))
                vals.append(-conductance)
                diagonal += conductance
        rows.append(k)
        cols.append(k)
        vals.append(diagonal)
    return sp.csr_matrix((vals, (rows, cols)),
                         shape=(len(cells), len(cells))), cells, index


def _electrode(index, mask, origin, cell_mm, points, radius_mm):
    ny, nx = mask.shape
    out = set()
    span = max(int(np.ceil(radius_mm / cell_mm)), 1)
    for (x, y) in points:
        ci = int((x - origin[0]) / cell_mm)
        cj = int((y - origin[1]) / cell_mm)
        for dj in range(-span, span + 1):
            for di in range(-span, span + 1):
                j, i = cj + dj, ci + di
                if 0 <= j < ny and 0 <= i < nx and mask[j, i]:
                    out.add(int(index[j, i]))
    return sorted(out)


def plane_resistance(polygon, copper_oz: float, terminals: dict,
                     source: str, cell_mm: float = PLANE_CELL_MM,
                     electrode_radius_mm: float = ELECTRODE_RADIUS_MM,
                     layer: str = "", net: str = "") -> PlaneSolution:
    """Two-terminal resistance from `source` to every other terminal.

    `terminals` maps a name to a list of ``(x, y)`` contact points — the via
    positions where current actually enters or leaves the plane.

    Each electrode is merged into a single node by **Galerkin projection**
    (``A' = P.T @ A @ P``), which keeps every face conductance of every cell in
    the group. Rewriting the rows in place instead silently shrinks the
    electrode to one cell, and the spreading resistance of a point contact is
    enormous — that mistake read 59 W on a plane that dissipates 4.5 W, with no
    symptom other than an implausible number.

    One factorisation, one back-substitution per terminal, so the result is a
    true two-terminal resistance and does not depend on an assumed load split.
    """
    sheet = sheet_resistance_ohm_sq(copper_oz)
    mask, origin = _rasterise(polygon, cell_mm)

    # Keep only the grid component the source sits in. A pour rasterises into
    # islands — a neck thinner than one cell, or genuinely separate copper —
    # and an island with no electrode in it has no Dirichlet reference, which
    # makes the matrix exactly singular. That is not a numerical nicety: it
    # crashed the solve on the second board it was run against.
    labels, count = ndimage.label(mask)
    if count > 1:
        source_points = terminals.get(source, [])
        source_labels = set()
        ny, nx = mask.shape
        for (x, y) in source_points:
            i = int((x - origin[0]) / cell_mm)
            j = int((y - origin[1]) / cell_mm)
            if 0 <= j < ny and 0 <= i < nx and labels[j, i]:
                source_labels.add(int(labels[j, i]))
        if not source_labels:
            biggest = int(np.argmax(np.bincount(labels.ravel())[1:])) + 1
            source_labels = {biggest}
        mask = np.isin(labels, list(source_labels))

    matrix, cells, index = _laplacian(mask, sheet)
    n = matrix.shape[0]
    if n == 0:
        raise physics.PhysicsError("no copper to solve on")

    groups: dict[str, list] = {}
    for name, points in terminals.items():
        nodes = _electrode(index, mask, origin, cell_mm, points,
                           electrode_radius_mm)
        if nodes:
            groups[name] = nodes
    if source not in groups or len(groups) < 2:
        return PlaneSolution(layer=layer, net=net, sheet_ohm_sq=sheet,
                             cells=n, source=source)

    # Two terminals close together can claim the same cells. Assign each cell
    # to the first claimant and drop any group left with none — otherwise its
    # reduced node has an all-zero row and the factorisation is exactly
    # singular. (Found on the second board this ran against, and again on the
    # third for this reason rather than the disconnected-island one.)
    names = [source] + [k for k in groups if k != source]
    claimed: set[int] = set()
    exclusive: dict[str, list] = {}
    for name in names:
        mine = [k for k in groups[name] if k not in claimed]
        if mine:
            exclusive[name] = mine
            claimed.update(mine)
    names = [k for k in names if k in exclusive]
    if source not in exclusive or len(names) < 2:
        return PlaneSolution(layer=layer, net=net, sheet_ohm_sq=sheet,
                             cells=n, source=source)
    groups = exclusive

    owner = np.arange(n)
    for gi, name in enumerate(names):
        owner[groups[name]] = -(gi + 1)
    keep = owner >= 0
    reduced = -np.ones(n, dtype=np.int64)
    m = len(names)
    reduced[keep] = m + np.arange(int(keep.sum()))
    for gi in range(m):
        reduced[owner == -(gi + 1)] = gi
    projection = sp.csr_matrix((np.ones(n), (np.arange(n), reduced)),
                               shape=(n, m + int(keep.sum())))
    reduced_matrix = (projection.T @ matrix @ projection).tolil()

    # gauge: hold the source at 0 V, then inject 1 A at each other terminal in
    # turn. V[k] is then exactly R(source, k).
    reduced_matrix.rows[0] = [0]
    reduced_matrix.data[0] = [1.0]
    factorised = spla.factorized(reduced_matrix.tocsc())

    resistance = {}
    for gi, name in enumerate(names[1:], start=1):
        rhs = np.zeros(reduced_matrix.shape[0])
        rhs[gi] = 1.0
        rhs[0] = 0.0
        potential = factorised(rhs)
        resistance[name] = float(potential[gi] - potential[0])
    return PlaneSolution(layer=layer, net=net, sheet_ohm_sq=sheet, cells=n,
                         resistance_ohm=resistance, source=source)


# ---------------------------------------------------------------------------
# Putting it on a board
# ---------------------------------------------------------------------------

@dataclass
class Feed:
    """One terminal's path from its net's source, and how it compares."""
    net: str
    terminal: str
    method: str                     # "plane_resistance" | "bottleneck"
    resistance_ohm: float | None = None
    bottleneck_mm: float | None = None
    capacity_a: float | None = None
    vias: int = 0
    via_capacity_a: float | None = None
    layer: str = ""
    location: tuple | None = None


def _net_name(board: Board, code) -> str | None:
    try:
        return board.net(code).name
    except KeyError:
        return None


def _terminals(board: Board, net: str) -> dict:
    """Pads on this net that are a device or connector terminal, grouped by part.

    A rail reaches dozens of decoupling capacitors and they are not what
    carries current, so parts whose every pad on this net is a two-pad passive
    are excluded. What is left is the sources and the sinks.
    """
    by_ref: dict[str, list] = {}
    for pad in board.pads:
        if _net_name(board, pad.net) != net:
            continue
        by_ref.setdefault(pad.ref, []).append(pad)
    pad_counts: dict[str, int] = {}
    for pad in board.pads:
        pad_counts[pad.ref] = pad_counts.get(pad.ref, 0) + 1
    return {ref: pads for ref, pads in by_ref.items()
            if pad_counts.get(ref, 0) > 2 or ref.startswith(("J", "Q", "FH"))}


def worst_feed(board: Board, net: str, copper_oz_by_layer: dict
               ) -> tuple[list[Feed], dict]:
    """Feed resistance or bottleneck from a net's source to each terminal.

    The source is the terminal with the largest total pad area — a connector
    shell, a FET tab, a regulator output. That is a heuristic and it is stated
    on the finding.

    **It picks an end that cannot always be measured from**, and the correction
    matters more than the heuristic. Pad area answers "which end is the
    source"; it says nothing about whether that end has a point on the sheet
    being solved, and the two were being decided by one number. On the plane
    path the source is now re-picked from the terminals that **do** have one.

    The sheet is chosen first, by `_solve_region`, and that ordering is the
    whole of it: a pour is several disjoint polygons, only terminals on the
    same one can be compared, and the region that carries the most terminals is
    the one to solve. Choosing the *largest* region instead — and then
    discovering the source was not on it — returned **zero** feeds for `GND` on
    the 6-layer V3 board, a net with 49 terminals of which 40 reach the pour.
    Region-first takes that net from 0 feeds to 30.
    """
    terminals = _terminals(board, net)
    notes = {"net": net, "terminals": len(terminals),
             "method": None, "source": None}
    if len(terminals) < MIN_TERMINALS:
        notes["skipped"] = (
            f"fewer than {MIN_TERMINALS} terminals, so at most "
            f"{max(len(terminals) - 1, 0)} feed(s) exist and no feed can be an "
            f"outlier against a median of that few")
        return [], notes

    areas = {ref: sum(p.copper.area for p in pads)
             for ref, pads in terminals.items()}
    source = max(areas, key=areas.get)
    notes["source"] = source

    vias = [v for v in board.vias if _net_name(board, v.net) == net]

    # the layer carrying the most of this net's poured copper is the bus
    poured = {layer: board.zone_fill(layer, net).area
              for layer in board.copper_layers}
    layer = max(poured, key=poured.get) if poured else None
    if layer is None or poured[layer] < 20.0:
        notes["method"] = "bottleneck"
        feeds = _tracked_feeds(board, net, terminals, source,
                               copper_oz_by_layer, vias)
        if not feeds:
            # `_tracked_feeds` drops a terminal whose `bottleneck_mm` erosion
            # search finds no channel to the source on any layer, and returns
            # an empty list when that happens to every one of them. The caller
            # had already set `method`, so the net arrived at the audit with a
            # method and no reason and read as an unexplained silence (§54.2).
            notes["skipped"] = (
                f"no channel could be eroded from {source} to any other "
                f"terminal on any layer, so no bottleneck width exists to "
                f"compare — the terminals are not joined by continuous "
                f"copper on a single layer")
        return feeds, notes

    notes["method"] = "plane_resistance"
    notes["layer"] = layer
    copper = board.copper_on_layer(layer, net)
    pieces = _pieces(copper)
    if not pieces:
        notes["skipped"] = "no poured copper to solve on"
        return [], notes

    contacts = {}
    for ref, pads in terminals.items():
        region = pads[0].copper
        for pad in pads[1:]:
            region = region.union(pad.copper)
        near = region.buffer(VIA_SEARCH_MM)
        points = [v.pos for v in vias if near.contains(Point(*v.pos))]
        # a through-hole terminal reaches the plane without a via
        if not points and any(p.is_plated_hole for p in pads):
            points = [p.pos for p in pads]
        if points:
            contacts[ref] = points
    notes["contacts"] = len(contacts)
    if not contacts:
        notes["skipped"] = (
            f"no terminal on this net has a via within {VIA_SEARCH_MM:.1f} mm "
            f"of its own pads and none is through-hole, so there is no point "
            f"on the {layer} pour to measure from")
        return [], notes

    # The sheet comes before the source. `plane_resistance` solves one region
    # of copper, so which region is being solved decides which terminals can
    # be compared at all — and picking the largest region rather than the
    # best-populated one is what silently zeroed `GND` (see `_solve_region`).
    sheet, on_region = _solve_region(pieces, contacts)
    notes["regions"] = len(pieces)
    if not on_region:
        notes["skipped"] = (
            f"the {len(contacts)} terminal(s) that reach the {layer} pour "
            f"have no contact point inside any of its {len(pieces)} copper "
            f"region(s)")
        return [], notes
    notes["on_region"] = len(on_region)
    if len(on_region) < len(contacts):
        notes["off_region"] = len(contacts) - len(on_region)

    # Two different failures wore one sentence, and they call for different
    # things: a source with no via of its own is a routing observation about
    # one part, and a net whose terminals mostly do not reach the plane is a
    # statement about the net. Measured over the five boards, the three
    # declines here split 1 and 2 (§54.2's "3 vias could not be located").
    if source not in on_region:
        # Re-pick, rather than abandon. The largest-area terminal answers a
        # different question from "which terminal can we measure from", and
        # deciding both with one number threw away every net whose biggest pad
        # happens to be an SMD part with no via of its own — or, once the
        # region is chosen properly, whose biggest pad sits on a different
        # piece of the pour. The replacement is still the largest-area
        # terminal, just the largest of those on the region being solved, so
        # the heuristic is unchanged and only its candidates are.
        reachable = {ref: area for ref, area in areas.items() if ref in on_region}
        notes["source_rejected"] = source
        source = max(reachable, key=reachable.get)
        notes["source"] = source
        notes["source_fallback"] = (
            f"the largest terminal {notes['source_rejected']} has no contact "
            f"point on the {layer} region being solved — either no via within "
            f"{VIA_SEARCH_MM:.1f} mm of its own pads, or its vias land on a "
            f"different region of the same pour; {source} is the largest that "
            f"does")
    if len(on_region) < MIN_TERMINALS - 1:
        if len(on_region) < len(contacts):
            notes["skipped"] = (
                f"{len(contacts)} of {len(terminals)} terminals reach the "
                f"{layer} pour, but only {len(on_region)} of them sit on the "
                f"one region of it that carries the most ({len(pieces)} "
                f"disjoint region(s) on this layer). A two-terminal "
                f"resistance is only comparable between terminals on the same "
                f"sheet, and {MIN_TERMINALS - 1} are needed to compare one "
                f"against the others")
        else:
            notes["skipped"] = (
                f"only {len(contacts)} of {len(terminals)} terminals reach "
                f"the {layer} pour through a via within {VIA_SEARCH_MM:.1f} "
                f"mm of their pads, and {MIN_TERMINALS - 1} are needed to "
                f"compare one against the others")
        return [], notes

    solution = plane_resistance(sheet, copper_oz_by_layer.get(layer, 1.0),
                                on_region, source, layer=layer, net=net)
    feeds = []
    for ref, ohms in solution.resistance_ohm.items():
        pads = terminals[ref]
        local = [v for v in vias
                 if pads[0].copper.buffer(2.5).contains(Point(*v.pos))]
        feeds.append(Feed(
            net=net, terminal=ref, method="plane_resistance",
            # the vias that reach the region this feed was measured on, not
            # every via near the pad — a terminal straddling two regions of
            # the same pour does not carry current into this one through the
            # ones that land on the other
            resistance_ohm=ohms, vias=len(on_region.get(ref, [])),
            via_capacity_a=(sum(via_capacity_a(v.drill_mm) for v in local)
                            if local else None),
            layer=layer, location=tuple(pads[0].pos)))
    notes["sheet_ohm_sq"] = solution.sheet_ohm_sq
    notes["cells"] = solution.cells
    notes["solved"] = len(feeds)
    # A terminal that reached the pour can still produce no feed, in two
    # distinct ways, and they were reported as one sentence that named neither
    # correctly. **Off the region**: its contact points sit on a different
    # polygon of the same pour, which no single-sheet solve can compare against
    # this one — that is a fact about the copper and it is now decided, and
    # counted, before the solve. **Lost inside the solve**: `plane_resistance`
    # returns a *short* `resistance_ohm` rather than raising when an electrode
    # falls on a grid island of its own or sits close enough to a neighbour to
    # claim the same cells. Splitting them matters because they want different
    # fixes — the first is geometry, the second is grid resolution.
    #
    # Until this said anything at all, a net with ten terminals on the plane
    # and two solved feeds was declined by the caller as "only 2 produced a
    # comparable number", which blames the comparison floor for a loss that
    # happened upstream of it.
    off_region = len(contacts) - len(on_region)
    lost = max(len(on_region) - 1 - len(feeds), 0)
    if lost:
        notes["electrodes_lost"] = lost
    if lost or off_region:
        why = []
        if off_region:
            why.append(
                f"{off_region} sit on a different copper region of the same "
                f"pour ({notes['regions']} region(s) on this layer), which a "
                f"single-sheet solve cannot compare against this one")
        if lost:
            why.append(
                f"{lost} electrode(s) were lost inside the solve "
                f"({solution.cells} cells at {PLANE_CELL_MM} mm), sitting on "
                f"a grid island of their own or close enough to a neighbour "
                f"to claim the same cells")
        notes["skipped"] = (
            f"{len(contacts)} of {len(terminals)} terminals reach the "
            f"{layer} pour and {len(feeds)} feed(s) were solved from "
            f"{source}: " + "; ".join(why))
    return feeds, notes


def _tracked_feeds(board, net, terminals, source, copper_oz_by_layer, vias):
    """Erosion bottleneck per layer, for a net routed as tracks rather than poured."""
    source_pads = terminals[source]
    a = Point(*source_pads[0].pos)
    feeds = []
    for ref, pads in terminals.items():
        if ref == source:
            continue
        b = Point(*pads[0].pos)
        best = None
        for layer in board.copper_layers:
            copper = board.copper_on_layer(layer, net)
            if copper.is_empty:
                continue
            width = bottleneck_mm(copper, a, b)
            if width is None:
                continue
            oz = copper_oz_by_layer.get(layer, 1.0)
            capacity = physics.current_capacity_a(
                width, physics.copper_thickness_mm(oz), RISE_C,
                internal=layer not in (board.copper_layers[0],
                                       board.copper_layers[-1]))
            if best is None or capacity < best[2]:
                best = (layer, width, capacity)
        if best is None:
            continue
        local = [v for v in vias
                 if pads[0].copper.buffer(2.5).contains(Point(*v.pos))]
        feeds.append(Feed(
            net=net, terminal=ref, method="bottleneck",
            bottleneck_mm=best[1], capacity_a=best[2], layer=best[0],
            vias=len(local),
            via_capacity_a=(sum(via_capacity_a(v.drill_mm) for v in local)
                            if local else None),
            location=tuple(pads[0].pos)))
    return feeds


@dataclass
class WidthProfile:
    """A net's routed widths and the current each can carry."""
    net: str
    total_mm: float
    #: width -> routed length, only stretches worth counting
    by_width: dict = field(default_factory=dict)
    #: the widths that produced the weakest and strongest capacity. NOT the
    #: narrowest and widest: a 1.0 mm stretch on an inner 0.5 oz layer carries
    #: less than a 0.2 mm one on an outer 1 oz layer, and taking the extremes
    #: by width reported a "ratio" below 1 on two real nets.
    weakest_mm: float = 0.0
    strongest_mm: float = 0.0
    weakest_capacity_a: float = 0.0
    strongest_capacity_a: float = 0.0
    weakest_layer: str = ""

    @property
    def capacity_ratio(self) -> float:
        if self.weakest_capacity_a <= 0:
            return 1.0
        return self.strongest_capacity_a / self.weakest_capacity_a


#: A stretch shorter than this fraction of the net is a pad entry or a via
#: escape, not a section of the conductor. Without it every net reports a
#: ratio, because the last 0.3 mm into a 0402 is always thinner.
SIGNIFICANT_LENGTH_FRACTION = 0.05


def width_profile(board: Board, net: str, copper_oz_by_layer: dict,
                  rise_c: float = RISE_C) -> WidthProfile | None:
    """How much the current capacity varies along one net's routed copper.

    The brief, 2026-08-03: *"it can flag an issue if the current capacity
    varies significantly across the trace."* A net routed 0.8 mm for most of
    its length and 0.2 mm for the rest carries what the 0.2 mm carries, and
    the wide part is telling you what the designer thought it needed.

    Distinct from `conductor_neck`, which only fires when the narrow stretch
    *strands pads* — a net that necks in the middle of a run is invisible to
    that check and visible to this one.
    """
    lengths: dict[float, float] = {}
    layers: dict[float, set] = {}
    for track in board.tracks:
        try:
            name = board.net(track.net).name
        except KeyError:
            continue
        if name != net:
            continue
        key = round(track.width_mm, 4)
        lengths[key] = lengths.get(key, 0.0) + track.length_mm
        layers.setdefault(key, set()).add(track.layer)

    total = sum(lengths.values())
    if total <= 0:
        return None
    significant = {w: l for w, l in lengths.items()
                   if l >= SIGNIFICANT_LENGTH_FRACTION * total}
    if len(significant) < 2:
        return None

    outer_layers = {board.copper_layers[0], board.copper_layers[-1]}

    def capacity(width):
        """The weakest capacity this width has anywhere it is routed.

        The same width exists on several layers and an inner 0.5 oz layer
        carries far less than an outer 1 oz one. Picking a layer arbitrarily
        made a 0.8 mm stretch read the same as a 0.4 mm one; the limit is the
        worst of them.
        """
        options = []
        for layer in sorted(layers[width]):
            on_outer = layer in outer_layers
            oz = copper_oz_by_layer.get(layer, 1.0 if on_outer else 0.5)
            options.append((physics.current_capacity_a(
                width, physics.copper_thickness_mm(oz), rise_c,
                internal=not on_outer), layer))
        return min(options)

    # (capacity, layer) per width, so the reported layer is the one that
    # actually produced the limit rather than whichever sorts first
    per_width = {w: capacity(w) for w in significant}
    weakest = min(per_width, key=lambda w: per_width[w][0])
    strongest = max(per_width, key=lambda w: per_width[w][0])
    return WidthProfile(
        net=net, total_mm=round(total, 2),
        by_width={w: round(l, 2) for w, l in sorted(significant.items())},
        weakest_mm=weakest, strongest_mm=strongest,
        weakest_capacity_a=per_width[weakest][0],
        strongest_capacity_a=per_width[strongest][0],
        weakest_layer=per_width[weakest][1])


def coverage(board: Board, power_nets: set[str]) -> dict:
    """What this module had to look at. Print it before believing a zero."""
    examined, skipped = [], {}
    for net in sorted(power_nets):
        terminals = _terminals(board, net)
        if len(terminals) < MIN_TERMINALS:
            skipped[net] = len(terminals)
        else:
            examined.append(net)
    return {
        "power_nets": len(power_nets),
        "nets_examined": examined,
        "nets_with_too_few_terminals": skipped,
        "via_plating_um": VIA_PLATING_UM,
        "rise_c": RISE_C,
    }


# ---------------------------------------------------------------------------
# Absolute ampacity — what the copper carries, whether or not it varies
# ---------------------------------------------------------------------------

@dataclass
class Ampacity:
    """What one net's copper can actually carry, and where it is weakest.

    `width_profile` answers *"does this net's capacity vary along it"* and
    returns `None` when it does not — a net of one uniform width has fewer
    than two significant widths and never reaches the calculation at all. That
    left a real hole, reported from a real board on 2026-08-05:

        Four firing-channel nets are 0.600 mm along their entire length.
        Uniform width gives a ratio of 1.0 […] The net was silent from both,
        for opposite reasons. So the real gap is that `current_capacity` cannot
        see a uniformly undersized conductor at all. […] A trace that is 3x too
        thin along 100% of its length is indistinguishable from a perfect one.

    This is the absolute number, computed for **every** power net regardless of
    whether anything about it varies. the ruling, 2026-08-06: *"for power nets,
    or nets which the user specifically flagged for current, the system should
    always flag it"*.

    `via_capacity_a` is the barrels in parallel; `weakest_a` is the tracks in
    series. They are reported side by side and deliberately **not** combined:
    which vias are in series with which stretch is a topology question this
    does not answer, and inventing a single number would hide that.
    """
    net: str
    #: the weakest routed stretch, by CAPACITY not by width — an inner-layer
    #: 1.0 mm run on 0.5 oz carries less than an outer 0.2 mm run on 1 oz
    weakest_a: float | None
    weakest_mm: float | None
    weakest_layer: str
    #: The weakest stretch **including** the short ones the significance floor
    #: drops. Physically every series stretch carries the whole current, so a
    #: 0.3 mm pad entry really is the limit — but reporting that as the
    #: headline would make every net on every board read as 1 A, which is why
    #: the floor exists at all. Both are printed: the headline is the one a
    #: designer can act on, this one is the one that leaves nowhere to hide.
    rise_c: float
    weakest_any_a: float | None = None
    weakest_any_mm: float | None = None
    weakest_any_layer: str = ""
    weakest_any_length_mm: float = 0.0
    #: every (layer, width) -> (length_mm, amps), so the front end can show
    #: the whole picture without recomputing anything
    by_width: dict = field(default_factory=dict)
    vias: int = 0
    via_capacity_a: float | None = None
    poured_mm2: float = 0.0

    @property
    def measured(self) -> bool:
        """Is there any ampacity number at all for this net?"""
        return self.weakest_a is not None or self.via_capacity_a is not None


def ampacity(board: Board, net: str, copper_oz_by_layer: dict,
             rise_c: float = RISE_C) -> Ampacity:
    """The absolute current this net's copper carries at `rise_c`.

    No thresholds, no ratios, no early return. Every power net gets one of
    these, because a number that exists and is not printed is the failure the
    field report named.
    """
    by_width: dict = {}
    lengths: dict = {}
    outer = (board.copper_layers[0], board.copper_layers[-1])
    try:
        tracks = board.tracks_on_net(board.net(net).code)
    except KeyError:
        # A name in `power_nets` that this board does not have. Real when a
        # caller passes a net list from elsewhere, and it must not crash the
        # check — an empty result says "no copper", which is the truth.
        tracks = []
    for track in tracks:
        key = (track.layer, round(track.width_mm, 4))
        lengths[key] = lengths.get(key, 0.0) + track.length_mm

    weakest_a = None
    weakest_key = None
    weakest_any = None
    weakest_any_key = None
    for key, length in lengths.items():
        layer, width = key
        oz = copper_oz_by_layer.get(layer, 1.0 if layer in outer else 0.5)
        amps = physics.current_capacity_a(
            width, physics.copper_thickness_mm(oz), rise_c,
            internal=layer not in outer)
        by_width[key] = (round(length, 3), round(amps, 3))
        if weakest_any is None or amps < weakest_any:
            weakest_any, weakest_any_key = amps, key
        # A stretch shorter than a pad entry is not what the net "carries" —
        # the same floor `width_profile` uses, for the same reason.
        if length < SIGNIFICANT_LENGTH_FRACTION * max(sum(lengths.values()), 1e-9):
            continue
        if weakest_a is None or amps < weakest_a:
            weakest_a, weakest_key = amps, key

    vias = [v for v in board.vias if _net_name(board, v.net) == net]
    barrels = (sum(via_capacity_a(v.drill_mm) for v in vias) if vias else None)
    poured = sum(board.zone_fill(layer, net).area
                 for layer in board.copper_layers)

    return Ampacity(
        net=net,
        weakest_a=round(weakest_a, 3) if weakest_a is not None else None,
        weakest_mm=weakest_key[1] if weakest_key else None,
        weakest_layer=weakest_key[0] if weakest_key else "",
        weakest_any_a=round(weakest_any, 3) if weakest_any is not None else None,
        weakest_any_mm=weakest_any_key[1] if weakest_any_key else None,
        weakest_any_layer=weakest_any_key[0] if weakest_any_key else "",
        weakest_any_length_mm=(round(lengths[weakest_any_key], 3)
                               if weakest_any_key else 0.0),
        rise_c=rise_c, by_width=by_width, vias=len(vias),
        via_capacity_a=round(barrels, 2) if barrels is not None else None,
        poured_mm2=round(poured, 2))
