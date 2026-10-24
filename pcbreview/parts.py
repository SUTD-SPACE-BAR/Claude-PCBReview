"""The Parts table — the design brief §2.5's other half, and the manifest writer.

Phase 3 built the Nets half of the Facts tab and deliberately left this one
(§30.1): its columns are junction temperatures, and nothing could compute one
until a datasheet had been read. `datasheets.py` and `reader.py` now do that,
so this joins the three things that have to meet for a part to be reviewable:

    what the board has   ->  the schematic's designators and LCSC fields
    what the part is     ->  a datasheet, read and cited
    what it is doing     ->  the dissipation, which only the user knows

The third one is the point
--------------------------
the design brief §2.6: *"`dissipation_w` is never extracted, because it is not in the
datasheet — it is an operating-point calculation. So it stays an editable
field."* `thermal.py` agrees from the other side and refuses an entry without
one, because a missing dissipation *"would silently read as 0 W and report a
comfortable junction temperature"*.

So this module's job is not to fill the manifest in. It is to get every part as
far as it can be got, and then **say exactly what is still missing and why** —
which is §2.10's fourth principle, *specific questions with evidence attached,
never a blank form*.

What that costs, measured on a real project
-------------------------------------------
On a small two-layer board with four semiconductors and three datasheets, the
deterministic path alone completes **no entries at all**, and the reason is
worth stating precisely because it is not the one you would guess:

| part | read | writable? |
|---|---|---|
| two MOSFETs | nothing — the PDF is scanned | **no**, and the reason is *could not be read*, never *states nothing* |
| a third MOSFET | θJC 6.2 only | **no** — `thermal.load` refuses a part with no θJA *and* no dissipation, because there is then nothing to bound. θJC is not a substitute: it is the path to the package surface, not to air |
| a TVS diode | — | **no** — no datasheet for its LCSC code was supplied |
| a fourth part | θJA 36, θJC 6.5, Tj 150 | **the one document that read completely, and nothing on the board carried its LCSC code.** It gets a row saying so rather than being silently dropped |

So the count is not "one of three parts": it is **one of three documents, and
zero of four parts**, and the two facts point at different work. Cheap
second-source datasheets are where `thermal.py` already says the risk
concentrates, and this is what that looks like from the inside.

It is also the measurement that says what the model is actually for. The
vision path was the only route to the two scanned parts, which were half that
board's semiconductors.

Writing the file
----------------
the facts-tab record §6's pattern, and every clause of it earns its place:

* **validated by the engine's own loader before it lands** — `thermal.load`
  is strict, and a manifest this wrote that the audit then refused would be a
  worse failure than not writing one;
* **atomic** — written whole and moved, so a crash cannot leave half a
  manifest that loads;
* **never over a file that will not parse** — that file is a person's work in
  progress and the tool has no idea what they meant;
* **a hand-written file is copied aside exactly once** — the user's own
  reasoning lives in its comments, and this writer does not round-trip them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pcbreview import thermal as thermal_module               # noqa: E402

#: What `thermal.py` calls the file, and where `audit_files` looks for it.
SUFFIX = ".thermal.yaml"


class PartsError(Exception):
    """Raised when a manifest cannot be written."""


@dataclass
class Row:
    """One part, and everything known about whether it can be reviewed."""
    reference: str
    value: str = ""
    lcsc: str | None = None
    mpn: str | None = None
    package: str | None = None
    #: the datasheet this part's numbers came from, if one was found
    document: str | None = None
    theta_ja_c_per_w: float | None = None
    theta_jc_c_per_w: float | None = None
    t_junction_max_c: float | None = None
    #: `field -> {source, page, text}`, straight from the reading
    cites: dict = field(default_factory=dict)
    #: what the user has already declared, if a manifest exists
    dissipation_w: float | None = None
    in_manifest: bool = False
    #: why this part cannot be written into a manifest yet, in the user's terms
    blocked: list = field(default_factory=list)
    #: things worth saying that are not blocking
    notes: list = field(default_factory=list)
    #: Whether this part has a *junction* — a semiconductor. False for a power
    #: inductor, a shunt or a fuse, which dissipate and get hot and have no
    #: junction temperature. `thermal.load` has no entry shape for those, so
    #: they are listed and said to be unmodelled rather than left out. See
    #: `_components`.
    junction: bool = True
    #: Why this part is in the table at all — the designator prefix, or the
    #: words in its own description that say it dissipates.
    because: str = ""
    #: Values a reading offered and the audits refused, with the reason. Kept
    #: because a field that stayed empty and a field that was offered 3.0 and
    #: refused are different states, and only one of them needs a human.
    refused: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.blocked

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in (
            "reference", "value", "lcsc", "mpn", "package", "document",
            "theta_ja_c_per_w", "theta_jc_c_per_w", "t_junction_max_c",
            "cites", "dissipation_w", "in_manifest", "blocked", "notes",
            "junction", "because", "refused")}
        out["ready"] = self.ready
        return out


def assemble(schematic, readings, manifest=None, attached=None) -> list[Row]:
    """Join the schematic, the datasheets and the manifest into one table.

    Two ways a document reaches a designator, and the second exists because the
    first is narrower than it looks:

    * **the LCSC code**, in the symbol and in the PDF's filename. It is the one
      link that requires no guessing, and it is how LCSC serves the files;
    * **`attached`**, a `{pdf name or LCSC code: designator}` map somebody
      wrote by hand — see `attachments()`. A datasheet called
      `TPS62840_rev_C.pdf` carries no code to match on, and reading a part
      number out of the document body is exactly the guess this module refuses
      to make. **A person looking at both files is the right resolver**, and
      one line of a sidecar is cheaper than a wrong match.

    A PDF that still reaches no designator gets a row of its own saying so.
    Every supplied datasheet is accounted for; a document silently ignored is
    the failure that rule exists to prevent.
    """
    attached = dict(attached or {})
    by_code = {r.identity.lcsc: r for r in readings
               if r.identity and r.identity.lcsc}
    # A reading may also be claimed by name, which is how a document with no
    # code in its filename reaches a part at all.
    by_name = {Path(r.path).name: r for r in readings if r.path}
    entered = dict(getattr(manifest, "parts", {}) or {})

    #: designator -> reading, from the sidecar. Both key shapes resolve here so
    #: that `assemble` never has to care which was written.
    by_reference: dict[str, object] = {}
    for key, reference in attached.items():
        found = by_name.get(key) or by_code.get(str(key).upper())
        if found is not None:
            by_reference[reference] = found

    rows: list[Row] = []
    claimed: set[int] = set()
    for reference, code, value, junction, because in _components(schematic):
        attached_here = by_reference.get(reference)
        reading = attached_here or (by_code.get(code) if code else None)
        if reading is not None:
            claimed.add(id(reading))
        row = _row(reference, value, code, reading, entered, junction)
        row.because = because
        # **A hand attachment that contradicts the symbol is worth saying.**
        # Attaching by hand is how a document with an unhelpful filename
        # reaches a part; it is also how a document reaches the wrong part.
        # Both codes are facts, they disagree, and the person who wrote the
        # attachment is the only one who can say which is right.
        if attached_here is not None and code:
            theirs = (attached_here.identity.lcsc
                      if attached_here.identity else None)
            if theirs and theirs.upper() != code.upper():
                row.notes.append(
                    f"you attached {Path(attached_here.path).name} to this "
                    f"part by hand, and that document is {theirs} while this "
                    f"symbol says {code}. One of the two is wrong and nothing "
                    f"here can tell which")
        rows.append(row)

    for reading in sorted(readings, key=lambda r: str(r.path)):
        if id(reading) in claimed:
            continue
        code = reading.identity.lcsc if reading.identity else None
        name = Path(reading.path).name if reading.path else "a datasheet"
        row = _row(f"({name}: no part attached)", "", code, reading, {})
        row.notes.append(
            "this datasheet was supplied and nothing on the board claims it — "
            "listed so that it is not silently ignored")
        row.blocked.append(
            f"no designator to attach it to. If you know which part it is, "
            f"`--attach {name}=<REF>` records that")
        rows.append(row)
    return rows


#: Designator prefixes that carry a junction. These are in the table whatever
#: their description says, because a symbol with an empty description is the
#: normal case and a semiconductor with no row is the failure this table exists
#: to prevent.
_JUNCTION_PREFIX = re.compile(r"^(?:U|Q|D|IC|VR|OK|ISO)\d")

#: Designators that are passive and can still get hot: an inductor, a resistor,
#: a fuse, a ferrite. Not thermal candidates on their own — most of them are
#: 0402s dissipating microwatts — only when something states a current.
_PASSIVE_PREFIX = re.compile(r"^(?:L|R|RN|F|FB|TH)\d")

#: A current rating of an amp or more, in amps, anywhere in a part's value or
#: description: `1.5uH 2.7A`, `3A`, `rated 10 A`.
#:
#: **This is the rule that puts a power inductor in the table**, and it is
#: deliberately not "the description says inductor". Measured over two real
#: boards: matching the word would add four inductors of which one is a power
#: part and three are RF chokes dissipating nothing, whereas a stated amp
#: rating picks out exactly the part somebody sized for current. `thermal.py`
#: refused `\bdiode\b` on the same grounds and for the same reason.
#:
#: `mA` is excluded by the word boundary on purpose — a 500 mA signal part is
#: not a thermal question.
_RATED_AMPS = re.compile(r"(?<![.\d])(\d+(?:\.\d+)?)\s*A\b")


def _rated_current(*fields) -> float | None:
    """The largest stated current rating of at least 1 A, or `None`."""
    best = None
    for text in fields:
        for match in _RATED_AMPS.finditer(text or ""):
            # `mA` and `nA` are ruled out by requiring the digit to touch the
            # `A`, and by the boundary; `uH` and `nH` never match at all.
            if match.group(0).lower().endswith(("ma", "na", "ua")):
                continue
            try:
                amps = float(match.group(1))
            except ValueError:
                continue
            if amps >= 1.0 and (best is None or amps > best):
                best = amps
    return best


def _components(schematic):
    """`(reference, lcsc, value, junction, because)` per designator, once each.

    **A designator prefix is not the rule, and using one as the rule was a
    real gap.** The first version took a `U`/`Q`/`D` prefix, on the grounds
    that a resistor does not have a junction — which is true and beside the
    point. A power inductor's DCR loss and a shunt's I²R are two of the
    commonest thermal problems on a power board, and under that rule neither
    got a row, a question, or a mention. The table's whole job is to say what
    is missing, and it was silently missing them.

    So there are two ways in, and they answer different questions:

    * **it has a junction** — a `U`, `Q`, `D` and friends, by designator. These
      need θJA and Tj(max) and `thermal.load` has an entry shape for them;
    * **it says it dissipates** — `thermal.dissipative_candidates`, which
      matches on what the symbol calls itself and already knows the vocabulary
      (`shunt`, `current sense`, `solenoid`, `relay`, `motor`, `regulat`,
      `mosfet`, …). Prefix-agnostic, which is the point: it catches `L3` and
      `R47`.

    A part that arrives only by the second door is marked `junction=False`. It
    is **not** silently dropped and it is **not** pretended to be a
    semiconductor: `_row` says that this tool models junction temperatures and
    has nothing to say about its rise, which is a fact the user is entitled to
    rather than a silence.
    """
    if not schematic:
        return []
    dissipative = dict(thermal_module.dissipative_candidates(schematic))
    seen: set[str] = set()
    out = []
    for component in getattr(schematic, "components", ()):
        reference = component.reference or ""
        if getattr(component, "dnp", False) or reference in seen:
            continue
        junction = bool(_JUNCTION_PREFIX.match(reference))
        named = reference in dissipative
        amps = (_rated_current(component.value,
                               getattr(component, "description", ""))
                if _PASSIVE_PREFIX.match(reference) else None)
        if not (junction or named or amps):
            continue
        seen.add(reference)
        if junction:
            because = "its designator says it is a semiconductor"
        elif named:
            because = (f"its own description says it dissipates "
                       f"({dissipative[reference]})")
        else:
            because = (f"it is a passive rated at {amps:g} A, so somebody "
                       f"sized it for current")
        out.append((reference, thermal_module._lcsc_code(component),
                    (component.value or "").strip(), junction, because))
    return sorted(out)


def _row(reference, value, code, reading, entered, junction: bool = True) -> Row:
    row = Row(reference=reference, value=value, lcsc=code, junction=junction)
    existing = entered.get(reference)
    if existing is not None:
        row.in_manifest = True
        row.dissipation_w = (existing.dissipation_w
                             if getattr(existing, "dissipation_stated", True)
                             else None)

    if not junction:
        # **Listed, and honestly.** This part dissipates — its own description
        # says so — and it has no junction, so `thermal.load` has no entry
        # shape for it and no check in this tool computes its rise. Saying that
        # is the whole reason it is in the table; leaving it out was the bug.
        row.blocked.append(
            "this part dissipates and has no junction, so there is no thermal "
            "manifest entry for it and NO CHECK HERE COMPUTES ITS TEMPERATURE. "
            "Its rise is real and is unmodelled — size it from its own "
            "datasheet (I²·DCR for an inductor, I²·R for a shunt) and say so "
            "in the review")
        if reading is None:
            return row

    if reading is None:
        if code:
            row.blocked.append(f"no datasheet for {code} was supplied")
        else:
            row.blocked.append(
                "this symbol carries no LCSC code, so no datasheet can be "
                "matched to it automatically. `--attach <pdf>=" + str(reference)
                + "` attaches one by hand")
        return row

    row.document = Path(reading.path).name if reading.path else None
    row.mpn = reading.identity.mpn if reading.identity else None
    row.package = reading.identity.package if reading.identity else None
    row.theta_ja_c_per_w = reading.theta_ja_c_per_w
    row.theta_jc_c_per_w = reading.theta_jc_c_per_w
    row.t_junction_max_c = reading.t_junction_max_c
    row.cites = dict(reading.cites)
    row.notes.extend(reading.notes)
    row.refused = list(getattr(reading, "refused", ()) or ())

    if not reading.readable:
        # The distinction `datasheets.py` refuses to blur, carried through to
        # the row a person actually looks at.
        row.blocked.append(reading.why_not)
    if not junction:
        # It already carries the reason that matters. Asking it for a junction
        # temperature it cannot have would be noise.
        return row
    if reading.t_junction_max_c is None:
        row.blocked.append(
            "no maximum junction temperature was found, and `thermal.load` "
            "requires one or an explicit `t_junction_max_stated: false`")
    if reading.theta_ja_c_per_w is None:
        row.blocked.append(
            "no junction-to-ambient thermal resistance was found. Without one "
            "and without a dissipation there is nothing to bound, which is "
            "what `thermal.load` refuses"
            + (" — this datasheet does quote junction-to-case, which is not a "
               "substitute: it is the path to the package surface, not to air"
               if reading.theta_jc_c_per_w is not None else ""))
    return row



# ---------------------------------------------------------------------------
# Attaching a document to a part by hand
# ---------------------------------------------------------------------------

#: Where a hand-written attachment lives. Beside the board, like every other
#: manifest, and readable — it is a statement somebody made and they are
#: entitled to see it, correct it and delete it.
ATTACH_SUFFIX = ".datasheets.yaml"

_ATTACH_MARKER = "# maintained by pcbreview"


def attach_path(board_path: Path | str) -> Path:
    board = Path(board_path)
    return board.with_name(board.stem + ATTACH_SUFFIX)


def attachments(board_path: Path | str) -> dict:
    """`{pdf filename or LCSC code: designator}`, or empty.

    **Why this exists at all.** A datasheet reaches a part through the LCSC
    code in its filename, and that covers the files LCSC itself serves. It does
    not cover `TPS62840_rev_C.pdf`, and reading a part number out of the
    document *body* is the guess this module refuses to make — a number in a
    PDF may be a cross-reference to a competitor's device.

    A person looking at the schematic and the document knows the answer in
    seconds. This is where they write it down so the next run has it too.

    Never raises: an unreadable sidecar becomes empty with the reason in
    `attachment_error`, because a review must not stop over an optional file.
    """
    return _attachments(board_path)[0]


def attachment_error(board_path: Path | str) -> str | None:
    return _attachments(board_path)[1]


def _attachments(board_path) -> tuple[dict, str | None]:
    import yaml                                               # noqa: PLC0415

    path = attach_path(board_path)
    if not path.exists():
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {}, f"{path.name}: {exc}"
    if not isinstance(data, dict):
        return {}, f"{path.name} is not a mapping"
    attach = data.get("attach") or {}
    if not isinstance(attach, dict):
        return {}, f"{path.name}: `attach` must be a mapping"
    return {str(k): str(v) for k, v in attach.items()}, None


def attach(board_path: Path | str, pairs: dict) -> dict:
    """Record `{pdf or LCSC: designator}`, merged over whatever is there.

    Written whole and moved into place, like every other writer here, so a
    crash midway cannot leave a half-written file that the next run reads as
    somebody's statement.
    """
    import yaml                                               # noqa: PLC0415

    existing, error = _attachments(board_path)
    if error:
        raise PartsError(
            f"the attachment file beside this board cannot be read, so it "
            f"will not be overwritten: {error}")
    merged = {**existing, **{str(k): str(v) for k, v in pairs.items()}}

    target = attach_path(board_path)
    body = yaml.safe_dump({"attach": merged}, sort_keys=True,
                          allow_unicode=True)
    document = (
        f"{_ATTACH_MARKER}.\n"
        f"#\n"
        f"# Which datasheet belongs to which designator, where the filename\n"
        f"# does not say. The key is a PDF filename or an LCSC code; the value\n"
        f"# is a designator on this board. Delete a line to undo it.\n"
        f"#\n"
        f"# Nothing here is inferred from a document's contents. A part number\n"
        f"# inside a PDF may be a cross-reference to somebody else's device.\n"
        f"\n{body}")
    scratch = target.with_suffix(target.suffix + ".partial")
    scratch.write_text(document, encoding="utf-8")
    scratch.replace(target)
    return {"path": str(target), "attach": merged}


# ---------------------------------------------------------------------------
# Writing the manifest
# ---------------------------------------------------------------------------

def destination(board_path: Path | str) -> Path:
    board = Path(board_path)
    return board.with_name(board.stem + SUFFIX)


def document(rows, ambient_c: float = 25.0,
             dissipation: dict | None = None) -> str:
    """The manifest text for every row that can be written.

    Rendered rather than round-tripped, exactly as `netclass.propose` does and
    for the reason §30.6 rule 4 records: a second YAML writer whose output the
    engine's loader would have to be trusted to accept is worse than a
    generated file plus a one-time backup of anything hand-written.

    A part with no known dissipation is written with
    `dissipation_stated: false`, which is the loader's own third state and
    makes the check report *how much this part can dissipate* instead of
    inventing a load it does not know.
    """
    dissipation = dissipation or {}
    lines = [
        "# Written by pcbreview from the datasheets in this project.",
        "#",
        "# Every number below carries the document and page it was read from,",
        "# in `source`. Check any of them against the PDF; that is what the",
        "# field is for.",
        "#",
        "# `dissipation_stated: false` means nobody has said what this part is",
        "# actually doing. It is not a guess at zero — the check reports how",
        "# much the part CAN dissipate before it reaches its rating. Replace it",
        "# with `dissipation_w:` once you know the load.",
        "",
        f"ambient_c: {float(ambient_c)}",
        "",
        "parts:",
    ]
    written = 0
    for row in rows:
        if not row.ready:
            continue
        written += 1
        watts = dissipation.get(row.reference, row.dissipation_w)
        lines.append(f"  {row.reference}:")
        if row.mpn:
            lines.append(f"    part: {row.mpn}")
        lines.append(f"    theta_ja_c_per_w: {row.theta_ja_c_per_w}")
        if row.theta_jc_c_per_w is not None:
            lines.append(f"    theta_jc_c_per_w: {row.theta_jc_c_per_w}")
        lines.append(f"    t_junction_max_c: {row.t_junction_max_c}")
        if watts is None:
            lines.append("    dissipation_stated: false")
        else:
            lines.append(f"    dissipation_w: {float(watts)}")
        lines.append(f"    source: {_source(row)!r}")
        lines.append("")
    if not written:
        raise PartsError(
            "no part in this project has enough read from its datasheet to be "
            "written into a manifest. The table says what each one is missing.")
    return "\n".join(lines).rstrip() + "\n"


def _source(row: Row) -> str:
    """`thermal.py`'s required `source`, built from the cites rather than prose.

    It names the document, the page, and **which stage read it** — a value a
    model proposed and a value a regex matched against a checked line are not
    equally strong, and a person auditing the file is entitled to know which
    they are looking at.
    """
    parts = [row.document or "datasheet"]
    if row.mpn:
        parts.insert(0, row.mpn)
    for name, label in (("theta_ja_c_per_w", "theta_JA"),
                        ("theta_jc_c_per_w", "theta_JC"),
                        ("t_junction_max_c", "Tj(max)")):
        cite = row.cites.get(name)
        if not cite:
            continue
        value = getattr(row, name)
        parts.append(f"{label} {value} p.{cite.get('page')} "
                     f"[{cite.get('source')}]")
    return " — ".join([parts[0]] + parts[1:]) if len(parts) > 1 else parts[0]


def write(board_path: Path | str, rows, ambient_c: float = 25.0,
          dissipation: dict | None = None, overwrite: bool = False) -> dict:
    """Write `<board>.thermal.yaml`, validated by the engine before it lands."""
    target = destination(board_path)
    text = document(rows, ambient_c=ambient_c, dissipation=dissipation)

    replaced = None
    if target.exists():
        if not overwrite:
            raise PartsError(
                f"{target.name} already exists. It may be hand-written and "
                f"this writer does not keep comments, so replacing it has to "
                f"be asked for.")
        try:
            thermal_module.load(target)
        except thermal_module.ThermalError as exc:
            raise PartsError(
                f"{target.name} is on disk and does not load ({exc}). That is "
                f"somebody's work in progress and this will not write over "
                f"it — fix or move it first.") from None
        replaced = _back_up(target)

    scratch = target.with_suffix(target.suffix + ".partial")
    scratch.write_text(text, encoding="utf-8")
    try:
        loaded = thermal_module.load(scratch)
    except thermal_module.ThermalError as exc:
        scratch.unlink(missing_ok=True)
        raise PartsError(
            f"the manifest this produced does not load, so it was not "
            f"written: {exc}") from None
    scratch.replace(target)
    return {"path": str(target),
            "replaced": str(replaced) if replaced else None,
            "parts": sorted(loaded.parts),
            "written": len(loaded.parts)}


def _back_up(path: Path) -> Path:
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.replaced-{index}")
        if not candidate.exists():
            candidate.write_bytes(path.read_bytes())
            return candidate
    raise PartsError("999 replaced manifests already beside this board")


# ---------------------------------------------------------------------------
# The command line — the datasheets, read
# ---------------------------------------------------------------------------

USAGE = """\
usage: python -m pcbreview.parts <project folder | board.kicad_pcb> [options]

