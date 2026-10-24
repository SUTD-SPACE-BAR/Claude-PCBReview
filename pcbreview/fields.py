"""2D electrostatic field solver for PCB cross-sections.

The closed forms in `physics.py` each hold over a band of geometries and fail
outside it, and the geometries on these boards sit near the edges of those
bands. Two concrete failures motivated this:

* **Coplanar.** JLCPCB quotes its targets as coplanar — a ground pour beside
  the trace. scikit-rf's CPW model reports 56 ohm where microstrip says 52, and
  it *never converges back to microstrip as the gap widens*: at a 5 mm gap, 24x
  the dielectric height, where the pour cannot physically matter, it still says
  61.8 ohm against a physically demanded 52.5. Trusting it would flag the user's
  GPS traces as 20% mismatched when they are not.
* **Thick copper.** On the six-layer board the copper is 0.035 mm against a 0.0994 mm
  dielectric — 35% — where Hammerstad-Jensen's thickness correction is
  strained and says so in its own warnings.

A field solver has no validity band. It spans tight-coplanar to wide-coplanar
to pure microstrip continuously, and it is the only way to answer the question
the layout actually poses: *this pair was designed to 90 ohm assuming 0.2032 mm
to the pour, but 12 mm of it runs at 0.35 mm — what is it really?*

Method
------
Discretise the cross-section and solve Laplace's equation with variable
permittivity, twice:

1. with the real dielectrics, giving the capacitance matrix **C**;
2. with vacuum everywhere, giving **C_air**.

Then **L = mu_0 eps_0 C_air^-1**. That identity is Paul's generalised
capacitance-matrix method (1976): with the dielectrics removed, the structure
propagates at c, so the inductance — which does not depend on the dielectric —
can be read off the vacuum capacitance. It means the magnetic problem never has
to be solved, and no per-conductor inductance is ever quoted, which is the
right outcome given that inductance is a property of a loop rather than of a
conductor.

Discretisation is finite-volume box integration on a graded rectilinear mesh.
Grid lines are pinned to every conductor edge and dielectric interface, so a
cell face never straddles a material boundary in the direction of its flux:
vertical faces sum eps over the sub-segments they span (parallel), and
horizontal faces lie within a single slab by construction. Charges come from
the *reaction* on the Dirichlet rows -- assemble the operator over every node,
solve for the free ones, then Q = (A phi) summed over each conductor. That is
exactly discretely charge-conserving, unlike integrating the normal field
around a staircase contour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

VACUUM_PERMITTIVITY = 8.8541878128e-12       # F/m
VACUUM_PERMEABILITY = 4e-7 * math.pi         # H/m
SPEED_OF_LIGHT = 299_792_458.0               # m/s

MM = 1e-3

# Mesh growth away from a feature. 1.25 keeps neighbouring cells similar enough
# that the second-order accuracy of box integration is not lost to the grading.
DEFAULT_GROWTH = 1.25

# How far the side walls and lid sit from the outermost conductor, in units of
# the substrate height. Both are natural (zero-flux) boundaries, so this must
# be large enough that the fringing field has decayed; `test_fields.py` shows
# the answer is flat from about 10 upward.
DEFAULT_MARGIN_RATIO = 20.0

DEFAULT_MASK_EPSILON_R = 3.8


class FieldSolverError(Exception):
    """Raised when a cross-section cannot be solved as described."""


# ---------------------------------------------------------------------------
# Cross-section description
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Conductor:
    """A rectangular conductor in the cross-section. Metres."""
    x0: float
    x1: float
    y0: float
    y1: float
    name: str
    is_ground: bool = False

    def contains(self, x: float, y: float, tol: float) -> bool:
        return (self.x0 - tol <= x <= self.x1 + tol
                and self.y0 - tol <= y <= self.y1 + tol)


@dataclass(frozen=True)
class Slab:
    """A horizontal dielectric layer spanning the full width. Metres."""
    y0: float
    y1: float
    epsilon_r: float


@dataclass
class CrossSection:
    """Everything the solver needs: stratified dielectrics plus conductors."""
    slabs: list[Slab]
    conductors: list[Conductor]
    x_left: float
    x_right: float
    y_bottom: float
    y_top: float
    #: shortest feature in the section; sets the base mesh size
    feature_mm: float = 0.05
    description: str = ""

    @property
    def signals(self) -> list[Conductor]:
        return [c for c in self.conductors if not c.is_ground]

    def epsilon_at(self, y: float) -> float:
        for slab in self.slabs:
            if slab.y0 <= y <= slab.y1:
                return slab.epsilon_r
        return 1.0

    def epsilon_over(self, y0: float, y1: float) -> float:
        """Length-weighted mean permittivity over a y-span.

        Correct for a vertical cell face, where the sub-segments in each
        material carry flux in parallel.
        """
        if y1 <= y0:
            return self.epsilon_at(y0)
        total = 0.0
        for slab in self.slabs:
            overlap = min(y1, slab.y1) - max(y0, slab.y0)
            if overlap > 0:
                total += overlap * slab.epsilon_r
        covered = sum(max(0.0, min(y1, s.y1) - max(y0, s.y0)) for s in self.slabs)
        if covered < (y1 - y0):
            total += (y1 - y0 - covered) * 1.0          # air above the stack
        return total / (y1 - y0)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SolderMask:
    """LPI solder mask over an outer layer.

    Two thicknesses, because mask is applied conformally and ends up thinner
    over copper than over the bare substrate beside it. Fabs publish both — JLC
    quotes at least 10 um over copper — and they are used as published. The
    difference is not cosmetic: modelling mask as one uniform block filling the
    whole gap puts these boards' fab targets 3.5-4.9% low, and leaving mask out
    entirely puts them 1.3-4.8% high.
    """
    over_substrate_mm: float = 0.020
    over_copper_mm: float = 0.010
    epsilon_r: float = DEFAULT_MASK_EPSILON_R


#: What a JLCPCB outer layer actually looks like. Passed explicitly rather than
#: applied by default, so a comparison against a bare-copper closed form is
#: never silently given a mask the closed form does not model.
JLC_SOLDER_MASK = SolderMask()


def _mask_slabs(height: float, thickness: float,
                mask: SolderMask | None) -> list[Slab]:
    """Solder mask as horizontal bands: in the gap, then over the copper.

    The gap beside a trace is mask up to `over_substrate_mm` and air above
    that; the trace top carries `over_copper_mm`. Both are exactly
    representable in a stratified solver, and correspond to the C1/C2/C3
    parameters Polar's own models expose.
    """
    if mask is None:
        return []
    in_gap = min(mask.over_substrate_mm * MM, thickness)
    over_copper = mask.over_copper_mm * MM

    slabs: list[Slab] = []
    if in_gap > 0:
        slabs.append(Slab(height, height + in_gap, mask.epsilon_r))
    if in_gap < thickness:
        slabs.append(Slab(height + in_gap, height + thickness, 1.0))
    top = height + max(thickness, mask.over_substrate_mm * MM)
    if over_copper > 0:
        slabs.append(Slab(top, top + over_copper, mask.epsilon_r))
    return slabs


def microstrip_section(width_mm: float, height_mm: float, thickness_mm: float,
                       epsilon_r: float,
                       gap_to_pour_mm: float | None = None,
                       margin_ratio: float = DEFAULT_MARGIN_RATIO,
                       mask: SolderMask | None = None) -> CrossSection:
    """One trace over a plane, optionally with coplanar pours beside it.

    ``gap_to_pour_mm=None`` is a plain microstrip. Supplying a gap adds ground
    pours on the same layer, which is what the fab means by "coplanar
    single-ended" — and, unlike a CPWG closed form, letting the gap grow
    returns this continuously to the microstrip answer.

    Pass ``mask=JLC_SOLDER_MASK`` for an outer layer being compared against a
    fab target; leave it off for a bare-copper comparison against a closed
    form, which has no mask either.
    """
    width, height = width_mm * MM, height_mm * MM
    thickness = thickness_mm * MM
    half = width / 2.0

    margin = max(margin_ratio * height, 4 * width)
    if gap_to_pour_mm is not None:
        margin = max(margin, 3 * gap_to_pour_mm * MM)
    x_right = half + margin
    y_top = height + thickness + margin

    conductors = [
        Conductor(-x_right, x_right, -thickness, 0.0, "ground", is_ground=True),
        Conductor(-half, half, height, height + thickness, "signal"),
    ]
    if gap_to_pour_mm is not None:
        gap = gap_to_pour_mm * MM
        conductors += [
            Conductor(-x_right, -(half + gap), height, height + thickness,
                      "pour_left", is_ground=True),
            Conductor(half + gap, x_right, height, height + thickness,
                      "pour_right", is_ground=True),
        ]

    feature = min(width_mm, height_mm, thickness_mm,
                  gap_to_pour_mm if gap_to_pour_mm else width_mm,
                  mask.over_copper_mm if mask else width_mm)
    return CrossSection(
        slabs=([Slab(-thickness, 0.0, epsilon_r), Slab(0.0, height, epsilon_r)]
               + _mask_slabs(height, thickness, mask)),
        conductors=conductors,
        x_left=-x_right, x_right=x_right, y_bottom=-thickness, y_top=y_top,
        feature_mm=feature,
        description=(f"microstrip w={width_mm} h={height_mm} t={thickness_mm} "
                     f"er={epsilon_r}"
                     + (f" gap={gap_to_pour_mm}" if gap_to_pour_mm else "")
                     + (" +mask" if mask else "")))


def differential_section(width_mm: float, spacing_mm: float, height_mm: float,
                         thickness_mm: float, epsilon_r: float,
                         gap_to_pour_mm: float | None = None,
                         margin_ratio: float = DEFAULT_MARGIN_RATIO,
                         mask: SolderMask | None = None) -> CrossSection:
    """An edge-coupled pair over a plane, optionally with coplanar pours.

    ``spacing_mm`` is edge to edge between the two traces, matching how fabs
    and `coupling.py` both quote it.
    """
    width, height = width_mm * MM, height_mm * MM
    thickness, spacing = thickness_mm * MM, spacing_mm * MM

    inner = spacing / 2.0
    outer = inner + width
    margin = max(margin_ratio * height, 4 * width, 2 * spacing)
    if gap_to_pour_mm is not None:
        margin = max(margin, 3 * gap_to_pour_mm * MM)
    x_right = outer + margin
    y_top = height + thickness + margin

    conductors = [
        Conductor(-x_right, x_right, -thickness, 0.0, "ground", is_ground=True),
        Conductor(-outer, -inner, height, height + thickness, "signal_n"),
        Conductor(inner, outer, height, height + thickness, "signal_p"),
    ]
    if gap_to_pour_mm is not None:
        gap = gap_to_pour_mm * MM
        conductors += [
            Conductor(-x_right, -(outer + gap), height, height + thickness,
                      "pour_left", is_ground=True),
            Conductor(outer + gap, x_right, height, height + thickness,
                      "pour_right", is_ground=True),
        ]

    feature = min(width_mm, height_mm, thickness_mm, spacing_mm,
                  gap_to_pour_mm if gap_to_pour_mm else spacing_mm,
                  mask.over_copper_mm if mask else spacing_mm)
    return CrossSection(
        slabs=([Slab(-thickness, 0.0, epsilon_r), Slab(0.0, height, epsilon_r)]
               + _mask_slabs(height, thickness, mask)),
        conductors=conductors,
        x_left=-x_right, x_right=x_right, y_bottom=-thickness, y_top=y_top,
        feature_mm=feature,
        description=(f"differential w={width_mm} s={spacing_mm} h={height_mm} "
                     f"t={thickness_mm} er={epsilon_r}"
                     + (f" gap={gap_to_pour_mm}" if gap_to_pour_mm else "")
                     + (" +mask" if mask else "")))


def _coupled_spans(victim_width_mm: float, aggressor_width_mm: float,
                   spacing_mm: float) -> tuple[float, float, float, float]:
    """x extents of a victim/aggressor pair straddling the origin, in metres.

    The gap between them is centred on x=0 so that an equal-width pair is
    exactly the geometry `differential_section` builds, which keeps the two
    builders' answers comparable.
    """
    half_gap = spacing_mm * MM / 2.0
    return (-(half_gap + victim_width_mm * MM), -half_gap,
            half_gap, half_gap + aggressor_width_mm * MM)


def coupled_microstrip_section(victim_width_mm: float, aggressor_width_mm: float,
                               spacing_mm: float, height_mm: float,
                               thickness_mm: float, epsilon_r: float,
                               gap_to_pour_mm: float | None = None,
                               margin_ratio: float = DEFAULT_MARGIN_RATIO,
                               mask: SolderMask | None = None) -> CrossSection:
    """Two outer-layer traces of *different* widths over a plane.

    `differential_section` is the equal-width case of this and stays the entry
    point for pairs, where the two halves are the same by construction. A
    crosstalk victim and its aggressor are two unrelated nets and generally are
    not, so the widths are separate here.

    **The victim is conductor 0.** Every quantity indexed off row 0 —
    ``z0_ohm``, ``epsilon_eff`` — therefore describes the victim, which is what
    the crosstalk coefficients need to be normalised against.
    """
    height, thickness = height_mm * MM, thickness_mm * MM
    victim_left, victim_right, aggressor_left, aggressor_right = _coupled_spans(
        victim_width_mm, aggressor_width_mm, spacing_mm)

    margin = max(margin_ratio * height,
                 4 * max(victim_width_mm, aggressor_width_mm) * MM,
                 2 * spacing_mm * MM)
    if gap_to_pour_mm is not None:
        margin = max(margin, 3 * gap_to_pour_mm * MM)
    x_left, x_right = victim_left - margin, aggressor_right + margin
    y_top = height + thickness + margin

    conductors = [
        Conductor(x_left, x_right, -thickness, 0.0, "ground", is_ground=True),
        Conductor(victim_left, victim_right, height, height + thickness, "victim"),
        Conductor(aggressor_left, aggressor_right, height, height + thickness,
                  "aggressor"),
    ]
    if gap_to_pour_mm is not None:
        gap = gap_to_pour_mm * MM
        conductors += [
            Conductor(x_left, victim_left - gap, height, height + thickness,
                      "pour_left", is_ground=True),
            Conductor(aggressor_right + gap, x_right, height, height + thickness,
                      "pour_right", is_ground=True),
        ]

    feature = min(victim_width_mm, aggressor_width_mm, height_mm, thickness_mm,
                  spacing_mm,
                  gap_to_pour_mm if gap_to_pour_mm else spacing_mm,
                  mask.over_copper_mm if mask else spacing_mm)
    return CrossSection(
        slabs=([Slab(-thickness, 0.0, epsilon_r), Slab(0.0, height, epsilon_r)]
               + _mask_slabs(height, thickness, mask)),
        conductors=conductors,
        x_left=x_left, x_right=x_right, y_bottom=-thickness, y_top=y_top,
        feature_mm=feature,
        description=(f"coupled microstrip wv={victim_width_mm} "
                     f"wa={aggressor_width_mm} s={spacing_mm} h={height_mm} "
                     f"t={thickness_mm} er={epsilon_r}"
                     + (f" gap={gap_to_pour_mm}" if gap_to_pour_mm else "")
                     + (" +mask" if mask else "")))


def coupled_stripline_section(victim_width_mm: float, aggressor_width_mm: float,
                              spacing_mm: float, height_below_mm: float,
                              height_above_mm: float, thickness_mm: float,
                              epsilon_r: float,
                              gap_to_pour_mm: float | None = None,
                              margin_ratio: float = DEFAULT_MARGIN_RATIO
                              ) -> CrossSection:
    """Two buried traces between two planes — the coupled stripline.

    This is the section §12a recorded as missing, and its absence is why
    `routed.analyse_pair` refused inner-layer differential pairs rather than
    approximating them. It is also the only honest way to look at coupling
    between two inner-layer signals, which on a six-layer board is where most
    of the routing lives.

    Being fully embedded, the medium is homogeneous, so the two coupling
    mechanisms cancel exactly and forward crosstalk vanishes — see
    `crosstalk.coefficients`, where that is used as the sharpest available
    check on this solver's mutual terms.
    """
    below, above = height_below_mm * MM, height_above_mm * MM
    thickness = thickness_mm * MM
    total = below + thickness + above
    victim_left, victim_right, aggressor_left, aggressor_right = _coupled_spans(
        victim_width_mm, aggressor_width_mm, spacing_mm)

    margin = max(margin_ratio * min(below, above),
                 4 * max(victim_width_mm, aggressor_width_mm) * MM,
                 2 * spacing_mm * MM)
    if gap_to_pour_mm is not None:
        margin = max(margin, 3 * gap_to_pour_mm * MM)
    x_left, x_right = victim_left - margin, aggressor_right + margin

    conductors = [
        Conductor(x_left, x_right, -thickness, 0.0, "ground_lower", is_ground=True),
        Conductor(x_left, x_right, total, total + thickness, "ground_upper",
                  is_ground=True),
        Conductor(victim_left, victim_right, below, below + thickness, "victim"),
        Conductor(aggressor_left, aggressor_right, below, below + thickness,
                  "aggressor"),
    ]
    if gap_to_pour_mm is not None:
        gap = gap_to_pour_mm * MM
        conductors += [
            Conductor(x_left, victim_left - gap, below, below + thickness,
                      "pour_left", is_ground=True),
            Conductor(aggressor_right + gap, x_right, below, below + thickness,
                      "pour_right", is_ground=True),
        ]

    return CrossSection(
        slabs=[Slab(0.0, total, epsilon_r)],
        conductors=conductors,
        x_left=x_left, x_right=x_right,
        y_bottom=-thickness, y_top=total + thickness,
        feature_mm=min(victim_width_mm, aggressor_width_mm, height_below_mm,
                       height_above_mm, thickness_mm, spacing_mm,
                       gap_to_pour_mm if gap_to_pour_mm else spacing_mm),
        description=(f"coupled stripline wv={victim_width_mm} "
                     f"wa={aggressor_width_mm} s={spacing_mm} "
                     f"hb={height_below_mm} ha={height_above_mm} "
                     f"t={thickness_mm} er={epsilon_r}"
                     + (f" gap={gap_to_pour_mm}" if gap_to_pour_mm else "")))


def broadside_section(victim_width_mm: float, aggressor_width_mm: float,
                      separation_mm: float, offset_mm: float,
                      victim_height_mm: float | None,
                      aggressor_height_mm: float | None,
                      thickness_mm: float, epsilon_r: float,
                      margin_ratio: float = DEFAULT_MARGIN_RATIO,
                      mask: SolderMask | None = None) -> CrossSection:
    """Two traces on *adjacent* layers, one stacked over the other.

    The geometry §10 recorded as unmeasured. Unlike the side-by-side builders,
    the separation here is not a routing choice — it is the prepreg between two
    signal layers, fixed by the stackup — so the only thing the layout controls
    is ``offset_mm``, the lateral distance between the two centrelines. Zero is
    perfectly stacked and worst case.

    That difference matters for how the result is used: side-by-side coupling
    is tightened by a clearance rule the designer sets, broadside coupling by
    moving one layer's routing sideways or putting it on a different layer.

    Each height is that trace's distance to the plane on its **own far side**,
    or ``None`` when that side is air — an outer layer. At least one must be a
    plane. Which of the two is physically uppermost is not modelled and does
    not need to be: flipping a 2D electrostatic section is a symmetry. Having
    air on your far side rather than copper is *not*, so that is what the
    layout decides here, and the section is built with the plane-backed trace
    at the bottom either way.

    **The victim is conductor 0**, matching the other coupled builders, so
    everything indexed off row 0 describes it whichever way up it sits.
    """
    if separation_mm <= 0:
        raise FieldSolverError("broadside separation must be positive")
    if victim_height_mm is None and aggressor_height_mm is None:
        raise FieldSolverError(
            "a broadside pair needs a reference plane on at least one side")

    # Put whichever trace has a plane behind it at the bottom. When both do,
    # the victim goes there, which keeps the common buried case oriented the
    # obvious way.
    victim_at_bottom = victim_height_mm is not None
    if victim_at_bottom:
        below_height, above_height = victim_height_mm, aggressor_height_mm
    else:
        below_height, above_height = aggressor_height_mm, victim_height_mm

    below = below_height * MM
    separation = separation_mm * MM
    thickness = thickness_mm * MM
    offset = offset_mm * MM

    lower_y0, lower_y1 = below, below + thickness
    upper_y0 = lower_y1 + separation
    upper_y1 = upper_y0 + thickness

    victim_half = victim_width_mm * MM / 2.0
    aggressor_half = aggressor_width_mm * MM / 2.0
    # The victim straddles the origin; the aggressor carries the offset.
    victim_left, victim_right = -victim_half, victim_half
    aggressor_left, aggressor_right = offset - aggressor_half, offset + aggressor_half

    reference_height = (min(below, above_height * MM)
                        if above_height is not None else below)
    margin = max(margin_ratio * reference_height,
                 4 * max(victim_width_mm, aggressor_width_mm) * MM,
                 2 * abs(offset) + 4 * separation)
    x_left = min(victim_left, aggressor_left) - margin
    x_right = max(victim_right, aggressor_right) + margin

    victim_y = (lower_y0, lower_y1) if victim_at_bottom else (upper_y0, upper_y1)
    aggressor_y = (upper_y0, upper_y1) if victim_at_bottom else (lower_y0, lower_y1)

    conductors = [
        Conductor(x_left, x_right, -thickness, 0.0, "ground_lower",
                  is_ground=True),
        Conductor(victim_left, victim_right, *victim_y, "victim"),
        Conductor(aggressor_left, aggressor_right, *aggressor_y, "aggressor"),
    ]

    if above_height is not None:
        total = upper_y1 + above_height * MM
        conductors.append(Conductor(x_left, x_right, total, total + thickness,
                                    "ground_upper", is_ground=True))
        slabs = [Slab(0.0, total, epsilon_r)]
        y_top = total + thickness
    else:
        # The upper trace is on an outer layer: substrate up to its underside,
        # then conformal mask over it and air above.
        slabs = [Slab(0.0, upper_y0, epsilon_r)] + _mask_slabs(
            upper_y0, thickness, mask)
        y_top = upper_y1 + margin

    feature = min(victim_width_mm, aggressor_width_mm, separation_mm,
                  below_height, thickness_mm,
                  above_height if above_height is not None else separation_mm,
                  mask.over_copper_mm if mask else separation_mm)
    return CrossSection(
        slabs=slabs,
        conductors=conductors,
        x_left=x_left, x_right=x_right, y_bottom=-thickness, y_top=y_top,
        feature_mm=feature,
        description=(f"broadside wv={victim_width_mm} wa={aggressor_width_mm} "
                     f"sep={separation_mm} offset={offset_mm} "
                     f"hv={victim_height_mm} ha={aggressor_height_mm} "
                     f"t={thickness_mm} er={epsilon_r}"
                     + (" +mask" if mask else "")))


def stripline_section(width_mm: float, height_below_mm: float,
                      height_above_mm: float, thickness_mm: float,
                      epsilon_r: float, gap_to_pour_mm: float | None = None,
                      margin_ratio: float = DEFAULT_MARGIN_RATIO
                      ) -> CrossSection:
    """A buried trace between two planes, symmetric or offset.

    The offset case is what `physics.stripline_impedance` approximates by
    paralleling two half-structures and flags as wanting a field solver.

    ``gap_to_pour_mm`` adds ground copper beside the trace on its own layer,
    which is the inner-layer equivalent of coplanar and is common wherever an
    inner signal layer is also poured.
    """
    width = width_mm * MM
    below, above = height_below_mm * MM, height_above_mm * MM
    thickness = thickness_mm * MM
    half = width / 2.0

    total = below + thickness + above
    margin = max(margin_ratio * min(below, above), 4 * width)
    if gap_to_pour_mm is not None:
        margin = max(margin, 3 * gap_to_pour_mm * MM)
    x_right = half + margin

    conductors = [
        Conductor(-x_right, x_right, -thickness, 0.0, "ground_lower",
                  is_ground=True),
        Conductor(-x_right, x_right, total, total + thickness, "ground_upper",
                  is_ground=True),
        Conductor(-half, half, below, below + thickness, "signal"),
    ]
    if gap_to_pour_mm is not None:
        gap = gap_to_pour_mm * MM
        conductors += [
            Conductor(-x_right, -(half + gap), below, below + thickness,
                      "pour_left", is_ground=True),
            Conductor(half + gap, x_right, below, below + thickness,
                      "pour_right", is_ground=True),
        ]

    return CrossSection(
        slabs=[Slab(0.0, total, epsilon_r)],
        conductors=conductors,
        x_left=-x_right, x_right=x_right,
        y_bottom=-thickness, y_top=total + thickness,
        feature_mm=min(width_mm, height_below_mm, height_above_mm, thickness_mm,
                       gap_to_pour_mm if gap_to_pour_mm else width_mm),
        description=(f"stripline w={width_mm} hb={height_below_mm} "
                     f"ha={height_above_mm} t={thickness_mm} er={epsilon_r}"
                     + (f" gap={gap_to_pour_mm}" if gap_to_pour_mm else "")))


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------

def _grade(a: float, b: float, h_min: float, growth: float,
           h_max: float) -> list[float]:
    """Interior nodes for [a, b], fine at both ends and coarse in the middle."""
    span = b - a
    if span <= 2 * h_min:
        return []
    middle = a + span / 2.0

    left, position, step = [], a, h_min
    while True:
        position += step
        if position >= middle:
            break
        left.append(position)
        step = min(step * growth, h_max)

    right, position, step = [], b, h_min
    while True:
        position -= step
        if position <= middle:
            break
        right.append(position)
        step = min(step * growth, h_max)

    return sorted(left + right)


def _axis(critical: list[float], h_min: float, growth: float,
          h_max: float) -> np.ndarray:
    """A graded axis through every critical coordinate.

    Pinning the critical values — conductor edges and dielectric interfaces —
    is what keeps a cell face from straddling a material change, and is why the
    permittivity handling can stay as simple as it is.
    """
    keep = sorted(set(round(value, 12) for value in critical))
    nodes = [keep[0]]
    for start, end in zip(keep, keep[1:]):
        nodes.extend(_grade(start, end, h_min, growth, h_max))
        nodes.append(end)
    return np.array(nodes)


@dataclass
class Mesh:
    x: np.ndarray
    y: np.ndarray

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.x), len(self.y)

    @property
    def nodes(self) -> int:
        return len(self.x) * len(self.y)


def build_mesh(section: CrossSection, refinement: float = 1.0,
               growth: float = DEFAULT_GROWTH) -> Mesh:
    """Graded mesh pinned to every feature edge.

    ``refinement`` scales the base cell size: 0.5 halves it, which is what the
    convergence study varies.
    """
    base = section.feature_mm * MM / 8.0 * refinement
    if base <= 0:
        raise FieldSolverError("cross-section has a non-positive feature size")

    xs = {section.x_left, section.x_right}
    ys = {section.y_bottom, section.y_top}
    for conductor in section.conductors:
        xs.update((conductor.x0, conductor.x1))
        ys.update((conductor.y0, conductor.y1))
    for slab in section.slabs:
        ys.update((slab.y0, slab.y1))

    xs = {value for value in xs if section.x_left <= value <= section.x_right}
    ys = {value for value in ys if section.y_bottom <= value <= section.y_top}

    width = section.x_right - section.x_left
    height = section.y_top - section.y_bottom
    return Mesh(
        x=_axis(sorted(xs), base, growth, width / 12.0),
        y=_axis(sorted(ys), base, growth, height / 12.0),
    )


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

def _assemble(section: CrossSection, mesh: Mesh, vacuum: bool) -> sp.csr_matrix:
    """Finite-volume operator over every node, no boundary conditions applied.

    Row i of ``A phi`` is the net electric flux out of node i's box, which is
    zero in free space and equal to the enclosed charge on a conductor. Leaving
    the Dirichlet rows in is what makes the charge extraction exact.
    """
    x, y = mesh.x, mesh.y
    nx, ny = len(x), len(y)

    dx = np.diff(x)
    dy = np.diff(y)
    # Cell extents: half a step either side, clipped at the domain edge.
    cell_x = np.empty(nx)
    cell_x[1:-1] = (x[2:] - x[:-2]) / 2.0
    cell_x[0] = dx[0] / 2.0
    cell_x[-1] = dx[-1] / 2.0
    cell_y = np.empty(ny)
    cell_y[1:-1] = (y[2:] - y[:-2]) / 2.0
    cell_y[0] = dy[0] / 2.0
    cell_y[-1] = dy[-1] / 2.0

    # Vertical faces sit between x[i] and x[i+1] and span one cell in y, so
    # they can cross a slab boundary: average eps over the span (parallel).
    if vacuum:
        eps_face_x = np.ones(ny)
        eps_between_y = np.ones(ny - 1)
    else:
        lower = np.empty(ny)
        upper = np.empty(ny)
        lower[1:] = (y[1:] + y[:-1]) / 2.0
        lower[0] = y[0]
        upper[:-1] = (y[1:] + y[:-1]) / 2.0
        upper[-1] = y[-1]
        eps_face_x = np.array([section.epsilon_over(lo, hi)
                               for lo, hi in zip(lower, upper)])
        # Horizontal faces lie within one slab, since interfaces are grid lines.
        eps_between_y = np.array([section.epsilon_at((a + b) / 2.0)
                                  for a, b in zip(y[:-1], y[1:])])

    index = np.arange(nx * ny).reshape(nx, ny)
    rows, cols, values = [], [], []

    def add(a_idx, b_idx, conductance):
        rows.extend((a_idx, b_idx, a_idx, b_idx))
        cols.extend((a_idx, b_idx, b_idx, a_idx))
        values.extend((conductance, conductance, -conductance, -conductance))

    # x-direction faces
    for i in range(nx - 1):
        conductance = VACUUM_PERMITTIVITY * eps_face_x * cell_y / dx[i]
        add(index[i, :], index[i + 1, :], conductance)

    # y-direction faces
    for j in range(ny - 1):
        conductance = (VACUUM_PERMITTIVITY * eps_between_y[j] * cell_x / dy[j])
        add(index[:, j], index[:, j + 1], conductance)

    rows = np.concatenate([np.atleast_1d(r) for r in rows])
    cols = np.concatenate([np.atleast_1d(c) for c in cols])
    values = np.concatenate([np.atleast_1d(v) for v in values])
    return sp.coo_matrix((values, (rows, cols)),
                         shape=(nx * ny, nx * ny)).tocsr()


def _conductor_nodes(section: CrossSection, mesh: Mesh) -> list[np.ndarray]:
    """Flat node indices belonging to each conductor."""
    grid_x, grid_y = np.meshgrid(mesh.x, mesh.y, indexing="ij")
    flat_x, flat_y = grid_x.ravel(), grid_y.ravel()
    tolerance = 1e-12

    groups = []
    for conductor in section.conductors:
        inside = ((flat_x >= conductor.x0 - tolerance)
                  & (flat_x <= conductor.x1 + tolerance)
                  & (flat_y >= conductor.y0 - tolerance)
                  & (flat_y <= conductor.y1 + tolerance))
        groups.append(np.flatnonzero(inside))
    return groups


def _capacitance_matrix(section: CrossSection, mesh: Mesh,
                        vacuum: bool) -> np.ndarray:
    operator = _assemble(section, mesh, vacuum)
    groups = _conductor_nodes(section, mesh)

    fixed = np.zeros(mesh.nodes, dtype=bool)
    for nodes in groups:
        fixed[nodes] = True
    free = np.flatnonzero(~fixed)
    if free.size == 0:
        raise FieldSolverError("conductors fill the domain; nothing to solve")

    signals = [i for i, c in enumerate(section.conductors) if not c.is_ground]
    if not signals:
        raise FieldSolverError("cross-section has no signal conductor")

    free_block = operator[free][:, free].tocsc()
    solve = spla.factorized(free_block)

    matrix = np.zeros((len(signals), len(signals)))
    for column, driven in enumerate(signals):
        potential = np.zeros(mesh.nodes)
        potential[groups[driven]] = 1.0
        # A_ff phi_f = -A_fd phi_d
        rhs = -(operator[free] @ potential)
        potential[free] = solve(rhs)

        charge = operator @ potential
        for row, conductor in enumerate(signals):
            matrix[row, column] = charge[groups[conductor]].sum()

    # The discrete operator is symmetric, so the matrix should be too; any
    # asymmetry is solver error, and averaging is the standard symmetrisation.
    return (matrix + matrix.T) / 2.0


@dataclass
class FieldSolution:
    """Capacitance, inductance and impedances for one cross-section."""
    section: CrossSection
    capacitance: np.ndarray                  # F/m, with dielectrics
    capacitance_air: np.ndarray              # F/m, vacuum
    mesh_nodes: int
    warnings: list[str] = field(default_factory=list)

    @property
    def inductance(self) -> np.ndarray:
        """L = mu_0 eps_0 C_air^-1 — Paul's generalised capacitance method."""
        return VACUUM_PERMEABILITY * VACUUM_PERMITTIVITY * np.linalg.inv(
            self.capacitance_air)

    @property
    def epsilon_eff(self) -> float:
        return float(self.capacitance[0, 0] / self.capacitance_air[0, 0])

    @staticmethod
    def _impedance(capacitance: float, capacitance_air: float) -> float:
        if capacitance <= 0 or capacitance_air <= 0:
            raise FieldSolverError("non-physical capacitance; check the mesh")
        return 1.0 / (SPEED_OF_LIGHT * math.sqrt(capacitance * capacitance_air))

    def z0_of(self, index: int) -> float:
        """Single-ended impedance of one conductor, its neighbours grounded.

        ``sqrt(L_ii / C_ii)``, taking the self-inductance from the *inverted*
        vacuum capacitance matrix rather than from its diagonal. For a section
        with one signal conductor the two are the same thing; with two they are
        not, because ``inv(C)[0,0]`` is not ``1 / C[0,0]``, and quietly using
        the reciprocal would put a percent or two of error into the crosstalk
        normalisation.
        """
        capacitance = self.capacitance[index, index]
        inductance = self.inductance[index, index]
        if capacitance <= 0 or inductance <= 0:
            raise FieldSolverError("non-physical L or C; check the mesh")
        return math.sqrt(inductance / capacitance)

    @property
    def z0_ohm(self) -> float:
        """Single-ended characteristic impedance of conductor 0."""
        return self.z0_of(0)

    @property
    def z_odd_ohm(self) -> float:
        """Odd-mode impedance. C_odd = C11 - C12, with C12 negative.

        Assumes the two conductors are identical, which is what makes the
        even/odd decomposition the modal one. `differential_section` builds
        exactly that; `coupled_microstrip_section` with unequal widths does
        not, and its modal impedances are approximate.
        """
        self._require_pair()
        odd = self.capacitance[0, 0] - self.capacitance[0, 1]
        odd_air = self.capacitance_air[0, 0] - self.capacitance_air[0, 1]
        return self._impedance(odd, odd_air)

    @property
    def z_even_ohm(self) -> float:
        self._require_pair()
        even = self.capacitance[0, 0] + self.capacitance[0, 1]
        even_air = self.capacitance_air[0, 0] + self.capacitance_air[0, 1]
        return self._impedance(even, even_air)

    @property
    def z_differential_ohm(self) -> float:
        return 2.0 * self.z_odd_ohm

    @property
    def z_common_ohm(self) -> float:
        return self.z_even_ohm / 2.0

    @property
    def coupling_coefficient(self) -> float:
        """|C12| / C11 — how strongly the two halves see each other."""
        self._require_pair()
        return abs(self.capacitance[0, 1]) / self.capacitance[0, 0]

    def _require_pair(self):
        if self.capacitance.shape[0] != 2:
            raise FieldSolverError(
                "mode impedances need exactly two signal conductors, got "
                f"{self.capacitance.shape[0]}")

    def propagation_delay_ps_per_mm(self) -> float:
        return 1e9 * math.sqrt(self.epsilon_eff) / SPEED_OF_LIGHT

    def summary(self) -> dict:
        result = {
            "section": self.section.description,
            "nodes": self.mesh_nodes,
            "epsilon_eff": round(self.epsilon_eff, 3),
            "c_pf_per_m": round(self.capacitance[0, 0] * 1e12, 3),
            "delay_ps_per_mm": round(self.propagation_delay_ps_per_mm(), 4),
            "warnings": self.warnings,
        }
        if self.capacitance.shape[0] == 2:
            result.update({
                "z_diff_ohm": round(self.z_differential_ohm, 2),
                "z_odd_ohm": round(self.z_odd_ohm, 2),
                "z_common_ohm": round(self.z_common_ohm, 2),
                "coupling": round(self.coupling_coefficient, 4),
            })
        else:
            result["z0_ohm"] = round(self.z0_ohm, 2)
        return result


