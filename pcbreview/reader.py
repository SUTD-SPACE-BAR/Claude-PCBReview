"""The datasheet reader's audit — five checks over a reading somebody offered.

Where the reading comes from, and why it is not a local model any more
----------------------------------------------------------------------
The app this was ported from has no Claude in it, so it grew a small local
vision model to read the datasheets its pattern-matcher could not. In a skill
that is the wrong tool, and the app's own measurements say so:

* on the scanned document that is the *only* case a model is needed for, six
  identical runs returned a value **zero** times; a seventh, on the same
  document with the same model and the same settings, returned one;
* the one time it did answer, its page cite was **wrong** — page 2 for a table
  on page 1 — which no audit here can catch, because a scan has no text to
  check a quotation against;
* `temperature 0` and a fixed seed give repeatability on the text path,
  measured over three runs each, and **do not give it on images**.

Claude is reading this file. It has the document, it has the schematic, and it
is a far better reader than a 4B model — so the model is gone and the reading
comes from Claude, through `python -m pcbreview.parts --read`.

**Nothing about the audit changed, and that is the point.** A proposal is
proposed; it is not believed because of who made it. Every check below applied
to the model and applies unchanged to Claude, including the one that exists
because a model transcribed a vendor's own mislabelling faithfully.

The five audits, and the last two are the ones that earn their keep
--------------------------------------------------------------------
Every proposed value must survive all five or it is dropped and the field stays
empty:

1. **A cite is required.** No page, no value. It is also what makes 2 and 3
   possible at all.
2. **The page must exist.** A cite to page 9 of a six-page document is a
   fabrication, and saying so is free.
3. **The quoted line must actually be on that page.** The reader returns the
   exact text it read the number out of, and that text is checked against the
   page it claims — *and the number must appear in it*. A reader that
   misremembers a plausible θJA cannot also invent a line that is really in the
   document, so this catches the failure that matters rather than the failure
   that is obvious. Matching ignores whitespace, because pdfplumber has already
   collapsed it.
4. **The same range checks the table path uses**, from `datasheets.accept` and
   `datasheets.cross_check`. Source-blind on purpose: a rule the reader is
   trusted to have followed is not a rule.
5. **The quoted line's symbol must not name a different quantity than the
   field it was offered for** — see `_quote_names_the_right_thing`. A real
   datasheet labels one row `RθJC` in the symbol column and "Junction to
   Ambient" in the parameter column; a reader transcribed that faithfully and
   filed the 3.0 °C/W beside it as junction-to-ambient. No band catches it —
   3.0 is plausible — and only the citation does. The symbol outranks the
   prose, because prose is where these vendors copy and paste.

**Audit 3 is only available where there is text.** A scanned page has nothing
to check a quotation against, so a reading taken from the image is accepted on
audits 1, 2, 4 and 5 alone and every value it produces is marked
`source="vision"` rather than `"read"`. That is a real weakening and it is
recorded rather than smoothed over — a report can say which of the two it is,
and `--read` will not let you claim the stronger one.

What Claude is expected to do
-----------------------------
1. `python -m pcbreview.parts <project>` — the rows, and what each is missing;
2. `python -m pcbreview.parts <project> --pages <pdf>` — the extracted text,
   page by page, so no PDF tooling of your own is needed. **Zero characters
   means it is a scan**: open the PDF yourself and read the image;
3. `--read C1234567:theta_ja=36,page=1,quote="RthJA … 36 C/W"` — offer the
   value. It is audited here, and stored against the PDF's content hash, so a
   document is read once ever.

Quote the line **exactly as `--pages` printed it**, run-together spacing and
all. Audit 3 compares against that text, not against the rendered page.
"""

from __future__ import annotations

import re

from pcbreview import datasheets

#: The fields a reading may fill. `dissipation_w` is deliberately absent: it is
#: an operating-point calculation, not a datasheet figure, and no amount of
#: reading produces it.
FIELDS = ("theta_ja_c_per_w", "theta_jc_c_per_w", "t_junction_max_c")

#: Short names accepted on the command line, because `theta_ja_c_per_w=` is
#: typed by a person at a prompt.
ALIASES = {
    "theta_ja": "theta_ja_c_per_w", "theta_jc": "theta_jc_c_per_w",
    "tj_max": "t_junction_max_c", "tjmax": "t_junction_max_c",
    "theta_ja_c_per_w": "theta_ja_c_per_w",
    "theta_jc_c_per_w": "theta_jc_c_per_w",
    "t_junction_max_c": "t_junction_max_c",
}

