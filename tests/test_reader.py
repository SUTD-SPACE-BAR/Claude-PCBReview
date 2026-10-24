"""The datasheet audit, and the readings it refuses.

Almost every test here is about **refusing** a value, and that is the point.
These audits were written when a small local model produced the readings; the
model is gone and Claude produces them now, and not one assertion needed
changing. **The audit does not care who proposed a number** — a rule the reader
is trusted to have followed is not a rule.

The one that matters most is `test_a_quote_that_is_not_on_the_page_is_refused`.
A reader that misremembers a plausible 36 °C/W cannot also produce a line that
is really in the document, so checking the quotation catches the failure that
matters rather than the failure that is obvious.

No PDF is opened for the text-path tests: what is under test is the audit, not
pdfplumber, so `page_text` is patched. `tests/test_datasheets.py` builds real
PDFs and exercises the extraction.
"""

from __future__ import annotations

import pytest

from pcbreview import datasheets, reader
from tests import pdfs


def a_reading(text: str = "RthJA Junction-to-Ambient 36 C/W\n") -> tuple:
    """A `Reading` with nothing found, and one page of text behind it."""
    reading = datasheets.Reading(path="synthetic.pdf", sha256="x", pages=1,
                                 characters=len(text), readable=True)
    return reading, [text]


def offer(monkeypatch, proposal: dict, text: str | None = None,
          reading=None, **kwargs):
    """Hand `proposal` to the audit over one synthetic page. Returns the reading."""
    made, pages = a_reading() if text is None else a_reading(text)
    reading = reading if reading is not None else made
    monkeypatch.setattr(reader, "page_text", lambda path: pages)
    return reader.accept(reading, proposal, "synthetic.pdf", **kwargs)


# ---------------------------------------------------------------------------
# Accepting, and not overwriting
# ---------------------------------------------------------------------------

def test_a_cited_value_that_is_really_there_is_accepted(monkeypatch):
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 1, "quote": "RthJA Junction-to-Ambient 36 C/W"}})
    assert reading.theta_ja_c_per_w == 36.0
    assert reading.cites["theta_ja_c_per_w"]["source"] == reader.TEXT
    assert reading.cites["theta_ja_c_per_w"]["page"] == 1


def test_the_reader_never_overwrites_what_the_tables_found(monkeypatch):
    """A regex over a line checked to contain the number is a better witness."""
    existing, _ = a_reading()
    existing.theta_ja_c_per_w = 36.0
    existing.cites["theta_ja_c_per_w"] = datasheets.Cite(
        "table", 1, "x").as_dict()
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 99, "page": 1, "quote": "RthJA Junction-to-Ambient 36 C/W"}},
        reading=existing, fields=["theta_ja_c_per_w"])
    assert reading.theta_ja_c_per_w == 36.0
    assert reading.cites["theta_ja_c_per_w"]["source"] == "table"


def test_a_null_is_recorded_as_the_document_being_silent(monkeypatch):
    """Different from "the reader was never asked", so it is written down."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {"value": None}})
    assert reading.theta_ja_c_per_w is None
    assert any("states no theta_ja" in n for n in reading.notes)


def test_a_document_that_was_offered_a_reading_counts_as_asked(monkeypatch):
    """Recorded before the audit, not after.

    A document whose every proposed value is refused has still been read, and
    re-queueing it on every view is how a hard datasheet gets asked about
    forever.
    """
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 9, "quote": "nowhere"}})
    assert reading.asked_a_reader is True
    assert reading.theta_ja_c_per_w is None


# ---------------------------------------------------------------------------
# The five audits
# ---------------------------------------------------------------------------

def test_a_value_with_no_cite_is_refused(monkeypatch):
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {"value": 36}})
    assert reading.theta_ja_c_per_w is None
    assert "without a cite" in reading.refused[0]["why"]


def test_a_cite_to_a_page_that_does_not_exist_is_refused(monkeypatch):
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 9, "quote": "RthJA Junction-to-Ambient 36 C/W"}})
    assert reading.theta_ja_c_per_w is None
    assert "does not exist" in reading.refused[0]["why"]


def test_a_quote_that_is_not_on_the_page_is_refused(monkeypatch):
    """The audit that catches a *plausible* invention.

    36 C/W is an ordinary junction-to-ambient figure and passes every range
    check there is. What it cannot do is come with a line that is really in the
    document.
    """
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 1,
        "quote": "Thermal Resistance Junction to Ambient ... 36 C/W"}},
        text="this page says nothing about thermals\n")
    assert reading.theta_ja_c_per_w is None
    assert "not on the page" in reading.refused[0]["why"]


def test_a_quote_that_does_not_contain_the_number_is_refused(monkeypatch):
    """The line is real; the number was not read out of it."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 42, "page": 1, "quote": "RthJA Junction-to-Ambient 36 C/W"}})
    assert reading.theta_ja_c_per_w is None
    assert "does not contain" in reading.refused[0]["why"]


