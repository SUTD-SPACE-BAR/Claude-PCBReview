"""Net topology: what a net is actually made of, and how it hangs together.

Connectivity here is derived from *geometry* — two pieces of copper on the same
net and layer are joined when their polygons intersect — rather than from
KiCad's netlist. That is the point: KiCad tells us which pads are *supposed* to
be connected, and this module works out whether copper actually does it.

The agreement between the two is the correctness check. Where KiCad reports a
net fully routed, we must find exactly one connected component; anything else
is a bug in our geometry, not a finding about the board.

Everything downstream (return paths, length matching, stub detection) needs the
routed graph, not just a bag of segments, so this module comes first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.strtree import STRtree

from pcbreview import physics
from pcbreview.model import Board, Net, Pad, Track, Via

# Copper this close is treated as touching. KiCad stores coordinates in
# nanometres, so genuinely-joined items overlap far more than this; the margin
# only absorbs floating-point noise from arc tessellation and buffering.
TOUCH_TOLERANCE_MM = 0.0005


class _UnionFind:
    def __init__(self, size: int):
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        if self._rank[root_a] < self._rank[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        if self._rank[root_a] == self._rank[root_b]:
            self._rank[root_a] += 1

    def groups(self) -> list[list[int]]:
        buckets: dict[int, list[int]] = {}
        for item in range(len(self._parent)):
            buckets.setdefault(self.find(item), []).append(item)
        return list(buckets.values())


@dataclass
class CopperItem:
    """One piece of copper on one layer, as connectivity sees it."""
    kind: str                       # track | via | pad | zone
    layer: str
    geometry: Polygon | MultiPolygon
    source: Track | Via | Pad | object

    @property
    def designator(self) -> str:
        source = self.source
        if isinstance(source, Pad):
            return source.designator
        if isinstance(source, Track):
            return f"track#{source.id}"
        if isinstance(source, Via):
            return f"via#{source.id}"
        return f"zone#{getattr(source, 'id', '?')}"


@dataclass
class Island:
    """A set of copper items that are physically joined."""
    items: list[CopperItem]

    @cached_property
    def pads(self) -> list[Pad]:
        return [i.source for i in self.items if i.kind == "pad"]

    @cached_property
    def layers(self) -> set[str]:
        return {i.layer for i in self.items}

    @cached_property
    def routed_length_mm(self) -> float:
        seen: set[int] = set()
        total = 0.0
        for item in self.items:
            if item.kind == "track" and id(item.source) not in seen:
                seen.add(id(item.source))
                total += item.source.length_mm
        return total

    @property
    def has_zone(self) -> bool:
        return any(i.kind == "zone" for i in self.items)


@dataclass
class NetTopology:
    """A net's copper, grouped into physically-connected islands."""
    net: Net
    islands: list[Island]
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)

    @property
    def is_connected(self) -> bool:
        """True when all this net's copper forms a single island."""
        return len(self.islands) <= 1

    @property
    def island_count(self) -> int:
        return len(self.islands)

    @cached_property
    def routed_length_mm(self) -> float:
        """Total routed copper length. Includes every branch, counted once."""
        return sum(t.length_mm for t in self.tracks)

    @cached_property
    def layers_used(self) -> list[str]:
        return sorted({t.layer for t in self.tracks})

    @cached_property
    def length_by_layer(self) -> dict[str, float]:
        by_layer: dict[str, float] = {}
        for track in self.tracks:
            by_layer[track.layer] = by_layer.get(track.layer, 0.0) + track.length_mm
        return dict(sorted(by_layer.items(), key=lambda kv: -kv[1]))

    @cached_property
    def min_width_mm(self) -> float | None:
        """Width of the narrowest routed point — the current-capacity limiter."""
        widths = [t.width_mm for t in self.tracks]
        return min(widths) if widths else None

    @cached_property
    def max_width_mm(self) -> float | None:
        widths = [t.width_mm for t in self.tracks]
        return max(widths) if widths else None

    @cached_property
    def width_profile(self) -> list[tuple[float, float]]:
        """(width_mm, length_mm) sorted by width, so necks are visible."""
        by_width: dict[float, float] = {}
        for track in self.tracks:
            by_width[track.width_mm] = by_width.get(track.width_mm, 0.0) + track.length_mm
        return sorted(by_width.items())

    @property
    def via_count(self) -> int:
        return len(self.vias)

    def unconnected_pads(self) -> list[Pad]:
        """Pads not attached to the largest island — i.e. genuinely stranded."""
        if len(self.islands) <= 1:
            return []
        largest = max(self.islands, key=lambda i: (len(i.pads), i.routed_length_mm))
        return [p for island in self.islands if island is not largest
                for p in island.pads]

    def summary(self) -> dict:
        return {
            "net": self.net.name,
            "islands": self.island_count,
            "connected": self.is_connected,
            "length_mm": round(self.routed_length_mm, 4),
            "vias": self.via_count,
            "pads": len(self.pads),
            "layers": self.layers_used,
            "min_width_mm": self.min_width_mm,
        }


