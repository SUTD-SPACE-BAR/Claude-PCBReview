"""Power paths at a stated current: drop, via current, conductor heating, board temperature.

    python -m pcbreview.ampacity <project>                    # the review
    python -m pcbreview.ampacity <project> --template         # a manifest skeleton
    python -m pcbreview.ampacity <project> --images out/      # and the maps
    python -m pcbreview.ampacity <project> --check-grid       # and grid convergence
    python -m pcbreview.ampacity <project> --coupled          # and hot copper

`current_capacity` measures what copper *could* carry and compares a net's
feeds with each other. This check starts where that one stops: somebody has
said **what a path carries**, in `<board>.powerpaths.yaml`, and the questions
become absolute.

* **Drop and loss** — `dcflow`, all layers and vias together.
* **Via current** — each barrel's share, against IPC-2221 at the path's
  allowed rise. On a real board the shares are wildly unequal, and the via
  nearest the load runs hot while its neighbours idle.
* **Conductor heating** — the temperature rise the path's own copper reaches
  from copper loss, `boardheat`, against the rise the path allows (10 C by
  default, the same strict default `current_capacity` uses).
* **Board temperature** — copper loss plus the thermal manifest's part
  dissipation, absolute, against laminate and solder-mask limits.
* **Pulses** — a path with `pulse_s` is solved as a transient on top of the
  continuous load, and its barrels against an adiabatic bound when the pulse
  is shorter than a barrel takes to reach equilibrium.
* **Neighbour heating** — how much everything else raises the board under each
  part the thermal manifest names. `check_thermal` takes ambient as the air;
  this says what the part actually sits on.

Temperatures are a bracket, never a number
-------------------------------------------
Every temperature is solved in each of `boardheat.ENVIRONMENTS` and reported
as a bracket. The verdict is taken in the environment the manifest declares,
or — when it declares none — in the **worst** one, and the finding asks the
question that would settle it. `heatsink.py` records why: the surroundings
move the answer more than anything on the board does.

What is not modelled, said on every temperature finding
--------------------------------------------------------
Packages as solids (a part's heat enters at its pads), conduction out through
harness wires and connectors, an enclosure's walls warming, and copper
resistance rising with temperature unless `--coupled` is given — which
re-solves every continuous path at the temperature the first pass found.
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from shapely.geometry import box as _box
from shapely.ops import unary_union

from pcbreview import boardheat, dcflow, powerpaths
from pcbreview import current as current_module
from pcbreview.findings import Finding, _severity

CHECK = "power_path"

#: Barrel current over its IPC-2221 rating at the path's allowed rise. IPC
#: treats a barrel as a long conductor; a real one is 1.6 mm between two heat
#: sinks and runs cooler, which is why major starts at 1.5x and not at 1x.
VIA_RATIO_MINOR, VIA_RATIO_MAJOR, VIA_RATIO_CRITICAL = 1.0, 1.5, 2.5

#: Conductor rise over the rise the path allows. 4x the default 10 C is a
#: 40 C rise from current alone, which on top of a warm bay is where laminate
#: limits start.
RISE_RATIO_MINOR, RISE_RATIO_MAJOR, RISE_RATIO_CRITICAL = 1.0, 2.0, 4.0

#: Drop over the path's declared `max_drop_mv`.
DROP_RATIO_MINOR, DROP_RATIO_MAJOR, DROP_RATIO_CRITICAL = 1.0, 1.5, 3.0

#: Absolute board temperature. Judgement, stated as such: FR-4 laminates are
#: commonly rated for 105-130 C continuous, solder mask and most board-mount
#: connectors for 85-105 C. Major at 90 C leaves room for the error in the
#: environment; critical at 105 C is the lowest common rating.
BOARD_MAJOR_C = 90.0
BOARD_CRITICAL_C = 105.0

#: Rise under a part from everything else on the board before it is worth
#: telling `check_thermal`'s reader: 10 C moves a junction estimate by more
#: than the arithmetic's own precision.
NEIGHBOUR_MINOR_C = 10.0

#: Below this a pulse is too short for a barrel to reach equilibrium (its
#: thermal time constant along 1.6 mm of copper is ~6 ms), so the adiabatic
#: bound is the honest limit; above it, the barrel is effectively steady.
SHORT_PULSE_S = 0.05

#: Grid-halving disagreement worth a finding.
GRID_TOLERANCE = 0.10

PULSE_STEPS = 30

#: Above this a pulse's own transient is solved in every environment rather
#: than once: over tens of seconds the surfaces carry a real share of the heat.
LONG_PULSE_S = 10.0


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class PathOutcome:
    path: powerpaths.PowerPath
    solution: dcflow.FlowSolution | None = None
    error: str | None = None
    refined_drop_v: float | None = None
    refined_cell_mm: float | None = None
    hot: bool = False
    cold_loss_w: float | None = None
    #: the cold solve's drop at the default pitch — what the grid check
    #: compares the half-pitch solve with, so a hot re-solve cannot read as
    #: grid error
    cold_drop_v: float | None = None


@dataclass
class EnvOutcome:
    """One scenario in one environment."""
    copper_max: tuple[float, str, float, float]          # rise, layer, x, y
    total_max: tuple[float, str, float, float]
    #: each path's copper, from its own current alone — the IPC sense of
    #: "rise", and what the path's allowance is judged against
    path_rise: dict[str, float] = field(default_factory=dict)
    #: the same copper with everything else in the scenario heating it too
    path_total: dict[str, float] = field(default_factory=dict)
    pulse_conductor: dict[str, float] = field(default_factory=dict)
    pulse_total: dict[str, float] = field(default_factory=dict)


@dataclass
class ScenarioOutcome:
    scenario: powerpaths.Scenario
    by_env: dict[str, EnvOutcome] = field(default_factory=dict)
    shared_nets: list[str] = field(default_factory=list)


@dataclass
class AmpacityResult:
    paths: list[PathOutcome]
    scenarios: list[ScenarioOutcome]
    environments: dict[str, float]
    verdict_env: str
    verdict_declared: bool
    ambient_c: float
    ambient_source: str
    neighbour: dict[str, dict] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    seconds: float = 0.0
    #: the verdict environment's fields, kept for pictures
    maps: dict[str, object] = field(default_factory=dict)
    #: copper thickness per layer, for the adiabatic pulse bound
    thickness_mm: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------

def _path_cells(model: boardheat.HeatModel, solution: dcflow.FlowSolution) -> np.ndarray:
    """Indices in the heat model's vector of every cell the path's copper is in."""
    ny, nx = solution.shape
    h = solution.cell_mm
    xs = solution.origin[0] + (np.arange(nx) + 0.5) * h
    ys = solution.origin[1] + (np.arange(ny) + 0.5) * h
    gx, gy = np.meshgrid(xs, ys)
    s = model.sheets
    out = []
    for layer, mask in solution.layer_mask.items():
        if layer not in model.layers or not mask.any():
            continue
        i = np.clip(((gx[mask] - s.origin[0]) / s.cell_mm).astype(int), 0, s.inside.shape[1] - 1)
        j = np.clip(((gy[mask] - s.origin[1]) / s.cell_mm).astype(int), 0, s.inside.shape[0] - 1)
        cell = model.index[j, i]
        cell = np.unique(cell[cell >= 0])
        out.append(model.layers.index(layer) * model.n_cells + cell)
    return np.concatenate(out) if out else np.empty(0, dtype=np.int64)


def _part_cells(model: boardheat.HeatModel, board, ref: str) -> np.ndarray:
    return boardheat.part_cells(model, board, ref)


def _max_of(raw: np.ndarray, cells: np.ndarray) -> float:
    return float(raw[cells].max()) if cells.size else 0.0


def _argmax(model: boardheat.HeatModel, raw: np.ndarray) -> tuple[float, str, float, float]:
    k = int(np.argmax(raw))
    layer = model.layers[k // model.n_cells]
    cell = k % model.n_cells
    j, i = np.argwhere(model.sheets.inside)[cell]
    s = model.sheets
    return (float(raw[k]), layer, s.origin[0] + (i + 0.5) * s.cell_mm,
            s.origin[1] + (j + 0.5) * s.cell_mm)


def _parts_with_heat(board, thermal) -> dict[str, float]:
    if not thermal:
        return {}
    refs = {f.ref for f in board.footprints}
    out = {}
    for ref, part in thermal.parts.items():
        watts = getattr(part, "dissipation_w", None)
        stated = getattr(part, "dissipation_stated", True)
        if ref in refs and stated and watts and watts > 0:
            out[ref] = float(watts)
    return out


def validate(board, manifest: powerpaths.PowerPathManifest) -> list[str]:
    """Every path's net and pads against the board, without solving anything.

    Raises `powerpaths.PowerPathError` naming the first wrong one; returns one
    line per path describing what it resolved to. `--validate` prints these,
    which is how a manifest is checked before it is trusted.
    """
    names = {n.name for n in board.nets.values()}
    lines = []
    for p in manifest.paths:
        if p.net not in names:
            near = sorted(n for n in names if p.net.strip("/").lower()
                          in n.lower())[:5]
            raise powerpaths.PowerPathError(
                f"path '{p.name}': net {p.net!r} is not on this board"
                + (f"; did you mean {near}?" if near else ""))
        try:
            source = dcflow.expand_pads(board, p.net, p.source)
            sinks = [dcflow.expand_pads(board, p.net, s.pads) for s in p.sinks]
        except dcflow.FlowError as exc:
            raise powerpaths.PowerPathError(f"path '{p.name}': {exc}") from exc
        lines.append(f"{p.name}: {p.net}, {p.current_a:g} A from "
                     f"{', '.join(x.designator for x in source)} to "
                     + "; ".join(", ".join(x.designator for x in pads)
                                 for pads in sinks)
                     + (f", {p.pulse_s:g} s pulse" if p.is_pulse else ""))
    for mount in manifest.mounts:
        if not any(f.ref == mount for f in board.footprints):
            raise powerpaths.PowerPathError(f"mount {mount!r} is not a "
                                            f"footprint on this board")
    return lines


def evaluate(board, manifest: powerpaths.PowerPathManifest, thermal=None, *,
             cell_mm: float = dcflow.CELL_MM,
             thermal_cell_mm: float = boardheat.CELL_MM,
             check_grid: bool = False, coupled: bool = False,
             power_nets: set[str] | None = None,
             progress=None) -> AmpacityResult:
    """Solve every path, heat the board in every environment, judge.

    Raises `powerpaths.PowerPathError` when the manifest names a net or a pad
    the board does not have — that is a wrong manifest, not a finding.
    """
    started = time.time()
    say = progress or (lambda message: None)
    plating = manifest.plating_um or current_module.VIA_PLATING_UM
    if manifest.ambient_c is not None:
        ambient, ambient_source = manifest.ambient_c, "the power-path manifest"
    elif thermal:
        ambient, ambient_source = thermal.ambient_c, "the thermal manifest"
    else:
        ambient, ambient_source = 25.0, "assumed — nobody stated it"

    validate(board, manifest)

    def solve_all(temperature=None, only=None):
        out = {}
        for p in manifest.paths:
            if only is not None and p.name not in only:
                continue
            say(f"solving {p.name}")
            try:
                sol = dcflow.solve_path(board, p.net, p.source,
                                        [(s.pads, s.current_a) for s in p.sinks],
                                        cell_mm=cell_mm, plating_um=plating,
                                        temperature_c=temperature)
                out[p.name] = PathOutcome(path=p, solution=sol, hot=temperature is not None)
            except dcflow.FlowError as exc:
                out[p.name] = PathOutcome(path=p, error=str(exc))
        return out

    outcomes = solve_all()
    if check_grid:
        for name, o in outcomes.items():
            if o.solution is None:
                continue
            fine = o.solution.cell_mm / 2
            try:
                p = o.path
                ref = dcflow.solve_path(board, p.net, p.source,
                                        [(s.pads, s.current_a) for s in p.sinks],
                                        cell_mm=fine, plating_um=plating)
                o.refined_drop_v, o.refined_cell_mm = ref.worst_drop_v, ref.cell_mm
                o.cold_drop_v = o.solution.worst_drop_v
            except dcflow.FlowError:
                pass

    environments = dict(boardheat.ENVIRONMENTS)
    if manifest.environment and manifest.environment not in environments:
        environments[manifest.environment] = manifest.surface_w_m2k
    verdict_env = manifest.environment or boardheat.DEFAULT_VERDICT_ENVIRONMENT
    say("building the board heat model")
    base = boardheat.sheets_for(board, environments[verdict_env],
                                cell_mm=thermal_cell_mm, mounts=manifest.mounts,
                                plating_um=plating)
    models = {env: boardheat.HeatModel(boardheat.with_environment(base, coeff))
              for env, coeff in environments.items()}
    ref_model = models[verdict_env]
    parts = _parts_with_heat(board, thermal)

    def heat(outcomes):
        q_path, cells = {}, {}
        for name, o in outcomes.items():
            if o.solution is None:
                continue
            q = ref_model.zeros()
            boardheat.deposit_flow(ref_model, q, o.solution)
            q_path[name] = q
            cells[name] = _path_cells(ref_model, o.solution)
        q_part, part_cells = {}, {}
        for ref, watts in parts.items():
            q = ref_model.zeros()
            boardheat.deposit_part(ref_model, q, board, ref, watts)
            q_part[ref] = q
            part_cells[ref] = _part_cells(ref_model, board, ref)
        results, neighbour, maps = [], {}, {}
        for sc in manifest.scenarios:
            outcome = ScenarioOutcome(scenario=sc)
            nets = [outcomes[n].path.net for n in sc.paths
                    if n in outcomes and outcomes[n].solution is not None]
            outcome.shared_nets = sorted({n for n in nets if nets.count(n) > 1})
            results.append(outcome)
        increments = {}

        def pulse_increment(env, model, name):
            """A pulse's own field, by linearity added to any steady
            background. Over a pulse of seconds the surfaces shed little of
            the heat, so it is solved once, in the verdict environment, and
            reused; a pulse longer than `LONG_PULSE_S` is solved per
            environment because there the surroundings start to matter."""
            secs = outcomes[name].path.pulse_s
            key = (env if secs > LONG_PULSE_S else verdict_env, name)
            if key not in increments:
                say(f"pulse transient: {name}, {secs:g} s")
                use = model if secs > LONG_PULSE_S else models[verdict_env]
                increments[key] = use.pulse_rise(q_path[name], secs, steps=PULSE_STEPS)
            return increments[key]

        for env, model in models.items():
            say(f"heating the board: {env}")
            order = list(q_path) + list(q_part)
            fields = dict(zip(order, model.steady_many(
                [q_path[n] for n in q_path] + [q_part[r] for r in q_part])))
            for outcome in results:
                sc = outcome.scenario
                steady = [n for n in sc.paths if n in q_path
                          and not outcomes[n].path.is_pulse]
                copper = model.zeros()
                for n in steady:
                    copper += sc.paths[n] ** 2 * fields[n]
                parts_raw = model.zeros()
                if sc.parts:
                    for r in q_part:
                        parts_raw += fields[r]
                total = copper + parts_raw
                env_out = EnvOutcome(copper_max=_argmax(model, copper),
                                     total_max=_argmax(model, total))
                for n in steady:
                    env_out.path_rise[n] = _max_of(sc.paths[n] ** 2 * fields[n], cells[n])
                    env_out.path_total[n] = _max_of(total, cells[n])
                for n in sc.paths:
                    if n not in q_path or not outcomes[n].path.is_pulse:
                        continue
                    rise = sc.paths[n] ** 2 * pulse_increment(env, model, n)
                    env_out.pulse_conductor[n] = _max_of(rise, cells[n])
                    env_out.path_total[n] = _max_of(total + rise, cells[n])
                    env_out.pulse_total[n] = float((total + rise).max())
                outcome.by_env[env] = env_out
                if env == verdict_env:
                    if sc.parts:
                        for r in q_part:
                            others = total - fields[r]
                            rise = _max_of(others, part_cells[r])
                            if rise > neighbour.get(r, {}).get("rise", -1):
                                neighbour[r] = {"rise": rise, "scenario": sc.name,
                                                "own": _max_of(fields[r], part_cells[r])}
                    maps[sc.name] = model._result(total, env)
        return results, neighbour, maps, q_path

    scenario_results, neighbour, maps, q_path = heat(outcomes)

    if coupled:
        worst = max(scenario_results, key=lambda r: r.by_env[verdict_env].total_max[0],
                    default=None)
        if worst is not None:
            field_ = maps[worst.scenario.name]
            look = field_.sampler()
            temperature = lambda layer, xs, ys: ambient + look(layer, xs, ys)
            steady_names = [n for n in worst.scenario.paths
                            if not manifest.path(n).is_pulse]
            hot = solve_all(temperature, only=set(steady_names))
            for name, o in hot.items():
                if o.solution is not None and outcomes[name].solution is not None:
                    o.cold_loss_w = outcomes[name].solution.loss_w
                    o.refined_drop_v = outcomes[name].refined_drop_v
                    o.refined_cell_mm = outcomes[name].refined_cell_mm
                    o.cold_drop_v = outcomes[name].cold_drop_v
                    outcomes[name] = o
            scenario_results, neighbour, maps, q_path = heat(outcomes)

    result = AmpacityResult(
        paths=[outcomes[p.name] for p in manifest.paths],
        scenarios=scenario_results, environments=environments,
        verdict_env=verdict_env, verdict_declared=manifest.environment is not None,
        ambient_c=ambient, ambient_source=ambient_source, neighbour=neighbour,
        maps=maps, thickness_mm=dcflow.stack_geometry(board)[0])
    result.assumptions = _assumptions(result, manifest, plating, coupled, parts)
    result.findings = _findings(result, manifest, plating, power_nets)
    result.seconds = time.time() - started
    return result


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def _bracket(by_env: dict, pick) -> str:
    return ", ".join(f"{env} +{pick(o):.1f} C" for env, o in by_env.items())


def _assumptions(result, manifest, plating, coupled, parts) -> list[str]:
    a = [f"ambient {result.ambient_c:g} C, from {result.ambient_source}",
         f"via plating {plating:g} um — scales every barrel's resistance and "
         f"rating directly",
         "temperatures are solved in every environment "
         + ", ".join(f"{k} ({v:g} W/m^2K per face)" for k, v in result.environments.items())
         + f"; the verdict is taken in '{result.verdict_env}'"
         + ("" if result.verdict_declared else
            " because none was declared, and it is the worst of them"),
         "laminate conductivity 0.8 W/mK in-plane and 0.3 W/mK through; "
         "packages are not modelled as solids — a part's heat enters at its "
         "pads; no heat leaves through wires or connectors",
         ("copper resistance re-solved at the temperature the first pass "
          "found (--coupled)" if coupled else
          "copper resistivity at 20 C; a hot path dissipates ~4% more per "
          "10 C, which --coupled accounts for"),
         ("part dissipation from the thermal manifest: "
          + ", ".join(f"{r} {w:g} W" for r, w in parts.items()))
         if parts else "no part dissipation included — no thermal manifest "
                       "states any"]
    if any(p.is_pulse for p in manifest.paths):
        a.append(f"a pulse's own rise is solved once, in '{result.verdict_env}', "
                 f"and added to each environment's steady background — exact by "
                 f"linearity for the background, and the surroundings barely act "
                 f"over a pulse of seconds; pulses over {LONG_PULSE_S:g} s are "
                 f"solved per environment")
    for sc in result.scenarios:
        if sc.scenario.implied:
            a.append(f"scenario '{sc.scenario.name}' was implied: the manifest "
                     f"names none, so every continuous path runs at once — the "
                     f"worst case the declared currents allow")
        if sc.shared_nets:
            a.append(f"scenario '{sc.scenario.name}': paths on {sc.shared_nets} "
                     f"were solved separately and their heat added, which "
                     f"misses the cross term of (I1 + I2)^2 — declare one path "
                     f"with several sinks instead")
    return a


def _hotspot_geometry(solution, count=3):
    if solution is None or not solution.hotspots:
        return None
    half = dcflow.HOTSPOT_WINDOW_MM / 2
    return unary_union([_box(h.x - half, h.y - half, h.x + half, h.y + half)
                        for h in solution.hotspots[:count]])


def _findings(result: AmpacityResult, manifest, plating, power_nets) -> list[Finding]:
    out: list[Finding] = []
    venv = result.verdict_env
    env_note = (f"in the declared environment '{venv}'" if result.verdict_declared
                else f"in '{venv}', the worst environment, because none was declared")
    env_question = (None if result.verdict_declared else
                    "the board's surroundings — a sealed bay, open still air, "
                    "or moving air; the temperature verdict changes with it")

    for o in result.paths:
        p = o.path
        if o.solution is None:
            out.append(Finding(
                check=CHECK, severity="critical", score=10.0, nets=[p.net],
                title=f"{p.name}: the declared path does not exist in copper",
                detail=[o.error or "no solution"],
                basis="the path's copper on every layer, joined through vias "
                      "and plated holes, searched for a connection between "
                      "its source and sink pads",
                intent_needed="whether the manifest names the right pads, or "
                              "the board is missing a connection"))
            continue
        sol = o.solution
        drop_mv = sol.worst_drop_v * 1e3
        detail = [f"{p.current_a:g} A from {', '.join(p.source)} to "
                  + "; ".join(f"{', '.join(s.pads)} ({s.current_a:g} A)" for s in p.sinks),
                  f"drop {drop_mv:.1f} mV"
                  + (f" ({sol.resistance_ohm * 1e3:.2f} mOhm)" if sol.resistance_ohm else "")
                  + (f", {100 * sol.worst_drop_v / p.voltage_v:.1f}% of {p.voltage_v:g} V"
                     if p.voltage_v else "")
                  + f"; I^2R {sol.loss_w * 1e3:.0f} mW",
                  f"{len(sol.barrels)} barrel(s) carry current; solved on "
                  f"{sol.cell_mm:g} mm cells ({sol.cells:,} cells)"]
        if o.cold_loss_w is not None:
            detail.append(f"re-solved hot: loss {o.cold_loss_w * 1e3:.0f} -> "
                          f"{sol.loss_w * 1e3:.0f} mW")
        for hs in sol.hotspots[:3]:
            detail.append(f"loss concentrates at ({hs.x:.2f}, {hs.y:.2f}) on "
                          f"{hs.layer}: {hs.density_mw_mm2:.1f} mW/mm^2, "
                          f"{hs.sheet_a_per_mm:.2f} A per mm of width")
        detail += sol.notes
        severity, score = "info", 0.0
        if p.max_drop_mv:
            severity, score = _severity(drop_mv / p.max_drop_mv, DROP_RATIO_MINOR,
                                        DROP_RATIO_MAJOR, DROP_RATIO_CRITICAL)
            detail.append(f"allowed drop {p.max_drop_mv:g} mV")
        out.append(Finding(
            check=CHECK, severity=severity, score=score, nets=[p.net],
            layers=sorted({hs.layer for hs in sol.hotspots[:3]}),
            location=(sol.hotspots[0].x, sol.hotspots[0].y) if sol.hotspots else None,
            title=(f"{p.name}: {p.current_a:g} A drops {drop_mv:.1f} mV and "
                   f"dissipates {sol.loss_w * 1e3:.0f} mW in the copper"),
            detail=detail,
            measurement={"current_a": p.current_a, "drop_mv": round(drop_mv, 3),
                         "loss_mw": round(sol.loss_w * 1e3, 2),
                         "resistance_mohm": (round(sol.resistance_ohm * 1e3, 4)
                                             if sol.resistance_ohm else None),
                         "cell_mm": sol.cell_mm},
            basis="DC current flow over the path's copper on every layer, "
                  "joined by via and plated-hole barrels, on a square grid; "
                  "terminals merged into single nodes by projection",
            assumption=f"copper thickness from the bound stackup; via plating "
                       f"{plating:g} um",
            intent_needed=(None if p.max_drop_mv else
                           "the drop this path can tolerate — without one the "
                           "drop is a fact, not a verdict"),
            geometry=_hotspot_geometry(sol), geometry_label="loss hotspots"))

        # vias
        if sol.barrels:
            short = p.is_pulse and p.pulse_s < SHORT_PULSE_S
            worst_ratio, rows = 0.0, []
            for b in sol.barrels:
                if short:
                    tube_mm = b.drill_mm * math.pi
                    bound = dcflow.adiabatic_rise_c(
                        b.current_a / tube_mm, plating / 1000.0, p.pulse_s)
                    ratio = bound / p.rise_c
                    rows.append((ratio, f"{b.label}: {b.current_a:.2f} A for "
                                        f"{p.pulse_s * 1e3:g} ms, adiabatic bound "
                                        f"+{bound:.1f} C"))
                else:
                    rating = dcflow.barrel_rating_a(b.drill_mm, p.rise_c, plating)
                    ratio = b.current_a / rating
                    rows.append((ratio, f"{b.label} ({b.drill_mm:g} mm): "
                                        f"{b.current_a:.2f} A against "
                                        f"{rating:.2f} A at +{p.rise_c:g} C"))
                worst_ratio = max(worst_ratio, ratio)
            rows.sort(key=lambda r: -r[0])
            severity, score = _severity(worst_ratio, VIA_RATIO_MINOR,
                                        VIA_RATIO_MAJOR, VIA_RATIO_CRITICAL)
            top = sol.barrels[0]
            over = sum(1 for r, _ in rows if r >= 1.0)
            out.append(Finding(
                check=CHECK, severity=severity, score=score, nets=[p.net],
                location=(top.x, top.y), layers=list(top.span),
                title=(f"{p.name}: the busiest via carries {top.current_a:.2f} A "
                       f"of {p.current_a:g} A"
                       + (f" — {over} barrel(s) over their rating" if over else
                          " — every barrel within its rating")),
                detail=[text for _, text in rows[:6]]
                       + ([f"... and {len(rows) - 6} more"] if len(rows) > 6 else []),
                measurement={"worst_via_a": round(top.current_a, 3),
                             "worst_ratio": round(worst_ratio, 3),
                             "barrels": len(rows), "over_rating": over},
                basis=("each barrel's current from the same solve; "
                       + ("an adiabatic bound, because the pulse is shorter than "
                          "a barrel takes to shed heat" if short else
                          "IPC-2221 with the barrel's circumference as width and "
                          "its plating as thickness, external constant")),
                assumption=f"via plating {plating:g} um; allowed rise "
                           f"{p.rise_c:g} C ({'stated' if p.rise_stated else 'default'})",
                intent_needed=(None if p.rise_stated else
                               "the temperature rise this path may run at")))

    # conductor heating, per path, worst scenario
    worst_rise: dict[str, tuple] = {}
    for sc in result.scenarios:
        env_out = sc.by_env.get(venv)
        if env_out is None:
            continue
        for name, rise in env_out.path_rise.items():
            if rise > worst_rise.get(name, (-1,))[0]:
                worst_rise[name] = (rise, sc)
        for name, rise in env_out.pulse_conductor.items():
            if rise > worst_rise.get(name, (-1,))[0]:
                worst_rise[name] = (rise, sc)
    for o in result.paths:
        p = o.path
        if p.name not in worst_rise:
            continue
        rise, sc = worst_rise[p.name]
        pick = ((lambda e, n=p.name: e.pulse_conductor.get(n, 0.0)) if p.is_pulse
                else (lambda e, n=p.name: e.path_rise.get(n, 0.0)))
        severity, score = _severity(rise / p.rise_c, RISE_RATIO_MINOR,
                                    RISE_RATIO_MAJOR, RISE_RATIO_CRITICAL)
        kind = (f"at the end of a {p.pulse_s:g} s pulse" if p.is_pulse
                else "in steady state")
        bound = []
        sol = next((o.solution for o in result.paths if o.path is p), None)
        if p.is_pulse and sol is not None and sol.hotspots:
            hs = sol.hotspots[0]
            t = result.thickness_mm.get(hs.layer)
            if t:
                adiabatic = dcflow.adiabatic_rise_c(hs.sheet_a_per_mm, t, p.pulse_s)
                bound = [f"if no heat left its worst spot ({hs.x:.2f}, {hs.y:.2f}) "
                         f"on {hs.layer}, that spot would rise +{adiabatic:.0f} C "
                         f"over the pulse — an upper bound the model sits far "
                         f"below because copper sheds heat sideways in "
                         f"milliseconds; a model result near it means the "
                         f"spreading is doing little"]
        out.append(Finding(
            check=CHECK, severity=severity, score=score, nets=[p.net],
            title=(f"{p.name}: its own current heats its copper +{rise:.1f} C "
                   f"{kind}, against +{p.rise_c:g} C allowed"),
            detail=[f"scenario '{sc.scenario.name}', {env_note}",
                    "bracket: " + _bracket(sc.by_env, pick),
                    f"with everything else in the scenario heating the board "
                    f"too, this copper reaches +"
                    f"{sc.by_env[venv].path_total.get(p.name, rise):.1f} C — "
                    f"judged in the board temperature finding, not here"]
                   + bound,
            measurement={"rise_c": round(rise, 2), "allowed_c": p.rise_c,
                         "environment": venv},
            basis="this path's copper I^2R, deposited where it was dissipated, "
                  "through a one-sheet-per-layer conduction model of the board "
                  "with convection from both faces; the hottest cell its copper "
                  "occupies",
            assumption=f"surface {result.environments[venv]:g} W/m^2K per face; "
                       f"allowed rise {'stated' if p.rise_stated else 'default'}",
            intent_needed=env_question or (None if p.rise_stated else
                                           "the rise this path may run at")))

    # board temperature, per scenario
    for sc in result.scenarios:
        env_out = sc.by_env.get(venv)
        if env_out is None:
            continue
        rise, layer, x, y = env_out.total_max
        pulse_peak = max(env_out.pulse_total.values(), default=0.0)
        hottest = result.ambient_c + max(rise, pulse_peak)
        if hottest >= BOARD_CRITICAL_C:
            severity = "critical"
        elif hottest >= BOARD_MAJOR_C:
            severity = "major"
        else:
            severity = "info"
        out.append(Finding(
            check=CHECK, severity=severity, score=hottest / BOARD_MAJOR_C,
            layers=[layer], location=(x, y),
            nets=sorted({result.paths[i].path.net for i, o in enumerate(result.paths)
                         if o.path.name in sc.scenario.paths}),
            title=(f"scenario '{sc.scenario.name}': the board reaches "
                   f"{hottest:.0f} C ({result.ambient_c:g} C ambient "
                   f"+{hottest - result.ambient_c:.1f} C) at ({x:.1f}, {y:.1f})"),
            detail=[f"{env_note}",
                    "steady bracket: " + _bracket(sc.by_env, lambda e: e.total_max[0]),
                    f"of which copper loss alone: "
                    + _bracket(sc.by_env, lambda e: e.copper_max[0])]
                   + ([f"pulse peak +{pulse_peak:.1f} C"] if pulse_peak else []),
            measurement={"max_c": round(hottest, 1), "rise_c": round(hottest - result.ambient_c, 2),
                         "environment": venv},
            basis="copper loss plus thermal-manifest dissipation in the same "
                  "board conduction model",
            assumption=f"ambient {result.ambient_c:g} C ({result.ambient_source}); "
                       f"limits {BOARD_MAJOR_C:g} C major and {BOARD_CRITICAL_C:g} C "
                       f"critical are judgement — laminate, mask and connector ratings",
            intent_needed=env_question or "the ambient the board actually sits in"))

    # neighbour heating
    for ref, n in sorted(result.neighbour.items()):
        severity = "minor" if n["rise"] >= NEIGHBOUR_MINOR_C else "info"
        out.append(Finding(
            check=CHECK, severity=severity, score=n["rise"] / NEIGHBOUR_MINOR_C,
            title=(f"{ref} sits on board +{n['rise']:.1f} C above ambient from "
                   f"everything else, before its own {n['own']:.1f} C"),
            detail=[f"scenario '{n['scenario']}', {env_note}",
                    "`check_thermal` adds theta_JA * P to ambient; the air "
                    "this part sees is ambient plus this"],
            measurement={"neighbour_rise_c": round(n["rise"], 2)},
            basis="the same heat model solved once per source, by "
                  "superposition, and the part's own share removed",
            assumption="a part's heat enters the board at its pads"))

    # paths whose grid did not settle
    for o in result.paths:
        if o.solution is None or o.refined_drop_v is None:
            continue
        coarse, fine = (o.cold_drop_v or o.solution.worst_drop_v), o.refined_drop_v
        change = abs(coarse - fine) / fine if fine else 0.0
        out.append(Finding(
            check=CHECK, severity="minor" if change > GRID_TOLERANCE else "info",
            score=change / GRID_TOLERANCE, nets=[o.path.net],
            title=(f"{o.path.name}: halving the grid moves the drop by "
                   f"{100 * change:.1f}%"),
            detail=[f"{o.solution.cell_mm:g} mm: {coarse * 1e3:.2f} mV; "
                    f"{o.refined_cell_mm:g} mm: {fine * 1e3:.2f} mV (both at 20 C)",
                    "a coarse grid can only overstate resistance — it cannot "
                    "resolve the narrowest copper"],
            measurement={"change": round(change, 4)},
            basis="the same path solved at two pitches"))

    # power nets with no path
    if power_nets:
        declared = {p.net for p in manifest.paths}
        missing = sorted(set(power_nets) - declared)
        if missing:
            out.append(Finding(
                check=CHECK, severity="info", score=0.0, nets=missing[:40],
                title=(f"{len(missing)} power net(s) have no declared path, so "
                       f"their drop, via current and heating were not computed"),
                detail=[", ".join(missing[:40])
                        + (f" ... and {len(missing) - 40} more" if len(missing) > 40 else "")],
                basis="the audit's power nets against the manifest's paths",
                intent_needed="which pads each carries current between, and how much"))
    out.sort(key=lambda f: f.rank)
    return out


def check_power_paths(board, manifest, thermal=None, power_nets=None,
                      **kwargs) -> list[Finding]:
    """The `audit` entry point: findings only."""
    return evaluate(board, manifest, thermal, power_nets=power_nets,
                    **kwargs).findings


def coverage(manifest) -> dict:
    if manifest is None:
        return {"paths_declared": 0}
    return {"paths_declared": len(manifest.paths),
            "scenarios": len(manifest.scenarios),
            "pulses": sum(1 for p in manifest.paths if p.is_pulse)}


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

def write_images(result: AmpacityResult, board, out_dir) -> list[Path]:
    from pcbreview import heatmap
    out_dir = Path(out_dir)
    written = []
    for name, field_ in result.maps.items():
        peak = np.fmax.reduce([g for g in field_.rise_c.values()]) + result.ambient_c
        rise, layer, x, y = field_.max_rise()
        written.append(heatmap.write(
            out_dir / f"temperature-{_slug(name)}", peak, field_.origin,
            field_.cell_mm, title=f"{name}: hottest layer at each point, "
                                  f"{field_.environment}",
            unit="C", outline=board.outline,
            markers=[(x, y, f"{result.ambient_c + rise:.0f} C on {layer}")]))
    for o in result.paths:
        sol = o.solution
        if sol is None:
            continue
        sheets = [np.where(sol.layer_mask[l], sol.layer_sheet_a_per_mm[l], np.nan)
                  for l in sol.layer_sheet_a_per_mm]
        grid = np.fmax.reduce(sheets)
        flowing = grid[np.isfinite(grid) & (grid > 0)]
        # a handful of contact cells would otherwise set the scale and leave
        # the whole path dark; anything above the clip shows at full brightness
        hi = float(np.percentile(flowing, 99.5)) if flowing.size else 1.0
        written.append(heatmap.write(
            out_dir / f"current-{_slug(o.path.name)}", grid, sol.origin, sol.cell_mm,
            title=f"{o.path.name}: {o.path.current_a:g} A, current per mm of "
                  f"copper width (busiest layer; scale clipped at the 99.5th "
                  f"percentile)", unit="A/mm", lo=0.0, hi=hi,
            outline=board.outline, scale=2,
            markers=[(h.x, h.y, f"{h.density_mw_mm2:.0f} mW/mm2")
                     for h in sol.hotspots[:3]]
                    + [(b.x, b.y, f"{b.current_a:.1f} A via")
                       for b in sol.barrels[:2]]))
    return written


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower()[:60]


# ---------------------------------------------------------------------------
# The report and the command
# ---------------------------------------------------------------------------

def report(result: AmpacityResult) -> str:
    lines = []
    envs = list(result.environments)
    lines.append(f"POWER PATHS — {len(result.paths)}")
    for o in result.paths:
        p = o.path
        if o.solution is None:
            lines.append(f"  {p.name:28} NOT SOLVED: {o.error}")
            continue
        s = o.solution
        worst = s.worst_barrel()
        via = (f"{worst.current_a:.2f} A busiest via" if worst else "no vias")
        lines.append(f"  {p.name:28} {p.current_a:6.2f} A  drop {s.worst_drop_v * 1e3:8.2f} mV"
                     f"  loss {s.loss_w * 1e3:8.1f} mW  {via}"
                     + (f"  [{p.pulse_s:g} s pulse]" if p.is_pulse else ""))
    lines.append("")
    lines.append(f"TEMPERATURE RISE ABOVE {result.ambient_c:g} C — "
                 + " / ".join(envs) + f"; verdict in '{result.verdict_env}'")
    for sc in result.scenarios:
        lines.append(f"  {sc.scenario.name}")
        lines.append("    copper only   " + " / ".join(
            f"+{sc.by_env[e].copper_max[0]:.1f}" for e in envs))
        lines.append("    with parts    " + " / ".join(
            f"+{sc.by_env[e].total_max[0]:.1f}" for e in envs))
        own = {**sc.by_env[envs[0]].path_rise, **sc.by_env[envs[0]].pulse_conductor}
        for n in sorted(own, key=lambda n: -sc.by_env[result.verdict_env].path_rise.get(
                n, sc.by_env[result.verdict_env].pulse_conductor.get(n, 0.0))):
            lines.append(f"    {n[:24]:24}" + " / ".join(
                f"+{sc.by_env[e].path_rise.get(n, sc.by_env[e].pulse_conductor.get(n, 0)):.1f}"
                for e in envs) + "   own current")
    lines.append("")
    lines.append(f"FINDINGS — {len(result.findings)}")
    for f in result.findings:
        lines.append(f.report())
        lines.append("")
    lines.append("ASSUMPTIONS")
    lines += [f"  - {a}" for a in result.assumptions]
    lines.append(f"\n{result.seconds:.0f} s")
    return "\n".join(lines)


USAGE = """usage: python -m pcbreview.ampacity <project-or-board> [options]

  --template          print a power-path manifest skeleton and stop
  --validate          check the manifest's nets and pads against the board, solve nothing
  --images DIR        write temperature and current-density maps (SVG)
  --check-grid        solve every path again at half the pitch
  --coupled           re-solve hot: copper resistance at the found temperature
  --cell-mm N         current-flow pitch (default 0.1)
  --thermal-cell-mm N heat-model pitch (default 0.25)
  --json FILE         write findings and numbers as JSON

