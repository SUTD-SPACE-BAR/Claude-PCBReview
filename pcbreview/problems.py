"""The problems panel — every finding, grouped the way the engine groups them.

the design brief §2.4:

> **Problems panel (left).** The DRC-style list you asked for. Grouped by
> shared cause by default — `grouping.py` already does this exactly, and takes
> 306 findings to 33 headings with nothing collapsed away. Toggle to flat or
> by-check. Click a row → canvas zooms and highlights. **Nothing in this panel
> is model-generated.**

Two things separate this from `server/report/`, and confusing them would undo
Phase 1.

**The report is a selection; this is the whole list.** `generate.py` answers
"what must be reported", by rule, and drops nothing that qualifies. This panel
answers "what did the run find", and drops nothing at all — including the
`info` findings, which are where every coverage statement lives. A user
browsing their board is entitled to all of it; a report that showed all of it
would be the 306-item wall the grouping exists to fix.

**Absence appears here too.** Principle 5 of §2.10 — *absence is never shown as
a pass* — is a UI rule, not only a report rule. A panel listing 204 `info`
findings and staying quiet about the seven checks that examined nothing would
be the most persuasive "this board is fine" the app could produce. So rules 3
and 3b come through from the report, at the top, in their own section.
"""

from __future__ import annotations

from pcbreview import grouping as grouping_module             # noqa: E402

from pcbreview.report.generate import GROUP_SEVERITIES, finding_ref  # noqa: E402
from pcbreview.report.model import LAYOUT, SCHEMATIC, SEVERITY_ORDER  # noqa: E402

#: Ordered worst-first, so a UI can iterate without re-sorting.
SEVERITIES = ("critical", "major", "minor", "info")


def _entry(ref, ident: str, finding=None) -> dict:
    measurement = getattr(finding, "measurement", None) or {}
    return {
        "id": ident,
        "source": ref.source, "check": ref.check, "severity": ref.severity,
        "title": ref.title, "score": round(ref.score, 2),
        "nets": list(ref.nets), "layers": list(ref.layers),
        "location": list(ref.location) if ref.location else None,
        "detail": list(ref.detail), "basis": ref.basis,
        "assumption": ref.assumption, "intent_needed": ref.intent_needed,
        "demoted_from": ref.demoted_from, "demoted_by": ref.demoted_by,
        # The user's severity policy, if one touched this finding. Carried so
        # the panel can keep an ignored finding out of the ranked list and
        # still say how many it is holding — never so it can drop one.
        "ignored_by_policy": bool(measurement.get("ignored_by_policy")),
        "severity_before_policy": measurement.get("severity_before_policy"),
    }


def build(layout_report, schematic_report, report) -> dict:
    """The whole panel model. A pure function of the two audits and the report.

    `report` is the deterministic `Report`; it contributes only the coverage
    sections, which it has already computed correctly and which nothing here
    should compute a second time.
    """
    findings: list[dict] = []
    counts: dict[str, dict[str, int]] = {}

    pairs = [(layout_report, LAYOUT)]
    if schematic_report is not None:
        pairs.append((schematic_report, SCHEMATIC))

    for source_report, source in pairs:
        bucket = counts.setdefault(source, {s: 0 for s in SEVERITIES})
        for position, finding in enumerate(source_report.findings):
            ref = finding_ref(finding, source)
            findings.append(_entry(ref, f"{source[0]}{position}", finding))
            bucket[finding.severity] = bucket.get(finding.severity, 0) + 1

    findings.sort(key=lambda e: (SEVERITY_ORDER.get(e["severity"], 9),
                                 -e["score"], e["source"], e["check"],
                                 e["title"]))

    return {
        "findings": findings,
        "counts": counts,
        "groups": _groups(pairs),
        "by_check": _by_check(pairs),
        "coverage": _coverage(report),
        "total": len(findings),
        # How many the user's policy is holding out of the ranking. Stated so
        # the panel can say "12 ignored by your policy" rather than quietly
        # showing a shorter list — §2.10 principle 5 again.
        "ignored_by_policy": sum(1 for f in findings if f["ignored_by_policy"]),
    }


def _groups(pairs) -> list[dict]:
    """Findings under their shared cause. Every member, never a subset.

    `grouping.py`'s own constraint is that no information is lost or hidden, so
    a heading here is a heading over a complete list and the count beside it is
    the real one.

    Built over `critical`/`major`/`minor` — `GROUP_SEVERITIES`, the same set
    the report groups over. `info` is excluded for the reason stated there: an
    `info` finding is overwhelmingly a coverage statement, and coverage has its
    own section where it is not one line among two hundred.
    """
    out: list[dict] = []
    for source_report, source in pairs:
        shown = [f for f in source_report.findings
                 if f.severity in GROUP_SEVERITIES]
        for position, group in enumerate(grouping_module.group_by_cause(shown)):
            out.append({
                "id": f"g{source[0]}{position}",
                "source": source, "check": group.cause.check,
                "cause": str(group.cause), "label": group.cause.label,
                "shared": list(group.cause.shared),
                "worst": group.worst, "count": len(group.findings),
                "by_severity": dict(group.by_severity),
                "nets": list(group.nets),
                "members": [_entry(finding_ref(f, source), f"{source[0]}g{i}")
                            for i, f in enumerate(
                                sorted(group.findings,
                                       key=lambda f: f.rank))],
            })
    out.sort(key=lambda g: (SEVERITY_ORDER.get(g["worst"], 9), -g["count"],
                            g["source"], g["cause"]))
    return out


def _by_check(pairs) -> list[dict]:
    """The third view: one row per check, worst finding first."""
    checks: dict[tuple, dict] = {}
    for source_report, source in pairs:
        for finding in source_report.findings:
            key = (source, finding.check)
            entry = checks.setdefault(key, {
                "source": source, "check": finding.check, "count": 0,
                "worst": "info", "by_severity": {}})
            entry["count"] += 1
            entry["by_severity"][finding.severity] = (
                entry["by_severity"].get(finding.severity, 0) + 1)
            if (SEVERITY_ORDER.get(finding.severity, 9)
                    < SEVERITY_ORDER.get(entry["worst"], 9)):
                entry["worst"] = finding.severity
    return sorted(checks.values(),
                  key=lambda e: (SEVERITY_ORDER.get(e["worst"], 9),
                                 -e["count"], e["source"], e["check"]))


def _coverage(report) -> dict:
    """Rules 3 and 3b, carried into the panel.

    Straight from the report rather than recomputed. `coverage.py` reads free
    text to find these and has two guards in it that were each a real false
    positive first; a second reading of the same notes would need the same two
    guards and would be where they drifted apart.
    """
    return {
        "could_not_run": [
            {"source": gap.source, "check": gap.check, "reason": gap.reason,
             "statement": gap.statement, "note": gap.note,
             "findings_anyway": gap.findings_anyway}
            for gap in report.could_not_run],
        "not_measured": [
            {"source": gap.source, "check": gap.check, "found": gap.found,
             "measured": gap.measured, "unmeasured": gap.unmeasured,
             "noun": gap.noun, "means": gap.means, "detail": list(gap.detail)}
            for gap in report.not_measured],
    }
