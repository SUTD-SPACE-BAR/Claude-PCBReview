"""SKILL.md §"What you MUST report", as code.

> Two Opus 5 sessions reviewed the same board with the same prompt and the same
> datasheet, and reported **different things**. The engine did not vary — same
> inputs give byte-identical findings, and that is tested. The *selection* did,
> because nothing here constrained it. — SKILL.md:384

What is written there is not "use better judgement". It is a mechanical
checklist, and this module is that checklist. Removing the model from selection
removes the model *and* closes the reproducibility hole the prose was failing to
patch — a smaller local model would open it wider.

The contract, restated so it cannot erode
-----------------------------------------
`generate` is a **pure function of two `AuditReport`s**. No I/O, no clock, no
randomness, no board. That is what makes "two runs on the same inputs are
identical" a testable claim rather than a hope. Everything it needs is already
in the reports: the engine's checks record their own coverage, and its findings
carry their own basis.

Judgement — a human's, or Model B's from Phase 6 — decides **order and prose**.
It never decides what appears, and it never touches a measurement, a severity,
a threshold or a demotion.
"""

from __future__ import annotations

import dataclasses

from pcbreview import findings as findings_module      # noqa: E402
from pcbreview import grouping as grouping_module      # noqa: E402

from pcbreview.report import coverage as coverage_module  # noqa: E402
from pcbreview.report.model import (                      # noqa: E402
    LAYOUT, SCHEMATIC, SEVERITY_ORDER, CoverageGap, EntryPoint, FindingRef,
    GroupRef, MeasuredGap, RenderPlan, Report,
)

#: Rule 1. Not configurable — "no exceptions, no summarising away".
SEVERE = ("critical", "major")

#: Rule 2's threshold, verbatim from SKILL.md:397.
GROUP_MIN = 5

#: Which severities the grouped view is built over. SKILL.md's own invocation
#: (line 431) passes `("critical", "major", "minor")`, and `--all` is
#: documented as not combining with `--grouped`. Info findings are not lost by
#: this: every one that matters is a coverage statement, and coverage is rules
#: 3 and 3b, which read it directly rather than through a group heading.
GROUP_SEVERITIES = ("critical", "major", "minor")

#: The states a schematic review can be in when there is no schematic report.
#: "not_run" is the dangerous one and therefore the default — a caller that
#: forgot to run the second entry point must not be able to produce a report
#: that stays silent about it.
SCHEMATIC_STATES = ("not_run", "not_supplied", "failed")

# The report re-states the engine's severity order so a serialised report can
# be read without the engine. If they ever diverge, every rank in here is
# wrong and silently so.
assert SEVERITY_ORDER == findings_module.SEVERITY_ORDER, (
    "model.SEVERITY_ORDER has drifted from findings.SEVERITY_ORDER")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _demotion(finding) -> tuple[str | None, str | None]:
    """What lowered this finding's severity, if anything.

    Two mechanisms, and they mean different things. `netclass` lowers because
    **the user declared the net unimportant** — the only thing permitted to
    lower a severity (engine rule 5). `panel` lowers because the net crosses a
    copper-free channel that is probably a break-off panel, which is an
    inference and is reversible if the channel turns out to be a routing error.
    Both keep the original, and the report says which happened.
    """
    measurement = finding.measurement or {}
    if not isinstance(measurement, dict):
        return None, None
    before = measurement.get("severity_before_netclass")
    if before:
        return before, "netclass"
    before = measurement.get("severity_before_partition")
    if before:
        return before, "partition"
    return None, None


def finding_ref(finding, source: str, *rules: str) -> FindingRef:
    """Project one engine `Finding` into the report's own shape.

    Public because the app's inspector shows the findings that name a net
    (`server/index/dossier.py`) and must show them in the *same* shape the
    report does. A second projection would drift, and the field it would drift
    on is the demotion trail — the one thing a reader has to see to know that a
    `minor` on screen was a `major` before a declaration lowered it.
    """
    demoted_from, demoted_by = _demotion(finding)
    return FindingRef(
        source=source, check=finding.check, severity=finding.severity,
        title=finding.title, score=float(finding.score),
        nets=tuple(finding.nets or ()), layers=tuple(finding.layers or ()),
        location=(tuple(finding.location) if finding.location else None),
        detail=tuple(finding.detail or ()), basis=finding.basis or "",
        assumption=finding.assumption, intent_needed=finding.intent_needed,
        measurement=finding.measurement or {},
        demoted_from=demoted_from, demoted_by=demoted_by, rules=tuple(rules))


