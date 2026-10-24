"""Contract tests that run against whatever board you register.

Every other shipped test is closed-form: it pins a calculation against an
analytic answer and needs no design at all. These are the opposite — they need
a real board, but they assert nothing about *which* board. Point them at your
own (see `tests/boards.py`) and they check the two things that matter most
when you have changed something:

1. **The geometry agrees with KiCad's own engine**, to machine epsilon, on
   your copper. Every conclusion this tool draws rests on the extraction being
   exact, and the extraction carries KiCad's own measurements of the same pour
   it just filled, so the comparison needs no second process and no reference
   file — see `test_the_geometry_agrees_with_kicad`.
2. **Every finding is well formed**: a number, the geometry it came from, its
   basis, and a severity that came from a stated threshold. A finding with a
   title and nothing behind it is the exact failure mode this tool exists to
   avoid, and it is the one a refactor reintroduces.

With no board registered every test here skips, and the skip says how to
switch it on. That is the normal state of a fresh install.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tests import boards
from pcbreview import audit as audit_module
from pcbreview import render as render_module
from pcbreview.findings import SEVERITY_ORDER
from pcbreview.model import Board
from pcbreview.stackup import StackupGateError

# Every role that can be audited needs a stackup; `tiny` and `no_stackup` are
# registered without one on purpose.
AUDITABLE = [role for role in boards.ROLES
             if boards.installed(role) and boards.REGISTRY[role].stackup]


def _fastest_auditable() -> str:
    """The smallest installed board that can be audited.

    A full audit is minutes on a large board and seconds on a small one, and
    these tests assert on the *shape* of the output rather than on its
    content, so the smallest board proves as much as the largest.
    """
    if not AUDITABLE:
        # Any board with a stackup will do, so name that rather than a role —
        # saying "no board for the role 'four_layer'" reads as though only
        # that one role would satisfy this test, and it would not.
        pytest.skip(
            "no board is registered with a stackup, and the audit refuses "
            "without one (Gate 1). Point PCBREVIEW_TEST_BOARDS at a YAML file "
            "giving any board and its stackup — see tests/boards.py. Roles "
            f"known: {', '.join(boards.ROLES)}")
    return min(AUDITABLE,
               key=lambda role: boards.REGISTRY[role].pcb.stat().st_size)


@pytest.fixture(scope="module")
def report():
    role = _fastest_auditable()
    board = boards.loaded(role)
    # `crosstalk` and `broadside` are the expensive pair and neither is needed
    # to prove the contract, which is about the shape of a finding.
    return audit_module.audit(board, skip=("crosstalk", "broadside"),
                              pcb_path=boards.REGISTRY[role].pcb)


# -- 1. the extraction against KiCad's own engine --------------------------

@pytest.mark.parametrize("role", boards.any_installed() or ["four_layer"])
def test_the_geometry_agrees_with_kicad(role):
    """Outline, zone fills, track lengths and pad copper, to KiCad's numbers.

    KiCad's `ZONE_FILLER` is not reproducible on every board — some pours
    polygonise two different ways across identical runs — so measuring the
    truth in a *second* KiCad process can compare two different pieces of
    copper. The extraction therefore carries KiCad's own measurements of the
    fill it just performed, and this compares within one fill by construction.
    """
    board = Board.from_kicad(boards.board_path(role))
    truth = board.kicad_measurements
    assert truth, ("the extraction must carry KiCad's own measurements; "
                   "re-extract with Board.from_kicad(path, refresh=True)")

    if truth["outline_area"] > 0:
        assert board.outline.area == pytest.approx(truth["outline_area"],
                                                   rel=1e-9)
    for key, expected in truth["zone_area"].items():
        zone_id, layer = key.split("|")
        ours = board.zones[int(zone_id)].fill_on(layer).area
        assert ours == pytest.approx(expected, rel=1e-9), f"zone {zone_id}"

    checked = 0
    for name, expected in truth["track_len"].items():
        try:
            ours = sum(t.length_mm for t in board.tracks_on_net(name))
        except KeyError:
            continue
        assert ours == pytest.approx(expected, rel=1e-5), f"net {name}"
        checked += 1

    by_designator = {p.designator: p for p in board.pads}
    for designator, expected in truth["pad_area"].items():
        pad = by_designator.get(designator)
        if pad is None or expected <= 0:
            continue
        assert pad.copper.area == pytest.approx(expected, rel=1e-6), designator
        checked += 1
    # An empty board is a legitimate design and proves nothing; say so rather
    # than passing silently on zero comparisons.
    if checked == 0:
        pytest.skip(f"{role} has no routed copper to compare")


# -- 2. Gate 1 -------------------------------------------------------------

def test_gate_one_refuses_a_board_with_no_stackup():
    """The refusal is the feature. Without real dielectric heights every
    impedance and coupling number would be invented rather than absent."""
    role = next((r for r in boards.REGISTRY
                 if boards.installed(r) and not boards.REGISTRY[r].stackup),
                None)
    if role is None:
        pytest.skip("every installed board has a stackup; register one "
                    "without under the role 'no_stackup' to exercise Gate 1")
    board = Board.from_kicad(boards.board_path(role))
    with pytest.raises(audit_module.AuditError, match="stackup"):
        audit_module.audit(board)


# -- 3. the shape of a finding --------------------------------------------

def test_every_finding_carries_a_number_and_a_basis(report):
    assert report.findings, "an audit that finds nothing at all is suspicious"
    for finding in report.findings:
        assert finding.check, "a finding must name the check that made it"
        assert finding.title.strip()
        assert finding.severity in SEVERITY_ORDER, finding.severity
        assert finding.basis.strip(), (
            f"{finding.check}: a finding with no basis cannot be checked "
            f"— {finding.title}")
        assert isinstance(finding.score, float)


def test_every_finding_above_info_carries_a_measurement(report):
    """Severity has to come from a computed quantity against a threshold.

    `info` findings are allowed to be prose — "this check had nothing to
    measure" is a legitimate thing to say. Anything the user is being asked to
    act on is not.
    """
    checked = 0
    for finding in report.findings:
        if finding.severity == "info":
            continue
        assert finding.measurement, (
            f"{finding.check}: {finding.title} is {finding.severity} with no "
            f"measurement behind it")
        checked += 1
    # A board whose findings are all `info` proves nothing here, and passing
    # silently on that would make this test look like evidence it is not.
    if checked == 0:
        pytest.skip("this board produced no finding above `info`, so there "
                    "was nothing for this test to check — register a board "
                    "with a real defect to exercise it")


def test_the_ranking_is_monotone_in_severity_then_score(report):
    ranks = [f.rank for f in report.findings]
    assert ranks == sorted(ranks), "findings must come back ranked"


def test_no_check_is_silently_skipped(report):
    """A check that could not run must say so and why. The alternative is a
    zero that reads as a clean bill of health."""
    stopped = 0
    for result in report.checks:
        assert result.name
        if not result.ran:
            assert result.skipped_because, (
                f"{result.name} did not run and did not say why")
            stopped += 1
    # The fixture skips `crosstalk` and `broadside`, so this should never be
    # zero; if it is, the report has stopped listing checks that did not run,
    # which is the failure this test exists to catch.
    assert stopped >= 2, ("the report should list the checks that did not "
                          "run, and at least two were skipped by the caller")


def test_an_assumption_is_stated_rather_than_taken(report):
    """Anything not measured is declared. With no schematic the audit falls
    back on net-name heuristics, and it has to admit that."""
    assert isinstance(report.assumptions, list)
    for finding in report.findings:
        if finding.assumption is not None:
            assert finding.assumption.strip()


# -- 4. the tool never touches the design ---------------------------------

def test_auditing_does_not_modify_the_board_file():
    """This runs over someone's real design. Reading it must not write it."""
    role = _fastest_auditable()
    pcb = boards.REGISTRY[role].pcb
    before = pcb.stat().st_mtime_ns
    board = boards.loaded(role)
    audit_module.audit(board, skip=("crosstalk", "broadside"), pcb_path=pcb)
    assert pcb.stat().st_mtime_ns == before, "the audit wrote to the board file"