def _collect_items(board: Board, net: Net, include_zones: bool = True,
                   exclude_tracks: set[int] | None = None) -> list[CopperItem]:
    items: list[CopperItem] = []

    for track in board.tracks_on_net(net.code):
        if exclude_tracks and track.id in exclude_tracks:
            continue
        items.append(CopperItem("track", track.layer, track.copper, track))

    for via in board.vias_on_net(net.code):
        for layer in via.layers:
            if layer in board.copper_layers:
                items.append(CopperItem("via", layer, via.pad_on(layer), via))

    for pad in board.pads_on_net(net.code):
        copper = pad.copper
        for layer in pad.copper_layers:
            if layer in board.copper_layers:
                items.append(CopperItem("pad", layer, copper, pad))

    if include_zones:
        for zone in board.zones_on_net(net.code):
            for layer in zone.layers:
                if layer not in board.copper_layers:
                    continue
                fill = zone.fill_on(layer)
                if fill.is_empty:
                    continue
                # Each island is separate copper. Adding the fill as one item
                # would bridge islands that never touch: a via landing on one
                # island and a track on another are NOT connected, and treating
                # them as connected hides real breaks.
                for island in fill.geoms:
                    items.append(CopperItem("zone", layer, island, zone))

    return items


def build_topology(board: Board, net: int | str,
                   include_zones: bool = True,
                   tolerance: float = TOUCH_TOLERANCE_MM,
                   exclude_tracks: set[int] | None = None) -> NetTopology:
    """Group a net's copper into physically-connected islands.

    Items join when their polygons intersect on the same layer; a via joins its
    own lands across every layer it spans, which is what carries connectivity
    vertically.

    ``exclude_tracks`` omits track ids from the graph, which turns this into a
    cut test: rebuild a net without a suspect stretch and see whether the pads
    it served fall off. That is how a narrow stretch is shown to be a *series*
    bottleneck rather than one of several parallel paths — see
    `audit.check_conductor_necks`.
    """
    net_obj = board.net(net)
    items = _collect_items(board, net_obj, include_zones, exclude_tracks)

    union = _UnionFind(len(items))

    # Vertical: a via's lands are the same conductor on every layer it spans.
    via_lands: dict[int, list[int]] = {}
    for index, item in enumerate(items):
        if item.kind == "via":
            via_lands.setdefault(id(item.source), []).append(index)
    for indices in via_lands.values():
        for other in indices[1:]:
            union.union(indices[0], other)

    # A plated through-hole pad likewise spans its layers.
    pad_layers: dict[int, list[int]] = {}
    for index, item in enumerate(items):
        if item.kind == "pad":
            pad_layers.setdefault(id(item.source), []).append(index)
    for indices in pad_layers.values():
        for other in indices[1:]:
            union.union(indices[0], other)

    # Lateral: intersecting copper on the same layer, indexed per layer so the
    # trees stay small.
    by_layer: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        by_layer.setdefault(item.layer, []).append(index)

    for indices in by_layer.values():
        if len(indices) < 2:
            continue
        geoms = [items[i].geometry for i in indices]
        tree = STRtree(geoms)
        for position, index in enumerate(indices):
            geometry = geoms[position]
            probe = geometry.buffer(tolerance) if tolerance else geometry
            for hit in tree.query(probe):
                if hit <= position:
                    continue
                if probe.intersects(geoms[hit]):
                    union.union(index, indices[hit])

    islands = [Island([items[i] for i in group]) for group in union.groups()]
    islands.sort(key=lambda i: (-len(i.pads), -i.routed_length_mm))

    return NetTopology(
        net=net_obj,
        islands=islands,
        tracks=board.tracks_on_net(net_obj.code),
        vias=board.vias_on_net(net_obj.code),
        pads=board.pads_on_net(net_obj.code),
    )


