"""One command over a project folder, with no agent in the loop.

This is Claude's jobs 1 and 2 (HANDOFF §1): resolving where the engine lives and
which KiCad to use, and working out which attached file is what. Both were
already deterministic — `inputs.route()` classifies by *content*, and
`inputs.resolve_stackup()` knows the two places a stackup is allowed to be — so
this file is mostly the loop that was living in prose.

Job 6 is `server/report/`. Together they are Phase 1's claim: **one command on a
project folder produces the full report, and two runs on the same inputs are
identical.**

The thing that is genuinely new here is running **both entry points**. Passing a
schematic to `pcbreview.audit` enables `placement` and `parity`; it does not run
the eight schematic checks. A session that ran only the layout audit reported a
schematic as clean while `/SCL` and `/SDA` were each driven by two output pins.
`review()` runs both, always, and when it cannot, the report says so in the one
place a reader cannot miss (rule 0).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pcbreview import bom as bom_module                  # noqa: E402
from pcbreview import inputs as inputs_module            # noqa: E402
from pcbreview.audit import audit_files                  # noqa: E402
from pcbreview.findings import AuditError                # noqa: E402
from pcbreview.schematic_audit import audit_schematic_file  # noqa: E402
from pcbreview.stackup import StackupGateError           # noqa: E402

from pcbreview import severity as severity_module           # noqa: E402
from pcbreview.extraction import refill_to_temp             # noqa: E402
from pcbreview.report import generate                       # noqa: E402
from pcbreview.report.model import Report                   # noqa: E402

#: Suffixes `discover()` will pick up when pointed at a folder. Everything else
#: is left to `inputs.classify`, which opens the file rather than trusting it.
_SCANNED = (".kicad_pcb", ".kicad_sch", ".yaml", ".yml", ".csv", ".pdf")

#: Directories a project scan never descends into.
#:
#: `review/` holds a *previous* run's artefacts, including
#: `proposed.netintent.yaml` — a machine-written file that must never be
#: mistaken for the user's own word (`audit.PROPOSED_INTENT_NAME`).
#:
#: The rest are copies of the project. KiCad writes `<board>-backups/`, and
#: every one of these example folders also has a `.history/` from VS Code's
#: Local History extension holding **a second `.kicad_pcb` with the same
#: name**. Without this the scan finds two boards and refuses to guess, which
#: is the correct refusal to the wrong question.
_SKIP_DIRS = frozenset({"review", "__pycache__", "fp-info-cache", "backups"})


def _is_skipped_dir(name: str) -> bool:
    # Any dotfolder: `.git`, `.history`, `.vscode`. None of them holds an
    # input, and all of them hold copies of ones that do.
    return (name in _SKIP_DIRS or name.startswith(".")
            or name.endswith("-backups"))


class PipelineError(Exception):
    """Raised when a project cannot be reviewed at all."""


@dataclass
class Project:
    """Which file is what, decided by reading them — never by their position."""
    root: Path
    board: Path
    stackup: Path | None = None
    schematic: Path | None = None
    thermal: Path | None = None
    netintent: Path | None = None
    bom: Path | None = None
    datasheets: tuple[Path, ...] = ()
    unknown: tuple[Path, ...] = ()
    #: what routing did that the user should be able to see it did
    notes: tuple[str, ...] = ()

    def describe(self) -> str:
        rows = [("board", self.board), ("stackup", self.stackup),
                ("schematic", self.schematic), ("thermal", self.thermal),
                ("netintent", self.netintent), ("BOM", self.bom)]
        lines = [f"  {name:10} {path.name if path else '— none found —'}"
                 for name, path in rows]
        for pdf in self.datasheets:
            lines.append(f"  {'datasheet':10} {pdf.name} — not read by the "
                         f"audit; `python -m pcbreview.parts` reads it and "
                         f"writes the thermal manifest")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


@dataclass
class ReviewResult:
    """Everything one pipeline run produced."""
    project: Project
    report: Report
    layout: object                      # pcbreview.findings.AuditReport
    schematic: object | None = None
    schematic_state: str = "not_run"
    schematic_note: str | None = None
    notes: list[str] = field(default_factory=list)
    #: The `model.Board` the layout audit measured, when `keep_board` was set.
    #: `None` otherwise — the CLI has no use for it and it is megabytes.
    board: object | None = None
    #: The path actually audited. Not always `project.board`: `--refill` audits
    #: a temp copy, and that copy is deleted when this call returns.
    board_path: Path | None = None
    #: The user's severity policy, and what applying it moved. Empty when there
    #: is no policy file, which is the ordinary case.
    policy: object = None
    policy_outcome: dict = field(default_factory=dict)
    #: Every finding the checks produced, before `ignore` withheld any. Kept so
    #: a policy change can be applied to a loaded review without re-auditing.
    all_layout_findings: list = field(default_factory=list)
    all_schematic_findings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery — job 2
# ---------------------------------------------------------------------------

def _candidate_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(_is_skipped_dir(part)
               for part in path.relative_to(root).parts[:-1]):
            continue
        if path.suffix.lower() in _SCANNED:
            found.append(path)
    return found


#: How a parent sheet names a child. KiCad writes one `Sheetfile` property per
#: sheet symbol, and the value is the child's filename.
_SHEETFILE = re.compile(r'Sheetfile"\s*"([^"]+)"')


def _referenced_sheets(path: Path) -> set[str]:
    """The filenames this sheet places as children. Empty if it cannot be read.

    Read as text rather than parsed: this runs over every `.kicad_sch` in a
    folder before anything is known about which one matters, and a full
    S-expression parse of a hierarchy to answer "which file is the root" would
    cost more than the review it is setting up.
    """
    try:
        return set(_SHEETFILE.findall(path.read_text(encoding="utf-8",
                                                     errors="replace")))
    except OSError:
        return set()


def _root_schematic(candidates: list[Path], board: Path) -> Path | None:
    """The root sheet of a hierarchy, not one of its children.

    Handing a *child* sheet to `schematic_audit` reviews that sheet's fragment
    of the netlist and reports the rest as unconnected, which reads as a
    catastrophe rather than as a mistake. So this would rather return `None` —
    "no schematic", which rule 0 reports honestly — than return the wrong file.

