"""Everything the engine knows about one object, ready before it is asked for.

The user's own words for what this tab is (HANDOFF §1):

> when I hover over a trace/pour/component, it tells me everything that
> pcbreview can possibly tell me about it… So instead of AI deciding what
> traces are important to run analysis on, we just run analysis on EVERYTHING,
> and the user can decide which traces to look at.

`audit.net_dossier()` already assembles most of this — and returns a **string**,
which serves a console and nothing else. This is the same assembly as data, and
it calls the same engine functions to get it: `current.ampacity` for every
ampacity, `audit._copper_oz_by_layer` for the copper weights that feed it,
`board.zone_fill` for poured area. Nothing here re-derives a number the engine
already computes. `net_dossier`'s own docstring records what happens when
something does — an earlier draft of it recomputed ampacity and *"immediately
disagreed with the finding by 0.02 A"*.

Where the precompute boundary actually is
-----------------------------------------
The ruling (HANDOFF §8) is that hover is pure retrieval: nothing is computed
*because* you hovered. What that has to mean in practice, stated plainly rather
than blurred:

* **Precomputed, at build time.** Every net's `Ampacity` — the IPC-2221 closed
  form over the whole board, which the caching note §1 lists as one of the two gaps to
  close for full hover coverage. Every net's poured copper, pads, vias, and the
  findings naming it. This is the part that costs real time, and it is paid
  once, up front, for every net rather than for the ones somebody guessed were
  interesting.
* **Assembled on request.** Turning a precomputed net dossier plus an already
  extracted `Track` into inspector rows. That is dictionary building over
  values that all exist — no geometry, no solve, no engine call — and it is
  microseconds. Precomputing it as well would mean holding a dossier for every
  one of a four-layer board's 6 400 objects to save nothing measurable.

The distinction that matters is not "is any Python executed", it is **can a
hover make the app think**. It cannot: there is no path from here into
`fields.solve`, and the field solver is the only thing on this board that is
slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pcbreview import current as current_module              # noqa: E402
from pcbreview import physics as physics_module              # noqa: E402
# Private by name and public by purpose: its docstring says it was lifted out
# of `audit()` **so that the net dossier computes ampacity from the same
# weights every finding does**. This is that caller.
from pcbreview.audit import _copper_oz_by_layer              # noqa: E402

from pcbreview.report.generate import finding_ref               # noqa: E402
from pcbreview.report.model import LAYOUT, SCHEMATIC            # noqa: E402


@dataclass(frozen=True)
class Row:
    """One line in the inspector.

    `basis` is the derivation and `assumption` is what was taken rather than
    measured; both are carried per row because principle 3 of the design brief §2.10
    is that **every number is traceable in one click**, and a row whose basis
    lives somewhere else is not.
    """
    label: str
    value: str
    unit: str = ""
    basis: str = ""
    assumption: str = ""

    def as_dict(self) -> dict:
        out = {"label": self.label, "value": self.value}
        for name in ("unit", "basis", "assumption"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        return out


@dataclass
class NetDossier:
    """One net, measured. Built once for every net on the board."""
    name: str
    code: int
    netclass: str | None = None
    rows: list[Row] = field(default_factory=list)
    routed: list[dict] = field(default_factory=list)
    poured: dict = field(default_factory=dict)
    pads: list[str] = field(default_factory=list)
    findings: list = field(default_factory=list)
    #: `current.Ampacity`, kept whole so an object dossier can quote the one
    #: (layer, width) row that applies to it rather than the net's worst.
    ampacity: object = None

    def as_dict(self) -> dict:
        return {
            "name": self.name, "code": self.code, "netclass": self.netclass,
            "rows": [r.as_dict() for r in self.rows],
            "routed": self.routed, "poured": self.poured, "pads": self.pads,
            "findings": [_finding_dict(f) for f in self.findings],
        }


def _finding_dict(ref) -> dict:
    """A `FindingRef` as JSON, without the engine's `measurement` blob.

    The inspector shows a finding to explain the object under the cursor, and
    `measurement` is an arbitrary engine-internal dict — sometimes a coverage
    census, sometimes a float (`return_path` writes one), and occasionally
    large. The report carries it in full; this view carries what is displayable
    and says which check to read for the rest.
    """
    return {
        "source": ref.source, "check": ref.check, "severity": ref.severity,
        "title": ref.title, "score": round(ref.score, 2),
        "nets": list(ref.nets), "layers": list(ref.layers),
        "location": list(ref.location) if ref.location else None,
        "detail": list(ref.detail), "basis": ref.basis,
        "assumption": ref.assumption, "intent_needed": ref.intent_needed,
        "demoted_from": ref.demoted_from, "demoted_by": ref.demoted_by,
    }


def _fmt(value, places: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{places}f}"


# ---------------------------------------------------------------------------
# Net dossiers — the precomputed half
# ---------------------------------------------------------------------------

def _net_rows(board, net, measured) -> list[Row]:
    """The headline numbers for a net, in the order a designer reads them."""
    rows: list[Row] = []
    outer = (board.copper_layers[0], board.copper_layers[-1])

    if measured.weakest_a is not None:
        rows.append(Row(
            "carries", _fmt(measured.weakest_a, 2), "A",
            basis=(f"IPC-2221 at a {measured.rise_c:.0f} °C rise over the "
                   f"weakest significant stretch — {_fmt(measured.weakest_mm)} "
                   f"mm on {measured.weakest_layer}. Weakest by CAPACITY, not "
                   f"by width: an inner layer is thinner copper"),
            assumption=("the copper weight comes from the fab stackup where it "
                        "states one, else 1 oz outer / 0.5 oz inner")))
    if (measured.weakest_any_a is not None and measured.weakest_a is not None
            and measured.weakest_any_a < measured.weakest_a):
        rows.append(Row(
            "weaker over a short stretch", _fmt(measured.weakest_any_a, 2), "A",
            basis=(f"{_fmt(measured.weakest_any_mm)} mm on "
                   f"{measured.weakest_any_layer}, "
                   f"{_fmt(measured.weakest_any_length_mm, 2)} mm long. Every "
                   f"series stretch carries the whole current, so this really "
                   f"is the limit; the headline above drops stretches too "
                   f"short to act on")))
    if measured.via_capacity_a is not None:
        drills = sorted({round(v.drill_mm, 3)
                         for v in board.vias_on_net(net.code)})
        rows.append(Row(
            "vias in parallel", _fmt(measured.via_capacity_a, 2), "A",
            basis=(f"{measured.vias} barrel(s), drill "
                   f"{', '.join(_fmt(d) for d in drills)} mm, as IPC-2221 "
                   f"conductors of circumference width. Deliberately NOT "
                   f"combined with the track figure: which vias are in series "
                   f"with which stretch is a topology question this does not "
                   f"answer"),
            assumption=(f"{current_module.VIA_PLATING_UM:.0f} µm barrel "
                        f"plating, because the fab has not said")))
    if measured.poured_mm2:
        rows.append(Row("poured copper", _fmt(measured.poured_mm2, 1), "mm²",
                        basis="filled zone area summed over every layer"))

    tracks = board.tracks_on_net(net.code)
    if tracks:
        total = sum(t.length_mm for t in tracks)
        layers = sorted({t.layer for t in tracks},
                        key=lambda n: board.layer_index(n))
        rows.append(Row(
            "routed length", _fmt(total, 2), "mm",
            basis=(f"{len(tracks)} segment(s) on {', '.join(layers)}; arcs "
                   f"measured as r·θ, not as their chords")))
    elif not measured.poured_mm2:
        rows.append(Row("routed copper", "none",
                        basis="this net has no track segments and no pour"))

    if outer and net.netclass:
        rows.append(Row("net class", net.netclass,
                        basis="KiCad's own net class assignment"))
    return rows


def _build_net(board, net, findings_by_net, copper_oz) -> NetDossier:
    measured = current_module.ampacity(board, net.name, copper_oz)

    routed = [
        {"layer": layer, "width_mm": width, "length_mm": length, "amps": amps}
        for (layer, width), (length, amps) in sorted(
            measured.by_width.items(), key=lambda kv: -kv[1][0])]

    poured = {}
    for layer in board.copper_layers:
        area = board.zone_fill(layer, net.code).area
        if area > 0:
            poured[layer] = round(area, 2)

    by_ref: dict[str, list[str]] = {}
    for pad in board.pads_on_net(net.code):
        by_ref.setdefault(pad.ref, []).append(pad.number)
    pads = [f"{ref}.{'/'.join(sorted(numbers))}"
            for ref, numbers in sorted(by_ref.items())]

    return NetDossier(
        name=net.name, code=net.code, netclass=net.netclass,
        rows=_net_rows(board, net, measured), routed=routed, poured=poured,
        pads=pads, findings=findings_by_net.get(net.name, []),
        ampacity=measured)


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------

@dataclass
class TraceIndex:
    """The board's traces, numbered once so a drawn object can name one.

    `topology.build_traces` numbers a trace within its own net; the canvas
    needs one integer per object across the whole board, so this flattens the
    per-net lists and keeps the two lookups the app actually performs:
    **which trace is this track part of**, and **give me that trace**.

    `of_via` holds only vias *inside* a run. A via where three conductors meet
    belongs to all of them, and answering "which trace is this via" with one of
    them would be a guess presented as a fact.
    """
    traces: list = field(default_factory=list)
    of_track: dict = field(default_factory=dict)
    of_via: dict = field(default_factory=dict)
    #: tracks split across more than one trace, where `of_track` names the one
    #: holding the longest strand. Measured on the corpus: 8 of 1107 on the V3
    #: two-layer board, 30 of 3297 on a four-layer one, 8 of 350 on a small one.
    split_tracks: int = 0
    seconds: float = 0.0

    def get(self, number: int | None):
        if number is None or not (0 <= number < len(self.traces)):
            return None
        return self.traces[number]


def build_traces(board, copper_oz: dict | None = None) -> TraceIndex:
    """Decompose every net's routed copper, and index it by drawn object.

    Cheap enough to sit in the index rather than behind a request: 0.34 s on
    a four-layer board's 3297 tracks, 0.17 s on a six-layer one's 1107. Hover stays
    pure retrieval (HANDOFF §8), which it would not if this ran on demand.
    """
    import time
    from pcbreview import topology as topology_module

    started = time.time()
    weights = copper_oz if copper_oz is not None else _copper_oz_by_layer(board)
    # By object identity, not by `Track.id`: that is KiCad's id and the payload
    # and the hit index both key on the **list position**, which is the trap
    # `geometry.py` records for its own object ids.
    track_index = {id(track): position
                   for position, track in enumerate(board.tracks)}
    via_index = {id(via): position for position, via in enumerate(board.vias)}

    index = TraceIndex()
    longest: dict[int, float] = {}
    for net_traces in topology_module.build_all_traces(board, weights).values():
        for trace in net_traces:
            number = len(index.traces)
            index.traces.append(trace)
            by_track: dict[int, float] = {}
            for strand in trace.strands:
                position = track_index.get(id(strand.track))
                if position is None:
                    continue
                by_track[position] = by_track.get(position, 0.0) + strand.length_mm
            for position, length in by_track.items():
                # A track cut by a junction lies on two runs. The canvas has
                # one field per object, so it names the run that owns most of
                # the track — and `split_tracks` counts how often that is an
                # approximation rather than the whole truth.
                if position in index.of_track:
                    index.split_tracks += 1
                    if length <= longest.get(position, 0.0):
                        continue
                index.of_track[position] = number
                longest[position] = length
            for via_id in trace.via_ids:
                via = next((v for v in board.vias if v.id == via_id), None)
                if via is not None and id(via) in via_index:
                    index.of_via[via_index[id(via)]] = number
    index.seconds = time.time() - started
    return index


def _trace_dict(trace, board) -> dict:
    """The trace block the inspector shows above the net block.

    Every number here comes from `topology.Trace`, which computed them from the
    same `physics.current_capacity_a` the net-level ampacity uses — verified on
    the V3 board, where the weakest trace on a net equals that net's
    `ampacity().weakest_any_a` to the digit on every net checked.
    """
    rows = [
        Row("runs between", trace.name,
            basis=("a trace is one edge of the net's copper graph — the run "
                   "between two pads, junctions or vias. A junction is where "
                   "three or more conductors meet, and it is where one run "
                   "ends and the next begins")),
        Row("length", _fmt(trace.length_mm, 3), "mm",
            basis="this run only, not the net"),
        Row("layers", ", ".join(trace.layers)),
        Row("narrowest", _fmt(trace.narrowest_mm), "mm",
            basis=f"on {trace.narrowest_layer}"),
    ]
    if trace.capacity_a is not None:
        rows.append(Row(
            "this run carries", _fmt(trace.capacity_a, 2), "A",
            basis=(f"IPC-2221 at a {trace.rise_c:.0f} °C rise for this run's "
                   f"narrowest point, {_fmt(trace.narrowest_mm)} mm on "
                   f"{trace.narrowest_layer}. No significance floor: every "
                   f"part of a series run carries the whole current, so the "
                   f"narrowest point is the limit — which is where this "
                   f"differs from the net's number below"),
            assumption=("the copper weight comes from the fab stackup where "
                        "it states one")))
    if trace.via_capacity_a is not None:
        rows.append(Row(
            "weakest via in series", _fmt(trace.via_capacity_a, 2), "A",
            basis=(f"{len(trace.via_ids)} via(s) lie inside this run, so they "
                   f"carry all of its current. Reported beside the copper and "
                   f"never merged into it: a run limited by a barrel and one "
                   f"limited by a neck want different fixes"),
            assumption=(f"{current_module.VIA_PLATING_UM:.0f} µm plating, "
                        f"because the fab has not said")))
    return {
        "id": None,                      # filled in by the caller that has it
        "name": trace.name,
        "net": trace.net,
        "limit_a": trace.limit_a,
        "rows": [r.as_dict() for r in rows],
        "track_ids": trace.track_ids,
    }


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

#: Above this many terminals a pour is a *plane*, and its erosion bottleneck
#: stops being a capacity and starts being an artefact of the via field.
#: `current.py`'s module docstring carries the measurement that sets this: on a
#: 40 A battery plane the erosion bottleneck read 5.5 mm — 3.4 A — while the
#: Laplace solve on the same copper said 2.0 mΩ and 3.1 W. **"The erosion
#: number would have been a false `critical`."** Two terminals cannot have a
#: via field between them to flow around, which is why that case is sound and
#: every other one is reported with a warning attached.
PLANE_TERMINALS = 2


def _point(pos):
    from shapely.geometry import Point
    return Point(pos[0], pos[1])


def _furthest_pair(pads):
    """The two pads furthest apart. O(n²) over one island's terminals."""
    best = None
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            d2 = (a.pos[0] - b.pos[0]) ** 2 + (a.pos[1] - b.pos[1]) ** 2
            if best is None or d2 > best[0]:
                best = (d2, a, b)
    return (best[1], best[2]) if best else None


