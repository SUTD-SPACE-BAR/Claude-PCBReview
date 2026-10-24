"""Impedance of the copper as it was actually routed.

`fields.py` answers "what is the impedance of *this* cross-section". The fab's
target answers "what is the impedance of the cross-section you were *supposed*
to route". This module measures the cross-section that exists, millimetre by
millimetre, and solves it — which is the whole reason the field solver was
built.

The finding it produces cannot be reached any other way:

    USB_D+/USB_D- targets 90 ohm at 0.2032 mm to the pour.
    7.1 mm of 9.1 mm runs at 0.35 mm or wider -> 96 ohm there.

A DRC cannot see this: every clearance passes. A closed form cannot see it
either, since the coplanar models break down at exactly these gaps
(`fields.py`, module docstring). It needs the routed geometry and a solver that
stays valid across it.

Three measurements are taken along each trace, at 25 um steps:

* **width**, which KiCad already stores per segment;
* **clearance to ground copper on the same layer** — to *ground*, not to any
  copper, because a neighbouring signal trace is coupling rather than a
  coplanar return, and belongs to `coupling.py`;
* for a pair, **edge-to-edge spacing to the other half**.

Samples are then bucketed, because solving a field problem per sample would be
absurd: geometry is quantised to the nearest 25 um and one solve is cached per
distinct cross-section. A typical net needs three to eight solves.

Which layer the cross-section is built against is measured, not assumed. The
adjacent copper layer is often not the reference: on the six-layer board a trace on In2.Cu has
another *signal* layer 0.109 mm below it and the ground plane 0.55 mm above,
and modelling it against the near one puts the answer out by a factor of two.
`layer_context` therefore searches outward for the nearest layer that actually
pours copper over the net (`REFERENCE_COVERAGE_MIN`) and carries the coverage
it found alongside the answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.ops import unary_union

from pcbreview import fields, solvestore
from pcbreview.model import Board

# Sampling step along the trace. Matches `coupling.SAMPLE_STEP_MM`.
SAMPLE_STEP_MM = 0.025

# Geometry is rounded to this before a cross-section is solved, so that a
# clearance wandering between 0.3011 and 0.3043 mm costs one solve, not eighty.
# Well below the ~25 um that changes Z0 by a measurable amount.
QUANTUM_MM = 0.025

# Past this, the pour stops affecting Z0 — the solver shows the coplanar answer
# flat to within 0.1% beyond about 4x the dielectric height. Capping keeps the
# mesh from spanning a huge empty domain to no purpose.
MAX_USEFUL_CLEARANCE_RATIO = 6.0

# A neighbouring copper layer is the reference only if it actually pours over
# the trace. Taking the *adjacent* layer on faith is wrong on any board with two
# signal layers next to each other: the six-layer board's In2.Cu sits 0.109 mm above In3.Cu,
# which is another signal layer covering 30% of the trace, and 0.55 mm below
# In1.Cu, which is a ground plane covering 96%. The plane is the reference; the
# adjacent layer is not.
#
# 0.5 is a discriminator between a plane and incidental copper, not a quality
# metric — `reference.py` owns coverage quality. Measured over both boards, the
# two populations do not overlap anywhere near it: real planes cover 79-100% of
# every net's footprint and signal layers cover 0-32%.
#
# **That gap is a property of the nets this module actually audits, and not of
# boards.** `audit_against_targets` only reaches nets whose width matches a fab
# target to +/-0.02 mm: three nets on the four-layer board, zero on the unreferenced four-layer board.
# Measured instead over *every* (net, signal layer, candidate layer) triple —
# 321, 1060 and 876 of them on the three boards — the distribution is
# continuous straight through 0.5, with 16-28% of samples landing within +/-0.15
# of it and no gap at all. Both original boards behave this way too, so this is
# not something the new board changed.
#
# Nothing is wrong today: on the handful of nets that reach here the call is
# never close. But the moment net selection widens — which is exactly what the
# intent manifest is for, §32.B1 and B7 — this constant starts deciding coin
# flips, and it will need a real discriminator rather than a threshold.
REFERENCE_COVERAGE_MIN = 0.5

# A deviation this large from the target is worth reporting. 10% of a 50 ohm
# line is 0.04 dB of mismatch loss, which is negligible on its own; the number
# is a reporting threshold, not a physical limit, and is a judgement call.
REPORTABLE_DEVIATION = 0.10


class RoutedError(Exception):
    """Raised when a net cannot be analysed as routed."""


@dataclass
class Segment:
    """A stretch of trace with one quantised cross-section."""
    layer: str
    length_mm: float
    width_mm: float
    clearance_mm: float
    spacing_mm: float | None
    z_ohm: float
    epsilon_eff: float
    reference_layer: str
    height_mm: float
    epsilon_r: float
    #: how much of the net's copper on this layer the reference actually covers
    reference_coverage: float | None = None
    #: clearance was capped because the pour is too far to matter
    clearance_capped: bool = False

    def __str__(self) -> str:
        gap = "" if self.spacing_mm is None else f", s={self.spacing_mm:.4f}"
        return (f"{self.length_mm:6.2f} mm on {self.layer}: w={self.width_mm:.4f}"
                f"{gap}, clearance {self.clearance_mm:.4f} -> {self.z_ohm:.1f} ohm")


@dataclass
class RoutedImpedance:
    """What a net's impedance actually is, along its length."""
    net: str
    kind: str                               # single_ended | differential
    segments: list[Segment]
    target_ohm: float | None
    nominal_ohm: float | None               # solved at the target's own geometry
    nominal_clearance_mm: float | None
    unmeasured_mm: float = 0.0              # length on layers with no reference
    warnings: list[str] = field(default_factory=list)

    @property
    def routed_length_mm(self) -> float:
        return sum(s.length_mm for s in self.segments)

    @property
    def mean_ohm(self) -> float:
        """Length-weighted mean impedance over the routed length."""
        if self.routed_length_mm <= 0:
            return float("nan")
        return sum(s.z_ohm * s.length_mm for s in self.segments) / self.routed_length_mm

    @property
    def min_ohm(self) -> float:
        return min((s.z_ohm for s in self.segments), default=float("nan"))

    @property
    def max_ohm(self) -> float:
        return max((s.z_ohm for s in self.segments), default=float("nan"))

    @property
    def reference_ohm(self) -> float | None:
        """What the routed impedance should be measured against.

        The *nominal* where there is one, not the fab's target. Both the
        nominal and every segment come from the same solver, so the model's
        known ~1.5% offset against the fab (`fields`, section on solder mask)
        cancels exactly in the comparison. Measuring the routed value against
        the raw target instead would charge the layout for a modelling bias.
        """
        return self.nominal_ohm if self.nominal_ohm is not None else self.target_ohm

    def length_beyond(self, tolerance: float = REPORTABLE_DEVIATION) -> float:
        """Routed length more than ``tolerance`` away from the reference."""
        reference = self.reference_ohm
        if reference is None:
            return 0.0
        return sum(s.length_mm for s in self.segments
                   if abs(s.z_ohm - reference) / reference > tolerance)

    @property
    def worst_segment(self) -> Segment | None:
        reference = self.reference_ohm
        if not self.segments or reference is None:
            return max(self.segments, key=lambda s: s.length_mm, default=None)
        return max(self.segments, key=lambda s: abs(s.z_ohm - reference))

    @property
    def deviation(self) -> float | None:
        """Length-weighted mean deviation from the reference impedance."""
        reference = self.reference_ohm
        if reference is None or not self.segments:
            return None
        return (self.mean_ohm - reference) / reference

    def summary(self) -> dict:
        return {
            "net": self.net,
            "kind": self.kind,
            "routed_mm": round(self.routed_length_mm, 3),
            "target_ohm": self.target_ohm,
            "nominal_ohm": round(self.nominal_ohm, 2) if self.nominal_ohm else None,
            "mean_ohm": round(self.mean_ohm, 2) if self.segments else None,
            "range_ohm": ((round(self.min_ohm, 2), round(self.max_ohm, 2))
                          if self.segments else None),
            "deviation_pct": (round(self.deviation * 100, 1)
                              if self.deviation is not None else None),
            "off_target_mm": round(self.length_beyond(), 2),
            "segments": len(self.segments),
            "unmeasured_mm": round(self.unmeasured_mm, 3),
            "warnings": self.warnings,
        }

    def report(self) -> str:
        lines = [f"{self.net} ({self.kind})"]
        if self.target_ohm is not None:
            solved = (f", solved {self.nominal_ohm:.1f} ohm"
                      if self.nominal_ohm is not None else "")
            lines.append(
                f"  designed for {self.target_ohm:.0f} ohm at "
                f"{self.nominal_clearance_mm} mm to pour{solved}")
        if not self.segments:
            lines.append("  nothing measurable was routed")
            return "\n".join(lines + [f"  ! {w}" for w in self.warnings])
        lines.append(
            f"  routed {self.routed_length_mm:.2f} mm, "
            f"{self.min_ohm:.1f}-{self.max_ohm:.1f} ohm, "
            f"mean {self.mean_ohm:.1f}")
        if self.deviation is not None:
            lines.append(f"  {self.deviation:+.1%} against the same solver's "
                         f"answer for the intended cross-section")
            off = self.length_beyond()
            if off > 0:
                lines.append(
                    f"  {off:.2f} mm of {self.routed_length_mm:.2f} more than "
                    f"{REPORTABLE_DEVIATION:.0%} away from it")
        for segment in sorted(self.segments, key=lambda s: -s.length_mm)[:6]:
            lines.append(f"    {segment}")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Geometry measurement