def _merge_rules(refs: list[FindingRef]) -> list[FindingRef]:
    """One entry per finding, carrying every rule that selected it.

    A DRC error is both rule 1 and rule 4. Listing it twice would overstate the
    board and, worse, make the two rules look independent when a reader is
    checking that rule 4 fired.
    """
    merged: dict[tuple, FindingRef] = {}
    for ref in refs:
        key = (ref.source, ref.check, ref.title, ref.nets, ref.location)
        existing = merged.get(key)
        if existing is None:
            merged[key] = ref
            continue
        rules = tuple(dict.fromkeys(existing.rules + ref.rules))
        merged[key] = dataclasses.replace(existing, rules=rules)
    return sorted(merged.values(), key=lambda r: r.rank)


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def _rule_0(layout, schematic, state: str, note: str | None
            ) -> tuple[EntryPoint, ...]:
    """Both entry points, or an explicit statement that one did not run.

    `python -m pcbreview.audit board.kicad_pcb board.kicad_sch` enables
    `placement` and `parity`. It does **not** run the eight schematic checks.
    A session that ran only the layout audit reported a schematic as clean
    while `/SCL` and `/SDA` were each driven by two output pins.
    """
    def _ran(report) -> str:
        # "17 checks" hides a check that raised. `ran` is what matters, and
        # section 3 then names each one that did not.
        ran = sum(1 for c in report.checks if c.ran)
        return f"{ran} of {len(report.checks)} check(s) ran"

    entries = [EntryPoint(
        name="audit", state="reviewed", subject=layout.board,
        findings=len(layout.findings),
        statement=(f"the layout was reviewed: {_ran(layout)} "
                   f"over {layout.board}"))]

    if schematic is not None:
        entries.append(EntryPoint(
            name="schematic_audit", state="reviewed", subject=schematic.board,
            findings=len(schematic.findings),
            statement=(f"the schematic was reviewed: {_ran(schematic)} "
                       f"over {schematic.board}")))
        return tuple(entries)

    reasons = {
        "not_supplied": ("no schematic was supplied, so the schematic was NOT "
                         "reviewed. The eight schematic checks did not run and "
                         "nothing here says anything about the netlist"),
        "not_run": ("`pcbreview.schematic_audit` was NOT run, so the schematic "
                    "has NOT been reviewed — passing a schematic to the layout "
                    "audit does not run the schematic checks. Treat every "
                    "schematic-level defect as unexamined, not as absent"),
        "failed": ("the schematic review was attempted and failed, so the "
                   "schematic has NOT been reviewed"),
    }
    statement = reasons[state]
    entries.append(EntryPoint(name="schematic_audit", state=state,
                              statement=statement + (f" — {note}" if note
                                                     else "")))
    return tuple(entries)


def _rule_1(layout, schematic) -> list[FindingRef]:
    """Every `critical` and every `major`, from both entry points."""
    refs = [finding_ref(f, LAYOUT, "critical_major")
            for f in layout.findings if f.severity in SEVERE]
    if schematic is not None:
        refs += [finding_ref(f, SCHEMATIC, "critical_major")
                 for f in schematic.findings if f.severity in SEVERE]
    return refs


def _rule_2(layout, schematic) -> tuple[GroupRef, ...]:
    """Every group of five or more, by heading, with every member under it."""
    groups: list[GroupRef] = []
    for report, source in ((layout, LAYOUT), (schematic, SCHEMATIC)):
        if report is None:
            continue
        shown = [f for f in report.findings if f.severity in GROUP_SEVERITIES]
        for group in grouping_module.group_by_cause(shown):
            if len(group.findings) < GROUP_MIN:
                continue
            groups.append(GroupRef(
                source=source, check=group.cause.check,
                cause=str(group.cause), label=group.cause.label,
                shared=tuple(group.cause.shared), worst=group.worst,
                by_severity=dict(group.by_severity),
                nets=tuple(group.nets),
                members=tuple(sorted(
                    (finding_ref(f, source, "group") for f in group.findings),
                    key=lambda r: r.rank))))
    return tuple(sorted(groups, key=lambda g: g.rank))


def _rule_3(layout, schematic) -> tuple[CoverageGap, ...]:
    """Every check that could not run, never rendered as a pass."""
    gaps = coverage_module.coverage_gaps(layout, LAYOUT)
    if schematic is not None:
        gaps += coverage_module.coverage_gaps(schematic, SCHEMATIC)
    return tuple(sorted(gaps, key=lambda g: g.rank))


def _rule_3b(layout, schematic) -> tuple[MeasuredGap, ...]:
    """Every found/measured pair that disagrees, as "not measured"."""
    gaps = coverage_module.measured_gaps(layout, LAYOUT)
    if schematic is not None:
        gaps += coverage_module.measured_gaps(schematic, SCHEMATIC)
    return tuple(sorted(gaps, key=lambda g: g.rank))


