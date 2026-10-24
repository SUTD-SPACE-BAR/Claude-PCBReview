"""Cross-validate the extractor and model against KiCad on every board found.

For each .kicad_pcb this generates KiCad's own geometry (via KiCad's Python),
rebuilds it through pcbreview, and reports the worst disagreement. Any
deviation above the thresholds below means our geometry is wrong.

    python3 tests/validate_all_boards.py [search_root]
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcbreview.model import Board, DEFAULT_KICAD_PYTHON

# Relative tolerances. Areas should agree to floating-point noise; lengths carry
# KiCad's own 1 nm rounding on arc endpoints.
AREA_TOLERANCE = 1e-9
LENGTH_TOLERANCE = 1e-5

#: How many times a disagreeing board is re-measured before it is believed.
#: Two, not "until it passes" — see the comment in `main`.
ATTEMPTS = 2


def kicad_truth(pcb_path: Path, interpreter: str) -> dict:
    script = Path(__file__).with_name("kicad_truth.py")
    with tempfile.TemporaryDirectory() as tmp:
        slug = "tmp_validate"
        # The child runs in a temp dir, so the board path must be absolute.
        # It used to be passed through as given, which meant a relative search
        # root -- `validate_all_boards.py ../some-board-dir` -- silently failed
        # to find the board while the parent, running in its own cwd, loaded it
        # fine. Every board reported FAILED with the harmless wxApp assert as
        # the reason.
        result = subprocess.run(
            [interpreter, str(script), str(pcb_path.resolve()), slug],
            capture_output=True, text=True, cwd=tmp)
        truth_path = (Path(__file__).resolve().parents[1] / "fixtures" / "truth"
                      / f"{slug}.truth.json")
        if not truth_path.exists():
            raise RuntimeError(_child_failure(result))
        data = json.loads(truth_path.read_text())
        truth_path.unlink()
        return data


def _child_failure(result: subprocess.CompletedProcess) -> str:
    """Why the truth subprocess produced nothing, minus the noise.

    `pcbnew` always writes an `assert "traits" failed ... create wxApp` line to
    stderr on import, so reporting raw stderr names that as the cause of every
    failure. Worse, the filter the docs tell you to pipe through --
    `grep -v "assert"` -- then deletes the whole FAILED line, so a board
    vanishes from the table with no explanation at all.
    """
    noise = ("create wxApp", "Warning: PCB_VIA")
    lines = [line for line in (result.stderr or "").splitlines()
             if line.strip() and not any(n in line for n in noise)]
    lines += [line for line in (result.stdout or "").splitlines() if line.strip()]
    detail = " | ".join(lines) if lines else "no output"
    return f"exit {result.returncode}: {detail}"


def compare(board: Board, truth: dict) -> dict:
    worst = {"outline": 0.0, "zone": 0.0, "track": 0.0, "pad": 0.0}
    detail = {}

    if truth["outline_area"] > 0:
        worst["outline"] = abs(board.outline.area - truth["outline_area"]) / truth["outline_area"]

    for key, expected in truth["zone_area"].items():
        zone_id, layer = key.split("|")
        ours = board.zones[int(zone_id)].fill_on(layer).area
        deviation = abs(ours - expected) / expected if expected else 0.0
        if deviation > worst["zone"]:
            worst["zone"], detail["zone"] = deviation, key

    for name, expected in truth["track_len"].items():
        try:
            ours = sum(t.length_mm for t in board.tracks_on_net(name))
        except KeyError:
            continue
        deviation = abs(ours - expected) / expected if expected else 0.0
        if deviation > worst["track"]:
            worst["track"], detail["track"] = deviation, name

    by_designator = {p.designator: p for p in board.pads}
    for designator, expected in truth["pad_area"].items():
        pad = by_designator.get(designator)
        if pad is None or expected <= 0:
            continue
        deviation = abs(pad.copper.area - expected) / expected
        if deviation > worst["pad"]:
            worst["pad"], detail["pad"] = deviation, designator

    return {"worst": worst, "detail": detail}


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[2]
    interpreter = DEFAULT_KICAD_PYTHON
    if not Path(interpreter).exists():
        print(f"KiCad Python not found at {interpreter}")
        return 2

    boards = sorted(p for p in root.rglob("*.kicad_pcb") if ".history" not in p.parts)
    if not boards:
        print(f"no boards found under {root}")
        return 1

    print(f"validating {len(boards)} boards against KiCad's geometry engine\n")
    header = (f"{'board':<44} {'trk':>5} {'via':>5} {'zone':>5} "
              f"{'outline':>9} {'zonearea':>9} {'length':>9} {'pad':>9} {'s':>5}")
    print(header)
    print("-" * len(header))

    failures = 0
    wobbled = []
    for pcb_path in boards:
        start = time.time()
        outcome = None
        # A disagreement is re-measured before it is believed. KiCad's own
        # ZONE_FILLER gives two different answers on some boards (§40), and
        # this script draws the fill *twice* -- once through `extract.py` and
        # once through `kicad_truth.py` -- so the two sides can legitimately be
        # describing different pours. Retrying costs a couple of seconds and
        # removes the commonest false alarm this script produces.
        #
        # Bounded at ATTEMPTS on purpose: if it disagrees every time, that is a
        # real disagreement and a human has to look at it. Retrying until it
        # passes would turn a genuine geometry bug into an infinite loop that
        # eventually reports success.
        for attempt in range(1, ATTEMPTS + 1):
            try:
                board = Board.from_kicad(pcb_path, refresh=True)
                # KiCad's own numbers now ride along in the extraction, taken
                # in the same process from the same pour (§40b). The old path
                # spawned a second KiCad that refilled the zones again, so a
                # non-reproducible fill made the two sides describe different
                # copper. Kept as a fallback for caches written before schema 2.
                truth = board.kicad_measurements or kicad_truth(
                    pcb_path, interpreter)
            except Exception as exc:  # noqa: BLE001 - report and continue
                outcome = ("error", str(exc)[:60], None, None, attempt)
                break

            result = compare(board, truth)
            worst = result["worst"]
            bad = (worst["outline"] > AREA_TOLERANCE
                   or worst["zone"] > AREA_TOLERANCE
                   or worst["pad"] > 1e-6 or worst["track"] > LENGTH_TOLERANCE)
            outcome = ("bad" if bad else "ok", result, board, worst, attempt)
            if not bad:
                break

        kind, result, board, worst, attempt = outcome
        elapsed = time.time() - start
        if kind == "error":
            print(f"{pcb_path.name[:43]:<44} FAILED: {result}")
            failures += 1
            continue

        flag = ""
        if kind == "bad":
            flag = f"  <-- FAIL after {attempt} attempt(s)"
            failures += 1
        elif attempt > 1:
            flag = f"  (agreed on attempt {attempt})"
            wobbled.append(pcb_path.name)

        print(f"{pcb_path.name[:43]:<44} "
              f"{len(board.tracks):>5} {len(board.vias):>5} "
              f"{len([z for z in board.zones if not z.is_rule_area]):>5} "
              f"{worst['outline']:>9.2e} {worst['zone']:>9.2e} "
              f"{worst['track']:>9.2e} {worst['pad']:>9.2e} {elapsed:>5.1f}{flag}")
        if kind == "bad":
            print(f"    worst offenders: {result['detail']}")

    print()
    if wobbled:
        print(f"{len(wobbled)} board(s) disagreed once and agreed on a re-run: "
              f"{', '.join(wobbled)}")
        print("  that is KiCad's zone filler, not this code — see HANDOFF §40")
    if failures:
        print(f"{failures} board(s) disagree with KiCad after {ATTEMPTS} "
              f"attempts — geometry is not trustworthy, and a re-run will not "
              f"fix it. Look at the worst offenders above.")
        return 1
    print("all boards agree with KiCad's geometry engine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
