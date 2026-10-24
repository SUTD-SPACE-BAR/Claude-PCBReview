#!/usr/bin/env python3
"""
KiCad Schematic Parser v2 — with net connectivity tracing
==========================================================
Parses a .kicad_sch file (KiCad 6+, S-expression format) and outputs
a structured Markdown summary including a NET LIST showing which
component pins are connected to which named nets.

Usage:
    python3 parse_schematic.py path/to/schematic.kicad_sch [--no-nets]

The connectivity tracer works by:
  1. Reading every pin from every lib_symbol definition with its relative
     position, then computing absolute pin positions for each symbol
     instance (translation + rotation + optional mirror).
  2. Treating wires, labels, junctions, and no-connects as endpoints
     in a spatial graph.
  3. Two endpoints at the same (x, y) coordinate (within rounding) are
     connected. A wire connects its two endpoints.
  4. Running union-find to compute connected components = nets.
  5. Each net is named by the labels attached to it
     (priority: power > global > hierarchical > local > unnamed).
"""

import sys
import math
from pathlib import Path
from collections import defaultdict

import sexpdata


# ---------------------------------------------------------------------------
# S-expression helpers
# ---------------------------------------------------------------------------

def sym_name(node):
    if isinstance(node, list) and len(node) > 0:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return str(first)
    return None

def find_children(node, tag):
    return [c for c in node[1:]
            if isinstance(c, list) and sym_name(c) == tag]

def find_child(node, tag):
    for c in node[1:]:
        if isinstance(c, list) and sym_name(c) == tag:
            return c
    return None

def get_value(node, tag):
    c = find_child(node, tag)
    if c and len(c) > 1:
        return c[1]
    return None

def get_str(node, tag):
    v = get_value(node, tag)
    return str(v) if v is not None else ""


# ---------------------------------------------------------------------------
# Coordinate snapping (KiCad uses fine-grained float coords, snap for matching)
# ---------------------------------------------------------------------------
COORD_EPSILON = 0.01  # mm

def snap(c):
    return round(c / COORD_EPSILON) * COORD_EPSILON

def pt_key(x, y):
    return (snap(x), snap(y))


# ---------------------------------------------------------------------------
# Lib symbol pin extraction
# ---------------------------------------------------------------------------

def extract_lib_symbol_pins(root):
    """
    Return dict: lib_id -> list of pins.
    Each pin: {'number', 'name', 'type', 'x', 'y', 'angle'}
    Pins live inside nested sub-symbols (Name_0_1, Name_1_1, etc.) — we
    recurse to find them all.
    """
    lib_pins = {}
    lib_node = find_child(root, 'lib_symbols')
    if not lib_node:
        return lib_pins

    for symdef in find_children(lib_node, 'symbol'):
        if len(symdef) < 2:
            continue
        lib_id = str(symdef[1])
        pins = []

        def collect(node, unit=0, style=1):
            for child in node[1:]:
                if not isinstance(child, list):
                    continue
                tag = sym_name(child)
                if tag == 'pin':
                    pin_unit, pin_style = unit, style
                    pin = {}
                    if len(child) > 1 and isinstance(child[1], sexpdata.Symbol):
                        pin['type'] = str(child[1])
                    at_node = find_child(child, 'at')
                    if at_node and len(at_node) >= 3:
                        pin['x']     = float(at_node[1])
                        pin['y']     = float(at_node[2])
                        pin['angle'] = float(at_node[3]) if len(at_node) > 3 else 0.0
                    name_node = find_child(child, 'name')
                    if name_node and len(name_node) > 1:
                        pin['name'] = str(name_node[1])
                    num_node = find_child(child, 'number')
                    if num_node and len(num_node) > 1:
                        pin['number'] = str(num_node[1])
                    if 'x' in pin and 'number' in pin:
                        pin['unit'] = pin_unit
                        pin['style'] = pin_style
                        pins.append(pin)
                elif tag == 'symbol':
                    # Sub-symbols are named "<Symbol>_<unit>_<bodystyle>". A
                    # placed instance uses exactly one unit (plus unit 0, which
                    # is common to all) and one body style. Collecting every
                    # sub-symbol's pins duplicates them at wrong coordinates and
                    # manufactures phantom nets.
                    sub_unit, sub_style = unit, style
                    if len(child) > 1:
                        parts = str(child[1]).rsplit('_', 2)
                        if len(parts) == 3:
                            try:
                                sub_unit, sub_style = int(parts[1]), int(parts[2])
                            except ValueError:
                                pass
                    collect(child, sub_unit, sub_style)

        collect(symdef)
        lib_pins[lib_id] = pins

    return lib_pins


