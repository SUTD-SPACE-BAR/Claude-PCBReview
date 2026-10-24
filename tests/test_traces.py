"""Trace decomposition — a net's routed copper as pin-to-pin runs.

Every board here is **built by hand, so the right answer is known by
construction** rather than by looking at a picture. That is the same standard
`test_current.py` holds `bottleneck_mm` to, and it is the only standard
available: the decomposition's whole job is to decide where one run ends and
the next begins, and a real board cannot tell you whether it got that right.

The three cases worth naming, because each is a way the graph can be wrong
while still looking plausible:

* `test_a_tee_splits_into_three_traces` — the shared trunk. This is the case
  the design note calls out: a segment on the trunk lies on every pin-to-pin
  path through it, so the trunk must be **its own edge** rather than being
  attributed to one of them.
* `test_a_via_dropped_mid_track_splits_it` — a track that nothing terminates
  on still has a node in the middle of it, and the graph has to find that node
  from geometry because no endpoint is there.
* `test_the_narrowest_point_limits_the_trace_not_the_significant_one` — the
  difference between this and `current.ampacity`. A net-level capacity drops
  short stretches on purpose; inside one series run there is nothing to drop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import physics, topology
from pcbreview.model import Net, Pad, Track, Via


class _Board:
    """The five things `build_traces` asks a board for.

    A real `Board` needs an extraction to build, and none of these tests want
    one — the point is geometry chosen so the answer is arithmetic.
    """

    def __init__(self, tracks=(), vias=(), pads=(),
                 layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")):
        self.tracks = list(tracks)
        self.vias = list(vias)
        self.pads = list(pads)
        self.copper_layers = list(layers)
        self.zones = []
        self.stackup = None
        self.nets = {1: Net(code=1, name="/N")}

    def net(self, key):
        return self.nets[1]

    def tracks_on_net(self, code):
        return [t for t in self.tracks if t.net == code]

    def vias_on_net(self, code):
        return [v for v in self.vias if v.net == code]

    def pads_on_net(self, code):
        return [p for p in self.pads if p.net == code]


def _track(index, x0, y0, x1, y1, width=0.5, layer="F.Cu"):
    return Track(id=index, net=1, layer=layer, width_mm=width, kind="segment",
                 start=(x0, y0), end=(x1, y1), net_name="/N")


def _pad(index, ref, number, x, y, size=1.0, layers=("F.Cu",)):
    return Pad(ref=ref, number=number, net=1, net_name="/N", shape="circle",
               attribute="smd", pos=(x, y), size_mm=(size, size),
               orientation_deg=0.0, drill_mm=(0.0, 0.0), layers=list(layers),
               copper_layers=list(layers))


def _via(index, x, y, drill=0.3, layers=("F.Cu", "B.Cu")):
    return Via(id=index, net=1, pos=(x, y), drill_mm=drill, via_type="through",
               top_layer=layers[0], bottom_layer=layers[-1],
               layers=list(layers),
               width_by_layer={l: drill + 0.3 for l in layers}, net_name="/N")


def _capacity(width_mm, oz=1.0, rise=10.0, internal=False):
    return physics.current_capacity_a(width_mm, physics.copper_thickness_mm(oz),
                                      rise, internal=internal)


# -- the shape of the graph -----------------------------------------------

def test_a_plain_run_between_two_pads_is_one_trace():
    board = _Board(tracks=[_track(0, 1, 0, 5, 0), _track(1, 5, 0, 9, 0)],
                   pads=[_pad(0, "U1", "3", 1, 0), _pad(1, "R5", "1", 9, 0)])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 1
    # Two tracks meeting at a corner is one run, not two: the joint has degree
    # two and nothing terminates on it.
    assert len(traces[0].strands) == 2
    assert traces[0].name in ("U1.3 → R5.1", "R5.1 → U1.3")
    assert traces[0].length_mm == pytest.approx(8.0)


def test_a_tee_splits_into_three_traces():
    """The shared trunk is its own edge.

    `U1.1` feeds a junction which fans out to `R1.1` and `R2.1`. The trunk lies
    on both pin-to-pin paths, so attributing it to either would be a choice
    with no basis; it is the third edge.
    """
    board = _Board(
        tracks=[_track(0, 0, 0, 10, 0),      # trunk
                _track(1, 10, 0, 20, 5),     # branch a
                _track(2, 10, 0, 20, -5)],   # branch b
        pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "R1", "1", 20, 5),
              _pad(2, "R2", "1", 20, -5)])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 3
    names = {t.name for t in traces}
    trunk = [t for t in traces if t.length_mm == pytest.approx(10.0)]
    assert len(trunk) == 1
    assert {e.kind for e in trunk[0].ends} == {"pad", "junction"}
    assert any("R1.1" in n for n in names) and any("R2.1" in n for n in names)


def test_a_via_dropped_mid_track_splits_it():
    """No endpoint sits at the via, so the split has to come from geometry."""
    board = _Board(tracks=[_track(0, 0, 0, 20, 0)],
                   vias=[_via(0, 10, 0)],
                   pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "U2", "1", 20, 0)])
    traces = topology.build_traces(board, 1)
    # The via has degree two, so it is a pass-through and the run stays one
    # trace — but it is now two strands of the same track, which is what lets
    # a genuine junction at the same place split it.
    assert len(traces) == 1
    assert len(traces[0].strands) == 2
    assert traces[0].length_mm == pytest.approx(20.0)
    assert traces[0].via_ids == [0]


def test_a_via_a_third_conductor_meets_is_a_junction():
    board = _Board(tracks=[_track(0, 0, 0, 20, 0),
                           _track(1, 10, 0, 10, 8, layer="B.Cu")],
                   vias=[_via(0, 10, 0)],
                   pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "U2", "1", 20, 0),
                         _pad(2, "U3", "1", 10, 8, layers=("B.Cu",))])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 3
    assert all(len(t.strands) == 1 for t in traces)


def test_a_run_through_a_via_keeps_going_onto_the_other_layer():
    """A trace is not per-layer. A run that drops to B.Cu and continues is one
    conductor between the same two pins, and cutting it at the via would
    report two halves of a thing nobody built as two things."""
    board = _Board(tracks=[_track(0, 0, 0, 10, 0),
                           _track(1, 10, 0, 20, 0, layer="B.Cu")],
                   vias=[_via(0, 10, 0)],
                   pads=[_pad(0, "U1", "1", 0, 0),
                         _pad(1, "U2", "1", 20, 0, layers=("B.Cu",))])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 1
    assert traces[0].layers == ["B.Cu", "F.Cu"]
    assert traces[0].via_ids == [0]


def test_a_stub_is_an_edge_too():
    """A run from a junction to nothing. Worth keeping: a stub with a neck in
    it is a real finding, and dropping it would hide copper that exists."""
    board = _Board(
        tracks=[_track(0, 0, 0, 10, 0), _track(1, 10, 0, 20, 0),
                _track(2, 10, 0, 10, 6)],
        pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "U2", "1", 20, 0)])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 3
    stub = [t for t in traces if any(e.kind == "open" for e in t.ends)]
    assert len(stub) == 1
    assert stub[0].length_mm == pytest.approx(6.0)


def test_a_pad_joins_a_track_that_ends_inside_it_not_at_its_centre():
    """A router stops a track when it reaches the pad, not at the pad origin,
    so coordinate equality would never join the two."""
    board = _Board(tracks=[_track(0, 0.4, 0, 10, 0)],       # stops inside U1.1
                   pads=[_pad(0, "U1", "1", 0, 0, size=1.2),
                         _pad(1, "U2", "1", 10, 0)])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 1
    assert {e.name for e in traces[0].ends} == {"U1.1", "U2.1"}


def test_a_ring_is_reported_rather_than_dropped():
    """Every node has degree two, so a walk that starts from terminals finds
    nothing. A net routed as a loop is unusual and is not nothing."""
    board = _Board(tracks=[_track(0, 0, 0, 10, 0), _track(1, 10, 0, 10, 10),
                           _track(2, 10, 10, 0, 10), _track(3, 0, 10, 0, 0)])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 1
    assert traces[0].length_mm == pytest.approx(40.0)


def test_a_net_with_no_tracks_has_no_traces():
    """A poured net is not a trace — `bottleneck_mm` is documented as invalid
    on a plane and `plane_resistance` is what that case has."""
    board = _Board(pads=[_pad(0, "U1", "1", 0, 0)])
    assert topology.build_traces(board, 1) == []


# -- the measurement ------------------------------------------------------

def test_the_narrowest_point_limits_the_trace_not_the_significant_one():
    """The difference from `current.ampacity` in one assertion.

    A 0.2 mm entry 0.5 mm long on a 40 mm run of 1.0 mm is under any
    significance floor, and it still carries the whole current.
    """
    # The landing pad is 0.3 mm across on purpose. At 1.0 mm it swallows the
    # whole 0.5 mm entry — the pad's copper reaches back past where the wide
    # track ends — and the entry stops being a strand at all. That is correct
    # behaviour and a useless test.
    board = _Board(tracks=[_track(0, 0, 0, 40, 0, width=1.0),
                           _track(1, 40, 0, 40.5, 0, width=0.2)],
                   pads=[_pad(0, "U1", "1", 0, 0),
                         _pad(1, "U2", "1", 40.5, 0, size=0.3)])
    trace = topology.build_traces(board, 1)[0]
    assert trace.narrowest_mm == pytest.approx(0.2)
    assert trace.capacity_a == pytest.approx(round(_capacity(0.2), 3))


def test_each_branch_of_a_tee_carries_its_own_capacity():
    """The whole point of the exercise: a 2 mm trunk and a 0.2 mm branch on
    one net get two different numbers, where `ampacity()` gives the net one."""
    board = _Board(
        tracks=[_track(0, 0, 0, 10, 0, width=2.0),
                _track(1, 10, 0, 20, 5, width=2.0),
                _track(2, 10, 0, 20, -5, width=0.2)],
        pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "C1", "1", 20, 5),
              _pad(2, "D1", "1", 20, -5)])
    by_name = {t.name: t for t in topology.build_traces(board, 1)}
    fat = next(t for n, t in by_name.items() if "C1.1" in n)
    thin = next(t for n, t in by_name.items() if "D1.1" in n)
    assert fat.capacity_a == pytest.approx(round(_capacity(2.0), 3))
    assert thin.capacity_a == pytest.approx(round(_capacity(0.2), 3))
    # IPC-2152 capacity goes as W^0.575, so ten times the width is ~3.8x
    assert fat.capacity_a > 3 * thin.capacity_a


def test_an_inner_layer_run_carries_what_its_own_copper_carries():
    """Each strand takes its own layer's copper weight — the same rule
    `ampacity()` uses, so the two cannot disagree. Inner copper defaults to
    0.5 oz and carries less for that reason alone: IPC-2152 is one chart for
    every layer, and IPC-2221's halving of inner capacity is gone."""
    outer = _Board(tracks=[_track(0, 0, 0, 10, 0, width=0.5)],
                   pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "U2", "1", 10, 0)])
    inner = _Board(tracks=[_track(0, 0, 0, 10, 0, width=0.5, layer="In1.Cu")],
                   pads=[_pad(0, "U1", "1", 0, 0, layers=("In1.Cu",)),
                         _pad(1, "U2", "1", 10, 0, layers=("In1.Cu",))])
    a = topology.build_traces(outer, 1)[0]
    b = topology.build_traces(inner, 1)[0]
    assert a.capacity_a > b.capacity_a
    assert b.capacity_a == pytest.approx(
        round(_capacity(0.5, oz=0.5, internal=True), 3))
    same_weight = topology.build_traces(inner, 1, {"In1.Cu": 1.0})[0]
    assert same_weight.capacity_a == pytest.approx(a.capacity_a)


