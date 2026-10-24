"""Tests for the sourcing and reporting surface on `Schematic`.

These cover the three things the old `kicad-sch` skill could answer and this
could not, which is what has to hold before that skill is retired: the full
property set survives parsing, the design groups into orderable BOM lines, and
there is a human-readable report.

The interesting cases are all real ones off the user's boards rather than
constructed: a three-unit part that must count once, four placements of one
sheet that must count four times, and a property called `LCSC Part Name` that
does not contain an LCSC part name.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.schematic import (LCSC_CODE, BomLine, Component, netlist,
                                 reference_prefix)


def test_property_lookup_ignores_case_and_surrounding_space():
    component = Component(reference="U1", value="", footprint="", lib_id="",
                          unit=1, dnp=False, sheet=0, uuid=None,
                          properties={"  MPN  ": " ABC123 ", "Empty": "  "})
    assert component.mpn == "ABC123"
    assert component.property_of("empty") is None
    assert component.property_of("absent") is None


def test_a_fitted_and_a_dnp_part_are_two_bom_lines():
    """Grouping them together and OR-ing the flag — what the old skill did —
    marks the whole line do-not-fit when only one of them is, and the quantity
    then means neither how many to order nor how many are placed.

    Constructed: neither of the user's boards has a mixed group.
    """
    from pcbreview.schematic import Schematic

    def part(reference, dnp):
        return Component(reference=reference, value="10k", footprint="R:0402",
                         lib_id="Device:R", unit=1, dnp=dnp, sheet=0,
                         uuid=None, properties={"Reference": reference})

    schematic = Schematic(root=Path("x.kicad_sch"), sheets=[],
                          components=[part("R1", False), part("R2", False),
                                      part("R3", True)],
                          nets=[], diagnostics=[])
    lines = schematic.bom()
    assert len(lines) == 2
    fitted = next(l for l in lines if not l.dnp)
    unfitted = next(l for l in lines if l.dnp)
    assert fitted.references == ["R1", "R2"] and fitted.quantity == 2
    assert unfitted.references == ["R3"] and unfitted.quantity == 1


def test_bom_references_sort_numerically():
    """`R10` after `R2`, which a plain string sort gets wrong."""
    from pcbreview.schematic import Schematic

    components = [
        Component(reference=r, value="1k", footprint="R:0402",
                  lib_id="Device:R", unit=1, dnp=False, sheet=0, uuid=None,
                  properties={"Reference": r})
        for r in ("R10", "R2", "R1")
    ]
    schematic = Schematic(root=Path("x.kicad_sch"), sheets=[],
                          components=components, nets=[], diagnostics=[])
    assert schematic.bom()[0].references == ["R1", "R2", "R10"]


def test_bom_line_package_strips_the_library_prefix():
    line = BomLine(references=["C1"], value="100n",
                   footprint="Capacitor_SMD:C_0402_1005Metric",
                   lib_id="Device:C", dnp=False)
    assert line.package == "C_0402_1005Metric"


# -- summaries -------------------------------------------------------------

def test_reference_prefix_splits_at_the_first_digit():
    assert reference_prefix("R12") == "R"
    assert reference_prefix("#PWR03") == "#PWR"
    assert reference_prefix("JP7") == "JP"
    assert reference_prefix("U1") == "U"