One row per part that dissipates: what it is, what its datasheet states, the
page each number came from, and — for the ones that cannot be written yet —
exactly what is missing and why.

Two deterministic stages fill what they can:

  1. the LCSC catalogue     resolves C1234567.pdf to a part, class and package
  2. the datasheet's tables `pdfplumber` plus patterns on RthJA / theta-JA and
                            deg C/W, each value carrying its page

**The third reader is you.** Whatever those two miss is listed per row, and
you fill it with --read. There is no local model any more: you have the
document and the schematic, and the audits below do not care who proposed a
value.

  --pages PDF     print that document's text, page by page, so you need no PDF
                  tooling of your own. ZERO CHARACTERS MEANS IT IS A SCAN —
                  open the PDF and read the image yourself, then --read with
                  --vision
  --read SPEC     offer a value. Repeatable. SPEC is
                    <LCSC or pdf name>:<field>=<value>,page=<n>,quote=<line>
                  field is theta_ja | theta_jc | tj_max. Quote the line EXACTLY
                  as --pages printed it; it is checked against that page
  --vision        the values in this --read were read off an image, not text.
                  Marks them `vision` — one audit weaker, and recorded as such
  --attach A=B    say which part a datasheet belongs to when its filename does
                  not. A is a PDF name or LCSC code, B a designator. Written to
                  <board>.datasheets.yaml and remembered
  --write         write <board>.thermal.yaml from the rows that are complete
  --replace       allow --write over a manifest that is already there
  --ambient C     ambient temperature for the manifest (default 25)
  --watts REF=W   what a part actually dissipates. Repeatable

