"""Closed-form transmission-line and DC physics.

Everything here is gated on a validated stackup: without real dielectric
heights these functions would return confident nonsense, which is the whole
reason Gate 1 exists.

Two deliberate choices about what *not* to write:

* **Microstrip comes from scikit-rf**, not from a reimplementation. It carries
  Hammerstad-Jensen with Kirschning-Jansen dispersion, a causal
  Djordjevic-Svensson dielectric model, skin effect and roughness — all
  mature and widely validated. Rewriting that would add risk, not accuracy.
* **Coplanar structures are not solved here.** JLCPCB quotes its impedance
  targets as "coplanar single-ended" / "coplanar differential", meaning a
  ground pour beside the trace. The standard closed-form CPWG models break
  down exactly where these boards sit: with a 0.508 mm gap over a 0.21 mm
  dielectric the side pour is nearly irrelevant, yet scikit-rf's CPW model
  still reports ~56 ohm against microstrip's 52 ohm, and it never converges
  back to the microstrip answer as the gap widens. So microstrip is used, its
  ~3% optimism against the fab's coplanar number is stated rather than hidden,
  and the exact treatment waits for the Phase 4 field solver.

What *is* done about coplanar: :func:`coplanar_clearance` measures the actual
gap from each trace to neighbouring same-layer copper, so a trace designed to a
0.508 mm assumption but routed at 0.2 mm can be flagged even before the field
solver exists.
"""

from __future__ import annotations

import math
import warnings as warnings_module
from dataclasses import dataclass, field

from shapely.ops import unary_union
from shapely.strtree import STRtree

from pcbreview.model import Board, Track
from pcbreview.stackup import Stackup

# Copper at 20 C.
COPPER_RESISTIVITY_OHM_M = 1.72e-8
COPPER_TEMPCO_PER_C = 0.00393
COPPER_THICKNESS_PER_OZ_MM = 0.0348

SPEED_OF_LIGHT_MM_PER_S = 299_792_458_000.0
VACUUM_PERMEABILITY = 4e-7 * math.pi

# IPC-2152 conductor temperature rise, as Brooks fits its still-air external
# charts ("Trace Currents and Temperatures Revisited", Eq. 3-2, the same
# equation for the 2 oz and the 3 oz data): dT = C * I^2 * W^-a * t^-b, with
# W and t in mils. `current_capacity_a` inverts it.
IPC2152_C = 215.3
IPC2152_WIDTH_EXPONENT = 1.15
IPC2152_THICKNESS_EXPONENT = 1.0
MM_PER_MIL = 0.0254

# IPC-2152's charts are for boards larger than 3 x 3 in; on a smaller board it
# recommends its conservative chart instead. Read as board area.
IPC2152_MIN_BOARD_AREA_MM2 = (3 * 25.4) ** 2

# IPC-2221 conductor current capacity: I = k * dT^0.44 * A^0.725, A in mil^2.
# The historical chart, kept for comparison and for IPC-2152's conservative
# chart, which is IPC-2221's internal curve (`ipc2221_current_a`).
IPC2221_K_EXTERNAL = 0.048
IPC2221_K_INTERNAL = 0.024
MM2_TO_MIL2 = 1550.0031

# The fab's coplanar model runs a few percent below a plain microstrip model
# for the geometries these boards use. Stated so callers can widen tolerances
# rather than chase a difference that is a known model limitation.
COPLANAR_MODEL_BIAS = 0.03

# A trace behaves as a transmission line once it is an appreciable fraction of
# the signal's rise-time length. Below this it is a lumped connection and
# impedance does not matter.
TRANSMISSION_LINE_FRACTION = 1 / 6


def skin_depth_mm(frequency_hz: float,
                  resistivity_ohm_m: float = COPPER_RESISTIVITY_OHM_M) -> float:
    """Depth at which current density falls to 1/e. sqrt(rho / (pi f mu0))."""
    if frequency_hz <= 0:
        return float("inf")
    return 1e3 * math.sqrt(
        resistivity_ohm_m / (math.pi * frequency_hz * VACUUM_PERMEABILITY))


class PhysicsError(Exception):
    """Raised when a calculation is asked for outside its validity."""