exit 0 reviewed, 1 refused (no stackup, or a manifest that does not load),
2 the command was not understood.
"""


def main(argv: list[str]) -> int:
    if "--help" in argv or "-h" in argv or len(argv) < 2:
        sys.stderr.write(USAGE)
        return 0 if ("--help" in argv or "-h" in argv) else 2
    options, positional, i = {}, [], 1
    valued = {"--images", "--cell-mm", "--thermal-cell-mm", "--json"}
    while i < len(argv):
        arg = argv[i]
        if arg in valued:
            if i + 1 >= len(argv):
                sys.stderr.write(f"{arg} needs a value\n")
                return 2
            options[arg] = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--"):
            options[arg] = True
        else:
            positional.append(arg)
        i += 1
    unknown = set(options) - valued - {"--template", "--validate", "--check-grid",
                                       "--coupled"}
    if unknown or not positional:
        sys.stderr.write(f"not understood: {sorted(unknown)}\n{USAGE}" if unknown else USAGE)
        return 2

    from pcbreview import pipeline
    from pcbreview import thermal as thermal_module
    try:
        project = pipeline.discover(positional[0])
    except pipeline.PipelineError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if project.stackup is None:
        sys.stderr.write(f"{project.board.name}: no validated fabrication stackup. "
                         f"Copper thickness is half of every resistance here, and "
                         f"it is not guessed — `python -m pcbreview.stackups` "
                         f"binds one.\n")
        return 1
    board = pipeline._load_board(project.board, project.stackup)

    if options.get("--template"):
        from pcbreview import netclass
        nets = sorted(set(netclass.power_by_width(board))
                      | {z.net_name for z in board.zones
                         if z.net_name and not z.is_rule_area})
        print(powerpaths.template(board, nets))
        return 0

    path = project.powerpaths or powerpaths.default_path(project.board)
    print("inputs")
    print(project.describe())
    print()
    if not Path(path).exists():
        sys.stderr.write(
            f"no power-path manifest at {path}. Nothing here knows what any "
            f"path carries; `--template` prints a skeleton to fill in from what "
            f"the user says.\n")
        return 1
    if options.get("--validate"):
        try:
            lines = validate(board, powerpaths.load(path))
        except powerpaths.PowerPathError as exc:
            sys.stderr.write(f"refused: {exc}\n")
            return 1
        print(f"{Path(path).name}: {len(lines)} path(s), every net and pad found")
        print("\n".join(f"  {line}" for line in lines))
        return 0
    try:
        manifest = powerpaths.load(path)
        thermal = thermal_module.load(project.thermal) if project.thermal else None
        result = evaluate(
            board, manifest, thermal,
            cell_mm=float(options.get("--cell-mm", dcflow.CELL_MM)),
            thermal_cell_mm=float(options.get("--thermal-cell-mm", boardheat.CELL_MM)),
            check_grid=bool(options.get("--check-grid")),
            coupled=bool(options.get("--coupled")),
            progress=lambda m: sys.stderr.write(f"  {m}\n"))
    except powerpaths.PowerPathError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1
    print(report(result))
    if options.get("--images"):
        written = write_images(result, board, options["--images"])
        print("\nimages")
        print("\n".join(f"  {w}" for w in written))
    if options.get("--json"):
        Path(options["--json"]).write_text(json.dumps({
            "findings": [f.summary() for f in result.findings],
            "paths": [{"name": o.path.name, "net": o.path.net,
                       "current_a": o.path.current_a, "error": o.error,
                       "drop_v": o.solution.drop_v if o.solution else None,
                       "loss_w": o.solution.loss_w if o.solution else None,
                       "cell_mm": o.solution.cell_mm if o.solution else None,
                       "barrels": [vars(b) for b in o.solution.barrels[:10]]
                       if o.solution else []}
                      for o in result.paths],
            "scenarios": [{"name": s.scenario.name,
                           "by_env": {e: {"copper_max_c": v.copper_max[0],
                                          "total_max_c": v.total_max[0],
                                          "path_rise_c": v.path_rise,
                                          "pulse_conductor_c": v.pulse_conductor}
                                      for e, v in s.by_env.items()}}
                          for s in result.scenarios],
            "assumptions": result.assumptions,
            "ambient_c": result.ambient_c,
            "verdict_environment": result.verdict_env,
        }, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
