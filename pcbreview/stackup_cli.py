"""Getting a usable stackup, and proving it is usable.

Gate 1 refuses to run without a validated stackup, correctly, and that means a
new user hits a wall in the first minute. This is the way through it.

    python3 -m pcbreview.stackup_cli init board.kicad_pcb      # what is needed
    <screenshot the JLCPCB impedance calculator>
    python3 -m pcbreview.stackup_cli transcribe board.kicad_pcb rows.txt \\
        --target ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.1494,\\
                 spacing=0.2032,to_copper=0.2032 > my.stackup.yaml
    python3 -m pcbreview.stackup_cli verify board.kicad_pcb my.stackup.yaml

There is no `pcbreview` console script and there is not going to be: a human
never types any of this. Claude does, on the user's behalf.

The division of labour, which is the project's usual one
--------------------------------------------------------
**Claude transcribes text; deterministic code does the physics.**
`stackup.parse_jlc_table` takes the calculator's own layer rows verbatim and
derives ε_r from the glass style in the material string — 3313 → 4.25,
2116 → 4.35, 7628 → 4.6. Having an LLM write ε_r into YAML by hand would put a
physical constant in its gift, which is the one thing this project exists not
to do.

`transcribe` is what makes that reachable from a command. Until it existed the
only route to `parse_jlc_table` was to write a script, which is what §40 had to
do — the single step in the whole flow where Claude wrote code rather than ran
a command, and the one place a hand-authored ε_r could have crept in unnoticed.

Impedance targets are given explicitly with `--target` rather than parsed out
of the screenshot, because the calculator lays that table out inconsistently:
its result row and its configure row can describe *different* calculations,
which is what made four of the user's screenshots unusable in §40a.

`to_copper` is required, and `to_copper=none` is how a "Non coplanar" row is
written. `ImpedanceTarget.is_coplanar` is nothing but "did this field have a
value", so an omitted key silently decides a real physical difference — and
that is exactly how the six-layer board's 90 Ω target came to be stored as coplanar at a width
*wider* than the non-coplanar answer for the same stackup, which is backwards.

Why the transcription is verified rather than trusted
-----------------------------------------------------
A misread 0.1088 → 0.1008 silently biases every impedance and coupling result
and nothing downstream notices. But the fab's impedance targets are *redundant
information that constrains the dielectric numbers*, and §12 of the handoff
established that the field solver reproduces all four of JLCPCB's published
targets at −1.4% to −1.7%, consistently across two boards and two stackups.

So `verify` solves them. Land near that residual and the transcription is
right; land 8% out and a number was misread. This turns "trust the OCR" into
"check it against physics", using machinery that already exists.

Measured, by corrupting the six-layer board's transcription one number at a time:

| what was changed | what the targets read |
|---|---|
| nothing | −1.4%, −1.4% — ok |
| prepreg 0.0994 → 0.0904 (a digit transposed) | −6.6%, −5.4% — suspect |
| prepreg 0.0994 → 0.1994 (a decimal misread) | +39.7%, +22.3% — wrong |
| ε_r 4.25 → 3.3 (wrong glass style) | +8.8%, +7.9% — wrong |
| L1 copper 0.035 → 0.0152 (oz misread) | +3.1%, +5.2% — suspect |

Every mode is caught, including a 9% thickness slip.

**The honest limit:** a target only constrains the layers it *references*,
which is usually L1↔L2. A misread inner core thickness is caught by the
thickness sum and not by the impedance. Copper weight on a referenced layer is
constrained, if weakly — the 0.5 oz/1 oz confusion above moves the targets
3–5%. On a layer no target references, nothing here sees it at all. `verify`
prints which layers were constrained and which were not.

Why the board's own stackup is not a source of physics
------------------------------------------------------
KiCad 7+ has a Board Setup → Physical Stackup and it is tempting. Both boards
here have one and **both are unusable**: the six-layer board says 0.035 mm inner copper where
JLC ships 0.0152 (out by 2.3x) and 4.5 for every ε_r; the four-layer board says 0.01 mm
for every dielectric, an untouched default on a board that is really 1.59 mm.

It is still worth reading, for two reasons: the layer map and count are exact,
and the disagreement is a review finding in its own right — see
`audit.check_stackup_mismatch`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from pcbreview.stackup import (DEFAULT_LOSS_TANGENT, ImpedanceTarget, Stackup,
                               StackupLayer, StackupGateError, compare_to_board,
                               default_layer_map, dump_stackup, from_kicad_file,
                               load_stackup, parse_jlc_table, validate)

#: The residual §12 established for this solver against JLCPCB's own numbers,
#: consistently over two boards, two stackups and both target kinds. It is
#: almost certainly unmodelled solder-mask thickness, which the fab does not
#: publish per order.
EXPECTED_RESIDUAL = -0.015

#: A target landing within this of the fab's number confirms the transcription.
TARGET_TOLERANCE = 0.03
#: Beyond this, a dielectric height or an ε_r was almost certainly misread.
TARGET_SUSPECT = 0.08


@dataclass
class TargetVerdict:
    """One fab impedance target, solved and judged."""
    target_ohm: float
    modelled_ohm: float
    kind: str
    signal_layer: str
    reference_layer: str
    error: float
    verdict: str            # ok | suspect | wrong
    #: single-field perturbations that would reconcile this target, filled in
    #: only when the verdict is not `ok` — see `diagnose`
    consistent_with: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"[{self.verdict.upper():7s}] {self.kind} "
                f"{self.target_ohm:.0f} ohm on {self.signal_layer} "
                f"(ref {self.reference_layer}): solver "
                f"{self.modelled_ohm:.2f} ohm, {self.error:+.1%}")


#: How far a single field is allowed to be searched before the answer stops
#: being "you misread a digit" and starts being "you read the wrong row".
_SEARCH_SPAN = (0.05, 8.0)


def _reconciles(stackup, target, refinement: float) -> float | None:
    """The signed error of a perturbed target, or None if it cannot be solved."""
    from pcbreview.fields import solve_target
    try:
        modelled, _ = solve_target(stackup, target, refinement=refinement)
    except (ValueError, ZeroDivisionError):
        return None
    return modelled / target.impedance_ohm - 1.0


def _bisect_field(stackup, target, field_name: str, refinement: float
                  ) -> float | None:
    """The value of one field that would land the target on its stated ohms.

    Every field here moves impedance monotonically — wider trace down, wider
    gap to the pour up, wider pair spacing up — so a bisection is exact and
    needs no derivative. Roughly ten solves, about a second.
    """
    current = getattr(target, field_name)
    if current is None or current <= 0:
        return None

    low, high = current * _SEARCH_SPAN[0], current * _SEARCH_SPAN[1]
    error_low = _reconciles(stackup, replace(target, **{field_name: low}),
                            refinement)
    error_high = _reconciles(stackup, replace(target, **{field_name: high}),
                             refinement)
    if error_low is None or error_high is None:
        return None
    if (error_low > 0) == (error_high > 0):
        return None                       # no crossing inside the search span

    for _ in range(18):
        middle = (low + high) / 2
        error = _reconciles(stackup, replace(target, **{field_name: middle}),
                            refinement)
        if error is None:
            return None
        if abs(error - EXPECTED_RESIDUAL) < 0.002:
            return middle
        if (error > 0) == (error_high > 0):
            high, error_high = middle, error
        else:
            low, error_low = middle, error
    return (low + high) / 2


def diagnose(stackup: Stackup, target: ImpedanceTarget,
             refinement: float = 1.5) -> list[str]:
    """What single misread field would reconcile a failing target.

    The gate catching a bad transcription is the point of `verify` and it
    works. What it could not do was say *which* field was wrong, and that cost
    an outside session a round trip: hand-sweeping the solver, they found two
    different single-field corrections that both landed inside ±3% and neither
    was the real error, which was `kind` (§48.3 item 4).

    So `kind` and `is_coplanar` are perturbed here as well as the numbers — a
    misread of either is invisible to any width or gap correction, and on a
    thick-core two-layer board "is it coplanar" moves the answer far more than
    trace width does (105 -> 60 -> 51 ohm as the gap goes none -> 0.2 -> 0.13).

    Returned as prose, ordered widest-moving first. **These are candidates to
    re-read on the screenshot, not corrections to apply** — the point is that
    more than one usually reconciles, which is itself the finding.
    """
    lines: list[str] = []
    # Things that cannot be *solved* but are still worth re-reading. Kept
    # apart from `lines` so one of them can never stand in for a
    # reconciliation and suppress the dielectric fallback below.
    notes: list[str] = []

    # The two categorical fields first: no numeric perturbation reaches them.
    flipped_kind = "differential" if target.kind == "single_ended" else "single_ended"
    candidate = replace(target, kind=flipped_kind)
    if flipped_kind == "differential" and candidate.trace_spacing_mm is None:
        notes.append(
            f"kind={flipped_kind} cannot be solved from what was transcribed "
            f"(no trace_spacing_mm) — if the calculator row said "
            f"'Differential Pair', the spacing is missing too, so re-read "
            f"that whole row rather than one field of it")
    else:
        error = _reconciles(stackup, candidate, refinement)
        if error is not None and abs(error) <= TARGET_TOLERANCE:
            lines.append(f"kind={flipped_kind} ({error:+.1%})")

    if target.is_coplanar:
        error = _reconciles(stackup, replace(target, trace_to_copper_mm=None),
                            refinement)
        if error is not None and abs(error) <= TARGET_TOLERANCE:
            lines.append(f"to_copper=none, i.e. a 'Non coplanar' row "
                         f"({error:+.1%})")
    else:
        # No gap to bisect from, so try the pour clearances a fab actually
        # offers rather than searching an unbounded range.
        for gap in (0.0762, 0.127, 0.2032, 0.254, 0.508):
            error = _reconciles(stackup, replace(target, trace_to_copper_mm=gap),
                                refinement)
            if error is not None and abs(error) <= TARGET_TOLERANCE:
                lines.append(f"to_copper={gap} — the row is coplanar after all "
                             f"({error:+.1%})")
                break

    for field_name, label in (("trace_width_mm", "trace_width"),
                              ("trace_spacing_mm", "trace_spacing"),
                              ("trace_to_copper_mm", "to_copper")):
        value = _bisect_field(stackup, target, field_name, refinement)
        if value is None:
            continue
        if abs(value - getattr(target, field_name)) < 1e-4:
            continue
        lines.append(f"{label}={value:.4f} instead of "
                     f"{getattr(target, field_name):.4f}")

    if not lines:
        return ["no single field reconciles this within the search range — "
                "the dielectric heights or the glass style are the next thing "
                "to re-read, since those move every target on the layer"] + notes
    return lines + notes


@dataclass
class VerifyResult:
    stackup_name: str
    problems: list                       # stackup.Problem
    targets: list[TargetVerdict]
    board_comparison: list               # stackup.Problem
    constrained_layers: list[str]
    unconstrained_layers: list[str]

    @property
    def ok(self) -> bool:
        blocking = any(p.severity == "error" for p in self.problems)
        return not blocking and all(t.verdict == "ok" for t in self.targets)

    def report(self) -> str:
        lines = [f"stackup: {self.stackup_name}", ""]

        lines.append("1. layer thicknesses against the finished thickness")
        errors = [p for p in self.problems if p.severity == "error"]
        warnings = [p for p in self.problems if p.severity == "warning"]
        if errors:
            lines += [f"   {p}" for p in errors]
        else:
            lines.append("   [OK     ] internally consistent and fits the board")
        lines += [f"   {p}" for p in warnings]
        lines.append("")

        lines.append("2. the fab's own impedance targets, through the field solver")
        if not self.targets:
            lines.append("   [MISSING] no impedance targets in the stackup — "
                         "this is the check that catches a misread dielectric, "
                         "so transcribe them")
        else:
            for target in self.targets:
                lines.append(f"   {target}")
                if target.consistent_with:
                    lines.append("      this residual is consistent with: "
                                 + "; or ".join(target.consistent_with))
                    lines.append("      -- re-read those fields on the "
                                 "screenshot. More than one usually "
                                 "reconciles, so do not just apply the first")
            lines.append(f"   expected residual is about "
                         f"{EXPECTED_RESIDUAL:+.1%}; this solver reads low "
                         f"against JLCPCB consistently, most likely unmodelled "
                         f"solder-mask thickness")
            lines.append(f"   these targets constrain "
                         f"{', '.join(self.constrained_layers) or 'nothing'}")
            if self.unconstrained_layers:
                lines.append(
                    f"   they say nothing about "
                    f"{', '.join(self.unconstrained_layers)} — only the "
                    f"thickness sum covers those")
        lines.append("")

        lines.append("3. the stackup recorded inside the board file")
        if not self.board_comparison:
            lines.append("   [OK     ] agrees with the fab's, or the board has "
                         "no stackup block")
        else:
            lines += [f"   {p}" for p in self.board_comparison]
            lines.append("   KiCad's own length-tuning and impedance hints use "
                         "the board's copy, not this one")
        lines.append("")
        lines.append("VERDICT: " + ("usable" if self.ok
                                    else "do not use until the above is resolved"))
        return "\n".join(lines)


def skeleton(copper_layers: list[str], finished_thickness_mm: float | None = None
             ) -> str:
    """A stackup file with everything the board knows filled in and the rest blank.

    The layer *map* and *count* are the two things the board file states
    reliably, and `bind_stackup` enforces agreement on them anyway. Every
    dielectric number is left at zero, so the file fails `validate` loudly until
    it is filled in — which is the point. A skeleton that passed the gate with
    plausible defaults would be worse than no skeleton at all.
    """
    count = len(copper_layers)
    layer_map = default_layer_map(count)
    if sorted(layer_map.values()) != sorted(copper_layers):
        # Non-standard layer naming: map positionally and say so.
        layer_map = {f"L{i + 1}": name for i, name in enumerate(copper_layers)}

    layers: list[StackupLayer] = []
    for index, label in enumerate(layer_map):
        outer = index in (0, count - 1)
        layers.append(StackupLayer(
            role="copper", name=label, thickness_mm=0.0,
            material="FILL IN — e.g. 'Outer Copper Weight1oz'" if outer
            else "FILL IN — e.g. 'Inner Copper Weight'",
            copper_weight_oz=None))
        if index < count - 1:
            layers.append(StackupLayer(
                role="prepreg" if outer else "core",
                name=f"dielectric{index + 1}", thickness_mm=0.0,
                material="FILL IN — the glass style matters, e.g. '3313 RC57%'",
                epsilon_r=None, loss_tangent=DEFAULT_LOSS_TANGENT,
                epsilon_r_source="unset"))

    document = dump_stackup(Stackup(
        layers=layers, name="FILL IN", fab_code=None,
        finished_thickness_mm=finished_thickness_mm, layer_map=layer_map))

    return f"""\
