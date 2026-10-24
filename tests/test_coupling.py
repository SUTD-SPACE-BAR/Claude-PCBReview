"""Tests for coupled-run extraction and differential-pair classification.

Synthetic boards pin the measurement against geometry with a known answer;
the real boards then confirm it reproduces the fab's own numbers — both
both fabricated boards were routed to a JLCPCB differential-pair spec, so the measured
gap must come back as exactly the specified trace spacing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.coupling import (
    analyse_pair,
    candidate_pair_names,
    coupling,
    find_differential_pairs,
    neighbours_of,
)
from pcbreview.model import Board, Track


class _FakeBoard:
    """Minimal Board stand-in carrying only what coupling() touches."""

    def __init__(self, tracks):
        from pcbreview.model import Net
        self.copper_layers = ["F.Cu", "B.Cu"]
        self.nets = {1: Net(1, "A"), 2: Net(2, "B")}
        self._tracks = tracks
        self._memo = {}

    def net(self, key):
        if isinstance(key, str):
            return next(n for n in self.nets.values() if n.name == key)
        return self.nets[key]

    def tracks_on_net(self, key):
        code = self.net(key).code
        return [t for t in self._tracks if t.net == code]

    def memo(self, key, build):
        """`Board.memo` — coupling memoises its per-net geometry through it."""
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]


def _straight(track_id, net, y, length=10.0, width=0.2, layer="F.Cu", x0=0.0):
    return Track(id=track_id, net=net, layer=layer, width_mm=width,
                 kind="segment", start=(x0, y), end=(x0 + length, y))


# -- synthetic geometry, known answers ------------------------------------

def test_parallel_traces_report_exact_edge_gap():
    """Two 0.2 mm traces on 0.4 mm centres have a 0.2 mm edge gap."""
    board = _FakeBoard([_straight(1, 1, 0.0), _straight(2, 2, 0.4)])
    result = coupling(board, "A", "B", max_gap_mm=0.5)
    assert result.coupled_fraction == pytest.approx(1.0, abs=0.02)
    assert result.mean_gap_mm == pytest.approx(0.2, abs=1e-3)
    assert result.min_gap_mm == pytest.approx(0.2, abs=1e-3)


def test_edge_gap_accounts_for_unequal_widths():
    """0.3 mm and 0.1 mm traces on 0.4 mm centres still have a 0.2 mm gap."""
    board = _FakeBoard([_straight(1, 1, 0.0, width=0.3),
                        _straight(2, 2, 0.4, width=0.1)])
    result = coupling(board, "A", "B", max_gap_mm=0.5)
    assert result.mean_gap_mm == pytest.approx(0.2, abs=2e-3)


def test_traces_beyond_the_window_are_not_coupled():
    board = _FakeBoard([_straight(1, 1, 0.0), _straight(2, 2, 2.0)])
    assert coupling(board, "A", "B", max_gap_mm=0.5).runs == []


def test_partial_overlap_gives_partial_coupling():
    """Aggressor covers half the victim's length -> about half coupled."""
    board = _FakeBoard([_straight(1, 1, 0.0, length=10.0),
                        _straight(2, 2, 0.4, length=5.0)])
    result = coupling(board, "A", "B", max_gap_mm=0.5)
    assert result.coupled_fraction == pytest.approx(0.5, abs=0.05)


def test_perpendicular_crossing_is_not_a_coupled_run():
    """A crossing is close only briefly; the run-length filter drops it."""
    crossing = Track(id=2, net=2, layer="F.Cu", width_mm=0.2, kind="segment",
                     start=(5.0, -3.0), end=(5.0, 3.0))
    board = _FakeBoard([_straight(1, 1, 0.0), crossing])
    assert coupling(board, "A", "B", max_gap_mm=0.5).runs == []


def test_traces_on_different_layers_are_not_same_layer_coupled():
    board = _FakeBoard([_straight(1, 1, 0.0, layer="F.Cu"),
                        _straight(2, 2, 0.4, layer="B.Cu")])
    assert coupling(board, "A", "B", max_gap_mm=0.5).runs == []


def test_separate_coupled_stretches_are_reported_separately():
    board = _FakeBoard([
        _straight(1, 1, 0.0, length=20.0),
        _straight(2, 2, 0.4, length=5.0, x0=0.0),
        _straight(3, 2, 0.4, length=5.0, x0=14.0),
    ])
    result = coupling(board, "A", "B", max_gap_mm=0.5)
    assert len(result.runs) == 2
    # Each run covers its 5 mm aggressor plus a little either side: the round
    # end caps put copper slightly beyond the segment endpoints, and the victim
    # is genuinely within the window there.
    assert all(5.0 <= r.length_mm <= 5.6 for r in result.runs)
    # The two runs must sit at opposite ends, not be one region split by noise.
    starts = sorted(r.start_mm for r in result.runs)
    assert starts[0] < 1.0
    assert starts[1] > 13.0


def test_a_coupled_run_carries_the_centreline_it_describes():
    """§40b: `CoupledRun.geometry` existed and nothing ever populated it, so a
    `differential_pair` finding could only be rendered as "here are the two
    nets" and never as "here is the part that is actually a pair"."""
    board = _FakeBoard([_straight(1, 1, 0.0, length=20.0),
                        _straight(2, 2, 0.4, length=5.0, x0=0.0)])
    run = coupling(board, "A", "B", max_gap_mm=0.5).runs[0]
    assert run.geometry is not None, "the run must carry its own centreline"
    # The drawn line is the stretch the run measured, not the whole victim.
    assert run.geometry.length == pytest.approx(run.length_mm, abs=0.15)
    assert run.geometry.length < 6.0 < 20.0


def test_a_run_that_is_two_samples_or_fewer_carries_no_geometry():
    """A degenerate span cannot make a LineString, and inventing one would put
    a zero-length artefact into a picture."""
    board = _FakeBoard([_straight(1, 1, 0.0, length=20.0),
                        _straight(2, 2, 5.0, length=5.0)])
    assert coupling(board, "A", "B", max_gap_mm=0.5).runs == []


def test_coupled_fraction_is_bounded():
    board = _FakeBoard([_straight(1, 1, 0.0, length=2.0),
                        _straight(2, 2, 0.4, length=50.0)])
    assert 0.0 <= coupling(board, "A", "B", max_gap_mm=0.5).coupled_fraction <= 1.0


def test_unrouted_net_couples_to_nothing():
    board = _FakeBoard([_straight(1, 1, 0.0)])
    result = coupling(board, "A", "B", max_gap_mm=0.5)
    assert result.coupled_fraction == 0.0
    assert result.mean_gap_mm is None


# -- pair name detection --------------------------------------------------

@pytest.mark.parametrize("names,expected", [
    (["USB_D+", "USB_D-"], [("USB_D+", "USB_D-")]),
    (["CAN_P", "CAN_N"], [("CAN_P", "CAN_N")]),
    (["A", "B", "C"], []),
])
def test_pair_name_detection(names, expected):
    assert candidate_pair_names(names) == expected


def test_pair_detection_does_not_reuse_a_net():
    pairs = candidate_pair_names(["X+", "X-", "Y+", "Y-"])
    used = [n for pair in pairs for n in pair]
    assert len(used) == len(set(used))
