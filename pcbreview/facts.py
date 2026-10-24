"""The Facts tab — the manifest conversation, as two tables and a queue.

the design brief §2.5 calls this *"where the biggest AI-removal win lives"*, and that
is the test to hold it to: **this file exists to kill the manifest
conversation.** Telling the engine that a net carries 2 A used to mean writing
`<board>.netintent.yaml` by hand or having a model write it. Claude's job 5
(HANDOFF §1) stops being a job here.

What this is NOT
----------------
**No inference lives here.** Every class, every piece of evidence and the
shortlist itself come from `netclass.py`, which owns them; this assembles them
into rows. §29.6 states the test plainly — *if the tab computes a net's class
itself, it is wrong* — and the reason is §9: the moment selection or
classification moves into the presentation layer, the reproducibility that
SKILL.md §382-420 was written to fix goes with it.

**The question queue is not a model, and this is the place it would be easiest
to put one.** Each question is a finding's own `intent_needed` string with the
finding attached. The engine wrote it, and it is specific because the engine
had the measurement:

    /PYRO_CH1_DRAIN is 0.600 mm throughout, carrying 1.65 A continuously at a
    10 °C rise. If these fire briefly, how long, and what rise is acceptable?

Nothing here generates a word of that. A model asked to produce it would have
to invent the 0.600 mm.

Two files, and only one of them is the user's
--------------------------------------------
The engine already distinguishes `<board>.netintent.yaml` — the user's own word,
auto-discovered and applied — from `review/proposed.netintent.yaml`, which is a
machine inference. `audit.py`'s own comment says renaming one over the other
*"replaces the user's own judgement — the one thing"* that must not happen.

**This module writes the former and never the latter**, and it never promotes
the proposal. `write_intent` is the only writer in this file.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pcbreview import netclass as netclass_module              # noqa: E402

from pcbreview.report.generate import finding_ref                 # noqa: E402
from pcbreview.report.model import LAYOUT, SCHEMATIC, SEVERITY_ORDER  # noqa: E402

#: Marker written into the header of any manifest this module has produced.
#: Its only job is to tell a file we wrote from one the user hand-wrote, so the
#: first overwrite of a hand-written file can take a copy first — a YAML
#: round-trip drops comments, and the comments in a hand-written manifest are
#: where the reasoning lives.
_MARKER = "# maintained by pcbreview"

#: The classes `netclass.load_intent` accepts. Repeated here only to reject a
#: bad request early with a clear message; `load_intent` is still the authority
#: and every write is validated by it before it lands.
VALID_CLASSES = ("protocol", "power", "other")


class IntentError(Exception):
    """A proposed edit that must not be written. Carries the engine's reason."""


# ---------------------------------------------------------------------------
# Reading — the two tables
# ---------------------------------------------------------------------------

def _declaration_dict(declaration) -> dict:
    return {"match": declaration.match, "net_class": declaration.net_class,
            "kind": declaration.kind, "note": declaration.note,
            "current_a": declaration.current_a, "rise_c": declaration.rise_c}


def read_intent(path) -> tuple[list, str | None]:
    """The user's manifest as `Declaration`s, and why it could not be read.

    A manifest that will not parse must not take the Facts tab down with it:
    the tab is *where you fix that*, and a 500 in front of the fix is the one
    unhelpful outcome. So the reason comes back as a string and the caller
    shows it.
    """
    if not path:
        return [], None
    path = Path(path)
    if not path.exists():
        return [], None
    try:
        return list(netclass_module.load_intent(path).declarations), None
    except (netclass_module.NetIntentError, OSError) as exc:
        return [], str(exc)


def _governing(declarations, net: str):
    """The declaration that decides this net today, or `None`. First match wins.

    `fnmatch`, not equality: a manifest may say `/FIRE_CH*` and cover eight
    nets with one line. Editing one of those eight must not rewrite the line
    that covers the other seven, which is why `write_intent` inserts an exact
    match *above* a pattern rather than editing it.
    """
    for declaration in declarations:
        if fnmatch.fnmatchcase(net, declaration.match):
            return declaration
    return None


