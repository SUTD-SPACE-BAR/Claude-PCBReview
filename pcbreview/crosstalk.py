"""Crosstalk between routed nets, from the field solver's mutual C and L.

`coupling.py` answers *how much of net A runs close to net B, and how close*.
`fields.py` answers *what are the mutual capacitance and inductance of this
cross-section*. This module is the join: it walks each coupled run, solves the
cross-section that exists there, and turns the mutual terms into the two
numbers a reviewer can act on.

Physics
-------
For two coupled lines, an aggressor's forward wave drives the victim through a
distributed series EMF (``-Lm dI/dt``) and a distributed shunt current
(``Cm dV/dt``). Integrating those along the line gives the standard weak-coupling
result — Paul, *Analysis of Multiconductor Transmission Lines*; Johnson &
Graham, *High-Speed Digital Design*, ch. 5:

    k_b = (1/4) (Cm/Cs + Lm/Ls)        backward, toward the victim's near end
    k_f = (1/2) (Cm/Cs - Lm/Ls)        forward, toward its far end

Both are dimensionless ratios of the aggressor's own swing. They behave quite
differently, and the difference is the whole reason to compute them separately:

* **Backward crosstalk does not accumulate.** The near-end waveform is a scan
  of ``k_b`` along the line — the noise arriving at time *t* was generated at
  ``z = v t / 2`` — smoothed by a window of ``v t_r / 2``. So a long coupled run
  does not couple more than a short one, it couples for longer, and the peak is
  the *worst local* ``k_b``, not the total. Letting ``t_r`` go to zero gives the
  saturated value, which is an upper bound over every edge rate and therefore
  the one number here that needs no assumption about the signal.
* **Forward crosstalk does accumulate**, coherently, because it travels with
  the aggressor's edge. It is reported as an integral with units of time
  (``k_f`` times the coupled delay), so that FEXT as a fraction of the swing is
  just that divided by the rise time.

In a homogeneous medium ``Cm/Cs`` and ``Lm/Ls`` are equal and ``k_f`` vanishes:
a stripline has no forward crosstalk. `test_fields.py` asserts that identity to
1e-9 on this solver's own matrices, and it is the sharpest check available that
the mutual terms are right. Microstrip's fringing runs through air, which weakens
the electric coupling but not the magnetic, so ``k_f`` is negative there — far-end
crosstalk on an outer layer has the opposite polarity to the aggressor's edge.

For unequal conductors the ratios are normalised by the geometric mean of the
two self terms. Reciprocity requires the coupling to be symmetric between the
two lines, and the geometric mean is the only normalisation that both is
symmetric and reduces to the textbook form when the widths match.

What this is good for
---------------------
**Ranking, not spec compliance.** The numbers say which of a board's coupled
runs is worst and by roughly how much; they are not a substitute for a
simulation of a particular link. Five things stand between the two:

1. **Weak coupling.** The aggressor is assumed undisturbed by the victim, which
   is good to a few percent while ``k_b`` stays well under 1, and these boards
   are all in that regime.
2. **Terminations are assumed matched at both ends.** Real CMOS is not: a
   high-impedance receiver roughly doubles the near-end voltage, and a driver
   with a low output impedance shorts it. This is the largest single error, and
   it is a property of the parts rather than of the layout.
3. **Rise time is an assumption** until the intent manifest supplies one. The
   saturated NEXT is quoted precisely because it does not need one.
4. **Same-layer and broadside coupling are measured separately** —
   `analyse_coupling`/`survey` for traces side by side, `analyse_broadside`/
   `broadside_survey` for traces stacked on adjacent layers. They are not
   summed: the two are different cross-sections with different remedies, and
   adding coefficients that were each normalised against their own section
   would not mean anything.
5. **2D and quasi-static**: no vias, bends, or the fact that the aggressor may
   never switch at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.ops import unary_union
from shapely.strtree import STRtree

from pcbreview import coupling as coupling_module
from pcbreview import fields, routed, solvepool, solvestore
from pcbreview.model import Board

# Matches `coupling.SAMPLE_STEP_MM` and `routed.SAMPLE_STEP_MM`.
SAMPLE_STEP_MM = 0.025

# Geometry is quantised to this before a cross-section is solved, exactly as in
# `routed.py`: a gap wandering between 0.201 and 0.204 mm is one solve, not a
# hundred. The gap is the dominant parameter, so it keeps the fine quantum.
QUANTUM_MM = 0.025

# Clearance to the surrounding pour gets a coarser one, because the coefficients
# are far less sensitive to it than the impedance is. Measured on the four-layer board's
# 0.2 mm/0.2 mm geometry, k_b moves 13% as the pour retreats from 0.15 to
# 0.5 mm and only 2% more out to 1.26 mm, so 0.05 mm steps cost under 0.4% and
# capping at three dielectric heights costs under 1.5% — against roughly a
# quarter as many field solves.
CLEARANCE_QUANTUM_MM = 0.05
MAX_USEFUL_CLEARANCE_RATIO = 3.0

# Coupled stretches shorter than this are corner clips, not runs.
MIN_RUN_LENGTH_MM = coupling_module.MIN_RUN_LENGTH_MM

# The edge rate findings are *ranked* at, when nothing knows better. 1 ns is a
# 3.3 V CMOS GPIO; the same board carries 300 ps GPS and 500 ns I2C, three
# orders of magnitude apart, and FEXT scales as 1/t_r across all of it. So this
# is an assumption, it is stated wherever it is used, and it is what the intent
# manifest exists to replace. `next_saturated` needs no such assumption and is
# always reported beside it.
REFERENCE_RISE_TIME_PS = 1000.0

# Below this the coupling is not worth a finding, measured at the reference edge
# rate. 1% of a 3.3 V swing is 33 mV, comfortably inside any logic family's
# noise margin. A reporting threshold and a judgement call, not a physical limit.
REPORTABLE_COUPLING = 0.01

# A coarser mesh than `routed.py` uses, for the same reason and more so: the
# quantity of interest is a *ratio* of matrix entries from one solve, so the
# mesh bias largely cancels. Measured over a 3.3x range of node counts, k_b
# spreads 0.55% and this refinement sits 0.25% from the finest — against a
# survey that runs hundreds of solves per board.
DEFAULT_REFINEMENT = 3.0


class CrosstalkError(Exception):
    """Raised when a coupled run cannot be analysed."""


# ---------------------------------------------------------------------------
# The coefficients themselves
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Coefficients:
    """Backward and forward coupling for one solved cross-section."""
    k_backward: float
    k_forward: float
    capacitive_ratio: float          # Cm / sqrt(Cs_v Cs_a)
    inductive_ratio: float           # Lm / sqrt(Ls_v Ls_a)
    z0_victim_ohm: float
    z0_aggressor_ohm: float
    epsilon_eff: float
    delay_ps_per_mm: float
    c_mutual_pf_per_m: float
    l_mutual_nh_per_m: float

    @property
    def is_inductively_dominated(self) -> bool:
        return self.inductive_ratio > self.capacitive_ratio


def coefficients(solution: fields.FieldSolution) -> Coefficients:
    """NEXT and FEXT coefficients from a two-conductor field solution.

    Conductor 0 is the victim (`fields.coupled_microstrip_section` builds it
    that way). ``Cs`` is the Maxwell diagonal — the self capacitance with the
    other conductor grounded — and ``Ls`` comes from the *inverted* vacuum
    capacitance, which is not the reciprocal of its diagonal.
    """
    capacitance = solution.capacitance
    if capacitance.shape[0] != 2:
        raise CrosstalkError(
            "crosstalk needs exactly two signal conductors, got "
            f"{capacitance.shape[0]}")
    inductance = solution.inductance

    mutual_c = -capacitance[0, 1]                  # Maxwell mutual is negative
    mutual_l = inductance[0, 1]
    self_c = math.sqrt(capacitance[0, 0] * capacitance[1, 1])
    self_l = math.sqrt(inductance[0, 0] * inductance[1, 1])
    if self_c <= 0 or self_l <= 0:
        raise CrosstalkError("non-physical self terms; check the mesh")

    capacitive = mutual_c / self_c
    inductive = mutual_l / self_l
    return Coefficients(
        k_backward=0.25 * (capacitive + inductive),
        k_forward=0.5 * (capacitive - inductive),
        capacitive_ratio=capacitive,
        inductive_ratio=inductive,
        z0_victim_ohm=solution.z0_of(0),
        z0_aggressor_ohm=solution.z0_of(1),
        epsilon_eff=solution.epsilon_eff,
        delay_ps_per_mm=solution.propagation_delay_ps_per_mm(),
        c_mutual_pf_per_m=mutual_c * 1e12,
        l_mutual_nh_per_m=mutual_l * 1e9,
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class CrosstalkSection:
    """A stretch of coupled run with one quantised cross-section."""
    layer: str
    length_mm: float
    gap_mm: float
    victim_width_mm: float
    aggressor_width_mm: float
    clearance_mm: float
    reference_layer: str
    height_mm: float
    coefficients: Coefficients
    #: set for a broadside section, where the two traces are on different
    #: layers. ``gap_mm`` is then the lateral centre-to-centre offset, not an
    #: edge-to-edge clearance, and zero is the worst case rather than a short.
    aggressor_layer: str | None = None
    separation_mm: float | None = None

    @property
    def is_broadside(self) -> bool:
        return self.aggressor_layer is not None

    @property
    def fext_ps(self) -> float:
        """Forward crosstalk contributed by this stretch, as ps of dV/dt."""
        return (self.coefficients.k_forward * self.length_mm
                * self.coefficients.delay_ps_per_mm)

    def __str__(self) -> str:
        if self.is_broadside:
            return (f"{self.length_mm:6.2f} mm {self.layer} over "
                    f"{self.aggressor_layer} at {self.separation_mm:.4f}: "
                    f"offset {self.gap_mm:.3f}, "
                    f"w {self.victim_width_mm:.3f}/{self.aggressor_width_mm:.3f} "
                    f"-> NEXT {self.coefficients.k_backward:.4f}, "
                    f"FEXT {self.coefficients.k_forward:+.4f}")
        return (f"{self.length_mm:6.2f} mm on {self.layer}: gap {self.gap_mm:.3f}, "
                f"w {self.victim_width_mm:.3f}/{self.aggressor_width_mm:.3f}, "
                f"ref {self.reference_layer} at {self.height_mm:.4f} "
                f"-> NEXT {self.coefficients.k_backward:.4f}, "
                f"FEXT {self.coefficients.k_forward:+.4f}")


@dataclass
class CrosstalkEstimate:
    """Crosstalk at one assumed edge rate, as a fraction of the aggressor swing."""
    rise_time_ps: float
    next_fraction: float
    fext_fraction: float             # signed; negative is opposite polarity
    saturated: bool
    saturation_length_mm: float

    @property
    def worst_fraction(self) -> float:
        return max(self.next_fraction, abs(self.fext_fraction))

    def millivolts(self, swing_v: float) -> tuple[float, float]:
        return (self.next_fraction * swing_v * 1e3,
                self.fext_fraction * swing_v * 1e3)

    def __str__(self) -> str:
        note = "" if self.saturated else " (below saturation)"
        return (f"at {self.rise_time_ps:.0f} ps edges: NEXT "
                f"{self.next_fraction:.1%}{note}, FEXT {self.fext_fraction:+.1%}")


@dataclass
class Crosstalk:
    """Coupling from one aggressor onto one victim, solved along the run."""
    victim: str
    aggressor: str
    sections: list[CrosstalkSection]
    victim_length_mm: float
    #: per-layer k_b profiles: (step_mm, array over the victim's samples)
    profiles: list[tuple[float, np.ndarray]] = field(default_factory=list,
                                                     repr=False)
    aggressor_is_rail: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def coupled_length_mm(self) -> float:
        return sum(s.length_mm for s in self.sections)

    @property
    def coupled_fraction(self) -> float:
        if self.victim_length_mm <= 0:
            return 0.0
        return min(1.0, self.coupled_length_mm / self.victim_length_mm)

    @property
    def next_saturated(self) -> float:
        """Peak backward coupling over every edge rate — the assumption-free bound.

        Backward crosstalk is a scan of ``k_b`` along the line, so its peak is
        the worst local value rather than an accumulation. Any finite rise time
        averages that peak down, never up, which is what makes this an upper
        bound rather than a typical case.

        **Do not rank on this.** It is set by the tightest gap anywhere on the
        run, and the tightest gap on a board is the design rule — so it comes
        out at much the same value for every net that touches minimum clearance
        once, and separates nothing. On the four-layer board it reads 9.6-9.7% for a
        2 mm coupled stub and for a 24 mm parallel run alike. `next_mm` and
        `at_rise_time` are the length-aware answers.
        """
        return max((s.coefficients.k_backward for s in self.sections), default=0.0)

    @property
    def next_mm(self) -> float:
        """``k_b`` integrated along the run, in coefficient-millimetres.

        The length-aware counterpart to `next_saturated`, and the other half of
        the pair that brackets NEXT without assuming an edge rate: for a rise
        time whose window exceeds the coupled length, NEXT is this divided by
        the window length, and for a shorter one it is the saturated value.
        """
        return sum(s.coefficients.k_backward * s.length_mm for s in self.sections)

    @property
    def fext_ps(self) -> float:
        """Forward crosstalk integrated along the run, as ps of dV/dt.

        FEXT as a fraction of the aggressor's swing is this divided by the rise
        time, so quoting it this way keeps the geometry separate from the
        assumption about the signal.
        """
        return sum(s.fext_ps for s in self.sections)

    @property
    def delay_ps_per_mm(self) -> float:
        """Length-weighted propagation delay over the coupled run."""
        total = self.coupled_length_mm
        if total <= 0:
            return 0.0
        return sum(s.coefficients.delay_ps_per_mm * s.length_mm
                   for s in self.sections) / total

    @property
    def min_gap_mm(self) -> float | None:
        return min((s.gap_mm for s in self.sections), default=None)

    @property
    def worst_section(self) -> CrosstalkSection | None:
        return max(self.sections, key=lambda s: s.coefficients.k_backward,
                   default=None)

    @property
    def layers(self) -> list[str]:
        return sorted({s.layer for s in self.sections})

    def saturation_length_mm(self, rise_time_ps: float) -> float:
        """Coupled length at which backward crosstalk stops growing: ``v t_r / 2``."""
        delay = self.delay_ps_per_mm
        if delay <= 0:
            return float("inf")
        return rise_time_ps / (2.0 * delay)

    def at_rise_time(self, rise_time_ps: float) -> CrosstalkEstimate:
        """NEXT and FEXT for an assumed edge rate.

        NEXT is the peak of ``k_b`` averaged over a sliding window of
        ``v t_r / 2``, which is exactly the near-end waveform a step of that
        rise time produces. FEXT is the integrated coefficient over the rise
        time.
        """
        if rise_time_ps <= 0:
            raise CrosstalkError("rise time must be positive")
        window_mm = self.saturation_length_mm(rise_time_ps)

        peak = 0.0
        for step_mm, profile in self.profiles:
            samples = max(1, int(round(window_mm / step_mm)))
            if samples <= 1 or profile.size == 0:
                peak = max(peak, float(profile.max()) if profile.size else 0.0)
                continue
            # Zero-padded so a run shorter than the window is averaged down,
            # which is the physical behaviour: it never reaches saturation.
            padded = np.concatenate((np.zeros(samples - 1), profile))
            window = np.convolve(padded, np.ones(samples) / samples, mode="valid")
            peak = max(peak, float(window.max()))

        return CrosstalkEstimate(
            rise_time_ps=rise_time_ps,
            next_fraction=peak,
            fext_fraction=self.fext_ps / rise_time_ps,
            saturated=self.coupled_length_mm >= window_mm,
            saturation_length_mm=window_mm)

    def summary(self) -> dict:
        return {
            "victim": self.victim,
            "aggressor": self.aggressor,
            "coupled_mm": round(self.coupled_length_mm, 3),
            "coupled_pct": round(self.coupled_fraction * 100, 1),
            "min_gap_mm": round(self.min_gap_mm, 4) if self.min_gap_mm else None,
            "next_saturated_pct": round(self.next_saturated * 100, 2),
            "next_mm": round(self.next_mm, 4),
            "next_at_reference_pct": round(
                self.at_rise_time(REFERENCE_RISE_TIME_PS).next_fraction * 100, 2),
            "reference_rise_time_ps": REFERENCE_RISE_TIME_PS,
            "fext_ps": round(self.fext_ps, 3),
            "layers": self.layers,
            "sections": len(self.sections),
            "aggressor_is_rail": self.aggressor_is_rail,
            "warnings": self.warnings,
        }

    def report(self, rise_times_ps: tuple[float, ...] = (300.0, 1000.0, 5000.0)
               ) -> str:
        lines = [f"{self.victim} <- {self.aggressor}"]
        if not self.sections:
            return "\n".join(lines + ["  no coupled run long enough to solve"])
        lines.append(
            f"  {self.coupled_length_mm:.2f} mm coupled of "
            f"{self.victim_length_mm:.2f} ({self.coupled_fraction:.0%}) on "
            f"{', '.join(self.layers)}, closest {self.min_gap_mm:.3f} mm")
        lines.append(
            f"  NEXT {self.next_saturated:.2%} saturated (upper bound over all "
            f"edge rates), {self.next_mm:.3f} coefficient-mm integrated; "
            f"FEXT {self.fext_ps:+.2f} ps of dV/dt")
        for rise_time in rise_times_ps:
            lines.append(f"    {self.at_rise_time(rise_time)}")
        worst = self.worst_section
        if worst is not None:
            lines.append(f"    worst: {worst}")
        if self.aggressor_is_rail:
            lines.append("  ! aggressor is a power rail; it only injects noise "
                         "if it is a switching node")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)


@dataclass
class VictimCrosstalk:
    """Every aggressor on one victim, ranked.

    Aggressors superpose. They rarely switch simultaneously or in the same
    direction, so summing them is pessimistic — but it is the bound, and on a
    bus whose lines genuinely do switch together it is close to the truth.
    Both the worst single aggressor and the sum are reported, because the
    difference between them is itself a finding: one bad neighbour is a routing
    fix, six mediocre ones is a channel that is too crowded.
    """
    victim: str
    aggressors: list[Crosstalk]
    victim_length_mm: float
    rise_time_ps: float = REFERENCE_RISE_TIME_PS

    @property
    def worst(self) -> Crosstalk | None:
        return max(self.aggressors, key=lambda c: c.next_mm, default=None)

    @property
    def next_saturated(self) -> float:
        """Worst single aggressor's assumption-free upper bound."""
        return max((c.next_saturated for c in self.aggressors), default=0.0)

    def next_fraction(self, rise_time_ps: float | None = None) -> float:
        """Peak NEXT from all aggressors switching together, at an edge rate."""
        rise_time_ps = rise_time_ps or self.rise_time_ps
        return sum(c.at_rise_time(rise_time_ps).next_fraction
                   for c in self.aggressors)

    def fext_fraction(self, rise_time_ps: float | None = None) -> float:
        rise_time_ps = rise_time_ps or self.rise_time_ps
        return sum(c.at_rise_time(rise_time_ps).fext_fraction
                   for c in self.aggressors)

    @property
    def worst_fraction(self) -> float:
        """The number this is ranked on: NEXT or FEXT at the reference edge rate."""
        return max(self.next_fraction(), abs(self.fext_fraction()))

    @property
    def fext_ps_sum(self) -> float:
        return sum(c.fext_ps for c in self.aggressors)

    def summary(self) -> dict:
        return {
            "victim": self.victim,
            "aggressors": len(self.aggressors),
            "worst_aggressor": self.worst.aggressor if self.worst else None,
            "next_saturated_pct": round(self.next_saturated * 100, 2),
            "next_all_switching_pct": round(self.next_fraction() * 100, 2),
            "fext_all_switching_pct": round(self.fext_fraction() * 100, 2),
            "reference_rise_time_ps": self.rise_time_ps,
            "fext_ps_sum": round(self.fext_ps_sum, 3),
        }

    def report(self, **kwargs) -> str:
        head = (f"{self.victim}: {len(self.aggressors)} aggressor(s); at "
                f"{self.rise_time_ps:.0f} ps edges NEXT {self.next_fraction():.2%} "
                f"and FEXT {self.fext_fraction():+.2%} with all of them "
                f"switching together (worst single aggressor bounds NEXT at "
                f"{self.next_saturated:.2%} for any edge rate)")
        return "\n".join([head] + [c.report(**kwargs) for c in self.aggressors])


