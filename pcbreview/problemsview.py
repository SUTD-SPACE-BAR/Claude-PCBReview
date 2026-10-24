"""`problems.build()` as console text — the fix list, by shared cause.

The report is the artefact of record and its order is rule order, which is the
right order for *"is this the same review as yesterday"* and the wrong one for
*"what do I change first"*. This is the second shape: every finding under the
cause it shares with the others, worst first, so a heading is one edit rather
than twenty-seven.

It is a renderer. `grouping.py` decides what shares a cause and `problems.py`
decides what is in the model; nothing here selects.
"""

from __future__ import annotations

_BAR = "=" * 74


def render(model: dict, limit: int = 0) -> str:
    """The three views: by cause, by check, and what could not run.

    `limit` caps the members printed under each heading. The default of 0 means
    *no cap*, and that is deliberate: `grouping.py`'s own constraint is that no
    information is lost or hidden, and a group that printed ten of forty-seven
    would be exactly the summary this tool exists to refuse.
    """
    out = ["", _BAR, "PROBLEMS — the same findings, grouped by what causes them",
           _BAR]

    counts = model.get("counts") or {}
    for source in sorted(counts):
        by = ", ".join(f"{n} {s}" for s, n in counts[source].items() if n)
        out.append(f"  {source}: {by or 'nothing found'}")
    if model.get("ignored_by_policy"):
        out.append(f"  {model['ignored_by_policy']} finding(s) are held out of "
                   f"the ranking by your severity policy — they are still "
                   f"counted above")
    out.append("")

    groups = model.get("groups") or []
    out.append(f"BY SHARED CAUSE — {len(groups)} group(s). Each heading is one "
               f"edit.")
    out.append("")
    if not groups:
        out.append("  no two findings share a cause")
    for group in groups:
        by = ", ".join(f"{n} {s}" for s, n in group["by_severity"].items())
        out.append(f"  === [{group['worst'].upper():8s}] {group['cause']} — "
                   f"{group['count']} finding(s) ({by})  [{group['source']}]")
        members = group["members"]
        for member in (members[:limit] if limit else members):
            nets = f"  {', '.join(member['nets'][:4])}" if member["nets"] else ""
            out.append(f"        [{member['severity']:8s}] "
                       f"{member['title']}{nets}")
        if limit and len(members) > limit:
            out.append(f"        (+{len(members) - limit} more under this "
                       f"heading — pass limit=0 to print them)")
        out.append("")

    by_check = model.get("by_check") or []
    out.append(f"BY CHECK — {len(by_check)} check(s) produced something")
    out.append("")
    for entry in by_check:
        by = ", ".join(f"{n} {s}" for s, n in entry["by_severity"].items())
        out.append(f"  [{entry['worst']:8s}] {entry['source']}/"
                   f"{entry['check']:22s} {entry['count']:4d}  ({by})")

    coverage = model.get("coverage") or {}
    blind = coverage.get("could_not_run") or []
    out += ["", f"COULD NOT RUN — {len(blind)}. Not a pass.", ""]
    for gap in blind:
        out.append(f"  [{gap['reason']:15s}] {gap['source']}/{gap['check']}: "
                   f"{gap['statement']}")
    unmeasured = coverage.get("not_measured") or []
    out += ["", f"FOUND BUT NOT MEASURED — {len(unmeasured)}", ""]
    for gap in unmeasured:
        out.append(f"  {gap['source']}/{gap['check']}: {gap['unmeasured']} of "
                   f"{gap['found']} {gap['noun']} were never measured")
    return "\n".join(out)