Four rules, in order, and only the first was there before:

    1. **`<board stem>.kicad_sch`.** KiCad names the root after the project, so
       this is right on every board KiCad creates.
    2. **The only sheet there is.**
    3. **What the `.kicad_pro` says.** KiCad records the hierarchy root as the
       first entry of its `sheets` list. Authoritative, and it beats inferring.
    4. **The sheet nothing else places**, if there is exactly one. A hierarchy
       is a tree and its root is the file no other file names in a `Sheetfile`
       property. On a clean project this is decisive; on a real one it often is
       not, which is why rule 3 comes first.

    Rules 2–4 are what handles a project whose board and schematic have
    different stems — the case where rule 1 misses and the old code then gave
    up and reviewed no schematic at all.

    Ambiguity is still `None` rather than a pick. Two unreferenced sheets is
    two designs in a folder, or a stray copy, and choosing between them is the
    user's call — `--schematic` is how they make it.
    """
    beside = board.with_suffix(".kicad_sch")
    if beside.exists():
        return beside

    sheets = [p for p in candidates if p.suffix.lower() == ".kicad_sch"]
    if len(sheets) == 1:
        return sheets[0]
    if not sheets:
        return None

    named = _root_from_project(board.parent, sheets)
    if named is not None:
        return named

    placed: set[str] = set()
    for sheet in sheets:
        placed |= _referenced_sheets(sheet)
    # Compare on the name: `Sheetfile` is relative to its parent, and every
    # sheet of one hierarchy sits in one directory in every project KiCad
    # makes.
    roots = [s for s in sheets if s.name not in placed]
    return roots[0] if len(roots) == 1 else None


def _root_from_project(directory: Path, sheets: list[Path]) -> Path | None:
    """The root sheet as KiCad's own project file records it.

    **The authoritative answer, and it is worth reaching for before inferring
    anything.** A `.kicad_pro` carries a `sheets` list of `[uuid, name]` with
    the **root first** — that is KiCad stating which file is the top of the
    hierarchy, rather than this module deducing it.

    It matters because deduction is genuinely ambiguous on real projects.
    Measured on a six-layer design here: 19 sheets on disk and **seven** that
    nothing else places — the root plus six orphans left behind by edits. The
    unreferenced-sheet rule cannot separate those; the project file names the
    right one immediately.

    Returns `None` rather than guessing when there is no project file, when it
    will not parse, or when the name it gives is not on disk. Every one of
    those is a project in a state this should not have an opinion about.
    """
    import json                                               # noqa: PLC0415

    by_stem = {s.stem: s for s in sheets}
    for project in sorted(directory.glob("*.kicad_pro")):
        try:
            data = json.loads(project.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        listed = data.get("sheets")
        if not isinstance(listed, list) or not listed:
            continue
        first = listed[0]
        # `[uuid, name]`, and the name has no extension. A one-element entry
        # or a bare string are both shapes older files use.
        name = (first[1] if isinstance(first, list) and len(first) > 1
                else first if isinstance(first, str) else None)
        found = by_stem.get(str(name)) if name else None
        if found is None:
            # KiCad also names the root after the project itself, which is the
            # case when the `sheets` entry carries an empty name.
            found = by_stem.get(project.stem)
        if found is not None:
            return found
    return None


def discover(target, schematic=None) -> Project:
    """Route a project folder, or a board file, into slots.

    `inputs.route()` decides what each file is by opening it, so a `.kicad_sch`
    can never land in the stackup slot. This adds the folder walk and the
    root-sheet rule, and refuses rather than guessing when a folder holds two
    boards — picking one silently is how a whole review gets computed from the
    wrong file.
    """
    target = Path(target)
    if not target.exists():
        raise PipelineError(f"not found: {target}")

    if target.is_file():
        if target.suffix.lower() != ".kicad_pcb":
            raise PipelineError(
                f"{target.name} is not a .kicad_pcb. Point this at a board or "
                f"at the folder holding one")
        root, candidates = target.parent, _candidate_files(target.parent)
    else:
        root = target
        candidates = _candidate_files(root)

    boards = [p for p in candidates if p.suffix.lower() == ".kicad_pcb"]
    if target.is_file():
        board = target
    elif not boards:
        raise PipelineError(f"no .kicad_pcb under {root}")
    elif len(boards) > 1:
        raise PipelineError(
            f"{len(boards)} boards under {root}: "
            + ", ".join(str(p.relative_to(root)) for p in boards)
            + ". Name the one you mean")
    else:
        board = boards[0]

    notes: list[str] = []
    if schematic is not None:
        schematic = Path(schematic)
        if not schematic.exists():
            raise PipelineError(f"not found: {schematic}")
        notes.append(f"the schematic was named explicitly: {schematic.name}")
    else:
        schematic = _root_schematic(candidates, board)
        sheets = [p for p in candidates if p.suffix.lower() == ".kicad_sch"]
        if schematic is None and sheets:
            # **Say why, rather than reporting "no schematic supplied".** Rule 0
            # renders the absence honestly either way, but a user whose folder
            # visibly contains sheets is owed the reason and the way out.
            notes.append(
                f"{len(sheets)} .kicad_sch file(s) are here and none of them "
                f"is unambiguously the root of a hierarchy ("
                + ", ".join(sorted(p.name for p in sheets)[:6])
                + "). The schematic was NOT reviewed; name the root sheet to "
                  "review it")

    # Three slots are decided before `route()` sees anything, each by the
    # resolver that already owns it:
    #
    #  * the board, by extension and by there being exactly one;
    #  * the schematic, by the root-sheet rule above, which knows something
    #    `inputs.classify` does not;
    #  * the BOM, by `bom.find_bom()` and its ordered `_BOM_PATTERNS`.
    #
    # The BOM is the one that has to be taken away from `route()` rather than
    # merely helped. `classify` calls **every** `.csv` a BOM without opening
    # it, so a KiCad `production/` folder — which holds `bom.csv`,
    # `designators.csv` and `positions.csv` — routes as three BOMs and
    # `route()` correctly refuses to guess. Parsing them does not separate them
    # either: `positions.csv` has a `Ref` column and loads as a valid BOM with
    # one line per placement. `find_bom` is the engine's own answer to which
    # file is meant, and it is also what `audit_files` would have used, so
    # using it here keeps the report's inputs card honest about the run.
    bom_path = bom_module.find_bom(board)
    # `find_bom` builds its own path from `_BOM_PATTERNS`, so it is equal to
    # the scanned one only after both are resolved — `production/bom.csv` and
    # `<root>/production/bom.csv` are the same file and different `Path`s, and
    # comparing them raw reported the BOM as a file that had been ignored.
    bom_resolved = bom_path.resolve() if bom_path else None
    ignored_csv = sorted(p for p in candidates
                         if p.suffix.lower() == ".csv"
                         and p.resolve() != bom_resolved)
    if ignored_csv:
        notes.append(
            f"{len(ignored_csv)} other .csv file(s) were left alone "
            f"({', '.join(p.name for p in ignored_csv)}) — a placement or "
            f"designator export is not a BOM")

    routable = [str(p) for p in candidates
                if p.suffix.lower() not in (".kicad_sch", ".kicad_pcb", ".csv")]
    slots = inputs_module.route(routable, require_board=False)

    stackup_path, stackup_notes = inputs_module.resolve_stackup(
        str(board), slots.get("stackup"))
    notes.extend(stackup_notes)

    def _as_path(value) -> Path | None:
        return Path(value) if value else None

    return Project(
        root=root, board=board, stackup=_as_path(stackup_path),
        schematic=schematic, thermal=_as_path(slots.get("thermal")),
        netintent=_as_path(slots.get("netintent")),
        bom=_as_path(bom_path),
        datasheets=tuple(Path(p) for p in slots.get("datasheets", [])),
        unknown=tuple(Path(p) for p in slots.get("unknown", [])),
        notes=tuple(notes))


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _load_board(board_path: Path, stackup_path: Path):
    """The audited board, loaded again for the app, with its stackup bound.

    Deliberately the same three calls `audit_files` makes, in the same order —
    `Board.from_kicad`, `load_and_validate`, `bind_stackup` — because binding
    is not decoration. `current.ampacity` reads the copper weight per layer off
    the stackup, and an unbound board silently falls back to 1 oz outer /
    0.5 oz inner, which would put a plausible and wrong ampacity beside every
    net in the inspector while the report showed the right one.

    The `problems` list is dropped here and not re-reported: `audit_files`
    already put it in `report.assumptions`, where rule 5 renders it. Reporting
    it twice would suggest two stackup readings happened.
    """
    from pcbreview.model import Board
    from pcbreview.stackup import load_and_validate

    board = Board.from_kicad(board_path)
    stackup, _problems = load_and_validate(stackup_path, board.copper_layers)
    board.bind_stackup(stackup)
    return board


def review(project: Project, *, refill: bool = False, keep_board: bool = False,
           workdir=None, **audit_kwargs) -> ReviewResult:
    """Run both entry points over `project` and select what must be reported.

    `refill` copies the board to a temp directory and refills its zones with
    `kicad-cli` first (`server/extraction/refill.py`). **Off by default**, and
    the reason is in that module: refilling moves the geometry ~0.21 % and
    gives the extraction cache a new key, which is how a comparison against an
    earlier run shows movement nobody caused. KiCad saves fills when it saves a
    board; this is for the board where that is not true.

    `keep_board` returns the loaded `model.Board` on the result. The app needs
    it — the canvas draws from its geometry and the inspector measures from it
    — and it has to be loaded **in here**, before the `finally` below, because
    a refilled board lives in a temp directory this function deletes on the way
    out. Loading it afterwards from `project.board` would silently give the app
    the *unrefilled* board while the report described the refilled one, and
    nothing downstream could tell.

    It is cheap. The extraction is already cached by `Board.from_kicad`, so
    this is a JSON read of the file the audit just used — 0.04 s on the V3
    board — and both loads therefore describe identical copper. The CLI leaves
    it off because it has no use for megabytes of geometry.

    A schematic that fails to review does not stop the layout review. It
    becomes `schematic_state="failed"` with the reason, and rule 0 renders that
    as *not reviewed* — which is the honest reading, and the one thing that
    must never be silent.
    """
    if project.stackup is None:
        # Gate 1, restated at the pipeline boundary so the message names the
        # project rather than surfacing as a stackup-module error.
        raise PipelineError(
            f"{project.board.name}: no validated fabrication stackup was "
            f"found. Transcribe the fab's own stackup with "
            f"`python -m pcbreview.stackup_cli transcribe` — do not invent one")

    notes: list[str] = []
    board_path = project.board
    temp: tempfile.TemporaryDirectory | None = None
    try:
        if refill:
            if workdir is None:
                temp = tempfile.TemporaryDirectory(prefix="pcbreview-refill-")
                workdir = temp.name
            outcome = refill_to_temp(board_path, workdir)
            if outcome.ok:
                board_path = outcome.board
                notes.append(
                    f"zones were refilled by kicad-cli on a temp copy in "
                    f"{outcome.seconds:.1f} s before extraction; your board "
                    f"file was not touched. KiCad's filler is not perfectly "
                    f"reproducible (~0.21 % on fill area measured), so numbers "
                    f"here can differ slightly from a run over the saved fills")
                notes.extend(outcome.notes)
            else:
                notes.append(f"a zone refill was requested and did not happen "
                             f"({outcome.why_not}); the fills saved in the "
                             f"board file were used as they are")

        # **Every optional input is pinned, never left to auto-discovery.**
        # `audit_files` reads `False` as "there is none, and do not go
        # looking", and that third state exists because twice a run measured a
        # different *input* while everyone believed it was measuring different
        # *code* (its docstring, §47.9 and §52.4). `discover()` has already
        # said what this project contains and printed it; if the audit then
        # found a manifest of its own beside the board, the report would
        # describe a run that did not happen.
        def _pin(path) -> str | bool:
            return str(path) if path else False

        try:
            layout = audit_files(
                board_path, project.stackup,
                _pin(project.schematic), _pin(project.thermal),
                _pin(project.netintent), _pin(project.bom),
                **audit_kwargs)
        except (AuditError, StackupGateError) as exc:
            raise PipelineError(f"{project.board.name}: {exc}") from exc

        schematic_report = None
        state, note = "not_supplied", None
        if project.schematic is not None:
            try:
                schematic_report = audit_schematic_file(project.schematic)
                state = "reviewed"
            except (AuditError, OSError, ValueError) as exc:
                state, note = "failed", f"{type(exc).__name__}: {exc}"
                notes.append(f"the schematic review failed: {note}")

        board = None
        if keep_board:
            # Inside the `try`, so a refilled board is still on disk. The
            # extraction cache means this re-reads the JSON the audit produced
            # rather than re-running KiCad, so the geometry is the same
            # geometry and not merely an equivalent one.
            board = _load_board(board_path, project.stackup)
    finally:
        if temp is not None:
            temp.cleanup()

    # **The user's severity policy, applied here and nowhere else.**
    #
    # After both audits and before `generate`, which is the only position that
    # is correct: every check has computed exactly what it computes (§3.2
    # rule 4 is about the engine, and it is untouched), and the report is then
    # built from the findings the user will actually see, so the artefact of
    # record and the screen cannot disagree.
    #
    # It is a *user declaration*, which is what §3.2 rule 5 permits — the same
    # standing `netclass.apply` has — and like that one it records what it
    # changed on the finding rather than overwriting it. See `severity.py`.
    # Kept whole, before `ignore` takes any of them out, so a policy change can
    # be undone without re-auditing — `Session.reapply_policy` restores from
    # these. They are the findings the *checks* produced.
    all_layout = list(layout.findings)
    all_schematic = (list(schematic_report.findings)
                     if schematic_report is not None else [])

    policy = severity_module.Policy()
    policy_outcome = {"applied": False}
    try:
        policy = severity_module.load(
            severity_module.default_path(project.board))
    except severity_module.SeverityError as exc:
        # A policy that will not parse must not silently become "no policy" —
        # that is a review that looks like it honoured your settings.
        notes.append(f"the severity policy beside this board could not be read "
                     f"and was NOT applied ({exc}); every finding below is at "
                     f"the severity its check computed")
    else:
        policy_outcome = apply_policy(layout, schematic_report, policy)
        line = severity_module.summary(policy, policy_outcome)
        if line:
            notes.append(line)

    # **The pin, explained.** `audit_files` warns that suppressing
    # auto-discovery "is wrong for a real review", and it is right about the
    # caller it was written for — one that passes `False` without having looked.
    # This one has: `discover()` walked the project, said what it found in the
    # inputs card, and pinned exactly that. Left unqualified the warning reads
    # as though this review had ignored a manifest sitting beside the board,
    # which is the opposite of what happened, so the correction goes in the
    # same section rather than being argued about somewhere a reader is not.
    if any("suppressed auto-discovery" in str(a) for a in layout.assumptions):
        notes.append(
            "…that suppression is this pipeline's, and it followed a scan: the "
            "inputs card above lists every file found beside the board and the "
            "run was pinned to exactly those. Nothing that exists was treated "
            "as absent")

    # The engine's own pattern — `audit_files` appends to `report.assumptions`
    # after `audit()` returns, for exactly this: things the *caller* did that
    # a reader of the findings has to know about. Rule 5 then reports them.
    layout.assumptions = list(layout.assumptions) + notes

    report = generate(layout, schematic_report, schematic_state=state,
                      schematic_note=note)
    return ReviewResult(project=project, report=report, layout=layout,
                        schematic=schematic_report, schematic_state=state,
                        schematic_note=note, notes=notes, board=board,
                        board_path=board_path, policy=policy,
                        policy_outcome=policy_outcome,
                        all_layout_findings=all_layout,
                        all_schematic_findings=all_schematic)


def apply_policy(layout, schematic_report, policy) -> dict:
    """Apply a severity policy to two reports **in place**, and hide `ignore`.

    Split out of `review` so `Session.reapply_policy` can call the same code
    on an already-loaded review. A severity policy relabels; it measures
    nothing, so re-running the audit to change one would be re-computing a
    Laplace field solve to rename a row.

    **`ignore` is removed from `findings` here and nowhere else.** Doing it at
    this one seam means `generate`, `build_index` and `problems.build` need no
    special case and cannot forget one — which is exactly how the first version
    ended up showing ignored findings in the inspector and the report while the
    problems panel filtered them.
    """
    findings = list(layout.findings)
    if schematic_report is not None:
        findings += list(schematic_report.findings)
    outcome = severity_module.apply(findings, policy)

    if outcome.get("applied"):
        layout.findings = [f for f in layout.findings
                           if not severity_module.is_ignored(f)]
        if schematic_report is not None:
            schematic_report.findings = [
                f for f in schematic_report.findings
                if not severity_module.is_ignored(f)]
    return outcome


def restore_policy(layout, schematic_report, all_layout, all_schematic) -> None:
    """Put every finding back, at the severity its check computed.

    The lists come from the review that produced them, so this restores what
    `ignore` withheld as well as what a level change moved.
    """
    layout.findings = list(all_layout)
    severity_module.restore(layout.findings)
    if schematic_report is not None:
        schematic_report.findings = list(all_schematic)
        severity_module.restore(schematic_report.findings)


def review_path(target, schematic=None, **kwargs) -> ReviewResult:
    """`discover` then `review`. The whole pipeline, in one call."""
    return review(discover(target, schematic=schematic), **kwargs)
