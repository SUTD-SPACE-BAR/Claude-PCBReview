"""Reviewing a schematic on its own — the second entry point.

`audit.audit()` reviews a board and needs a validated stackup to do it. A
schematic has no stackup and no geometry, so it gets its own entry point rather
than a flag on that one: making Gate 1 conditional would weaken the gate, and
the gate is load-bearing.

What is answerable from a netlist alone
---------------------------------------
Connectivity and topology. Not proximity, not loop area, not impedance — those
need the layout, and `placement.py` owns them. The division is deliberate and
shows up in the decoupling check, which asks only "does this rail have a
decoupling capacitor at all" and says outright that whether it is *near* the
pin is a different check on a different input.

The pin-type problem, and why every check here reports coverage
---------------------------------------------------------------
KiCad symbols carry an electrical type per pin, and an ERC-style review leans
on it heavily. On the boards here it is largely absent: **211 of 877 pins on
the six-layer board are `unspecified`**, including every pin of one bus
transceiver, because most of those symbols simply do not set it. A check keyed on pin type is
therefore half-blind, and a clean result from it is not evidence of a clean
design — it is partly evidence of a library that does not say.

So every type-driven check reports how much of the board it could actually see,
and `check_pin_type_coverage` states the overall figure as a finding in its own
right. `unspecified` is never treated as benign.

Checks that were written, measured, and refused
-----------------------------------------------
Two checks from the plan are deliberately absent. Both were implemented against
these two boards and both were wrong more often than right:

* **`power_in` pin with nothing `power_out` driving its net.** Produced 5
  findings on the six-layer board and 2 on the four-layer board, and *all seven were false positives* —
  `+3.3V`, `+5V` and a radio rail are each driven by a regulator whose output
  pin the
  library types as something other than `power_out`. The check measures library
  typing, not the design. The one genuinely useful subset — a power pin
  connected to nothing — is caught with certainty by
  `check_unconnected_power_pins`.

* **Missing I2C pull-ups, found from pin names.** The differential-pair proposer
  in `intent.py` works because two devices independently agree on a pin name;
  that strengthening is unavailable here. Every I2C candidate on both boards has
  exactly *one* device naming the pin, and of the six candidates on the four-layer board
  four are not I2C at all: `U2.2(SDx)`/`U2.3(SCx)` are address straps tied to
  GND, and `U2.13(SCL)`/`U2.14(SDA)` are dual-function pins on a part wired in
  SPI mode, on nets the designer named `/SPI2 CLK` and `/SPI2 MOSI`. It also
  *misses* the six-layer board's one real I2C bus entirely. Do not rebuild
  it from pin names.

`check_missing_pullups` survives in the only form that has no false-positive
mode: keyed on the `open_collector`/`open_emitter` pin type, which is hard
evidence. There are **zero such pins in this entire project**, so on these
boards it correctly finds nothing and says that it saw nothing.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from pcbreview import intent as intent_module
from pcbreview.findings import (AuditError, AuditReport, CheckResult, Finding,
                                _severity)
from pcbreview.schematic import Schematic, netlist

# ---------------------------------------------------------------------------
# Pin types
# ---------------------------------------------------------------------------

#: Types that mean "this pin expects to be fed a supply".
POWER_INPUT_TYPES = frozenset({"power_in"})

#: A `power_in` pin whose own *name* says it is a control input. Libraries type
#: enable pins `power_in` routinely, and every check keyed on that type then
#: reads a GPIO-driven enable line as an undecoupled supply rail.
#:
#: This is not classifying the net from a pin name — §26 measured that approach
#: and refused it. It is noticing the library contradicting *itself*, the same
#: shape as `datasheet_is_not_a_link`: a value whose form disagrees with the
#: field it sits in.
#:
#: Measured over every design in the repo — 91 `power_in` pins on nets that
#: reach a second pin — the split is total. Two pins match, both named exactly
#: `EN`, and both are enables driven by logic: a radio module's enable, driven
#: by a processor GPIO, and an LDO's enable driven by a power sequencer. Every
#: other name is `VDD`,
#: `VCC`, `VS`, `VIN`, `VBUS`, `3V3` and the like. **No genuine rail is lost**
#: — the count is zero, not small.
#:
#: Anchored to the whole pin name on purpose: `VCC_EN` is a rail's enable line
#: and `EN` is the pin itself. `~{…}` is KiCad's overbar notation.
CONTROL_PIN_NAME = re.compile(
    r"^~?\{?(EN|ENABLE|NEN|CE|CS|SHDN|SHUTDOWN|NSHDN|PD|NPD|STBY|STANDBY|"
    r"RST|NRST|RESET|NRESET|MR|ON|ONOFF|ON_OFF|KEY|WAKE|SLP|SLEEP)\}?$",
    re.IGNORECASE)


def is_control_pin(pin) -> bool:
    """Is this `power_in` pin actually a control input the library mistyped?"""
    return bool(CONTROL_PIN_NAME.match(pin.name or ""))
#: Types that mean "this pin sources a supply".
POWER_OUTPUT_TYPES = frozenset({"power_out"})
#: Types that need an external pull to reach a valid high level.
OPEN_DRAIN_TYPES = frozenset({"open_collector", "open_emitter"})
#: The pin type that declares a pin is meant to be left unconnected. Distinct
#: from `Net.has_no_connect`, which is the little X the designer draws. Both
#: count as "deliberately unconnected" and missing this one is the difference
#: between 0 and 42 false findings on these two boards.
NO_CONNECT_TYPE = "no_connect"
#: Pins whose type the library did not state. Never treated as benign.
UNSPECIFIED_TYPE = "unspecified"

#: Below this fraction of typed pins, a type-driven check is reporting more
#: about the symbol library than about the design, and says so.
TYPE_COVERAGE_POOR = 0.80


def _rail_names(schematic: Schematic) -> tuple[set[str], set[str]]:
    """(ground nets, all rail nets), from decoupling topology rather than names."""
    roles = intent_module.classify_rails(schematic)
    return ({r.net for r in roles if r.role == "ground"},
            {r.net for r in roles})


def pin_type_coverage(schematic: Schematic) -> dict:
    """How much of this design the type-driven checks can actually see."""
    pins = [pin for net in schematic.nets for pin in net.pins]
    unspecified = sum(1 for p in pins if p.type == UNSPECIFIED_TYPE)
    total = len(pins)
    return {
        "pins": total,
        "unspecified": unspecified,
        "typed_fraction": (total - unspecified) / total if total else 0.0,
        "by_type": {t: sum(1 for p in pins if p.type == t)
                    for t in sorted({p.type for p in pins})},
    }


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_pin_type_coverage(schematic: Schematic) -> list[Finding]:
    """State up front how blind the type-driven checks are on this design.

    Always emitted. A reviewer who reads "no output conflicts" needs to know
    whether that means the design is clean or the symbols are silent.
    """
    coverage = pin_type_coverage(schematic)
    if not coverage["pins"]:
        return []
    fraction = coverage["typed_fraction"]
    unspecified = coverage["unspecified"]
    severity = "minor" if fraction < TYPE_COVERAGE_POOR else "info"

    detail = [
        f"{unspecified} of {coverage['pins']} pins carry no electrical type, "
        f"so {1 - fraction:.0%} of this design is invisible to any check keyed "
        f"on one",
        "pins by type: " + ", ".join(f"{t} {n}" for t, n
                                     in coverage["by_type"].items()),
    ]
    worst = sorted(
        ((component.reference,
          sum(1 for p in component.pins if p.type == UNSPECIFIED_TYPE))
         for component in schematic.components),
        key=lambda pair: -pair[1])[:5]
    worst = [(ref, count) for ref, count in worst if count]
    if worst:
        detail.append("most untyped parts: " + ", ".join(
            f"{ref} ({count} pins)" for ref, count in worst))

    return [Finding(
        check="pin_type_coverage", severity=severity,
        score=(1 - fraction) / (1 - TYPE_COVERAGE_POOR),
        title=(f"{fraction:.0%} of pins carry an electrical type — the "
               f"type-driven checks below can see that much of the design"),
        detail=detail, measurement=coverage,
        basis=("every pin's `type` field counted straight off the resolved "
               "netlist; `unspecified` means the symbol did not say, which is "
               "a fact about the library rather than about the circuit"),
        intent_needed=("nothing — but a clean result from a type-driven check "
                       "is only as good as this number"))]


def check_unconnected_power_pins(schematic: Schematic) -> list[Finding]:
    """A supply pin that is alone on its net.

    Hard evidence and no name guessing: the pin says it wants a supply and the
    netlist says nothing else is on the wire. Both boards have exactly one, and
    both are worth telling the designer about.
    """
    findings: list[Finding] = []
    for net in schematic.nets:
        if len(net.pins) != 1:
            continue
        pin = net.pins[0]
        if pin.type not in POWER_INPUT_TYPES | POWER_OUTPUT_TYPES:
            continue
        component = schematic.component(pin.reference)
        findings.append(Finding(
            check="unconnected_power_pin",
            severity="minor" if pin.type in POWER_OUTPUT_TYPES else "major",
            score=1.0,
            title=(f"{pin.key} ({pin.name}) is a {pin.type} pin with nothing "
                   f"else on its net"),
            nets=[net.name],
            detail=[
                f"{pin.reference} is "
                f"{component.value if component else 'unknown'}"
                + (f" — {component.description}"
                   if component and component.description else ""),
                f"the net is `{net.name}`, and {pin.key} is its only pin",
                ("nothing sources this rail" if pin.type in POWER_INPUT_TYPES
                 else "this output feeds nothing"),
            ],
            measurement={"pin": pin.key, "pin_name": pin.name,
                         "type": pin.type, "net": net.name},
            basis=("pin electrical type read from the symbol, net membership "
                   "from the resolved hierarchy netlist"),
            intent_needed=("whether this pin is meant to be left open — some "
                           "are, and a no-connect marker would say so")))
    return findings


def check_floating_pins(schematic: Schematic) -> list[Finding]:
    """A pin alone on its net that nothing says is meant to be.

    Two things count as "meant to be", and missing the second one is the
    difference between nothing and 42 wrong findings on these boards:

    * a no-connect marker drawn on the sheet (`Net.has_no_connect`), and
    * the pin's own type being `no_connect`, which is the manufacturer's symbol
      declaring the pin unusable.

    Every single-pin net on both boards here is the second case — 34
    `Reserved` pins on a GNSS module and 8 `NC` pins on the four-layer board.
    Crediting only the marker would report all 42 as floating.
    """
    findings: list[Finding] = []
    for net in schematic.nets:
        if len(net.pins) != 1 or net.has_no_connect:
            continue
        pin = net.pins[0]
        if pin.type in {NO_CONNECT_TYPE} | POWER_INPUT_TYPES | POWER_OUTPUT_TYPES:
            continue  # declared NC, or already reported as a power pin
        if pin.type == "passive":
            # A single passive pin is a test point, a mounting hole, or one leg
            # of a part used as a mechanical anchor. Real, but not a defect.
            severity = "info"
        else:
            severity = "minor"
        findings.append(Finding(
            check="floating_pin", severity=severity, score=1.0,
            title=(f"{pin.key} ({pin.name}) is a {pin.type} pin connected to "
                   f"nothing, with no no-connect marker"),
            nets=[net.name],
            detail=[f"the net is `{net.name}` and {pin.key} is its only pin",
                    "no no-connect marker on the sheet, and the symbol does "
                    "not type the pin as no-connect either"],
            measurement={"pin": pin.key, "pin_name": pin.name,
                         "type": pin.type, "net": net.name},
            basis=("nets of exactly one pin, less those the designer marked "
                   "no-connect and those the symbol types as no-connect"),
            intent_needed="whether this pin is deliberately unused"))
    return findings


def check_output_conflicts(schematic: Schematic) -> list[Finding]:
    """Two devices both driving one net.

    The same-device exclusion is what makes this a check rather than noise:
    both boards have a regulator with two `output`-typed pins that are the same
    physical output brought out twice (U14.4/U14.5 on the six-layer board, U13.4/U13.5 on
    The four-layer board). Counting pins rather than devices reports each of those as a
    short. Counting devices, both boards are clean.
    """
    findings: list[Finding] = []
    for net in schematic.nets:
        drivers = [p for p in net.pins if p.type == "output"]
        devices = sorted({p.reference for p in drivers})
        if len(devices) < 2:
            continue
        findings.append(Finding(
            check="output_conflict", severity="major",
            score=float(len(devices)),
            title=(f"`{net.name}` is driven by {len(devices)} different "
                   f"devices' output pins"),
            nets=[net.name],
            detail=[
                "driving pins: " + ", ".join(f"{p.key}({p.name})"
                                             for p in drivers),
                "two push-pull outputs on one net fight whenever they "
                "disagree; this is only safe if one is open-drain, and no pin "
                "here is typed as one",
            ],
            measurement={"net": net.name, "devices": devices,
                         "pins": [p.key for p in drivers]},
            basis=("pins typed `output` on one net, grouped by device so a "
                   "part with its output brought out on two pads is not "
                   "reported as driving itself"),
            intent_needed=("whether one of these is actually open-drain and "
                           "the symbol does not say so")))
    return findings


def check_missing_pullups(schematic: Schematic) -> list[Finding]:
    """An open-drain pin with no resistor to a supply on its net.

    Keyed on pin type, which is hard evidence, and *not* on pin names. The
    name-driven version was built and measured and is refused — the module
    docstring records why.

    There are no open-drain pins anywhere in this project, so this finds
    nothing here. That is a coverage result, not a clean bill of health, and
    `check_pin_type_coverage` is what says so.
    """
    grounds, rails = _rail_names(schematic)
    net_of = {pin.key: net for net in schematic.nets for pin in net.pins}

    pulled: set[str] = set()
    for component in schematic.components:
        if len(component.pins) != 2 or component.dnp:
            continue
        if not component.reference.startswith("R"):
            continue
        first = net_of.get(component.pins[0].key)
        second = net_of.get(component.pins[1].key)
        if first is None or second is None:
            continue
        supplies = rails - grounds
        if first.name in supplies:
            pulled.add(second.name)
        if second.name in supplies:
            pulled.add(first.name)

    findings: list[Finding] = []
    for net in schematic.nets:
        drains = [p for p in net.pins if p.type in OPEN_DRAIN_TYPES]
        if not drains or net.name in pulled or net.name in rails:
            continue
        findings.append(Finding(
            check="missing_pullup", severity="major", score=float(len(drains)),
            title=(f"`{net.name}` has {len(drains)} open-drain pin(s) and no "
                   f"resistor to a supply"),
            nets=[net.name],
            detail=["open-drain pins: " + ", ".join(f"{p.key}({p.name})"
                                                    for p in drains),
                    "an open-drain output can pull low and cannot drive high, "
                    "so without a pull-up this net has no defined high level"],
            measurement={"net": net.name,
                         "open_drain_pins": [p.key for p in drains]},
            basis=("pins typed open-collector or open-emitter, against "
                   "two-pin resistors joining the net to a rail that "
                   "decoupling topology says is a supply rather than ground"),
            intent_needed=("whether the pull-up is inside a module or on "
                           "another board")))
    return findings


#: Below this, `classify_rails` could not reach its answer structurally and
#: fell back to the net name. §10 recorded that path as never having run on a
#: real board; three of the seven designs here take it, so it is not a corner.
RAIL_CONFIDENCE_WEAK = 0.9


def rail_assumptions(schematic: Schematic, roles) -> list[str]:
    """State how ground was found, when it was not found structurally.

    `classify_rails` computes a confidence and, until this existed, the report
    threw it away. That matters because the structural answer needs rails to
    *be* rails — nets driven by power symbols — and a designer who draws supplies
    as ordinary labels leaves it nothing to work with. Measured over every
    design here:

    | design | power-symbol nets | ground found by |
    |---|---|---|
    | the six-layer board, the four-layer board, the lightly-routed six-layer board, Compute Module | 4-9 | decoupling topology |
    | **the unreferenced four-layer board** (210 parts, 195 nets) | **2** | **its name** |
    | the smallest board | 1 | its name |
    | **a third-party example board** | **3, none of them a ground** | **not at all** |

    So this is not the small-board corner §33 predicted. A full six-layer
    design lands in it, and one board has no identifiable ground at all — which
    silently disabled the decoupling check until `check_decoupling` was made to
    say so.
    """
    notes: list[str] = []
    power_nets = [n for n in schematic.nets if n.source == "power"]
    grounds = [r for r in roles if r.role == "ground"]

    if not grounds:
        notes.append(
            f"no ground rail could be identified — only {len(power_nets)} net(s) "
            f"here are driven by power symbols and none of them reads as a "
            f"ground, so every check that needs to know what ground is was "
            f"unable to run rather than finding nothing")
    elif any(r.confidence < RAIL_CONFIDENCE_WEAK for r in grounds):
        notes.append(
            f"ground was taken from the net name `{grounds[0].net}`, not from "
            f"decoupling topology — only {len(power_nets)} net(s) in this design "
            f"are driven by power symbols, too few for the structural test, so "
            f"anything resting on which net is ground is a guess here")
    return notes


def check_decoupling(schematic: Schematic) -> list[Finding]:
    """A supply pin on a rail with no decoupling capacitor anywhere.

    Deliberately a weak, topological question. Whether the capacitor is *near*
    the pin — which is the part that actually matters — needs the layout, and
    `placement.py` answers it with real loop-area geometry. Asking it here from
    a netlist would mean inventing a "within N components" distance that has no
    physical meaning.

    What this catches is the case the layout check cannot: a locally filtered
    rail that was never given a capacitor at all.
    """
    grounds, _ = _rail_names(schematic)
    if not grounds:
        # Used to `return []`, which is indistinguishable in the report from a
        # board with every rail decoupled. `the processor module-4.1-example` hits this: its
        # three power-symbol nets are +3V3, +5V and +5VL, so there is no ground
        # to measure a capacitor *to*, and the check read as clean.
        candidates = sorted({net.name for net in schematic.nets
                             if any(p.type in POWER_INPUT_TYPES
                                    for p in net.pins)})
        return [Finding(
            check="decoupling", severity="info", score=0.0,
            title=("decoupling was not checked — no ground rail could be "
                   "identified in this schematic"),
            detail=[
                "ground is found structurally, as the node two or more "
                "supplies' decoupling capacitors return to; no net here "
                "qualifies and none is driven by a ground power symbol",
                f"{len(candidates)} net(s) feed a `power_in` pin and went "
                f"unexamined: " + ", ".join(f"`{n}`" for n in candidates[:8])
                + (" …" if len(candidates) > 8 else ""),
                "this is a zero because the check could not run, not because "
                "the design is clean",
            ],
            measurement={"grounds": [], "unexamined_nets": candidates},
            basis="`intent.classify_rails`, which needs nets driven by power "
                  "symbols and found no ground among them",
            intent_needed="which net is ground")]
    net_of = {pin.key: net for net in schematic.nets for pin in net.pins}

    decoupled: set[str] = set()
    for component in schematic.components:
        if len(component.pins) != 2 or component.dnp:
            continue
        if not component.reference.startswith("C"):
            continue
        first = net_of.get(component.pins[0].key)
        second = net_of.get(component.pins[1].key)
        if first is None or second is None:
            continue
        if first.name in grounds:
            decoupled.add(second.name)
        if second.name in grounds:
            decoupled.add(first.name)

    findings: list[Finding] = []
    for net in schematic.nets:
        if net.name in grounds or net.name in decoupled or len(net.pins) < 2:
            continue
        typed = [p for p in net.pins if p.type in POWER_INPUT_TYPES]
        if not typed:
            continue
        consumers = [p for p in typed if not is_control_pin(p)]
        if not consumers:
            # Every supply pin on this net is named like a control input, so
            # this is an enable line the library typed `power_in`, not a rail.
            # Reported rather than dropped: the mistyping is a real (small)
            # defect, and a reader who sees no decoupling finding here should
            # know it was skipped and why.
            findings.append(Finding(
                check="decoupling", severity="info", score=0.0,
                title=(f"`{net.name}` was not checked for decoupling — its "
                       f"supply pin(s) are named like control inputs"),
                nets=[net.name],
                detail=[
                    "pins: " + ", ".join(f"{p.key}({p.name})" for p in typed),
                    "these are typed `power_in` by the symbol library but "
                    "named as enables, so the net is read as a control line "
                    "rather than a supply rail — a capacitor on it would be "
                    "wrong, not missing",
                    "the library typing is the defect here, not the circuit",
                ],
                measurement={"net": net.name,
                             "control_pins": [p.key for p in typed]},
                basis=("pin name matched against a whole-name list of control "
                       "inputs; over every design here the split from real "
                       "supply names is total and no genuine rail matches"),
                intent_needed=("whether this pin really is an enable — the "
                               "symbol library is the only evidence either "
                               "way")))
            continue
        severity, score = _severity(float(len(consumers)), 1.0, 3.0)
        findings.append(Finding(
            check="decoupling", severity=severity, score=score,
            title=(f"`{net.name}` feeds {len(consumers)} power pin(s) and has "
                   f"no decoupling capacitor to ground anywhere"),
            nets=[net.name],
            detail=[
                "power pins on it: " + ", ".join(f"{p.key}({p.name})"
                                                 for p in consumers),
                "no two-pin capacitor joins this net to a ground rail",
                "this is the topological question only — whether a capacitor "
                "is close enough to the pin is a layout question, answered by "
                "the placement check against real loop geometry",
            ],
            measurement={"net": net.name,
                         "power_pins": [p.key for p in consumers]},
            basis=("two-pin capacitors between this net and a rail that "
                   "decoupling topology identifies as ground; ground is found "
                   "structurally, not from net names"),
            intent_needed=("whether this rail is decoupled off-board or "
                           "inside a module")))
    return findings


#: How each field anomaly is reported. `severity` is the *ceiling*; a kind that
#: affects one part is reported a step lower than one affecting forty.
#:
#: `one_off_field` is `info` on purpose. A part imported from LCSC brings a
#: dozen parametric fields with it — `GYROSCOPE MEASUREMENT RANGE (MAX)` and so
#: on — and those are noise, not mistakes. The check still runs because the same
#: rule is what surfaces `Field5`, which held an orderable part number.
FIELD_ANOMALY_REPORTING = {
    "missing_footprint": ("major", "a part with no footprint cannot be placed"),
    "supplier_code_in_unexpected_field":
        ("minor", "the orderable part number is somewhere no BOM tool looks"),
    "datasheet_is_not_a_link":
        ("minor", "a part number filed under Datasheet is lost to any tool"),
    "inconsistent_field_naming":
        ("minor", "one datum under two spellings; a lookup finds one of them"),
    "missing_value": ("minor", "the BOM line has nothing to identify it"),
    "link_in_unexpected_field": ("info", "a URL filed somewhere unusual"),
    "one_off_field": ("info", "a field used on almost nothing"),
}


def check_field_hygiene(schematic: Schematic) -> list[Finding]:
    """Component fields whose content does not match what they are called.

    Component fields are hand-typed and have no schema, so the same datum lands
    wherever the person putting it in happened to put it. On the six-layer board:

    * **39 of 229 parts carry their LCSC catalogue number in `Description`**;
    * one carries it in a field called `Field5`;
    * two carry a manufacturer part number in `Datasheet`;
    * eight field names exist in two capitalisations at once
      (`SUPPLIER PART` and `Supplier Part`).

    None of that is visible to a lookup by field name — `Component.mpn` finds
    what is where it should be and is silent about the rest. This compares the
    *shape* of every value against the *name* of the field holding it, and
    reports the mismatches.

    It reports; it does not correct, and it does not guess which is the mistake.
    Whether `Description` holding a part number is an error or a house
    convention is a judgement about this team's practice, and the job here is to
    put the evidence in front of whoever makes it. `Schematic.field_survey()`
    returns the whole picture — every field, its usage count, the shapes of its
    values and examples — for exactly that.

    One finding per *kind*, not per part, or the six-layer board alone would produce 90.
    """
    survey = schematic.field_survey()
    findings: list[Finding] = []

    for kind in survey.kinds:
        items = survey.by_kind(kind)
        ceiling, why = FIELD_ANOMALY_REPORTING.get(
            kind, ("info", "an unexpected field entry"))
        # One affected part is a slip; forty is a convention worth naming.
        severity = ceiling
        if ceiling == "major" and len(items) < 2:
            severity = "minor"
        elif ceiling == "minor" and len(items) < 2:
            severity = "info"

        affected = sorted({a.reference for a in items if a.reference != "(design)"})
        fields = sorted({a.key for a in items})
        detail = [str(a) for a in items[:6]]
        if len(items) > 6:
            detail.append(f"... and {len(items) - 6} more")
        if affected:
            detail.append(f"parts affected: " + ", ".join(affected[:12])
                          + (f" and {len(affected) - 12} more"
                             if len(affected) > 12 else ""))

        findings.append(Finding(
            check="field_hygiene", severity=severity,
            score=float(len(items)),
            title=(f"{len(items)} field entry(s) of kind `{kind}` — {why}"),
            detail=detail,
            measurement={"kind": kind, "count": len(items),
                         "fields": fields, "references": affected},
            basis=("every component property classified by the *shape* of its "
                   "value — LCSC code, URL, path, part number, component value "
                   "— and compared against what the field is named"),
            intent_needed=("whether this is a mistake or this team's own "
                           "convention; the tool cannot tell, and the "
                           "distinction is about practice rather than about "
                           "the circuit")))

    if survey.unsourceable:
        fraction = len(survey.unsourceable) / max(survey.total_components, 1)
        findings.append(Finding(
            check="field_hygiene", severity="info",
            score=float(len(survey.unsourceable)),
            title=(f"{len(survey.unsourceable)} of {survey.total_components} "
                   f"parts ({fraction:.0%}) carry neither a manufacturer part "
                   f"number nor a supplier code"),
            detail=[
                "cannot be ordered from this schematic alone: "
                + ", ".join(survey.unsourceable[:12])
                + (f" and {len(survey.unsourceable) - 12} more"
                   if len(survey.unsourceable) > 12 else ""),
                "generic passives are often ordered on value and package "
                "alone, so a high count here is not automatically a problem — "
                "but it is the reason a BOM cannot be handed to an assembler "
                "unedited",
            ],
            measurement={"unsourceable": survey.unsourceable,
                         "fraction": round(fraction, 3)},
            basis=("no value under any manufacturer-part or supplier-part "
                   "alias, after the alias and shape rules that find them "
                   "wherever they were actually filed"),
            intent_needed=("which of these are generic parts the assembler "
                           "chooses and which are specific parts whose number "
                           "was never recorded")))
    return findings


def check_dnp_dependencies(schematic: Schematic) -> list[Finding]:
    """What stops working when the not-fitted parts are actually not fitted.

    "A DNP part carries a net" is true of every DNP part and is therefore not a
    finding. The sharp version is a cut test, the same shape as
    `audit.check_conductor_necks`: rebuild the netlist without the DNP parts
    and report the nets that lose their last real connection.

    On the six-layer board this finds one net: a sensor marked do-not-fit
    whose interrupt line is the only thing driving it, so with the board as
    built the processor module input floats.
    """
    unfitted = {c.reference for c in schematic.components if c.dnp}
    if not unfitted:
        return []

    findings: list[Finding] = []
    for net in schematic.nets:
        lost = [p for p in net.pins if p.reference in unfitted]
        if not lost:
            continue
        remaining = [p for p in net.pins if p.reference not in unfitted]
        if len(remaining) >= 2 or not remaining:
            # Still a net without them, or it was only ever the DNP part's own
            # stub — neither is a surprise.
            continue
        survivor = remaining[0]
        findings.append(Finding(
            check="dnp_dependency", severity="minor",
            score=1.0,
            title=(f"`{net.name}` is left with only {survivor.key} once the "
                   f"do-not-fit parts are off the board"),
            nets=[net.name],
            detail=[
                "not fitted: " + ", ".join(f"{p.key}({p.name})" for p in lost),
                f"still connected: {survivor.key}({survivor.name}), "
                f"a {survivor.type} pin",
                "as built, that pin has nothing on the other end",
            ],
            measurement={"net": net.name,
                         "removed": [p.key for p in lost],
                         "remaining": survivor.key,
                         "remaining_type": survivor.type},
            basis=("netlist rebuilt with every part marked DNP removed, then "
                   "nets left with fewer than two pins reported — the same cut "
                   "test the conductor-neck check uses on copper"),
            intent_needed=("whether this option is ever fitted, and whether "
                           "the surviving pin minds floating")))
    return findings


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

CHECKS = {
    "pin_type_coverage": check_pin_type_coverage,
    "unconnected_power_pin": check_unconnected_power_pins,
    "floating_pin": check_floating_pins,
    "output_conflict": check_output_conflicts,
    "missing_pullup": check_missing_pullups,
    "decoupling": check_decoupling,
    "dnp_dependency": check_dnp_dependencies,
    "field_hygiene": check_field_hygiene,
}


def audit_schematic(schematic: Schematic, skip: tuple[str, ...] = ()
                    ) -> AuditReport:
    """Review a netlist on its own, with no board and no stackup.

    Deliberately *not* a flag on `audit.audit()`. That function refuses without
    a validated stackup and must keep refusing — every impedance and coupling
    number it produces depends on real dielectric heights. A stackup is
    meaningless for a schematic, so rather than make the gate conditional, this
    is a separate function that never goes near it.
    """
    if schematic is None or not schematic.nets:
        raise AuditError("no netlist to review")

    started = time.time()
    findings: list[Finding] = []
    checks: list[CheckResult] = []

    for name, function in CHECKS.items():
        if name in skip:
            checks.append(CheckResult(name, ran=False,
                                      skipped_because="skipped by the caller"))
            continue
        start = time.time()
        try:
            produced = function(schematic)
        except Exception as exc:                          # noqa: BLE001
            checks.append(CheckResult(
                name, ran=False,
                skipped_because=f"{type(exc).__name__}: {exc}"))
            continue
        findings.extend(produced)
        checks.append(CheckResult(name, ran=True, seconds=time.time() - start,
                                  findings=len(produced)))

    coverage = pin_type_coverage(schematic)
    inferred = intent_module.infer(schematic)
    assumptions = [
        f"{coverage['unspecified']} of {coverage['pins']} pins carry no "
        f"electrical type, so every type-driven check here saw at most "
        f"{coverage['typed_fraction']:.0%} of the design",
        "no board was given, so nothing about placement, proximity, loop area "
        "or impedance was checked — those need the layout",
        "two checks from the plan are deliberately absent because measuring "
        "them on real boards showed them to be wrong more often than right: "
        "`power_in` with no `power_out` driver, and I2C pull-ups found from "
        "pin names. See the module docstring.",
    ]
    assumptions.extend(rail_assumptions(schematic, inferred.roles))
    if schematic.diagnostics:
        assumptions.append(
            f"the netlist parser recorded {len(schematic.diagnostics)} "
            f"diagnostic(s); see `Schematic.diagnostics`")

    findings.sort(key=lambda f: f.rank)
    return AuditReport(
        board=schematic.root.stem,
        board_summary={
            "sheets": len(schematic.sheets),
            "parts": len({c.reference for c in schematic.components}),
            "nets": len(schematic.nets),
            "pins": coverage["pins"],
        },
        stackup="not applicable — schematic review",
        findings=findings, checks=checks,
        intent=inferred.summary(),
        intent_evidence=[f"{r.net}: {r.role} ({r.confidence:.2f}) — "
                         f"{r.evidence}" for r in inferred.roles],
        assumptions=assumptions,
        seconds=time.time() - started)


def audit_schematic_file(path: str | Path, **kwargs) -> AuditReport:
    """Resolve a hierarchy from its root sheet and review it."""
    return audit_schematic(netlist(path), **kwargs)


USAGE = """\
usage: python -m pcbreview.schematic_audit <top.kicad_sch> [--report]

  (no flag)   run the eight schematic checks and print the ranked findings.
              **This is the invocation that reviews the schematic.**
  --report    print a DESCRIPTIVE dump instead — summary, designator table,
              sheet tree, net table, BOM. It contains NO findings, and a
              schematic reviewed only this way reads as clean whatever is
              wrong with it. Run the bare form as well, always.

