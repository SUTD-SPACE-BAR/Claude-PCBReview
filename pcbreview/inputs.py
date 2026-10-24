"""What a file *is*, decided by reading it — never by where it sat on a command line.

The ruling, 2026-08-06:

    User inputs can appear in random order when attached to the claude session
    chat during prompting. They should be looked at by claude for their
    contents and they should be looked at by file type, not just assume that
    the user is going to follow some convention of what order to give claude
    the documents in.

    I should be able to give my stackup, my datasheets, my thermal
    explanations, my system functionality descriptions, whatever else I feel is
    useful to give to claude as context, all at once and in any order when im
    attaching files.

What went wrong without this
----------------------------
`audit.main` read its arguments by position: board, stackup, schematic. An
outside session omitted the stackup — because `SKILL.md` told them it would be
found automatically, which was false — so the **schematic slid into the stackup
slot**. The tool handed a `.kicad_sch` to the YAML loader and died twelve frames
down with::

    yaml.scanner.ScannerError: found character '\\t' that cannot start any token
        \t(version 20260306)

A tab-character complaint, for "you put a schematic where the stackup goes".
It cost a full review run, and nothing in the message pointed at the cause.

Why sniffing the *content* and not the extension
------------------------------------------------
Three of the six inputs are `.yaml` and they are not interchangeable: a stackup
in the thermal slot would be silently ignored, and a thermal manifest in the
stackup slot fails Gate 1 with a validation error about missing layers. The
extension cannot tell them apart; their top-level keys can, and unambiguously —
`layers` for a stackup, `parts` for thermal, `nets` for net intent, and no real
file of one kind carries another's key.

**A wrong answer here must be loud, never silent.** Everything below returns
`unknown` rather than guessing, and the caller refuses with the filename.
"""

from __future__ import annotations

from pathlib import Path

#: The kinds this recognises. `datasheet` is included deliberately even though
#: no engine here reads one: a user hands over PDFs with everything else, and
#: "that is a datasheet — give it to Claude, not to the auditor" is a far more
#: useful answer than a parse error. `unknown` is a real answer too.
KINDS = ("board", "schematic", "stackup", "thermal", "netintent", "bom",
         "powerpaths", "datasheet", "unknown")

#: Which top-level YAML key identifies each manifest. Order matters only in
#: that a file carrying two of them is ambiguous and is reported as such.
_YAML_SIGNATURES = {
    "layers": "stackup",
    "parts": "thermal",
    "nets": "netintent",
    "paths": "powerpaths",
}

#: How much of a file to read to identify it. A KiCad board's first line is its
#: format token; a manifest's top-level keys are at the left margin. Nothing
#: here needs more, and reading a 40 MB `.kicad_pcb` in full to learn it is a
#: board would make this cost more than the audit.
_SNIFF_BYTES = 8192


