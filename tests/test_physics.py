"""Tests for closed-form physics.

The strongest check available before a field solver exists is the fab's own
impedance targets: JLCPCB states a geometry and the impedance it yields, so any
disagreement is ours. Both fabricated boards carry two targets each.

Everything else is checked against textbook relationships and analytic limits
rather than against the implementation's own output.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.physics import (
    COPLANAR_MODEL_BIAS,
    PhysicsError,
    check_fab_targets,
    critical_length_mm,
    current_capacity_a,
    is_transmission_line,
    knee_frequency_hz,
    microstrip_impedance,
    propagation_delay_ps_per_mm,
    required_width_mm,
    skin_depth_mm,
    skew_ps,
    stripline_impedance,
    trace_resistance_ohm,
    voltage_drop_v,
)
from pcbreview.stackup import load_stackup

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# -- against the fab's own numbers ----------------------------------------

@pytest.mark.parametrize("slug", ["four-layer-1", "six-layer-1"])
def test_model_reproduces_jlc_impedance_targets(slug):
    """Microstrip should read slightly high against JLC's coplanar target.

    The side pour JLC assumes pulls Z0 down a few percent, so a plain
    microstrip model is expected to be optimistic — but only by a few percent.
    A larger error, or an error in the other direction, means the geometry or
    the dielectric is wrong.
    """
    stackup = load_stackup(FIXTURES / f"{slug}.stackup.yaml")
    checks = check_fab_targets(stackup)
    assert checks, "fixture carries no single-ended targets"
    for check in checks:
        assert check.error > -0.01, f"{check} — model below a coplanar target"
        assert check.error < COPLANAR_MODEL_BIAS + 0.03, str(check)


def test_fab_target_check_records_the_coplanar_caveat():
    stackup = load_stackup(FIXTURES / "four-layer-1.stackup.yaml")
    check = check_fab_targets(stackup)[0]
    assert "coplanar" in check.note
    assert str(check.target_ohm).startswith("50")


# -- microstrip behaviour -------------------------------------------------

def test_wider_traces_have_lower_impedance():
    z = [microstrip_impedance(w, 0.2104, 0.035, 4.6).z0_ohm
         for w in (0.15, 0.25, 0.35, 0.5)]
    assert z == sorted(z, reverse=True)


def test_thicker_dielectric_raises_impedance():
    thin = microstrip_impedance(0.3, 0.1, 0.035, 4.6).z0_ohm
    thick = microstrip_impedance(0.3, 0.3, 0.035, 4.6).z0_ohm
    assert thick > thin


def test_higher_permittivity_lowers_impedance_and_slows_propagation():
    low = microstrip_impedance(0.3424, 0.2104, 0.035, 3.5)
    high = microstrip_impedance(0.3424, 0.2104, 0.035, 4.8)
    assert high.z0_ohm < low.z0_ohm
    assert high.epsilon_eff > low.epsilon_eff
    assert high.propagation_delay_ps_per_mm > low.propagation_delay_ps_per_mm


def test_effective_permittivity_lies_between_air_and_substrate():
    """A microstrip's field is partly in air, so 1 < eeff < er, always."""
    for epsilon_r in (3.0, 4.4, 5.5):
        result = microstrip_impedance(0.3, 0.2, 0.035, epsilon_r)
        assert 1.0 < result.epsilon_eff < epsilon_r


def test_dispersion_raises_impedance_with_frequency():
    """Kirschning-Jansen: eeff falls toward the air value as frequency rises."""
    low = microstrip_impedance(0.3424, 0.2104, 0.035, 4.6, frequency_hz=1e6)
    high = microstrip_impedance(0.3424, 0.2104, 0.035, 4.6, frequency_hz=1.575e9)
    assert high.z0_ohm > low.z0_ohm
    assert high.epsilon_eff < low.epsilon_eff


def test_low_frequency_flags_the_skin_depth_limit_without_voiding_z0():
    result = microstrip_impedance(0.3424, 0.2104, 0.035, 4.6, frequency_hz=1e6)
    assert any(w.startswith("LOSS ONLY") for w in result.warnings)
    assert result.is_trustworthy          # Z0 is still usable


