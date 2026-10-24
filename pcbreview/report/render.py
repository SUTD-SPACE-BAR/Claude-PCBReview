"""A `Report` as text, as Markdown, and as JSON.

Three renderings of **one selection**. Nothing in here decides what appears —
if a renderer can drop an item, the contract in `generate.py` is already broken.
That is why every section prints even when it is empty: "no check was blind" is
a result, and a missing section is indistinguishable from a rule that never ran.

The text renderer emits `Ω`, `≤` and friends because the findings do. Callers
that print to a Windows console must call `pcbreview.console_utf8()` first —
`cli.py` does. This module never touches a stream.
"""

from __future__ import annotations

import dataclasses
import json

from pcbreview.report.generate import GROUP_MIN
from pcbreview.report.model import SEVERITY_ORDER, Report

_BAR = "=" * 74
_RULE = "-" * 74


def _severity_tag(severity: str) -> str:
    return f"[{severity.upper():8s}]"


def _drc_violations(report: Report) -> int:
    """Instances, not rule types. `measurement["count"]` per finding."""
    return sum(int((ref.measurement or {}).get("count", 0) or 0)
               for ref in report.drc_errors)


def _finding_lines(ref, indent: str = "") -> list[str]:
    lines = [f"{indent}{_severity_tag(ref.severity)} {ref.title}"]
    pad = f"{indent}             "
    for line in ref.detail:
        lines.append(f"{pad}{line}")
    if ref.location:
        where = f"{pad}at ({ref.location[0]:.2f}, {ref.location[1]:.2f})"
        if ref.layers:
            where += f" on {', '.join(ref.layers)}"
        lines.append(where)
    if ref.demoted_from:
        lines.append(f"{pad}severity lowered from {ref.demoted_from} by "
                     f"{ref.demoted_by} — the measurement is unchanged")
    if ref.basis:
        lines.append(f"{pad}basis: {ref.basis}")
    if ref.assumption:
        lines.append(f"{pad}assumed: {ref.assumption}")
    if ref.intent_needed:
        lines.append(f"{pad}intent: {ref.intent_needed}")
    lines.append(f"{pad}selected by: {', '.join(ref.rules)}  "
                 f"[{ref.source}/{ref.check}]")
    return lines


def _contents(report: Report) -> list[str]:
    """An index over the sections, printed before them.

    **Not a summary and never a substitute.** Every section below still prints
    in full; this says how much of each there is, so a reader who has just
    opened a 4 000-line file knows what it holds before scrolling it.

    It earns its place on dense boards. One four-layer board selects 278
    findings into section 1 and 11 groups into section 2 — those 278 are 11
    facts repeated, which the index makes visible in six lines and the sections
    themselves take two thousand to say.
    """
    counts = [
        ("0", "what was reviewed", len(report.entry_points), "entry point(s)"),
        ("1", "every critical and major", len(report.severe), "finding(s)"),
        ("2", f"every group of {GROUP_MIN} or more", len(report.groups),
         "group(s)"),
        ("3", "checks that could not run", len(report.could_not_run),
         "check(s)"),
        ("3b", "found but not measured", len(report.not_measured), "check(s)"),
        ("4", "every DRC error", len(report.drc_errors),
         f"rule type(s), {_drc_violations(report)} violation(s)"),
        ("5", "every assumption", len(report.assumptions), "assumption(s)"),
    ]
    out = ["CONTENTS"]
    for number, title, count, noun in counts:
        out.append(f"  {number:3s} {title:32s} {count:5d}  {noun}")
    out.append(f"  {'':3s} {'render floor':32s} "
               f"{len(report.render_plan.nets):5d}  net(s), plus the largest "
               f"group")
    out.append("")
    if len(report.severe) > 40 and report.groups:
        # The one navigation hint worth printing, and only when it applies.
        out.append("  Section 1 is long. Section 2 is the same findings under "
                   "the causes they")
        out.append("  share — read that first, then grep section 1 for the "
                   "cause you are chasing.")
        out.append("")
    return out


