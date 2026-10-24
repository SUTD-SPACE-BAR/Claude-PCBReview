"""The board registry — how a test asks for a board without naming one.

No board ships with this tool, and none can: a `.kicad_pcb` is somebody's
design. So every board-dependent test asks for a board *by role* and gets a
clean skip when no board fills that role. Two things follow, and both are the
point:

* **A missing board is not a failure.** A fresh install runs the whole suite
  green with no board at all. The board-dependent half announces itself as
  skipped and the skip message says how to switch it on.
* **Roles, not names.** A test wants "a six-layer board with impedance
  targets", not a particular project. Saying so in the role is what makes it
  obvious what a substitute board would have to provide.

### Pointing it at your own boards

Set `PCBREVIEW_TEST_BOARDS` to a YAML file:

    six_layer:
      pcb: ~/designs/mainboard/mainboard.kicad_pcb
      stackup: ~/designs/mainboard/mainboard.stackup.yaml
      note: six layers, impedance-controlled
    four_layer:
      pcb: ~/designs/sensor/sensor.kicad_pcb
      stackup: ~/designs/sensor/sensor.stackup.yaml

`pcb` is required; `stackup` is required for anything that runs the audit,
because Gate 1 refuses a board without one. The schematic is looked for beside
the board under the same stem, which is where KiCad puts it. Roles you do not
list are simply not installed and their tests skip.

The roles the shipped tests ask for are in `ROLES` below, each with what it
has to provide. Any board meeting the description will do — nothing is keyed
on a particular design.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

#: What each role has to provide, for the skip message and for anyone
#: assembling a registry of their own.
ROLES: dict[str, str] = {
    "six_layer": "six or more copper layers, with fab impedance targets in "
                 "its stackup — exercises inner-layer references and "
                 "broadside coupling across several plate separations",
    "four_layer": "four copper layers with poured zones — the ordinary case",
    "unreferenced": "a board that routes signal on layers with no adjacent "
                    "plane, so return-path and layer-transition findings "
                    "actually fire",
    "simple": "any straightforward board — used where a test only needs a "
              "real audit to run, and wants the fastest one available",
    "tiny": "any small board; used where the test only needs geometry to "
            "exist and wants to be fast. `test_facts.py`'s demotion tests "
            "also want it to carry at least one finding that names exactly "
            "one net and sits above the net-intent severity cap — they skip "
            "with that message when it does not",
    "no_stackup": "any board you have NOT written a stackup for — used to "
                  "check that Gate 1 refuses it",
}


@dataclass(frozen=True)
class BoardEntry:
    """One installed board: where it is, and what it is good for."""

    role: str
    pcb: Path
    stackup: Path | None
    note: str = ""

    @property
    def schematic(self) -> Path:
        """KiCad puts the root schematic beside the board, under one name."""
        return self.pcb.with_suffix(".kicad_sch")

    def installed(self) -> bool:
        return self.pcb.exists()


def _from_yaml(path: str) -> dict[str, BoardEntry]:
    import yaml

    data = yaml.safe_load(Path(path).read_text()) or {}
    registry = {}
    for role, entry in data.items():
        if not isinstance(entry, dict) or "pcb" not in entry:
            raise ValueError(
                f"{path}: role {role!r} needs at least a `pcb:` key")
        registry[role] = BoardEntry(
            role=role,
            pcb=Path(entry["pcb"]).expanduser(),
            stackup=(Path(entry["stackup"]).expanduser()
                     if entry.get("stackup") else None),
            note=entry.get("note", ROLES.get(role, "")),
        )
    return registry


def _load_registry() -> dict[str, BoardEntry]:
    override = os.environ.get("PCBREVIEW_TEST_BOARDS")
    if override:
        return _from_yaml(override)
    # `boards_local.py` is this repository's own development registry. It
    # names real designs, so it is the one file in `tests/` that is held back
    # from the shipped set — see the packaging notes. Its absence is the
    # normal case for anyone else and is not an error.
    try:
        from tests.boards_local import DEFAULTS  # type: ignore
    except ImportError:
        try:
            from boards_local import DEFAULTS  # type: ignore
        except ImportError:
            return {}
    return {
        role: BoardEntry(role=role, pcb=Path(pcb),
                         stackup=Path(stackup) if stackup else None,
                         note=note or ROLES.get(role, ""))
        for role, (pcb, stackup, note) in DEFAULTS.items()
    }


REGISTRY = _load_registry()


def _skip(role: str) -> None:
    """Skip with a message that is a set of instructions.

    "board not present" tells someone nothing, and the whole reason these
    tests skip rather than fail is that a stranger should get a green run and
    then be able to opt in.
    """
    wanted = REGISTRY[role].note if role in REGISTRY else ROLES.get(role, "")
    pytest.skip(
        f"no board installed for the role {role!r}. This test needs "
        f"{wanted or 'a board'}. Point PCBREVIEW_TEST_BOARDS at a YAML file "
        f"mapping roles to board paths to switch it on — see "
        f"tests/boards.py for the format and the full list of roles.")


def board_path(role: str) -> Path:
    """The board for `role`, or skip the calling test."""
    entry = REGISTRY.get(role)
    if entry is None or not entry.installed():
        _skip(role)
    return entry.pcb


def stackup_path(role: str) -> Path:
    """The stackup for `role`, or skip.

    A board with no stackup cannot be audited at all — Gate 1 — so a role
    registered without one is a skip rather than a `None` to be handled.
    """
    entry = REGISTRY.get(role)
    if entry is None or not entry.installed() or entry.stackup is None:
        _skip(role)
    return entry.stackup


def schematic_path(role: str) -> Path:
    """The schematic beside the board for `role`, or skip."""
    entry = REGISTRY.get(role)
    if entry is None or not entry.schematic.exists():
        _skip(role)
    return entry.schematic


def installed(role: str) -> bool:
    """Whether `role` is available, without skipping — for `skipif` marks and
    for parametrised lists, which are built at collection time."""
    entry = REGISTRY.get(role)
    return entry is not None and entry.installed()


def any_installed() -> list[str]:
    """Every installed role, for tests that will take whatever they can get."""
    return [role for role in REGISTRY if installed(role)]


def loaded(role: str, with_stackup: bool = True):
    """A `Board` for `role`, stackup bound, or skip the calling test."""
    from pcbreview.model import Board
    from pcbreview.stackup import load_and_validate

    pcb = board_path(role)
    board = Board.from_kicad(pcb)
    if with_stackup:
        stackup, _ = load_and_validate(stackup_path(role), board.copper_layers)
        board.bind_stackup(stackup)
    return board
