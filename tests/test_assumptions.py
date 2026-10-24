"""The places where code used to assume something a person would have known.

Each test here names a case where the tool was quietly deciding rather than
measuring, and asserts that it now either measures or says so. They are grouped
in one file because they share that shape, not because they share a module.
"""

from __future__ import annotations

import json
import shutil

import pytest

from pcbreview import netclass, parts, pipeline
from pcbreview.report import coverage


# ---------------------------------------------------------------------------
# The parts table stopped being a designator regex
# ---------------------------------------------------------------------------

class _Component:
    def __init__(self, reference, value="", description="", supplier_part=""):
        self.reference = reference
        self.value = value
        self.description = description
        self.supplier_part = supplier_part
        self.lib_id = value
        self.dnp = False


class _Schematic:
    def __init__(self, components):
        self.components = components


def test_a_dissipating_part_with_no_junction_gets_a_row(monkeypatch):
    """The gap this closes, and it was a real one.

    A power inductor's DCR loss and a shunt's I²R are two of the commonest
    thermal problems on a power board. Under a `^[UQD]\\d` rule neither got a
    row, a question, or a mention — the table's whole job is to say what is
    missing, and it was silently missing them.
    """
    schematic = _Schematic([
        _Component("L3", "4.7uH", "Power Inductor, shielded, 3A"),
        _Component("R47", "5m", "Current sense shunt resistor"),
        _Component("R2", "10k", "Chip resistor, thick film"),
    ])
    rows = {r.reference: r for r in parts.assemble(schematic, [])}

    assert "L3" in rows and "R47" in rows, "a dissipator was dropped"
    assert "R2" not in rows, "an ordinary chip resistor is not a thermal part"

    # Two different doors, and the row says which it came through — an inductor
    # by its stated 3 A rating, a shunt by the words in its description.
    assert "rated at 3 A" in rows["L3"].because
    assert "description says it dissipates" in rows["R47"].because

    for reference in ("L3", "R47"):
        row = rows[reference]
        assert row.junction is False
        # Listed AND said to be unmodelled. Both halves matter: a silence reads
        # as a pass, and a demand for a θJA it cannot have reads as a bug.
        assert any("NO CHECK HERE COMPUTES ITS TEMPERATURE" in b
                   for b in row.blocked)
        assert not any("junction temperature was found" in b
                       for b in row.blocked)


def test_a_semiconductor_is_in_the_table_whatever_its_description_says():
    """A symbol with an empty description is the normal case, and a
    semiconductor with no row is the failure the table exists to prevent."""
    schematic = _Schematic([_Component("U7", "", ""), _Component("Q1", "", "")])
    rows = {r.reference: r for r in parts.assemble(schematic, [])}
    assert set(rows) == {"U7", "Q1"}
    assert all(r.junction for r in rows.values())
    assert all("designator" in r.because for r in rows.values())


