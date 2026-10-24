"""What a deterministic review report *is*, as data.

A structure, never a string. The same selection has to feed the CLI text, the
app's Report tab and the MCP tool surface, and a function that returns prose can
only serve the first of those.

Everything here is frozen and hashable-ish on purpose: a `Report` is a pure
function of its inputs (`generate.generate`), so two runs over the same audit
produce two objects that compare equal. `measurement` is the one mutable field
— it is the engine's own dict, carried through verbatim rather than copied into
a schema this module would have to keep in step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The engine's own ordering, re-stated rather than imported, so that reading a
#: serialised report needs no engine. `findings.SEVERITY_ORDER` is the source of
#: truth and `generate` asserts the two agree.
SEVERITY_ORDER: dict[str, int] = {
    "critical": 0, "major": 1, "minor": 2, "info": 3}

#: Which entry point a finding came from. The two check disjoint things
#: (HANDOFF §3.3) and the report must never blur them.
LAYOUT = "layout"
SCHEMATIC = "schematic"


@dataclass(frozen=True)
class FindingRef:
    """One finding, selected by a rule, with why it was selected attached.

    A projection of `findings.Finding`, minus `geometry` (a shapely object, not
    reportable) and plus `source`, `rules` and the demotion trail.
    """
    source: str                      # LAYOUT | SCHEMATIC
    check: str
    severity: str                    # as reported, i.e. after any demotion
    title: str
    score: float
    nets: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()
    location: tuple[float, float] | None = None
    detail: tuple[str, ...] = ()
    basis: str = ""
    assumption: str | None = None
    intent_needed: str | None = None
    measurement: dict = field(default_factory=dict, compare=False)
    #: the severity this finding's own measurement produced, before a
    #: *declaration* lowered it. Only a user declaration may (engine rule 5),
    #: and the report says so rather than presenting the demoted value as the
    #: whole truth.
    demoted_from: str | None = None
    demoted_by: str | None = None    # "netclass" | "partition"
    #: which report rules pulled this in — "critical_major", "drc_error", …
    rules: tuple[str, ...] = ()

    @property
    def rank(self) -> tuple:
        """Total order. Deliberately not the engine's `Finding.rank`.

        The engine's is `(severity, -score)`, which leaves ties to list order.
        A report is a *set* selected from that list and must not inherit an
        ordering that depends on how the set was walked, so every remaining
        tie is broken on the finding's own content.
        """
        return (SEVERITY_ORDER.get(self.severity, 9), -self.score,
                self.source, self.check, self.title, self.nets,
                self.location or (0.0, 0.0))


@dataclass(frozen=True)
class GroupRef:
    """A shared cause and **every** finding under it. Never a subset.

    `grouping.py` exists because 47 of 58 findings on one board were one fact.
    Its own constraint — "no info is lost or hidden" — carries over here: a
    group is a heading over a complete list, so `members` is all of them.
    """
    source: str
    check: str
    cause: str                       # `str(grouping.Cause)`
    label: str = ""                  # "referenced to", "no plane over", or ""
    shared: tuple[str, ...] = ()
    worst: str = "info"
    by_severity: dict = field(default_factory=dict, compare=False)
    nets: tuple[str, ...] = ()
    members: tuple[FindingRef, ...] = ()

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def rank(self) -> tuple:
        return (SEVERITY_ORDER.get(self.worst, 9), -self.count,
                self.source, self.cause)


@dataclass(frozen=True)
class CoverageGap:
    """A check that had nothing to look at — rule 3.

    **Never rendered as a pass.** `impedance … (1 fab target(s), 0 net(s)
    examined)` is a blind check, and the whole reason this class exists is that
    it reads like a clean one.
    """
    source: str
    check: str
    #: "skipped" — the check did not run at all;
    #: "zero_population" — it ran over an empty population;
    #: "no_input" — its note says an input was absent;
    #: "unread" — its note carries a zero the coverage reader could not
    #: classify. Weaker than the others and deliberately present: a reader
    #: that matches free text will eventually meet text it does not match,
    #: and saying so beats a section that silently got shorter.
    reason: str
    #: the engine's own words for what was empty, quoted not paraphrased
    statement: str
    #: the whole `CheckResult.note` or `.skipped_because`, for a reader who
    #: wants to check this module's reading of it
    note: str | None = None
    #: findings the check produced anyway. Non-zero is normal and important:
    #: `current_capacity` reports 18 absolute-ampacity findings on a board
    #: where zero power nets had enough terminals to *compare*.
    findings_anyway: int = 0

    @property
    def rank(self) -> tuple:
        return (self.source, self.check, self.reason, self.statement)


@dataclass(frozen=True)
class MeasuredGap:
    """Found, then not measured — rule 3b.

    The harder half of rule 3, because the count reads as evidence. One board
    reported `esd_clamps: 11` and no findings while thirteen of its fourteen
    clamped lines had never been compared.
    """
    source: str
    check: str
    found_key: str
    measured_key: str
    found: int
    measured: int
    #: what a candidate is, in words — "crystal(s)", "ESD clamp(s)"
    noun: str = ""
    #: what "measured" would have meant for it
    means: str = ""
    detail: tuple[str, ...] = ()

    @property
    def unmeasured(self) -> int:
        return self.found - self.measured

    @property
    def rank(self) -> tuple:
        return (self.source, self.check, -self.unmeasured, self.found_key)


@dataclass(frozen=True)
class EntryPoint:
    """Whether one of the two entry points ran — rule 0.

    Silence about the schematic reads as "the schematic is clean", which is how
    a board shipped with `/SCL` and `/SDA` each driven by two output pins. So
    the absence is a *reported state* with a reason, never an omission.
    """
    name: str                        # "audit" | "schematic_audit"
    #: "reviewed" | "not_supplied" | "not_run" | "failed"
    state: str
    statement: str
    subject: str | None = None       # board or root sheet name
    findings: int = 0

    @property
    def reviewed(self) -> bool:
        return self.state == "reviewed"


@dataclass(frozen=True)
class RenderPlan:
    """What must be drawn. A floor, not a ceiling — SKILL.md:415.

    Selection only: this names the largest group and every net a `critical` or
    `major` finding names. Turning that into pictures is `render.py`'s job in
    the engine today and the canvas's job from Phase 2.
    """
    largest_group: str | None = None
    largest_group_source: str | None = None
    nets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    """Everything a review must contain, selected by rule and by nothing else.

    Judgement — a human's or a model's — decides the order things are read in
    and the words wrapped around them. It does not decide what is in here. If
    it did, the review would stop being repeatable, which is the defect this
    whole structure exists to close.
    """
    board: str
    entry_points: tuple[EntryPoint, ...] = ()
    severe: tuple[FindingRef, ...] = ()          # rule 1
    groups: tuple[GroupRef, ...] = ()            # rule 2
    could_not_run: tuple[CoverageGap, ...] = ()  # rule 3
    not_measured: tuple[MeasuredGap, ...] = ()   # rule 3b
    drc_errors: tuple[FindingRef, ...] = ()      # rule 4
    assumptions: tuple[str, ...] = ()            # rule 5
    render_plan: RenderPlan = field(default_factory=RenderPlan)
    #: per-source `{severity: count}` over *every* finding, including `info`.
    #: The generator is given `--all` and renders a subset; these are the
    #: totals it selected from, so a reader can see what was left out.
    counts: dict = field(default_factory=dict, compare=False)
    #: rule id -> how many items it contributed. A test asserts each rule
    #: fires on a board that should trip it; a reader can see at a glance that
    #: rule 3b found nothing rather than was not run.
    rule_trace: dict = field(default_factory=dict, compare=False)
    #: content fingerprints of what was reviewed, straight from
    #: `AuditReport.artefacts`, so a stored report can be checked against the
    #: files it describes
    artefacts: dict = field(default_factory=dict, compare=False)

    @property
    def schematic_reviewed(self) -> bool:
        return any(e.name == "schematic_audit" and e.reviewed
                   for e in self.entry_points)
