"""An opt-in `review/` folder, and where the extraction cache lives.

The user, 2026-08-03, asked what survives a review and where it goes:

> im fine with none of the audit surviving except for records in the claude
> session context and chat of that session. I dont think the audit should by
> default write records into the working directory of the project that its
> reviewing, but it should offer to do so - offer to make a "review" folder
> which contains all the review data from previous runs, and regenerate the
> schematic parsing when the schematic was detected to have changed between
> reviews and regenerate the layout parsing when the layout was detected to
> have changed between reviews.
>
> It definitely shouldnt randomly hoard findings somewhere.

So, in order:

**The default does not change.** Findings are printed and nothing is written.
Nothing in this module runs unless a `review/` folder already exists beside
the board or the front end was told to make one.

**The folder is the user's.** It sits beside their board, it is plain JSON,
and it is theirs to read, commit or delete. `create` writes a README saying
exactly that, because a directory that appears in someone's project without
explanation is the thing the ruling is against.

**Staleness is per artefact.** A run records a fingerprint of the layout and a
fingerprint of the schematic *separately*, so the next run can say which one
moved. `Board.from_kicad` already does this for the layout against its own
cache (`model.py`); this extends the idea across runs and to the schematic.

The honest note about the schematic side
-----------------------------------------
That wording implies re-parsing is the expensive thing being avoided.
Measured: `schematic.netlist` takes **0.08 s** on a 195-net board and **0.19 s**
on a 230-net hierarchical one. Caching that would save nothing worth a cache
file. So what is built here is the *change detection* — which artefact moved
since the last review — because that is what actually has value: it is the
difference between "re-reviewing a board" and "reviewing what changed". The
layout extraction, which does cost about a second and is megabytes, keeps its
existing cache; this module only decides **where** that cache lives.

Where the extraction cache lives — the part that was a real leak
-----------------------------------------------------------------
It used to sit in the *install* directory and accumulate forever. After one
outside review the installed skill held a 602 KB geometry dump of a board
belonging to a different company, and nothing pruned it. Ruled:

| | |
|---|---|
| a `review/` folder exists | inside it, next to the review data, the user's to delete |
| no `review/` folder | the OS temp directory, cleared by the OS |
| the skill install directory | **never**, in either case |
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

#: The folder's name, beside the board. One name, so a second session finds
#: what the first one made.
FOLDER_NAME = "review"

#: Where the extraction cache goes when there is no review folder. Under the
#: OS temp directory, which is the one place a cache is allowed to accumulate
#: because the OS clears it.
TEMP_CACHE_NAME = "pcbreview-cache"

README = """\
# pcbreview review folder

Written by the pcbreview design-review skill, with your agreement. It holds
one record per review run of the board beside it, and the cached geometry
extraction those runs share.

Nothing here is needed to run a review. **Delete the whole folder whenever you
like** — the next run will simply have nothing to compare against. Commit it
if you want the review history in version control; ignore it if you do not.

    runs/<timestamp>/run.json   one review: what was measured and what moved
    latest.json                 a pointer to the most recent run
    .cache/                     the KiCad geometry extraction, regenerated
                                whenever the board changes

Each run records a fingerprint of the layout and of the schematic separately,
so the next review can tell you which of the two changed since the last one.
"""


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _hash_file(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint(paths) -> str | None:
    """A content hash over one artefact's files. `None` if it has none.

    Content rather than mtime, deliberately. `Board.from_kicad`'s own cache is
    right to use mtime — it is guarding a derived file against its source in
    the same session — but "did the schematic change since the last review"
    must not answer yes because someone opened and saved it unchanged, and
    must not answer no because a file was restored from a backup.

    A hierarchical schematic is many files, so this takes a list. Its sheet
    paths come from `Schematic.sheets`, which is the tree KiCad actually
    walks — never a glob of the directory (a `.kicad_sch` sitting beside a
    board is not necessarily part of it).
    """
    entries = sorted({Path(p).resolve() for p in paths if p})
    entries = [p for p in entries if p.exists()]
    if not entries:
        return None
    digest = hashlib.sha1()
    for path in entries:
        digest.update(path.name.encode())
        digest.update(_hash_file(path).encode())
    return digest.hexdigest()[:16]


def schematic_files(schematic) -> list[Path]:
    """Every sheet file a parsed `Schematic` was built from."""
    paths = [Path(schematic.root)]
    for sheet in getattr(schematic, "sheets", ()):
        path = getattr(sheet, "path", None)
        if path:
            paths.append(Path(path))
    return paths


# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------

def folder_for(board_path) -> Path:
    """Where a board's review folder would be. Does not create it."""
    return Path(board_path).resolve().parent / FOLDER_NAME