# ---------------------------------------------------------------------------

def _ground_copper(board: Board, layer: str, ground_nets: set[str]):
    """All ground copper on one layer: zone fill, tracks and pads.

    Memoised on the board per `(layer, ground nets)`. `analyse_coupling` builds
    this per net *pair*, and it unions every ground track and pad on the layer —
    on a board whose pour is carved up by 366 tracks that is not a small union.
    """

    def build():
        parts = []
        for name in ground_nets:
            fill = board.zone_fill(layer, name)
            if not fill.is_empty:
                parts.append(fill)
        for track in board.tracks:
            if track.layer == layer and board.nets[track.net].name in ground_nets:
                parts.append(track.copper)
        for pad in board.pads:
            if (pad.on_layer(layer) and pad.net in board.nets
                    and board.nets[pad.net].name in ground_nets):
                parts.append(pad.copper)
        return unary_union(parts) if parts else None

    return board.memo(("ground_copper", layer, frozenset(ground_nets)), build)


def footprints_by_layer(board: Board, net: int | str) -> dict[str, object]:
    """The copper a net occupies, per layer — what a reference plane must cover.

    Memoised per net: it depends on nothing else, and the coupling survey asks
    for the same net's footprint once for every aggressor it is measured against.
    """
    code = board.net(net).code

    def build():
        by_layer: dict[str, list] = {}
        for track in board.tracks_on_net(code):
            if track.length_mm > 0:
                by_layer.setdefault(track.layer, []).append(track.copper)
        return {layer: unary_union(parts) for layer, parts in by_layer.items()}

    return board.memo(("footprints_by_layer", code), build)


