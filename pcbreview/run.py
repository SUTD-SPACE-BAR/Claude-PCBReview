"""`python -m pcbreview.run <project folder | board>` — the whole review, once.

What this replaces
------------------
The flow this tool shipped with asked the agent to run the auditor **three
times** — a grouped triage pass, an `--all` coverage pass, and a separate
schematic audit — then read all three and decide what mattered. Two things were
wrong with that and both were measured rather than argued:

* **It cost three runs to answer one question.** Even with the solve cache the
  second and third passes re-walk the geometry, re-run KiCad's DRC and re-print
  a report nobody reads twice.
* **The deciding was not reproducible.** Two sessions with identical inputs
  reported different things, which is the defect `SKILL.md` §"What you MUST
  report" was written to close. Rules written in prose are followed by
  judgement; the same rules written in `pcbreview/report/generate.py` are
  followed by a machine.

So this runs both entry points once, applies the user's severity policy, and
prints the selection `report/generate.py` makes. **Nothing here decides what
appears.** The agent's remaining job is the one a program cannot do: asking the
user what their nets are for, what their parts dissipate, and saying in a
sentence what the findings have in common.

What it prints, in order
------------------------
1. the inputs card — which file was read as what, the one place a mis-route shows
2. the deterministic report — rules 0 through 5 and the render floor
3. the questions queue — every `intent_needed` the run raised, deduplicated,
   with its evidence attached. **This is the agent's worklist.**
4. what is still unread — datasheets on disk that no thermal manifest covers
5. the solve-cache summary

`--quiet` drops 1, 4 and 5; `--questions-only` prints 3 alone, which is what a
second visit to a board wants.
"""

from __future__ import annotations

import sys
from pathlib import Path

USAGE = """\
usage: python -m pcbreview.run <project folder | board.kicad_pcb> [options]

Runs BOTH entry points — the layout audit and the schematic audit — over a
project, once, and prints the report `report/generate.py` selects. This
replaces the three-pass flow: there is no triage left to do by hand.

  --markdown PATH   also write the report as Markdown
  --json PATH       also write the whole report as JSON. `-` writes JSON to
                    stdout INSTEAD of the text report
  --facts           append the nets table: every net, its class, the evidence
                    for it, what the copper actually carries, and the
                    shortlist. Add --all-nets for the rows behind it
  --problems        append the problems view: findings grouped by shared cause
                    and by check, which is the shape a fix list wants
  --questions-only  print ONLY the questions queue. Everything else is skipped,
                    including the audit's report — use it on a board you have
                    already reviewed in this session
  --quiet           the report and nothing around it

  --refill          refill zones with kicad-cli on a temp copy first. OFF by
                    default: it moves the geometry ~0.21% and re-keys the
                    extraction cache, so a comparison with an earlier run shows
                    movement nobody caused. Use it when the fills are stale
  --no-solve-cache  solve every cross-section from scratch (much slower)
  --verify-cache    re-solve everything the cache served and exit non-zero if
                    any of it disagrees

Gate 1 still refuses a board with no validated fabrication stackup. When that
happens this names the shipped stackups that fit the board — see
`python -m pcbreview.stackups`.
"""


def _stackup_help(board: Path) -> str:
    """What to do about a missing stackup, with the answer already looked up.

    The old message was *"transcribe the fab's own stackup"*, which is correct
    and is also a wall: it hands back a task rather than an answer. Since the
    library ships entries, the honest message names the ones that could be this
    board — and only falls back to transcription when none can.
    """
    from pcbreview import stackups as stackups_module

    try:
        offer = stackups_module.offer(board)
    except Exception:                                         # noqa: BLE001
        offer = {}
    candidates = offer.get("candidates") or []
    if not candidates:
        return (
            "Transcribe the fab's own stackup table — "
            "`python -m pcbreview.stackup_cli transcribe` — and do not invent "
            "one. `python -m pcbreview.stackups` lists what is shipped and "
            "says why none of it fits this board.")
    lines = [f"{len(candidates)} shipped stackup(s) have this board's copper "
             f"layer count. Show the user these and let them choose — "
             f"do not pick for them:"]
    for entry in candidates:
        thickness = (f"{entry['finished_thickness_mm']} mm"
                     if entry.get("finished_thickness_mm") else
                     "thickness not stated by the fab")
        lines.append(f"  {entry['code']:20s} {entry['fab']}, {thickness} — "
                     f"{entry['title']}")
    lines.append("Then: python -m pcbreview.stackups <board> use <CODE>")
    return "\n".join(lines)


