"""Reference planes and return paths.

Every signal current has a return current, and on a PCB that return flows in
the nearest reference plane, directly beneath the trace. Where the plane is
missing, slotted, or belongs to the wrong net, the return current detours — and
the loop area it encloses is what radiates, couples, and adds inductance.

This is the analysis the tool exists for, and the one existing tools do badly:
they sample points along a trace and ask "is there copper near here?". That
misses narrow slots, cannot measure how long a gap actually is, and gives false
confidence on thermal-relief swiss cheese. Here it is exact polygon geometry —
the trace's own copper footprint intersected with the plane below it.

Two things make this non-trivial on real stackups:

* **Which plane?** On a 6-layer sig/gnd/sig/pwr/gnd/sig board, a trace on L3
  sits 0.109 mm from L4 (power) and 0.550 mm from L2 (ground). It references
  the *power* plane, and its return current flows there — which is fine only
  if that plane is well decoupled to ground nearby.
* **What happens at a via?** When a net changes layer, its reference may change
  too. A ground-to-ground reference change needs a stitching via nearby; a
  ground-to-power change needs a decoupling capacitor. Without one, the return
  current has to find its own way around, and that detour is usually the single
  largest loop on the board.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cached_property

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from pcbreview.model import Board, Track, Via

# A plane must cover at least this fraction of a trace's footprint for that
# segment to count as referenced. Below it the "plane" is incidental copper.
COVERAGE_THRESHOLD = 0.98

# Below this, an unreferenced stretch is numerical noise from polygon edges
# rather than a feature of the board.
MIN_REPORTABLE_GAP_MM = 0.05

# An enclosed hole (antipad, thermal relief) only matters once the detour
# around it is comparable to the trace geometry itself. A 0.9 mm antipad costs
# about 0.8 mm of detour; a slot costs far more.
SIGNIFICANT_DETOUR_MM = 2.0

# How far a stitching via may be from a layer transition before the return
# current's detour starts to matter. A quarter of this is already significant
# at high edge rates; see `stitching_distance_limit_mm` for the rate-aware form.
DEFAULT_STITCH_RADIUS_MM = 2.0

# Ground detection lives in `intent`, which reads it off the schematic. These
# re-exports keep the name-only fallback reachable for boards with no schematic.
from pcbreview.intent import GROUND_NAME_HINTS, looks_like_ground  # noqa: E402,F401


@dataclass
class PlaneCandidate:
    """A copper layer considered as a reference for some signal layer."""
    layer: str
    net_name: str
    distance_mm: float          # dielectric thickness to the signal layer
    epsilon_r: float
    is_ground: bool
    coverage: float             # fraction of the trace footprint it covers

    @property
    def quality(self) -> str:
        if self.coverage < COVERAGE_THRESHOLD:
            return "partial"
        return "ground" if self.is_ground else "power"


@dataclass
class Gap:
    """A stretch of trace with no reference copper directly beneath it.

    Raw absence of copper is not the same as a broken return path, and
    conflating the two is why naive tools drown users in false positives. A
    trace crossing a via antipad has no copper under it for ~0.6 mm, but the
    plane is continuous all around, so the return current simply spreads around
    the hole. A trace crossing a plane *split* has nowhere to go.

    ``kind`` distinguishes them, and ``detour_mm`` quantifies the extra
    distance the return current must travel — which is the thing that actually
    costs loop area.
    """
    length_mm: float
    centre: tuple[float, float]
    kind: str = "off_plane"          # enclosed | off_plane
    detour_mm: float = 0.0
    geometry: Polygon | MultiPolygon = field(repr=False, default=None)

    @property
    def is_enclosed(self) -> bool:
        """The gap sits in a hole with plane copper all the way around it."""
        return self.kind == "enclosed"

    def is_significant(self, detour_limit_mm: float) -> bool:
        """Significance is the detour, not the gap length or its kind.

        A trace can leave the plane entirely for half a millimetre near a pad
        without the return current meaningfully detouring, and it can cross a
        0.6 mm antipad that happens to sit in a long slot and detour by
        centimetres. Applying one detour threshold to both keeps the metric
        physical. For an off-plane gap the detour is taken as the gap's own
        length, which is a lower bound — there is no local way across.
        """
        return self.detour_mm > detour_limit_mm

    def __str__(self) -> str:
        return (f"{self.length_mm:.2f} mm {self.kind} gap at "
                f"({self.centre[0]:.2f}, {self.centre[1]:.2f}), "
                f"return detour {self.detour_mm:.2f} mm")


def _detour_around(hole: Polygon, piece) -> float:
    """Extra path length the return current takes to get around a plane hole.

    The current leaves the plane where the trace enters the hole and rejoins it
    where the trace leaves, so the detour is the shorter of the two ways round
    the hole boundary between those points, minus the straight crossing.

    Using half the hole's perimeter instead is badly wrong in via fanout
    regions, where dozens of antipads merge into one void with a perimeter of
    tens of millimetres — while the actual detour around the local lobe is a
    fraction of a millimetre.
    """
    ring = hole.exterior
    perimeter = ring.length
    if perimeter <= 0:
        return piece.length

    start, end = Point(piece.coords[0]), Point(piece.coords[-1])
    entry, exit_ = ring.project(start), ring.project(end)
    along = abs(entry - exit_)
    shorter = min(along, perimeter - along)
    return max(0.0, shorter - piece.length)


@dataclass
class SegmentReference:
    """The reference situation for one track segment."""
    track: Track
    above: PlaneCandidate | None
    below: PlaneCandidate | None

    @property
    def primary(self) -> PlaneCandidate | None:
        """The plane this segment actually references: nearest with coverage.

        Proximity dominates — return current concentrates in the closest plane
        — but a nearer plane that does not actually cover the trace loses to a
        further one that does.
        """
        candidates = [c for c in (self.above, self.below) if c is not None]
        if not candidates:
            return None
        covering = [c for c in candidates if c.coverage >= COVERAGE_THRESHOLD]
        pool = covering or candidates
        return min(pool, key=lambda c: (c.distance_mm, -c.coverage))

    @property
    def is_referenced(self) -> bool:
        primary = self.primary
        return primary is not None and primary.coverage >= COVERAGE_THRESHOLD

    @property
    def references_ground(self) -> bool:
        primary = self.primary
        return primary is not None and primary.is_ground


@dataclass
class NetReference:
    """Reference-plane analysis for a whole net."""
    net_name: str
    segments: list[SegmentReference]
    gaps: list[Gap]
    total_length_mm: float
    covered_length_mm: float
    detour_limit_mm: float = SIGNIFICANT_DETOUR_MM

    @property
    def raw_coverage(self) -> float:
        """Fraction of routed length with copper literally underneath.

        Reported for transparency, but do not act on it: every via antipad the
        trace crosses subtracts from it without the return path being harmed.
        """
        if self.total_length_mm <= 0:
            return 1.0
        return self.covered_length_mm / self.total_length_mm

    @property
    def significant_gaps(self) -> list[Gap]:
        """Gaps where the return current genuinely has to detour."""
        return [g for g in self.gaps if g.is_significant(self.detour_limit_mm)]

    @property
    def coverage(self) -> float:
        """Fraction of routed length with an intact return path beneath it.

        Antipads and thermal reliefs are credited as covered — the plane is
        continuous around them — so this is the number worth acting on.
        """
        if self.total_length_mm <= 0:
            return 1.0
        lost = sum(g.length_mm for g in self.significant_gaps)
        return max(0.0, 1.0 - lost / self.total_length_mm)

    @property
    def uncovered_length_mm(self) -> float:
        return sum(g.length_mm for g in self.significant_gaps)

    @property
    def worst_gap(self) -> Gap | None:
        gaps = self.significant_gaps or self.gaps
        return max(gaps, key=lambda g: g.detour_mm or g.length_mm) if gaps else None

    @cached_property
    def reference_nets(self) -> dict[str, float]:
        """Which nets this trace references, by length. Reveals split planes."""
        by_net: dict[str, float] = {}
        for segment in self.segments:
            primary = segment.primary
            if primary is None:
                continue
            by_net[primary.net_name] = (
                by_net.get(primary.net_name, 0.0) + segment.track.length_mm)
        return dict(sorted(by_net.items(), key=lambda kv: -kv[1]))

    @property
    def references_multiple_nets(self) -> bool:
        return len(self.reference_nets) > 1

    def summary(self) -> dict:
        worst = self.worst_gap
        return {
            "net": self.net_name,
            "length_mm": round(self.total_length_mm, 3),
            "coverage": round(self.coverage, 4),
            "raw_coverage": round(self.raw_coverage, 4),
            "uncovered_mm": round(self.uncovered_length_mm, 3),
            "gaps": len(self.significant_gaps),
            "enclosed_gaps": len(self.gaps) - len(self.significant_gaps),
            "worst_gap_mm": round(worst.length_mm, 3) if worst else 0.0,
            "worst_detour_mm": round(worst.detour_mm, 3) if worst else 0.0,
            "reference_nets": {k: round(v, 2) for k, v in self.reference_nets.items()},
        }


class ReferenceResolver:
    """Resolves reference planes for a board with a bound stackup.

    Plane geometry per layer is built once and reused, since the expensive part
    is unioning zone fills and wide tracks, not the per-trace intersection.
    """

    def __init__(self, board: Board, ground_nets: set[str] | None = None,
                 coverage_threshold: float = COVERAGE_THRESHOLD,
                 schematic=None):
        """Pass ``schematic`` (a `pcbreview.schematic.Schematic`) to have the
        ground nets read off the circuit instead of guessed from names.

        Which plane a trace references, and whether a layer change needs a
        stitching via or a decoupling capacitor, both turn on knowing which
        nets are ground. Getting that from names is the weakest link in the
        chain: it takes a signal called `GND_SW` for a plane and misses a
        return rail called `0V`. `intent.classify_rails` derives it from
        decoupling topology instead — see its docstring.

        Explicit ``ground_nets`` still wins, so a caller who knows can say so.
        """
        if board.stackup is None:
            raise ValueError(
                "reference analysis needs a bound stackup — call "
                "board.bind_stackup() with a validated stackup first")
        self.board = board
        self.stackup = board.stackup
        self.coverage_threshold = coverage_threshold
        self.ground_evidence: list = []

        if ground_nets is None and schematic is not None:
            from pcbreview.intent import infer
            intent = infer(schematic, board)
            ground_nets = intent.ground_nets
            self.ground_evidence = [r for r in intent.roles if r.role == "ground"]

        self.ground_nets = (
            ground_nets if ground_nets is not None
            else {n.name for n in board.nets.values() if looks_like_ground(n.name)})
        self._plane_cache: dict[tuple[str, str], MultiPolygon] = {}
        self._hole_cache: dict[tuple[str, str], tuple] = {}
        self._union_cache: dict[str, MultiPolygon] = {}
        self._layer_nets_cache: dict[str, list[str]] = {}

        # Map board layer name -> stackup copper layer name, for dielectrics.
        self._to_stackup = {
            self.stackup.resolve_layer(l.name): l.name
            for l in self.stackup.copper_layers
        }

    # -- plane geometry ---------------------------------------------------

    def plane_geometry(self, layer: str, net_name: str) -> MultiPolygon:
        """Plane-like copper for one net on one layer.

        Includes zone fill and wide tracks: a signal running over a fat power
        *track* is referenced just as well as one over a poured zone, and
        boards without zones rely on exactly that.
        """
        key = (layer, net_name)
        if key in self._plane_cache:
            return self._plane_cache[key]

        parts: list = []
        fill = self.board.zone_fill(layer, net_name)
        if not fill.is_empty:
            parts.append(fill)
        for track in self.board.tracks:
            if track.layer == layer and self.board.nets[track.net].name == net_name:
                parts.append(track.copper)

        geometry = unary_union(parts) if parts else MultiPolygon()
        if geometry.geom_type == "Polygon":
            geometry = MultiPolygon([geometry])
        self._plane_cache[key] = geometry
        return geometry

    def plane_union(self, layer: str) -> MultiPolygon:
        """All plane-like copper on a layer, regardless of which net owns it.

        Return current cares first about whether there is *a* plane underneath;
        which net owns it is the second question. Measuring gaps against a
        single net's plane would count a trace crossing from over-ground to
        over-power as unreferenced, when in fact it is referenced throughout —
        that crossing is a split-plane finding, reported separately by
        :meth:`NetReference.reference_nets`.
        """
        if layer in self._union_cache:
            return self._union_cache[layer]
        parts = [self.plane_geometry(layer, name)
                 for name in self.candidate_nets_on(layer)[:8]]
        parts = [p for p in parts if not p.is_empty]
        geometry = unary_union(parts) if parts else MultiPolygon()
        if geometry.geom_type == "Polygon":
            geometry = MultiPolygon([geometry])
        self._union_cache[layer] = geometry
        return geometry

    def plane_holes(self, layer: str, net_name: str):
        """Interior holes of a plane, indexed, with the detour around each.

        A hole is copper-free area completely enclosed by plane copper — a via
        antipad, a thermal relief, a mounting-hole keepout. Return current
        crossing one flows around it, and the detour is roughly half the hole's
        perimeter instead of the straight line through it.

        Anything *not* inside a hole is off the plane entirely, where the
        return current has no local way across.
        """
        key = (layer, net_name)
        if key in self._hole_cache:
            return self._hole_cache[key]

        plane = (self.plane_union(layer) if net_name is None
                 else self.plane_geometry(layer, net_name))
        holes: list[Polygon] = []
        for polygon in plane.geoms:
            for ring in polygon.interiors:
                hole = Polygon(ring)
                if hole.is_valid and not hole.is_empty:
                    holes.append(hole)

        tree = STRtree(holes) if holes else None
        self._hole_cache[key] = (tree, holes)
        return self._hole_cache[key]

    def classify_gap(self, piece, layer: str, net_name: str) -> Gap:
        """Turn a length of unreferenced centreline into a classified Gap."""
        midpoint = piece.interpolate(0.5, normalized=True)
        centre = (midpoint.x, midpoint.y)
        tree, holes = self.plane_holes(layer, net_name)

        if tree is not None:
            for index in tree.query(piece):
                hole = holes[index]
                if not hole.contains(midpoint) and not hole.intersects(piece):
                    continue
                return Gap(length_mm=piece.length, centre=centre, kind="enclosed",
                           detour_mm=_detour_around(hole, piece),
                           geometry=piece)

        return Gap(length_mm=piece.length, centre=centre, kind="off_plane",
                   detour_mm=piece.length, geometry=piece)

    def candidate_nets_on(self, layer: str) -> list[str]:
        """Nets with enough copper on a layer to act as a reference.

        Ranked by poured area, so ground and power planes come first and we do
        not test every signal net.
        """
        if layer in self._layer_nets_cache:
            return self._layer_nets_cache[layer]

        areas: dict[str, float] = {}
        for zone in self.board.zones:
            if zone.is_rule_area or layer not in zone.layers:
                continue
            fill = zone.fill_on(layer)
            if not fill.is_empty:
                areas[zone.net_name] = areas.get(zone.net_name, 0.0) + fill.area
        for track in self.board.tracks:
            if track.layer != layer or track.width_mm < 0.5:
                continue
            name = self.board.nets[track.net].name
            areas[name] = areas.get(name, 0.0) + track.copper.area

        ranked = [n for n, _ in sorted(areas.items(), key=lambda kv: -kv[1]) if n]
        self._layer_nets_cache[layer] = ranked
        return ranked

    # -- stackup neighbours ----------------------------------------------

    def neighbours(self, layer: str) -> tuple[str | None, str | None]:
        """The copper layers immediately above and below a signal layer."""
        index = self.board.layer_index(layer)
        above = self.board.copper_layers[index - 1] if index > 0 else None
        below = (self.board.copper_layers[index + 1]
                 if index + 1 < len(self.board.copper_layers) else None)
        return above, below

    def dielectric_to(self, layer_a: str, layer_b: str) -> tuple[float, float]:
        return self.stackup.dielectric_between(
            self._to_stackup[layer_a], self._to_stackup[layer_b])

    # -- per-segment analysis --------------------------------------------

    def _best_candidate(self, footprint: Polygon, signal_layer: str,
                        plane_layer: str | None) -> PlaneCandidate | None:
        if plane_layer is None:
            return None
        distance, epsilon = self.dielectric_to(signal_layer, plane_layer)
        area = footprint.area
        if area <= 0:
            return None

        best: PlaneCandidate | None = None
        for net_name in self.candidate_nets_on(plane_layer)[:6]:
            plane = self.plane_geometry(plane_layer, net_name)
            if plane.is_empty or not plane.intersects(footprint):
                continue
            coverage = footprint.intersection(plane).area / area
            if coverage <= 0.01:
                continue
            candidate = PlaneCandidate(
                layer=plane_layer, net_name=net_name, distance_mm=distance,
                epsilon_r=epsilon, is_ground=net_name in self.ground_nets,
                coverage=coverage)
            # Prefer better coverage, then ground over power.
            if best is None or (candidate.coverage, candidate.is_ground) > (
                    best.coverage, best.is_ground):
                best = candidate
            if coverage >= 0.999:
                break
        return best

    def segment_reference(self, track: Track) -> SegmentReference:
        footprint = track.copper
        above_layer, below_layer = self.neighbours(track.layer)
        return SegmentReference(
            track=track,
            above=self._best_candidate(footprint, track.layer, above_layer),
            below=self._best_candidate(footprint, track.layer, below_layer),
        )

    # -- per-net analysis -------------------------------------------------

    def analyse_net(self, net: int | str,
                    min_gap_mm: float = MIN_REPORTABLE_GAP_MM,
                    detour_limit_mm: float = SIGNIFICANT_DETOUR_MM) -> NetReference:
        """Reference coverage for a whole net, with the gaps located.

        Coverage is measured on the trace's *centreline* rather than its area:
        a gap is a length of signal path with no plane under it, and length is
        what determines the return-current detour.
        """
        net_obj = self.board.net(net)
        tracks = self.board.tracks_on_net(net_obj.code)

        segments: list[SegmentReference] = []
        gaps: list[Gap] = []
        total = covered = 0.0

        for track in tracks:
            reference = self.segment_reference(track)
            segments.append(reference)
            total += track.length_mm

            primary = reference.primary
            if primary is None:
                midpoint = track.centreline.interpolate(0.5, normalized=True)
                gaps.append(Gap(track.length_mm, (midpoint.x, midpoint.y),
                                kind="off_plane", detour_mm=track.length_mm,
                                geometry=track.centreline))
                continue

            plane = self.plane_union(primary.layer)
            centreline = track.centreline
            inside = centreline.intersection(plane)
            covered_here = inside.length if not inside.is_empty else 0.0
            covered += covered_here

            outside = centreline.difference(plane)
            if outside.is_empty:
                continue
            pieces = ([outside] if outside.geom_type == "LineString"
                      else list(getattr(outside, "geoms", [])))
            for piece in pieces:
                if piece.length < min_gap_mm:
                    continue
                gaps.append(self.classify_gap(piece, primary.layer, None))

        gaps.sort(key=lambda g: (-g.detour_mm, -g.length_mm))
        return NetReference(net_name=net_obj.name, segments=segments, gaps=gaps,
                            total_length_mm=total, covered_length_mm=covered,
                            detour_limit_mm=detour_limit_mm)


# -- layer transitions ----------------------------------------------------

@dataclass
class Transition:
    """A via where a net changes layer, and what happens to its reference."""
    via: Via
    from_layer: str
    to_layer: str
    from_reference: PlaneCandidate | None
    to_reference: PlaneCandidate | None
    nearest_stitch_mm: float | None
    nearest_stitch_net: str | None

    @property
    def reference_changed(self) -> bool:
        if self.from_reference is None or self.to_reference is None:
            return True
        return self.from_reference.net_name != self.to_reference.net_name

    @property
    def kind(self) -> str:
        """What the return current needs in order to follow the signal."""
        if not self.reference_changed:
            return "same_reference"          # nothing needed
        if self.from_reference is None or self.to_reference is None:
            return "unreferenced"
        if self.from_reference.is_ground and self.to_reference.is_ground:
            return "ground_to_ground"        # needs a stitching via
        return "ground_to_power"             # needs a decoupling capacitor

    @property
    def needs_stitching(self) -> bool:
        return self.kind == "ground_to_ground"

    def __str__(self) -> str:
        source = self.from_reference.net_name if self.from_reference else "none"
        target = self.to_reference.net_name if self.to_reference else "none"
        stitch = (f"{self.nearest_stitch_mm:.2f} mm"
                  if self.nearest_stitch_mm is not None else "none found")
        return (f"via#{self.via.id} {self.from_layer}->{self.to_layer}: "
                f"reference {source}->{target}, nearest stitch {stitch}")


def stitching_distance_limit_mm(rise_time_ps: float, epsilon_r: float = 4.4,
                                fraction: float = 1 / 20) -> float:
    """How close a stitching via must be, from the signal's edge rate.

    Uses the knee frequency (0.5 / t_r) and the usual lambda/20 rule for when a
    discontinuity stops being electrically small.
    """
    if rise_time_ps <= 0:
        return DEFAULT_STITCH_RADIUS_MM
    knee_hz = 0.5 / (rise_time_ps * 1e-12)
    velocity_mm_s = 299_792_458_000.0 / math.sqrt(epsilon_r)
    return (velocity_mm_s / knee_hz) * fraction


def nearest_bridge_capacitor(board: Board, net_a: str, net_b: str,
                             point: tuple[float, float]
                             ) -> tuple[float, str] | None:
    """The closest two-pad part joining two nets, and how far away it is.

    When a signal changes layer and its reference changes from ground to a
    power plane, the return current has to cross between the two planes, and
    the only path it has is a capacitor bridging them. A stitching via cannot
    help — the planes are different nets. So the question "is there a
    decoupling capacitor near this transition" is the power-plane counterpart
    of "is there a stitching via near this one", and it is answered the same
    way: by measuring.

    Found from the board's own pads rather than from the schematic, so it works
    without one; any two-pad part between the two nets counts, since a ferrite
    or a resistor there would be a finding of its own kind.
    """
    here = Point(point)
    wanted = {net_a, net_b}
    best: tuple[float, str] | None = None
    for footprint in board.footprints:
        pads = [p for p in footprint.pads if p.net_name]
        if len({p.net_name for p in pads}) != 2:
            continue
        if {p.net_name for p in pads} != wanted:
            continue
        distance = min(here.distance(Point(p.pos)) for p in pads)
        if best is None or distance < best[0]:
            best = (distance, footprint.ref)
    return best


def find_transitions(resolver: ReferenceResolver, net: int | str,
                     search_radius_mm: float = DEFAULT_STITCH_RADIUS_MM
                     ) -> list[Transition]:
    """Layer changes on a net, and whether the return current can follow.

    For each via carrying the net between layers that have tracks, resolve the
    reference plane on each side. Where the reference changes, find the nearest
    via that could carry the return current between those two planes.
    """
    board = resolver.board
    net_obj = board.net(net)
    tracks = board.tracks_on_net(net_obj.code)
    if not tracks:
        return []

    # Which layers this net actually uses, and a reference sample per layer.
    by_layer: dict[str, list[Track]] = {}
    for track in tracks:
        by_layer.setdefault(track.layer, []).append(track)

    reference_by_layer: dict[str, PlaneCandidate | None] = {}
    for layer, layer_tracks in by_layer.items():
        longest = max(layer_tracks, key=lambda t: t.length_mm)
        reference_by_layer[layer] = resolver.segment_reference(longest).primary

    transitions: list[Transition] = []
    for via in board.vias_on_net(net_obj.code):
        touching = [layer for layer in via.layers if layer in by_layer
                    and any(t.copper.intersects(via.pad_on(layer))
                            for t in by_layer[layer])]
        if len(touching) < 2:
            continue

        order = sorted(touching, key=board.layer_index)
        from_layer, to_layer = order[0], order[-1]
        from_reference = reference_by_layer.get(from_layer)
        to_reference = reference_by_layer.get(to_layer)

        stitch_distance = stitch_net = None
        if (from_reference and to_reference
                and from_reference.net_name != to_reference.net_name
                or (from_reference and to_reference
                    and from_reference.layer != to_reference.layer)):
            wanted = {from_reference.net_name, to_reference.net_name}
            here = Point(via.pos)
            for other in board.vias:
                name = board.nets[other.net].name if other.net in board.nets else ""
                if name not in wanted or other.id == via.id:
                    continue
                if not ({from_reference.layer, to_reference.layer} <= set(other.layers)):
                    continue
                distance = here.distance(Point(other.pos))
                if stitch_distance is None or distance < stitch_distance:
                    stitch_distance, stitch_net = distance, name

        transitions.append(Transition(
            via=via, from_layer=from_layer, to_layer=to_layer,
            from_reference=from_reference, to_reference=to_reference,
            nearest_stitch_mm=stitch_distance, nearest_stitch_net=stitch_net))

    return transitions