# ---------------------------------------------------------------------------
# Symbol instance transform
# ---------------------------------------------------------------------------

def transform_pin(pin_x, pin_y, sym_x, sym_y, sym_angle, mirror=None):
    """
    Apply a symbol instance's transform to a relative pin coord.

    IMPORTANT KiCad coordinate conventions:
    - lib_symbol coordinates use Y-UP (CAD/math convention)
    - schematic (.kicad_sch) coordinates use Y-DOWN (screen convention)
    So we must flip Y when converting from lib frame to schematic frame.

    Transform order:
      1. rotate by sym_angle (CCW) in the lib frame
      2. flip Y — lib is Y-up, schematic is Y-down
      3. mirror around the symbol origin, IN THE SCHEMATIC FRAME
      4. translate by (sym_x, sym_y)

    Step 3 order matters and is not the obvious one. KiCad composes the
    mirror onto the left of the orientation matrix
    (`SCH_SYMBOL::SetOrientation`: `m_transform = mirror * m_transform`),
    so the mirror acts on already-rotated screen coordinates. Mirroring
    first in the lib frame instead swaps the two ends of every rotated
    symbol -- a series resistor's pins 1 and 2 exchange nets, which is
    invisible on a symmetric part and silently wrong on a series one.

    Determined empirically, not from the source: all eight combinations of
    {rotation sense} x {mirror before/after} x {which axis 'x' names} were
    scored against KiCad's own netlist over 11 single-instance sheets on
    both boards. This one is the only combination with zero disagreements
    (340 nets exact); the previous convention gave 5 split and 5 merged.

    KiCad mirror conventions, in the schematic frame:
    - (mirror x): invert Y
    - (mirror y): invert X
    """
    # Step 1: rotate CCW in lib frame (standard math rotation)
    theta = math.radians(sym_angle)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    x = pin_x * cos_t - pin_y * sin_t
    y = pin_x * sin_t + pin_y * cos_t

    # Step 2: lib frame (Y-up) -> schematic frame (Y-down)
    y = -y

    # Step 3: mirror in the schematic frame
    if mirror == 'x':
        y = -y
    elif mirror == 'y':
        x = -x

    # Step 4: translate
    return (sym_x + x, sym_y + y)