Every value from any source must cite a page and quote the line it came from,
and the quote is checked against that page. Readings are cached by the PDF's
content hash, so a document is read once ever.

Without --watts a part is written `dissipation_stated: false`, the loader's own
third state: the check then reports how much the part CAN dissipate before it
reaches its rating, rather than reading a missing load as nought watts.
`dissipation_w` is an operating-point calculation and is in no datasheet.

PCBREVIEW_LCSC_CATALOGUE moves or disables stage 1.
"""


def _render(rows) -> str:
    """The table, and for every blocked row the reason in the user's terms."""
    ready = [r for r in rows if r.ready]
    out = ["", "=" * 74,
           f"PARTS — {len(rows)} row(s), {len(ready)} with enough read to be "
           f"written", "=" * 74, ""]
    for row in rows:
        head = f"  {row.reference:14s} {row.value[:18]:18s}"
        identity = " ".join(x for x in (row.mpn, row.package) if x)
        out.append(f"{head} {row.lcsc or '(no LCSC code)':12s} {identity}")
        if row.because:
            out.append(f"                 in this table because {row.because}")
        if row.document:
            out.append(f"                 from {row.document}")
        for name, label in (("theta_ja_c_per_w", "theta_JA"),
                            ("theta_jc_c_per_w", "theta_JC"),
                            ("t_junction_max_c", "Tj max  ")):
            value = getattr(row, name)
            if value is None:
                continue
            cite = row.cites.get(name) or {}
            out.append(f"                 {label} {value:>8}   "
                       f"p.{cite.get('page', '?')} [{cite.get('source', '?')}]")
        if row.dissipation_w is not None:
            out.append(f"                 dissipation {row.dissipation_w} W "
                       f"(from your manifest)")
        for note in row.notes:
            out.append(f"                 note: {note}")
        for refusal in row.refused:
            # A value that was offered and refused. Shown, because a field that
            # stayed empty and a field that was refused need different work.
            out.append(f"                 REFUSED {refusal['field']}="
                       f"{refusal['value']} (p.{refusal.get('page')}): "
                       f"{refusal['why']}")
        for reason in row.blocked:
            out.append(f"                 BLOCKED: {reason}")
        out.append("")
    if not ready:
        out.append("  Nothing can be written yet. Each row says what it needs.")
    return "\n".join(out)


