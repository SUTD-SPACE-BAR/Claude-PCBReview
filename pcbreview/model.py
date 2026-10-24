"""The normalised board model.

Loads the JSON produced by :mod:`pcbreview.extract` and turns it into typed
objects backed by exact shapely geometry. Nothing here imports ``pcbnew``, so
this runs under a normal Python with numpy/scipy/shapely available.

The design rule for this module: expose *geometry*, not conclusions. Every
analysis in later phases builds on the primitives here, so they must be exact.
Arcs are tessellated to a stated sagitta tolerance; everything else is carried
through at full precision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from pcbreview.stackup import Stackup

# Maximum deviation between a tessellated arc and the true arc, in mm.
# 2 um is well below fabrication tolerance and keeps segment counts modest.
ARC_SAGITTA_TOLERANCE_MM = 0.002

#: Kept only so `PCBREVIEW_CACHE_DIR` and an explicit `cache_dir=` still have
#: a documented default to be compared against. **Nothing uses it as a
#: location any more** — see `review.cache_dir_for`, which puts the extraction
#: in the board's `review/` folder when there is one and in the OS temp
#: directory otherwise. It used to be here, in the *install* directory, where
#: it accumulated a full geometry dump of every board ever reviewed and never
#: pruned: after one outside review the installed skill held a 602 KB
#: extraction of a board belonging to a different company (§48.8, §49.1).
LEGACY_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

#: Ceilings on the two subprocesses this module shells out to. **Both were
#: unbounded until 2026-08-06**, which made them the only calls in the package
#: that could block forever — `drc.py` and `render.py` have always had one.
#:
#: This is not theoretical. A run here stalled for **forty minutes** having
#: accumulated 2.85 s of CPU: an infinite loop burns a core, a blocked
#: subprocess burns nothing, and that is how the two are told apart. A hang
#: with no ceiling is indistinguishable from a machine that has stopped, and
#: it takes the whole test suite and every gate with it.
#:
#: Generous on purpose — the slowest real board here extracts in about 40 s, so
#: 600 s is fifteen times the worst honest case and can only be reached by a
#: process that is stuck rather than busy.
EXTRACT_TIMEOUT_S = 600.0
#: `python -c "import pcbnew"` either works in a second or is the wrong
#: interpreter. Short, because this runs once per candidate on the discovery
#: path and a stuck candidate must not delay finding the good one.
PROBE_TIMEOUT_S = 60.0

#: The macOS location, kept as a named constant because it is the one this was
#: developed against and the error message quotes it.
DEFAULT_KICAD_PYTHON = (
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/3.9/bin/python3"
)

#: Where KiCad's bundled Python lives, per platform. Globs because the KiCad
#: version and the Python minor version are both in the path and neither is
#: fixed — KiCad 9 ships 3.9, and KiCad 11 will not.
_KICAD_PYTHON_GLOBS = {
    "darwin": [
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
        "Versions/*/bin/python3",
    ],
    "win32": [
        r"C:\Program Files\KiCad\*\bin\python.exe",
        r"C:\Program Files (x86)\KiCad\*\bin\python.exe",
    ],
    "linux": [
        "/usr/lib/kicad/bin/python3",
        "/usr/bin/python3",          # distro packages put pcbnew on the system one
        "/snap/kicad/current/usr/bin/python3",
        "/app/bin/python3",          # flatpak
    ],
}


def find_kicad_python(explicit: str | None = None) -> str:
    """Locate an interpreter that can ``import pcbnew``.

    The same shape as `drc.find_kicad_cli`, and for the same reason: this was
    hardcoded to one macOS path, which is fine on the machine it was written on
    and the first thing to break for anyone else.

    Candidates are *tested*, not just globbed, because on Linux the distro
    package puts `pcbnew` on the system Python and there is no way to tell that
    from the path alone. The environment variable wins over everything so a
    user with an unusual install has an escape hatch.
    """
    import glob

    from pcbreview.drc import newest_first

    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    from_env = os.environ.get("PCBREVIEW_KICAD_PYTHON")
    if from_env:
        candidates.append(from_env)
    for pattern in _KICAD_PYTHON_GLOBS.get(sys.platform, []):
        # `newest_first`, not `sorted(reverse=True)` — the latter picks KiCad
        # 9.0 over 10.0, because lexicographically "9" > "1". See its
        # docstring; that bug cost a Windows user their whole first run.
        candidates.extend(newest_first(glob.glob(pattern)))

    tried = []
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        tried.append(candidate)
        try:
            probe = subprocess.run([candidate, "-c", "import pcbnew"],
                                   capture_output=True, text=True,
                                   timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # An interpreter that hangs on `import pcbnew` is not a usable
            # one, and waiting on it forever is worse than moving to the next
            # candidate. Unbounded until 2026-08-06 — see EXTRACT_TIMEOUT_S.
            continue
        if probe.returncode == 0:
            return candidate

    raise BoardError(
        "no Python with `pcbnew` found. KiCad ships its own interpreter and "
        "that is the only one that can read a board; the analysis side needs "
        "a different one with shapely and numpy, which is why there are two.\n"
        + (f"  tried: {', '.join(tried)}\n" if tried else
           f"  looked for: {DEFAULT_KICAD_PYTHON}\n")
        + "  fix: set PCBREVIEW_KICAD_PYTHON, or pass kicad_python=")


class BoardError(Exception):
    """Raised when a board cannot be loaded or is internally inconsistent."""


# -- geometry helpers -----------------------------------------------------

@dataclass(frozen=True)
class ArcGeometry:
    """A three-point arc resolved to centre, radius and signed sweep."""
    centre: tuple[float, float]
    radius: float
    start_angle: float
    sweep: float          # signed, radians; positive is CCW in board coords

    @property
    def length(self) -> float:
        return abs(self.sweep) * self.radius


def resolve_arc(start: tuple[float, float], mid: tuple[float, float],
                end: tuple[float, float]) -> ArcGeometry | None:
    """Resolve KiCad's start/mid/end arc representation.

    Returns None for degenerate arcs (collinear or coincident points), which do
    occur on real boards; callers treat those as straight segments.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end

    # Circumcentre of the three points.
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None

    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1)
          + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3)
          + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    radius = math.hypot(x1 - ux, y1 - uy)
    if radius < 1e-9:
        return None

    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    two_pi = 2 * math.pi
    sweep_ccw = (a3 - a1) % two_pi
    mid_ccw = (a2 - a1) % two_pi

    # Pick the direction whose sweep actually passes through the midpoint.
    if mid_ccw <= sweep_ccw:
        sweep = sweep_ccw
    else:
        sweep = sweep_ccw - two_pi

    if abs(sweep) < 1e-12:
        return None
    return ArcGeometry((ux, uy), radius, a1, sweep)


