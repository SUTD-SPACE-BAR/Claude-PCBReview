"""Tests for grouping findings by cause.

The user's constraint is the whole specification: *"grouped by cause,
followed by comprehensive list of consequences, so its clear that fixing this
one thing fixes all this, but no info is lost or hidden."*

So the load-bearing test here is not any particular grouping — it is
`test_grouping_is_a_partition`, which asserts that nothing is ever dropped,
merged away or shown twice. Everything else is about the key being the cause
rather than an accident of formatting.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import grouping
from pcbreview.findings import Finding


def _finding(check, severity="major", score=5.0, nets=(), **measurement):
    return Finding(check=check, title=f"{check} on {', '.join(nets) or '-'}",
                   severity=severity, score=score, nets=list(nets),
                   measurement=dict(measurement))


# -- the constraint --------------------------------------------------------

def _mixed_findings():
    return [
        _finding("return_path", nets=["/A"], reference_nets=["GND"]),
        _finding("return_path", nets=["/B"], reference_nets=["GND"]),
        _finding("return_path", nets=["/C"], reference_nets=["/VIN", "GND"]),
        # the same cause with the two nets written the other way round
        _finding("return_path", nets=["/D"], reference_nets=["GND", "/VIN"]),
        # a dict, which is how reference_split records it
        _finding("reference_split", nets=["/E"],
                 reference_nets={"/VIN": 2.37, "GND": 1.28}),
        _finding("layer_transition", nets=["/F"],
                 unreferenced_layers=["In1.Cu"]),
        _finding("crosstalk", nets=["/G", "/H"], victim="/G"),
        _finding("crosstalk", nets=["/I", "/J"], victim="/I"),
        _finding("thermal", severity="critical", score=9.0),
        _finding("board_state", severity="minor", score=2.0),
    ]


def test_grouping_is_a_partition():
    """No info is lost or hidden — the rule the whole feature exists under.

    Every finding comes back out, exactly once, and the objects are the same
    objects rather than copies.
    """
    findings = _mixed_findings()
    groups = grouping.group_by_cause(findings)
    regrouped = [f for g in groups for f in g.findings]
    assert len(regrouped) == len(findings)
    assert {id(f) for f in regrouped} == {id(f) for f in findings}


def test_a_group_report_lists_every_one_of_its_findings():
    """The heading is a heading, never a summary that replaces the list."""
    findings = _mixed_findings()
    for group in grouping.group_by_cause(findings):
        text = group.report()
        for finding in group.findings:
            assert finding.title in text


def test_a_member_is_visually_distinct_from_its_heading():
    """Both start `[MAJOR   ]`, so without indentation a group of 34 reads as
    35 unrelated findings — the opposite of the point. Caught by looking at
    real output, not by a test that already existed."""
    group = grouping.group_by_cause([
        _finding("return_path", nets=["/A"], reference_nets=["GND"]),
        _finding("return_path", nets=["/B"], reference_nets=["GND"]),
    ])[0]
    lines = group.report().splitlines()
    assert not lines[0].startswith(" ")
    assert all(line.startswith("  ") for line in lines[1:] if line)


def test_the_whole_report_shows_every_finding():
    findings = _mixed_findings()
    text = grouping.report(findings)
    for finding in findings:
        assert finding.title in text


def test_an_empty_input_says_so_rather_than_printing_a_header():
    assert "no findings" in grouping.report([])


# -- the key is the cause --------------------------------------------------

def test_findings_sharing_a_reference_boundary_share_a_group():
    groups = {str(g.cause): g for g in
              grouping.group_by_cause(_mixed_findings())}
    assert len(groups["return_path — referenced to GND"].findings) == 2


def test_the_order_of_the_reference_nets_does_not_split_a_cause():
    """`['GND', '/VIN']` and `['/VIN', 'GND']` are the same plane boundary."""
    groups = {str(g.cause): g for g in
              grouping.group_by_cause(_mixed_findings())}
    assert len(groups["return_path — referenced to /VIN, GND"].findings) == 2


def test_a_dict_valued_cause_field_is_handled():
    """`reference_split` records `{net: mm}`, not a list. Grouping the raw
    values raises `unhashable type: dict`, which is how this was found."""
    groups = {str(g.cause): g for g in
              grouping.group_by_cause(_mixed_findings())}
    assert "reference_split — referenced to /VIN, GND" in groups


def test_a_check_with_no_cause_field_groups_under_itself():
    """The coarsest honest key. `crosstalk` records a victim and aggressors,
    neither of which is a shared cause, so the check is the whole key."""
    groups = {str(g.cause): g for g in
              grouping.group_by_cause(_mixed_findings())}
    assert len(groups["crosstalk"].findings) == 2


def test_layer_transitions_group_by_the_plane_that_is_missing():
    groups = {str(g.cause): g for g in
              grouping.group_by_cause(_mixed_findings())}
    assert "layer_transition — no plane over In1.Cu" in groups


# -- ordering and the headline --------------------------------------------

def test_the_worst_group_comes_first_and_the_largest_wins_a_tie():
    groups = grouping.group_by_cause(_mixed_findings())
    assert groups[0].cause.check == "thermal"        # the only critical
    majors = [g for g in groups if g.worst == "major"]
    assert [len(g.findings) for g in majors] == sorted(
        (len(g.findings) for g in majors), reverse=True)


def test_a_headline_counts_its_consequences_without_replacing_them():
    group = grouping.group_by_cause([
        _finding("return_path", severity="major", nets=["/A"],
                 reference_nets=["GND"]),
        _finding("return_path", severity="minor", nets=["/B"],
                 reference_nets=["GND"]),
    ])[0]
    headline = group.headline()
    assert "2 finding(s)" in headline
    assert "1 major, 1 minor" in headline
    assert "/A, /B" in headline
    assert group.worst == "major"


def test_many_nets_are_counted_rather_than_listed_in_the_headline():
    """A heading naming forty nets is not a heading. The nets are still on
    every finding underneath, which is where they belong."""
    group = grouping.group_by_cause([
        _finding("return_path", nets=[f"/N{n}"], reference_nets=["GND"])
        for n in range(40)])[0]
    assert "on 40 net(s)" in group.headline()
    assert "/N7" in group.report()


# -- the caller keeps its own verbosity choice ----------------------------

def test_severity_filtering_is_the_callers_choice_not_groupings():
    findings = _mixed_findings()
    text = grouping.report(findings, severities=("critical", "major"))
    assert "board_state" not in text
    assert "thermal" in text