def solution_from(section: CrossSection, capacitance: np.ndarray,
                  capacitance_air: np.ndarray,
                  mesh_nodes: int) -> FieldSolution:
    """A `FieldSolution` from matrices that are already known.

    Split out of `solve` so that `solvestore`, which puts the two matrices on
    disk and reads them back, cannot assemble a solution that differs from a
    solved one in any respect the solver decides — today that is the mesh
    warning, and a second copy of the threshold in another module is exactly
    the kind of drift a cached path must not have.
    """
    warnings: list[str] = []
    if mesh_nodes > 400_000:
        warnings.append(
            f"MESH: {mesh_nodes} nodes; consider a coarser refinement")
    return FieldSolution(section=section, capacitance=capacitance,
                         capacitance_air=capacitance_air,
                         mesh_nodes=mesh_nodes, warnings=warnings)


def solve(section: CrossSection, refinement: float = 1.0,
          growth: float = DEFAULT_GROWTH) -> FieldSolution:
    """Solve a cross-section for C, C_air and the impedances that follow."""
    mesh = build_mesh(section, refinement=refinement, growth=growth)
    capacitance = _capacitance_matrix(section, mesh, vacuum=False)
    capacitance_air = _capacitance_matrix(section, mesh, vacuum=True)
    return solution_from(section, capacitance, capacitance_air, mesh.nodes)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def microstrip_z0(width_mm: float, height_mm: float, thickness_mm: float,
                  epsilon_r: float, gap_to_pour_mm: float | None = None,
                  mask: SolderMask | None = None,
                  refinement: float = 1.0) -> FieldSolution:
    return solve(microstrip_section(width_mm, height_mm, thickness_mm,
                                    epsilon_r, gap_to_pour_mm, mask=mask),
                 refinement=refinement)


