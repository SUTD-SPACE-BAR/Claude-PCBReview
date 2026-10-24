"""Coupled runs between nets, and differential-pair routing quality.

One primitive underpins both differential-pair checking and crosstalk: *how much
of net A runs close to net B, and how close*. Answer that exactly and the rest
follows — a differential pair is a coupled run that is supposed to exist, and a
crosstalk risk is one that is not.

Coupling is measured **edge to edge**, not centre to centre. Two 0.2 mm traces
with 0.2 mm of empty space between them have a 0.2 mm gap here, which is what
the field sees and what fab rules quote. Centre-to-centre would call the same
geometry 0.4 mm and silently rescale every coupling result.

Proximity alone is *not* sufficient evidence of coupling. A trace crossing
another at a right angle sits within 0.5 mm of it for about 1.4 mm of travel —
long enough to survive a minimum-run-length filter, but not coupling in any
useful sense.

Parallelism is therefore tested directly, and cheaply, from the rate of change
of the gap along the victim. For two straight traces meeting at angle theta,
moving ``ds`` along one changes the separation by ``ds * sin(theta)``, so
``|d(gap)/ds|`` *is* ``sin(theta)``: 0 for parallel traces, 1 for a
perpendicular crossing. Thresholding it needs no extra geometry queries, since
the gap array is already in hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.ops import unary_union

from pcbreview.model import Board

# Sampling step along the victim trace. 25 um is well below any feature size
# that matters and keeps a 100 mm net to 4000 samples.
SAMPLE_STEP_MM = 0.025

# Coupled stretches shorter than this are corner clips, not runs.
MIN_RUN_LENGTH_MM = 0.5

# |d(gap)/ds| = sin(angle between the traces). 0.5 admits anything converging
# at up to 30 degrees and rejects steeper approaches and crossings. Also
# excludes the region past an aggressor's end cap, where the gap opens at
# roughly 1 mm per mm travelled.
MAX_CONVERGENCE = 0.5

# Default coupling window when no intent data says otherwise. Beyond ~3x the
# dielectric height coupling falls off steeply; 0.5 mm is a practical default
# for the 0.1 mm prepregs these boards use.
DEFAULT_MAX_GAP_MM = 0.5

# How far apart two broadside centrelines may sit, as a multiple of the
# dielectric between their layers, before the run stops being reported.
#
# Chosen to match what `DEFAULT_MAX_GAP_MM` already tolerates side by side
# rather than to reach a floor, because there is no floor to reach: on the
# stackup here (0.109 mm between In2 and In3) k_b is 0.316 stacked and still
# 0.052 at eight times the separation. But 0.052 is what two traces 0.5 mm
# apart on one layer couple at, which is exactly where the side-by-side window
# closes — so the two windows admit the same strength of coupling, and a
# finding that clears one clears the other.
BROADSIDE_OFFSET_RATIO = 8.0

# Name patterns for differential pairs, in KiCad and common house styles.
_PAIR_SUFFIXES = [
    (re.compile(r"^(?P<base>.*)\+$"), re.compile(r"^(?P<base>.*)-$")),
    (re.compile(r"^(?P<base>.*)_P$", re.I), re.compile(r"^(?P<base>.*)_N$", re.I)),
    (re.compile(r"^(?P<base>.*)_?D\+$", re.I), re.compile(r"^(?P<base>.*)_?D-$", re.I)),
    (re.compile(r"^(?P<base>.*)P$"), re.compile(r"^(?P<base>.*)N$")),
]


@dataclass
class CoupledRun:
    """One contiguous stretch where two nets run close together."""
    layer: str
    length_mm: float
    start_mm: float                 # distance along the victim from its start
    min_gap_mm: float
    max_gap_mm: float
    mean_gap_mm: float
    geometry: LineString | None = field(default=None, repr=False)

    @property
    def gap_variation_mm(self) -> float:
        return self.max_gap_mm - self.min_gap_mm

    def __str__(self) -> str:
        return (f"{self.length_mm:.2f} mm on {self.layer}, gap "
                f"{self.min_gap_mm:.3f}-{self.max_gap_mm:.3f} mm "
                f"(mean {self.mean_gap_mm:.3f})")


@dataclass
class Coupling:
    """How much of one net runs close to another."""
    victim: str
    aggressor: str
    max_gap_mm: float
    runs: list[CoupledRun]
    victim_length_mm: float
    aggressor_length_mm: float

    @cached_property
    def coupled_length_mm(self) -> float:
        return sum(r.length_mm for r in self.runs)

    @property
    def coupled_fraction(self) -> float:
        """Fraction of the victim that runs coupled. Your `isDifferentialPair`.

        Reported as a fraction so the caller decides what counts as "mostly
        coupled" — 0.9 is a well-routed pair, 0.3 is two traces that happen to
        share a channel.
        """
        if self.victim_length_mm <= 0:
            return 0.0
        return min(1.0, self.coupled_length_mm / self.victim_length_mm)

    @property
    def is_coupled(self) -> bool:
        return bool(self.runs)

    @property
    def longest_run(self) -> CoupledRun | None:
        return max(self.runs, key=lambda r: r.length_mm) if self.runs else None

    @cached_property
    def min_gap_mm(self) -> float | None:
        return min((r.min_gap_mm for r in self.runs), default=None)

    @cached_property
    def mean_gap_mm(self) -> float | None:
        """Length-weighted mean gap across all coupled runs."""
        if not self.runs:
            return None
        total = self.coupled_length_mm
        if total <= 0:
            return None
        return sum(r.mean_gap_mm * r.length_mm for r in self.runs) / total

    @property
    def layers(self) -> list[str]:
        return sorted({r.layer for r in self.runs})

    def summary(self) -> dict:
        return {
            "victim": self.victim,
            "aggressor": self.aggressor,
            "coupled_mm": round(self.coupled_length_mm, 3),
            "coupled_pct": round(self.coupled_fraction * 100, 1),
            "victim_mm": round(self.victim_length_mm, 3),
            "runs": len(self.runs),
            "min_gap_mm": round(self.min_gap_mm, 4) if self.min_gap_mm else None,
            "mean_gap_mm": round(self.mean_gap_mm, 4) if self.mean_gap_mm else None,
            "layers": self.layers,
        }


def _tracks_by_layer(board: Board, net: int | str) -> dict[str, list]:
    """A net's routed tracks grouped by layer, zero-length ones dropped.

    Memoised on the board. Every function here needs it, and the survey calls
    them once per net *pair*, so an aggressor to twenty victims regrouped its
    own tracks twenty times.
    """
    code = board.net(net).code

    def build():
        by_layer: dict[str, list] = {}
        for track in board.tracks_on_net(code):
            if track.length_mm <= 0:
                continue
            by_layer.setdefault(track.layer, []).append(track)
        return by_layer

    return board.memo(("tracks_by_layer", code), build)


def _copper_union(board: Board, net: int | str, layer: str):
    """A net's copper on one layer, unioned once. ``None`` if it has none there.

    Every track on the layer, *including* zero-length ones: a degenerate segment
    still buffers to a real disc of copper, and the gap to the aggressor is a
    distance to copper rather than to a centreline. `_centreline_union` drops
    them, because a zero-length line has no direction to run parallel to.
    """
    code = board.net(net).code

    def build():
        parts = [t.copper for t in board.tracks_on_net(code) if t.layer == layer]
        return unary_union(parts) if parts else None

    return board.memo(("copper_union", code, layer), build)


def _centreline_union(board: Board, net: int | str, layer: str):
    """A net's track centrelines on one layer, unioned once.

    This union was the largest single `shapely.union_all` cost in the broadside
    profile: `broadside_gaps` rebuilt the aggressor's centrelines on every call,
    so a net aggressing twenty victims built the identical union twenty times.
    """
    code = board.net(net).code

    def build():
        parts = [t.centreline for t in _tracks_by_layer(board, code).get(layer, ())]
        return unary_union(parts) if parts else None

    return board.memo(("centreline_union", code, layer), build)


def _runs_from_samples(distances: np.ndarray, coupled: np.ndarray,
                       step: float, layer: str, offset: float,
                       min_run_mm: float,
                       points: np.ndarray | None = None) -> list[CoupledRun]:
    """Turn a boolean coupled/not mask along a path into contiguous runs.

    ``points`` are the victim's sample points, so each run can carry the piece
    of centreline it covers. `CoupledRun.geometry` existed and was never
    populated by anything, which meant a `differential_pair` finding could only
    ever be rendered as "here are the two nets" rather than "here is the part
    of them that is actually a pair" (HANDOFF §40b).
    """
    runs: list[CoupledRun] = []
    if not coupled.any():
        return runs

    # Indices where the mask flips, giving [start, end) spans of True.
    padded = np.concatenate(([False], coupled, [False]))
    flips = np.flatnonzero(padded[1:] != padded[:-1])
    for start, end in zip(flips[0::2], flips[1::2]):
        length = (end - start) * step
        if length < min_run_mm:
            continue
        window = distances[start:end]
        geometry = None
        if points is not None and end - start >= 2:
            geometry = LineString([(p.x, p.y) for p in points[start:end]])
        runs.append(CoupledRun(
            layer=layer,
            length_mm=length,
            start_mm=offset + start * step,
            min_gap_mm=float(window.min()),
            max_gap_mm=float(window.max()),
            mean_gap_mm=float(window.mean()),
            geometry=geometry,
        ))
    return runs


@dataclass
class GapSamples:
    """The victim sampled along one track, against one aggressor.

    The raw measurement `coupling` aggregates and `crosstalk` solves. It is
    exposed so both go through the same edge-to-edge convention and the same
    parallelism test rather than each measuring proximity its own way.
    """
    layer: str
    step_mm: float
    offset_mm: float                 # where this track starts along the victim
    victim_width_mm: float
    gap_mm: np.ndarray               # edge to edge, per sample
    coupled: np.ndarray              # bool: within the window and parallel
    points: np.ndarray = field(default=None, repr=False)   # shapely points

    @property
    def coupled_length_mm(self) -> float:
        return float(self.coupled.sum()) * self.step_mm


def sampled_gaps(board: Board, victim: int | str, aggressor: int | str,
                 max_gap_mm: float = DEFAULT_MAX_GAP_MM,
                 step_mm: float = SAMPLE_STEP_MM,
                 max_convergence: float = MAX_CONVERGENCE) -> list[GapSamples]:
    """Sample the victim against the aggressor, one entry per victim track.

    Gaps are edge to edge: distance from the victim's centreline to the
    aggressor's *copper*, less the victim's own half-width. Measuring to the
    aggressor's copper rather than its centreline accounts for its width
    exactly, even where it changes width mid-run.

    A sample counts as coupled only if the gap is within the window *and* the
    two traces are running roughly parallel there — see the module docstring
    for why proximity alone is not enough.
    """
    victim_net = board.net(victim)
    aggressor_net = board.net(aggressor)
    victim_layers = _tracks_by_layer(board, victim_net.code)

    samples: list[GapSamples] = []
    for layer, entries in victim_layers.items():
        aggressor_copper = _copper_union(board, aggressor_net.code, layer)
        if aggressor_copper is None:
            continue

        offset = 0.0
        for track in entries:
            line = track.centreline
            victim_width = track.width_mm
            if line.length < step_mm:
                offset += line.length
                continue
            points, actual_step = track.sample_points(step_mm)

            to_copper = shapely.distance(points, aggressor_copper)
            edge_gap = np.maximum(to_copper - victim_width / 2.0, 0.0)

            # sin(angle between the traces), from the gap's rate of change.
            convergence = np.abs(np.gradient(edge_gap, actual_step))
            samples.append(GapSamples(
                layer=layer, step_mm=actual_step, offset_mm=offset,
                victim_width_mm=victim_width, gap_mm=edge_gap,
                coupled=((edge_gap <= max_gap_mm)
                         & (convergence <= max_convergence)),
                points=points))
            offset += line.length
    return samples


@dataclass
class BroadsideSamples:
    """The victim sampled against an aggressor on the layer next to it.

    Deliberately not a `GapSamples`. Side-by-side coupling is measured edge to
    edge, because the designer sets that clearance and zero would be a short;
    broadside coupling is measured **centreline to centreline**, because zero
    is the worst case rather than an impossible one, and because the quantity
    the cross-section needs is the lateral offset of one trace from the other.
    Sharing one field between the two conventions would make every consumer
    responsible for remembering which it had.
    """
    layer: str                       # the victim's layer
    aggressor_layer: str
    step_mm: float
    offset_mm: float                 # where this track starts along the victim
    victim_width_mm: float
    lateral_mm: np.ndarray           # centre to centre, per sample
    coupled: np.ndarray              # bool: within the window and parallel
    points: np.ndarray = field(default=None, repr=False)

    @property
    def coupled_length_mm(self) -> float:
        return float(self.coupled.sum()) * self.step_mm


def layer_pair_key(layer_a: str, layer_b: str) -> frozenset:
    """Order-free key for a pair of adjacent layers."""
    return frozenset((layer_a, layer_b))


def broadside_gaps(board: Board, victim: int | str, aggressor: int | str,
                   windows_mm: dict[frozenset, float],
                   step_mm: float = SAMPLE_STEP_MM,
                   max_convergence: float = MAX_CONVERGENCE
                   ) -> list[BroadsideSamples]:
    """Sample the victim against an aggressor directly above or below it.

    One entry per (victim track, aggressor layer). ``windows_mm`` is keyed by
    `layer_pair_key` and gives each adjacency its own lateral window, which
    matters because the windows differ by a factor of five on a six-layer
    board: the prepreg between In2 and In3 is 0.109 mm and the cores either
    side of them are 0.55 mm. One window sized off the largest would sample
    five times more of the board than the geometry can couple over.

    The measured quantity is the distance from the victim's centreline to the
    aggressor's *centreline*, not to its copper. Two stacked traces overlap, so
    a distance to copper would read zero over the whole overlap and could not
    tell a perfectly stacked pair from one offset by half a trace width — which
    is most of the coupling.
    """
    victim_net = board.net(victim)
    aggressor_net = board.net(aggressor)
    neighbours: dict[str, set[str]] = {}
    for pair in windows_mm:
        upper, lower = sorted(pair, key=board.copper_layers.index)
        neighbours.setdefault(upper, set()).add(lower)
        neighbours.setdefault(lower, set()).add(upper)

    victim_layers = _tracks_by_layer(board, victim_net.code)
    aggressor_layers = _tracks_by_layer(board, aggressor_net.code)

    samples: list[BroadsideSamples] = []
    for layer, entries in victim_layers.items():
        for other in sorted(neighbours.get(layer, ())):
            if not aggressor_layers.get(other):
                continue
            window = windows_mm[layer_pair_key(layer, other)]
            aggressor_lines = _centreline_union(board, aggressor_net.code, other)

            offset = 0.0
            for track in entries:
                line = track.centreline
                victim_width = track.width_mm
                if line.length < step_mm:
                    offset += line.length
                    continue
                points, actual_step = track.sample_points(step_mm)

                lateral = shapely.distance(points, aggressor_lines)
                convergence = np.abs(np.gradient(lateral, actual_step))
                samples.append(BroadsideSamples(
                    layer=layer, aggressor_layer=other, step_mm=actual_step,
                    offset_mm=offset, victim_width_mm=victim_width,
                    lateral_mm=lateral,
                    coupled=((lateral <= window)
                             & (convergence <= max_convergence)),
                    points=points))
                offset += line.length
    return samples


def coupling(board: Board, victim: int | str, aggressor: int | str,
             max_gap_mm: float = DEFAULT_MAX_GAP_MM,
             step_mm: float = SAMPLE_STEP_MM,
             min_run_mm: float = MIN_RUN_LENGTH_MM,
             max_convergence: float = MAX_CONVERGENCE) -> Coupling:
    """Measure how much of ``victim`` runs within ``max_gap_mm`` of ``aggressor``.

    Only same-layer coupling is measured. Broadside coupling (a trace directly
    over another on an adjacent layer) is a real effect but a different
    calculation, and on these stackups the planes between signal layers make it
    largely moot.
    """
    victim_net = board.net(victim)
    aggressor_net = board.net(aggressor)

    victim_layers = _tracks_by_layer(board, victim_net.code)
    aggressor_layers = _tracks_by_layer(board, aggressor_net.code)
    victim_length = sum(t.centreline.length for entries in victim_layers.values()
                        for t in entries)
    aggressor_length = sum(t.centreline.length
                           for entries in aggressor_layers.values()
                           for t in entries)

    runs: list[CoupledRun] = []
    for entry in sampled_gaps(board, victim_net.code, aggressor_net.code,
                              max_gap_mm=max_gap_mm, step_mm=step_mm,
                              max_convergence=max_convergence):
        runs.extend(_runs_from_samples(
            entry.gap_mm, entry.coupled, entry.step_mm, entry.layer,
            entry.offset_mm, min_run_mm, entry.points))

    runs.sort(key=lambda r: -r.length_mm)
    return Coupling(victim=victim_net.name, aggressor=aggressor_net.name,
                    max_gap_mm=max_gap_mm, runs=runs,
                    victim_length_mm=victim_length,
                    aggressor_length_mm=aggressor_length)


# -- differential pairs ---------------------------------------------------

@dataclass
class DifferentialPair:
    """Routing quality of a candidate differential pair."""
    positive: str
    negative: str
    coupling: Coupling
    length_positive_mm: float
    length_negative_mm: float
    target_gap_mm: float | None = None
    #: Why this is believed to be a pair — device pin names, or a net-name
    #: convention. Empty when the caller named the pair itself.
    evidence: list[str] = field(default_factory=list)
    confidence: float | None = None

    @property
    def coupled_fraction(self) -> float:
        return self.coupling.coupled_fraction

    @property
    def skew_mm(self) -> float:
        """Raw length mismatch between the two halves.

        Intra-pair skew, not the propagation-delay skew — converting needs the
        stackup's effective permittivity, which belongs to the physics phase.
        """
        return abs(self.length_positive_mm - self.length_negative_mm)

    @property
    def skew_fraction(self) -> float:
        longer = max(self.length_positive_mm, self.length_negative_mm)
        return self.skew_mm / longer if longer > 0 else 0.0

    @property
    def uncoupled_mm(self) -> float:
        """Length routed as two single-ended traces rather than as a pair."""
        return max(0.0, self.coupling.victim_length_mm
                   - self.coupling.coupled_length_mm)

    @property
    def gap_consistency_mm(self) -> float | None:
        """Spread between the tightest and loosest gap over coupled runs.

        A pair whose gap wanders has an impedance that wanders with it.
        """
        if not self.coupling.runs:
            return None
        return (max(r.max_gap_mm for r in self.coupling.runs)
                - min(r.min_gap_mm for r in self.coupling.runs))

    @property
    def layers(self) -> list[str]:
        return self.coupling.layers

    def summary(self) -> dict:
        return {
            "pair": f"{self.positive} / {self.negative}",
            "coupled_pct": round(self.coupled_fraction * 100, 1),
            "uncoupled_mm": round(self.uncoupled_mm, 3),
            "skew_mm": round(self.skew_mm, 4),
            "length_mm": (round(self.length_positive_mm, 3),
                          round(self.length_negative_mm, 3)),
            "mean_gap_mm": (round(self.coupling.mean_gap_mm, 4)
                            if self.coupling.mean_gap_mm else None),
            "gap_spread_mm": (round(self.gap_consistency_mm, 4)
                              if self.gap_consistency_mm is not None else None),
            "layers": self.layers,
            "runs": len(self.coupling.runs),
        }


def candidate_pair_names(net_names: list[str]) -> list[tuple[str, str]]:
    """Find differential-pair candidates from net naming conventions.

    The weakest of the three ways to find a pair, and the only one that works
    with no schematic. It sees both boards' USB pairs and misses the six-layer board's CAN
    bus entirely, whose nets are unnamed. Prefer
    `intent.propose_differential_pairs`, which reads the *pins*.
    """
    lookup = set(net_names)
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    for name in sorted(net_names):
        if name in seen:
            continue
        for positive_pattern, negative_pattern in _PAIR_SUFFIXES:
            match = positive_pattern.match(name)
            if not match:
                continue
            base = match.group("base")
            # Rebuild the negative name using the same convention.
            for candidate in (f"{base}-", f"{base}_N", f"{base}n", f"{base}N"):
                if candidate in lookup and candidate != name and candidate not in seen:
                    pairs.append((name, candidate))
                    seen.update((name, candidate))
                    break
            if name in seen:
                break
    return pairs


def analyse_pair(board: Board, positive: str, negative: str,
                 max_gap_mm: float = DEFAULT_MAX_GAP_MM,
                 evidence: list[str] | None = None,
                 confidence: float | None = None,
                 **kwargs) -> DifferentialPair:
    """Routing quality for one differential pair."""
    result = coupling(board, positive, negative, max_gap_mm=max_gap_mm, **kwargs)
    return DifferentialPair(
        positive=board.net(positive).name,
        negative=board.net(negative).name,
        coupling=result,
        length_positive_mm=result.victim_length_mm,
        length_negative_mm=result.aggressor_length_mm,
        evidence=evidence or [],
        confidence=confidence,
    )


def find_differential_pairs(board: Board,
                            max_gap_mm: float = DEFAULT_MAX_GAP_MM,
                            schematic=None,
                            **kwargs) -> list[DifferentialPair]:
    """Analyse every routed pair the design says exists.

    Two independent sources, **unioned**, because neither is a superset of the
    other. With a ``schematic``, pairs come from the *pins* they land on — a
    device's own `CANH`/`CANL` or `D+`/`D-` — which finds pairs whose nets
    carry no naming convention at all. Net-name suffixes find pairs whose
    *symbols* carry no pin-name convention, which is the common case once
    custom symbols are involved: an ESP32-C3 symbol naming its USB pins
    `IO18`/`IO19` hides a pair whose nets are literally `/D+` and `/D-`.

    This used to be an ``if``/``else``, so supplying a schematic — which
    `SKILL.md` pushes hard for — turned the net-name detector *off*. On an
    outside board that cost the review its most valuable number (§48.2): the
    pair went undetected, `impedance` examined zero nets, and `crosstalk`
    reported the pair's own intra-pair coupling as two MAJOR findings.

    Pin-name evidence wins where both fire; net-name proposals come in at
    their existing 0.6, which the `confidence < 0.9` demotion downstream
    already handles.
    """
    routed = {n.name for n in board.nets.values() if board.tracks_on_net(n.code)}

    proposals: list[tuple[str, str, list[str], float | None]] = []
    seen: set[frozenset[str]] = set()

    if schematic is not None:
        from pcbreview.intent import infer
        for pair in infer(schematic, board).pairs:
            key = frozenset({pair.positive, pair.negative})
            if key in seen:
                continue
            seen.add(key)
            proposals.append((pair.positive, pair.negative,
                              pair.evidence, pair.confidence))

    for positive, negative in candidate_pair_names(sorted(routed)):
        key = frozenset({positive, negative})
        if key in seen:
            continue
        seen.add(key)
        proposals.append((positive, negative, ["net name convention"], 0.6))

    results = []
    for positive, negative, evidence, confidence in proposals:
        if positive not in routed or negative not in routed:
            continue
        results.append(analyse_pair(board, positive, negative,
                                    max_gap_mm=max_gap_mm, evidence=evidence,
                                    confidence=confidence, **kwargs))
    results.sort(key=lambda p: -p.coupling.victim_length_mm)
    return results


def neighbours_of(board: Board, victim: int | str,
                  max_gap_mm: float = DEFAULT_MAX_GAP_MM,
                  min_coupled_mm: float = 1.0, **kwargs) -> list[Coupling]:
    """Every other net that runs coupled to this one — the crosstalk shortlist.

    Candidates are pre-filtered by a buffered bounding-box intersection so only
    plausible aggressors get the full sampled measurement.
    """
    victim_net = board.net(victim)
    victim_tracks = board.tracks_on_net(victim_net.code)
    if not victim_tracks:
        return []

    victim_layers = {t.layer for t in victim_tracks}
    envelope = unary_union([t.copper for t in victim_tracks]).buffer(max_gap_mm + 0.1)

    results = []
    for net in board.nets.values():
        if net.code == victim_net.code:
            continue
        tracks = board.tracks_on_net(net.code)
        if not tracks or not ({t.layer for t in tracks} & victim_layers):
            continue
        if not any(envelope.intersects(t.copper) for t in tracks):
            continue
        result = coupling(board, victim_net.code, net.code,
                          max_gap_mm=max_gap_mm, **kwargs)
        if result.coupled_length_mm >= min_coupled_mm:
            results.append(result)

    results.sort(key=lambda c: -c.coupled_length_mm)
    return results