# -- 5. the renderer ------------------------------------------------------

def test_a_finding_that_names_a_net_can_be_drawn(report, tmp_path):
    """The rule the front end follows is "render every finding that names a
    net", so a finding with nets must be drawable and one without must refuse
    rather than produce an empty picture."""
    role = _fastest_auditable()
    board = boards.loaded(role)

    def routed(name: str) -> bool:
        try:
            return bool(board.tracks_on_net(name))
        except KeyError:
            return False

    with_nets = next((f for f in report.findings
                      if f.nets and any(routed(n) for n in f.nets)), None)
    if with_nets is None:
        pytest.skip("no finding on this board names a routed net")
    pcb = boards.REGISTRY[role].pcb
    out = render_module.render_finding(board, pcb, with_nets, tmp_path)
    assert out, "render_finding returned nothing"

    # The other half of the rule: a finding naming no net must refuse rather
    # than draw an empty picture. `stackup_mismatch`, `placement` and
    # `thermal` are the checks that produce them, so a board that exercises
    # none of those leaves this half unproven — say so rather than pass.
    netless = next((f for f in report.findings if not f.nets), None)
    if netless is None:
        pytest.skip("every finding on this board names a net, so the "
                    "refusal half of the rendering rule went unexercised")
    # **Match the message, not just the type.** `render()` raises the same
    # `RenderError` a few lines further on for "nothing to draw", so asserting
    # the bare exception type passed even with this refusal deleted — the
    # mutation only ever failed because the old anchor injected a net as a
    # side effect. A test that cannot tell two different refusals apart is not
    # testing this one.
    with pytest.raises(render_module.RenderError, match="names no nets"):
        render_module.render_finding(board, pcb, netless, tmp_path)

    # And the escape from that refusal has to work, or `--around` is a
    # documented feature nothing exercises.
    if netless.location is not None:
        drawn = render_module.render_finding(board, pcb, netless, tmp_path,
                                             radius_mm=5.0)
        assert drawn.width_mm == pytest.approx(10.0)
        assert drawn.height_mm == pytest.approx(10.0)


