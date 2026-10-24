"""The extraction seam: one interface, two backends, one of them not yet built.

`Backend.load(board_path) -> pcbreview.model.Board` is the whole interface. It
is small because it should be: everything downstream of it — topology, return
paths, the field solver — already works on the normalised model and neither
knows nor cares how the polygons were obtained.

Backends
--------
* **`PcbnewBackend`** — today. Delegates to `Board.from_kicad`, which shells out
  to KiCad's own Python and caches the JSON. No behaviour of its own, on
  purpose: wrapping it in logic here would be a second place for extraction to
  differ from the one the test suite validates.

* **`PureParserBackend`** — for KiCad 11, which removes SWIG `pcbnew`. Not
  implemented, and deliberately present rather than absent. Exactly two things
  need `pcbnew`: `pad.GetEffectivePolygon()` (roundrect, oval, trapezoid,
  chamfered and custom pad shapes) and `board.GetBoardPolygonOutlines()`
  (chaining Edge.Cuts segments and arcs). Both are testable against
  `tests/kicad_truth.py`, which dumps KiCad's own geometry and is what the
  engine's agreement figures — 8e-16 on outline area, 8e-14 on 3478 pads — are
  measured with. That harness is the acceptance test for this class.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from pcbreview.model import Board, BoardError    # noqa: E402


class ExtractionError(Exception):
    """Raised when a board cannot be turned into a model."""


class Backend(ABC):
    """One way of getting a `Board` out of a `.kicad_pcb`."""

    #: shown in the report and in the app's inputs card
    name: str = "backend"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """`(usable, why not)`. Never raises — this is what picks a backend."""

    @abstractmethod
    def load(self, board_path, **kwargs) -> Board:
        """The normalised model. Raises `ExtractionError` on failure."""


class PcbnewBackend(Backend):
    """KiCad's own Python, via the engine's `extract.py`. The one that runs."""

    name = "pcbnew"

    def __init__(self, kicad_python: str | None = None):
        self.kicad_python = kicad_python

    def available(self) -> tuple[bool, str]:
        try:
            from pcbreview.model import find_kicad_python

            interpreter = find_kicad_python(self.kicad_python)
        except Exception as exc:                        # noqa: BLE001
            return False, str(exc)
        if not Path(interpreter).exists() and shutil.which(interpreter) is None:
            return False, f"KiCad's Python was resolved to {interpreter!r}, " \
                          f"which does not exist"
        return True, f"KiCad's Python at {interpreter}"

    def load(self, board_path, **kwargs) -> Board:
        try:
            return Board.from_kicad(board_path,
                                    kicad_python=self.kicad_python, **kwargs)
        except BoardError as exc:
            raise ExtractionError(str(exc)) from exc


class PureParserBackend(Backend):
    """s-expression parsing with no KiCad. **Not implemented.**

    Named so that "what happens in KiCad 11" has an answer with a location
    rather than being a discovery. See the module docstring for the two
    functions that stand between this and working.
    """

    name = "pure-parser"

    def available(self) -> tuple[bool, str]:
        return False, ("not implemented — needs pad effective polygons and "
                       "Edge.Cuts outline chaining, both validated against "
                       "tests/kicad_truth.py")

    def load(self, board_path, **kwargs) -> Board:
        raise ExtractionError(self.available()[1])


def default_backend(kicad_python: str | None = None) -> Backend:
    """The best backend this machine can use.

    Today that is always `PcbnewBackend`; the ordering exists so that the day
    `PureParserBackend` works, KiCad 11 users get it without a code change
    here, and KiCad 9/10 users keep the backend the test suite validates.
    """
    for backend in (PcbnewBackend(kicad_python), PureParserBackend()):
        usable, _ = backend.available()
        if usable:
            return backend
    raise ExtractionError(
        "no extraction backend is available. "
        + "; ".join(f"{b.name}: {b.available()[1]}"
                    for b in (PcbnewBackend(kicad_python), PureParserBackend())))