def build_all(board: Board, include_zones: bool = True,
              skip_empty: bool = True) -> dict[str, NetTopology]:
    """Topology for every net on the board, keyed by net name."""
    result: dict[str, NetTopology] = {}
    for net in board.nets.values():
        if skip_empty and not (board.tracks_on_net(net.code)
                               or board.pads_on_net(net.code)):
            continue
        result[net.name] = build_topology(board, net.code, include_zones)
    return result


# ---------------------------------------------------------------------------
# Traces: a net's routed copper as a graph of pin-to-pin runs
# ---------------------------------------------------------------------------
#
# An island answers *"is this net joined up"*. A trace answers a different and
# sharper question, and it is the user's: **"how much can THIS run carry"**.
#
#     "when we hover over a trace, that trace is only part of a net. The net
#      could branch from anywhere to anywhere, but between two pins of two
#      components, that is a single trace, and we should check the current
#      capacity."
#
# `current.ampacity` is net-level and position-blind: it buckets every track on
# the net by `(layer, width)` and reports the narrowest bucket carrying a
# significant share of the length. A net with a 2 mm trunk from a regulator to
# a bulk cap and a 0.2 mm branch to an LED therefore reports the LED branch's
# capacity as the net's — correct about the net, and wrong about the trunk that
# somebody hovering the trunk is asking about.
#
# What a trace is, precisely
# --------------------------
# The net's routed copper is a graph. Nodes are **pads**, **vias**, and
# **junctions** — points where three or more conductors meet. Every maximal run
# between two nodes is one **edge**, and that edge is a trace.
#
# **The edge, not a pin-to-pin path**, and this is the one ambiguity worth
# stating rather than discovering. A regulator output fanning out to five loads
# has a trunk that lies on all five pin-to-pin paths, so "the trace" for a
# segment on the trunk has five equally true answers. An edge has one. Splitting
# at junctions is exactly what makes the question well-posed, and the trace can
# then name the pads at its own two ends.
#
# Three consequences that are deliberate
# --------------------------------------
# * **A via at a degree-2 node is a pass-through**, so a run that changes layer
#   is still one trace. `current.ampacity`'s docstring says of vias and tracks
#   that *"which vias are in series with which stretch is a topology question
#   this does not answer"*. This graph answers it, so a trace reports the
#   weakest barrel *in series along its own length* — separately from the
#   copper, never silently combined into one number.
# * **No significance floor.** `ampacity()` drops stretches shorter than
#   `SIGNIFICANT_LENGTH_FRACTION` of the net, because otherwise every net on
#   every board reads as its pad entry. Inside one edge that reasoning does not
#   apply: every part of a series run carries the whole current, so the
#   narrowest point is the limit, full stop.
# * **Pours are not traces.** `current.bottleneck_mm` is documented as "not
#   valid on a poured plane" and `plane_resistance` exists for that case, so
#   zones are not collected here at all. A `GND` pour produces no trace, which
#   is the honest answer rather than a made-up one.
#
# Nothing in here is a threshold or a severity. It is a decomposition and a
# measurement; `audit.py` owns what counts as bad.

#: Two endpoints closer than this are the same node. KiCad stores coordinates
#: in nanometres and a router joins tracks at *exactly* equal endpoints, so
#: this only has to absorb the arithmetic in `LineString.interpolate` for the
#: cut points computed below — it is not a "near enough" connectivity rule, and
#: widening it would start merging genuinely distinct nodes.
NODE_TOLERANCE_MM = 0.001

#: A cut this close to a track's own end is that end, not an interior junction.
#: Without it a via landing on a track's endpoint would split off a zero-length
#: strand and inflate the node degree, turning a plain corner into a junction.
CUT_CLEARANCE_MM = 0.005


@dataclass(frozen=True)
class TraceEnd:
    """What a trace terminates on. `name` is what the inspector prints."""
    kind: str                       # pad | via | junction | open
    name: str
    pos: tuple[float, float]
    layer: str


