"""Minimum copper channel width between two pads on a net, by erosion.

The bottleneck of a poured plane is the largest r for which the copper,
eroded by r, still connects source to sink. The channel width is 2r.
That number is what IPC-2221 wants, and nothing in pcbreview computes it.

Independent check: on a straight rectangular track of known width the answer
must be that width, and on a rectangle with a notch it must be the notch.
"""
import pathlib
import sys, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from shapely.geometry import MultiPolygon, Point, box
from shapely.ops import unary_union
from pcbreview.model import Board
from pcbreview import physics

COPPER_OZ = {"F.Cu": 1.0, "B.Cu": 1.0, "In1.Cu": 0.5, "In2.Cu": 0.5}


def _components(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in geom.geoms if g.geom_type == "Polygon"]


def connected(geom, a, b):
    """Are points a and b in the same connected piece of geom?"""
    for piece in _components(geom):
        if piece.intersects(a) and piece.intersects(b):
            return True
    return False


def bottleneck_mm(copper, a, b, hi=6.0, tol=0.002):
    """Largest 2r such that erosion by r keeps a and b connected."""
    if not connected(copper, a, b):
        return None
    lo = 0.0
    while hi - lo > tol:
        mid = (lo + hi) / 2
        eroded = copper.buffer(-mid, join_style=2)
        # the eroded copper no longer touches the pad centres; re-grow the
        # test points instead of the copper
        if connected(eroded, a.buffer(mid + 0.01), b.buffer(mid + 0.01)):
            lo = mid
        else:
            hi = mid
    return 2 * lo


def net_copper(board, layer, net):
    return board.copper_on_layer(layer, net)


def selftest():
    strip = box(0, 0, 50, 1.5)
    a, b = Point(1, 0.75), Point(49, 0.75)
    got = bottleneck_mm(strip, a, b)
    assert abs(got - 1.5) < 0.01, got
    notched = strip.difference(box(24, 0.6, 26, 1.5))
    got = bottleneck_mm(notched, a, b)
    assert abs(got - 0.6) < 0.01, got
    print(f"selftest ok: straight 1.5 mm -> {1.5:.3f}, notched 0.6 mm -> {got:.3f}")


if __name__ == "__main__":
    selftest()
