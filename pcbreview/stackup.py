"""Gate 1: the stackup.

Every impedance, crosstalk and inductance number this tool produces is a
function of the layer stack. KiCad boards routinely ship with a placeholder
stackup (10 um dielectrics, 0.19 mm total thickness) that is silently wrong
rather than obviously wrong, so computing from it would produce confident
nonsense.

This module therefore *refuses* rather than guesses. ``load_and_validate``
raises :class:`StackupGateError` unless it is handed a stackup that is both
internally consistent and consistent with the board it will be applied to.

The intended source is the JLCPCB impedance calculator, whose table can be
pasted straight in via :func:`parse_jlc_table`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

# Dielectric constants are not quoted by the JLCPCB stackup table, so we carry
# nominal values for the common glass styles keyed by resin content. These are
# nominal @1 GHz and carry roughly +/-0.15 of spread between laminate vendors;
# `epsilon_r_source` records whether a value was stated or defaulted, and
# calibration against the fab's own impedance targets is preferred over both.
GLASS_STYLE_EPSILON_R = {
    "7628": 4.6,   # glass-rich, lowest resin -> highest Dk
    "2116": 4.35,
    "3313": 4.25,
    "1080": 4.1,
    "106": 4.0,    # resin-rich, lowest Dk
}
DEFAULT_CORE_EPSILON_R = 4.5
DEFAULT_LOSS_TANGENT = 0.02

# Below this a dielectric layer is a placeholder, not a real laminate. KiCad's
# own default is exactly 0.01 mm, which is what makes this check worth having.
MIN_PLAUSIBLE_DIELECTRIC_MM = 0.02
MIN_PLAUSIBLE_BOARD_MM = 0.3
EPSILON_R_RANGE = (2.0, 6.0)

COPPER_ROLES = {"copper"}
DIELECTRIC_ROLES = {"prepreg", "core"}


class StackupGateError(Exception):
    """Raised when the stackup is missing, malformed, or implausible."""


@dataclass
class Problem:
    severity: str  # "error" blocks; "warning" is reported and allowed
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.code}: {self.message}"


@dataclass
class StackupLayer:
    role: str                      # copper | prepreg | core
    name: str                      # "L1" / "F.Cu" for copper, material for dielectric
    thickness_mm: float
    material: str | None = None
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    copper_weight_oz: float | None = None
    epsilon_r_source: str = "unset"  # stated | glass_style_default | core_default

    @property
    def is_copper(self) -> bool:
        return self.role in COPPER_ROLES


@dataclass
class ImpedanceTarget:
    """A row from the fab's impedance calculator.

    These are the fab's own solver output for a stated geometry, which makes
    them the best available validation and calibration data for ours.
    """
    impedance_ohm: float
    kind: str                      # single_ended | differential
    signal_layer: str
    trace_width_mm: float
    trace_to_copper_mm: float | None = None
    trace_spacing_mm: float | None = None
    top_ref: str | None = None
    bottom_ref: str | None = None
    #: When this target was **derived** rather than transcribed, the layer of
    #: the stated target it was mirrored from. `None` means the fab said it.
    #: Nothing that *validates* a transcription may look at a derived target —
    #: solving one against our own solver is circular and always passes.
    derived_from: str | None = None

    @property
    def is_coplanar(self) -> bool:
        return self.trace_to_copper_mm is not None

    @property
    def is_stated(self) -> bool:
        """Did the fab write this row, or did we mirror it?"""
        return self.derived_from is None


@dataclass
class Stackup:
    layers: list[StackupLayer]
    name: str | None = None
    fab_code: str | None = None
    finished_thickness_mm: float | None = None
    impedance_targets: list[ImpedanceTarget] = field(default_factory=list)
    layer_map: dict[str, str] = field(default_factory=dict)  # "L1" -> "F.Cu"

    # -- derived geometry -------------------------------------------------

    @property
    def copper_layers(self) -> list[StackupLayer]:
        return [l for l in self.layers if l.is_copper]

    @property
    def total_thickness_mm(self) -> float:
        return sum(l.thickness_mm for l in self.layers)

    def copper_z_positions(self) -> dict[str, tuple[float, float]]:
        """Map copper layer name -> (z_top, z_bottom) in mm from board top.

        This is what reference-plane resolution walks: for a trace on layer X,
        find the nearest copper layer above and below and the dielectric
        between them.
        """
        positions: dict[str, tuple[float, float]] = {}
        z = 0.0
        for layer in self.layers:
            if layer.is_copper:
                positions[layer.name] = (z, z + layer.thickness_mm)
            z += layer.thickness_mm
        return positions

    def dielectric_between(self, layer_a: str, layer_b: str) -> tuple[float, float]:
        """Dielectric (thickness_mm, effective epsilon_r) between two copper layers.

        Where several dielectric plies sit between the layers, epsilon_r is
        combined as series capacitors — the correct average for a field
        crossing them in sequence, and the one that matters for impedance.
        """
        names = [l.name for l in self.layers]
        try:
            index_a, index_b = names.index(layer_a), names.index(layer_b)
        except ValueError as exc:
            raise KeyError(f"unknown copper layer: {exc}") from exc
        lo, hi = sorted((index_a, index_b))

        plies = [l for l in self.layers[lo + 1:hi] if l.role in DIELECTRIC_ROLES]
        thickness = sum(p.thickness_mm for p in plies)
        if thickness <= 0:
            return 0.0, 1.0
        # Series capacitance: h_total / eps_eff = sum(h_i / eps_i)
        reciprocal = sum(p.thickness_mm / (p.epsilon_r or DEFAULT_CORE_EPSILON_R)
                         for p in plies)
        return thickness, thickness / reciprocal

    def resolve_layer(self, label: str) -> str:
        """Translate a fab label ("L1") into a KiCad layer name ("F.Cu")."""
        return self.layer_map.get(label, label)

    # -- symmetry, and the targets it entitles us to ----------------------

    @property
    def is_symmetric(self) -> bool:
        """Is this stack the same read from the bottom as from the top?

        Every layer must mirror its counterpart in **role, thickness, epsilon
        and copper weight**. Thickness is compared to 1 um and epsilon to
        0.001, which is finer than any fab states either.

        Symmetry is the normal case and is a manufacturing requirement, not a
        coincidence: an asymmetric laminate warps on reflow. All three
        stackups in this repository's fixtures are exactly symmetric. It is
        still *tested* rather than assumed, because the one place it fails —
        an odd build, or a transcription that stopped halfway — is exactly
        where mirroring a target would invent a number.
        """
        layers = self.layers
        if len(layers) < 3:
            return False
        for near, far in zip(layers, reversed(layers)):
            if near.role != far.role:
                return False
            if abs(near.thickness_mm - far.thickness_mm) > 1e-3:
                return False
            if (near.epsilon_r or 0.0) - (far.epsilon_r or 0.0) > 1e-3:
                return False
            if (near.copper_weight_oz or 0.0) != (far.copper_weight_oz or 0.0):
                return False
        return True

    def mirror_layer(self, label: str) -> str | None:
        """The copper layer geometrically opposite `label`, or `None`.

        L1 <-> Ln, L2 <-> Ln-1, and so on, by position in the copper stack.
        """
        names = [l.name for l in self.copper_layers]
        if label not in names:
            return None
        return names[len(names) - 1 - names.index(label)]

    def mirrored_targets(self) -> list[ImpedanceTarget]:
        """The fab's targets, reflected onto the other half of a symmetric stack.

        **Why this is not inventing a number.** On a symmetric stackup the
        cross-section a trace on Ln sees referenced to Ln-1 is the *same
        geometry* as one on L1 referenced to L2 — same copper weight, same
        dielectric thickness, same epsilon, same mask. The impedance is
        therefore the fab's own stated number, not our model's: reflection is
        an exact symmetry of the section, and applying it costs no accuracy at
        all. Solved both ways on the six-layer stackup here, L1/L2 and L6/L5
        agree to 0.1%.

        **Why it is worth doing.** Fabs quote one row per *unique* geometry, so
        an outer-layer target is written for L1 and never repeated for Ln. A
        board whose RF feed is routed on the bottom layer then has no target
        matching it, and `impedance` reports `0 net(s) examined` — a blind
        check, on the one net the user cared most about. That is what happened
        on the six-layer board (§53.11).

        Returned **separately** from `impedance_targets` on purpose. These are
        derived, so anything that validates the *transcription* — `verify`,
        `physics.check_fab_targets`, `fields.check_fab_targets` — must not see
        them: solving a derived target against the solver that derived it
        always passes and proves nothing. Only the routed-impedance audit,
        which compares real copper against a target, consumes them.
        """
        if not self.is_symmetric:
            return []
        stated = {(t.signal_layer, t.kind, t.trace_width_mm)
                  for t in self.impedance_targets}
        mirrored: list[ImpedanceTarget] = []
        for target in self.impedance_targets:
            if not target.is_stated:
                continue
            signal = self.mirror_layer(target.signal_layer)
            if signal is None or signal == target.signal_layer:
                continue
            if (signal, target.kind, target.trace_width_mm) in stated:
                continue      # the fab wrote both rows out; nothing to add
            mirrored.append(replace(
                target,
                signal_layer=signal,
                top_ref=self.mirror_layer(target.top_ref) if target.top_ref
                else None,
                bottom_ref=self.mirror_layer(target.bottom_ref)
                if target.bottom_ref else None,
                derived_from=target.signal_layer))
        return mirrored


# -- JLCPCB table parsing -------------------------------------------------

_JLC_STACK_ROW = re.compile(
    r"^(?P<label>L\d+|Prepreg|Core)\s+(?P<material>.*?)\s+"
    r"(?P<mil>[\d.]+)\s+(?P<mm>[\d.]+)\s*$",
    re.IGNORECASE,
)
_JLC_CODE = re.compile(r"(JLC\d{2}\d{3}H?-[\w]+)", re.IGNORECASE)
_JLC_THICKNESS = re.compile(r"[Ff]inished\s+thickness\s*([\d.]+)\s*mm", re.IGNORECASE)


def _epsilon_for_material(role: str, material: str) -> tuple[float, str]:
    if role == "prepreg":
        for style, value in GLASS_STYLE_EPSILON_R.items():
            if style in material:
                return value, "glass_style_default"
    return DEFAULT_CORE_EPSILON_R, "core_default"


def parse_jlc_table(text: str) -> Stackup:
    """Parse a pasted JLCPCB stackup table into a :class:`Stackup`.

    Accepts the layer table rows (``L1  Outer Copper Weight1oz  1.38  0.0350``)
    and, when present, the fab stackup code and finished thickness. Impedance
    target rows are not parsed here — they are entered explicitly, because the
    calculator lays them out too inconsistently to read reliably.
    """
    layers: list[StackupLayer] = []
    fab_code = None
    finished = None
    copper_index = 0

    rejected: tuple[str, int] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip().replace("\t", " ")
        line = re.sub(r"\s{2,}", "  ", line)
        if not line:
            continue

        if fab_code is None:
            match = _JLC_CODE.search(line)
            if match:
                fab_code = match.group(1)
        if finished is None:
            match = _JLC_THICKNESS.search(line)
            if match:
                finished = float(match.group(1))

        collapsed = re.sub(r"\s{2,}", " ", line)
        match = _JLC_STACK_ROW.match(collapsed)
        if not match:
            # Remember the closest near-miss so the error can name the cause
            # rather than show a sample and leave the reader to diff it. The
            # commonest failure by far is dropping a column the reader judged
            # redundant — the row needs at least 4 whitespace-separated fields
            # and field 3 is *ignored*, which is exactly the shape someone
            # tidies away. That cost a session its first command.
            fields = collapsed.split(" ")
            if len(fields) >= 2 and not rejected:
                rejected = (raw_line.strip(), len(fields))
            continue

        label = match.group("label")
        material = match.group("material").strip()
        thickness = float(match.group("mm"))

        if label.upper().startswith("L"):
            copper_index += 1
            weight = None
            weight_match = re.search(r"([\d.]+)\s*oz", material, re.IGNORECASE)
            if weight_match:
                weight = float(weight_match.group(1))
            elif "inner" in material.lower():
                weight = 0.5
            layers.append(StackupLayer(
                role="copper", name=label.upper(), thickness_mm=thickness,
                material=material, copper_weight_oz=weight))
        else:
            role = label.lower()
            epsilon, source = _epsilon_for_material(role, material)
            layers.append(StackupLayer(
                role=role, name=f"{role}{len([l for l in layers if not l.is_copper]) + 1}",
                thickness_mm=thickness, material=material,
                epsilon_r=epsilon, loss_tangent=DEFAULT_LOSS_TANGENT,
                epsilon_r_source=source))

    if not layers:
        message = ["no stackup rows recognised"]
        if rejected:
            sample, count = rejected
            message.append(
                f"  {count} whitespace-separated field(s) found on "
                f"'{sample[:60]}'; at least 4 are required")
            if count == 3:
                message.append(
                    "  Do NOT drop columns. Field 3 is ignored by the parser "
                    "but must be present — a 3-column table looks tidier and "
                    "does not parse.")
        message.append(
            "  copy the calculator's table verbatim, its own column order, "
            "header row included, e.g.")
        message.append("    L1  Outer Copper Weight1oz  1.38  0.0350")
        message.append("    (Layer / Material / anything / Thickness(mm))")
        raise StackupGateError("\n".join(message))

    return Stackup(layers=layers, fab_code=fab_code,
                   finished_thickness_mm=finished,
                   layer_map=default_layer_map(copper_index))


def default_layer_map(copper_count: int) -> dict[str, str]:
    """Conventional L1..Ln -> KiCad copper layer names for an n-layer board."""
    if copper_count < 2:
        return {}
    names = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, copper_count - 1)] + ["B.Cu"]
    return {f"L{i + 1}": name for i, name in enumerate(names)}


# -- stackup as recorded in the board file --------------------------------

def _extract_block(text: str, opener: str) -> str | None:
    """Pull one balanced S-expression block out of a board file by its opener."""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


_SEXP_LAYER = re.compile(r'\(layer\s+"([^"]+)"(.*?)(?=\(layer\s+"|\Z)', re.DOTALL)
_SEXP_FIELD = {
    "type": re.compile(r'\(type\s+"([^"]*)"\)'),
    "material": re.compile(r'\(material\s+"([^"]*)"\)'),
    "thickness": re.compile(r"\(thickness\s+([\d.]+)\)"),
    "epsilon_r": re.compile(r"\(epsilon_r\s+([\d.]+)\)"),
    "loss_tangent": re.compile(r"\(loss_tangent\s+([\d.]+)\)"),
}

_BOARD_TYPE_TO_ROLE = {"copper": "copper", "prepreg": "prepreg", "core": "core"}


def from_kicad_file(path: str | Path) -> Stackup:
    """Read the stackup recorded inside a .kicad_pcb file.

    This is for *comparison only*. KiCad boards are routinely saved with the
    default placeholder stackup, so this result is expected to fail
    :func:`validate` — that failure is the diagnostic.
    """
    text = Path(path).read_text(errors="replace")
    block = _extract_block(text, "(stackup")
    if block is None:
        raise StackupGateError(f"{path} contains no stackup block")

    layers: list[StackupLayer] = []
    for name, body in _SEXP_LAYER.findall(block):
        fields = {key: pattern.search(body) for key, pattern in _SEXP_FIELD.items()}
        layer_type = fields["type"].group(1).lower() if fields["type"] else ""
        role = next((r for k, r in _BOARD_TYPE_TO_ROLE.items() if k in layer_type), None)
        if role is None:
            continue  # silkscreen, mask, paste — no electrical role here
        layers.append(StackupLayer(
            role=role,
            name=name,
            thickness_mm=float(fields["thickness"].group(1)) if fields["thickness"] else 0.0,
            material=fields["material"].group(1) if fields["material"] else None,
            epsilon_r=float(fields["epsilon_r"].group(1)) if fields["epsilon_r"] else None,
            loss_tangent=(float(fields["loss_tangent"].group(1))
                          if fields["loss_tangent"] else None),
            epsilon_r_source="stated" if fields["epsilon_r"] else "unset",
        ))

    thickness_block = _extract_block(text, "(general")
    finished = None
    if thickness_block:
        match = re.search(r"\(thickness\s+([\d.]+)\)", thickness_block)
        if match:
            finished = float(match.group(1))

    return Stackup(layers=layers, name=f"from board file: {Path(path).name}",
                   finished_thickness_mm=finished,
                   layer_map={l.name: l.name for l in layers if l.is_copper})


# -- YAML load/save -------------------------------------------------------

def _layer_from_dict(data: dict) -> StackupLayer:
    role = data["role"].lower()
    epsilon = data.get("epsilon_r")
    source = "stated" if epsilon is not None else "unset"
    if epsilon is None and role in DIELECTRIC_ROLES:
        epsilon, source = _epsilon_for_material(role, data.get("material", ""))
    return StackupLayer(
        role=role,
        name=data["name"],
        thickness_mm=float(data["thickness_mm"]),
        material=data.get("material"),
        epsilon_r=epsilon,
        loss_tangent=data.get("loss_tangent",
                              DEFAULT_LOSS_TANGENT if role in DIELECTRIC_ROLES else None),
        copper_weight_oz=data.get("copper_weight_oz"),
        epsilon_r_source=data.get("epsilon_r_source", source),
    )


def load_stackup(path: str | Path) -> Stackup:
    path = Path(path)
    if not path.exists():
        raise StackupGateError(f"stackup file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "layers" not in data:
        raise StackupGateError(f"{path} is not a stackup document (needs 'layers')")

    layers = [_layer_from_dict(entry) for entry in data["layers"]]
    targets = [
        ImpedanceTarget(
            impedance_ohm=float(t["impedance_ohm"]),
            kind=t.get("kind", "single_ended"),
            signal_layer=t["signal_layer"],
            trace_width_mm=float(t["trace_width_mm"]),
            trace_to_copper_mm=t.get("trace_to_copper_mm"),
            trace_spacing_mm=t.get("trace_spacing_mm"),
            top_ref=t.get("top_ref"),
            bottom_ref=t.get("bottom_ref"),
        )
        for t in data.get("impedance_targets", [])
    ]
    copper_count = sum(1 for l in layers if l.is_copper)
    return Stackup(
        layers=layers,
        name=data.get("name"),
        fab_code=data.get("fab_code"),
        finished_thickness_mm=data.get("finished_thickness_mm"),
        impedance_targets=targets,
        layer_map=data.get("layer_map") or default_layer_map(copper_count),
    )


def dump_stackup(stackup: Stackup) -> str:
    document = {
        "name": stackup.name,
        "fab_code": stackup.fab_code,
        "finished_thickness_mm": stackup.finished_thickness_mm,
        "layer_map": stackup.layer_map,
        "layers": [
            {k: v for k, v in {
                "role": l.role,
                "name": l.name,
                "material": l.material,
                "thickness_mm": l.thickness_mm,
                "epsilon_r": l.epsilon_r,
                "loss_tangent": l.loss_tangent,
                "copper_weight_oz": l.copper_weight_oz,
                "epsilon_r_source": l.epsilon_r_source,
            }.items() if v is not None}
            for l in stackup.layers
        ],
        "impedance_targets": [
            {k: v for k, v in {
                "impedance_ohm": t.impedance_ohm,
                "kind": t.kind,
                "signal_layer": t.signal_layer,
                "top_ref": t.top_ref,
                "bottom_ref": t.bottom_ref,
                "trace_width_mm": t.trace_width_mm,
                "trace_spacing_mm": t.trace_spacing_mm,
                "trace_to_copper_mm": t.trace_to_copper_mm,
            }.items() if v is not None}
            for t in stackup.impedance_targets
        ],
    }
    return yaml.safe_dump({k: v for k, v in document.items() if v is not None},
                          sort_keys=False, default_flow_style=False)


# -- validation -----------------------------------------------------------

def validate(stackup: Stackup, board_copper_layers: list[str] | None = None
             ) -> list[Problem]:
    """Check a stackup for internal consistency and fitness for the board.

    Returns every problem found rather than raising on the first, so a user
    fixing their stackup file sees the whole list at once.
    """
    problems: list[Problem] = []
    copper = stackup.copper_layers

    if len(copper) < 2:
        problems.append(Problem(
            "error", "STK001",
            f"stackup has {len(copper)} copper layers; need at least 2"))

    for layer in stackup.layers:
        if layer.thickness_mm <= 0:
            problems.append(Problem(
                "error", "STK002",
                f"layer '{layer.name}' has non-positive thickness "
                f"{layer.thickness_mm} mm"))

        if layer.role in DIELECTRIC_ROLES:
            if layer.thickness_mm < MIN_PLAUSIBLE_DIELECTRIC_MM:
                problems.append(Problem(
                    "error", "STK003",
                    f"dielectric '{layer.name}' is {layer.thickness_mm} mm — below "
                    f"{MIN_PLAUSIBLE_DIELECTRIC_MM} mm this is a placeholder, not a "
                    f"laminate (KiCad's default is 0.01 mm)"))
            epsilon = layer.epsilon_r
            if epsilon is None:
                problems.append(Problem(
                    "error", "STK004",
                    f"dielectric '{layer.name}' has no epsilon_r"))
            elif not EPSILON_R_RANGE[0] <= epsilon <= EPSILON_R_RANGE[1]:
                problems.append(Problem(
                    "error", "STK005",
                    f"dielectric '{layer.name}' epsilon_r={epsilon} is outside the "
                    f"plausible range {EPSILON_R_RANGE}"))
            elif layer.epsilon_r_source != "stated":
                problems.append(Problem(
                    "warning", "STK006",
                    f"dielectric '{layer.name}' epsilon_r={epsilon} is a default for "
                    f"'{layer.material}', not a stated value — impedance results "
                    f"inherit roughly +/-3% from this"))

    total = stackup.total_thickness_mm
    if total < MIN_PLAUSIBLE_BOARD_MM:
        problems.append(Problem(
            "error", "STK007",
            f"total stackup thickness {total:.3f} mm is implausible for a "
            f"{len(copper)}-layer board"))

    if stackup.finished_thickness_mm:
        # Finished thickness excludes mask/plating, and fabs quote +/-10%.
        deviation = abs(total - stackup.finished_thickness_mm) / stackup.finished_thickness_mm
        if deviation > 0.15:
            problems.append(Problem(
                "error", "STK008",
                f"layer thicknesses sum to {total:.3f} mm but finished thickness is "
                f"{stackup.finished_thickness_mm} mm ({deviation:.0%} apart)"))

    if board_copper_layers is not None:
        if len(copper) != len(board_copper_layers):
            problems.append(Problem(
                "error", "STK009",
                f"stackup describes {len(copper)} copper layers but the board has "
                f"{len(board_copper_layers)}: {board_copper_layers}"))
        else:
            resolved = [stackup.resolve_layer(l.name) for l in copper]
            unknown = [r for r in resolved if r not in board_copper_layers]
            if unknown:
                problems.append(Problem(
                    "error", "STK010",
                    f"stackup layers {unknown} do not map to board copper layers "
                    f"{board_copper_layers}; fix 'layer_map'"))
            elif resolved != board_copper_layers:
                problems.append(Problem(
                    "error", "STK011",
                    f"stackup copper order {resolved} does not match board order "
                    f"{board_copper_layers}"))

    return problems


def compare_to_board(stackup: Stackup, board_stackup: Stackup,
                     tolerance: float = 0.05) -> list[Problem]:
    """Warn where the authoritative stackup diverges from the board's own.

    A board can carry a stackup that is perfectly *plausible* — and therefore
    passes :func:`validate` — while still not being the stack the fab will
    build. The six-layer board is exactly this case: 0.100 mm prepreg and 0.535 mm
    core in KiCad against JLC's 0.0994 mm and 0.550 mm.

    The divergence usually does not matter, but the designer should know it
    exists, because KiCad's own length-tuning and impedance hints are computed
    from the board's copy, not ours.
    """
    problems: list[Problem] = []

    ours = stackup.copper_layers
    theirs = board_stackup.copper_layers
    if len(ours) != len(theirs):
        problems.append(Problem(
            "warning", "STK012",
            f"board file declares {len(theirs)} copper layers, supplied stackup "
            f"has {len(ours)}; skipping per-layer comparison"))
        return problems

    for index in range(len(ours) - 1):
        our_h, _ = stackup.dielectric_between(ours[index].name, ours[index + 1].name)
        their_h, _ = board_stackup.dielectric_between(
            theirs[index].name, theirs[index + 1].name)
        if our_h <= 0 or their_h <= 0:
            continue
        deviation = abs(our_h - their_h) / our_h
        if deviation > tolerance:
            problems.append(Problem(
                "warning", "STK013",
                f"dielectric {ours[index].name}->{ours[index + 1].name} is "
                f"{our_h:.4f} mm in the supplied stackup but {their_h:.4f} mm in the "
                f"board file ({deviation:.0%} apart); KiCad's own impedance hints use "
                f"the board's value"))

    for index, (mine, theirs_layer) in enumerate(zip(ours, theirs)):
        if mine.thickness_mm <= 0 or theirs_layer.thickness_mm <= 0:
            continue
        deviation = abs(mine.thickness_mm - theirs_layer.thickness_mm) / mine.thickness_mm
        if deviation > tolerance:
            problems.append(Problem(
                "warning", "STK014",
                f"copper {mine.name} is {mine.thickness_mm:.4f} mm in the supplied "
                f"stackup but {theirs_layer.thickness_mm:.4f} mm in the board file "
                f"({deviation:.0%} apart)"))

    return problems


def load_and_validate(path: str | Path,
                      board_copper_layers: list[str] | None = None
                      ) -> tuple[Stackup, list[Problem]]:
    """Load a stackup and refuse to return it unless it is usable.

    Raises :class:`StackupGateError` listing every blocking problem. Warnings
    are returned alongside the stackup so callers can surface them in reports.
    """
    stackup = load_stackup(path)
    problems = validate(stackup, board_copper_layers)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        raise StackupGateError(
            "stackup rejected — physics results would be meaningless:\n  "
            + "\n  ".join(str(p) for p in errors))
    return stackup, [p for p in problems if p.severity == "warning"]