# ---------------------------------------------------------------------------
# Measuring the coupled cross-section
# ---------------------------------------------------------------------------

def _aggressor_widths(board: Board, aggressor_code: int, layer: str,
                      points: np.ndarray) -> np.ndarray:
    """Width of the nearest aggressor track at each sampled point.

    The aggressor's width belongs in the cross-section: a 0.8 mm power trace
    and a 0.15 mm signal beside the same victim do not couple to it equally,
    and `differential_section` cannot express the difference.
    """
    def build():
        tracks = [t for t in board.tracks_on_net(aggressor_code)
                  if t.layer == layer and t.length_mm > 0]
        if len(tracks) < 2:
            return tracks, None
        return tracks, STRtree([t.copper for t in tracks])

    # Memoised per (net, layer): the tree depends only on the aggressor's own
    # copper, and the survey rebuilt it for every victim that net threatens.
    tracks, tree = board.memo(("aggressor_widths", aggressor_code, layer), build)
    if not tracks:
        return np.zeros(len(points))
    if tree is None:
        return np.full(len(points), tracks[0].width_mm)
    return np.array([tracks[i].width_mm for i in tree.nearest(points)])


def _quantise(value: float, quantum: float = QUANTUM_MM) -> float:
    return round(round(value / quantum) * quantum, 6)