#: What a value read off an image is called. Weaker than `read` by exactly one
#: audit, and named so that nothing downstream has to remember which.
VISION = "vision"
TEXT = "read"


def page_text(path) -> list[str]:
    """Every page's extracted text, in order. Empty list if it cannot be read.

    The one place this module touches a PDF. `--pages` prints it and audit 3
    checks against it, so the reader and the auditor are looking at exactly the
    same characters — which is why quotes must be copied from `--pages` output
    rather than retyped from a rendered page.
    """
    try:
        import pdfplumber                                     # noqa: PLC0415
    except ImportError:
        return []
    try:
        with pdfplumber.open(str(path)) as pdf:
            return [(page.extract_text() or "") for page in pdf.pages]
    except Exception:                                         # noqa: BLE001
        return []


def is_scanned(pages: list[str]) -> bool:
    """Whether this document has no text at all.

    **Not `not pages`.** A scanned document opens fine and yields one empty
    string per page, so the list is non-empty while the text is not — which is
    how a scan once went down the text path and had audit 3 refuse a reading it
    could never have satisfied. Decide on the text, not on the page count.
    """
    return not any(page.strip() for page in pages)


def accept(reading: datasheets.Reading, proposal: dict, pdf_path=None,
           fields: list[str] | None = None,
           source: str | None = None) -> datasheets.Reading:
    """Audit a proposed reading and fill what survives. Mutates and returns it.

    `proposal` is `{field: {"value": float|None, "page": int, "quote": str}}`.

    **Only ever fills gaps.** A value the tables already produced is never
    overwritten, because a regex over a line that was checked to contain the
    number is a better witness than anybody's reading of it.

    `source` is normally decided here — `VISION` when the document has no text,
    `TEXT` when it has — and may be forced to `VISION` by a caller who read an
    image of a page that *does* carry text. It may never be forced the other
    way: claiming `read` on a document with no text would claim audit 3 ran.
    """
    wanted = [f for f in (fields or reading.wants_a_reader) if f in FIELDS]
    if not wanted:
        return reading

    pages = page_text(pdf_path or reading.path)
    scanned = is_scanned(pages)
    if source == VISION or scanned:
        source = VISION
    else:
        source = TEXT

    # Recorded before the audit rather than after, so a document that was
    # offered a reading and had all of it refused still counts as asked and is
    # not re-queued on every view of the table.
    reading.asked_a_reader = True

    _absorb(reading, proposal, pages, wanted, source)
    for problem in datasheets.cross_check(reading):
        reading.notes.append(problem)
    return reading


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

def _absorb(reading: datasheets.Reading, proposal: dict, pages: list[str],
            wanted: list[str], source: str) -> None:
    vision = source == VISION
    for name in wanted:
        entry = proposal.get(name)
        if not isinstance(entry, dict):
            continue
        raw = entry.get("value")
        if raw is None:
            # An honest "the document does not state it". Recorded, because it
            # is a different answer from "the reader was never asked".
            reading.notes.append(
                f"the reader reports that this document states no {name} — "
                f"which is a different answer from never having been asked")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            _refuse(reading, name, raw, entry,
                    "the value was not a number", source)
            continue

        page = entry.get("page")
        quote = str(entry.get("quote") or "").strip()
        if not isinstance(page, int) or not quote:
            _refuse(reading, name, value, entry,
                    "no page and quote were given, and a value without a cite "
                    "is not accepted from any source", source)
            continue
        if not (1 <= page <= max(len(pages), 1)):
            _refuse(reading, name, value, entry,
                    f"page {page} does not exist in a "
                    f"{len(pages)}-page document", source)
            continue
        if not vision:
            problem = _quote_is_real(quote, value, pages[page - 1])
            if problem:
                _refuse(reading, name, value, entry, problem, source)
                continue
        # Audit 5, and it applies on the vision path too — where it is the only
        # thing standing between a faithfully-transcribed mislabelling and a
        # thermal resistance that is wrong by an order of magnitude.
        mislabelled = _quote_names_the_right_thing(name, quote)
        if mislabelled:
            _refuse(reading, name, value, entry, mislabelled, source)
            continue
        refusal = datasheets.accept(name, value)
        if refusal:
            _refuse(reading, name, value, entry, refusal, source)
            continue

        setattr(reading, name, value)
        reading.cites[name] = datasheets.Cite(
            source=source, page=page, text=quote[:160]).as_dict()


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _quote_is_real(quote: str, value: float, page_text: str) -> str | None:
    """Audit 3 — the quoted line is on the page it claims, and contains the
    number.

    Whitespace-insensitive because pdfplumber has already collapsed it, so a
    model reading the rendered page and this reading the extracted text will
    not agree character for character.
    """
    if _squash(quote) not in _squash(page_text):
        return ("the quoted line is not on the page it was cited to, so the "
                "value is not attributable to this document")
    rendered = f"{value:g}"
    if rendered not in _squash(quote) and rendered not in quote:
        return (f"the quoted line does not contain {rendered}, so the number "
                f"did not come from the text it was attributed to")
    return None