def clearance_profile(board: Board, net: int | str, ground_nets: set[str],
                      step_mm: float = SAMPLE_STEP_MM
                      ) -> dict[str, list[tuple[float, float, float]]]:
    """Per-layer samples of ``(step_length, width, clearance_to_ground)``.

    Clearance is edge to edge: distance from the sampled centreline point to
    the ground copper, less the trace's own half-width. Measured to *ground*
    only — a neighbouring signal trace changes the impedance too, but that is
    coupling and belongs to `coupling.py`, not to the coplanar return.
    """
    net_obj = board.net(net)
    tracks = [t for t in board.tracks_on_net(net_obj.code) if t.length_mm > 0]
    if not tracks:
        raise RoutedError(f"net {net_obj.name} has no routed copper")

    by_layer: dict[str, list[tuple[float, float, float]]] = {}
    ground_cache: dict[str, object] = {}

    for track in tracks:
        if track.layer not in ground_cache:
            ground_cache[track.layer] = _ground_copper(
                board, track.layer, ground_nets)
        ground = ground_cache[track.layer]
        if ground is None:
            by_layer.setdefault(track.layer, []).append(
                (track.length_mm, track.width_mm, float("inf")))
            continue

        points, actual_step = track.sample_points(step_mm)
        distances = shapely.distance(points, ground)
        clearances = np.maximum(distances - track.width_mm / 2.0, 0.0)
        by_layer.setdefault(track.layer, []).extend(
            (actual_step, track.width_mm, float(c)) for c in clearances)

    return by_layer


def _pair_profile(board: Board, positive: int | str, negative: int | str,
                  ground_nets: set[str], step_mm: float = SAMPLE_STEP_MM
                  ) -> dict[str, list[tuple[float, float, float, float]]]:
    """As `clearance_profile`, plus edge-to-edge spacing to the other half."""
    positive_net = board.net(positive)
    negative_net = board.net(negative)
    tracks = [t for t in board.tracks_on_net(positive_net.code) if t.length_mm > 0]
    if not tracks:
        raise RoutedError(f"net {positive_net.name} has no routed copper")

    by_layer: dict[str, list[tuple[float, float, float, float]]] = {}
    ground_cache: dict[str, object] = {}
    partner_cache: dict[str, object] = {}

    for track in tracks:
        layer = track.layer
        if layer not in ground_cache:
            ground_cache[layer] = _ground_copper(board, layer, ground_nets)
            partner = [t.copper for t in board.tracks_on_net(negative_net.code)
                       if t.layer == layer]
            partner_cache[layer] = unary_union(partner) if partner else None
        ground, partner = ground_cache[layer], partner_cache[layer]
        if partner is None:
            continue

        points, actual_step = track.sample_points(step_mm)
        spacing = np.maximum(
            shapely.distance(points, partner) - track.width_mm / 2.0, 0.0)
        if ground is None:
            clearance = np.full(len(spacing), float("inf"))
        else:
            clearance = np.maximum(
                shapely.distance(points, ground) - track.width_mm / 2.0, 0.0)

        by_layer.setdefault(layer, []).extend(
            (actual_step, track.width_mm, float(c), float(s))
            for c, s in zip(clearance, spacing))

    return by_layer


