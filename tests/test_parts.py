"""The Parts table and the manifest writer.

The measurement this file pins down is `test_a_real_board_can_write_nothing
_yet_and_says_why`. On the user's own board the deterministic path completes
**no** manifest entries, and the reasons differ per part — scanned, no θJA, no
datasheet at all, and one document that reads perfectly and belongs to nothing
on the board. Every one of those is a different question to ask a person, which
is the design brief §2.10's fourth principle and the whole point of the table.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbreview import datasheets, parts                        # noqa: E402

from pcbreview import thermal as thermal_module              # noqa: E402
from pcbreview.schematic import netlist                     # noqa: E402




def a_reading(lcsc="C1000001", mpn="PART", ja=36.0, jc=6.5, tj=150.0,
              readable=True, why_not="") -> datasheets.Reading:
    reading = datasheets.Reading(
        path=f"{lcsc}.pdf", sha256="d" * 64, pages=1, characters=100,
        readable=readable, why_not=why_not)
    reading.identity = datasheets.Identity(lcsc=lcsc, mpn=mpn, package="DFN-8")
    reading.theta_ja_c_per_w, reading.theta_jc_c_per_w = ja, jc
    reading.t_junction_max_c = tj
    for name, value in (("theta_ja_c_per_w", ja), ("theta_jc_c_per_w", jc),
                        ("t_junction_max_c", tj)):
        if value is not None:
            reading.cites[name] = datasheets.Cite(
                "table", 1, f"{name} {value}").as_dict()
    return reading


class _Component:
    """Enough of a schematic component for `thermal._lcsc_code`.

    It reads `supplier_part`, and only accepts `C` plus three or more digits —
    so the stub has to carry a realistic code, not a short placeholder.
    """

    def __init__(self, reference, value, lcsc=None):
        self.reference, self.value, self.dnp = reference, value, False
        self.lib_id = "Device:Q"
        self.supplier_part = lcsc or ""
        self.description = ""


class _Schematic:
    def __init__(self, components):
        self.components = components


def _entry_lines(document: str) -> list[str]:
    """Just the emitted entries.

    The document's header explains `dissipation_stated: false` in prose, so a
    substring test over the whole file would match the explanation rather than
    the data. What is being asserted is what was *written for the part*.
    """
    body = document.split("parts:", 1)[1]
    return [line for line in body.splitlines() if line.startswith("    ")]


# ---------------------------------------------------------------------------
# A real board, whichever one is installed
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_a_real_board_accounts_for_every_part_and_every_document(boards,
                                                                 monkeypatch):
    """Nothing is silently dropped, in either direction.

    Two counts, and they are different questions. Every semiconductor on the
    board gets a row whether or not a document was found for it, and every
    document gets a row whether or not anything on the board carries its LCSC
    code. A part with no datasheet reads as *no datasheet was supplied*; a
    document belonging to no designator is listed with that as its reason.

    The board is whichever one the registry installed, so this asserts the
    property rather than a count that belongs to one design.
    """
    monkeypatch.setenv(datasheets.CACHE_ENV, "off")
    board = boards["simple"]
    schematic_path = board.with_suffix(".kicad_sch")
    if not schematic_path.exists():
        pytest.skip("this board has no schematic beside it")

    schematic = netlist(str(schematic_path))
    readings = datasheets.ingest(str(board))
    rows = parts.assemble(schematic, readings)

    designators = {r.reference for r in rows if not r.reference.startswith("(")}
    assert designators, "a board with no semiconductors proves nothing here"

    # Every row is either writable or carries a reason it is not. A row that
    # is neither is the silent drop this table exists to prevent.
    for row in rows:
        assert row.ready or row.blocked, f"{row.reference} says nothing"

    # Every document that was read is accounted for by exactly one row.
    read = [r for r in readings if r.identity and r.identity.lcsc]
    for reading in read:
        owners = [r for r in rows if r.lcsc == reading.identity.lcsc]
        assert owners, f"{reading.identity.lcsc} was read and then dropped"

    if not any(r.ready for r in rows):
        with pytest.raises(parts.PartsError, match="no part in this project"):
            parts.document(rows)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_a_part_with_a_complete_datasheet_is_ready():
    schematic = _Schematic([_Component("U1", "REG", "C1000001")])
    rows = parts.assemble(schematic, [a_reading("C1000001", "REG-3V3")])
    assert len(rows) == 1 and rows[0].ready
    assert rows[0].mpn == "REG-3V3" and rows[0].theta_ja_c_per_w == 36.0


def test_a_symbol_with_no_lcsc_field_says_that_is_why():
    schematic = _Schematic([_Component("U1", "REG")])
    row = parts.assemble(schematic, [a_reading()])[0]
    assert not row.ready
    assert "no LCSC code" in row.blocked[0]


def test_passives_are_not_in_the_table():
    """A resistor does not have a junction."""
    schematic = _Schematic([_Component("R1", "10k", "C1000001"),
                            _Component("C1", "100nF", "C1000001"),
                            _Component("U1", "REG", "C1000001")])
    assert [r.reference for r in parts.assemble(schematic, [])] == ["U1"]


def test_one_row_per_designator_not_per_placement():
    schematic = _Schematic([_Component("U1", "REG", "C1000001"),
                            _Component("U1", "REG", "C1000001")])
    assert len(parts.assemble(schematic, [])) == 1


def test_an_existing_manifest_entry_is_carried_through(tmp_path):
    manifest_path = tmp_path / "b.thermal.yaml"
    manifest_path.write_text(
        "ambient_c: 25.0\nparts:\n  U1:\n    part: REG\n"
        "    theta_ja_c_per_w: 52.0\n    t_junction_max_c: 125.0\n"
        "    dissipation_w: 0.62\n    source: 'test'\n", encoding="utf-8")
    manifest = thermal_module.load(manifest_path)
    schematic = _Schematic([_Component("U1", "REG", "C1000001")])
    row = parts.assemble(schematic, [a_reading("C1000001")], manifest)[0]
    assert row.in_manifest and row.dissipation_w == 0.62


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def test_an_unknown_dissipation_is_declared_not_guessed_at_zero():
    """`thermal.py`: a missing dissipation would read as 0 W and report a
    comfortable junction temperature. The third state says so instead."""
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001")])
    entry = _entry_lines(parts.document(rows))
    assert "    dissipation_stated: false" in entry
    assert not any(l.startswith("    dissipation_w:") for l in entry)


def test_a_known_dissipation_is_written_as_a_number():
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001")])
    entry = _entry_lines(parts.document(rows, dissipation={"U1": 0.62}))
    assert "    dissipation_w: 0.62" in entry
    assert not any("dissipation_stated" in l for l in entry)


def test_the_source_names_the_document_the_page_and_the_stage():
    """A model's proposal and a checked regex match are not equally strong,
    and whoever audits the file is entitled to know which they have."""
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001", "REG-3V3")])
    source = parts._source(rows[0])
    assert "REG-3V3" in source and "C1000001.pdf" in source
    assert "p.1" in source and "[table]" in source


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_what_is_written_is_loadable_by_the_engine(tmp_path):
    """The clause that matters: a manifest this wrote that the audit refused
    would be worse than not writing one."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text("x", encoding="utf-8")
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001", "REG-3V3")])
    outcome = parts.write(board, rows)

    loaded = thermal_module.load(outcome["path"])
    assert outcome["parts"] == ["U1"] and outcome["replaced"] is None
    entry = loaded.parts["U1"]
    assert entry.theta_ja_c_per_w == 36.0
    assert entry.t_junction_max_c == 150.0
    assert entry.dissipation_stated is False
    assert "p.1" in entry.source