def _net_row(kind, dossier, declaration, findings, shortlisted: bool) -> dict:
    """One row of the Nets table.

    `kind` is `netclass.NetKind` — the engine's classification and the evidence
    for it. Nothing here second-guesses either.
    """
    measured = getattr(dossier, "ampacity", None) if dossier else None
    worst = min((f["severity"] for f in findings),
                key=lambda s: SEVERITY_ORDER.get(s, 9), default=None)
    return {
        "net": kind.net,
        "net_class": kind.net_class,
        "kind": kind.kind,
        # The engine's own sentence for why it thinks so. the design brief §2.5's
        # third column is this string and not a paraphrase of it.
        "evidence": kind.evidence,
        "declared": bool(kind.declared),
        "shortlisted": shortlisted,
        # What the user has said, if anything. `current_a` is the one fact only
        # the designer can supply, and `None` is a different thing from zero.
        "current_a": kind.current_a,
        "rise_c": kind.rise_c,
        "note": declaration.note if declaration else None,
        # The pattern that governs this net today, **as the manifest stands on
        # disk**. Equal to the net name when the user declared it by name; a
        # glob when one line covers many nets; `None` when nothing has been
        # said about it.
        #
        # `declared` above comes from the *run*, so between an edit and the
        # re-run this row honestly reads "you have said this, and the review
        # below has not measured it yet" — which is what the staleness banner
        # is there to explain. Making them agree by reading both from the same
        # place would mean either hiding the edit or claiming the review had
        # taken it into account.
        "match": declaration.match if declaration else None,
        "covered_by_pattern": bool(declaration
                                   and declaration.match != kind.net),
        # Measured, not declared — so a user can see what the copper does
        # before saying what it must carry.
        "carries_a": (round(measured.weakest_a, 2)
                      if measured is not None and measured.weakest_a is not None
                      else None),
        "measured_rise_c": (measured.rise_c if measured is not None else None),
        # The board-wide total from the engine's own `Ampacity`, not a sum of
        # `dossier.poured` — that dict is per layer, and adding it up here
        # would be a second way to compute a number the engine already has.
        "poured_mm2": (round(measured.poured_mm2, 1)
                       if measured is not None and measured.poured_mm2
                       else None),
        "terminals": len(dossier.pads) if dossier else 0,
        "findings": len(findings),
        "worst": worst,
    }


