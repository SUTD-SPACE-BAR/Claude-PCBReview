"""What kind of thing a net is, and therefore how much a defect on it matters.

This is Gate 2. It was blocked for five sessions on a design question that
the user answered directly, 2026-08-03:

> The system should just infer whether something is a signal communication
> trace like uart, i2c, spi, rf, canbus, usb, pwm, and care a lot about those
> traces. And power traces should be cared about. any other trace should still
> be audited but low severity if theres issues.

Three sources, in order of authority:

1. **A declaration from the user**, relayed by Claude into a net-intent
   manifest. the user, 2026-08-03: *"the user should tell the system to look out
   for specific traces that need special attention, such as the firing
   channels, and that intent is declared to the auditor."* A firing channel is the
   motivating case: it is a power *output* that no power symbol drives, so
   nothing structural calls it a rail, and it caps at `minor` unless the person
   who designed it says otherwise. Declarations win over everything below.
2. **Structure** — pin names, rails and grounds, routed width.
3. **The net's own name**, last and at low confidence.

So there are three classes:

    protocol   uart, i2c, spi, can, usb, rf, pwm, clock
    power      rails and grounds, from `intent.classify_rails`
    other      everything else

The inferred cap is gone — 2026-08-03, and the measurement that ended it
---------------------------------------------------------------------
`other` used to cap at `minor`. §48.1 proposed splitting a positively
low-stakes `other` from an "we could not tell" `unknown` and capping only the
first. **Measured on four boards, that split leaves `other` empty**: 69/69,
192/192, 188/188 and 65/65 of the `other` nets were there because nothing
identified them. The classifier never positively identifies a net as
low-stakes — it only ever falls back — so the cap was, on every board here,
an inference from absence of evidence, recorded at `confidence=1.0`.

The user, with that cost measured (65 → 93 majors on the densest board,
48 → 63 on the four-layer power board):

> we cannot rely completely on pin types of the schematic symbols - very often
> people will make custom symbols and just not declare the type properly. When
> in doubt, treat the pin and hence the net as high priority.

So **every finding now keeps the severity its check computed**, and ranking
falls back to `score`, which is a measurement rather than an inference about
intent. **The only thing that still demotes is the user saying so** in the
net-intent manifest — `net_class: other` on a pattern they wrote. That makes
the manifest's demotion half load-bearing rather than a nicety.

**What the classification is still for**, and why this module stays:

* `power` gates `check_current_capacity` and `check_heat_path` — the width
  rule (§46.2) feeds those, and an *unidentified* net is deliberately **not**
  fed to them. "High priority" was ruled to mean severity only; running a
  Laplace solve on 188 button traces was the alternative and is not worth it.
* the manifest still promotes, and `census()` still reports what was inferred.

**Nothing here changes a measurement.** Every number a check computed stays
exactly as computed; only `severity` moves, only on a user declaration, the
original is kept in the finding's measurement as `severity_before_netclass`,
and a line is added saying who did it and why. A demotion is therefore visible
and reversible rather than silent.

Where the evidence comes from
-----------------------------
**Pin names on the manufacturer's symbol**, first and by a long way. `SDA`,
`MOSI`, `CANH`, `D+` are names the part vendor chose, not the designer, which
is the same reason `intent.propose_differential_pairs` trusts them. A net
touching a pin called `SCL` is an I2C net whatever the designer called it.

**The net's own name**, second and at lower confidence. `looks_like` matching
on designer-chosen names is what this module exists to replace, so it is only
consulted when no pin says anything — but a net named `/CAN_TX` with a
generically-named transceiver symbol is still worth catching.

Which way to be wrong
---------------------
**Over-inclusion is the safe direction and this module leans that way.** Since
the cap went, a net wrongly called `protocol` costs nothing at all in severity
terms; what it still does is stay out of the power-scoped checks, which is the
conservative direction there too. So the net-name fallback is deliberately
broad, and `GPS`, `GNSS` and `LORA` are in it even though those name a
subsystem rather than an RF trace.

Two things this deliberately does not do
----------------------------------------
It does not *raise* severity. A check that computed `info` stays `info`: a
user declaration of `protocol` or `power` exempts a net from demotion and
admits it to the power-scoped checks; it does not invent urgency.

It does not classify by *speed*. A 12 Mbps USB link and a 480 Mbps one look
identical here, and an I2C bus at 100 kHz tolerates a return-path detour that
would ruin an SPI bus at 40 MHz. `intent_needed` on the finding still says so.
Edge rate remains the one datum only a human can supply.
"""

from __future__ import annotations

import collections
import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pcbreview.schematic import Schematic

#: Full severity for these. the user's list, plus `clock` — a crystal or
#: oscillator net is the one thing on a board that is unambiguously fast, and
#: leaving it in `other` would cap the crystal check at `minor` for no reason.
PROTOCOL_KINDS = ("i2c", "spi", "uart", "can", "usb", "rf", "pwm", "clock")

FULL_SEVERITY_CLASSES = ("protocol", "power")