def test_an_existing_manifest_is_never_replaced_without_being_asked(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("x", encoding="utf-8")
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001")])
    parts.write(board, rows)
    with pytest.raises(parts.PartsError, match="already exists"):
        parts.write(board, rows)


def test_overwriting_backs_the_old_one_up_first(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("x", encoding="utf-8")
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001")])
    parts.write(board, rows)
    before = parts.destination(board).read_text(encoding="utf-8")
    outcome = parts.write(board, rows, overwrite=True)
    assert outcome["replaced"]
    assert Path(outcome["replaced"]).read_text(encoding="utf-8") == before


def test_it_refuses_to_write_over_a_manifest_that_will_not_load(tmp_path):
    """That file is somebody's work in progress and this has no idea what
    they meant."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text("x", encoding="utf-8")
    parts.destination(board).write_text(
        "parts:\n  U1:\n    theta_ja_c_per_w: 52.0\n", encoding="utf-8")
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001")])
    with pytest.raises(parts.PartsError, match="does not load"):
        parts.write(board, rows, overwrite=True)
    # And it left the file exactly as it found it.
    assert "theta_ja_c_per_w: 52.0" in parts.destination(board).read_text(
        encoding="utf-8")


def test_nothing_partial_is_left_behind_when_validation_fails(tmp_path,
                                                              monkeypatch):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("x", encoding="utf-8")
    rows = parts.assemble(_Schematic([_Component("U1", "REG", "C1000001")]),
                          [a_reading("C1000001")])
    monkeypatch.setattr(parts, "document",
                        lambda *a, **k: "parts:\n  U1:\n    part: broken\n")
    with pytest.raises(parts.PartsError, match="does not load"):
        parts.write(board, rows)
    assert not parts.destination(board).exists()
    assert not list(tmp_path.glob("*.partial"))