def differential_z(width_mm: float, spacing_mm: float, height_mm: float,
                   thickness_mm: float, epsilon_r: float,
                   gap_to_pour_mm: float | None = None,
                   mask: SolderMask | None = None,
                   refinement: float = 1.0) -> FieldSolution:
    return solve(differential_section(width_mm, spacing_mm, height_mm,
                                      thickness_mm, epsilon_r, gap_to_pour_mm,
                                      mask=mask),
                 refinement=refinement)


@dataclass
class TargetCheck2D:
    """A fab impedance target solved from its own stated geometry."""
    target_ohm: float
    modelled_ohm: float
    kind: str
    signal_layer: str
    reference_layer: str
    width_mm: float
    nodes: int
    note: str = ""

    @property
    def error(self) -> float:
        return (self.modelled_ohm - self.target_ohm) / self.target_ohm

    def __str__(self) -> str:
        return (f"{self.kind} {self.target_ohm:.0f}R on {self.signal_layer} "
                f"(ref {self.reference_layer}, w={self.width_mm}): "
                f"solver {self.modelled_ohm:.2f}R, {self.error:+.1%}")


def check_fab_targets(stackup, refinement: float = 0.7,
                      mask: SolderMask | None = JLC_SOLDER_MASK
                      ) -> list[TargetCheck2D]:
    """Reproduce the fab's impedance targets by solving its stated geometry.

    The strongest validation available: JLCPCB states a cross-section *and* the
    impedance it produces, so any disagreement is ours. Unlike
    `physics.check_fab_targets`, this covers the differential targets and the
    coplanar pours, neither of which a closed form handles here.

    Mask is on by default because the fab's own calculator includes it and
    these targets are all outer-layer. Pass ``mask=None`` for the bare-copper
    answer, which is what a closed form would give.
    """
    checks: list[TargetCheck2D] = []
    for target in stackup.impedance_targets:
        reference = target.bottom_ref or target.top_ref
        if reference is None:
            continue
        modelled, solution = solve_target(stackup, target,
                                          refinement=refinement, mask=mask)
        checks.append(TargetCheck2D(
            target_ohm=target.impedance_ohm, modelled_ohm=modelled,
            kind=target.kind, signal_layer=target.signal_layer,
            reference_layer=reference, width_mm=target.trace_width_mm,
            nodes=solution.mesh_nodes,
            note=("solder mask included" if mask
                  else "bare copper, no solder mask")))
    return checks


