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

        def collect(node):
            for child in node[1:]:
                if not isinstance(child, list):
                    continue
                tag = sym_name(child)
                if tag == 'pin':
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
                        pins.append(pin)
                elif tag == 'symbol':
                    collect(child)

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

    Transform order (in lib frame, Y-up):
      1. mirror around symbol origin (in lib frame)
      2. rotate by sym_angle (CCW, standard math rotation)
    Then convert to schematic frame:
      3. flip Y, translate by (sym_x, sym_y)

    KiCad mirror conventions:
    - (mirror x): flip across X axis → invert Y in lib frame
    - (mirror y): flip across Y axis → invert X in lib frame
    """
    x, y = pin_x, pin_y  # in lib frame (Y-up)

    # Step 1: mirror in lib frame
    if mirror == 'x':
        y = -y
    elif mirror == 'y':
        x = -x

    # Step 2: rotate CCW in lib frame (standard math rotation)
    theta = math.radians(sym_angle)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    x_rot = x * cos_t - y * sin_t
    y_rot = x * sin_t + y * cos_t

    # Step 3: convert to schematic frame (Y-down) and translate
    # Flip Y because schematic uses Y-down, lib uses Y-up
    return (sym_x + x_rot, sym_y - y_rot)


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

        comp['reference'] = comp['properties'].get('Reference', '??')
        comp['value']     = comp['properties'].get('Value', '')
        comp['footprint'] = comp['properties'].get('Footprint', '')
        comp['datasheet'] = comp['properties'].get('Datasheet', '')

        comp['pins'] = []
        if lib_id in lib_pins:
            for lp in lib_pins[lib_id]:
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


def build_bom(components):
    groups = defaultdict(list)
    for comp in components:
        ref = comp['reference']
        if ref.startswith('#'):
            continue
        key = (comp['value'], comp['footprint'], comp['lib_id'])
        dnp = comp['dnp'].lower() not in ('no', 'false', '0', '')
        groups[key].append({'ref': ref, 'dnp': dnp})
    bom = []
    for (val, fp, lib), insts in groups.items():
        refs = sorted(set(i['ref'] for i in insts))
        bom.append({'refs': refs, 'count': len(refs),
                    'value': val, 'footprint': fp, 'lib_id': lib,
                    'dnp': any(i['dnp'] for i in insts)})
    bom.sort(key=lambda x: (x['refs'][0][0] if x['refs'] else 'Z', x['refs']))
    return bom


def summarize_by_type(components):
    counts = defaultdict(int)
    for c in components:
        ref = c['reference']
        if ref.startswith('#'):
            continue
        p = get_ref_prefix(ref)
        if p:
            counts[p] += 1
    return dict(sorted(counts.items()))


def extract_power_nets(components):
    nets = set()
    for c in components:
        if c['reference'].startswith('#PWR') or c['lib_id'].startswith('power:'):
            nets.add(c['value'])
    return sorted(nets)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def format_report(filepath, title, components, labels, no_connects,
                  wires, junctions, sheets, nets, trace_enabled):
    lines = []
    fname = Path(filepath).name
    lines.append(f"# KiCad Schematic Analysis: `{fname}`\n")

    if title:
        lines.append("## Schematic Metadata\n")
        lines.append("| Field | Value |")
        lines.append("|-------|-------|")
        for k, v in title.items():
            lines.append(f"| {k.replace('_',' ').title()} | {v} |")
        lines.append("")

    real_comps = [c for c in components if not c['reference'].startswith('#')]
    type_summary = summarize_by_type(components)
    power_nets = extract_power_nets(components)

    lines.append("## Component Summary\n")
    lines.append(f"- Real components: {len(real_comps)}")
    lines.append(f"- Power symbols: "
                 f"{sum(1 for c in components if c['reference'].startswith('#PWR'))}")
    lines.append(f"- Wire segments: {len(wires)}")
    lines.append(f"- Junctions: {len(junctions)}")
    lines.append(f"- No-connect markers: {len(no_connects)}")
    lines.append(f"- Sub-sheets: {len(sheets)}")
    if trace_enabled:
        lines.append(f"- Distinct nets: {len(nets)}")
    lines.append("")

    if type_summary:
        lines.append("### By Reference Type\n")
        lines.append("| Prefix | Count |")
        lines.append("|--------|-------|")
        for p, c in type_summary.items():
            lines.append(f"| {p} | {c} |")
        lines.append("")

    if power_nets:
        lines.append("## Power Rails\n")
        for n in power_nets:
            lines.append(f"- `{n}`")
        lines.append("")

    # ---- Net List ------------------------------------------------------
    if trace_enabled:
        lines.append("## Net List\n")
        lines.append("Each row is a net and the pins connected to it.")
        lines.append("⚡ = power net, ↔ = hierarchical (crosses sheet boundary), 🚫NC = has no-connect marker.\n")
        lines.append("| Net | Pins |")
        lines.append("|-----|------|")

        named   = sorted([n for n in nets if n['name']],
                          key=lambda n: (not n['is_power'], n['name'] or ''))
        unnamed = [n for n in nets if not n['name'] and n['pins']]

        for net in named:
            net_label = f"`{net['name']}`"
            if net['is_power']:        net_label += " ⚡"
            elif net['is_hierarchical']: net_label += " ↔"
            if net['has_no_connect']:  net_label += " 🚫NC"
            pin_strs = [f"{ref}.{pnum}({pname})"
                        for ref, pnum, pname, _ in net['pins']]
            lines.append(f"| {net_label} | {', '.join(pin_strs) if pin_strs else '_(no pins)_'} |")

        for i, net in enumerate(unnamed):
            pin_strs = [f"{ref}.{pnum}({pname})"
                        for ref, pnum, pname, _ in net['pins']]
            tag = "🚫NC" if net['has_no_connect'] else ""
            lines.append(f"| `<unnamed_{i}>` {tag} | {', '.join(pin_strs)} |")
        lines.append("")

        # ---- Per-component pin connections -----------------------------
        lines.append("## Component Pin Connections\n")
        lines.append("For each non-power component, every pin and its net.\n")

        pin_to_netname = {}
        # Build a unique label for each unnamed net so connected pins on
        # the same unnamed net share a label like "(unnamed_5)".
        unnamed_idx = 0
        for net in nets:
            if net['name']:
                # Named net — use the name
                label = net['name']
                if net['has_no_connect']:
                    label += " (+NC)"
            else:
                # Unnamed net
                if net['has_no_connect']:
                    # No-connect net (intentionally unconnected, e.g. ANT_OFF)
                    label = "<NC>"
                elif len(net['pins']) >= 2:
                    # Multiple pins share this unnamed net — give it a tag
                    label = f"(unnamed_{unnamed_idx})"
                    unnamed_idx += 1
                else:
                    # Single pin with no label and no NC — truly floating
                    label = "<floating?>"
            for ref, pnum, pname, ptype in net['pins']:
                pin_to_netname[(ref, pnum)] = label

        sorted_comps = sorted(
            [c for c in components if not c['reference'].startswith('#') and c['pins']],
            key=lambda c: (get_ref_prefix(c['reference']),
                           int(''.join(filter(str.isdigit, c['reference'])) or '0'))
        )
        for comp in sorted_comps:
            ref, val = comp['reference'], comp['value']
            lines.append(f"### {ref} — {val}\n")
            lines.append("| Pin | Name | Type | Net |")
            lines.append("|-----|------|------|-----|")
            pins_sorted = sorted(
                comp['pins'],
                key=lambda p: (int(p['number']) if p['number'].isdigit() else 9999,
                               p['number'])
            )
            for pin in pins_sorted:
                net = pin_to_netname.get((ref, pin['number']), '<not traced>')
                lines.append(f"| {pin['number']} | {pin['name']} | {pin['type']} | {net} |")
            lines.append("")

    # ---- Labels --------------------------------------------------------
    lines.append("## Net Labels (by type)\n")
    if labels['local']:
        names = sorted(set(l['text'] for l in labels['local']))
        lines.append(f"**Local ({len(names)})**: " + ", ".join(f"`{n}`" for n in names))
        lines.append("")
    if labels['global']:
        names = sorted(set(l['text'] for l in labels['global']))
        lines.append(f"**Global ({len(names)})**: " + ", ".join(f"`{n}`" for n in names))
        lines.append("")
    if labels['hierarchical']:
        names = sorted(set(l['text'] for l in labels['hierarchical']))
        lines.append(f"**Hierarchical ({len(names)})**: " + ", ".join(f"`{n}`" for n in names))
        lines.append("")

    # ---- BOM -----------------------------------------------------------
    bom = build_bom(components)
    lines.append("## Bill of Materials\n")
    lines.append("| Refs | Qty | Value | Footprint | Lib | DNP |")
    lines.append("|------|-----|-------|-----------|-----|-----|")
    for it in bom:
        fp = it['footprint'].split(':')[-1] if ':' in it['footprint'] else it['footprint']
        dnp = "DNP" if it['dnp'] else ""
        lines.append(f"| {', '.join(it['refs'])} | {it['count']} | {it['value']} | {fp} | {it['lib_id']} | {dnp} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
