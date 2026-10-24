"""The shipped fab stackup library — Gate 1 turned into a list.

The wall this removes
---------------------
the design brief §2.2 calls the stackup "the wall". Gate 1 refuses to review a board
without a validated fabrication stackup, and it is right to — every impedance,
coupling and reference-plane number depends on real dielectric heights, and a
review run on invented ones would be confident nonsense rather than obviously
absent.

But until now the only way past that wall was to transcribe the fab's table by
hand. In the app that shows up as a `PipelineError` telling the user to go and
run `python -m pcbreview.stackup_cli transcribe`, which breaks the design brief §2.10's
first principle — *"the user never sees a YAML, a command, or a file path
unless they ask"* — as bluntly as it can be broken. It is the last place in the
app where the answer to "what do I do now" is a command line.

So this module offers the boards that have already been transcribed, filtered
to the ones that could physically be this board.

What it is not
--------------
**It is not a guess, and it must never become one.** Every entry is a
transcription of something a fab published, with the provenance recorded beside
it — `stackups/README.md` states the rules and this module enforces the
mechanical half of them at load time. An entry whose rows do not parse, whose
targets do not parse, or which claims a layer count its own table contradicts is
**dropped with a reason** rather than offered. A library that quietly served a
half-valid stackup would be worse than no library, because Gate 1 exists
precisely to stop numbers being invented upstream of the physics.

**It does not decide anything.** Choosing an entry is a click by a person, the
entry is filtered by copper-layer count only, and what lands on disk is the same
`<board>.stackup.yaml` the CLI would have written — through
`stackup_cli.transcribe`, the same function, so there is one path from a fab
table to a stackup and not two.

**It does not skip verification.** `apply()` writes the file and `verify()`
re-solves every target from its own stated geometry against the number the fab
published. That is the check that catches a misread dielectric, and it runs
before any review does.

Why targets are stored as the CLI's own `key=value` strings
-----------------------------------------------------------
Because `stackup_cli.parse_target` is the one parser, and a second format would
have to be kept in step with it. It also keeps a subtlety that a friendlier
schema would lose: `to_copper=none` means *non-coplanar* and omitting the field
is an error, not a default. `parse_target`'s docstring explains why at length,
and the six-layer board in this repository is the mistake it is explaining.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pcbreview import stackup_cli                            # noqa: E402
from pcbreview.stackup import parse_jlc_table                # noqa: E402

#: Where the shipped entries live: inside the installed package, so the library
#: travels with the skill and a review on a fresh machine has it. Not in the
#: user's project — `review.py` rules that out — and not in a directory the
#: install could be separated from.
#:
#: `PCBREVIEW_STACKUPS` points at a second directory of the user's own entries,
#: which are loaded *after* the shipped ones and may not shadow a shipped code:
#: an entry is a transcription of something a fab published, and silently
#: replacing one with a local file of the same name would break exactly the
#: provenance guarantee the library exists for.
LIBRARY_DIR = Path(__file__).resolve().parent / "stackup_library"

#: Where a user keeps entries this install did not ship.
LIBRARY_ENV = "PCBREVIEW_STACKUPS"

#: What `stackup_cli` writes and `inputs.resolve_stackup` looks for.
STACKUP_SUFFIX = ".stackup.yaml"


class LibraryError(Exception):
    """Raised when a chosen entry cannot be applied to a board."""


@dataclass
class Entry:
    """One fab stackup somebody has already transcribed and verified."""
    code: str
    fab: str
    title: str
    summary: str
    copper_layers: int
    finished_thickness_mm: float | None
    rows: str
    targets: list[str]
    provenance: str
    path: Path

    def as_dict(self) -> dict:
        return {"code": self.code, "fab": self.fab, "title": self.title,
                "summary": self.summary, "copper_layers": self.copper_layers,
                "finished_thickness_mm": self.finished_thickness_mm,
                "targets": list(self.targets),
                "provenance": self.provenance,
                "layer_names": [layer.name for layer in
                                parse_jlc_table(self.rows).copper_layers]}


@dataclass
class Rejected:
    """An entry that is on disk and is not offered, and why.

    Kept and reported rather than swallowed. A stackup file someone added that
    silently never appears is a worse outcome than one that appears in a list
    of problems — and `stackups/README.md` promises that adding two files is
    all it takes, so the app owes an answer when it was not enough.
    """
    path: Path
    reason: str

    def as_dict(self) -> dict:
        return {"file": self.path.name, "reason": self.reason}


@dataclass
class Library:
    entries: list[Entry] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)

    def for_layers(self, count: int) -> list[Entry]:
        """Entries that could be this board. Layer count is the only filter.

        Finished thickness is deliberately *not* filtered on, only displayed.
        A board file does not state what the fab will build it to, and
        rejecting a 1.6 mm stackup because someone wanted 1.0 mm would hide
        the entry that is almost certainly right.
        """
        return [e for e in self.entries if e.copper_layers == count]

    def get(self, code: str) -> Entry | None:
        return next((e for e in self.entries if e.code == code), None)


def load(directory: Path | str | None = None) -> Library:
    """Every valid entry in the library, and every invalid one with its reason.

    Two directories, in order: the shipped one, then whatever
    `PCBREVIEW_STACKUPS` names. A user entry may **not** shadow a shipped code
    — it is reported as rejected instead. The library's whole value is that an
    entry is a transcription of something a fab published, and a local file
    silently standing in for a shipped code is exactly how that stops being
    true without anyone noticing.

    Naming `directory` explicitly loads that one alone, which is what the tests
    and `--library` want.
    """
    if directory is not None:
        return _load_one(Path(directory), Library())

    library = _load_one(LIBRARY_DIR, Library())
    extra = os.environ.get(LIBRARY_ENV)
    if extra:
        shipped = {entry.code for entry in library.entries}
        mine = _load_one(Path(extra).expanduser(), Library())
        for entry in mine.entries:
            if entry.code in shipped:
                library.rejected.append(Rejected(
                    entry.path,
                    f"{entry.code} is already a shipped entry, and a local "
                    f"file may not stand in for one — rename it"))
            else:
                library.entries.append(entry)
        library.rejected.extend(mine.rejected)
        library.entries.sort(key=lambda e: e.code)
    return library


def _load_one(root: Path, library: "Library") -> Library:
    if not root.is_dir():
        return library
    for path in sorted(root.glob("*.yaml")):
        try:
            library.entries.append(_entry(path))
        except LibraryError as exc:
            library.rejected.append(Rejected(path, str(exc)))
    return library


def _entry(path: Path) -> Entry:
    """One file into an `Entry`, or `LibraryError` saying what is wrong.

    Everything here is checked against the *rows*, not taken on the yaml's
    word: an entry that says four layers over a two-layer table would produce
    a stackup that `bind_stackup` rejects much later and much less clearly.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LibraryError(f"unreadable: {exc}") from None
    if not isinstance(data, dict):
        raise LibraryError("not a mapping")

    for required in ("code", "rows", "targets", "provenance", "copper_layers"):
        if not data.get(required):
            raise LibraryError(
                f"no {required}: — stackups/README.md says what an entry needs, "
                f"and provenance is required because the library's whole value "
                f"is that it is not guesswork")

    rows_path = path.parent / str(data["rows"])
    try:
        rows = rows_path.read_text(encoding="utf-8")
    except OSError:
        raise LibraryError(f"rows file {rows_path.name} is missing") from None

    try:
        parsed = parse_jlc_table(rows)
    except Exception as exc:                                  # noqa: BLE001
        raise LibraryError(f"{rows_path.name} does not parse: {exc}") from None

    found = len(parsed.copper_layers)
    if found != int(data["copper_layers"]):
        raise LibraryError(
            f"says copper_layers: {data['copper_layers']} but "
            f"{rows_path.name} describes {found}")

    targets = [str(t) for t in data["targets"]]
    for spec in targets:
        try:
            stackup_cli.parse_target(spec)
        except stackup_cli.TranscribeError as exc:
            raise LibraryError(f"target {spec!r}: {exc}") from None

    return Entry(
        code=str(data["code"]),
        fab=str(data.get("fab") or "unknown"),
        title=str(data.get("title") or data["code"]),
        summary=str(data.get("summary") or ""),
        copper_layers=found,
        finished_thickness_mm=(
            float(data["finished_thickness_mm"])
            if data.get("finished_thickness_mm") is not None else None),
        rows=rows,
        targets=targets,
        provenance=" ".join(str(data["provenance"]).split()),
        path=path,
    )