def _quantise_clearance(clearance_mm: float,
                        context: routed.LayerContext) -> float:
    """Clearance to the pour, capped and quantised on its own coarser ladder."""
    limit = MAX_USEFUL_CLEARANCE_RATIO * context.height_mm
    if math.isinf(clearance_mm) or clearance_mm > limit:
        clearance_mm = limit
    return max(CLEARANCE_QUANTUM_MM,
               _quantise(clearance_mm, CLEARANCE_QUANTUM_MM))


def _section_for(context: routed.LayerContext, victim_width_mm: float,
                 aggressor_width_mm: float, gap_mm: float, clearance_mm: float,
                 mask: fields.SolderMask | None) -> fields.CrossSection:
    """The cross-section a victim and aggressor form on their layer.

    The measured clearance to ground is applied on *both* outer sides, matching
    what `routed.py` does for a single trace: only one clearance is measured —
    the nearest ground in any direction — and a symmetric pour is the least
    assuming way to spend it.
    """
    pour = None if math.isinf(clearance_mm) else clearance_mm
    if context.is_outer:
        return fields.coupled_microstrip_section(
            victim_width_mm, aggressor_width_mm, gap_mm, context.height_mm,
            context.thickness_mm, context.epsilon_r, gap_to_pour_mm=pour,
            mask=mask)
    return fields.coupled_stripline_section(
        victim_width_mm, aggressor_width_mm, gap_mm, context.height_mm,
        context.second_height_mm or context.height_mm, context.thickness_mm,
        context.epsilon_r, gap_to_pour_mm=pour)


