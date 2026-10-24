"""The full audit: every check, ranked, with the evidence attached.

This is the entry point everything else exists for. The alternative shape — a
library of fifty functions for a caller to choose between and stitch together —
relocates the thinking rather than removing it, and the thinking is the part
that should be deterministic. So one call runs everything the board's data
supports and returns findings in priority order.

What a finding is
-----------------
A number, the geometry it came from, and why it was believed. That is the shape
`routed.RoutedImpedance` and `intent.NetRole` already have and the one every
check here produces:

    /UART2 RX -- return path detours around 28.3 mm of missing plane
      coverage 37%, worst detour 12.0 mm at (163.4, 92.1) on In3.Cu
      basis: trace centreline intersected with the poured plane below it;
             antipads credited as covered, detour measured around the hole
      intent: severity depends on this net's edge rate

Nothing here says "possible EMC issue". Either a quantity was computed from the
board's geometry, in which case it is reported with its value and its
derivation, or it was not, in which case the check says it was skipped and why.

Severity, and why it is provisional
-----------------------------------
Severity without intent is guesswork, and that is not a limitation to be
apologised for — it is the central design claim of this tool. A 40 mm run with
a mediocre return path is a defect on a 1.575 GHz GPS feed and irrelevant on an
I2C line, and *nothing in the layout says which is which*. So:

* every check assigns severity from a **stated threshold on a computed
  quantity**, never from a vibe;
* every finding whose severity would move given a net's role, edge rate or
  current carries `intent_needed` saying exactly which datum;
* `score` is "how many times over its own threshold this finding is", which is
  the only cross-check ordering that means anything before the intent manifest
  exists.

Claude's job is that adjudication, and it is the only part of the review that
is not deterministic.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from pcbreview import (ampacity as ampacity_module,
                       coupling as coupling_module, crosstalk as crosstalk_module,
                       bom as bom_module, current as current_module, drc,
                       heatsink as heatsink_module, intent as intent_module,
                       netclass as netclass_module,
                       review as review_module,
                       panel, parity as parity_module,
                       powerpaths as powerpaths_module,
                       physics, placement as placement_module,
                       reference as reference_module, routed, solvepool,
                       solvestore, thermal as thermal_module, topology)
from pcbreview.findings import (SEVERITY_ORDER, AuditError, AuditReport,
                                CheckResult, Finding, _severity)
from pcbreview.model import Board
from pcbreview.schematic import Schematic, netlist
from shapely.geometry import MultiPoint
from shapely.ops import unary_union

# ---------------------------------------------------------------------------
# Thresholds
#
# Every one of these is a reporting threshold on a computed quantity, and every
# one is a judgement call rather than a derived limit. They are gathered here
# rather than buried in the checks so that the whole set can be argued with in
# one place — and so the intent manifest can eventually replace them per net
# class, which is the point of writing them down.
# ---------------------------------------------------------------------------

#: impedance, as a fraction of the *nominal* the same solver gives for the
#: intended cross-section (never the fab's raw target — see `routed`)
IMPEDANCE_MINOR = 0.10
IMPEDANCE_MAJOR = 0.20

#: return path: routed length whose return current detours significantly
RETURN_PATH_MINOR_MM = 2.0
RETURN_PATH_MAJOR_MM = 10.0

#: a layer transition's stitching via or bridging capacitor. Unlike the rest of
#: these, this pair is *derived* rather than chosen: the distance at which a
#: discontinuity stops being electrically small is a twentieth of the knee
#: wavelength, so the threshold follows the edge rate the audit is running at —
#: 14 mm at 1 ns, 4 mm at 300 ps. Major is twice minor.
STITCH_LIMIT_FRACTION = 1 / 20

#: differential pairs
PAIR_COUPLED_MINOR = 0.90
PAIR_COUPLED_MAJOR = 0.50
PAIR_SKEW_MINOR_MM = 0.50
PAIR_SKEW_MAJOR_MM = 2.00

#: crosstalk, as a fraction of the aggressor's swing at the reference edge rate
CROSSTALK_MINOR = 0.02
CROSSTALK_MAJOR = 0.05

#: a rail that necks down to this fraction of its own typical width
NECK_FRACTION = 0.5
NECK_MIN_LENGTH_MM = 1.0

#: how far KiCad's own board stackup may drift from the fab's before it is
#: worth telling the designer. Any disagreement at all matters in principle;
#: below 25% it is a rounding difference between a nominal and a shipped
#: laminate, and above 50% one of the two is a placeholder rather than a stack.
STACKUP_MISMATCH_MINOR = 0.25
STACKUP_MISMATCH_MAJOR = 0.50

#: how far a terminal's feed may sit from its own net's median before it is
#: worth reporting. Relative on purpose: an absolute capacity needs a current,
#: and a current is intent, but a board that feeds eight identical channels
#: from one source states its own expectation. Measured over five boards and
#: 147 terminal feeds: p50 1.00, p90 1.81, **p95 2.30**, p99 9.31, max 11.56 —
#: nine above 2x, six above 3x, two above 6x. 3x sits above everything the
#: normal population does and below every outlier found so far.
FEED_RATIO_MINOR = 3.0
FEED_RATIO_MAJOR = 6.0

#: **Comparable FEEDS**, not terminals — the name is a mistake kept because it
#: is in shipped output, and getting it backwards cost a third of this check's
#: candidate population. One terminal is the source, so a net needs
#: `current.MIN_TERMINALS` = 4 terminals to yield this many feeds, and
#: `current.coverage()` counted candidates at three until it was measured.
#:
#: Three is the floor for the same reason a median needs a middle, and the
#: arithmetic says two can never work rather than merely usually not: with
#: feeds `a <= b` the median is `(a + b) / 2`, so the worst ratio is
#: `2b / (a + b)` — bounded above by 2, and `FEED_RATIO_MINOR` is 3. A
#: two-feed net cannot produce a finding however bad it is, so lowering this
#: to 2 would add noise and no findings. `current.MIN_TERMINALS` carries the
#: full derivation.
FEED_MIN_TERMINALS = 3

#: how far a power net's current capacity may vary along its own length before
#: it is worth reporting. The brief: a net widened for current that then
#: necks is telling you two different things about what it carries. Measured
#: over five boards and 24 wide nets with a real width step: p50 1.94,
#: p90 3.21, max 5.46 — so 2.0 sits at the median and 3.0 near the top decile.
#: Distinct from `conductor_neck`, which needs the narrow stretch to *strand*
#: pads; a net that necks mid-run is invisible to that and visible to this.
CAPACITY_SPREAD_MINOR = 2.0
CAPACITY_SPREAD_MAJOR = 3.0

#: dissipation at which a part's heat path out of its package is worth
#: reporting on. Judgement calls (§9), declared as such: there is no standard
#: that sets them. 1 W in a bottom-cooled package with no via in its tab is a
#: real defect on any board; 0.25 W is where it stops being worth a line.
HEAT_PATH_MINOR_W = 0.25
HEAT_PATH_MAJOR_W = 1.0

#: attached copper, as a fraction of one JESD51-7 plane layer, below which the
#: quoted theta_JA is being applied to a board nothing like the one it was
#: measured on. 0.25 is a quarter of JEDEC's card and still generous.
HEAT_SPREAD_MINOR_FRACTION = 0.25
HEAT_SPREAD_MAJOR_FRACTION = 0.10

#: a pair shorter than this is a fanout, not a routed pair. Nothing is coupled
#: over a millimetre and a half of escape from a BGA, and calling that "0%
#: coupled" is technically true and useless.
PAIR_MIN_LENGTH_MM = 5.0


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _impedance_geometry(board: Board, result):
    """The copper of the stretches that are off target, for the renderer.

    `routed` buckets its samples by (layer, quantised width, clearance) and
    **discards their positions**, which is why this check was the last one with
    no `Finding.geometry` (§32.B3). The positions are recoverable by joining
    back on the part of the key a track carries: its layer and its width.

    Honest about what that costs. Two stretches with the same width on the same
    layer but different clearance to the pour land in different buckets and
    cannot be told apart here, so a drawn stretch may include copper that was
    on target. It is evidence for a human, not a measurement — every number in
    the finding still comes from the bucketed solve.
    """
    if result.nominal_ohm is None or not result.segments:
        return None
    off_target = {(s.layer, routed._quantise(s.width_mm)) for s in result.segments
                  if abs(s.z_ohm - result.nominal_ohm) / result.nominal_ohm
                  >= routed.REPORTABLE_DEVIATION}
    if not off_target:
        return None
    names = set(result.net.split(" / "))
    codes = {board.net(n).code for n in names if n in
             {x.name for x in board.nets.values()}}
    parts = [track.copper for track in board.tracks
             if track.net in codes
             and (track.layer, routed._quantise(track.width_mm)) in off_target]
    if not parts:
        return None
    return unary_union(parts)


def check_impedance(board: Board, ground_nets: set[str],
                    pairs: list[tuple[str, str]],
                    examined: list[str] | None = None) -> list[Finding]:
    """Impedance as routed, against the cross-section that was intended.

    Only nets a fab target was meant for are audited, which is the one piece of
    intent available without a manifest: the fab was told a layer, a width and
    a clearance, so any net routed that way was asking for that impedance.

    `examined` collects every net a target actually matched, so the coverage
    line can say `0 net(s) examined` rather than only how many targets were
    loaded — a distinction that read as a pass on an outside board (§48.3).
    """
    findings: list[Finding] = []
    for result in routed.audit_against_targets(board, ground_nets, pairs=pairs):
        if examined is not None:
            examined.append(result.net)
        if result.deviation is None or not result.segments:
            if result.warnings:
                findings.append(Finding(
                    check="impedance", severity="info", score=0.0,
                    title=f"{result.net} — no impedance-bearing geometry to measure",
                    nets=result.net.split(" / "),
                    detail=list(result.warnings),
                    basis="routed cross-section measured per 25 um and solved"))
            continue

        severity, score = _severity(abs(result.deviation), IMPEDANCE_MINOR,
                                    IMPEDANCE_MAJOR)
        worst = result.worst_segment
        detail = [
            f"routed {result.routed_length_mm:.2f} mm at "
            f"{result.min_ohm:.1f}-{result.max_ohm:.1f} ohm, mean "
            f"{result.mean_ohm:.1f}; intended cross-section solves to "
            f"{result.nominal_ohm:.1f} ohm ({result.target_ohm:.0f} ohm target)",
            f"{result.deviation:+.1%} against the intended geometry; "
            f"{result.length_beyond():.2f} mm more than "
            f"{routed.REPORTABLE_DEVIATION:.0%} away from it",
        ]
        if worst is not None:
            detail.append(f"worst stretch: {worst}")
        detail += result.warnings

        findings.append(Finding(
            check="impedance", severity=severity, score=score,
            title=(f"{result.net} — routed impedance is "
                   f"{result.deviation:+.1%} from its intended cross-section"),
            nets=result.net.split(" / "),
            layers=sorted({s.layer for s in result.segments}),
            geometry=_impedance_geometry(board, result),
            geometry_label="the off-target stretches",
            detail=detail, measurement=result.summary(),
            basis=("width, clearance to ground and pair spacing measured per "
                   "25 um along the routed copper, bucketed and solved with the "
                   "2D field solver; compared against the same solver's answer "
                   "for the fab's intended cross-section so the model's ~1.5% "
                   "offset cancels"),
            intent_needed=("whether this net is a transmission line at its own "
                           "edge rate — below the critical length, impedance "
                           "does not matter at all")))
    return findings


def check_return_path(resolver: reference_module.ReferenceResolver,
                      nets: list[str]) -> list[Finding]:
    """Return-current continuity, and the detour where it is broken."""
    findings: list[Finding] = []
    for name in nets:
        result = resolver.analyse_net(name)
        if result.total_length_mm <= 0:
            continue

        uncovered = result.uncovered_length_mm
        if uncovered > 0:
            severity, score = _severity(uncovered, RETURN_PATH_MINOR_MM,
                                        RETURN_PATH_MAJOR_MM)
            worst = result.worst_gap
            # The holes the return current has to go round: exactly the thing
            # a picture of this finding should show, beside the trace itself.
            holes = [g.geometry for g in result.significant_gaps
                     if g.geometry is not None and not g.geometry.is_empty]
            findings.append(Finding(
                geometry=unary_union(holes) if holes else None,
                geometry_label="stretch with no reference beneath",
                check="return_path", severity=severity, score=score,
                title=(f"{name} — return current detours around "
                       f"{uncovered:.2f} mm of its path"),
                nets=[name],
                location=worst.centre if worst else None,
                detail=[
                    f"coverage {result.coverage:.1%} of "
                    f"{result.total_length_mm:.2f} mm "
                    f"({len(result.significant_gaps)} significant gap(s))",
                    (f"worst gap {worst.length_mm:.2f} mm, {worst.kind}, "
                     f"return detour {worst.detour_mm:.2f} mm"
                     if worst else ""),
                    f"references {', '.join(f'{k} ({v:.1f} mm)' for k, v in list(result.reference_nets.items())[:3])}",
                ],
                measurement=result.summary(),
                basis=("trace centreline intersected with the poured plane "
                       "beneath it; antipads and thermal reliefs are credited "
                       "as covered because the plane is continuous around "
                       "them, and each gap's cost is the shorter way round its "
                       "boundary, not its length"),
                intent_needed=("this net's edge rate — the loop area a detour "
                               "encloses only radiates at frequencies the "
                               "signal contains")))

        # A signal whose reference changes net partway along has a return
        # current that must cross between two planes somewhere.
        grounds = {n for n in result.reference_nets if n in resolver.ground_nets}
        others = {n for n in result.reference_nets if n not in resolver.ground_nets}
        if grounds and others:
            off_ground = sum(v for k, v in result.reference_nets.items()
                             if k in others)
            severity, score = _severity(
                off_ground / result.total_length_mm, 0.10, 0.40)
            # The stretches sitting over the *power* plane rather than ground.
            # Where the crossing happens is the whole question — a picture that
            # only highlighted the net would say nothing this text does not.
            # `primary`, not `below` — that is what `reference_nets` aggregates,
            # so the picture and the numbers describe the same segments.
            off_ground_copper = [
                segment.track.copper for segment in result.segments
                if segment.primary is not None
                and segment.primary.net_name in others]
            findings.append(Finding(
                check="reference_split", severity=severity, score=score,
                geometry=(unary_union(off_ground_copper)
                          if off_ground_copper else None),
                geometry_label=f"referenced to {', '.join(sorted(others))}",
                title=(f"{name} — crosses between a ground and a power "
                       f"reference ({off_ground:.1f} mm over "
                       f"{', '.join(sorted(others))})"),
                nets=[name],
                detail=[f"reference by length: " + ", ".join(
                    f"{k} {v:.1f} mm" for k, v in result.reference_nets.items()),
                    "the return current has to cross between those planes, "
                    "which it can only do through a capacitor bridging them"],
                measurement={"reference_nets": {
                    k: round(v, 2) for k, v in result.reference_nets.items()}},
                basis=("per-segment reference resolved as the nearest copper "
                       "layer that actually covers the trace, then aggregated "
                       "by routed length"),
                intent_needed=("whether these planes are decoupled to each "
                               "other where the crossing happens")))
    return findings


def _length_on_layer(board, net_name: str, layer: str,
                     memo: dict) -> float:
    """Routed length of one net on one layer, in mm.

    Memoised per call rather than globally: a net with fourteen vias onto the
    same unreferenced layer would otherwise walk its tracks fourteen times, and
    a module-level cache keyed on a board would outlive the board.
    """
    key = (net_name, layer)
    if key not in memo:
        try:
            code = board.net(net_name).code
        except KeyError:
            memo[key] = 0.0
        else:
            memo[key] = sum(t.length_mm for t in board.tracks_on_net(code)
                            if t.layer == layer)
    return memo[key]


def _copper_on_layer(board, net_name: str, layer: str) -> list:
    """The net's copper on one layer — the geometry behind `_length_on_layer`.

    Kept separate rather than folded into the memo above because the length is
    asked for on every stranded transition and the shapes only when a finding
    is actually built.
    """
    try:
        code = board.net(net_name).code
    except KeyError:
        return []
    return [t.copper for t in board.tracks_on_net(code) if t.layer == layer]


def check_layer_transitions(resolver: reference_module.ReferenceResolver,
                            nets: list[str], rise_time_ps: float
                            ) -> list[Finding]:
    """Vias where the reference changes, and whether the return can follow.

    The threshold is not a chosen millimetre count. A discontinuity stops being
    electrically small at about a twentieth of the knee wavelength, so it comes
    from the edge rate: 14 mm at 1 ns, 4 mm at 300 ps. Everything here is
    therefore quoted against a stated edge rate rather than against a rule of
    thumb that would be wrong by a factor of three at either end of this board's
    signal set.
    """
    board = resolver.board
    minor_mm = reference_module.stitching_distance_limit_mm(
        rise_time_ps, fraction=STITCH_LIMIT_FRACTION)
    major_mm = 2.0 * minor_mm
    findings: list[Finding] = []
    length_memo: dict = {}
    #: (net, unreferenced layers) -> the vias that land there. One net crossing
    #: onto one unreferenced layer is *one* problem however many vias do it, and
    #: once the score became a property of the net and the layer rather than of
    #: the via, per-via findings were literally identical: `/VIN` produced eight
    #: rows with the same title and the same 11.58.
    stranded_vias: dict = {}
    for name in nets:
        for transition in reference_module.find_transitions(resolver, name):
            if not transition.reference_changed:
                continue
            kind = transition.kind
            source = (transition.from_reference.net_name
                      if transition.from_reference else None)
            target = (transition.to_reference.net_name
                      if transition.to_reference else None)

            if kind == "unreferenced":
                # No plane resolves on one side, so there is nothing for the
                # return current to follow at all — a different and worse
                # situation than a reference change with no stitch nearby.
                #
                # This used to be a flat `severity="major", score=5.0`. On the
                # two boards it was written against that branch fired seven
                # times in total, so the constant never showed. A board that
                # routes 696 signal tracks through both inner layers of a
                # four-layer board, leaving no clean plane, fires it 88
                # times — 88 findings carrying one identical score, which is
                # the `next_saturated` mistake §8 records, reproduced.
                #
                # The magnitude that actually varies is *how far* the return
                # has nothing to follow: the net's routed length on the
                # unreferenced layer(s). Scored against the same knee-wavelength
                # limit as the stitch distance below, so it scales with the edge
                # rate rather than being a chosen millimetre count.
                missing = tuple(
                    layer for layer, reference in
                    ((transition.from_layer, transition.from_reference),
                     (transition.to_layer, transition.to_reference))
                    if reference is None)
                stranded_vias.setdefault((name, missing), []).append(transition)
                continue

            if kind == "ground_to_ground":
                distance = transition.nearest_stitch_mm
                need = "stitching via"
            else:
                bridge = reference_module.nearest_bridge_capacitor(
                    board, source, target, transition.via.pos)
                distance = bridge[0] if bridge else None
                need = (f"bridging capacitor ({bridge[1]})" if bridge
                        else "bridging capacitor")
            found = (f"the nearest {need} is {distance:.2f} mm away"
                     if distance is not None
                     else f"there is no {need} anywhere on the board")

            severity, score = _severity(
                distance if distance is not None else 1e3, minor_mm, major_mm)
            if severity == "info":
                continue

            findings.append(Finding(
                check="layer_transition", severity=severity, score=score,
                title=(f"{name} — changes layer and reference "
                       f"({source} -> {target}) and {found}"),
                nets=[name],
                location=transition.via.pos,
                layers=[transition.from_layer, transition.to_layer],
                detail=[
                    f"via #{transition.via.id} {transition.from_layer} -> "
                    f"{transition.to_layer}, {kind.replace('_', ' ')}",
                    f"the return current has to reach the other plane through "
                    f"a {need}, and {found}",
                    f"at {rise_time_ps:.0f} ps edges a discontinuity stops "
                    f"being electrically small past {minor_mm:.1f} mm",
                ],
                measurement={"kind": kind, "distance_mm": distance,
                             "from": source, "to": target,
                             "limit_mm": round(minor_mm, 2),
                             "rise_time_ps": rise_time_ps},
                assumption=f"{rise_time_ps:.0f} ps edges on this net",
                basis=("reference plane resolved on each side of the via by "
                       "coverage, then the nearest via or two-pad part joining "
                       "those two nets measured from the via's position; the "
                       "threshold is a twentieth of the knee wavelength"),
                intent_needed=("this net's edge rate — the limit is 4 mm at "
                               "300 ps and 72 mm at 5 ns")))

    for (name, missing), transitions in stranded_vias.items():
        stranded = sum(_length_on_layer(board, name, layer, length_memo)
                       for layer in missing)
        severity, score = _severity(stranded, minor_mm, major_mm)
        layers = sorted({layer for t in transitions
                         for layer in (t.from_layer, t.to_layer)})
        # The copper that has nothing under it — the `stranded` millimetres in
        # the title, drawn. The via is already carried by `location`.
        stranded_copper = [shape for layer in missing
                           for shape in _copper_on_layer(board, name, layer)]
        findings.append(Finding(
            check="layer_transition", severity=severity, score=score,
            geometry=(unary_union(stranded_copper)
                      if stranded_copper else None),
            geometry_label=f"running on {', '.join(missing)} with no plane under it",
            title=(f"{name} — crosses onto {', '.join(missing)} at "
                   f"{len(transitions)} via(s), where no plane covers it and "
                   f"the {stranded:.1f} mm it runs there has no return path "
                   f"to follow"),
            nets=[name], location=transitions[0].via.pos, layers=layers,
            detail=[
                f"vias: " + ", ".join(
                    f"#{t.via.id} {t.from_layer}->{t.to_layer}"
                    for t in transitions[:8])
                + (f" and {len(transitions) - 8} more"
                   if len(transitions) > 8 else ""),
                f"this net runs {stranded:.2f} mm on {', '.join(missing)}, "
                f"against a {minor_mm:.1f} mm limit at {rise_time_ps:.0f} ps "
                f"edges",
                "no plane resolves under that copper at all, so unlike a "
                "reference change there is nothing for the return current to "
                "stitch to",
            ],
            measurement={"kind": "unreferenced",
                         "unreferenced_layers": list(missing),
                         "stranded_length_mm": round(stranded, 3),
                         "vias": [t.via.id for t in transitions],
                         "limit_mm": round(minor_mm, 2),
                         "rise_time_ps": rise_time_ps},
            assumption=f"{rise_time_ps:.0f} ps edges on this net",
            basis=("reference plane resolved on each side of the via by how "
                   "much of the trace the plane covers; ranked on the net's "
                   "routed length where no plane resolves, against the same "
                   "knee-wavelength limit as the stitch distance"),
            intent_needed="this net's edge rate"))
    return findings


def check_differential_pairs(board: Board, schematic: Schematic | None,
                             found: list[str] | None = None) -> list[Finding]:
    """How well each pair the design says exists was actually routed.

    `found` collects every pair detected, so the coverage line can carry a
    count. Without one, `0 finding(s) (pairs from device pin names)` reads as
    a pass when it means no pair was detected at all (§48.3).
    """
    findings: list[Finding] = []
    for pair in coupling_module.find_differential_pairs(board,
                                                        schematic=schematic):
        name = f"{pair.positive} / {pair.negative}"
        if found is not None:
            found.append(name)
        evidence = "; ".join(pair.evidence[:2]) or "net name convention"
        longest = max(pair.length_positive_mm, pair.length_negative_mm)
        if longest < PAIR_MIN_LENGTH_MM:
            findings.append(Finding(
                check="differential_pair", severity="info", score=0.0,
                title=(f"{name} — {longest:.2f} mm of escape routing, too short "
                       f"to be routed as a pair"),
                nets=[pair.positive, pair.negative], layers=pair.layers,
                detail=[f"believed to be a pair from: {evidence}"],
                measurement=pair.summary(), confidence=pair.confidence or 0.6,
                basis="routed length of each half"))
            continue

        coupled_severity, coupled_score = _severity(
            1.0 - pair.coupled_fraction, 1.0 - PAIR_COUPLED_MINOR,
            1.0 - PAIR_COUPLED_MAJOR)
        skew_severity, skew_score = _severity(
            pair.skew_mm, PAIR_SKEW_MINOR_MM, PAIR_SKEW_MAJOR_MM)

        severity = min((coupled_severity, skew_severity),
                       key=lambda s: SEVERITY_ORDER[s])
        if severity == "info":
            continue

        # A finding is only as strong as the belief that these two nets are a
        # pair at all. One device calling its pins IN+/IN- is good evidence and
        # not proof — those are as likely to be a sense pair wanting a Kelvin
        # connection as a transmission line wanting tight coupling — so a
        # single-device proposal cannot produce a major finding on its own.
        demoted = None
        if (pair.confidence or 0.0) < 0.9 and severity == "major":
            severity, demoted = "minor", pair.confidence

        # The stretch that *is* a pair, so the picture distinguishes it from
        # the rest of the two highlighted nets, which by definition is not.
        # A pair at 0% coupled has no runs and so no evidence — correct, and
        # the two nets diverging is the whole finding in that case.
        coupled = [run.geometry for run in pair.coupling.runs
                   if run.geometry is not None and not run.geometry.is_empty]
        findings.append(Finding(
            check="differential_pair", severity=severity,
            score=max(coupled_score, skew_score),
            geometry=unary_union(coupled) if coupled else None,
            geometry_label="the stretch actually routed as a pair",
            title=(f"{name} — routed {pair.coupled_fraction:.0%} coupled with "
                   f"{pair.skew_mm:.3f} mm of skew"),
            nets=[pair.positive, pair.negative], layers=pair.layers,
            detail=[
                f"{pair.uncoupled_mm:.2f} mm of "
                f"{pair.length_positive_mm:.2f} mm routed as two single-ended "
                f"traces rather than as a pair",
                (f"mean gap {pair.coupling.mean_gap_mm:.4f} mm, spread "
                 f"{pair.gap_consistency_mm:.4f} mm — the impedance wanders "
                 f"with the gap" if pair.coupling.mean_gap_mm else "never runs "
                 "close enough to be a pair anywhere"),
                f"believed to be a pair from: {evidence}",
                (f"held below major: only one device names these as a pair "
                 f"(confidence {demoted:.2f}), and IN+/IN- on a single part is "
                 f"as likely a sense pair as a transmission line"
                 if demoted is not None else ""),
            ],
            measurement=pair.summary(),
            confidence=pair.confidence or 0.6,
            basis=("edge-to-edge gap sampled per 25 um with a parallelism test, "
                   "so a crossing is not counted as coupling; skew is raw "
                   "length mismatch, with arcs measured as r*theta"),
            intent_needed=("the protocol's own skew budget and whether this "
                           "pair is impedance-controlled — USB full speed "
                           "tolerates far more than this, gigabit does not")))
    return findings


def check_crosstalk(board: Board, ground_nets: set[str], rail_nets: set[str],
                    rise_time_ps: float, cache) -> list[Finding]:
    """Coupling between nets, solved from the mutual C and L along each run."""
    findings: list[Finding] = []
    for victim in crosstalk_module.survey(
            board, ground_nets, rail_nets=rail_nets, cache=cache,
            rise_time_ps=rise_time_ps):
        worst = victim.worst
        if worst is None:
            continue
        severity, score = _severity(victim.worst_fraction, CROSSTALK_MINOR,
                                    CROSSTALK_MAJOR)
        rails = [c.aggressor for c in victim.aggressors if c.aggressor_is_rail]

        detail = [
            f"NEXT {victim.next_fraction():.2%} and FEXT "
            f"{victim.fext_fraction():+.2%} of the aggressors' swing with all "
            f"{len(victim.aggressors)} of them switching together",
            f"worst single aggressor {worst.aggressor}: "
            f"{worst.coupled_length_mm:.2f} mm coupled at "
            f"{worst.min_gap_mm:.3f} mm on {', '.join(worst.layers)}",
            f"NEXT from it is bounded at {worst.next_saturated:.2%} for any "
            f"edge rate however fast",
        ]
        if abs(worst.fext_ps) < 1e-9 and worst.sections:
            detail.append("far-end coupling is zero: the run is buried between "
                          "planes, so the electric and magnetic coupling cancel")
        if rails:
            detail.append(f"aggressors that are power rails: "
                          f"{', '.join(sorted(set(rails)))} — these inject "
                          f"nothing unless one is a switching node")

        findings.append(Finding(
            check="crosstalk", severity=severity, score=score,
            title=(f"{victim.victim} — picks up {victim.worst_fraction:.1%} of "
                   f"its neighbours' swing from {len(victim.aggressors)} "
                   f"aggressor(s)"),
            nets=[victim.victim] + [c.aggressor for c in victim.aggressors],
            layers=worst.layers, detail=detail, measurement=victim.summary(),
            basis=("mutual C and L from a 2D field solve of the cross-section "
                   "at each point of the coupled run; NEXT is the peak of a "
                   "sliding rise-time window over the coupling profile, FEXT "
                   "the coefficient integrated along the run"),
            assumption=(f"{rise_time_ps:.0f} ps edges on the aggressors, and "
                        f"both ends of the victim matched to its own impedance "
                        f"— a high-impedance CMOS receiver roughly doubles the "
                        f"near-end figure"),
            intent_needed=("the aggressors' real edge rates and whether they "
                           "switch at all, and the victim's noise margin")))
    return findings


def check_broadside(board: Board, ground_nets: set[str], rail_nets: set[str],
                    rise_time_ps: float, cache) -> list[Finding]:
    """Coupling between nets stacked on adjacent layers with no plane between.

    Separate from `check_crosstalk` because the two have different remedies and
    a reviewer acts on them differently: side-by-side coupling is fixed by
    widening a clearance, stacked coupling by moving one layer's routing
    sideways or by putting it on a layer with a plane in the way. Reporting
    both under one heading would suggest a clearance rule could fix either.
    """
    findings: list[Finding] = []
    for victim in crosstalk_module.broadside_survey(
            board, ground_nets, rail_nets=rail_nets, cache=cache,
            rise_time_ps=rise_time_ps):
        worst = victim.worst
        if worst is None or not worst.sections:
            continue
        severity, score = _severity(victim.worst_fraction, CROSSTALK_MINOR,
                                    CROSSTALK_MAJOR)
        section = worst.worst_section
        rails = [c.aggressor for c in victim.aggressors if c.aggressor_is_rail]

        detail = [
            f"NEXT {victim.next_fraction():.2%} of the aggressors' swing with "
            f"all {len(victim.aggressors)} of them switching together",
            f"worst single aggressor {worst.aggressor}: "
            f"{worst.coupled_length_mm:.2f} mm running on {section.layer} "
            f"over {section.aggressor_layer}, {section.separation_mm:.4f} mm "
            f"apart vertically and {section.gap_mm:.3f} mm laterally",
            f"NEXT from it is bounded at {worst.next_saturated:.2%} for any "
            f"edge rate however fast",
            f"neither layer shields the other — the two are adjacent in the "
            f"stackup, so the coupling is set by how far apart the two "
            f"centrelines sit rather than by any clearance rule",
        ]
        if rails:
            detail.append(f"aggressors that are power rails: "
                          f"{', '.join(sorted(set(rails)))} — these inject "
                          f"nothing unless one is a switching node")

        findings.append(Finding(
            check="broadside", severity=severity, score=score,
            title=(f"{victim.victim} — runs stacked over "
                   f"{len(victim.aggressors)} net(s) on the layer beside it, "
                   f"picking up {victim.worst_fraction:.1%} of their swing"),
            nets=[victim.victim] + [c.aggressor for c in victim.aggressors],
            layers=sorted({section.layer, section.aggressor_layer}),
            detail=detail, measurement=victim.summary(),
            basis=("mutual C and L from a 2D field solve of the stacked "
                   "cross-section at each point of the run, with the lateral "
                   "centre-to-centre offset measured off the board and each "
                   "trace referenced to the plane on its own far side"),
            assumption=(f"{rise_time_ps:.0f} ps edges on the aggressors, and "
                        f"both ends of the victim matched to its own "
                        f"impedance"),
            intent_needed=("the aggressors' real edge rates, and whether "
                           "these two layers were meant to carry unrelated "
                           "signals over each other at all")))
    return findings


def check_parity(board: Board, schematic: Schematic,
                 coverage: dict | None = None) -> list[Finding]:
    """Where the board and the schematic describe different circuits."""
    result = parity_module.check(board, schematic)
    if coverage is not None:
        coverage.update(result.summary())
    findings: list[Finding] = []

    for gap in result.components:
        findings.append(Finding(
            check="parity", severity="info" if gap.dnp else "major",
            score=1.0, title=f"{gap.reference} — {gap}",
            detail=[str(gap)],
            measurement={"reference": gap.reference, "side": gap.side,
                         "dnp": gap.dnp},
            basis="components matched by designator between the two sides"))

    connected = result.connected
    if connected:
        board_side = [p for p in connected if p.side == "board_only"]
        schematic_side = [p for p in connected if p.side == "schematic_only"]
        if board_side:
            findings.append(Finding(
                check="parity", severity="minor", score=len(board_side),
                title=(f"{len(board_side)} pad(s) carry a net with no symbol "
                       f"pin behind them"),
                nets=sorted({p.net_name for p in board_side if p.net_name}),
                detail=[str(p) for p in board_side],
                measurement={"pads": [p.designator for p in board_side]},
                basis=("pads matched to symbol pins by designator and pad "
                       "number, the same key KiCad uses to push a netlist into "
                       "a layout"),
                intent_needed=("whether these were netted deliberately in the "
                               "board editor — thermal pads and shield tabs "
                               "usually are, and then this is expected")))
        if schematic_side:
            findings.append(Finding(
                check="parity", severity="major", score=len(schematic_side),
                title=(f"{len(schematic_side)} symbol pin(s) are connected in "
                       f"the schematic and have no pad on the board"),
                nets=sorted({p.net_name for p in schematic_side if p.net_name}),
                detail=[str(p) for p in schematic_side],
                measurement={"pins": [p.designator for p in schematic_side]},
                basis="as above; the connection exists on the drawing only"))

    for conflict in result.net_conflicts:
        findings.append(Finding(
            check="parity", severity="critical", score=10.0,
            title=f"schematic and board disagree about a net: {conflict}",
            detail=[conflict],
            basis="a schematic net whose pads land on two different board nets"))
    return findings


def _longest_connected_run(tracks) -> float:
    """The longest chain of touching narrow tracks, in mm.

    The aggregate over a net's narrow copper is not a path — on a BGA fanout it
    is dozens of independent pad-to-via stubs — so it must not be the only
    number reported. This groups the narrow tracks into connected components by
    shared endpoints and returns the largest component's total length, which is
    the longest stretch current could actually be forced through.

    Endpoints are rounded to 1 um before comparison: KiCad's own coordinates
    are exact at that scale, and two tracks meeting at a point agree there.
    """
    parent: dict = {}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        for key in (a, b):
            parent.setdefault(key, key)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def node(point):
        return (round(point[0], 3), round(point[1], 3))

    for track in tracks:
        union(node(track.start), node(track.end))

    totals: dict = {}
    for track in tracks:
        root = find(node(track.start))
        totals[root] = totals.get(root, 0.0) + track.length_mm
    return max(totals.values(), default=0.0)


def check_conductor_necks(board: Board, rail_nets: set[str]) -> list[Finding]:
    """Rails whose only path to part of the circuit is a narrow stretch.

    Current requirements are intent and are not available here, so this looks
    for two things that are not: a rail routed mostly at one width with a
    stretch at half of it, *and* that stretch being the sole path rather than
    one of several parallel ones. The second half is what makes it a finding
    instead of noise — nearly every poured rail has a thin track somewhere, and
    on these boards most of them are shorted out by the pour a millimetre later.

    The cut test is exact: rebuild the net's copper graph with the narrow
    tracks removed and see whether any pad it served falls off. If none does,
    the current has another way round and the width does not matter.
    """
    findings: list[Finding] = []
    for name in sorted(rail_nets):
        try:
            code = board.net(name).code
        except KeyError:
            continue
        tracks = [t for t in board.tracks_on_net(code) if t.length_mm > 0]
        if sum(t.length_mm for t in tracks) < 5.0:
            continue

        by_width: dict[float, float] = {}
        for track in tracks:
            by_width[track.width_mm] = by_width.get(track.width_mm, 0.0) + track.length_mm
        typical = max(by_width, key=by_width.get)
        narrow = [t for t in tracks if t.width_mm <= NECK_FRACTION * typical]
        length = sum(t.length_mm for t in narrow)
        if not narrow or length < NECK_MIN_LENGTH_MM:
            continue

        intact = topology.build_topology(board, code)
        cut = topology.build_topology(board, code,
                                      exclude_tracks={t.id for t in narrow})
        served = {p.designator for island in intact.islands
                  for p in island.pads} if intact.islands else set()
        reachable = max((({p.designator for p in island.pads})
                         for island in cut.islands),
                        key=len, default=set())
        stranded = sorted(served - reachable)
        if not stranded:
            continue

        narrowest = min(t.width_mm for t in narrow)
        outer = any(t.layer in (board.copper_layers[0], board.copper_layers[-1])
                    for t in narrow if t.width_mm == narrowest)
        thickness = 0.035 if outer else 0.0152
        capacity = physics.current_capacity_a(narrowest, thickness,
                                              internal=not outer)
        # The drop the neck imposes when it is worked to its own IPC-2152
        # rating. Needs no intent — the current is the neck's own capacity —
        # and it is what turns "0.74 A" into something a designer can weigh
        # against a rail's tolerance. (`physics.voltage_drop_v` had no caller
        # until this; §44.3 item 8.)
        drop_mv = 1e3 * physics.voltage_drop_v(capacity, length, narrowest,
                                               thickness)
        severity, score = _severity(typical / narrowest, 1 / NECK_FRACTION, 4.0)
        worst_run = _longest_connected_run(narrow)

        findings.append(Finding(
            geometry=unary_union([t.copper for t in narrow]),
            geometry_label="the narrow stretch",
            check="conductor_neck", severity=severity, score=score,
            # The aggregate first and the worst *single* run beside it. The
            # aggregate alone reads as one long bottleneck: on a BGA fanout,
            # "+3V3 reaches 10 pads only through 12.07 mm" was ten separate
            # sub-millimetre stubs from a pad to its own plane via, plus one
            # real 3.95 mm run. The qualifier saying it is not a series
            # resistance sat four lines below a headline that implied it was.
            title=(f"{name} — reaches {len(stranded)} pad(s) only through "
                   f"{narrowest:.3f} mm track, worst single run "
                   f"{worst_run:.2f} mm, {length:.2f} mm in aggregate, where "
                   f"the net is otherwise {typical:.3f} mm"),
            nets=[name],
            layers=sorted({t.layer for t in narrow}),
            location=narrow[0].centreline.interpolate(0.5, normalized=True).coords[0],
            detail=[
                f"the {length:.2f} mm is every narrow stretch on this net "
                f"added up, not one path; the longest connected run of it is "
                f"{worst_run:.2f} mm, and that is the number that decides "
                f"whether to act",
                f"stranded without it: {', '.join(stranded[:8])}"
                + (f" and {len(stranded) - 8} more" if len(stranded) > 8 else ""),
                f"widths by length: " + ", ".join(
                    f"{w:.3f} mm for {l:.1f} mm" for w, l in
                    sorted(by_width.items(), key=lambda kv: -kv[1])[:4]),
                f"{narrowest:.3f} mm carries {capacity:.2f} A at a 10 C rise "
                f"(IPC-2152, still air, {'outer' if outer else 'inner'} layer)",
                f"at that {capacity:.2f} A the neck drops "
                f"{drop_mv:.0f} mV over its {length:.2f} mm",
            ],
            measurement={"typical_mm": typical, "neck_mm": narrowest,
                         "neck_length_mm": round(length, 2),
                         "capacity_a": round(capacity, 3),
                         "drop_at_capacity_mv": round(drop_mv, 2),
                         "stranded_pads": stranded},
            basis=("track widths weighted by routed length, then the net's "
                   "copper graph rebuilt without the narrow tracks to prove "
                   "they carry the whole current; capacity from IPC-2152's "
                   "still-air chart, with no credit for nearby planes. "
                   "The length quoted is every narrow stretch on the net, not "
                   "one path, so it is not a series resistance"),
            intent_needed=(f"how much current {name} carries to those pads — "
                           f"the neck is only a defect above {capacity:.2f} A")))
    return findings


#: Checks whose physics a copper-free channel across the board explains
#: outright. A net that appears on both sides of one has a discontinuity in it
#: by construction: there is no plane under the channel, no reference to hold,
#: and no continuous pair geometry to measure. Coupling and impedance are *not*
#: in here — the copper that does exist still couples and still has a
#: cross-section, so those findings stand on their own.
PARTITION_EXPLAINS = {"return_path", "reference_split", "layer_transition",
                      "differential_pair"}


def check_panelisation(board: Board, division: panel.Partition) -> list[Finding]:
    """Whether the board's copper is one board or several.

    Always `info`: a partitioned board is a fact about the geometry, not a
    defect. It is reported because it changes how the rest of the report should
    be read, and because the alternative reading — that a quarter of the board
    has no ground plane — is alarming and wrong.
    """
    if not division.is_partitioned:
        return []
    crossing = sorted(division.crossing_nets())
    detached = division.detached
    detail = [f"main region {division.main.area_mm2:.0f} mm² "
              f"({division.main.fraction:.0%} of the copper)"]
    for region in detached:
        x0, y0, x1, y1 = region.bounds
        detail.append(f"detached region {region.area_mm2:.0f} mm² "
                      f"({region.fraction:.0%}) over x {x0:.1f}–{x1:.1f}, "
                      f"y {y0:.1f}–{y1:.1f}, carrying {len(region.nets)} net(s)")
    if division.gap_mm is not None:
        detail.append(f"narrowest copper-free channel {division.gap_mm:.2f} mm "
                      f"at ({division.gap_at[0]:.1f}, {division.gap_at[1]:.1f})")
    if crossing:
        detail.append(
            f"{len(crossing)} net(s) have copper on both sides and are "
            f"therefore discontinuous: " + ", ".join(crossing[:10])
            + (f" and {len(crossing) - 10} more" if len(crossing) > 10 else ""))
        detail.append(
            "return-path, reference and pair findings on those nets are "
            "reported at info, because the channel explains them")

    return [Finding(
        geometry=(unary_union([r.polygon for r in detached])
                  if detached else None),
        geometry_label="the detached copper region",
        check="panelisation", severity="info", score=1.0,
        title=(f"the board's copper is {len(division.regions)} separate "
               f"regions, not one — "
               f"{'a break-off panel would look exactly like this' if crossing else 'nothing bridges them'}"),
        nets=crossing,
        location=division.gap_at,
        detail=detail, measurement=division.summary(),
        basis=("every layer's copper unioned into one geometry, then split "
               "into connected components; a component counts as a region "
               "above both 5% of the board's copper and 50 mm², which over "
               "eight boards separates a real division (16%) from every "
               "mounting-hole annulus (≤2.3%)"),
        intent_needed=("whether this is a break-off panel snapped and "
                       "reattached after assembly, a deliberately isolated "
                       "section, or a routing error"))]


def _board_source_path(board: Board) -> Path | None:
    """The board file this extraction came from, resolvable from anywhere.

    `source.file` is absolute for anything extracted since that was fixed, but
    a cache written by an older build can hold a relative path — and caches now
    outlive the session and the working directory that made them. Falls back to
    the extraction's own location, which sits beside the board in a review
    folder.
    """
    origin = board._data.get("source", {}).get("file")
    if origin:
        candidate = Path(origin)
        if candidate.is_file():
            return candidate
        if board.source_path is not None:
            beside = Path(board.source_path).parent / candidate.name
            if beside.is_file():
                return beside
            beside = Path(board.source_path).parent.parent / candidate.name
            if beside.is_file():
                return beside
    return None


def check_stackup_mismatch(board: Board) -> list[Finding]:
    """Where KiCad's own board stackup disagrees with the fab's.

    Not a defect in the layout — it is a defect in the *design environment*,
    and it is worth a finding because everything the designer sees inside KiCad
    is computed from the board's copy. Its length-tuning delays, its impedance
    hints and its 3D thickness all use those numbers.

    Both boards this tool was built against are badly wrong. The six-layer board
    records
    0.035 mm inner copper where JLC ships 0.0152, and 4.5 for every epsilon_r
    where the glass styles give 4.25/4.35/4.5. The four-layer board records
    0.01 mm for
    every dielectric — an untouched KiCad default on a board that is really
    1.59 mm thick — because nothing in KiCad forces you to fill it in.

    This is why `stackup.from_kicad_file` must never be a silent source of
    physics. The layer map and count it gives are exact and `bind_stackup`
    already enforces them; the dielectric numbers are not.
    """
    from pcbreview import stackup as stackup_module

    if board.stackup is None or board.source_path is None:
        return []
    origin = _board_source_path(board)
    if origin is None:
        return []
    try:
        theirs = stackup_module.from_kicad_file(origin)
    except stackup_module.StackupGateError:
        return []

    problems = stackup_module.compare_to_board(board.stackup, theirs)
    if not problems:
        return []

    # How wrong, as the worst single ratio — that is what decides whether
    # KiCad's own numbers are merely imprecise or unusable.
    worst = 0.0
    for problem in problems:
        match = re.search(r"\((\d+)% apart\)", problem.message)
        if match:
            worst = max(worst, int(match.group(1)) / 100.0)

    severity, score = _severity(worst, STACKUP_MISMATCH_MINOR,
                                STACKUP_MISMATCH_MAJOR)
    if severity == "info" and worst == 0.0:
        return []

    return [Finding(
        check="stackup_mismatch", severity=severity, score=score,
        title=(f"the stackup saved in the board disagrees with the fab's by up "
               f"to {worst:.0%} — every impedance KiCad shows you is computed "
               f"from the board's copy"),
        detail=[p.message for p in problems] + [
            "this analysis uses the fab stackup and is unaffected; KiCad's own "
            "length tuning, impedance hints and 3D view are not",
            "fixing it in Board Setup > Physical Stackup costs nothing and "
            "makes KiCad's own numbers agree with this tool's",
        ],
        measurement={"worst_disagreement": round(worst, 3),
                     "problems": [p.code for p in problems]},
        basis=("the fab stackup compared layer by layer against the one "
               "recorded in the .kicad_pcb file, dielectric heights first and "
               "then copper thicknesses"),
        intent_needed=("nothing — but if the board file is right and the "
                       "supplied stackup is wrong, every number in this report "
                       "is wrong instead, so check which is which"))]


def check_placement(board: Board, schematic: Schematic) -> list[Finding]:
    """Where parts sit relative to each other and to the planes.

    Needs a schematic — every check in `placement.py` keys on what a *part* is
    (a supply pin, a feedback pin, a shunt), and the copper alone does not say.
    The headline one is the decoupling loop, traced through the via positions
    that actually set it rather than measured centre to centre.

    Every check in here can find nothing for two different reasons, and until
    the unreferenced four-layer board they were indistinguishable. `placement.supply_pins`
    selects pads that are typed `power_in` *and* sit on a net `intent` calls a
    supply, and `intent` only calls a net a rail if a power symbol drives it.
    That board draws its supplies as ordinary hierarchical labels, so all 58 of
    its `power_in` pads were dropped, the decoupling-loop check ran over an
    empty list, and the audit reported a clean zero. `placement.coverage()`
    said `supply_pins: 0` and nothing printed it.

    So the coverage is now a finding whenever a check had nothing to look at.
    §32.C's rule — always print coverage before believing a zero — only works
    if the coverage reaches the reader.
    """
    findings = placement_module.analyse(board, schematic)
    coverage = placement_module.coverage(board, schematic)

    #: what each placement check needs before it can find anything, and what
    #: the reader loses when it is missing
    NEEDS = [
        ("supply_pins", "decoupling loop area",
         "pads typed `power_in` that sit on a net inferred to be a supply"),
        ("feedback_pins", "regulator feedback proximity",
         "pins whose name marks them as a regulator feedback node"),
        ("sense_pins", "Kelvin sense connection",
         "current-monitor sense pins"),
        ("crystals", "crystal loop area", "crystals or oscillators"),
        ("esd_clamps", "ESD clamp position", "parts that name themselves as a "
         "TVS or ESD array"),
    ]
    absent = [(check, needed) for key, check, needed in NEEDS
              if not coverage.get(key)]

    # A candidate counted and then not measured is the *other* way a coverage
    # line can mislead, and the harder one to spot: a reader takes a non-zero
    # count as proof the check ran. It is reported here as its own line rather
    # than folded into `absent`, because "found none" and "found some and
    # measured none" are different statements.
    #
    # This started as one hand-written block for crystals (§52.1). Auditing the
    # rest of `placement.py` for the same shape found three more, and the worst
    # was not the crystal: one board reported `esd_clamps: 11` with no findings
    # at all while **thirteen of its fourteen clamped lines had never been
    # compared**, because no connector pad sat on them. Every pair below is now
    # counted by the check itself rather than by a second copy of its
    # preconditions, so the table cannot drift from the code.
    #
    #: (found key, measured key, what a candidate is, what "measured" means)
    MEASURED_PAIRS = [
        ("crystals", "crystal_loops_measured", "crystal(s)",
         "their oscillator loop was not measured"),
        ("sense_pins", "sense_pins_measured", "current-sense pin(s)",
         "their tap offset was not compared against a shunt"),
        ("esd_clamps", "esd_clamps_measured", "ESD clamp(s)",
         "their position relative to a connector was not measured"),
        ("feedback_pins", "feedback_pins_measured", "feedback pin(s)",
         "neither the divider distance nor a switch-node run was measured"),
    ]
    for found_key, measured_key, noun, what in MEASURED_PAIRS:
        found = coverage.get(found_key, 0)
        measured = coverage.get(measured_key, 0)
        if not found or measured >= found:
            continue
        findings.append(Finding(
            check="placement", severity="info", score=0.0,
            title=(f"{found - measured} of {found} {noun} were found but "
                   f"{what}"),
            detail=[f"`{found_key}` counts candidates; `{measured_key}` counts "
                    f"the ones actually measured",
                    "the findings from that check say why for each one, and "
                    "name what the reason is normally expected for",
                    "a non-zero coverage count is not evidence a check ran — "
                    "that is the failure this pair exists to make visible"],
            measurement={found_key: found, measured_key: measured},
            basis="`placement.coverage()`, which counts the two separately"))

    if absent:
        findings.append(Finding(
            check="placement", severity="info", score=0.0,
            title=(f"{len(absent)} placement check(s) had nothing to examine "
                   f"on this board"),
            detail=[f"`{check}` found no {needed}" for check, needed in absent]
            + ["a zero from these is 'nothing to measure', not 'measured and "
               "clean' — the distinction matters most for the decoupling loop, "
               "which is the check this module exists for",
               "coverage: " + ", ".join(f"{k}={v}" for k, v in coverage.items()
                                        if k != "ground_planes")],
            measurement={"coverage": {k: v for k, v in coverage.items()
                                      if k != "ground_planes"},
                         "without_input": [c for c, _ in absent]},
            basis="`placement.coverage()`, counted before any check ran",
            intent_needed=("whether the design really has none of these, or "
                           "whether the symbols and net sources do not say so")))
    return findings


def check_thermal(schematic: Schematic,
                  manifest: thermal_module.ThermalManifest | None
                  ) -> list[Finding]:
    """Junction temperature, for the parts a datasheet was actually read for.

    **Not a gate.** Gate 1 refuses without a stackup because every impedance
    number would otherwise be invented; thermal is additive, so its absence
    invalidates nothing else and the audit runs regardless. the ruling,
    §36, and `thermal.py`'s docstring carries it in full.

    With no manifest this reports exactly one thing — that thermal analysis did
    not run, and which parts look like they need a datasheet. That is not a
    placeholder: under the ruling it is the *correct* output, and it is what
    tells the front end which PDFs to go and ask the user for.

    Nothing here reads a symbol field or follows a datasheet URL. The brief:
    "we must never trust that the user used the symbol fields in the schematic
    for each component properly". The part list below is a shortlist for a
    human, never an input to a number.
    """
    findings: list[Finding] = []
    coverage = thermal_module.coverage(schematic, manifest)
    candidates = thermal_module.dissipative_candidates(schematic)
    unclassified = thermal_module.unclassified_parts(schematic)

    def _unclassified_lines() -> list[str]:
        """Parts nothing could classify — neither dissipative nor not.

        Kept beside the shortlist rather than merged into it, because these
        are not a claim that they dissipate. On the board that prompted this
        (§43) the eight parts here were the largest heat sources on it.

        Split by whether there is a catalogue number to resolve the part with,
        which is the ruling of 2026-08-04: a part with an LCSC code can be
        looked up, a part without one needs the user before it can be
        reviewed at all. The front end acts on that split, so the finding has
        to make it.
        """
        if not unclassified:
            return []
        with_code = [p for p in unclassified if p.lcsc]
        without = [p for p in unclassified if not p.lcsc]
        lines = [f"{len(unclassified)} part(s) could not be classified either "
                 f"way — nothing here knows whether they dissipate, so they "
                 f"are neither on the shortlist above nor excluded from it"]
        if with_code:
            named = ", ".join(f"{p.reference} ({p.label}, {p.lcsc})"
                              for p in with_code[:12])
            lines.append(
                f"{len(with_code)} of them carry an LCSC catalogue number and "
                f"can be resolved from the JLCPCB/LCSC parts catalogue without "
                f"asking anyone: {named}"
                + (f" ... and {len(with_code) - 12} more"
                   if len(with_code) > 12 else ""))
        if without:
            named = ", ".join(f"{p.reference} ({p.label})"
                              for p in without[:12])
            lines.append(
                f"{len(without)} carry no catalogue number either, so THE USER "
                f"MUST SAY WHAT THESE PARTS ARE before they can be reviewed — "
                f"report them as unreviewed, not as clean: {named}"
                + (f" ... and {len(without) - 12} more"
                   if len(without) > 12 else ""))
        lines.extend(f"    {p.reference}: {p.reason}" for p in unclassified[:8])
        if len(unclassified) > 8:
            lines.append(f"    ... and {len(unclassified) - 8} more, each with "
                         f"its own reason in `measurement`")
        return lines

    if not manifest:
        shortlist = [f"{ref} ({value})" for ref, value in candidates[:20]]
        findings.append(Finding(
            check="thermal", severity="info", score=0.0,
            title=("thermal analysis did not run — no datasheet thermal data "
                   f"was supplied for any of this board's {len(candidates)} "
                   "dissipative part(s)"),
            detail=(["nothing here is a thermal pass: no junction temperature "
                     "was computed for any part on this board"]
                    + ([f"parts that look like they dissipate, and so are "
                        f"worth a datasheet: {', '.join(shortlist)}"]
                       + ([f"... and {len(candidates) - 20} more"]
                          if len(candidates) > 20 else [])
                       if candidates else
                       ["no part on this board matched a dissipative class, "
                        "which is itself only a guess from the symbol names"])
                    + _unclassified_lines()
                    + ["to run it: supply the datasheets and let the front end "
                       "write the thermal manifest — theta_JA, Tj(max) and the "
                       "dissipation at the operating point, per part"]),
            measurement={"coverage": coverage,
                         "candidates": [r for r, _ in candidates],
                         "unclassified": [p.reference for p in unclassified],
                         "unclassified_detail": [
                             {"reference": p.reference, "part": p.label,
                              "lcsc": p.lcsc, "reason": p.reason}
                             for p in unclassified]},
            basis="`thermal.coverage()`, counted before any check ran",
            assumption=("that the symbol's value or description names the part "
                        "class — the only thing used to build this shortlist, "
                        "and used for nothing else"),
            intent_needed=("theta_JA, Tj(max) and the dissipated power per "
                           "part, read from a real datasheet")))
        return findings

    # A manifest entry naming a designator this board does not have is a
    # transcription error, and it is reported as one below — but it must not
    # also produce a junction temperature. "U99 runs at 41 C" for a part that
    # does not exist reads as a measurement and is not one.
    on_board = {c.reference for c in schematic.components if not c.dnp}
    results = [r for r in thermal_module.assess(manifest)
               if r.part.reference in on_board]
    for result in results:
        part = result.part
        label = f"{part.reference}" + (f" ({part.part})" if part.part else "")

        if not part.dissipation_stated:
            # The mirror case: the thermal resistance IS known and the LOAD is
            # not. A session held four such datasheets and could enter none of
            # them, so four dissipative parts stayed unreviewed while it was
            # holding the documents (field report, 2026-08-05). Inventing a
            # dissipation is refused, correctly — but the inverse is a real,
            # checkable number and it bounds the part instead of dropping it.
            budget = part.dissipation_budget_w
            findings.append(Finding(
                check="thermal", severity="info", score=0.0,
                title=(f"{label} can dissipate {budget:.2f} W before it "
                       f"reaches Tj {part.t_junction_max_c:.0f} C — its load "
                       f"was not stated, so this is a budget, not a "
                       f"temperature"
                       if budget is not None else
                       f"{label} — neither its dissipation nor a usable "
                       f"thermal resistance was stated"),
                detail=[
                    (f"theta_JA {part.theta_ja_c_per_w:.1f} C/W at "
                     f"{result.ambient_c:.0f} C ambient gives "
                     f"{budget:.2f} W of headroom to Tj(max) "
                     f"{part.t_junction_max_c:.0f} C"
                     if budget is not None and part.theta_ja_c_per_w
                     else "no thermal resistance to work from"),
                    "`dissipation_stated: false` — the datasheet was read and "
                    "the load is unknown. Work out what this part actually "
                    "draws and compare it against the budget above; if it is "
                    "close, put the real figure in the manifest and re-run",
                    f"datasheet: {part.source}",
                ] + ([part.note] if part.note else []),
                measurement={**result.summary(),
                             "dissipation_budget_w": (round(budget, 3)
                                                      if budget else None),
                             "dissipation_stated": False},
                basis=("(Tj_max - Ta) / theta_JA — the dissipation at which "
                       "this package reaches its own junction limit on "
                       "JEDEC's test board"),
                assumption=(f"ambient {result.ambient_c:.0f} C, and theta_JA "
                            f"as a JEDEC-board figure rather than one for this "
                            f"board"),
                intent_needed=("what this part actually dissipates at the "
                               "operating point — the one number that turns "
                               "this budget into a verdict")))
            continue

        if not result.assessed:
            # The datasheet was supplied and states no thermal resistance.
            # Reporting nothing here is what lost a board's largest heat source
            # once already (§43); the required theta_JA is the number that
            # decides the argument, so report that instead.
            required = result.required_theta_ja_c_per_w
            # ...unless there is nothing to decide. Below the floor, the
            # "required theta_JA" is a seven-digit number that says only that
            # the part cannot get hot, and emitting it at `minor` ranked two
            # pieces of arithmetic noise above the one part on the board with
            # a real computed junction temperature. SKILL.md tells the front
            # end to enter *every* supplied datasheet, so it must not then
            # punish it for the honest entries (§51).
            if part.dissipation_w < thermal_module.NEGLIGIBLE_DISSIPATION_W:
                findings.append(Finding(
                    check="thermal", severity="info", score=0.0,
                    title=(f"{label} — entered at "
                           f"{part.dissipation_w * 1000:.2f} mW, thermally "
                           f"negligible"),
                    detail=[
                        f"no theta_JA in the datasheet, and none is needed: "
                        f"{part.dissipation_w * 1000:.2f} mW is under 1 C of "
                        f"rise in any package that exists",
                        f"datasheet: {part.source}",
                    ] + ([part.note] if part.note else []),
                    measurement=result.summary(),
                    basis=(f"dissipation below the "
                           f"{thermal_module.NEGLIGIBLE_DISSIPATION_W * 1000:.0f}"
                           f" mW floor at which a junction temperature can be "
                           f"resolved at all")))
                continue
            findings.append(Finding(
                check="thermal", severity=result.severity, score=result.score,
                title=(f"{label} dissipates {part.dissipation_w:.2f} W and its "
                       f"datasheet states no theta_JA — it needs "
                       f"{required:.0f} C/W or better to stay inside "
                       f"{part.t_junction_max_c:.0f} C"),
                detail=[
                    f"required theta_JA = (Tj_max - Ta) / P = "
                    f"({part.t_junction_max_c:.0f} - {result.ambient_c:.0f}) / "
                    f"{part.dissipation_w:.3f} = {required:.1f} C/W",
                    f"datasheet: {part.source}",
                    "compare that against what this package can actually "
                    "achieve on this board — no junction temperature can be "
                    "computed for it, and this is not a pass",
                ] + ([part.note] if part.note else []),
                measurement=result.summary(),
                basis=("the dissipation transcribed by hand from the datasheet "
                       "supplied, inverted against Tj(max); no theta_JA was "
                       "available to invent one from"),
                assumption=(f"ambient assumed {result.ambient_c:.0f} C"
                            if part.ambient_c is None else None),
                intent_needed=("this package's real theta_JA on this board — a "
                               "vendor figure, a thermal measurement, or a "
                               "decision to change the part")))
            continue

        findings.append(Finding(
            check="thermal", severity=result.severity, score=result.score,
            title=(f"{label} runs at {result.junction_c:.0f} C junction, "
                   f"{result.headroom_c:+.0f} C from its {part.t_junction_max_c:.0f} C "
                   f"limit ({result.rise_fraction:.0%} of the available rise)"),
            detail=[
                f"Tj = Ta + theta_JA x P = {result.ambient_c:.0f} + "
                f"{part.theta_ja_c_per_w:.1f} x {part.dissipation_w:.3f} = "
                f"{result.junction_c:.1f} C",
                f"datasheet: {part.source}",
            ] + ([part.note] if part.note else []),
            measurement=result.summary(),
            basis=("steady-state Tj = Ta + theta_JA x P, from datasheet values "
                   "transcribed by hand"),
            assumption=(
                "theta_JA is JEDEC's number, measured on JESD51-7's test board "
                "and not on this one — on a small board with a starved thermal "
                "pad the real figure is routinely 1.5-3x higher, which moves "
                "this answer far more than anything else in it. Treat it as a "
                "ranking, not a temperature"
                + (f"; ambient assumed {result.ambient_c:.0f} C"
                   if part.ambient_c is None else "")),
            intent_needed=(None if part.ambient_c is not None else
                           "the real ambient inside the enclosure")))

    missing = [(r, v) for r, v in candidates if r not in manifest.parts]
    # `or unclassified`: this finding used to appear only when a *classified*
    # candidate lacked data, so a board whose manifest covered every candidate
    # printed nothing at all about the parts nothing could classify — and they
    # are the ones no manifest can cover, because nobody knew to ask for their
    # datasheets. That is the §52.1 shape again: a complete-looking thermal
    # section over a population that was never examined.
    if missing or unclassified:
        findings.append(Finding(
            check="thermal", severity="info", score=0.0,
            title=(f"{len(missing)} dissipative part(s) still have no thermal "
                   f"data and were not assessed"
                   + (f"; {len(unclassified)} more could not be classified "
                      f"either way" if unclassified else "")),
            detail=([f"not assessed: "
                     + ", ".join(f"{r} ({v})" for r, v in missing[:20])]
                    if missing else
                    ["every part the class list identified as dissipative has "
                     "thermal data"])
            + ([f"... and {len(missing) - 20} more"] if len(missing) > 20 else [])
            + _unclassified_lines()
            + ["a zero from these is 'not measured', not 'measured and cool'"],
            measurement={"coverage": coverage,
                         "without_data": [r for r, _ in missing],
                         "unclassified": [p.reference for p in unclassified],
                         "unclassified_detail": [
                             {"reference": p.reference, "part": p.label,
                              "lcsc": p.lcsc, "reason": p.reason}
                             for p in unclassified]},
            basis="`thermal.coverage()`, against the manifest supplied",
            intent_needed="their datasheets"))

    if coverage["unknown_references"]:
        findings.append(Finding(
            check="thermal", severity="minor", score=1.0,
            title=(f"{len(coverage['unknown_references'])} thermal manifest "
                   "entr(ies) name a part that is not on this board"),
            detail=["not found in the schematic: "
                    + ", ".join(coverage["unknown_references"]),
                    "a designator that does not exist is a transcription "
                    "error, and the part it was meant for went unassessed"],
            measurement={"unknown": coverage["unknown_references"]},
            basis="manifest designators against the schematic's own"))
    return findings


#: Net name as KiCad writes it inside a DRC item description, e.g.
#: `Track [/3.3V] on F.Cu, length 0.5311 mm`.
_DRC_NET = re.compile(r"\[([^\]]+)\]")


#: KiCad DRC severity -> ours. `error` is what KiCad itself will not let you
#: fabricate past without acknowledging, and on the board that prompted this
#: two of them were pads 0.1125 mm apart against a 0.200 mm rule — an assembly
#: short on a switching node. `exclusion` is a violation the user has already
#: dismissed in their own board file and is deliberately not re-raised.
_DRC_SEVERITY = {"error": "major", "warning": "minor"}

#: Radius of the mark drawn at each DRC violation's position. A marker size,
#: not a measurement: big enough to see on a whole-board crop and small enough
#: not to cover the neighbouring pad on a 0.4 mm pitch.
DRC_MARK_MM = 0.5

#: One run per audit, shared by `check_drc` and `_unconnected_nets`. Keyed by
#: path because the audit only ever looks at one board, and the DRC costs ~4 s.
_DRC_CACHE: dict[str, object] = {}

_NET_IN_BRACKETS = re.compile(r"\[([^\]]+)\]")


def _drc_report(pcb_path):
    """KiCad's own DRC for this board, or `None` with a reason.

    Opportunistic in exactly the way `_unconnected_nets` already was: a missing
    or broken `kicad-cli` degrades to "the check could not run" and never
    fails the audit.
    """
    if pcb_path is None:
        return None, "no board path was given"
    # Cache first: once a report exists, whether the file is still on disk is
    # not a question worth re-asking, and it is what makes the check testable
    # without a board.
    key = str(pcb_path)
    if key not in _DRC_CACHE:
        if not Path(pcb_path).is_file():
            return None, "the board file is not where the extraction recorded it"
        try:
            _DRC_CACHE[key] = (drc.run_drc(pcb_path), None)
        except (drc.DrcError, OSError) as exc:
            _DRC_CACHE[key] = (None, f"{type(exc).__name__}: {exc}")
    return _DRC_CACHE[key]


def check_drc(pcb_path) -> list[Finding]:
    """KiCad's own rule violations, grouped by rule.

    **This reports KiCad's engine; it does not reimplement it** — the same
    division `drc.py` has always stated. Clearance classes, custom rules,
    courtyards and hole-to-hole spacing are things a reimplementation would
    track wrongly, and it is the same engine the user sees in the GUI.

    It exists because the data was already being computed and thrown away.
    `run_drc` has always parsed every violation with its severity, and
    `check_board_state` used only the unconnected list — and only ran the DRC
    at all when the ratsnest count was already non-zero. So on a board with
    nothing unconnected the DRC never ran, and `board_state  0 finding(s)` sat
    at the top of the report reading as a clean bill of health on a board with
    **six DRC errors**, two of them a 0.1125 mm clearance on a buck switching
    node. That was among the most actionable defects on the board and nothing
    here mentioned it.

    One finding per rule *type*, with every instance listed underneath: 21
    silkscreen texts under the minimum height are one decision, not 21
    findings.

    Item descriptions and positions are safe to quote here, and this is the
    one place in the codebase where that is true. **Unconnected items are
    not** — KiCad names one of the two items either side of a missing
    connection and which one varies between identical runs (§14, §38), which
    is why `_unconnected_nets` deliberately returns only net names. Measured
    over repeated runs before writing this: rule violations come back
    byte-identical, items and coordinates included.
    """
    report, why_not = _drc_report(pcb_path)
    if report is None:
        return [Finding(
            check="drc", severity="info", score=0.0,
            title="KiCad's DRC could not be run, so no rule violation was checked",
            detail=[why_not or "unknown reason",
                    "this is not a pass — clearance, courtyard, silkscreen and "
                    "hole-to-hole rules were not examined at all",
                    "run `kicad-cli pcb drc --format json --severity-all` by "
                    "hand to cover it"],
            basis="kicad-cli pcb drc, which is the same engine KiCad's GUI uses")]

    findings: list[Finding] = []
    for kind, unsorted in report.by_type().items():
        # Sorted, so the representative rule text and location below are a
        # function of the violation *set* and not of kicad-cli's output order.
        # On the one board whose zone fill KiCad does not refill reproducibly
        # (§40b), a different fill gives a different starved-thermal set — and
        # taking `violations[0]` from input order made the finding's `rule`
        # and `location` move as well, which reads as a regression rather than
        # as the known wobble. The set itself can still move on that board;
        # re-running is the answer, as it always was.
        violations = sorted(
            unsorted,
            key=lambda v: (v.description, v.pos or (0.0, 0.0)))
        severity = _DRC_SEVERITY.get(violations[0].severity)
        if severity is None:          # `exclusion`: the user already dismissed it
            continue
        nets = sorted({name for violation in violations
                       for item in violation.items
                       for name in _NET_IN_BRACKETS.findall(item.description)})
        # Collapse identical lines. KiCad reports one violation per offending
        # polygon, so a single malformed courtyard on one footprint arrives as
        # twenty identical entries; printing them twenty times says nothing the
        # first one did not.
        seen: dict[str, int] = {}
        for violation in violations:
            where = (f" at ({violation.pos[0]:.2f}, {violation.pos[1]:.2f})"
                     if violation.pos else "")
            items = " / ".join(item.description for item in violation.items[:2])
            line = f"{items or violation.description}{where}"
            seen[line] = seen.get(line, 0) + 1
        # The count leads the detail, since it no longer leads the title.
        detail = [f"{len(violations)} violation(s) of this rule, at "
                  f"{len(seen)} distinct location(s)"]
        detail += [line + (f"  (x{count})" if count > 1 else "")
                   for line, count in list(seen.items())[:20]]
        if len(seen) > 20:
            detail.append(f"... and {len(seen) - 20} more distinct location(s)")
        detail.append(violations[0].description)
        if kind in drc.OVERLAPPING_TYPES:
            detail.append("this rule overlaps a measurement pcbreview makes "
                          "itself — expect to see the same geometry twice")

        findings.append(Finding(
            check="drc", severity=severity,
            # Severity dominates and the count only breaks ties. Straight
            # count ranked 20 cosmetic courtyard violations above 8
            # copper-edge-clearance errors on the same board, which is
            # backwards — one is a redraw, the other is a fab problem.
            score=(10.0 if severity == "major" else 1.0) + min(len(violations), 20) * 0.05,
            # **The count is deliberately NOT in the title.** The title is the
            # review diff's key, and a count that moves turns a re-run into a
            # "resolved" plus a "new" — pure noise at the top of a second
            # review, which is where SKILL.md says to lead. An outside session
            # on Windows saw `malformed_courtyard` return 22, 21 and 23 on
            # three runs of a byte-identical board.
            #
            # Not reproduced here: three runs on macOS gave identical counts
            # for every rule type, so this is platform- or version-specific.
            # The fragility is real regardless, and it is the same rule §14
            # already states for DRC item descriptions — a value that can vary
            # between identical runs must never reach a diff key. The count is
            # in `detail` and in `measurement`, where it belongs.
            title=f"{kind} violation(s) from KiCad's own DRC",
            nets=nets,
            location=next((v.pos for v in violations if v.pos), None),
            # The evidence is where KiCad says the violations are. A rule type
            # touches dozens of nets across the board, so a picture of its
            # nets is a picture of the board; these marks are what to look at.
            geometry=(MultiPoint([v.pos for v in violations if v.pos])
                      .buffer(DRC_MARK_MM)
                      if any(v.pos for v in violations) else None),
            geometry_label="where KiCad reports a violation",
            detail=detail,
            measurement={"type": kind, "severity": violations[0].severity,
                         "count": len(violations),
                         "rule": violations[0].description},
            basis=(f"`kicad-cli pcb drc --severity-all` on the refilled board, "
                   f"KiCad {report.kicad_version} — its rule engine, not ours"),
            intent_needed=("whether this rule is the one you meant to set — "
                           "the DRC checks the constraints in your board "
                           "setup, and a wrong constraint passes silently")))
    return findings


def _unconnected_nets(pcb_path) -> tuple[dict[str, int], int, str | None]:
    """Which nets `kicad-cli pcb drc` leaves open, and how many breaks each has.

    Returns the *net names* and counts, deliberately not the item descriptions.
    KiCad names one of the two items either side of a missing connection and
    **which one it picks varies between identical runs** — two consecutive
    audits of this board reported `Track [/3.3V] on F.Cu` and then
    `Pad 7 [/3.3V] of U10 on F.Cu` for the same break. Putting that text in a
    finding would make `tests/dump_audit.py --diff` report a change on every
    run. The net names and the counts are stable; `drc.run_drc` gives the
    item-level detail to anyone drilling down.

    Opportunistic: a missing or broken `kicad-cli` degrades to the bare
    ratsnest count rather than failing the audit. `find_kicad_cli` is only
    verified on macOS (§32.E), so this must not be load bearing.
    """
    report, why_not = _drc_report(pcb_path)
    if report is None:
        return {}, 0, why_not

    by_net: dict[str, int] = {}
    for violation in report.unconnected:
        names = {match.group(1)
                 for item in violation.items
                 for match in [_DRC_NET.search(item.description)] if match}
        for name in names:
            by_net[name] = by_net.get(name, 0) + 1
    return by_net, len(report.unconnected), None


#: How close a net's declared current may come to what its copper carries.
#: Above 1.0 the conductor cannot deliver the stated current at the stated
#: rise; below it, margin. A ratio of exactly 1.0 is not a pass — IPC-2152 is a
#: curve fit to charts, not a guarantee, and every real design leaves headroom.
AMPACITY_MINOR = 0.80
AMPACITY_MAJOR = 1.00
AMPACITY_CRITICAL = 2.00


def _width_ampacity_table(board: Board, copper_oz_by_layer: dict) -> Finding:
    """Every track width on the board and what it carries. One finding, always.

    Asked for by the field report of 2026-08-05, which had to answer *"does 5 A
    get through"* by lifting numbers out of three **unrelated** findings and
    applying them to the firing-channel nets by hand — *"exactly the 'computing a
    conclusion the engine didn't' that SKILL.md tells the front end not to
    do, but the alternative was not answering the question the user asked."*

    Three lines of output, no new physics, and it answers the commonest
    question a hardware person brings to this tool. It also means no width on
    the board is ever silent, which is what lets the per-net findings below be
    reserved for nets with real power evidence.
    """
    outer = (board.copper_layers[0], board.copper_layers[-1])
    lengths: dict = {}
    for track in board.tracks:
        key = (track.layer, round(track.width_mm, 4))
        lengths[key] = lengths.get(key, 0.0) + track.length_mm

    rows = []
    for (layer, width), length in sorted(lengths.items(),
                                         key=lambda kv: -kv[1]):
        oz = copper_oz_by_layer.get(layer, 1.0 if layer in outer else 0.5)
        amps = physics.current_capacity_a(
            width, physics.copper_thickness_mm(oz),
            current_module.RISE_C, internal=layer not in outer)
        rows.append((layer, width, round(length, 2), round(amps, 3)))

    return Finding(
        check="current_capacity", severity="info", score=0.0,
        title=(f"{len(rows)} track width(s) on this board, and what each "
               f"carries at a {current_module.RISE_C:.0f} C rise"),
        detail=[f"{layer:8} {width:6.3f} mm ({length:8.2f} mm routed) "
                f"-> {amps:6.2f} A"
                for layer, width, length, amps in rows]
        + ["inner layers are thinner copper, so a wider inner track can carry "
           "less than a narrower outer one — compare the amps, not the widths",
           "this is the whole board, not one net. For a single net use "
           "`--net NET`, which adds its vias, pours and findings"],
        measurement={"widths": [{"layer": l, "width_mm": w,
                                 "routed_mm": mm, "amps": a}
                                for l, w, mm, a in rows],
                     "rise_c": current_module.RISE_C},
        basis=(f"IPC-2152 at a {current_module.RISE_C:.0f} C rise per routed "
               f"width, taking each layer's own copper weight from the bound "
               f"stackup"),
        intent_needed=("nothing — this is a property of the copper. What each "
                       "net needs to carry is the designer's to state"))


def _ampacity_findings(board: Board, power_nets: set[str],
                       copper_oz_by_layer: dict,
                       net_intent, evidenced: set[str] | None = None
                       ) -> list[Finding]:
    """The absolute current every power net's copper carries. Always emitted.

    the ruling, 2026-08-06, after a board whose firing channels produced no
    measurement at all:

        Especially for power nets, ampacity should always be reported. […] for
        power nets, or nets which the user specifically flagged for current,
        the system should always flag it.

        We must ALWAYS prefer false positives and pestering the user with
        questions and clarifications, than silently pass something.

    So: **report always, rank only when told.** With no `current_a` this is an
    `info` stating what the copper carries and asking for the number that would
    turn it into a verdict. With one, it is ranked against the declared
    current, at the declared rise.

    The default rise is 10 C for everything, deliberately — *"assume 10c rise
    is the max for all cases that arent specified as being allowed to run
    hotter"*. A pulsed conductor legitimately runs hotter, and that is the
    designer's statement to make in `rise_c`, not this tool's to assume. Where
    a net looks like it might not be continuous, the finding says so and asks.
    """
    findings: list[Finding] = [_width_ampacity_table(board,
                                                     copper_oz_by_layer)]
    for net in sorted(power_nets):
        # Read from the declaration directly: the checks run before
        # `netclass.classify()`, so the NetClassMap does not exist yet.
        declaration = net_intent.find(net) if net_intent else None
        declared_a = getattr(declaration, "current_a", None)
        rise = getattr(declaration, "rise_c", None) or current_module.RISE_C

        # **A per-net line needs real power evidence, or a declaration.**
        # `power_nets` is `rails | grounds | power_by_width`, and the width
        # rule is a heuristic: on one six-layer board it swept in 58 nets,
        # including CAN signal pairs and GPIO. Emitting one finding for each
        # buried the four that mattered under fifty-seven that did not.
        #
        # This is within the ruling rather than a narrowing of it — *"If the
        # current requirements aren't given then I can understand if the
        # ampacity isn't reported for a perfectly uniform net, IF IT'S NOT A
        # POWER NET"*. A net swept in by track width alone is exactly that
        # case, and nothing is hidden: every width on the board, including
        # theirs, is in the table above.
        if (declared_a is None and evidenced is not None
                and net not in evidenced):
            continue
        measured = current_module.ampacity(board, net, copper_oz_by_layer,
                                           rise_c=rise)
        if not measured.measured:
            continue          # no copper of any kind: nothing to state

        available = measured.weakest_a
        headline_source = "its weakest routed stretch carries"
        if available is None:
            # A poured net has no single width to reduce to, so the barrels
            # feeding it are the only ampacity number that exists. Saying so
            # beats saying nothing, which is what used to happen.
            available = measured.via_capacity_a
            headline_source = "the vias feeding its pour carry"

        detail = []
        if measured.weakest_a is not None:
            detail.append(
                f"weakest routed stretch {measured.weakest_a:.2f} A — "
                f"{measured.weakest_mm:.3f} mm on {measured.weakest_layer}, at "
                f"a {rise:.0f} C rise. Weakest by CAPACITY, not by width: "
                f"inner layers are thinner copper")
            if (measured.weakest_any_a is not None
                    and measured.weakest_any_a < measured.weakest_a):
                detail.append(
                    f"a shorter stretch is weaker still: "
                    f"{measured.weakest_any_a:.2f} A over "
                    f"{measured.weakest_any_length_mm:.2f} mm of "
                    f"{measured.weakest_any_mm:.3f} mm on "
                    f"{measured.weakest_any_layer}. Physically that carries the "
                    f"whole current too; it is below the significance floor "
                    f"only so a pad entry does not become every net's headline")
        if measured.via_capacity_a is not None:
            detail.append(
                f"{measured.vias} via(s) carry it between layers — "
                f"{measured.via_capacity_a:.2f} A in parallel, assuming "
                f"{current_module.VIA_PLATING_UM:.0f} um plating. NOT combined "
                f"with the track figure: which vias sit in series with which "
                f"stretch is a topology question this does not answer")
        if measured.poured_mm2 > 0:
            detail.append(
                f"{measured.poured_mm2:.1f} mm2 of poured copper, which has no "
                f"single width and is not reduced to one here")

        common = {
            "net": net,
            "weakest_a": measured.weakest_a,
            "weakest_mm": measured.weakest_mm,
            "weakest_layer": measured.weakest_layer,
            "weakest_any_a": measured.weakest_any_a,
            "via_capacity_a": measured.via_capacity_a,
            "vias": measured.vias,
            "poured_mm2": measured.poured_mm2,
            "rise_c": rise,
            "by_width": {f"{layer} {width}": value
                         for (layer, width), value in
                         measured.by_width.items()},
        }
        basis = (f"IPC-2152 at a {rise:.0f} C rise on the measured width of "
                 f"every routed stretch, taking each stretch's own layer and "
                 f"copper weight; via barrels as IPC-2152 conductors of "
                 f"circumference width and plating thickness")

        if declared_a is None:
            findings.append(Finding(
                check="current_capacity", severity="info", score=0.0,
                # "GND carries 0.45 A" reads as though a plane were limited
                # by a stub. Name what was measured, not the net.
                title=(f"{net} — {headline_source} {available:.2f} A "
                       f"at a {rise:.0f} C rise. No current was declared for "
                       f"it, so this is a fact about the copper and not a "
                       f"verdict"),
                nets=[net],
                layers=[measured.weakest_layer] if measured.weakest_layer
                else [],
                detail=detail + [
                    "to turn this into a pass or a fail, declare the current "
                    "in the net-intent manifest: `current_a:`, and `rise_c:` "
                    "if this net is allowed to run hotter than "
                    f"{current_module.RISE_C:.0f} C",
                    "IF THIS NET IS NOT CONTINUOUS — a firing channel, a "
                    "burn wire, an inrush path — ASK THE USER how long it "
                    "carries current and what rise they accept. A conductor "
                    "energised for a second once per flight is judged wrongly "
                    f"against a continuous {current_module.RISE_C:.0f} C rise",
                ],
                measurement=common, basis=basis,
                intent_needed=("how much current this net carries, for how "
                               "long, and what temperature rise is acceptable "
                               "— all three are the designer's to state")))
            continue

        ratio = declared_a / available if available else float("inf")
        severity, score = _severity(ratio, AMPACITY_MINOR, AMPACITY_MAJOR)
        if ratio >= AMPACITY_CRITICAL:
            severity, score = "critical", max(score, ratio)
        findings.append(Finding(
            check="current_capacity", severity=severity, score=score,
            title=(f"{net} is declared to carry {declared_a:.2f} A and "
                   f"{headline_source} {available:.2f} A at a "
                   f"{rise:.0f} C rise "
                   f"— {ratio:.2f}x "
                   + ("OVER its capacity" if ratio >= 1.0 else
                      "of its capacity")),
            nets=[net],
            layers=[measured.weakest_layer] if measured.weakest_layer else [],
            detail=detail + [
                f"declared {declared_a:.2f} A at a permitted {rise:.0f} C rise, "
                f"from the net-intent manifest",
                ("the conductor cannot deliver the declared current at the "
                 "declared rise; it will run hotter than stated"
                 if ratio >= 1.0 else
                 f"{(1 - ratio) * 100:.0f}% margin against the declared "
                 f"current"),
            ],
            measurement={**common, "declared_a": declared_a,
                         "ratio_declared_to_available": round(ratio, 3)},
            basis=basis,
            assumption=(f"a {rise:.0f} C rise is acceptable on this net, which "
                        f"the manifest states; IPC-2152's still-air chart for "
                        f"every layer; {current_module.VIA_PLATING_UM:.0f} um via "
                        f"plating"),
            intent_needed=(None if rise != current_module.RISE_C else
                           "whether this net is continuous — if it fires for a "
                           "second and then rests, a higher `rise_c` is "
                           "legitimate and this verdict is too strict")))
    return findings


def check_current_capacity(board: Board, power_nets: set[str],
                           copper_oz_by_layer: dict,
                           net_intent=None,
                           evidenced_power: set[str] | None = None
                           ) -> list[Finding]:
    """Where a power net's copper pinches, and how it compares to its siblings.

    §32.B9. `conductor_neck` looks only at tracks, so a board whose power bus
    is a *pour* got no conducted-current analysis at all — which is how a 40 A
    plane went unexamined on the board that prompted this (§43, §44.3).

    The headline is **relative**: a terminal whose feed resistance is an outlier
    against its own net's median. That needs no current, which is what makes it
    reportable before the intent manifest exists — and it is exactly how the one
    real constriction found so far surfaced, at 9.3x its siblings.

    The absolute IPC-2152 capacity is reported beside it and carries
    `intent_needed`, because turning it into a verdict needs the current.

    Method is chosen per net and stated on every finding: resistance for poured
    copper, erosion bottleneck for tracked. Erosion on a plane is wrong and the
    module docstring says why at length.
    """
    findings: list[Finding] = []
    coverage = current_module.coverage(board, power_nets)
    examined = 0
    #: net -> why `worst_feed` returned nothing for it. `worst_feed` has always
    #: written a reason into its meta dict; this consumer threw it away with a
    #: bare `continue`, so a net with four terminals whose vias could not be
    #: located was indistinguishable from one that was solved and found clean.
    #: `coverage['nets_examined']` counts nets with enough *terminals*, which
    #: is not the same set — and the old title printed one number over the
    #: other's list.
    declined: dict[str, str] = {}

    for net in sorted(power_nets):
        feeds, notes = current_module.worst_feed(board, net,
                                                 copper_oz_by_layer)
        if not feeds:
            if net in coverage["nets_examined"]:
                declined[net] = notes.get(
                    "skipped", "no reason was recorded, which is itself a bug")
            continue
        values = [(f, f.resistance_ohm if f.resistance_ohm is not None
                   else (1.0 / f.capacity_a if f.capacity_a else None))
                  for f in feeds]
        values = [(f, v) for f, v in values if v]
        if len(values) < FEED_MIN_TERMINALS:
            # `worst_feed`'s own reason wins when it has one. A net with ten
            # terminals on the plane and two solved feeds was reported here as
            # "only 2 produced a comparable number", which names the floor for
            # a loss that happened in the plane solve — the floor was innocent
            # and the wrong thing would have been changed (§54.5 item 4).
            declined[net] = notes.get("skipped") or (
                f"{len(feeds)} feed(s) were solved but only "
                f"{len(values)} produced a comparable number, "
                f"and {FEED_MIN_TERMINALS} are needed to compare "
                f"a terminal against its siblings")
            continue
        median = statistics.median(v for _, v in values)
        if median <= 0:
            declined[net] = ("the median feed resistance came out at or below "
                             "zero, so no ratio could be formed")
            continue
        # Counted here rather than on entry: a net whose feeds were solved and
        # then found incomparable was previously counted as examined.
        examined += 1

        for feed, value in sorted(values, key=lambda kv: -kv[1]):
            ratio = value / median
            if ratio < FEED_RATIO_MINOR:
                continue
            severity, score = _severity(ratio, FEED_RATIO_MINOR,
                                        FEED_RATIO_MAJOR)
            if feed.method == "plane_resistance":
                headline = (f"{feed.resistance_ohm * 1e3:.1f} mOhm from "
                            f"{notes['source']}, {ratio:.1f}x the "
                            f"{median * 1e3:.1f} mOhm its siblings see")
                how = (f"solved on the {feed.layer} pour at "
                       f"{current_module.PLANE_CELL_MM} mm, "
                       f"{notes.get('cells', 0)} cells, sheet resistance "
                       f"{notes.get('sheet_ohm_sq', 0) * 1e3:.3f} mOhm/sq")
            else:
                headline = (f"narrowest channel {feed.bottleneck_mm:.2f} mm on "
                            f"{feed.layer}, {ratio:.1f}x the resistance its "
                            f"siblings see")
                how = (f"erosion bottleneck on {feed.layer}; IPC-2152 gives "
                       f"{feed.capacity_a:.1f} A at "
                       f"{current_module.RISE_C:.0f} C rise")

            detail = [how,
                      f"{len(values)} terminal(s) on this net were compared; "
                      f"the source is {notes['source']}, taken as the terminal "
                      f"with the most pad copper" +
                      (f" of those on the copper region being solved — "
                       f"{notes['source_rejected']} has more, but no contact "
                       f"point on it" if notes.get("source_rejected") else "")]
            if notes.get("off_region"):
                detail.append(
                    f"{notes['off_region']} further terminal(s) reach this "
                    f"pour on a different one of its {notes['regions']} "
                    f"disjoint copper regions and are not part of this "
                    f"comparison — a two-terminal resistance is only "
                    f"comparable within one sheet")
            if feed.via_capacity_a is not None:
                detail.append(
                    f"{feed.vias} via(s) carry this terminal between layers — "
                    f"{feed.via_capacity_a:.1f} A total at "
                    f"{current_module.RISE_C:.0f} C rise, assuming "
                    f"{current_module.VIA_PLATING_UM:.0f} um plating")

            findings.append(Finding(
                check="current_capacity", severity=severity, score=score,
                title=f"{net} reaches {feed.terminal} through {headline}",
                detail=detail,
                nets=[net], layers=[feed.layer] if feed.layer else [],
                location=feed.location,
                measurement={
                    "net": net, "terminal": feed.terminal,
                    "method": feed.method,
                    "resistance_ohm": (round(feed.resistance_ohm, 8)
                                       if feed.resistance_ohm is not None
                                       else None),
                    "bottleneck_mm": (round(feed.bottleneck_mm, 4)
                                      if feed.bottleneck_mm is not None
                                      else None),
                    "capacity_a": (round(feed.capacity_a, 3)
                                   if feed.capacity_a is not None else None),
                    "median_ohm": round(median, 8),
                    "ratio_to_median": round(ratio, 2),
                    "terminals_compared": len(values),
                    "source_terminal": notes["source"],
                    "vias": feed.vias,
                    "via_capacity_a": (round(feed.via_capacity_a, 2)
                                       if feed.via_capacity_a is not None
                                       else None),
                },
                basis=("two-terminal DC resistance from a Laplace solve on the "
                       "poured copper, one factorisation and one solve per "
                       "terminal" if feed.method == "plane_resistance" else
                       "largest erosion radius that still joins the two pads, "
                       "doubled — the narrowest channel between them"),
                assumption=(f"via plating {current_module.VIA_PLATING_UM:.0f} um, "
                            "which the fab has not stated and which scales via "
                            "capacity directly"),
                intent_needed=("how much current this terminal draws — the "
                               "comparison against its siblings needs none, "
                               "but turning it into a verdict does")))

    # How much the capacity varies along each power net's own length. Needs no
    # terminals and no plane, so it runs on nets `worst_feed` cannot examine.
    # The absolute ampacity of every power net, whether or not anything about
    # it varies. Field report, 2026-08-05: four firing channels 0.600 mm along
    # their entire length produced **nothing** — `worst_feed` declined them for
    # too few terminals and the width spread returned early *because the net is
    # uniform*. "A trace that is 3x too thin along 100% of its length is
    # indistinguishable from a perfect one."
    findings.extend(_ampacity_findings(board, power_nets, copper_oz_by_layer,
                                       net_intent, evidenced_power))

    spreads = 0
    for net in sorted(power_nets):
        profile = current_module.width_profile(board, net, copper_oz_by_layer)
        if profile is None:
            continue
        spreads += 1
        ratio = profile.capacity_ratio
        if ratio < CAPACITY_SPREAD_MINOR:
            continue
        severity, score = _severity(ratio, CAPACITY_SPREAD_MINOR,
                                    CAPACITY_SPREAD_MAJOR)
        findings.append(Finding(
            check="current_capacity", severity=severity, score=score,
            title=(f"{net} carries {profile.weakest_capacity_a:.2f} A where it "
                   f"is weakest and {profile.strongest_capacity_a:.2f} A where "
                   f"it is strongest — {ratio:.1f}x along one net"),
            detail=[
                f"weakest: {profile.weakest_mm:.3f} mm on "
                f"{profile.weakest_layer}; strongest: "
                f"{profile.strongest_mm:.3f} mm",
                f"routed lengths by width: {profile.by_width} "
                f"(of {profile.total_mm:.1f} mm total)",
                "the net carries what its weakest stretch carries; the wide "
                "part is what the designer thought it needed",
                "not the same as `conductor_neck`, which reports only a narrow "
                "stretch that strands pads",
            ],
            nets=[net], layers=[profile.weakest_layer],
            measurement={
                "net": net,
                "weakest_mm": profile.weakest_mm,
                "strongest_mm": profile.strongest_mm,
                "weakest_capacity_a": round(profile.weakest_capacity_a, 3),
                "strongest_capacity_a": round(profile.strongest_capacity_a, 3),
                "capacity_ratio": round(ratio, 2),
                "by_width_mm": profile.by_width,
                "total_mm": profile.total_mm,
            },
            basis=(f"IPC-2152 at {current_module.RISE_C:.0f} C rise per routed "
                   f"width, taking each width's weakest layer; stretches under "
                   f"{current_module.SIGNIFICANT_LENGTH_FRACTION:.0%} of the "
                   f"net are ignored as pad entries"),
            intent_needed=("how much current this net carries — the variation "
                           "is a fact about the copper, the verdict is not")))

    # The title used to read "{examined} power net(s) had enough terminals to
    # compare" over a detail line listing `coverage['nets_examined']` — but
    # `examined` counts nets that produced a *comparison* and `nets_examined`
    # counts nets with enough *terminals*. Those are different sets, and the
    # gap between them is exactly the population that was silently declined.
    candidates = coverage["nets_examined"]
    findings.append(Finding(
        check="current_capacity", severity="info", score=0.0,
        title=(f"{examined} of {len(candidates)} power net(s) with enough "
               f"terminals were actually compared; "
               f"{len(coverage['nets_with_too_few_terminals'])} had too few"),
        detail=[f"enough terminals (needs {current_module.MIN_TERMINALS}, one "
                f"of which is the source, to give the {FEED_MIN_TERMINALS} "
                f"comparable feeds a median needs a middle of): "
                f"{', '.join(candidates) or 'none'}",
                f"too few terminals: "
                f"{coverage['nets_with_too_few_terminals'] or 'none'}"]
        + ([f"found but NOT compared, with the reason each time:"]
           + [f"    {net} — {why}" for net, why in sorted(declined.items())]
           if declined else
           ["every net with enough terminals was compared"])
        + ["a zero here is 'looked and found nothing', not 'did not look' — "
           "and a net in the candidate list that is not in the compared count "
           "was not measured, however encouraging the totals look"],
        measurement={"coverage": {**coverage, "width_profiles": spreads,
                                  "nets_compared": examined,
                                  "nets_declined": declined}},
        basis="`current.coverage()` for the candidates, and `worst_feed`'s own "
              "recorded reason for each net it declined"))
    return findings


def check_bom_parity(schematic: Schematic,
                     comparison: "bom_module.BomComparison | None"
                     ) -> list[Finding]:
    """The BOM against the schematic — a third document, and the one built from.

    `check_parity` compares the schematic against the *board*. This compares it
    against the file the assembler reads, which can disagree with both and is
    the only one that decides what gets soldered on.
    """
    findings: list[Finding] = []
    if comparison is None:
        findings.append(Finding(
            check="bom_parity", severity="info", score=0.0,
            title="no BOM was compared — none was supplied or found beside the board",
            detail=["a BOM can disagree with the schematic and with the board, "
                    "and it is the document the assembler works from",
                    "supply one, or export `<board>.csv` beside the board"],
            measurement={"coverage": bom_module.coverage(None)},
            basis="the absence of a file, reported rather than assumed clean"))
        return findings

    for reference, on_schematic, in_bom in comparison.value_mismatches:
        findings.append(Finding(
            check="bom_parity", severity="major", score=2.0,
            title=(f"{reference} is '{on_schematic}' in the schematic and "
                   f"'{in_bom}' in the BOM"),
            detail=["the BOM is what gets ordered and assembled",
                    "this does not judge whether the two parts are "
                    "interchangeable — that needs both datasheets"],
            measurement={"reference": reference, "schematic": on_schematic,
                         "bom": in_bom},
            basis="designator-keyed comparison of the two documents' value "
                  "fields, whitespace and case insensitive",
            intent_needed="whether the substitution was deliberate"))

    if comparison.missing_from_bom:
        names = comparison.missing_from_bom
        findings.append(Finding(
            check="bom_parity", severity="major",
            score=len(names) / 2.0,
            title=(f"{len(names)} part(s) are in the schematic and not in the "
                   f"BOM, and are not marked DNP"),
            detail=[", ".join(names[:20])
                    + (f" ... and {len(names) - 20} more"
                       if len(names) > 20 else ""),
                    "these will not be ordered and will not be fitted",
                    "a BOM exported before the schematic was finished looks "
                    "exactly like this"],
            measurement={"missing_from_bom": names,
                         "coverage": comparison.summary()},
            basis="schematic designators absent from the BOM, less those the "
                  "schematic marks DNP and those the BOM marks excluded",
            intent_needed="whether each was left off deliberately"))

    if comparison.not_in_schematic:
        names = comparison.not_in_schematic
        findings.append(Finding(
            check="bom_parity", severity="minor", score=1.0,
            title=(f"{len(names)} BOM line(s) name a part the schematic does "
                   f"not have"),
            detail=[", ".join(names[:20])
                    + (f" ... and {len(names) - 20} more"
                       if len(names) > 20 else ""),
                    "a stale export: the part number ordered belongs to a "
                    "design that no longer exists"],
            measurement={"not_in_schematic": names},
            basis="BOM designators absent from the schematic"))

    findings.append(Finding(
        check="bom_parity", severity="info", score=0.0,
        title=(f"{comparison.compared} part(s) compared against the BOM; "
               f"{len(comparison.value_mismatches)} value disagreement(s)"),
        detail=[f"BOM: {comparison.source_path}",
                f"marked DNP and correctly absent: "
                f"{comparison.absent_but_marked or 'none'}",
                "a zero here is 'compared and agreed', not 'did not compare'"],
        measurement={"coverage": comparison.summary()},
        basis="`bom.compare()`, counted before any threshold was applied"))
    return findings


def check_heat_path(board: Board,
                    manifest: thermal_module.ThermalManifest | None
                    ) -> list[Finding]:
    """The heatsink each dissipating part actually has, against JEDEC's.

    `check_thermal` computes Tj from a datasheet theta_JA and says on every
    finding that theta_JA is a property of JESD51-7's test card and not of this
    board. This check measures the gap, and it is the layout half of the same
    question: on the board that prompted it, both `critical` junction
    temperatures had the same concrete cause — **no via inside either FET's
    thermal tab** — and nothing said so (§43, §44.3).

    **Scoped to the manifest on purpose.** Run over every large pad on a board
    it reports capacitor pads, connector shells and inductor terminals, none of
    which are heat sources. The manifest is the list of parts a human read a
    datasheet for, so it is the only list here that knows what dissipates. With
    no manifest this reports coverage and nothing else, exactly as
    `check_thermal` does.
    """
    findings: list[Finding] = []
    if not manifest:
        return findings

    references = set(manifest.parts)
    pads = heatsink_module.thermal_pads(board, references)
    coverage = heatsink_module.coverage(board, references)

    if not pads:
        findings.append(Finding(
            check="heat_path", severity="info", score=0.0,
            title=(f"none of the {len(references)} part(s) with thermal data "
                   "has a pad large enough to be a thermal tab"),
            detail=["so there is no bottom-cooled heat path to examine — these "
                    "parts get rid of their heat through their leads, and "
                    "theta_JA already accounts for that",
                    f"a pad counts as a tab at "
                    f"{heatsink_module.TAB_MIN_RATIO:.0f}x its own part's "
                    f"median pad area"],
            measurement={"coverage": coverage},
            basis="pad copper outlines from the board, merged per part pin"))
        return findings

    for pad in sorted(pads, key=lambda p: -p.area_mm2):
        part = manifest.parts.get(pad.reference)
        if part is None:
            continue
        watts = part.dissipation_w

        if not pad.has_path_to_plane and watts >= HEAT_PATH_MINOR_W:
            severity, score = _severity(watts, HEAT_PATH_MINOR_W,
                                        HEAT_PATH_MAJOR_W)
            near = (f"{pad.vias_within_2mm} within 2 mm, nearest "
                    f"{pad.nearest_via_mm:.2f} mm away"
                    if pad.nearest_via_mm is not None
                    else "and none anywhere on the net")
            findings.append(Finding(
                check="heat_path", severity=severity, score=score,
                title=(f"{pad.reference} dissipates {watts:.2f} W through a "
                       f"{pad.area_mm2:.1f} mm2 tab with no via inside it"),
                detail=[
                    f"pad {pad.designator_text()} on {pad.net}: 0 vias inside "
                    f"the tab outline, {near}",
                    "a bottom-cooled package moves nearly all of its heat "
                    "through the tab, and the tab can only reach an inner "
                    "plane through vias in it — vias around it conduct only "
                    "after spreading laterally through the outer copper",
                    f"attached copper {pad.attached_copper_mm2:.0f} mm2 = "
                    f"{100 * pad.jedec_fraction:.0f}% of one JESD51-7 plane "
                    f"layer",
                ],
                nets=[pad.net],
                location=None,
                measurement={
                    "reference": pad.reference, "pad": pad.pad_number,
                    "dissipation_w": watts,
                    "tab_area_mm2": pad.area_mm2,
                    "vias_in_pad": pad.vias_in_pad,
                    "vias_within_2mm": pad.vias_within_2mm,
                    "nearest_via_mm": pad.nearest_via_mm,
                    "attached_copper_mm2": pad.attached_copper_mm2,
                    "copper_by_layer": pad.copper_by_layer,
                    "jedec_plane_fraction": round(pad.jedec_fraction, 3),
                },
                basis=("via centres tested against the pad's own copper "
                       "outline, with multi-pad exposed pads merged back into "
                       "one tab"),
                intent_needed=("whether this part is meant to be heatsunk "
                               "through its tab at all — some are cooled "
                               "through their leads by design")))

        if (watts >= HEAT_PATH_MINOR_W
                and pad.jedec_fraction < HEAT_SPREAD_MINOR_FRACTION):
            # score on how far *below* the threshold it is, so a starved pad
            # outranks a merely small one
            shortfall = HEAT_SPREAD_MINOR_FRACTION / max(pad.jedec_fraction,
                                                         1e-4)
            severity = ("major"
                        if pad.jedec_fraction < HEAT_SPREAD_MAJOR_FRACTION
                        else "minor")
            findings.append(Finding(
                check="heat_path", severity=severity, score=shortfall,
                title=(f"{pad.reference} has {pad.attached_copper_mm2:.0f} mm2 "
                       f"of copper on its tab — "
                       f"{100 * pad.jedec_fraction:.0f}% of the plane its "
                       f"datasheet theta_JA was measured over"),
                detail=[
                    f"JESD51-7's test card is 76.2 x 114.3 mm with two solid "
                    f"one-ounce planes — "
                    f"{heatsink_module.JESD51_7_PLANE_MM2:.0f} mm2 per layer",
                    f"here: {pad.copper_by_layer}",
                    "less copper than the card can only make theta_JA worse, "
                    "never better, so the junction temperature reported for "
                    "this part is optimistic by an amount this does not try "
                    "to convert into degrees",
                ],
                nets=[pad.net],
                measurement={
                    "reference": pad.reference, "pad": pad.pad_number,
                    "dissipation_w": watts,
                    "attached_copper_mm2": pad.attached_copper_mm2,
                    "copper_by_layer": pad.copper_by_layer,
                    "jesd51_7_plane_mm2": round(
                        heatsink_module.JESD51_7_PLANE_MM2, 1),
                    "jedec_plane_fraction": round(pad.jedec_fraction, 3),
                },
                basis=("copper on the same net in the island touching the "
                       "tab, summed over layers, against one JESD51-7 plane"),
                assumption=("that the datasheet's theta_JA is a JEDEC figure. "
                            "Where `source` says otherwise this comparison "
                            "does not apply"),
                intent_needed=("the real convection environment — a ratio of "
                               "copper areas is not a thermal resistance")))

    findings.append(Finding(
        check="heat_path", severity="info", score=0.0,
        title=(f"{len(pads)} thermal tab(s) examined across "
               f"{len(references)} part(s) with datasheet data"),
        detail=[f"{coverage['pads_with_no_path_to_plane']} of them have no via "
                f"inside the tab",
                "a zero here is 'looked and found nothing', not 'did not look'"],
        measurement={"coverage": coverage},
        basis="`heatsink.coverage()`, counted before any threshold was applied"))
    return findings


def check_board_state(board: Board, pcb_path=None) -> list[Finding]:
    """Things about the file itself rather than about the design.

    Given ``pcb_path`` this runs KiCad's own DRC to name the nets behind the
    unconnected count. §36 found why that matters: this board's bare "1
    unconnected item" is in fact the processor module's 3.3 V output not reaching a level
    shifter's supply pin — a functional defect that a count cannot express, and
    one the ratsnest number under-reports (it says 1 where DRC says 2).

    The DRC costs ~4 s and is only run when there is something to explain.
    """
    findings: list[Finding] = []
    fill = board._data.get("zone_fill", {})
    if fill.get("was_stale"):
        findings.append(Finding(
            check="board_state", severity="major", score=5.0,
            title="the saved zone fills are stale",
            detail=[f"saved fill {fill.get('area_before_mm2')} mm2, refilled "
                    f"{fill.get('area_after_mm2')} mm2",
                    "this analysis used the refilled geometry; anything "
                    "exported from the file as saved would not match"],
            measurement=fill,
            basis="zones refilled in memory at extraction and the areas compared"))

    unconnected = board.connectivity.get("unconnected_count")
    if unconnected:
        by_net, drc_count, why_not = _unconnected_nets(pcb_path)
        # DRC is the authoritative count and the only one that names nets. It
        # is *not* the same measurement as the ratsnest: the unreferenced four-layer board
        # reads 1 here and 2 from `kicad-cli pcb drc` on the identical refilled
        # board (§36). Where DRC ran, report its answer and say so.
        detail = ["a deliberately unrouted section — a snap-off panel, a "
                  "no-fit option — looks exactly like this"]
        measurement = {"unconnected": unconnected,
                       "source": "CONNECTIVITY_DATA::GetUnconnectedCount"}
        if by_net:
            detail = [
                f"{name}: {count} missing connection(s)"
                for name, count in sorted(by_net.items())
            ] + ["`pcbreview.drc.run_drc` names the pads either side of each",
                 ] + detail
            if drc_count != unconnected:
                detail.append(
                    f"KiCad's ratsnest counts {unconnected} and its DRC counts "
                    f"{drc_count} on this same file — two different "
                    "measurements, and the DRC list is the one to act on")
            measurement["nets"] = dict(sorted(by_net.items()))
            measurement["drc_unconnected"] = drc_count
            title = (f"{len(by_net)} net(s) are left open on the board: "
                     f"{', '.join(sorted(by_net))}")
            score = float(max(unconnected, drc_count))
        else:
            detail.append(
                "this is KiCad's ratsnest count, which can read lower than "
                "`kicad-cli pcb drc` on the same board; run the DRC for the "
                "authoritative list, and treat that as the real one")
            if why_not:
                detail.append(f"the per-net breakdown is missing because the "
                              f"DRC could not be run — {why_not}")
            title = f"KiCad reports {unconnected} unconnected item(s)"
            score = float(unconnected)
        findings.append(Finding(
            check="board_state", severity="minor", score=score,
            title=title, detail=detail, measurement=measurement,
            basis=("`kicad-cli pcb drc` unconnected_items, on the refilled "
                   "board" if by_net else
                   "KiCad's own connectivity engine, on the refilled board"),
            intent_needed="whether any part of this board is meant to be unrouted"))
    return findings


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

def audit(board: Board, schematic: Schematic | None = None,
          ground_nets: set[str] | None = None,
          rise_time_ps: float = crosstalk_module.REFERENCE_RISE_TIME_PS,
          skip: tuple[str, ...] = (),
          crosstalk_cache=None,
          thermal: thermal_module.ThermalManifest | None = None,
          net_intent: "netclass_module.NetIntent | None" = None,
          bom_comparison=None,
          pcb_path=None,
          progress=None,
          solve_store=None,
          power_paths=None) -> AuditReport:
    """Run every check the board's data supports, over a warm solve cache.

    A thin wrapper around `_audit`, and the only thing it adds is
    `solvestore`. The field solver is 85% of this function and the cache is
    what makes a *second* review of a board that barely moved cost seconds
    instead of a minute, so it is on by default — but it is a plain argument
    and not a hidden global, and it takes the same three states every optional
    input to `audit_files` takes:

    ``None``
        the shared persistent cache, or the one a caller already attached.
    ``False``
        none. Solve everything, as this function did before the cache existed.
    a `solvestore.SolveStore`, or a path
        that one. `--verify-cache` and the tests both go through here.

    **It cannot change a finding**, and that is the property the whole design
    protects: a cached solve is the same two capacitance matrices the solver
    produced, and every number is derived from them by the same arithmetic
    either way. The design notes record the four-board check that this holds.
    """
    with solvestore.for_audit(solve_store):
        return _audit(board, schematic, ground_nets, rise_time_ps, skip,
                      crosstalk_cache, thermal, net_intent, bom_comparison,
                      pcb_path, progress, power_paths)


def _audit(board: Board, schematic: Schematic | None = None,
           ground_nets: set[str] | None = None,
           rise_time_ps: float = crosstalk_module.REFERENCE_RISE_TIME_PS,
           skip: tuple[str, ...] = (),
           crosstalk_cache=None,
           thermal: thermal_module.ThermalManifest | None = None,
           net_intent: "netclass_module.NetIntent | None" = None,
           bom_comparison=None,
           pcb_path=None,
           progress=None,
           power_paths=None) -> AuditReport:
    """Run every check the board's data supports and rank what they found.

    Refuses without a bound stackup. That is Gate 1, and it is not negotiable:
    every impedance, crosstalk and reference-plane result in here depends on
    real dielectric heights, and without them the whole report would be
    confident nonsense rather than obviously absent.

    Pass a ``schematic`` and the ground nets, the differential pairs and the
    board/schematic parity all come from the circuit rather than from net names.
    Without one the audit still runs, on the name heuristics, and says so.

    ``thermal`` is deliberately *not* a gate — see `check_thermal`. Without one
    the audit runs in full and states which parts went unassessed.

    Watching a long run
    -------------------
    ``progress`` is called as ``progress(event, check_name, result)`` with
    ``event`` in ``{"start", "done", "skipped"}``; ``result`` is the check's
    `CheckResult` on the last two and ``None`` on the first. It exists because
    this audit is 160 s on a 4-layer board and a front end has nothing to show
    for it otherwise — `checks` is only readable once the whole thing has
    returned.

    It is **observation only**. It cannot change what runs, in what order, or
    what any check concludes; an exception raised inside it is swallowed rather
    than allowed to fail a check. Leave it ``None`` and this function behaves
    exactly as it did before it existed, which is what the byte-identity of the
    four example boards' findings rests on.
    """
    if board.stackup is None:
        raise AuditError(
            "the audit needs a validated fabrication stackup — call "
            "board.bind_stackup(). Without real dielectric heights every "
            "impedance and coupling result would be invented.")

    started = time.time()
    findings: list[Finding] = []
    checks: list[CheckResult] = []
    assumptions: list[str] = []

    inferred = None
    intent_evidence: list[str] = []
    if schematic is not None:
        inferred = intent_module.infer(schematic, board)
        if ground_nets is None:
            ground_nets = inferred.ground_nets
        intent_evidence = [f"{r.net}: {r.role} ({r.confidence:.2f}) — "
                           f"{r.evidence}" for r in inferred.roles]
    if ground_nets is None:
        ground_nets = {n.name for n in board.nets.values()
                       if intent_module.looks_like_ground(n.name)}
        assumptions.append(
            "no schematic given, so ground nets come from net names — "
            f"{sorted(ground_nets)}. A rail called 0V or RTN would be missed "
            "and a signal called GND_SW would be taken for a plane.")
    rail_nets = inferred.rail_nets if inferred else set(ground_nets)
    pairs = ([(p.positive, p.negative) for p in inferred.pairs]
             if inferred else [])

    signal_nets = [n.name for n in board.nets.values()
                   if n.name and n.name not in rail_nets
                   and board.tracks_on_net(n.code)]

    def announce(event: str, name: str, result: CheckResult | None = None):
        """Tell the caller a check started or finished. Never raise.

        A progress callback is the one thing a long audit owes a front end, and
        it has to come from in here: `run` is the only place that knows a check
        has begun, and by the time this function returns the whole 160 s is
        over. What it must **not** do is influence the audit. A callback that
        raises would turn a display concern into a failed check, so it is
        swallowed — the run is the artefact, the progress bar is not.
        """
        if progress is None:
            return
        try:
            progress(event, name, result)
        except Exception:                             # noqa: BLE001
            pass

    def run(name: str, function, note=None):
        """`note` may be a callable, which is evaluated *after* the check ran.

        That is not a nicety. A coverage line has to carry the number that
        makes a zero readable — how many nets were examined, not how many
        targets were loaded — and on two checks that number only exists once
        the check has run. `impedance 0 finding(s) (1 fab target(s))` read as
        coverage on an outside board when it meant the opposite: one target
        loaded and zero nets matched (§48.3 item 3).
        """
        if name in skip:
            result = CheckResult(name, ran=False,
                                 skipped_because="skipped by the caller")
            checks.append(result)
            announce("skipped", name, result)
            return
        announce("start", name)
        start = time.time()
        try:
            produced = function()
        except Exception as exc:                      # noqa: BLE001
            result = CheckResult(name, ran=False,
                                 skipped_because=f"{type(exc).__name__}: {exc}")
            checks.append(result)
            announce("done", name, result)
            return
        findings.extend(produced)
        result = CheckResult(name, ran=True, seconds=time.time() - start,
                             findings=len(produced),
                             note=note() if callable(note) else note)
        checks.append(result)
        announce("done", name, result)

    resolver = reference_module.ReferenceResolver(board, ground_nets=ground_nets)
    division = panel.partition(board)

    run("board_state", lambda: check_board_state(board, pcb_path))
    drc_report, _ = _drc_report(pcb_path)
    run("drc", lambda: check_drc(pcb_path),
        note=(f"{len(drc_report.violations)} violation(s): "
              f"{len(drc_report.errors)} error, "
              f"{len(drc_report.warnings)} warning"
              if drc_report is not None else
              "KiCad's DRC could not be run — rules were not checked"))
    run("stackup_mismatch", lambda: check_stackup_mismatch(board))
    run("panelisation", lambda: check_panelisation(board, division),
        note=(f"{len(division.regions)} copper region(s)"))
    impedance_examined: list[str] = []
    run("impedance",
        lambda: check_impedance(board, ground_nets, pairs,
                                examined=impedance_examined),
        note=lambda: (
            f"{len(board.stackup.impedance_targets)} fab target(s)"
            + (f" + {len(board.stackup.mirrored_targets())} mirrored onto the "
               f"far side of a symmetric stackup"
               if board.stackup.mirrored_targets() else "")
            + f", {len(impedance_examined)} net(s) examined"))
    run("return_path", lambda: check_return_path(resolver, signal_nets),
        note=f"{len(signal_nets)} signal nets")
    run("layer_transition",
        lambda: check_layer_transitions(resolver, signal_nets, rise_time_ps),
        note=(f"stitch limit "
              f"{reference_module.stitching_distance_limit_mm(rise_time_ps, fraction=STITCH_LIMIT_FRACTION):.1f} mm "
              f"at {rise_time_ps:.0f} ps"))
    pairs_found: list[str] = []
    run("differential_pair",
        lambda: check_differential_pairs(board, schematic, found=pairs_found),
        note=lambda: (f"{len(pairs_found)} pair(s) found from "
                      + ("device pin names and net names" if schematic
                         else "net names only")))
    cache = crosstalk_cache or crosstalk_module.SolveCache()
    adjacent = [f"{u}/{l}" for u, l in routed.adjacent_signal_layers(board)]
    # These two checks are 85% of the run and almost all of that is inside the
    # field solver, so they get a warm process pool for exactly as long as they
    # need one. It spans both because they share `cache`, which is worth about
    # 300 s on the four-layer board — and because a pool built twice would pay
    # its spawn-and-import cost twice.
    with solvepool.attached(cache):
        run("crosstalk",
            lambda: check_crosstalk(board, ground_nets, rail_nets, rise_time_ps,
                                    cache),
            note=f"ranked at {rise_time_ps:.0f} ps edges")
        run("broadside",
            lambda: check_broadside(board, ground_nets, rail_nets, rise_time_ps,
                                    cache),
            note=f"{len(adjacent)} adjacent layer pair(s)")
    run("conductor_neck", lambda: check_conductor_necks(board, rail_nets))
    copper_oz = {layer.name: (layer.copper_weight_oz or
                              (1.0 if layer.name in (board.copper_layers[0],
                                                     board.copper_layers[-1])
                               else 0.5))
                 for layer in board.stackup.layers if layer.is_copper}
    copper_oz = {board.stackup.layer_map.get(name, name): oz
                 for name, oz in copper_oz.items()}
    # A track is widened to carry current, so width counts as power evidence
    # here too — otherwise the check that measures current capacity cannot see
    # the nets the width rule just called power (the user, §46).
    power_nets = (set(rail_nets) | set(ground_nets)
                  | set(netclass_module.power_by_width(board)))
    current_coverage = current_module.coverage(board, power_nets)
    # "compared" was the wrong word and the same conflation the check's own
    # coverage finding had: this is the count of nets with **enough
    # terminals**, which is not the count that were compared. On one board
    # only 1 of 5 candidates produced a comparison and the note said 5.
    run("current_capacity",
        lambda: check_current_capacity(board, power_nets, copper_oz,
                                       net_intent=net_intent,
                                       evidenced_power=(set(rail_nets)
                                                        | set(ground_nets))),
        note=(f"{len(current_coverage['nets_examined'])} power net(s) had "
              f"enough terminals; see the check's own coverage finding for "
              f"how many were compared and why the rest were not"))
    heat_coverage = heatsink_module.coverage(
        board, set(thermal.parts) if thermal else set())
    run("heat_path", lambda: check_heat_path(board, thermal),
        note=(f"{heat_coverage['thermal_pads']} thermal tab(s)" if thermal
              else "no thermal data supplied, so no part is known to dissipate"))
    # Additive, like `thermal`: a current is intent, so without a declared
    # path there is nothing to solve, and that is said rather than skipped.
    # The whole result is kept, not just its findings: its temperature and
    # current fields are what the review's pictures draw, and solving again
    # to get them back would double the slowest check in the audit.
    power_result = None
    if power_paths is not None:
        def power_path_check():
            nonlocal power_result
            power_result = ampacity_module.evaluate(
                board, power_paths, thermal, power_nets=power_nets)
            return power_result.findings

        run("power_path", power_path_check,
            note=(f"{len(power_paths.paths)} declared path(s), "
                  f"{len(power_paths.scenarios)} scenario(s)"))
    else:
        checks.append(CheckResult(
            "power_path", ran=False,
            skipped_because=("no power paths declared, so no drop, via "
                             "current or board temperature was computed at a "
                             "stated current — `python -m pcbreview.ampacity "
                             "<project> --template` starts a manifest")))

    if schematic is None:
        for name in ("parity", "bom_parity", "placement", "thermal"):
            checks.append(CheckResult(
                name, ran=False,
                skipped_because=("no schematic given; nothing to compare the "
                                 "board to" if name in ("parity", "bom_parity")
                                 else
                                 "no schematic given, and thermal data is "
                                 "keyed by designator" if name == "thermal" else
                                 "no schematic given, and every placement "
                                 "check keys on what a part is")))
    else:
        parity_coverage: dict = {}
        run("parity",
            lambda: check_parity(board, schematic, coverage=parity_coverage),
            note=lambda: (
                f"{parity_coverage.get('matched_pins', 0)} pin(s) matched "
                f"across {parity_coverage.get('matched_components', 0)} part(s)"
                + (f"; {len(parity_coverage['decals'])} pad-less footprint(s) "
                   f"skipped as artwork"
                   if parity_coverage.get("decals") else "")))
        run("bom_parity", lambda: check_bom_parity(schematic, bom_comparison),
            # The comparison carries its own source, which `audit()` can see;
            # `bom_path` is a local of `audit_files` and reaching for it here
            # was a NameError that only fired on a board that actually had a
            # BOM — so two of five baselines, and no unit test.
            note=(f"{bom_comparison.compared} part(s) against "
                  f"{Path(bom_comparison.source_path).name}"
                  if bom_comparison and bom_comparison.source_path
                  else f"{bom_comparison.compared} part(s) against the BOM"
                  if bom_comparison else "no BOM supplied or found"))
        placement_coverage = placement_module.coverage(board, schematic)
        run("placement", lambda: check_placement(board, schematic),
            note=(f"{placement_coverage['decoupling_loops']} decoupling loop(s), "
                  f"{placement_coverage['feedback_pins']} feedback pin(s), "
                  f"{placement_coverage['crystals']} crystal(s), "
                  f"{placement_coverage['crystal_loops_measured']} loop(s) "
                  f"measured"))
        # Runs whether or not a manifest exists — with none, it reports that it
        # did not run and which parts need datasheets, which is the correct
        # output under the ruling rather than a placeholder.
        run("thermal", lambda: check_thermal(schematic, thermal),
            note=(f"{len(thermal.parts)} part(s) from datasheets" if thermal
                  else "no thermal data supplied"))

    # A copper-free channel across the board makes certain findings inevitable
    # rather than informative. They are kept, with every number intact and the
    # original severity recorded, and moved to `info` so they stop competing
    # with the findings a reviewer can act on. Nothing is deleted: if the
    # channel turns out to be a routing error rather than a break-off panel,
    # the panelisation finding says so and these are still all there.
    crossing = division.crossing_nets()
    demoted = 0
    for finding in findings:
        if finding.check not in PARTITION_EXPLAINS:
            continue
        if not crossing or not set(finding.nets) & crossing:
            continue
        if finding.severity == "info":
            continue
        finding.measurement = dict(finding.measurement)
        finding.measurement["severity_before_partition"] = finding.severity
        finding.severity = "info"
        finding.detail = finding.detail + [
            "reported at info: this net has copper on both sides of the "
            f"{division.gap_mm:.2f} mm copper-free channel that divides the "
            "board, so it is discontinuous by construction"]
        finding.intent_needed = (
            "whether the channel this net crosses is a break-off panel — if "
            "it is, this is by design; if not, "
            + (finding.intent_needed or "this is a real discontinuity"))
        demoted += 1
    if demoted:
        assumptions.append(
            f"{demoted} finding(s) on the {len(crossing)} net(s) crossing the "
            f"board's copper-free channel were moved to info — see the "
            f"panelisation finding, and reverse this if that channel is not a "
            f"break-off panel")

    assumptions.append(
        f"crosstalk is ranked at {rise_time_ps:.0f} ps edges, which is an "
        f"assumption — the saturated figures beside each one need none")

    # Gate 2. Every measurement above stands as computed, and since §49.3 so
    # does every severity: an unidentified net is no longer demoted, because
    # measured across four boards "unidentified" was the *only* thing the cap
    # ever fired on. The classification still gates the power-scoped checks
    # and still carries the user's own declarations, which are now the only
    # thing that lowers a severity.
    classes = netclass_module.classify(
        schematic, rail_nets, ground_nets,
        inferred.mapping if inferred else None,
        {n.name for n in board.nets.values() if n.name},
        board=board, intent=net_intent)
    capping = netclass_module.apply(findings, classes)
    census = classes.census()
    net_class_summary = {**census, **capping}
    unidentified = census['by_class'].get('other', 0)
    assumptions.append(
        f"net class: {census['by_class'].get('protocol', 0)} protocol net(s), "
        f"{census['by_class'].get('power', 0)} power, "
        f"{unidentified} that nothing identified. "
        f"An unidentified net keeps the full severity its check computed — "
        f"custom symbols under-name their pins, so absence of evidence is not "
        f"evidence the net is unimportant. "
        f"{capping['capped']} finding(s) lowered to "
        f"{netclass_module.OTHER_SEVERITY_CAP} because you declared their "
        f"nets unimportant in the net-intent manifest; nothing was "
        f"re-measured and no severity was raised")
    # **Whether classifying by track width could say anything here.**
    # `POWER_WIDTH_MM` is an absolute 0.2 mm, and on a board whose widest track
    # is below it *no net is ever classified power on geometry* — so
    # `current_capacity` and `heat_path` examine nothing while every coverage
    # line honestly reports zero. That is invisible without this sentence, and
    # it is the one an agent needs in order to know to ask which nets are
    # rails rather than to trust the silence.
    state = census.get("width_rule")
    widest, median = (census.get("widest_routed_width_mm"),
                      census.get("median_routed_width_mm"))
    if state == "never_fires":
        assumptions.append(
            f"net class: NO net was classified power by track width, because "
            f"the widest track on this board is {widest:.3f} mm and the "
            f"threshold is {netclass_module.POWER_WIDTH_MM} mm. The "
            f"power-scoped checks (current_capacity, heat_path) therefore saw "
            f"only nets a pin name or a power symbol identified — ASK which "
            f"nets carry current and declare them, rather than reading their "
            f"coverage as a pass")
    elif state == "always_fires":
        assumptions.append(
            f"net class: this board's median routed width is {median:.3f} mm, "
            f"at or above the {netclass_module.POWER_WIDTH_MM} mm power "
            f"threshold, so width identifies almost everything as power and "
            f"discriminates between very little of it")

    # Positive confirmation that the user's own file was read, always — not
    # only when something went wrong with it.
    confirmation = netclass_module.declaration_report(
        classes, capping, net_intent.source_path if net_intent else None)
    if confirmation:
        assumptions.append(confirmation)
    if census.get("declarations_matching_nothing"):
        assumptions.append(
            "net-intent declarations matching no net on this board: "
            + ", ".join(census["declarations_matching_nothing"])
            + " — a pattern that matches nothing protects nothing")
    if (classes.median_width_mm is not None
            and classes.median_width_mm > netclass_module.POWER_WIDTH_MM):
        assumptions.append(
            f"this board's median routed width is "
            f"{classes.median_width_mm:.3f} mm, above the "
            f"{netclass_module.POWER_WIDTH_MM} mm at which a track is treated "
            f"as power-carrying, so the width rule classifies much of the "
            f"board as power. That admits those nets to the current-capacity "
            f"and heat-path checks; it changes no severity")
    assumptions.append(
        "edge rate, current and noise margin are still not known for any net, "
        "so severity within a class remains a ranking rather than a verdict")
    # IPC-2152's charts are for boards over 3 x 3 in, and on a smaller one it
    # recommends its conservative chart. The capacity function cannot see the
    # board, so the switch is said here, with the number it would give.
    outline_area = (board.outline.area
                    if board.outline is not None and not board.outline.is_empty
                    else None)
    if outline_area and outline_area < physics.IPC2152_MIN_BOARD_AREA_MM2:
        assumptions.append(
            f"every current capacity is IPC-2152's still-air chart, which is "
            f"for boards over 3 x 3 in; this board is {outline_area:.0f} mm^2, "
            f"and on a board that small IPC-2152 recommends its conservative "
            f"chart (IPC-2221's internal curve), which allows about half the "
            f"current — 0.72 A against 1.40 A for 0.5 mm of 1 oz at a 10 C "
            f"rise")

    findings.sort(key=lambda f: f.rank)
    origin = board._data.get("source", {}).get("file")
    return AuditReport(
        board=(Path(origin).stem if origin
               else board.source_path.stem if board.source_path else "board"),
        board_summary=board.summary(),
        stackup=(f"{board.stackup.name or 'stackup'} "
                 f"({board.stackup.fab_code or 'no fab code'})"),
        findings=findings, checks=checks,
        intent=inferred.summary() if inferred else None,
        net_classes=net_class_summary, net_class_map=classes,
        intent_evidence=intent_evidence, assumptions=assumptions,
        seconds=time.time() - started, power_paths_result=power_result)


def audit_files(pcb_path, stackup_path, schematic_path=None,
                thermal_path=None, net_intent_path=None, bom_path=None,
                power_paths_path=None, **kwargs) -> AuditReport:
    """Load a board, its stackup and its schematic, then audit the lot.

    The schematic defaults to the one beside the board, which is where KiCad
    puts it. Without one the audit falls back to net-name heuristics for ground
    and for differential pairs, and records that it did.

    The thermal manifest defaults to ``<board>.thermal.yaml`` if it exists.
    Unlike the stackup it is optional and its absence is reported rather than
    refused — `check_thermal`.

    Saying "none, deliberately"
    ---------------------------
    Every optional input above has **three** states, and until now only two
    could be expressed. `None` means *discover it beside the board*; a path
    means *use this one*; and **`False` means "there is none, and do not go
    looking"**.

    That third state is not a nicety. Twice now a run has measured a different
    *input* while everyone involved believed it was measuring different *code*:

    * §47.9 — a manifest kept somewhere other than beside the board, so a
      re-run silently lost it and the diff blamed the code;
    * §52.4 — two review sessions left `<board>.thermal.yaml` and
      `<board>.netintent.yaml` beside the boards they reviewed, so re-running a
      baseline auto-discovered manifests that had not existed when it was made.
      One board moved 96 -> 122 findings with nothing in the engine responsible.

    Both are the same defect in opposite directions, and both are closed by
    being able to pin the inputs. `tests/dump_audit.py` takes `none` on the
    command line for exactly this, and a baseline made that way cannot be moved
    by a file appearing beside someone's board.

    The suppression is **reported**, not silent: `report.assumptions` says which
    inputs were deliberately withheld, because an audit that quietly skipped
    the thermal manifest sitting right there would be its own kind of lie.
    """
    from pcbreview.stackup import load_and_validate

    pcb_path = Path(pcb_path)
    #: which optional inputs the caller explicitly suppressed, so the report
    #: can say so rather than looking like a board that simply has none
    suppressed = [name for name, value in
                  (("schematic", schematic_path), ("thermal", thermal_path),
                   ("net-intent", net_intent_path), ("BOM", bom_path),
                   ("power paths", power_paths_path))
                  if value is False]
    board = Board.from_kicad(pcb_path)
    stackup, problems = load_and_validate(stackup_path, board.copper_layers)
    board.bind_stackup(stackup)

    if schematic_path is None:
        candidate = pcb_path.with_suffix(".kicad_sch")
        schematic_path = candidate if candidate.exists() else None
    schematic = netlist(schematic_path) if schematic_path else None

    if thermal_path is None:
        candidate = thermal_module.default_path(pcb_path)
        thermal_path = candidate if candidate.exists() else None
    manifest = thermal_module.load(thermal_path) if thermal_path else None

    # Both of these are auto-discovered beside the board rather than added to
    # the command line, which already takes four positional arguments. Claude
    # writes the net-intent manifest from what the user said; the BOM is
    # whatever the designer exported.
    if net_intent_path is None:
        candidate = netclass_module.default_intent_path(pcb_path)
        net_intent_path = candidate if candidate.exists() else None
    net_intent = (netclass_module.load_intent(net_intent_path)
                  if net_intent_path else None)

    # The power-path manifest, like the thermal one, is additive: without it
    # `power_path` reports that it could not run and says how to start one.
    if power_paths_path is None:
        candidate = powerpaths_module.default_path(pcb_path)
        power_paths_path = candidate if candidate.exists() else None
    power_paths = (powerpaths_module.load(power_paths_path)
                   if power_paths_path else None)

    if bom_path is None:
        bom_path = bom_module.find_bom(pcb_path)
    comparison = None
    if bom_path and schematic is not None:
        try:
            comparison = bom_module.compare(
                schematic, bom_module.load(bom_path), bom_path)
        except bom_module.BomError:
            comparison = None

    report = audit(board, schematic, thermal=manifest, pcb_path=pcb_path,
                   net_intent=net_intent, bom_comparison=comparison,
                   power_paths=power_paths, **kwargs)
    report.assumptions = [f"stackup: {p}" for p in problems] + report.assumptions
    if suppressed:
        report.assumptions.append(
            f"the caller suppressed auto-discovery of: "
            f"{', '.join(suppressed)} — these were treated as absent even if a "
            f"file for them sits beside the board. That is how a baseline is "
            f"pinned to its inputs; it is wrong for a real review")
    if manifest is None:
        report.assumptions.append(
            "no thermal data was supplied, so no junction temperature was "
            "computed for any part — see the `thermal` finding for which parts "
            "need a datasheet")
    report.artefacts = {
        "layout": review_module.fingerprint([pcb_path]),
        "schematic": (review_module.fingerprint(
            review_module.schematic_files(schematic))
            if schematic is not None else None),
    }
    return report


def record_review(report: AuditReport, pcb_path, create: bool = False) -> dict:
    """Write this run into the board's `review/` folder — **if it has one**.

    The default is that nothing is written (§49.1), so this does nothing at
    all unless the folder already exists or `create` is set because the user
    said yes. Returns what it did and what changed since the last review, or
    `{"recorded": False, …}` when there is no folder — which is not a failure
    and is the normal case.
    """
    root = review_module.find(pcb_path)
    if root is None:
        if not create:
            return {"recorded": False,
                    "offer": (f"nothing was written. To keep a record of this "
                              f"and future reviews, and to stop the geometry "
                              f"cache living in a temp directory, run again "
                              f"with --review and a "
                              f"{review_module.FOLDER_NAME}/ folder will be "
                              f"created beside the board")}
        root = review_module.create(pcb_path)

    artefacts = {k: v for k, v in (report.artefacts or {}).items()
                 if v is not None}
    staleness = review_module.changed_since(root, artefacts)
    moved = review_module.compare(review_module.previous(root),
                                  [f.summary() for f in report.findings])
    written = review_module.record(
        root, report.board, artefacts, report.summary(),
        [f.summary() for f in report.findings])
    return {"recorded": True, "path": str(written), "artefacts": staleness,
            "since_last_review": moved}


#: What the proposed manifest is called inside a review folder. Deliberately
#: **not** `<board>.netintent.yaml`: that name is auto-discovered and applied,
#: and a machine-written file must never be mistaken for the user's own word.
PROPOSED_INTENT_NAME = "proposed.netintent.yaml"


def propose_intent(report: AuditReport, pcb_path) -> dict:
    """The inference, written down — the user's ruling on where it goes.

    > write it into the review folder if a review folder was opted for, and if
    > no review folder, then offer anyway and output the file in the claude
    > session chat for the user to grab, and offer again to make a review
    > folder to put it in.

    The **shortlist** — what Claude puts in front of the user — is the nets
    nothing identified *that this run's findings actually name*, also his
    ruling. Measured on the boards here that is 9, 10, 13 and 73 nets against
    50-157 raw, because half of every board's `other` count is `unconnected-*`
    placeholders that carry no copper and the rest mostly produced no finding.
    Those are the only nets where an opinion changes anything: demote one to
    quieten its findings, or call it power to admit it to the current-capacity
    and heat-path checks.
    """
    classes = report.net_class_map
    if classes is None:
        return {"available": False}

    named = {net for finding in report.findings for net in finding.nets if net}
    document = netclass_module.propose(classes, named_by_findings=named)
    # The same predicate `propose` just used, from the same function, so the
    # names printed here and the live entries in the document it wrote cannot
    # drift apart. It used to be written out a second time here.
    shortlist = [kind.net for kind in
                 netclass_module.shortlist(classes, named_by_findings=named)]
    unidentified = sum(1 for k in classes.by_net.values()
                       if k.net_class == "other" and not k.declared
                       and not netclass_module.is_placeholder(k.net))

    root = review_module.find(pcb_path)
    written = None
    if root is not None:
        written = Path(root) / PROPOSED_INTENT_NAME
        # UTF-8 named, to match `load_intent`'s read. A net name can carry a
        # character `cp1252` cannot represent, and the default here would
        # raise on it rather than write the file.
        written.write_text(document, encoding="utf-8")
    # Whether a manifest already exists changes the instruction from "rename"
    # to "merge", and getting that wrong destroys work: the proposal is a
    # generated inference over every net, so renaming it over a curated
    # hand-written manifest replaces the user's own judgement — the one thing
    # in this tool that is theirs (§54.3).
    existing = netclass_module.default_intent_path(pcb_path)
    return {"available": True, "shortlist": shortlist,
            "unidentified": unidentified, "path": str(written) if written
            else None, "document": document,
            "existing_manifest": str(existing) if existing.exists() else None}


USAGE = """\
usage: python -m pcbreview.audit <board.kicad_pcb> [any other files, any order]
       [--grouped] [--review] [--all | --severity LEVEL] [--net NET]
       [--no-solve-cache | --verify-cache]

