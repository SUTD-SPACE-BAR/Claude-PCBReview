"""Junction temperature, from data a human read out of a real datasheet.

the ruling, 2026-08-02, and the whole design follows from it:

> The thermals should be done if a datasheet for the component was provided
> with the junction temperature, but if the information is not available then
> the user should be told that the thermals weren't calculated due to lack of
> that information. The review still runs — it doesn't refuse to run without
> thermal information like how it refuses to run without a stackup.

and, on where the numbers come from:

> We must never trust that the user used the symbol fields in the schematic for
> each component properly. People are careless and lazy and won't know. We must
> also never trust the datasheet links which are sometimes recorded in the
> symbol fields, unless the user swears that the datasheet link is correct.

So this module **reads nothing from the schematic's symbol fields**, and it
follows no URL. Its only input is a thermal manifest that Claude writes after
reading a PDF datasheet the user handed over. The user never edits it; Claude is
the front end (§38). Every entry therefore carries a `source` string naming the
document it came from, and a finding quotes it, so a wrong number is traceable
to a wrong reading rather than to the tool.

That policy is not fussiness. Measured over all eight designs in this repo:
**not one of 782 symbols carries a thermal field**, and part identity is absent
exactly where it would be needed — 5 MPNs across the unreferenced four-layer board's 212 parts,
1 across the four-layer board's 104. There is nothing here to trust even if trusting were
wise.

Three thermal gates, and this module is not one of them
-------------------------------------------------------
Gate 1 (the stackup) refuses to run, because every impedance number without it
is invented. Thermal is **additive**: its absence invalidates nothing else, so
the audit runs and reports everything else and says plainly what it could not
assess. That is why the first thing built here is `coverage`, not the physics.

The physics
-----------
    Tj = Ta + theta_JA * P

which is a one-line steady-state model and is exactly as good as its inputs.
The dominant error is **not** the arithmetic:

**`theta_JA` from a datasheet is a property of JEDEC's test board, not of
yours.** JESD51-2/51-7 fix the board — usually 1s0p or 2s2p, 76.2 x 114.3 mm,
with a specified copper area — precisely so that two vendors' numbers can be
compared. On a small board with a starved thermal pad the real figure is
routinely 1.5-3x the quoted one, and it moves the answer far more than any
rounding here. So a `theta_ja_c_per_w` result is reported with that stated on
the finding, always, in the same spirit as the standing rule against claiming
EMC pass/fail: this is a ranking, not a temperature measurement.

Where a `theta_jc` and a real heatsinking path are known, pass `theta_ja` for
the path that actually exists and say so in `source`. This module does not
invent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Ambient assumed when the manifest names none. 25 C is the bench, not a
# rocket airframe in the sun; it is stated on every finding as an assumption
# rather than buried, because a 40 C error here is a 40 C error in Tj.
DEFAULT_AMBIENT_C = 25.0

# Fraction of the datasheet's available junction rise (Tj_max - Ta) a part may
# use before it is worth reporting. Judgement calls, and declared as such (§9):
# there is no standard that sets them. The reasoning is margin for the things
# this model cannot see — theta_JA being a JEDEC-board number, ambient being
# assumed, and dissipation being an operating-point estimate rather than a
# measurement. Any one of those is worth tens of percent.
RISE_FRACTION_MINOR = 0.60
RISE_FRACTION_MAJOR = 0.80
RISE_FRACTION_CRITICAL = 1.00

# Part classes that dissipate enough to be worth asking for a datasheet. Used
# only to *name what is missing* — never to infer a thermal number. Matched
# against the symbol's value, library id and description together.
#
# Three things here were wrong on the first run and are worth not repeating:
#
# * **The manufacturer prefixes must be anchored.** Unanchored, `mp\d{4}` matches
#   the substring "MP3354" inside `RCLAMP3354S.TCT` and reports an ESD clamp as
#   a dissipative part. They earn their place even so — `TPS63020DSJR`,
#   `LM74700` and `NCP692` are real converters whose descriptions say nothing
#   useful — but only with the `\b`.
# * **"switch" alone catches pushbuttons.** KiCad describes `SW_Push` as "Push
#   button switch", which matched on four of the eight designs here. Only the
#   semiconductor senses are listed.
# * **A power *rating* is not a dissipation.** Matching `\d+\s*W` pulled in every
#   0.1 W resistor and every `SMAJ5.0A` TVS, whose 400 W is peak pulse power.
#   `current sense` is kept, because a 1 W 20 mOhm shunt genuinely does get hot.
#
# The parametric markers on the last line matter more than they look. On one
# small board a second-source MOSFET rated 17 W had a description never using
# word MOSFET — it reads "N-Channel Drain to Source Voltage:30V … Power
# Dissipation:17W". Class words alone miss it entirely.
#
# A *fifth* was wrong, found by the four-layer motor controller (§43): the eight
# TMI8260SP-MS H-bridges — the largest per-part dissipators on that board, 5 A
# each — matched nothing. Their symbol's value *and* library id are the bare
# part number and the description is empty, so `driver`, `h-bridge` and `motor`
# all had nothing to read. `\btmi8\d{3}` is the same remedy as the other
# manufacturer prefixes and was measured the same way: over all eight designs
# it adds exactly those eight parts and nothing else. It is also why
# `unclassified_parts` exists — a prefix list only ever catches the families
# somebody has already been bitten by.
#
# A fourth thing was wrong, found by the lightly-routed six-layer board (§40): the list was
# built entirely out of *power* parts, so it missed every board's **processor**
# — the ESP32-S3-WROOM module that is the single largest heat source on three
# of these designs, and the i.MX RT1062 on two more. A Wi-Fi module in a small
# enclosure is exactly the case where theta_JA matters, and the front end was
# never asking for its datasheet. Measured over all seven designs, the
# processor line below adds 7 parts and every one is that board's own MCU or
# radio; nothing else moved. `esp32`/`teensy` are there for the same reason the
# manufacturer prefixes are: the unreferenced four-layer board's processor has value `~`, no
# description, and only its library id to go on.
# A sixth was wrong, found by the six-layer board's own review: two 10S45SP
# catch diodes — 10 A Schottky rectifiers in a *non-synchronous* buck, where
# the catch diode is often the hottest part on the board — appeared on **no
# list at all**. The field report and the handoff both recorded this as "the
# shortlist only classifies Q- and U-prefixed parts", and both are wrong: the
# parts are U7 and U9, and every D-prefixed part on that board is a TVS or an
# LED and correctly excluded. Widening the prefix set would not have caught
# them.
#
# They fell between the two lists. `_DISSIPATIVE` had no rectifier vocabulary,
# and their description is a 240-character parametric dump — "Diode
# Configuration:… Current - Rectified:10A… Forward(Vf@If):340mV" — that names
# no class word; while `unclassified_parts` skipped them *because* that
# description is non-empty, which it takes as evidence the part was described.
#
# `rectif` is the marker, and it was chosen by measurement over all eight
# designs: it adds exactly those two plus one sibling and nothing else. The two
# obvious alternatives were measured and refused — `schottky` adds nine, of
# which eight are BAT54 signal diodes that dissipate nothing, and `\bdiode\b`
# adds fifty-six, which is every LED and every TVS on every board.
#
# What it does NOT fix, said plainly: a part whose whole description is
# "Schottky diode" states no rating at all, so nothing here can tell a 3 A SS34
# from a 200 mA BAT54. That is an `unclassified_parts` question, not a
# shortlist one, and it is not solved here.
_DISSIPATIVE = re.compile(
    r"regulat|\bldo\b|mosfet|\bfet\b|driver|h-?bridge|transistor|rectif|"
    r"side switch|load switch|power switch|switching\s+(regulat|convert)|"
    r"\bbuck\b|\bboost\b|dc-?dc|amplifier|\bopamp\b|op-amp|shunt|current sense|"
    r"\bload\b|charger|\bpmic\b|motor|solenoid|relay|\bigbt\b|"
    r"\btps\d|\blm\d{3}|\bncp\d|\bap\d{2,4}[a-z]|\bmp\d{4}|\btmi8\d{3}|"
    r"\bmcu\b|microcontroller|\bsoc\b|\bfpga\b|\bprocessor\b|rf module|"
    r"\besp32\b|\bteensy\b|"
    r"power dissipation|rds\s*\(?on|[np]-channel", re.I)


class ThermalError(Exception):
    """Raised when a thermal manifest cannot be used."""


@dataclass(frozen=True)
class PartThermal:
    """One part's datasheet thermal data, as read by a human from a PDF.

    ``theta_ja_c_per_w`` is **optional, and its absence is a stated fact, not a
    gap in the transcription.** the ruling, 2026-08-03:

    > Even if the frontend wasn't going to ASK me for the datasheets of the
    > H bridges, I GAVE the datasheets, so Claude ought to have looked at
    > everything I gave and TOLD the auditor to pay special attention to those
    > parts, not ignored what I gave unless it thought it needs it.

    That is what `theta_ja_stated: false` is for. Some datasheets — usually the
    second-source ones, which is exactly where the risk is — quote a package,
    a current rating and an Rds(on) and no thermal resistance at all. Before
    this, such a part could not be entered, so it fell out of the manifest
    entirely and the auditor never saw the largest dissipator on the board
    (§43). Now it is entered with the dissipation that *is* known, and the
    check reports the theta_JA the part would need instead of a junction
    temperature it cannot compute.
    """
    reference: str
    theta_ja_c_per_w: float | None
    t_junction_max_c: float
    dissipation_w: float
    #: the document this came from, quoted on every finding. Required — an
    #: unattributed thermal number is indistinguishable from a guess.
    source: str
    part: str | None = None
    ambient_c: float | None = None
    theta_jc_c_per_w: float | None = None
    note: str | None = None
    #: A power-dissipation figure quoted on a *named* board, which is what
    #: many second-source and Asian datasheets give instead of a theta_JA:
    #: "PD = 2.4 W at Tj = 150 C on a 76.2 x 114.3 x 1.6 mm 4-layer board".
    #: That converts to a theta_JA **on that board**, which is a real,
    #: defensible number and strictly more informative than declaring the
    #: thermal resistance absent — the schema used to force the latter.
    pd_w: float | None = None
    pd_at_tj_c: float | None = None
    pd_board_note: str | None = None
    #: Some datasheets state no Tj(max) at all — only a storage limit and an
    #: operating *ambient* range. For those the binding constraint is the
    #: ambient limit, and there was no field for it.
    t_junction_max_stated: bool = True
    t_ambient_max_c: float | None = None
    #: False when the datasheet's thermal resistance is known but this part's
    #: load is not. `dissipation_w` is then meaningless and the check reports
    #: the **budget** — how much this package can dissipate before it reaches
    #: Tj(max) — instead of a junction temperature it cannot compute.
    dissipation_stated: bool = True

    @property
    def dissipation_budget_w(self) -> float | None:
        """Watts this part can dissipate before Tj(max), or `None`.

        `(Tj_max - Ta) / theta_JA`. A real number, checkable against a load
        the designer works out later, and strictly more useful than leaving
        the part off every list.
        """
        theta = self.theta_ja_c_per_w or self.theta_ja_derived_from_pd
        if theta is None or theta <= 0:
            return None
        ambient = self.ambient_c if self.ambient_c is not None else 25.0
        return (self.t_junction_max_c - ambient) / theta

    @property
    def theta_ja_derived_from_pd(self) -> float | None:
        """theta_JA implied by a PD figure on a named board, or `None`.

        `(Tj_at_which_PD_is_quoted - 25 C) / PD`. The 25 C is the ambient the
        vendor's PD derating curve starts from, which is the convention every
        such figure uses.
        """
        if self.pd_w in (None, 0) or self.pd_at_tj_c is None:
            return None
        return (self.pd_at_tj_c - 25.0) / self.pd_w

    @property
    def theta_ja_stated(self) -> bool:
        """Did the datasheet give a theta_JA at all?

        A PD-on-a-named-board figure counts: it is the vendor's own
        measurement, on a board they named, and the arithmetic to a theta_JA
        is exact. What it is *not* is a theta_JA on **this** board — but
        neither is a JEDEC figure, and every finding says so already.
        """
        return (self.theta_ja_c_per_w is not None
                or self.theta_ja_derived_from_pd is not None)

    @property
    def effective_theta_ja_c_per_w(self) -> float | None:
        """The theta_JA to compute with: stated first, then derived from PD."""
        if self.theta_ja_c_per_w is not None:
            return self.theta_ja_c_per_w
        return self.theta_ja_derived_from_pd

    def junction_c(self, default_ambient_c: float) -> float | None:
        rise = self.rise_c
        return None if rise is None else self.ambient_at(default_ambient_c) + rise

    def ambient_at(self, default_ambient_c: float) -> float:
        return (self.ambient_c if self.ambient_c is not None
                else default_ambient_c)

    @property
    def rise_c(self) -> float | None:
        """Junction rise above ambient — the part the layout can influence."""
        theta = self.effective_theta_ja_c_per_w
        if theta is None:
            return None
        return theta * self.dissipation_w

    def required_theta_ja_c_per_w(self, default_ambient_c: float) -> float | None:
        """The theta_JA this part needs to stay inside Tj(max) as placed.

        The one number that is still computable when the datasheet gives no
        thermal resistance, and it is the number that decides the argument:
        compare it against what the package can actually achieve.
        """
        available = self.t_junction_max_c - self.ambient_at(default_ambient_c)
        if self.dissipation_w <= 0:
            return None
        return available / self.dissipation_w

    def headroom_c(self, default_ambient_c: float) -> float | None:
        junction = self.junction_c(default_ambient_c)
        return None if junction is None else self.t_junction_max_c - junction

    def rise_fraction(self, default_ambient_c: float) -> float:
        """Fraction of the available rise used: (Tj - Ta) / (Tj_max - Ta).

        Normalised against the *available* rise rather than against Tj_max
        absolutely, because a part rated to 150 C sitting in a 60 C bay has
        90 C to play with, not 150.
        """
        available = self.t_junction_max_c - self.ambient_at(default_ambient_c)
        if available <= 0:
            return float("inf")
        rise = self.rise_c
        return 0.0 if rise is None else rise / available


@dataclass
class ThermalManifest:
    """Everything a human read out of datasheets for one board."""
    parts: dict[str, PartThermal] = field(default_factory=dict)
    ambient_c: float = DEFAULT_AMBIENT_C
    source_path: Path | None = None

    def __bool__(self) -> bool:
        return bool(self.parts)


_REQUIRED = ("t_junction_max_c", "dissipation_w", "source")

#: Set this to `false` on an entry whose datasheet states no thermal
#: resistance. It is required in that case rather than allowing
#: `theta_ja_c_per_w` to be merely absent, for the same reason
#: `stackup_cli transcribe` requires `to_copper` (§40c): an omitted key would
#: silently decide the difference between "the datasheet does not say" and
#: "the transcription forgot", and those must not look alike.
#: Below this, a part cannot get hot and a missing theta_JA decides nothing.
#: 10 mW into any real package is under 1 C of rise even at 100 C/W, so the
#: "required theta_JA" that the no-theta_JA branch computes comes out in the
#: millions and reads as alarm where there is none.
#:
#: The rule exists because `SKILL.md` is emphatic that every supplied datasheet
#: gets a manifest entry — and then the check ranked two 0.1 mW parts above the
#: only computed junction temperature on the board. A tool must not punish the
#: user for following its own instruction (the user's ruling, §51).
#:
#: Note what this does NOT relax: a part is only ever entered from a datasheet
#: PDF handed to Claude directly. A datasheet *link* in a schematic symbol
#: field is never followed and never counts as a datasheet supplied — two of
#: those held a part number rather than a URL.
NEGLIGIBLE_DISSIPATION_W = 0.010

_THETA_JA_STATED = "theta_ja_stated"
_DISSIPATION_STATED = "dissipation_stated"


def load(path: str | Path) -> ThermalManifest:
    """Read a thermal manifest. Raises rather than half-loading.

    Missing fields are an error, not a default: a part entered with no
    `dissipation_w` would silently read as 0 W and report a comfortable
    junction temperature, which is the exact silent-zero failure §36 found
    twice and the ruling exists to prevent.
    """
    path = Path(path)
    if not path.exists():
        raise ThermalError(f"thermal manifest not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "parts" not in data:
        raise ThermalError(f"{path} is not a thermal manifest (needs 'parts')")

    ambient = float(data.get("ambient_c", DEFAULT_AMBIENT_C))
    parts: dict[str, PartThermal] = {}
    for reference, entry in (data["parts"] or {}).items():
        if not isinstance(entry, dict):
            raise ThermalError(f"{path}: entry for {reference} is not a mapping")
        # `dissipation_stated: false` is the third state, and it exists for
        # the same reason `theta_ja_stated: false` does. Field report,
        # 2026-08-05: a session held datasheets for four dissipative parts,
        # had their theta_JA and Tj(max), and **could enter none of them**
        # because the load currents were unknown — so *"four dissipative parts
        # stayed unreviewed while I was holding their datasheets."*
        #
        # Inventing a dissipation is exactly what this schema refuses, and it
        # is right to. But the inverse is a real, checkable number: at a known
        # theta_JA and Tj(max), how much CAN this part dissipate before it
        # reaches its limit? That turns an unreviewed part into a bounded one.
        dissipation_stated = entry.get(_DISSIPATION_STATED)
        required = list(_REQUIRED)
        if dissipation_stated is False:
            required.remove("dissipation_w")
            if entry.get("dissipation_w") not in (None, ""):
                raise ThermalError(
                    f"{path}: {reference} sets `{_DISSIPATION_STATED}: false` "
                    "and also gives dissipation_w — one of the two is wrong")
            if entry.get("theta_ja_c_per_w") in (None, "") \
                    and entry.get("pd_w") is None:
                raise ThermalError(
                    f"{path}: {reference} states no dissipation AND no "
                    "thermal resistance, so there is nothing to bound. Give "
                    "theta_ja_c_per_w (or pd_w on a named board), or leave "
                    "the part out and say no datasheet was supplied")
        missing = [k for k in required if entry.get(k) in (None, "")]
        if missing:
            raise ThermalError(
                f"{path}: {reference} is missing {', '.join(missing)} — every "
                "thermal entry must state its datasheet, or the number cannot "
                "be checked"
                + (". If the datasheet gives a thermal resistance but you do "
                   "not know this part's load, say "
                   f"`{_DISSIPATION_STATED}: false` and the check reports how "
                   "much it CAN dissipate instead"
                   if "dissipation_w" in missing else ""))

        pd_w = entry.get("pd_w")
        pd_at_tj = entry.get("pd_at_tj_c")
        if (pd_w is None) != (pd_at_tj is None):
            raise ThermalError(
                f"{path}: {reference} gives one of pd_w / pd_at_tj_c and not "
                "the other. A power-dissipation figure means nothing without "
                "the junction temperature it is quoted at")
        if pd_w is not None and not entry.get("pd_board_note"):
            raise ThermalError(
                f"{path}: {reference} gives pd_w without pd_board_note. A PD "
                "figure is only meaningful with the board it was measured on "
                "— name it, the same way `source` names the document")

        theta_ja = entry.get("theta_ja_c_per_w")
        stated = entry.get(_THETA_JA_STATED)
        if theta_ja in (None, "") and pd_w is not None:
            # A PD figure on a named board carries the thermal resistance.
            theta_ja = None
        elif theta_ja in (None, ""):
            if stated is not False:
                raise ThermalError(
                    f"{path}: {reference} has no theta_ja_c_per_w. If the "
                    "datasheet genuinely states none, say so explicitly with "
                    f"`{_THETA_JA_STATED}: false` and the check will report "
                    "the theta_JA the part would need instead. Leaving the key "
                    "out cannot be told apart from forgetting to transcribe "
                    "it. If the datasheet instead quotes a PD on a named "
                    "board, give pd_w / pd_at_tj_c / pd_board_note")
            theta_ja = None
        else:
            if stated is False:
                raise ThermalError(
                    f"{path}: {reference} sets `{_THETA_JA_STATED}: false` and "
                    "also gives theta_ja_c_per_w — one of the two is wrong")
            theta_ja = float(theta_ja)

        parts[str(reference)] = PartThermal(
            reference=str(reference),
            theta_ja_c_per_w=theta_ja,
            t_junction_max_c=float(entry["t_junction_max_c"]),
            dissipation_w=(float(entry["dissipation_w"])
                           if dissipation_stated is not False else 0.0),
            dissipation_stated=dissipation_stated is not False,
            source=str(entry["source"]),
            part=entry.get("part"),
            ambient_c=(float(entry["ambient_c"])
                       if entry.get("ambient_c") is not None else None),
            theta_jc_c_per_w=(float(entry["theta_jc_c_per_w"])
                              if entry.get("theta_jc_c_per_w") is not None
                              else None),
            note=entry.get("note"),
            pd_w=float(pd_w) if pd_w is not None else None,
            pd_at_tj_c=float(pd_at_tj) if pd_at_tj is not None else None,
            pd_board_note=entry.get("pd_board_note"),
            t_junction_max_stated=bool(
                entry.get("t_junction_max_stated", True)),
            t_ambient_max_c=(float(entry["t_ambient_max_c"])
                             if entry.get("t_ambient_max_c") is not None
                             else None))
    return ThermalManifest(parts=parts, ambient_c=ambient, source_path=path)


def default_path(board_path: str | Path) -> Path:
    """Where a board's thermal manifest lives, if it has one."""
    return Path(board_path).with_suffix(".thermal.yaml")