#: The cap applied to everything else. `minor` rather than `info` because
#: the user asked for these to be *audited* at low severity, not suppressed.
OTHER_SEVERITY_CAP = "minor"

#: A routed net wider than this is treated as power-carrying. the user's
#: instruction, 2026-08-03: *"any trace above 0.2mm wide is probably power
#: carrying somehow and should be treated as such"* — a designer widens a
#: track for exactly one reason.
#:
#: Measured before implementing, because the one thing that could make it
#: misfire is a board whose *default* width is already above it. On the five
#: boards here the median routed width is 0.200 mm on every one, so the rule
#: bites only where a track was deliberately widened — except on one board
#: whose bus routing is 0.267 mm, where it reclassifies 38% of nets. That is
#: safe in the only direction that matters: `power` exempts a net from capping
#: and never raises anything, so a false positive restores the behaviour every
#: finding had before Gate 2 existed. `census()` reports the board's own median
#: width so the case is visible rather than silent.
POWER_WIDTH_MM = 0.2

#: and it must be most of the net, not one fat stub off a signal trace
POWER_WIDTH_FRACTION = 0.5

#: Pin names, anchored. Every one of these is a name the *part vendor* chose.
#:
#: The anchoring is not decoration. Unanchored, `RX` matches `RXEN`, `TX`
#: matches `TXB0104`, `CS` matches `CSENSE` and `DM` matches `DMA`. Measured
#: over seven designs, dropping the anchors roughly triples the protocol count
#: and every added net is a false positive.
_PIN_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("i2c", re.compile(r"^(SDA|SCL|SDA\d|SCL\d|I2C_(SDA|SCL))$", re.I)),
    ("spi", re.compile(
        r"^(MOSI|MISO|SCK|SCLK|SDO|SDI|COPI|CIPO|NSS|SS|~?CS|CSB|CSN|"
        r"SPI_(MOSI|MISO|SCK|CS))$", re.I)),
    ("uart", re.compile(
        r"^(TX|RX|TXD|RXD|TXD\d|RXD\d|CTS|RTS|~?CTS|~?RTS|"
        r"UART_(TX|RX))$", re.I)),
    ("can", re.compile(r"^(CANH|CANL|CAN_H|CAN_L|CANTX|CANRX|CAN_(TX|RX))$",
                       re.I)),
    ("usb", re.compile(
        r"^(D\+|D-|DP|DM|USB_?D\+|USB_?D-|USB_?DP|USB_?DM|"
        r"USB_OTG_FS_D[+-]|SSTX[PN]|SSRX[PN])$", re.I)),
    ("rf", re.compile(r"^(ANT|ANTENNA|RF|RF_?IN|RF_?OUT|RF_?IO|RFC)$", re.I)),
    # `EN` was in here on the first run and is simply wrong: an enable pin on a
    # regulator is a DC control line, and it classified `/VIN`, `/EN` and a
    # LoRa module's enable as PWM across four designs. `IN1`/`IN2`/`INA`/`INB`
    # went with it — those are op-amp inputs as often as bridge inputs.
    ("pwm", re.compile(r"^(PWM|PWM\d+|LED\d+|PH|FI|BI|AIN[12]|BIN[12])$",
                       re.I)),
    ("clock", re.compile(r"^(XIN|XOUT|XTAL[12]?|OSC(IN|OUT)?|CLK|CLKIN|"
                         r"CLKOUT|MCLK|EXTCLK)$", re.I)),
)

#: Net-name fallback, consulted only when no pin on the net says anything.
#: Lower confidence, and recorded as such on the result.
#:
#: Ordered most-specific first, and `uart` is last on purpose: `TX` and `RX`
#: are the most generic tokens here and would otherwise claim `/CAN_TX`.
#:
#: Two things about the boundaries matter as much as the words, and both were
#: wrong on the first run:
#:
#: * `_` is a regex word character, so `\bCAN\b` does **not** match `CAN_TX`.
#:   `_protocol_from_name` normalises separators to spaces first.
#: * a trailing index is a word character too, so `\bSERVO\b` does **not**
#:   match `SERVO5`. Every bus word therefore carries `\d*`. Without it, eight
#:   servo PWM channels on a real board were classified `other` and capped —
#:   exactly the class the rule exists to protect.
_NAME_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("can", re.compile(r"\b(CAN|CANH|CANL)\d*\b", re.I)),
    ("usb", re.compile(r"\b(USB|DP|DM)\d*\b|D[+-]$", re.I)),
    ("i2c", re.compile(r"\b(I2C|SDA|SCL)\d*\b", re.I)),
    ("spi", re.compile(r"\b(SPI|MOSI|MISO|SCLK|SCK)\d*\b", re.I)),
    ("rf", re.compile(r"\b(RF|ANT|ANTENNA|GPS|GNSS|LORA)\d*\b", re.I)),
    ("pwm", re.compile(r"\b(PWM|SERVO|MOTOR)\d*\b", re.I)),
    ("clock", re.compile(r"\b(XTAL|OSC|CLK|MCLK)\d*\b", re.I)),
    ("uart", re.compile(r"\b(UART|USART|TXD?\d*|RXD?\d*)\b", re.I)),
)

