"""Board-free parity tests — the fabricated cases.

`test_parity.py` asserts on two specific designs and is held back from the
shipped zip for that reason. These need no board at all: they build the four
attributes `parity.check` reads out of plain objects, which is exactly what
lets them ship and guard the behaviour for a user reviewing their own board.

The behaviour they guard came from outside this repository. Every design here
happened to have no pad-less footprints, so `parity` reported logos as
`major` "footprint with no symbol" for as long as it has existed, and nothing
here could ever have caught it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pcbreview import parity


class _Pad:
    def __init__(self, number="", net_name=None):
        self.number, self.net_name = number, net_name


class _Footprint:
    def __init__(self, ref, value, lib_id, pads=()):
        self.ref, self.value, self.lib_id = ref, value, lib_id
        self.pads = list(pads)


class _FakeBoard:
    """The four fields `parity.check` reads. No KiCad, no geometry."""

    def __init__(self, footprints):
        self.footprints = list(footprints)
        self.pads = [p for f in self.footprints for p in f.pads]
        self.nets = {}
        self.connectivity = {"pads_by_net": {}}


class _SchPin:
    def __init__(self, reference, number, name="", kind="passive"):
        self.reference, self.number = reference, str(number)
        self.name, self.type = name, kind
        self.key = f"{reference}.{self.number}"


class _Component:
    def __init__(self, reference, pins, value="", footprint="", dnp=False):
        self.reference, self.pins = reference, list(pins)
        self.value, self.footprint, self.dnp = value, footprint, dnp


class _FakeSchematic:
    def __init__(self, components):
        self.components = list(components)
        self.nets = []


def test_a_footprint_with_no_pads_is_not_a_missing_symbol():
    """A logo, a fiducial or a dimension mark cannot have a symbol behind it by
    construction — there is no pin to correspond to.

    Reported as `major` "footprint with no symbol", four LOGO decals were four
    of the alarming findings on the first board outside this repository that
    the skill ever saw. None of the boards here has a pad-less footprint, which
    is why seven designs never caught it.
    """
    board = _FakeBoard([
        _Footprint("G1", "LOGO", "LOGO"),
        _Footprint("REF**", "", "Fiducial"),
        _Footprint("R1", "10k", "R_0402",
                   [_Pad("1", "/VCC"), _Pad("2", "/OUT")]),
    ])
    schematic = _FakeSchematic([
        _Component("R1", [_SchPin("R1", 1), _SchPin("R1", 2)], value="10k"),
    ])
    result = parity.check(board, schematic)
    assert result.decals == ["G1", "REF**"]
    assert result.components == []
    assert result.matched_components == 1 and result.matched_pins == 2


def test_a_footprint_that_does_have_pads_is_still_reported():
    """The counterpart zero: the check this is narrowing must still fire on a
    part that is genuinely on the board and not on the schematic."""
    board = _FakeBoard([_Footprint("U9", "ESP32", "MOD",
                                   [_Pad("1", "/EN"), _Pad("2", None)])])
    result = parity.check(board, _FakeSchematic([]))
    assert result.decals == []
    assert [c.reference for c in result.components] == ["U9"]
    assert result.components[0].side == "board_only"