# ---------------------------------------------------------------------------
# Applying one to a board
# ---------------------------------------------------------------------------

def destination(board_path: Path | str) -> Path:
    """Where a chosen stackup lands: `<board>.stackup.yaml`, beside the board.

    The same place `inputs.resolve_stackup` already looks, so the next review
    finds it with no further plumbing — and the same place a hand-written one
    would be, so the two are interchangeable.
    """
    board = Path(board_path)
    return board.with_name(board.stem + STACKUP_SUFFIX)


@dataclass
class Applied:
    """What `apply` did, in enough detail for the UI to say it."""
    code: str
    path: Path
    replaced: Path | None            # the backup, if one was needed
    verification: dict

    def as_dict(self) -> dict:
        return {"code": self.code, "path": str(self.path),
                "replaced": str(self.replaced) if self.replaced else None,
                "verification": self.verification}


def apply(entry: Entry, board_path: Path | str, copper_layers: list[str],
          overwrite: bool = False) -> Applied:
    """Write this entry as the board's stackup, then verify it.

    Goes through `stackup_cli.transcribe`, which is the same call the command
    line makes: the library supplies the rows and the targets that a user would
    otherwise have typed, and nothing else about the path differs. One route
    from a fab table to a stackup, not two.

    **An existing stackup is never quietly replaced.** Without ``overwrite``
    this refuses; with it, the old file is copied aside first. A stackup may be
    a transcription somebody checked by hand against a screenshot, and losing
    that to a mis-click is the kind of thing that is only noticed much later,
    when a number is wrong for a reason nobody can find. the facts-tab record §6 takes the
    same position for `netintent.yaml` and this is deliberately the same shape.
    """
    target = destination(board_path)
    replaced: Path | None = None
    if target.exists():
        if not overwrite:
            raise LibraryError(
                f"{target.name} already exists. Reviewing uses it as it is; "
                f"replacing it needs to be asked for, because it may be a "
                f"transcription that was checked by hand.")
        replaced = _back_up(target)

    try:
        document = stackup_cli.transcribe(
            entry.rows, copper_layers, targets=entry.targets, name=entry.code)
    except stackup_cli.TranscribeError as exc:
        raise LibraryError(str(exc)) from None

    # Written whole, then moved into place, so a crash midway cannot leave a
    # half-written stackup that `load_and_validate` would read as a real one.
    scratch = target.with_suffix(target.suffix + ".partial")
    scratch.write_text(document, encoding="utf-8")
    scratch.replace(target)

    return Applied(code=entry.code, path=target, replaced=replaced,
                   verification=verify(target, board_path))


