"""Tests for the opt-in review folder and where the extraction cache lives.

Two properties carry the user's ruling and are the ones to keep:

* `test_nothing_is_written_without_a_folder` — the default is unchanged.
  *"It definitely shouldnt randomly hoard findings somewhere."*
* `test_the_cache_never_lands_in_the_install_directory` — the leak that
  actually happened: after one outside review the installed skill held a
  602 KB geometry dump of a board belonging to a different company.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcbreview import review


def _board(tmp_path: Path, text: str = "(kicad_pcb)") -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    return path


# -- opt-in ----------------------------------------------------------------

def test_nothing_is_written_without_a_folder(tmp_path):
    """`find` is the whole opt-in mechanism, and it must not create anything."""
    board = _board(tmp_path)
    assert review.find(board) is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["board.kicad_pcb"]


def test_creating_the_folder_explains_itself(tmp_path):
    """A directory appearing in someone's project without explanation is the
    thing the ruling is against."""
    board = _board(tmp_path)
    root = review.create(board)
    assert root == tmp_path / "review"
    assert review.find(board) == root
    readme = (root / "README.md").read_text()
    assert "Delete the whole folder" in readme
    assert "Nothing here is needed to run a review" in readme
    assert (root / "runs").is_dir()


def test_creating_twice_does_not_clobber_an_existing_readme(tmp_path):
    board = _board(tmp_path)
    root = review.create(board)
    (root / "README.md").write_text("the user edited this")
    review.create(board)
    assert (root / "README.md").read_text() == "the user edited this"


# -- where the cache goes --------------------------------------------------

def test_the_cache_never_lands_in_the_install_directory(tmp_path):
    """The leak this ruling exists to close, asserted in both branches."""
    board = _board(tmp_path)
    install = Path(review.__file__).resolve().parent.parent

    without = review.cache_dir_for(board)
    assert install not in without.parents and without != install

    review.create(board)
    with_folder = review.cache_dir_for(board)
    assert install not in with_folder.parents and with_folder != install


def test_the_cache_sits_in_the_review_folder_when_there_is_one(tmp_path):
    board = _board(tmp_path)
    review.create(board)
    assert review.cache_dir_for(board) == tmp_path / "review" / ".cache"


def test_the_cache_falls_back_to_the_os_temp_directory(tmp_path):
    board = _board(tmp_path)
    cache = review.cache_dir_for(board)
    assert cache.parent == Path(tempfile.gettempdir())


# -- fingerprints ----------------------------------------------------------

def test_a_fingerprint_is_content_and_not_mtime(tmp_path):
    """Opening and saving a file unchanged must not read as a change, and a
    file restored from a backup must not read as unchanged."""
    board = _board(tmp_path, "(kicad_pcb A)")
    first = review.fingerprint([board])
    board.touch()
    assert review.fingerprint([board]) == first
    board.write_text("(kicad_pcb B)")
    assert review.fingerprint([board]) != first


def test_a_fingerprint_covers_every_sheet_of_a_hierarchy(tmp_path):
    """A change in a sub-sheet is a change to the schematic. Fingerprinting
    only the root would miss it silently."""
    root = tmp_path / "top.kicad_sch"
    child = tmp_path / "power.kicad_sch"
    root.write_text("(top)")
    child.write_text("(power)")
    before = review.fingerprint([root, child])
    child.write_text("(power, edited)")
    assert review.fingerprint([root, child]) != before
    assert review.fingerprint([root]) != before        # root alone is not it


def test_a_missing_artefact_fingerprints_as_nothing(tmp_path):
    assert review.fingerprint([tmp_path / "absent.kicad_sch"]) is None
    assert review.fingerprint([]) is None


# -- runs and per-artefact staleness ---------------------------------------

def test_the_first_review_reports_every_artefact_as_new(tmp_path):
    board = _board(tmp_path)
    root = review.create(board)
    assert review.changed_since(root, {"layout": "a", "schematic": "b"}) == {
        "layout": "new", "schematic": "new"}


def test_only_the_artefact_that_moved_is_reported_as_changed(tmp_path):
    """The ruling in one assertion: schematic and layout are checked
    separately, so a board whose layout moved does not drag the schematic
    side along with it."""
    board = _board(tmp_path)
    root = review.create(board)
    review.record(root, "board", {"layout": "a", "schematic": "b"}, {}, [])
    assert review.changed_since(root, {"layout": "a2", "schematic": "b"}) == {
        "layout": "changed", "schematic": "unchanged"}


def test_a_run_is_kept_and_pointed_at(tmp_path):
    board = _board(tmp_path)
    root = review.create(board)
    written = review.record(root, "board", {"layout": "a"},
                            {"by_severity": {"major": 2}},
                            [{"check": "return_path", "nets": ["/A"],
                              "title": "t", "severity": "major"}])
    assert written.exists()
    assert len(list((root / "runs").iterdir())) == 1
    last = review.previous(root)
    assert last is not None and last.summary == {"by_severity": {"major": 2}}


def test_a_corrupt_record_does_not_stop_a_review(tmp_path):
    """It is a comparison, not an input to any measurement."""
    board = _board(tmp_path)
    root = review.create(board)
    (root / "latest.json").write_text("{ not json")
    assert review.previous(root) is None
    assert review.changed_since(root, {"layout": "a"}) == {"layout": "new"}


# -- what changed since the last review ------------------------------------

def _entry(check, net, title, severity="major"):
    return {"check": check, "nets": [net], "title": title, "severity": severity}


def test_the_first_run_says_so_rather_than_claiming_everything_is_new():
    assert review.compare(None, [_entry("return_path", "/A", "t")]) == {
        "first_run": True, "resolved": [], "new": [], "changed": []}


def test_a_comparison_names_what_was_fixed_and_what_appeared(tmp_path):
    board = _board(tmp_path)
    root = review.create(board)
    review.record(root, "board", {}, {}, [
        _entry("return_path", "/A", "detour on /A"),
        _entry("return_path", "/B", "detour on /B"),
    ])
    moved = review.compare(review.previous(root), [
        _entry("return_path", "/B", "detour on /B"),
        _entry("crosstalk", "/C", "coupling on /C"),
    ])
    assert moved["resolved"] == ["return_path: detour on /A"]
    assert moved["new"] == ["crosstalk: coupling on /C"]
    assert moved["changed"] == []


def test_a_severity_that_moved_is_reported_as_changed_not_as_two_events(tmp_path):
    board = _board(tmp_path)
    root = review.create(board)
    review.record(root, "board", {}, {},
                  [_entry("return_path", "/A", "detour on /A", "minor")])
    moved = review.compare(review.previous(root),
                           [_entry("return_path", "/A", "detour on /A", "major")])
    assert moved["resolved"] == [] and moved["new"] == []
    assert moved["changed"] == ["return_path: detour on /A — minor -> major"]