# pcbreview stackup — SKELETON, not usable yet.
#
# The layer map and the layer count came from the board and are correct.
# Everything else is zero and must be filled in from your fab's stackup.
#
# For JLCPCB: open the impedance calculator for your board's stackup code,
# screenshot the layer table, and have Claude transcribe the rows. It parses
# them with `stackup.parse_jlc_table`, which derives epsilon_r from the glass
# style in the material string rather than guessing.
#
# TRANSCRIBE THE IMPEDANCE TARGETS TOO. They are redundant information that
# constrains the dielectric numbers, and `stackup_cli verify` solves them to
# check the transcription against physics instead of trusting it. Without them
# a misread thickness is silent.
#
# Then:  python -m pcbreview.stackup_cli verify <board.kicad_pcb> <this file>
#
# Do NOT copy the numbers out of KiCad's Board Setup. On both boards this tool
# was developed against, that stackup is wrong by up to 2.3x — see
# `audit.check_stackup_mismatch`.

{document}
# impedance_targets:
# - impedance_ohm: 50
#   kind: single_ended
#   signal_layer: L1
#   bottom_ref: L2
#   trace_width_mm: 0.0        # FILL IN from the calculator
#   trace_to_copper_mm: 0.0    # the coplanar pour clearance it assumes
# - impedance_ohm: 90
#   kind: differential
#   signal_layer: L1
#   bottom_ref: L2
#   trace_width_mm: 0.0
#   trace_spacing_mm: 0.0
#   trace_to_copper_mm: 0.0
"""


def verify(stackup: Stackup, copper_layers: list[str] | None = None,
           board_path: str | Path | None = None,
           refinement: float = 0.7) -> VerifyResult:
    """Three independent checks on one stackup.

    1. Layer thicknesses sum to the finished thickness the fab quotes, and the
       layer count and order match the board. Already in `validate`.
    2. The field solver reproduces each impedance target. **This is the one
       that catches a misread dielectric height**, and nothing called it at
       load time before.
    3. The stackup recorded in the board file, reported where it disagrees.
    """
    # Imported here: the solver pulls in numpy and scipy, and `init` should
    # work without them.
    from pcbreview.fields import check_fab_targets

    problems = validate(stackup, copper_layers)

    targets: list[TargetVerdict] = []
    constrained: set[str] = set()
    if not any(p.severity == "error" for p in problems):
        for check in check_fab_targets(stackup, refinement=refinement):
            error = check.error
            if abs(error) <= TARGET_TOLERANCE:
                verdict = "ok"
            elif abs(error) <= TARGET_SUSPECT:
                verdict = "suspect"
            else:
                verdict = "wrong"
            source = next((t for t in stackup.impedance_targets
                            if t.impedance_ohm == check.target_ohm
                            and t.kind == check.kind
                            and t.signal_layer == check.signal_layer), None)
            targets.append(TargetVerdict(
                target_ohm=check.target_ohm, modelled_ohm=check.modelled_ohm,
                kind=check.kind, signal_layer=check.signal_layer,
                reference_layer=check.reference_layer, error=error,
                verdict=verdict,
                consistent_with=(diagnose(stackup, source)
                                 if verdict != "ok" and source is not None
                                 else [])))
            constrained.add(check.signal_layer)
            constrained.add(check.reference_layer)

    board_comparison: list = []
    if board_path is not None:
        try:
            board_comparison = compare_to_board(stackup,
                                                from_kicad_file(board_path))
        except StackupGateError:
            board_comparison = []

    every = [l.name for l in stackup.copper_layers]
    return VerifyResult(
        stackup_name=stackup.name or str(stackup.fab_code or "stackup"),
        problems=problems, targets=targets, board_comparison=board_comparison,
        constrained_layers=[l for l in every if l in constrained],
        unconstrained_layers=[l for l in every if l not in constrained])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _copper_layers(board_path: str | Path) -> tuple[list[str], float | None]:
    from pcbreview.model import Board

    board = Board.from_kicad(board_path)
    return board.copper_layers, board.thickness_mm


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

_KIND_ALIASES = {
    "single_ended": "single_ended", "single": "single_ended",
    "se": "single_ended", "singleended": "single_ended",
    "differential": "differential", "diff": "differential",
    "pair": "differential", "differentialpair": "differential",
}


class TranscribeError(Exception):
    """Raised when an impedance target cannot be read as written."""


def parse_target(spec: str) -> ImpedanceTarget:
    """One `--target` argument into an `ImpedanceTarget`.

        ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.1494,
        spacing=0.2032,to_copper=0.2032

    Targets are entered explicitly rather than parsed out of the screenshot
    because JLC's calculator lays that table out inconsistently — the result
    row and the configure row can describe different calculations, which is
    exactly what made four of the user's screenshots unusable in §40a.

    **`to_copper` is required, and `none` is how you say non-coplanar.**
    Omitting it would be the natural way to mean "no coplanar ground", and
    that is precisely the mistake already in the repo: the six-layer board's 90 ohm target is
    stored as coplanar at a width that is *wider* than the non-coplanar answer
    for the same stackup, which is backwards (§40a). `is_coplanar` is nothing
    but "did this field have a value", so it is a silent, physical difference
    decided by an absent key. Making it explicit costs four characters.
    """
    fields: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise TranscribeError(
                f"target field {part!r} is not key=value, in {spec!r}")
        key, value = part.split("=", 1)
        fields[key.strip().lower()] = value.strip()

    def need(key: str) -> str:
        if key not in fields or fields[key] == "":
            raise TranscribeError(
                f"target is missing {key}= — every target needs "
                f"ohm, kind, layer, width and to_copper "
                f"(to_copper=none for a non-coplanar row). Got: {spec!r}")
        return fields[key]

    def number(key: str, value: str) -> float:
        try:
            return float(value)
        except ValueError:
            raise TranscribeError(
                f"{key}={value!r} is not a number, in {spec!r}") from None

    kind_raw = need("kind").lower().replace(" ", "").replace("-", "_")
    kind = _KIND_ALIASES.get(kind_raw)
    if kind is None:
        raise TranscribeError(
            f"kind={need('kind')!r} is not one of single_ended / differential")

    to_copper_raw = need("to_copper").lower()
    to_copper = (None if to_copper_raw in ("none", "no", "/", "-")
                 else number("to_copper", to_copper_raw))

    spacing = fields.get("spacing")
    spacing_mm = None if spacing in (None, "", "none", "/") else number(
        "spacing", spacing)
    if kind == "differential" and spacing_mm is None:
        raise TranscribeError(
            "a differential target needs spacing= — the pair gap is what "
            f"makes it differential. Got: {spec!r}")

    # 90 rather than 90.0 in the YAML: fab targets are whole ohms in practice
    # and the file is read by humans checking a transcription.
    ohm = number("ohm", need("ohm"))
    return ImpedanceTarget(
        impedance_ohm=int(ohm) if ohm == int(ohm) else ohm,
        kind=kind,
        signal_layer=need("layer"),
        trace_width_mm=number("width", need("width")),
        trace_to_copper_mm=to_copper,
        trace_spacing_mm=spacing_mm,
        top_ref=fields.get("top_ref") or None,
        bottom_ref=fields.get("bottom_ref") or None,
    )


def transcribe(rows: str, copper_layers: list[str],
               targets: list[str] | None = None,
               name: str | None = None) -> str:
    """Pasted calculator rows plus explicit targets into a stackup YAML.

    The point of this existing at all: `parse_jlc_table` derives epsilon_r from
    the glass style in the material string rather than from anyone's memory
    (§25 correction 1), and before this command there was no way to reach it
    from the command line. Getting §40's board in meant writing a throwaway
    script — the only step in the whole flow where Claude had to write code
    rather than run a command.
    """
    stackup = parse_jlc_table(rows)
    if name:
        stackup.name = name
    # L1..Ln onto the board's own copper names in stack order. Not
    # `default_layer_map` alone: that assumes the conventional F.Cu/In*.Cu
    # names, and the board is the authority on what its layers are called.
    # Sorted numerically — `sorted()` on strings puts L10 before L2.
    stackup.layer_map = {
        f"L{i + 1}": layer for i, layer in enumerate(copper_layers)}
    stackup.impedance_targets = [parse_target(t) for t in (targets or [])]

    found = [l.name for l in stackup.copper_layers]
    if len(found) != len(copper_layers):
        raise TranscribeError(
            f"the rows describe {len(found)} copper layers ({', '.join(found)}) "
            f"but the board has {len(copper_layers)} "
            f"({', '.join(copper_layers)}) — transcribe the whole table")
    return dump_stackup(stackup)


USAGE = """usage:
  python3 -m pcbreview.stackup_cli init       <board.kicad_pcb>
  python3 -m pcbreview.stackup_cli transcribe <board.kicad_pcb> [rows.txt]
                                              [--out FILE] [--name NAME]
                                              [--target SPEC ...]
  python3 -m pcbreview.stackup_cli verify     <board.kicad_pcb> <stackup.yaml>