# -- 8. inputs can be pinned, so a baseline measures code and not files ----

def test_a_suppressed_manifest_is_not_discovered_and_is_reported(tmp_path):
    """`False` means "there is none, and do not go looking".

    Without this a baseline is not pinned to its inputs. Both manifests are
    auto-discovered beside the board, so a review session that leaves a
    `<board>.thermal.yaml` behind silently changes every later run — one
    baseline here moved 96 -> 122 findings with nothing in the engine
    responsible, and the diff blamed the code (§52.4). The mirror-image trap,
    a manifest kept somewhere else and silently lost, is the same defect
    (§47.9).

    The suppression must also be **stated**: an audit that quietly ignored the
    manifest sitting beside the board would be its own kind of coverage lie.
    """
    role = _fastest_auditable()
    entry = boards.REGISTRY[role]

    thermal = entry.pcb.with_suffix(".thermal.yaml")
    if thermal.exists():
        pytest.skip(f"{thermal.name} already exists beside the board; this "
                    f"test writes one and will not overwrite a real manifest")

    # A manifest that would certainly change the output if it were read.
    thermal.write_text(
        "parts:\n"
        "  U1:\n"
        "    t_junction_max_c: 150\n"
        "    theta_ja_c_per_w: 50\n"
        "    dissipation_w: 2.0\n"
        "    source: fabricated by a test; every value here is made up\n")
    try:
        suppressed = audit_module.audit_files(
            entry.pcb, entry.stackup, thermal_path=False,
            skip=("crosstalk", "broadside"))
    finally:
        thermal.unlink()

    assert any("suppressed auto-discovery" in a and "thermal" in a
               for a in suppressed.assumptions), suppressed.assumptions
    thermal_findings = [f for f in suppressed.findings if f.check == "thermal"]
    assert thermal_findings, "the thermal check must still report"
    assert all(f.measurement.get("coverage", {}).get("manifest") is not True
               for f in thermal_findings
               if "coverage" in f.measurement), \
        "a suppressed manifest must not be read"