def test_a_document_can_be_attached_to_a_part_by_hand(tmp_path):
    """A datasheet whose filename carries no LCSC code reaches a designator
    only if somebody says which part it is. Reading a part number out of the
    document *body* is the guess this refuses to make."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text("", encoding="utf-8")
    assert parts.attachments(board) == {}

    parts.attach(board, {"TPS62840_rev_C.pdf": "U3"})
    assert parts.attachments(board) == {"TPS62840_rev_C.pdf": "U3"}

    # Merged, not replaced — one attachment does not forget another.
    parts.attach(board, {"C845602.pdf": "Q8"})
    assert parts.attachments(board) == {"TPS62840_rev_C.pdf": "U3",
                                        "C845602.pdf": "Q8"}


def test_an_unreadable_attachment_file_is_reported_not_swallowed(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text("", encoding="utf-8")
    parts.attach_path(board).write_text("attach: [not, a, mapping]",
                                        encoding="utf-8")
    assert parts.attachments(board) == {}
    assert "mapping" in (parts.attachment_error(board) or "")
    with pytest.raises(parts.PartsError, match="will not be overwritten"):
        parts.attach(board, {"x.pdf": "U1"})


# ---------------------------------------------------------------------------
# The power-width rule says when it cannot say anything
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("widest,median,state", [
    (0.15, 0.10, "never_fires"),      # nothing on the board is wide enough
    (0.50, 0.25, "always_fires"),     # everything is
    (1.00, 0.15, "discriminates"),    # the ordinary case
    (None, None, "no_routing"),
])
def test_the_width_rule_reports_whether_it_can_discriminate(widest, median,
                                                            state):
    """`POWER_WIDTH_MM` is an absolute number and has two ends to fall off.

    Neither is a reason to move it — it is the user's, and a relative threshold
    would make one net power on one board and not on another. It is a reason to
    say so, because on a fine-pitch board *no* net is classified power on
    geometry and the power-scoped checks then examine nothing while every
    coverage line honestly reports zero.
    """
    assert netclass.width_rule_state(widest, median) == state


@pytest.mark.slow
def test_a_real_board_states_which_end_it_is_on(boards):
    result = pipeline.review_path(boards["simple"].parent)
    assumptions = " ".join(str(a) for a in result.layout.assumptions)
    census = result.layout.net_classes
    assert census["width_rule"] in ("never_fires", "always_fires",
                                    "discriminates", "no_routing")
    if census["width_rule"] in ("never_fires", "always_fires"):
        assert "power threshold" in assumptions or "classified power" in \
            assumptions, "the width rule's blind spot was not reported"


# ---------------------------------------------------------------------------
# The coverage reader says when it cannot read a note
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("note,empty,unread", [
    ("1 fab target(s), 0 net(s) examined", ["0 net(s) examined"], []),
    # The phrasing `_LEADING_COUNT` cannot see, because it is anchored so that
    # a number mid-clause reads as a parameter.
    ("examined 0 of 14 nets", [], ["examined 0 of 14 nets"]),
    # Results, not populations. A DRC that examined the board and found nothing
    # is a pass, and calling it blind is the same failure with the sign flipped.
    ("0 violation(s): 0 error, 0 warning", [], []),
    # Parameters, not counts.
    ("ranked at 1000 ps edges", [], []),
    ("stitch limit 14.3 mm at 0.5 mm", [], []),
    ("14 net(s) examined", [], []),
])
def test_a_zero_the_reader_cannot_classify_is_reported_not_dropped(
        note, empty, unread):
    """This module reads free text, and free text changes.

    A check reworded to "examined 0 of 14 nets" would simply stop appearing in
    section 3. The section would shrink and look better, which is exactly the
    failure it exists to prevent, with nothing anywhere saying so.
    """
    assert coverage._zero_populations(note) == (empty, unread)


# ---------------------------------------------------------------------------
# The root schematic is found, not guessed at
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_a_board_named_differently_from_its_schematic_still_gets_reviewed(
        boards, tmp_path):
    """The old rule was `<board stem>.kicad_sch` or nothing.

    A project whose board and schematic have different stems got **no schematic
    review at all**, reported honestly by rule 0 as "not supplied" — which is
    the safe failure and still the wrong answer.
    """
    source = boards["six_layer"] if "six_layer" in boards else boards["simple"]
    project = tmp_path / "project"
    project.mkdir()
    for sheet in source.parent.glob("*.kicad_sch"):
        shutil.copy2(sheet, project / sheet.name)
    for pro in source.parent.glob("*.kicad_pro"):
        shutil.copy2(pro, project / pro.name)
    shutil.copy2(source, project / "renamed-board.kicad_pcb")

    found = pipeline.discover(project)
    assert found.board.name == "renamed-board.kicad_pcb"
    assert found.schematic is not None, "the schematic was not found"
    assert found.schematic.name == source.with_suffix(".kicad_sch").name


@pytest.mark.slow
def test_an_ambiguous_hierarchy_says_so_rather_than_picking(boards, tmp_path):
    """Two unreferenced sheets and no project file is two designs in a folder,
    or a stray copy. Choosing between them is the user's call."""
    source = boards["simple"]
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(source, project / "renamed-board.kicad_pcb")
    for name in ("one.kicad_sch", "two.kicad_sch"):
        shutil.copy2(source.with_suffix(".kicad_sch"), project / name)

    found = pipeline.discover(project)
    assert found.schematic is None
    assert any("none of them is unambiguously the root" in n
               for n in found.notes), "the reason was not stated"

    # …and naming it is the way out.
    named = pipeline.discover(project, schematic=project / "one.kicad_sch")
    assert named.schematic == project / "one.kicad_sch"


def test_the_project_file_names_the_root_sheet(tmp_path):
    """KiCad records the hierarchy root as the first entry of its `sheets`
    list. Authoritative, and it beats inferring — measured on a real six-layer
    design, 19 sheets on disk had **seven** that nothing else placed."""
    sheets = []
    for name in ("root", "orphan-a", "orphan-b"):
        path = tmp_path / f"{name}.kicad_sch"
        path.write_text("(kicad_sch)", encoding="utf-8")
        sheets.append(path)
    (tmp_path / "proj.kicad_pro").write_text(json.dumps({
        "sheets": [["uuid-1", "root"], ["uuid-2", "orphan-a"]]}),
        encoding="utf-8")

    assert pipeline._root_from_project(tmp_path, sheets) == \
        tmp_path / "root.kicad_sch"


def test_no_project_file_is_no_opinion(tmp_path):
    sheets = [tmp_path / "a.kicad_sch"]
    sheets[0].write_text("(kicad_sch)", encoding="utf-8")
    assert pipeline._root_from_project(tmp_path, sheets) is None


# ---------------------------------------------------------------------------
# A declaration's reason travels with the finding it quietens
# ---------------------------------------------------------------------------

def test_the_users_own_words_reach_the_finding_they_demote():
    """A demotion is the only thing in this tool that lowers a severity, so the
    one place its justification must not be is in a different file."""
    kind = netclass.NetKind(
        net="/TP1", net_class="other", kind=None, evidence="declared",
        confidence=1.0, declared=True, note="test point, probed at bring-up")
    assert kind.note == "test point, probed at bring-up"
    assert kind.full_severity is False