#: Characters a designer uses as word separators inside a net name, which the
#: regex engine does not treat as word boundaries.
_SEPARATORS = re.compile(r"[_\-./:]+")


class NetIntentError(Exception):
    """Raised when a net-intent manifest cannot be used."""


@dataclass(frozen=True)
class Declaration:
    """One net-class declaration the user made, relayed by Claude."""
    match: str                  # fnmatch pattern against board net names
    net_class: str              # protocol | power | other
    kind: str | None = None
    note: str | None = None
    #: What this net actually carries, in amps. **The one fact only the
    #: designer can supply**, and the thing that turns an ampacity from a
    #: statement about copper into a verdict. the ruling, 2026-08-06:
    #:
    #:     Especially for power nets, ampacity should always be reported. […]
    #:     If the current requirements aren't given then I can understand if
    #:     the ampacity isn't reported for a perfectly uniform net, if it's not
    #:     a power net. But for power nets, or nets which the user specifically
    #:     flagged for current, the system should always flag it.
    current_a: float | None = None
    #: Temperature rise this net is *allowed* to reach, in C. Defaults to
    #: `current.RISE_C` (10 C), and the default is deliberately strict:
    #:
    #:     assume 10c rise is the max for all cases that arent specified as
    #:     being allowed to run hotter
    #:
    #: A conductor energised for a second once per flight can run far hotter
    #: than one carrying continuous current, and that is the designer's call to
    #: state — not the tool's to assume. IPC-2221 already takes rise as a
    #: parameter, so honouring it per net is plumbing rather than new physics.
    rise_c: float | None = None


@dataclass
class NetIntent:
    """Everything the user said about what their nets are for."""
    declarations: list = field(default_factory=list)
    source_path: object = None

    def __bool__(self) -> bool:
        return bool(self.declarations)

    def find(self, net: str):
        """The first declaration matching `net`, or None. First wins, so a
        specific pattern placed above a broad one overrides it."""
        for declaration in self.declarations:
            if fnmatch.fnmatchcase(net, declaration.match):
                return declaration
        return None


_VALID_CLASSES = ("protocol", "power", "other")


def load_intent(path) -> NetIntent:
    """Read a net-intent manifest. Raises rather than half-loading.

    The user never writes this; Claude does, from what the user said (§38).
    Its whole purpose is to carry a statement no geometry can make — that a
    particular net matters more, or less, than its structure suggests.

        nets:
          - match: "/FIRE_CH*"
            net_class: power
            note: "firing channel, 5 A for 100 ms into a low-ohm load"
          - match: "/SPARE*"
            net_class: other

    `note` is **optional on every class**, and recommended on any of them: it
    is how a reader of the report learns why a net was declared. It used to be
    required on `power` and `protocol` to justify "exempting them from
    capping" — a cap that has not existed since 2026-08-03, and an exemption
    those classes never granted. See `load_intent`'s body for the whole story.
    """
    path = Path(path)
    if not path.exists():
        raise NetIntentError(f"net-intent manifest not found: {path}")
    # **UTF-8 named, not the platform default.** YAML 1.2 mandates UTF-8, and
    # the default here is `cp1252` on Windows — which does not raise on UTF-8
    # bytes, it silently mis-decodes them. A `note` reading "5 A at 85 °C" was
    # therefore written correctly and read back mangled, and the corrupted text
    # then travelled into the finding detail that quotes it. Pure-ASCII
    # manifests — which is every one in the corpus — decode identically either
    # way, so this changes nothing except the case it was getting wrong.
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "nets" not in data:
        raise NetIntentError(f"{path} is not a net-intent manifest (needs 'nets')")

    declarations = []
    for index, entry in enumerate(data["nets"] or []):
        if not isinstance(entry, dict):
            raise NetIntentError(f"{path}: entry {index} is not a mapping")
        pattern = entry.get("match")
        net_class = entry.get("net_class")
        if not pattern:
            raise NetIntentError(f"{path}: entry {index} has no `match`")
        if net_class not in _VALID_CLASSES:
            raise NetIntentError(
                f"{path}: entry {index} has net_class={net_class!r}; "
                f"it must be one of {', '.join(_VALID_CLASSES)}")
        # **`note` is optional on every class, and the rule that required it
        # here was both vestigial and backwards.** It read: "declared
        # {net_class}, which exempts it from capping, so it must carry a note
        # saying why."
        #
        # Nothing is capped any more. The inferred cap was removed on
        # 2026-08-03 (this module's own docstring, and the four-board
        # measurement behind it): *every finding now keeps the severity its
        # check computed*, and **the only thing that demotes is
        # `net_class: other`**. So `power` and `protocol` exempt a net from
        # nothing — what they actually do is *admit* it to
        # `check_current_capacity` and `check_heat_path`, which is more
        # scrutiny, not less.
        #
        # The rule therefore demanded a justification for asking the engine to
        # look harder, while `other` — the one declaration that genuinely
        # lowers a severity — needed none. The user, on being refused a plain
        # `+3.3V is power`: *"I think dont try to assume the user must explain
        # why they assert this or that."* They are right, and the mechanism the
        # message described stopped existing eight days before they hit it.
        #
        # A `note` is still carried, still shown, and still worth writing — it
        # is how a reader of the report learns *why* a net was declared. It is
        # simply no longer a precondition for saying something true about your
        # own board.
        note = entry.get("note")
        current_a = _positive_number(path, pattern, entry, "current_a")
        rise_c = _positive_number(path, pattern, entry, "rise_c")
        if rise_c is not None and current_a is None:
            raise NetIntentError(
                f"{path}: '{pattern}' allows a {rise_c} C rise but states no "
                f"`current_a`. A permitted rise on its own changes nothing and "
                f"reads as though the net had been checked")
        declarations.append(Declaration(
            match=str(pattern), net_class=net_class,
            kind=entry.get("kind"), note=note,
            current_a=current_a, rise_c=rise_c))
    return NetIntent(declarations=declarations, source_path=path)