# ---------------------------------------------------------------------------
# What could not be assessed, which is the whole point until a manifest exists
# ---------------------------------------------------------------------------

def dissipative_candidates(schematic) -> list[tuple[str, str]]:
    """Parts worth asking the user for a datasheet for, as (reference, why).

    A *shortlist for the human*, never an input to a calculation. It matches on
    what the symbol calls itself, which is unreliable in exactly the way the user
    described — so it is used only to say "these look like they dissipate, send
    me their datasheets", and a miss costs a prompt rather than a wrong number.

    The label falls back to the library id when the value is empty **or is
    KiCad's `~` placeholder** (§40). the unreferenced four-layer board's processor is exactly
    that case, and `"U1 — ~"` in a list whose whole job is to name the parts to
    go and find a PDF for is worse than useless.
    """
    found: list[tuple[str, str]] = []
    for component in schematic.components:
        if component.dnp:
            continue
        haystack = " ".join(filter(None, (
            component.value, component.lib_id, component.description)))
        match = _DISSIPATIVE.search(haystack)
        if match:
            value = (component.value or "").strip()
            found.append((component.reference,
                          value if value not in ("", "~") else component.lib_id))
    found.sort()
    return found


#: A description that is nothing but a supplier catalogue number. `C9100001` is
#: an order code, not a description of anything — and treating it as one is how
#: four sensors and a transistor on two boards here counted as "described".
_SUPPLIER_CODE_ONLY = re.compile(r"^\s*C\d{4,}\s*$", re.I)

