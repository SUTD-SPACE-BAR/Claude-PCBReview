"""Tests for crosstalk from the field solver's mutual C and L.

Crosstalk is the easiest thing in this project to get plausibly wrong: every
formula in the literature is quoted with a different factor, sometimes a factor
of two, and any of them returns a believable percentage. So the coefficients are
pinned by identities that hold *exactly* rather than by comparison with a rule
of thumb:

1. **A homogeneous medium has no forward crosstalk.** A stripline's electric and
   magnetic coupling are the same structure seen twice, so ``Cm/Cs`` and
   ``Lm/Ls`` must cancel to solver precision. Nothing is fitted; the answer is
   zero or the extraction is wrong.
2. **Twice the backward coefficient is the coupled-line coupling factor**
   ``(Ze - Zo)/(Ze + Zo)``. That reaches the same number from sums and
   differences of the matrices rather than from ratios, and settles the
   factor-of-two question against a quantity with an independent definition.
3. **Mutual capacitance approaches the parallel-plate value** as two tall
   conductors close on each other — an analytic limit on the mutual term itself,
   in the same spirit as the parallel-plate test in `test_fields.py`.

Then the two *behaviours* that distinguish backward from forward crosstalk, and
finally the two fabricated boards.

A note on what is deliberately not tested: agreement with Johnson & Graham's
``1/(1 + (D/H)^2)`` rule of thumb. It was checked, and it tracks this solver to
within 25% in the middle of its stated band — but it has no width term at all,
and two geometries with the same D/H and different w/h disagree with it by a
factor of 1.6 in opposite directions. It is evidence about the rule of thumb,
not about the solver, and asserting on it would be fitting to a fit.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from pcbreview import crosstalk, fields, routed
from pcbreview.crosstalk import Coefficients, Crosstalk, CrosstalkSection
from pcbreview.intent import infer
from pcbreview.model import Board
from pcbreview.schematic import netlist
from pcbreview.stackup import load_and_validate


# -- 1. identities the coefficients must satisfy --------------------------

@pytest.mark.parametrize("spacing", [0.1, 0.2, 0.4])
def test_a_homogeneous_medium_has_no_forward_crosstalk(spacing):
    """The sharpest check there is, and the one that would catch a swapped
    sign or a wrong normalisation instantly."""
    solution = fields.solve(fields.coupled_stripline_section(
        0.2, 0.2, spacing, 0.2, 0.2, 0.035, 4.6), refinement=1.0)
    result = crosstalk.coefficients(solution)
    assert abs(result.k_forward) < 1e-9 * result.k_backward
    assert result.capacitive_ratio == pytest.approx(result.inductive_ratio,
                                                    rel=1e-9)


@pytest.mark.parametrize("spacing", [0.1, 0.2, 0.4])
def test_twice_the_backward_coefficient_is_the_coupled_line_coupling_factor(
        spacing):
    """Settles the factor of two, against a quantity defined a different way.

    ``k_b`` comes from ratios of the capacitance matrix and the inverse of the
    vacuum one. ``(Ze - Zo)/(Ze + Zo)`` comes from sums and differences of the
    same matrices through the modal impedances. In a homogeneous medium the two
    are algebraically identical and must agree to solver precision; a
    coefficient off by two would fail here and nowhere else.
    """
    solution = fields.solve(fields.coupled_stripline_section(
        0.2, 0.2, spacing, 0.2, 0.2, 0.035, 4.6), refinement=1.0)
    result = crosstalk.coefficients(solution)
    identity = ((solution.z_even_ohm - solution.z_odd_ohm)
                / (solution.z_even_ohm + solution.z_odd_ohm))
    assert 2 * result.k_backward == pytest.approx(identity, rel=1e-6)


def test_the_same_identity_holds_approximately_for_an_inhomogeneous_section():
    """Microstrip's two modes travel at different speeds, so the identity is no
    longer exact — but it must stay close, or the C and L matrices are not
    describing the same structure."""
    solution = fields.solve(fields.coupled_microstrip_section(
        0.2, 0.2, 0.2, 0.2104, 0.035, 4.6), refinement=1.0)
    result = crosstalk.coefficients(solution)
    identity = ((solution.z_even_ohm - solution.z_odd_ohm)
                / (solution.z_even_ohm + solution.z_odd_ohm))
    assert 2 * result.k_backward == pytest.approx(identity, rel=0.02)


@pytest.mark.parametrize("thickness,spacing", [(0.5, 0.02), (0.5, 0.01),
                                               (1.0, 0.02)])
def test_mutual_capacitance_approaches_the_parallel_plate_limit(thickness,
                                                                spacing):
    """An analytic limit on the mutual term itself.

    Two tall conductors nearly touching, with the reference plane pushed far
    away, form a parallel-plate capacitor of height ``t`` and separation ``s``
    in air. The solver must land just above ``eps0 t / s`` — above, because the
    fringing field at the top and bottom of the gap adds to it.
    """
    solution = fields.solve(fields.coupled_microstrip_section(
        0.2, 0.2, spacing, 4.0, thickness, 1.0), refinement=0.7)
    result = crosstalk.coefficients(solution)
    ideal = fields.VACUUM_PERMITTIVITY * (thickness * 1e-3) / (spacing * 1e-3)
    ratio = result.c_mutual_pf_per_m / (ideal * 1e12)
    assert 1.0 < ratio < 1.15


def test_coupling_vanishes_as_the_traces_separate_and_saturates_as_they_close():
    """Both physical bounds, on the same structure."""
    far = crosstalk.coefficients(fields.solve(fields.coupled_microstrip_section(
        0.2, 0.2, 20.0, 0.1, 0.035, 4.0), refinement=1.0))
    near = crosstalk.coefficients(fields.solve(fields.coupled_microstrip_section(
        0.2, 0.2, 0.005, 20.0, 0.035, 4.0), refinement=1.0))
    assert far.k_backward < 1e-4
    assert 0.4 < near.k_backward <= 0.5, "two conductors in contact couple fully"


def test_forward_crosstalk_on_an_outer_layer_opposes_the_aggressor_edge():
    """Microstrip's fringing runs through air, which weakens the electric
    coupling but not the magnetic — so FEXT comes out negative, which is the
    textbook result and the reason the sign is carried rather than discarded."""
    result = crosstalk.coefficients(fields.solve(
        fields.coupled_microstrip_section(0.2, 0.2, 0.2, 0.2104, 0.035, 4.6),
        refinement=1.0))
    assert result.k_forward < 0
    assert result.is_inductively_dominated


def test_the_coefficients_converge_under_mesh_refinement():
    """A field solver that is not shown to converge is a number generator.

    This also justifies `DEFAULT_REFINEMENT` being coarser than `routed.py`'s:
    the coefficients are ratios taken from one solve, so the mesh bias largely
    cancels and a survey can afford hundreds of them.
    """
    section = lambda: fields.coupled_microstrip_section(
        0.2, 0.15, 0.2, 0.2104, 0.035, 4.6, gap_to_pour_mm=0.3,
        mask=fields.JLC_SOLDER_MASK)
    values = [crosstalk.coefficients(fields.solve(section(), refinement=r))
              for r in (4.0, crosstalk.DEFAULT_REFINEMENT, 1.0, 0.6)]
    backward = [v.k_backward for v in values]
    assert (max(backward) - min(backward)) / min(backward) < 0.02
    assert values[1].k_backward == pytest.approx(values[-1].k_backward, rel=0.01)


def test_a_section_with_one_conductor_is_refused_rather_than_guessed():
    solution = fields.solve(fields.microstrip_section(0.2, 0.2, 0.035, 4.4),
                            refinement=2.0)
    with pytest.raises(crosstalk.CrosstalkError):
        crosstalk.coefficients(solution)


# -- 2. backward saturates, forward accumulates ---------------------------

def _synthetic(length_mm: float, k_backward: float = 0.08,
               k_forward: float = -0.03, delay_ps_per_mm: float = 6.0,
               step_mm: float = 0.025) -> Crosstalk:
    """A uniformly coupled run, so length is the only variable."""
    coefficients = Coefficients(
        k_backward=k_backward, k_forward=k_forward,
        capacitive_ratio=0.05, inductive_ratio=0.11,
        z0_victim_ohm=50.0, z0_aggressor_ohm=50.0, epsilon_eff=3.2,
        delay_ps_per_mm=delay_ps_per_mm, c_mutual_pf_per_m=5.0,
        l_mutual_nh_per_m=50.0)
    section = CrosstalkSection(
        layer="F.Cu", length_mm=length_mm, gap_mm=0.2, victim_width_mm=0.2,
        aggressor_width_mm=0.2, clearance_mm=0.5, reference_layer="In1.Cu",
        height_mm=0.21, coefficients=coefficients)
    samples = int(round(length_mm / step_mm))
    return Crosstalk(victim="V", aggressor="A", sections=[section],
                     victim_length_mm=length_mm,
                     profiles=[(step_mm, np.full(samples, k_backward))])


def test_a_longer_coupled_run_does_not_couple_backward_any_harder():
    """The property that makes `next_saturated` a bound rather than a total.

    The near-end waveform is a scan of ``k_b`` along the line, so a longer run
    produces the same peak for longer. Treating NEXT as an accumulation — the
    obvious mistake — would make a 40 mm run look ten times worse than a 4 mm
    one at the same gap, which it is not.
    """
    short, long = _synthetic(4.0), _synthetic(40.0)
    assert short.next_saturated == pytest.approx(long.next_saturated)
    # 20 ps of edge is 1.7 mm of line, so both runs are past saturation and the
    # ten-to-one difference in length buys the aggressor nothing.
    fast = 20.0
    assert short.at_rise_time(fast).saturated and long.at_rise_time(fast).saturated
    assert short.at_rise_time(fast).next_fraction == pytest.approx(
        long.at_rise_time(fast).next_fraction, rel=1e-6)
    # ... and it does accumulate forward, in proportion.
    assert long.fext_ps == pytest.approx(10 * short.fext_ps)


def test_backward_crosstalk_grows_with_length_only_until_it_saturates():
    slow = 4000.0                       # ps: window is far longer than the run
    short, long = _synthetic(4.0), _synthetic(40.0)
    assert long.at_rise_time(slow).next_fraction > (
        5 * short.at_rise_time(slow).next_fraction)
    assert not short.at_rise_time(slow).saturated
    assert short.at_rise_time(20.0).saturated


def test_a_slower_edge_couples_less():
    run = _synthetic(10.0)
    fractions = [run.at_rise_time(t).next_fraction
                 for t in (100.0, 500.0, 2000.0, 10000.0)]
    assert fractions == sorted(fractions, reverse=True)
    assert fractions[0] == pytest.approx(run.next_saturated, rel=1e-6)


def test_forward_crosstalk_is_the_integrated_coefficient_over_the_rise_time():
    run = _synthetic(10.0, k_forward=-0.03, delay_ps_per_mm=6.0)
    assert run.fext_ps == pytest.approx(-0.03 * 10.0 * 6.0)
    assert run.at_rise_time(1800.0).fext_fraction == pytest.approx(
        run.fext_ps / 1800.0)


def test_the_saturation_length_is_half_the_rise_time_in_line_length():
    run = _synthetic(10.0, delay_ps_per_mm=6.0)
    assert run.saturation_length_mm(1200.0) == pytest.approx(100.0)


# -- 4. broadside: two traces stacked on adjacent layers -------------------
#
# Held to the same identities as the side-by-side sections, because they are
# the checks that would catch the mistake this builder is most likely to make:
# putting the two conductors at the wrong heights, or normalising the coupling
# against the wrong conductor's self terms. Both would still return a
# believable percentage.

# the six-layer board's In2/In3, the one adjacent signal-layer pair on either board.
BROADSIDE = dict(separation_mm=0.1088, victim_height_mm=0.55,
                 aggressor_height_mm=0.55, thickness_mm=0.0152, epsilon_r=4.35)


@pytest.mark.parametrize("offset", [0.0, 0.2, 0.5])
def test_a_stacked_pair_between_two_planes_has_no_forward_crosstalk(offset):
    """Buried on both sides, so the medium is homogeneous whichever way the
    field goes round — the same identity that pins `coupled_stripline_section`,
    applied to a geometry it cannot express."""
    section = fields.broadside_section(0.275, 0.275, offset_mm=offset, **BROADSIDE)
    result = crosstalk.coefficients(fields.solve(section, refinement=2.0))
    assert abs(result.k_forward) < 1e-9
    # The two mechanisms cancel, so neither dominates — the ratios are equal.
    assert result.capacitive_ratio == pytest.approx(result.inductive_ratio,
                                                    rel=1e-6)


@pytest.mark.parametrize("offset", [0.0, 0.2, 0.5])
def test_twice_the_backward_coefficient_is_the_coupler_factor_broadside(offset):
    """The independent definition, reached through the modal impedances rather
    than through ratios of the C and L matrices."""
    section = fields.broadside_section(0.275, 0.275, offset_mm=offset, **BROADSIDE)
    solution = fields.solve(section, refinement=2.0)
    coupler = ((solution.z_even_ohm - solution.z_odd_ohm)
               / (solution.z_even_ohm + solution.z_odd_ohm))
    assert 2 * crosstalk.coefficients(solution).k_backward == pytest.approx(
        coupler, rel=1e-6)


@pytest.mark.parametrize("victim_w,aggressor_w", [(0.275, 0.8), (0.15, 0.5)])
def test_broadside_coupling_is_reciprocal_between_unequal_widths(victim_w,
                                                                 aggressor_w):
    """Swapping which trace is the victim must not move the coupling.

    Reciprocity is what forces the geometric-mean normalisation in
    `coefficients`; a normalisation against the victim's self terms alone
    would fail this by the ratio of the two widths.
    """
    forward = crosstalk.coefficients(fields.solve(fields.broadside_section(
        victim_w, aggressor_w, offset_mm=0.2, **BROADSIDE), refinement=2.0))
    reverse = crosstalk.coefficients(fields.solve(fields.broadside_section(
        aggressor_w, victim_w, offset_mm=0.2, **BROADSIDE), refinement=2.0))
    assert forward.k_backward == pytest.approx(reverse.k_backward, rel=1e-9)


def test_a_stacked_pair_couples_harder_than_the_design_rule_side_by_side():
    """The measurement that justifies building this at all.

    On this stackup two traces stacked on adjacent signal layers 0.109 mm apart
    couple about twice as hard as two traces on one layer at the 0.2 mm
    clearance the board is routed to. The §10 note that broadside was "largely
    moot on these stackups" assumed a plane between every pair of signal
    layers, which In2/In3 do not have.
    """
    stacked = crosstalk.coefficients(fields.solve(fields.broadside_section(
        0.2, 0.2, offset_mm=0.0, **BROADSIDE), refinement=2.0))
    side_by_side = crosstalk.coefficients(fields.solve(
        fields.coupled_stripline_section(0.2, 0.2, 0.2, 0.55,
                                         0.55 + 0.1088 + 0.0152, 0.0152, 4.35),
        refinement=2.0))
    assert stacked.k_backward > 1.8 * side_by_side.k_backward


def test_broadside_coupling_decays_with_lateral_offset():
    """Offset is the only thing the layout controls here, so the check is that
    it controls something.

    The window `BROADSIDE_OFFSET_RATIO` sets is 8x the separation, and this
    pins what that buys: coupling falls monotonically, is down to a fifth of
    the stacked peak by the edge of the window, and is negligible well before
    30x. It is emphatically *not* zero at the window edge — the cutoff is
    chosen to match what the side-by-side window already tolerates, not
    because the physics has stopped.
    """
    separation = BROADSIDE["separation_mm"]
    offsets = [0.0, 1.0, 2.0, 4.0, 8.0, 30.0]
    values = [crosstalk.coefficients(fields.solve(fields.broadside_section(
        0.2, 0.2, offset_mm=ratio * separation, **BROADSIDE),
        refinement=1.5)).k_backward for ratio in offsets]
    assert values == sorted(values, reverse=True)
    assert values[-2] < 0.2 * values[0]        # at the window edge
    assert values[-1] < 0.01 * values[0]       # far outside it


def test_a_far_offset_stacked_trace_returns_to_its_own_stripline_impedance():
    """The limit that says the section is built at the right heights: with the
    aggressor pushed aside, the victim must be exactly the stripline it would
    be if the other layer were empty."""
    alone = fields.solve(fields.stripline_section(
        0.275, 0.55, 0.1088 + 0.0152 + 0.55, 0.0152, 4.35), refinement=2.0)
    pair = fields.solve(fields.broadside_section(
        0.275, 0.275, offset_mm=6.4, **BROADSIDE), refinement=2.0)
    assert pair.z0_of(0) == pytest.approx(alone.z0_ohm, rel=2e-3)


def test_a_broadside_pair_with_no_plane_on_either_side_is_refused():
    """Two signal layers with air behind both is not a transmission line, and
    guessing a reference would invent the answer."""
    with pytest.raises(fields.FieldSolverError):
        fields.broadside_section(0.2, 0.2, 0.1, 0.0, None, None, 0.0152, 4.35)


def test_the_broadside_coefficients_converge_under_mesh_refinement():
    values = [crosstalk.coefficients(fields.solve(fields.broadside_section(
        0.275, 0.275, offset_mm=0.0, **BROADSIDE), refinement=r)).k_backward
        for r in (1.0, 2.0, 3.3)]
    assert (max(values) - min(values)) / np.mean(values) < 0.01
