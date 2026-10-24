"""Generate KiCad-computed reference geometry for cross-validation.

Runs under KiCad's bundled Python and writes fixtures/truth/<slug>.truth.json.
These files are the independent check on our shapely reconstruction: they come
from KiCad's own geometry engine, not from anything in pcbreview.

    /path/to/kicad/python3 tests/kicad_truth.py <board.kicad_pcb> <slug>
"""

import json
import os
import sys

import pcbnew

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Imported for `kicad_measurements` only. `extract` is the one module that may
# import pcbnew, and this script already runs under KiCad's Python, so there is
# no interpreter split to worry about here.
from pcbreview import extract  # noqa: E402

TRUTH_DIR = os.path.join(_ROOT, "fixtures", "truth")


def collect(board):
    """Refill, then measure — the standalone path.

    **Prefer `Board.kicad_measurements`.** Since schema 2 the same numbers ride
    along inside every extraction, measured in the same process from the same
    pour, and that is what `validate_all_boards.py` and `test_model.py` now
    read. This script exists for regenerating the checked-in
    `fixtures/truth/*.json` by hand and as the fallback for an older cache.

    The measurement itself lives in `pcbreview.extract.kicad_measurements` so
    there is exactly one implementation of it. Only the fill is done here.
    """
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        pass
    return extract.kicad_measurements(board)


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: kicad_truth.py <board.kicad_pcb> <slug>\n")
        return 2
    # Check the file before handing it to pcbnew. `LoadBoard` on a path that
    # does not exist returns a null BOARD rather than raising, and the first
    # thing `collect` does with it is construct a ZONE_FILLER, which
    # dereferences it inside `BOARD::GetDesignSettings()` and takes the whole
    # interpreter down with SIGSEGV. That is not catchable from Python — the
    # `try/except` in `collect` never sees it — and on macOS each occurrence
    # raises a "Python quit unexpectedly" dialog at the user.
    if not os.path.isfile(argv[1]):
        sys.stderr.write("no such board: %s\n" % argv[1])
        return 2
    board = pcbnew.LoadBoard(argv[1])
    if board is None:
        sys.stderr.write("pcbnew could not load: %s\n" % argv[1])
        return 2
    data = collect(board)
    if not os.path.isdir(TRUTH_DIR):
        os.makedirs(TRUTH_DIR)
    path = os.path.join(TRUTH_DIR, "%s.truth.json" % argv[2])
    with open(path, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    sys.stderr.write("wrote %s (%d zones, %d nets, %d pads)\n" % (
        path, len(data["zone_area"]), len(data["track_len"]), len(data["pad_area"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