def _questions(pairs, declarations) -> list[dict]:
    """The queue, from `intent_needed` and nothing else.

    Every finding carries `basis`, `assumption` and `intent_needed` — the
    derivation, what was assumed, and **the datum that would settle its
    severity** (§3.2 rule 7). That last one is a question the engine already
    knows how to ask, so the queue is a filter over findings, not a generator.

    **One row per (check, question), with the nets unioned.** Keyed that way
    from measurement, not taste: on the harness board `return_path` asks the
    same sentence — *"this net's edge rate"* — about forty different nets, and
    keying on the nets as well produced a queue of 103 rows where a queue of
    about a dozen was the point. The findings all stay attached, every one of
    them, because the evidence is what makes the question answerable and
    dropping it would leave a question nobody can act on.

    A question naming **no** net is still listed and is marked
    `answerable: False`. `bom_parity` asking *"whether each was left off
    deliberately"* is a real thing the engine needs to know and there is
    nothing in a net-intent manifest that says it — so it is shown, because
    absence is never a pass (§2.10 principle 5), and it is not given an answer
    box that would silently do nothing.
    """
    queue: dict[tuple, dict] = {}
    for source_report, source in pairs:
        for finding in source_report.findings:
            ref = finding_ref(finding, source)
            if not ref.intent_needed:
                continue
            key = (ref.check, ref.intent_needed)
            entry = queue.get(key)
            if entry is None:
                entry = queue[key] = {
                    "id": f"q{len(queue)}",
                    "question": ref.intent_needed,
                    "check": ref.check,
                    "source": ref.source,
                    "nets": [],
                    "worst": ref.severity,
                    "findings": [],
                }
            for net in ref.nets:
                if net and net not in entry["nets"]:
                    entry["nets"].append(net)
            if (SEVERITY_ORDER.get(ref.severity, 9)
                    < SEVERITY_ORDER.get(entry["worst"], 9)):
                entry["worst"] = ref.severity
            entry["findings"].append({
                "source": ref.source, "check": ref.check,
                "severity": ref.severity, "title": ref.title,
                "score": round(ref.score, 2),
                "detail": list(ref.detail), "basis": ref.basis,
                "assumption": ref.assumption,
                "nets": list(ref.nets),
                "location": list(ref.location) if ref.location else None,
                # The engine never hides what it lowered, and neither does
                # this: a demoted finding shows the severity it had before.
                "demoted_from": ref.demoted_from,
                "demoted_by": ref.demoted_by,
            })
    out = sorted(queue.values(),
                 key=lambda q: (SEVERITY_ORDER.get(q["worst"], 9),
                                -len(q["findings"]), q["check"], q["question"]))
    for position, entry in enumerate(out):
        entry["id"] = f"q{position}"
        entry["nets"].sort()
        entry["findings"].sort(
            key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), -f["score"]))
        # Which nets the user could actually answer for. A net the manifest
        # already settles is answered, and re-asking it is what made one
        # session believe their file had been ignored.
        #
        # **From the manifest on disk, not from `classes`.** `NetKind.declared`
        # is what the *run* read, so a net declared since would keep being
        # asked about until the re-run — the user answering a question and
        # watching it stay open. `_governing` is the same first-match-wins
        # lookup the Nets table uses, so the two views agree.
        entry["undeclared_nets"] = [
            n for n in entry["nets"] if _governing(declarations, n) is None]
        # A declaration is the only answer this tab can write, so a question
        # naming no net has no answer here. Said, rather than implied by a box
        # that does nothing.
        entry["answerable"] = bool(entry["undeclared_nets"])
        if not entry["nets"]:
            entry["why_not_answerable"] = (
                "this question names no net, so a net-intent declaration "
                "cannot answer it — it is here because the engine asked it")
        elif not entry["undeclared_nets"]:
            entry["why_not_answerable"] = (
                "every net this names is already declared in your manifest")
    return out


def build(result, index) -> dict:
    """The whole tab. A pure function of a `ReviewResult` and its `BoardIndex`.

    Kept a pure function for the reason `problems.py` and `dossier.py` are: it
    is then testable over a real board with no server, and the four-board
    corpus is the only acceptance step this project has (§0A.2's box — the user
    cannot test any of this).
    """
    layout = result.layout
    classes = getattr(layout, "net_class_map", None)
    project = result.project

    # **From disk, not from `project.netintent`.** `result.project` is the
    # routing as it was when the review ran, so a manifest written since — the
    # normal case, because writing one is what this tab does — would not be in
    # it, and the user would watch their edit vanish. `default_intent_path` is
    # where the file lives whether or not any run has read it yet; whether a
    # run *has* is `manifest.in_use`, which is a different question and is
    # answered separately.
    board = getattr(project, "board", None)
    on_disk = netclass_module.default_intent_path(board) if board else None
    declarations, intent_error = read_intent(on_disk)

    pairs = [(layout, LAYOUT)]
    if result.schematic is not None:
        pairs.append((result.schematic, SCHEMATIC))

    if classes is None:
        # `net_class_map` is `None` only when classification could not run at
        # all. Saying so beats an empty table, which reads as "this board has
        # no nets" — principle 5 of §2.10 in the small.
        return {
            "available": False,
            "why": ("no net classification is available for this run, so there "
                    "is nothing to declare against. The audit's `net_classes` "
                    "summary is the place to look for why"),
            "nets": [], "shortlist": [], "questions": [],
            "manifest": _manifest_state(project, declarations, intent_error),
        }

    findings_by_net: dict[str, list] = {}
    for source_report, source in pairs:
        for finding in source_report.findings:
            for name in (getattr(finding, "nets", None) or ()):
                findings_by_net.setdefault(name, []).append(
                    {"severity": finding.severity, "check": finding.check})

    # **Layout findings only, and this is not an oversight.** `netclass.apply`
    # is called from `audit()` over the layout findings; `schematic_audit.py`
    # does not import `netclass` at all and takes no manifest. So a declaration
    # cannot demote a schematic finding, and a net named *only* by one is a net
    # where an answer changes nothing — precisely the row the shortlist exists
    # to keep out. It is also what `audit.propose_intent` means by `named`, so
    # the two agree by construction and not by coincidence.
    named = {net for finding in layout.findings
             for net in (getattr(finding, "nets", None) or ()) if net}
    # The one definition of the shortlist, from the engine (§29.3: *the
    # shortlist is the feature*).
    shortlisted = [k.net for k in
                   netclass_module.shortlist(classes, named_by_findings=named)]
    shortlisted_set = set(shortlisted)

    rows = []
    for kind in classes.by_net.values():
        rows.append(_net_row(
            kind, index.nets.get(kind.net) if index else None,
            _governing(declarations, kind.net),
            findings_by_net.get(kind.net, []),
            kind.net in shortlisted_set))
    # Shortlist first — the design brief §2.5 pins it to the top, and it is the only
    # part of a 229-row table anyone should have to read. Then by whether
    # anything was found on the net, then by name.
    rows.sort(key=lambda r: (not r["shortlisted"],
                             SEVERITY_ORDER.get(r["worst"], 9),
                             r["net"]))

    return {
        "available": True,
        "nets": rows,
        "shortlist": shortlisted,
        "unidentified": sum(1 for k in classes.by_net.values()
                            if k.net_class == "other" and not k.declared
                            and not netclass_module.is_placeholder(k.net)),
        "placeholders": sum(1 for k in classes.by_net.values()
                            if netclass_module.is_placeholder(k.net)),
        "questions": _questions(pairs, declarations),
        "manifest": _manifest_state(project, declarations, intent_error),
        "census": classes.census(),
    }


