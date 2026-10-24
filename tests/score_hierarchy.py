"""Score a whole-hierarchy schematic netlist against the PCB's, which KiCad
produced.

The single-sheet scorer (`score_schematic.py`) compares one file at a time and
so can only see the pins that file happens to contain. This one resolves the
entire sheet tree and compares every pin on the board, which is the only
measurement that says whether the netlist is usable for a design review.

Two things are scored separately and they are not equally important:

* **the partition** — which pins share a net. Everything downstream depends on
  it, so this must be exact.
* **the names** — reported split by whether a human named the net. A name a
  designer wrote is intent and must match; a name KiCad generated for a net
  nobody named is cosmetic (see `schematic._auto_name`).

    python3 tests/score_hierarchy.py <board.kicad_pcb> <top.kicad_sch>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcbreview.model import Board
from pcbreview.schematic import Schematic, netlist


def score(board: Board, schematic: Schematic) -> dict:
    pcb_groups = {
        board.nets[int(code)].name: set(pads)
        for code, pads in board.connectivity["pads_by_net"].items()
    }
    all_pcb = set().union(*pcb_groups.values()) if pcb_groups else set()
    all_sch = set().union(*[n.pin_keys for n in schematic.nets]) if schematic.nets else set()
    common = all_pcb & all_sch

    exact = split = merged = 0
    named_ok = named_total = auto_ok = auto_total = 0
    split_examples: list[str] = []
    merged_examples: list[str] = []
    name_examples: list[str] = []

    for net in schematic.nets:
        pins = net.pin_keys & common
        if not pins:
            continue
        owners = {name for name, pads in pcb_groups.items() if pads & pins}
        if len(owners) > 1:
            merged += 1
            if len(merged_examples) < 5:
                merged_examples.append(f"{net.name} -> {sorted(owners)[:3]}")
            continue
        owner = owners.pop()
        if (pcb_groups[owner] & common) != pins:
            split += 1
            if len(split_examples) < 5:
                missing = sorted((pcb_groups[owner] & common) - pins)[:4]
                split_examples.append(f"{net.name} is missing {missing} of {owner}")
            continue

        exact += 1
        if net.source == "auto":
            auto_total += 1
            auto_ok += owner == net.name
        else:
            named_total += 1
            named_ok += owner == net.name
            if owner != net.name and len(name_examples) < 5:
                name_examples.append(f"{net.name!r} vs KiCad {owner!r}")

    total = exact + split + merged
    return {
        "sheets": len(schematic.sheets),
        "components": len(schematic.components),
        "pins_compared": len(common),
        "pcb_only": sorted(all_pcb - all_sch),
        "schematic_only": sorted(all_sch - all_pcb),
        "nets": total,
        "exact": exact,
        "split": split,
        "merged": merged,
        "accuracy": exact / total if total else 0.0,
        "named_names": (named_ok, named_total),
        "auto_names": (auto_ok, auto_total),
        "split_examples": split_examples,
        "merged_examples": merged_examples,
        "name_examples": name_examples,
        "diagnostics": schematic.diagnostics,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2

    result = score(Board.from_kicad(argv[1]), netlist(argv[2]))
    named_ok, named_total = result["named_names"]
    auto_ok, auto_total = result["auto_names"]

    print(f"{result['sheets']} sheet placements, {result['components']} components, "
          f"{result['pins_compared']} pins compared")
    print(f"partition  nets={result['nets']:>4} exact={result['exact']:>4} "
          f"split={result['split']:>3} merged={result['merged']:>3} "
          f"accuracy={result['accuracy'] * 100:5.1f}%")
    print(f"names      designer-named {named_ok}/{named_total}   "
          f"auto-generated {auto_ok}/{auto_total} (cosmetic)")

    for example in result["split_examples"]:
        print("   SPLIT :", example)
    for example in result["merged_examples"]:
        print("   MERGED:", example)
    for example in result["name_examples"]:
        print("   NAME  :", example)
    if result["pcb_only"]:
        print(f"   on the board but not the schematic: {result['pcb_only'][:8]}"
              f"{' ...' if len(result['pcb_only']) > 8 else ''}")
    if result["schematic_only"]:
        print(f"   in the schematic but not on the board: "
              f"{result['schematic_only'][:8]}"
              f"{' ...' if len(result['schematic_only']) > 8 else ''}")
    for diagnostic in result["diagnostics"][:10]:
        print("   DIAG  :", diagnostic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