@dataclass
class ImpedanceResult:
    """Characteristic impedance with its model and assumptions attached.

    The assumptions are part of the answer. A bare number invites the reader to
    trust it more than the model deserves.
    """
    z0_ohm: float
    epsilon_eff: float
    model: str
    width_mm: float
    height_mm: float
    epsilon_r: float
    frequency_hz: float
    warnings: list[str] = field(default_factory=list)

    @property
    def propagation_delay_ps_per_mm(self) -> float:
        return 1e12 * math.sqrt(self.epsilon_eff) / SPEED_OF_LIGHT_MM_PER_S

    @property
    def is_trustworthy(self) -> bool:
        return not any(w.startswith("OUT OF RANGE") for w in self.warnings)

    def summary(self) -> dict:
        return {
            "z0_ohm": round(self.z0_ohm, 2),
            "epsilon_eff": round(self.epsilon_eff, 3),
            "model": self.model,
            "delay_ps_per_mm": round(self.propagation_delay_ps_per_mm, 4),
            "warnings": self.warnings,
        }


def microstrip_impedance(width_mm: float, height_mm: float, thickness_mm: float,
                         epsilon_r: float, frequency_hz: float = 1e9,
                         loss_tangent: float = 0.02,
                         roughness_mm: float = 0.00015) -> ImpedanceResult:
    """Surface-layer trace over a plane, via scikit-rf's Hammerstad-Jensen.

    Frequency matters: ``epsilon_eff`` rises with it, so a 1.575 GHz GPS trace
    and a 1 MHz SPI trace on identical copper do not have identical impedance.
    """
    from skrf import Frequency
    from skrf.media import MLine

    if min(width_mm, height_mm, thickness_mm) <= 0:
        raise PhysicsError("microstrip geometry must be positive")

    warnings: list[str] = []
    ratio = width_mm / height_mm
    if not 0.05 <= ratio <= 20:
        warnings.append(
            f"OUT OF RANGE: w/h = {ratio:.2f} is outside Hammerstad-Jensen's "
            f"validated 0.05-20 band")
    if thickness_mm > height_mm / 2:
        warnings.append(
            f"OUT OF RANGE: copper {thickness_mm} mm is thick relative to the "
            f"{height_mm} mm dielectric; the thickness correction is strained")

    # Below ~3 skin depths the conductor-loss model is optimistic. Z0 and
    # epsilon_eff remain sound, so this constrains loss results only.
    depth_mm = skin_depth_mm(frequency_hz)
    if thickness_mm < 3 * depth_mm:
        warnings.append(
            f"LOSS ONLY: copper {thickness_mm:.4f} mm is under 3 skin depths "
            f"({3 * depth_mm:.4f} mm at {frequency_hz / 1e6:.3g} MHz); "
            f"conductor loss is understated, Z0 is unaffected")

    frequency = Frequency(frequency_hz / 1e9, frequency_hz / 1e9, 1, "ghz")
    with warnings_module.catch_warnings():
        # scikit-rf raises the same skin-depth caveat we have just recorded.
        warnings_module.simplefilter("ignore", RuntimeWarning)
        line = MLine(frequency=frequency, w=width_mm * 1e-3, h=height_mm * 1e-3,
                     t=thickness_mm * 1e-3, ep_r=epsilon_r, tand=loss_tangent,
                     rho=COPPER_RESISTIVITY_OHM_M, rough=roughness_mm * 1e-3)

    return ImpedanceResult(
        z0_ohm=float(line.z0_characteristic[0].real),
        epsilon_eff=float(line.ep_reff[0].real),
        model="microstrip (Hammerstad-Jensen, scikit-rf)",
        width_mm=width_mm, height_mm=height_mm, epsilon_r=epsilon_r,
        frequency_hz=frequency_hz, warnings=warnings)