#: The **symbol** for each quantity — `RthetaJA`, `RthJC`, `thetaJA`. Unambiguous.
_SYMBOL_JA = re.compile(r"(?i)R\s*[\u03b8\u0398]\s*JA\b|R\s*th\s*JA\b"
                        r"|[\u03b8\u0398]\s*JA\b|\bRJA\b")
_SYMBOL_JC = re.compile(r"(?i)R\s*[\u03b8\u0398]\s*JC\b|R\s*th\s*JC\b"
                        r"|[\u03b8\u0398]\s*JC\b|\bRJC\b")

#: The **prose** for each. Where these vendors copy and paste.
_PROSE_JA = re.compile(r"(?i)Junction\s*(?:-|\s|to|\u2013)*\s*Ambient")
_PROSE_JC = re.compile(r"(?i)Junction\s*(?:-|\s|to|\u2013)*\s*Case")


def _quote_names_the_right_thing(field: str, quote: str) -> str | None:
    """Audit 5 — the citation must not name a different quantity.

    **Both halves of this rule come from real datasheets in this repository,
    and the second half comes from getting the first half wrong.**

    One vendor's Absolute Maximum Ratings table has a row whose *symbol* is
    ``RθJC`` and whose *parameter text* reads "Thermal Resistance, Junction to
    Ambient". A vision model read it faithfully and offered the 3.0 °C/W beside
    it as **θJA**. For a DFN3x3 that is impossible — a package reaching 3 °C/W
    to still air does not exist — but no honest band excludes 3.0, and a
    scanned page has no text for audit 3 to check against. Only the citation
    catches it.

    The first version of this refused any quote naming *both* quantities as
    self-contradictory. That was too blunt, and a second document showed why:
    another vendor's θJC row reads

        RθJC ThermalResistanceJunction-Ambient1 --- 6.5 ℃/W

    — the same copy-paste, on a value that is certainly the junction-to-case
    one. Refusing it would have thrown away a good number, **and it disagreed
    with this module's own table path**, which matches on the symbol and takes
    it happily.

    So: **the symbol column outranks the prose.** A symbol is a machine-readable
    token that these vendors get right; the parameter text is prose, and prose
    is where the copy-paste errors live. Where a quote carries a symbol, that
    decides. Where it carries none, the prose is all there is and is used.
    A quote naming neither is left alone — real tables routinely put the symbol
    in one cell and the number in another.
    """
    if field not in ("theta_ja_c_per_w", "theta_jc_c_per_w"):
        return None
    wants_ja = field == "theta_ja_c_per_w"
    named = "junction-to-case" if wants_ja else "junction-to-ambient"

    symbol_ja, symbol_jc = bool(_SYMBOL_JA.search(quote)), bool(_SYMBOL_JC.search(quote))
    if symbol_ja or symbol_jc:
        mine, theirs = (symbol_ja, symbol_jc) if wants_ja else (symbol_jc, symbol_ja)
        if mine:
            return None
        if theirs:
            return (f"the quoted line's symbol names {named}, which is not the "
                    f"quantity this value was offered as")
        return None

    prose_ja, prose_jc = bool(_PROSE_JA.search(quote)), bool(_PROSE_JC.search(quote))
    mine, theirs = (prose_ja, prose_jc) if wants_ja else (prose_jc, prose_ja)
    if theirs and not mine:
        return (f"the quoted line names {named}, which is not the quantity "
                f"this value was offered as, and it carries no symbol to "
                f"outrank the wording")
    return None


def _refuse(reading: datasheets.Reading, name: str, value, entry: dict,
            why: str, source: str = TEXT) -> None:
    """Drop a proposed value and say why, on the reading itself.

    **Kept, not discarded.** A refused value is evidence about the document and
    about the reading, and `parts` shows it — a field that silently stayed
    empty and a field that was offered 3.0 and refused are different states.
    """
    reading.refused.append({
        "field": name, "value": value, "page": entry.get("page"),
        "text": str(entry.get("quote") or "")[:160],
        "why": why, "source": source})
