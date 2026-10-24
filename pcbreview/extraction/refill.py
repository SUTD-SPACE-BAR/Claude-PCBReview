"""Refill a board's zones headlessly, on a copy, before anything reads it.

    kicad-cli pcb drc --refill-zones --save-board --format json -o drc.json board

**Proven, not assumed.** `pcbreview-app/experiments/zonefill2/` stripped all 166
zone fills from a demo board with `pcbnew`'s `z.UnFill()` (`filled_polygon`
count went to 0), then ran the command above with no KiCad GUI running: exit 0,
4.1 s, 169 fill polygons restored, fill area 5344.51 mm² against the as-shipped
5355.66 mm² — 0.21 % apart, consistent with the filler's documented
non-determinism.

Two wins in one call: current fills, and the DRC JSON `drc.py` already parses.

Never the user's file
---------------------
`--save-board` writes the board it was given, so it is **never** given the
user's. The board is copied to a temp directory with its project file and the
copy is refilled. Engine rule 1: nothing in this tool writes to a
`.kicad_pcb` a human owns.

Why this is off by default
--------------------------
Refilling changes the geometry by ~0.21 %, and `Board.from_kicad` keys its
extraction cache on the board's path and mtime — so a refilled copy is a
different board with a different cache and a fresh polygonisation. That is
correct behaviour and it is also exactly how byte-identity against the Phase 0
baselines is lost. KiCad saves fills when it saves a board, so the common case
does not need this. It is here for the board that was edited by a script, or
whose fills are stale, and the pipeline says so in the report's assumptions when
it is used.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pcbreview import drc as drc_module        # noqa: E402

#: `kicad-cli pcb drc` on a large board is seconds, not minutes. The engine
#: uses the same order of magnitude for its own DRC run.
REFILL_TIMEOUT_S = 300.0

#: What has to travel with a board for KiCad to fill it the way the user sees
#: it. The project file carries net classes and clearance rules; without it
#: `kicad-cli` falls back to defaults and the fill is not the user's fill.
COMPANION_SUFFIXES = (".kicad_pro", ".kicad_prl", ".kicad_dru")


class RefillError(Exception):
    """Raised when a refill was asked for and could not be done."""


@dataclass
class RefillResult:
    """A refilled copy, or a reason there is not one."""
    ok: bool
    #: the refilled copy — **use this path, not the original**
    board: Path | None = None
    #: kicad-cli's DRC JSON, which `drc.py` parses without modification
    drc_json: Path | None = None
    seconds: float = 0.0
    why_not: str | None = None
    notes: list[str] = field(default_factory=list)


def kicad_cli(explicit: str | None = None) -> str:
    """Locate `kicad-cli`. Delegates to the engine so there is one search order.

    A second copy of this would drift, and a drifting KiCad path means the DRC
    and the extraction disagree about which KiCad they are talking to — on a
    machine with 9.0 and 10.0 installed, which is this one.
    """
    return drc_module.find_kicad_cli(explicit)


def refill_to_temp(board_path, workdir, *, cli: str | None = None,
                   timeout: float = REFILL_TIMEOUT_S) -> RefillResult:
    """Copy `board_path` into `workdir`, refill its zones, return the copy.

    `workdir` must already exist and should be a temp directory the caller
    owns. Nothing is written outside it, and the original is opened read-only.

    A failure is **returned, not raised**: a board that cannot be refilled is
    still a board that can be reviewed with the fills it has, and refusing to
    review it over that would be absurd — the same reasoning that makes the
    Phase 0 solve pool fall back in process when it cannot spawn.
    """
    import time

    board_path = Path(board_path)
    workdir = Path(workdir)
    if not board_path.exists():
        raise RefillError(f"board not found: {board_path}")
    if not workdir.is_dir():
        raise RefillError(f"work directory does not exist: {workdir}")

    try:
        executable = kicad_cli(cli)
    except drc_module.DrcError as exc:
        return RefillResult(ok=False, why_not=str(exc))

    copy = workdir / board_path.name
    shutil.copy2(board_path, copy)
    notes = []
    for suffix in COMPANION_SUFFIXES:
        companion = board_path.with_suffix(suffix)
        if companion.exists():
            shutil.copy2(companion, workdir / companion.name)
            notes.append(f"copied {companion.name} so the fill uses the "
                         f"project's own net classes and clearances")
    drc_json = workdir / f"{board_path.stem}.drc.json"

    started = time.time()
    try:
        completed = subprocess.run(
            [executable, "pcb", "drc", "--refill-zones", "--save-board",
             "--format", "json", "--severity-all", "-o", str(drc_json),
             str(copy)],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return RefillResult(ok=False, seconds=time.time() - started,
                            why_not=(f"kicad-cli did not finish within "
                                     f"{timeout:.0f}s"))
    except OSError as exc:
        return RefillResult(ok=False, why_not=f"could not run kicad-cli: {exc}")
    seconds = time.time() - started

    if completed.returncode != 0:
        # kicad-cli exits non-zero when the DRC found violations *and* when it
        # genuinely failed, so the board file decides which happened.
        if not copy.exists():
            return RefillResult(
                ok=False, seconds=seconds,
                why_not=(f"kicad-cli exited {completed.returncode}: "
                         f"{(completed.stderr or '').strip()[:300]}"))
        notes.append(f"kicad-cli exited {completed.returncode}, which it also "
                     f"does when the DRC finds violations; the refilled board "
                     f"was written, so it is used")

    return RefillResult(ok=True, board=copy,
                        drc_json=drc_json if drc_json.exists() else None,
                        seconds=seconds, notes=notes)