# ---------------------------------------------------------------------------
# Stackup context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LayerContext:
    """Where a signal layer sits: its planes, dielectrics and whether it is outer."""
    reference_layer: str
    height_mm: float
    epsilon_r: float
    is_outer: bool
    thickness_mm: float
    second_reference: str | None = None
    second_height_mm: float | None = None
    #: how much of the net's footprint each reference layer actually pours over
    reference_coverage: float | None = None
    second_coverage: float | None = None
    #: kept a tuple, not a list — this is a cache key in `_SolveCache`
    warnings: tuple[str, ...] = ()


def plane_copper(board: Board, layer: str, cache: dict | None = None):
    """Plane-like copper on a layer: every net's zone fill, plus wide tracks.

    Deliberately net-agnostic. Return current cares first about whether there
    is *copper* under the trace at all; which net owns it is the next question
    and `reference.py` owns that one. A trace running over a fat power track is
    referenced as well as one over a poured zone, which is why tracks are
    included above the same 0.5 mm width `reference.candidate_nets_on` uses.
    """
    if cache is not None and layer in cache:
        return cache[layer]

    def build():
        parts = []
        fill = board.zone_fill(layer)
        if not fill.is_empty:
            parts.append(fill)
        for track in board.tracks:
            if track.layer == layer and track.width_mm >= 0.5:
                parts.append(track.copper)
        return unary_union(parts) if parts else None

    # Memoised on the board whether or not the caller brought a cache. The
    # caller-supplied `cache` is a *survey*-scoped dict and `analyse_coupling`
    # and `analyse_broadside` each create a fresh one per net pair — so on the
    # board this was measured on, four layers' plane copper was rebuilt 223
    # times over 40 pairs, 8.5% of the whole runtime, for four distinct answers.
    geometry = board.memo(("plane_copper", layer), build)
    if cache is not None:
        cache[layer] = geometry
    return geometry


def plane_coverage(board: Board, layer: str, footprint,
                   cache: dict | None = None) -> float:
    """Fraction of a net's footprint that has plane-like copper on ``layer``."""
    plane = plane_copper(board, layer, cache)
    if plane is None or plane.is_empty or footprint.area <= 0:
        return 0.0
    return footprint.intersection(plane).area / footprint.area


def _first_reference(board: Board, index: int, step: int, footprint,
                     cache: dict | None) -> tuple[str | None, float | None, bool]:
    """Nearest copper layer in one direction that is actually a plane there.

    Returns ``(layer, coverage, is_fallback)``. Without a footprint this is
    just the adjacent layer, which is the geometry-blind answer and the one
    that was wrong on the six-layer board — see the note on `REFERENCE_COVERAGE_MIN`.
    """
    position = index + step
    first: str | None = None
    while 0 <= position < len(board.copper_layers):
        candidate = board.copper_layers[position]
        if first is None:
            first = candidate
        if footprint is None:
            return candidate, None, False
        coverage = plane_coverage(board, candidate, footprint, cache)
        if coverage >= REFERENCE_COVERAGE_MIN:
            return candidate, coverage, False
        position += step
    if first is None:
        return None, None, False
    coverage = (None if footprint is None
                else plane_coverage(board, first, footprint, cache))
    return first, coverage, True