rows.txt is the fab's layer table, copied verbatim -- one row per line, the
calculator's own column order, header row included and ignored. Tabs or runs
of spaces both work as separators. From JLCPCB's impedance calculator:

  Layer   Material                        Type        Thickness(mm)
  L1      Outer Copper Weight1oz          copper      0.035
          7628, RC 49%, 8.6 mil           prepreg     0.2104
  L2      Inner Copper Weight             copper      0.0152

Do not edit the material strings: epsilon_r is derived from the glass style in
them (7628 -> 4.6, 3313 -> 4.25, 2116 -> 4.35), which is the whole reason the
rows are transcribed rather than the numbers.

transcribe reads those rows from a file, or from stdin when no file is given,
and writes a stackup YAML to stdout. Nothing is written to disk unless you ask
for it:

  --out FILE   write the YAML to FILE instead of stdout and print its path.
  --out -      the default: stdout, so `transcribe ... > board.stackup.yaml`
               is equivalent.

  Whichever you use, `verify` needs that file -- it does not read stdin, and
  transcribe on its own leaves no file behind.

  --target ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.1494,
           spacing=0.2032,to_copper=0.2032

  ohm, kind, layer, width and to_copper are required; spacing is required for
  a differential pair. Use to_copper=none for a "Non coplanar" row -- that is
  a physical difference, not a missing field, and leaving it out is how a
  wrong target got into this repo once already.

  Repeat --target once per row in the calculator's result table. Transcribe
  the targets even if you think you do not need them: verify solves them to
  check the rest of the transcription against physics, and without them a
  misread thickness is silent.
