"""KiCad's own DRC, via ``kicad-cli``.

We deliberately do not reimplement design rule checking. KiCad's engine knows
about clearance classes, custom rules, courtyards, hole-to-hole spacing and
board setup in a way no reimplementation would track correctly, and it is the
same engine the user sees in the GUI.

pcbreview's checks are strictly *additive*: return paths, impedance, coupling
and intent — the things a DRC engine structurally cannot know.

Note on zone fills: ``--refill-zones`` refills in memory for the duration of
the check. Writing those fills back to the board requires ``save_board=True``,
which modifies the user's file and is therefore never the default.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"

#: Where `kicad-cli` lives when it is not on PATH, which on Windows is the
#: normal case — the installer does not add it. Globs because both the KiCad
#: version and, on macOS, the app location vary. Same shape and same reason as
#: `model._KICAD_PYTHON_GLOBS`.
_KICAD_CLI_GLOBS = {
    "darwin": [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        str(Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    ],
    "win32": [
        r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
        r"C:\Program Files (x86)\KiCad\*\bin\kicad-cli.exe",
    ],
    "linux": [
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/snap/kicad/current/usr/bin/kicad-cli",
        "/app/bin/kicad-cli",          # flatpak
    ],
}

# DRC types that overlap with checks pcbreview makes itself, kept separate so
# reports can avoid saying the same thing twice.
OVERLAPPING_TYPES = {
    "track_width", "via_diameter", "annular_width", "copper_edge_clearance",
}


class DrcError(Exception):
    """Raised when kicad-cli is unavailable or the DRC run fails."""


@dataclass
class DrcItem:
    description: str
    pos: tuple[float, float] | None
    uuid: str | None


@dataclass
class Violation:
    type: str
    severity: str            # error | warning | exclusion
    description: str
    items: list[DrcItem] = field(default_factory=list)

    @property
    def pos(self) -> tuple[float, float] | None:
        for item in self.items:
            if item.pos is not None:
                return item.pos
        return None

    def __str__(self) -> str:
        where = f" at ({self.pos[0]:.3f}, {self.pos[1]:.3f})" if self.pos else ""
        return f"[{self.severity}] {self.type}: {self.description}{where}"


@dataclass
class DrcReport:
    violations: list[Violation]
    unconnected: list[Violation]
    schematic_parity: list[Violation]
    kicad_version: str
    source: str
    coordinate_units: str = "mm"

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]

    def by_type(self) -> dict[str, list[Violation]]:
        grouped: dict[str, list[Violation]] = {}
        for violation in self.violations:
            grouped.setdefault(violation.type, []).append(violation)
        return dict(sorted(grouped.items(), key=lambda kv: -len(kv[1])))

    @property
    def is_clean(self) -> bool:
        return not self.errors and not self.unconnected

    def summary(self) -> dict:
        return {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "unconnected": len(self.unconnected),
            "schematic_parity": len(self.schematic_parity),
            "by_type": {k: len(v) for k, v in self.by_type().items()},
        }


def _parse_items(entries: list[dict]) -> list[DrcItem]:
    items = []
    for entry in entries or []:
        pos = entry.get("pos")
        items.append(DrcItem(
            description=entry.get("description", ""),
            pos=(pos["x"], pos["y"]) if pos else None,
            uuid=entry.get("uuid"),
        ))
    return items


def _parse_group(entries: list[dict]) -> list[Violation]:
    return [
        Violation(
            type=entry.get("type", "unknown"),
            severity=entry.get("severity", "error"),
            description=entry.get("description", ""),
            items=_parse_items(entry.get("items", [])),
        )
        for entry in entries or []
    ]


_VERSION_IN_PATH = re.compile(r"(\d+)\.(\d+)")


def newest_first(paths: list[str]) -> list[str]:
    """Sort install paths by the version number *in* them, newest first.

    `sorted(paths, reverse=True)` looks like it does this and does not.
    KiCad's installs are `…/KiCad/10.0/…` and `…/KiCad/9.0/…`, and
    lexicographically `"9" > "1"`, so a reverse string sort picks **9.0 over
    10.0** — silently, and only once someone has both installed.

    That is not hypothetical: it is what a Windows user with 10.0 and 9.0 side
    by side actually hit. KiCad 9's `pcbnew` cannot parse a 10.0 board and
    returns a null `BOARD` rather than raising, so the symptom was an
    `AttributeError` on `NoneType` several frames away with nothing anywhere
    saying "version". Everyone who upgrades in place from 9 will meet this.

    Ties and unparseable paths keep a stable order after the versioned ones.
    """
    def key(path: str) -> tuple:
        found = _VERSION_IN_PATH.findall(path)
        if not found:
            return (0, ())
        return (1, max(tuple(int(part) for part in pair) for pair in found))

    return sorted(paths, key=key, reverse=True)


def cli_beside(interpreter: str | None) -> str | None:
    """`kicad-cli` next to a given KiCad Python, if it is there.

    The two discovery paths are independent, so setting
    `PCBREVIEW_KICAD_PYTHON` to fix extraction left rendering and DRC still
    picking a *different* KiCad off PATH — one fix, two failures, and the
    second one reports only "Failed to load board". If the user has named an
    interpreter, its sibling is the obviously matching binary.
    """
    if not interpreter:
        return None
    directory = Path(interpreter).parent
    for name in ("kicad-cli", "kicad-cli.exe"):
        candidate = directory / name
        if candidate.exists():
            return str(candidate)
    return None


def find_kicad_cli(explicit: str | None = None) -> str:
    """Locate `kicad-cli`, or say what was looked for.

    Order: an explicit argument, then `PCBREVIEW_KICAD_CLI`, then the binary
    sitting beside `PCBREVIEW_KICAD_PYTHON` if that is set, then PATH, then
    the per-platform install locations newest-first.

    The sibling lookup comes **before** PATH deliberately. Someone who set
    `PCBREVIEW_KICAD_PYTHON` has already told us which KiCad they mean, and
    honouring PATH over that is how one fix turned into two failures.
    """
    import glob

    candidates = [explicit,
                  os.environ.get("PCBREVIEW_KICAD_CLI"),
                  cli_beside(os.environ.get("PCBREVIEW_KICAD_PYTHON")),
                  shutil.which("kicad-cli")]
    for pattern in _KICAD_CLI_GLOBS.get(sys.platform, []):
        candidates.extend(newest_first(glob.glob(pattern)))

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    looked = _KICAD_CLI_GLOBS.get(sys.platform) or [DEFAULT_KICAD_CLI]
    raise DrcError(
        "kicad-cli not found. Add it to PATH, set PCBREVIEW_KICAD_CLI, or "
        "pass kicad_cli=.\n  looked for: " + ", ".join(looked))


def run_drc(pcb_path: str | Path, *,
            refill_zones: bool = True,
            schematic_parity: bool = False,
            save_board: bool = False,
            all_track_errors: bool = True,
            kicad_cli: str | None = None,
            timeout: float = 300.0) -> DrcReport:
    """Run KiCad's DRC and parse the JSON report.

    Args:
        refill_zones: Refill zones before checking, so results reflect the
            current routing rather than possibly stale saved fills.
        schematic_parity: Also check PCB against schematic. Requires the
            matching .kicad_sch to be present.
        save_board: Write refilled zones back to the board file. This MODIFIES
            the user's board, so it stays off unless explicitly requested.
    """
    pcb_path = Path(pcb_path)
    if not pcb_path.exists():
        raise DrcError(f"board not found: {pcb_path}")
    if save_board and not refill_zones:
        raise DrcError("save_board requires refill_zones")

    executable = find_kicad_cli(kicad_cli)

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "drc.json"
        command = [executable, "pcb", "drc",
                   "--format", "json", "--severity-all",
                   "-o", str(out_path)]
        if refill_zones:
            command.append("--refill-zones")
        if save_board:
            command.append("--save-board")
        if schematic_parity:
            command.append("--schematic-parity")
        if all_track_errors:
            command.append("--all-track-errors")
        command.append(str(pcb_path))

        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DrcError(f"DRC timed out after {timeout}s on {pcb_path.name}") from exc

        if not out_path.exists():
            raise DrcError(
                f"DRC produced no report for {pcb_path.name} "
                f"(exit {result.returncode}):\n{result.stderr or result.stdout}")
        data = json.loads(out_path.read_text(encoding="utf-8"))

    return DrcReport(
        violations=_parse_group(data.get("violations", [])),
        unconnected=_parse_group(data.get("unconnected_items", [])),
        schematic_parity=_parse_group(data.get("schematic_parity", [])),
        kicad_version=data.get("kicad_version", "unknown"),
        source=data.get("source", str(pcb_path)),
        coordinate_units=data.get("coordinate_units", "mm"),
    )
