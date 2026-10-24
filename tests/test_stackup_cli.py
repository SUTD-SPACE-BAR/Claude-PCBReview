"""Tests for the stackup init/verify flow and the stackup_mismatch check.

The load-bearing test here is `test_verify_catches_a_misread_transcription`.
The whole reason `verify` exists is that a transcription error — a digit
transposed reading a screenshot of the fab's calculator — silently biases every
impedance and coupling number downstream and nothing else notices. If that test
ever stops failing on corrupted input, the verification is decorative.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import stackup_cli
from pcbreview.stackup import StackupGateError, load_stackup, validate

HERE = Path(__file__).resolve().parents[1]

SIX_LAYER_STACKUP = HERE / "fixtures" / "six-layer-1.stackup.yaml"
FOUR_LAYER_STACKUP = HERE / "fixtures" / "four-layer-1.stackup.yaml"

SIX_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
FOUR_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

needs_six_layer = pytest.mark.skipif(not SIX_LAYER_STACKUP.exists(), reason="fixture missing")
needs_four_layer = pytest.mark.skipif(not FOUR_LAYER_STACKUP.exists(), reason="fixture missing")


# -- init ------------------------------------------------------------------

def test_the_skeleton_carries_the_layer_map_and_nothing_else():
    text = stackup_cli.skeleton(SIX_LAYERS, 1.6)
    assert "L1: F.Cu" in text and "L6: B.Cu" in text
    assert "FILL IN" in text
    assert "finished_thickness_mm: 1.6" in text


def test_the_skeleton_refuses_the_gate_until_it_is_filled_in(tmp_path):
    """A skeleton that passed validation with plausible defaults would be worse
    than no skeleton at all — it would put invented dielectrics behind Gate 1.
    """
    path = tmp_path / "skeleton.yaml"
    path.write_text(stackup_cli.skeleton(SIX_LAYERS, 1.6))
    problems = validate(load_stackup(path), SIX_LAYERS)
    assert any(p.severity == "error" for p in problems)
    codes = {p.code for p in problems if p.severity == "error"}
    assert "STK003" in codes, "zero-thickness dielectrics must be rejected"


def test_the_skeleton_tells_the_user_not_to_copy_kicads_numbers():
    text = stackup_cli.skeleton(FOUR_LAYERS, 1.59)
    assert "Board Setup" in text
    assert "impedance_targets" in text


def test_the_skeleton_maps_positionally_when_layer_names_are_unusual():
    layers = ["F.Cu", "Inner1", "Inner2", "B.Cu"]
    text = stackup_cli.skeleton(layers, 1.6)
    assert "L2: Inner1" in text and "L3: Inner2" in text


# -- transcribe ------------------------------------------------------------

#: The six-layer JLC06161H-3313 table, exactly as the calculator prints it.
JLC_ROWS = """
JLC06161H-3313(Standard/Finished thickness1.54mm±10%)
L1  Outer Copper Weight1oz  1.38  0.0350
Prepreg  3313 RC57% 4.2mil  3.91  0.0994
L2  Inner Copper Weight  0.60  0.0152
Core  0.55mm H/Hwithout copper  21.65  0.5500
L3  Inner Copper Weight  0.60  0.0152
Prepreg  2116 RC54% 4.9mil  4.28  0.1088
L4  Inner Copper Weight  0.60  0.0152
Core  0.55mm H/Hwithout copper  21.65  0.5500
L5  Inner Copper Weight  0.60  0.0152
Prepreg  3313 RC57% 4.2mil  3.91  0.0994
L6  Outer Copper Weight1oz  1.38  0.0350
"""

COPLANAR_90 = ("ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.1494,"
               "spacing=0.2032,to_copper=0.2032")


def test_transcribe_derives_epsilon_r_from_the_glass_style_not_from_memory():
    """The whole reason this command exists (§25 correction 1): the numbers a
    model would otherwise have to remember come out of the material string."""
    text = stackup_cli.transcribe(JLC_ROWS, SIX_LAYERS, [COPLANAR_90])
    assert "epsilon_r: 4.25" in text      # 3313
    assert "epsilon_r: 4.35" in text      # 2116
    assert "epsilon_r_source: glass_style_default" in text
    assert "fab_code: JLC06161H-3313" in text
    assert "finished_thickness_mm: 1.54" in text


def test_transcribe_maps_l1_ln_onto_the_boards_own_layer_names():
    layers = ["F.Cu", "Inner1", "Inner2", "Inner3", "Inner4", "B.Cu"]
    text = stackup_cli.transcribe(JLC_ROWS, layers, [COPLANAR_90])
    assert "L2: Inner1" in text and "L6: B.Cu" in text


def test_a_transcription_passes_the_gate_and_solves_its_own_target(tmp_path):
    """End to end: rows in, a stackup that Gate 1 accepts and whose target the
    field solver reproduces."""
    path = tmp_path / "s.yaml"
    path.write_text(stackup_cli.transcribe(JLC_ROWS, SIX_LAYERS, [COPLANAR_90]))
    stackup = load_stackup(path)
    assert not [p for p in validate(stackup, SIX_LAYERS) if p.severity == "error"]
    result = stackup_cli.verify(stackup, SIX_LAYERS)
    assert len(result.targets) == 1
    assert abs(result.targets[0].error) < stackup_cli.TARGET_TOLERANCE


def test_to_copper_must_be_stated_because_omitting_it_is_a_physical_claim():
    """§40a. `is_coplanar` is nothing but "did this key have a value", so an
    absent key silently decides a real physical difference — and that is how
    The six-layer board's 90 ohm target came to be stored as coplanar at a width that is
    *wider* than the non-coplanar answer for the same stackup, which is
    backwards. `none` is four characters."""
    with pytest.raises(stackup_cli.TranscribeError, match="to_copper"):
        stackup_cli.parse_target(
            "ohm=90,kind=diff,layer=L1,width=0.1537,spacing=0.2032")

    target = stackup_cli.parse_target(
        "ohm=90,kind=diff,layer=L1,width=0.1537,spacing=0.2032,to_copper=none")
    assert target.trace_to_copper_mm is None
    assert not target.is_coplanar
    assert stackup_cli.parse_target(COPLANAR_90).is_coplanar


def test_a_differential_target_without_a_spacing_is_refused():
    with pytest.raises(stackup_cli.TranscribeError, match="spacing"):
        stackup_cli.parse_target(
            "ohm=90,kind=diff,layer=L1,width=0.15,to_copper=none")


@pytest.mark.parametrize("spec,pattern", [
    ("ohm=50,kind=wat,layer=L1,width=0.3,to_copper=none", "single_ended"),
    ("ohm=50,kind=se,layer=L1,width=abc,to_copper=none", "not a number"),
    ("ohm=50,kind=se,layer=L1,width=0.3,to_copper", "key=value"),
    ("kind=se,layer=L1,width=0.3,to_copper=none", "ohm"),
])
def test_a_malformed_target_is_refused_rather_than_guessed(spec, pattern):
    with pytest.raises(stackup_cli.TranscribeError, match=pattern):
        stackup_cli.parse_target(spec)


def test_kind_accepts_what_the_calculator_actually_says():
    for alias in ("diff", "differential", "Differential Pair", "pair"):
        spec = f"ohm=90,kind={alias},layer=L1,width=0.15,spacing=0.2,to_copper=none"
        assert stackup_cli.parse_target(spec).kind == "differential"
    for alias in ("se", "single", "single_ended", "Single Ended"):
        spec = f"ohm=50,kind={alias},layer=L1,width=0.3,to_copper=none"
        assert stackup_cli.parse_target(spec).kind == "single_ended"


def test_a_half_transcribed_table_is_refused_against_the_boards_layer_count():
    """Stopping halfway down the screenshot is the easy mistake, and the
    resulting stackup would be a plausible-looking four-layer board."""
    truncated = "\n".join(JLC_ROWS.strip().splitlines()[:6])
    with pytest.raises(stackup_cli.TranscribeError, match="copper layers"):
        stackup_cli.transcribe(truncated, SIX_LAYERS, [COPLANAR_90])


def test_an_integral_target_is_written_as_an_integer():
    """The file is read by a human checking a transcription against a
    screenshot; `impedance_ohm: 90.0` against a screenshot saying 90 is noise."""
    text = stackup_cli.transcribe(JLC_ROWS, SIX_LAYERS, [COPLANAR_90])
    assert "impedance_ohm: 90\n" in text


def test_transcribe_reproduces_a_fixture_that_was_written_by_hand():
    """`fixtures/six-layer-2.stackup.yaml` was built with a throwaway script
    before this command existed. It must now come out of the command
    unchanged, or the command is not the thing that was actually used.

    Needs no board: it is the transcriber against a file on disk.
    """
    fixture = HERE / "fixtures" / "six-layer-2.stackup.yaml"
    if not fixture.exists():
        pytest.skip("fixture missing")
    produced = stackup_cli.transcribe(
        JLC_ROWS, SIX_LAYERS, [COPLANAR_90],
        name="Six-layer 1.55 mm, 1 oz outer / 0.5 oz inner (worked example)")
    assert produced.strip() == fixture.read_text().strip()


# -- verify, on the real stackups -----------------------------------------

@needs_six_layer
@needs_four_layer
@pytest.mark.parametrize("fixture,layers,expected", [
    (SIX_LAYER_STACKUP, SIX_LAYERS, 2),
    (FOUR_LAYER_STACKUP, FOUR_LAYERS, 2),
])
def test_verify_reproduces_the_fabs_own_targets(fixture, layers, expected):
    """§12's result, now reachable as a command rather than only as a test.

    All four targets across both boards land at -1.4% to -1.7%, which is the
    consistent residual the solver has against JLCPCB — most likely unmodelled
    solder-mask thickness.
    """
    result = stackup_cli.verify(load_stackup(fixture), layers)
    assert len(result.targets) == expected
    assert result.ok
    for target in result.targets:
        assert target.verdict == "ok"
        assert -0.020 < target.error < -0.010, str(target)


@needs_six_layer
def test_verify_says_which_layers_the_targets_actually_constrain():
    """The honest limit: a target only says something about the layers it
    references. On a six-layer board that is two of six."""
    result = stackup_cli.verify(load_stackup(SIX_LAYER_STACKUP), SIX_LAYERS)
    assert result.constrained_layers == ["L1", "L2"]
    assert result.unconstrained_layers == ["L3", "L4", "L5", "L6"]
    assert "say nothing about" in result.report()


@needs_six_layer
@pytest.mark.parametrize("label,mutate,worst_verdict", [
    # A digit transposed reading the calculator's own table.
    ("prepreg thickness 0.0994 -> 0.0904",
     lambda s: setattr(s.layers[1], "thickness_mm", 0.0904), "suspect"),
    # A decimal point in the wrong place.
    ("prepreg thickness 0.0994 -> 0.1994",
     lambda s: setattr(s.layers[1], "thickness_mm", 0.1994), "wrong"),
    # The wrong glass style, so the wrong epsilon_r.
    ("epsilon_r 4.25 -> 3.3",
     lambda s: setattr(s.layers[1], "epsilon_r", 3.3), "wrong"),
    # Half-ounce copper read as one ounce.
    ("outer copper 0.035 -> 0.0152",
     lambda s: setattr(s.layers[0], "thickness_mm", 0.0152), "suspect"),
])
def test_verify_catches_a_misread_transcription(label, mutate, worst_verdict):
    """The reason this command exists.

    A misread dielectric height biases every impedance and coupling result and
    nothing downstream notices. The fab's impedance targets are redundant
    information that constrains those numbers, so solving them turns "trust the
    transcription" into "check it against physics".

    Every corruption mode is caught, including a 9% thickness slip.
    """
    stackup = copy.deepcopy(load_stackup(SIX_LAYER_STACKUP))
    mutate(stackup)
    result = stackup_cli.verify(stackup, SIX_LAYERS)
    assert not result.ok, f"{label} slipped through"
    verdicts = {t.verdict for t in result.targets}
    assert verdicts != {"ok"}
    assert worst_verdict in verdicts, (label, verdicts,
                                       [t.error for t in result.targets])


@needs_six_layer
def test_verify_reports_a_stackup_with_no_targets_as_unverifiable():
    """Without targets the impedance half cannot run, and silence would read
    as a pass."""
    stackup = copy.deepcopy(load_stackup(SIX_LAYER_STACKUP))
    stackup.impedance_targets = []
    result = stackup_cli.verify(stackup, SIX_LAYERS)
    assert result.targets == []
    assert "[MISSING]" in result.report()


@needs_six_layer
def test_verify_does_not_solve_a_stackup_that_already_failed_validation():
    """Solving nonsense produces a number, and a number invites belief."""
    stackup = copy.deepcopy(load_stackup(SIX_LAYER_STACKUP))
    stackup.layers[1].thickness_mm = 0.0
    result = stackup_cli.verify(stackup, SIX_LAYERS)
    assert result.targets == []
    assert not result.ok


def test_the_mismatch_check_is_silent_without_a_bound_stackup():
    class _Bare:
        stackup = None
        source_path = None
        _data: dict = {}

    from pcbreview.audit import check_stackup_mismatch
    assert check_stackup_mismatch(_Bare()) == []


# -- diagnosing a failing target (§48.3 item 4) ----------------------------

def _two_layer_stackup():
    """A 2-layer 1.465 mm core — the regime where `is_coplanar` dominates.

    On a thick core the coplanar pours set the impedance almost entirely
    (105 -> 60 -> 51 ohm as the gap goes none -> 0.2032 -> 0.127 at fixed
    width), which is why a misread `kind` or `to_copper` there is invisible to
    any width correction.
    """
    return stackup_cli.Stackup(
        layers=[
            stackup_cli.StackupLayer(
                role="copper", name="L1", thickness_mm=0.035,
                material="Outer Copper Weight1oz", copper_weight_oz=1.0),
            stackup_cli.StackupLayer(
                role="core", name="core1", thickness_mm=1.465,
                material="7628 core", epsilon_r=4.6, loss_tangent=0.02,
                epsilon_r_source="glass style"),
            stackup_cli.StackupLayer(
                role="copper", name="L2", thickness_mm=0.035,
                material="Outer Copper Weight1oz", copper_weight_oz=1.0),
        ],
        name="two-layer 1.5 mm core", finished_thickness_mm=1.6,
        layer_map={"L1": "F.Cu", "L2": "B.Cu"})


def test_a_failing_target_says_which_field_could_be_wrong():
    """The exact misread from the first outside board.

    They typed `50 ohm, Single Ended (Non coplanar), w=0.4625`; the row said
    `90 ohm, Coplanar Differential Pair, w=0.4625, s=0.2032, to_copper=0.2032`.
    The gate caught it at +109%, and then could say nothing about *which* of
    five fields to re-read. Hand-sweeping the solver, they found two
    single-field corrections that both landed inside the tolerance and neither
    was the real error — which was `kind`, a field no width or gap
    perturbation reaches.
    """
    stackup = _two_layer_stackup()
    misread = stackup_cli.ImpedanceTarget(
        impedance_ohm=50, kind="single_ended", signal_layer="L1",
        bottom_ref="L2", trace_width_mm=0.4625, trace_to_copper_mm=None)
    lines = stackup_cli.diagnose(stackup, misread)
    joined = " ".join(lines)
    # The categorical fields are named, which is the whole point.
    assert "kind=differential" in joined
    assert "to_copper=" in joined
    # And it does not present one of them as the answer.
    assert len(lines) > 1


def test_a_correct_target_is_not_diagnosed_at_all():
    """`diagnose` runs only on a failing verdict — a passing stackup must not
    pay ~2.5 s per target for a list nobody needs."""
    stackup = _two_layer_stackup()
    stackup.impedance_targets = [stackup_cli.ImpedanceTarget(
        impedance_ohm=90, kind="differential", signal_layer="L1",
        bottom_ref="L2", trace_width_mm=0.4625, trace_spacing_mm=0.2032,
        trace_to_copper_mm=0.2032)]
    result = stackup_cli.verify(stackup, ["F.Cu", "B.Cu"])
    assert result.ok
    assert [t.consistent_with for t in result.targets] == [[]]


def test_a_wrong_target_carries_its_diagnosis_into_the_report():
    stackup = _two_layer_stackup()
    stackup.impedance_targets = [stackup_cli.ImpedanceTarget(
        impedance_ohm=50, kind="single_ended", signal_layer="L1",
        bottom_ref="L2", trace_width_mm=0.4625, trace_to_copper_mm=None)]
    result = stackup_cli.verify(stackup, ["F.Cu", "B.Cu"])
    assert not result.ok
    assert result.targets[0].consistent_with
    report = result.report()
    assert "this residual is consistent with" in report
    assert "re-read those fields on the screenshot" in report


def test_a_diagnosis_that_finds_nothing_points_at_the_dielectric():
    """A misread dielectric height moves every target on the layer and no
    single target field reaches it, so saying so is the useful answer."""
    stackup = _two_layer_stackup()
    stackup.layers[1].thickness_mm = 0.05      # nothing like the real core
    impossible = stackup_cli.ImpedanceTarget(
        impedance_ohm=500, kind="single_ended", signal_layer="L1",
        bottom_ref="L2", trace_width_mm=0.4625, trace_to_copper_mm=None)
    lines = stackup_cli.diagnose(stackup, impossible)
    assert "dielectric" in lines[0]
    # Nothing after the first line may look like a correction that would work.
    assert not any(line.startswith(("trace_width=", "trace_spacing=",
                                    "to_copper=")) for line in lines)
