"""Tests for the kicad-cli DRC wrapper.

These are integration tests — they invoke the real kicad-cli against a real
board, because the point of this module is that we do not model KiCad's rules
ourselves.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import drc
from pcbreview.drc import DrcError, find_kicad_cli, run_drc

try:
    find_kicad_cli()
    HAVE_CLI = True
except DrcError:
    HAVE_CLI = False


def test_missing_board_raises():
    with pytest.raises(DrcError, match="not found"):
        run_drc("/nonexistent/board.kicad_pcb")


def test_save_board_requires_refill(tmp_path):
    # The refusal being tested is argument validation and has nothing to do
    # with which board it is pointed at — but `run_drc` checks existence
    # first, so the path has to exist or this asserts the wrong refusal. An
    # empty temp file is enough and keeps the test board-independent.
    stand_in = tmp_path / "board.kicad_pcb"
    stand_in.touch()
    with pytest.raises(DrcError, match="requires refill_zones"):
        run_drc(stand_in, refill_zones=False, save_board=True)


# -- KiCad discovery, from a Windows run with 9.0 and 10.0 installed -------

def test_a_reverse_string_sort_would_pick_kicad_9_over_10():
    """The Windows blocker, as a property.

    `sorted(paths, reverse=True)` looks like "newest first" and is not:
    lexicographically `"9" > "1"`, so with KiCad 10.0 and 9.0 side by side it
    picks 9.0. KiCad 9's `pcbnew` cannot parse a 10.0 board and returns a null
    BOARD instead of raising, so the symptom was an `AttributeError` on
    `NoneType` several frames away with nothing saying "version".
    """
    paths = [r"C:\Program Files\KiCad\9.0\bin\python.exe",
             r"C:\Program Files\KiCad\10.0\bin\python.exe",
             r"C:\Program Files\KiCad\8.0\bin\python.exe"]
    assert "9.0" in sorted(paths, reverse=True)[0]          # the old behaviour
    assert "10.0" in drc.newest_first(paths)[0]             # the fixed one
    assert [p.split("\\")[-3] for p in drc.newest_first(paths)] == [
        "10.0", "9.0", "8.0"]


def test_unversioned_paths_survive_the_sort():
    """macOS and Linux paths carry no KiCad version. They must not vanish."""
    paths = ["/usr/bin/kicad-cli",
             "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    assert sorted(drc.newest_first(paths)) == sorted(paths)


def test_the_cli_is_taken_from_beside_a_declared_kicad_python(tmp_path):
    """One fix, two failures: setting PCBREVIEW_KICAD_PYTHON fixed extraction
    and left the renderer picking a different KiCad off PATH, reporting only
    `Failed to load board`."""
    binaries = tmp_path / "KiCad" / "10.0" / "bin"
    binaries.mkdir(parents=True)
    interpreter = binaries / "python.exe"
    interpreter.write_text("")
    (binaries / "kicad-cli").write_text("")
    assert drc.cli_beside(str(interpreter)) == str(binaries / "kicad-cli")


def test_no_sibling_cli_is_not_an_error(tmp_path):
    lonely = tmp_path / "python3"
    lonely.write_text("")
    assert drc.cli_beside(str(lonely)) is None
    assert drc.cli_beside(None) is None