def stripline_impedance(width_mm: float, height_above_mm: float,
                        height_below_mm: float, thickness_mm: float,
                        epsilon_r: float) -> ImpedanceResult:
    """Inner-layer trace between two planes (IPC-2141 / Wadell).

    The symmetric form is a well-validated closed form. Offset striplines are
    handled as two half-structures in parallel, which is a documented
    approximation and degrades as the offset grows — flagged in ``warnings``
    rather than silently returned.
    """
    if min(width_mm, height_above_mm, height_below_mm) <= 0:
        raise PhysicsError("stripline geometry must be positive")

    warnings: list[str] = []
    separation = height_above_mm + height_below_mm + thickness_mm
    asymmetry = (abs(height_above_mm - height_below_mm)
                 / max(height_above_mm, height_below_mm))

    def symmetric(total_b: float) -> float:
        # IPC-2141A: Z0 = 60/sqrt(er) * ln(4b / (0.67*pi*(0.8w + t)))
        denominator = 0.67 * math.pi * (0.8 * width_mm + thickness_mm)
        if denominator <= 0:
            raise PhysicsError("degenerate stripline geometry")
        return (60.0 / math.sqrt(epsilon_r)) * math.log(4 * total_b / denominator)

    if asymmetry < 0.1:
        z0 = symmetric(separation)
        model = "stripline, symmetric (IPC-2141)"
    else:
        # Each plane sees a half-structure; combining in parallel is the
        # standard first-order treatment for an offset stripline.
        upper = symmetric(2 * height_above_mm + thickness_mm)
        lower = symmetric(2 * height_below_mm + thickness_mm)
        z0 = 2.0 * upper * lower / (upper + lower)
        model = "stripline, offset (parallel half-structures)"
        warnings.append(
            f"APPROXIMATION: planes are {asymmetry:.0%} asymmetric; the offset "
            f"stripline form is first-order and wants a field solver")

    if width_mm / (separation - thickness_mm) > 0.35:
        warnings.append(
            "OUT OF RANGE: w/(b-t) > 0.35 exceeds the IPC-2141 stripline fit")
    if thickness_mm / separation > 0.25:
        warnings.append("OUT OF RANGE: t/b > 0.25 exceeds the IPC-2141 fit")

    return ImpedanceResult(
        z0_ohm=z0,
        # A stripline is fully embedded, so the effective permittivity is
        # simply the dielectric's.
        epsilon_eff=epsilon_r,
        model=model, width_mm=width_mm,
        height_mm=min(height_above_mm, height_below_mm),
        epsilon_r=epsilon_r, frequency_hz=0.0, warnings=warnings)


# -- fab-target comparison ------------------------------------------------

@dataclass
class TargetCheck:
    """A fab impedance target measured against our own model."""
    target_ohm: float
    modelled_ohm: float
    kind: str
    signal_layer: str
    reference_layer: str
    width_mm: float
    note: str = ""

    @property
    def error(self) -> float:
        return (self.modelled_ohm - self.target_ohm) / self.target_ohm

    @property
    def within_expected_bias(self) -> bool:
        """Our microstrip model should read slightly high against a coplanar target."""
        return -0.01 <= self.error <= COPLANAR_MODEL_BIAS + 0.03

    def __str__(self) -> str:
        return (f"{self.kind} {self.target_ohm:.0f}R on {self.signal_layer} "
                f"(ref {self.reference_layer}, w={self.width_mm}): "
                f"model {self.modelled_ohm:.2f}R, {self.error:+.1%}")


def check_fab_targets(stackup: Stackup, frequency_hz: float = 1e9
                      ) -> list[TargetCheck]:
    """Reproduce the fab's own impedance targets with our model.

    This is the strongest validation available without a field solver: the fab
    states a geometry and the impedance it produces, so any disagreement is
    ours. Single-ended targets only — differential needs coupled-line
    treatment, which is Phase 4.
    """
    checks: list[TargetCheck] = []
    for target in stackup.impedance_targets:
        if target.kind != "single_ended":
            continue
        reference = target.bottom_ref or target.top_ref
        if reference is None:
            continue

        height, epsilon = stackup.dielectric_between(target.signal_layer, reference)
        copper = next((l for l in stackup.copper_layers
                       if l.name == target.signal_layer), None)
        thickness = copper.thickness_mm if copper else 0.035

        result = microstrip_impedance(
            target.trace_width_mm, height, thickness, epsilon,
            frequency_hz=frequency_hz)

        note = ""
        if target.is_coplanar:
            note = (f"fab target is coplanar with {target.trace_to_copper_mm} mm "
                    f"to pour; modelled as plain microstrip, expected ~"
                    f"{COPLANAR_MODEL_BIAS:.0%} high")

        checks.append(TargetCheck(
            target_ohm=target.impedance_ohm, modelled_ohm=result.z0_ohm,
            kind=target.kind, signal_layer=target.signal_layer,
            reference_layer=reference, width_mm=target.trace_width_mm, note=note))
    return checks


