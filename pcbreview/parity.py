"""Where the board and the schematic describe different circuits.

KiCad's own ERC and DRC each see one side. The schematic knows what the designer
drew; the board knows what will be manufactured; nothing checks that the two
agree pin by pin, because KiCad assumes they do — it built one from the other.
They stop agreeing when a footprint is swapped for one with a different pad map,
when a pad is netted by hand in the board editor, or when a symbol carries a pin
its footprint has nowhere to put.

Three asymmetries, and only the first two are usually real findings:

* **A pad with no symbol pin behind it.** The board editor lets a pad be
  assigned to a net directly, and the schematic then has no record of that
  connection. Usually deliberate — a thermal pad stitched to ground, a shield
  tab — but it means the netlist the designer reviewed is not the netlist that
  will be fabricated, and there is no other check that says so.
* **A symbol pin with no pad.** The schematic connects the pin to something and
  the footprint has nowhere for it to land, so that connection silently does not
  exist on the board. Worse than the first case, because the drawn circuit and
  the built one differ in a way that reads as connected.
* **A component on one side only.** Fitted parts missing from the layout, or
  footprints on the board that no symbol accounts for.

Both fabricated boards have examples of the first two, and they are the reason
this is worth writing rather than assuming: the four-layer board's buck-boost
converter thermal stitching and card-socket shield tabs are pads with no
symbol pin, and the six-layer board's processor module
carries a 3V3 output pin its footprint does not break out. All are intentional.
A tool that called them defects would be wrong; a tool that could not see them
at all would miss the case where they are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pcbreview.model import Board
from pcbreview.schematic import Schematic


@dataclass
class PinPad:
    """One pin or pad that exists on only one side."""
    designator: str                  # "U12.16"
    reference: str
    number: str
    side: str                        # board_only | schematic_only
    net_name: str | None = None      # the net it carries, on its own side
    pin_name: str | None = None      # the symbol's name for it, where known
    pin_type: str | None = None
    #: other pins on that net — a pin alone on its net connects to nothing
    net_pin_count: int = 0

    @property
    def carries_net(self) -> bool:
        """Whether the asymmetry actually changes the circuit.

        A pad with no net and no symbol pin is a footprint feature the symbol
        does not draw — a castellation, an unused module pad — and connects
        nothing to nothing. A pad *on a net* with no symbol pin is a connection
        that exists on the board and not in the reviewed netlist, which is a
        different claim entirely.
        """
        return bool(self.net_name) and self.net_pin_count > 1

    def __str__(self) -> str:
        where = ("pad with no symbol pin" if self.side == "board_only"
                 else "symbol pin with no pad")
        net = f" on {self.net_name}" if self.net_name else " (no net)"
        name = f" ({self.pin_name})" if self.pin_name else ""
        return f"{self.designator}{name}: {where}{net}"


@dataclass
class ComponentGap:
    """A component present on one side only."""
    reference: str
    side: str                        # board_only | schematic_only
    value: str = ""
    footprint: str = ""
    dnp: bool = False

    def __str__(self) -> str:
        where = ("footprint with no symbol" if self.side == "board_only"
                 else "symbol with no footprint")
        return (f"{self.reference} ({self.value}): {where}"
                + (" [marked do-not-populate]" if self.dnp else ""))


@dataclass
class Parity:
    """Everything the board and the schematic disagree about, pin by pin."""
    pins: list[PinPad] = field(default_factory=list)
    components: list[ComponentGap] = field(default_factory=list)
    net_conflicts: list[str] = field(default_factory=list)
    matched_pins: int = 0
    matched_components: int = 0
    #: footprints carrying no pads at all — logos, fiducial artwork, dimension
    #: marks, courtyard-only decals. Counted rather than reported (see `check`).
    decals: list[str] = field(default_factory=list)

    @property
    def board_only(self) -> list[PinPad]:
        return [p for p in self.pins if p.side == "board_only"]

    @property
    def schematic_only(self) -> list[PinPad]:
        return [p for p in self.pins if p.side == "schematic_only"]

    @property
    def connected(self) -> list[PinPad]:
        """The asymmetries that change the circuit rather than the drawing."""
        return [p for p in self.pins if p.carries_net]

    @property
    def is_clean(self) -> bool:
        return not (self.pins or self.components or self.net_conflicts)

    def summary(self) -> dict:
        return {
            "matched_pins": self.matched_pins,
            "matched_components": self.matched_components,
            "board_only_pads": [p.designator for p in self.board_only],
            "schematic_only_pins": [p.designator for p in self.schematic_only],
            "connected": [p.designator for p in self.connected],
            "component_gaps": [c.reference for c in self.components],
            "net_conflicts": self.net_conflicts,
            "decals": self.decals,
        }

    def report(self) -> str:
        if self.is_clean:
            return (f"board and schematic agree on all {self.matched_pins} pins "
                    f"of {self.matched_components} components")
        lines = [f"{self.matched_pins} pins matched across "
                 f"{self.matched_components} components"]
        for component in self.components:
            lines.append(f"  {component}")
        for pin in self.pins:
            lines.append(f"  {pin}")
        for conflict in self.net_conflicts:
            lines.append(f"  ! {conflict}")
        return "\n".join(lines)


def _schematic_pins(schematic: Schematic) -> dict[str, dict[str, tuple[str, str]]]:
    """``reference -> {pin number: (pin name, pin type)}``.

    Units are merged. A multi-unit part appears once per unit in the netlist,
    and its pads are all on one footprint, so comparing per unit would report
    every pin of every other unit as missing.
    """
    by_reference: dict[str, dict[str, tuple[str, str]]] = {}
    for component in schematic.components:
        pins = by_reference.setdefault(component.reference, {})
        for pin in component.pins:
            pins.setdefault(pin.number, (pin.name, pin.type))
    return by_reference


def _board_pads(board: Board) -> dict[str, dict[str, str]]:
    """``reference -> {pad number: net name}``.

    Numbered pads only: an unnumbered pad is a mechanical feature — a mounting
    hole, a fiducial, a castellation — and has no symbol pin to correspond to
    by construction.
    """
    by_reference: dict[str, dict[str, str]] = {}
    for footprint in board.footprints:
        pads = by_reference.setdefault(footprint.ref, {})
        for pad in footprint.pads:
            if pad.number:
                pads.setdefault(pad.number, pad.net_name)
    return by_reference


def check(board: Board, schematic: Schematic,
          include_dnp: bool = True) -> Parity:
    """Compare the board's pads with the schematic's pins, one component at a time.

    Matching is by designator and pad number, which is the same key KiCad uses
    to push a netlist into a layout, so a disagreement here is a disagreement
    KiCad's own update would have had to resolve.

    ``include_dnp`` keeps do-not-populate parts in the comparison. They are
    reported with the flag set rather than filtered out, because a DNP part with
    no footprint is expected and a DNP part whose footprint is missing from the
    board is still worth seeing.
    """
    schematic_pins = _schematic_pins(schematic)
    board_pads = _board_pads(board)
    net_of_reference = {c.reference: c for c in schematic.components}
    footprint_of = {f.ref: f for f in board.footprints}

    # What the schematic thinks each of its pins is connected to, so a pin with
    # no pad can say which connection the board is missing.
    schematic_net_of: dict[str, tuple[str, int]] = {}
    for net in schematic.nets:
        for pin in net.pins:
            schematic_net_of[f"{pin.reference}.{pin.number}"] = (net.name,
                                                                 len(net.pins))
    board_net_pins: dict[str, int] = {}
    for pad in board.pads:
        if pad.net_name:
            board_net_pins[pad.net_name] = board_net_pins.get(pad.net_name, 0) + 1

    result = Parity()

    for reference in sorted(set(schematic_pins) | set(board_pads)):
        component = net_of_reference.get(reference)
        if component is not None and component.dnp and not include_dnp:
            continue

        if reference not in board_pads:
            result.components.append(ComponentGap(
                reference=reference, side="schematic_only",
                value=component.value if component else "",
                footprint=component.footprint if component else "",
                dnp=bool(component and component.dnp)))
            continue
        if reference not in schematic_pins:
            placed = footprint_of.get(reference)
            # A footprint with no pads at all cannot have a symbol behind it by
            # construction — logos, fiducial artwork, dimension marks,
            # courtyard-only decals. Reporting those as `major` "footprint with
            # no symbol" produced four alarming findings on the first outside
            # board this ever saw and can never be right (§48.3 item 2). Not
            # silent: they are counted on the result and named in the coverage
            # line. None of the boards this grew up on has one, which is
            # exactly why it took an outside board to find.
            if placed is not None and not placed.pads:
                result.decals.append(reference)
                continue
            result.components.append(ComponentGap(
                reference=reference, side="board_only",
                value=placed.value if placed else "",
                footprint=placed.lib_id if placed else ""))
            continue

        result.matched_components += 1
        pins, pads = schematic_pins[reference], board_pads[reference]
        result.matched_pins += len(set(pins) & set(pads))

        for number in sorted(set(pads) - set(pins)):
            net_name = pads[number] or None
            result.pins.append(PinPad(
                designator=f"{reference}.{number}", reference=reference,
                number=number, side="board_only", net_name=net_name,
                net_pin_count=board_net_pins.get(net_name, 0)))
        for number in sorted(set(pins) - set(pads)):
            name, kind = pins[number]
            net_name, pin_count = schematic_net_of.get(
                f"{reference}.{number}", (None, 0))
            result.pins.append(PinPad(
                designator=f"{reference}.{number}", reference=reference,
                number=number, side="schematic_only", pin_name=name,
                pin_type=kind, net_name=net_name, net_pin_count=pin_count))

    # A schematic net spanning two board nets means the two sides have genuinely
    # diverged. It cannot happen while the partition is exact, so surfacing it
    # matters more than the fact that it never fires on these boards.
    from pcbreview.intent import map_to_board
    result.net_conflicts = list(map_to_board(schematic, board).conflicts)
    return result