def _pour_bottleneck(board, zone, layer, copper_oz):
    """The narrowest channel across one pour, and what it carries.

    The user asked for this and stated its own caveat in the same breath:

    > A pour that only connects two nets is basically a trace and we should be
    > given an ampacity. Honestly any pour should have an ampacity (but the
    > user must be told that this is measured by taking thinnest point, and may
    > not be useful for something like an interrupted ground plane which may
    > have thin areas between vias which will act like the thinnest point but
    > dont carry current)

    They are right on both halves, and the engine had already measured the
    second — see `PLANE_TERMINALS`. So the number is computed for every pour
    and its trustworthiness is reported beside it rather than being silently
    decided here.

    **Per island, not per zone.** `fill_on` returns a MultiPolygon and a zone's
    copper is routinely several disconnected islands; eroding between two pads
    on *different* islands answers `None`. A first pass that ignored this
    measured nothing on 59 of the V3 board's 68 pours. Each island is measured
    against its own terminals and the pour takes the **narrowest** result,
    because that is the channel current has to get through.
    """
    fill = zone.fill_on(layer)
    if fill is None or fill.is_empty:
        return None
    pads = [p for p in board.pads
            if p.net_name == zone.net_name and p.on_layer(layer)]
    if len(pads) < 2:
        return None

    oz = copper_oz.get(layer, 1.0)
    internal = layer not in (board.copper_layers[0], board.copper_layers[-1])
    best = None
    terminals_seen = 0
    for island in getattr(fill, "geoms", [fill]):
        inside = [p for p in pads if island.covers(_point(p.pos))]
        if len(inside) < 2:
            continue
        terminals_seen = max(terminals_seen, len(inside))
        far = _furthest_pair(inside)
        if far is None:
            continue
        width = current_module.bottleneck_mm(island, _point(far[0].pos),
                                             _point(far[1].pos))
        if width is None or width <= 0:
            continue
        amps = physics_module.current_capacity_a(
            width, physics_module.copper_thickness_mm(oz),
            current_module.RISE_C, internal=internal)
        if best is None or width < best[0]:
            best = (width, amps, far)

    if best is None:
        return None
    width, amps, far = best
    return {"width_mm": width, "amps": amps, "terminals": terminals_seen,
            "between": (far[0].designator, far[1].designator),
            "internal": internal, "oz": oz}