def extract_symbol_instances(root, lib_pins):
    """
    Extract all top-level (symbol ...) instances with pins at absolute coords.
    """
    components = []

    for node in root[1:]:
        if sym_name(node) != 'symbol':
            continue

        comp = {}
        lib_id = get_str(node, 'lib_id')
        comp['lib_id'] = lib_id

        at_node = find_child(node, 'at')
        sym_x = sym_y = 0.0
        sym_angle = 0.0
        if at_node and len(at_node) >= 3:
            sym_x = float(at_node[1])
            sym_y = float(at_node[2])
            sym_angle = float(at_node[3]) if len(at_node) > 3 else 0.0
        comp['x'], comp['y'], comp['angle'] = sym_x, sym_y, sym_angle

        mirror_node = find_child(node, 'mirror')
        mirror = str(mirror_node[1]) if mirror_node and len(mirror_node) > 1 else None
        comp['mirror'] = mirror

        comp['in_bom']   = get_str(node, 'in_bom')
        comp['on_board'] = get_str(node, 'on_board')
        dnp_node = find_child(node, 'dnp')
        comp['dnp'] = str(dnp_node[1]) if dnp_node and len(dnp_node) > 1 else 'no'

        comp['properties'] = {}
        for child in node[1:]:
            if sym_name(child) == 'property' and len(child) >= 3:
                comp['properties'][str(child[1])] = str(child[2])

        uuid_node = find_child(node, 'uuid')
        comp['uuid'] = str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else None

        # KiCad 7+ records one reference designator per *sheet instance path*.
        # A sheet placed more than once (four such sheets here) has four
        # entries; the Reference property holds only whichever was edited last.
        comp['instances'] = {}
        inst_node = find_child(node, 'instances')
        if inst_node:
            for proj in find_children(inst_node, 'project'):
                for path_node in find_children(proj, 'path'):
                    if len(path_node) < 2:
                        continue
                    unit_value = get_value(path_node, 'unit')
                    comp['instances'][str(path_node[1])] = {
                        'reference': get_str(path_node, 'reference'),
                        'unit': int(unit_value) if unit_value is not None else 1,
                    }

        comp['reference'] = comp['properties'].get('Reference', '??')
        comp['value']     = comp['properties'].get('Value', '')
        comp['footprint'] = comp['properties'].get('Footprint', '')
        comp['datasheet'] = comp['properties'].get('Datasheet', '')

        # Which unit and body style this instance draws. Pins belonging to
        # other units live in the same lib_symbol and must not be placed here.
        unit_node = find_child(node, 'unit')
        inst_unit = int(unit_node[1]) if unit_node and len(unit_node) > 1 else 1
        style_node = find_child(node, 'convert')
        inst_style = int(style_node[1]) if style_node and len(style_node) > 1 else 1
        comp['unit'] = inst_unit

        comp['pins'] = []
        if lib_id in lib_pins:
            for lp in lib_pins[lib_id]:
                # Unit 0 is common to every unit; style 0 likewise.
                if lp.get('unit', 0) not in (0, inst_unit):
                    continue
                if lp.get('style', 1) not in (0, inst_style):
                    continue
                ax, ay = transform_pin(lp['x'], lp['y'],
                                       sym_x, sym_y, sym_angle, mirror)
                comp['pins'].append({
                    'number': lp.get('number', '?'),
                    'name':   lp.get('name', '~'),
                    'type':   lp.get('type', '?'),
                    'x': ax, 'y': ay,
                })

        components.append(comp)

    return components


# ---------------------------------------------------------------------------
# Wires, labels, junctions, no-connects
# ---------------------------------------------------------------------------

def extract_wires(root):
    wires = []
    for node in root[1:]:
        if sym_name(node) != 'wire':
            continue
        pts_node = find_child(node, 'pts')
        if not pts_node:
            continue
        endpoints = []
        for xy in find_children(pts_node, 'xy'):
            if len(xy) >= 3:
                endpoints.append((float(xy[1]), float(xy[2])))
        if len(endpoints) >= 2:
            wires.append({'p1': endpoints[0], 'p2': endpoints[1]})
    return wires


def extract_all_labels(root):
    local, glob, hier = [], [], []
    for node in root[1:]:
        tag = sym_name(node)
        if tag not in ('label', 'global_label', 'hierarchical_label'):
            continue
        text = ""
        if len(node) > 1 and isinstance(node[1], str):
            text = node[1]
        else:
            tn = find_child(node, 'text')
            if tn and len(tn) > 1:
                text = str(tn[1])
        at_node = find_child(node, 'at')
        if not at_node or len(at_node) < 3:
            continue
        pos = (float(at_node[1]), float(at_node[2]))
        shape = get_str(node, 'shape')
        e = {'text': text, 'pos': pos, 'shape': shape}
        if tag == 'label':              local.append(e)
        elif tag == 'global_label':     glob.append(e)
        else:                           hier.append(e)
    return {'local': local, 'global': glob, 'hierarchical': hier}