# -- measured coplanar clearance ------------------------------------------

# -- DC behaviour ---------------------------------------------------------

def copper_thickness_mm(weight_oz: float) -> float:
    return weight_oz * COPPER_THICKNESS_PER_OZ_MM


def trace_resistance_ohm(length_mm: float, width_mm: float,
                         thickness_mm: float, temperature_c: float = 20.0
                         ) -> float:
    """DC resistance of a rectangular conductor, temperature corrected."""
    if min(length_mm, width_mm, thickness_mm) <= 0:
        raise PhysicsError("resistance geometry must be positive")
    area_m2 = (width_mm * 1e-3) * (thickness_mm * 1e-3)
    resistance = COPPER_RESISTIVITY_OHM_M * (length_mm * 1e-3) / area_m2
    return resistance * (1 + COPPER_TEMPCO_PER_C * (temperature_c - 20.0))


def voltage_drop_v(current_a: float, length_mm: float, width_mm: float,
                   thickness_mm: float, temperature_c: float = 20.0) -> float:
    return current_a * trace_resistance_ohm(
        length_mm, width_mm, thickness_mm, temperature_c)


def ipc2152_rise_c(current_a: float, width_mm: float,
                   thickness_mm: float) -> float:
    """IPC-2152 still-air temperature rise of a conductor, in C.

    Brooks's fit to IPC-2152's external charts, `dT = 215.3 I^2 W^-1.15
    t^-1.0` with W and t in mils — one equation for both the 2 oz and the
    3 oz data, and the one his independent 3D thermal model reproduces.
    """
    if current_a < 0 or min(width_mm, thickness_mm) <= 0:
        raise PhysicsError("rise inputs must be positive")
    width_mil = width_mm / MM_PER_MIL
    thickness_mil = thickness_mm / MM_PER_MIL
    return (IPC2152_C * current_a ** 2 * width_mil ** -IPC2152_WIDTH_EXPONENT
            * thickness_mil ** -IPC2152_THICKNESS_EXPONENT)


def current_capacity_a(width_mm: float, thickness_mm: float,
                       temperature_rise_c: float = 10.0,
                       internal: bool = False) -> float:
    """IPC-2152 current capacity: the current that raises a conductor by
    `temperature_rise_c` in still air. Every capacity in this package —
    tracks, necks, via barrels — is this function.

    **Why IPC-2152 and not IPC-2221.** IPC-2152 (2009) is the measured
    successor to IPC-2221's chart: traces in boards of known construction,
    with no copper planes, in still air and vacuum. IPC-2221's chart is
    1950s data whose test conditions are not recorded. They disagree, and
    not in one direction:

    * **outer conductors** — IPC-2152 allows ~25% less current than IPC-2221
      for a wide trace and somewhat more for a narrow one (0.83 A against
      0.74 A at 0.2 mm, 1 oz, +10 C; 2.08 A against 2.38 A at 1 mm). IPC-2152
      dropped IPC-2221's external chart because it was only safe on boards
      over 3 x 3 in that also have planes. An earlier version of this
      docstring said IPC-2221 was the conservative of the two; for a bare
      outer trace that was wrong;
    * **inner conductors** — IPC-2221 halves the external capacity. IPC-2152
      measured inner traces running as cool as outer ones or cooler, because
      laminate conducts heat better than still air; IPC-2221's internal curve
      turned out to describe a conductor in free air.

    **One chart for both.** IPC-2152 recommends its universal chart for inner
    and outer conductors alike, so `internal` does not change the answer. Its
    own data has inner traces cooler, so this errs safe for them.

    **What is not applied**, all of it said rather than silently assumed:
    IPC-2152's plane modifier, which only adds capacity (a plane under a
    trace can halve its rise); its board-thickness modifier, which makes a
    1.6 mm board a few percent hotter than its 1.78 mm baseline; and its
    material modifier, ~2% for FR-4 against polyimide. On a board smaller
    than 3 x 3 in IPC-2152 recommends its conservative chart instead
    (`ipc2221_current_a(..., internal=True)`); the audit says so as an
    assumption when the board is that small (`IPC2152_MIN_BOARD_AREA_MM2`).
    """
    if min(width_mm, thickness_mm) <= 0 or temperature_rise_c <= 0:
        raise PhysicsError("current capacity inputs must be positive")
    width_mil = width_mm / MM_PER_MIL
    thickness_mil = thickness_mm / MM_PER_MIL
    return math.sqrt(temperature_rise_c * width_mil ** IPC2152_WIDTH_EXPONENT
                     * thickness_mil ** IPC2152_THICKNESS_EXPONENT / IPC2152_C)


