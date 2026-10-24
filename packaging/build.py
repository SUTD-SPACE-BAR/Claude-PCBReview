"""Assemble the distribution, refuse to ship it if it leaks, then zip it.

    python packaging/build.py                 # -> dist/pcbreview/ and dist/pcbreview-skill.zip
    python packaging/build.py --out /somewhere/else
    python packaging/build.py --no-zip        # leave the tree, skip the archive

Three steps, and the middle one is a gate rather than a report:

1. **Copy**, excluding by name — everything in `EXCLUDE_NAMES` and every
   `EXCLUDE_GLOBS` match. That list is the whole reason this file exists: the
   development registry `tests/boards_local.py` names four real designs, and it
   is the one file whose accidental inclusion would be invisible in a `git
   status` and fatal in a zip.
2. **Scan** the copy with `packaging/scan.py`. A non-empty result stops the
   build. Not a warning — a partial pass is worse than none, because a code
   name and a real name in the same distribution identify each other.
3. **Zip**, with the tree rooted at `pcbreview/` so that unzipping into
   `~/.claude/skills` puts the skill exactly where it belongs.

The scan runs on the **copy** and never on the source tree, which is where
`boards_local.py`, `pcb_examples/` and a warm `.cache/` legitimately live.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Directories and files that never ship, matched on their exact name at any
#: depth. `boards_local.py` is the important one and `boards.py` explains why
#: it exists at all.
EXCLUDE_NAMES = {
    "__pycache__", ".pytest_cache", ".git", ".history", ".cache", ".venv",
    "dist", "boards_local.py", ".DS_Store",
    # The forbidden-name list is a list of the forbidden names. It stays here.
    "private-names.txt",
}

#: Shapes that never ship whatever they are called. A `.kicad_pcb` *is*
#: somebody's design; a `.stackup.yaml` beside one is a transcription of a
#: board somebody ordered. `scan.py` checks for these too — this list is so
#: the build does not have to fail to find out.
EXCLUDE_GLOBS = (
    "*.kicad_pcb", "*.kicad_sch", "*.kicad_pro", "*.kicad_prl",
    "*.truth.json", "*.net", "*.pyc", "*.zip",
)

#: The stackup *library* is a set of fab transcriptions and must survive the
#: `*.yaml` neighbourhood above. Named explicitly so that widening the globs
#: later cannot quietly drop it.
KEEP_UNDER = ("pcbreview/stackup_library",)


def _force_writable(func, path, _exc_info):
    """`shutil.rmtree` error handler for Windows.

    A read-only bit — which OneDrive and some AV products set on files they are
    syncing — makes `os.unlink` and `os.rmdir` raise `PermissionError` even for
    the owner. Clearing it and retrying once is the standard fix and is safe:
    this only ever runs over a directory this script itself created.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _excluded(path: Path, relative: Path) -> bool:
    if any(part in EXCLUDE_NAMES for part in relative.parts):
        return True
    if any(str(relative).replace("\\", "/").startswith(keep)
           for keep in KEEP_UNDER):
        return False
    return any(path.match(pattern) for pattern in EXCLUDE_GLOBS)


def copy_tree(source: Path, destination: Path) -> int:
    if destination.exists():
        shutil.rmtree(destination, onexc=_force_writable)
    destination.mkdir(parents=True)
    copied = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if _excluded(path, relative):
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return copied


def scan(tree: Path) -> int:
    """`packaging/scan.py` over the copy. Its exit code is this one."""
    return subprocess.call(
        [sys.executable, str(ROOT / "packaging" / "scan.py"), str(tree)])


def archive(tree: Path, zip_path: Path) -> int:
    """Zip `tree` so that it unpacks as `pcbreview/…`.

    `INSTALL.md` tells people to unzip into `~/.claude/skills`, so the archive
    has to carry that one directory level. A zip of loose files would scatter
    the skill across whatever directory they happened to be in.
    """
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive_file:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                archive_file.write(path, Path("pcbreview")
                                   / path.relative_to(tree))
                written += 1
    return written


def main(argv: list[str]) -> int:
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv \
        else ROOT / "dist"
    tree = out / "pcbreview"

    print(f"copying {ROOT} -> {tree}")
    copied = copy_tree(ROOT, tree)
    print(f"  {copied} file(s)")

    print("scanning the copy for anything that identifies a board or an owner")
    if scan(tree) != 0:
        sys.stderr.write(
            "\nBUILD REFUSED. The tree above is what would have shipped. "
            "Fix the hits or add the file to EXCLUDE_NAMES; do not weaken "
            "scan.py to make this pass.\n")
        return 1

    if "--no-zip" in argv:
        print(f"\n--no-zip: the tree is at {tree}")
        return 0

    zip_path = out / "pcbreview-skill.zip"
    written = archive(tree, zip_path)
    size = zip_path.stat().st_size / 1_000_000
    print(f"\nwrote {zip_path}  ({written} files, {size:.1f} MB)")
    print("unzip it into ~/.claude/skills — see INSTALL.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