def _back_up(path: Path) -> Path:
    """Copy a stackup aside before it is replaced, without ever clobbering.

    Numbered rather than timestamped so two clicks a second apart cannot land
    on one name, and so the ordering is readable.
    """
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.replaced-{index}")
        if not candidate.exists():
            candidate.write_bytes(path.read_bytes())
            return candidate
    raise LibraryError("999 replaced stackups already beside this board")


# ---------------------------------------------------------------------------
# Verification — the panel the design brief §2.2 asks for
# ---------------------------------------------------------------------------

def verify(stackup_path: Path | str, board_path: Path | str | None = None
           ) -> dict:
    """Re-solve every target from its own geometry, against what the fab said.

    This is `stackup_cli.verify` rendered as data rather than as text. It is
    the step that turns *"trust the transcription"* into *"the physics agrees"*,
    and the design brief §2.2 wants it shown immediately and always — so it runs on
    apply rather than waiting for someone to ask.

    It costs a field solve per target at a fine mesh. Since §35 those solves
    are cached across runs and across boards, so the second board on a stackup
    verifies instantly; the first pays a second or two.

    Never raises. A verification that could not run is reported as such, in
    keeping with the rule that absence is never shown as a pass.
    """
    from pcbreview.model import Board
    from pcbreview.stackup import load_and_validate

    stackup_path = Path(stackup_path)
    try:
        # The board's own copper layer names, so `validate` can check the
        # stackup describes *this* board rather than merely being internally
        # consistent. `from_kicad` is the cached extraction, not a re-parse.
        copper = (Board.from_kicad(board_path).copper_layers
                  if board_path is not None else None)
        loaded, _problems = load_and_validate(stackup_path, copper)
        # `verify` re-runs `validate` itself, and takes the board path for its
        # third check — the stackup KiCad has recorded inside the board file.
        result = stackup_cli.verify(loaded, copper, board_path)
    except Exception as exc:                                  # noqa: BLE001
        return {"ran": False,
                "why_not": f"{type(exc).__name__}: {exc}",
                "ok": False, "targets": [], "problems": [], "report": ""}

    return {
        "ran": True,
        "ok": result.ok,
        "stackup_name": result.stackup_name,
        "expected_residual": stackup_cli.EXPECTED_RESIDUAL,
        "targets": [_target(t) for t in result.targets],
        "problems": [{"severity": p.severity, "text": str(p)}
                     for p in result.problems],
        "board_comparison": [{"severity": p.severity, "text": str(p)}
                             for p in result.board_comparison],
        "constrained_layers": list(result.constrained_layers),
        "unconstrained_layers": list(result.unconstrained_layers),
        # The text form too. It carries the wording the CLI spent a long time
        # getting right, and it is what a user copies into a message when
        # something disagrees.
        "report": result.report(),
    }