def _manifest_state(project, declarations, intent_error) -> dict:
    """Which file the tab writes, whether it exists, and what it says now.

    The path is shown even though principle 1 of §2.10 says the user never sees
    a file path unless they ask — because §2.9's ruling is stronger here: *the
    app shows which manifests it read, always*. A tab that silently edits a
    file the user cannot name is the failure that ruling exists to prevent.
    """
    board = getattr(project, "board", None)
    path = netclass_module.default_intent_path(board) if board else None
    declared = getattr(project, "netintent", None)
    return {
        "path": str(path) if path else None,
        "exists": bool(path and Path(path).exists()),
        # Whether the *audit* read it. A manifest that exists but was not
        # routed into the run is the one state a user could not otherwise see.
        "in_use": bool(declared),
        "in_use_path": str(declared) if declared else None,
        "declarations": [_declaration_dict(d) for d in declarations],
        "error": intent_error,
    }


# ---------------------------------------------------------------------------
# Writing — the user's own word, and nothing else
# ---------------------------------------------------------------------------

@dataclass
class Edit:
    """One net's intent, as the tab collected it."""
    net: str
    net_class: str
    note: str | None = None
    current_a: float | None = None
    rise_c: float | None = None
    #: Remove this net's exact-match declaration instead of writing one.
    remove: bool = False


def _quote(text: str) -> str:
    """A YAML double-quoted scalar. Escapes what that context requires.

    Written by hand rather than with `yaml.dump` because the file is also a
    document a human reads and occasionally hand-edits — `netclass.propose`
    emits it the same way, with the evidence as trailing comments — and a
    round-trip through the dumper reflows all of that into something the engine
    accepts and nobody wants to open.
    """
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _entry_lines(entry: dict) -> list[str]:
    lines = [f"  - match: {_quote(entry['match'])}",
             f"    net_class: {entry['net_class']}"]
    for key in ("kind", "note"):
        if entry.get(key):
            lines.append(f"    {key}: {_quote(entry[key])}")
    for key in ("current_a", "rise_c"):
        value = entry.get(key)
        if value is not None:
            lines.append(f"    {key}: {value:g}")
    return lines