def arc_points(start: tuple[float, float], mid: tuple[float, float],
               end: tuple[float, float],
               tolerance: float = ARC_SAGITTA_TOLERANCE_MM
               ) -> list[tuple[float, float]]:
    """Tessellate a three-point arc into a polyline.

    Segment count is chosen so the sagitta stays under ``tolerance``. Note that
    the polyline is for *area* operations — arc length is computed analytically
    by :meth:`Track.length_mm`, since chords always understate a curve.
    """
    arc = resolve_arc(start, mid, end)
    if arc is None:
        return [start, end]

    total = abs(arc.sweep)
    ratio = max(-1.0, min(1.0, 1.0 - tolerance / arc.radius))
    max_step = 2 * math.acos(ratio) if arc.radius > tolerance else total
    steps = max(2, int(math.ceil(total / max(max_step, 1e-6))))

    cx, cy = arc.centre
    points = [
        (cx + arc.radius * math.cos(arc.start_angle + arc.sweep * i / steps),
         cy + arc.radius * math.sin(arc.start_angle + arc.sweep * i / steps))
        for i in range(steps + 1)
    ]
    points[0], points[-1] = start, end
    return points


def _polys_from_json(entries: list[dict]) -> list[Polygon]:
    polygons = []
    for entry in entries:
        outer = entry["outer"]
        if len(outer) < 3:
            continue
        holes = [h for h in entry.get("holes", []) if len(h) >= 3]
        try:
            polygon = Polygon(outer, holes)
        except Exception:
            continue
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_empty:
            polygons.append(polygon)
    return polygons