def extract_junctions(root):
    js = []
    for node in root[1:]:
        if sym_name(node) == 'junction':
            at_node = find_child(node, 'at')
            if at_node and len(at_node) >= 3:
                js.append((float(at_node[1]), float(at_node[2])))
    return js


def extract_no_connects(root):
    ncs = []
    for node in root[1:]:
        if sym_name(node) == 'no_connect':
            at_node = find_child(node, 'at')
            if at_node and len(at_node) >= 3:
                ncs.append((float(at_node[1]), float(at_node[2])))
    return ncs


def extract_file_uuid(root):
    """The schematic file's own UUID — the first element of every instance path."""
    node = find_child(root, 'uuid')
    return str(node[1]) if node and len(node) > 1 else None


def extract_sheet_nodes(root):
    """Child-sheet symbols, with the UUID and pins the hierarchy walk needs.

    `extract_hier_sheets` returns only the property bag, which is enough for a
    report and not enough to traverse: the UUID identifies this *placement*
    (the same file placed twice has two UUIDs and two sets of designators),
    and the pins are where the parent's wires meet the child's hierarchical
    labels.
    """
    sheets = []
    for node in root[1:]:
        if sym_name(node) != 'sheet':
            continue
        props = {}
        for child in node[1:]:
            if sym_name(child) == 'property' and len(child) >= 3:
                props[str(child[1])] = str(child[2])

        pins = []
        for pin in find_children(node, 'pin'):
            at_node = find_child(pin, 'at')
            if len(pin) < 2 or not at_node or len(at_node) < 3:
                continue
            shape = pin[2] if len(pin) > 2 and isinstance(pin[2], sexpdata.Symbol) else ''
            pins.append({
                'name': str(pin[1]),
                'shape': str(shape),
                'pos': (float(at_node[1]), float(at_node[2])),
            })

        uuid_node = find_child(node, 'uuid')
        sheets.append({
            'uuid': str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else None,
            'name': props.get('Sheetname', ''),
            'file': props.get('Sheetfile', ''),
            'properties': props,
            'pins': pins,
        })
    return sheets


def extract_hier_sheets(root):
    sheets = []
    for node in root[1:]:
        if sym_name(node) != 'sheet':
            continue
        s = {}
        for child in node[1:]:
            if sym_name(child) == 'property' and len(child) >= 3:
                s[str(child[1])] = str(child[2])
        sheets.append(s)
    return sheets


def extract_title_block(root):
    tb = find_child(root, 'title_block')
    if not tb:
        return {}
    info = {}
    for f in ['title', 'rev', 'date', 'company']:
        v = get_value(tb, f)
        if v is not None:
            info[f] = str(v)
    for child in tb[1:]:
        if sym_name(child) == 'comment' and len(child) >= 3:
            info[f'comment_{child[1]}'] = str(child[2])
    return info


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent = {}
    def add(self, x):
        if x not in self.parent:
            self.parent[x] = x
    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]
    def union(self, x, y):
        self.add(x); self.add(y)
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry


# Half the snap grid: a point this close to a wire is on it.
ON_WIRE_TOLERANCE = COORD_EPSILON / 2.0


def _point_on_segment(px, py, x1, y1, x2, y2, tol=ON_WIRE_TOLERANCE):
    """True if (px,py) lies on the segment, endpoints included."""
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return abs(px - x1) <= tol and abs(py - y1) <= tol
    # Project onto the segment and clamp to it.
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    cx, cy = x1 + t * dx, y1 + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2 <= tol * tol


