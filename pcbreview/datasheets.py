"""Reading datasheets — the LCSC catalogue first, the document's tables second.

Two stages here, in increasing order of doubt, with every value carrying
**where it came from and what page it was on**. A number without a cite is not
accepted from anywhere.

There used to be a third stage — a small local vision model, for what the first
two missed. It is gone, and `reader.py` says why at length: Claude is reading
this project, has the document and the schematic, and is a better reader than a
4B model that answered a scanned page about one time in seven. What survives is
the part that mattered: **the five audits**, which now stand over Claude's
reading exactly as they stood over the model's.

What the two stages are actually good for
------------------------------------------
Measured on three real datasheets, which is what this was built against rather
than an imagined TI part:

| document | text layer | what the deterministic path gets |
|---|---|---|
| a second-source MOSFET | 3 979 chars | θJC, and no θJA — the datasheet states none |
| a second-source P-channel | 2 900 chars | all three, on page 1 |
| a second-source load switch | **zero** | **nothing, ever** — five pages of images |

That last row is the finding that shaped this module. **One of the three is a
scanned image with no text layer at all** — 15 images, 0 characters — so no
regex will ever read it, and a reader that can look at the page is not an edge
case but a third of that corpus.

Regular "Thermal Information" tables are a TI/ADI/onsemi habit. These were
cheap second-source parts, which `thermal.py` already notes is exactly where
the risk concentrates, and their datasheets are far less regular.

The distinction this module refuses to blur
--------------------------------------------
**"This document says no θJA" and "this document could not be read" are
opposite claims and must never look alike.** `thermal.py` already fights this
battle — `theta_ja_stated: false` is *required* rather than letting the key be
merely absent, because an omitted key cannot be told apart from a forgotten
one. The same failure is available here and is worse, because it would be
introduced by a tool rather than by a person.

So a `Reading` records `readable`, and a document with no text layer comes back
with **nothing extracted and a stated reason**, never as a part whose datasheet
is silent on thermals. `wants_a_reader` then names the fields still open, which
is what `parts` prints and `parts --read` fills.

The cache, and why it may be shared
------------------------------------
Keyed by the PDF's own content hash, so a document is read once ever —
including the part a person read. It sits beside the solve cache in the OS
cache directory, and it may, because what it holds is **public information
about a catalogue part**: that a part has a junction-to-case resistance of
6.2 °C/W is a fact about something anyone can buy, not a fact about anybody's
board.

Designators stay out of it. The mapping from a designator to an LCSC code
belongs to the project and is never written here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Point at another jlc-trawler catalogue, or `off` to skip the lookup.
CATALOGUE_ENV = "PCBREVIEW_LCSC_CATALOGUE"

#: Where jlc-trawler keeps its databases by default.
DEFAULT_CATALOGUE = Path.home() / "jlc-trawler-data" / "jlcpcb-components.sqlite3"

#: Point the extraction cache elsewhere, or `off` to disable it.
CACHE_ENV = "PCBREVIEW_DATASHEET_CACHE"

_CACHE_NAME = "datasheets.sqlite3"

#: Bumped when the extraction *rules* change, so a cached reading made by an
#: older parser is re-read rather than trusted. The parser is not hashed the
#: way `solvestore` hashes the solver: a datasheet reading is cheap to redo
#: and a stale one is visible to a human, where a stale field solve is not.
PARSER_VERSION = 1


# ---------------------------------------------------------------------------
# Range checks — the design brief §2.6, and they apply to every source alike
# ---------------------------------------------------------------------------
#
# "Implausible values are surfaced, not silently accepted." Note the bands are
# wide on purpose. They are a filter against a misparse — a page number read as
# a temperature, a `2.2` from the wrong column — and not a judgement about
# whether a part is any good.

#: Junction-to-ambient, °C/W. A big TO-247 tab on a heatsink is single digits;
#: a bare 0402 in still air is several hundred.
THETA_JA_BAND = (0.5, 600.0)

#: Junction-to-case, °C/W. Always lower than θJA and rarely above ~100.
THETA_JC_BAND = (0.01, 200.0)

#: Maximum junction temperature, °C. Silicon parts cluster at 125/150/175;
#: the band admits the outliers without admitting a page number.
TJ_MAX_BAND = (85.0, 250.0)


@dataclass
class Cite:
    """Where one number came from. Required — no value is kept without one."""
    source: str                  # "table" | "model" | "catalogue"
    page: int | None
    text: str                    # the line or span it was read out of

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Identity:
    """What the part *is*, from the local catalogue. No model, no PDF."""
    lcsc: str
    mpn: str | None = None
    manufacturer: str | None = None
    package: str | None = None
    category: str | None = None
    subcategory: str | None = None
    description: str | None = None
    #: The catalogue's own attribute table, verbatim and unparsed.
    #:
    #: Deliberately not mined for thermal numbers. It carries things like
    #: `Operating Temperature: -55C~+175C` and `Power Dissipation (Pd): 40W`,
    #: and both are *nearly* what the manifest wants without being it: an
    #: operating range is not a stated Tj(max), and a Pd at an unstated case
    #: temperature is not a dissipation. Turning either into a manifest value
    #: would be inventing attribution, which is the one thing `thermal.py`'s
    #: `source` field exists to prevent.
    attributes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reading:
    """One datasheet, read as far as it can be, with every value attributed."""
    path: str
    sha256: str
    pages: int
    #: characters pdfplumber found. Zero means a scanned document.
    characters: int
    readable: bool
    #: why not, when `readable` is False. Never blank in that case.
    why_not: str = ""
    identity: Identity | None = None
    theta_ja_c_per_w: float | None = None
    theta_jc_c_per_w: float | None = None
    t_junction_max_c: float | None = None
    cites: dict = field(default_factory=dict)
    #: whether a model has actually been asked about this document.
    #:
    #: **Load-bearing, and it was missing.** The Parts tab opens with a
    #: deterministic `GET`, which caches the reading; a cached reading was then
    #: returned whole, so the "read with the model" button found a hit and
    #: never asked anything. The button silently did nothing, and only the real
    #: flow showed it — every test passed.
    asked_a_reader: bool = False
    #: values that were found and then refused, with the reason. Surfaced
    #: rather than dropped: a datasheet whose theta_JA parsed as 4000 is a
    #: parser bug worth seeing, not a part with no theta_JA.
    refused: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def found_any(self) -> bool:
        return any(v is not None for v in (self.theta_ja_c_per_w,
                                           self.theta_jc_c_per_w,
                                           self.t_junction_max_c))

    @property
    def wants_a_reader(self) -> list[str]:
        """Fields the deterministic path did not fill. What Model A is for."""
        return [name for name, value in (
            ("theta_ja_c_per_w", self.theta_ja_c_per_w),
            ("theta_jc_c_per_w", self.theta_jc_c_per_w),
            ("t_junction_max_c", self.t_junction_max_c),
        ) if value is None]

    def as_dict(self) -> dict:
        out = asdict(self)
        out["identity"] = self.identity.as_dict() if self.identity else None
        out["found_any"] = self.found_any
        out["wants_a_reader"] = self.wants_a_reader
        return out


def accept(field_name: str, value: float) -> str | None:
    """`None` if the value is plausible, else why it was refused.

    Applied to every source alike — catalogue, table and model. the design brief §2.6
    says the model proposes and code audits; making the audit source-blind is
    what stops that becoming a rule the model is trusted to have followed.
    """
    band = {"theta_ja_c_per_w": THETA_JA_BAND,
            "theta_jc_c_per_w": THETA_JC_BAND,
            "t_junction_max_c": TJ_MAX_BAND}.get(field_name)
    if band is None:
        return f"{field_name} is not a checked field"
    low, high = band
    if not (low <= value <= high):
        return (f"{value:g} is outside the plausible band for {field_name} "
                f"({low:g} to {high:g})")
    return None


def cross_check(reading: Reading) -> list[str]:
    """Relations between fields, once each has passed its own band.

    Only one holds universally and it is worth having: heat leaves a die
    through the case more easily than through the whole package to air, so
    θJC < θJA always. A pair that says otherwise means two numbers were read
    out of the wrong columns.
    """
    problems: list[str] = []
    ja, jc = reading.theta_ja_c_per_w, reading.theta_jc_c_per_w
    if ja is not None and jc is not None and jc >= ja:
        problems.append(
            f"junction-to-case {jc:g} C/W is not below junction-to-ambient "
            f"{ja:g} C/W, which is not physical — one of the two was read "
            f"from the wrong row")
    return problems


# ---------------------------------------------------------------------------
# Stage 1 — the LCSC catalogue. No model, no PDF.
# ---------------------------------------------------------------------------

def catalogue_path() -> Path | None:
    override = os.environ.get(CATALOGUE_ENV, "").strip()
    if override:
        if override.lower() in ("off", "none", "no", "0"):
            return None
        return Path(override).expanduser()
    return DEFAULT_CATALOGUE if DEFAULT_CATALOGUE.exists() else None


def lcsc_from_name(path: Path | str) -> str | None:
    """`C1234567.pdf` -> `C1234567`.

    The filename, because that is how LCSC serves them and how the user's own
    files are actually named. Nothing is inferred from the document body: a
    part number appearing inside a PDF may be a cross-reference to something
    else entirely.
    """
    stem = Path(path).stem.strip()
    # The whole stem first — the unambiguous case, and how LCSC serves them.
    match = re.fullmatch(r"(C\d{3,10})", stem, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    # Then a code delimited from the rest of the name: `C9100003_XYZ4321`,
    # `C9100003 (1)`, `XYZ4321-C9100003`. Real download folders look like this,
    # and refusing them sent perfectly identifiable documents to the
    # "no part attached" pile.
    #
    # **Still the filename, never the body.** A part number inside a PDF may be
    # a cross-reference to a competitor's device; a part number somebody typed
    # into the filename is somebody's statement about what the file is.
    # Lookahead on the trailing delimiter, not a consuming match: `C1 C2`
    # would otherwise eat the separator and find only the first, which is the
    # ambiguous case this exists to detect.
    found = re.findall(r"(?:^|[^0-9A-Za-z])(C\d{4,10})(?=[^0-9A-Za-z]|$)",
                       stem, re.IGNORECASE)
    # Exactly one, or it is ambiguous and the sidecar is the honest answer.
    unique = {code.upper() for code in found}
    return unique.pop() if len(unique) == 1 else None


def identify(code: str, catalogue: Path | str | None = None) -> Identity | None:
    """One LCSC code against the local catalogue. `None` if it is not there.

    Read-only, and never fatal: an absent or locked catalogue means the part is
    unidentified, which is a smaller loss than refusing to read the datasheet.
    """
    path = Path(catalogue) if catalogue is not None else catalogue_path()
    if path is None or not Path(path).exists():
        return None
    try:
        number = int(code[1:])
    except (ValueError, IndexError):
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                     timeout=5.0)
        try:
            row = connection.execute(
                "SELECT c.mfr, c.package, c.description, c.extra, "
                "       cat.category, cat.subcategory, m.name "
                "FROM components c "
                "LEFT JOIN categories cat ON cat.id = c.category_id "
                "LEFT JOIN manufacturers m ON m.id = c.manufacturer_id "
                "WHERE c.lcsc = ?", (number,)).fetchone()
        finally:
            connection.close()
    except Exception:                                        # noqa: BLE001
        return None
    if row is None:
        return None

    mpn, package, description, extra, category, subcategory, maker = row
    attributes: dict = {}
    try:
        attributes = (json.loads(extra) or {}).get("attributes") or {}
    except (TypeError, ValueError):
        attributes = {}
    return Identity(lcsc=code.upper(), mpn=mpn, manufacturer=maker,
                    package=package, category=category,
                    subcategory=subcategory, description=description,
                    attributes=attributes)


# ---------------------------------------------------------------------------
# Stage 2 — the tables. pdfplumber, regex, page cites.
# ---------------------------------------------------------------------------
#
# The patterns are built against the user's own datasheets rather than an
# imagined one, and the single most important thing about them is the `\s*`
# everywhere: pdfplumber returns these documents with the spaces collapsed —
# `ThermalResistance,Junction-to-Case` and `OperatingJunctionTemperatureRange`
# are real lines from real files here. A pattern written against how the page
# *looks* would match neither.

_NUMBER = r"(-?\d+(?:\.\d+)?)"

#: The labels, matched on their own. **The value is not captured here**, and
#: that separation is the whole point — see `_value_on`.
_THETA_JA = re.compile(
    r"(?i)R\s*[\u03b8\u0398]?\s*JA|R\s*th\s*JA|[\u03b8\u0398]\s*JA"
    r"|Junction\s*-?\s*to\s*-?\s*Ambient|Junction\s*-\s*Ambient")

_THETA_JC = re.compile(
    r"(?i)R\s*[\u03b8\u0398]?\s*JC|R\s*th\s*JC|[\u03b8\u0398]\s*JC"
    r"|Junction\s*-?\s*to\s*-?\s*Case|Junction\s*-\s*Case")

#: Maximum junction temperature, often written as a *range* on these parts --
#: `-55to150` -- so the upper end is taken when a range is what is there.
_TJ_RANGE = re.compile(
    r"(?i)(?:T\s*J\b|Junction\s*Temperature)[^0-9\n]{0,60}?"
    r"-?\d+(?:\.\d+)?\s*(?:to|~|\.\.\.|-|\u2014|\u2013)\s*\+?" + _NUMBER)

_TJ_SINGLE = re.compile(
    r"(?i)(?:T\s*J\s*\(?\s*max\)?|Maximum\s+Junction\s+Temperature"
    r"|Junction\s+Temperature\s*,?\s*max)"
    r"[^0-9\n]{0,60}?" + _NUMBER)

#: A number, with what sits either side of it, so a footnote can be told from a
#: measurement. `(?<![A-Za-z])` is the load-bearing part.
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9.])(-?\d+(?:\.\d+)?)")

#: A thermal-resistance unit in any of the spellings these documents use:
#: `C/W`, `\u00b0C/W`, `\u2103/W`, with or without spaces.
_UNIT = re.compile(r"\s*(?:\u2103|\u00b0\s*C|C)\s*/\s*W", re.IGNORECASE)


def _value_on(line: str) -> tuple[float | None, str]:
    """The measured value on a labelled line, and how it was chosen.

    **This function exists because of a real defect, caught on the user's own
    datasheet before it could reach a finding.** The line

        R\u03b8JA ThermalResistanceJunction-Ambient1 --- 36 \u2103/W

    yielded **1.0** under the obvious rule of "the first number after the
    label": the `1` is a *footnote marker* glued to the word `Ambient`, and
    pdfplumber returns these documents with the spaces already collapsed so it
    does not even look like a superscript any more.

    Worse, the range check could not save it -- 1.0 \u00b0C/W is a perfectly
    plausible junction-to-ambient figure for a big tabbed package, so the audit
    would have accepted a part reading 36x cooler than it is. A plausible wrong
    number is the worst thing this module could produce.

    Two rules, in order:

    1. **A digit glued to a letter is not a measurement.** `Ambient1` and
       `Note2` are footnote markers, and `(?<![A-Za-z0-9.])` drops them.
    2. **A number carrying the unit wins.** `36 \u2103/W` beats every other number
       on the line whatever the order. Where no number carries a unit -- and
       `ThermalResistance,Junction-to-Case R 6.2` is a real line with none --
       the *last* candidate is taken, because these tables put the label on the
       left and the value on the right.
    """
    candidates = list(_CANDIDATE.finditer(line))
    if not candidates:
        return None, "no number on the line that was not a footnote marker"
    for match in candidates:
        if _UNIT.match(line, match.end()):
            return float(match.group(1)), "carried the C/W unit"
    return float(candidates[-1].group(1)), "last number on the line, no unit"


def read_tables(path: Path | str) -> Reading:
    """Everything the deterministic path can get out of one PDF.

    Never raises. A PDF that will not open, or has no text layer, comes back
    with `readable=False` and a reason — which is a different statement from
    "this datasheet quotes no thermal resistance", and the two must not be
    allowed to look alike.
    """
    path = Path(path)
    reading = Reading(path=str(path), sha256=content_hash(path), pages=0,
                      characters=0, readable=False)
    code = lcsc_from_name(path)
    if code:
        reading.identity = identify(code)

    try:
        import pdfplumber
    except ImportError:
        reading.why_not = (
            "pdfplumber is not installed, so no table could be read. "
            "The catalogue lookup above still applies.")
        return reading

    try:
        with pdfplumber.open(str(path)) as pdf:
            reading.pages = len(pdf.pages)
            lines: list[tuple[int, str]] = []
            for number, page in enumerate(pdf.pages, 1):
                reading.characters += len(page.chars)
                for line in (page.extract_text() or "").splitlines():
                    if line.strip():
                        lines.append((number, line.strip()))
    except Exception as exc:                                 # noqa: BLE001
        reading.why_not = f"the PDF could not be opened — {type(exc).__name__}"
        return reading

    if reading.characters == 0:
        reading.readable = False
        reading.why_not = (
            f"this document has no text layer at all — {reading.pages} page(s) "
            f"of images. Nothing can be read from it by pattern; it needs a "
            f"vision-capable reader or a human. **This is not the same as the "
            f"datasheet having no thermal data.**")
        return reading

    reading.readable = True
    _scan(reading, lines)
    for problem in cross_check(reading):
        reading.notes.append(problem)
    return reading


def _scan(reading: Reading, lines: list[tuple[int, str]]) -> None:
    """First plausible match per field wins, and records its page.

    The two thermal resistances find their *label* by pattern and then their
    *value* by `_value_on`, which is a separation the first version of this did
    not have and was wrong without. Junction temperature keeps its combined
    pattern because the thing being matched there is a range whose upper end is
    the answer, and pulling those apart would lose which end was which.
    """
    labelled = (("theta_ja_c_per_w", _THETA_JA),
                ("theta_jc_c_per_w", _THETA_JC))
    for name, pattern in labelled:
        for page, line in lines:
            if not pattern.search(line):
                continue
            value, how = _value_on(line)
            if value is None:
                continue
            refusal = accept(name, value)
            if refusal:
                reading.refused.append(
                    {"field": name, "value": value, "page": page,
                     "text": line[:160], "why": refusal})
                continue
            setattr(reading, name, value)
            reading.cites[name] = Cite(source="table", page=page,
                                       text=f"{line[:150]}  ({how})").as_dict()
            break

    for pattern in (_TJ_RANGE, _TJ_SINGLE):
        if reading.t_junction_max_c is not None:
            break
        for page, line in lines:
            match = pattern.search(line)
            if not match:
                continue
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            refusal = accept("t_junction_max_c", value)
            if refusal:
                reading.refused.append(
                    {"field": "t_junction_max_c", "value": value, "page": page,
                     "text": line[:160], "why": refusal})
                continue
            reading.t_junction_max_c = value
            reading.cites["t_junction_max_c"] = Cite(
                source="table", page=page, text=line[:160]).as_dict()
            break


# ---------------------------------------------------------------------------
# Content hashing and the cache
# ---------------------------------------------------------------------------

def content_hash(path: Path | str) -> str:
    """The PDF's own bytes. the design brief §2.6 — read once per document ever."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def cache_path() -> Path | None:
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        if override.lower() in ("off", "none", "no", "0"):
            return None
        candidate = Path(override).expanduser()
        return candidate / _CACHE_NAME if (candidate.is_dir()
                                           or not candidate.suffix) else candidate
    # Beside the solve cache, in the OS cache directory, and allowed there for
    # the same reason: what this holds is public information about a catalogue
    # part, not anything about anybody's board.
    from pcbreview import solvestore                         # noqa: PLC0415

    beside = solvestore.default_path()
    return beside.parent / _CACHE_NAME if beside is not None else None


