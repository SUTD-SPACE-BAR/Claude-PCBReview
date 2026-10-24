"""Score the schematic netlist against the PCB's, which KiCad itself produced.

The PCB stores an authoritative net assignment for every pad, so comparing the
schematic parser's pin grouping against it measures connectivity directly
rather than by inspection.

Names are ignored: what matters is the *partition* — which pins end up on the
same net. A net the parser leaves unnamed but groups correctly is a cosmetic
problem; one it splits is a real error that would corrupt every downstream
intent decision.

    python3 tests/score_schematic.py <board.kicad_pcb> <top.kicad_sch>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcbreview.model import Board


def schematic_groups(sch_path: Path, parser) -> list[tuple[set[str], str]]:
    root = parser.sexpdata.loads(Path(sch_path).read_text())
    lib = parser.extract_lib_symbol_pins(root)
    components = parser.extract_symbol_instances(root, lib)
    nets = parser.trace_nets(
        components,
        parser.extract_wires(root),
        parser.extract_all_labels(root),
        parser.extract_junctions(root),
        parser.extract_no_connects(root),
    )
    return [({f"{r}.{p}" for r, p, _, _ in n["pins"]}, n["name"] or "") for n in nets]


def score(board: Board, groups: list[tuple[set[str], str]]) -> dict:
    pcb_groups = {
        board.nets[int(code)].name: set(pads)
        for code, pads in board.connectivity["pads_by_net"].items()
    }
    all_pcb = set().union(*pcb_groups.values()) if pcb_groups else set()
    all_sch = set().union(*[g for g, _ in groups]) if groups else set()
    common = all_sch & all_pcb

    exact = split = merged = 0
    split_examples: list[str] = []
    merged_examples: list[str] = []

    for pins, name in groups:
        pins = pins & common
        if not pins:
            continue
        owners = {n for n, pads in pcb_groups.items() if pads & pins}
        if len(owners) > 1:
            merged += 1
            if len(merged_examples) < 5:
                merged_examples.append(f"{name or '<unnamed>'} -> {sorted(owners)[:3]}")
            continue
        owner = owners.pop()
        if (pcb_groups[owner] & common) == pins:
            exact += 1
        else:
            split += 1
            if len(split_examples) < 5:
                missing = sorted((pcb_groups[owner] & common) - pins)[:4]
                split_examples.append(
                    f"{name or '<unnamed>'} is missing {missing} of {owner}")

    total = exact + split + merged
    return {
        "pins_compared": len(common),
        "nets": total,
        "exact": exact,
        "split": split,
        "merged": merged,
        "accuracy": exact / total if total else 0.0,
        "named": sum(1 for _, n in groups if n),
        "split_examples": split_examples,
        "merged_examples": merged_examples,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2

    import importlib

    board = Board.from_kicad(argv[1])
    results = {}
    for label, module in (("BEFORE", "tests._original_parser"),
                          ("AFTER", "pcbreview._schematic_base")):
        try:
            parser = importlib.import_module(module)
        except ImportError:
            continue
        results[label] = score(board, schematic_groups(Path(argv[2]), parser))

    for label, result in results.items():
        print(f"{label:7s} nets={result['nets']:>4} exact={result['exact']:>4} "
              f"split={result['split']:>3} merged={result['merged']:>3} "
              f"accuracy={result['accuracy'] * 100:5.1f}% "
              f"named={result['named']}")
    final = results.get("AFTER") or next(iter(results.values()))
    for example in final["split_examples"]:
        print("   SPLIT :", example)
    for example in final["merged_examples"]:
        print("   MERGED:", example)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