The layout entry point (`python -m pcbreview.audit`) does not run these
checks, even when it is given the schematic. A full review needs both.
"""


def main(argv: list[str]) -> int:
    import sys

    from pcbreview import console_utf8

    console_utf8()

    # `--help` used to be read as the schematic path and die with
    # `No such file or directory: '.../--help'`, which left SKILL.md as the
    # only source of truth for the flags — and SKILL.md documented the one
    # invocation that emits nothing (§54.1).
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if len(argv) < 2:
        sys.stderr.write(USAGE)
        return 2
    # An empty or template schematic is a normal thing to be handed — the
    # Training Examples board in this project is exactly that, 0 components and
    # 0 nets — so it gets a sentence rather than a traceback.
    try:
        if "--report" in argv:
            print(netlist(argv[1]).report())
            # Said on the artefact itself, not only in SKILL.md. Two outside
            # sessions ran this, searched the output for a findings section,
            # found none, and one of them recovered only by guessing — on a
            # board whose bare run reports two majors (§54.1).
            print("\nThis is a descriptive dump and contains no findings. "
                  "Run the same command WITHOUT --report to review this "
                  "schematic.")
            return 0
        print(audit_schematic_file(argv[1]).report(limit=None))
    except AuditError as exc:
        sys.stderr.write(f"{Path(argv[1]).name}: {exc}\n")
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{Path(argv[1]).name}: could not be read — {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