def _target(verdict) -> dict:
    return {
        "verdict": verdict.verdict,
        "text": str(verdict),
        "target_ohm": getattr(verdict, "target_ohm", None),
        "modelled_ohm": getattr(verdict, "modelled_ohm", None),
        "error": getattr(verdict, "error", None),
        "consistent_with": list(getattr(verdict, "consistent_with", []) or []),
    }


# ---------------------------------------------------------------------------
# What the launch page asks for
# ---------------------------------------------------------------------------

def offer(board_path: Path | str, current: Path | str | None = None) -> dict:
    """The library, filtered to this board, plus what is already bound.

    **Deliberately does no solving.** Listing is instant; verification costs a
    field solve per target and belongs to the moment somebody picks or asks,
    not to opening a page. `apply()` verifies what it wrote, and
    `verify()` answers on demand.

    Entries that do not fit are **counted rather than hidden**. A library that
    silently showed one of three would leave a user wondering whether the one
    they wanted had been shipped at all, and the reason a stackup does not fit
    is exactly the kind of fact this app is supposed to state.
    """
    from pcbreview.model import Board

    library = load()
    payload: dict = {
        "current": _current(current, library),
        "candidates": [],
        "unavailable": [],
        "rejected": [r.as_dict() for r in library.rejected],
    }
    try:
        copper = Board.from_kicad(board_path).copper_layers
    except Exception as exc:                                  # noqa: BLE001
        # The board could not be read. Nothing here is worth guessing at, and
        # the review would fail for the same reason a moment later.
        payload["why_not"] = f"{type(exc).__name__}: {exc}"
        return payload

    payload["board"] = {"copper_layers": copper, "layer_count": len(copper)}
    fits = library.for_layers(len(copper))
    payload["candidates"] = [e.as_dict() for e in fits]
    payload["unavailable"] = [
        {"code": e.code, "title": e.title,
         "why_not": f"{e.copper_layers} copper layers, this board has "
                    f"{len(copper)}"}
        for e in library.entries if e not in fits]
    payload["destination"] = str(destination(board_path))
    return payload


def _current(path: Path | str | None, library: "Library") -> dict | None:
    """What is bound now, read cheaply — the name only, no validation."""
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        return None
    name = None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            name = data.get("name")
    except (OSError, yaml.YAMLError):
        # A stackup that will not parse is still the file that is there, and
        # saying so is more useful than pretending there is none.
        name = None
    return {"path": str(path), "name": name,
            "from_library": bool(name and library.get(str(name)))}


# ---------------------------------------------------------------------------
# The command line — Gate 1's answer, without a transcription
# ---------------------------------------------------------------------------

USAGE = """\
usage: python -m pcbreview.stackups <project folder | board.kicad_pcb>
       python -m pcbreview.stackups <board> use <CODE> [--replace]
       python -m pcbreview.stackups <board> verify [<stackup.yaml>]

With no verb: list the shipped fab stackups that have this board's copper layer
count, with the provenance of each. Nothing is written and nothing solves.

  use <CODE>   write <board>.stackup.yaml from that entry and immediately
               re-solve every impedance target from its own stated geometry
               against the number the fab published. --replace is required if a
               stackup is already there; the old one is copied aside first
  verify       re-solve an existing stackup's targets. Costs a field solve per
               target, cached across runs

An entry is a transcription of something a fab published, never an estimate.
Nothing here estimates a dielectric height, and nothing here chooses for the
user: show them the list and let them say which board they are having built.

PCBREVIEW_STACKUPS points at a directory of your own entries, loaded after the
shipped ones. See stackup_library/README.md for what an entry must contain.
"""


def _quoted(target: str) -> str:
    """A path as it must be typed back.

    Board files live in folders with spaces in them far more often than not,
    and a hint that does not run when pasted is worse than no hint.
    """
    return f'"{target}"' if any(c in target for c in " ()&") else target


