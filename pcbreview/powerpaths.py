"""The power-path manifest: which pads a current leaves from, which it reaches, how much.

`<board>.powerpaths.yaml`, beside the board, top-level key `paths`. It is the
input `ampacity` needs and nothing in a board file can supply: **a current is
intent**. The net-intent manifest already carries a net's `current_a`, but a
net is not a path — a 6 A servo rail and the 20 mA sensor regulator hanging
off the same copper load it very differently, and which pads the current runs
between is the whole question for a drop or a via.

Claude writes this file from what the user says, exactly as it writes the
net-intent and thermal manifests; `python -m pcbreview.ampacity <project>
--template` prints a skeleton listing every power net's pads by part so the
terminals can be picked rather than typed. The user never edits it.

    environment: enclosed        # enclosed | still_air | forced_air | W/m^2K
    ambient_c: 40                # else the thermal manifest's, else 25
    mounts: {H1: 0.05}           # optional: W/K from a footprint to structure
    paths:
      - name: servo_rail
        net: "+VSERVO"
        from: [L2.2]                         # pads; U3.17-24 is a range
        to:
          - {pads: [J10.2], current_a: 6.0}
        rise_c: 20                           # allowed conductor rise, default 10
        max_drop_mv: 150                     # optional
        note: "servo stall, continuous for seconds"
      - name: igniter
        net: "/FIRE_OUT"
        from: [Q3.5]
        to: [{pads: [J7.3], current_a: 7.6}]
        pulse_s: 1.0                         # a pulse, not a continuous load
        rise_c: 60
    scenarios:                               # optional — what runs together
      - name: flight
        paths: [servo_rail]                  # or {servo_rail: 0.5} to scale
        parts: true                          # add thermal-manifest dissipation

**Put every load of one net in one path.** Two paths on the same net are
solved separately and their losses added, which misses the cross term of
``(I1 + I2)^2`` — the run says so whenever it happens.

The loader refuses rather than half-loads, on the pattern every other
manifest here follows: an unknown key is almost always a typo for a known one
(`current: 5` for `current_a: 5`), and a typo that silently became "no
current" would read as a checked path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pcbreview import boardheat

#: Allowed conductor temperature rise when a path states none. The IPC
#: charts' conventional working point and the same strict default `current_capacity`
#: uses; a pulsed or deliberately hot path should say what it may reach.
DEFAULT_RISE_C = 10.0

_TOP_KEYS = {"paths", "scenarios", "environment", "ambient_c", "mounts",
             "plating_um", "note"}
_PATH_KEYS = {"name", "net", "from", "to", "rise_c", "max_drop_mv", "pulse_s",
              "voltage_v", "note"}
_SINK_KEYS = {"pads", "current_a", "name"}
_SCENARIO_KEYS = {"name", "paths", "parts", "note"}


def default_path(pcb_path) -> Path:
    """`<board>.powerpaths.yaml`, beside the board — where `audit_files` looks."""
    pcb_path = Path(pcb_path)
    return pcb_path.with_name(pcb_path.stem + ".powerpaths.yaml")


class PowerPathError(Exception):
    """Raised when a power-path manifest cannot be loaded as written."""


@dataclass
class Sink:
    pads: list[str]
    current_a: float
    name: str = ""


@dataclass
class PowerPath:
    name: str
    net: str
    source: list[str]
    sinks: list[Sink]
    rise_c: float = DEFAULT_RISE_C
    rise_stated: bool = False
    max_drop_mv: float | None = None
    voltage_v: float | None = None
    pulse_s: float | None = None
    note: str | None = None

    @property
    def current_a(self) -> float:
        return sum(s.current_a for s in self.sinks)

    @property
    def is_pulse(self) -> bool:
        return self.pulse_s is not None


@dataclass
class Scenario:
    name: str
    #: path name -> current scale factor
    paths: dict[str, float]
    parts: bool = True
    note: str | None = None
    #: True when the run made it up because the manifest named none
    implied: bool = False


@dataclass
class PowerPathManifest:
    paths: list[PowerPath]
    scenarios: list[Scenario]
    environment: str | None = None
    surface_w_m2k: float | None = None
    ambient_c: float | None = None
    mounts: dict[str, float] = field(default_factory=dict)
    plating_um: float | None = None
    source_path: Path | None = None

    def path(self, name: str) -> PowerPath:
        for p in self.paths:
            if p.name == name:
                return p
        raise KeyError(name)


def _number(where: str, key: str, value, positive: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PowerPathError(
            f"{where} has {key}={value!r}, which is not a number. Give a bare "
            f"number with no unit") from exc
    if positive and number <= 0:
        raise PowerPathError(f"{where} has {key}={number}; it must be positive")
    return number


def _unknown(where: str, entry: dict, allowed: set[str]) -> None:
    extra = sorted(set(entry) - allowed)
    if extra:
        raise PowerPathError(
            f"{where} has unknown key(s) {extra}; allowed are "
            f"{sorted(allowed)}. A misspelt key is refused rather than ignored, "
            f"because an ignored `current_a` reads as a checked path")


def _pad_list(where: str, key: str, value) -> list[str]:
    if isinstance(value, (str, int, float)):
        value = [value]
    if not isinstance(value, list) or not value:
        raise PowerPathError(f"{where} needs `{key}` as a non-empty list of pads")
    return [str(v).strip() for v in value]


def load(path) -> PowerPathManifest:
    path = Path(path)
    if not path.exists():
        raise PowerPathError(f"power-path manifest not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "paths" not in data:
        raise PowerPathError(f"{path} is not a power-path manifest (needs 'paths')")
    _unknown(str(path), data, _TOP_KEYS)

    environment, surface = None, None
    if data.get("environment") is not None:
        env = data["environment"]
        if isinstance(env, str) and env in boardheat.ENVIRONMENTS:
            environment = env
            surface = boardheat.ENVIRONMENTS[env]
        else:
            try:
                surface = _number(str(path), "environment", env)
            except PowerPathError as exc:
                raise PowerPathError(
                    f"{path}: environment={env!r} must be one of "
                    f"{sorted(boardheat.ENVIRONMENTS)} or a surface coefficient "
                    f"in W/m^2K per face") from exc
            environment = f"{surface:g} W/m^2K"

    ambient = (_number(str(path), "ambient_c", data["ambient_c"], positive=False)
               if data.get("ambient_c") is not None else None)
    plating = (_number(str(path), "plating_um", data["plating_um"])
               if data.get("plating_um") is not None else None)
    mounts = {}
    for ref, value in (data.get("mounts") or {}).items():
        mounts[str(ref)] = _number(f"{path}: mount {ref}", "conductance", value)

    paths: list[PowerPath] = []
    for index, entry in enumerate(data["paths"] or []):
        where = f"{path}: path {index}"
        if not isinstance(entry, dict):
            raise PowerPathError(f"{where} is not a mapping")
        _unknown(where, entry, _PATH_KEYS)
        name = entry.get("name")
        if not name:
            raise PowerPathError(f"{where} has no `name`")
        where = f"{path}: path '{name}'"
        if any(p.name == name for p in paths):
            raise PowerPathError(f"{where} is declared twice")
        if not entry.get("net"):
            raise PowerPathError(f"{where} has no `net`")
        if "from" not in entry:
            raise PowerPathError(f"{where} has no `from` — the pads the "
                                 f"current leaves by")
        source = _pad_list(where, "from", entry["from"])
        raw_sinks = entry.get("to")
        if not isinstance(raw_sinks, list) or not raw_sinks:
            raise PowerPathError(f"{where} needs `to` as a list of "
                                 f"{{pads: [...], current_a: N}}")
        sinks = []
        for k, sink in enumerate(raw_sinks):
            if not isinstance(sink, dict):
                raise PowerPathError(f"{where}: sink {k} is not a mapping")
            _unknown(f"{where}: sink {k}", sink, _SINK_KEYS)
            if "current_a" not in sink:
                raise PowerPathError(
                    f"{where}: sink {k} states no `current_a`. A path with no "
                    f"current measures nothing and would read as checked")
            sinks.append(Sink(pads=_pad_list(f"{where}: sink {k}", "pads",
                                             sink.get("pads")),
                              current_a=_number(f"{where}: sink {k}",
                                                "current_a", sink["current_a"]),
                              name=str(sink.get("name") or f"sink{k + 1}")))
        rise = entry.get("rise_c")
        paths.append(PowerPath(
            name=str(name), net=str(entry["net"]), source=source, sinks=sinks,
            rise_c=(_number(where, "rise_c", rise) if rise is not None
                    else DEFAULT_RISE_C),
            rise_stated=rise is not None,
            max_drop_mv=(_number(where, "max_drop_mv", entry["max_drop_mv"])
                         if entry.get("max_drop_mv") is not None else None),
            voltage_v=(_number(where, "voltage_v", entry["voltage_v"])
                       if entry.get("voltage_v") is not None else None),
            pulse_s=(_number(where, "pulse_s", entry["pulse_s"])
                     if entry.get("pulse_s") is not None else None),
            note=entry.get("note")))
    if not paths:
        raise PowerPathError(f"{path} declares no paths")

    names = {p.name for p in paths}
    scenarios: list[Scenario] = []
    for index, entry in enumerate(data.get("scenarios") or []):
        where = f"{path}: scenario {index}"
        if not isinstance(entry, dict):
            raise PowerPathError(f"{where} is not a mapping")
        _unknown(where, entry, _SCENARIO_KEYS)
        name = entry.get("name") or f"scenario {index + 1}"
        listed = entry.get("paths")
        if isinstance(listed, list):
            chosen = {str(n): 1.0 for n in listed}
        elif isinstance(listed, dict):
            chosen = {str(n): _number(f"{where}: {n}", "scale", v)
                      for n, v in listed.items()}
        else:
            raise PowerPathError(f"{where} needs `paths` as a list of path "
                                 f"names or a mapping of name to scale")
        missing = sorted(set(chosen) - names)
        if missing:
            raise PowerPathError(f"{where} names path(s) {missing} that are "
                                 f"not declared; declared are {sorted(names)}")
        scenarios.append(Scenario(name=str(name), paths=chosen,
                                  parts=bool(entry.get("parts", True)),
                                  note=entry.get("note")))
    if not scenarios:
        scenarios = implied_scenarios(paths)

    return PowerPathManifest(paths=paths, scenarios=scenarios,
                             environment=environment, surface_w_m2k=surface,
                             ambient_c=ambient, mounts=mounts,
                             plating_um=plating, source_path=path)


def implied_scenarios(paths: list[PowerPath]) -> list[Scenario]:
    """What runs together when nobody said: every continuous path at once,
    and each pulse on top of that. All-at-once is the worst steady case the
    declared currents allow, which is the direction to err."""
    steady = {p.name: 1.0 for p in paths if not p.is_pulse}
    out = []
    if steady:
        out.append(Scenario(name="every continuous path together",
                            paths=dict(steady), implied=True))
    for p in paths:
        if p.is_pulse:
            out.append(Scenario(name=f"{p.name} pulse over every continuous path",
                                paths={**steady, p.name: 1.0}, implied=True))
    return out


# ---------------------------------------------------------------------------
# A skeleton to fill in
# ---------------------------------------------------------------------------

def template(board, nets: list[str]) -> str:
    """A manifest skeleton listing each net's pads by part, as comments.

    Printed, never written: the currents in it are placeholders, and a file
    of placeholders beside the board would be read on the next run as the
    user's statement.
    """
    lines = ["# power-path manifest — SKELETON. Every current_a below is a",
             "# placeholder: replace it with what the user said, or delete the",
             "# path. Save as <board>.powerpaths.yaml beside the board.",
             "environment: enclosed      # enclosed | still_air | forced_air",
             "paths:"]
    for net in nets:
        code = board.net(net).code
        by_ref: dict[str, list[str]] = {}
        for pad in board.pads:
            if pad.net == code:
                by_ref.setdefault(pad.ref, []).append(pad.number)
        if not by_ref:
            continue
        lines.append(f"  # {net}:")
        for ref in sorted(by_ref):
            numbers = sorted(by_ref[ref], key=lambda n: (len(n), n))
            lines.append(f"  #   {ref}: {', '.join(numbers)}")
        lines += [f"  - name: {net.strip('/').replace('/', '_').replace(' ', '_')}",
                  f"    net: \"{net}\"",
                  "    from: [REF.PAD]        # where the current enters",
                  "    to:",
                  "      - {pads: [REF.PAD], current_a: 0.0}   # PLACEHOLDER",
                  ""]
    return "\n".join(lines)