# ---------------------------------------------------------------------------
# The cache — read once per document, ever
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    sha256  TEXT PRIMARY KEY,
    parser  INTEGER NOT NULL,
    payload TEXT NOT NULL,
    made    REAL NOT NULL
) WITHOUT ROWID;
"""


def _open_cache():
    """The cache connection, or `None`. Never fatal, exactly like `solvestore`."""
    path = cache_path()
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        connection.commit()
        return connection
    except Exception:                                        # noqa: BLE001
        return None


def cached(sha256: str) -> Reading | None:
    """A previous reading of this exact document, if the parser is unchanged.

    A reading made by an older `PARSER_VERSION` is ignored rather than trusted:
    the footnote defect this module was built around was a *parsing* bug, and
    every cached reading made before it was fixed carried a plausible wrong
    number. Bumping the version is how such a fix reaches documents already
    read.
    """
    if not sha256:
        return None
    connection = _open_cache()
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT payload FROM readings WHERE sha256 = ? AND parser = ?",
            (sha256, PARSER_VERSION)).fetchone()
    except Exception:                                        # noqa: BLE001
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        return _reading_from(json.loads(row[0]))
    except Exception:                                        # noqa: BLE001
        return None


def remember(reading: Reading) -> None:
    if not reading.sha256:
        return
    connection = _open_cache()
    if connection is None:
        return
    try:
        import time

        connection.execute(
            "INSERT INTO readings (sha256, parser, payload, made) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(sha256) DO UPDATE SET payload = excluded.payload, "
            "parser = excluded.parser, made = excluded.made",
            (reading.sha256, PARSER_VERSION,
             json.dumps(reading.as_dict(), default=str), time.time()))
        connection.commit()
    except Exception:                                        # noqa: BLE001
        pass
    finally:
        connection.close()


def _reading_from(data: dict) -> Reading:
    """A cached payload back into a `Reading`.

    `path` is deliberately **not** restored from the cache. The same document
    can sit in two projects under two names, and a reading is about the
    document rather than about where a copy of it happens to live.
    """
    identity = data.get("identity")
    reading = Reading(
        path="", sha256=data.get("sha256", ""), pages=int(data.get("pages", 0)),
        characters=int(data.get("characters", 0)),
        readable=bool(data.get("readable")), why_not=data.get("why_not", ""),
        asked_a_reader=bool(data.get("asked_a_reader")),
        theta_ja_c_per_w=data.get("theta_ja_c_per_w"),
        theta_jc_c_per_w=data.get("theta_jc_c_per_w"),
        t_junction_max_c=data.get("t_junction_max_c"),
        cites=data.get("cites") or {}, refused=data.get("refused") or [],
        notes=data.get("notes") or [])
    if identity:
        reading.identity = Identity(**{
            k: v for k, v in identity.items()
            if k in Identity.__dataclass_fields__})
    return reading


def clear_cache() -> None:
    connection = _open_cache()
    if connection is None:
        return
    try:
        connection.execute("DELETE FROM readings")
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# One document, through every stage
# ---------------------------------------------------------------------------

def read(path: Path | str, use_cache: bool = True) -> Reading:
    """The LCSC catalogue, then the document's own tables. Nothing else.

    **The third stage is not here any more, and its absence is deliberate.**
    The ordering was *catalogue, tables, then a model for the rest*, and in a
    skill the reader for "the rest" is Claude — which has the document, has the
    schematic, and reads a scanned page far better than the small local model
    this replaced. What is left in the gap is `wants_a_reader`: the list of
    fields the deterministic stages could not fill, which `parts` prints and
    `parts --read` fills, through the same five audits.

    A cached reading is returned whole, keyed by the PDF's content hash, so a
    document is read once ever — including the part a person read.
    """
    path = Path(path)
    digest = content_hash(path)
    if use_cache:
        hit = cached(digest)
        if hit is not None:
            hit.path = str(path)
            return hit

    reading = read_tables(path)
    if use_cache:
        remember(reading)
    return reading


# ---------------------------------------------------------------------------
# A whole project
# ---------------------------------------------------------------------------

#: Where datasheets are looked for, relative to the board. `Datasheets/` is
#: where the user's own live; the board's own folder is where a dropped file
#: would land.
_SEARCH = (".", "Datasheets", "datasheets", "docs", "Documents")


def find_pdfs(board_path: Path | str) -> list[Path]:
    """Every PDF beside the board, in the places one would actually be.

    Not a recursive walk: `pipeline.py` already refuses to descend into
    `review/`, `*-backups/` and the rest for good reasons, and a datasheet
    folder six levels down is not a convention worth guessing at.
    """
    root = Path(board_path).resolve().parent
    found: list[Path] = []
    #: Identity, not spelling. `_SEARCH` carries both `Datasheets` and
    #: `datasheets` because either is a real convention — and on a
    #: case-insensitive filesystem (macOS by default, Windows always) BOTH
    #: match the one directory, so every PDF in it is found twice under two
    #: spellings that compare unequal as paths. `assemble()` then claims one
    #: copy for its designator and reports the other as a datasheet "nothing
    #: on the board claims", which is the exact false statement the unattached
    #: row exists to prevent.
    #:
    #: **`st_ino` is not always available and 0 does not mean "the same file".**
    #: Windows populates it from `GetFileInformationByHandle` on NTFS, but FAT32
    #: and some SMB shares return 0 for everything — and keying on that would
    #: collapse every datasheet in the folder into one and silently drop the
    #: rest, which is worse than the duplicate it exists to prevent. So the
    #: inode is used only when the filesystem actually gave one, and the
    #: fallback is the case-folded real path, which is what makes the two
    #: spellings the same file in the first place.
    seen: set = set()
    for relative in _SEARCH:
        directory = root / relative if relative != "." else root
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.pdf")):
            if not candidate.is_file():
                continue
            key = _identity(candidate)
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
    return found


def _identity(path: Path):
    """What makes two paths the same document, on any filesystem.

    The inode when there is one — it also catches a symlinked or hardlinked
    duplicate, which no amount of path normalisation would. Otherwise the
    case-folded real path, which is enough for the case this exists for.
    """
    try:
        info = path.stat()
        if info.st_ino:
            return (info.st_dev, info.st_ino)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(str(path)))


def ingest(board_path: Path | str, use_cache: bool = True) -> list[Reading]:
    """Read every datasheet in the project, deterministically."""
    return [read(pdf, use_cache=use_cache) for pdf in find_pdfs(board_path)]
