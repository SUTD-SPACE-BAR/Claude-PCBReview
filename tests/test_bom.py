"""Tests for the BOM comparison — the third document, and the one built from.

`parity` compares the schematic against the board. This compares it against the
file the assembler reads, which can disagree with both. The motivating case is
real: a buck's catch diode is a 40 V part in the schematic and a **20 V** part
in the BOM, and only the BOM gets ordered.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import bom
from pcbreview.audit import check_bom_parity


class _Component:
    def __init__(self, reference, value, dnp=False):
        self.reference, self.value, self.dnp = reference, value, dnp


class _Schematic:
    def __init__(self, components):
        self.components = components


def _csv(tmp_path, text):
    path = tmp_path / "b.csv"
    path.write_text(text)
    return path


BASIC = """Reference,Qty,Value,LCSC
R1,1,10k,C1234
"C2,C3",2,100nF,C5678
D2,1,SS34,C9999
"""


def test_a_multi_reference_cell_becomes_several_designators(tmp_path):
    """Every exporter groups identical parts onto one line."""
    lines = bom.load(_csv(tmp_path, BASIC))
    assert sorted(lines) == ["C2", "C3", "D2", "R1"]
    assert lines["C3"].value == "100nF"


def test_the_header_is_found_by_alias_not_by_position(tmp_path):
    """KiCad, the Fabrication Toolkit and JLCPCB's template disagree on every
    column name."""
    lines = bom.load(_csv(tmp_path, "Designator,Comment\nU1,ESP32\n"))
    assert lines["U1"].value == "ESP32"


def test_a_file_with_no_reference_column_is_refused(tmp_path):
    """Refusing beats guessing which column holds the designators."""
    with pytest.raises(bom.BomError) as excinfo:
        bom.load(_csv(tmp_path, "Part,Cost\nwidget,1.00\n"))
    assert "reference column" in str(excinfo.value)


def test_rows_that_are_not_designators_are_ignored(tmp_path):
    lines = bom.load(_csv(tmp_path, "Reference,Value\nTOTAL,\n,\nR1,10k\n"))
    assert list(lines) == ["R1"]


# -- the comparison -------------------------------------------------------

def test_a_value_disagreement_is_found(tmp_path):
    """The case this exists for."""
    schematic = _Schematic([_Component("D2", "SS34")])
    result = bom.compare(schematic,
                         bom.load(_csv(tmp_path, "Reference,Value\nD2,1N5820\n")))
    assert result.value_mismatches == [("D2", "SS34", "1N5820")]
    assert not result.is_clean()


def test_values_are_compared_loosely_on_whitespace_and_case(tmp_path):
    schematic = _Schematic([_Component("R1", "10K")])
    result = bom.compare(schematic,
                         bom.load(_csv(tmp_path, "Reference,Value\nR1, 10k \n")))
    assert result.value_mismatches == []


def test_a_part_missing_from_the_bom_is_reported(tmp_path):
    """It will not be ordered and will not be fitted."""
    schematic = _Schematic([_Component("R1", "10k"), _Component("R2", "1k")])
    result = bom.compare(schematic,
                         bom.load(_csv(tmp_path, "Reference,Value\nR1,10k\n")))
    assert result.missing_from_bom == ["R2"]


def test_a_dnp_part_missing_from_the_bom_is_not_a_defect(tmp_path):
    schematic = _Schematic([_Component("R1", "10k"),
                            _Component("R2", "1k", dnp=True)])
    result = bom.compare(schematic,
                         bom.load(_csv(tmp_path, "Reference,Value\nR1,10k\n")))
    assert result.missing_from_bom == []
    assert result.absent_but_marked == ["R2 (DNP)"]


def test_a_bom_line_the_schematic_does_not_have_is_a_stale_export(tmp_path):
    schematic = _Schematic([_Component("R1", "10k")])
    result = bom.compare(schematic,
                         bom.load(_csv(tmp_path,
                                       "Reference,Value\nR1,10k\nR9,4k7\n")))
    assert result.not_in_schematic == ["R9"]


def test_a_part_the_bom_marks_dnp_is_not_value_checked(tmp_path):
    schematic = _Schematic([_Component("R1", "10k")])
    result = bom.compare(schematic, bom.load(
        _csv(tmp_path, "Reference,Value,DNP\nR1,DIFFERENT,yes\n")))
    assert result.value_mismatches == []


# -- the check ------------------------------------------------------------

def test_no_bom_reports_that_rather_than_a_clean_pass():
    finding, = check_bom_parity(_Schematic([]), None)
    assert finding.severity == "info"
    assert "no BOM" in finding.title


def test_a_clean_comparison_produces_only_the_coverage_line(tmp_path):
    schematic = _Schematic([_Component("R1", "10k")])
    result = bom.compare(schematic,
                         bom.load(_csv(tmp_path, "Reference,Value\nR1,10k\n")))
    findings = check_bom_parity(schematic, result)
    assert [f.severity for f in findings] == ["info"]
    assert findings[0].measurement["coverage"]["compared"] == 1


def test_the_three_disagreements_each_get_their_own_finding(tmp_path):
    schematic = _Schematic([_Component("D2", "SS34"), _Component("R2", "1k")])
    result = bom.compare(schematic, bom.load(
        _csv(tmp_path, "Reference,Value\nD2,1N5820\nR9,4k7\n")))
    findings = check_bom_parity(schematic, result)
    titles = " | ".join(f.title for f in findings)
    assert "SS34" in titles and "1N5820" in titles
    assert "not in the BOM" in titles
    assert "the schematic does not have" in titles


# -- discovery (§51) -------------------------------------------------------

def test_the_bom_beside_the_board_is_found_first(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("")
    (tmp_path / "b.csv").write_text("Reference,Value\n")
    (tmp_path / "production").mkdir()
    (tmp_path / "production" / "bom.csv").write_text("Reference,Value\n")
    assert bom.find_bom(board) == tmp_path / "b.csv"


def test_a_fabrication_toolkits_bom_one_directory_down_is_found(tmp_path):
    """`production/bom.csv` is what every KiCad fabrication toolkit writes.
    Only `<board>.csv` was checked, so `bom_parity` reported "no BOM was
    compared" with a perfectly good BOM sitting one directory down."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text("")
    (tmp_path / "production").mkdir()
    (tmp_path / "production" / "bom.csv").write_text("Reference,Value\n")
    assert bom.find_bom(board) == tmp_path / "production" / "bom.csv"


def test_a_board_with_no_bom_anywhere_finds_nothing(tmp_path):
    """The counterpart zero — discovery must not match some unrelated csv."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text("")
    (tmp_path / "positions.csv").write_text("")
    assert bom.find_bom(board) is None