@dataclass(frozen=True)
class Strand:
    """A portion of one `Track`, between two parameters along its centreline.

    A track is split when something lands in the middle of it — a via dropped
    onto an existing run, a branch tee'd off it — because the graph needs a
    node there. Width and layer are constant along a `Track` by construction,
    so splitting one changes no physics: a strand's capacity is its track's.
    """
    track: Track
    t0: float
    t1: float

    @property
    def length_mm(self) -> float:
        return self.track.length_mm * (self.t1 - self.t0)

    @property
    def is_whole(self) -> bool:
        return self.t0 <= 0.0 and self.t1 >= 1.0


@dataclass
class Trace:
    """One maximal run of routed copper between two nodes of a net's graph."""
    id: int
    net: str
    ends: tuple[TraceEnd, TraceEnd]
    strands: list[Strand]
    #: vias *strictly inside* the run, i.e. in series with all of it. A via at
    #: an end is shared with the traces on the other side of that node, so
    #: attributing its barrel to this one would double-count it.
    via_ids: list[int]
    layers: list[str]
    length_mm: float
    narrowest_mm: float
    narrowest_layer: str
    rise_c: float
    #: IPC-2152 for the weakest strand. `None` only if the trace has no copper,
    #: which cannot happen — kept typed that way to match `Ampacity`.
    capacity_a: float | None = None
    #: weakest interior barrel, or `None` when the run has no via in series
    via_capacity_a: float | None = None

    @property
    def limit_a(self) -> float | None:
        """What this run actually carries: copper and barrels in series.

        Reported *alongside* the two rather than instead of them, because a
        trace limited by a via and one limited by a neck want different fixes.
        """
        candidates = [c for c in (self.capacity_a, self.via_capacity_a)
                      if c is not None]
        return min(candidates) if candidates else None

    @property
    def name(self) -> str:
        return f"{self.ends[0].name} → {self.ends[1].name}"

    @property
    def track_ids(self) -> list[int]:
        return sorted({s.track.id for s in self.strands})

    def summary(self) -> dict:
        return {
            "id": self.id, "net": self.net, "name": self.name,
            "layers": self.layers,
            "length_mm": round(self.length_mm, 3),
            "narrowest_mm": self.narrowest_mm,
            "narrowest_layer": self.narrowest_layer,
            "capacity_a": self.capacity_a,
            "via_capacity_a": self.via_capacity_a,
            "limit_a": self.limit_a,
            "rise_c": self.rise_c,
            "vias_in_series": len(self.via_ids),
        }


