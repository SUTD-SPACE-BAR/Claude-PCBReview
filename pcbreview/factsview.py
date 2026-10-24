"""`facts.build()` as console text — the Nets table, for a terminal.

The app renders this model as two sortable tables. An agent gets the same
model, and needs it in the same shape it will quote it in, so this is a
renderer and nothing more: it computes no number, applies no threshold and
drops no row that `facts.py` selected.

**The shortlist is the feature.** A four-layer board has 229 nets and 13 of
them are rows where an answer changes anything: nets nothing identified, that
findings actually name. Those come first and are marked, and the rest of the
table is behind `--all-nets` because a 229-row dump is how the shortlist stops
being read.
"""

from __future__ import annotations

_BAR = "=" * 74


def _cell(value, places: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


def render(result, all_nets: bool = False, index=None) -> str:
    """The Nets table and the manifest state, from a `pipeline.ReviewResult`.

    `index` is an optional `dossier.BoardIndex`. With one, three more columns
    are filled — what the copper actually carries, its temperature rise and
    how much pour is on the net — which is the difference between asking *"what
    should this net carry"* and asking it beside *"and here is what it can"*.
    Building it costs a pass over the board's copper, so it is the caller's
    choice and not this module's.
    """
    from pcbreview import facts as facts_module

    table = facts_module.build(result, index)
    out = ["", _BAR, "NETS — what the engine inferred, and why", _BAR]

    if not table.get("available"):
        out.append(f"  unavailable: {table.get('why', 'no reason given')}")
        return "\n".join(out + _manifest(table))

    rows = table["nets"]
    shortlist = set(table.get("shortlist") or [])
    shown = rows if all_nets else [r for r in rows if r["shortlisted"]]

    census = table.get("census") or {}
    by_class = census.get("by_class") or {}
    if by_class:
        out.append("  " + ", ".join(
            f"{count} {name}" for name, count
            in sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0]))))
    kinds = census.get("protocol_kinds") or {}
    if kinds:
        out.append("  protocols: " + ", ".join(
            f"{count} {name}" for name, count
            in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))))
    if census:
        # Where the classification came from. A board classified entirely off
        # net *names* has been read, not measured, and that is worth knowing
        # before anyone trusts a class.
        out.append(f"  evidence: {census.get('from_pin_names', 0)} from pin "
                   f"names, {census.get('from_net_names', 0)} from net names, "
                   f"{census.get('from_routed_width', 0)} from routed width")
        unmatched = census.get("declarations_matching_nothing") or []
        if unmatched:
            out.append(f"  {len(unmatched)} declaration(s) in your manifest "
                       f"match NO net on this board: {', '.join(unmatched)}")
    out.append(f"  {len(rows)} net(s); {table.get('unidentified', 0)} that "
               f"nothing identified; {table.get('placeholders', 0)} "
               f"placeholder name(s)")
    out.append(f"  SHORTLIST — {len(shortlist)} net(s) nothing identified that "
               f"findings name. These are the only rows where an answer "
               f"changes a severity.")
    out.append("")

    header = (f"  {'net':28s} {'class':10s} {'declared':8s} {'carries':>8s} "
              f"{'rise':>6s} {'finds':>5s}  evidence")
    out.append(header)
    out.append("  " + "-" * (len(header) - 2))
    for row in shown:
        mark = "*" if row["shortlisted"] else " "
        declared = (row["match"] or "") if row["match"] else "—"
        if row.get("covered_by_pattern"):
            declared = f"~{declared}"
        out.append(
            f" {mark}{row['net'][:28]:28s} {row['net_class'][:10]:10s} "
            f"{declared[:8]:8s} {_cell(row['carries_a']):>8s} "
            f"{_cell(row['measured_rise_c'], 0):>6s} "
            f"{row['findings']:>5d}  {row['evidence'][:60]}")
    if not shown:
        out.append("  every net a finding names was identified as a bus or a "
                   "rail — nothing to ask about")
    if not all_nets and len(rows) > len(shown):
        out.append(f"  ({len(rows) - len(shown)} more net(s) — nothing "
                   f"identified them and no finding names them, so an answer "
                   f"changes nothing. `--all-nets` prints them)")
    return "\n".join(out + _manifest(table))


def _manifest(table: dict) -> list[str]:
    """Which file an answer is written into, and whether the run read it.

    Both facts, always, and they are different questions: a manifest can exist
    and not have been routed into the run, which is the one state a user could
    not otherwise see and the one that makes them believe their file was
    ignored.
    """
    manifest = table.get("manifest") or {}
    exists = bool(manifest.get("exists"))
    if manifest.get("in_use"):
        state = "this run read it"
    elif exists:
        # The one state a user cannot otherwise see, and the one that makes
        # them believe their file was ignored — because it was.
        state = ("IT EXISTS AND THIS RUN DID NOT READ IT — nothing above was "
                 "measured with it applied")
    else:
        state = "nothing to read; `--declare` creates it"
    out = ["", "  net-intent manifest — where an answer to the questions goes"]
    out.append(f"    file      {manifest.get('path') or '—'}")
    out.append(f"    exists    {'yes' if exists else 'no'}")
    out.append(f"    state     {state}")
    declarations = manifest.get("declarations") or []
    out.append(f"    says      {len(declarations)} declaration(s)")
    for declaration in declarations[:20]:
        bits = [f"{declaration['match']} -> {declaration['net_class']}"]
        if declaration.get("current_a") is not None:
            bits.append(f"{declaration['current_a']} A")
        if declaration.get("rise_c") is not None:
            bits.append(f"{declaration['rise_c']} °C rise")
        out.append(f"              {', '.join(bits)}")
    if manifest.get("error"):
        out.append(f"    ERROR     {manifest['error']}")
    return out
