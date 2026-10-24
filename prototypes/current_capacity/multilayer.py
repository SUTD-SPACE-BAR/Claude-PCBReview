"""Multi-layer bottleneck: erode every layer together, keep vias as links.

A net's copper is a 3-D network: pieces of copper on each layer, joined by
vias and through-hole pads. Eroding one layer at a time answers the wrong
question, because the current can hop layers. This erodes all layers by the
same r and rebuilds connectivity through the vias (a via survives while its
own land does), then reports the largest r for which source and sink are
still joined -- and, separately, how many vias the surviving path uses,
because a via is a current limit in its own right.
"""
import pathlib
import sys, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from shapely.geometry import Point
from shapely.strtree import STRtree
from pcbreview.model import Board
from pcbreview import physics

OZ = {"F.Cu": 1.0, "B.Cu": 1.0, "In1.Cu": 0.5, "In2.Cu": 0.5}
INTERNAL = {"In1.Cu", "In2.Cu"}


def pieces(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in geom.geoms if g.geom_type == "Polygon"]


class Net3D:
    def __init__(self, board, net):
        self.b, self.net = board, net
        self.layers = list(board.copper_layers)
        self.cu = {L: board.copper_on_layer(L, net) for L in self.layers}
        self.vias = [v for v in board.vias if self._name(v.net) == net]
        self.thpads = [p for p in board.pads
                       if self._name(p.net) == net and not p.is_smd
                       and len([l for l in p.layers if l.endswith(".Cu")]) > 1]

    def _name(self, code):
        try:
            return self.b.net(code).name
        except KeyError:
            return None

    def connected_at(self, r, a_pt, b_pt):
        """Union-find over eroded per-layer pieces + via links. Returns
        (connected, n_pieces_in_path_component, vias_used_lower_bound)."""
        nodes = []          # (layer, polygon)
        trees = {}
        for L in self.layers:
            g = self.cu[L].buffer(-r, join_style=2) if r > 0 else self.cu[L]
            ps = pieces(g)
            start = len(nodes)
            nodes.extend((L, p) for p in ps)
            trees[L] = (STRtree(ps) if ps else None, ps, start)

        parent = list(range(len(nodes)))
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        def union(i, j):
            i, j = find(i), find(j)
            if i != j: parent[i] = j

        probe = max(r + 0.01, 0.01)
        links = 0
        for conn in list(self.vias) + list(self.thpads):
            pt = Point(*conn.pos).buffer(probe)
            hit = []
            for L in self.layers:
                tree, ps, start = trees[L]
                if tree is None: continue
                for idx in tree.query(pt):
                    if ps[idx].intersects(pt):
                        hit.append(start + idx)
            for k in hit[1:]:
                union(hit[0], k); links += 1

        def owner(pt):
            g = pt.buffer(probe)
            for L in self.layers:
                tree, ps, start = trees[L]
                if tree is None: continue
                for idx in tree.query(g):
                    if ps[idx].intersects(g):
                        return find(start + idx)
            return None

        oa, ob = owner(a_pt), owner(b_pt)
        return (oa is not None and oa == ob), len(nodes), links

    def bottleneck(self, a_pt, b_pt, hi=8.0, tol=0.002):
        ok, _, _ = self.connected_at(0.0, a_pt, b_pt)
        if not ok:
            return None
        lo = 0.0
        while hi - lo > tol:
            mid = (lo + hi) / 2
            if self.connected_at(mid, a_pt, b_pt)[0]:
                lo = mid
            else:
                hi = mid
        return 2 * lo


def via_capacity_a(drill_mm, plating_um=25.0, rise_c=10.0):
    """A via barrel as an IPC-2221 conductor: width = circumference, t = plating."""
    return physics.current_capacity_a(3.14159265 * drill_mm, plating_um / 1000.0,
                                      rise_c, internal=False)