class _NodeIndex:
    """Points to node ids, with a tolerance, plus union-find over the ids.

    A plain dict keyed on a rounded coordinate is not enough: two points either
    side of a rounding boundary are microns apart and would become two nodes.
    This buckets on a grid and searches the eight neighbouring buckets too.
    """

    def __init__(self, tolerance: float = NODE_TOLERANCE_MM):
        self._tolerance = tolerance
        self._buckets: dict[tuple, list[int]] = {}
        self._points: list[tuple[str, float, float]] = []
        self._parent: list[int] = []

    def __len__(self) -> int:
        return len(self._points)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        return (int(x // self._tolerance), int(y // self._tolerance))

    def node(self, layer: str, x: float, y: float) -> int:
        cx, cy = self._cell(x, y)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for index in self._buckets.get((layer, cx + dx, cy + dy), ()):
                    _, px, py = self._points[index]
                    if abs(px - x) <= self._tolerance and abs(py - y) <= self._tolerance:
                        return index
        index = len(self._points)
        self._points.append((layer, x, y))
        self._buckets.setdefault((layer, cx, cy), []).append(index)
        self._parent.append(index)
        return index

    # The union-find is kept here rather than built afterwards so that pads and
    # vias can merge nodes as they are discovered.
    def find(self, item: int) -> int:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def point(self, index: int) -> tuple[str, float, float]:
        return self._points[index]


def _cut_parameters(track: Track, points) -> list[float]:
    """Where `track` must be split, as parameters in (0, 1).

    A point cuts a track when it lies within the copper — inside half the
    width of the centreline — and far enough from both ends to be an interior
    junction rather than a corner.
    """
    line = track.centreline
    length = line.length
    if length <= 0:
        return []
    half = track.width_mm / 2.0 + NODE_TOLERANCE_MM
    clearance = CUT_CLEARANCE_MM
    cuts: set[float] = set()
    for x, y in points:
        probe = Point(x, y)
        if line.distance(probe) > half:
            continue
        along = line.project(probe)
        if along <= clearance or along >= length - clearance:
            continue
        cuts.add(round(along / length, 9))
    return sorted(cuts)


def _strand_point(track: Track, t: float) -> tuple[float, float]:
    """The point at parameter `t`, exact at the ends.

    `interpolate(0)` and `interpolate(length)` do return the endpoints, but
    going through the arithmetic for them would put the node a rounding error
    away from the identical endpoint of the track joined to it — which is the
    one place `NODE_TOLERANCE_MM` should not have to do any work.
    """
    if t <= 0.0:
        return track.start
    if t >= 1.0:
        return track.end
    point = track.centreline.interpolate(t * track.centreline.length)
    return (point.x, point.y)


def build_traces(board: Board, net: int | str,
                 copper_oz_by_layer: dict | None = None,
                 rise_c: float | None = None) -> list[Trace]:
    """A net's routed copper, decomposed into pin-to-pin runs.

    Returns one `Trace` per edge of the net's copper graph, each carrying its
    own narrowest point and IPC-2152 capacity. Zones are **not** included; see
    this section's header for why a pour is not a trace.

    `copper_oz_by_layer` should be `audit._copper_oz_by_layer(board)` so that a
    trace's capacity and the net's come from the same copper weights. The
    default matches `current.ampacity`'s: 1 oz outer, 0.5 oz inner.
    """
    # Local, and only here. `current` pulls in `scipy.ndimage`, `scipy.sparse`
    # and `scipy.sparse.linalg` for the plane solve; `topology` is imported by
    # almost everything and has no business dragging that in for callers that
    # only want islands.
    from pcbreview import current as current_module

    rise = current_module.RISE_C if rise_c is None else rise_c
    net_obj = board.net(net)
    outer = (board.copper_layers[0], board.copper_layers[-1])
    weights = copper_oz_by_layer or {}

    tracks = [t for t in board.tracks_on_net(net_obj.code)
              if t.layer in board.copper_layers]
    if not tracks:
        return []
    vias = board.vias_on_net(net_obj.code)
    pads = board.pads_on_net(net_obj.code)

    # 1. Every point that could land in the middle of a track, per layer.
    candidates: dict[str, list[tuple[float, float]]] = {}
    for track in tracks:
        candidates.setdefault(track.layer, []).extend((track.start, track.end))
    for via in vias:
        for layer in via.layers:
            if layer in board.copper_layers:
                candidates.setdefault(layer, []).append(via.pos)
    for pad in pads:
        for layer in pad.copper_layers:
            if layer in board.copper_layers:
                candidates.setdefault(layer, []).append(pad.pos)

    # Indexed per layer so a point is only tested against tracks it could
    # possibly touch — the alternative is tracks x points, which on the V3
    # board's 1107 tracks is a million distance calls for nothing.
    by_layer: dict[str, list[Track]] = {}
    for track in tracks:
        by_layer.setdefault(track.layer, []).append(track)

    strands: list[Strand] = []
    for layer, layer_tracks in by_layer.items():
        points = candidates.get(layer, [])
        if not points:
            for track in layer_tracks:
                strands.append(Strand(track, 0.0, 1.0))
            continue
        tree = STRtree([t.copper for t in layer_tracks])
        hits: dict[int, list[tuple[float, float]]] = {}
        for point in points:
            for index in tree.query(Point(point)):
                hits.setdefault(int(index), []).append(point)
        for index, track in enumerate(layer_tracks):
            cuts = _cut_parameters(track, hits.get(index, []))
            bounds = [0.0, *cuts, 1.0]
            for a, b in zip(bounds, bounds[1:]):
                if b - a > 0:
                    strands.append(Strand(track, a, b))

    # 2. Nodes, and the merges that make a pad one terminal and a via one node.
    index = _NodeIndex()
    strand_nodes: list[tuple[int, int]] = []
    for strand in strands:
        p0 = _strand_point(strand.track, strand.t0)
        p1 = _strand_point(strand.track, strand.t1)
        strand_nodes.append((index.node(strand.track.layer, *p0),
                             index.node(strand.track.layer, *p1)))

    # A via is one node: it is one conductor, and a run passing through it
    # changes layer without becoming two traces.
    via_node: dict[int, int] = {}
    for via in vias:
        spanned = [l for l in via.layers if l in board.copper_layers]
        if not spanned:
            continue
        nodes = [index.node(layer, *via.pos) for layer in spanned]
        for other in nodes[1:]:
            index.union(nodes[0], other)
        via_node[via.id] = nodes[0]

    # A pad is one terminal across every layer it occupies, and it absorbs
    # every node standing on its copper — a track ends *inside* a pad, not at
    # its centre, so coordinates alone would never join them.
    pad_node: dict[int, TraceEnd] = {}
    pad_layers: dict[int, list[str]] = {}
    anchors: dict[int, int] = {}
    for position, pad in enumerate(pads):
        layers = [l for l in pad.copper_layers if l in board.copper_layers]
        if not layers:
            continue
        pad_layers[position] = layers
        anchor = index.node(layers[0], *pad.pos)
        for layer in layers[1:]:
            index.union(anchor, index.node(layer, *pad.pos))
        anchors[position] = anchor
    # Every node is placed by now, so the spatial index is built once and each
    # pad asks it which nodes stand on its copper. The obvious loop — every pad
    # against every node — is `pads x nodes` containment tests, and `GND` on the
    # V3 board is 49 pads against several thousand nodes.
    if anchors:
        sites = [Point(x, y) for _, x, y in
                 (index.point(n) for n in range(len(index)))]
        tree = STRtree(sites)
        for position, anchor in anchors.items():
            pad = pads[position]
            layers = pad_layers[position]
            copper = pad.copper.buffer(NODE_TOLERANCE_MM)
            for hit in tree.query(copper):
                node = int(hit)
                layer, x, y = index.point(node)
                if layer in layers and copper.contains(sites[node]):
                    index.union(anchor, node)
        # Resolved **after** every union, not inside the loop: two pads whose
        # copper touches share a root, and a root recorded mid-loop is the one
        # that was current then rather than the one that survives.
        for position, anchor in anchors.items():
            pad = pads[position]
            pad_node[index.find(anchor)] = TraceEnd(
                "pad", pad.designator, pad.pos, pad_layers[position][0])

    # 3. Degrees, over the merged nodes.
    adjacency: dict[int, list[tuple[int, int]]] = {}
    for position, (a, b) in enumerate(strand_nodes):
        ra, rb = index.find(a), index.find(b)
        adjacency.setdefault(ra, []).append((position, rb))
        adjacency.setdefault(rb, []).append((position, ra))
    via_roots = {index.find(node): via_id
                 for via_id, node in via_node.items()}

    def is_terminal(node: int) -> bool:
        if node in pad_node:
            return True
        return len(adjacency.get(node, ())) != 2

    def end_for(node: int) -> TraceEnd:
        if node in pad_node:
            return pad_node[node]
        layer, x, y = index.point(node)
        degree = len(adjacency.get(node, ()))
        if node in via_roots:
            return TraceEnd("via", f"via#{via_roots[node]}", (x, y), layer)
        kind = "junction" if degree > 2 else "open"
        name = "junction" if degree > 2 else "open end"
        return TraceEnd(kind, name, (x, y), layer)

    # 4. Contract every chain of degree-2 nodes into one trace.
    used: set[int] = set()
    chains: list[tuple[list[int], int, int]] = []
    for node in [n for n in adjacency if is_terminal(n)]:
        for position, _ in adjacency[node]:
            if position in used:
                continue
            chain = [position]
            used.add(position)
            a, b = strand_nodes[position]
            current_node = index.find(b) if index.find(a) == node else index.find(a)
            while not is_terminal(current_node):
                nxt = [(p, o) for p, o in adjacency[current_node]
                       if p not in used]
                if not nxt:
                    break
                position2, other = nxt[0]
                used.add(position2)
                chain.append(position2)
                current_node = other
            chains.append((chain, node, current_node))
    # A ring with no terminal at all — a net routed as a loop. Rare, and it
    # would be silently dropped by the walk above, which is the one failure
    # mode of "start from the terminals" worth guarding.
    for position in range(len(strands)):
        if position in used:
            continue
        chain = [position]
        used.add(position)
        a, b = strand_nodes[position]
        start = index.find(a)
        current_node = index.find(b)
        while current_node != start:
            nxt = [(p, o) for p, o in adjacency[current_node] if p not in used]
            if not nxt:
                break
            position2, other = nxt[0]
            used.add(position2)
            chain.append(position2)
            current_node = other
        chains.append((chain, start, current_node))

    # 5. The physics, per trace.
    traces: list[Trace] = []
    for number, (chain, start, finish) in enumerate(chains):
        members = [strands[p] for p in chain]
        weakest = None
        weakest_key = None
        for strand in members:
            layer = strand.track.layer
            oz = weights.get(layer, 1.0 if layer in outer else 0.5)
            amps = physics.current_capacity_a(
                strand.track.width_mm, physics.copper_thickness_mm(oz),
                rise, internal=layer not in outer)
            if weakest is None or amps < weakest:
                weakest, weakest_key = amps, (strand.track.width_mm, layer)
        # Interior nodes only: an end node's via is shared with whatever is on
        # the other side of it.
        interior = set()
        for position in chain:
            for node in strand_nodes[position]:
                root = index.find(node)
                if root not in (start, finish):
                    interior.add(root)
        barrels = [via_id for root, via_id in via_roots.items()
                   if root in interior]
        via_limit = None
        if barrels:
            by_id = {v.id: v for v in vias}
            via_limit = min(current_module.via_capacity_a(
                by_id[v].drill_mm, rise_c=rise) for v in barrels)

        traces.append(Trace(
            id=number,
            net=net_obj.name,
            ends=(end_for(start), end_for(finish)),
            strands=members,
            via_ids=sorted(barrels),
            layers=sorted({s.track.layer for s in members}),
            length_mm=sum(s.length_mm for s in members),
            narrowest_mm=weakest_key[0] if weakest_key else 0.0,
            narrowest_layer=weakest_key[1] if weakest_key else "",
            rise_c=rise,
            capacity_a=round(weakest, 3) if weakest is not None else None,
            via_capacity_a=round(via_limit, 3) if via_limit is not None else None,
        ))
    return traces


def build_all_traces(board: Board, copper_oz_by_layer: dict | None = None,
                     rise_c: float | None = None) -> dict[str, list[Trace]]:
    """`build_traces` for every net that has routed copper, keyed by net name."""
    result: dict[str, list[Trace]] = {}
    for net in board.nets.values():
        if not board.tracks_on_net(net.code):
            continue
        result[net.name] = build_traces(board, net.code, copper_oz_by_layer,
                                        rise_c)
    return result


_NET_IN_BRACKETS = re.compile(r"\[([^\]]+)\]")


def broken_nets_per_kicad(drc_report) -> set[str]:
    """Net names KiCad's DRC reports as having a missing connection.

    Each ``unconnected_items`` violation names the two items it could not
    join, with the net in brackets — "Track [/UART2 RX] on In3.Cu". This is
    per-net ground truth, unlike the aggregate unconnected count.
    """
    names: set[str] = set()
    for violation in drc_report.unconnected:
        for item in violation.items:
            match = _NET_IN_BRACKETS.search(item.description)
            if match:
                names.add(match.group(1))
    return names


def disagreements_with_kicad(board: Board, drc_report,
                             topologies: dict[str, NetTopology] | None = None
                             ) -> list[str]:
    """Compare our geometric connectivity against KiCad's, in both directions.

    A net KiCad reports broken must split for us too, and a net it does not
    report must not. Either kind of disagreement means our geometry is wrong —
    these are not findings about the board.

    Returns human-readable descriptions; empty means we agree exactly.
    """
    if topologies is None:
        topologies = build_all(board)

    broken = broken_nets_per_kicad(drc_report)
    problems = []

    for name, topology in sorted(topologies.items()):
        islands = len([i for i in topology.islands if i.pads or i.has_zone])
        if islands <= 1 and name in broken:
            problems.append(
                f"{name}: KiCad reports a missing connection, we found it joined")
        elif islands > 1 and name not in broken:
            groups = [sorted(p.designator for p in i.pads) or [f"{len(i.items)} items"]
                      for i in topology.islands if i.pads or i.has_zone]
            problems.append(
                f"{name}: KiCad reports it fully joined, we split it into "
                f"{islands} islands {groups}")
    return problems