def _positive_number(path, pattern, entry: dict, key: str):
    """A positive float from a manifest entry, or `None`. Refuses anything else.

    A `current_a: "5A"` that silently became `None` would read as *"no current
    was declared"*, which is the one failure this field is here to remove.
    """
    if key not in entry or entry[key] is None:
        return None
    try:
        value = float(entry[key])
    except (TypeError, ValueError) as exc:
        raise NetIntentError(
            f"{path}: '{pattern}' has {key}={entry[key]!r}, which is not a "
            f"number. Give a bare number in amps or degrees C, no unit") from exc
    if value <= 0:
        raise NetIntentError(
            f"{path}: '{pattern}' has {key}={value}; it must be positive")
    return value


def default_intent_path(board_path):
    """Where a board's net-intent manifest lives, if it has one."""
    return Path(board_path).with_suffix(".netintent.yaml")


def power_by_width(board, threshold_mm: float = POWER_WIDTH_MM,
                   fraction: float = POWER_WIDTH_FRACTION) -> dict:
    """Nets whose routed copper is mostly wider than `threshold_mm`.

    A designer widens a track for one reason. This needs no schematic and no
    net name, which makes it the only power evidence that survives a board
    whose netlist is unavailable.

    Returns `{net: (fraction_over, total_mm, widest_mm)}`.
    """
    by_net = collections.defaultdict(lambda: collections.defaultdict(float))
    for track in board.tracks:
        try:
            name = board.net(track.net).name
        except KeyError:
            continue
        if name:
            by_net[name][round(track.width_mm, 4)] += track.length_mm

    out = {}
    for name, widths in by_net.items():
        total = sum(widths.values())
        if total <= 0:
            continue
        over = sum(length for width, length in widths.items()
                   if width > threshold_mm)
        if over / total > fraction:
            out[name] = (over / total, total, max(widths))
    return out


def width_rule_state(widest: float | None, median: float | None) -> str:
    """Whether classifying by track width can say anything on this board.

    `POWER_WIDTH_MM` is an absolute number — the user's own instruction, *"any
    trace above 0.2mm wide is probably power carrying somehow"* — and an
    absolute number has two ends it can fall off:

    * **nothing is wide enough.** On a fine-pitch board whose widest track is
      0.15 mm, `power_by_width` returns nothing, no net is classified power on
      geometry, and `current_capacity` and `heat_path` therefore examine
      *nothing* — while every coverage line honestly reports zero and nobody
      can see why. This is the dangerous end;
    * **everything is.** A board routed entirely at 0.25 mm has every net
      classified power, which is not wrong so much as uninformative.

    Neither is a reason to move the threshold — it is the user's, and a
    relative one would make the same net power on one board and not on another.
    It is a reason to **say so**, so the agent knows to ask which rails are
    rails instead of trusting a silence. `audit.py` puts it in the assumptions
    and rule 5 reports it.
    """
    if widest is None or median is None:
        return "no_routing"
    if widest < POWER_WIDTH_MM:
        return "never_fires"
    if median >= POWER_WIDTH_MM:
        return "always_fires"
    return "discriminates"


def widest_routed_width_mm(board) -> float | None:
    """The widest track on the board, so a threshold above it is visible."""
    widths = [track.width_mm for track in board.tracks]
    return max(widths) if widths else None