def find(board_path) -> Path | None:
    """The board's review folder if the user has one, else `None`.

    This is the whole opt-in mechanism: everything else in this module is
    reached only through a folder that exists.
    """
    candidate = folder_for(board_path)
    return candidate if candidate.is_dir() else None


def create(board_path) -> Path:
    """Make the review folder. **Only ever call this once the user agrees.**"""
    root = folder_for(board_path)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")
    return root


def cache_dir_for(board_path) -> Path:
    """Where this board's geometry extraction is cached.

    Never the install directory — see the module docstring. Returns a path;
    creating it is `Board.from_kicad`'s job, as it already was.
    """
    root = find(board_path)
    if root is not None:
        return root / ".cache"
    return Path(tempfile.gettempdir()) / TEMP_CACHE_NAME


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """One recorded review."""
    when: str
    board: str
    #: artefact name -> content fingerprint, e.g. `{"layout": "…", "schematic": "…"}`
    artefacts: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {"when": self.when, "board": self.board,
                "artefacts": self.artefacts, "summary": self.summary,
                "findings": self.findings}

    @classmethod
    def from_json(cls, data: dict) -> "Run":
        return cls(when=data.get("when", ""), board=data.get("board", ""),
                   artefacts=data.get("artefacts", {}),
                   summary=data.get("summary", {}),
                   findings=data.get("findings", []))


def previous(root) -> Run | None:
    """The most recently recorded run, or `None` on the first review."""
    latest = Path(root) / "latest.json"
    if not latest.exists():
        return None
    try:
        return Run.from_json(json.loads(latest.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        # A corrupt record must not stop a review. It is a comparison, not an
        # input to any measurement.
        return None


def changed_since(root, artefacts: dict) -> dict:
    """Per artefact: `new`, `changed` or `unchanged`, against the last run.

    Per artefact and not per board, which is the point of the ruling: a board
    whose layout moved and whose schematic did not needs only the layout side
    re-examined, and saying which is which is the thing that makes a second
    review cheaper than the first.
    """
    last = previous(root)
    if last is None:
        return {name: "new" for name in artefacts}
    return {name: ("unchanged" if last.artefacts.get(name) == value
                   else "changed" if name in last.artefacts else "new")
            for name, value in artefacts.items()}


def record(root, board: str, artefacts: dict, summary: dict,
           findings: list) -> Path:
    """Write this run into the folder and point `latest.json` at it.

    Every run is kept. The folder is small — a run is the findings' own
    `summary()` output, tens of kilobytes — and keeping them is the whole
    reason the folder exists: without a previous run, "what changed since the
    last review" is not answerable at all.
    """
    root = Path(root)
    when = time.strftime("%Y-%m-%dT%H-%M-%S")
    run = Run(when=when, board=board, artefacts=artefacts, summary=summary,
              findings=findings)
    directory = root / "runs" / when
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(run.to_json(), indent=1, sort_keys=True, default=str)
    (directory / "run.json").write_text(payload, encoding="utf-8")
    (root / "latest.json").write_text(payload, encoding="utf-8")
    return directory / "run.json"


def compare(before: Run | None, findings: list) -> dict:
    """What moved between the last recorded run and this one.

    Keyed the same way `tests/dump_audit.py` keys its diff — check, nets,
    title — because that key was already shown to be stable across runs that
    changed nothing. `score` is deliberately *not* in the key here: this is a
    report for a human asking what changed on their board, and a finding whose
    score moved by 0.1 is the same finding, not a loss and a gain.
    """
    if before is None:
        return {"first_run": True, "resolved": [], "new": [], "changed": []}

    def key(entry):
        return (entry.get("check"), tuple(entry.get("nets", ())),
                entry.get("title"))

    was = {key(f): f for f in before.findings}
    now = {key(f): f for f in findings}
    changed = [k for k in set(was) & set(now)
               if was[k].get("severity") != now[k].get("severity")]
    return {
        "first_run": False,
        "since": before.when,
        "resolved": sorted(f"{k[0]}: {k[2]}" for k in set(was) - set(now)),
        "new": sorted(f"{k[0]}: {k[2]}" for k in set(now) - set(was)),
        "changed": sorted(
            f"{k[0]}: {k[2]} — {was[k].get('severity')} -> "
            f"{now[k].get('severity')}" for k in changed),
    }
