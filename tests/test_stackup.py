"""Tests for Gate 1 (stackup).

The load-bearing test here is `test_board_file_stackup_is_rejected`: the
placeholder stackup KiCad writes by default must be refused, because silently
computing impedance from it is the failure mode this gate exists to prevent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.stackup import (
    ImpedanceTarget,
    Stackup,
    StackupGateError,
    StackupLayer,
    compare_to_board,
    from_kicad_file,
    load_and_validate,
    load_stackup,
    parse_jlc_table,
    validate,
)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

FOUR_LAYER = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
SIX_LAYER = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


# -- JLC table parsing ----------------------------------------------------

def test_parses_jlc_four_layer_table():
    stackup = parse_jlc_table("""
        JLC04161H-7628(Standard/Finished thickness1.59mm±10%)
        L1	Outer Copper Weight1oz	1.38	0.0350
        Prepreg	7628, RC 49%, 8.6 mil	8.28	0.2104
        L2	Inner Copper Weight	0.60	0.0152
        Core	1.1mm H/HOZ with copper	41.93	1.0650
        L3	Inner Copper Weight	0.60	0.0152
        Prepreg	7628, RC 49%, 8.6 mil	8.28	0.2104
        L4	Outer Copper Weight1oz	1.38	0.0350
    """)
    assert stackup.fab_code == "JLC04161H-7628"
    assert stackup.finished_thickness_mm == 1.59
    assert len(stackup.copper_layers) == 4
    assert stackup.total_thickness_mm == pytest.approx(1.5862, abs=1e-4)
    assert stackup.layer_map["L1"] == "F.Cu"
    assert stackup.layer_map["L4"] == "B.Cu"


def test_copper_weight_inferred_from_material():
    stackup = parse_jlc_table("""
        L1	Outer Copper Weight1oz	1.38	0.0350
        Prepreg	7628, RC 49%, 8.6 mil	8.28	0.2104
        L2	Inner Copper Weight	0.60	0.0152
    """)
    assert stackup.copper_layers[0].copper_weight_oz == 1.0
    assert stackup.copper_layers[1].copper_weight_oz == 0.5


def test_epsilon_r_from_glass_style():
    stackup = parse_jlc_table("""
        L1	Outer Copper Weight1oz	1.38	0.0350
        Prepreg	3313 RC57% 4.2mil	3.91	0.0994
        L2	Inner Copper Weight	0.60	0.0152
    """)
    prepreg = stackup.layers[1]
    assert prepreg.epsilon_r == 4.25          # 3313
    assert prepreg.epsilon_r_source == "glass_style_default"


def test_unparseable_table_raises():
    with pytest.raises(StackupGateError, match="no stackup rows"):
        parse_jlc_table("this is not a stackup")


# -- derived geometry -----------------------------------------------------

def test_dielectric_between_adjacent_coppers():
    stackup = load_stackup(FIXTURES / "four-layer-1.stackup.yaml")
    height, epsilon = stackup.dielectric_between("L1", "L2")
    assert height == pytest.approx(0.2104)
    assert epsilon == pytest.approx(4.6)


def test_dielectric_between_spanning_layers_averages_in_series():
    """L1->L3 crosses prepreg (0.2104 @ 4.6) plus core (1.065 @ 4.5)."""
    stackup = load_stackup(FIXTURES / "four-layer-1.stackup.yaml")
    height, epsilon = stackup.dielectric_between("L1", "L3")
    assert height == pytest.approx(0.2104 + 1.0650)
    # Series-capacitance average must sit between the two constituents,
    # and nearer the thicker one.
    assert 4.5 < epsilon < 4.6
    assert epsilon == pytest.approx(4.516, abs=0.002)


def test_copper_z_positions_are_ordered_and_span_the_board():
    stackup = load_stackup(FIXTURES / "six-layer-1.stackup.yaml")
    positions = stackup.copper_z_positions()
    assert list(positions) == ["L1", "L2", "L3", "L4", "L5", "L6"]
    tops = [z_top for z_top, _ in positions.values()]
    assert tops == sorted(tops)
    assert positions["L1"][0] == 0.0
    assert positions["L6"][1] == pytest.approx(stackup.total_thickness_mm)


def test_six_layer_reference_distances_differ_per_signal_layer():
    """The sig/gnd/sig/pwr/gnd/sig case: L1 sits much closer to L2 than L3 does."""
    stackup = load_stackup(FIXTURES / "six-layer-1.stackup.yaml")
    h_l1_l2, _ = stackup.dielectric_between("L1", "L2")
    h_l3_l2, _ = stackup.dielectric_between("L3", "L2")
    h_l3_l4, _ = stackup.dielectric_between("L3", "L4")
    assert h_l1_l2 == pytest.approx(0.0994)
    assert h_l3_l2 == pytest.approx(0.5500)   # across the core
    assert h_l3_l4 == pytest.approx(0.1088)   # across the 2116 prepreg
    # A trace on L3 references L4 far more strongly than L2.
    assert h_l3_l4 < h_l3_l2


# -- fixtures round-trip and validate -------------------------------------

@pytest.mark.parametrize("slug,layers", [
    ("four-layer-1", FOUR_LAYER),
    ("six-layer-1", SIX_LAYER),
])
def test_fixtures_validate_against_their_boards(slug, layers):
    stackup, warnings = load_and_validate(FIXTURES / f"{slug}.stackup.yaml", layers)
    assert len(stackup.copper_layers) == len(layers)
    # epsilon_r is defaulted from glass style, so warnings are expected —
    # but nothing blocking.
    assert all(w.severity == "warning" for w in warnings)
    assert any(w.code == "STK006" for w in warnings)


def test_fixture_carries_jlc_impedance_targets():
    stackup = load_stackup(FIXTURES / "four-layer-1.stackup.yaml")
    assert len(stackup.impedance_targets) == 2
    single = next(t for t in stackup.impedance_targets if t.kind == "single_ended")
    assert single.impedance_ohm == 50
    assert single.trace_width_mm == pytest.approx(0.3424)
    assert single.trace_to_copper_mm == pytest.approx(0.5080)
    assert single.is_coplanar


def test_missing_file_raises():
    with pytest.raises(StackupGateError, match="not found"):
        load_and_validate("/nonexistent/stackup.yaml")


def test_wrong_layer_count_is_rejected():
    """A 4-layer stackup applied to a 6-layer board must refuse."""
    with pytest.raises(StackupGateError, match="STK009"):
        load_and_validate(FIXTURES / "four-layer-1.stackup.yaml", SIX_LAYER)


def test_implausible_epsilon_r_is_rejected():
    stackup = Stackup(layers=[
        StackupLayer("copper", "L1", 0.035),
        StackupLayer("prepreg", "d1", 0.2, epsilon_r=12.0, epsilon_r_source="stated"),
        StackupLayer("copper", "L2", 0.035),
        StackupLayer("core", "d2", 1.0, epsilon_r=4.5, epsilon_r_source="stated"),
        StackupLayer("copper", "L3", 0.035),
    ])
    codes = {p.code for p in validate(stackup) if p.severity == "error"}
    assert "STK005" in codes


def test_thickness_inconsistent_with_finished_is_rejected():
    stackup = parse_jlc_table("""
        JLC04161H-7628(Standard/Finished thickness1.59mm±10%)
        L1	Outer Copper Weight1oz	1.38	0.0350
        Prepreg	7628, RC 49%, 8.6 mil	8.28	0.2104
        L2	Inner Copper Weight	0.60	0.0152
        Core	thin core	4.00	0.1000
        L3	Inner Copper Weight	0.60	0.0152
        Prepreg	7628, RC 49%, 8.6 mil	8.28	0.2104
        L4	Outer Copper Weight1oz	1.38	0.0350
    """)
    codes = {p.code for p in validate(stackup) if p.severity == "error"}
    assert "STK008" in codes


def test_a_dropped_column_says_so_rather_than_showing_a_sample():
    """The first command of every session, and the highest-leverage error
    message in the tool. A 3-column table is what someone produces after
    judging the mils column redundant; the old error showed a 4-column sample
    and left the reader to diff it."""
    from pcbreview.stackup import StackupGateError, parse_jlc_table
    with pytest.raises(StackupGateError) as excinfo:
        parse_jlc_table("Layer\tMaterial\tThickness(mm)\n"
                        "L1\tOuter Copper Weight1oz\t0.035\n")
    message = str(excinfo.value)
    assert "at least 4 are required" in message
    assert "Do NOT drop columns" in message
    assert "ignored by the parser but must be present" in message


# -- mirrored targets on a symmetric stack --------------------------------

def _symmetric_four_layer(prepreg_mm=0.2104, targets=()):
    """A plain symmetric 4-layer stack, and the one asymmetry that breaks it."""
    return Stackup(layers=[
        StackupLayer("copper", "L1", 0.035, copper_weight_oz=1.0),
        StackupLayer("prepreg", "prepreg1", 0.2104, epsilon_r=4.6),
        StackupLayer("copper", "L2", 0.0152, copper_weight_oz=0.5),
        StackupLayer("core", "core2", 1.065, epsilon_r=4.5),
        StackupLayer("copper", "L3", 0.0152, copper_weight_oz=0.5),
        StackupLayer("prepreg", "prepreg3", prepreg_mm, epsilon_r=4.6),
        StackupLayer("copper", "L4", 0.035, copper_weight_oz=1.0),
    ], impedance_targets=list(targets),
        layer_map={"L1": "F.Cu", "L2": "In1.Cu", "L3": "In2.Cu", "L4": "B.Cu"})


def test_a_stated_target_is_mirrored_onto_the_far_side():
    """A fab quotes one row per unique geometry, so an outer-layer target is
    written for L1 and never repeated for Ln — and a board whose RF feed is on
    the bottom layer then has no target matching it at all."""
    stated = ImpedanceTarget(50.0, "single_ended", "L1", 0.3424,
                             trace_to_copper_mm=0.508, bottom_ref="L2")
    stack = _symmetric_four_layer(targets=[stated])
    mirrored, = stack.mirrored_targets()

    assert (mirrored.signal_layer, mirrored.bottom_ref) == ("L4", "L3")
    assert mirrored.impedance_ohm == 50.0
    assert mirrored.trace_width_mm == stated.trace_width_mm
    assert mirrored.trace_to_copper_mm == stated.trace_to_copper_mm
    # and it says it was derived, because a validator must not solve it
    assert mirrored.derived_from == "L1"
    assert not mirrored.is_stated and stated.is_stated
    # the stated list is untouched — `verify` must keep seeing only the fab's
    assert [t.signal_layer for t in stack.impedance_targets] == ["L1"]


def test_an_asymmetric_stack_gets_no_mirrored_target():
    """The refusal is the point. One ply different and the far-side geometry
    is not the near-side geometry, so the fab's number does not apply — this
    is where mirroring would invent a number rather than reflect one."""
    stated = ImpedanceTarget(50.0, "single_ended", "L1", 0.3424,
                             trace_to_copper_mm=0.508, bottom_ref="L2")
    stack = _symmetric_four_layer(prepreg_mm=0.0920, targets=[stated])
    assert not stack.is_symmetric
    assert stack.mirrored_targets() == []


def test_a_fab_that_wrote_both_rows_is_not_given_a_third():
    stack = _symmetric_four_layer(targets=[
        ImpedanceTarget(50.0, "single_ended", "L1", 0.3424,
                        trace_to_copper_mm=0.508, bottom_ref="L2"),
        ImpedanceTarget(50.0, "single_ended", "L4", 0.3424,
                        trace_to_copper_mm=0.508, bottom_ref="L3"),
    ])
    assert stack.mirrored_targets() == []


def test_every_fixture_stackup_is_symmetric_and_mirrors_onto_the_bottom():
    """Symmetry is a manufacturing requirement — an asymmetric laminate warps
    on reflow — so this holds for every real stackup here, and the mirrored
    target always lands on the bottom copper layer."""
    for path in sorted(FIXTURES.glob("*.stackup.yaml")):
        stack = load_stackup(path)
        assert stack.is_symmetric, path.name
        bottom = stack.copper_layers[-1].name
        for target in stack.mirrored_targets():
            assert target.signal_layer == bottom
            assert target.derived_from == stack.copper_layers[0].name


def test_a_mirrored_target_solves_to_the_same_impedance():
    """The claim the whole feature rests on: reflection is an exact symmetry
    of the cross-section, so this costs no accuracy at all. Solved rather than
    asserted — and it agrees to every digit the solver prints."""
    from pcbreview import fields

    stack = load_stackup(FIXTURES / "six-layer-1.stackup.yaml")
    stated = [t for t in stack.impedance_targets
              if t.kind == "single_ended"][0]
    mirrored = [t for t in stack.mirrored_targets()
                if t.kind == "single_ended"][0]
    near, _ = fields.solve_target(stack, stated)
    far, _ = fields.solve_target(stack, mirrored)
    # not vacuous: both are real impedances near the fab's 50 ohm target
    assert 40.0 < near < 60.0
    assert near == pytest.approx(far, rel=1e-6)
