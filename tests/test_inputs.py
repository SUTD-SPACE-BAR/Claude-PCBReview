"""Files are routed by what they are, never by where they sat.

Board-free by construction: every fixture here is written to a temp directory,
so this ships and guards for the user (§50.9).

The failure it exists to prevent, from the field report of 2026-08-05: an
outside session omitted the stackup — because SKILL.md said it would be found
automatically, which was false — so the schematic slid into the stackup slot
and the YAML loader died on a tab character twelve frames down. Nothing in that
message pointed at the cause, and it cost a full review run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import inputs


# -- what each file is -----------------------------------------------------

def _write(directory: Path, name: str, text: str) -> Path:
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


BOARD = '(kicad_pcb (version 20260206) (generator "pcbnew")\n)\n'
SCHEMATIC = '(kicad_sch (version 20260306) (generator "eeschema")\n)\n'
STACKUP = "name: a stackup\nlayers:\n  - role: copper\n    name: L1\n    thickness_mm: 0.035\n"
THERMAL = "ambient_c: 25\nparts:\n  U1:\n    dissipation_w: 1.0\n"
NETINTENT = 'nets:\n  - match: "/X"\n    net_class: other\n'
POWERPATHS = 'paths:\n  - name: a\n    net: "/X"\n    from: [U1.1]\n    to: [{pads: [J1.1], current_a: 1}]\n'


def test_every_kind_is_identified_from_its_contents(tmp_path):
    assert inputs.classify(_write(tmp_path, "b.kicad_pcb", BOARD)) == "board"
    assert inputs.classify(_write(tmp_path, "s.kicad_sch", SCHEMATIC)) == "schematic"
    assert inputs.classify(_write(tmp_path, "a.stackup.yaml", STACKUP)) == "stackup"
    assert inputs.classify(_write(tmp_path, "a.thermal.yaml", THERMAL)) == "thermal"
    assert inputs.classify(_write(tmp_path, "a.netintent.yaml", NETINTENT)) == "netintent"
    assert inputs.classify(_write(tmp_path, "a.powerpaths.yaml", POWERPATHS)) == "powerpaths"
    assert inputs.classify(_write(tmp_path, "bom.csv", "Reference,Value\n")) == "bom"
    assert inputs.classify(_write(tmp_path, "d.pdf", "%PDF-1.4\n")) == "datasheet"


def test_the_yaml_manifests_are_told_apart_by_their_top_level_key():
    """The whole reason content beats extension. All four are `.yaml`, and a
    stackup in the thermal slot is silently ignored while a thermal manifest in
    the stackup slot fails Gate 1 with an error about missing layers."""
    assert inputs._YAML_SIGNATURES == {"layers": "stackup",
                                       "parts": "thermal",
                                       "nets": "netintent",
                                       "paths": "powerpaths"}


def test_a_renamed_file_is_still_identified(tmp_path):
    """A `.kicad_sch` saved under a board's name is exactly the case a
    position-free router should survive, so the token in the file wins."""
    disguised = _write(tmp_path, "board.kicad_pcb", SCHEMATIC)
    assert inputs.classify(disguised) == "schematic"


def test_an_ambiguous_or_unreadable_file_is_never_guessed_at(tmp_path):
    both = _write(tmp_path, "x.yaml", STACKUP + NETINTENT)
    assert inputs.classify(both) == "unknown"
    assert inputs.classify(tmp_path / "missing.yaml") == "unknown"
    assert inputs.classify(_write(tmp_path, "notes.txt", "hello")) == "unknown"


# -- routing ---------------------------------------------------------------

def test_order_is_irrelevant(tmp_path):
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    schematic = _write(tmp_path, "s.kicad_sch", SCHEMATIC)
    stackup = _write(tmp_path, "a.stackup.yaml", STACKUP)

    forwards = inputs.route([board, stackup, schematic])
    backwards = inputs.route([schematic, stackup, board])
    assert forwards == backwards
    assert Path(forwards["stackup"]).name == "a.stackup.yaml"
    assert Path(forwards["schematic"]).name == "s.kicad_sch"


def test_the_schematic_can_no_longer_land_in_the_stackup_slot(tmp_path):
    """The exact 2026-08-05 failure, pinned."""
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    schematic = _write(tmp_path, "s.kicad_sch", SCHEMATIC)
    routed = inputs.route([board, schematic])
    assert routed["stackup"] is None
    assert Path(routed["schematic"]).name == "s.kicad_sch"


def test_two_files_for_one_slot_are_refused_by_name(tmp_path):
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    one = _write(tmp_path, "one.stackup.yaml", STACKUP)
    two = _write(tmp_path, "two.stackup.yaml", STACKUP)
    with pytest.raises(inputs.InputError) as exc:
        inputs.route([board, one, two])
    assert "one.stackup.yaml" in str(exc.value)
    assert "two.stackup.yaml" in str(exc.value)


def test_a_missing_board_says_what_it_did_get(tmp_path):
    stackup = _write(tmp_path, "a.stackup.yaml", STACKUP)
    with pytest.raises(inputs.InputError) as exc:
        inputs.route([stackup])
    assert "a.stackup.yaml" in str(exc.value)


def test_datasheets_are_collected_and_named_rather_than_parsed(tmp_path):
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    pdf = _write(tmp_path, "part.pdf", "%PDF-1.4\n")
    routed = inputs.route([board, pdf])
    assert routed["datasheets"] == [str(pdf)]
    assert "not read by the auditor" in inputs.describe(routed)


# -- finding a stackup, and refusing two that disagree ---------------------

def test_a_stackup_beside_the_board_is_found_when_none_is_given(tmp_path):
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    beside = _write(tmp_path, "b.stackup.yaml", STACKUP)
    chosen, notes = inputs.resolve_stackup(board)
    assert Path(chosen) == beside.resolve()
    assert any("not given" in note for note in notes)


def test_nothing_is_found_when_there_is_nothing(tmp_path):
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    chosen, notes = inputs.resolve_stackup(board)
    assert chosen is None and notes == []


def test_two_stackups_that_disagree_are_refused_so_the_user_can_be_asked(tmp_path):
    """the ruling, 2026-08-06: *"If a new stackup is given and there was an old
    stackup already, ask the user which one to use, if they arent identical."*

    The engine cannot ask anyone, so it refuses with both paths named and lets
    the front end put the question. On one board here two transcriptions of one
    fab stackup moved crosstalk by a factor of 2.7."""
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    _write(tmp_path, "b.stackup.yaml", STACKUP)
    other = _write(tmp_path, "other.stackup.yaml",
                   STACKUP.replace("0.035", "0.070"))
    with pytest.raises(inputs.InputError) as exc:
        inputs.resolve_stackup(board, other)
    message = str(exc.value)
    assert "ASK THE USER" in message
    assert "b.stackup.yaml" in message and "other.stackup.yaml" in message


def test_two_stackups_that_agree_on_the_physics_are_not_a_conflict(tmp_path):
    """A re-transcription that renames the file, or reorders a key, is the same
    board. Refusing over that would make the check noise."""
    board = _write(tmp_path, "b.kicad_pcb", BOARD)
    _write(tmp_path, "b.stackup.yaml", STACKUP)
    same = _write(tmp_path, "same.stackup.yaml",
                  STACKUP.replace("a stackup", "the same stackup, renamed"))
    chosen, notes = inputs.resolve_stackup(board, same)
    assert Path(chosen) == same
    assert any("identical" in note for note in notes)