class BoardIndex:
    """Precomputed dossiers for a whole board, plus on-request assembly.

    Built after the review, from the same `Board` the review measured — see
    `session.py`, which keeps that board alive precisely so the two cannot
    describe different copper.
    """

    def __init__(self, board, nets: dict, findings_by_net: dict,
                 seconds: float = 0.0, traces=None, pour_necks=None):
        self.board = board
        self.nets = nets
        self.findings_by_net = findings_by_net
        self.seconds = seconds
        self.traces: TraceIndex = traces if traces is not None else TraceIndex()
        #: (zone position, layer) -> the narrowest channel across that pour and
        #: what it carries. Precomputed because §8 rules that hover is pure
        #: retrieval: an erosion binary search on hover would be the app
        #: thinking because the pointer moved.
        self.pour_necks = pour_necks or {}

    # -- traces ------------------------------------------------------------

    def trace_for_ident(self, ident: str) -> int | None:
        """The trace a hit belongs to, or `None`.

        `None` is a real answer and not a failure: a pad, a pour and a via at a
        junction all belong to no single run. Returning a trace for them would
        be picking one of several equally true ones.
        """
        if not ident or len(ident) < 2:
            return None
        try:
            index = int(ident[1:])
        except ValueError:
            return None
        if ident[0] == "t":
            return self.traces.of_track.get(index)
        if ident[0] == "v":
            return self.traces.of_via.get(index)
        return None

    # -- object dossiers ---------------------------------------------------

    def object(self, ident: str) -> dict:
        """One object's full dossier. `ident` is `spatial.Hit.ident`.

        Raises `KeyError` on an ident this board does not have, rather than
        returning an empty dossier — an inspector showing a blank panel for a
        real object and for a stale one is indistinguishable, and the stale
        case is the one that means something is wrong.
        """
        if not ident or len(ident) < 2:
            raise KeyError(f"not an object id: {ident!r}")
        kind, rest = ident[0], ident[1:]
        try:
            index = int(rest)
        except ValueError as exc:
            raise KeyError(f"not an object id: {ident!r}") from exc

        builders = {"t": self._track, "v": self._via, "p": self._pad,
                    "z": self._zone, "f": self._footprint}
        if kind not in builders:
            raise KeyError(f"unknown object kind in {ident!r}")
        return builders[kind](index, ident)

    def _wrap(self, ident: str, kind: str, title: str, subtitle: str,
              rows: list[Row], net_name: str, findings=None) -> dict:
        net = self.nets.get(net_name) if net_name else None
        number = self.trace_for_ident(ident)
        trace = self.traces.get(number)
        payload = {
            "ident": ident, "kind": kind, "title": title, "subtitle": subtitle,
            "net": net_name or None,
            "rows": [r.as_dict() for r in rows],
            "net_dossier": net.as_dict() if net else None,
            "findings": [_finding_dict(f) for f in
                         (findings if findings is not None
                          else (net.findings if net else []))],
        }
        if trace is not None:
            payload["trace"] = dict(_trace_dict(trace, self.board), id=number)
        return payload

    def _track(self, index: int, ident: str) -> dict:
        track = self.board.tracks[index]
        rows = [
            Row("layer", track.layer),
            Row("width", _fmt(track.width_mm), "mm"),
            Row("length", _fmt(track.length_mm, 3), "mm",
                basis=("r·θ for an arc, not the tessellated chord — a chord "
                       "always understates a curve and length feeds skew and "
                       "delay budgets"
                       if track.kind == "arc" else "centreline length")),
            Row("shape", "arc" if track.kind == "arc" else "segment"),
        ]
        dossier = self.nets.get(track.net_name)
        if dossier is not None and dossier.ampacity is not None:
            key = (track.layer, round(track.width_mm, 4))
            entry = dossier.ampacity.by_width.get(key)
            if entry is not None:
                length, amps = entry
                rows.append(Row(
                    "this width carries", _fmt(amps, 2), "A",
                    basis=(f"IPC-2221 at a {dossier.ampacity.rise_c:.0f} °C "
                           f"rise for {_fmt(track.width_mm)} mm on "
                           f"{track.layer}, over the {_fmt(length, 2)} mm the "
                           f"net routes at this width"),
                    assumption=("the copper weight comes from the fab "
                                "stackup where it states one")))
        return self._wrap(ident, "track", track.net_name or "(no net)",
                          f"track on {track.layer}", rows, track.net_name)

    def _via(self, index: int, ident: str) -> dict:
        via = self.board.vias[index]
        spanned = ", ".join(via.layers)
        rows = [
            Row("type", via.via_type),
            Row("spans", f"{via.top_layer} → {via.bottom_layer}",
                basis=f"copper on {spanned}"),
            Row("drill", _fmt(via.drill_mm), "mm"),
            Row("land", ", ".join(
                f"{layer} {_fmt(via.width_on(layer))}"
                for layer in via.layers), "mm"),
            Row("at", f"({via.pos[0]:.3f}, {via.pos[1]:.3f})", "mm"),
            Row("carries", _fmt(current_module.via_capacity_a(via.drill_mm), 2),
                "A",
                basis=("this barrel alone as an IPC-2221 conductor whose width "
                       "is its circumference and whose thickness is the "
                       "plating. The net's row above sums every barrel on it"),
                assumption=(f"{current_module.VIA_PLATING_UM:.0f} µm plating, "
                            f"because the fab has not said")),
        ]
        return self._wrap(ident, "via", via.net_name or "(no net)",
                          f"{via.via_type} via", rows, via.net_name)

    def _pad(self, index: int, ident: str) -> dict:
        pad = self.board.pads[index]
        footprint = next((fp for fp in self.board.footprints
                          if fp.ref == pad.ref), None)
        # `pad.copper_layers` is KiCad's, not this board's: a through-hole pad
        # carries every copper layer KiCad defines, so a 2-layer board's
        # connector pad claims `F.Cu, In1.Cu … In30.Cu, B.Cu`. The engine never
        # notices because it only ever asks `pad.on_layer(l)` for `l` in
        # `board.copper_layers`. Displaying the raw list would put thirty
        # layers that do not exist in front of the user, stated with the same
        # authority as the numbers that do.
        on_layers = [layer for layer in self.board.copper_layers
                     if pad.on_layer(layer)]
        rows = [
            Row("part", f"{pad.ref}"
                        + (f" — {footprint.value}" if footprint else "")),
            Row("pad", pad.number),
            Row("shape", pad.shape),
            Row("size", f"{_fmt(pad.size_mm[0])} × {_fmt(pad.size_mm[1])}",
                "mm"),
            Row("mounting", "SMD" if pad.is_smd else
                            "plated hole" if pad.is_plated_hole else
                            pad.attribute),
            Row("copper on", ", ".join(on_layers) or "none",
                basis=("this board's copper layers that the pad is on. KiCad "
                       "lists every layer it defines for a through-hole pad, "
                       "including ones this board does not have")),
            Row("at", f"({pad.pos[0]:.3f}, {pad.pos[1]:.3f})", "mm"),
        ]
        if pad.drill_mm and pad.drill_mm[0] > 0:
            rows.insert(5, Row("drill", _fmt(pad.drill_mm[0]), "mm"))
        if footprint is not None:
            rows.append(Row("library", footprint.lib_id))
        return self._wrap(ident, "pad", pad.designator,
                          f"pad on {pad.ref}", rows, pad.net_name)

    def _pour_capacity_rows(self, index: int, zone, layer) -> list:
        """The narrowest-channel rows for one pour, read from the precompute.

        Two rows and never one: the width is a measurement, the ampacity is a
        conclusion drawn from it, and on a plane the second is not safe to
        draw. Both carry the same caveat so neither can be read without it.
        """
        measured = self.pour_necks.get((index, layer))
        if measured is None:
            return []

        caveat = ("measured by erosion — the widest channel that still joins "
                  "this pour's two furthest terminals, so it is the THINNEST "
                  "POINT along that path")
        if measured["terminals"] > PLANE_TERMINALS:
            caveat += (
                f". This pour has {measured['terminals']} terminals, so treat "
                f"it as a FLOOR and not a capacity: current flows around a via "
                f"antipad rather than through it, so a field of holes reads as "
                f"a constriction that nothing has to squeeze through. Measured "
                f"on a 40 A plane elsewhere, erosion said 5.5 mm and 3.4 A "
                f"where the Laplace solve said 2.0 mΩ — it would have been a "
                f"false critical. `current_capacity` uses plane resistance for "
                f"poured nets for exactly this reason")
        else:
            caveat += (
                ". This pour joins two terminals with no via field between "
                "them, so the narrowest channel really is the limit — it is a "
                "wide trace, and this is the measurement a trace gets")

        between = " to ".join(measured["between"])
        return [
            Row(f"narrowest channel on {layer}",
                _fmt(measured["width_mm"], 3), "mm",
                basis=f"between {between}. {caveat}"),
            Row("that channel carries", _fmt(measured["amps"], 2), "A",
                basis=(f"IPC-2221 at a {current_module.RISE_C:.0f} °C rise for "
                       f"{_fmt(measured['width_mm'], 3)} mm of "
                       f"{measured['oz']:g} oz copper on {layer} "
                       f"({'inner' if measured['internal'] else 'outer'}). "
                       f"{caveat}"),
                assumption=("the copper weight comes from the fab stackup "
                            "where it states one")),
        ]

    def _zone(self, index: int, ident: str) -> dict:
        zone = self.board.zones[index]
        rows = [
            Row("kind", "rule area / keepout" if zone.is_rule_area else "pour"),
            Row("layers", ", ".join(zone.layers)),
            Row("priority", str(zone.priority),
                basis="higher priority pours first and pushes lower ones back"),
            Row("min thickness", _fmt(zone.min_thickness_mm), "mm"),
        ]
        if zone.name:
            rows.insert(0, Row("name", zone.name))
        if not zone.is_rule_area:
            for layer in zone.layers:
                area = zone.fill_on(layer).area
                rows.append(Row(
                    f"filled on {layer}", _fmt(area, 2), "mm²",
                    basis=("poured copper as KiCad filled it — islands and "
                           "thermal reliefs intact. The zone OUTLINE is where "
                           "copper was requested; this is where it landed")))
                rows.extend(self._pour_capacity_rows(index, zone, layer))
        return self._wrap(ident, "zone", zone.net_name or "(no net)",
                          "keepout" if zone.is_rule_area else "copper pour",
                          rows, zone.net_name)

    def _footprint(self, index: int, ident: str) -> dict:
        footprint = self.board.footprints[index]
        rows = [
            Row("value", footprint.value),
            Row("library", footprint.lib_id),
            Row("side", footprint.layer),
            Row("rotation", f"{footprint.orientation_deg:g}", "°"),
            Row("pads", str(len(footprint.pads))),
            Row("at", f"({footprint.pos[0]:.3f}, {footprint.pos[1]:.3f})",
                "mm"),
            Row("nets", ", ".join(sorted({p.net_name for p in footprint.pads
                                          if p.net_name})) or "none"),
        ]
        # A part is not on one net, so its findings are those naming any net it
        # touches — gathered here rather than through `_wrap`'s net lookup.
        seen: set[tuple] = set()
        findings = []
        for pad in footprint.pads:
            for ref in self.findings_by_net.get(pad.net_name, ()):
                key = (ref.source, ref.check, ref.title, ref.nets)
                if key not in seen:
                    seen.add(key)
                    findings.append(ref)
        return self._wrap(ident, "footprint", footprint.ref,
                          "component", rows, "",
                          findings=sorted(findings, key=lambda r: r.rank))

    # -- net dossiers ------------------------------------------------------

    def net(self, name: str) -> dict:
        dossier = self.nets.get(name)
        if dossier is None:
            raise KeyError(f"no net called {name!r} on this board")
        return dossier.as_dict()

    def net_names(self) -> list[str]:
        return sorted(self.nets)