#: A description that states a rating a thermal reader could use. Either a
#: number attached to a unit, or one of the parametric keys a distributor
#: export emits. The point is not the number itself — it is that a description
#: carrying one was written by something that knew the part's ratings, and a
#: description carrying none was not.
_STATES_A_RATING = re.compile(
    r"\d+\s*(v|a|ma|w|mw|mohm|ohm|mΩ|Ω)\b|"
    r"current\s*-|voltage\s*-|power dissipation|rds\s*\(?on|"
    r"\bvf\b|forward\s*\(", re.I)

#: Classes whose *worst* case is still not a board's thermal problem, so a
#: bare class word is enough for them and no rating is needed. Measured over
#: all eight designs, this is what a "no rating stated" rule would otherwise
#: add: 15 indicator LEDs, 8 zeners, 3 ESD clamps, 2 CAN bus protectors, 2
#: logic buffers, a NOR flash and a temperature sensor — 33 parts of no
#: thermal interest, against 9 Schottky diodes of which one is a 3 A
#: rectifier. Excluding them is what keeps this a list somebody reads.
#:
#: It is a judgement and it is the one part of this that will need extending.
#: A part wrongly excluded here is invisible to both thermal lists, so the bar
#: for adding a word is "this class cannot be the hot part on a board",
#: not "this class usually is not".
_THERMALLY_BENIGN = re.compile(
    r"light emitting|\bled\b|zener|esd protection|\btvs\b|transient|"
    r"bus protector|temperature sensor|\bbuffer\b|\bflash\b|\bgnss\b|"
    r"accelerometer|gyro|magnetometer|barometric|pressure sensor|"
    r"\beeprom\b|\bnor\b|\blogic\b|\bcmos\b|oscillator|crystal|resonator",
    re.I)