def required_width_mm(current_a: float, thickness_mm: float,
                      temperature_rise_c: float = 10.0,
                      internal: bool = False) -> float:
    """Invert IPC-2152 for the width a given current needs."""
    if current_a <= 0 or thickness_mm <= 0 or temperature_rise_c <= 0:
        raise PhysicsError("width inputs must be positive")
    thickness_mil = thickness_mm / MM_PER_MIL
    width_mil = (current_a ** 2 * IPC2152_C / temperature_rise_c
                 / thickness_mil ** IPC2152_THICKNESS_EXPONENT
                 ) ** (1 / IPC2152_WIDTH_EXPONENT)
    return width_mil * MM_PER_MIL


def ipc2221_current_a(width_mm: float, thickness_mm: float,
                      temperature_rise_c: float = 10.0,
                      internal: bool = False) -> float:
    """IPC-2221's historical chart: I = k * dT^0.44 * A^0.725, A in mil^2.

    Kept for comparison, and because its internal curve is IPC-2152's
    conservative chart — the one IPC-2152 recommends on boards under 3 x 3 in
    and in any doubt, since it approximates a conductor in free air.
    """
    if min(width_mm, thickness_mm) <= 0 or temperature_rise_c <= 0:
        raise PhysicsError("current capacity inputs must be positive")
    area_mil2 = width_mm * thickness_mm * MM2_TO_MIL2
    k = IPC2221_K_INTERNAL if internal else IPC2221_K_EXTERNAL
    return k * (temperature_rise_c ** 0.44) * (area_mil2 ** 0.725)


# -- timing ---------------------------------------------------------------

def propagation_delay_ps_per_mm(epsilon_eff: float) -> float:
    if epsilon_eff <= 0:
        raise PhysicsError("epsilon_eff must be positive")
    return 1e12 * math.sqrt(epsilon_eff) / SPEED_OF_LIGHT_MM_PER_S


def knee_frequency_hz(rise_time_ps: float) -> float:
    """Highest frequency with significant energy in a digital edge (0.5 / t_r)."""
    if rise_time_ps <= 0:
        raise PhysicsError("rise time must be positive")
    return 0.5 / (rise_time_ps * 1e-12)


def critical_length_mm(rise_time_ps: float, epsilon_eff: float = 4.0,
                       fraction: float = TRANSMISSION_LINE_FRACTION) -> float:
    """Trace length beyond which a signal must be treated as a transmission line."""
    delay = propagation_delay_ps_per_mm(epsilon_eff)
    return fraction * rise_time_ps / delay


def is_transmission_line(length_mm: float, rise_time_ps: float,
                         epsilon_eff: float = 4.0) -> bool:
    """Whether impedance matters at all for this net.

    The gate that keeps the audit honest: a 40 mm I2C trace at 400 kHz has an
    edge rate measured in hundreds of nanoseconds and is a lumped wire. Running
    impedance checks on it produces noise, not findings.
    """
    return length_mm > critical_length_mm(rise_time_ps, epsilon_eff)


def skew_ps(length_a_mm: float, length_b_mm: float, epsilon_eff: float) -> float:
    """Propagation-delay skew from a physical length mismatch."""
    return abs(length_a_mm - length_b_mm) * propagation_delay_ps_per_mm(epsilon_eff)