def solve_target(stackup, target, refinement: float = 0.7,
                 mask: SolderMask | None = JLC_SOLDER_MASK):
    """Solve one impedance target's stated cross-section. `(ohm, solution)`.

    Split out of `check_fab_targets` so a *perturbed* copy of a target can be
    solved without going through the whole stackup — which is what
    `stackup_cli.diagnose` needs to say which field is misread rather than only
    that one of them is.
    """
    reference = target.bottom_ref or target.top_ref
    if reference is None:
        raise ValueError("target names no reference layer")
    height, epsilon = stackup.dielectric_between(target.signal_layer, reference)
    copper = next((l for l in stackup.copper_layers
                   if l.name == target.signal_layer), None)
    thickness = copper.thickness_mm if copper else 0.035

    if target.kind == "single_ended":
        section = microstrip_section(
            target.trace_width_mm, height, thickness, epsilon,
            gap_to_pour_mm=target.trace_to_copper_mm, mask=mask)
        solution = solve(section, refinement=refinement)
        return solution.z0_ohm, solution

    if target.trace_spacing_mm is None:
        raise ValueError("a differential target needs trace_spacing_mm")
    section = differential_section(
        target.trace_width_mm, target.trace_spacing_mm, height,
        thickness, epsilon, gap_to_pour_mm=target.trace_to_copper_mm,
        mask=mask)
    solution = solve(section, refinement=refinement)
    return solution.z_differential_ohm, solution


def convergence(section: CrossSection,
                refinements: tuple[float, ...] = (2.0, 1.0, 0.5),
                quantity: str = "z0_ohm") -> list[tuple[int, float]]:
    """The same section at several mesh densities.

    A field solver that is not shown to converge is a plausible-looking number
    generator, so this is exposed rather than kept in the tests.
    """
    results = []
    for refinement in refinements:
        solution = solve(section, refinement=refinement)
        results.append((solution.mesh_nodes, float(getattr(solution, quantity))))
    return results