def _wanted(rows) -> str:
    """What is still open, as the exact commands that would close it.

    The table says what is missing; this says what to type. They are separate
    because the table is a record and this is a worklist — and a worklist that
    a reader has to translate into commands is where the round trips go.
    """
    lines: list[str] = []
    for row in rows:
        if not row.junction or not row.lcsc:
            continue
        gaps = [name for name, label in (("theta_ja_c_per_w", "theta_ja"),
                                         ("theta_jc_c_per_w", "theta_jc"),
                                         ("t_junction_max_c", "tj_max"))
                if getattr(row, name) is None]
        if not gaps or not row.document:
            continue
        short = {"theta_ja_c_per_w": "theta_ja",
                 "theta_jc_c_per_w": "theta_jc",
                 "t_junction_max_c": "tj_max"}
        lines.append(f"  {row.reference} ({row.document}) still needs "
                     f"{', '.join(short[g] for g in gaps)}:")
        lines.append(f"    python -m pcbreview.parts <project> --pages "
                     f"{row.document}")
        lines.append(f"    python -m pcbreview.parts <project> --read "
                     f"\"{row.lcsc}:{short[gaps[0]]}=<value>,page=<n>,"
                     f"quote=<the exact line>\"")
    if not lines:
        return ""
    return "\n".join(["", "STILL TO READ — you are the reader", ""] + lines)