def layer_context(board: Board, layer: str, footprint=None,
                  plane_cache: dict | None = None) -> LayerContext | None:
    """Resolve a signal layer's reference planes from the bound stackup.

    Pass ``footprint`` — the copper the trace occupies — and the reference is
    the nearest layer that is *actually a plane underneath it*, which is not
    always the adjacent one. Without it the adjacent copper layer is assumed to
    be a plane, which is the geometry-blind answer.
    """
    stackup = board.stackup
    if stackup is None:
        raise RoutedError(
            "routed impedance needs a bound stackup — call board.bind_stackup()")

    index = board.layer_index(layer)
    if len(board.copper_layers) < 2:
        return None
    is_outer = index == 0 or index == len(board.copper_layers) - 1

    above, above_coverage, above_fallback = _first_reference(
        board, index, -1, footprint, plane_cache)
    below, below_coverage, below_fallback = _first_reference(
        board, index, +1, footprint, plane_cache)
    if above is None and below is None:
        return None

    warnings: list[str] = []
    for side, name, coverage, fallback in (
            ("above", above, above_coverage, above_fallback),
            ("below", below, below_coverage, below_fallback)):
        if fallback and name is not None:
            warnings.append(
                f"no layer {side} {layer} pours copper over this net; "
                f"{name} assumed as the reference at {coverage:.0%} coverage"
                if coverage is not None else
                f"no layer {side} {layer} pours copper over this net; "
                f"{name} assumed as the reference")

    to_stackup = {stackup.resolve_layer(l.name): l.name
                  for l in stackup.copper_layers}
    copper = next((l for l in stackup.copper_layers
                   if l.name == to_stackup.get(layer)), None)
    thickness = copper.thickness_mm if copper else 0.035

    def dielectric(other):
        return stackup.dielectric_between(to_stackup[layer], to_stackup[other])

    if is_outer:
        reference, coverage = ((below, below_coverage) if index == 0
                               else (above, above_coverage))
        if reference is None:
            return None
        height, epsilon = dielectric(reference)
        return LayerContext(reference_layer=reference, height_mm=height,
                            epsilon_r=epsilon, is_outer=True,
                            thickness_mm=thickness,
                            reference_coverage=coverage,
                            warnings=tuple(warnings))

    height_above, epsilon_above = dielectric(above)
    height_below, epsilon_below = dielectric(below)
    # Name the nearer plane first; both are carried, since a stripline needs
    # each side's height separately.
    if height_below <= height_above:
        return LayerContext(reference_layer=below, height_mm=height_below,
                            epsilon_r=(epsilon_above + epsilon_below) / 2.0,
                            is_outer=False, thickness_mm=thickness,
                            second_reference=above,
                            second_height_mm=height_above,
                            reference_coverage=below_coverage,
                            second_coverage=above_coverage,
                            warnings=tuple(warnings))
    return LayerContext(reference_layer=above, height_mm=height_above,
                        epsilon_r=(epsilon_above + epsilon_below) / 2.0,
                        is_outer=False, thickness_mm=thickness,
                        second_reference=below, second_height_mm=height_below,
                        reference_coverage=above_coverage,
                        second_coverage=below_coverage,
                        warnings=tuple(warnings))


@dataclass(frozen=True)
class BroadsideContext:
    """Two adjacent copper layers with no plane between them.

    The geometry a broadside pair forms. ``separation_mm`` is not a routing
    choice — it is the prepreg or core between two signal layers — so unlike
    every other coupled section here it is fixed for the whole board, and the
    only thing the layout varies is how far apart the two centrelines sit
    laterally.

    Each layer keeps the distance to the plane on its *own* far side, because
    that is what `fields.broadside_section` asks for. ``upper_height_mm`` is
    ``None`` when the upper layer is outer, which is the air-above case.

    Frozen, and every field hashable, because this is a `SolveCache` key.
    """
    upper_layer: str
    lower_layer: str
    separation_mm: float
    epsilon_r: float
    thickness_mm: float
    upper_reference: str | None = None
    upper_height_mm: float | None = None
    lower_reference: str | None = None
    lower_height_mm: float | None = None
    warnings: tuple[str, ...] = ()

    def far_height_mm(self, layer: str) -> float | None:
        """Distance from ``layer`` to the plane on its far side from the other."""
        if layer == self.upper_layer:
            return self.upper_height_mm
        if layer == self.lower_layer:
            return self.lower_height_mm
        raise RoutedError(f"{layer} is not part of this broadside pair")

    def other(self, layer: str) -> str:
        return self.lower_layer if layer == self.upper_layer else self.upper_layer


def adjacent_signal_layers(board: Board) -> list[tuple[str, str]]:
    """Adjacent copper layer pairs with no plane between them.

    A pair is only broadside-coupled if neither layer shields the other, and
    adjacency is exactly that condition: any layer *between* two others is
    either a plane, which shields completely, or a third signal layer, which
    is not a geometry these stackups produce.

    Whether each layer is a plane is decided per net by
    `_first_reference`, so this returns every adjacent pair and lets the
    caller's own routing decide — a layer with no tracks on it simply yields
    no runs.
    """
    layers = board.copper_layers
    return [(layers[i], layers[i + 1]) for i in range(len(layers) - 1)]