def _head(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(_SNIFF_BYTES)
    except OSError:
        return ""


def classify(path) -> str:
    """What kind of input this file is. One of `KINDS`.

    Reads the file. A path that does not exist, or cannot be read, is
    `unknown` — this is identification, not validation, and the caller says so
    with the filename attached.
    """
    path = Path(path)
    if not path.is_file():
        return "unknown"

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "datasheet"
    if suffix == ".csv":
        return "bom"

    head = _head(path)

    # KiCad's own format tokens, checked in the content rather than trusted
    # from the extension: a `.kicad_sch` saved under a `.kicad_pcb` name is
    # rare, and a renamed file is exactly the case a position-free router is
    # supposed to survive.
    if "(kicad_pcb" in head:
        return "board"
    if "(kicad_sch" in head:
        return "schematic"

    if suffix in (".yaml", ".yml"):
        return _classify_yaml(path, head)

    # An extension we know, with content that does not match it, is worth
    # naming rather than shrugging at.
    if suffix in (".kicad_pcb", ".kicad_sch"):
        return "unknown"
    return "unknown"


def _classify_yaml(path: Path, head: str) -> str:
    """Which of the three manifests this is, from its top-level keys."""
    import yaml

    try:
        document = yaml.safe_load(open(path, "r", encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # Malformed YAML is still identifiably a manifest attempt; fall back to
        # the raw text so the caller can say which slot it was meant for and
        # let the real loader produce the real syntax error.
        matched = [kind for key, kind in _YAML_SIGNATURES.items()
                   if f"\n{key}:" in f"\n{head}"]
        return matched[0] if len(matched) == 1 else "unknown"

    if not isinstance(document, dict):
        return "unknown"
    matched = [kind for key, kind in _YAML_SIGNATURES.items()
               if key in document]
    # Two signatures in one document is ambiguous and must not be guessed at.
    return matched[0] if len(matched) == 1 else "unknown"


class InputError(Exception):
    """Raised when the files given cannot be routed to their slots."""


def route(paths, *, require_board: bool = True) -> dict:
    """Sort `paths` into slots by what each file is. Order is irrelevant.

    Returns a dict keyed by `KINDS`, values are paths (or `None`). Datasheets
    and unknowns are returned as lists under `datasheets` and `unknown`,
    because there can legitimately be several of the first and any number of
    the second.

    Two files claiming one slot is an error naming **both**, because the
    alternative is picking one and being silently wrong about which stackup a
    whole review was computed from.
    """
    slots: dict[str, object] = {k: None for k in KINDS if k not in
                                ("datasheet", "unknown")}
    slots["datasheets"] = []
    slots["unknown"] = []

    for path in paths:
        kind = classify(path)
        if kind == "datasheet":
            slots["datasheets"].append(str(path))
            continue
        if kind == "unknown":
            slots["unknown"].append(str(path))
            continue
        if slots[kind] is not None:
            raise InputError(
                f"two files were given for the {kind}: "
                f"{Path(str(slots[kind])).name} and {Path(str(path)).name}. "
                f"Give one, or say which with --{kind}")
        slots[kind] = str(path)

    if require_board and slots["board"] is None:
        given = ", ".join(Path(p).name for p in paths) or "nothing"
        raise InputError(
            f"no .kicad_pcb was given — a board is the one required input. "
            f"Received: {given}")
    return slots


def describe(slots: dict) -> str:
    """One line per routed file, for the run header.

    Printed on every run that routed anything by content, because a router
    that silently reassigns a user's arguments is worse than one that refuses.
    """
    lines = []
    for kind in ("board", "stackup", "schematic", "thermal", "netintent",
                 "bom", "powerpaths"):
        value = slots.get(kind)
        if value:
            lines.append(f"  {kind:10} {Path(value).name}")
    for pdf in slots.get("datasheets", []):
        lines.append(f"  {'datasheet':10} {Path(pdf).name} — not read by the "
                     f"auditor; read it yourself and write the thermal manifest")
    for other in slots.get("unknown", []):
        lines.append(f"  {'?':10} {Path(other).name} — not recognised, ignored")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Finding a stackup the user did not name, and refusing when two disagree
# ---------------------------------------------------------------------------

#: Where a stackup for this board may be sitting, in the order searched.
#: Beside the board is where `stackup_cli transcribe --out` is documented to
#: put it; the review folder is where an earlier run of this skill kept its
#: record, and the ruling of 2026-08-06 is explicit that a leftover there
#: counts:
#:
#:     I do agree that the stackup being omitted means the skill should see if
#:     theres a stackup leftover from previous runs of the skill.
def stackup_candidates(board_path) -> list[Path]:
    """Every stackup this board might already have, nearest first."""
    board = Path(board_path).resolve()
    found: list[Path] = []

    beside = board.with_suffix(".stackup.yaml")
    if beside.is_file():
        found.append(beside)

    from pcbreview import review as review_module
    root = review_module.find(board)
    if root is not None:
        for candidate in sorted(Path(root).glob("*.stackup.yaml")):
            if candidate not in found:
                found.append(candidate)

    return [p for p in found if classify(p) == "stackup"]


def _stackup_fingerprint(path) -> tuple:
    """What makes two stackups the *same* stackup, for the conflict test.

    Compared on the physics, not on the bytes: a re-transcription that renames
    a layer or reorders a key is the same board, and refusing over it would
    make the check noise. Two files agreeing on every layer's role, thickness
    and epsilon, and on every impedance target, will produce identical numbers
    everywhere — so they are interchangeable and there is nothing to ask about.
    """
    from pcbreview import stackup as stackup_module

    stack = stackup_module.load_stackup(path)
    layers = tuple((l.role, round(l.thickness_mm, 6),
                    round(l.epsilon_r or 0.0, 6),
                    round(l.copper_weight_oz or 0.0, 6))
                   for l in stack.layers)
    targets = tuple(sorted(
        (t.kind, round(t.impedance_ohm, 4), t.signal_layer,
         round(t.trace_width_mm, 6))
        for t in stack.impedance_targets))
    return layers, targets


def resolve_stackup(board_path, given=None) -> tuple[str | None, list[str]]:
    """`(stackup path, notes)` — discovering one, and refusing two that differ.

    Three outcomes, and the third is the ruling of 2026-08-06:

    * **given, and nothing else exists** — use it.
    * **not given** — search `stackup_candidates`. `SKILL.md` promised this
      behaviour long before the code had it, and a session that believed the
      document lost a whole run to it. Gate 1 is unweakened: a discovered file
      still has to validate, and finding nothing still refuses.
    * **given, and a *different* one already exists** — refuse, naming both.

      > If a new stackup is given and there was an old stackup already, ask the
      > user which one to use, if they arent identical.

      The engine cannot ask anybody, so it refuses with both paths and what
      differs, and `SKILL.md` tells the front end to put the question to the
      user. `--stackup PATH` is the answer coming back: it means *"I have
      decided"* and skips this check entirely.

    Identical-in-physics files are not a conflict (see `_stackup_fingerprint`).
    """
    notes: list[str] = []
    existing = stackup_candidates(board_path)

    if given is None:
        if not existing:
            return None, notes
        chosen = existing[0]
        notes.append(f"stackup not given; using {chosen} found beside the "
                     f"board or in its review folder")
        if len(existing) > 1:
            notes.append(
                f"{len(existing)} stackups exist for this board: "
                + ", ".join(str(p) for p in existing)
                + f". Used the first. Pass --stackup to choose another")
        return str(chosen), notes

    given_path = Path(given).resolve()
    others = [p for p in existing if p != given_path]
    if not others:
        return str(given), notes

    try:
        mine = _stackup_fingerprint(given_path)
    except Exception as exc:                                   # noqa: BLE001
        raise InputError(f"{Path(given).name} could not be read as a "
                         f"stackup: {exc}") from exc

    differing = []
    for other in others:
        try:
            if _stackup_fingerprint(other) != mine:
                differing.append(other)
        except Exception:                                      # noqa: BLE001
            differing.append(other)          # unreadable counts as different

    if not differing:
        notes.append(f"a second stackup exists ({others[0]}) and is identical "
                     f"in every layer and target, so it changes nothing")
        return str(given), notes

    raise InputError(
        f"two DIFFERENT stackups exist for this board and they do not agree, "
        f"so every impedance, coupling and crosstalk number depends on which "
        f"one is meant:\n"
        f"    given:    {given_path}\n"
        + "".join(f"    existing: {p}\n" for p in differing)
        + f"ASK THE USER which one to use, then pass it as --stackup <path>. "
        f"Do not guess: on one board here two transcriptions of the same "
        f"stackup moved crosstalk by a factor of 2.7.")