@dataclass(frozen=True)
class UnclassifiedPart:
    """A semiconductor nothing here could classify either way.

    `lcsc` is the whole reason this is a record rather than a tuple. the
    ruling, 2026-08-04:

    > If the JLC trawler skill is available, trigger the JLC trawler skill to
    > find information about the part if it has an LCSC code. Class word and no
    > rating is fine if LCSC data can't be retrieved, but then the user must be
    > told that this part needs the user to give information about it before
    > that can be reviewed.

    So the engine's job is to say *which* parts are unresolved and *whether
    there is a catalogue number to resolve them with*. Looking the number up is
    the front end's job — nothing in this package touches a catalogue, and a
    part resolved from one still ends up in the thermal manifest by hand.
    """
    reference: str
    label: str
    #: why nothing could classify it, in the words the finding prints
    reason: str
    #: an LCSC/JLCPCB catalogue number if the symbol carries one, else `None`
    lcsc: str | None = None


def _lcsc_code(component) -> str | None:
    """This part's LCSC catalogue number, from wherever it was actually filed.

    `supplier_part` prefers a `C`-code-shaped value across every supplier
    alias. The description is checked too, because a description that is
    *only* a catalogue number is precisely one of the cases here — the code is
    real, it is just filed in the wrong field.
    """
    code = (getattr(component, "supplier_part", None) or "").strip()
    if re.match(r"^C\d{3,}$", code, re.I):
        return code
    description = (getattr(component, "description", None) or "").strip()
    if _SUPPLIER_CODE_ONLY.match(description):
        return description
    return None


