"""Extract a KiCad board into a normalised JSON document.

This module is the ONLY part of pcbreview that depends on KiCad. It must run
under KiCad's bundled Python (3.9, has ``pcbnew``, no numpy/shapely), so it is
deliberately written in plain stdlib Python and never imported by the analysis
package.

Everything downstream consumes the JSON, which means the KiCad dependency can
be swapped for a pure S-expression reader later without touching any analysis
code. That matters: the SWIG ``pcbnew`` bindings are deprecated and scheduled
for removal in KiCad 11.

All lengths in the output are millimetres. Angles are degrees, CCW positive.

Usage (from the analysis side, via run_extract()):
    /path/to/kicad/python3 -m pcbreview.extract board.kicad_pcb out.json
"""

import json
import os
import re
import sys
import time

import pcbnew

SCHEMA_VERSION = 2

# KiCad stores internal units in nanometres.
NM_PER_MM = 1e6


def mm(nanometres):
    """Convert KiCad internal units to millimetres, rounded to 1 nm."""
    return round(nanometres / NM_PER_MM, 6)


def pt(vector2i):
    return [mm(vector2i.x), mm(vector2i.y)]


def _enum_map(prefix):
    """Build {value: name} for a family of pcbnew integer constants."""
    out = {}
    for name in dir(pcbnew):
        if name.startswith(prefix):
            value = getattr(pcbnew, name)
            if isinstance(value, int):
                out.setdefault(value, name[len(prefix):].lower())
    return out


PAD_SHAPES = _enum_map("PAD_SHAPE_")
PAD_ATTRIBS = _enum_map("PAD_ATTRIB_")
VIA_TYPES = _enum_map("VIATYPE_")


def poly_set_to_json(poly_set):
    """Convert a SHAPE_POLY_SET to [{outer: [[x,y]...], holes: [[[x,y]...]]}].

    Zone fills routinely arrive as many disjoint islands, and each island can
    carry holes (thermal reliefs, clearance cutouts), so both levels matter.
    """
    polygons = []
    for i in range(poly_set.OutlineCount()):
        outline = poly_set.Outline(i)
        outer = [pt(outline.CPoint(k)) for k in range(outline.PointCount())]
        holes = []
        for j in range(poly_set.HoleCount(i)):
            hole = poly_set.Hole(i, j)
            holes.append([pt(hole.CPoint(k)) for k in range(hole.PointCount())])
        polygons.append({"outer": outer, "holes": holes})
    return polygons


def copper_layers(board):
    """Copper layers in physical stack order, top to bottom."""
    layers = []
    for layer_id in board.GetEnabledLayers().CuStack():
        layers.append({
            "id": int(layer_id),
            "name": board.GetLayerName(layer_id),
        })
    return layers


def extract_nets(board):
    nets = []
    for code, net in board.GetNetsByNetcode().items():
        if code == 0:
            continue  # unconnected pseudo-net
        try:
            netclass = net.GetNetClassName()
        except AttributeError:
            netclass = None
        nets.append({
            "code": int(code),
            "name": net.GetNetname(),
            "netclass": str(netclass) if netclass else None,
        })
    return sorted(nets, key=lambda n: n["code"])


def extract_netclasses(board):
    """Netclass design rules — the design *intent* KiCad already knows about."""
    out = {}
    try:
        classes = board.GetAllNetClasses()
    except AttributeError:
        return out
    for name, nc in classes.items():
        entry = {}
        for key, getter in (
            ("clearance_mm", "GetClearance"),
            ("track_width_mm", "GetTrackWidth"),
            ("via_diameter_mm", "GetViaDiameter"),
            ("via_drill_mm", "GetViaDrill"),
            ("diff_pair_width_mm", "GetDiffPairWidth"),
            ("diff_pair_gap_mm", "GetDiffPairGap"),
        ):
            if hasattr(nc, getter):
                try:
                    entry[key] = mm(getattr(nc, getter)())
                except Exception:
                    pass
        out[str(name)] = entry
    return out