def _rule_4(layout) -> list[FindingRef]:
    """Every DRC error.

    Keyed on `measurement["severity"] == "error"`, which is KiCad's own word
    for it, and **not** on the finding's severity. `_DRC_SEVERITY` maps an
    error to `major`, so rule 1 normally catches these — but a net the user
    declared unimportant has its findings capped by `netclass`, and a net
    crossing a break-off panel is moved to `info`. Neither of those makes a
    clearance violation stop being a clearance violation, and the rule says
    *every* DRC error with no qualifier.
    """
    refs = []
    for finding in layout.by_check("drc"):
        measurement = finding.measurement or {}
        if isinstance(measurement, dict) and measurement.get("severity") == "error":
            refs.append(finding_ref(finding, LAYOUT, "drc_error"))
    return refs


def _rule_5(layout, schematic) -> tuple[str, ...]:
    """Every assumption the run made, from both entry points."""
    assumptions = [f"[layout] {a}" for a in layout.assumptions]
    if schematic is not None:
        assumptions += [f"[schematic] {a}" for a in schematic.assumptions]
    return tuple(assumptions)


def _render_plan(groups: tuple[GroupRef, ...],
                 severe: list[FindingRef]) -> RenderPlan:
    """The largest group, plus every net a critical or major names.

    A floor, not a ceiling (SKILL.md:416). Selection only — nothing is drawn
    here.
    """
    # `max` returns the first maximal element, and `groups` arrives in a total
    # order, so a tie between two equally large groups resolves the same way
    # every run.
    largest = max(groups, key=lambda g: (g.count, -SEVERITY_ORDER.get(g.worst, 9)),
                  default=None)
    nets = sorted({net for ref in severe for net in ref.nets if net})
    return RenderPlan(
        largest_group=largest.cause if largest else None,
        largest_group_source=largest.source if largest else None,
        nets=tuple(nets))


def _counts(report) -> dict:
    counts: dict[str, int] = {}
    for finding in report.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def generate(layout, schematic=None, *, schematic_state: str = "not_run",
             schematic_note: str | None = None) -> Report:
    """Select everything that must be reported. Pure; no I/O, no clock.

    `layout` is `audit.audit_files()`'s report and `schematic` is
    `schematic_audit.audit_schematic_file()`'s. They check disjoint things, so
    both are needed for a complete review and the absence of the second is a
    *reported state*:

    * `not_supplied` — the project has no schematic;
    * `not_run` — one exists and the second entry point was not run. **The
      default**, because a caller that simply forgot must not get a report that
      is silent about it;
    * `failed` — it was attempted and raised; pass the reason as
      `schematic_note`.

    The report is given every severity including `info` and renders a subset,
    exactly as SKILL.md:422 requires of a reader: several of the most useful
    outputs are `info` — every coverage statement, the thermal shortlist, a
    differential pair that came back clean.
    """
    if schematic is None and schematic_state not in SCHEMATIC_STATES:
        raise ValueError(
            f"schematic_state must be one of {SCHEMATIC_STATES}, "
            f"not {schematic_state!r}")

    entry_points = _rule_0(layout, schematic, schematic_state, schematic_note)
    severe = _rule_1(layout, schematic)
    groups = _rule_2(layout, schematic)
    could_not_run = _rule_3(layout, schematic)
    not_measured = _rule_3b(layout, schematic)
    drc_errors = _rule_4(layout)
    assumptions = _rule_5(layout, schematic)

    selected = _merge_rules(severe + drc_errors)

    counts = {LAYOUT: _counts(layout)}
    if schematic is not None:
        counts[SCHEMATIC] = _counts(schematic)

    return Report(
        board=layout.board,
        entry_points=entry_points,
        severe=tuple(selected),
        groups=groups,
        could_not_run=could_not_run,
        not_measured=not_measured,
        # The same objects as in `severe`, not a second projection of them, so
        # a DRC error carries both the rules that selected it in both places.
        drc_errors=tuple(r for r in selected if "drc_error" in r.rules),
        assumptions=assumptions,
        render_plan=_render_plan(groups, severe),
        counts=counts,
        rule_trace={
            "0_entry_points": len([e for e in entry_points if e.reviewed]),
            "1_critical_major": len(severe),
            "2_groups": len(groups),
            "3_could_not_run": len(could_not_run),
            "3b_not_measured": len(not_measured),
            "4_drc_errors": len(drc_errors),
            "5_assumptions": len(assumptions),
        },
        artefacts=dict(layout.artefacts or {}),
    )
