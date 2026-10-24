"""Reading datasheets: LCSC first, tables second, a model only for the rest.

The most important test in this file is `test_a_footnote_marker_is_not_a_value`.
It is here because the first version of the scanner read

    RθJA ThermalResistanceJunction-Ambient1 --- 36 ℃/W

as **1.0 °C/W** — the `1` is a footnote marker glued to `Ambient`, pdfplumber
having already collapsed the spaces — and 1.0 is a perfectly plausible
junction-to-ambient figure, so the range check let it through. The part would
have been reported 36× cooler than it is.

That is the failure mode this whole module is arranged against: not a value
that is obviously wrong, but one that is quietly wrong and plausible.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pcbreview import datasheets
from tests import pdfs

#: The three documents the reader was written against, reduced to the lines
#: that taught it something. No PDF ships with this tool — one would name a
#: part, and `packaging/scan.py` refuses that — so `tests/pdfs.py` builds a
#: real PDF carrying exactly these lines whenever a test needs one.
#:
#: `COMPLETE` is a vendor that states all three figures on one page.
#: `PARTIAL` states junction-to-case and no junction-to-ambient, which is the
#: absence Model A is then asked about. `SCANNED` has no text layer at all.
COMPLETE = [
    "Thermal Information",
    "RthJA ThermalResistanceJunction-Ambient1 --- 36 C/W",
    "RthJC ThermalResistanceJunction-Ambient1 --- 6.5 C/W",
    "TJ Operating Junction Temperature -55 to 150 C",
]
PARTIAL = [
    "Thermal Characteristics",
    "ThermalResistance,Junction-to-Case R 6.2",
]


def has_pdfplumber() -> bool:
    try:
        import pdfplumber                                    # noqa: F401
    except ImportError:
        return False
    return True


needs_pdfplumber = pytest.mark.skipif(
    not has_pdfplumber(), reason="pdfplumber is not installed")


# ---------------------------------------------------------------------------
# The value picker — the part that was wrong
# ---------------------------------------------------------------------------

def test_a_footnote_marker_is_not_a_value():
    """A digit glued to a letter is a footnote, not a measurement.

    The real line, from a second-source load-switch datasheet. Getting 1.0
    here instead of 36 is not a near miss — it is a plausible number, so
    nothing downstream could have caught it. The line is reproduced; the
    part it came from is not named, for the reason `pdfs.py` gives.
    """
    value, how = datasheets._value_on(
        "RθJA ThermalResistanceJunction-Ambient1 --- 36 ℃/W")
    assert value == 36.0
    assert "unit" in how


def test_the_unit_wins_over_position():
    value, _ = datasheets._value_on("RθJA Note2 --- 36 ℃/W at 25 C")
    assert value == 36.0


@pytest.mark.parametrize("unit", ["℃/W", "°C/W", "C/W", "°C / W", "℃ /W"])
def test_every_spelling_of_the_unit_is_recognised(unit):
    value, how = datasheets._value_on(f"RθJA Junction-Ambient1 --- 36 {unit}")
    assert value == 36.0, how


def test_with_no_unit_the_last_number_is_taken():
    """A real line from the GOFORD part, which carries no unit at all."""
    value, how = datasheets._value_on(
        "ThermalResistance,Junction-to-Case R 6.2")
    assert value == 6.2
    assert "last number" in how


def test_a_line_with_nothing_but_a_footnote_yields_nothing():
    value, why = datasheets._value_on("ThermalResistanceJunction-Ambient1")
    assert value is None
    assert "footnote" in why


# ---------------------------------------------------------------------------
# Range checks — applied to every source alike
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,value,ok", [
    ("theta_ja_c_per_w", 36.0, True),
    ("theta_ja_c_per_w", 0.1, False),
    ("theta_ja_c_per_w", 4000.0, False),
    ("theta_jc_c_per_w", 6.5, True),
    ("t_junction_max_c", 150.0, True),
    ("t_junction_max_c", 25.0, False),      # a page number or an ambient
    ("t_junction_max_c", 1200.0, False),
])
def test_the_plausible_bands(name, value, ok):
    assert (datasheets.accept(name, value) is None) is ok


def test_junction_to_case_must_be_below_junction_to_ambient():
    """The one relation that holds universally, and catches a column swap."""
    reading = datasheets.Reading(path="x", sha256="", pages=1, characters=1,
                                 readable=True)
    reading.theta_ja_c_per_w, reading.theta_jc_c_per_w = 36.0, 6.5
    assert datasheets.cross_check(reading) == []
    reading.theta_ja_c_per_w, reading.theta_jc_c_per_w = 6.5, 36.0
    assert "not physical" in datasheets.cross_check(reading)[0]


# ---------------------------------------------------------------------------
# The LCSC catalogue
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,code", [
    # The shape LCSC itself serves.
    ("C9100001.pdf", "C9100001"), ("c9100002.PDF", "C9100002"),
    # Shapes a real download folder is full of. Refusing these sent perfectly
    # identifiable documents to the "no part attached" pile.
    ("C9100001 (1).pdf", "C9100001"), ("C9100002_XYZ4321.pdf", "C9100002"),
    ("part-C9100002.pdf", "C9100002"),
    # No code, or an ambiguous one. Both are the sidecar's job — `--attach`.
    ("NOTACODE.pdf", None), ("C111111 C222222.pdf", None),
    ("datasheet.pdf", None),
])
def test_the_lcsc_code_comes_from_the_filename_only(name, code):
    """Never from the document body: a part number inside a PDF may be a
    cross-reference to something else entirely.

    The filename is different in kind — it is a statement somebody made about
    what the file is, and a code delimited inside one is as much a statement as
    a code that is the whole name. Two codes in one name is not a statement,
    it is an ambiguity, and `--attach` is the honest answer to it."""
    assert datasheets.lcsc_from_name(name) == code


def test_an_absent_catalogue_is_no_identity_not_an_error(tmp_path):
    assert datasheets.identify("C9100001", tmp_path / "nothing.sqlite3") is None


#: Real LCSC codes and the MPN each must resolve to, as
#: `PCBREVIEW_TEST_LCSC="<code>=<mpn>,<code>=<mpn>"`. Read from the
#: environment and never written down here: five real codes and their MPNs in
#: one file are a bill of materials, and `pdfs.py` says why that must not ship.
#: Without it the test skips, which is the same contract every board-dependent
#: test in this suite already has.
def _catalogue_vectors():
    raw = os.environ.get("PCBREVIEW_TEST_LCSC", "").strip()
    pairs = []
    for item in raw.split(","):
        if "=" in item:
            code, _, mpn = item.partition("=")
            pairs.append((code.strip(), mpn.strip()))
    return pairs


@pytest.mark.skipif(datasheets.catalogue_path() is None,
                    reason="no local jlc-trawler catalogue on this machine")
@pytest.mark.skipif(not _catalogue_vectors(),
                    reason="set PCBREVIEW_TEST_LCSC=\"C…=MPN,C…=MPN\" to check "
                           "the catalogue against parts you actually have")
@pytest.mark.parametrize("code,mpn", _catalogue_vectors())
def test_the_local_catalogue_identifies_a_known_part(code, mpn):
    identity = datasheets.identify(code)
    assert identity is not None and identity.mpn == mpn
    assert identity.package and identity.category
    # The attributes are carried verbatim and never mined for thermal values:
    # an operating range is not a stated Tj(max).
    assert isinstance(identity.attributes, dict)


# ---------------------------------------------------------------------------
# Whole documents — the three shapes a real datasheet comes in
# ---------------------------------------------------------------------------

@needs_pdfplumber
def test_a_scanned_datasheet_says_so_rather_than_saying_nothing_is_there(
        tmp_path):
    """The distinction this module refuses to blur.

    A scan is pages of images with **zero** text characters. It must come back
    as *could not be read*, never as a part whose datasheet is silent on
    thermals — those are opposite claims, and `thermal.py` requires
    `theta_ja_stated: false` precisely so a person cannot conflate them either.

    It is also the shape that caught a real bug: a scanned document yields one
    empty string per page, so `not pages` was False and the vision path was
    never taken.
    """
    reading = datasheets.read_tables(pdfs.image_pdf(tmp_path / "scan.pdf", 5))
    assert reading.pages == 5
    assert reading.characters == 0
    assert reading.readable is False
    assert "no text layer" in reading.why_not
    assert not reading.found_any


@needs_pdfplumber
def test_a_complete_datasheet_gives_all_three_with_page_cites(tmp_path):
    reading = datasheets.read_tables(
        pdfs.text_pdf(tmp_path / "complete.pdf", [COMPLETE]))
    assert reading.readable
    assert reading.theta_ja_c_per_w == 36.0
    assert reading.theta_jc_c_per_w == 6.5
    assert reading.t_junction_max_c == 150.0
    assert reading.wants_a_reader == []
    for name in ("theta_ja_c_per_w", "theta_jc_c_per_w", "t_junction_max_c"):
        cite = reading.cites[name]
        assert cite["source"] == "table"
        assert cite["page"] == 1
        assert cite["text"]
    assert datasheets.cross_check(reading) == []


@needs_pdfplumber
def test_a_partial_datasheet_gives_only_what_it_states(tmp_path):
    """Junction-to-case and no junction-to-ambient. The absence is reported as
    an absence, and it is what Model A is then asked for.

    The line also carries **no unit at all**, which is why the value picker
    falls back to the last number on the line rather than to the first."""
    reading = datasheets.read_tables(
        pdfs.text_pdf(tmp_path / "partial.pdf", [PARTIAL]))
    assert reading.readable
    assert reading.theta_jc_c_per_w == 6.2
    assert reading.theta_ja_c_per_w is None
    assert "theta_ja_c_per_w" in reading.wants_a_reader


def test_every_document_is_hashed_by_its_own_bytes(tmp_path):
    written = {
        "complete.pdf": pdfs.text_pdf(tmp_path / "complete.pdf", [COMPLETE]),
        "partial.pdf": pdfs.text_pdf(tmp_path / "partial.pdf", [PARTIAL]),
        "scan.pdf": pdfs.image_pdf(tmp_path / "scan.pdf", 2),
    }
    hashes = {name: datasheets.content_hash(path)
              for name, path in written.items()}
    assert all(len(h) == 64 for h in hashes.values())
    assert len(set(hashes.values())) == len(written)
    # Stable across calls — this is the cache key.
    assert (datasheets.content_hash(written["partial.pdf"])
            == hashes["partial.pdf"])


def test_a_missing_file_hashes_to_nothing_rather_than_raising(tmp_path):
    assert datasheets.content_hash(tmp_path / "absent.pdf") == ""


@needs_pdfplumber
def test_a_file_that_is_not_a_pdf_says_so(tmp_path):
    fake = tmp_path / "notreally.pdf"
    fake.write_text("this is not a pdf", encoding="utf-8")
    reading = datasheets.read_tables(fake)
    assert reading.readable is False
    assert reading.why_not
    assert not reading.found_any


# ---------------------------------------------------------------------------
# The cache must not freeze a document out of ever being read by a model
# ---------------------------------------------------------------------------

def test_a_cached_reading_is_returned_whole(tmp_path, monkeypatch):
    """A document is read once ever, and that now includes the part a person
    read.

    This used to be conditional: a cached reading did *not* satisfy a caller
    who wanted a model pass, because the deterministic read happened first and
    would otherwise freeze the document out of ever being read properly. There
    is no second pass inside `read()` any more — `parts --read` writes into the
    cache directly — so the rule is simply "cached wins", and what a person
    read is not thrown away by the next `ingest`.
    """
    monkeypatch.setenv(datasheets.CACHE_ENV, str(tmp_path))
    stored = datasheets.Reading(path="x.pdf", sha256="a" * 64, pages=1,
                                characters=50, readable=True)
    stored.theta_ja_c_per_w = 36.0
    stored.cites["theta_ja_c_per_w"] = datasheets.Cite(
        "read", 1, "RthJA 36 C/W").as_dict()
    datasheets.remember(stored)

    read_tables_called = []
    monkeypatch.setattr(datasheets, "content_hash", lambda p: "a" * 64)
    monkeypatch.setattr(datasheets, "read_tables",
                        lambda p: read_tables_called.append(p) or
                        datasheets.Reading(path=str(p), sha256="a" * 64,
                                           pages=1, characters=50,
                                           readable=True))

    got = datasheets.read(tmp_path / "x.pdf")
    assert read_tables_called == [], "a cached document was re-extracted"
    assert got.theta_ja_c_per_w == 36.0
    assert got.cites["theta_ja_c_per_w"]["source"] == "read"

    # …and `use_cache=False` is how a caller insists on re-extracting.
    datasheets.read(tmp_path / "x.pdf", use_cache=False)
    assert len(read_tables_called) == 1


def test_a_reading_that_was_asked_is_not_asked_again(tmp_path, monkeypatch):
    """`asked_a_reader` is set before the call, so a document that makes the
    model fall over is not retried on every view of the tab."""
    monkeypatch.setenv(datasheets.CACHE_ENV, str(tmp_path))
    stored = datasheets.Reading(path="y.pdf", sha256="b" * 64, pages=1,
                                characters=50, readable=True)
    stored.asked_a_reader = True
    datasheets.remember(stored)
    restored = datasheets.cached("b" * 64)
    assert restored.asked_a_reader is True


# ---------------------------------------------------------------------------
# find_pdfs — identity, not spelling
# ---------------------------------------------------------------------------

def test_a_case_insensitive_filesystem_does_not_double_every_datasheet(tmp_path):
    """`Datasheets/` and `datasheets/` are one directory on macOS and Windows.

    `_SEARCH` lists both spellings because either is a real convention. On a
    case-insensitive filesystem both resolve to the SAME directory, so every
    PDF in it was found twice under two paths that compare unequal — and
    `parts.assemble()` claims one copy for its designator and reports the other
    as a datasheet *"nothing on the board claims"*. That is the precise false
    statement the unattached row exists to prevent, and it fired on every real
    project on this platform.

    Skipped where the filesystem is case-sensitive: there the two spellings
    really are two directories and there is nothing to deduplicate.
    """
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    (tmp_path / "Datasheets").mkdir()
    pdfs.text_pdf(tmp_path / "Datasheets" / "C1.pdf", [["RthJA 36 C/W"]])

    if not (tmp_path / "datasheets").is_dir():
        pytest.skip("case-sensitive filesystem: the two spellings are two "
                    "directories, so there is no duplicate to collapse")

    found = datasheets.find_pdfs(board)
    assert len(found) == 1, (
        f"one document, found {len(found)} times: {[str(p) for p in found]}")


def test_a_symlinked_duplicate_is_the_same_document(tmp_path):
    """The same guard, reached the other way: one file, two names."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    (tmp_path / "docs").mkdir()
    real = pdfs.text_pdf(tmp_path / "C1.pdf", [["RthJA 36 C/W"]])
    try:
        (tmp_path / "docs" / "C1.pdf").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    found = datasheets.find_pdfs(board)
    assert len(found) == 1, (
        f"one document, found {len(found)} times: {[str(p) for p in found]}")


def test_a_filesystem_with_no_inodes_does_not_collapse_every_document(
        tmp_path, monkeypatch):
    """Some filesystems and network shares report `st_ino == 0` for every file.

    Keying the deduplication on that would make every datasheet in the folder
    the same document and drop all but the first — silently, and it is the
    thermal half of the review that would go quiet. Windows is where this
    matters and where it could not be reproduced by hand, so it is forced.
    """
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    for code in ("C1", "C2", "C3"):
        pdfs.text_pdf(tmp_path / f"{code}.pdf", [["RthJA 36 C/W"]])

    real_stat = Path.stat

    def no_inodes(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        return os.stat_result((
            info.st_mode, 0, 0, info.st_nlink, info.st_uid, info.st_gid,
            info.st_size, int(info.st_atime), int(info.st_mtime),
            int(info.st_ctime)))

    monkeypatch.setattr(Path, "stat", no_inodes)
    found = datasheets.find_pdfs(board)
    assert sorted(p.name for p in found) == ["C1.pdf", "C2.pdf", "C3.pdf"], (
        f"three distinct documents collapsed to {[p.name for p in found]}")