FILES ARE IDENTIFIED BY WHAT THEY ARE, NOT BY THEIR POSITION. Hand over the
board, the stackup, the schematic, the manifests and the BOM in whatever order
suits you; each is opened and read to find out what it is. A `.kicad_sch` can
never land in the stackup slot again.

  board.kicad_pcb   required — the only file that must be there
  *.stackup.yaml    if omitted, one beside the board or in its review/ folder
                    is used. Two that DISAGREE is refused, so you can ask the
                    user which is meant; answer with --stackup <path>
  *.kicad_sch       enables placement and parity. NOT the eight schematic
                    checks — those are `python -m pcbreview.schematic_audit`
  *.thermal.yaml    junction temperatures
  *.netintent.yaml  what the user says their nets are for
  *.csv             the BOM
  *.pdf             reported and NOT read — a datasheet is for you to read and
                    turn into a thermal manifest entry

  --grouped        print findings under their shared cause, with every
                   finding still listed under one heading
  --all            include `info` findings. Several of the most useful
                   outputs are `info`: every coverage statement, the thermal
                   shortlist, a differential pair that came back clean.
                   Do NOT combine with --grouped; run two passes
  --severity LEVEL show this level and everything above it
                   (critical | major | minor | info)
  --review         create a review/ folder beside the board and record this
                   run in it. Without it nothing is written
  --net NET        everything known about one net — widths, layers, vias,
                   zones, ampacity and every finding naming it — instead of
                   the ranked list
  --stackup PATH   name a slot explicitly. Also --schematic, --thermal,
                   --netintent, --bom

  --no-solve-cache solve every cross-section from scratch. The field solver is
                   85% of this run and its results are cached across runs and
                   across boards, so this is much slower — it is here so a
                   measurement can pin the cache off and know that it did
  --verify-cache   after the run, re-solve everything the cache served and
                   require it to match bit for bit. Costs the time the cache
                   saved. Exits 1 and says so if any of it disagrees