def _as_multipolygon(geoms) -> MultiPolygon:
    """Normalise any union result to a MultiPolygon for a stable return type."""
    if not geoms:
        return MultiPolygon()
    merged = unary_union(geoms) if not hasattr(geoms, "geom_type") else geoms
    if merged.is_empty:
        return MultiPolygon()
    if merged.geom_type == "Polygon":
        return MultiPolygon([merged])
    if merged.geom_type == "MultiPolygon":
        return merged
    parts = [g for g in getattr(merged, "geoms", []) if g.geom_type == "Polygon"]
    return MultiPolygon(parts)


# -- model objects --------------------------------------------------------

@dataclass
class Net:
    code: int
    name: str
    netclass: str | None = None


@dataclass
class Track:
    """One routed segment or arc. Width is constant along a single Track."""
    id: int
    net: int
    layer: str
    width_mm: float
    kind: str
    start: tuple[float, float]
    end: tuple[float, float]
    mid: tuple[float, float] | None = None
    #: The net's name, resolved at load time from the board's net table.
    #: `Pad` and `Zone` have always carried this and `Track` and `Via` did
    #: not, which is the sharpest edge in the model: getting `pad.net_name`
    #: right first teaches you the wrong lesson about tracks, and two separate
    #: outside sessions rebuilt a `{code: name}` map by hand because of it.
    net_name: str = ""
    #: step_mm -> (points, realised step); see `sample_points`
    _samples: dict = field(default_factory=dict, repr=False, compare=False)

    @cached_property
    def arc(self) -> "ArcGeometry | None":
        if self.kind == "arc" and self.mid is not None:
            return resolve_arc(self.start, self.mid, self.end)
        return None

    @cached_property
    def centreline(self) -> LineString:
        if self.kind == "arc" and self.mid is not None:
            return LineString(arc_points(self.start, self.mid, self.end))
        return LineString([self.start, self.end])

    @cached_property
    def length_mm(self) -> float:
        """Exact routed length.

        Arcs use r*theta rather than the tessellated polyline: chords always
        understate a curve, and length feeds skew and delay budgets where a
        systematic bias is worse than a random one.
        """
        arc = self.arc
        return arc.length if arc is not None else self.centreline.length

    @cached_property
    def copper(self) -> Polygon:
        """The actual copper footprint, with round caps as KiCad renders it."""
        return self.centreline.buffer(self.width_mm / 2.0, cap_style="round",
                                      join_style="round", quad_segs=16)

    def sample_points(self, step_mm: float) -> tuple[object, float]:
        """Evenly spaced points along the centreline, and the step actually used.

        Where the track is sampled depends only on the track and the step, never
        on what it is being measured against — so a net that is a victim to
        twenty aggressors interpolates its centreline once rather than twenty
        times. `line_interpolate_point` was the single most-called function in
        the crosstalk profile (247 855 calls over 40 net pairs) purely because
        this was recomputed per pair.

        The step is returned because the count is rounded up to a whole number
        of intervals, so the realised spacing is slightly under the request.
        """
        cached = self._samples.get(step_mm)
        if cached is None:
            line = self.centreline
            count = max(2, int(math.ceil(line.length / step_mm)) + 1)
            positions = np.linspace(0.0, line.length, count)
            points = shapely.points(np.array(
                [line.interpolate(float(d)).coords[0] for d in positions]))
            cached = (points, line.length / (count - 1))
            self._samples[step_mm] = cached
        return cached


@dataclass
class Via:
    id: int
    net: int
    pos: tuple[float, float]
    drill_mm: float
    via_type: str
    top_layer: str
    bottom_layer: str
    layers: list[str]
    width_by_layer: dict[str, float]
    #: as `Track.net_name`
    net_name: str = ""

    def width_on(self, layer: str) -> float:
        if layer in self.width_by_layer:
            return self.width_by_layer[layer]
        return max(self.width_by_layer.values()) if self.width_by_layer else self.drill_mm

    def pad_on(self, layer: str) -> Polygon:
        return Point(self.pos).buffer(self.width_on(layer) / 2.0, quad_segs=24)

    @cached_property
    def barrel(self) -> Polygon:
        return Point(self.pos).buffer(self.drill_mm / 2.0, quad_segs=24)

    @property
    def is_through(self) -> bool:
        return self.via_type == "through"