def _why_unclassified(component) -> str | None:
    """Why nothing can say whether this part dissipates, or `None` if it can.

    Three states, and the second and third exist because the first was too
    narrow. §53.11 recorded the hole: a part whose whole description is
    `Schottky diode` states no rating at all, so nothing can tell a 3 A SS34
    from a 200 mA BAT54 — and both were invisible to *both* thermal lists,
    because a non-empty description counted as evidence the part was described.
    """
    description = (component.description or "").strip()
    if not description:
        return ("its symbol carries no description at all, so nothing here "
                "knows what it is")
    if _SUPPLIER_CODE_ONLY.match(description):
        return (f"its whole description is the catalogue number "
                f"`{description}`, which describes nothing")
    if _STATES_A_RATING.search(description):
        return None
    if _THERMALLY_BENIGN.search(description + " " + (component.value or "")):
        return None
    return (f"its description — `{description}` — names a class but states no "
            f"rating, so a part that dissipates watts and one that dissipates "
            f"milliwatts read identically here")


def unclassified_parts(schematic) -> list[UnclassifiedPart]:
    """Semiconductors the shortlist could not classify *either way*.

    `dissipative_candidates` matches text, so a part whose symbol carries no
    description and whose value is a bare manufacturer part number is
    invisible to it — and, worse, is indistinguishable from a part that was
    examined and found not to dissipate. That is the silent zero §32.C's rule
    exists to catch, and it cost this project a real defect: on a four-layer
    motor controller the eight H-bridges, the largest heat sources on the
    board, were never put on any list for the front end to ask a datasheet
    for (§43).

    This is deliberately *not* a claim that these parts dissipate. It is a
    claim that nothing here knows. Measured over all eight designs it is 52
    parts, 0-10 per board, and among them are a load switch, an LDO and a 3 A
    Schottky rectifier that the class list also misses — so it is not specific
    to the board that found it. **22 of the 52 carry an LCSC catalogue
    number**, and on two of the designs every unclassified part does, which is
    the whole reason `lcsc` is on the record: those need nobody asked.
    """
    if not schematic:
        return []
    classified = {r for r, _ in dissipative_candidates(schematic)}
    out: list[UnclassifiedPart] = []
    seen: set[str] = set()
    for component in schematic.components:
        if component.dnp or component.reference in classified:
            continue
        if not re.match(r"^[UQD]\d", component.reference or ""):
            continue
        if component.reference in seen:
            continue          # one entry per designator, not per placement
        reason = _why_unclassified(component)
        if reason is None:
            continue
        seen.add(component.reference)
        value = (component.value or "").strip()
        out.append(UnclassifiedPart(
            reference=component.reference,
            label=value if value not in ("", "~") else component.lib_id,
            reason=reason, lcsc=_lcsc_code(component)))
    out.sort(key=lambda p: p.reference)
    return out


