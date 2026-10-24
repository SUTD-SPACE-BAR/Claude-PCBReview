"""Rules 3 and 3b: absence, and absence wearing a number.

> `impedance … (1 fab target(s), 0 net(s) examined)` is a blind check, not a
> clean one. — SKILL.md:399

Rule 3 reads `CheckResult`. Rule 3b reads the coverage dicts the checks have
already put into their own findings' `measurement`. Neither recomputes: the report
generator is a pure function of two `AuditReport`s (§15.5), so it has no board
to call `placement.coverage()` on, and would be lying about a different run if
it did.

Why rule 3 parses a note
------------------------
`CheckResult.note` is free text. That is not ideal and it is also the only place
several coverage numbers exist — `audit.run()` takes a *callable* note precisely
so the number that makes a zero readable can be evaluated after the check ran.
So this module reads the note, quotes the clause it read verbatim into the
finding it emits, and keeps the whole note beside it. A reader can always check
this module's reading against the engine's own words, which is the property that
matters when the alternative is silence.
"""

from __future__ import annotations

import re

from pcbreview.report.model import LAYOUT, CoverageGap, MeasuredGap

# ---------------------------------------------------------------------------
# Rule 3 — a check that had nothing to look at
# ---------------------------------------------------------------------------

#: Notes are written as clauses. `impedance` says "1 fab target(s) …, 0 net(s)
#: examined" and `placement` says "0 decoupling loop(s), 0 feedback pin(s), …"
#: — each clause names one population, so each is judged on its own.
_CLAUSES = re.compile(r"\s*[;,:]\s*")

#: "0 net(s) examined" → ("0", "net(s) examined"). Anchored, because a number
#: in the middle of a clause is a parameter ("stitch limit 14.3 mm at 1000 ps"),
#: not a population count.
_LEADING_COUNT = re.compile(r"^(\d+)\s+(\S.*)$")

#: Nouns that count *results*, not population. A DRC that examined the whole
#: board and found nothing reports "0 violation(s): 0 error, 0 warning" — a
#: genuine pass, and reporting it as a blind check would be the same failure
#: this module exists to prevent, with the sign reversed.
_RESULT_NOUNS = frozenset({
    "violation", "violations", "finding", "findings",
    "error", "errors", "warning", "warnings",
    "critical", "major", "minor", "info",
})

#: A note that names a missing *input* rather than an empty population:
#: "no thermal data supplied, so no part is known to dissipate",
#: "no BOM supplied or found", "KiCad's DRC could not be run — rules were not
#: checked". Judged on the whole note, not per clause, so one absent input
#: produces one statement.
_NO_INPUT_PREFIX = re.compile(r"^\s*(no|none)\b", re.IGNORECASE)
_COULD_NOT_RUN = re.compile(r"could not (?:be )?run", re.IGNORECASE)


def _population_noun(clause: str) -> str:
    """The first word after the count, normalised. `"net(s) examined"` → `net`."""
    word = clause.split(maxsplit=1)[0] if clause.split() else ""
    return word.lower().rstrip(".,;:").removesuffix("(s)").rstrip("s") or word


#: A zero that is *not* at the start of a clause — "examined 0 of 14 nets",
#: "0/14 measured". `_LEADING_COUNT` is anchored on purpose, so a phrasing like
#: that is invisible to it. Matching this and finding nothing to report is the
#: signal that the parser has met a note it does not understand.
#:
#: Excludes a decimal (`0.5 mm`) and a range (`0-14`), which are parameters.
_STRAY_ZERO = re.compile(r"(?<![\d.])0\s+(?:of\s+)?(?=[A-Za-z])")


