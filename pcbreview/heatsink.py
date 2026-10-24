"""The heatsink a hot part actually has, measured off the board.

`thermal.py` computes ``Tj = Ta + theta_JA * P`` and says on every finding that
``theta_JA`` is a property of *JEDEC's* board and not of yours. This module
measures the difference. It is the other half of that sentence, and the user
asked for it directly (§38, §43): the first question anybody asks a junction
temperature is "yes, but how far is my board from the card they measured on".

Two quantities, both structural and neither of them a temperature:

**Via count inside the thermal pad.** A bottom-cooled package — DFN, QFN, D2PAK,
SON — moves essentially all of its heat through its tab, and the tab can only
reach an inner plane through vias *in* it. Vias ringing the tab conduct through
1 oz copper first, which is a much longer thermal path. This is a pure geometry
question and it needs no thermal data at all.

**Copper attached to the tab, against JESD51-7's card.** JESD51-7 fixes the
board a `theta_JA` is measured on: 76.2 x 114.3 mm, four layers, two of them
**solid one-ounce planes**. That is 8710 mm^2 of plane per layer, and the number
is what makes two vendors' figures comparable. A tab sitting on 120 mm^2 of
1 oz copper is nothing like it, and the quoted `theta_JA` is optimistic by a
factor this module reports as a ratio rather than pretending to convert into
degrees.

Why a ratio and not a temperature
---------------------------------
Converting copper area into `theta_JA` needs a spreading model with a
convection boundary condition, and the honest range on that is wide enough to
be useless — the same 500 mm^2 gives very different answers in still air, in a
sealed enclosure and in a 2 m/s draught. So this reports **what is there**, says
what JEDEC assumed, and leaves the conversion to the human. Every rule in §9
about not inventing numbers applies.

The one thing it is allowed to say firmly is the *direction*: less copper than
JEDEC's card can only make `theta_JA` worse, never better.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from shapely.geometry import Point

from pcbreview.model import Board

#: One plane layer of JESD51-7's 2s2p test card: 76.2 x 114.3 mm of solid
#: one-ounce copper. The figure every datasheet theta_JA is implicitly quoted
#: against, and the only reason a comparison here means anything.
JESD51_7_PLANE_MM2 = 76.2 * 114.3

#: A thermal tab is defined **relative to its own part's pads**, not by an
#: absolute area. An absolute threshold was tried first and refuted by
#: measurement: at 3 mm^2 a TO-263-7's *source leads* (0.9 x 4.0 = 3.6 mm^2)
#: read as tabs, and at 4 mm^2 a VSON-10's exposed pad (3.96 mm^2) stopped
#: reading as one. There is no absolute gap between the two populations.
#:
#: There is an enormous *relative* one, because a tab exists precisely to be
#: bigger than the leads around it. Measured over the packages here:
#:
#:   package        median lead   tab      ratio
#:   TO-263-7            3.60    95.00      26x
#:   DFN-8 5x6           0.66    16.80      25x
#:   VSON-10 3x3         0.24     3.96      16x
#:   RF module 41-pin    1.35    15.20      11x
#:   SOP-8               1.29      —         1x   (no tab, correctly)
#:   SOT-23-6            0.79      —         1x   (no tab, correctly)
#:
#: 3x sits in a gap of nearly 4x with nothing in it. The absolute floor only
#: stops a two-pad part from calling its larger pad a tab.
TAB_MIN_RATIO = 3.0
TAB_MIN_AREA_MM2 = 2.0

#: Below this a tab is starved even by the standards of the packages that use
#: one. Not a derived limit: it is the point at which the attached copper is
#: smaller than the tab's own footprint by more than an order of magnitude,
#: and JEDEC's card is ~100x larger again.
STARVED_COPPER_RATIO = 0.02      # of one JESD51-7 plane layer


@dataclass(frozen=True)
class ThermalPad:
    """One heat-conducting pad and the path it has out of the package."""
    reference: str
    pad_number: str
    net: str
    area_mm2: float
    #: vias on the same net whose centre lies inside the pad outline
    vias_in_pad: int
    #: and within 2 mm of it, which conduct but only after lateral spreading
    vias_within_2mm: int
    nearest_via_mm: float | None
    #: copper on this net in the island touching the pad, summed over layers
    attached_copper_mm2: float
    #: the same, per layer, so a plane connection is distinguishable from a pour
    copper_by_layer: dict
    #: True when the pad itself is a plated through-pad — a via-in-pad
    #: footprint, which is the correct construction and needs no separate vias
    is_plated: bool

    def designator_text(self) -> str:
        return f"{self.reference}.{self.pad_number}"

    @property
    def has_path_to_plane(self) -> bool:
        """A via *in* the tab, or the tab is plated through."""
        return self.is_plated or self.vias_in_pad > 0

    @property
    def jedec_fraction(self) -> float:
        """Attached copper as a fraction of one JESD51-7 plane layer."""
        return self.attached_copper_mm2 / JESD51_7_PLANE_MM2


def _pieces(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in geom.geoms if g.geom_type == "Polygon"]


def thermal_pads(board: Board, references: set[str] | None = None
                 ) -> list[ThermalPad]:
    """Every pad big enough to be a thermal tab, with its path to a plane.

    `references` restricts the search to named parts — pass the manifest's
    designators to look only at parts a datasheet was supplied for. With no
    restriction it examines every large pad on the board, which is the right
    behaviour when there is no manifest yet.

    A multi-pad exposed pad (KiCad's `_ThermalVias` footprint variants split
    one EP into a grid of small plated pads) is merged back into a single
    entry, because the package sees one tab.
    """
    def net_of(code):
        try:
            return board.net(code).name
        except KeyError:
            return None

    grouped: dict[tuple[str, str], list] = {}
    for pad in board.pads:
        if references is not None and pad.ref not in references:
            continue
        if not pad.copper_layers:
            continue
        grouped.setdefault((pad.ref, str(pad.number)), []).append(pad)

    # merged pad area per part pin, then the part's own median, which is what
    # a tab is measured against
    areas: dict[tuple[str, str], float] = {}
    regions: dict[tuple[str, str], object] = {}
    for key, pads in grouped.items():
        region = pads[0].copper
        for pad in pads[1:]:
            region = region.union(pad.copper)
        regions[key] = region
        areas[key] = region.area

    by_part: dict[str, list[float]] = {}
    for (ref, _), area in areas.items():
        by_part.setdefault(ref, []).append(area)
    medians = {ref: statistics.median(v) for ref, v in by_part.items()}

    out: list[ThermalPad] = []
    for (ref, number), pads in sorted(grouped.items()):
        region = regions[(ref, number)]
        median = medians[ref]
        if (region.area < TAB_MIN_AREA_MM2
                or median <= 0
                or region.area / median < TAB_MIN_RATIO):
            continue

        net = net_of(pads[0].net)
        if net is None:
            continue

        on_net = [v for v in board.vias if net_of(v.net) == net]
        inside = sum(1 for v in on_net if region.contains(Point(*v.pos)))
        near = region.buffer(2.0)
        within = sum(1 for v in on_net if near.contains(Point(*v.pos)))
        distances = [region.distance(Point(*v.pos)) for v in on_net]
        nearest = min(distances) if distances else None

        by_layer: dict[str, float] = {}
        for layer in board.copper_layers:
            copper = board.copper_on_layer(layer, net)
            for piece in _pieces(copper):
                if piece.intersects(region):
                    by_layer[layer] = by_layer.get(layer, 0.0) + piece.area
                    break
        out.append(ThermalPad(
            reference=ref, pad_number=number, net=net,
            area_mm2=round(region.area, 3),
            vias_in_pad=inside, vias_within_2mm=within,
            nearest_via_mm=(round(nearest, 3) if nearest is not None else None),
            attached_copper_mm2=round(sum(by_layer.values()), 1),
            copper_by_layer={k: round(v, 1) for k, v in by_layer.items()},
            is_plated=any(p.is_plated_hole for p in pads)))
    return out


def coverage(board: Board, references: set[str] | None = None) -> dict:
    """What this module had to look at. Print it before believing a zero."""
    pads = thermal_pads(board, references)
    return {
        "thermal_pads": len(pads),
        "restricted_to_references": (sorted(references)
                                     if references is not None else None),
        "pads_with_no_path_to_plane": sum(1 for p in pads
                                          if not p.has_path_to_plane),
        "tab_min_ratio": TAB_MIN_RATIO,
        "jesd51_7_plane_mm2": round(JESD51_7_PLANE_MM2, 1),
    }