def extract_tracks(board):
    """Track segments and arcs. Vias share the container but come out separately."""
    segments, vias = [], []
    for index, track in enumerate(board.GetTracks()):
        cls = track.GetClass()
        net = track.GetNetCode()

        if cls == "PCB_VIA":
            layers = [board.GetLayerName(l) for l in track.GetLayerSet().Seq()]
            # KiCad 10 supports per-layer via annular rings.
            width_by_layer = {}
            for layer_id in track.GetLayerSet().Seq():
                try:
                    width_by_layer[board.GetLayerName(layer_id)] = mm(
                        track.GetWidth(layer_id))
                except Exception:
                    pass
            vias.append({
                "id": index,
                "net": net,
                "pos": pt(track.GetStart()),
                "drill_mm": mm(track.GetDrillValue()),
                "via_type": VIA_TYPES.get(int(track.GetViaType()), "unknown"),
                "top_layer": board.GetLayerName(track.TopLayer()),
                "bottom_layer": board.GetLayerName(track.BottomLayer()),
                "layers": layers,
                "width_by_layer": width_by_layer,
            })
            continue

        entry = {
            "id": index,
            "net": net,
            "layer": board.GetLayerName(track.GetLayer()),
            "width_mm": mm(track.GetWidth()),
            "start": pt(track.GetStart()),
            "end": pt(track.GetEnd()),
        }
        if cls == "PCB_ARC":
            entry["kind"] = "arc"
            entry["mid"] = pt(track.GetMid())
            entry["radius_mm"] = mm(track.GetRadius())
        else:
            entry["kind"] = "segment"
        segments.append(entry)
    return segments, vias


def _pad_polygon(pad, layer_id):
    """Exact pad copper outline, including roundrect/custom shapes.

    Taking KiCad's own effective polygon avoids reimplementing eight pad shape
    families (and their chamfer/roundrect corner maths) on the analysis side.
    """
    try:
        return poly_set_to_json(pad.GetEffectivePolygon(layer_id))
    except TypeError:
        return poly_set_to_json(pad.GetEffectivePolygon())
    except Exception:
        return []


def extract_footprints(board):
    footprints = []
    for fp in board.GetFootprints():
        pads = []
        for pad in fp.Pads():
            layers = [board.GetLayerName(l) for l in pad.GetLayerSet().Seq()]
            copper = [l for l in pad.GetLayerSet().CuStack()]
            drill = pad.GetDrillSize()
            pads.append({
                "number": pad.GetNumber(),
                "net": pad.GetNetCode(),
                "net_name": pad.GetNetname(),
                "shape": PAD_SHAPES.get(int(pad.GetShape()), "unknown"),
                "attribute": PAD_ATTRIBS.get(int(pad.GetAttribute()), "unknown"),
                "pos": pt(pad.GetPosition()),
                "size_mm": [mm(pad.GetSize().x), mm(pad.GetSize().y)],
                "orientation_deg": pad.GetOrientationDegrees(),
                "drill_mm": [mm(drill.x), mm(drill.y)],
                "layers": layers,
                "copper_layers": [board.GetLayerName(l) for l in copper],
                "polygon": _pad_polygon(pad, copper[0]) if copper else [],
            })

        footprints.append({
            "ref": fp.GetReference(),
            "value": fp.GetValue(),
            "lib_id": str(fp.GetFPID().GetUniStringLibId()),
            "pos": pt(fp.GetPosition()),
            "orientation_deg": fp.GetOrientationDegrees(),
            "layer": board.GetLayerName(fp.GetLayer()),
            "pads": pads,
        })
    return footprints


def extract_zones(board):
    zones = []
    for index, zone in enumerate(board.Zones()):
        layers = [board.GetLayerName(l) for l in zone.GetLayerSet().Seq()]
        fills = {}
        if not zone.GetIsRuleArea():
            for layer_id in zone.GetLayerSet().Seq():
                polys = poly_set_to_json(zone.GetFilledPolysList(layer_id))
                if polys:
                    fills[board.GetLayerName(layer_id)] = polys

        zones.append({
            "id": index,
            "net": zone.GetNetCode(),
            "net_name": zone.GetNetname(),
            "name": zone.GetZoneName(),
            "is_rule_area": bool(zone.GetIsRuleArea()),
            "priority": int(zone.GetAssignedPriority()),
            "min_thickness_mm": mm(zone.GetMinThickness()),
            "layers": layers,
            "outline": poly_set_to_json(zone.Outline()),
            "fills": fills,
        })
    return zones


def extract_outline(board):
    """Board edge, used for edge-clearance and cavity-resonance checks."""
    poly_set = pcbnew.SHAPE_POLY_SET()
    ok = board.GetBoardPolygonOutlines(poly_set, False)
    return {
        "resolved": bool(ok),
        "polygons": poly_set_to_json(poly_set) if ok else [],
    }