def _zero_populations(note: str) -> tuple[list[str], list[str]]:
    """`(empty populations, clauses carrying a zero this could not classify)`.

    The second half exists because **this module reads free text, and free text
    changes.** Every statement in section 3 is found by matching the engine's
    own wording; a check whose note is reworded to "examined 0 of 14 nets" —
    which `_LEADING_COUNT` cannot see, because it is anchored so that a number
    mid-clause reads as a parameter — would simply stop appearing. Section 3
    would shrink and look better, which is precisely the failure it exists to
    prevent, with nothing anywhere saying so.

    So a clause that carries an unclaimed zero is reported as **unread** rather
    than skipped. It is a weaker statement than "this check was blind" and it
    is an honest one: *there is a zero here and this parser could not tell you
    what it counts.*
    """
    empty: list[str] = []
    unread: list[str] = []
    for clause in _CLAUSES.split(note):
        clause = clause.strip()
        if not clause:
            continue
        match = _LEADING_COUNT.match(clause)
        if match is not None and match.group(1) == "0":
            noun = _population_noun(match.group(2))
            # `_RESULT_NOUNS` holds singular and plural; `_population_noun`
            # normalises toward singular but leaves irregulars alone.
            if noun in _RESULT_NOUNS or f"{noun}s" in _RESULT_NOUNS:
                continue
            empty.append(clause)
            continue
        if match is not None:
            # A population that is not empty. Understood, and not a gap.
            continue
        if _STRAY_ZERO.search(clause):
            unread.append(clause)
    return empty, unread


def coverage_gaps(report, source: str = LAYOUT) -> list[CoverageGap]:
    """Every check in `report` that could not run, or ran over nothing.

    Three shapes, kept apart because they are different statements to a reader:

    * `ran=False` — the check did not execute. `skipped_because` says why, and
      it can be a caller's choice, a missing schematic, or an exception.
    * a note naming a missing input — "no thermal data supplied".
    * a note whose population count is zero — "0 net(s) examined";
    * a note carrying a zero this parser could not classify, reported as
      **unread** rather than dropped. See `_zero_populations`.

    A check may produce all of these at once and each is emitted, because
    "there was no schematic" and "there were no crystals" are not the same
    sentence.
    """
    gaps: list[CoverageGap] = []
    for check in report.checks:
        if not check.ran:
            gaps.append(CoverageGap(
                source=source, check=check.name, reason="skipped",
                statement=check.skipped_because or "no reason recorded",
                note=check.skipped_because))
            continue
        note = check.note
        if not note:
            continue
        if _NO_INPUT_PREFIX.match(note) or _COULD_NOT_RUN.search(note):
            gaps.append(CoverageGap(
                source=source, check=check.name, reason="no_input",
                statement=note.strip(), note=note,
                findings_anyway=check.findings))
        empty, unread = _zero_populations(note)
        for clause in empty:
            gaps.append(CoverageGap(
                source=source, check=check.name, reason="zero_population",
                statement=clause, note=note,
                findings_anyway=check.findings))
        for clause in unread:
            gaps.append(CoverageGap(
                source=source, check=check.name, reason="unread",
                statement=(f"{clause} — this reader could not tell what that "
                           f"zero counts. Read the check's own note before "
                           f"treating it as a pass"),
                note=note, findings_anyway=check.findings))
    return gaps


# ---------------------------------------------------------------------------
# Rule 3b — found, and then not measured
# ---------------------------------------------------------------------------

#: `(check, found key, measured key, noun, what "measured" would have meant)`.
#:
#: Read out of `placement.coverage()` and `current.coverage()` rather than
#: guessed. `audit.check_placement` already emits an `info` for four of these;
#: it does **not** emit one for `decoupling_loops`, which is the check the whole
#: module exists for, and every one of them is `info` and so invisible to rule 1.
#: Surfacing them is this rule's entire job.
#:
#: `esd_lines_measured` is deliberately **not** a row. It counts *lines* where
#: `esd_clamps` counts *parts*, and `placement.py:1742` says so outright — a
#: multi-line array is one clamp and several lines, so the comparison could
#: report more measured than found. It is carried as detail on the
#: `esd_clamps` row instead, where it is informative and not nonsense.
MEASURED_PAIRS: tuple[tuple[str, str, str, str, str], ...] = (
    ("placement", "crystals", "crystal_loops_measured", "crystal(s)",
     "their oscillator loop was measured"),
    ("placement", "decoupling_loops", "decoupling_loops_measured",
     "decoupling loop(s)",
     "the loop area was actually compared against a threshold"),
    ("placement", "esd_clamps", "esd_clamps_measured", "ESD clamp(s)",
     "their position relative to a connector was measured"),
    ("placement", "feedback_pins", "feedback_pins_measured", "feedback pin(s)",
     "the divider distance or a switch-node run was measured"),
    ("placement", "sense_pins", "sense_pins_measured", "current-sense pin(s)",
     "their tap offset was compared against a shunt"),
)

