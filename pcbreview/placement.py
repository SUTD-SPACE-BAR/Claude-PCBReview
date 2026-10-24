"""Placement checks — where parts sit relative to each other and to the planes.

Everything here asks a question that the netlist cannot answer and the routed
copper can. `schematic_audit.check_decoupling` asks whether a rail has a
capacitor at all; this module asks how big a loop the current has to go round
to use it.

The one idea the whole module turns on
--------------------------------------
**A decoupling loop is set by via positions, not by how close the capacitor
looks to the pin.** Every other tool measures centre-to-centre distance from
the capacitor to the power pin, and on a planed board that is close to
meaningless: the current does not run across the surface from one to the other,
it dives to the plane and comes back up. Two capacitors sitting the same 1.5 mm
from a pin can differ by a factor of five in loop area depending on where their
ground vias are.

So the loop is traced through the geometry that exists:

    IC power pad → capacitor's power pad → through the capacitor
                 → capacitor's ground pad → its ground via → the plane
                 → the IC's ground via → back up to the IC's ground pad

and the enclosed area is that surface run times the depth to the plane. The
depth comes from the bound stackup, so it is the real dielectric height and not
a nominal.

    A = h · L_surface

`reference.py` already establishes the premise this rests on: return current
follows the plane directly beneath the outbound path. So the loop is a ribbon
of width `h` following the surface run, and its area is the product.

What is measured exactly and what is not
----------------------------------------
Exactly: every pad and via position, and `h` from the stackup.

Not exactly: each hop of the surface run is a **straight line between real
features**, not the length of the copper joining them. Copper wanders, so every
loop area here is a *lower bound*. That is stated on every finding. It is still
a large improvement on centre-to-centre distance, because the via positions —
the thing that actually sets the loop — are real.

Why severity is relative to the board's own median
--------------------------------------------------
Turning a loop area into henries needs a conductor width, and turning henries
into a verdict needs the capacitor's own ESL, which is a property of the part
and not of the layout. Neither is available, and inventing them would be
exactly the kind of confident nonsense the stackup gate exists to prevent.

So the ranked quantity is **how many times the board's own median loop this
loop is**. A designer who achieved 0.4 mm² on twelve capacitors and 4 mm² on
the thirteenth has told us what was achievable here, and the thirteenth is the
finding. The absolute area is reported beside it, and there is an absolute
backstop for a board where everything is badly placed — see
`DECOUPLING_ABSOLUTE_MAJOR_MM2`, which is a judgement call and is listed as one.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from pcbreview import coupling as coupling_module
from pcbreview import intent as intent_module
from pcbreview.findings import Finding, _severity
from pcbreview.model import Board, Pad, Via
from pcbreview.schematic import Schematic

# ---------------------------------------------------------------------------
# Thresholds
#
# As in `audit.py`: reporting thresholds on computed quantities, gathered here
# so the whole set can be argued with in one place.
# ---------------------------------------------------------------------------

#: A zone counts as a plane if it pours over this much of the board.
PLANE_AREA_FRACTION = 0.20

#: Decoupling loop area, as a multiple of the median loop on the same board.
#: Relative because the absolute limit depends on the capacitor's ESL, which is
#: a property of the part. A board's own best-placed capacitors say what was
#: achievable on it.
DECOUPLING_RELATIVE_MINOR = 3.0
DECOUPLING_RELATIVE_MAJOR = 6.0

#: How many loops a *relative* comparison needs before its median means
#: anything. Three is the floor for a median to have a middle — the same
#: reasoning as `current.MIN_TERMINALS`, and the same trap: below it this check
#: used to return an empty list and say nothing at all.
DECOUPLING_MIN_POPULATION = 3

#: Backstop for a board where every capacitor is badly placed, so the relative
#: test has nothing to compare against. **A judgement call, not derived.** For
#: scale: both fabricated boards here sit between 0.31 and 2.30 mm², so this is
#: an order of magnitude above anything either of them does.
DECOUPLING_ABSOLUTE_MAJOR_MM2 = 25.0

#: A regulator's feedback node is high impedance and its switch node swings the
#: full input voltage in nanoseconds. Coupling between them is the classic buck
#: instability.
#:
#: Measured as a **coupled run length with a parallelism test**, not as a
#: minimum distance, and that is not a refinement — the first version used
#: minimum distance and reported the six-layer board's U10 at 0.292 mm, which is the pin pitch
#: of the package. At an IC's own pads every net is within half a millimetre of
#: every other, so a proximity test fires on every regulator ever laid out.
#: `coupling.py` already solved this problem for crosstalk; this reuses it.
FEEDBACK_TO_SWITCH_GAP_MM = 0.50
FEEDBACK_RUN_MINOR_MM = 1.0
FEEDBACK_RUN_MAJOR_MM = 3.0

#: How far the divider may sit from the feedback pin before the node is picking
#: up more than it needs to.
FEEDBACK_DIVIDER_MINOR_MM = 5.0
FEEDBACK_DIVIDER_MAJOR_MM = 15.0

#: A Kelvin sense tap should land on the shunt's own pad. Beyond this it is
#: sampling the load current's own IR drop in the copper.
KELVIN_OFFSET_MINOR_MM = 0.30
KELVIN_OFFSET_MAJOR_MM = 1.00

#: Vacuum permeability, H/mm. Loop inductance is a magnetic quantity and the
#: dielectric does not enter it.
MU0_H_PER_MM = 4e-7 * math.pi * 1e-3      # 4*pi*1e-10 H/mm

#: Effective width of the current ribbon, used only for the loop-inductance
#: *estimate*. See `loop_inductance_nh` for why this exists and why the number
#: it produces is labelled an estimate everywhere it appears.
NOMINAL_RIBBON_WIDTH_MM = 0.5

#: How many two-terminal parts a sense connection may pass through on its way
#: to the shunt. Two covers the common input-filter topology (one series
#: resistor per leg, as on the six-layer board's sense amplifier). Deeper
#: walks start reaching parts
#: that are not on the sense path at all.
SENSE_TRACE_DEPTH = 2

#: Pin-name patterns, from manufacturers' symbols rather than net names — the
#: same principle as `intent._PIN_PAIR_PATTERNS`.
#: `SENSE` on its own is deliberately absent. It matched `U8.6` on the six-layer board, which
#: is the anode-sense pin of an LM74700 ideal-diode controller and not a
#: regulator feedback node at all. `VSENSE` is what TI's switchers use and is
#: specific enough.
FEEDBACK_PIN = re.compile(r"^(FB|VFB|FEEDBACK|VSENSE|ADJ)[_ ]?\d*$", re.I)
#: `BOOT` is deliberately absent too. It is a bootstrap node, it swings, and it
#: always sits a pin pitch from everything else on the package — including it
#: made every buck on the board a finding.
SWITCH_PIN = re.compile(r"^(SW|LX|PH|SWITCH|VLX)[_ ]?\d*$", re.I)
SENSE_PIN = re.compile(r"^(IN\+|IN-|VIN\+|VIN-|VSP|VSN|S\+|S-|SENSE\+|SENSE-)$",
                       re.I)
#: A pin named bare `+` or `-`. Useless on its own — a high-current battery
#: connector and a coin-cell holder both have one of each — so it is only
#: believed on a part whose *value* names a current-sense amplifier family,
#: below.
BARE_SENSE_PIN = re.compile(r"^[+-]$")
#: Current-shunt-amplifier families, anchored, from manufacturers' part
#: numbers. This exists because a motor-controller board carrying **eight
#: shunt amplifiers reported `sense_pins: 0`** — the exact topology
#: `check_kelvin_sense` was written for — since that symbol names its inputs
#: bare `+` and `-` rather than `IN+`/`IN-`. The check reported honestly that
#: it had nothing to measure, which was the right behaviour and still meant the
#: check earned nothing on the one board that should have exercised it.
#:
#: **Anchored for the reason §38's prefix list records**: unanchored, a
#: three-letter family prefix matches inside unrelated part numbers.
#: Value-and-pin-count rather than pin-name, so it is kept deliberately narrow
#: and every finding says which of the two routes identified the part.
#:
#: Measured over all eight designs here: it adds exactly those eight amplifiers
#: and nothing else. The only other parts anywhere with both a `+` and a `-`
#: pin are a two-pin battery connector and a three-pin coin-cell holder, and
#: both are rejected by the family test alone — `MIN_SENSE_AMP_PINS` is belt
#: and braces.
SENSE_AMP_FAMILY = re.compile(
    r"\bINA\d{3}|\bZXCT\d{3,4}|\bMAX40\d{3}|\bMAX99\d{2}|\bLTC?61\d{2}|"
    r"\bAD82\d{2}|\bTSC10\d|\bLMP8\d{3}|\bADM407\d|\bCC661\d", re.I)
#: A real current-sense amplifier has a supply, a reference and an output
#: besides its two inputs. A two-pin part whose value happens to match is a
#: connector, not an amplifier.
MIN_SENSE_AMP_PINS = 4
CRYSTAL_REFERENCE = re.compile(r"^(X|Y|XTAL)\d+$", re.I)
#: An ESD/TVS part, from its value or description. Weak evidence, and the
#: finding says so.
ESD_HINT = re.compile(r"esd|tvs|pesd|usblc|srv05|clamp|transient", re.I)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def loop_inductance_nh(area_mm2: float,
                       width_mm: float = NOMINAL_RIBBON_WIDTH_MM) -> float:
    """A labelled *estimate* of a current loop's inductance, in nH.

    **This is an estimate and every caller says so.** It is offered because a
    number in henries is easier to reason about than one in mm², not because it
    is as trustworthy as the area beside it.

    The model: a loop enclosing area `A` between a conductor and a plane `h`
    below it is a flattened rectangle of length `A/h`, and for a wide, close
    return path the partial-inductance result reduces to

        L ≈ mu0 · A / w

    with `w` the width of the current ribbon. The area is measured exactly; `w`
    is not, and the whole uncertainty lives there. It is taken as a nominal
    0.5 mm because that is the order of a decoupling capacitor's pad, and the
    dependence is **linear**, so a trace half that wide doubles the answer.

    Why this is not promoted to a measurement
    -----------------------------------------
    Handoff §5 rules that inductance is a property of a *current loop*, not of
    a via, and that every "1 nH per mm" rule of thumb online drops the mutual
    terms. This function does have a real loop, so it is not that mistake — but
    the exact answer needs the loop's conductor geometry along its whole length
    and a proper partial-inductance sum with the mutual terms kept, which is a
    different calculation from the one that produced the area.

    Treat it as good to a factor of about two. Rank on the area.
    """
    if width_mm <= 0:
        raise ValueError("ribbon width must be positive")
    return MU0_H_PER_MM * area_mm2 / width_mm * 1e9


# ---------------------------------------------------------------------------
# Board context shared by the checks
# ---------------------------------------------------------------------------

@dataclass
class PlacementContext:
    """Everything the checks need resolved once rather than per finding."""
    board: Board
    schematic: Schematic
    ground_nets: set[str]
    supply_nets: set[str]
    plane_layers: list[str]
    #: KiCad copper layer -> depth from the board's top face, in mm
    layer_depth: dict[str, float]
    pads_by_ref: dict[str, list[Pad]] = field(default_factory=dict)

    def pads_of(self, reference: str) -> list[Pad]:
        return self.pads_by_ref.get(reference, [])

    def pad(self, reference: str, number: str) -> Pad | None:
        for pad in self.pads_of(reference):
            if pad.number == number:
                return pad
        return None

    def nearest_via(self, net: int | str,
                    point: tuple[float, float]) -> tuple[float, Via] | None:
        """Closest via on a net to a point. `None` when the net has none."""
        best: tuple[float, Via] | None = None
        for via in self.board.vias_on_net(net):
            gap = _distance(via.pos, point)
            if best is None or gap < best[0]:
                best = (gap, via)
        return best

    def depth_to_plane(self, layer: str) -> float | None:
        """Distance from a copper layer to the nearest ground plane below or
        above it. `None` when this layer is the only ground plane."""
        if layer not in self.layer_depth:
            return None
        here = self.layer_depth[layer]
        depths = [abs(self.layer_depth[plane] - here)
                  for plane in self.plane_layers
                  if plane != layer and plane in self.layer_depth]
        return min(depths) if depths else None

    def net_copper(self, net_name: str):
        """Union of every piece of copper on a net, across all layers."""
        from shapely.ops import unary_union
        try:
            code = self.board.net(net_name).code
        except KeyError:
            return None
        pieces = [t.copper for t in self.board.tracks_on_net(code)]
        pieces += [p.copper for p in self.board.pads_on_net(code)]
        pieces += [v.pad_on(v.layers[0]) for v in self.board.vias_on_net(code)
                   if v.layers]
        pieces = [g for g in pieces if g is not None and not g.is_empty]
        return unary_union(pieces) if pieces else None


def build_context(board: Board, schematic: Schematic) -> PlacementContext:
    """Resolve planes, rails and pad lookups once for the whole run.

    Ground comes from `intent.infer` — decoupling topology, not net names — so
    a board whose ground rail is called `0V` still works.
    """
    if board.stackup is None:
        raise ValueError("placement checks need a bound stackup for the "
                         "depth to the plane")

    inferred = intent_module.infer(schematic, board)
    grounds = inferred.ground_nets
    supplies = inferred.rail_nets - grounds

    depth = {board.stackup.resolve_layer(name): z_top
             for name, (z_top, _) in board.stackup.copper_z_positions().items()}

    board_area = board.outline.area
    planes: list[str] = []
    for layer in board.copper_layers:
        for ground in grounds:
            fill = board.zone_fill(layer, ground)
            if not fill.is_empty and fill.area > PLANE_AREA_FRACTION * board_area:
                planes.append(layer)
                break

    pads_by_ref: dict[str, list[Pad]] = {}
    for pad in board.pads:
        pads_by_ref.setdefault(pad.ref, []).append(pad)

    return PlacementContext(
        board=board, schematic=schematic, ground_nets=grounds,
        supply_nets=supplies, plane_layers=planes, layer_depth=depth,
        pads_by_ref=pads_by_ref)


# ---------------------------------------------------------------------------
# Decoupling loop area
# ---------------------------------------------------------------------------

@dataclass
class DecouplingLoop:
    """One (supply pin, capacitor) pair and the loop between them."""
    pin: str                     # "U18.34"
    pin_name: str
    rail: str
    capacitor: str
    layer: str
    depth_mm: float              # to the nearest ground plane
    pin_to_cap_mm: float
    cap_body_mm: float
    cap_to_ground_via_mm: float
    pin_to_ground_via_mm: float

    @property
    def surface_mm(self) -> float:
        return (self.pin_to_cap_mm + self.cap_body_mm
                + self.cap_to_ground_via_mm + self.pin_to_ground_via_mm)

    @property
    def area_mm2(self) -> float:
        return self.surface_mm * self.depth_mm

    @property
    def inductance_nh(self) -> float:
        """A labelled estimate — see `loop_inductance_nh`. Good to ~2x."""
        return loop_inductance_nh(self.area_mm2)

    def summary(self) -> dict:
        return {
            "pin": self.pin, "pin_name": self.pin_name, "rail": self.rail,
            "capacitor": self.capacitor, "layer": self.layer,
            "depth_mm": round(self.depth_mm, 4),
            "surface_mm": round(self.surface_mm, 3),
            "area_mm2": round(self.area_mm2, 4),
            "inductance_nh_estimate": round(self.inductance_nh, 2),
            "pin_to_cap_mm": round(self.pin_to_cap_mm, 3),
            "cap_to_ground_via_mm": round(self.cap_to_ground_via_mm, 3),
            "pin_to_ground_via_mm": round(self.pin_to_ground_via_mm, 3),
        }

    def __str__(self) -> str:
        return (f"{self.pin} via {self.capacitor}: {self.area_mm2:.3f} mm² "
                f"(~{self.inductance_nh:.2f} nH est), "
                f"{self.surface_mm:.2f} mm of surface run at "
                f"{self.depth_mm:.4f} mm depth")


def decoupling_capacitors(context: PlacementContext
                          ) -> list[tuple[str, Pad, Pad]]:
    """Two-pad capacitors with one pad on a supply and one on ground.

    Read off the *board*, so a capacitor netted in the layout editor counts.
    """
    found: list[tuple[str, Pad, Pad]] = []
    for component in context.schematic.components:
        if not component.reference.startswith("C") or len(component.pins) != 2:
            continue
        pads = context.pads_of(component.reference)
        if len(pads) != 2:
            continue
        ground = [p for p in pads if p.net_name in context.ground_nets]
        supply = [p for p in pads if p.net_name in context.supply_nets]
        if len(ground) == 1 and len(supply) == 1:
            found.append((component.reference, ground[0], supply[0]))
    return found


def supply_pins(context: PlacementContext) -> dict[str, tuple[Pad, str]]:
    """Pads whose symbol pin is typed `power_in` and which sit on a supply.

    Keyed on pin *type* rather than name. On both boards here every `power_in`
    pin belongs to an IC, which is what makes this a clean filter: selecting by
    "pad on a rail belonging to a part with more than two pads" instead pulled
    in every connector shell and MOSFET drain, 49 candidates of which 38 were
    not decoupling targets at all.

    A multi-unit part appears once per unit in the netlist and its pads are all
    on one footprint, so results are keyed by pad designator to deduplicate.
    """
    targets: dict[str, tuple[Pad, str]] = {}
    for component in context.schematic.components:
        for pin in component.pins:
            if pin.type != "power_in":
                continue
            pad = context.pad(component.reference, pin.number)
            if pad is None or pad.net_name not in context.supply_nets:
                continue
            targets[pad.designator] = (pad, pin.name)
    return targets


def decoupling_loops(context: PlacementContext) -> list[DecouplingLoop]:
    """Trace the mounting loop for every supply pin that has a capacitor."""
    capacitors = decoupling_capacitors(context)
    loops: list[DecouplingLoop] = []

    for designator, (pad, pin_name) in supply_pins(context).items():
        on_rail = [c for c in capacitors if c[2].net_name == pad.net_name]
        if not on_rail:
            continue
        reference, cap_ground, cap_supply = min(
            on_rail, key=lambda c: _distance(c[2].pos, pad.pos))

        own_grounds = [p for p in context.pads_of(pad.ref)
                       if p.net_name in context.ground_nets]
        if not own_grounds:
            continue
        ic_ground = min(own_grounds, key=lambda p: _distance(p.pos, pad.pos))

        layer = (pad.copper_layers[0] if pad.copper_layers
                 else context.board.copper_layers[0])
        depth = context.depth_to_plane(layer)
        if depth is None or depth <= 0:
            continue

        cap_via = context.nearest_via(cap_ground.net, cap_ground.pos)
        ic_via = context.nearest_via(ic_ground.net, ic_ground.pos)
        if cap_via is None or ic_via is None:
            continue

        loops.append(DecouplingLoop(
            pin=designator, pin_name=pin_name, rail=pad.net_name,
            capacitor=reference, layer=layer, depth_mm=depth,
            pin_to_cap_mm=_distance(pad.pos, cap_supply.pos),
            cap_body_mm=_distance(cap_supply.pos, cap_ground.pos),
            cap_to_ground_via_mm=cap_via[0],
            pin_to_ground_via_mm=ic_via[0]))
    return loops


def check_decoupling_loops(context: PlacementContext) -> list[Finding]:
    """Supply pins whose decoupling loop is far worse than the board's own norm.

    **This check used to be unable to say it had run.** Field report,
    2026-08-05, on a board with twelve loops:

        for the 12 decoupling loops on this board I could not distinguish
        "measured and within limits" from "not measured" — which is precisely
        the failure mode the `_measured` pairs exist to prevent.

    They were right, and §53.2's *"nothing to do — measured, not assumed"* was
    wrong: `supply_pins` and `decoupling_loops` being equal says every supply
    pin got a loop *built*, not that any was *compared*. Every loop below
    `info` hit a bare `continue`, so a clean board and a declined one produced
    the same silence — the §52.1 shape, in the one check this module exists
    for and the one the audited list left out.

    Now every path speaks: too few to compare says so, and a board whose loops
    are all within limits gets one `info` saying that, with the median and the
    worst it found.
    """
    loops = decoupling_loops(context)
    findings: list[Finding] = []
    if len(loops) < DECOUPLING_MIN_POPULATION:
        if loops:
            findings.append(Finding(
                check="decoupling_loop", severity="info", score=0.0,
                title=(f"{len(loops)} decoupling loop(s) were built and NOT "
                       f"compared — {DECOUPLING_MIN_POPULATION} are needed for "
                       f"a median to mean anything"),
                nets=sorted({loop.rail for loop in loops}),
                detail=[f"{loop.pin} ({loop.pin_name}) through "
                        f"{loop.capacitor}: {loop.area_mm2:.2f} mm2"
                        for loop in sorted(loops, key=lambda l: -l.area_mm2)]
                + [f"the headline here is relative — a loop far worse than "
                   f"this board's own norm — and a population this small has "
                   f"no norm. The absolute areas above are still real",
                   f"a loop over {DECOUPLING_ABSOLUTE_MAJOR_MM2:.0f} mm2 would "
                   f"be reported on its own account; none here is"],
                measurement={"loops": len(loops),
                             "areas_mm2": [round(l.area_mm2, 3)
                                           for l in loops]},
                basis=("loop area from the supply pin through its capacitor "
                       "and back through the nearest ground return"),
                intent_needed=("nothing — but this is 'not compared', not "
                               "'compared and clean'")))
        return findings

    areas = sorted(loop.area_mm2 for loop in loops)
    median = areas[len(areas) // 2]
    compared = 0

    for loop in sorted(loops, key=lambda l: -l.area_mm2):
        ratio = loop.area_mm2 / median if median else 0.0
        severity, score = _severity(ratio, DECOUPLING_RELATIVE_MINOR,
                                    DECOUPLING_RELATIVE_MAJOR)
        if loop.area_mm2 >= DECOUPLING_ABSOLUTE_MAJOR_MM2:
            severity = "major"
            score = max(score, loop.area_mm2 / DECOUPLING_ABSOLUTE_MAJOR_MM2)
        compared += 1
        if severity == "info":
            continue

        pad = context.pad(*loop.pin.split(".", 1))
        findings.append(Finding(
            check="decoupling_loop", severity=severity, score=score,
            title=(f"{loop.pin} ({loop.pin_name}) — decoupling loop through "
                   f"{loop.capacitor} encloses {loop.area_mm2:.2f} mm², "
                   f"{ratio:.1f}x this board's median"),
            nets=[loop.rail], layers=[loop.layer],
            location=pad.pos if pad else None,
            detail=[
                f"{loop.surface_mm:.2f} mm of surface run at "
                f"{loop.depth_mm:.4f} mm above the nearest ground plane",
                f"pin to capacitor {loop.pin_to_cap_mm:.2f} mm, capacitor to "
                f"its ground via {loop.cap_to_ground_via_mm:.2f} mm, pin's own "
                f"ground via {loop.pin_to_ground_via_mm:.2f} mm",
                f"the board's median loop is {median:.2f} mm² over "
                f"{len(loops)} supply pins, so this one is achievable here",
                f"roughly {loop.inductance_nh:.1f} nH — an ESTIMATE, from "
                f"mu0*A/w with w taken as a nominal "
                f"{NOMINAL_RIBBON_WIDTH_MM} mm ribbon. The area is measured; "
                f"the width is not, and the dependence is linear, so treat "
                f"this as good to about a factor of two",
                "the ranking is on area rather than on that estimate, and is "
                "relative to this board because judging henries needs the "
                "capacitor's own ESL, which is a property of the part",
            ],
            measurement=loop.summary(),
            basis=("loop traced through real features — power pad to the "
                   "capacitor's pads to their ground vias to the pin's own "
                   "ground via — times the dielectric depth to the nearest "
                   "ground plane, from the bound stackup. Via positions set "
                   "this loop, which is why capacitor-to-pin distance alone "
                   "does not measure it"),
            assumption=("each hop is a straight line between real pad and via "
                        "positions rather than the length of the copper "
                        "joining them, so every area here is a lower bound"),
            intent_needed=(f"{loop.capacitor}'s ESL and this pin's transient "
                           f"current — the loop only matters against the "
                           f"capacitor's own parasitic inductance")))

    # "Measured and within limits" is a positive statement and it is the only
    # thing that distinguishes a clean result from a skipped one. Without it
    # this check was silent on a healthy board, which reads identically to a
    # check that declined.
    if compared and not findings:
        worst = max(loops, key=lambda l: l.area_mm2)
        findings.append(Finding(
            check="decoupling_loop", severity="info", score=0.0,
            title=(f"{compared} decoupling loop(s) measured and within limits "
                   f"— worst {worst.area_mm2:.2f} mm2 at {worst.pin} "
                   f"({worst.area_mm2 / median:.1f}x this board's median of "
                   f"{median:.2f} mm2)"),
            nets=sorted({loop.rail for loop in loops}),
            detail=[f"every loop is under {DECOUPLING_RELATIVE_MINOR:.0f}x the "
                    f"median and under the "
                    f"{DECOUPLING_ABSOLUTE_MAJOR_MM2:.0f} mm2 absolute floor",
                    f"median {median:.2f} mm2 over {compared} loop(s); "
                    f"largest {worst.area_mm2:.2f} mm2 at {worst.pin} "
                    f"({worst.pin_name}) through {worst.capacitor}",
                    "this is a positive result, not filler: it says the check "
                    "ran and found nothing wrong, which a zero on its own "
                    "cannot say"],
            measurement={"loops_compared": compared,
                         "median_mm2": round(median, 3),
                         "worst_mm2": round(worst.area_mm2, 3),
                         "worst_pin": worst.pin},
            basis=("loop area from the supply pin through its capacitor and "
                   "back through the nearest ground return, compared against "
                   "this board's own median"),
            intent_needed=("the capacitors' ESL — a loop is only meaningful "
                           "against the parasitic inductance it adds to")))
    return findings


# ---------------------------------------------------------------------------
# Regulator feedback
# ---------------------------------------------------------------------------

def _pins_matching(context: PlacementContext, pattern: re.Pattern
                   ) -> list[tuple[str, str, str, Pad]]:
    """(reference, pin number, pin name, pad) for every pin matching a name."""
    found = []
    for component in context.schematic.components:
        for pin in component.pins:
            if not pattern.match(pin.name.strip()):
                continue
            pad = context.pad(component.reference, pin.number)
            if pad is not None:
                found.append((component.reference, pin.number, pin.name, pad))
    return found


def sense_pins(context: PlacementContext
               ) -> list[tuple[str, str, str, Pad, str]]:
    """Current-sense input pins, as (reference, number, name, pad, how).

    Two routes, and `how` records which one fired so the finding can say it:

    * ``pin_name`` — the symbol names the pin `IN+`, `VSP`, `SENSE-` and so on.
      This is the original route and the strong one.
    * ``sense_amp_value`` — the symbol names the pin bare `+`/`-`, *and* the
      part's value matches a current-shunt-amplifier family, *and* it has
      enough pins to be an amplifier rather than a connector. Weaker, and
      narrow on purpose: see `SENSE_AMP_FAMILY`.

    A part is never taken by both routes; the named route wins, so a symbol
    that has `IN+` and a stray `-` is not counted twice.
    """
    found = [(reference, number, name, pad, "pin_name")
             for reference, number, name, pad
             in _pins_matching(context, SENSE_PIN)]
    already = {reference for reference, *_ in found}

    for component in context.schematic.components:
        if component.reference in already:
            continue
        if len(component.pins) < MIN_SENSE_AMP_PINS:
            continue
        haystack = " ".join(filter(None, (component.value, component.lib_id)))
        if not SENSE_AMP_FAMILY.search(haystack):
            continue
        bare = [pin for pin in component.pins
                if BARE_SENSE_PIN.match(pin.name.strip())]
        if len({pin.name.strip() for pin in bare}) < 2:
            continue          # needs both halves; one alone identifies nothing
        for pin in bare:
            pad = context.pad(component.reference, pin.number)
            if pad is not None:
                found.append((component.reference, pin.number, pin.name, pad,
                              "sense_amp_value"))
    return found


def _kelvin_not_measured(reference, pins, because, expected,
                         how="pin_name") -> Finding:
    """An `info` saying a sense pair was found and not measured.

    The same contract as `_crystal_not_measured`, for the same reason: a
    candidate counted in `coverage()` and then skipped is indistinguishable
    from a candidate examined and found clean, and the count reads as evidence
    that the check ran.
    """
    pads = [pad for *_, pad, _ in pins] if pins else []
    return Finding(
        check="kelvin_sense", severity="info", score=0.0,
        title=f"{reference} — its sense connection was NOT measured",
        nets=sorted({p.net_name for p in pads if p.net_name}),
        location=pads[0].pos if pads else None,
        detail=[because,
                f"expected for: {expected}",
                f"identified as a sense part by: "
                + ("its symbol's pin names" if how == "pin_name" else
                   "its value matching a current-sense amplifier family, with "
                   "inputs named bare + and -"),
                "this is 'not measured', not 'measured and clean' — coverage "
                "reports sense pins found and sense pins measured separately"],
        measurement={"part": reference, "measured": False,
                     "identified_by": how, "reason": because},
        basis="the Kelvin-sense check's own preconditions, reported rather "
              "than passed over",
        intent_needed="whether this part is a current-sense amplifier at all, "
                      "and which resistor is its shunt")


def _feedback_not_measured(designator, name, pad, because, expected) -> Finding:
    """An `info` saying a feedback pin was found and neither comparison ran."""
    return Finding(
        check="regulator_feedback", severity="info", score=0.0,
        title=f"{designator} ({name}) — its feedback node was NOT measured",
        nets=[pad.net_name] if pad.net_name else [],
        location=pad.pos, layers=sorted(set(pad.copper_layers)),
        detail=[because,
                f"expected for: {expected}",
                "this is 'not measured', not 'measured and clean' — coverage "
                "reports feedback pins found and feedback pins measured "
                "separately"],
        measurement={"pin": designator, "measured": False, "reason": because},
        basis="the feedback check's own preconditions, reported rather than "
              "passed over",
        intent_needed="whether this pin is a regulator feedback node at all, "
                      "and which resistors set its divider")


def check_regulator_feedback(context: PlacementContext) -> list[Finding]:
    """A regulator's feedback node against its divider and its switch node.

    Two separate problems, reported separately because the fixes differ:

    * the divider sitting a long way from the feedback pin, which makes the
      high-impedance node a long antenna;
    * the feedback node running close to the switch node, which is the classic
      buck instability — the switch node swings the full input in nanoseconds
      and the feedback node has no drive to reject it.

    Both halves can decline silently, and one of them does so through a falsy
    `if` rather than a `continue` — which is why tracing every `continue` in
    this module still missed it. A feedback pin with no resistor anywhere on
    its net, on a board with no switch pin either, produced **nothing at all**
    while `coverage()` reported `feedback_pins: 1`. Two of the five boards here
    are in exactly that state. `measured` counts the pins that got at least one
    of the two comparisons, and every pin that got neither says so.
    """
    findings: list[Finding] = []
    #: feedback pins that got at least one of the two comparisons. Not named
    #: `measured` — the switch-node loop below binds that to a coupling result.
    pins_compared: set[str] = set()
    #: pins that already have a finding above `info`, so the "measured, within
    #: limits" line is not also emitted for them.
    pins_flagged: set[str] = set()
    switch_nets = {pad.net_name for _, _, _, pad
                   in _pins_matching(context, SWITCH_PIN)
                   if pad.net_name}

    for reference, number, name, pad in _pins_matching(context, FEEDBACK_PIN):
        designator = f"{reference}.{number}"
        if not pad.net_name:
            findings.append(_feedback_not_measured(
                designator, name, pad,
                "its pad is on no net, so there is nothing to measure against",
                "an unrouted or unconnected feedback pin"))
            continue
        net_name = pad.net_name

        # -- the divider ---------------------------------------------------
        divider = [p for p in context.board.pads_on_net(pad.net)
                   if p.ref != reference and p.ref.startswith("R")]
        if divider:
            pins_compared.add(designator)
            nearest = min(divider, key=lambda p: _distance(p.pos, pad.pos))
            gap = _distance(nearest.pos, pad.pos)
            severity, score = _severity(gap, FEEDBACK_DIVIDER_MINOR_MM,
                                        FEEDBACK_DIVIDER_MAJOR_MM)
            if severity != "info":
                pins_flagged.add(designator)
                findings.append(Finding(
                    check="regulator_feedback", severity=severity, score=score,
                    # "the nearest resistor", not "the nearest divider
                    # resistor". The check finds a resistor on the net; it
                    # never establishes that any resistor is a divider. On a
                    # fixed-output XL1509-3.3 whose FB is tied straight to the
                    # rail, the "divider" it named was a pull-up for a button
                    # that happened to share the net.
                    title=(f"{reference}.{number} ({name}) — the nearest "
                           f"resistor on this net, {nearest.designator}, is "
                           f"{gap:.2f} mm away"),
                    nets=[net_name], location=pad.pos,
                    layers=sorted(set(pad.copper_layers)),
                    detail=[
                        f"feedback is a high-impedance node and this one runs "
                        f"{gap:.2f} mm before it reaches {nearest.designator}",
                        f"resistors on this net: " + ", ".join(
                            sorted(p.designator for p in divider)),
                        "this is the nearest resistor sharing the net, which "
                        "is not proof it is a feedback divider — a "
                        "fixed-output regulator ties FB to the rail and any "
                        "resistor on that rail will be named here",
                    ],
                    measurement={"pin": f"{reference}.{number}",
                                 "measured": True,
                                 "net": net_name,
                                 "nearest_resistor": nearest.designator,
                                 "distance_mm": round(gap, 3)},
                    basis=("straight-line distance from the feedback pad to "
                           "the nearest resistor pad on the same net"),
                    intent_needed=("the regulator's loop bandwidth — a slow "
                                   "LDO tolerates far more of this than a "
                                   "2 MHz buck")))

        # -- running alongside the switch node -----------------------------
        # Coupled run length, not minimum distance. See
        # FEEDBACK_TO_SWITCH_GAP_MM for why that distinction is the whole
        # check: at the regulator's own pads every net is a pin pitch from
        # every other one.
        for switch_net in sorted(switch_nets):
            if switch_net == net_name:
                continue
            try:
                measured = coupling_module.coupling(
                    context.board, net_name, switch_net,
                    max_gap_mm=FEEDBACK_TO_SWITCH_GAP_MM)
            except KeyError:
                continue
            run = measured.longest_run
            if run is None:
                continue
            # A parallel run existed and was measured, whatever its length.
            pins_compared.add(designator)
            severity, score = _severity(measured.coupled_length_mm,
                                        FEEDBACK_RUN_MINOR_MM,
                                        FEEDBACK_RUN_MAJOR_MM)
            if severity == "info":
                continue
            pins_flagged.add(designator)
            findings.append(Finding(
                check="regulator_feedback", severity=severity, score=score,
                title=(f"{reference}.{number} ({name}) — the feedback net runs "
                       f"{measured.coupled_length_mm:.2f} mm alongside the "
                       f"switch node `{switch_net}` at "
                       f"{measured.min_gap_mm:.3f} mm"),
                nets=[net_name, switch_net], location=pad.pos,
                layers=measured.layers,
                detail=[
                    f"longest parallel run {run.length_mm:.2f} mm on "
                    f"{run.layer}, closest approach "
                    f"{measured.min_gap_mm:.3f} mm edge to edge",
                    "the switch node swings the full input voltage in "
                    "nanoseconds and the feedback node is high impedance with "
                    "no drive to reject it — this is the classic buck "
                    "instability, and it shows up as jitter rather than as a "
                    "hard failure",
                    "measured as a parallel run rather than a closest "
                    "approach: at the regulator's own pads every net is "
                    "within a pin pitch of every other one",
                ],
                measurement={"pin": f"{reference}.{number}",
                             "measured": True,
                             "feedback_net": net_name,
                             "switch_net": switch_net,
                             **measured.summary()},
                basis=("edge-to-edge gap sampled per 25 um along the feedback "
                       "net with `coupling.py`'s parallelism test, so a "
                       "crossing or a fanout is not counted as a run"),
                intent_needed=("the regulator's switching frequency and edge "
                               "rate")))

        if designator in pins_flagged:
            continue          # it already has a finding of its own to read

        if designator in pins_compared:
            # Compared, and nothing crossed a threshold. Said out loud for the
            # same reason as every other clean case in this module: silence
            # plus a non-zero coverage count is indistinguishable from a
            # decline.
            what = []
            if divider:
                what.append(f"distance to the nearest resistor on `{net_name}`")
            if any(s != net_name for s in switch_nets):
                what.append("coupled run against every switch node")
            findings.append(Finding(
                check="regulator_feedback", severity="info", score=0.0,
                title=(f"{designator} ({name}) — feedback node measured, "
                       f"within limits"),
                nets=[net_name], location=pad.pos,
                layers=sorted(set(pad.copper_layers)),
                detail=["measured: " + "; ".join(what),
                        "'measured and within limits' — not the same statement "
                        "as a check that declined to measure"],
                measurement={"pin": designator, "measured": True,
                             "net": net_name},
                basis="straight-line distance to the nearest resistor on the "
                      "net, and `coupling.py`'s parallelism test against each "
                      "switch node"))
            continue

        reasons = []
        if not divider:
            reasons.append("no resistor sits on its net, so the distance to a "
                           "divider could not be measured")
        if not switch_nets:
            reasons.append("no pin on this board is named as a switch node, so "
                           "there was nothing to compare it against for "
                           "coupling")
        elif not any(s != net_name for s in switch_nets):
            reasons.append("the only switch net found is this net itself")
        else:
            reasons.append("no parallel run was found against any switch node")
        findings.append(_feedback_not_measured(
            designator, name, pad, "; ".join(reasons),
            "a fixed-output regulator with FB tied to the rail, or an LDO on a "
            "board with no switching converter"))
    return findings


# ---------------------------------------------------------------------------
# Kelvin sense connections
# ---------------------------------------------------------------------------

def check_kelvin_sense(context: PlacementContext) -> list[Finding]:
    """Whether a current-sense amplifier taps its shunt at the shunt's own pads.

    A sense connection that meets the shunt anywhere but on its pads measures
    the shunt *plus* whatever copper lies between, and that copper is carrying
    the load current. On a 20 mΩ shunt a millimetre of 0.5 mm track is several
    percent of the reading.

    **This does not scan every two-pin part.** It triggers only on a component
    whose manufacturer's symbol names two pins as the halves of a sense input
    (`IN+`/`IN-`, `VSP`/`VSN`, and so on), and then finds that component's own
    shunt. On both boards here that is one part, a current-sense amplifier.

    Finding the shunt goes *through* series parts
    ---------------------------------------------
    The shunt is rarely wired straight to the sense pins. The six-layer board's
    amplifier reaches
    it through a pair of 10 R input filter resistors, so a search for a part
    bridging the two sense nets directly finds only the differential filter
    capacitors — and the real shunt, a 1 W 20 mOhm part one hop further out,
    is invisible.

    `Schematic.reachable_through_series` walks through two-terminal parts of any
    kind, so a ferrite bead or an AC-coupling capacitor in the path is crossed
    the same way a resistor is. `Schematic.bridging_part` then attributes each
    reachable net to whichever sense pin reaches it sooner, which is what
    separates the shunt from a 10 kOhm pull-up that also touches the
    reachable set. Its docstring has the detail.

    `intent.propose_differential_pairs` flags a sense amplifier's `IN+`/`IN-`
    at 0.85
    confidence precisely because it cannot tell a Kelvin pair from a
    transmission line. This resolves that ambiguity the other way: two nets
    that reach the two ends of one shunt are a sense pair and want a short
    common path, not tight coupling.

    The weak step, stated plainly
    -----------------------------
    "Where the load connects" is taken as **the nearest other pad on the shunt's
    net**, and that is a proxy rather than a measurement. On the six-layer
    board's high-side node the nearest other pad is a 10 kOhm pull-up carrying
    no current at all, while the actual load is a MOSFET further away. The error direction is
    unhelpful — a nearby high-impedance part makes the apparent load offset
    small, which makes a finding *more* likely, not less.

    The better version measures the intervening copper's resistance against the
    shunt's own value (both are available: track width from the board, copper
    thickness from the stackup, and the shunt value is usually parseable from
    its Value field) and reports the error as a percentage of the reading. That
    is the right check and is not built.
    """
    findings: list[Finding] = []
    sense = sense_pins(context)
    if not sense:
        return []

    schematic = context.schematic
    rails = context.ground_nets | context.supply_nets

    by_component: dict[str, list] = {}
    how_found: dict[str, str] = {}
    for reference, number, name, pad, how in sense:
        by_component.setdefault(reference, []).append((number, name, pad, how))
        how_found.setdefault(reference, how)

    for reference, pins in by_component.items():
        how = how_found[reference]
        if len(pins) < 2:
            findings.append(_kelvin_not_measured(
                reference, pins,
                f"only {len(pins)} of its pins is a sense input, so there is "
                f"no pair to measure across a shunt",
                "a symbol naming one half of a sense pair, or a part whose "
                "second input has no pad on the board", how))
            continue
        # The traversal runs on *schematic* net names, so the starting net has
        # to come from the symbol pin and not from `pad.net_name`. KiCad's
        # generated names do not round-trip: U15.7 is `Net-(D3-A1)` on the
        # board and `Net-(C13-Pad2)` in the schematic. Starting from the board
        # name walks a net the schematic does not have and finds nothing —
        # silently, which is the whole reason §11a maps by pads rather than by
        # names.
        named = []
        for number, name, pad, _how in pins:
            net = schematic.net_of(reference, number)
            if net is not None and pad.net_name:
                named.append((number, name, pad, net.name))
        if len({n for *_, n in named}) < 2:
            findings.append(_kelvin_not_measured(
                reference, pins,
                "its sense inputs do not resolve to two distinct schematic "
                "nets, so there is no pair to trace to a shunt",
                "inputs tied together, or pins the schematic and the board do "
                "not agree on", how))
            continue
        first, second = named[0], named[1]
        if first[3] == second[3]:
            findings.append(_kelvin_not_measured(
                reference, pins,
                f"both sense inputs sit on the same net `{first[3]}`",
                "a sense amplifier whose inputs are shorted, which measures "
                "no shunt at all", how))
            continue

        reach = {
            side[3]: schematic.reachable_through_series(
                side[3], max_depth=SENSE_TRACE_DEPTH, rails=rails,
                skip={reference})
            for side in (first, second)
        }
        shunt = schematic.bridging_part(reach[first[3]], reach[second[3]],
                                        prefixes=("R",))
        if shunt is None:
            findings.append(_kelvin_not_measured(
                reference, pins,
                f"no resistor bridges its two sense nets within "
                f"{SENSE_TRACE_DEPTH} series part(s), so no shunt was found "
                f"to measure the tap against",
                "a shunt further out than the search depth, a sense amplifier "
                "measuring something that is not a resistor, or an unpopulated "
                "shunt", how))
            continue
        shunt_pads = context.pads_of(shunt.reference)
        if len(shunt_pads) != 2:
            findings.append(_kelvin_not_measured(
                reference, pins,
                f"the shunt found, {shunt.reference}, has "
                f"{len(shunt_pads)} pad(s) on the board rather than two",
                "a four-terminal Kelvin shunt, which this check cannot yet "
                "measure, or a footprint that does not match its symbol",
                how))
            continue

        #: sense pins of this part for which an offset was actually compared.
        #: Without it, `sense_pins: 2` and no findings is indistinguishable
        #: from a decline — which is the whole §52.1 failure.
        compared: set[str] = set()
        for number, name, sense_pad, sense_net in named:
            side = reach[sense_net]
            # The shunt pad this half of the pair is responsible for, and the
            # chain of parts the sense signal crosses to reach it. Matched
            # through the schematic net of the shunt's own pin, for the same
            # round-tripping reason as above.
            # Whichever of the shunt's two pads *this* side reaches soonest.
            # Taking the first one that is reachable at all puts both halves of
            # the pair on the same pad: each side can reach both ends of the
            # shunt, just at different depths, so depth is the attribution.
            options = []
            for candidate in shunt_pads:
                net = schematic.net_of(shunt.reference, candidate.number)
                if net is not None and net.name in side:
                    options.append((side[net.name].depth, candidate, net.name))
            if not options:
                findings.append(_kelvin_not_measured(
                    reference, pins,
                    f"pin {number} ({name}) reaches no pad of "
                    f"{shunt.reference} within {SENSE_TRACE_DEPTH} series "
                    f"part(s), so this half of the pair has no landing point",
                    "one half of a sense pair filtered more deeply than the "
                    "other", how))
                continue
            _, landing, landing_net = min(options, key=lambda o: o[0])
            chain = side[landing_net].through

            # Where the sense chain actually joins the shunt's net: the pad of
            # the last part it passed through, or the sense pin itself when it
            # is wired straight to the shunt.
            if chain:
                entry_candidates = [p for p in context.pads_of(chain[-1])
                                    if p.net == landing.net]
                entry = entry_candidates[0] if entry_candidates else sense_pad
            else:
                entry = sense_pad

            # Every other pad on that net is load-current copper.
            others = [p for p in context.board.pads_on_net(landing.net)
                      if p.designator not in (entry.designator,
                                              landing.designator)]
            if not others:
                findings.append(_kelvin_not_measured(
                    reference, pins,
                    f"nothing but the sense path and {shunt.reference} sits "
                    f"on `{landing.net_name}`, so there is no load connection "
                    f"to compare the tap against",
                    "a shunt whose load side reaches the rest of the circuit "
                    "through a pour rather than through a pad", how))
                continue
            carrier = min(others, key=lambda p: _distance(p.pos, landing.pos))
            load_offset = _distance(carrier.pos, landing.pos)
            sense_offset = _distance(entry.pos, landing.pos)
            # Past this point the comparison has been made, whatever it says.
            compared.add(number)
            if sense_offset <= load_offset:
                continue          # the sense tap is the closer connection

            severity, score = _severity(sense_offset - load_offset,
                                        KELVIN_OFFSET_MINOR_MM,
                                        KELVIN_OFFSET_MAJOR_MM)
            if severity == "info":
                continue

            route = " -> ".join((f"{reference}.{number}",) + chain
                                + (shunt.reference,))
            findings.append(Finding(
                check="kelvin_sense", severity=severity, score=score,
                title=(f"{reference}.{number} ({name}) reaches {shunt.reference} "
                       f"{sense_offset:.2f} mm from its pad, while the load "
                       f"connects {load_offset:.2f} mm from it"),
                nets=[landing.net_name], location=landing.pos,
                detail=[
                    f"sense path: {route}"
                    + (f" (through {len(chain)} series part(s))" if chain
                       else " (wired straight to the shunt)"),
                    f"on `{landing.net_name}` the sense path joins at "
                    f"{entry.designator}, {sense_offset:.2f} mm from "
                    f"{landing.designator}; the load joins at "
                    f"{carrier.designator}, {load_offset:.2f} mm from it",
                    f"{shunt.reference} is {shunt.value}",
                    "the sense pair therefore measures the shunt plus the "
                    "copper between, and that copper carries the load current",
                ],
                measurement={"pin": f"{reference}.{number}",
                             "measured": True,
                             "part": reference,
                             "shunt": shunt.reference,
                             "shunt_value": shunt.value,
                             "shunt_pad": landing.designator,
                             "through": list(chain),
                             "sense_entry": entry.designator,
                             "sense_offset_mm": round(sense_offset, 3),
                             "load_pad": carrier.designator,
                             "load_offset_mm": round(load_offset, 3)},
                basis=("the shunt found by walking outward from each sense pin "
                       "through two-terminal parts of any kind and taking the "
                       "resistor that bridges the two sides, then straight-line "
                       "distances on its net from its pad to where the sense "
                       "path joins and to the nearest load pad"),
                intent_needed=(f"{shunt.reference}'s resistance and the load "
                               f"current — the error is the intervening "
                               f"copper's resistance as a fraction of the "
                               f"shunt's")))

        # One statement per part that the comparison ran, whatever it said.
        # A pin whose tap is the closer connection, or whose offset is under
        # the threshold, produces no finding of its own — so without this a
        # measured-and-clean part and a declined part look identical from
        # outside, which is exactly the failure this round exists to close.
        if compared:
            findings.append(Finding(
                check="kelvin_sense", severity="info", score=0.0,
                title=(f"{reference} — sense tap measured against "
                       f"{shunt.reference} on {len(compared)} of "
                       f"{len(named)} input(s), within limits"),
                nets=sorted({p.net_name for *_, p, _ in pins if p.net_name}),
                location=pins[0][2].pos if pins else None,
                detail=[f"shunt {shunt.reference} ({shunt.value})",
                        f"identified as a sense part by "
                        + ("its symbol's pin names" if how == "pin_name" else
                           "its value matching a current-sense amplifier "
                           "family, with inputs named bare + and -"),
                        "'measured and within limits' — not the same statement "
                        "as a check that declined to measure"],
                measurement={"part": reference, "measured": True,
                             "identified_by": how,
                             "shunt": shunt.reference,
                             "pins_compared": len(compared)},
                basis="straight-line offsets on the shunt's own net from its "
                      "pad to where the sense path joins and to the nearest "
                      "load pad"))
    return findings


# ---------------------------------------------------------------------------
# ESD / TVS topological position
# ---------------------------------------------------------------------------

def check_esd_position(context: PlacementContext) -> list[Finding]:
    """Whether a shunt TVS sits between the connector and the part it protects.

    A two-pin TVS is a shunt clamp, so "in series or on a stub" is not the
    question — it is always a stub. The question that matters is *where on the
    line*: a clamp downstream of the protected input lets the strike reach that
    input first, and the diode then protects nothing.

    Evidence for what is a TVS at all is the part's value or description, which
    is weak, so these are reported at most `minor` and say so.

    **Multi-line arrays are examined too** (§40). This used to bail on
    `len(pads) != 2`, which threw away every clamp that is not a discrete
    two-pin diode — including `USBLC6-2SC6`, a part `ESD_HINT` names by *name*
    and the commonest ESD part on a USB port. The two-pin path did work (it
    reached the comparison on the four-layer board's `PESD0402-140`), so the check was
    not wholly inert; it was blind to the usual case. `coverage()` reported
    nothing about it either way, so neither could be told apart from a pass.

    The generalisation is small because the question is per *line*: take every
    pad that is on neither a ground nor a supply, group them by net, and ask
    the same "is the clamp nearer the connector than the victim is" question
    once per protected line.
    """
    findings: list[Finding] = []
    connector_pads: dict[str, list[Pad]] = {}
    for component in context.schematic.components:
        prefix = re.match(r"^[A-Za-z_]+", component.reference)
        if prefix and prefix.group(0).upper() in ("J", "CN", "P", "USB", "CONN"):
            connector_pads[component.reference] = context.pads_of(
                component.reference)

    for component in context.schematic.components:
        text = f"{component.value or ''} {component.description or ''}"
        if not ESD_HINT.search(text):
            continue
        pads = context.pads_of(component.reference)
        signal_by_net: dict[int, list[Pad]] = {}
        for pad in pads:
            if (pad.net_name and pad.net_name not in context.ground_nets
                    and pad.net_name not in context.supply_nets):
                signal_by_net.setdefault(pad.net, []).append(pad)
        if not signal_by_net:
            findings.append(_esd_not_measured(
                component, [], pads,
                "every pad it has sits on a ground or a supply, so no "
                "protected line could be identified",
                "a clamp whose signal pads are unrouted, or a bulk TVS across "
                "a rail rather than a line clamp"))
            continue

        measured_lines = 0
        for clamp_pads in signal_by_net.values():
            line = clamp_pads[0].net_name
            entry = [p for pads_ in connector_pads.values() for p in pads_
                     if p.net == clamp_pads[0].net]
            if not entry:
                # The commonest decline by far, and the one that made
                # `esd_clamps: 11` on a real board mean "none of the fourteen
                # lines was measured". The check needs a connector pad to say
                # where the strike enters; with none there is no upstream and
                # no downstream, and the question is unanswerable rather than
                # answered favourably.
                findings.append(_esd_not_measured(
                    component, [line], clamp_pads,
                    f"no connector pad sits on `{line}`, so there is no entry "
                    f"point to measure the clamp's position from",
                    "a line that leaves the board through a connector this "
                    "check does not recognise (it looks for references "
                    "starting J, CN, P, USB or CONN), a test point, or an "
                    "internal net that is not externally exposed at all"))
                continue
            # A multi-pin array lands two pads on the same line; the one the
            # strike meets first is the one that matters.
            source, clamp = min(
                ((e, c) for e in entry for c in clamp_pads),
                key=lambda pair: _distance(pair[0].pos, pair[1].pos))

            # Everything else on this net that is neither the connector nor the
            # clamp is something the clamp is meant to protect.
            protected = [p for p in context.board.pads_on_net(clamp.net)
                         if p.ref not in (component.reference, source.ref)]
            if not protected:
                findings.append(_esd_not_measured(
                    component, [line], clamp_pads,
                    f"nothing on `{line}` but the connector and the clamp "
                    f"itself, so there is no protected pin to compare against",
                    "a line routed to a connector and a clamp and nowhere "
                    "else yet"))
                continue
            nearest_victim = min(protected,
                                 key=lambda p: _distance(p.pos, source.pos))
            to_clamp = _distance(source.pos, clamp.pos)
            to_victim = _distance(source.pos, nearest_victim.pos)
            measured_lines += 1
            if to_clamp <= to_victim:
                continue

            findings.append(Finding(
                check="esd_position", severity="minor",
                score=to_clamp / max(to_victim, 1e-6),
                title=(f"{component.reference} clamps `{clamp.net_name}` "
                       f"{to_clamp:.2f} mm from the connector, but "
                       f"{nearest_victim.designator} is only "
                       f"{to_victim:.2f} mm from it"),
                nets=[clamp.net_name], location=clamp.pos,
                detail=[
                    f"entry point {source.designator}, clamp "
                    f"{component.reference} at {to_clamp:.2f} mm, first "
                    f"protected pin {nearest_victim.designator} at "
                    f"{to_victim:.2f} mm",
                    "a strike reaches the protected pin before the clamp, so "
                    "the clamp is downstream of what it is there to protect",
                ],
                measurement={"clamp": component.reference,
                             "measured": True,
                             "net": clamp.net_name,
                             "entry": source.designator,
                             "clamp_mm": round(to_clamp, 3),
                             "protected": nearest_victim.designator,
                             "protected_mm": round(to_victim, 3)},
                basis=("straight-line distance from the connector pad on this "
                       "net to the clamp and to the nearest other pin on it"),
                assumption=(f"{component.reference} is a TVS, inferred from "
                            f"its value or description rather than from the "
                            f"circuit — weak evidence, which is why this is "
                            f"not a major"),
                intent_needed="whether this net is externally exposed at all"))

        if measured_lines:
            findings.append(Finding(
                check="esd_position", severity="info", score=0.0,
                title=(f"{component.reference} — clamp position measured on "
                       f"{measured_lines} of {len(signal_by_net)} line(s), "
                       f"upstream of what it protects"),
                nets=sorted({p.net_name for pads_ in signal_by_net.values()
                             for p in pads_ if p.net_name}),
                location=pads[0].pos if pads else None,
                detail=["the clamp meets a strike before the pin it protects "
                        "does, on every line measured",
                        "'measured and correct' — not the same statement as a "
                        "line that had no connector to measure from"],
                measurement={"clamp": component.reference, "measured": True,
                             "lines_measured": measured_lines,
                             "lines_found": len(signal_by_net)},
                basis="straight-line distance from the connector pad on each "
                      "line to the clamp and to the nearest other pin on it",
                assumption=(f"{component.reference} is a TVS, inferred from "
                            f"its value or description rather than from the "
                            f"circuit")))
    return findings


def _esd_not_measured(component, nets, pads, because, expected) -> Finding:
    """An `info` saying a clamp, or one of its lines, was not measured.

    The measured case for this check was the worst of the lot: one board
    reported `esd_clamps: 11` and **zero** findings while thirteen of its
    fourteen clamped lines were never compared at all, because no connector pad
    sat on them. A reader takes eleven-found-and-nothing-wrong as a pass.
    """
    return Finding(
        check="esd_position", severity="info", score=0.0,
        title=(f"{component.reference} — clamp position was NOT measured"
               + (f" on `{nets[0]}`" if nets else "")),
        nets=list(nets),
        location=pads[0].pos if pads else None,
        detail=[because,
                f"expected for: {expected}",
                "this is 'not measured', not 'measured and clean' — coverage "
                "reports ESD clamps found and clamps measured separately"],
        measurement={"clamp": component.reference, "measured": False,
                     "net": nets[0] if nets else None, "reason": because},
        basis="the ESD-position check's own preconditions, reported rather "
              "than passed over",
        intent_needed="whether this net is externally exposed at all, and "
                      "through what")


def esd_clamps(context: PlacementContext) -> list[str]:
    """Parts `check_esd_position` recognises as clamps, so its zero is readable.

    `coverage()` never reported this, which is why the check being inert on
    every board went unnoticed for four boards (§40, §32.C).
    """
    return sorted(
        c.reference for c in context.schematic.components
        if ESD_HINT.search(f"{c.value or ''} {c.description or ''}"))


# ---------------------------------------------------------------------------
# Crystal loop area
# ---------------------------------------------------------------------------

def _crystal_not_measured(component, label, pads, because, expected) -> Finding:
    """An `info` saying plainly that a candidate was found and not measured.

    Every `continue` in `check_crystal_loop` ends here. A check that declines
    to measure something it counted must say so: silence plus a non-zero
    coverage count is indistinguishable from a pass, and that is exactly how a
    real crystal went unexamined with the report implying otherwise.
    """
    return Finding(
        check="crystal_loop", severity="info", score=0.0,
        title=f"{label} — oscillator loop was NOT measured",
        nets=sorted({p.net_name for p in pads if p.net_name}),
        location=pads[0].pos if pads else None,
        detail=[because,
                f"expected for: {expected}",
                "this is 'not measured', not 'measured and clean' — the "
                "coverage line reports crystals found and crystal loops "
                "measured separately for this reason"],
        measurement={"crystal": component.reference, "measured": False,
                     "reason": because},
        basis="the oscillator-loop check's own preconditions, reported rather "
              "than passed over",
        intent_needed="whether this part is an oscillator at all, and at what "
                      "frequency")


def _integrated_load_resonator(context, component, label, pads, signal,
                               loads) -> list[Finding]:
    """A 3-pin resonator with integrated load capacitors — measure what exists.

    No loop area is measurable: the load capacitors are inside the package, so
    there is no external path to enclose. Three things still are, and all three
    are things a designer would want told to them:

    * each terminal's routed length, and the **mismatch** between them —
      asymmetric drive and feedback on an oscillator loop;
    * how far the centre ground pad is from a via down to a plane. On a 3-pin
      resonator the whole point of the centre pad is a via directly beneath it,
      and the integrated capacitors' return current has nowhere else to go.

    Thresholds here are **judgement, not measurement**, and are stated as such
    on the finding: nothing in this project has a measured basis for what
    terminal mismatch costs an oscillator, because that depends on the drive
    level and the frequency, which only the designer knows.
    """
    lengths = {}
    for pad in signal:
        try:
            net = context.board.net(pad.net_name)
        except KeyError:
            continue
        lengths[pad.net_name] = sum(
            track.length_mm for track in context.board.tracks_on_net(net.code))
    if len(lengths) < 2:
        return [_crystal_not_measured(
            component, label, pads,
            "its terminals carry no routed track to measure",
            "an unrouted part, or one connected entirely through pours")]

    shortest = min(lengths.values())
    longest = max(lengths.values())
    ratio = longest / shortest if shortest > 0 else float("inf")

    grounded = [p for p in pads if p.net_name in context.ground_nets]
    via_mm = None
    if grounded:
        try:
            net = context.board.net(grounded[0].net_name)
            found = context.nearest_via(net.code, grounded[0].pos)
        except KeyError:
            found = None
        via_mm = found[0] if found else None

    # A mismatch beyond 2x is worth saying; beyond 4x is worth ranking. Both
    # are judgement (§9), and the finding says so in `assumption`.
    severity, score = _severity(ratio, 2.0, 4.0)
    detail = [
        "integrated-load-capacitor resonator: no discrete load capacitors on "
        "its terminals, so there is no external loop to enclose and no area "
        "can be measured — these are the terminal traces instead",
        ", ".join(f"{name} {length:.2f} mm"
                  for name, length in sorted(lengths.items())),
        f"longest terminal is {ratio:.1f}x the shortest",
    ]
    if via_mm is not None:
        detail.append(
            f"centre ground pad to the nearest via down to a plane: "
            f"{via_mm:.2f} mm — on a 3-pin resonator that pad is the "
            f"integrated capacitors' only return, and it wants a via directly "
            f"beneath it")
    elif grounded:
        detail.append("no via at all was found on the centre ground pad's net "
                      "near the part")
    if loads:
        detail.append(f"{len(loads)} discrete capacitor(s) also present "
                      f"({', '.join(r for r, _, _ in loads)}) — fewer than the "
                      f"two a discrete-load oscillator needs")

    return [Finding(
        check="crystal_loop", severity=severity, score=score,
        title=(f"{label} — oscillator terminals differ {ratio:.1f}x in length, "
               f"{shortest:.2f} mm vs {longest:.2f} mm"),
        nets=sorted(lengths),
        location=pads[0].pos if pads else None,
        detail=detail,
        measurement={"crystal": component.reference, "measured": True,
                     "kind": "integrated_load",
                     "terminal_lengths_mm": {k: round(v, 3)
                                             for k, v in lengths.items()},
                     "length_ratio": round(ratio, 3),
                     "ground_pad_to_via_mm": (round(via_mm, 3)
                                              if via_mm is not None else None)},
        basis="routed length of each terminal net, and the distance from the "
              "part's ground pad to the nearest via on that net",
        assumption="the 2x and 4x mismatch thresholds are judgement, not "
                   "measurement — what asymmetry costs depends on the drive "
                   "level and frequency, which are not in the design files",
        intent_needed="the oscillator's frequency and drive level; at a few "
                      "MHz a mismatch of this kind is usually harmless")]


def check_crystal_loop(context: PlacementContext) -> list[Finding]:
    """The oscillator loop, and — when there is no loop to measure — the
    geometry a 3-pin integrated-load resonator does have.

    The loop case is the same geometry as the decoupling loop and for the same
    reason: it closes through the ground plane, so where the load capacitors'
    ground vias sit is what sets its area.

    **Every early exit now emits an `info` finding**, and that is the point of
    this function as much as the measurement is. The first real crystal this
    check ever met was a Murata CSTCE16M0V53-R0 — a 3-pin ceramic resonator
    with *integrated* load capacitors — so the discrete-load-capacitor search
    found nothing, the loop `continue`d, and **nothing anywhere recorded that
    it had declined to measure**. Worse, `coverage()` reported `crystals=1`,
    which a reader correctly takes as proof the check ran.

    That is this project's own most-stated principle with the sign flipped. A
    zero meaning "did not look" is the failure the coverage lines exist to
    prevent; a *non-zero* meaning "did not look" is the same failure and is
    harder to catch, because the number looks like evidence. `coverage()` now
    reports `crystal_loops_measured` beside `crystals`, and the two disagreeing
    is the signal.

    **The integrated-load case is not exotic** — Murata CSTCE/CSTNE and the
    Abracon equivalents are the default on hobbyist and smallsat STM32/ATmega
    designs precisely because they remove the load-capacitor layout problem.
    No loop area is measurable for one, but three things still are, and all
    three matter: each terminal's routed length, the mismatch between them, and
    how far the centre ground pad is from a via down to a plane. On a 3-pin
    resonator the whole point of the centre pad is a via directly beneath it.
    """
    findings: list[Finding] = []
    for component in context.schematic.components:
        if not CRYSTAL_REFERENCE.match(component.reference):
            continue
        label = component.reference + (f" ({component.value})"
                                       if component.value else "")
        pads = context.pads_of(component.reference)
        signal = [p for p in pads if p.net_name
                  and p.net_name not in context.ground_nets]
        if len(signal) < 2:
            findings.append(_crystal_not_measured(
                component, label, pads,
                f"only {len(signal)} of its pads carry a non-ground net, so "
                f"there is no oscillator loop to trace",
                "an unrouted or single-ended part, or one whose terminals are "
                "on nets this run treats as ground"))
            continue

        # The load capacitors: two-pad parts with one pad on a crystal terminal
        # and one on ground.
        loads = []
        for other in context.schematic.components:
            if other.reference == component.reference:
                continue
            other_pads = context.pads_of(other.reference)
            if len(other_pads) != 2:
                continue
            grounded = [p for p in other_pads
                        if p.net_name in context.ground_nets]
            on_crystal = [p for p in other_pads
                          if p.net_name in {s.net_name for s in signal}]
            if len(grounded) == 1 and len(on_crystal) == 1:
                loads.append((other.reference, grounded[0], on_crystal[0]))

        layer = (pads[0].copper_layers[0] if pads[0].copper_layers
                 else context.board.copper_layers[0])
        depth = context.depth_to_plane(layer)

        if len(loads) < 2:
            # The integrated-load-capacitor case, and the one that exposed all
            # of this. Measure what exists rather than reporting nothing.
            findings.extend(_integrated_load_resonator(
                context, component, label, pads, signal, loads))
            continue

        if depth is None or depth <= 0:
            findings.append(_crystal_not_measured(
                component, label, pads,
                f"no ground plane resolves under {layer}, so the loop has no "
                f"depth and no area can be computed",
                "a board with no plane on the layer the crystal sits over"))
            continue

        runs = []
        for reference, grounded, on_crystal in loads:
            terminal = min(signal,
                           key=lambda p: _distance(p.pos, on_crystal.pos))
            via = context.nearest_via(grounded.net, grounded.pos)
            if via is None:
                continue
            runs.append((reference,
                         _distance(terminal.pos, on_crystal.pos)
                         + _distance(on_crystal.pos, grounded.pos) + via[0]))
        if len(runs) < 2:
            findings.append(_crystal_not_measured(
                component, label, pads,
                f"{len(loads)} load capacitor(s) found but only {len(runs)} "
                f"has a ground via to close the loop through",
                "load capacitors whose ground pads reach the plane through "
                "copper rather than through a via of their own"))
            continue

        surface = sum(length for _, length in runs)
        area = surface * depth
        severity, score = _severity(area, 2.0, 6.0)
        if severity == "info":
            # Measured and clean — which is a different statement from every
            # `continue` above, and must read as one.
            findings.append(Finding(
                check="crystal_loop", severity="info", score=0.0,
                title=(f"{label} — oscillator loop measured at "
                       f"{area:.2f} mm², within limits"),
                nets=sorted({p.net_name for p in signal}),
                location=pads[0].pos, layers=[layer],
                detail=[f"through {', '.join(r for r, _ in runs)}",
                        f"{surface:.2f} mm of surface run at {depth:.4f} mm "
                        f"above the nearest ground plane"],
                measurement={"crystal": component.reference,
                             "load_capacitors": [r for r, _ in runs],
                             "area_mm2": round(area, 4), "measured": True},
                basis="crystal terminal to each load capacitor to its ground "
                      "via, times the depth to the nearest ground plane"))
            continue

        findings.append(Finding(
            check="crystal_loop", severity=severity, score=score,
            title=(f"{component.reference} — oscillator loop through its load "
                   f"capacitors encloses {area:.2f} mm²"),
            nets=sorted({p.net_name for p in signal}),
            location=pads[0].pos, layers=[layer],
            detail=[
                f"{surface:.2f} mm of surface run at {depth:.4f} mm above the "
                f"nearest ground plane",
                "per capacitor: " + ", ".join(f"{r} {l:.2f} mm"
                                              for r, l in runs),
                "the oscillator's return current closes through the plane, so "
                "the load capacitors' ground vias set this loop",
            ],
            measurement={"crystal": component.reference,
                         "load_capacitors": [r for r, _ in runs],
                         "surface_mm": round(surface, 3),
                         "depth_mm": round(depth, 4),
                         "area_mm2": round(area, 4)},
            basis=("crystal terminal to each load capacitor to its ground via, "
                   "times the depth to the nearest ground plane — the same "
                   "method as the decoupling loop"),
            assumption=("straight-line hops between real features, so this is "
                        "a lower bound; and this check has no fabricated board "
                        "behind it — neither board here has a crystal"),
            intent_needed="the oscillator's frequency and drive level"))
    return findings


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

CHECKS = {
    "decoupling_loop": check_decoupling_loops,
    "regulator_feedback": check_regulator_feedback,
    "kelvin_sense": check_kelvin_sense,
    "esd_position": check_esd_position,
    "crystal_loop": check_crystal_loop,
}


def analyse(board: Board, schematic: Schematic) -> list[Finding]:
    """Run every placement check. Needs both a board and a schematic.

    A schematic is not optional here the way it is for the layout audit: every
    check keys on what a *part* is — a supply pin, a feedback pin, a shunt, a
    crystal — and the board alone does not say.
    """
    context = build_context(board, schematic)
    findings: list[Finding] = []
    for function in CHECKS.values():
        findings.extend(function(context))
    return findings


def _measured_count(findings: list[Finding], key: str) -> int:
    """Sum a `measurement` key over the findings that say they measured.

    Every check that can decline past a counted candidate reports its own
    measured tally this way, so the pair `<thing>` / `<thing>_measured` is
    always derived from what the check actually did rather than from a
    parallel reimplementation of its preconditions — which would drift.
    """
    return sum(f.measurement.get(key, 0) for f in findings
               if f.measurement.get("measured"))


def coverage(board: Board, schematic: Schematic) -> dict:
    """What each check found to look at, so a zero can be read correctly.

    **Every `<thing>` here that a check can skip past has a `<thing>_measured`
    beside it**, and the two disagreeing is the signal `check_placement` turns
    into a finding. A zero can mean "did not look"; a *non-zero* can mean
    exactly the same thing and is harder to catch, because the number reads as
    evidence. That was found on a crystal (§52.1) and then measured on four
    more boards: one reported `esd_clamps: 11` and no findings while thirteen
    of its fourteen clamped lines had never been compared, and two reported a
    feedback pin that neither half of `check_regulator_feedback` ever touched.
    """
    context = build_context(board, schematic)
    feedback = check_regulator_feedback(context)
    kelvin = check_kelvin_sense(context)
    esd = check_esd_position(context)
    loops = check_decoupling_loops(context)
    return {
        "ground_planes": context.plane_layers,
        "supply_pins": len(supply_pins(context)),
        "decoupling_capacitors": len(decoupling_capacitors(context)),
        "decoupling_loops": len(decoupling_loops(context)),
        # The pair the audited list left out. `supply_pins == decoupling_loops`
        # says every supply pin got a loop *built*; it says nothing about
        # whether any was *compared*, and an outside session could not tell the
        # two apart on a board with twelve of them (§2.4 of the 2026-08-05
        # report). Derived from the check's own findings, never from a second
        # copy of its preconditions.
        "decoupling_loops_measured": (
            sum(f.measurement.get("loops_compared", 0) for f in loops
                if "loops_compared" in (f.measurement or {}))
            or sum(1 for f in loops
                   if (f.measurement or {}).get("area_mm2") is not None)),
        "feedback_pins": len(_pins_matching(context, FEEDBACK_PIN)),
        # A pin counts as measured if it produced any finding that is not one
        # of the explicit "NOT measured" ones — a real finding above `info`, or
        # the "measured, within limits" line emitted when nothing crossed a
        # threshold. Derived from the check's own output, never from a second
        # copy of its preconditions.
        "feedback_pins_measured": len(
            {f.measurement.get("pin") for f in feedback
             if f.measurement.get("measured") is not False} - {None}),
        "switch_pins": len(_pins_matching(context, SWITCH_PIN)),
        "sense_pins": len(sense_pins(context)),
        "sense_pins_measured": _measured_count(kelvin, "pins_compared"),
        "crystals": sum(1 for c in schematic.components
                        if CRYSTAL_REFERENCE.match(c.reference)),
        # Counted separately, and the whole point. `crystals` says how many
        # candidates were *found*; this says how many were actually measured.
        # They used to be conflated, so `crystals=1` read as "found and
        # examined" on a board where the check had silently declined — a
        # non-zero coverage number meaning "did not look", which is the
        # failure the coverage lines exist to prevent, with the sign flipped.
        # Any check that can `continue` past a counted candidate needs a pair
        # like this.
        "crystal_loops_measured": sum(
            1 for f in check_crystal_loop(context)
            if f.measurement.get("measured")),
        "esd_clamps": len(esd_clamps(context)),
        # Counted as *parts*, to stay comparable with `esd_clamps` — a
        # multi-line array is one clamp and several lines, so counting lines
        # here could make "measured" exceed "found" and turn the disagreement
        # test into nonsense. `esd_lines_measured` carries the finer number.
        "esd_clamps_measured": len(
            {f.measurement.get("clamp") for f in esd
             if f.measurement.get("measured")} - {None}),
        "esd_lines_measured": _measured_count(esd, "lines_measured"),
    }