def questions(result) -> str:
    """The agent's worklist: what the engine asked and cannot answer itself.

    Every finding carries `intent_needed` — *the datum that would settle its
    severity*. `facts._questions` deduplicates them by (check, question) and
    unions the nets, which on one board turned 103 rows into a dozen. This
    renders that, and marks the ones a net-intent declaration cannot answer so
    nobody types into a box that does nothing.
    """
    from pcbreview import facts as facts_module

    table = facts_module.build(result, None)
    queue = table.get("questions") or []
    out = ["", "=" * 74,
           f"QUESTIONS FOR THE USER — {len(queue)}",
           "=" * 74,
           "The engine asked these. Nothing in a file answers them and no",
           "measurement can: they are what the board is FOR. Ask the user, then",
           "record the answers with `python -m pcbreview.facts … --declare`.",
           ""]
    if not queue:
        out.append("  none — every finding's severity is settled by "
                   "measurement alone")
    for entry in queue:
        out.append(f"  [{entry['worst']:8s}] {entry['question']}")
        out.append(f"             raised by {entry['source']}/{entry['check']} "
                   f"on {len(entry['findings'])} finding(s)")
        if entry["nets"]:
            shown = ", ".join(entry["nets"][:8])
            more = (f" (+{len(entry['nets']) - 8} more)"
                    if len(entry["nets"]) > 8 else "")
            out.append(f"             nets: {shown}{more}")
        if not entry["answerable"]:
            out.append(f"             NOT answerable by a declaration: "
                       f"{entry['why_not_answerable']}")
        elif entry["undeclared_nets"] != entry["nets"]:
            out.append(f"             {len(entry['undeclared_nets'])} of "
                       f"{len(entry['nets'])} still undeclared")
        # One finding, as the evidence that makes the question answerable.
        # The whole set is in `--facts`; a queue that printed all of them
        # would be the 103-row queue again.
        worst = entry["findings"][0]
        out.append(f"             e.g. {worst['title']}")
        if worst.get("basis"):
            out.append(f"                  basis: {worst['basis']}")
        out.append("")
    out += [
        "  WHAT AN ANSWER CAN RECORD, and what it cannot:",
        "    net_class   power | protocol | other  — `other` is the only one "
        "that lowers a severity",
        "    current     amps this net actually carries. The one fact only the "
        "designer has",
        "    rise        the temperature rise that is acceptable ON THIS NET "
        "(default 10 C)",
        "    note        free text. It is SHOWN beside any finding the "
        "declaration quietens and",
        "                is read by nobody else — a duty cycle or a "
        "\"ground test only\" does not",
        "                change a measurement. Say it in your report as well "
        "as in the note.",
        "",
    ]
    shortlist = table.get("shortlist") or []
    if shortlist:
        out.append(f"  {len(shortlist)} net(s) nothing identified, that "
                   f"findings above name — the only rows where an answer "
                   f"changes anything:")
        out.append("    " + ", ".join(shortlist))
    return "\n".join(out)


def _unread_datasheets(project) -> str:
    """PDFs beside the board that no thermal manifest accounts for.

    The old flow reported datasheets as *"not read by the auditor; read it
    yourself"*. Two of the three stages that turn one into a thermal manifest
    entry are now code, so the honest line names the command rather than the
    chore.
    """
    if not project.datasheets:
        return ""
    covered = "yes" if project.thermal else "no thermal manifest"
    return (f"\ndatasheets: {len(project.datasheets)} PDF(s) beside this board, "
            f"thermal manifest: {covered}\n"
            f"  `python -m pcbreview.parts {project.board.name}` reads them — "
            f"LCSC catalogue, then the datasheet's own tables, then a local "
            f"model for what those missed — and writes the manifest.")