@dataclass
class Pad:
    ref: str
    number: str
    net: int
    net_name: str
    shape: str
    attribute: str
    pos: tuple[float, float]
    size_mm: tuple[float, float]
    orientation_deg: float
    drill_mm: tuple[float, float]
    layers: list[str]
    copper_layers: list[str]
    _polygon: list[dict] = field(default_factory=list, repr=False)

    @property
    def designator(self) -> str:
        return f"{self.ref}.{self.number}"

    @property
    def is_smd(self) -> bool:
        return self.attribute == "smd"

    @property
    def is_plated_hole(self) -> bool:
        return self.attribute in ("pth", "conn")

    @cached_property
    def copper(self) -> Polygon:
        """Exact pad outline as KiCad computes it, including roundrect corners."""
        polygons = _polys_from_json(self._polygon)
        if polygons:
            return polygons[0] if len(polygons) == 1 else unary_union(polygons)
        # Fall back to the bounding shape if KiCad gave us nothing usable.
        width, height = self.size_mm
        if self.shape == "circle":
            return Point(self.pos).buffer(width / 2.0, quad_segs=24)
        angle = math.radians(self.orientation_deg)
        cx, cy = self.pos
        corners = []
        for dx, dy in ((-width / 2, -height / 2), (width / 2, -height / 2),
                       (width / 2, height / 2), (-width / 2, height / 2)):
            corners.append((cx + dx * math.cos(angle) - dy * math.sin(angle),
                            cy + dx * math.sin(angle) + dy * math.cos(angle)))
        return Polygon(corners)

    def on_layer(self, layer: str) -> bool:
        return layer in self.copper_layers


@dataclass
class Footprint:
    ref: str
    value: str
    lib_id: str
    pos: tuple[float, float]
    orientation_deg: float
    layer: str
    pads: list[Pad]


@dataclass
class Zone:
    id: int
    net: int
    net_name: str
    name: str
    is_rule_area: bool
    priority: int
    min_thickness_mm: float
    layers: list[str]
    _outline: list[dict] = field(default_factory=list, repr=False)
    _fills: dict[str, list[dict]] = field(default_factory=dict, repr=False)
    _fill_cache: dict = field(default_factory=dict, repr=False, compare=False)

    @cached_property
    def outline(self) -> MultiPolygon:
        return _as_multipolygon(_polys_from_json(self._outline))

    @property
    def area_mm2(self):
        """Deliberately absent — raises, rather than not existing.

        A zone's copper area is per *layer*, so there is no single number to
        return. A reviewer wrapped `zn.area_mm2` in a bare `try/except` and got
        a clean table of `0.0 mm²` for every pour on every layer, which reads
        as "this board has no copper" rather than "you used the wrong
        attribute". They caught it only because *every* value was zero.

        A missing attribute survives a defensive wrapper; a pointed one does
        not.
        """
        raise AttributeError(
            "Zone has no area_mm2 — a zone's copper is per layer. Use "
            "zone.fill_on(layer).area for the poured copper on one layer, or "
            "zone.outline.area for the drawn boundary.")

    def fill_on(self, layer: str) -> MultiPolygon:
        """Actual poured copper on a layer — islands and thermal cutouts intact.

        This is the geometry that matters for return paths. The zone *outline*
        is where copper was requested; the fill is where it landed.

        Memoised: rebuilding the polygons from the extraction JSON is the single
        largest cost under `Board.zone_fill`, and the analysis asks for the same
        layer's pour thousands of times over one survey.
        """
        if layer not in self._fill_cache:
            self._fill_cache[layer] = _as_multipolygon(
                _polys_from_json(self._fills.get(layer, [])))
        return self._fill_cache[layer]

    @property
    def is_filled(self) -> bool:
        return bool(self._fills)


# -- the board ------------------------------------------------------------

