"""Whole-hierarchy schematic netlist.

`_schematic_base` resolves connectivity within a single `.kicad_sch` file.
That is not a netlist for a hierarchical design, for three reasons:

1. **Designators are per placement.** A sheet placed four times (this project
   has four a repeated sheets) has four sets of reference designators, recorded
   against sheet-instance paths in each symbol's `(instances ...)` block. The
   `Reference` property holds only whichever placement was edited last, so
   parsing the file standalone attributes every net to one arbitrary channel.

2. **Net identity is scoped.** A local label `SENSE` means a different net in
   each placement of a sheet; a global label or power symbol means the same net
   everywhere. Merging label text within a file conflates the two.

3. **Nets cross the boundary.** A child's hierarchical label and the matching
   pin on the parent's sheet symbol are one net, and nothing within either file
   says so.

This module walks the sheet tree from the root, gives every point a
(sheet instance, coordinate) identity, and unions across boundaries by
matching sheet pins to hierarchical labels by name.

    from pcbreview.schematic import netlist
    sch = netlist("board.kicad_sch")
    sch.nets_by_name["/UART2 RX"].pins

Validated against KiCad's own net assignment, read back from the fabricated
board — see `tests/test_hierarchy.py`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import _schematic_base as base


# ---------------------------------------------------------------------------
# Property aliases
#
# The designer decides what a property is called, and on these two boards the
# manufacturer part number appears under six different spellings — `Manufacturer
# Part`, `MP`, `MANUFACTURER PART`, and so on. Anything keyed on one exact
# spelling silently reports "no MPN" for most of the board, so lookups here are
# alias-driven and case-insensitive. First non-empty alias wins, in the order
# listed, most specific first.
# ---------------------------------------------------------------------------

MPN_ALIASES = ("manufacturer part number", "manufacturer part", "mpn",
               "mfr part", "part number", "mp")
MANUFACTURER_ALIASES = ("manufacturer", "mfr", "mf", "mfg")
SUPPLIER_PART_ALIASES = ("lcsc", "lcsc part #", "lcsc part number",
                         "supplier part number", "supplier part",
                         "jlcpcb part", "lcsc part")

#: An LCSC/JLCPCB part number. `supplier_part` prefers a value of this shape
#: over alias order, and the reason is a real trap on these boards: the property
#: called `LCSC Part Name` does **not** hold a part number. It holds the
#: catalogue description, in Chinese — `1个N沟道 耐压:30V 电流:8A`. The orderable
#: code lives in `LCSC`, `LCSC Part #` or `Supplier Part`.
#:
#: Measured over both boards, the split is total: those three keys are
#: `C`-code-shaped in 62 of 62 values and `LCSC Part Name` in 0 of 26. So the
#: shape test is what actually finds the orderable number, and alias order alone
#: would hand back a description for every part that has both.
LCSC_CODE = re.compile(r"^C\d{3,}$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SheetInstance:
    """One *placement* of a schematic file in the hierarchy."""

    index: int
    path: Path
    uuid_path: tuple[str, ...]
    name_path: tuple[str, ...]
    parent: int | None

    @property
    def instance_path(self) -> str:
        """The key KiCad stores designators against: `/uuid/uuid/...`."""
        return "/" + "/".join(self.uuid_path)

    @property
    def depth(self) -> int:
        return len(self.name_path)

    @property
    def label_prefix(self) -> str:
        """Net-name prefix KiCad gives labels in this sheet: `/`, `/Power/`."""
        return "/" + "".join(name + "/" for name in self.name_path)

    def __str__(self) -> str:
        return self.label_prefix


@dataclass(frozen=True)
class PinRef:
    reference: str
    number: str
    name: str
    type: str
    sheet: int

    @property
    def key(self) -> str:
        return f"{self.reference}.{self.number}"


@dataclass
class Component:
    reference: str
    value: str
    footprint: str
    lib_id: str
    unit: int
    dnp: bool
    sheet: int
    uuid: str | None
    pins: list[PinRef] = field(default_factory=list)
    #: every `(property ...)` on the symbol — MPN, Datasheet, Description, and
    #: whatever else the designer added. `Value` and `Footprint` are in here
    #: too, duplicating the fields above.
    #:
    #: `Reference` is the one entry that is *not* verbatim: in the file it holds
    #: whichever placement was edited last, so on a sheet placed four times it
    #: is wrong for three of them (see the module docstring). It is rewritten
    #: here to this placement's resolved designator, so it always agrees with
    #: `Component.reference`.
    properties: dict[str, str] = field(default_factory=dict)

    def property_of(self, *aliases: str) -> str | None:
        """First non-empty property matching any alias, case-insensitively."""
        folded = {k.strip().lower(): v for k, v in self.properties.items()}
        for alias in aliases:
            value = folded.get(alias)
            if value and value.strip():
                return value.strip()
        return None

    @property
    def mpn(self) -> str | None:
        return self.property_of(*MPN_ALIASES)

    @property
    def manufacturer(self) -> str | None:
        return self.property_of(*MANUFACTURER_ALIASES)

    @property
    def supplier_part(self) -> str | None:
        """The orderable LCSC/JLCPCB code, preferring one that looks like one.

        Shape wins over alias order here — see `LCSC_CODE` for why. Where no
        alias holds a code-shaped value the first non-empty one is returned
        anyway, so a part with only a description still reports something rather
        than nothing; check `LCSC_CODE.match` if you need an orderable code.
        """
        for alias in SUPPLIER_PART_ALIASES:
            value = self.property_of(alias)
            if value and LCSC_CODE.match(value):
                return value
        return self.property_of(*SUPPLIER_PART_ALIASES)

    @property
    def datasheet(self) -> str | None:
        return self.property_of("datasheet")

    @property
    def description(self) -> str | None:
        return self.property_of("description")


@dataclass
class Net:
    name: str
    pins: list[PinRef]
    #: what named the net: power | global | local | hierarchical | sheet_pin | auto
    source: str
    has_no_connect: bool = False
    sheets: frozenset[int] = frozenset()

    @property
    def pin_keys(self) -> set[str]:
        return {p.key for p in self.pins}

    @property
    def is_power(self) -> bool:
        return self.source == "power"

    @property
    def crosses_sheets(self) -> bool:
        return len(self.sheets) > 1


# ---------------------------------------------------------------------------
# Field shapes
#
# Component fields are hand-typed and there is no schema. The same datum turns
# up under `LCSC`, `LCSC Part #`, `Supplier Part`, `Field5`, and — on 39 parts
# of one board here — inside `Description`. Nothing in KiCad objects.
#
# So values are classified by *shape* and reported beside the key they were
# found under, and the mismatch between the two is what a reviewer acts on.
# ---------------------------------------------------------------------------

#: An LCSC/JLCPCB catalogue number.
#: A field used on this many parts or fewer, on a design large enough to have
#: conventions, is either a typo for a field used elsewhere or a one-off note.
#: A judgement call — it exists to surface `Field5`, which held an LCSC code.
ONE_OFF_FIELD_LIMIT = 2

_SHAPE_LCSC = re.compile(r"^C\d{3,}$")
_SHAPE_URL = re.compile(r"^(https?://|www\.)", re.I)
_SHAPE_PATH = re.compile(r"^([A-Za-z]:[\\/]|[\\/~.]|\S+\.(pdf|htm|html)$)", re.I)
#: Loose: mixed letters and digits, no spaces, long enough not to be a value.
_SHAPE_PART_NUMBER = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][\w./+-]{4,}$")
#: A component value: `4.7uF`, `10k`, `3.16kR`, `0.1uF 35V`.
_SHAPE_VALUE = re.compile(
    r"^\d+(\.\d+)?\s*[munpkKMR]?[ΩRFHΩ]?\w*(\s|$)", re.I)


def value_shape(value: str) -> str:
    """What a field's *value* looks like, independent of what it is called.

    `lcsc_code | url | path | part_number | value | text | empty`
    """
    text = (value or "").strip()
    if not text or text == "~":
        return "empty"
    if _SHAPE_LCSC.match(text):
        return "lcsc_code"
    if _SHAPE_URL.match(text):
        return "url"
    if _SHAPE_PATH.match(text):
        return "path"
    if _SHAPE_VALUE.match(text):
        return "value"
    if _SHAPE_PART_NUMBER.match(text):
        return "part_number"
    return "text"


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


#: Keys whose *name* says they hold a supplier catalogue number.
_SUPPLIER_KEY = re.compile(r"lcsc|supplier|jlc|digikey|mouser|farnell|rs",
                           re.I)
#: Keys whose name says they hold a manufacturer part number.
_MPN_KEY = re.compile(r"(manufacturer|mfr|mfg)?\s*(part|mpn|mp)\b|^mpn$|^mp$",
                      re.I)


@dataclass
class FieldAnomaly:
    """One component field whose content does not match its name.

    Reported rather than corrected. Which of these is a mistake and which is a
    local convention is a judgement, and this is the layer that surfaces the
    evidence for it.
    """
    kind: str
    reference: str
    key: str
    value: str
    note: str

    def __str__(self) -> str:
        return f"{self.reference}: {self.key!r} = {self.value!r} — {self.note}"


@dataclass
class FieldSurvey:
    """Every component field on the design, grouped so oddities stand out."""
    #: normalised key -> the spellings actually used
    spellings: dict[str, set[str]] = field(default_factory=dict)
    #: key (as written) -> how many components carry it
    usage: dict[str, int] = field(default_factory=dict)
    #: key -> shape -> count
    shapes: dict[str, dict[str, int]] = field(default_factory=dict)
    #: key -> a few example values
    examples: dict[str, list[str]] = field(default_factory=dict)
    anomalies: list[FieldAnomaly] = field(default_factory=list)
    #: components with no manufacturer or supplier part number at all
    unsourceable: list[str] = field(default_factory=list)
    total_components: int = 0

    def by_kind(self, kind: str) -> list[FieldAnomaly]:
        return [a for a in self.anomalies if a.kind == kind]

    @property
    def kinds(self) -> list[str]:
        return sorted({a.kind for a in self.anomalies})

    def summary(self) -> dict:
        return {
            "components": self.total_components,
            "distinct_fields": len(self.usage),
            "anomalies": len(self.anomalies),
            "by_kind": {k: len(self.by_kind(k)) for k in self.kinds},
            "unsourceable": len(self.unsourceable),
            "inconsistent_spellings": {
                k: sorted(v) for k, v in self.spellings.items() if len(v) > 1},
        }


@dataclass(frozen=True)
class SeriesReach:
    """A net reachable from another by passing *through* two-terminal parts."""
    net: str
    depth: int
    #: the parts traversed to get here, nearest first
    through: tuple[str, ...]
    #: `True` when this net is a power rail, which is where a walk stops
    is_rail: bool = False

    def __str__(self) -> str:
        if not self.through:
            return self.net
        return f"{self.net} (through {' -> '.join(self.through)})"


@dataclass
class SeriesElement:
    """One two-terminal part, and the two nets it joins."""
    reference: str
    value: str
    net_a: str
    net_b: str

    def other(self, net: str) -> str | None:
        if net == self.net_a:
            return self.net_b
        if net == self.net_b:
            return self.net_a
        return None


def _reference_sort_key(reference: str) -> tuple:
    """Sort `R2` before `R10`, which a plain string sort does not."""
    prefix = reference_prefix(reference)
    digits = "".join(c for c in reference[len(prefix):] if c.isdigit())
    return (prefix, int(digits) if digits else 0, reference)


def reference_prefix(reference: str) -> str:
    """`R12` -> `R`, `#PWR03` -> `#PWR`."""
    prefix = ""
    for char in reference:
        if char.isalpha() or char in "_#":
            prefix += char
        else:
            break
    return prefix


@dataclass
class BomLine:
    """One orderable part, and every designator that wants it."""
    references: list[str]
    value: str
    footprint: str
    lib_id: str
    dnp: bool
    mpn: str | None = None
    manufacturer: str | None = None
    supplier_part: str | None = None
    description: str | None = None

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def package(self) -> str:
        """Footprint without its library prefix."""
        return self.footprint.rsplit(":", 1)[-1]

    def __str__(self) -> str:
        part = self.mpn or self.supplier_part or self.lib_id
        return (f"{self.quantity}x {self.value or '?'} [{self.package or '?'}] "
                f"{part}" + ("  DNP" if self.dnp else ""))


@dataclass
class Schematic:
    root: Path
    sheets: list[SheetInstance]
    components: list[Component]
    nets: list[Net]
    diagnostics: list[str]
    title: dict[str, str] = field(default_factory=dict)

    @property
    def nets_by_name(self) -> dict[str, Net]:
        return {n.name: n for n in self.nets}

    def net_of(self, reference: str, pin: str) -> Net | None:
        want = f"{reference}.{pin}"
        for net in self.nets:
            if want in net.pin_keys:
                return net
        return None

    def component(self, reference: str) -> Component | None:
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    # -- sourcing ---------------------------------------------------------

    @property
    def power_nets(self) -> list[str]:
        """Nets driven by a power symbol — exact, not a name test."""
        return sorted({n.name for n in self.nets if n.source == "power"})

    def counts_by_prefix(self) -> dict[str, int]:
        """How many distinct parts of each designator prefix — `{'C': 61, ...}`.

        Counts *parts*, not symbols, so a three-unit op-amp drawn as U8A/B/C is
        one `U`.
        """
        counts: dict[str, int] = defaultdict(int)
        for reference in {c.reference for c in self.components}:
            prefix = reference_prefix(reference)
            if prefix:
                counts[prefix] += 1
        return dict(sorted(counts.items()))

    def bom(self) -> list[BomLine]:
        """Group the design into orderable lines.

        Two things this has to get right that a flat symbol loop does not:

        * **A multi-unit part is one part.** the four-layer board's U8 is drawn as three
          units, all on one footprint, so it is one line of quantity one.
          References are deduplicated before counting.
        * **A sheet placed four times needs four sets of parts.** That falls out
          of the hierarchy walk — each placement carries its own designators —
          and is why the BOM is built here rather than per file.

        The grouping key includes `dnp`, so a fitted and a not-fitted part of
        the same value are two lines. The alternative — one line with the flag
        OR'd across it, which is what the old skill did — marks the whole line
        do-not-fit when only one of them is, and quantity then means neither
        "how many to order" nor "how many are placed". Neither board here has a
        mixed group to demonstrate it on, so this is reasoning about ordering
        rather than a measured finding.
        """
        by_reference: dict[str, Component] = {}
        for component in self.components:
            # Units of one part are identical apart from their pins; keep the
            # first and let its properties speak for the whole part.
            by_reference.setdefault(component.reference, component)

        groups: dict[tuple, list[Component]] = defaultdict(list)
        for component in by_reference.values():
            groups[(component.value, component.footprint, component.lib_id,
                    component.mpn, component.dnp)].append(component)

        lines = []
        for (value, footprint, lib_id, mpn, dnp), members in groups.items():
            first = members[0]
            lines.append(BomLine(
                references=sorted({m.reference for m in members},
                                  key=_reference_sort_key),
                value=value, footprint=footprint, lib_id=lib_id, dnp=dnp,
                mpn=mpn, manufacturer=first.manufacturer,
                supplier_part=first.supplier_part,
                description=first.description))
        lines.sort(key=lambda line: _reference_sort_key(line.references[0]))
        return lines

    # -- field hygiene ----------------------------------------------------

    def field_survey(self) -> FieldSurvey:
        """Every component field, grouped so hand-entry oddities stand out.

        Component fields have no schema and are typed by a human, so the same
        datum lands wherever that human put it. Measured on the six-layer board:
        **39 of 229 parts carry their LCSC catalogue number in `Description`**,
        one carries it in a field called `Field5`, two carry a manufacturer
        part number in `Datasheet`, and eight field names exist in two
        capitalisations at once.

        None of that is detectable by looking a key up by name, which is what
        `Component.mpn` and friends do — they find what is where it should be
        and are silent about the rest. This looks at *shape against name* and
        reports the mismatches.

        It reports; it does not correct. Whether `Description` holding a part
        number is a mistake or a local convention is a judgement, and the point
        of this is to put the evidence in front of whoever makes it.
        """
        survey = FieldSurvey(total_components=len({c.reference
                                                   for c in self.components}))
        seen: set[str] = set()
        for component in self.components:
            if component.reference in seen:
                continue          # one unit is enough; they share properties
            seen.add(component.reference)

            for key, raw in component.properties.items():
                value = (raw or "").strip()
                survey.usage[key] = survey.usage.get(key, 0) + 1
                survey.spellings.setdefault(_normalise_key(key), set()).add(key)
                shape = value_shape(value)
                survey.shapes.setdefault(key, {})
                survey.shapes[key][shape] = survey.shapes[key].get(shape, 0) + 1
                if value and len(survey.examples.setdefault(key, [])) < 3:
                    survey.examples[key].append(value)

                lowered = key.strip().lower()
                is_supplier_key = bool(_SUPPLIER_KEY.search(lowered))

                # A catalogue number somewhere that is not a supplier field.
                if shape == "lcsc_code" and not is_supplier_key:
                    survey.anomalies.append(FieldAnomaly(
                        kind="supplier_code_in_unexpected_field",
                        reference=component.reference, key=key, value=value,
                        note=(f"looks like an LCSC catalogue number but the "
                              f"field is called {key!r}, so nothing keyed on "
                              f"supplier fields will find it")))

                # A datasheet field that is not a link.
                if lowered == "datasheet" and shape not in ("empty", "url",
                                                            "path"):
                    survey.anomalies.append(FieldAnomaly(
                        kind="datasheet_is_not_a_link",
                        reference=component.reference, key=key, value=value,
                        note=(f"the Datasheet field holds a {shape}, not a "
                              f"link — a part number here is invisible to any "
                              f"BOM tool")))

                # A URL filed somewhere other than a datasheet-ish field.
                if shape == "url" and "datasheet" not in lowered \
                        and "url" not in lowered and "link" not in lowered:
                    survey.anomalies.append(FieldAnomaly(
                        kind="link_in_unexpected_field",
                        reference=component.reference, key=key, value=value,
                        note=f"a URL under {key!r}"))

            if not component.mpn and not component.supplier_part:
                survey.unsourceable.append(component.reference)

            if not (component.value or "").strip():
                survey.anomalies.append(FieldAnomaly(
                    kind="missing_value", reference=component.reference,
                    key="Value", value="",
                    note="no Value, so the BOM line has nothing to identify it"))
            if not (component.footprint or "").strip():
                survey.anomalies.append(FieldAnomaly(
                    kind="missing_footprint", reference=component.reference,
                    key="Footprint", value="",
                    note="no Footprint"))

        # Field names that differ only in capitalisation or punctuation.
        for normalised, spellings in survey.spellings.items():
            if len(spellings) < 2:
                continue
            for spelling in sorted(spellings)[1:]:
                survey.anomalies.append(FieldAnomaly(
                    kind="inconsistent_field_naming", reference="(design)",
                    key=spelling, value="",
                    note=(f"the same field is also spelled "
                          f"{sorted(spellings)[0]!r}; a lookup by exact name "
                          f"sees only one of them")))

        # A field used on one or two parts when the design has conventions.
        for key, count in survey.usage.items():
            if _normalise_key(key) in ("reference", "value", "footprint",
                                       "datasheet", "description"):
                continue
            if count <= ONE_OFF_FIELD_LIMIT and survey.total_components > 20:
                survey.anomalies.append(FieldAnomaly(
                    kind="one_off_field", reference="(design)", key=key,
                    value="; ".join(survey.examples.get(key, [])),
                    note=(f"used on {count} part(s) out of "
                          f"{survey.total_components} — either a typo for a "
                          f"field used elsewhere, or a one-off note")))
        return survey

    # -- tracing through parts --------------------------------------------

    def series_elements(self) -> list[SeriesElement]:
        """Every two-terminal part, and the pair of nets it joins.

        Deliberately *every* two-terminal part, not just resistors: a ferrite
        bead, an inductor, an AC-coupling capacitor, a series diode and a 0 R
        link are all "the same wire with a component on it" as far as tracing a
        circuit goes, and which of them a given question cares about is the
        caller's business, not this function's.

        DNP parts are included and can be filtered by the caller — whether a
        not-fitted link breaks the path is exactly the sort of thing a check
        wants to decide for itself.
        """
        net_of = {pin.key: net.name for net in self.nets for pin in net.pins}
        elements: list[SeriesElement] = []
        for component in self.components:
            if len(component.pins) != 2:
                continue
            first = net_of.get(component.pins[0].key)
            second = net_of.get(component.pins[1].key)
            if first is None or second is None or first == second:
                continue
            elements.append(SeriesElement(reference=component.reference,
                                          value=component.value,
                                          net_a=first, net_b=second))
        return elements

    def reachable_through_series(self, start: str, max_depth: int = 3,
                                 rails: set[str] | None = None,
                                 skip: set[str] | None = None,
                                 ) -> dict[str, SeriesReach]:
        """Every net reachable from `start` by passing through two-pin parts.

        The netlist primitive several checks needed and none had. Two nets
        either side of a series resistor are electrically distinct and
        topologically the same wire, and no amount of net-level reasoning
        recovers that.

        **A rail is a terminus, not a barrier.** It is returned in the result
        but never expanded from — expanding a supply rail would reach the
        whole board. Getting this backwards is not hypothetical: on one board
        here a current-sense amplifier's shunt sits between a high-side node
        and the rail it feeds, so a walk that refused
        to *include* rails would stop one part short of the thing it was
        looking for.

        `skip` drops parts by designator, which is how a caller excludes the
        part it started from.

        Returns the shallowest reach for each net, keyed by net name, including
        `start` itself at depth 0.
        """
        rails = rails or set()
        skip = skip or set()
        elements = [e for e in self.series_elements() if e.reference not in skip]
        by_net: dict[str, list[SeriesElement]] = defaultdict(list)
        for element in elements:
            by_net[element.net_a].append(element)
            by_net[element.net_b].append(element)

        found = {start: SeriesReach(net=start, depth=0, through=(),
                                    is_rail=start in rails)}
        frontier = [start]
        for depth in range(1, max_depth + 1):
            following: list[str] = []
            for net in frontier:
                if net in rails and net != start:
                    continue          # a terminus: kept, not expanded
                for element in by_net.get(net, []):
                    other = element.other(net)
                    if other is None or other in found:
                        continue
                    found[other] = SeriesReach(
                        net=other, depth=depth,
                        through=found[net].through + (element.reference,),
                        is_rail=other in rails)
                    following.append(other)
            if not following:
                break
            frontier = following
        return found

    def bridging_part(self, reach_a: dict[str, SeriesReach],
                      reach_b: dict[str, SeriesReach],
                      prefixes: tuple[str, ...] | None = None
                      ) -> SeriesElement | None:
        """The two-terminal part joining one side's territory to the other's.

        Takes the two *reach maps* from `reachable_through_series`, not two
        sets, and that is the whole difficulty. **The two sides' reachable sets
        overlap**, because anything bridging them directly — the differential
        filter capacitor across a current-sense amplifier's inputs, say — makes each side
        reachable from the other. Intersecting or differencing the raw sets
        gives either everything or nothing.

        So each net is attributed to whichever side reaches it *sooner*, and a
        net equidistant from both (a shared ground) belongs to neither. On the
        six-layer board's sense amplifier that puts the high-side node on the
        IN+ side at depth 1 against depth 2, the rail on the IN- side, and
        `GND` nowhere — which leaves the 20 mOhm shunt as the only resistor
        crossing between them. Comparing raw sets instead returned a 10 kOhm
        pull-up two parts away.

        `prefixes` restricts by designator: `("R",)` finds the shunt rather
        than the capacitor across the same pair of nets. Among several
        candidates the shallowest wins, since a shunt is the nearest bridging
        element and not the furthest.
        """
        side: dict[str, str] = {}
        for net in set(reach_a) | set(reach_b):
            depth_a = reach_a[net].depth if net in reach_a else None
            depth_b = reach_b[net].depth if net in reach_b else None
            if depth_a is None:
                side[net] = "b"
            elif depth_b is None:
                side[net] = "a"
            elif depth_a < depth_b:
                side[net] = "a"
            elif depth_b < depth_a:
                side[net] = "b"
            # equal depth: shared, belongs to neither

        def total_depth(element: SeriesElement) -> int:
            here = reach_a.get(element.net_a) or reach_b.get(element.net_a)
            there = reach_a.get(element.net_b) or reach_b.get(element.net_b)
            return (here.depth if here else 99) + (there.depth if there else 99)

        candidates = []
        for element in self.series_elements():
            if prefixes and not element.reference.startswith(prefixes):
                continue
            ends = {side.get(element.net_a), side.get(element.net_b)}
            if ends == {"a", "b"}:
                candidates.append(element)
        if not candidates:
            return None
        return min(candidates, key=total_depth)

    # -- report -----------------------------------------------------------

    def report(self, nets: bool = True, pins: bool = False) -> str:
        """A human-readable markdown rendering of the whole design.

        Replaces the old `kicad-sch` skill's `format_report`, which could only
        see one file. `pins` adds the per-component pin/net table, which is
        exhaustive and long — 229 parts on the six-layer board — so it is off by default.
        """
        return _format_report(self, nets=nets, pins=pins)


# ---------------------------------------------------------------------------
# Sheet tree
# ---------------------------------------------------------------------------

class _ParsedFile:
    """One `.kicad_sch` parsed once, reused by every placement of it."""

    __slots__ = ("uuid", "components", "wires", "labels", "junctions",
                 "no_connects", "sheet_nodes", "title")

    def __init__(self, path: Path):
        root = base.sexpdata.loads(Path(path).read_text(encoding="utf-8"))
        if base.sym_name(root) != "kicad_sch":
            raise ValueError(f"not a kicad_sch file: {path}")
        self.uuid = base.extract_file_uuid(root)
        self.title = base.extract_title_block(root)
        self.components = base.extract_symbol_instances(
            root, base.extract_lib_symbol_pins(root))
        self.wires = base.extract_wires(root)
        self.labels = base.extract_all_labels(root)
        self.junctions = base.extract_junctions(root)
        self.no_connects = base.extract_no_connects(root)
        self.sheet_nodes = base.extract_sheet_nodes(root)


def _walk(top: Path, files: dict[Path, _ParsedFile],
          diagnostics: list[str]) -> list[SheetInstance]:
    """Enumerate every sheet placement, depth-first from the root."""
    top = Path(top).resolve()
    files[top] = _ParsedFile(top)
    sheets = [SheetInstance(index=0, path=top, uuid_path=(files[top].uuid or "",),
                            name_path=(), parent=None)]

    queue = [0]
    while queue:
        parent_index = queue.pop(0)
        parent = sheets[parent_index]
        for node in files[parent.path].sheet_nodes:
            if not node["file"]:
                continue
            child_path = (parent.path.parent / node["file"]).resolve()
            if not child_path.exists():
                diagnostics.append(
                    f"{parent.label_prefix}: sub-sheet file not found: {node['file']}")
                continue
            # A sheet containing itself would recurse forever. KiCad forbids
            # it, but a hand-edited file could still say it.
            ancestry, walker = set(), parent
            while walker is not None:
                ancestry.add(walker.path)
                walker = sheets[walker.parent] if walker.parent is not None else None
            if child_path in ancestry:
                diagnostics.append(
                    f"{parent.label_prefix}: recursive sheet reference to "
                    f"{node['file']}, not traversed")
                continue

            if child_path not in files:
                files[child_path] = _ParsedFile(child_path)
            child = SheetInstance(
                index=len(sheets),
                path=child_path,
                uuid_path=parent.uuid_path + (node["uuid"] or "",),
                name_path=parent.name_path + (node["name"] or child_path.stem,),
                parent=parent_index,
            )
            sheets.append(child)
            queue.append(child.index)

    return sheets


def sheet_tree(top: str | Path) -> list[SheetInstance]:
    """Every sheet placement reachable from `top`, root first.

    Sheet files sitting in the project directory but not referenced from the
    root are *not* included — they belong to another board variant, and
    scoring them against this board compares colliding designators from
    unrelated circuits.
    """
    return _walk(Path(top), {}, [])


# ---------------------------------------------------------------------------
# Netlist
# ---------------------------------------------------------------------------

#: Higher wins when two labels name the same net within one sheet. KiCad's
#: driver priority: a local label overrides a hierarchical label, which
#: overrides a bare sheet pin.
_LABEL_RANK = {"local": 3, "hierarchical": 2, "sheet_pin": 1}


#: Pin electrical types KiCad drives a net name from in preference to an
#: ordinary pin (its POWER_PIN driver priority).
_POWER_PIN_TYPES = frozenset({"power_in", "power_out"})


def _auto_name(pins: list[PinRef]) -> str:
    """KiCad's name for a net no label drives.

    Two parts of this are solid:

    * a net with a single pin is `unconnected-(J4-SBU1-PadA8)`, and one with
      more is `Net-(U18-V_BCKP)`;
    * a power pin outranks an ordinary one, which is KiCad's documented
      driver priority and is what makes `Net-(U18-V_BCKP)` win over the
      battery and four capacitors sharing that net.

    The tie-break *among equals* is not. It lives in KiCad's connection graph,
    not in the file format, and none of the obvious candidates reproduces it:
    measured over the 57 multi-pin unnamed nets on the six-layer board, lowest pin UUID gets
    21, highest gets 30, alphabetical-by-designator gets 24. Lowest designator
    is used here because it is at least deterministic and explicable.

    This only ever affects nets *no designer named*, which is why the partition
    — not the name — is what the tests assert. Check `Net.source == "auto"`
    before relying on any name from here.
    """
    if not pins:
        return ""

    def rank(pin: PinRef) -> tuple:
        return (pin.type not in _POWER_PIN_TYPES, pin.reference, pin.number)

    pin = sorted(pins, key=rank)[0]
    label = f"Pad{pin.number}" if pin.name in ("", "~") else pin.name
    if len(pins) == 1:
        suffix = f"-Pad{pin.number}" if pin.name not in ("", "~") else ""
        return f"unconnected-({pin.reference}-{label}{suffix})"
    return f"Net-({pin.reference}-{label})"


def netlist(top: str | Path) -> Schematic:
    """Resolve the full hierarchy under `top` into nets."""
    top = Path(top).resolve()
    files: dict[Path, _ParsedFile] = {}
    diagnostics: list[str] = []
    sheets = _walk(top, files, diagnostics)

    uf = base.UnionFind()
    node_ids: dict[tuple[int, tuple[float, float]], int] = {}
    #: per sheet, the map `_connect_points_on_wires` expects
    local_maps: dict[int, dict[tuple[float, float], int]] = {}

    def node(sheet: int, x: float, y: float) -> int:
        key = base.pt_key(x, y)
        full = (sheet, key)
        if full not in node_ids:
            node_ids[full] = len(node_ids)
            uf.add(node_ids[full])
            local_maps.setdefault(sheet, {})[key] = node_ids[full]
        return node_ids[full]

    components: list[Component] = []
    pins_at: list[tuple[int, PinRef]] = []
    named: list[tuple[int, str, str, int]] = []   # node, text, kind, sheet
    power_at: list[tuple[int, str]] = []
    no_connect_nodes: set[int] = set()
    sheet_pin_nodes: dict[tuple[int, str], int] = {}   # (sheet, pin name) -> node

    for sheet in sheets:
        parsed = files[sheet.path]

        for wire in parsed.wires:
            uf.union(node(sheet.index, *wire["p1"]), node(sheet.index, *wire["p2"]))

        for raw in parsed.components:
            instance = raw.get("instances", {}).get(sheet.instance_path)
            reference = instance["reference"] if instance else raw["reference"]
            if not instance and raw.get("instances"):
                diagnostics.append(
                    f"{sheet.label_prefix}: {raw['reference']} has no instance "
                    f"entry for this sheet path; using the Reference property")

            is_power_symbol = (reference.startswith("#PWR")
                               or raw["lib_id"].startswith("power:"))
            component = Component(
                reference=reference,
                value=raw["value"],
                footprint=raw["footprint"],
                lib_id=raw["lib_id"],
                unit=raw.get("unit", 1),
                dnp=raw["dnp"].lower() not in ("no", "false", "0", ""),
                sheet=sheet.index,
                uuid=raw.get("uuid"),
                properties={**raw.get("properties", {}),
                            "Reference": reference},
            )
            for pin in raw.get("pins", []):
                identifier = node(sheet.index, pin["x"], pin["y"])
                if is_power_symbol:
                    power_at.append((identifier, raw["value"]))
                    continue
                reference_pin = PinRef(reference, pin["number"], pin["name"],
                                       pin["type"], sheet.index)
                component.pins.append(reference_pin)
                pins_at.append((identifier, reference_pin))
            if not reference.startswith("#"):
                components.append(component)

        for kind, entries in (("local", parsed.labels["local"]),
                              ("global", parsed.labels["global"]),
                              ("hierarchical", parsed.labels["hierarchical"])):
            for entry in entries:
                named.append((node(sheet.index, *entry["pos"]),
                              entry["text"], kind, sheet.index))

        for junction in parsed.junctions:
            node(sheet.index, *junction)

        # A child sheet's pins are connection points in *this* sheet.
        for child_node in parsed.sheet_nodes:
            for pin in child_node["pins"]:
                identifier = node(sheet.index, *pin["pos"])
                sheet_pin_nodes[(sheet.index, child_node["uuid"], pin["name"])] = identifier
                named.append((identifier, pin["name"], "sheet_pin", sheet.index))

        for no_connect in parsed.no_connects:
            no_connect_nodes.add(node(sheet.index, *no_connect))

        # Must run after every point in this sheet is registered: a pin or
        # label touching a wire mid-span is connected to it.
        base._connect_points_on_wires(uf, local_maps.get(sheet.index, {}), parsed.wires)

    # -- scoped label merging ------------------------------------------------
    # Local and hierarchical labels merge by text *within one sheet placement*.
    # Global labels and power symbols merge across the whole design.
    scoped: dict[tuple, list[int]] = defaultdict(list)
    for identifier, text, kind, sheet_index in named:
        if not text:
            continue
        if kind == "global":
            scoped[("global", text)].append(identifier)
        elif kind in ("local", "hierarchical"):
            scoped[(sheet_index, kind, text)].append(identifier)
    for identifier, value in power_at:
        scoped[("power", value)].append(identifier)
    for group in scoped.values():
        for other in group[1:]:
            uf.union(group[0], other)

    # -- cross-boundary union ------------------------------------------------
    # Each child's hierarchical labels join the same-named pin on the sheet
    # symbol that placed it.
    hier_labels: dict[tuple[int, str], int] = {}
    for identifier, text, kind, sheet_index in named:
        if kind == "hierarchical" and text:
            hier_labels.setdefault((sheet_index, text), identifier)

    for sheet in sheets:
        if sheet.parent is None:
            continue
        placement_uuid = sheet.uuid_path[-1]
        for pin_name in [p["name"] for node_ in files[sheets[sheet.parent].path].sheet_nodes
                         if node_["uuid"] == placement_uuid for p in node_["pins"]]:
            parent_node = sheet_pin_nodes.get((sheet.parent, placement_uuid, pin_name))
            child_node = hier_labels.get((sheet.index, pin_name))
            if parent_node is None:
                continue
            if child_node is None:
                diagnostics.append(
                    f"{sheet.label_prefix}: sheet pin '{pin_name}' has no "
                    f"matching hierarchical label in the child sheet")
                continue
            uf.union(parent_node, child_node)

    # -- collect -------------------------------------------------------------
    grouped: dict[int, dict] = defaultdict(
        lambda: {"pins": [], "names": [], "power": set(), "nc": False, "sheets": set()})

    for identifier, pin in pins_at:
        entry = grouped[uf.find(identifier)]
        entry["pins"].append(pin)
        entry["sheets"].add(pin.sheet)

    for identifier, text, kind, sheet_index in named:
        if text:
            grouped[uf.find(identifier)]["names"].append((text, kind, sheet_index))

    for identifier, value in power_at:
        grouped[uf.find(identifier)]["power"].add(value)

    for identifier in no_connect_nodes:
        grouped[uf.find(identifier)]["nc"] = True

    nets: list[Net] = []
    for entry in grouped.values():
        pins = sorted(set(entry["pins"]), key=lambda p: (p.reference, p.number))
        if entry["power"]:
            name, source = sorted(entry["power"])[0], "power"
        else:
            globals_ = sorted({t for t, k, _ in entry["names"] if k == "global"})
            if globals_:
                name, source = globals_[0], "global"
            elif entry["names"]:
                # KiCad's driver priority first, sheet depth only as the
                # tie-break. Verified against three cases on this board that
                # pull in opposite directions:
                #   /CH2                         local label at the root beats
                #                                a hierarchical label below it
                #   .../a regulator sheet/DIVIDER_LOWER_R
                #                                hierarchical label two sheets
                #                                down beats the parent's sheet
                #                                pin — so depth cannot lead
                #   .../a repeated sheet 3/CH_DRAIN  likewise for a local label
                text, kind, sheet_index = min(
                    entry["names"],
                    key=lambda n: (-_LABEL_RANK.get(n[1], 0), sheets[n[2]].depth, n[0]))
                name = sheets[sheet_index].label_prefix + text
                source = kind
            else:
                name, source = _auto_name(pins), "auto"

        if not pins and source == "auto":
            continue
        nets.append(Net(name=name, pins=pins, source=source,
                        has_no_connect=entry["nc"],
                        sheets=frozenset(entry["sheets"])))

    # Nets that genuinely share an identity — every `GND` stub, every instance
    # of a global label — were already unioned above, so two nets arriving here
    # with the same name are two *different* nets the naming rule failed to
    # tell apart. Merging them would corrupt the partition, which is the one
    # thing that must stay right, so keep them separate, mark them, and say so.
    seen: dict[str, int] = {}
    for net in sorted(nets, key=lambda n: (n.name, sorted(n.pin_keys)[:1])):
        seen[net.name] = seen.get(net.name, 0) + 1
        if seen[net.name] > 1:
            diagnostics.append(
                f"net name collision: '{net.name}' names {seen[net.name]} "
                f"unconnected groups; disambiguated")
            net.name = f"{net.name} #{seen[net.name]}"

    return Schematic(root=top, sheets=sheets, components=components,
                     nets=sorted(nets, key=lambda n: n.name),
                     diagnostics=diagnostics,
                     title=dict(files[top].title))


# ---------------------------------------------------------------------------
# Markdown report
#
# The old `kicad-sch` skill's user-facing output was a markdown report over one
# file. This is the same idea over a resolved hierarchy, which changes three
# things: the sheet tree is worth showing, designators are per placement, and
# the net list carries where each name came from rather than just the name.
# ---------------------------------------------------------------------------

#: Marks on a net name in the report.
_SOURCE_MARK = {"power": "rail", "global": "global", "hierarchical": "hier",
                "local": "local", "sheet_pin": "sheet pin", "auto": "unnamed"}


def _escape(text: str) -> str:
    """Keep a pipe in a value from breaking the markdown table it sits in."""
    return str(text).replace("|", "\\|")


def _format_report(schematic: Schematic, nets: bool = True,
                   pins: bool = False) -> str:
    lines: list[str] = [f"# Schematic: `{schematic.root.name}`", ""]

    if schematic.title:
        lines += ["| field | value |", "|---|---|"]
        lines += [f"| {k.replace('_', ' ')} | {_escape(v)} |"
                  for k, v in schematic.title.items()]
        lines.append("")

    parts = {c.reference for c in schematic.components}
    unnamed = [n for n in schematic.nets if n.source == "auto"]
    lines += [
        "## Summary", "",
        f"- {len(parts)} parts over {len(schematic.components)} symbols "
        f"({len(schematic.components) - len(parts)} extra units of multi-unit parts)",
        f"- {len(schematic.sheets)} sheet placement(s) of "
        f"{len({s.path for s in schematic.sheets})} file(s)",
        f"- {len(schematic.nets)} nets, {len(unnamed)} of them unnamed",
        f"- {sum(len(n.pins) for n in schematic.nets)} pin connections",
        f"- {sum(1 for c in schematic.components if c.dnp)} part(s) marked DNP",
        "",
    ]

    counts = schematic.counts_by_prefix()
    if counts:
        lines += ["### By designator prefix", "",
                  "| prefix | count |", "|---|---|"]
        lines += [f"| {p} | {c} |" for p, c in counts.items()]
        lines.append("")

    # -- sheet tree --------------------------------------------------------
    if len(schematic.sheets) > 1:
        lines += ["## Sheet tree", "", "```"]
        for sheet in schematic.sheets:
            here = sum(1 for c in schematic.components if c.sheet == sheet.index)
            name = sheet.name_path[-1] if sheet.name_path else schematic.root.stem
            lines.append(f"{'  ' * sheet.depth}{name}"
                         f"  ({sheet.path.name}, {here} parts)")
        lines += ["```", ""]

    # -- power rails -------------------------------------------------------
    rails = schematic.power_nets
    if rails:
        lines += ["## Power rails", "",
                  "Nets driven by a power symbol — read from the symbols, not "
                  "from net names.", ""]
        for rail in rails:
            count = len(schematic.nets_by_name[rail].pins)
            lines.append(f"- `{rail}` — {count} pin(s)")
        lines.append("")

    # -- nets --------------------------------------------------------------
    if nets:
        lines += [
            "## Nets", "",
            "`source` is what gave the net its name. Anything marked `unnamed` "
            "was named by us and not by the designer, so the name is "
            "best-effort while the membership is exact.", "",
            "| net | source | pins |", "|---|---|---|",
        ]
        for net in sorted(schematic.nets,
                          key=lambda n: (n.source != "power", n.name)):
            mark = _SOURCE_MARK.get(net.source, net.source)
            if net.has_no_connect:
                mark += ", NC"
            if net.crosses_sheets:
                mark += f", {len(net.sheets)} sheets"
            members = ", ".join(f"{p.reference}.{p.number}"
                                + (f"({p.name})" if p.name not in ("", "~") else "")
                                for p in net.pins)
            lines.append(f"| `{_escape(net.name)}` | {mark} | "
                         f"{_escape(members) or '_no pins_'} |")
        lines.append("")

    # -- per-part pins -----------------------------------------------------
    if pins:
        lines += ["## Pin connections", ""]
        net_of = {p.key: n.name for n in schematic.nets for p in n.pins}
        for component in sorted(schematic.components,
                                key=lambda c: (_reference_sort_key(c.reference),
                                               c.unit)):
            if not component.pins:
                continue
            heading = component.reference
            if len({c.reference for c in schematic.components
                    if c.reference == component.reference}) > 1:
                heading += f" (unit {component.unit})"
            lines += [f"### {heading} — {_escape(component.value)}", "",
                      "| pin | name | type | net |", "|---|---|---|---|"]
            for pin in sorted(component.pins,
                              key=lambda p: (not p.number.isdigit(),
                                             int(p.number) if p.number.isdigit() else 0,
                                             p.number)):
                lines.append(f"| {pin.number} | {_escape(pin.name)} | {pin.type} "
                             f"| `{_escape(net_of.get(pin.key, '?'))}` |")
            lines.append("")

    # -- BOM ---------------------------------------------------------------
    bom = schematic.bom()
    lines += ["## Bill of materials", "",
              f"{sum(l.quantity for l in bom if not l.dnp)} parts to fit over "
              f"{sum(1 for l in bom if not l.dnp)} line(s)"
              + (f", plus {sum(l.quantity for l in bom if l.dnp)} DNP"
                 if any(l.dnp for l in bom) else "") + ".", "",
              "| refs | qty | value | package | MPN | supplier | DNP |",
              "|---|---|---|---|---|---|---|"]
    for line in bom:
        lines.append(
            f"| {_escape(', '.join(line.references))} | {line.quantity} | "
            f"{_escape(line.value)} | {_escape(line.package)} | "
            f"{_escape(line.mpn or '')} | {_escape(line.supplier_part or '')} | "
            f"{'DNP' if line.dnp else ''} |")
    lines.append("")

    # -- field survey ------------------------------------------------------
    # The raw picture of what is in the component fields, so a reader can see
    # where a datum has been filed rather than only that a lookup found it.
    survey = schematic.field_survey()
    lines += ["## Component fields", "",
              f"{len(survey.usage)} distinct field names over "
              f"{survey.total_components} parts. `shapes` is what the *values* "
              f"look like, independent of what the field is called — a "
              f"mismatch between the two is where hand entry goes wrong.", "",
              "| field | parts | shapes | examples |", "|---|---|---|---|"]
    for key, count in sorted(survey.usage.items(), key=lambda kv: (-kv[1], kv[0])):
        shapes = ", ".join(f"{s} {n}" for s, n
                           in sorted(survey.shapes.get(key, {}).items(),
                                     key=lambda kv: -kv[1]))
        samples = "; ".join(survey.examples.get(key, []))[:70]
        lines.append(f"| `{_escape(key)}` | {count} | {shapes} | "
                     f"{_escape(samples)} |")
    lines.append("")
    if survey.anomalies:
        lines += [f"{len(survey.anomalies)} field entry(s) look out of place:",
                  ""]
        for kind in survey.kinds:
            items = survey.by_kind(kind)
            lines.append(f"- **{kind}** ({len(items)}): "
                         + _escape("; ".join(str(a) for a in items[:3]))
                         + (f" … and {len(items) - 3} more"
                            if len(items) > 3 else ""))
        lines.append("")

    if schematic.diagnostics:
        lines += ["## Parser diagnostics", ""]
        lines += [f"- {_escape(d)}" for d in schematic.diagnostics]
        lines.append("")

    return "\n".join(lines)