def test_whitespace_does_not_defeat_the_quote_check(monkeypatch):
    """pdfplumber collapses spacing, so the match must ignore it."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 1,
        "quote": "RthJA   Junction-to-Ambient    36 C/W"}},
        text="RthJAJunction-to-Ambient36C/W\n")
    assert reading.theta_ja_c_per_w == 36.0


def test_an_implausible_value_is_refused_even_with_a_real_quote(monkeypatch):
    """The range checks are source-blind — `datasheets.accept`, unchanged."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 4000, "page": 1, "quote": "RthJA nonsense 4000 C/W"}},
        text="RthJA nonsense 4000 C/W\n")
    assert reading.theta_ja_c_per_w is None
    assert "plausible band" in reading.refused[0]["why"]


def test_a_value_that_is_not_a_number_is_refused(monkeypatch):
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": "about 36", "page": 1, "quote": "RthJA 36 C/W"}})
    assert reading.theta_ja_c_per_w is None
    assert "not a number" in reading.refused[0]["why"]


# ---------------------------------------------------------------------------
# A scanned document — the weaker audit, marked as such
# ---------------------------------------------------------------------------

def test_a_scanned_document_is_marked_as_read_by_vision(tmp_path):
    """There is no text to check a quotation against, so audits 1, 2, 4 and 5
    apply and 3 cannot. The source records which of the two it was."""
    pytest.importorskip("pdfplumber")
    scanned = pdfs.image_pdf(tmp_path / "scan.pdf", 5)
    reading = datasheets.read_tables(scanned)
    assert reading.characters == 0, "this test needs a document with no text"

    reader.accept(reading, {"t_junction_max_c": {
        "value": 175, "page": 1,
        "quote": "TJ Operating Junction -55 to 175"}}, scanned,
        ["t_junction_max_c"])
    assert reading.t_junction_max_c == 175.0
    assert reading.cites["t_junction_max_c"]["source"] == reader.VISION


def test_a_caller_may_declare_a_reading_as_vision_but_not_the_reverse(
        monkeypatch):
    """`--vision` on a document that *does* carry text is honest — somebody
    read a rotated table off the image. The opposite is not available: claiming
    `read` on a document with no text would claim audit 3 ran when it could
    not."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 1, "quote": "RthJA Junction-to-Ambient 36 C/W"}},
        source=reader.VISION)
    assert reading.cites["theta_ja_c_per_w"]["source"] == reader.VISION


# ---------------------------------------------------------------------------
# Audit 5 — the citation must name the quantity it was offered as
# ---------------------------------------------------------------------------
#
# Every case below is drawn from a real reading of a real datasheet, whose
# Absolute Maximum Ratings table labels one row `RθJC` in the symbol column and
# "Thermal Resistance, Junction to Ambient" in the parameter column — a
# copy-paste the vendor never fixed. A reader transcribed it faithfully and
# offered 3.0 °C/W as junction-to-ambient. For a 3 mm DFN that is impossible —
# a package reaching 3 °C/W to still air does not exist — but no plausible band
# excludes it, and a scanned page has no text for audit 3 to check against.
#
# So the fifth audit is not a threshold. Thresholds are what failed here.

def test_the_symbol_outranks_the_prose_when_they_disagree(monkeypatch):
    """That case, verbatim. The symbol decides, so the value is refused."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 3.0, "page": 1,
        "quote": "RθJC Thermal Resistance, Junction to Ambient 3.0 °C/W"}},
        text="RθJC Thermal Resistance, Junction to Ambient 3.0 °C/W\n")
    assert reading.theta_ja_c_per_w is None
    assert "symbol names junction-to-case" in reading.refused[0]["why"]