def _broadside_section_for(context: routed.BroadsideContext, victim_layer: str,
                           victim_width_mm: float, aggressor_width_mm: float,
                           offset_mm: float, mask: fields.SolderMask | None
                           ) -> fields.CrossSection:
    """The cross-section a broadside pair forms across two adjacent layers.

    Each trace carries the distance to the plane on its own far side, which is
    what the builder asks for; it decides the orientation itself. The mask is
    passed only when one of the two is an outer layer, matching the rule the
    other builders follow.
    """
    aggressor_layer = context.other(victim_layer)
    victim_height = context.far_height_mm(victim_layer)
    aggressor_height = context.far_height_mm(aggressor_layer)
    outer = victim_height is None or aggressor_height is None
    return fields.broadside_section(
        victim_width_mm, aggressor_width_mm,
        separation_mm=context.separation_mm, offset_mm=offset_mm,
        victim_height_mm=victim_height, aggressor_height_mm=aggressor_height,
        thickness_mm=context.thickness_mm, epsilon_r=context.epsilon_r,
        mask=mask if outer else None)


class SolveCache:
    """One field solve per distinct quantised cross-section.

    Shared across a whole survey rather than per net pair, because a board
    routed to one set of design rules produces the same handful of
    cross-sections over and over: on these two boards a full survey of every
    coupled run reuses roughly nine solves in ten.

    Optionally backed by a warm process pool (`solvepool.attached`). The pool
    sits *behind* this cache and never beside it: a lookup still consults one
    central dictionary, and only a miss is dispatched. That ordering is
    load-bearing — `audit()` shares one cache between the crosstalk and
    broadside checks, worth about 300 s on the four-layer board, and giving
    each worker its own cache would trade that away for less.

    With a pool attached the miss is dispatched but **not waited for**, so the
    caller can keep walking geometry while the solve runs. `request_*` returns
    a key, `resolve()` is the barrier, and `value()` reads the answer. The
    direct `coefficients()`/`broadside_coefficients()` calls are those three in
    a row and behave exactly as they always did.

    The persistent store sits behind *both*
    ------------------------------------------
    `solvestore` is consulted on an in-memory miss and **before** anything is
    dispatched, so a cross-section this machine has solved before costs a
    lookup and no worker at all. The ordering matters for the same reason the
    pool's does: a re-review whose copper did not move should not spend three
    seconds spawning nineteen processes it will never give work to. That is why
    `solves` counts solves that actually happened and `reused` counts the ones
    that came off disk — only the first drives `POOL_THRESHOLD`.

    What crosses the process boundary is the pair of capacitance matrices
    rather than the ten coefficients derived from them, because those matrices
    are what the store keeps: one solve then serves impedance and coupling
    alike, and the reduction happens in one place instead of two that could
    drift.
    """

    #: Start the pool only once this many cross-sections have actually needed
    #: solving. Spawning and warming the workers costs about 3 s, which is
    #: roughly 30 solves — so on a board that only ever needs 52 of them (the
    #: two-layer harness board) an eagerly-started pool made the audit
    #: *slower*, 11.6 s to 14.9 s. Below the threshold the in-process path is
    #: simply the better one, and a small board never pays for machinery it
    #: cannot use. Set well above 30 so the decision is not marginal.
    POOL_THRESHOLD = 96

    def __init__(self, refinement: float = DEFAULT_REFINEMENT,
                 mask: fields.SolderMask | None = fields.JLC_SOLDER_MASK,
                 pool=None, pool_factory=None):
        self.refinement = refinement
        self.mask = mask
        self.pool = pool
        #: called to build a pool once `POOL_THRESHOLD` solves are needed
        self.pool_factory = pool_factory
        self._cache: dict[tuple, Coefficients] = {}
        #: keys dispatched to the pool and not yet collected, each with the
        #: section and store key needed to record the answer when it lands
        self._pending: dict[tuple, _Dispatched] = {}
        #: cross-sections this run actually solved. Drives `POOL_THRESHOLD`.
        self.solves = 0
        #: cross-sections served by the persistent store instead of solved
        self.reused = 0
        self.lookups = 0

    # -- registering work ---------------------------------------------------

    def request_coefficients(self, context: routed.LayerContext,
                             victim_width_mm: float, aggressor_width_mm: float,
                             gap_mm: float, clearance_mm: float) -> tuple:
        """Ensure this cross-section will be solved; return its key."""
        key = (context, victim_width_mm, aggressor_width_mm, gap_mm, clearance_mm)
        return self._request(key, lambda: _section_for(
            context, victim_width_mm, aggressor_width_mm, gap_mm, clearance_mm,
            self.mask))

    def request_broadside(self, context: routed.BroadsideContext,
                          victim_layer: str, victim_width_mm: float,
                          aggressor_width_mm: float,
                          offset_mm: float) -> tuple:
        key = ("broadside", context, victim_layer, victim_width_mm,
               aggressor_width_mm, offset_mm)
        return self._request(key, lambda: _broadside_section_for(
            context, victim_layer, victim_width_mm, aggressor_width_mm,
            offset_mm, self.mask))

    def _request(self, key: tuple, build_section) -> tuple:
        self.lookups += 1
        if key in self._cache or key in self._pending:
            return key
        section = build_section()

        # The persistent store first, and before any decision about the pool:
        # a solve that is already on disk needs no worker, and a board whose
        # copper has not moved should never start one.
        store_key, cached = solvestore.lookup(section, self.refinement)
        if cached is not None:
            self.reused += 1
            self._cache[key] = coefficients(cached)
            return key

        self.solves += 1
        if (self.pool is None and self.pool_factory is not None
                and self.solves >= self.POOL_THRESHOLD):
            self._start_pool()
        if self.pool is None or not self.pool.enabled:
            solution = fields.solve(section, refinement=self.refinement)
            solvestore.remember(store_key, solution)
            self._cache[key] = coefficients(solution)
        else:
            self._pending[key] = _Dispatched(
                future=self.pool.submit(solvepool.solve_matrices, section,
                                        self.refinement),
                section=section, store_key=store_key)
        return key

    def _land(self, key: tuple, dispatched: "_Dispatched") -> None:
        """One worker's answer into the cache, and into the store.

        The reduction to coefficients happens here rather than in the worker so
        that the solved path and the cached path run the identical arithmetic
        over the identical matrices — which is what makes a warm review
        byte-identical to a cold one rather than merely close.
        """
        capacitance, capacitance_air, mesh_nodes = dispatched.future.result()
        solution = fields.solution_from(dispatched.section, capacitance,
                                        capacitance_air, mesh_nodes)
        solvestore.remember(dispatched.store_key, solution)
        self._cache[key] = coefficients(solution)

    def _start_pool(self) -> None:
        """Bring the pool up, once and only once, and never fatally.

        A failure here means this board is reviewed on one core instead of
        twenty. That is a slow answer, not a wrong one, and it is a far better
        outcome than refusing to review a PCB because the machine would not
        spawn a process — see `solvepool.attached`.
        """
        factory, self.pool_factory = self.pool_factory, None
        try:
            self.pool = factory()
        except Exception:                            # noqa: BLE001
            self.pool = None

    # -- collecting it ------------------------------------------------------

    def resolve(self, ignore_errors: bool = False) -> None:
        """Wait for every dispatched solve. The barrier.

        Called once per victim rather than once per net pair: a pair typically
        contributes a couple of new cross-sections, which would leave the pool
        mostly idle, while a victim contributes tens.
        """
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        for key, dispatched in pending.items():
            try:
                self._land(key, dispatched)
            except Exception:                       # noqa: BLE001
                if not ignore_errors:
                    raise
                # Unrecorded, so a later lookup solves it in process rather
                # than reading a value that was never computed.
                self.solves -= 1

    def value(self, key: tuple) -> Coefficients:
        """The coefficients for a key that `request_*` has already registered."""
        if key in self._pending:
            self._land(key, self._pending.pop(key))
        try:
            return self._cache[key]
        except KeyError:
            raise CrosstalkError(
                "cross-section was never requested from the cache; "
                "`request_*` must precede `value`") from None

    # -- the direct form, unchanged -----------------------------------------

    def coefficients(self, context: routed.LayerContext, victim_width_mm: float,
                     aggressor_width_mm: float, gap_mm: float,
                     clearance_mm: float) -> Coefficients:
        return self.value(self.request_coefficients(
            context, victim_width_mm, aggressor_width_mm, gap_mm, clearance_mm))

    def broadside_coefficients(self, context: routed.BroadsideContext,
                               victim_layer: str, victim_width_mm: float,
                               aggressor_width_mm: float, offset_mm: float
                               ) -> Coefficients:
        return self.value(self.request_broadside(
            context, victim_layer, victim_width_mm, aggressor_width_mm,
            offset_mm))

    @property
    def distinct(self) -> int:
        """Cross-sections this run needed at all — solved plus read from disk."""
        return self.solves + self.reused

    @property
    def hit_rate(self) -> float:
        """How much of the walk this in-memory cache absorbed.

        Deliberately still measured against `distinct` and not `solves`, so it
        keeps meaning what it always meant — how often one cross-section was
        asked for twice — rather than silently becoming a report on the
        persistent store, which has its own counters.
        """
        return 1.0 - self.distinct / self.lookups if self.lookups else 0.0