def extract_connectivity(board):
    """KiCad's own connectivity verdict — ground truth for our geometry.

    Two independent things are captured:

    * ``pads_by_net`` — KiCad's net assignment for every pad. This says which
      pads *should* be joined.
    * ``unconnected_count`` / ``ratsnest_by_net`` — KiCad's verdict on whether
      they actually *are* joined by copper.

    When we later build connectivity geometrically (segments touching pads,
    vias, zone fills), any net that KiCad calls fully routed but we split into
    islands is a bug in our geometry, not a finding about the board.
    """
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()

    pads_by_net = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            code = pad.GetNetCode()
            if code == 0:
                continue
            pads_by_net.setdefault(str(code), []).append(
                "%s.%s" % (fp.GetReference(), pad.GetNumber()))

    ratsnest_by_net = {}
    for code in pads_by_net:
        try:
            edges = connectivity.GetRatsnestForNet(int(code))
            if edges:
                ratsnest_by_net[code] = len(edges)
        except Exception:
            pass

    return {
        "pads_by_net": {k: sorted(v) for k, v in pads_by_net.items()},
        "unconnected_count": int(connectivity.GetUnconnectedCount(True)),
        "ratsnest_by_net": ratsnest_by_net,
        "node_count": int(connectivity.GetNodeCount()),
    }


def total_fill_area_mm2(board):
    total = 0.0
    for zone in board.Zones():
        if zone.GetIsRuleArea():
            continue
        for layer_id in zone.GetLayerSet().Seq():
            total += zone.GetFilledPolysList(layer_id).Area() / 1e12
    return total


def refill_zones(board):
    """Refill zones in memory so geometry reflects the current routing.

    Boards are routinely saved with stale (or entirely absent) zone fills —
    one board in this repo had 0.00 mm2 of saved fill against 7958 mm2 after
    a refill. Analysing the saved fill would then report return-path holes
    that do not exist, or miss ones that do.

    This mirrors what `kicad-cli pcb drc --refill-zones` does. The board is
    never written back; the refill lives only in this process.
    """
    before = total_fill_area_mm2(board)
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception as exc:  # pragma: no cover - depends on KiCad build
        return {"refilled": False, "error": str(exc),
                "area_before_mm2": round(before, 4),
                "area_after_mm2": round(before, 4)}
    after = total_fill_area_mm2(board)
    # A few um2 of churn is just polygon rebuild noise, not staleness.
    stale = abs(after - before) > max(0.01, 0.001 * max(after, 1.0))
    return {
        "refilled": True,
        "was_stale": bool(stale),
        "area_before_mm2": round(before, 4),
        "area_after_mm2": round(after, 4),
    }


def kicad_measurements(board):
    """KiCad's own answer for the geometry we are about to hand to shapely.

    This is the independent ground truth the whole validation rests on: the
    numbers come from KiCad's geometry engine, ours come from shapely, and the
    two must agree to floating point.

    **It lives here, and is taken after `refill_zones`, on purpose.** It used to
    live in `tests/kicad_truth.py`, which loaded the board in a *second* KiCad
    process and refilled the zones *again*. KiCad's `ZONE_FILLER` is not
    reproducible — on some boards it returns two different polygonisations of
    the same pour, 5.8e-4 apart in area (HANDOFF §40) — so the two processes
    could legitimately be describing different copper, and the comparison then
    failed with nothing wrong on either side. Roughly one run in five on
    The four-layer board.

    Measuring here makes the comparison the one it was always meant to be:
    *our* measurement of a pour against *KiCad's* measurement of **that same
    pour**. The independence that matters — two different geometry engines — is
    untouched. The only thing given up is a second, redundant fill, which was
    never the thing being validated.
    """
    zone_area = {}
    for index, zone in enumerate(board.Zones()):
        if zone.GetIsRuleArea():
            continue
        for layer in zone.GetLayerSet().Seq():
            area = zone.GetFilledPolysList(layer).Area() / 1e12
            if area > 0:
                zone_area["%d|%s" % (index, board.GetLayerName(layer))] = area

    track_len = {}
    for track in board.GetTracks():
        if track.GetClass() == "PCB_VIA":
            continue
        name = track.GetNetname() or "<none>"
        track_len[name] = track_len.get(name, 0.0) + track.GetLength() / 1e6

    outline = pcbnew.SHAPE_POLY_SET()
    board.GetBoardPolygonOutlines(outline, False)

    pad_area = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            copper = list(pad.GetLayerSet().CuStack())
            if not copper:
                continue
            key = "%s.%s" % (fp.GetReference(), pad.GetNumber())
            try:
                poly = pad.GetEffectivePolygon(copper[0])
            except TypeError:
                poly = pad.GetEffectivePolygon()
            pad_area[key] = poly.Area() / 1e12

    return {
        "outline_area": outline.Area() / 1e12,
        "zone_area": zone_area,
        "track_len": track_len,
        "pad_area": pad_area,
        "unconnected": int(board.GetConnectivity().GetUnconnectedCount(True)),
    }