def broadside_context(board: Board, upper_layer: str, lower_layer: str,
                      upper_footprint=None, lower_footprint=None,
                      plane_cache: dict | None = None
                      ) -> BroadsideContext | None:
    """Resolve the stackup geometry two adjacent signal layers form.

    Returns ``None`` when the two layers are not adjacent, or when neither has
    a reference plane to speak of.
    """
    stackup = board.stackup
    if stackup is None:
        raise RoutedError(
            "broadside geometry needs a bound stackup — call board.bind_stackup()")

    upper_index = board.layer_index(upper_layer)
    lower_index = board.layer_index(lower_layer)
    if lower_index != upper_index + 1:
        return None

    to_stackup = {stackup.resolve_layer(l.name): l.name
                  for l in stackup.copper_layers}
    separation, epsilon = stackup.dielectric_between(
        to_stackup[upper_layer], to_stackup[lower_layer])
    if separation <= 0:
        return None

    copper = next((l for l in stackup.copper_layers
                   if l.name == to_stackup.get(upper_layer)), None)
    thickness = copper.thickness_mm if copper else 0.035

    warnings: list[str] = []

    def far_plane(layer, index, step, footprint):
        """The plane on ``layer``'s far side, and its distance."""
        if not (0 <= index + step < len(board.copper_layers)):
            return None, None                     # outer layer: air out there
        name, coverage, fallback = _first_reference(
            board, index, step, footprint, plane_cache)
        if name is None:
            return None, None
        if fallback:
            warnings.append(
                f"nothing beyond {layer} pours over this net; {name} assumed "
                "as its reference")
        height, _ = stackup.dielectric_between(to_stackup[layer],
                                               to_stackup[name])
        return name, height

    upper_reference, upper_height = far_plane(
        upper_layer, upper_index, -1, upper_footprint)
    lower_reference, lower_height = far_plane(
        lower_layer, lower_index, +1, lower_footprint)
    if upper_reference is None and lower_reference is None:
        return None

    return BroadsideContext(
        upper_layer=upper_layer, lower_layer=lower_layer,
        separation_mm=separation, epsilon_r=epsilon, thickness_mm=thickness,
        upper_reference=upper_reference, upper_height_mm=upper_height,
        lower_reference=lower_reference, lower_height_mm=lower_height,
        warnings=tuple(sorted(set(warnings))))


# ---------------------------------------------------------------------------
# Solving the routed geometry
# ---------------------------------------------------------------------------

def _quantise(value: float, quantum: float = QUANTUM_MM) -> float:
    return round(round(value / quantum) * quantum, 6)


def _section_for(context: LayerContext, width_mm: float, clearance_mm: float,
                 spacing_mm: float | None,
                 mask: fields.SolderMask | None) -> fields.CrossSection:
    gap = None if math.isinf(clearance_mm) else clearance_mm
    if context.is_outer:
        if spacing_mm is None:
            return fields.microstrip_section(
                width_mm, context.height_mm, context.thickness_mm,
                context.epsilon_r, gap_to_pour_mm=gap, mask=mask)
        return fields.differential_section(
            width_mm, spacing_mm, context.height_mm, context.thickness_mm,
            context.epsilon_r, gap_to_pour_mm=gap, mask=mask)
    if spacing_mm is not None:
        return fields.coupled_stripline_section(
            width_mm, width_mm, spacing_mm, context.height_mm,
            context.second_height_mm or context.height_mm,
            context.thickness_mm, context.epsilon_r, gap_to_pour_mm=gap)
    return fields.stripline_section(
        width_mm, context.height_mm, context.second_height_mm or context.height_mm,
        context.thickness_mm, context.epsilon_r, gap_to_pour_mm=gap)


class _SolveCache:
    """One field solve per distinct quantised cross-section.

    Backed by `solvestore` when a review has attached one, so a cross-section
    solved for a previous review — or for the crosstalk survey on this one, or
    on another board sharing the stackup — costs a lookup. The two caches key
    on different things (a request here, a content hash there) and that is
    deliberate: this one collapses the walk, that one outlives the process.
    """

    def __init__(self, refinement: float, mask: fields.SolderMask | None):
        self.refinement = refinement
        self.mask = mask
        self._cache: dict[tuple, fields.FieldSolution] = {}

    def solve(self, context: LayerContext, width_mm: float, clearance_mm: float,
              spacing_mm: float | None) -> fields.FieldSolution:
        key = (context, width_mm, clearance_mm, spacing_mm)
        if key not in self._cache:
            section = _section_for(context, width_mm, clearance_mm, spacing_mm,
                                   self.mask)
            self._cache[key] = solvestore.solve(section, self.refinement)
        return self._cache[key]

    @property
    def solves(self) -> int:
        return len(self._cache)


def _cap_clearance(clearance_mm: float, context: LayerContext) -> tuple[float, bool]:
    limit = MAX_USEFUL_CLEARANCE_RATIO * context.height_mm
    if math.isinf(clearance_mm) or clearance_mm > limit:
        return _quantise(limit), True
    return _quantise(clearance_mm), False


