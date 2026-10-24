"""The user's severity policy — KiCad's Violation Severity page, for this tool.

The property this file protects is the one that makes the feature safe rather
than merely convenient: **a policy re-levels what the engine reported, and
never destroys what the engine measured.** Every test that changes a severity
also asserts the computed one is still recoverable, and the "ignore" tests
assert the finding is still *there*.

Everything that writes runs against a copy in a temp directory. Nothing here
touches `pcb_examples/`.
"""

from __future__ import annotations

import shutil

import pytest

from pcbreview import dossier as dossier_module
from pcbreview import facts, pipeline, problems
from pcbreview import severity
from pcbreview.report import generate


@pytest.fixture
def board_copy(boards, tmp_path):
    source = boards["tiny"].parent
    root = tmp_path / source.name
    shutil.copytree(source, root,
                    ignore=shutil.ignore_patterns(".history", "*-backups"))
    return pipeline.discover(root)


class _Finding:
    """The two attributes `apply` touches, and nothing else."""

    def __init__(self, check, severity):
        self.check = check
        self.severity = severity
        self.measurement = {}


# ---------------------------------------------------------------------------
# The policy file
# ---------------------------------------------------------------------------

def test_a_policy_round_trips_through_its_own_loader(board_copy):
    policy = severity.Policy({"crosstalk": "ignore", "drc": "ignore"})
    written = severity.write(board_copy.board, policy)
    back = severity.load(written)
    assert back.checks == {"crosstalk": "ignore", "drc": "ignore"}
    assert back.enabled is True


def test_a_retired_severity_is_read_and_dropped_not_honoured(board_copy):
    """A file from when this page re-levelled must still open.

    The user removed per-check severities — the engine computes severity, so a
    typed one is a number with nothing behind it. Refusing such a file would
    make an older one unopenable; honouring it would reintroduce the thing that
    was removed. So it is read, dropped, and *reported*.
    """
    target = severity.default_path(board_copy.board)
    target.write_text("checks:\n  crosstalk: minor\n  drc: ignore\n",
                      encoding="utf-8")
    policy = severity.load(target)
    assert policy.checks == {"drc": "ignore"}, "a severity was honoured"
    assert policy.dropped == ["crosstalk=minor"], "the drop went unreported"


def test_report_is_the_same_as_saying_nothing(board_copy):
    findings = [_Finding("crosstalk", "major")]
    severity.apply(findings,
                          severity.Policy({"crosstalk": "report"}))
    assert findings[0].severity == "major"
    assert not severity.is_ignored(findings[0])


def test_a_policy_with_an_unknown_level_is_refused(board_copy):
    target = severity.default_path(board_copy.board)
    target.write_text("checks:\n  crosstalk: urgent\n", encoding="utf-8")
    with pytest.raises(severity.SeverityError) as excinfo:
        severity.load(target)
    assert "urgent" in str(excinfo.value)


def test_a_refused_policy_never_becomes_the_file(board_copy):
    """The same discipline `facts.write_intent` uses: validate, then replace."""
    target = severity.default_path(board_copy.board)
    severity.write(board_copy.board,
                          severity.Policy({"drc": "minor"}))
    before = target.read_bytes()
    with pytest.raises(severity.SeverityError):
        severity.write(board_copy.board,
                              severity.Policy({"drc": "nonsense"}))
    assert target.read_bytes() == before
    assert not list(target.parent.glob("*.writing"))


def test_no_policy_file_is_not_an_error(board_copy):
    policy = severity.load(
        severity.default_path(board_copy.board))
    assert not policy
    assert policy.checks == {}


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------

def test_a_policy_never_changes_a_severity():
    """The property the whole page rests on now.

    §3.2 rule 4: severity comes from a stated threshold on a computed quantity.
    A policy decides whether a finding is *shown*, never what it is worth — so
    every severity here must survive `apply` untouched.
    """
    findings = [_Finding("crosstalk", "major"), _Finding("drc", "minor")]
    severity.apply(findings, severity.Policy(
        {"crosstalk": "ignore", "drc": "ignore"}))
    assert findings[0].severity == "major"
    assert findings[1].severity == "minor"
    assert all("severity_before_policy" not in f.measurement for f in findings)


