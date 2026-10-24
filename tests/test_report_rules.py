"""Each rule of SKILL.md §"What you MUST report", asserted on its own.

Built from synthetic `AuditReport`s rather than from boards, so that a rule can
be tested against the case that would break it — a demoted DRC error, a
non-zero coverage count that means "did not look" — instead of against whatever
the example boards happen to contain. The boards are the regression suite;
these are the specification.
"""

from __future__ import annotations

import pytest

from pcbreview.findings import AuditReport, CheckResult, Finding  # noqa: E402

from pcbreview.report import LAYOUT, SCHEMATIC, generate             # noqa: E402
from pcbreview.report import render                                  # noqa: E402


def finding(check="return_path", severity="major", title="t", score=1.0,
            **kwargs) -> Finding:
    return Finding(check=check, title=title, severity=severity, score=score,
                   **kwargs)


def report(findings=(), checks=(), assumptions=(), board="B") -> AuditReport:
    return AuditReport(board=board, board_summary={}, stackup="s",
                       findings=list(findings), checks=list(checks),
                       assumptions=list(assumptions))


# ---------------------------------------------------------------------------
# Rule 0 — both entry points
# ---------------------------------------------------------------------------

def test_missing_schematic_review_is_stated_not_omitted():
    result = generate(report())
    schematic = [e for e in result.entry_points
                 if e.name == "schematic_audit"][0]
    assert not schematic.reviewed
    assert "NOT" in schematic.statement
    assert not result.schematic_reviewed


def test_not_run_is_the_default_state():
    """A caller who forgot the second entry point must not get silence."""
    result = generate(report())
    schematic = [e for e in result.entry_points
                 if e.name == "schematic_audit"][0]
    assert schematic.state == "not_run"
    assert "does not run the schematic checks" in schematic.statement


def test_not_supplied_and_not_run_are_different_statements():
    absent = generate(report(), schematic_state="not_supplied")
    forgot = generate(report(), schematic_state="not_run")
    assert (absent.entry_points[1].statement
            != forgot.entry_points[1].statement)


def test_an_unknown_schematic_state_is_refused():
    with pytest.raises(ValueError):
        generate(report(), schematic_state="fine")


def test_a_reviewed_schematic_contributes_its_findings():
    layout = report([finding(check="drc", severity="minor")])
    schematic = report([finding(check="output_conflict", severity="major",
                                title="two output pins drive /SCL")])
    result = generate(layout, schematic)
    assert result.schematic_reviewed
    assert [r.source for r in result.severe] == [SCHEMATIC]


# ---------------------------------------------------------------------------
# Rule 1 — every critical and every major
# ---------------------------------------------------------------------------

def test_every_critical_and_major_appears():
    layout = report([
        finding(severity="critical", title="c"),
        finding(severity="major", title="m"),
        finding(severity="minor", title="n"),
        finding(severity="info", title="i"),
    ])
    titles = [r.title for r in generate(layout).severe]
    assert titles == ["c", "m"]


def test_selection_is_independent_of_input_order():
    findings = [finding(severity="major", title=t, score=s)
                for t, s in (("a", 1.0), ("b", 1.0), ("c", 2.0))]
    forward = generate(report(findings)).severe
    backward = generate(report(list(reversed(findings)))).severe
    assert [r.title for r in forward] == [r.title for r in backward]


def test_a_demotion_is_carried_not_hidden():
    demoted = finding(severity="minor", title="capped",
                      measurement={"severity_before_netclass": "major"})
    result = generate(report([demoted]))
    # It is a minor now, so rule 1 does not select it — only a user declaration
    # may lower a severity, and that is the user's call. But when it *is*
    # rendered, the trail comes with it.
    assert not result.severe
    kept = finding(severity="major", title="kept",
                   measurement={"severity_before_partition": "critical"})
    ref = generate(report([kept])).severe[0]
    assert (ref.demoted_from, ref.demoted_by) == ("critical", "partition")


# ---------------------------------------------------------------------------
# Rule 2 — every group of five or more
# ---------------------------------------------------------------------------

def test_groups_of_five_or_more_only():
    four = [finding(check="conductor_neck", title=f"n{i}", severity="minor")
            for i in range(4)]
    five = [finding(check="return_path", title=f"r{i}", severity="minor",
                    measurement={"reference_nets": {"GND": 1.0}})
            for i in range(5)]
    result = generate(report(four + five))
    assert len(result.groups) == 1
    assert result.groups[0].check == "return_path"