def _render(entries: list[dict]) -> str:
    # **Deliberately pure ASCII.** The user's own `note` may be anything and is
    # written as UTF-8; this preamble stays ASCII so that the fixed part of the
    # file cannot be what a mis-configured reader trips over. §11A.4 is the
    # precedent — the Unicode crash there was worse than it looked.
    lines = [
        _MARKER + ".",
        "#",
        "# This is YOUR word about your own nets, and it is the only thing in",
        "# pcbreview that lowers a severity. Nothing infers a demotion.",
        "#",
        "#   net_class: other      you are not interested in this net. Its",
        "#                         findings are still measured and reported,",
        "#                         they just stop outranking findings on nets",
        "#                         you did not rule out.",
        "#   net_class: power      it carries current - admits it to the",
        "#                         current-capacity and heat-path checks.",
        "#   net_class: protocol   it is a bus.",
        "#",
        "# `note` is optional on all three. It is for whoever reads the report.",
        "#",
        "# `current_a` is the one fact only you can supply: it turns an",
        "# ampacity from a statement about copper into a verdict. `rise_c`",
        "# defaults to 10 C, deliberately strict.",
        "#",
        "# First match wins, so an exact net name above a glob overrides it.",
        "",
        "nets:",
    ]
    if not entries:
        lines.append("  []")
        return "\n".join(lines) + "\n"
    for entry in entries:
        lines.extend(_entry_lines(entry))
    return "\n".join(lines) + "\n"


def _validated(entry: dict) -> dict:
    """One edit, checked for the things `load_intent` refuses, named per field.

    `load_intent` is still the authority and validates the whole file before it
    lands. This exists so the *tab* can put the message beside the field the
    user is filling in, rather than reporting a file-level parse error about a
    file they never saw.
    """
    net_class = entry.get("net_class")
    if net_class not in VALID_CLASSES:
        raise IntentError(
            f"{entry.get('match')}: net_class={net_class!r}; it must be one of "
            f"{', '.join(VALID_CLASSES)}")
    # **No note is required, on any class.** The rule that demanded one for
    # `power`/`protocol` was removed from the engine — it described a cap that
    # has not existed since 2026-08-03 and an exemption those classes never
    # granted, and it asked the user to justify requesting *more* scrutiny
    # while `other`, the only declaration that lowers a severity, asked for
    # nothing. `netclass.load_intent` has the full account.
    note = (entry.get("note") or "").strip() or None

    def number(key: str):
        value = entry.get(key)
        if value is None or value == "":
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise IntentError(
                f"{entry.get('match')}: {key}={entry.get(key)!r} is not a "
                f"number. Give a bare number in amps or degrees C, no "
                f"unit") from None
        if value <= 0:
            raise IntentError(
                f"{entry.get('match')}: {key}={value:g}; it must be positive")
        return value

    current_a, rise_c = number("current_a"), number("rise_c")
    if rise_c is not None and current_a is None:
        raise IntentError(
            f"{entry.get('match')} allows a {rise_c:g} C rise but states no "
            f"current. A permitted rise on its own changes nothing and reads "
            f"as though the net had been checked")
    return {"match": entry["match"], "net_class": net_class,
            "kind": (entry.get("kind") or None), "note": note,
            "current_a": current_a, "rise_c": rise_c}