class Board:
    """A KiCad board as exact geometry, indexed for spatial queries."""

    def __init__(self, data: dict, source_path: Path | None = None):
        self._data = data
        self.source_path = source_path
        self.kicad_version: str = data["source"].get("kicad_version", "unknown")
        self.thickness_mm: float = data["board"]["thickness_mm"]
        self.copper_layers: list[str] = [l["name"] for l in data["copper_layers"]]

        self.nets: dict[int, Net] = {
            n["code"]: Net(n["code"], n["name"], n.get("netclass"))
            for n in data["nets"]
        }
        self._net_by_name: dict[str, Net] = {n.name: n for n in self.nets.values()}
        self.netclasses: dict[str, dict] = data.get("netclasses", {})

        #: KiCad's own measurements of **this extraction's** geometry — outline
        #: area, per-zone fill area, per-net track length, per-pad copper area.
        #: Written by `extract.kicad_measurements` in the same process and after
        #: the same `refill_zones`, which is the whole point: comparing our
        #: shapely reconstruction against a *separately refilled* board meant
        #: the two could describe different copper whenever KiCad's zone filler
        #: returned a different polygonisation (HANDOFF §40). Empty for a cache
        #: written before schema 2.
        self.kicad_measurements: dict = data.get("kicad_measurements", {})

        # Resolved here rather than added to the extraction, so no cache is
        # invalidated and KiCad is not involved. Net 0 is the unconnected
        # pseudo-net and has no entry, hence the `.get`.
        _name_of = {code: net.name for code, net in self.nets.items()}
        self.tracks: list[Track] = [
            Track(id=t["id"], net=t["net"], layer=t["layer"],
                  width_mm=t["width_mm"], kind=t["kind"],
                  start=tuple(t["start"]), end=tuple(t["end"]),
                  mid=tuple(t["mid"]) if t.get("mid") else None,
                  net_name=_name_of.get(t["net"], ""))
            for t in data["tracks"]
        ]
        self.vias: list[Via] = [
            Via(id=v["id"], net=v["net"], pos=tuple(v["pos"]),
                drill_mm=v["drill_mm"], via_type=v["via_type"],
                top_layer=v["top_layer"], bottom_layer=v["bottom_layer"],
                layers=v["layers"], width_by_layer=v["width_by_layer"],
                net_name=_name_of.get(v["net"], ""))
            for v in data["vias"]
        ]

        self.footprints: list[Footprint] = []
        self.pads: list[Pad] = []
        for fp in data["footprints"]:
            pads = [
                Pad(ref=fp["ref"], number=p["number"], net=p["net"],
                    net_name=p["net_name"], shape=p["shape"],
                    attribute=p["attribute"], pos=tuple(p["pos"]),
                    size_mm=tuple(p["size_mm"]),
                    orientation_deg=p["orientation_deg"],
                    drill_mm=tuple(p["drill_mm"]), layers=p["layers"],
                    copper_layers=p["copper_layers"], _polygon=p["polygon"])
                for p in fp["pads"]
            ]
            self.footprints.append(Footprint(
                ref=fp["ref"], value=fp["value"], lib_id=fp["lib_id"],
                pos=tuple(fp["pos"]), orientation_deg=fp["orientation_deg"],
                layer=fp["layer"], pads=pads))
            self.pads.extend(pads)

        self.zones: list[Zone] = [
            Zone(id=z["id"], net=z["net"], net_name=z["net_name"], name=z["name"],
                 is_rule_area=z["is_rule_area"], priority=z["priority"],
                 min_thickness_mm=z["min_thickness_mm"], layers=z["layers"],
                 _outline=z["outline"], _fills=z["fills"])
            for z in data["zones"]
        ]

        self.connectivity: dict = data.get("connectivity", {})
        self.stackup: Stackup | None = None

        # Per-net indexes. Every one of these was a linear scan of the whole
        # board, and the coupling survey asks for them once per *net pair* —
        # 3297 tracks scanned 1678 times on the board this was measured on.
        self._tracks_by_net: dict[int, list[Track]] = {}
        for track in self.tracks:
            self._tracks_by_net.setdefault(track.net, []).append(track)
        self._vias_by_net: dict[int, list[Via]] = {}
        for via in self.vias:
            self._vias_by_net.setdefault(via.net, []).append(via)
        self._pads_by_net: dict[int, list[Pad]] = {}
        for pad in self.pads:
            self._pads_by_net.setdefault(pad.net, []).append(pad)

        #: Scratch space for derived geometry that is a pure function of this
        #: board — see `memo`. Populated lazily by the analysis modules.
        self._memo: dict = {}

    def memo(self, key, build):
        """Memoise a pure function of this board's immutable geometry.

        The board is read-only once loaded, so anything derived from it can be
        computed once and shared. Used for the expensive unions the coupling
        survey rebuilds per net pair — plane copper, ground copper, per-net
        centrelines. Deliberately a plain dict keyed by the caller: the values
        are large geometries and the owning module knows what identifies them.
        """
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    # -- construction -----------------------------------------------------

    @classmethod
    def from_json(cls, path: str | Path) -> "Board":
        path = Path(path)
        if not path.exists():
            raise BoardError(f"extraction not found: {path}")
        return cls(json.loads(path.read_text(encoding="utf-8")), source_path=path)

    @classmethod
    def from_kicad(cls, pcb_path: str | Path, cache_dir: str | Path | None = None,
                   kicad_python: str | None = None, refresh: bool = False) -> "Board":
        """Extract a .kicad_pcb via KiCad's Python, then load the result.

        The extraction is cached and reused unless the board is newer, because
        it costs about a second and gets called repeatedly during a review.

        **Where the cache lives is a ruling, not a convenience** (§49.1). It
        goes in the board's `review/` folder when the user has one — theirs to
        inspect and delete — and in the OS temp directory when they do not. It
        never goes in the install directory again: sitting there, it silently
        turned the skill folder into a collection of other people's designs.

        Entries are keyed by a hash of the full board path, so identically
        named boards in different projects do not collide.
        """
        pcb_path = Path(pcb_path)
        if not pcb_path.exists():
            raise BoardError(f"board not found: {pcb_path}")

        from pcbreview import review as review_module
        cache_dir = (Path(cache_dir) if cache_dir
                     else review_module.cache_dir_for(pcb_path))
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(pcb_path.resolve()).encode()).hexdigest()[:12]
        json_path = cache_dir / f"{pcb_path.stem}.{digest}.json"

        # Invalidate on the extractor's own mtime too, not just the board's:
        # changing extraction logic must not silently reuse an old dump.
        script = Path(__file__).with_name("extract.py")
        newest_input = max(pcb_path.stat().st_mtime, script.stat().st_mtime)
        stale = (refresh or not json_path.exists()
                 or json_path.stat().st_mtime < newest_input)
        if stale:
            interpreter = find_kicad_python(kicad_python)
            try:
                result = subprocess.run(
                    [interpreter, str(script), str(pcb_path), str(json_path)],
                    capture_output=True, text=True,
                    timeout=EXTRACT_TIMEOUT_S)
            except subprocess.TimeoutExpired as exc:
                raise BoardError(
                    f"extraction timed out after {EXTRACT_TIMEOUT_S:.0f}s on "
                    f"{pcb_path.name}. KiCad's own Python stopped making "
                    f"progress — this is not a slow board, it is a stuck "
                    f"process. Try again; if it repeats, run the extractor by "
                    f"hand:\n"
                    f"  {interpreter} {script} {pcb_path} /tmp/out.json") from exc
            if result.returncode != 0:
                raise BoardError(
                    f"extraction failed for {pcb_path.name}:\n{result.stderr}")
        return cls.from_json(json_path)

    def bind_stackup(self, stackup: Stackup) -> None:
        """Attach a validated stackup. Physics queries require this."""
        resolved = [stackup.resolve_layer(l.name) for l in stackup.copper_layers]
        if resolved != self.copper_layers:
            raise BoardError(
                f"stackup layers {resolved} do not match board layers "
                f"{self.copper_layers}")
        self.stackup = stackup

    # -- lookups ----------------------------------------------------------

    def net(self, key: int | str) -> Net:
        if isinstance(key, int):
            if key not in self.nets:
                raise KeyError(f"no net with code {key}")
            return self.nets[key]
        if key not in self._net_by_name:
            raise KeyError(f"no net named {key!r}")
        return self._net_by_name[key]

    def tracks_on_net(self, key: int | str) -> list[Track]:
        """The net's tracks, in board order. **Shared — do not mutate.**"""
        return self._tracks_by_net.get(self.net(key).code, [])

    def vias_on_net(self, key: int | str) -> list[Via]:
        return self._vias_by_net.get(self.net(key).code, [])

    def pads_on_net(self, key: int | str) -> list[Pad]:
        return self._pads_by_net.get(self.net(key).code, [])

    def zones_on_net(self, key: int | str) -> list[Zone]:
        code = self.net(key).code
        return [z for z in self.zones if z.net == code and not z.is_rule_area]

    def layer_index(self, layer: str) -> int:
        try:
            return self.copper_layers.index(layer)
        except ValueError as exc:
            raise KeyError(f"{layer!r} is not a copper layer on this board") from exc

    # -- geometry ---------------------------------------------------------

    @cached_property
    def outline(self) -> MultiPolygon:
        return _as_multipolygon(_polys_from_json(
            self._data["board"]["outline"].get("polygons", [])))

    def zone_fill(self, layer: str, net: int | str | None = None) -> MultiPolygon:
        """Poured zone copper on a layer, optionally restricted to one net.

        Memoised on `(layer, net code)`. Return-path, clearance and coupling
        analysis all ask for the same handful of pours over and over.
        """
        code = self.net(net).code if net is not None else None

        def build():
            parts = []
            for zone in self.zones:
                if zone.is_rule_area or (code is not None and zone.net != code):
                    continue
                fill = zone.fill_on(layer)
                if not fill.is_empty:
                    parts.append(fill)
            return _as_multipolygon(parts)

        return self.memo(("zone_fill", layer, code), build)

    def copper_on_layer(self, layer: str, net: int | str | None = None
                        ) -> MultiPolygon:
        """Every piece of copper on a layer: tracks, pads, via lands, zone fill.

        Used by reference-plane and clearance analysis, where a trace running
        over a wide power *track* is just as referenced as one over a zone.
        """
        code = self.net(net).code if net is not None else None
        parts: list = []
        for track in self.tracks:
            if track.layer == layer and (code is None or track.net == code):
                parts.append(track.copper)
        for via in self.vias:
            if layer in via.layers and (code is None or via.net == code):
                parts.append(via.pad_on(layer))
        for pad in self.pads:
            if pad.on_layer(layer) and (code is None or pad.net == code):
                parts.append(pad.copper)
        fill = self.zone_fill(layer, net)
        if not fill.is_empty:
            parts.append(fill)
        return _as_multipolygon(parts)

    def index_on_layer(self, layer: str) -> tuple[STRtree, list]:
        """Spatial index over individual copper items on a layer.

        Returns the tree plus the item list it indexes, since shapely 2's
        STRtree returns positional indices rather than the objects.
        """
        items: list = []
        geoms: list = []
        for track in self.tracks:
            if track.layer == layer:
                items.append(track)
                geoms.append(track.copper)
        for via in self.vias:
            if layer in via.layers:
                items.append(via)
                geoms.append(via.pad_on(layer))
        for pad in self.pads:
            if pad.on_layer(layer):
                items.append(pad)
                geoms.append(pad.copper)
        return STRtree(geoms), items

    # -- summary ----------------------------------------------------------

    def summary(self) -> dict:
        return {
            "layers": self.copper_layers,
            "thickness_mm": self.thickness_mm,
            "nets": len(self.nets),
            "tracks": len(self.tracks),
            "vias": len(self.vias),
            "pads": len(self.pads),
            "footprints": len(self.footprints),
            "zones": len([z for z in self.zones if not z.is_rule_area]),
            "rule_areas": len([z for z in self.zones if z.is_rule_area]),
            "unconnected": self.connectivity.get("unconnected_count"),
            "kicad_version": self.kicad_version,
        }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: python -m pcbreview.model <board.kicad_pcb>\n")
        return 2
    board = Board.from_kicad(argv[1])
    for key, value in board.summary().items():
        print(f"{key:>16}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