def parse_read(spec: str) -> tuple[str, dict]:
    """`C123:theta_ja=36,page=1,quote=RthJA … 36 C/W` -> (document, proposal).

    The quote is taken off the end whole, because a datasheet line contains
    commas and equals signs and everything else, and a parser that split on
    them would mangle exactly the field that has to survive verbatim.
    """
    from pcbreview import reader as reader_module               # noqa: PLC0415

    if ":" not in spec:
        raise PartsError(
            f"{spec!r}: a --read is `<LCSC or pdf name>:<field>=<value>,"
            f"page=<n>,quote=<line>`")
    document, remainder = spec.split(":", 1)
    document = document.strip()

    quote = None
    if ",quote=" in remainder:
        remainder, quote = remainder.split(",quote=", 1)
    elif remainder.startswith("quote="):
        raise PartsError(f"{spec!r}: a quote alone is not a value")
    if quote is None:
        raise PartsError(
            f"{spec!r}: no quote. A value without the line it was read from is "
            f"not accepted from any source — that is what makes it checkable")

    fields: dict[str, str] = {}
    for piece in remainder.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise PartsError(f"{spec!r}: {piece!r} is not `key=value`")
        key, value = piece.split("=", 1)
        fields[key.strip().lower()] = value.strip()

    page = fields.pop("page", None)
    if page is None:
        raise PartsError(f"{spec!r}: no page. A cite is required")
    try:
        page = int(page)
    except ValueError:
        raise PartsError(f"{spec!r}: page={page!r} is not a number") from None

    if len(fields) != 1:
        raise PartsError(
            f"{spec!r}: name exactly one field per --read. Known: "
            f"{', '.join(sorted(set(reader_module.ALIASES)))}")
    name, raw = next(iter(fields.items()))
    canonical = reader_module.ALIASES.get(name)
    if canonical is None:
        raise PartsError(
            f"{spec!r}: unknown field {name!r}. Known: "
            f"{', '.join(sorted(set(reader_module.ALIASES)))}")
    try:
        value = float(raw)
    except ValueError:
        raise PartsError(f"{spec!r}: {name}={raw!r} is not a number") from None

    return document, {canonical: {"value": value, "page": page,
                                  "quote": quote}}


