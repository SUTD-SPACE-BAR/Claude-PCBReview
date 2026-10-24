"""What the nets are *for*, inferred from the schematic.

Geometry alone cannot rank findings. A 40 mm run with a mediocre return path is
a defect on a 1.575 GHz GPS feed and irrelevant on an I2C line, and nothing in
the layout says which is which. Until now the code guessed from net names:
`looks_like_ground` matched `GND|VSS|AGND|…` against every net on the board,
and differential pairs were found by `_P`/`_N` suffixes.

Names are the designer's choice of words. The schematic carries the actual
circuit, so this module derives the same conclusions structurally and reports
the evidence for each one.

This is the *proposer* half of the Net Intent Manifest (Gate 2): deterministic
code proposes with evidence and a confidence, Claude adjudicates, deterministic
code audits the result. Nothing here decides anything on its own — every result
carries what it was inferred from, so a wrong call is visible rather than
silent.

    from pcbreview.intent import infer
    intent = infer(schematic, board)
    intent.ground_nets              # {'GND'}
    intent.pairs                    # CANH/CANL included, without naming it
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pcbreview.model import Board
from pcbreview.schematic import Schematic

# Fallback only, for when the structural tests are inconclusive. Kept narrow.
GROUND_NAME_HINTS = ("GND", "GROUND", "AGND", "DGND", "PGND", "VSS", "EARTH")

# A rail that is the common node for at least this many *other* rails is
# behaving as a ground. One supply plus its decoupling is not evidence; being
# the shared side of several supplies' decoupling is.
GROUND_BRIDGE_THRESHOLD = 2

#: Pin-name conventions for the two halves of a differential pair. These are
#: names from the *manufacturer's symbol* (`CANH`/`CANL`, `D+`/`D-`, `IN+`/`IN-`),
#: not net names the designer chose, which is what makes them worth trusting.
_PIN_PAIR_PATTERNS = [
    (re.compile(r"^(?P<base>.*?)[_ ]?P$", re.I), re.compile(r"^(?P<base>.*?)[_ ]?N$", re.I)),
    (re.compile(r"^(?P<base>.*?)\+$"), re.compile(r"^(?P<base>.*?)-$")),
    (re.compile(r"^(?P<base>.*?)H$", re.I), re.compile(r"^(?P<base>.*?)L$", re.I)),
    (re.compile(r"^(?P<base>.*?)[_ ]?DP$", re.I), re.compile(r"^(?P<base>.*?)[_ ]?DM$", re.I)),
]


def looks_like_ground(net_name: str) -> bool:
    """Name-only ground test — the fallback, not the answer.

    Wrong in both directions: it accepts a signal called `GND_SW` and rejects a
    ground rail called `0V` or `RTN`. Use `infer()` where a schematic is
    available.
    """
    upper = net_name.upper().lstrip("/")
    return any(hint in upper for hint in GROUND_NAME_HINTS)


@dataclass
class NetRole:
    """What a net is, and why we think so."""
    net: str                    # board net name where mapped, else schematic's
    role: str                   # ground | supply | signal
    evidence: str
    confidence: float


@dataclass
class PairCandidate:
    """Two nets that the schematic says belong together."""
    positive: str               # board net names where mapped
    negative: str
    evidence: list[str]
    confidence: float

    def __str__(self) -> str:
        return (f"{self.positive} / {self.negative}  "
                f"({self.confidence:.2f}: {'; '.join(self.evidence[:2])})")


@dataclass
class BoardMapping:
    """Schematic net names to board net names, matched by *pins*, not names.

    Names would be the obvious key and are the wrong one: KiCad's generated
    names for unnamed nets do not round-trip reliably (see
    `schematic._auto_name`), and those are exactly the nets a CAN or sense pair
    tends to live on. The partition is exact, so a shared pad is proof.
    """
    by_schematic: dict[str, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def board_net(self, schematic_net: str) -> str | None:
        return self.by_schematic.get(schematic_net)


def map_to_board(schematic: Schematic, board: Board) -> BoardMapping:
    """Match every schematic net to the board net sharing its pads."""
    owner: dict[str, str] = {}
    for code, pads in board.connectivity["pads_by_net"].items():
        name = board.nets[int(code)].name
        for pad in pads:
            owner[pad] = name

    mapping = BoardMapping()
    for net in schematic.nets:
        seen = {owner[key] for key in net.pin_keys if key in owner}
        if not seen:
            mapping.unmapped.append(net.name)
        elif len(seen) == 1:
            mapping.by_schematic[net.name] = seen.pop()
        else:
            # Cannot happen while the partition is exact; if it ever does, the
            # netlist and the board have diverged and silence would be worse.
            mapping.conflicts.append(f"{net.name} spans board nets {sorted(seen)}")
            mapping.by_schematic[net.name] = sorted(seen)[0]
    return mapping


# ---------------------------------------------------------------------------
# Rails
# ---------------------------------------------------------------------------

def _rail_bridges(schematic: Schematic, rails: set[str]) -> dict[str, set[str]]:
    """For each rail, the other rails it shares a two-pin capacitor with."""
    net_of = {pin.key: net.name for net in schematic.nets for pin in net.pins}
    bridges: dict[str, set[str]] = {rail: set() for rail in rails}
    for component in schematic.components:
        if not component.reference.startswith("C") or len(component.pins) != 2:
            continue
        first = net_of.get(component.pins[0].key)
        second = net_of.get(component.pins[1].key)
        if first in rails and second in rails and first != second:
            bridges[first].add(second)
            bridges[second].add(first)
    return bridges


def classify_rails(schematic: Schematic) -> list[NetRole]:
    """Split the power-symbol nets into grounds and supplies, without names.

    Two steps, and only the first is fully structural:

    1. **Which nets are rails at all** comes from the schematic exactly — a net
       driven by a power symbol, `Net.source == "power"`. This alone is worth
       having: the old name scan ran over every net on the board, so a signal
       named `GND_SW` could be taken for a plane.

    2. **Which rail is ground** comes from decoupling topology. Ground is the
       node every supply's decoupling capacitors return to, so the rail that is
       the common side of two or more *different* supplies' capacitors is the
       ground. On these boards the separation is total — GND bridges 7 and 5
       other rails, every other rail bridges 1 or 0 — and no name is consulted
       to reach it. Split AGND/DGND grounds both qualify, which is why this is a
       threshold rather than an argmax.

    The symbol's `lib_id` looked like a better signal and is not: on these
    boards `power:+3.3V` is placed for `+5V`, `VBAT`, `VBUS` and an auxiliary
    rail, the
    symbol reused with the value edited. Only `power:GND` is used consistently,
    which is not enough to build on.

    Where step 2 is inconclusive — a board with one supply gives ground nothing
    to bridge — it falls back to the name test and says so in the evidence, at
    a confidence that reflects the guess.
    """
    rails = {net.name for net in schematic.nets if net.source == "power"}
    if not rails:
        return []

    bridges = _rail_bridges(schematic, rails)
    grounds = {rail for rail, others in bridges.items()
               if len(others) >= GROUND_BRIDGE_THRESHOLD}

    roles: list[NetRole] = []
    if grounds:
        for rail in sorted(rails):
            if rail in grounds:
                others = sorted(bridges[rail])
                agrees = looks_like_ground(rail)
                roles.append(NetRole(
                    net=rail, role="ground",
                    evidence=(f"common node of decoupling for {len(others)} "
                              f"supplies ({', '.join(others[:4])})"
                              + ("" if agrees else "; name does not look like ground")),
                    confidence=0.99 if agrees else 0.90))
            else:
                partner = sorted(bridges[rail])
                roles.append(NetRole(
                    net=rail, role="supply",
                    evidence=("power symbol; decoupled to "
                              + (", ".join(partner) if partner else "nothing")),
                    confidence=0.95))
        return roles

    # No decoupling topology to read — fall back, and be honest about it.
    for rail in sorted(rails):
        is_ground = looks_like_ground(rail)
        roles.append(NetRole(
            net=rail,
            role="ground" if is_ground else "supply",
            evidence="power symbol; decoupling topology inconclusive, name used",
            confidence=0.5))
    return roles


# ---------------------------------------------------------------------------
# Differential pairs
# ---------------------------------------------------------------------------

def propose_differential_pairs(schematic: Schematic,
                               rails: set[str] | None = None) -> list[PairCandidate]:
    """Pairs found from *pin* names on a shared component.

    A differential pair is two nets that arrive at the same device on pins the
    device itself calls two halves of one signal. That is a property of the
    manufacturer's symbol, so it holds whatever the designer named the nets —
    which is what the net-name heuristic could not do. The six-layer board's bus
    transceiver is the
    case in point: its nets are unnamed, and `Net-(U24-CANH)` / `Net-(U24-CANL)`
    match no `_P`/`_N` convention, but U24's pins are called `CANH` and `CANL`.

    Rails are excluded. A battery's `+` and `-` pins match the `+`/`-` pattern
    perfectly and are not a differential pair; requiring both halves to be
    signals removes that class without a special case for batteries.
    """
    rails = rails or set()
    net_of = {pin.key: net for net in schematic.nets for pin in net.pins}

    found: dict[tuple[str, str], list[str]] = {}
    for component in schematic.components:
        by_name = {p.name: p for p in component.pins if p.name not in ("", "~")}
        for positive_pattern, negative_pattern in _PIN_PAIR_PATTERNS:
            for name, pin in by_name.items():
                match = positive_pattern.match(name)
                if not match:
                    continue
                for other_name, other_pin in by_name.items():
                    other = negative_pattern.match(other_name)
                    if (other is None or other_name == name
                            or other.group("base") != match.group("base")):
                        continue
                    first, second = net_of.get(pin.key), net_of.get(other_pin.key)
                    if first is None or second is None or first.name == second.name:
                        continue
                    if first.name in rails or second.name in rails:
                        continue
                    # A pin with nothing else on it is not routed as a pair.
                    if len(first.pins) < 2 or len(second.pins) < 2:
                        continue
                    key = (first.name, second.name)
                    found.setdefault(key, []).append(
                        f"{component.reference}.{pin.number}({name})/"
                        f"{other_pin.number}({other_name})")

    candidates = []
    for (positive, negative), evidence in found.items():
        candidates.append(PairCandidate(
            positive=positive, negative=negative,
            evidence=sorted(set(evidence)),
            # Two devices agreeing that these are a pair is stronger than one.
            confidence=0.95 if len(set(evidence)) > 1 else 0.85))
    candidates.sort(key=lambda c: (-c.confidence, c.positive))
    return candidates


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    """Everything inferred from one schematic, in board net names where possible."""
    roles: list[NetRole]
    pairs: list[PairCandidate]
    mapping: BoardMapping | None

    @property
    def ground_nets(self) -> set[str]:
        return {r.net for r in self.roles if r.role == "ground"}

    @property
    def supply_nets(self) -> set[str]:
        return {r.net for r in self.roles if r.role == "supply"}

    @property
    def rail_nets(self) -> set[str]:
        return {r.net for r in self.roles}

    def role_of(self, net: str) -> NetRole | None:
        for role in self.roles:
            if role.net == net:
                return role
        return None

    def summary(self) -> dict:
        return {
            "ground": sorted(self.ground_nets),
            "supplies": sorted(self.supply_nets),
            "pairs": [f"{p.positive} / {p.negative}" for p in self.pairs],
            "unmapped_nets": len(self.mapping.unmapped) if self.mapping else 0,
            "conflicts": self.mapping.conflicts if self.mapping else [],
        }


def infer(schematic: Schematic, board: Board | None = None) -> Intent:
    """Infer net roles and differential pairs, in board net names if given.

    Pass the board and every name comes back in the board's vocabulary, matched
    by pads rather than by name so the unnamed nets survive the translation.
    """
    roles = classify_rails(schematic)
    pairs = propose_differential_pairs(schematic, rails={r.net for r in roles})

    if board is None:
        return Intent(roles=roles, pairs=pairs, mapping=None)

    mapping = map_to_board(schematic, board)
    translated_roles = [
        NetRole(net=mapping.board_net(r.net) or r.net, role=r.role,
                evidence=r.evidence, confidence=r.confidence)
        for r in roles
    ]
    translated_pairs = []
    for pair in pairs:
        positive = mapping.board_net(pair.positive)
        negative = mapping.board_net(pair.negative)
        if positive is None or negative is None:
            continue
        translated_pairs.append(PairCandidate(
            positive=positive, negative=negative,
            evidence=pair.evidence, confidence=pair.confidence))
    return Intent(roles=translated_roles, pairs=translated_pairs, mapping=mapping)
