"""Findings gathered under the thing that caused them.

The user, 2026-08-03, after an outside review where **47 of 58 findings were
one fact** and triage meant reading 47 entries to write one sentence:

> Maybe the findings can be grouped by cause, followed by comprehensive list of
> consequences, so its clear that fixing this one thing fixes all this, but no
> info is lost or hidden.

**The constraint that shapes every decision here is the second half of that
sentence.** A group is a *heading over a complete list*, never a summary that
replaces one. Nothing is collapsed, elided, truncated or counted-and-dropped:
`group_by_cause` is a partition, and every finding it is given comes back out
in exactly one group. `test_grouping.py` asserts that as a property rather
than trusting it.

Why this is in code rather than in `SKILL.md`'s prose
----------------------------------------------------
The alternative was telling the front end to do the grouping while reading.
That asks an LLM to redo, unreliably and every time, a partition the data
already supports exactly — and the point of this project is that conclusions
are computed in code (§1). It is also testable this way, which prose is not.

What "the same cause" means, and its honest limit
--------------------------------------------------
The key is `(check, the thing the findings share)`. What that second element
is depends on the check, because different checks record their cause in
different fields:

| check | shared thing | why that is the cause |
|---|---|---|
| `return_path`, `reference_split` | `reference_nets` | one plane boundary produces every detour across it |
| `layer_transition` | `unreferenced_layers` | one missing plane over one layer pair strands every via that crosses it |
| everything else | nothing | the check itself is the coarsest honest key |

Measured on the five boards here, that takes 306 findings to 33 groups, 187
to 20, 96 to 32, 49 to 15 and 44 to 14 — roughly a 10x reduction in top-level
items on the dense boards with nothing hidden.

**The limit is worth stating plainly.** This groups by a shared *measurement*,
not by a shared physical explanation. On the board that prompted it, the real
cause was "F.Cu is a +3V3 pour and B.Cu is a GND pour, so every signal via
flips its return reference, and the only four bridging capacitors sit in a
26 mm strip". That sentence is a synthesis; this module gets you as far as
"these 27 findings all concern the +3V3/GND reference boundary", which is the
heading under which a human or the front end writes the sentence. It does not
pretend to more than it measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pcbreview.findings import SEVERITY_ORDER, Finding

#: `measurement` keys that record what a finding's cause was, most specific
#: first. The first one present decides the group.
#:
#: Order matters: `reference_split` carries `reference_nets` and nothing else,
#: while `layer_transition` carries `unreferenced_layers`, so no finding is
#: ever keyed on two of these and the choice is not a tie-break.
_CAUSE_FIELDS: tuple[tuple[str, str], ...] = (
    ("reference_nets", "referenced to"),
    ("unreferenced_layers", "no plane over"),
)


def _values(raw) -> tuple[str, ...]:
    """A hashable, order-independent form of whatever the field held.

    These fields are not one shape. `return_path` writes a list of net names;
    `reference_split` writes a **dict** of net name to millimetres, which is
    why a naive `Counter` over the raw values raises `unhashable type: dict`.
    Sorting also means `['GND', '/VIN']` and `['/VIN', 'GND']` are one cause,
    which they are.
    """
    if isinstance(raw, dict):
        return tuple(sorted(map(str, raw)))
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(sorted(map(str, raw)))
    return (str(raw),)


@dataclass(frozen=True)
class Cause:
    """What a group of findings has in common."""
    check: str
    #: the shared values, or empty when the check itself is the whole key
    shared: tuple[str, ...] = ()
    #: how to read `shared` — "referenced to", "no plane over"
    label: str = ""

    def __str__(self) -> str:
        if not self.shared:
            return self.check
        return f"{self.check} — {self.label} {', '.join(self.shared)}"


def cause_of(finding: Finding) -> Cause:
    """The cause key for one finding. Total: every finding has one."""
    for name, label in _CAUSE_FIELDS:
        raw = finding.measurement.get(name)
        if raw:
            return Cause(check=finding.check, shared=_values(raw), label=label)
    return Cause(check=finding.check)


@dataclass
class Group:
    """One cause, and **every** finding it produced."""
    cause: Cause
    findings: list[Finding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        return min((f.severity for f in self.findings),
                   key=lambda s: SEVERITY_ORDER.get(s, 9), default="info")

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    @property
    def nets(self) -> list[str]:
        return sorted({n for f in self.findings for n in f.nets if n})

    def headline(self) -> str:
        """One line naming the cause and the size of its consequence.

        Deliberately says *nothing* the findings underneath do not — the value
        is "fixing this one thing fixes all of these", and inventing a
        diagnosis on top of that would be exactly the LLM-side reasoning this
        project computes instead.
        """
        counts = ", ".join(
            f"{count} {name}" for name, count in
            sorted(self.by_severity.items(),
                   key=lambda kv: SEVERITY_ORDER.get(kv[0], 9)))
        nets = self.nets
        touching = ""
        if nets:
            touching = (f", on {len(nets)} net(s)" if len(nets) > 3
                        else f", on {', '.join(nets)}")
        return (f"=== [{self.worst.upper():8s}] {self.cause} — "
                f"{len(self.findings)} finding(s) ({counts}){touching}")

    def report(self) -> str:
        """The heading, then every finding under it. Never a subset.

        Members are indented under the heading. Without that the two are
        indistinguishable — both start `[MAJOR   ]` — and a group of 34 reads
        as 35 unrelated findings, which is the opposite of the point.
        """
        lines = [self.headline()]
        for finding in self.findings:
            lines += [f"  {line}" for line in finding.report().splitlines()]
        return "\n".join(lines)


def group_by_cause(findings) -> list[Group]:
    """Partition `findings` by shared cause, worst and largest first.

    A partition, not a filter: `sum(len(g.findings) for g in result)` equals
    `len(findings)` for any input, and each finding appears in exactly one
    group. Findings keep their existing relative order inside a group, which
    on an `AuditReport` means they stay ranked.
    """
    groups: dict[Cause, Group] = {}
    for finding in findings:
        cause = cause_of(finding)
        groups.setdefault(cause, Group(cause=cause)).findings.append(finding)

    return sorted(groups.values(),
                  key=lambda g: (SEVERITY_ORDER.get(g.worst, 9),
                                 -len(g.findings),
                                 -max((f.score for f in g.findings),
                                      default=0.0),
                                 str(g.cause)))


def report(findings, severities: tuple[str, ...] | None = None) -> str:
    """The grouped view as text.

    `severities` filters which findings are *shown*, exactly as the ungrouped
    report does — that is the caller's existing choice about verbosity and not
    something grouping is allowed to decide.
    """
    if severities is not None:
        findings = [f for f in findings if f.severity in severities]
    groups = group_by_cause(findings)
    if not groups:
        return "no findings at these severities"
    lines = [f"{len(findings)} finding(s) under {len(groups)} shared cause(s) "
             f"— every finding is listed, under exactly one heading", ""]
    for group in groups:
        lines.append(group.report())
        lines.append("")
    return "\n".join(lines).rstrip()