def test_the_same_line_offered_as_the_symbol_says_is_accepted(monkeypatch):
    """The reason the rule is not "refuse both".

    A second vendor's θJC row carries the same copy-pasted prose, on a value
    that is certainly the junction-to-case one. Refusing it would throw away a
    good number **and disagree with this module's own table path**, which
    matches on the symbol and takes it happily.
    """
    reading = offer(monkeypatch, {"theta_jc_c_per_w": {
        "value": 6.5, "page": 1,
        "quote": "RθJC ThermalResistanceJunction-Ambient1 --- 6.5 ℃/W"}},
        text="RθJC ThermalResistanceJunction-Ambient1 --- 6.5 ℃/W\n")
    assert reading.theta_jc_c_per_w == 6.5


def test_a_quote_naming_only_the_other_quantity_is_refused(monkeypatch):
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 6.2, "page": 1,
        "quote": "Thermal Resistance, Junction-to-Case 6.2"}},
        text="Thermal Resistance, Junction-to-Case 6.2\n")
    assert reading.theta_ja_c_per_w is None
    assert "junction-to-case" in reading.refused[0]["why"]


def test_a_quote_naming_the_right_quantity_passes(monkeypatch):
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 1,
        "quote": "RthJA Thermal Resistance, Junction-to-Ambient 36 C/W"}},
        text="RthJA Thermal Resistance, Junction-to-Ambient 36 C/W\n")
    assert reading.theta_ja_c_per_w == 36.0


def test_a_quote_naming_neither_is_left_alone(monkeypatch):
    """Real tables routinely put the symbol in one cell and the number in
    another, and refusing those would cost far more than it saved."""
    reading = offer(monkeypatch, {"theta_ja_c_per_w": {
        "value": 36, "page": 1, "quote": "Thermal Resistance 36 C/W"}},
        text="Thermal Resistance 36 C/W\n")
    assert reading.theta_ja_c_per_w == 36.0


def test_audit_five_applies_on_the_vision_path_too():
    """Where it is the only thing standing between a faithful transcription of
    a mislabelling and a thermal resistance wrong by an order of magnitude."""
    assert reader._quote_names_the_right_thing(
        "theta_ja_c_per_w",
        "RθJC Thermal Resistance, Junction to Ambient 3.0") is not None
    # The same line, offered as what its symbol says, passes.
    assert reader._quote_names_the_right_thing(
        "theta_jc_c_per_w",
        "RθJC Thermal Resistance, Junction to Ambient 3.0") is None
    assert reader._quote_names_the_right_thing(
        "t_junction_max_c", "TJ Operating Junction -55 to 175") is None


# ---------------------------------------------------------------------------
# Deciding whether a document has text at all
# ---------------------------------------------------------------------------

def test_a_scan_is_detected_on_its_text_and_not_on_its_page_count():
    """A scanned document yields one **empty string per page**, so the list is
    non-empty while the text is not. `not pages` was False, and the vision path
    was never taken — a real bug, and this is the shape of it."""
    assert reader.is_scanned(["", "", ""]) is True
    assert reader.is_scanned([]) is True
    assert reader.is_scanned(["", "RthJA 36", ""]) is False