def _document_for(readings, key: str):
    """The reading a `--read` or `--attach` names, by LCSC code or filename."""
    key = key.strip()
    for reading in readings:
        if reading.identity and reading.identity.lcsc \
                and reading.identity.lcsc.upper() == key.upper():
            return reading
    for reading in readings:
        if reading.path and Path(reading.path).name.lower() == key.lower():
            return reading
    return None


def main(argv: list[str]) -> int:
    import sys

    from pcbreview import console_utf8

    console_utf8()

    if "--help" in argv or "-h" in argv or len(argv) < 2:
        sys.stderr.write(USAGE)
        return 0 if ("--help" in argv or "-h" in argv) else 2

    positional: list[str] = []
    watts: dict[str, float] = {}
    reads: list[str] = []
    attaches: dict[str, str] = {}
    pages_of: str | None = None
    ambient = 25.0
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in ("--watts", "--attach"):
            if index + 1 >= len(argv) or "=" not in argv[index + 1]:
                sys.stderr.write(f"{argument} needs A=B\n")
                return 2
            left, right = argv[index + 1].split("=", 1)
            if argument == "--watts":
                try:
                    watts[left.strip()] = float(right)
                except ValueError:
                    sys.stderr.write(f"--watts {argv[index + 1]}: "
                                     f"{right!r} is not a number\n")
                    return 2
            else:
                attaches[left.strip()] = right.strip()
            index += 2
            continue
        if argument in ("--read", "--pages", "--ambient"):
            if index + 1 >= len(argv):
                sys.stderr.write(f"{argument} needs a value\n")
                return 2
            value = argv[index + 1]
            if argument == "--read":
                reads.append(value)
            elif argument == "--pages":
                pages_of = value
            else:
                try:
                    ambient = float(value)
                except ValueError:
                    sys.stderr.write(f"--ambient {value!r} is not a number\n")
                    return 2
            index += 2
            continue
        if not argument.startswith("--"):
            positional.append(argument)
        index += 1

    if not positional:
        sys.stderr.write(USAGE)
        return 2

    from pcbreview import datasheets as datasheets_module
    from pcbreview import pipeline
    from pcbreview import reader as reader_module
    from pcbreview import schematic as schematic_module

    try:
        project = pipeline.discover(positional[0])
    except pipeline.PipelineError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    readings = datasheets_module.ingest(project.board)

    # --pages first, and without needing a schematic: dumping a document's text
    # is a question about the document, and refusing it because the project has
    # no schematic would be refusing the one thing that always works.
    if pages_of is not None:
        found = _document_for(readings, pages_of)
        path = Path(found.path) if found is not None else Path(pages_of)
        if not path.exists():
            sys.stderr.write(
                f"{pages_of}: no such document in this project. Known: "
                + ", ".join(sorted(Path(r.path).name for r in readings
                                   if r.path)) + "\n")
            return 2
        text = reader_module.page_text(path)
        if not text:
            sys.stderr.write(
                f"{path.name}: could not be opened. `pip install pdfplumber` "
                f"is what reads a PDF here.\n")
            return 2
        print(f"{path.name} — {len(text)} page(s)")
        for number, body in enumerate(text, 1):
            print(f"\n--- page {number} ---")
            print(body or "(no text on this page)")
        if reader_module.is_scanned(text):
            print(f"\nZERO CHARACTERS ON EVERY PAGE. This is a scan, and no "
                  f"pattern will ever read it.\nOpen {path} yourself, read the "
                  f"thermal table off the image, and offer it with\n"
                  f"`--read … --vision`. A vision reading cannot have its "
                  f"quote checked against\nthe page, so it is marked `vision` "
                  f"and is one audit weaker. That is recorded,\nnot hidden.")
        return 0

    if attaches:
        try:
            outcome = attach(project.board, attaches)
        except PartsError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        print(f"wrote {outcome['path']}")
        for key, reference in sorted(attaches.items()):
            found = _document_for(readings, key)
            state = "" if found is not None else \
                "  — WARNING: no document in this project is called that"
            print(f"  {key} -> {reference}{state}")
        return 0

    if reads:
        wrote = 0
        for spec in reads:
            try:
                key, proposal = parse_read(spec)
            except PartsError as exc:
                sys.stderr.write(f"{exc}\n")
                return 2
            found = _document_for(readings, key)
            if found is None:
                sys.stderr.write(
                    f"{key}: no such document in this project. Known: "
                    + ", ".join(sorted(Path(r.path).name for r in readings
                                       if r.path)) + "\n")
                return 2
            before = dict(found.cites)
            reader_module.accept(
                found, proposal, found.path, list(proposal),
                source=reader_module.VISION if "--vision" in argv else None)
            datasheets_module.remember(found)
            for name in proposal:
                cite = found.cites.get(name)
                if cite and name not in before:
                    wrote += 1
                    print(f"accepted {name}={getattr(found, name)} from "
                          f"{Path(found.path).name} p.{cite['page']} "
                          f"[{cite['source']}]")
                else:
                    refusal = next((r for r in reversed(found.refused)
                                    if r["field"] == name), None)
                    sys.stderr.write(
                        f"REFUSED {name}: "
                        f"{refusal['why'] if refusal else 'already read'}\n")
        if not wrote:
            return 2

    if project.schematic is None:
        sys.stderr.write(
            f"{project.board.name}: no schematic, so there is no designator to "
            f"attach a datasheet to. A board file carries footprints, not the "
            f"LCSC codes that link a part to its document.\n")
        return 2

    netlist = schematic_module.netlist(project.schematic)

    manifest = None
    if project.thermal:
        from pcbreview import thermal as thermal_module

        try:
            manifest = thermal_module.load(project.thermal)
        except thermal_module.ThermalError as exc:
            print(f"note: {Path(project.thermal).name} is on disk and does not "
                  f"load ({exc}); the rows below ignore it")

    error = attachment_error(project.board)
    if error:
        print(f"note: {error} — no attachment was applied")
    rows = assemble(netlist, readings, manifest,
                    attached=attachments(project.board))
    print(_render(rows))
    print(_wanted(rows))

    if "--write" not in argv:
        ready = sum(1 for r in rows if r.ready)
        if ready:
            print(f"  --write turns the {ready} complete row(s) into "
                  f"{destination(project.board).name}")
        return 0

    try:
        outcome = write(project.board, rows, ambient_c=ambient,
                        dissipation=watts, overwrite="--replace" in argv)
    except PartsError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    print(f"wrote {outcome['path']} — {outcome['written']} part(s): "
          f"{', '.join(outcome['parts'])}")
    if outcome["replaced"]:
        print(f"  the manifest that was there is now "
              f"{Path(outcome['replaced']).name}")
    print("  a review already on screen no longer describes what is on disk; "
          "the manifest is an INPUT to the audit, so re-run "
          "`python -m pcbreview.run`")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