def test_extreme_aspect_ratio_is_flagged_out_of_range():
    result = microstrip_impedance(5.0, 0.05, 0.035, 4.6)
    assert not result.is_trustworthy
    assert any(w.startswith("OUT OF RANGE") for w in result.warnings)


def test_non_positive_geometry_raises():
    with pytest.raises(PhysicsError):
        microstrip_impedance(0.0, 0.2, 0.035, 4.6)


# -- stripline ------------------------------------------------------------

def test_symmetric_stripline_is_slower_than_microstrip():
    """Fully embedded copper sees the whole dielectric, so eeff == er."""
    result = stripline_impedance(0.2, 0.2, 0.2, 0.035, 4.4)
    assert result.epsilon_eff == pytest.approx(4.4)
    assert result.propagation_delay_ps_per_mm > propagation_delay_ps_per_mm(3.3)


def test_stripline_impedance_falls_with_width():
    z = [stripline_impedance(w, 0.2, 0.2, 0.035, 4.4).z0_ohm
         for w in (0.1, 0.15, 0.2)]
    assert z == sorted(z, reverse=True)


def test_offset_stripline_is_flagged_as_an_approximation():
    result = stripline_impedance(0.15, 0.11, 0.55, 0.035, 4.4)
    assert "offset" in result.model
    assert any(w.startswith("APPROXIMATION") for w in result.warnings)


def test_offset_stripline_sits_below_the_symmetric_case():
    """Moving a trace nearer one plane lowers its impedance."""
    symmetric = stripline_impedance(0.15, 0.33, 0.33, 0.035, 4.4).z0_ohm
    offset = stripline_impedance(0.15, 0.11, 0.55, 0.035, 4.4).z0_ohm
    assert offset < symmetric


# -- DC -------------------------------------------------------------------

def test_resistance_matches_rho_l_over_a():
    """1 m of 1 mm x 0.0348 mm copper: R = rho*L/A."""
    expected = 1.72e-8 * 1.0 / (1e-3 * 3.48e-5)
    assert trace_resistance_ohm(1000.0, 1.0, 0.0348) == pytest.approx(expected, rel=1e-9)


def test_resistance_rises_with_temperature():
    cold = trace_resistance_ohm(100, 0.5, 0.035, temperature_c=20)
    hot = trace_resistance_ohm(100, 0.5, 0.035, temperature_c=85)
    assert hot / cold == pytest.approx(1 + 0.00393 * 65, rel=1e-6)


def test_voltage_drop_is_ohms_law():
    resistance = trace_resistance_ohm(50, 0.5, 0.035)
    assert voltage_drop_v(2.0, 50, 0.5, 0.035) == pytest.approx(2.0 * resistance)


def test_capacity_is_ipc2152_as_brooks_fits_its_charts():
    """Brooks, "Trace Currents and Temperatures Revisited", Eq. 3-2: one fit
    for IPC-2152's 2 oz and 3 oz still-air external charts. At 16 A a 100 mil
    2 oz trace rises ~100 C — and the capacity at that rise is 16 A again."""
    from pcbreview.physics import ipc2152_rise_c
    two_oz = 2 * 0.0348
    rise = ipc2152_rise_c(16.0, 2.54, two_oz)
    assert rise == pytest.approx(215.3 * 16 ** 2 * 100 ** -1.15
                                 / (two_oz / 0.0254), rel=1e-9)
    assert 95 < rise < 105
    assert current_capacity_a(2.54, two_oz, rise) == pytest.approx(16.0, rel=1e-9)


def test_ipc2152_is_one_chart_for_inner_and_outer_conductors():
    """IPC-2152 recommends its universal chart for both, and measured inner
    traces running as cool as outer ones or cooler — IPC-2221's halving of
    inner capacity turned out to describe a conductor in free air."""
    assert (current_capacity_a(1.0, 0.0348, 10, internal=True)
            == current_capacity_a(1.0, 0.0348, 10, internal=False))