def analyse_net(board: Board, net: int | str, ground_nets: set[str],
                target_ohm: float | None = None,
                nominal_clearance_mm: float | None = None,
                mask: fields.SolderMask | None = fields.JLC_SOLDER_MASK,
                refinement: float = 1.5,
                step_mm: float = SAMPLE_STEP_MM) -> RoutedImpedance:
    """Single-ended impedance of a net, as routed.

    ``refinement`` defaults coarser than `fields.solve` does, because this runs
    many solves and the *differences* between segments are what matter here.
    The absolute bias from a coarse mesh is under 1% and applies equally to the
    nominal, so the comparison stays sound.
    """
    net_obj = board.net(net)
    profile = clearance_profile(board, net_obj.code, ground_nets, step_mm)
    cache = _SolveCache(refinement, mask)

    buckets: dict[tuple, float] = {}
    warnings: list[str] = []
    unmeasured = 0.0
    contexts: dict[str, LayerContext] = {}
    footprints = footprints_by_layer(board, net_obj.code)
    plane_cache: dict = {}

    for layer, samples in profile.items():
        context = layer_context(board, layer, footprints.get(layer), plane_cache)
        if context is None:
            unmeasured += sum(length for length, _, _ in samples)
            warnings.append(f"{layer} has no reference plane; skipped")
            continue
        contexts[layer] = context
        warnings.extend(context.warnings)
        for length, width, clearance in samples:
            capped_clearance, capped = _cap_clearance(clearance, context)
            key = (layer, _quantise(width), capped_clearance, capped)
            buckets[key] = buckets.get(key, 0.0) + length

    segments: list[Segment] = []
    for (layer, width, clearance, capped), length in sorted(buckets.items()):
        context = contexts[layer]
        solution = cache.solve(context, width, clearance, None)
        segments.append(Segment(
            layer=layer, length_mm=length, width_mm=width,
            clearance_mm=clearance, spacing_mm=None,
            z_ohm=solution.z0_ohm, epsilon_eff=solution.epsilon_eff,
            reference_layer=context.reference_layer, height_mm=context.height_mm,
            epsilon_r=context.epsilon_r,
            reference_coverage=context.reference_coverage,
            clearance_capped=capped))

    nominal = None
    if target_ohm is not None and nominal_clearance_mm is not None and contexts:
        context = contexts[max(profile, key=lambda l: len(profile[l]))]
        widths = {s.width_mm: 0.0 for s in segments}
        for segment in segments:
            widths[segment.width_mm] += segment.length_mm
        dominant_width = max(widths, key=widths.get)
        nominal = cache.solve(context, dominant_width,
                              _quantise(nominal_clearance_mm), None).z0_ohm

    return RoutedImpedance(
        net=net_obj.name, kind="single_ended", segments=segments,
        target_ohm=target_ohm, nominal_ohm=nominal,
        nominal_clearance_mm=nominal_clearance_mm,
        unmeasured_mm=unmeasured, warnings=warnings)


def analyse_pair(board: Board, positive: int | str, negative: int | str,
                 ground_nets: set[str], target_ohm: float | None = None,
                 nominal_clearance_mm: float | None = None,
                 nominal_spacing_mm: float | None = None,
                 mask: fields.SolderMask | None = fields.JLC_SOLDER_MASK,
                 refinement: float = 1.5,
                 step_mm: float = SAMPLE_STEP_MM) -> RoutedImpedance:
    """Differential impedance of a pair, as routed.

    Both the spacing between the halves and the clearance to the pour are
    measured along the positive half. Where the two run far apart they are not
    a pair there at all, and the solver would be answering a question about a
    structure that does not exist — so samples wider than four times the
    dielectric height are reported as uncoupled length rather than solved.
    """
    positive_net = board.net(positive)
    negative_net = board.net(negative)
    profile = _pair_profile(board, positive_net.code, negative_net.code,
                            ground_nets, step_mm)
    cache = _SolveCache(refinement, mask)

    buckets: dict[tuple, float] = {}
    warnings: list[str] = []
    unmeasured = 0.0
    contexts: dict[str, LayerContext] = {}
    footprints = footprints_by_layer(board, positive_net.code)
    plane_cache: dict = {}

    for layer, samples in profile.items():
        context = layer_context(board, layer, footprints.get(layer), plane_cache)
        if context is None:
            unmeasured += sum(length for length, _, _, _ in samples)
            warnings.append(f"{layer} has no reference plane; skipped")
            continue
        contexts[layer] = context
        warnings.extend(context.warnings)
        spacing_limit = 4.0 * context.height_mm
        for length, width, clearance, spacing in samples:
            if spacing > spacing_limit:
                unmeasured += length
                continue
            capped_clearance, capped = _cap_clearance(clearance, context)
            key = (layer, _quantise(width), capped_clearance,
                   _quantise(spacing), capped)
            buckets[key] = buckets.get(key, 0.0) + length

    if unmeasured > 0:
        warnings.append(
            f"{unmeasured:.2f} mm routed too far apart to be a pair; "
            f"not included in the impedance")

    segments: list[Segment] = []
    for (layer, width, clearance, spacing, capped), length in sorted(buckets.items()):
        context = contexts[layer]
        solution = cache.solve(context, width, clearance, spacing)
        segments.append(Segment(
            layer=layer, length_mm=length, width_mm=width,
            clearance_mm=clearance, spacing_mm=spacing,
            z_ohm=solution.z_differential_ohm,
            epsilon_eff=solution.epsilon_eff,
            reference_layer=context.reference_layer, height_mm=context.height_mm,
            epsilon_r=context.epsilon_r,
            reference_coverage=context.reference_coverage,
            clearance_capped=capped))

    nominal = None
    if (target_ohm is not None and nominal_clearance_mm is not None
            and nominal_spacing_mm is not None and contexts and segments):
        context = contexts[max(profile, key=lambda l: len(profile[l]))]
        widths: dict[float, float] = {}
        for segment in segments:
            widths[segment.width_mm] = widths.get(segment.width_mm, 0.0) + segment.length_mm
        dominant_width = max(widths, key=widths.get)
        nominal = cache.solve(context, dominant_width,
                              _quantise(nominal_clearance_mm),
                              _quantise(nominal_spacing_mm)).z_differential_ohm

    return RoutedImpedance(
        net=f"{positive_net.name} / {negative_net.name}", kind="differential",
        segments=segments, target_ohm=target_ohm, nominal_ohm=nominal,
        nominal_clearance_mm=nominal_clearance_mm,
        unmeasured_mm=unmeasured, warnings=warnings)