@dataclass
class _Dispatched:
    """A solve running in a worker, and what is needed when it lands.

    The section is held because `fields.solution_from` takes one and because
    the store is written in the parent; the store key is held so a section is
    hashed once per solve rather than once on the way out and again on the way
    back.
    """
    future: object
    section: fields.CrossSection
    store_key: bytes | None


@dataclass
class _ProfileFill:
    """Where a walked profile's values go once the solves have landed.

    The walk cannot write ``k_backward`` into the profile as it goes any more —
    the answer may still be in a worker — so it records the positions and the
    cache keys, and the assembly pass fills them in.
    """
    profile: np.ndarray
    indices: np.ndarray
    keys: list[tuple]

    def apply(self, cache: SolveCache) -> None:
        for index, key in zip(self.indices, self.keys):
            self.profile[index] = cache.value(key).k_backward


@dataclass
class _WalkedCoupling:
    """One net pair's geometry, measured but not yet solved."""
    victim: str
    aggressor: str
    victim_length_mm: float
    aggressor_is_rail: bool
    buckets: dict[tuple, float]
    contexts: dict
    profiles_by_layer: dict
    fills: list[_ProfileFill]
    warnings: list[str]

    @property
    def has_sections(self) -> bool:
        """Whether assembly will produce any section, known before solving."""
        return bool(self.buckets)


def analyse_coupling(board: Board, victim: int | str, aggressor: int | str,
                     ground_nets: set[str],
                     max_gap_mm: float = coupling_module.DEFAULT_MAX_GAP_MM,
                     min_run_mm: float = MIN_RUN_LENGTH_MM,
                     cache: SolveCache | None = None,
                     step_mm: float = SAMPLE_STEP_MM,
                     aggressor_is_rail: bool = False) -> Crosstalk:
    """Solve the coupling between two nets along the geometry they share.

    Every sample of the victim that runs coupled to the aggressor carries five
    measurements — layer, both widths, the gap between them, and the clearance
    to ground — which together are the cross-section. Those are quantised and
    bucketed exactly as `routed.py` does, so a 40 mm run costs a handful of
    field solves rather than sixteen hundred.

    Walk, barrier, assemble. `analyse_victim` runs the same three phases with
    the barrier moved out past every aggressor, which is what lets a pool see
    tens of cross-sections at once instead of two.
    """
    cache = cache or SolveCache()
    walked = _walk_coupling(board, victim, aggressor, ground_nets,
                            max_gap_mm=max_gap_mm, min_run_mm=min_run_mm,
                            cache=cache, step_mm=step_mm,
                            aggressor_is_rail=aggressor_is_rail)
    cache.resolve()
    return _assemble_coupling(walked, cache)