#: Two checks state their version of the same thing under keys that are not a
#: found/measured pair, and each gets its own rule below.
#:
#: `current_capacity` — "N of M power net(s) with enough terminals were actually
#: compared". `nets_examined` is a *list*, `nets_compared` an int.
_CURRENT = ("current_capacity", "nets_examined", "nets_compared")
#: `thermal` — `parts_with_data` counts the manifest, which may name parts that
#: are not candidates, so it is **not** the measured half of
#: `dissipative_candidates`. `candidates_without_data` is, by subtraction.
_THERMAL = ("thermal", "dissipative_candidates", "candidates_without_data")


def _coverage_dicts(report) -> dict[str, dict]:
    """Merge every coverage dict a check put in a finding, keyed by check.

    Two shapes are carried through the engine and both are read here: a bare
    `measurement={found: n, measured: m}` (the placement pair findings) and a
    nested `measurement["coverage"] = {...}` (the census findings).

    **`measurement["coverage"]` is not always a dict.** `return_path` writes a
    float there — the fraction of a trace with an intact return beneath it —
    and taking it for a coverage census raises or, worse, silently reads
    `0.0` as an empty population.
    """
    merged: dict[str, dict] = {}
    for finding in report.findings:
        measurement = finding.measurement or {}
        if not isinstance(measurement, dict):
            continue
        into = merged.setdefault(finding.check, {})
        nested = measurement.get("coverage")
        if isinstance(nested, dict):
            into.update(nested)
        into.update({k: v for k, v in measurement.items()
                     if k != "coverage" and isinstance(v, (int, float, list,
                                                           dict, str))})
    return merged


def _count(value) -> int | None:
    """A population size from whatever the coverage dict held there."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return None


def measured_gaps(report, source: str = LAYOUT) -> list[MeasuredGap]:
    """Every found/measured pair in `report` that disagrees."""
    coverage = _coverage_dicts(report)
    gaps: list[MeasuredGap] = []

    for check, found_key, measured_key, noun, means in MEASURED_PAIRS:
        data = coverage.get(check)
        if not data:
            continue
        found = _count(data.get(found_key))
        measured = _count(data.get(measured_key))
        if found is None or measured is None or not found or measured >= found:
            continue
        detail = [
            f"`{found_key}` counts candidates; `{measured_key}` counts the "
            f"ones actually measured",
            "a non-zero coverage count is not evidence a check ran",
        ]
        if check == "placement" and found_key == "esd_clamps":
            lines = _count(data.get("esd_lines_measured"))
            if lines is not None:
                detail.append(
                    f"`esd_lines_measured` = {lines}; it counts lines rather "
                    f"than parts, so it is reported beside this and not "
                    f"compared against it")
        gaps.append(MeasuredGap(
            source=source, check=check, found_key=found_key,
            measured_key=measured_key, found=found, measured=measured,
            noun=noun, means=means, detail=tuple(detail)))

    check, found_key, measured_key = _CURRENT
    data = coverage.get(check) or {}
    found = _count(data.get(found_key))
    measured = _count(data.get(measured_key))
    if found and measured is not None and measured < found:
        declined = data.get("nets_declined")
        detail = [
            f"`{found_key}` are the nets with enough terminals to compare; "
            f"`{measured_key}` is how many produced a comparison",
        ]
        if isinstance(declined, dict) and declined:
            detail += [f"{net} — {why}" for net, why in sorted(declined.items())]
        gaps.append(MeasuredGap(
            source=source, check=check, found_key=found_key,
            measured_key=measured_key, found=found, measured=measured,
            noun="power net(s) with enough terminals",
            means="a width comparison across their feeds was produced",
            detail=tuple(detail)))

    check, found_key, without_key = _THERMAL
    data = coverage.get(check) or {}
    found = _count(data.get(found_key))
    without = _count(data.get(without_key))
    if found and without:
        gaps.append(MeasuredGap(
            source=source, check=check, found_key=found_key,
            measured_key=without_key, found=found, measured=found - without,
            noun="dissipative part(s)",
            means="a datasheet supplied the thermal resistance a junction "
                  "temperature is computed from",
            detail=(
                f"`{found_key}` counts parts the schematic says can dissipate; "
                f"`{without_key}` counts those with no entry in the thermal "
                f"manifest",
                "no junction temperature was computed for those parts — that "
                "is a missing datasheet, not a cool part")))

    return gaps