# ---------------------------------------------------------------------------
# Driving it from the stackup's own targets
# ---------------------------------------------------------------------------

def audit_against_targets(board: Board, ground_nets: set[str],
                          pairs: list[tuple[str, str]] | None = None,
                          refinement: float = 1.5) -> list[RoutedImpedance]:
    """Check every routed net on a target's layer against that target.

    The fab's targets state a layer, a width and an assumed clearance. Any net
    routed at that width on that layer was meant to hit that impedance, so it
    can be checked without any further intent data — which makes this useful
    before the Net Intent Manifest exists.
    """
    stackup = board.stackup
    if stackup is None:
        raise RoutedError("audit needs a bound stackup")

    to_board = {stackup.resolve_layer(l.name): l.name
                for l in stackup.copper_layers}
    board_layer = {v: k for k, v in to_board.items()}

    results: list[RoutedImpedance] = []
    seen: set[str] = set()

    # Differential targets first, so that a net already audited as half of a
    # pair is not audited again single-ended. A pair's halves *do* each have a
    # single-ended impedance, but reporting both invites the reader to treat
    # one finding as two.
    # Mirrored targets last within their kind, so a net that matches a stated
    # target is never claimed by a derived one. On a symmetric stack the two
    # are the same geometry (the solver gives 49.301 Ohm for both halves of
    # the six-layer stackup here), but the stated row is the fab's own and
    # should be the one a finding cites.
    ordered = sorted(stackup.impedance_targets + stackup.mirrored_targets(),
                     key=lambda t: (t.kind != "differential", not t.is_stated))

    for target in ordered:
        layer = board_layer.get(target.signal_layer)
        if layer is None:
            continue

        if target.kind == "differential":
            for positive, negative in (pairs or []):
                key = f"{positive}/{negative}"
                if key in seen:
                    continue
                tracks = board.tracks_on_net(board.net(positive).code)
                if not any(t.layer == layer for t in tracks):
                    continue
                seen.update((key, positive, negative))
                results.append(_note_mirror(analyse_pair(
                    board, positive, negative, ground_nets,
                    target_ohm=target.impedance_ohm,
                    nominal_clearance_mm=target.trace_to_copper_mm,
                    nominal_spacing_mm=target.trace_spacing_mm,
                    refinement=refinement), target))
            continue

        tolerance = 0.02
        for net in board.nets.values():
            if not net.name or net.name in seen:
                continue
            tracks = [t for t in board.tracks_on_net(net.code) if t.layer == layer]
            if not tracks:
                continue
            matching = sum(t.length_mm for t in tracks
                           if abs(t.width_mm - target.trace_width_mm) <= tolerance)
            if matching < 1.0:
                continue
            seen.add(net.name)
            results.append(_note_mirror(analyse_net(
                board, net.code, ground_nets,
                target_ohm=target.impedance_ohm,
                nominal_clearance_mm=target.trace_to_copper_mm,
                refinement=refinement), target))

    results.sort(key=lambda r: -(abs(r.deviation or 0.0) * r.routed_length_mm))
    return results


def _note_mirror(result: RoutedImpedance, target) -> RoutedImpedance:
    """Say on the finding when the target was mirrored rather than stated.

    A derived target is as good as a stated one on a symmetric stack — it is
    the same cross-section — but the reader is entitled to know the fab did
    not write this row, and to check the symmetry claim themselves.
    """
    if not target.is_stated:
        result.warnings.append(
            f"the {target.impedance_ohm:.0f} ohm target for "
            f"{target.signal_layer} was mirrored from the fab's "
            f"{target.derived_from} row: this stackup is symmetric, so the two "
            f"cross-sections are identical and the fab's own number applies to "
            f"both. The fab states one row per unique geometry and never "
            f"repeats it for the far side")
    return result