def test_a_via_in_series_is_reported_beside_the_copper_never_merged_into_it():
    """`ampacity()`'s docstring says which vias are in series with which
    stretch is "a topology question this does not answer". This graph answers
    it — so the barrel is reported *and kept separate*, because a run limited
    by a via and one limited by a neck want different fixes."""
    board = _Board(tracks=[_track(0, 0, 0, 10, 0, width=2.0),
                           _track(1, 10, 0, 20, 0, width=2.0)],
                   vias=[_via(0, 10, 0, drill=0.2)],
                   pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "U2", "1", 20, 0)])
    trace = topology.build_traces(board, 1)[0]
    assert trace.capacity_a == pytest.approx(round(_capacity(2.0), 3))
    assert trace.via_capacity_a is not None
    assert trace.via_capacity_a < trace.capacity_a
    assert trace.limit_a == trace.via_capacity_a
    # and the copper number is still there, unmodified, beside it
    assert trace.summary()["capacity_a"] > trace.summary()["via_capacity_a"]


def test_a_via_at_an_end_is_not_charged_to_this_trace():
    """It is shared with whatever is on the other side of that node, and
    counting it on both sides would double-count one barrel."""
    board = _Board(tracks=[_track(0, 0, 0, 10, 0),
                           _track(1, 10, 0, 20, 5),
                           _track(2, 10, 0, 20, -5)],
                   vias=[_via(0, 10, 0, drill=0.2)],
                   pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "R1", "1", 20, 5),
                         _pad(2, "R2", "1", 20, -5)])
    traces = topology.build_traces(board, 1)
    assert len(traces) == 3
    assert all(t.via_capacity_a is None for t in traces)