def declared_versions(path):
    """`(version, generator_version)` read from the board's own first lines.

    Plain text, no KiCad needed, which is what lets the version-mismatch error
    below name both numbers even though the KiCad we have could not open the
    file at all.
    """
    version = generator = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                found = re.search(r'\(version\s+"?([\w.]+)"?\s*\)', line)
                if found and version is None:
                    version = found.group(1)
                found = re.search(r'\(generator_version\s+"([^"]+)"\s*\)', line)
                if found and generator is None:
                    generator = found.group(1)
    except OSError:
        pass
    return version, generator


def extract_board(path, refill=True):
    board = pcbnew.LoadBoard(path)
    # `LoadBoard` returns a NULL BOARD rather than raising when it cannot parse
    # the file — and the commonest reason is that this KiCad is older than the
    # board's format. Unguarded, the next call explodes with
    # `AttributeError: 'NoneType' object has no attribute 'Zones'` several
    # frames away, with nothing anywhere saying "version". A Windows user with
    # KiCad 9 and 10 installed side by side hit exactly that and reasonably
    # concluded the skill was broken. §14 has the same warning for the
    # missing-file case, where it segfaults instead.
    if board is None:
        version, generator = declared_versions(path)
        raise SystemExit(
            f"pcbnew {pcbnew.GetBuildVersion()} could not load {path}.\n"
            f"  the file declares version {version or 'unknown'}"
            + (f", written by KiCad {generator}" if generator else "") + "\n"
            "  This is almost always a KiCad version mismatch, not a broken "
            "install and not a corrupt board.\n"
            "  fix: set PCBREVIEW_KICAD_PYTHON to a newer KiCad's python, e.g.\n"
            "       PCBREVIEW_KICAD_PYTHON='C:\\Program Files\\KiCad\\10.0\\"
            "bin\\python.exe'\n"
            "  kicad-cli is then taken from beside it automatically.")
    fill_status = refill_zones(board) if refill else {"refilled": False}
    segments, vias = extract_tracks(board)
    return {
        "zone_fill": fill_status,
        "kicad_measurements": kicad_measurements(board),
        "schema": SCHEMA_VERSION,
        "source": {
            # Absolute, always. This string is reopened later by
            # `check_stackup_mismatch` to read the board's own stackup block,
            # and the extraction is now cached in a review folder or the OS
            # temp directory — shared across sessions and working directories.
            # A session that ran with the board's own directory as cwd wrote a
            # *relative* path into the cache, and every later consumer with a
            # different cwd then failed with FileNotFoundError on a board that
            # was plainly there.
            "file": os.path.abspath(path),
            "kicad_version": pcbnew.GetBuildVersion(),
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "board": {
            "thickness_mm": mm(board.GetDesignSettings().GetBoardThickness()),
            "outline": extract_outline(board),
        },
        "copper_layers": copper_layers(board),
        "nets": extract_nets(board),
        "netclasses": extract_netclasses(board),
        "tracks": segments,
        "vias": vias,
        "footprints": extract_footprints(board),
        "zones": extract_zones(board),
        "connectivity": extract_connectivity(board),
    }


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: extract.py <board.kicad_pcb> <out.json>\n")
        return 2
    data = extract_board(argv[1])
    with open(argv[2], "w") as handle:
        json.dump(data, handle, separators=(",", ":"))
    fill = data.get("zone_fill", {})
    note = ""
    if fill.get("was_stale"):
        note = " [zone fills were STALE: %.1f -> %.1f mm2]" % (
            fill["area_before_mm2"], fill["area_after_mm2"])
    sys.stderr.write(
        "extracted %d tracks, %d vias, %d pads, %d zones, %d nets%s\n" % (
            len(data["tracks"]), len(data["vias"]),
            sum(len(f["pads"]) for f in data["footprints"]),
            len(data["zones"]), len(data["nets"]), note))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