def test_a_group_lists_every_member():
    findings = [finding(check="return_path", title=f"r{i}", severity="minor",
                        measurement={"reference_nets": {"GND": 1.0}})
                for i in range(9)]
    group = generate(report(findings)).groups[0]
    assert group.count == 9
    assert len(group.members) == 9
    assert {m.title for m in group.members} == {f"r{i}" for i in range(9)}


def test_info_findings_do_not_form_groups():
    """Coverage statements are `info` and are rules 3 and 3b, not headings."""
    findings = [finding(check="placement", title=f"i{i}", severity="info")
                for i in range(9)]
    assert generate(report(findings)).groups == ()


# ---------------------------------------------------------------------------
# Rule 3 — a check that could not run is never a pass
# ---------------------------------------------------------------------------

def test_a_skipped_check_is_reported():
    checks = [CheckResult("parity", ran=False,
                          skipped_because="no schematic given")]
    gap = generate(report(checks=checks)).could_not_run[0]
    assert (gap.check, gap.reason) == ("parity", "skipped")


def test_zero_examined_is_reported_even_though_the_check_ran():
    checks = [CheckResult("impedance", ran=True, findings=0,
                          note="1 fab target(s), 0 net(s) examined")]
    gap = generate(report(checks=checks)).could_not_run[0]
    assert gap.reason == "zero_population"
    assert gap.statement == "0 net(s) examined"


def test_a_clean_drc_is_not_a_blind_check():
    """The inverse failure: a real pass reported as "could not run"."""
    checks = [CheckResult("drc", ran=True, findings=0,
                          note="0 violation(s): 0 error, 0 warning")]
    assert generate(report(checks=checks)).could_not_run == ()


def test_a_missing_input_is_reported_once_not_per_clause():
    checks = [CheckResult("heat_path", ran=True, findings=0,
                          note="no thermal data supplied, so no part is "
                               "known to dissipate")]
    gaps = generate(report(checks=checks)).could_not_run
    assert len(gaps) == 1
    assert gaps[0].reason == "no_input"


def test_a_parameter_in_a_note_is_not_a_population():
    checks = [CheckResult("layer_transition", ran=True, findings=3,
                          note="stitch limit 14.3 mm at 1000 ps")]
    assert generate(report(checks=checks)).could_not_run == ()


def test_findings_produced_anyway_are_recorded():
    """`current_capacity` reports absolute ampacity with zero nets comparable."""
    checks = [CheckResult("current_capacity", ran=True, findings=18,
                          note="0 power net(s) had enough terminals; see the "
                               "check's own coverage finding")]
    gap = generate(report(checks=checks)).could_not_run[0]
    assert gap.findings_anyway == 18


# ---------------------------------------------------------------------------
# Rule 3b — found, then not measured
# ---------------------------------------------------------------------------

def test_a_non_zero_count_that_measured_nothing_is_reported():
    """The motivating failure: `esd_clamps: 11`, no findings, 13 of 14 lines
    never compared."""
    coverage = finding(check="placement", severity="info", score=0.0,
                       title="coverage",
                       measurement={"coverage": {"esd_clamps": 11,
                                                 "esd_clamps_measured": 1}})
    gap = generate(report([coverage])).not_measured[0]
    assert (gap.found, gap.measured, gap.unmeasured) == (11, 1, 10)
    assert gap.found_key == "esd_clamps"


def test_a_pair_that_agrees_is_not_reported():
    coverage = finding(check="placement", severity="info", score=0.0,
                       measurement={"coverage": {"crystals": 2,
                                                 "crystal_loops_measured": 2}})
    assert generate(report([coverage])).not_measured == ()


def test_the_decoupling_pair_the_engine_does_not_emit_is_caught():
    """`audit.check_placement`'s MEASURED_PAIRS omits `decoupling_loops`, and
    that is the check the module exists for."""
    coverage = finding(check="placement", severity="info", score=0.0,
                       measurement={"coverage": {
                           "decoupling_loops": 12,
                           "decoupling_loops_measured": 0}})
    gap = generate(report([coverage])).not_measured[0]
    assert gap.found_key == "decoupling_loops"
    assert gap.unmeasured == 12


def test_a_float_coverage_field_is_not_read_as_a_census():
    """`return_path` writes a float to `measurement["coverage"]`."""
    trace = finding(check="return_path", severity="major",
                    measurement={"coverage": 0.0, "length_mm": 23.7})
    assert generate(report([trace])).not_measured == ()