def _merge(existing: list, edits: list[Edit]) -> tuple[list[dict], list[str]]:
    """Existing declarations plus the edits, in a first-match-wins order.

    Three cases, and the third is the one that would quietly destroy work:

    * the net has an **exact-match** declaration — update it in place, so its
      position relative to any glob is preserved;
    * the net has **nothing** — append it;
    * the net is covered by a **glob** (`/FIRE_CH*`) — insert the exact match
      *above* that glob and leave the glob alone. Rewriting the glob would
      silently re-declare every other net it covers, and the user asked about
      one net.
    """
    entries = [_declaration_dict(d) for d in existing]
    notes: list[str] = []
    for edit in edits:
        at = next((i for i, e in enumerate(entries)
                   if e["match"] == edit.net), None)
        if edit.remove:
            if at is not None:
                entries.pop(at)
                notes.append(f"{edit.net}: declaration removed")
            else:
                notes.append(f"{edit.net}: nothing to remove — it had no "
                             f"declaration of its own")
            continue
        row = {"match": edit.net, "net_class": edit.net_class,
               "kind": None, "note": edit.note,
               "current_a": edit.current_a, "rise_c": edit.rise_c}
        if at is not None:
            row["kind"] = entries[at].get("kind")
            entries[at] = row
            notes.append(f"{edit.net}: declaration updated")
            continue
        covering = next((i for i, e in enumerate(entries)
                         if e["match"] != edit.net
                         and fnmatch.fnmatchcase(edit.net, e["match"])), None)
        if covering is None:
            entries.append(row)
            notes.append(f"{edit.net}: declared {edit.net_class}")
        else:
            entries.insert(covering, row)
            notes.append(
                f"{edit.net}: declared {edit.net_class}, inserted above the "
                f"pattern {entries[covering + 1]['match']!r} that was covering "
                f"it — first match wins, and that pattern still covers every "
                f"other net it matched")
    return entries, notes


