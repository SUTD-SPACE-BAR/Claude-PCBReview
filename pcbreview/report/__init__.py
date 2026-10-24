"""The deterministic report generator.

`SKILL.md` §"What you MUST report" turned into code. What appears in a review
is decided here and nowhere else; a model may reorder it and write prose over
it, and may not add to it or take from it.

    from pcbreview.report import generate, render

    report = generate(layout_audit, schematic_audit)
    print(render.text(report))
"""

from pcbreview.report.generate import (GROUP_MIN, GROUP_SEVERITIES, SEVERE,
                                    generate)
from pcbreview.report.model import (LAYOUT, SCHEMATIC, CoverageGap, EntryPoint,
                                 FindingRef, GroupRef, MeasuredGap, RenderPlan,
                                 Report)

__all__ = [
    "generate", "GROUP_MIN", "GROUP_SEVERITIES", "SEVERE",
    "Report", "FindingRef", "GroupRef", "CoverageGap", "MeasuredGap",
    "EntryPoint", "RenderPlan", "LAYOUT", "SCHEMATIC",
]
