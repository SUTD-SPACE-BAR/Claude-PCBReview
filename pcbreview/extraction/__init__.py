"""Getting geometry out of a KiCad board, behind an interface.

Why an interface at all
-----------------------
KiCad 9 and 10 both ship the SWIG `pcbnew` module and `extract.py` uses it.
**KiCad 11 removes it.** Two things still genuinely need it —
`pad.GetEffectivePolygon()` for roundrect/oval/trapezoid/chamfered/custom pads,
and `board.GetBoardPolygonOutlines()` for chaining Edge.Cuts segments and arcs
— and both are replaceable by a pure parser validated against
`tests/kicad_truth.py`. That is not day-one work; having somewhere to put it is.

So: `Backend` is the seam. `PcbnewBackend` is what runs today and delegates to
the engine, unchanged. `PureParserBackend` is a named, documented hole rather
than a surprise in 2027.

`refill.py` is separate from either backend because it is not extraction: it is
a preparation step over the *file*, and it works headlessly on both KiCad
versions.
"""

from pcbreview.extraction.backend import (Backend, ExtractionError, PcbnewBackend,
                                       PureParserBackend, default_backend)
from pcbreview.extraction.refill import (RefillResult, kicad_cli, refill_to_temp)

__all__ = ["Backend", "PcbnewBackend", "PureParserBackend", "default_backend",
           "ExtractionError", "RefillResult", "refill_to_temp", "kicad_cli"]