def _connect_points_on_wires(uf, point_to_id, wires):
    """Union every registered point with each wire it lies on.

    Bucketed by the coarse grid the wire spans so this stays near-linear
    instead of points x wires.
    """
    if not point_to_id or not wires:
        return
    # Index registered points by integer cell.
    cell = 2.54  # mm; schematic features sit on a 1.27 mm grid
    buckets = defaultdict(list)
    for (kx, ky), nid in point_to_id.items():
        buckets[(int(kx // cell), int(ky // cell))].append((kx, ky, nid))

    for w in wires:
        (x1, y1), (x2, y2) = w['p1'], w['p2']
        wire_id = point_to_id.get(pt_key(x1, y1))
        if wire_id is None:
            continue
        lo_x, hi_x = sorted((x1, x2))
        lo_y, hi_y = sorted((y1, y2))
        for cx in range(int((lo_x - cell) // cell), int((hi_x + cell) // cell) + 1):
            for cy in range(int((lo_y - cell) // cell), int((hi_y + cell) // cell) + 1):
                for px, py, nid in buckets.get((cx, cy), ()):
                    if _point_on_segment(px, py, x1, y1, x2, y2):
                        uf.union(wire_id, nid)


def _merge_by_label_name(uf, label_to_node):
    """Union all nets carrying the same label text."""
    by_name = defaultdict(list)
    for nid, text, kind, _shape in label_to_node:
        if text:
            by_name[(kind if kind == 'local' else 'shared', text)].append(nid)
    for ids in by_name.values():
        for other in ids[1:]:
            uf.union(ids[0], other)


def trace_nets(components, wires, labels, junctions, no_connects):
    """
    Build connectivity graph using spatial coincidence.
    Returns list of net dicts.
    """
    uf = UnionFind()
    point_to_id = {}

    def get_id(x, y):
        key = pt_key(x, y)
        if key not in point_to_id:
            new_id = len(point_to_id)
            point_to_id[key] = new_id
            uf.add(new_id)
        return point_to_id[key]

    # Wires connect their two endpoints
    for w in wires:
        a = get_id(*w['p1'])
        b = get_id(*w['p2'])
        uf.union(a, b)

    # Pins
    pin_to_node = []
    for comp in components:
        ref = comp['reference']
        for pin in comp.get('pins', []):
            nid = get_id(pin['x'], pin['y'])
            pin_to_node.append((nid, ref, pin['number'], pin['name'], pin['type']))

    # Labels
    label_to_node = []
    for e in labels['local']:
        label_to_node.append((get_id(*e['pos']), e['text'], 'local', e.get('shape','')))
    for e in labels['global']:
        label_to_node.append((get_id(*e['pos']), e['text'], 'global', e.get('shape','')))
    for e in labels['hierarchical']:
        label_to_node.append((get_id(*e['pos']), e['text'], 'hierarchical', e.get('shape','')))

    # Junctions: just register positions (auto-merges anything at same coord)
    for jx, jy in junctions:
        get_id(jx, jy)

    # No-connects
    nc_nodes = set()
    for ncx, ncy in no_connects:
        nc_nodes.add(uf.find(get_id(ncx, ncy)))

    # Power symbols: their value (e.g. "+3V3") names the net
    power_to_node = []
    for comp in components:
        if comp['reference'].startswith('#PWR') or \
           comp['lib_id'].startswith('power:'):
            for pin in comp.get('pins', []):
                nid = get_id(pin['x'], pin['y'])
                power_to_node.append((nid, comp['value']))

    # FIX: attach anything lying ON a wire, not merely at its endpoints.
    # KiCad connects a pin, label or wire-end that touches a wire anywhere
    # along its length. Matching only exact endpoint coincidence silently
    # splits nets -- the dominant error mode measured against real boards.
    _connect_points_on_wires(uf, point_to_id, wires)

    # FIX: identically-named labels are the same net. KiCad merges local
    # labels within a sheet and global labels across sheets; without this a
    # net drawn as several labelled stubs comes out as several nets.
    _merge_by_label_name(uf, label_to_node)

    # Group by net root
    nets_by_root = defaultdict(lambda: {
        'pins': [], 'labels': [], 'power_names': set(),
        'has_no_connect': False,
    })

    for nid, ref, pnum, pname, ptype in pin_to_node:
        if ref.startswith('#PWR') or ref.startswith('#FLG'):
            continue
        nets_by_root[uf.find(nid)]['pins'].append((ref, pnum, pname, ptype))

    for nid, text, kind, shape in label_to_node:
        nets_by_root[uf.find(nid)]['labels'].append({
            'text': text, 'kind': kind, 'shape': shape
        })

    for nid, name in power_to_node:
        nets_by_root[uf.find(nid)]['power_names'].add(name)

    for root in nc_nodes:
        nets_by_root[root]['has_no_connect'] = True

    # Build final net list
    nets = []
    for root, info in nets_by_root.items():
        name, is_power, is_hier = None, False, False
        if info['power_names']:
            name = sorted(info['power_names'])[0]
            is_power = True
        else:
            for kind in ('global', 'hierarchical', 'local'):
                ms = [l['text'] for l in info['labels'] if l['kind'] == kind]
                if ms:
                    name = sorted(set(ms))[0]
                    if kind == 'hierarchical':
                        is_hier = True
                    break

        nets.append({
            'name': name,
            'pins': sorted(set(info['pins'])),
            'labels': info['labels'],
            'is_power': is_power,
            'is_hierarchical': is_hier,
            'has_no_connect': info['has_no_connect'],
        })

    # ---- Merge nets that share a name -----------------------------------
    # In KiCad, two power symbols with the same value (e.g. two GND
    # placements) form ONE net even if not physically connected by wires.
    # Same for global labels with same text, and hierarchical/local
    # labels with same text WITHIN a sheet. We merge here by name.
    merged = {}  # name -> merged net dict
    unnamed = []  # nets without a name pass through unchanged

    for net in nets:
        if net['name'] is None:
            unnamed.append(net)
            continue
        key = (net['name'], net['is_power'], net['is_hierarchical'])
        if key not in merged:
            merged[key] = {
                'name':            net['name'],
                'pins':            list(net['pins']),
                'labels':          list(net['labels']),
                'is_power':        net['is_power'],
                'is_hierarchical': net['is_hierarchical'],
                'has_no_connect':  net['has_no_connect'],
            }
        else:
            merged[key]['pins'].extend(net['pins'])
            merged[key]['labels'].extend(net['labels'])
            merged[key]['has_no_connect'] |= net['has_no_connect']

    final_nets = []
    for m in merged.values():
        m['pins'] = sorted(set(tuple(p) for p in m['pins']))
        final_nets.append(m)
    final_nets.extend(unnamed)

    return final_nets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_ref_prefix(ref):
    p = ''
    for ch in ref:
        if ch.isalpha() or ch == '_':
            p += ch
        else:
            break
    return p


# `build_bom`, `summarize_by_type`, `extract_power_nets` and `format_report`
# lived here and were unreachable from the moment `Schematic` gained its own
# reporting (§26). Deleted 2026-08-03 (§45). The originals are still in
# `tests/_original_parser.py`, which is the before/after scoring baseline and
# is the only thing that ever called them.


def parse_schematic(filepath, trace=True):
    with open(filepath, 'r', encoding='utf-8') as f:
        root = sexpdata.loads(f.read())
    if sym_name(root) != 'kicad_sch':
        raise ValueError(f"Not a kicad_sch file: root tag {sym_name(root)}")

    title       = extract_title_block(root)
    lib_pins    = extract_lib_symbol_pins(root)
    components  = extract_symbol_instances(root, lib_pins)
    wires       = extract_wires(root)
    labels      = extract_all_labels(root)
    junctions   = extract_junctions(root)
    no_connects = extract_no_connects(root)
    sheets      = extract_hier_sheets(root)

    nets = trace_nets(components, wires, labels, junctions, no_connects) if trace else []
    return format_report(filepath, title, components, labels, no_connects,
                         wires, junctions, sheets, nets, trace)


if __name__ == '__main__':
    args = sys.argv[1:]
    trace = '--no-nets' not in args
    args = [a for a in args if not a.startswith('--')]
    if not args:
        print("Usage: parse_schematic.py <file.kicad_sch> [--no-nets]", file=sys.stderr)
        sys.exit(1)
    try:
        print(parse_schematic(args[0], trace))
    except Exception as e:
        import traceback
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
