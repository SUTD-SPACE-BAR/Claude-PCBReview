"""What a finding is, and how findings are ranked.

Shared by both entry points. `audit.audit()` reviews a board and
`schematic_audit.audit_schematic()` reviews a netlist; they produce the same
kind of object and are ranked the same way, so the types live here rather than
in either one of them.

The shape of a finding is the design claim of this whole tool: a number, the
geometry or topology it came from, and why it was believed. Never a vibe.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class AuditError(Exception):
    """Raised when an audit cannot run at all."""


SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def _severity(value: float, minor: float, major: float,
              critical: float | None = None) -> tuple[str, float]:
    """Bucket a magnitude and score it in multiples of the minor threshold."""
    score = value / minor if minor else value
    if critical is not None and value >= critical:
        return "critical", score
    if value >= major:
        return "major", score
    if value >= minor:
        return "minor", score
    return "info", score


@dataclass
class Finding:
    """One computed conclusion, with everything needed to check it."""
    check: str                       # slug of the check that produced it
    title: str
    severity: str                    # critical | major | minor | info
    score: float                     # multiples of the check's own threshold
    nets: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)
    measurement: dict = field(default_factory=dict)
    layers: list[str] = field(default_factory=list)
    location: tuple[float, float] | None = None
    #: what was computed, and from what geometry — the derivation, in one line
    basis: str = ""
    #: anything assumed rather than measured, stated so it can be overridden
    assumption: str | None = None
    #: the intent datum that would settle this finding's severity
    intent_needed: str | None = None
    confidence: float = 1.0
    #: optional shapely geometry showing *why* — the gap polygons for a return
    #: path, the coupled stretch for a crosstalk finding, the narrow tracks for
    #: a neck. `render.render_finding` draws it in a distinct colour, and it is
    #: what makes a rendered finding evidence rather than a screenshot.
    #:
    #: Deliberately excluded from `summary()`: that is the machine-readable
    #: view and a polygon is neither small nor JSON.
    geometry: object | None = field(default=None, repr=False)
    geometry_label: str | None = None

    @property
    def rank(self) -> tuple:
        return (SEVERITY_ORDER.get(self.severity, 9), -self.score)

    def summary(self) -> dict:
        return {
            "check": self.check, "severity": self.severity,
            "score": round(self.score, 2), "title": self.title,
            "nets": self.nets, "layers": self.layers,
            "location": self.location, "measurement": self.measurement,
            "basis": self.basis, "assumption": self.assumption,
            "intent_needed": self.intent_needed,
            "confidence": self.confidence,
        }

    def report(self) -> str:
        lines = [f"[{self.severity.upper():8s}] {self.title}"]
        for line in self.detail:
            lines.append(f"             {line}")
        if self.location:
            lines.append(f"             at ({self.location[0]:.2f}, "
                         f"{self.location[1]:.2f})"
                         + (f" on {', '.join(self.layers)}" if self.layers else ""))
        if self.basis:
            lines.append(f"             basis: {self.basis}")
        if self.assumption:
            lines.append(f"             assumed: {self.assumption}")
        if self.intent_needed:
            lines.append(f"             intent: {self.intent_needed}")
        return "\n".join(lines)


@dataclass
class CheckResult:
    """Whether a check ran, what it cost, and what it found."""
    name: str
    ran: bool
    seconds: float = 0.0
    findings: int = 0
    skipped_because: str | None = None
    note: str | None = None

    def __str__(self) -> str:
        if not self.ran:
            return f"{self.name:22s} skipped — {self.skipped_because}"
        note = f"  ({self.note})" if self.note else ""
        return (f"{self.name:22s} {self.findings:3d} finding(s) in "
                f"{self.seconds:5.1f}s{note}")


@dataclass
class AuditReport:
    """Everything one audit produced."""
    board: str
    board_summary: dict
    stackup: str
    findings: list[Finding]
    checks: list[CheckResult]
    intent: dict | None = None
    #: `netclass` census plus how many findings it capped — Gate 2's coverage
    net_classes: dict | None = None
    intent_evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    seconds: float = 0.0
    #: content fingerprints of what was reviewed — `{"layout": …,
    #: "schematic": …}` — so a later run can say which of the two moved.
    #: Filled in by `audit_files`; empty when `audit` is called directly on an
    #: already-loaded board, which has no paths to fingerprint.
    artefacts: dict = field(default_factory=dict)
    #: the `netclass.NetClassMap` this run inferred, kept rather than
    #: discarded so the proposer can emit it without reclassifying (§48.9).
    #: Not serialised — `net_classes` above is its summary.
    net_class_map: object = None

    def of_severity(self, *severities: str) -> list[Finding]:
        return [f for f in self.findings if f.severity in severities]

    def by_check(self, check: str) -> list[Finding]:
        return [f for f in self.findings if f.check == check]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return {
            "board": self.board,
            "stackup": self.stackup,
            "findings": len(self.findings),
            "by_severity": counts,
            "checks_run": sum(1 for c in self.checks if c.ran),
            "checks_skipped": [c.name for c in self.checks if not c.ran],
            "seconds": round(self.seconds, 1),
        }

    def report(self, limit: int | None = 20,
               severities: tuple[str, ...] = ("critical", "major", "minor"),
               grouped: bool = False) -> str:
        """The findings as text.

        `grouped` prints them under their shared cause instead of as one flat
        ranked list — a 10x reduction in top-level items on a dense board with
        **nothing collapsed away**: each heading is followed by every finding
        under it. `limit` does not apply in that mode, because truncating a
        group would be exactly the "info is lost or hidden" the grouping was
        asked not to do (§49.2).
        """
        counts = self.summary()["by_severity"]
        lines = [
            f"{self.board} — {self.stackup}",
            # Layout keys first, then the schematic-review ones. A whitelist
            # rather than everything, because a board summary carries a lot
            # that does not belong in a one-line header.
            "  " + ", ".join(f"{k}={v}" for k, v in self.board_summary.items()
                             if k in ("layers", "nets", "tracks", "vias",
                                      "zones", "sheets", "parts", "pins")),
            "",
            "checks",
        ]
        lines += [f"  {check}" for check in self.checks]
        lines += ["", f"findings: " + ", ".join(
            f"{count} {name}" for name, count in sorted(
                counts.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9)))
            or "none", ""]

        shown = [f for f in self.findings if f.severity in severities]
        if grouped:
            from pcbreview.grouping import report as grouped_report
            lines.append(grouped_report(shown))
        else:
            for finding in shown[:limit]:
                lines.append(finding.report())
            if limit is not None and len(shown) > limit:
                lines.append(
                    f"  ... {len(shown) - limit} more at these severities")
        if self.assumptions:
            lines += ["", "assumptions this run made:"]
            lines += [f"  - {a}" for a in self.assumptions]
        return "\n".join(lines)