def test_ignore_marks_and_the_caller_removes_it():
    """`ignore` means "do not show me this", and `apply` only marks.

    The user set every check to ignore and reported that nothing changed:
    *"Ignored should just not show the error at all anywhere."* An earlier
    version lowered to `info` and left the finding in place, so it still
    appeared in the inspector, on the canvas and in the report while only the
    problems panel filtered it.

    Removal happens at one seam, `pipeline.apply_policy`, so that `generate`,
    `build_index` and `problems.build` need no special case and cannot forget.
    """
    findings = [_Finding("drc", "critical")]
    outcome = severity.apply(
        findings, severity.Policy({"drc": "ignore"}))
    assert outcome["ignored"] == 1
    assert severity.is_ignored(findings[0])
    # The severity is untouched — the finding is withheld, not re-levelled.
    assert findings[0].severity == "critical"


def test_restore_undoes_a_policy_completely():
    """What makes a policy change need no re-review."""
    findings = [_Finding("crosstalk", "major"), _Finding("drc", "critical")]
    severity.apply(findings, severity.Policy(
        {"crosstalk": "ignore", "drc": "ignore"}))
    assert all(severity.is_ignored(f) for f in findings)

    severity.restore(findings)
    assert not any(severity.is_ignored(f) for f in findings)
    assert findings[0].severity == "major"
    assert findings[1].severity == "critical"


def test_a_disabled_policy_changes_nothing():
    """The one switch the user asked for, doing exactly what it says."""
    findings = [_Finding("crosstalk", "major")]
    outcome = severity.apply(
        findings, severity.Policy({"crosstalk": "info"}, enabled=False))
    assert outcome["applied"] is False
    assert findings[0].severity == "major"
    assert "severity_before_policy" not in findings[0].measurement


def test_a_check_with_no_entry_is_left_alone():
    findings = [_Finding("crosstalk", "major"), _Finding("parity", "critical")]
    severity.apply(findings,
                          severity.Policy({"crosstalk": "minor"}))
    assert findings[1].severity == "critical"
    assert "severity_before_policy" not in findings[1].measurement

def test_the_summary_names_the_file_and_says_nothing_was_removed():
    policy = severity.Policy({"drc": "ignore"}, source="x.severity.yaml")
    line = severity.summary(policy, {"applied": True, "changed": 0,
                                            "ignored": 3})
    assert "x.severity.yaml" in line
    assert "drc=ignore" in line
    assert "withheld" in line and "still RAN" in line


def test_no_policy_means_no_line_in_the_report():
    assert severity.summary(severity.Policy(),
                                   {"applied": False}) is None


# ---------------------------------------------------------------------------
# End to end through the pipeline
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_a_policy_reaches_the_report_and_says_so(board_copy):
    """The user's ruling: it changes the artefact, not just the view."""
    plain = pipeline.review(board_copy)
    target = next((f for f in plain.layout.findings
                   if f.severity == "major"), None)
    assert target is not None, "no major finding to withhold"
    check = target.check
    before = sum(1 for f in plain.layout.findings if f.check == check)

    severity.write(board_copy.board,
                          severity.Policy({check: "ignore"}))
    after = pipeline.review(pipeline.discover(board_copy.root))

    assert after.policy_outcome["applied"] is True
    assert after.policy_outcome["ignored"] == before
    # Gone from the report's own findings...
    assert not [f for f in after.layout.findings if f.check == check]
    # ...and the report says a policy ran, so the artefact is not silently short.
    assert any("severity policy was applied" in line
               for line in after.layout.assumptions)

@pytest.mark.slow
def test_an_unreadable_policy_is_reported_and_not_silently_ignored(board_copy):
    """A review that looks like it honoured your settings and did not is the
    one outcome worse than refusing."""
    severity.default_path(board_copy.board).write_text(
        "checks:\n  drc: nonsense\n", encoding="utf-8")
    result = pipeline.review(pipeline.discover(board_copy.root))
    assert result.policy_outcome.get("applied") is not True
    assert any("could not be read" in line and "NOT applied" in line
               for line in result.layout.assumptions)