def write_intent(project, edits: list[Edit]) -> dict:
    """Write `<board>.netintent.yaml`. **The only writer in this module.**

    Never `review/proposed.netintent.yaml`, and never a promotion of it: the
    proposal is a generated inference over every net, and renaming it over a
    curated manifest replaces the user's own judgement — the one thing in this
    tool that is theirs.

    The write is validated before it lands and atomic when it does:

    1. render the merged document;
    2. write it to a temp file **beside the target** and parse that with
       `load_intent`, so the engine's own loader is what decides whether it is
       acceptable — not a second opinion in this file that could drift from it;
    3. copy the existing file aside if we did not write it (a YAML round-trip
       drops the comments a hand-written manifest carries its reasoning in);
    4. `os.replace` the temp file over the target.

    A failure at step 2 leaves the user's file untouched and returns the
    engine's message.
    """
    board = getattr(project, "board", None)
    if board is None:
        raise IntentError("this project has no board, so there is nowhere to "
                          "write a manifest")
    target = Path(netclass_module.default_intent_path(board))

    existing, error = read_intent(target if target.exists() else None)
    if error:
        raise IntentError(
            f"the manifest already beside this board cannot be read, so it "
            f"will not be overwritten: {error}")

    merged, notes = _merge(existing, edits)
    document = _render([_validated(entry) for entry in merged])

    temp = target.with_suffix(target.suffix + ".writing")
    temp.write_text(document, encoding="utf-8")
    try:
        # The engine's loader, over the exact bytes about to become the file.
        # `_validated` above checks the same rules per field for a better
        # message; this is what makes the guarantee.
        loaded = netclass_module.load_intent(temp)
    except netclass_module.NetIntentError as exc:
        temp.unlink(missing_ok=True)
        raise IntentError(str(exc).replace(str(temp), str(target))) from exc

    backup = None
    if target.exists() and _MARKER not in target.read_text(encoding="utf-8",
                                                           errors="replace"):
        # Written by hand, or by an earlier tool. One copy, once — the round
        # trip below keeps every declaration but not the comments around them.
        backup = target.with_name(
            f"{target.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)

    os.replace(temp, target)
    return {"written": True, "path": str(target),
            "declarations": len(loaded.declarations),
            "backup": str(backup) if backup else None,
            "notes": notes}


# ---------------------------------------------------------------------------
# The command line — the manifest conversation, without the YAML
# ---------------------------------------------------------------------------

USAGE = """\
usage: python -m pcbreview.facts <project folder | board.kicad_pcb> [options]

Runs the review and prints what the engine INFERRED about every net, the
evidence for it, and the shortlist: the nets nothing identified that findings
actually name. On a 229-net board that is about a dozen rows, and they are the
only ones where an answer changes a severity.

  --all-nets     every net, not just the shortlist
  --questions    the question queue instead of the table
  --measured     fill in what the copper actually carries. Costs a pass over
                 the board's copper; worth it before asking a user what a net
                 must carry
  --declare SPEC record an answer. Repeatable. The manifest is written through
                 the engine's own loader, so a file it would refuse never
                 lands, and a hand-written one is copied aside once before the
                 first overwrite
  --remove NET   drop NET's own declaration
  --force        write a declaration even though it matches no net here

SPEC is `net=class` plus optional `,current=A`, `,rise=C`, `,note=text`:

  --declare "/CH1_DRAIN=other,current=1.65,rise=40,note=fires for 1 s"
  --declare "/SCL=protocol"

class is one of: power, protocol, other — `netclass.VALID_CLASSES`, and this
prints them if you get it wrong. `other` is the only one that LOWERS a
severity, and it is recorded as your declaration: the finding keeps the
severity its measurement produced, marked as demoted by you.
"""

#: `--declare` field names, mapped to `Edit`'s. Kept short on purpose: this is
#: typed by hand at a prompt, and `current_a=` invites `current_a=1.65 A`.
_DECLARE_FIELDS = {"current": "current_a", "rise": "rise_c"}


def parse_declaration(spec: str) -> Edit:
    """`net=class,current=1.65,rise=40,note=...` into one `Edit`.

    Split on the FIRST `=` for the net, because a net name may not contain one
    but a note certainly may — so the note is taken off the end first and keeps
    every comma inside it.
    """
    if "=" not in spec:
        raise IntentError(
            f"{spec!r}: a declaration is `net=class` with optional "
            f"`,current=`, `,rise=` and `,note=`")
    net, remainder = spec.split("=", 1)
    net = net.strip()
    if not net:
        raise IntentError(f"{spec!r}: no net name before the `=`")

    note = None
    if ",note=" in remainder:
        remainder, note = remainder.split(",note=", 1)

    pieces = [p for p in remainder.split(",") if p.strip()]
    if not pieces:
        raise IntentError(f"{spec!r}: no net class. One of "
                          f"{', '.join(VALID_CLASSES)}")
    net_class = pieces[0].strip()
    if net_class not in VALID_CLASSES:
        raise IntentError(f"{spec!r}: net_class={net_class!r}; it must be one "
                          f"of {', '.join(VALID_CLASSES)}")

    fields: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" not in piece:
            raise IntentError(f"{spec!r}: {piece!r} is not `key=value`")
        key, value = piece.split("=", 1)
        key = key.strip()
        if key not in _DECLARE_FIELDS:
            raise IntentError(
                f"{spec!r}: unknown field {key!r}. Known: "
                f"{', '.join(_DECLARE_FIELDS)} and note, with the net class "
                f"first")
        fields[_DECLARE_FIELDS[key]] = value.strip()

    def number(key: str):
        raw = fields.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except ValueError:
            raise IntentError(
                f"{spec!r}: {key}={raw!r} is not a number. A bare number, "
                f"amps or degrees C, no unit") from None

    return Edit(net=net, net_class=net_class,
                note=(note.strip() if note else None),
                current_a=number("current_a"), rise_c=number("rise_c"))



def nets_not_on(board_path, matches: list[str]) -> list[str]:
    """Which of `matches` name nothing on this board. Globs allowed.

    Read straight off the board's own net table, which is the cached
    extraction — so this costs nothing on a board that has just been reviewed
    and about a second on one that has not.

    `netclass.load_intent` cannot do this and is not failing to: it validates a
    manifest against itself, and a manifest is perfectly valid while describing
    a board it has never met. That is the whole failure mode — a declaration
    that names no net writes cleanly, loads cleanly and changes nothing.
    """
    from pcbreview.model import Board

    try:
        # `Board.nets` is `{code: Net}`, so the values, not the keys.
        names = {net.name for net in Board.from_kicad(board_path).nets.values()}
    except Exception:                                         # noqa: BLE001
        # The board could not be read. Refusing to write on the strength of a
        # check that did not run would be worse than writing.
        return []
    # **Exact match first, and it is not redundant.** KiCad writes a bus member
    # as `/DATA[0]`, and `[0]` is an fnmatch character class — so the net's own
    # name, pasted verbatim, matches nothing, and without this line the tool
    # would tell a user that a net they are looking at does not exist.
    #
    # It is a narrower fix than it looks. `netclass._governing` matches the
    # same way, so a bracketed net has never been reachable by its exact name;
    # what this stops is the false denial. To actually declare one, glob it
    # (`/DATA*`) or escape the bracket (`/DATA[[]0]`).
    return [match for match in matches
            if match not in names
            and not any(fnmatch.fnmatchcase(name, match) for name in names)]


def main(argv: list[str]) -> int:
    import sys

    from pcbreview import console_utf8

    console_utf8()

    if "--help" in argv or "-h" in argv or len(argv) < 2:
        sys.stderr.write(USAGE)
        return 0 if ("--help" in argv or "-h" in argv) else 2

    positional: list[str] = []
    declares: list[str] = []
    removes: list[str] = []
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in ("--declare", "--remove"):
            if index + 1 >= len(argv):
                sys.stderr.write(f"{argument} needs a value\n")
                return 2
            (declares if argument == "--declare" else removes).append(
                argv[index + 1])
            index += 2
            continue
        if not argument.startswith("--"):
            positional.append(argument)
        index += 1

    if not positional:
        sys.stderr.write(USAGE)
        return 2

    from pcbreview import pipeline

    try:
        project = pipeline.discover(positional[0])
    except pipeline.PipelineError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    # **Writing first, and without running the audit.** A declaration is the
    # user's own word; it needs no measurement to record, and making somebody
    # wait a minute for a review before their answer lands is how an answer
    # gets given twice.
    if declares or removes:
        try:
            edits = [parse_declaration(spec) for spec in declares]
            edits += [Edit(net=net, net_class="other", remove=True)
                      for net in removes]
            unknown = [] if "--force" in argv else nets_not_on(
                project.board, [e.net for e in edits])
            if unknown:
                # **A declaration naming no net is silent, and silence here is
                # expensive.** It writes cleanly, loads cleanly, changes
                # nothing, and the next review reports the same severity — so
                # the user concludes the manifest was ignored.
                #
                # It is also how a POSIX-emulating shell on Windows breaks
                # this: MSYS rewrites a bare `/GND` argument into
                # `C:/Program Files/Git/GND` before Python ever sees it, and
                # the result is a declaration for a net no board has.
                sys.stderr.write(
                    f"{len(unknown)} declaration(s) match no net on "
                    f"{project.board.name}, and would change nothing:\n")
                for name in unknown:
                    sys.stderr.write(f"  {name}\n")
                sys.stderr.write(
                    "Nothing was written. A glob is fine as long as it matches "
                    "something. If a name above looks like a Windows path, "
                    "your shell rewrote a leading `/`: quote the whole "
                    "argument, or set MSYS_NO_PATHCONV=1. `--force` writes it "
                    "anyway.\n")
                return 2
            outcome = write_intent(project, edits)
        except IntentError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        print(f"wrote {outcome['path']} — {outcome['declarations']} "
              f"declaration(s)")
        for note in outcome["notes"]:
            print(f"  {note}")
        if outcome["backup"]:
            print(f"  the hand-written manifest that was there is now "
                  f"{Path(outcome['backup']).name} — a YAML round trip does "
                  f"not keep comments")
        print("  a review already on screen no longer describes what is on "
              "disk; re-run `python -m pcbreview.run` to measure with this "
              "applied")
        return 0

    if project.stackup is None:
        sys.stderr.write(
            f"{project.board.name}: no validated fabrication stackup, so the "
            f"review that produces these facts cannot run. "
            f"`python -m pcbreview.stackups {project.board.name}` lists the "
            f"shipped ones that fit.\n")
        return 1

    try:
        result = pipeline.review(project, keep_board="--measured" in argv)
    except pipeline.PipelineError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    index_obj = None
    if "--measured" in argv and result.board is not None:
        from pcbreview import dossier as dossier_module

        index_obj = dossier_module.build_index(
            result.board, result.layout, result.schematic)

    if "--questions" in argv:
        from pcbreview import run as run_module

        print(run_module.questions(result))
        return 0

    from pcbreview import factsview

    print(factsview.render(result, all_nets="--all-nets" in argv,
                           index=index_obj))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