def text(report: Report) -> str:
    """The report as console text, in rule order.

    Rule order, not importance order. Importance is judgement's job and this
    is the artefact of record — the thing that must be the same on Tuesday as
    it was on Monday.
    """
    out: list[str] = [_BAR,
                      f"DETERMINISTIC REVIEW — {report.board}",
                      _BAR, ""]
    out += _contents(report)

    out.append("0. WHAT WAS REVIEWED")
    for entry in report.entry_points:
        mark = "OK  " if entry.reviewed else "NOT "
        out.append(f"  {mark} {entry.name}: {entry.statement}")
    for source, counts in sorted(report.counts.items()):
        total = sum(counts.values())
        by = ", ".join(f"{counts[s]} {s}" for s in sorted(
            counts, key=lambda k: SEVERITY_ORDER.get(k, 9)))
        out.append(f"       {source}: {total} finding(s) — {by}")
    out += ["", _RULE]

    out.append(f"1. EVERY CRITICAL AND MAJOR — {len(report.severe)}")
    out.append("   Reported in full, with no exception. A finding you think is "
               "insignificant")
    out.append("   is reported and said to be insignificant; it is not removed.")
    out.append("")
    if not report.severe:
        out.append("   none at these severities")
    for ref in report.severe:
        out += _finding_lines(ref, "   ")
        out.append("")
    out.append(_RULE)

    out.append(f"2. EVERY GROUP OF {GROUP_MIN} OR MORE — "
               f"{len(report.groups)} group(s)")
    out.append("   Each heading is followed by EVERY finding under it. Nothing "
               "is collapsed.")
    out.append("")
    if not report.groups:
        out.append("   no cause produced five or more findings")
    for group in report.groups:
        by = ", ".join(f"{c} {s}" for s, c in sorted(
            group.by_severity.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9)))
        nets = (f", on {len(group.nets)} net(s)" if len(group.nets) > 3
                else f", on {', '.join(group.nets)}" if group.nets else "")
        out.append(f"   === {_severity_tag(group.worst)} {group.cause} — "
                   f"{group.count} finding(s) ({by}){nets}  [{group.source}]")
        for ref in group.members:
            out += _finding_lines(ref, "     ")
        out.append("")
    out.append(_RULE)

    out.append(f"3. CHECKS THAT COULD NOT RUN — {len(report.could_not_run)}")
    out.append("   NOT a pass. A check with nothing to look at found nothing "
               "because it")
    out.append("   looked at nothing.")
    out.append("")
    if not report.could_not_run:
        out.append("   every check had something to examine")
    for gap in report.could_not_run:
        out.append(f"   [{gap.reason:15s}] {gap.source}/{gap.check}: "
                   f"{gap.statement}")
        if gap.findings_anyway:
            out.append(f"                     it still produced "
                       f"{gap.findings_anyway} finding(s) from what it COULD "
                       f"examine")
        if gap.note and gap.note.strip() != gap.statement:
            out.append(f"                     note: {gap.note}")
    out += ["", _RULE]

    out.append(f"3b. FOUND BUT NOT MEASURED — {len(report.not_measured)}")
    out.append("   A non-zero count is not evidence a check ran, and reads as "
               "evidence.")
    out.append("")
    if not report.not_measured:
        out.append("   every candidate any check counted was also measured")
    for gap in report.not_measured:
        out.append(f"   {gap.source}/{gap.check}: {gap.unmeasured} of "
                   f"{gap.found} {gap.noun} were found but NOT measured "
                   f"({gap.measured} were)")
        out.append(f"       measured would have meant: {gap.means}")
        for line in gap.detail:
            out.append(f"       {line}")
    out += ["", _RULE]

    # `check_drc` emits one finding per rule *type* with every instance under
    # it — 14 board-edge clearance violations are one decision, not fourteen —
    # so a bare count of findings understates the board by an order of
    # magnitude. Both numbers, always.
    out.append(f"4. EVERY DRC ERROR — {len(report.drc_errors)} rule type(s), "
               f"{_drc_violations(report)} violation(s)")
    out.append("   KiCad's own rule engine, reported whatever severity "
               "pcbreview gave it.")
    out.append("")
    if not report.drc_errors:
        out.append("   no DRC errors — check section 3 for whether the DRC ran "
                   "at all")
    for ref in report.drc_errors:
        out += _finding_lines(ref, "   ")
        out.append("")
    out.append(_RULE)

    out.append(f"5. EVERY ASSUMPTION THIS RUN MADE — {len(report.assumptions)}")
    out.append("")
    for assumption in report.assumptions:
        out.append(f"   - {assumption}")
    out += ["", _RULE]

    plan = report.render_plan
    out.append("RENDER FLOOR — the largest group, plus every net a critical or "
               "major names")
    out.append(f"   largest group : {plan.largest_group or '(none)'}"
               + (f"  [{plan.largest_group_source}]"
                  if plan.largest_group_source else ""))
    out.append(f"   nets ({len(plan.nets)}) : "
               + (", ".join(plan.nets) if plan.nets else "(none)"))
    out += ["", _RULE, "rule trace: " + ", ".join(
        f"{k}={v}" for k, v in sorted(report.rule_trace.items()))]
    if report.artefacts:
        out.append("artefacts: " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.artefacts.items())))
    return "\n".join(out)