@pytest.mark.slow
def test_an_ignored_check_is_shown_nowhere_but_still_counted(board_copy):
    """*"Ignored should just not show the error at all anywhere."*

    So the findings are absent from the report, the index and the panel — and
    the **count** is still stated, because a review that silently got shorter
    is the failure §2.10 principle 5 exists to prevent.
    """
    plain = pipeline.review(board_copy)
    check = next(f.check for f in plain.layout.findings
                 if f.severity in ("major", "critical"))
    before = sum(1 for f in plain.layout.findings if f.check == check)
    assert before

    severity.write(board_copy.board,
                          severity.Policy({check: "ignore"}))
    result = pipeline.review(pipeline.discover(board_copy.root))

    # Gone from the findings the whole app reads from...
    assert not [f for f in result.layout.findings if f.check == check]
    panel = problems.build(result.layout, result.schematic,
                                  result.report)
    assert not [f for f in panel["findings"] if f["check"] == check]
    index = dossier_module.build_index(
        pipeline.review(pipeline.discover(board_copy.root),
                        keep_board=True).board,
        result.layout, result.schematic)
    assert not any(ref.check == check
                   for refs in index.findings_by_net.values() for ref in refs)
    # ...and counted, in the outcome and in the report's own assumptions.
    assert result.policy_outcome["ignored"] == before
    assert any("severity policy was applied" in line
               for line in result.layout.assumptions)


@pytest.mark.slow
def test_turning_the_policy_off_brings_every_finding_back(board_copy):
    """`enabled: false` is the one control, and it must be complete."""
    plain = pipeline.review(board_copy)
    check = next(f.check for f in plain.layout.findings
                 if f.severity in ("major", "critical"))
    before = sorted(f.title for f in plain.layout.findings)

    severity.write(board_copy.board,
                          severity.Policy({check: "ignore"}))
    assert not [f for f in pipeline.review(
        pipeline.discover(board_copy.root)).layout.findings
        if f.check == check]

    severity.write(board_copy.board,
                          severity.Policy({check: "ignore"},
                                                 enabled=False))
    restored = pipeline.review(pipeline.discover(board_copy.root))
    assert sorted(f.title for f in restored.layout.findings) == before
@pytest.mark.slow
def test_a_policy_change_needs_no_rerun(boards, tmp_path):
    """*"Why does changing severity of things require a rerun? Shouldnt it just
    relabel and refilter what gets shown to the user?"*

    It should. A severity policy relabels findings a check already computed and
    withholds the ones set to `ignore`; it measures nothing. So it is applied
    to a review that has already run, and nothing is re-audited.

    Contrast `netintent`, which does need a re-run: declaring a net `power`
    admits it to `check_current_capacity` and `check_heat_path`, and those
    numbers do not exist until the audit runs again.

    The audit here runs **once**, and the test's whole claim is that both the
    change and its undo happen without a second one.
    """
    source = boards["tiny"].parent
    root = tmp_path / source.name
    shutil.copytree(source, root,
                    ignore=shutil.ignore_patterns(".history", "*-backups",
                                                  "review"))

    result = pipeline.review_path(root)
    check = next(f.check for f in result.layout.findings
                 if f.severity in ("major", "critical"))
    before = len([f for f in result.layout.findings if f.check == check])
    assert before, "the fixture needs a check with findings to withhold"

    policy = severity.Policy(checks={check: "ignore"})
    outcome = pipeline.apply_policy(result.layout, result.schematic, policy)
    assert outcome["applied"] is True

    # The effect is immediate and total: the findings are gone from the list
    # every downstream view reads, so `generate`, the problems view and the
    # facts table need no special case and cannot each forget a different one.
    assert not [f for f in result.layout.findings if f.check == check]
    report = generate(result.layout, result.schematic)
    assert not [ref for ref in report.severe if ref.check == check]
    panel = problems.build(result.layout, result.schematic, report)
    assert not [f for f in panel["findings"] if f["check"] == check]

    # Turning it off restores every one of them, at the severity its check
    # computed — not at whatever the policy last said.
    pipeline.restore_policy(result.layout, result.schematic,
                            result.all_layout_findings,
                            result.all_schematic_findings)
    assert len([f for f in result.layout.findings
                if f.check == check]) == before
    assert not any(severity.is_ignored(f) for f in result.layout.findings)