def test_ipc2152_scales_as_the_square_root_of_the_rise():
    low = current_capacity_a(1.0, 0.0348, 10.0)
    high = current_capacity_a(1.0, 0.0348, 20.0)
    assert high / low == pytest.approx(2 ** 0.5, rel=1e-9)


def test_ipc2152_is_stricter_than_ipc2221_on_a_wide_outer_trace_only():
    """The two standards disagree, and not in one direction. An earlier
    docstring called IPC-2221 the conservative one; for a wide bare outer trace
    IPC-2152 allows ~25% less, and for a narrow one somewhat more."""
    from pcbreview.physics import ipc2221_current_a
    wide_2152, wide_2221 = (current_capacity_a(3.0, 0.0348),
                            ipc2221_current_a(3.0, 0.0348))
    assert wide_2152 < 0.8 * wide_2221
    assert current_capacity_a(0.2, 0.0348) > ipc2221_current_a(0.2, 0.0348)


def test_ipc2221_is_kept_as_it_was_for_comparison():
    """k = 0.024 internal against 0.048 external, dT^0.44, and 1 mm of 1 oz
    outer copper at roughly 2.3-2.5 A for a 10 C rise."""
    from pcbreview.physics import ipc2221_current_a
    external = ipc2221_current_a(1.0, 0.0348, 10, internal=False)
    assert ipc2221_current_a(1.0, 0.0348, 10, internal=True) / external \
        == pytest.approx(0.5, rel=1e-9)
    assert 2.2 < external < 2.6
    assert ipc2221_current_a(1.0, 0.0348, 20.0) / external \
        == pytest.approx(2 ** 0.44, rel=1e-9)


def test_required_width_inverts_current_capacity():
    width = required_width_mm(3.0, 0.0348, 10.0)
    assert current_capacity_a(width, 0.0348, 10.0) == pytest.approx(3.0, rel=1e-6)


def test_current_capacity_rejects_bad_inputs():
    with pytest.raises(PhysicsError):
        current_capacity_a(0.0, 0.0348, 10.0)
    with pytest.raises(PhysicsError):
        current_capacity_a(1.0, 0.0348, 0.0)


# -- timing and the transmission-line gate --------------------------------

def test_propagation_delay_in_vacuum_is_the_speed_of_light():
    assert propagation_delay_ps_per_mm(1.0) == pytest.approx(3.3356, rel=1e-4)


def test_fr4_propagation_delay_is_about_6_ps_per_mm():
    assert 5.5 < propagation_delay_ps_per_mm(3.3) < 6.5


def test_knee_frequency_is_half_the_inverse_rise_time():
    assert knee_frequency_hz(1000) == pytest.approx(500e6)


def test_skin_depth_falls_as_root_frequency():
    assert skin_depth_mm(1e6) / skin_depth_mm(4e6) == pytest.approx(2.0, rel=1e-9)
    assert skin_depth_mm(1e6) * 1000 == pytest.approx(66.0, rel=0.02)


def test_slow_signals_are_lumped_however_long_the_trace():
    """A 400 kHz I2C net is not a transmission line at any board-scale length."""
    assert not is_transmission_line(length_mm=40, rise_time_ps=500_000)
    assert critical_length_mm(500_000) > 1000


def test_fast_signals_become_transmission_lines_quickly():
    """A 300 ps edge is distributed after less than a centimetre."""
    assert is_transmission_line(length_mm=30, rise_time_ps=300)
    assert critical_length_mm(300) < 10


def test_the_gate_separates_this_project_s_actual_signals():
    """SPI at 1 MHz stays lumped; the 1.575 GHz GPS feed does not."""
    assert not is_transmission_line(40, 50_000, 3.2)     # 1 MHz SPI
    assert is_transmission_line(30, 300, 3.2)            # GPS


def test_skew_converts_length_to_delay():
    assert skew_ps(10.0, 11.0, 3.3) == pytest.approx(
        propagation_delay_ps_per_mm(3.3), rel=1e-9)


# -- measured clearance ---------------------------------------------------