def main(argv: list[str]) -> int:
    from pcbreview import console_utf8

    console_utf8()

    if "--help" in argv or "-h" in argv or len(argv) < 2:
        sys.stderr.write(USAGE)
        return 0 if ("--help" in argv or "-h" in argv) else 2

    options: dict[str, str | None] = {"markdown": None, "json": None}
    positional: list[str] = []
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in ("--markdown", "--json"):
            if index + 1 >= len(argv):
                sys.stderr.write(f"{argument} needs a path\n")
                return 2
            options[argument.lstrip("-")] = argv[index + 1]
            index += 2
            continue
        if not argument.startswith("--"):
            positional.append(argument)
        index += 1

    if not positional:
        sys.stderr.write(USAGE)
        return 2

    quiet = "--quiet" in argv
    questions_only = "--questions-only" in argv
    verify_cache = "--verify-cache" in argv
    use_cache = "--no-solve-cache" not in argv

    from pcbreview import pipeline, solvestore
    from pcbreview.report import render

    try:
        project = pipeline.discover(positional[0])
    except pipeline.PipelineError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    if not (quiet or questions_only):
        print("inputs")
        print(project.describe())
        print()

    if project.stackup is None:
        sys.stderr.write(
            f"{project.board.name}: no validated fabrication stackup, and the "
            f"audit will not invent one — every impedance, coupling and "
            f"return-path number depends on real dielectric heights.\n"
            f"{_stackup_help(project.board)}\n")
        return 1

    store = solvestore.open_default() if use_cache else None
    if store is not None:
        store.retain = verify_cache
    elif verify_cache:
        sys.stderr.write("--verify-cache: the solve cache is switched off, so "
                         "there is nothing to verify.\n")
        return 2

    try:
        # `keep_board` only when something downstream needs the geometry.
        # The facts table's measured columns — what the copper actually
        # carries — come from a `dossier` index, and that needs the loaded
        # board. It has to be kept *inside* `review()`, because a refilled
        # board lives in a temp directory that call deletes on the way out.
        result = pipeline.review(
            project, refill="--refill" in argv,
            keep_board="--facts" in argv,
            solve_store=store if store is not None else False)
    except pipeline.PipelineError as exc:
        # Gate 1 and its neighbours refusing is a *correct* outcome and the
        # commonest way this exits, so it reads as a message, not a crash.
        sys.stderr.write(f"{exc}\n")
        if store is not None:
            store.close()
        return 1

    if questions_only:
        print(questions(result))
    elif options["json"] == "-":
        print(render.to_json(result.report))
    else:
        print(render.text(result.report))
        print(questions(result))
        if "--facts" in argv:
            from pcbreview import dossier as dossier_module
            from pcbreview import factsview

            index = (dossier_module.build_index(result.board, result.layout,
                                                result.schematic)
                     if result.board is not None else None)
            print(factsview.render(result, all_nets="--all-nets" in argv,
                                   index=index))
        if "--problems" in argv:
            from pcbreview import problems as problems_module
            from pcbreview import problemsview
            print(problemsview.render(problems_module.build(
                result.layout, result.schematic, result.report)))
        if not quiet:
            print(_unread_datasheets(result.project))

    if options["json"] and options["json"] != "-":
        Path(options["json"]).write_text(render.to_json(result.report),
                                         encoding="utf-8")
        if not quiet:
            print(f"json: {options['json']}")
    if options["markdown"]:
        Path(options["markdown"]).write_text(render.markdown(result.report),
                                             encoding="utf-8")
        if not quiet:
            print(f"markdown: {options['markdown']}")

    status = 0
    if store is not None:
        if not (quiet or questions_only):
            print()
            print(store.summary())
        if verify_cache:
            print(f"  verifying all {store.retained} reused solve(s) by "
                  f"solving them again — this costs the time the cache saved, "
                  f"which is the price of the answer")
            outcome = store.verify_served()
            print(f"  {outcome['matched']} of {outcome['checked']} matched bit "
                  f"for bit, {outcome['absent']} no longer stored")
            if outcome["mismatched"]:
                sys.stderr.write(
                    f"SOLVE CACHE IS WRONG on {len(outcome['mismatched'])} "
                    f"cross-section(s). Do not trust this review; clear the "
                    f"cache and report it.\n")
                for description in outcome["mismatched"][:10]:
                    sys.stderr.write(f"  {description}\n")
                status = 1
        store.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