def coverage(schematic, manifest: ThermalManifest | None) -> dict:
    """What thermal analysis had to work with. Print this before believing a zero.

    The same discipline as `placement.coverage` (§32.C): a check that finds
    nothing because it had nothing to look at must be distinguishable from one
    that looked and found nothing.
    """
    candidates = dissipative_candidates(schematic) if schematic else []
    have = set(manifest.parts) if manifest else set()
    references = ({c.reference for c in schematic.components if not c.dnp}
                  if schematic else set())
    return {
        "manifest": bool(manifest),
        "parts_with_data": len(have),
        "dissipative_candidates": len(candidates),
        "candidates_without_data": len([r for r, _ in candidates
                                        if r not in have]),
        "unclassified_parts": len(unclassified_parts(schematic)),
        "unknown_references": sorted(have - references) if references else [],
    }


# ---------------------------------------------------------------------------
# The assessment
# ---------------------------------------------------------------------------

@dataclass
class JunctionResult:
    """One part's junction temperature and how much margin is left.

    When the datasheet stated no theta_JA, `junction_c`, `headroom_c` and
    `rise_fraction` are all `None` and `required_theta_ja_c_per_w` carries the
    only number that is still computable. A result in that state is *not* a
    pass — see `severity`.
    """
    part: PartThermal
    ambient_c: float
    junction_c: float | None
    headroom_c: float | None
    rise_fraction: float | None
    required_theta_ja_c_per_w: float | None = None

    @property
    def assessed(self) -> bool:
        return self.junction_c is not None

    @property
    def severity(self) -> str:
        if not self.assessed:
            # A dissipation is known and the thermal resistance is not, so no
            # junction temperature exists to rank. `minor` rather than `info`
            # because this is a part the user handed a datasheet for and it
            # still cannot be cleared -- the whole point of the ruling that
            # created this path (§43a).
            return "minor"
        if self.rise_fraction >= RISE_FRACTION_CRITICAL:
            return "critical"
        if self.rise_fraction >= RISE_FRACTION_MAJOR:
            return "major"
        if self.rise_fraction >= RISE_FRACTION_MINOR:
            return "minor"
        return "info"

    @property
    def score(self) -> float:
        """Multiples of the minor threshold, as every other check scores."""
        if not self.assessed:
            return 1.0
        return self.rise_fraction / RISE_FRACTION_MINOR

    def summary(self) -> dict:
        return {
            "reference": self.part.reference,
            "part": self.part.part,
            "theta_ja_c_per_w": self.part.effective_theta_ja_c_per_w,
            "theta_ja_source": (
                "stated" if self.part.theta_ja_c_per_w is not None
                else "derived from PD on a named board"
                if self.part.theta_ja_derived_from_pd is not None else None),
            "pd_board_note": self.part.pd_board_note,
            "t_junction_max_stated": self.part.t_junction_max_stated,
            "t_ambient_max_c": self.part.t_ambient_max_c,
            "theta_ja_stated": self.part.theta_ja_stated,
            "required_theta_ja_c_per_w": (
                round(self.required_theta_ja_c_per_w, 1)
                if self.required_theta_ja_c_per_w is not None else None),
            "dissipation_w": self.part.dissipation_w,
            "ambient_c": round(self.ambient_c, 1),
            "junction_c": (round(self.junction_c, 1)
                           if self.junction_c is not None else None),
            "t_junction_max_c": self.part.t_junction_max_c,
            "headroom_c": (round(self.headroom_c, 1)
                           if self.headroom_c is not None else None),
            "rise_fraction": (round(self.rise_fraction, 3)
                              if self.rise_fraction is not None else None),
        }


def assess(manifest: ThermalManifest) -> list[JunctionResult]:
    """Junction temperature for every part the manifest carries, worst first."""
    results = []
    for part in manifest.parts.values():
        ambient = part.ambient_at(manifest.ambient_c)
        results.append(JunctionResult(
            part=part, ambient_c=ambient,
            junction_c=part.junction_c(manifest.ambient_c),
            headroom_c=part.headroom_c(manifest.ambient_c),
            rise_fraction=(part.rise_fraction(manifest.ambient_c)
                           if part.theta_ja_stated else None),
            required_theta_ja_c_per_w=part.required_theta_ja_c_per_w(
                manifest.ambient_c)))
    # Unassessable parts sort just under the ones that breach, and above the
    # ones that pass: they are the open question, not the good news.
    results.sort(key=lambda r: -(r.rise_fraction if r.assessed
                                 else RISE_FRACTION_MAJOR))
    return results