"""


def _copper_oz_by_layer(board) -> dict:
    """Copper weight per KiCad layer name, defaulting outer 1 oz / inner 0.5.

    Lifted out of `audit()` so the net dossier computes ampacity from the same
    weights every finding does. Two copies of this would drift, and a drifting
    copper weight moves every ampacity by the same factor with no symptom.
    """
    if board.stackup is None:
        outer = (board.copper_layers[0], board.copper_layers[-1])
        return {name: (1.0 if name in outer else 0.5)
                for name in board.copper_layers}
    weights = {layer.name: (layer.copper_weight_oz or
                            (1.0 if layer.name in (board.copper_layers[0],
                                                   board.copper_layers[-1])
                             else 0.5))
               for layer in board.stackup.layers if layer.is_copper}
    return {board.stackup.layer_map.get(name, name): oz
            for name, oz in weights.items()}


def net_dossier(board, report, net_name: str) -> str:
    """Everything this tool knows about one net, in one place.

    The field report of 2026-08-05 asked for it and named the reason: a net is
    *"the natural unit of a hardware conversation"*, and that session assembled
    this by hand twice out of unrelated findings. Nothing here is a new
    measurement — every number below is already computed somewhere in the
    audit; the value is that they arrive together and named.
    """
    from pcbreview import current as current_module, physics

    # `board.nets` is `dict[int, Net]`, so iterating it gives ints — the
    # mistake REFERENCE.md's cheat-sheet already warns about.
    known = {n.name for n in board.nets.values() if n.name}
    if net_name not in known:
        near = sorted(n for n in known if net_name.lower() in n.lower())
        return (f"no net called {net_name!r} on this board.\n"
                + (f"Did you mean: {', '.join(near[:10])}\n" if near else
                   "Net names are case-sensitive and carry their sheet path; "
                   "`{slash}` escapes a `/` inside a label's own name.\n"))

    net = board.net(net_name)
    tracks = board.tracks_on_net(net.code)
    vias = board.vias_on_net(net.code)
    pads = board.pads_on_net(net.code)
    outer = (board.copper_layers[0], board.copper_layers[-1])

    lines = [f"=== {net_name} ===", ""]

    # Every number here comes from `current.ampacity`, never from a second
    # copy of the same arithmetic. §53.3's rule: a duplicated precondition
    # drifts, and a drifting copper weight moves every ampacity by one factor
    # with no symptom. An earlier draft of this function recomputed it and
    # immediately disagreed with the finding by 0.02 A.
    measured = current_module.ampacity(board, net_name,
                                       _copper_oz_by_layer(board))
    if measured.by_width:
        lines.append(f"routed copper — width, length, and IPC-2152 ampacity "
                     f"at a {measured.rise_c:.0f} C rise")
        for (layer, width), (length, amps) in sorted(
                measured.by_width.items(), key=lambda kv: -kv[1][0]):
            lines.append(f"    {layer:8} {width:6.3f} mm x {length:8.2f} mm "
                         f"-> {amps:6.2f} A")
        if measured.weakest_a is not None:
            lines.append(
                f"    the net carries what its weakest stretch carries: "
                f"{measured.weakest_a:.2f} A — {measured.weakest_mm:.3f} mm on "
                f"{measured.weakest_layer}. Weakest by CAPACITY, not by width: "
                f"an inner layer is thinner copper")
        if (measured.weakest_any_a is not None
                and measured.weakest_a is not None
                and measured.weakest_any_a < measured.weakest_a):
            lines.append(
                f"    weaker still over a short stretch: "
                f"{measured.weakest_any_a:.2f} A on "
                f"{measured.weakest_any_mm:.3f} mm of "
                f"{measured.weakest_any_layer}, "
                f"{measured.weakest_any_length_mm:.2f} mm long")
    else:
        lines.append("routed copper: none — this net has no track segments")
    lines.append("")

    if measured.via_capacity_a is not None:
        drills = sorted({round(v.drill_mm, 3) for v in vias})
        lines.append(
            f"vias: {measured.vias}, drill "
            f"{', '.join(f'{d:.3f}' for d in drills)} mm -> "
            f"{measured.via_capacity_a:.2f} A in parallel at a "
            f"{measured.rise_c:.0f} C rise, assuming "
            f"{current_module.VIA_PLATING_UM:.0f} um plating")
    else:
        lines.append("vias: none")

    # -- poured copper -------------------------------------------------------
    poured = {layer: board.zone_fill(layer, net_name).area
              for layer in board.copper_layers}
    poured = {k: v for k, v in poured.items() if v > 0}
    if poured:
        lines.append("poured copper: " + ", ".join(
            f"{layer} {area:.1f} mm2" for layer, area in poured.items()))
    else:
        lines.append("poured copper: none")

    # -- terminals, in the user's own designators ---------------------------
    if pads:
        refs: dict[str, list] = {}
        for pad in pads:
            refs.setdefault(pad.ref, []).append(pad.number)
        lines.append(f"pads: {len(pads)} on {len(refs)} part(s) — " + ", ".join(
            f"{ref}.{'/'.join(sorted(numbers))}"
            for ref, numbers in sorted(refs.items())))
    lines.append("")

    # -- every finding that names it ----------------------------------------
    naming = [f for f in report.findings if net_name in (f.nets or [])]
    if naming:
        lines.append(f"findings naming this net: {len(naming)}")
        for finding in naming:
            lines.append(f"    [{finding.severity.upper():8}] "
                         f"{finding.check}: {finding.title}")
    else:
        lines.append("findings naming this net: none — which is not the same "
                     "as clean. Read the coverage lines in the full run.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import sys

    from pcbreview import console_utf8

    # Before anything prints. The report carries `Ω` and `≤`, and this run is
    # expensive enough that losing it at the last line is unacceptable.
    console_utf8()

    # `--review` is the only flag and it is opt-in by design: without it this
    # command writes nothing anywhere (§49.1). With it, or when a `review/`
    # folder already exists beside the board, each run is recorded there.
    create_review = "--review" in argv
    grouped = "--grouped" in argv
    # The solve cache is on by default because a review nobody can afford to
    # re-run is a review that gets run once, at the end, which is the habit
    # this whole tool is trying to break. `--no-solve-cache` is how a
    # measurement pins it off; `--verify-cache` re-solves everything the cache
    # served and reports whether it told the truth, at the cost of the run it
    # just saved.
    verify_cache = "--verify-cache" in argv
    use_cache = "--no-solve-cache" not in argv
    # `info` findings were unreachable from the command line, and several of
    # the most useful things a run produces are `info`: every coverage
    # statement, the thermal shortlist, a differential pair that is fine. One
    # session dropped into Python to read them and re-ran the whole audit —
    # including 93 s of crosstalk — four times to do it (§51).
    order = ["critical", "major", "minor", "info"]
    severities = tuple(order) if "--all" in argv else tuple(order[:3])
    positional: list[str] = []
    #: flags that name a slot explicitly. `--stackup` is the one that matters:
    #: it is the answer coming back after the user was asked which of two
    #: disagreeing stackups to use, and it skips the conflict check.
    named: dict[str, str] = {}
    focus_net: str | None = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--severity":
            if index + 1 >= len(argv):
                sys.stderr.write("--severity needs a level\n")
                return 2
            floor = argv[index + 1].strip().lower()
            if floor not in order:
                sys.stderr.write(
                    f"--severity must be one of {', '.join(order)}\n")
                return 2
            severities = tuple(order[:order.index(floor) + 1])
            index += 2
            continue
        if argument == "--net":
            if index + 1 >= len(argv):
                sys.stderr.write("--net needs a net name\n")
                return 2
            focus_net = argv[index + 1]
            index += 2
            continue
        if argument.lstrip("-") in ("stackup", "schematic", "thermal",
                                    "netintent", "bom") and \
                argument.startswith("--"):
            if index + 1 >= len(argv):
                sys.stderr.write(f"{argument} needs a path\n")
                return 2
            named[argument.lstrip("-")] = argv[index + 1]
            index += 2
            continue
        if not argument.startswith("--"):
            positional.append(argument)
        index += 1

    if len(positional) < 2 and not named:
        sys.stderr.write(USAGE)
        return 2

    from pcbreview.stackup import StackupGateError
    from pcbreview import inputs as inputs_module

    # **Files are routed by what they are, not by where they sat.** the
    # ruling, 2026-08-06: a user attaches a board, a stackup, a schematic and
    # some datasheets in whatever order suits them, and the tool reads each one
    # to find out what it is. Positional order still works — it is simply no
    # longer load-bearing.
    try:
        slots = inputs_module.route(positional[1:])
        slots.update({k: v for k, v in named.items() if v})
        stackup_path, stackup_notes = inputs_module.resolve_stackup(
            slots["board"],
            named.get("stackup") or slots.get("stackup"),
        ) if "stackup" not in named else (named["stackup"], [])
    except inputs_module.InputError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    board_path = slots["board"]
    slots["stackup"] = stackup_path
    print("inputs")
    print(inputs_module.describe(slots))
    for note in stackup_notes:
        print(f"  note: {note}")
    print()

    if stackup_path is None:
        sys.stderr.write(
            f"{Path(board_path).name}: the audit needs a validated "
            f"fabrication stackup and none was given or found beside the "
            f"board. Transcribe the fab's own stackup screenshot with "
            f"`stackup_cli transcribe` — do not invent one.\n")
        return 1

    store = solvestore.open_default() if use_cache else None
    if store is not None:
        store.retain = verify_cache
    elif verify_cache:
        sys.stderr.write(
            "--verify-cache: there is no solve cache to verify — it is "
            "switched off, or could not be opened.\n")
        return 2

    # Gate 1 refusing is the commonest way this exits and it is a *correct*
    # outcome, so it reads as a message rather than as a crash.
    try:
        report = audit_files(board_path, stackup_path,
                             slots.get("schematic"), slots.get("thermal"),
                             slots.get("netintent"), slots.get("bom"),
                             solve_store=store if store is not None else False)
    except (AuditError, StackupGateError) as exc:
        sys.stderr.write(f"{Path(board_path).name}: {exc}\n")
        if store is not None:
            store.close()
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{Path(board_path).name}: could not be read — {exc}\n")
        if store is not None:
            store.close()
        return 2

    if focus_net is not None:
        from pcbreview.model import Board

        print(net_dossier(Board.from_kicad(board_path), report, focus_net))
        if store is not None:
            store.close()
        return 0

    print(report.report(limit=None, grouped=grouped, severities=severities))

    outcome = record_review(report, board_path, create=create_review)
    print()
    if not outcome["recorded"]:
        print(f"review record: {outcome['offer']}")
    else:
        print(f"review record: {outcome['path']}")
        for name, state in sorted(outcome["artefacts"].items()):
            print(f"  {name}: {state} since the last review")
        moved = outcome["since_last_review"]
        if moved["first_run"]:
            print("  first recorded review of this board — nothing to "
                  "compare against yet")
        else:
            print(f"  since {moved['since']}: {len(moved['resolved'])} "
                  f"resolved, {len(moved['new'])} new, "
                  f"{len(moved['changed'])} changed severity")
            for line in moved["resolved"][:10]:
                print(f"    resolved: {line[:100]}")
            for line in moved["new"][:10]:
                print(f"    new:      {line[:100]}")
            for line in moved["changed"][:10]:
                print(f"    changed:  {line[:100]}")

    intent_outcome = propose_intent(report, board_path)
    if intent_outcome.get("available"):
        print()
        shortlist = intent_outcome["shortlist"]
        if shortlist:
            print(f"net intent: {len(shortlist)} net(s) that nothing "
                  f"identified, and that findings above name — say if any of "
                  f"these do not matter, or carry current:")
            print("  " + ", ".join(shortlist))
            others = intent_outcome["unidentified"] - len(shortlist)
            if others:
                print(f"  ({others} more unidentified net(s) carry no "
                      f"finding; every net is in the proposed manifest)")
        else:
            print("net intent: every net a finding names was identified as a "
                  "bus or a rail — nothing to ask about")

        existing = intent_outcome.get("existing_manifest")
        if intent_outcome["path"]:
            print(f"  the full inference, every net with its evidence: "
                  f"{intent_outcome['path']}")
            if existing:
                print(f"  {existing} ALREADY EXISTS — do not rename over it. "
                      f"It carries the user's own declarations and this file "
                      f"is a generated inference over every net. Merge the "
                      f"lines you want into it.")
            else:
                print(f"  edit a line there and rename it to "
                      f"<board>.netintent.yaml to apply it")
        else:
            # No review folder, so it goes to the session instead — the
            # user's ruling. The inference must not evaporate just
            # because nothing was written to disk.
            print("  no review folder, so the proposed manifest follows in "
                  "full rather than being written anywhere. "
                  + (f"MERGE the lines you want into {existing}, which "
                     f"already exists and carries the user's own "
                     f"declarations — do not overwrite it. "
                     if existing else
                     "Save it beside the board as <board>.netintent.yaml to "
                     "apply it. ")
                  + "Run again with --review to keep it in a review/ folder:")
            print()
            print("----- BEGIN proposed netintent.yaml -----")
            print(intent_outcome["document"].rstrip())
            print("----- END proposed netintent.yaml -----")

    if store is not None:
        print()
        print(store.summary())
        if verify_cache:
            print(f"  verifying all {store.retained} reused solve(s) by "
                  f"solving them again — this costs the time the cache "
                  f"saved, which is the price of the answer")
            result = store.verify_served()
            print(f"  {result['matched']} of {result['checked']} matched "
                  f"bit for bit, {result['absent']} no longer stored")
            if result["mismatched"]:
                sys.stderr.write(
                    f"SOLVE CACHE IS WRONG on {len(result['mismatched'])} "
                    f"cross-section(s). The key does not capture something "
                    f"that changes the answer. Do not trust this review; "
                    f"clear the cache and report it.\n")
                for description in result["mismatched"][:10]:
                    sys.stderr.write(f"  {description}\n")
                store.close()
                return 1
        store.close()
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