def build_index(board, layout_report, schematic_report=None) -> BoardIndex:
    """Precompute every net's dossier. The one expensive step, paid up front.

    the caching note §1 lists per-net ampacity as one of two gaps between what the
    audit computes and what full hover coverage needs, and notes it is a closed
    form with no field solve — *"extending per-net ampacity to all 195 nets is
    milliseconds"*. This is that extension.
    """
    import time
    started = time.time()

    findings_by_net: dict[str, list] = {}
    pairs = [(layout_report, LAYOUT)]
    if schematic_report is not None:
        pairs.append((schematic_report, SCHEMATIC))
    for report, source in pairs:
        for finding in report.findings:
            for name in (finding.nets or ()):
                findings_by_net.setdefault(name, []).append(
                    finding_ref(finding, source, "names_this_net"))
    for name in findings_by_net:
        findings_by_net[name].sort(key=lambda r: r.rank)

    copper_oz = _copper_oz_by_layer(board)
    # Every pour's narrowest channel, up front. 2.5 s on the V3 board's 68
    # pours against a 63 s review — cheap enough to pay once, far too slow to
    # pay on a pointer move.
    pour_necks = {}
    for position, zone in enumerate(board.zones):
        if zone.is_rule_area:
            continue
        for layer in zone.layers:
            try:
                measured = _pour_bottleneck(board, zone, layer, copper_oz)
            except Exception:                                 # noqa: BLE001
                # A degenerate fill shapely will not erode must not take the
                # whole index down; that pour then reports no channel, which is
                # exactly what every pour did before this existed.
                measured = None
            if measured is not None:
                pour_necks[(position, layer)] = measured
    # The same weights the nets' ampacity uses, passed rather than recomputed
    # so a trace's capacity and its net's cannot come from different copper.
    traces = build_traces(board, copper_oz)
    nets = {}
    # `board.nets` is `dict[int, Net]` — `.values()`, because iterating it
    # yields ints (REFERENCE.md's cheat-sheet; §4.4 says sessions keep
    # rediscovering this one).
    for net in board.nets.values():
        if not net.name:
            continue
        nets[net.name] = _build_net(board, net, findings_by_net, copper_oz)

    return BoardIndex(board, nets, findings_by_net,
                      seconds=time.time() - started, traces=traces,
                      pour_necks=pour_necks)