def _cell(value) -> str:
    """One table cell. Engine text is quoted verbatim, so it must be escaped.

    A finding's title can hold a KiCad net name, and a rule description can
    hold a constraint expression. Either may contain a `|`, which silently
    splits a Markdown row and shifts every column after it.
    """
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown(report: Report) -> str:
    """The same selection as Markdown, for export and for the Report tab."""
    out: list[str] = [f"# Deterministic review — {report.board}", ""]

    out.append("## Contents")
    out.append("")
    out.append("| section | count |")
    out.append("|---|---|")
    for label, count in (
            ("0. What was reviewed", len(report.entry_points)),
            ("1. Every critical and major", len(report.severe)),
            (f"2. Every group of {GROUP_MIN} or more", len(report.groups)),
            ("3. Checks that could not run", len(report.could_not_run)),
            ("3b. Found but not measured", len(report.not_measured)),
            ("4. Every DRC error", len(report.drc_errors)),
            ("5. Every assumption", len(report.assumptions))):
        out.append(f"| {label} | {count} |")
    out.append("")

    out.append("## 0. What was reviewed")
    out.append("")
    out.append("| entry point | state | statement |")
    out.append("|---|---|---|")
    for entry in report.entry_points:
        out.append(f"| `{entry.name}` | **{entry.state}** | "
                   f"{_cell(entry.statement)} |")
    out.append("")

    out.append(f"## 1. Every critical and major ({len(report.severe)})")
    out.append("")
    if not report.severe:
        out.append("*None at these severities.*")
    for ref in report.severe:
        out.append(f"### `{ref.severity}` — {ref.title}")
        out.append("")
        out.append(f"- check: `{ref.source}/{ref.check}`"
                   + (f", nets: {', '.join(f'`{n}`' for n in ref.nets)}"
                      if ref.nets else ""))
        for line in ref.detail:
            out.append(f"- {line}")
        if ref.demoted_from:
            out.append(f"- **severity lowered from `{ref.demoted_from}` by "
                       f"{ref.demoted_by}**; the measurement is unchanged")
        if ref.basis:
            out.append(f"- basis: {ref.basis}")
        if ref.assumption:
            out.append(f"- assumed: {ref.assumption}")
        if ref.intent_needed:
            out.append(f"- intent needed: {ref.intent_needed}")
        out.append("")

    out.append(f"## 2. Every group of {GROUP_MIN} or more "
               f"({len(report.groups)})")
    out.append("")
    if not report.groups:
        out.append("*No cause produced five or more findings.*")
    for group in report.groups:
        out.append(f"### {group.cause} — {group.count} finding(s), "
                   f"worst `{group.worst}`")
        out.append("")
        for ref in group.members:
            out.append(f"- `{ref.severity}` {ref.title}")
        out.append("")

    out.append(f"## 3. Checks that could not run ({len(report.could_not_run)})")
    out.append("")
    out.append("**Not a pass.**")
    out.append("")
    if not report.could_not_run:
        out.append("*Every check had something to examine.*")
    else:
        out.append("| check | why | the engine's words |")
        out.append("|---|---|---|")
        for gap in report.could_not_run:
            out.append(f"| `{gap.source}/{gap.check}` | {gap.reason} | "
                       f"{_cell(gap.statement)} |")
    out.append("")

    out.append(f"## 3b. Found but not measured ({len(report.not_measured)})")
    out.append("")
    if not report.not_measured:
        out.append("*Every candidate any check counted was also measured.*")
    else:
        out.append("| check | found | measured | what was not measured |")
        out.append("|---|---|---|---|")
        for gap in report.not_measured:
            out.append(f"| `{gap.source}/{gap.check}` | {gap.found} "
                       f"{_cell(gap.noun)} | {gap.measured} | "
                       f"{_cell(gap.means)} |")
    out.append("")

    out.append(f"## 4. Every DRC error — {len(report.drc_errors)} rule "
               f"type(s), {_drc_violations(report)} violation(s)")
    out.append("")
    if not report.drc_errors:
        out.append("*None — see section 3 for whether the DRC ran at all.*")
    for ref in report.drc_errors:
        out.append(f"- **{ref.title}** — "
                   + "; ".join(ref.detail[:2] or ("",)))
    out.append("")

    out.append(f"## 5. Every assumption ({len(report.assumptions)})")
    out.append("")
    for assumption in report.assumptions:
        out.append(f"- {assumption}")
    out.append("")

    plan = report.render_plan
    out.append("## Render floor")
    out.append("")
    out.append(f"- largest group: {plan.largest_group or '(none)'}")
    out.append(f"- nets named by a critical or major ({len(plan.nets)}): "
               + (", ".join(f"`{n}`" for n in plan.nets) or "(none)"))
    return "\n".join(out)


def as_dict(report: Report) -> dict:
    """The whole structure, JSON-ready. The app and the MCP surface read this."""
    return dataclasses.asdict(report)


def to_json(report: Report, **kwargs) -> str:
    """Canonical JSON — sorted keys, so two runs diff to nothing."""
    kwargs.setdefault("sort_keys", True)
    kwargs.setdefault("indent", 2)
    kwargs.setdefault("default", str)
    return json.dumps(as_dict(report), **kwargs)
