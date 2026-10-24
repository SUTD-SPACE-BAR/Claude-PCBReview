"""Tests for the 2D electrostatic field solver.

A field solver is easy to write and hard to trust: it will always return a
plausible number. So the tests are layered, and only the last layer looks at
the fab's data.

1. **Analytic limits** the solver must reproduce exactly — parallel plate,
   and a stripline's effective permittivity, which is the dielectric's by
   definition and so is a pure test of the discretisation.
2. **Structural invariants** — matrix symmetry, sign of the mutual term,
   ordering of the modal impedances, and `sqrt(L/C)` agreeing with the
   impedance taken from the capacitances, which exercises the
   `L = mu_0 eps_0 C_air^-1` route independently.
3. **Numerical convergence** — under mesh refinement and under moving the
   outer boundary, since either could silently set the answer.
4. **Independent models** — IPC-2141 stripline and Hammerstad-Jensen
   microstrip, in the regimes where those are valid.
5. **The fab's own targets**, last, because they are the only data that could
   be fitted rather than predicted.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from pcbreview import fields
from pcbreview.fields import (JLC_SOLDER_MASK, SPEED_OF_LIGHT,
                              VACUUM_PERMITTIVITY, SolderMask,
                              differential_section, microstrip_section,
                              solve, stripline_section)
from pcbreview.physics import microstrip_impedance, stripline_impedance


@pytest.fixture(scope="module")
def fab_microstrip():
    """A 50 ohm outer-layer geometry from a real fab's calculator, bare
    copper. Reused by several tests, and the numbers are the fab's own for a
    four-layer 1.6 mm stackup — the point being that they were not chosen to
    make the solver look good."""
    return solve(microstrip_section(0.3424, 0.2104, 0.035, 4.6))


# -- 1. analytic limits ---------------------------------------------------

def test_a_wide_trace_reproduces_the_parallel_plate_capacitance():
    """C -> eps_0 eps_r W / h as fringing becomes negligible.

    The one case with a closed-form answer that owes nothing to any model.
    """
    width, height, epsilon_r = 5.0, 0.1, 4.0
    solution = solve(microstrip_section(width, height, 0.01, epsilon_r),
                     refinement=1.5)
    ideal = VACUUM_PERMITTIVITY * epsilon_r * (width / height)
    assert solution.capacitance[0, 0] == pytest.approx(ideal, rel=0.05)


def test_a_vacuum_solve_has_unit_effective_permittivity():
    solution = solve(microstrip_section(1.0, 0.1, 0.01, 1.0), refinement=1.5)
    assert solution.epsilon_eff == pytest.approx(1.0, abs=1e-9)


def test_a_stripline_is_fully_embedded_so_eps_eff_is_the_dielectrics():
    """No air anywhere in the section, so eps_eff must be exactly eps_r.

    Any deviation is discretisation error and nothing else, which makes this
    the sharpest available test of the assembly.
    """
    solution = solve(stripline_section(0.15, 0.55, 0.55, 0.0152, 4.5),
                     refinement=1.0)
    assert solution.epsilon_eff == pytest.approx(4.5, rel=1e-6)


# -- 2. structural invariants ---------------------------------------------

def test_the_capacitance_matrix_is_symmetric_with_negative_mutual_terms():
    solution = solve(differential_section(0.2, 0.2, 0.1, 0.035, 4.4),
                     refinement=1.5)
    matrix = solution.capacitance
    assert matrix[0, 0] > 0 and matrix[1, 1] > 0
    assert matrix[0, 1] < 0, "Maxwell mutual capacitance must be negative"
    assert matrix[0, 1] == pytest.approx(matrix[1, 0], rel=1e-12)
    assert matrix[0, 0] == pytest.approx(matrix[1, 1], rel=1e-3), "symmetric pair"


def test_inductance_from_the_vacuum_capacitance_reproduces_z0():
    """Checks the L = mu_0 eps_0 C_air^-1 route, not just the C route.

    `z0_ohm` is computed from the two capacitances directly. Going the long way
    round — invert C_air for L, then sqrt(L/C) — must agree, or the inductance
    that crosstalk will later depend on is wrong.
    """
    solution = solve(microstrip_section(0.3, 0.2, 0.035, 4.4), refinement=1.5)
    inductance = solution.inductance[0, 0]
    long_way = math.sqrt(inductance / solution.capacitance[0, 0])
    assert long_way == pytest.approx(solution.z0_ohm, rel=1e-9)


def test_modal_impedances_are_ordered_and_the_pair_relations_hold():
    solution = solve(differential_section(0.2, 0.2, 0.2, 0.035, 4.4),
                     refinement=1.5)
    assert solution.z_odd_ohm < solution.z_even_ohm
    assert solution.z_differential_ohm == pytest.approx(2 * solution.z_odd_ohm)
    assert solution.z_common_ohm == pytest.approx(solution.z_even_ohm / 2)


def test_tighter_spacing_couples_the_pair_more_strongly():
    loose = solve(differential_section(0.2, 0.6, 0.2, 0.035, 4.4), refinement=1.5)
    tight = solve(differential_section(0.2, 0.1, 0.2, 0.035, 4.4), refinement=1.5)
    assert tight.coupling_coefficient > loose.coupling_coefficient
    assert tight.z_differential_ohm < loose.z_differential_ohm


def test_impedance_moves_the_right_way_with_geometry():
    base = solve(microstrip_section(0.3, 0.2, 0.035, 4.4), refinement=1.5)
    wider = solve(microstrip_section(0.6, 0.2, 0.035, 4.4), refinement=1.5)
    thicker_dielectric = solve(microstrip_section(0.3, 0.4, 0.035, 4.4),
                               refinement=1.5)
    denser = solve(microstrip_section(0.3, 0.2, 0.035, 8.0), refinement=1.5)

    assert wider.z0_ohm < base.z0_ohm
    assert thicker_dielectric.z0_ohm > base.z0_ohm
    assert denser.z0_ohm < base.z0_ohm
    assert 1.0 < base.epsilon_eff < 4.4, "microstrip is part air, part dielectric"


# -- coupled sections for unequal conductors ------------------------------

def test_an_equal_width_coupled_section_is_the_differential_section():
    """The general builder must not be a second, subtly different model.

    A crosstalk victim and its aggressor are two unrelated nets and generally
    have different widths, which `differential_section` cannot express. Where
    the widths do coincide the two must describe the same copper, so the answers
    have to agree to solver precision rather than merely closely.
    """
    pair = solve(differential_section(0.2, 0.2, 0.1, 0.035, 4.3,
                                      gap_to_pour_mm=0.3), refinement=1.5)
    coupled = solve(fields.coupled_microstrip_section(0.2, 0.2, 0.2, 0.1, 0.035,
                                                      4.3, gap_to_pour_mm=0.3),
                    refinement=1.5)
    assert coupled.mesh_nodes == pair.mesh_nodes
    assert coupled.z_differential_ohm == pytest.approx(pair.z_differential_ohm,
                                                       rel=1e-12)


def test_the_victim_is_conductor_zero_in_a_coupled_section():
    """Everything indexed off row 0 has to describe the victim, or the
    crosstalk normalisation is silently against the wrong line."""
    section = fields.coupled_microstrip_section(0.15, 0.60, 0.2, 0.1, 0.035, 4.3)
    assert [c.name for c in section.signals] == ["victim", "aggressor"]
    solution = solve(section, refinement=1.5)
    narrow = solve(fields.microstrip_section(0.15, 0.1, 0.035, 4.3),
                   refinement=1.5)
    # The victim is the narrow one, so row 0 must read as the high-impedance
    # line rather than the wide aggressor's.
    assert solution.z0_of(0) > solution.z0_of(1)
    assert solution.z0_of(0) == pytest.approx(narrow.z0_ohm, rel=0.1)


def test_a_coupled_stripline_is_homogeneous_so_the_two_couplings_cancel():
    """The sharpest check available on the mutual terms.

    A buried pair sits in one dielectric, so the electric and magnetic coupling
    are the same structure seen twice and `Lm/Ls` must equal `Cm/Cs` *exactly*.
    Any difference is discretisation error and nothing else — the same argument
    as the stripline effective-permittivity test, applied to the off-diagonals
    that crosstalk depends on.
    """
    solution = solve(fields.coupled_stripline_section(
        0.2, 0.2, 0.2, 0.2, 0.2, 0.035, 4.3), refinement=1.0)
    capacitive = -solution.capacitance[0, 1] / solution.capacitance[0, 0]
    inductive = solution.inductance[0, 1] / solution.inductance[0, 0]
    assert capacitive == pytest.approx(inductive, rel=1e-9)


def test_a_microstrip_couples_more_magnetically_than_capacitively():
    """The inhomogeneous counterpart, and the reason microstrip has forward
    crosstalk at all: the field above the traces runs through air, which weakens
    the electric coupling without touching the magnetic."""
    solution = solve(fields.coupled_microstrip_section(
        0.2, 0.2, 0.2, 0.1, 0.035, 4.3), refinement=1.5)
    capacitive = -solution.capacitance[0, 1] / solution.capacitance[0, 0]
    inductive = solution.inductance[0, 1] / solution.inductance[0, 0]
    assert inductive > capacitive


def test_z0_takes_the_self_inductance_from_the_inverted_matrix():
    """`inv(C)[0,0]` is not `1 / C[0,0]` once a second conductor is present.

    With one signal conductor the two agree and the fab validation cannot tell
    them apart; with two they differ by a couple of percent, which would land
    straight in the crosstalk normalisation.
    """
    solution = solve(fields.coupled_microstrip_section(
        0.2, 0.2, 0.15, 0.1, 0.035, 4.3), refinement=1.5)
    reciprocal = 1.0 / (SPEED_OF_LIGHT * math.sqrt(
        solution.capacitance[0, 0] * solution.capacitance_air[0, 0]))
    assert solution.z0_ohm != pytest.approx(reciprocal, rel=1e-3)
    assert solution.z0_ohm == pytest.approx(
        math.sqrt(solution.inductance[0, 0] / solution.capacitance[0, 0]),
        rel=1e-12)


# -- 3. numerical convergence ---------------------------------------------

def test_the_answer_converges_under_mesh_refinement():
    section = microstrip_section(0.3424, 0.2104, 0.035, 4.6)
    results = fields.convergence(section, refinements=(1.0, 0.5, 0.35))
    impedances = [z for _, z in results]
    assert results[0][0] < results[-1][0], "refinement must add nodes"
    spread = (max(impedances) - min(impedances)) / min(impedances)
    assert spread < 0.005, f"not converged: {results}"


def test_the_answer_does_not_depend_on_where_the_outer_boundary_sits():
    """Natural boundaries only work if they are far enough away."""
    impedances = [
        solve(microstrip_section(0.3424, 0.2104, 0.035, 4.6, margin_ratio=ratio),
              refinement=1.0).z0_ohm
        for ratio in (10.0, 20.0, 40.0)
    ]
    spread = (max(impedances) - min(impedances)) / min(impedances)
    assert spread < 0.01, impedances


# -- 4. against independent models ----------------------------------------

def test_microstrip_agrees_with_hammerstad_jensen(fab_microstrip):
    closed_form = microstrip_impedance(0.3424, 0.2104, 0.035, 4.6)
    assert fab_microstrip.z0_ohm == pytest.approx(
        closed_form.z0_ohm, rel=0.01)
    assert fab_microstrip.epsilon_eff == pytest.approx(
        closed_form.epsilon_eff, rel=0.02)


@pytest.mark.parametrize("width,below,above,epsilon_r", [
    (0.15, 0.55, 0.55, 4.5),
    (0.20, 0.50, 0.50, 4.4),
    (0.10, 0.30, 0.30, 4.3),
])
def test_symmetric_stripline_agrees_with_ipc_2141(width, below, above, epsilon_r):
    solution = solve(stripline_section(width, below, above, 0.0152, epsilon_r),
                     refinement=0.7)
    closed_form = stripline_impedance(width, above, below, 0.0152, epsilon_r)
    assert solution.z0_ohm == pytest.approx(closed_form.z0_ohm, rel=0.02)


# -- the differentiator ---------------------------------------------------

def test_coplanar_returns_to_microstrip_as_the_gap_widens(fab_microstrip):
    """The failure that justified building this.

    scikit-rf's CPW model reports 55.06 ohm at a 0.508 mm gap and *60.38 at
    5 mm* — 24x the dielectric height, where the pour cannot physically matter
    and the answer must be the microstrip one. Using it would report the user's
    GPS traces as badly mismatched when they are not.
    """
    plain = fab_microstrip.z0_ohm
    far = solve(microstrip_section(0.3424, 0.2104, 0.035, 4.6,
                                   gap_to_pour_mm=5.0), refinement=1.0).z0_ohm
    assert far == pytest.approx(plain, rel=0.01), (
        f"coplanar at a 5 mm gap gave {far}, microstrip gives {plain}")


def test_a_pour_close_to_the_trace_pulls_the_impedance_down_monotonically():
    impedances = [
        solve(microstrip_section(0.3424, 0.2104, 0.035, 4.6, gap_to_pour_mm=gap),
              refinement=1.0).z0_ohm
        for gap in (0.1, 0.2032, 0.508, 1.0, 2.0)
    ]
    assert impedances == sorted(impedances), impedances
    assert impedances[0] < 0.9 * impedances[-1], "a tight pour must matter"


def test_mask_free_solutions_are_used_for_closed_form_comparisons():
    """The default must not quietly add mask a closed form does not model."""
    section = microstrip_section(0.3424, 0.2104, 0.035, 4.6)
    assert all(slab.epsilon_r in (4.6,) for slab in section.slabs)
    masked = microstrip_section(0.3424, 0.2104, 0.035, 4.6, mask=JLC_SOLDER_MASK)
    assert any(slab.epsilon_r == JLC_SOLDER_MASK.epsilon_r
               for slab in masked.slabs)


# -- reporting ------------------------------------------------------------

def test_the_summary_reports_modal_results_for_a_pair_and_z0_for_one_trace():
    single = solve(microstrip_section(0.3, 0.2, 0.035, 4.4), refinement=2.0)
    pair = solve(differential_section(0.2, 0.2, 0.2, 0.035, 4.4), refinement=2.0)
    assert "z0_ohm" in single.summary() and "z_diff_ohm" not in single.summary()
    assert "z_diff_ohm" in pair.summary() and "z0_ohm" not in pair.summary()
    with pytest.raises(fields.FieldSolverError):
        single.z_odd_ohm