def _walk_coupling(board: Board, victim: int | str, aggressor: int | str,
                   ground_nets: set[str],
                   max_gap_mm: float = coupling_module.DEFAULT_MAX_GAP_MM,
                   min_run_mm: float = MIN_RUN_LENGTH_MM,
                   cache: SolveCache | None = None,
                   step_mm: float = SAMPLE_STEP_MM,
                   aggressor_is_rail: bool = False) -> _WalkedCoupling:
    """Measure the geometry and register the cross-sections it needs.

    Solves nothing itself. Every cross-section it meets is handed to the cache,
    which either has it already or dispatches it; the answers are read back in
    `_assemble_coupling` once the caller has decided where to put the barrier.
    """
    victim_net = board.net(victim)
    aggressor_net = board.net(aggressor)
    cache = cache or SolveCache()

    samples = coupling_module.sampled_gaps(
        board, victim_net.code, aggressor_net.code, max_gap_mm=max_gap_mm,
        step_mm=step_mm)
    victim_length = sum(t.length_mm for t in board.tracks_on_net(victim_net.code))

    ground_cache: dict[str, object] = {}
    plane_cache: dict = {}
    contexts: dict[str, routed.LayerContext | None] = {}
    footprints = routed.footprints_by_layer(board, victim_net.code)

    buckets: dict[tuple, float] = {}
    profiles_by_layer: dict[str, list[tuple[float, np.ndarray]]] = {}
    fills: list[_ProfileFill] = []
    warnings: list[str] = []
    minimum_samples = max(1, int(round(min_run_mm / step_mm)))

    for entry in samples:
        layer = entry.layer
        if layer not in contexts:
            contexts[layer] = routed.layer_context(
                board, layer, footprints.get(layer), plane_cache)
            context = contexts[layer]
            if context is not None:
                warnings.extend(context.warnings)
        context = contexts[layer]
        if context is None:
            warnings.append(f"{layer} has no reference plane; skipped")
            continue

        # Drop stretches too short to be a run, so a corner clip does not become
        # a section. Same criterion `coupling.py` applies to its runs.
        keep = _long_enough(entry.coupled, minimum_samples)
        if not keep.any():
            profiles_by_layer.setdefault(layer, []).append(
                (entry.step_mm, np.zeros(len(entry.gap_mm))))
            continue

        if layer not in ground_cache:
            ground_cache[layer] = routed._ground_copper(board, layer, ground_nets)
        ground = ground_cache[layer]
        if ground is None:
            clearances = np.full(len(entry.gap_mm), float("inf"))
        else:
            clearances = np.maximum(
                shapely.distance(entry.points, ground)
                - entry.victim_width_mm / 2.0, 0.0)

        widths = _aggressor_widths(board, aggressor_net.code, layer, entry.points)

        profile = np.zeros(len(entry.gap_mm))
        indices = np.flatnonzero(keep)
        keys: list[tuple] = []
        for index in indices:
            clearance = _quantise_clearance(float(clearances[index]), context)
            key = (layer,
                   _quantise(entry.victim_width_mm),
                   _quantise(float(widths[index])),
                   max(QUANTUM_MM, _quantise(float(entry.gap_mm[index]))),
                   clearance)
            buckets[key] = buckets.get(key, 0.0) + entry.step_mm
            keys.append(cache.request_coefficients(
                context, key[1], key[2], key[3], key[4]))
        profiles_by_layer.setdefault(layer, []).append((entry.step_mm, profile))
        fills.append(_ProfileFill(profile, indices, keys))

    return _WalkedCoupling(
        victim=victim_net.name, aggressor=aggressor_net.name,
        victim_length_mm=victim_length, aggressor_is_rail=aggressor_is_rail,
        buckets=buckets, contexts=contexts,
        profiles_by_layer=profiles_by_layer, fills=fills,
        warnings=warnings)


def _assemble_coupling(walked: _WalkedCoupling, cache: SolveCache) -> Crosstalk:
    """Turn a walked pair into its `Crosstalk`. Pure lookups, no solving."""
    for fill in walked.fills:
        fill.apply(cache)

    sections: list[CrosstalkSection] = []
    for (layer, victim_width, aggressor_width, gap, clearance), length in sorted(
            walked.buckets.items()):
        context = walked.contexts[layer]
        sections.append(CrosstalkSection(
            layer=layer, length_mm=length, gap_mm=gap,
            victim_width_mm=victim_width, aggressor_width_mm=aggressor_width,
            clearance_mm=clearance, reference_layer=context.reference_layer,
            height_mm=context.height_mm,
            coefficients=cache.coefficients(context, victim_width,
                                            aggressor_width, gap, clearance)))

    # Concatenate each layer's per-track profiles in track order, so a coupled
    # run spanning two track objects is one stretch when the rise-time window is
    # applied. Branching nets have no single position axis and are approximated
    # here; the saturated peak, which is the headline, does not depend on it.
    profiles = []
    for layer, entries in walked.profiles_by_layer.items():
        step = entries[0][0]
        profiles.append((step, np.concatenate([p for _, p in entries])))

    return Crosstalk(victim=walked.victim, aggressor=walked.aggressor,
                     sections=sections,
                     victim_length_mm=walked.victim_length_mm,
                     profiles=profiles,
                     aggressor_is_rail=walked.aggressor_is_rail,
                     warnings=sorted(set(walked.warnings)))


def _long_enough(mask: np.ndarray, minimum_samples: int) -> np.ndarray:
    """Keep only contiguous True stretches of at least ``minimum_samples``."""
    if not mask.any() or minimum_samples <= 1:
        return mask
    padded = np.concatenate(([False], mask, [False]))
    flips = np.flatnonzero(padded[1:] != padded[:-1])
    keep = np.zeros_like(mask)
    for start, end in zip(flips[0::2], flips[1::2]):
        if end - start >= minimum_samples:
            keep[start:end] = True
    return keep


# ---------------------------------------------------------------------------
# Driving it over a whole board
# ---------------------------------------------------------------------------

def analyse_victim(board: Board, victim: int | str, ground_nets: set[str],
                   rail_nets: set[str] | None = None,
                   max_gap_mm: float = coupling_module.DEFAULT_MAX_GAP_MM,
                   min_coupled_mm: float = 1.0,
                   cache: SolveCache | None = None,
                   rise_time_ps: float = REFERENCE_RISE_TIME_PS,
                   **kwargs) -> VictimCrosstalk:
    """Every aggressor coupled to one victim, solved and ranked.

    The shortlist comes from `coupling.neighbours_of`, which pre-filters
    candidates by a buffered bounding box so only plausible aggressors get the
    full sampled measurement. Ground nets are never aggressors: they do not
    switch, and a grounded trace beside a signal is a guard rather than a
    threat.
    """
    rail_nets = rail_nets or set()
    cache = cache or SolveCache()
    victim_net = board.net(victim)

    neighbours = coupling_module.neighbours_of(
        board, victim_net.code, max_gap_mm=max_gap_mm,
        min_coupled_mm=min_coupled_mm)

    # Walk every aggressor first, then solve, then assemble. One barrier per
    # victim rather than per pair: a pair contributes a couple of new
    # cross-sections and would leave a pool almost idle, a victim contributes
    # tens. Misses were dispatched during the walk, so most of this batch is
    # already finished by the time the barrier is reached.
    walked = [
        _walk_coupling(board, victim_net.code, neighbour.aggressor, ground_nets,
                       max_gap_mm=max_gap_mm, cache=cache,
                       aggressor_is_rail=neighbour.aggressor in rail_nets,
                       **kwargs)
        for neighbour in neighbours if neighbour.aggressor not in ground_nets]
    cache.resolve()

    results = [_assemble_coupling(pair, cache)
               for pair in walked if pair.has_sections]
    results.sort(key=lambda c: -c.next_mm)
    return VictimCrosstalk(
        victim=victim_net.name, aggressors=results, rise_time_ps=rise_time_ps,
        victim_length_mm=sum(t.length_mm
                             for t in board.tracks_on_net(victim_net.code)))