def test_copper_weights_come_from_the_stackup_when_given():
    board = _Board(tracks=[_track(0, 0, 0, 10, 0, width=0.5)],
                   pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "U2", "1", 10, 0)])
    heavy = topology.build_traces(board, 1, {"F.Cu": 2.0})[0]
    light = topology.build_traces(board, 1, {"F.Cu": 1.0})[0]
    assert heavy.capacity_a > light.capacity_a
    assert heavy.capacity_a == pytest.approx(round(_capacity(0.5, oz=2.0), 3))


def test_every_track_is_accounted_for_exactly_once():
    """Copper that belongs to no trace is copper the inspector cannot explain,
    and a strand counted twice is length that does not exist."""
    board = _Board(
        tracks=[_track(0, 0, 0, 10, 0), _track(1, 10, 0, 20, 5),
                _track(2, 10, 0, 20, -5), _track(3, 20, 5, 30, 5),
                _track(4, 30, 5, 30, 15, layer="B.Cu")],
        vias=[_via(0, 30, 5)],
        pads=[_pad(0, "U1", "1", 0, 0), _pad(1, "R1", "1", 20, -5),
              _pad(2, "R2", "1", 30, 15, layers=("B.Cu",))])
    traces = topology.build_traces(board, 1)
    total = sum(t.length_mm for t in traces)
    assert total == pytest.approx(sum(t.length_mm for t in board.tracks))
    covered = sorted(i for t in traces for i in t.track_ids)
    assert covered == sorted({i for t in traces for i in t.track_ids})
    assert set(covered) == {0, 1, 2, 3, 4}