"""


def _transcribe_argv(argv: list[str]) -> int:
    rows_path: str | None = None
    name: str | None = None
    out_path: str | None = None
    targets: list[str] = []
    index = 3
    while index < len(argv):
        argument = argv[index]
        if argument == "--out":
            if index + 1 >= len(argv):
                sys.stderr.write("--out needs a path\n")
                return 2
            out_path = argv[index + 1]
            index += 2
        elif argument == "--target":
            if index + 1 >= len(argv):
                sys.stderr.write("--target needs a value\n")
                return 2
            targets.append(argv[index + 1])
            index += 2
        elif argument == "--name":
            if index + 1 >= len(argv):
                sys.stderr.write("--name needs a value\n")
                return 2
            name = argv[index + 1]
            index += 2
        elif argument.startswith("--"):
            sys.stderr.write(f"unknown option {argument}\n{USAGE}")
            return 2
        elif rows_path is None:
            rows_path = argument
            index += 1
        else:
            sys.stderr.write(f"unexpected argument {argument}\n{USAGE}")
            return 2

    if rows_path in (None, "-"):
        if sys.stdin.isatty():
            sys.stderr.write(
                "transcribe: paste the calculator's layer rows on stdin, or "
                "give a file\n")
            return 2
        rows = sys.stdin.read()
    else:
        rows = Path(rows_path).read_text(encoding="utf-8")

    layers, _ = _copper_layers(argv[2])
    try:
        document = transcribe(rows, layers, targets=targets, name=name)
    except (TranscribeError, StackupGateError) as exc:
        sys.stderr.write(f"transcribe: {exc}\n")
        return 2

    # Where it went, so the `verify` line below can name a real path rather
    # than "<this file>" — a placeholder the shipped SKILL.md then copied as
    # if it were a file that existed (§48.3 item 5).
    if out_path in (None, "-"):
        sys.stdout.write(document)
        written = None
    else:
        written = Path(out_path)
        written.write_text(document, encoding="utf-8")

    # Say plainly whether this is finished, because the next step is `verify`
    # and a stackup with no targets cannot be checked against physics at all.
    if not targets:
        sys.stderr.write(
            "\nWARNING: no --target given, so `verify` can only check the "
            "thickness sum.\nA misread dielectric will not be caught. "
            "Transcribe the impedance targets.\n")
    if written is None:
        sys.stderr.write(
            f"\ntranscribed {len(layers)} copper layers and {len(targets)} "
            f"impedance target(s) to stdout — nothing was written to disk.\n"
            f"Redirect it, or re-run with --out, then verify:\n"
            f"  python3 -m pcbreview.stackup_cli transcribe {argv[2]!r} ... "
            f"> board.stackup.yaml\n"
            f"  python3 -m pcbreview.stackup_cli verify "
            f"{argv[2]!r} board.stackup.yaml\n")
    else:
        sys.stderr.write(
            f"\ntranscribed {len(layers)} copper layers and {len(targets)} "
            f"impedance target(s) — wrote {written}\nnow run:\n"
            f"  python3 -m pcbreview.stackup_cli verify "
            f"{argv[2]!r} {str(written)!r}\n")
    return 0


def main(argv: list[str]) -> int:
    from pcbreview import console_utf8

    # Same crash class as `audit.main`: this module's own diagnostics spell
    # `Ω`, `ε` and `↔`.
    console_utf8()

    # `--help` anywhere prints USAGE. It used to be swallowed: every
    # subcommand takes the board as `argv[2]`, so `transcribe --help` was read
    # as a board path and died with `BoardError: board not found: --help`,
    # after a full KiCad extraction attempt. The USAGE text existed and was
    # good; it was simply unreachable from the command line, which sent one
    # user to read the source to find out what goes in `rows.txt`.
    if "--help" in argv or "-h" in argv:
        sys.stderr.write(USAGE)
        return 0

    if len(argv) < 3 or argv[1] not in ("init", "transcribe", "verify"):
        sys.stderr.write(USAGE)
        return 2

    if argv[1] == "init":
        layers, thickness = _copper_layers(argv[2])
        sys.stdout.write(skeleton(layers, thickness))
        return 0

    if argv[1] == "transcribe":
        return _transcribe_argv(argv)

    if len(argv) < 4:
        sys.stderr.write("verify needs a board and a stackup file\n")
        return 2
    layers, _ = _copper_layers(argv[2])
    result = verify(load_stackup(argv[3]), layers, board_path=argv[2])
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