def broadside_windows(board: Board,
                      offset_ratio: float = coupling_module.BROADSIDE_OFFSET_RATIO
                      ) -> dict[frozenset, float]:
    """Each adjacent layer pair's lateral window, from its own dielectric.

    Per pair rather than one number for the board, because the separations
    differ by five times on a six-layer stackup — 0.109 mm of prepreg between
    In2 and In3, 0.55 mm of core either side. Sizing every window off the
    largest would put five times as many nets through the sampled measurement
    for coupling the geometry cannot produce.
    """
    windows: dict[frozenset, float] = {}
    plane_cache: dict = {}
    for upper, lower in routed.adjacent_signal_layers(board):
        context = routed.broadside_context(board, upper, lower,
                                           plane_cache=plane_cache)
        if context is not None:
            windows[coupling_module.layer_pair_key(upper, lower)] = (
                offset_ratio * context.separation_mm)
    return windows


def analyse_broadside(board: Board, victim: int | str, aggressor: int | str,
                      min_run_mm: float = MIN_RUN_LENGTH_MM,
                      cache: SolveCache | None = None,
                      step_mm: float = SAMPLE_STEP_MM,
                      offset_ratio: float = coupling_module.BROADSIDE_OFFSET_RATIO,
                      windows: dict[frozenset, float] | None = None,
                      aggressor_is_rail: bool = False) -> Crosstalk:
    """Solve the coupling between two nets stacked on adjacent layers.

    The mirror of `analyse_coupling` across the stackup. The cross-section is
    fixed by the fab in every dimension except one — the lateral offset between
    the two centrelines — so a broadside run buckets on offset alone and needs
    no clearance measurement: the pours beside a stacked pair are much further
    away than the layer below it, and the field between them never reaches
    them.

    Walk, barrier, assemble, as `analyse_coupling` does; `broadside_survey`
    moves the barrier out to the victim.
    """
    cache = cache or SolveCache()
    walked = _walk_broadside(board, victim, aggressor, min_run_mm=min_run_mm,
                             cache=cache, step_mm=step_mm,
                             offset_ratio=offset_ratio, windows=windows,
                             aggressor_is_rail=aggressor_is_rail)
    cache.resolve()
    return _assemble_broadside(walked, cache)


def _walk_broadside(board: Board, victim: int | str, aggressor: int | str,
                    min_run_mm: float = MIN_RUN_LENGTH_MM,
                    cache: SolveCache | None = None,
                    step_mm: float = SAMPLE_STEP_MM,
                    offset_ratio: float = coupling_module.BROADSIDE_OFFSET_RATIO,
                    windows: dict[frozenset, float] | None = None,
                    aggressor_is_rail: bool = False) -> _WalkedCoupling:
    """Measure a stacked pair's geometry and register its cross-sections."""
    victim_net = board.net(victim)
    aggressor_net = board.net(aggressor)
    cache = cache or SolveCache()

    plane_cache: dict = {}
    victim_footprints = routed.footprints_by_layer(board, victim_net.code)
    aggressor_footprints = routed.footprints_by_layer(board, aggressor_net.code)

    contexts: dict[tuple[str, str], routed.BroadsideContext | None] = {}
    warnings: list[str] = []

    def context_for(victim_layer: str, aggressor_layer: str):
        key = (victim_layer, aggressor_layer)
        if key not in contexts:
            upper, lower = (
                (victim_layer, aggressor_layer)
                if board.layer_index(victim_layer) < board.layer_index(aggressor_layer)
                else (aggressor_layer, victim_layer))
            footprints = {victim_layer: victim_footprints.get(victim_layer),
                          aggressor_layer: aggressor_footprints.get(aggressor_layer)}
            contexts[key] = routed.broadside_context(
                board, upper, lower, footprints.get(upper), footprints.get(lower),
                plane_cache)
            if contexts[key] is not None:
                warnings.extend(contexts[key].warnings)
        return contexts[key]

    if windows is None:
        windows = broadside_windows(board, offset_ratio)
    if not windows:
        return _WalkedCoupling(
            victim=victim_net.name, aggressor=aggressor_net.name,
            victim_length_mm=0.0, aggressor_is_rail=aggressor_is_rail,
            buckets={}, contexts={}, profiles_by_layer={}, fills=[],
            warnings=[])

    samples = coupling_module.broadside_gaps(
        board, victim_net.code, aggressor_net.code, windows, step_mm=step_mm)
    victim_length = sum(t.length_mm for t in board.tracks_on_net(victim_net.code))

    buckets: dict[tuple, float] = {}
    profiles_by_layer: dict[tuple[str, str], list[tuple[float, np.ndarray]]] = {}
    fills: list[_ProfileFill] = []
    minimum_samples = max(1, int(round(min_run_mm / step_mm)))

    for entry in samples:
        context = context_for(entry.layer, entry.aggressor_layer)
        if context is None:
            continue
        key_layers = (entry.layer, entry.aggressor_layer)
        # `broadside_gaps` already applied this adjacency's own window.
        keep = _long_enough(entry.coupled, minimum_samples)
        if not keep.any():
            profiles_by_layer.setdefault(key_layers, []).append(
                (entry.step_mm, np.zeros(len(entry.lateral_mm))))
            continue

        widths = _aggressor_widths(board, aggressor_net.code,
                                   entry.aggressor_layer, entry.points)
        profile = np.zeros(len(entry.lateral_mm))
        indices = np.flatnonzero(keep)
        keys: list[tuple] = []
        for index in indices:
            key = (entry.layer, entry.aggressor_layer,
                   _quantise(entry.victim_width_mm),
                   _quantise(float(widths[index])),
                   _quantise(float(entry.lateral_mm[index])))
            buckets[key] = buckets.get(key, 0.0) + entry.step_mm
            keys.append(cache.request_broadside(
                context, entry.layer, key[2], key[3], key[4]))
        profiles_by_layer.setdefault(key_layers, []).append(
            (entry.step_mm, profile))
        fills.append(_ProfileFill(profile, indices, keys))

    return _WalkedCoupling(
        victim=victim_net.name, aggressor=aggressor_net.name,
        victim_length_mm=victim_length, aggressor_is_rail=aggressor_is_rail,
        buckets=buckets, contexts=contexts,
        profiles_by_layer=profiles_by_layer, fills=fills, warnings=warnings)