def test_current_capacity_compares_a_list_against_a_count():
    coverage = finding(check="current_capacity", severity="info", score=0.0,
                       measurement={"coverage": {
                           "nets_examined": ["+BATT", "GND", "+3V3", "+5V",
                                             "VBUS"],
                           "nets_compared": 1,
                           "nets_declined": {"GND": "no comparable feeds"}}})
    gap = generate(report([coverage])).not_measured[0]
    assert (gap.found, gap.measured) == (5, 1)
    assert any("GND" in line for line in gap.detail)


# ---------------------------------------------------------------------------
# Rule 4 — every DRC error
# ---------------------------------------------------------------------------

def test_a_drc_error_demoted_below_major_is_still_reported():
    """A user declaring a net unimportant caps its findings. It does not make
    a clearance violation stop being one."""
    demoted = finding(check="drc", severity="minor", title="clearance",
                      measurement={"type": "clearance", "severity": "error",
                                   "severity_before_netclass": "major"})
    result = generate(report([demoted]))
    assert [r.title for r in result.drc_errors] == ["clearance"]
    assert [r.title for r in result.severe] == ["clearance"]


def test_a_drc_warning_is_not_an_error():
    warning = finding(check="drc", severity="minor", title="silk",
                      measurement={"type": "silk", "severity": "warning"})
    assert generate(report([warning])).drc_errors == ()


def test_a_drc_error_is_selected_once_by_two_rules():
    error = finding(check="drc", severity="major", title="clearance",
                    measurement={"type": "clearance", "severity": "error"})
    result = generate(report([error]))
    assert len(result.severe) == 1
    assert set(result.severe[0].rules) == {"critical_major", "drc_error"}


# ---------------------------------------------------------------------------
# Rule 5 — every assumption
# ---------------------------------------------------------------------------

def test_assumptions_from_both_entry_points_are_tagged():
    result = generate(report(assumptions=["edge rate unknown"]),
                      report(assumptions=["no board given"]))
    assert result.assumptions == ("[layout] edge rate unknown",
                                  "[schematic] no board given")


# ---------------------------------------------------------------------------
# The render floor, and purity
# ---------------------------------------------------------------------------

def test_the_render_floor_is_the_largest_group_plus_severe_nets():
    groups = [finding(check="return_path", title=f"r{i}", severity="minor",
                      measurement={"reference_nets": {"GND": 1.0}})
              for i in range(7)]
    severe = [finding(severity="critical", title="c", nets=["+BATT"],
                      check="conductor_neck")]
    plan = generate(report(groups + severe)).render_plan
    assert plan.nets == ("+BATT",)
    assert "return_path" in plan.largest_group


def test_two_runs_over_the_same_input_are_identical():
    layout = report(
        [finding(severity="critical", title="c", nets=["+BATT"]),
         finding(check="drc", severity="major", title="d",
                 measurement={"severity": "error", "type": "clearance"})],
        checks=[CheckResult("impedance", ran=True,
                            note="1 fab target(s), 0 net(s) examined")],
        assumptions=["a"])
    first, second = generate(layout), generate(layout)
    assert first == second
    assert render.text(first) == render.text(second)
    assert render.to_json(first) == render.to_json(second)


def test_every_rule_is_traced_even_when_it_finds_nothing():
    trace = generate(report()).rule_trace
    assert set(trace) == {"0_entry_points", "1_critical_major", "2_groups",
                          "3_could_not_run", "3b_not_measured",
                          "4_drc_errors", "5_assumptions"}


def test_the_drc_section_counts_violations_not_just_rule_types():
    """One finding per rule type, with every instance under it: 14 board-edge
    clearance violations are one decision and fourteen problems."""
    error = finding(check="drc", severity="major", title="edge clearance",
                    measurement={"type": "copper_edge_clearance",
                                 "severity": "error", "count": 14})
    out = render.text(generate(report([error])))
    assert "1 rule type(s), 14 violation(s)" in out


def test_empty_sections_still_render():
    """A missing section is indistinguishable from a rule that never ran."""
    out = render.text(generate(report()))
    for heading in ("1. EVERY CRITICAL AND MAJOR", "2. EVERY GROUP",
                    "3. CHECKS THAT COULD NOT RUN", "3b. FOUND BUT NOT "
                    "MEASURED", "4. EVERY DRC ERROR", "5. EVERY ASSUMPTION"):
        assert heading in out