def _print_offer(payload: dict, board: Path, target: str) -> int:
    import sys

    if payload.get("why_not"):
        sys.stderr.write(f"{board.name}: {payload['why_not']}\n")
        return 2
    info = payload.get("board") or {}
    print(f"{board.name}: {info.get('layer_count', '?')} copper layer(s) — "
          f"{', '.join(info.get('copper_layers') or [])}")
    current = payload.get("current")
    if current:
        origin = ("from the shipped library" if current["from_library"]
                  else "not one of the shipped entries")
        print(f"  already bound: {current['name'] or '(unnamed)'} "
              f"({origin})\n                 {current['path']}")
    print()
    candidates = payload.get("candidates") or []
    print(f"{len(candidates)} shipped stackup(s) fit this board")
    for entry in candidates:
        thickness = (f"{entry['finished_thickness_mm']} mm finished"
                     if entry.get("finished_thickness_mm")
                     else "finished thickness not stated by the fab")
        print(f"\n  {entry['code']}  —  {entry['fab']}, {thickness}")
        print(f"      {entry['title']}")
        if entry.get("summary"):
            print(f"      {entry['summary']}")
        print(f"      layers   {', '.join(entry['layer_names'])}")
        print(f"      targets  {'; '.join(entry['targets']) or '(none)'}")
        print(f"      source   {entry['provenance']}")
    for entry in payload.get("unavailable") or []:
        print(f"\n  {entry['code']} does not fit: {entry['why_not']}")
    for entry in payload.get("rejected") or []:
        print(f"\n  REJECTED {entry['file']}: {entry['reason']}")
    if candidates:
        # Every candidate, not the first one. Which stackup a board is being
        # built on is a fact about an order somebody placed; nothing here knows
        # it, and offering one code as though it were the answer invites it to
        # be taken as one.
        print("\nApply one — ASK THE USER WHICH; do not choose for them:")
        for entry in candidates:
            print(f"  python -m pcbreview.stackups {_quoted(target)} use "
                  f"{entry['code']}")
    else:
        print("\nNothing shipped fits. Transcribe the fab's own table with "
              "`python -m pcbreview.stackup_cli transcribe` — do not invent "
              "one.")
    return 0


def _print_verification(verification: dict) -> int:
    import sys

    if not verification.get("ran"):
        sys.stderr.write(f"verification could not run: "
                         f"{verification.get('why_not')}\n")
        return 2
    print(verification["report"])
    return 0 if verification.get("ok") else 1


def main(argv: list[str]) -> int:
    import sys

    from pcbreview import console_utf8

    console_utf8()

    if "--help" in argv or "-h" in argv or len(argv) < 2:
        sys.stderr.write(USAGE)
        return 0 if ("--help" in argv or "-h" in argv) else 2

    positional = [a for a in argv[1:] if not a.startswith("--")]
    from pcbreview import pipeline

    try:
        project = pipeline.discover(positional[0])
    except pipeline.PipelineError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    board = project.board

    verb = positional[1] if len(positional) > 1 else None
    if verb is None:
        return _print_offer(offer(board, project.stackup), board,
                            positional[0])

    if verb == "verify":
        target = Path(positional[2]) if len(positional) > 2 else project.stackup
        if target is None:
            sys.stderr.write(f"{board.name}: there is no stackup to verify.\n")
            return 2
        print(f"verifying {Path(target).name} against {board.name}")
        return _print_verification(verify(target, board))

    if verb != "use":
        sys.stderr.write(f"unknown verb {verb!r}\n{USAGE}")
        return 2

    if len(positional) < 3:
        sys.stderr.write("use needs a stackup code\n")
        return 2
    code = positional[2]
    library = load()
    entry = library.get(code)
    if entry is None:
        known = ", ".join(e.code for e in library.entries) or "(none)"
        sys.stderr.write(f"no shipped stackup is called {code!r}. "
                         f"Known: {known}\n")
        return 2

    from pcbreview.model import Board

    try:
        copper = Board.from_kicad(board).copper_layers
    except Exception as exc:                                  # noqa: BLE001
        sys.stderr.write(f"{board.name} could not be read: {exc}\n")
        return 2
    if entry.copper_layers != len(copper):
        sys.stderr.write(
            f"{code} describes {entry.copper_layers} copper layers and "
            f"{board.name} has {len(copper)}. Applying it would produce a "
            f"stackup that does not describe this board.\n")
        return 2

    try:
        applied = apply(entry, board, copper, overwrite="--replace" in argv)
    except LibraryError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    print(f"wrote {applied.path}")
    if applied.replaced:
        print(f"  the stackup that was there is now {applied.replaced.name}")
    print()
    return _print_verification(applied.verification)


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