def _assemble_broadside(walked: _WalkedCoupling, cache: SolveCache) -> Crosstalk:
    """Turn a walked stacked pair into its `Crosstalk`. Pure lookups."""
    for fill in walked.fills:
        fill.apply(cache)

    sections: list[CrosstalkSection] = []
    for (layer, aggressor_layer, victim_width, aggressor_width,
         offset), length in sorted(walked.buckets.items()):
        context = walked.contexts[(layer, aggressor_layer)]
        reference = (context.upper_reference if layer == context.upper_layer
                     else context.lower_reference)
        sections.append(CrosstalkSection(
            layer=layer, length_mm=length, gap_mm=offset,
            victim_width_mm=victim_width, aggressor_width_mm=aggressor_width,
            clearance_mm=float("inf"),
            reference_layer=reference or "air",
            height_mm=context.far_height_mm(layer) or 0.0,
            coefficients=cache.broadside_coefficients(
                context, layer, victim_width, aggressor_width, offset),
            aggressor_layer=aggressor_layer,
            separation_mm=context.separation_mm))

    profiles = []
    for entries in walked.profiles_by_layer.values():
        step = entries[0][0]
        profiles.append((step, np.concatenate([p for _, p in entries])))

    return Crosstalk(victim=walked.victim, aggressor=walked.aggressor,
                     sections=sections,
                     victim_length_mm=walked.victim_length_mm,
                     profiles=profiles,
                     aggressor_is_rail=walked.aggressor_is_rail,
                     warnings=sorted(set(walked.warnings)))


def broadside_neighbours(board: Board, victim: int | str,
                         windows: dict[frozenset, float]) -> list[int]:
    """Net codes with copper on a layer next to the victim's, overlapping it.

    The broadside counterpart of `coupling.neighbours_of`'s bounding-box
    prefilter — cheap, and it keeps the sampled measurement off nets that
    cannot possibly stack over this one. Each adjacency is buffered by its own
    window, so a thick core between two layers does not widen the search over
    a thin prepreg between two others.
    """
    victim_net = board.net(victim)
    victim_tracks = board.tracks_on_net(victim_net.code)
    if not victim_tracks:
        return []

    # (victim layer, aggressor layer) -> the victim's copper buffered by that
    # adjacency's own window.
    envelopes: dict[tuple[str, str], object] = {}
    for pair, window in windows.items():
        for layer in pair:
            other = next(iter(pair - {layer}))
            parts = [t.copper for t in victim_tracks if t.layer == layer]
            if parts:
                envelopes[(layer, other)] = unary_union(parts).buffer(window)
    if not envelopes:
        return []

    out = []
    for net in board.nets.values():
        if net.code == victim_net.code or not net.name:
            continue
        for track in board.tracks_on_net(net.code):
            hit = any(track.layer == other and envelope.intersects(track.copper)
                      for (_, other), envelope in envelopes.items())
            if hit:
                out.append(net.code)
                break
    return out


def broadside_survey(board: Board, ground_nets: set[str],
                     rail_nets: set[str] | None = None,
                     threshold: float = REPORTABLE_COUPLING,
                     min_coupled_mm: float = 1.0,
                     cache: SolveCache | None = None,
                     rise_time_ps: float = REFERENCE_RISE_TIME_PS,
                     victims_exclude: set[str] | None = None,
                     offset_ratio: float = coupling_module.BROADSIDE_OFFSET_RATIO,
                     **kwargs) -> list[VictimCrosstalk]:
    """Every stacked run on the board, solved and ranked.

    Kept separate from `survey` rather than folded into it, because the two
    measure different quantities and answer to different design controls: a
    side-by-side finding is fixed by widening a clearance, a broadside one by
    moving one of the two layers' routing sideways or by changing which layer
    it uses.
    """
    ground_nets = set(ground_nets)
    rail_nets = set(rail_nets or set())
    victims_exclude = (ground_nets | rail_nets if victims_exclude is None
                       else set(victims_exclude))
    cache = cache or SolveCache()

    windows = broadside_windows(board, offset_ratio)
    if not windows:
        return []

    results: list[VictimCrosstalk] = []
    for net in board.nets.values():
        if not net.name or net.name in victims_exclude:
            continue
        if not board.tracks_on_net(net.code):
            continue
        # One barrier per victim, as in `analyse_victim`.
        walked = [
            _walk_broadside(board, net.code, code, cache=cache,
                            aggressor_is_rail=board.net(code).name in rail_nets,
                            windows=windows, **kwargs)
            for code in broadside_neighbours(board, net.code, windows)
            if board.net(code).name not in ground_nets]
        cache.resolve()

        aggressors: list[Crosstalk] = []
        for pair in walked:
            if not pair.has_sections:
                continue
            result = _assemble_broadside(pair, cache)
            if result.coupled_length_mm >= min_coupled_mm:
                aggressors.append(result)
        if not aggressors:
            continue
        aggressors.sort(key=lambda c: -c.next_mm)
        victim = VictimCrosstalk(
            victim=net.name, aggressors=aggressors, rise_time_ps=rise_time_ps,
            victim_length_mm=sum(t.length_mm
                                 for t in board.tracks_on_net(net.code)))
        if victim.worst_fraction >= threshold:
            results.append(victim)

    results.sort(key=lambda v: -v.worst_fraction)
    return results


def survey(board: Board, ground_nets: set[str],
           rail_nets: set[str] | None = None,
           max_gap_mm: float = coupling_module.DEFAULT_MAX_GAP_MM,
           min_coupled_mm: float = 1.0,
           threshold: float = REPORTABLE_COUPLING,
           cache: SolveCache | None = None,
           rise_time_ps: float = REFERENCE_RISE_TIME_PS,
           victims_exclude: set[str] | None = None,
           **kwargs) -> list[VictimCrosstalk]:
    """Every coupled run on the board, solved and ranked.

    Ranked on the coupling at ``rise_time_ps``, which is an assumption and is
    carried on every result — see `REFERENCE_RISE_TIME_PS`. It is nonetheless
    the right thing to rank on, because the alternative that needs no
    assumption, `Crosstalk.next_saturated`, is set by the minimum gap and
    therefore reads much the same for every net on a board routed to one
    clearance rule.

    Power rails are not audited as victims — noise on a decoupled rail is a
    different question from noise on a signal — but they remain candidate
    aggressors, flagged, since a regulator's switch node genuinely is one.
    """
    ground_nets = set(ground_nets)
    rail_nets = set(rail_nets or set())
    victims_exclude = (ground_nets | rail_nets if victims_exclude is None
                       else set(victims_exclude))
    cache = cache or SolveCache()

    results: list[VictimCrosstalk] = []
    for net in board.nets.values():
        if not net.name or net.name in victims_exclude:
            continue
        if not board.tracks_on_net(net.code):
            continue
        victim = analyse_victim(board, net.code, ground_nets,
                                rail_nets=rail_nets, max_gap_mm=max_gap_mm,
                                min_coupled_mm=min_coupled_mm, cache=cache,
                                rise_time_ps=rise_time_ps, **kwargs)
        if victim.worst_fraction >= threshold:
            results.append(victim)

    results.sort(key=lambda v: -v.worst_fraction)
    return results