def median_routed_width_mm(board) -> float | None:
    """The board's own median routed width, weighted by length.

    Printed in `census()` so a board whose default routing is already above
    `POWER_WIDTH_MM` is visible rather than silently reclassified.
    """
    weighted = []
    for track in board.tracks:
        weighted.extend([track.width_mm] * max(int(track.length_mm), 1))
    if not weighted:
        return None
    weighted.sort()
    return weighted[len(weighted) // 2]


@dataclass(frozen=True)
class NetKind:
    """One net's class, with what it was inferred from."""
    net: str
    net_class: str          # protocol | power | other
    kind: str | None        # i2c, spi, … for protocol; ground/supply for power
    evidence: str
    confidence: float
    #: True only when this came from the user's own net-intent manifest. It is
    #: what separates "the user told us this is unimportant" from "we could not
    #: tell", which are the two things the old `other` conflated.
    declared: bool = False
    #: Carried through from the user's declaration so `current_capacity` can
    #: turn an ampacity into a verdict. `None` means they did not say, which
    #: is a different thing from zero and is reported as such.
    current_a: float | None = None
    rise_c: float | None = None
    #: The user's own words for why they declared this net as they did.
    #: Carried onto the finding that a declaration quietens: whoever audits a
    #: demoted `major` is entitled to read the reason it was demoted, in the
    #: sentence the person actually wrote, rather than only "you declared it".
    note: str | None = None

    @property
    def full_severity(self) -> bool:
        return self.net_class in FULL_SEVERITY_CLASSES

    @property
    def demoted_by_user(self) -> bool:
        """The one remaining reason a finding's severity may be lowered."""
        return self.declared and self.net_class not in FULL_SEVERITY_CLASSES


@dataclass
class NetClassMap:
    """Every net's class, and the census that makes a zero readable."""
    by_net: dict = field(default_factory=dict)
    #: nets the user declared explicitly, and patterns that matched nothing
    declared: list = field(default_factory=list)
    unmatched_declarations: list = field(default_factory=list)
    #: the board's own median routed width, for reading the width rule
    median_width_mm: float | None = None
    #: The widest track on the board, and whether `POWER_WIDTH_MM` sits inside
    #: the range at all. See `width_rule_state` — a threshold above every track
    #: on the board classifies nothing, and that has to be visible.
    widest_width_mm: float | None = None
    width_rule: str = "no_routing"


    def of(self, net: str) -> NetKind | None:
        return self.by_net.get(net)

    def full_severity(self, net: str) -> bool:
        kind = self.by_net.get(net)
        return kind.full_severity if kind else False

    def census(self) -> dict:
        classes = Counter(k.net_class for k in self.by_net.values())
        kinds = Counter(k.kind for k in self.by_net.values()
                        if k.net_class == "protocol")
        return {
            "nets": len(self.by_net),
            "by_class": dict(classes),
            "protocol_kinds": dict(kinds),
            "from_pin_names": sum(1 for k in self.by_net.values()
                                  if k.evidence.startswith("pin")),
            "from_net_names": sum(1 for k in self.by_net.values()
                                  if k.evidence.startswith("net name")),
            "from_routed_width": sum(1 for k in self.by_net.values()
                                     if k.kind == "wide"),
            "declared_by_user": sorted(self.declared),
            "declarations_matching_nothing": self.unmatched_declarations,
            "median_routed_width_mm": self.median_width_mm,
            "power_width_threshold_mm": POWER_WIDTH_MM,
            "widest_routed_width_mm": self.widest_width_mm,
            "width_rule": self.width_rule,
        }


def _protocol_from_pins(pins) -> tuple[str, str] | None:
    """(kind, evidence) from the first pin name that matches, or None."""
    for pin in pins:
        name = (getattr(pin, "name", "") or "").strip()
        if not name:
            continue
        for kind, pattern in _PIN_PATTERNS:
            if pattern.match(name):
                return kind, (f"pin {pin.reference}.{pin.number} is named "
                              f"'{name}' on the manufacturer's symbol")
    return None


def _protocol_from_name(net_name: str) -> tuple[str, str] | None:
    bare = _SEPARATORS.sub(" ", net_name.lstrip("/").replace("{slash}", "/"))
    for kind, pattern in _NAME_PATTERNS:
        if pattern.search(bare):
            return kind, f"net name '{net_name}' contains a {kind} word"
    return None


def classify(schematic: Schematic | None, rail_nets: set[str],
             ground_nets: set[str], mapping=None,
             board_nets: set[str] | None = None,
             board=None, intent: NetIntent | None = None) -> NetClassMap:
    """Classify every net into protocol / power / other.

    `mapping` is `intent.BoardMapping`; pass it and the result comes back in
    the board's vocabulary, matched by pads rather than by name so the unnamed
    nets survive the translation (§14).

    `board` enables the routed-width rule; `intent` is the user's own
    declarations and overrides everything else.
    """
    result = NetClassMap()

    for net in sorted(set(rail_nets) | set(ground_nets)):
        result.by_net[net] = NetKind(
            net=net, net_class="power",
            kind="ground" if net in ground_nets else "supply",
            evidence="power symbol and decoupling topology (`intent`)",
            confidence=1.0)

    if board is not None:
        result.median_width_mm = median_routed_width_mm(board)
        result.widest_width_mm = widest_routed_width_mm(board)
        result.width_rule = width_rule_state(result.widest_width_mm,
                                             result.median_width_mm)
        for net, (fraction, total, widest) in power_by_width(board).items():
            if net in result.by_net:
                continue
            result.by_net[net] = NetKind(
                net=net, net_class="power", kind="wide",
                evidence=(f"{fraction:.0%} of its {total:.1f} mm of routing is "
                          f"wider than {POWER_WIDTH_MM} mm (widest "
                          f"{widest:.3f} mm) — a track is widened to carry "
                          f"current"),
                confidence=0.7)

    if schematic is not None:
        for net in schematic.nets:
            name = net.name
            if mapping is not None:
                name = mapping.board_net(net.name) or net.name
            if name in result.by_net:
                continue
            found = _protocol_from_pins(net.pins)
            confidence = 0.9
            if found is None:
                found = _protocol_from_name(net.name)
                confidence = 0.5
            if found is None:
                result.by_net[name] = NetKind(
                    net=name, net_class="other", kind=None,
                    evidence="no pin name and no net name identified a bus",
                    confidence=1.0)
            else:
                kind, evidence = found
                result.by_net[name] = NetKind(
                    net=name, net_class="protocol", kind=kind,
                    evidence=evidence, confidence=confidence)

    for net in sorted(board_nets or ()):
        result.by_net.setdefault(net, NetKind(
            net=net, net_class="other", kind=None,
            evidence="not present in the schematic netlist",
            confidence=0.5))

    # The user's own declarations, last so they win over everything.
    if intent:
        matched = set()
        for net in list(result.by_net):
            declaration = intent.find(net)
            if declaration is None:
                continue
            matched.add(declaration.match)
            was = result.by_net[net]
            note = f" — {declaration.note}" if declaration.note else ""
            result.by_net[net] = NetKind(
                net=net, net_class=declaration.net_class,
                kind=declaration.kind or was.kind,
                evidence=(f"declared by the user as {declaration.net_class}"
                          f"{note} (was {was.net_class})"
                          + (f", carrying {declaration.current_a} A"
                             if declaration.current_a else "")),
                confidence=1.0, declared=True,
                current_a=declaration.current_a, rise_c=declaration.rise_c,
                note=declaration.note)
            result.declared.append(net)
        # A pattern that matches nothing is a typo, and a silent one would
        # leave the user believing a net is protected when it is not. Same
        # rule as the thermal manifest's `unknown_references`.
        result.unmatched_declarations = sorted(
            d.match for d in intent.declarations if d.match not in matched)
    return result


#: Checks whose severity is about a *part*, not about what a net carries.
#: the user's rule is about traces; a junction temperature does not become less
#: urgent because the FET's drain net is not a bus. `heat_path` names the tab's
#: net so the renderer can draw it, and capping on that basis demoted a 3.84 W
#: part to `minor` on the first run — which is exactly the kind of silent
#: severity loss this module must not introduce.
#: Every check `placement.py` and `thermal.py` emit, plus the board-wide ones.
#: A Kelvin sense connection, a decoupling loop and a crystal loop are all
#: statements about how a *part* is wired, and a net name on them is there for
#: the renderer. `kelvin_sense` was missing from the first version and seven
#: findings on one board were demoted because of it.
PART_CHECKS = frozenset({
    "thermal", "heat_path", "placement", "parity", "stackup_mismatch",
    "panelisation", "board_state",
    "regulator_feedback", "decoupling_loop", "kelvin_sense", "crystal_loop",
    "esd_position",
})


def apply(findings, classes: NetClassMap) -> dict:
    """Demote findings the **user** declared unimportant, in place.

    This used to cap every finding whose nets were merely *unclassified*.
    That is gone (see the module docstring): absence of evidence is not
    evidence of unimportance, and measured across four boards it was the only
    thing the cap ever fired on. What is left is the user's own word — a
    `net_class: other` declaration in the net-intent manifest — which is the
    only way a net gets quietened now.

    A finding is demoted only when **every** net it names was declared
    unimportant: one undeclared net on a finding is enough to leave it alone.
    A finding naming no net is left alone, and so is one from a check in
    `PART_CHECKS` — those are about parts and layers, and a net name on them is
    there for the renderer.
    """
    capped = 0
    kept = 0
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    for finding in findings:
        if not finding.nets or finding.check in PART_CHECKS:
            continue
        kinds = [classes.of(n) for n in finding.nets]
        if not all(k is not None and k.demoted_by_user for k in kinds):
            kept += 1
            continue
        if order.get(finding.severity, 9) >= order[OTHER_SEVERITY_CAP]:
            continue
        before = finding.severity
        finding.severity = OTHER_SEVERITY_CAP
        finding.measurement["severity_before_netclass"] = before
        named = sorted(finding.nets)
        governing = classes.of(named[0])
        declared_as = governing.net_class
        finding.detail.append(
            f"severity lowered from {before} to {OTHER_SEVERITY_CAP}: "
            f"you declared {', '.join(named)} as {declared_as} in the "
            f"net-intent manifest, so a defect on it is reported but not "
            f"ranked above one on a net you did not")
        if governing.note:
            # **The reason, in the user's own words, beside the finding it
            # quietened.** A demotion is the only thing in this tool that
            # lowers a severity, so the one place its justification must not
            # be is in a different file.
            finding.detail.append(f"your reason: {governing.note}")
        capped += 1
    return {"capped": capped, "kept_at_full_severity": kept}


# ---------------------------------------------------------------------------
# The proposer — writing the inference down (§48.9)
# ---------------------------------------------------------------------------
#
# `intent.py` and `classify` above have always inferred a class for every net
# on every audit, and then thrown all of it away. Nothing wrote it down, showed
# it, or let the user correct it — so the manifest that shipped was *purely
# reactive*: the user says nothing, Claude writes nothing, and a session that
# ran a full review correctly reported it had "not built the manifest".
#
# That was never the design. §32.B1 specified three parts — the proposer, the
# schema, and an adjudication loop that shows the inference and lets the user
# correct it. The first two were built. This is the third's missing half: the
# inference, emitted as the manifest's own format, so it is auditable instead
# of invisible.
#
# Note what this is NOT for any more. Before §49.3 an unidentified net was
# capped at `minor`, so the manifest was how you rescued one. It no longer
# demotes anything on its own, so the file's value is the two things the user
# alone can say: which nets they do *not* care about (the only remaining way
# to quieten a finding), and which nets are power despite looking like nothing
# (which admits them to `current_capacity` and `heat_path`).

#: Nets KiCad names itself for a pin that is connected to nothing. They carry
#: no copper and cannot be a review finding, and on the boards here they are
#: 24-65 of every board's "unidentified" count — more than half of it. Listing
#: them would bury the nets a user might actually want to say something about.
_PLACEHOLDER = re.compile(r"^/?unconnected-", re.I)


def is_placeholder(net: str) -> bool:
    return bool(_PLACEHOLDER.match(net or ""))


def shortlist(classes: NetClassMap, named_by_findings: set[str] | None = None,
              routed: set[str] | None = None) -> list:
    """The nets where an opinion changes something, as `NetKind`s.

    Nothing identified them, they are not `unconnected-*` placeholders, and the
    user has not already ruled on them — a net they declared is settled, and
    re-proposing it reads as *"your file was ignored"*, which is exactly how one
    session read it.

    `named_by_findings` narrows that to the nets **this run's findings actually
    mention**, which is what turns 188 unidentified nets into the 9-13 a user
    could have an opinion about. `routed` is the weaker fallback: every net
    carrying track copper. Pass neither and every unidentified net comes back.

    Lifted out of `propose()` because `audit.propose_intent` had written the
    same predicate a second time, and the app's Facts tab would have been the
    third. The shortlist is the whole feature — a table of 229 nets is a worse
    manifest conversation, not a better one — so there is one definition of it
    and the callers agree by construction rather than by inspection.
    """
    unidentified = [k for k in classes.by_net.values()
                    if k.net_class == "other" and not is_placeholder(k.net)
                    and not k.declared]
    if named_by_findings is not None:
        keep = named_by_findings
    elif routed is not None:
        keep = routed
    else:
        return sorted(unidentified, key=lambda k: k.net)
    return sorted((k for k in unidentified if k.net in keep),
                  key=lambda k: k.net)


def propose(classes: NetClassMap, board=None,
            named_by_findings: set[str] | None = None) -> str:
    """The inference, as a net-intent manifest the user can edit.

    Every net appears, with the class inferred for it and the evidence that
    produced it, so a wrong inference is visible rather than silent. The nets
    worth a human's attention are grouped at the top under a comment saying
    what to do with them; everything else follows, commented out, so the file
    is a complete record without being a list of 190 live declarations.

    `named_by_findings` — the nets this run's findings actually mention — is
    what turns 188 unidentified nets into the handful a user could have an
    opinion about. Pass `report.findings`' nets; omit it and the shortlist
    falls back to every routed unidentified net.
    """
    routed = None
    if board is not None:
        routed = {n.name for n in board.nets.values()
                  if n.name and board.tracks_on_net(n.code)}

    listed_kinds = shortlist(classes, named_by_findings=named_by_findings,
                             routed=routed)
    listed = {k.net for k in listed_kinds}
    rest = [k for k in classes.by_net.values() if k.net not in listed]

    lines = [
        "# net intent — PROPOSED by pcbreview, for a human to correct.",
        "#",
        "# Every net on this board appears below with the class that was",
        "# inferred for it and the evidence that produced it. The inference is",
        "# good at buses and rails and blind to anything else, so the nets it",
        "# could not identify are listed FIRST and live: those are the ones",
        "# worth an opinion.",
        "#",
        "# An unidentified net is NOT demoted — it keeps the full severity its",
        "# check computed. Editing this file is how you change that in either",
        "# direction:",
        "#",
        "#   net_class: other      quieten it. The only thing that lowers a",
        "#                         severity anywhere in this tool.",
        "#   net_class: power      it carries current. Admits it to the",
        "#                         current-capacity and heat-path checks.",
        "#   net_class: protocol   it is a bus the inference missed.",
        "#",
        "# Delete any line you have no opinion about. First match wins.",
        "",
        "nets:",
    ]

    if listed_kinds:
        lines.append(f"  # --- {len(listed_kinds)} net(s) nothing identified"
                     + (", and that this run's findings name"
                        if named_by_findings is not None else ", routed")
                     + " ---")
        for kind in listed_kinds:
            lines.append(f'  - match: "{kind.net}"')
            lines.append(f"    net_class: other        # <- CHANGE OR DELETE. "
                         f"{kind.evidence}")
    else:
        lines.append("  # nothing on this board was left unidentified.")

    lines += [
        "",
        f"  # --- the other {len(rest)} net(s), for the record. Uncomment to",
        "  # override one. `unconnected-*` placeholders carry no copper and",
        "  # cannot produce a finding.",
    ]
    for kind in sorted(rest, key=lambda k: k.net):
        lines.append(f'  # - match: "{kind.net}"')
        lines.append(f"  #   net_class: {kind.net_class}"
                     f"{' ' * max(1, 10 - len(kind.net_class))}"
                     f"# {kind.evidence}")
    return "\n".join(lines) + "\n"


def declaration_report(classes: NetClassMap, capping: dict,
                       source_path=None) -> str | None:
    """One line confirming the user's manifest was read, or `None` if none was.

    Silence used to be the only signal. A session wrote a manifest with four
    declarations, the audit said `0 finding(s) lowered to minor`, and then
    re-proposed all four nets as unidentified — so the output actively
    suggested the file had been ignored when in fact every pattern had matched
    and "0 lowered" was correct (all four were already at the cap). They
    established it by calling `load_intent` by hand.

    An auto-discovered input that is never named is an input nobody can check.
    """
    if not classes.declared and not classes.unmatched_declarations:
        return None
    # Nets and patterns are different things and adding them was wrong: two
    # patterns matching four nets is not "six declarations".
    matched = len(classes.declared)
    missed = len(classes.unmatched_declarations)
    where = f" {Path(source_path).name}" if source_path else ""
    line = (f"net intent: loaded{where} — {matched} net(s) matched your "
            f"declarations, {missed} pattern(s) matched nothing, "
            f"{capping.get('capped', 0)} finding(s) lowered")
    if matched and not capping.get("capped"):
        line += (" (the declared nets' findings were already at or below "
                 "minor, so there was nothing to lower)")
    return line


def main(argv: list[str]) -> int:
    """`python3 -m pcbreview.netclass propose <board.kicad_pcb> [schematic]`."""
    import sys

    if len(argv) < 3 or argv[1] != "propose":
        sys.stderr.write(
            "usage: python3 -m pcbreview.netclass propose "
            "<board.kicad_pcb> [top.kicad_sch] [--out FILE]\n"
            "\n"
            "Writes the class inferred for every net, with its evidence, as a\n"
            "net-intent manifest for a human to correct. Goes to stdout unless\n"
            "--out is given; nothing is written to disk without it.\n")
        return 2

    from pcbreview.model import Board
    from pcbreview import intent as intent_module
    from pcbreview.schematic import netlist

    out_path = None
    positional = []
    index = 2
    while index < len(argv):
        if argv[index] == "--out":
            if index + 1 >= len(argv):
                sys.stderr.write("--out needs a path\n")
                return 2
            out_path, index = argv[index + 1], index + 2
        else:
            positional.append(argv[index])
            index += 1

    board = Board.from_kicad(positional[0])
    schematic_path = (positional[1] if len(positional) > 1
                      else Path(positional[0]).with_suffix(".kicad_sch"))
    schematic = (netlist(schematic_path) if Path(schematic_path).exists()
                 else None)

    inferred = intent_module.infer(schematic, board) if schematic else None
    classes = classify(
        schematic,
        inferred.rail_nets if inferred else set(),
        inferred.ground_nets if inferred else set(),
        inferred.mapping if inferred else None,
        {n.name for n in board.nets.values() if n.name},
        board=board)

    document = propose(classes, board=board)
    if out_path:
        Path(out_path).write_text(document, encoding="utf-8")
        sys.stderr.write(f"wrote {out_path}\n")
    else:
        sys.stdout.write(document)

    census = classes.census()
    sys.stderr.write(
        f"\n{census['nets']} net(s): "
        f"{census['by_class'].get('protocol', 0)} protocol, "
        f"{census['by_class'].get('power', 0)} power, "
        f"{census['by_class'].get('other', 0)} unidentified "
        f"(of which "
        f"{sum(1 for k in classes.by_net.values() if is_placeholder(k.net))} "
        f"are unconnected-* placeholders that carry no copper)\n")
    if schematic is None:
        sys.stderr.write(
            "no schematic was found, so every class here comes from net names "
            "and routed width alone — much weaker evidence than pin names\n")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
