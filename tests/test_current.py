"""Tests for conducted current — §32.B9.

Both solvers are checked against closed form, which is the only reason either
is trusted: `bottleneck_mm` against a strip and a notched strip whose answers
are known by construction, and `plane_resistance` against ``V = I*Rsheet*L/W``.

Two mistakes are pinned here on purpose, both of which produced a *plausible*
wrong number rather than an error:

* `test_erosion_is_not_a_plane_measurement` — a field of via antipads reads as
  a constriction under erosion and is not one. Taken at face value on a real
  40 A plane that reading said 3.4 A.
* `test_two_terminals_sharing_cells_do_not_make_it_singular` and
  `test_a_disconnected_island_does_not_make_it_singular` — both crashed the
  solve on real boards, on the second and third it was run against.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import MultiPolygon, Point, box

from pcbreview import current, physics


# -- the narrowest channel ------------------------------------------------

def test_a_plain_strip_reads_its_own_width():
    strip = box(0, 0, 50, 1.5)
    got = current.bottleneck_mm(strip, Point(1, 0.75), Point(49, 0.75))
    assert got == pytest.approx(1.5, abs=0.01)


def test_a_notched_strip_reads_the_notch():
    strip = box(0, 0, 50, 1.5).difference(box(24, 0.6, 26, 1.5))
    got = current.bottleneck_mm(strip, Point(1, 0.75), Point(49, 0.75))
    assert got == pytest.approx(0.6, abs=0.01)


def test_disconnected_copper_reads_none_rather_than_zero():
    """None is 'these are not joined'; 0.0 would read as 'joined by nothing'."""
    apart = MultiPolygon([box(0, 0, 10, 2), box(20, 0, 30, 2)])
    assert current.bottleneck_mm(apart, Point(5, 1), Point(25, 1)) is None


def test_erosion_is_not_a_plane_measurement():
    """A pour perforated by antipads erodes into pieces long before its real
    conducting width is reached, so erosion reports a constriction that carries
    no current penalty. This is why `worst_feed` uses resistance on poured nets
    and it is the trap the module docstring exists for."""
    plane = box(0, 0, 60, 40)
    holes = [Point(x, y).buffer(0.8)
             for x in range(4, 58, 4) for y in range(4, 38, 4)]
    perforated = plane.difference(MultiPolygon(holes).buffer(0))
    eroded_width = current.bottleneck_mm(perforated, Point(1, 20),
                                         Point(59, 20))
    # the copper is 40 mm wide and erosion reports a small fraction of it
    assert eroded_width is not None and eroded_width < 4.0

    # the resistance, which is the truth, is barely affected by the holes
    solid = current.plane_resistance(
        plane, 1.0, {"a": [(0.5, y) for y in range(1, 40)],
                     "b": [(59.5, y) for y in range(1, 40)]}, "a")
    drilled = current.plane_resistance(
        perforated, 1.0, {"a": [(0.5, y) for y in range(1, 40)],
                          "b": [(59.5, y) for y in range(1, 40)]}, "a")
    assert drilled.resistance_ohm["b"] < 2.0 * solid.resistance_ohm["b"]


# -- the plane solve ------------------------------------------------------

def test_a_rectangle_matches_the_closed_form():
    """R = Rsheet * L / W, with the conducting span shortened by the two
    electrodes. The whole plane solve rests on this one."""
    length, width, oz = 40.0, 12.0, 1.0
    sheet = current.sheet_resistance_ohm_sq(oz)
    solution = current.plane_resistance(
        box(0, 0, length, width), oz,
        {"a": [(0.3, y / 4) for y in range(2, 4 * int(width))],
         "b": [(length - 0.3, y / 4) for y in range(2, 4 * int(width))]}, "a")
    exact = sheet * (length - 2 * 0.45 - 0.6) / width
    assert solution.resistance_ohm["b"] == pytest.approx(exact, rel=0.06)


def test_sheet_resistance_is_the_textbook_value():
    """1 oz copper is 34.8 um, so rho/t = 1.72e-8 / 34.8e-6 = 0.494 mOhm/sq."""
    assert current.sheet_resistance_ohm_sq(1.0) == pytest.approx(4.94e-4,
                                                                 rel=0.01)
    assert current.sheet_resistance_ohm_sq(0.5) == pytest.approx(9.89e-4,
                                                                 rel=0.01)


def test_resistance_scales_with_length_and_inversely_with_width():
    oz = 1.0
    def solve(length, width):
        return current.plane_resistance(
            box(0, 0, length, width), oz,
            {"a": [(0.3, y / 2) for y in range(1, 2 * int(width))],
             "b": [(length - 0.3, y / 2) for y in range(1, 2 * int(width))]},
            "a").resistance_ohm["b"]
    base = solve(40, 10)
    assert solve(80, 10) / base == pytest.approx(2.0, rel=0.10)
    assert solve(40, 20) / base == pytest.approx(0.5, rel=0.15)


def test_two_terminals_sharing_cells_do_not_make_it_singular():
    """Electrodes close enough to claim the same grid cells left one reduced
    node with an all-zero row. Exactly singular, with no other symptom."""
    solution = current.plane_resistance(
        box(0, 0, 40, 10), 1.0,
        {"a": [(1.0, 5.0)], "b": [(1.1, 5.0)], "c": [(39.0, 5.0)]}, "a")
    assert "c" in solution.resistance_ohm
    assert solution.resistance_ohm["c"] > 0


def test_a_disconnected_island_does_not_make_it_singular():
    """A pour with a detached island has no Dirichlet reference in that island.
    The island is dropped rather than crashing the factorisation."""
    pour = MultiPolygon([box(0, 0, 40, 10), box(60, 0, 70, 10)])
    solution = current.plane_resistance(
        pour, 1.0, {"a": [(1.0, 5.0)], "b": [(39.0, 5.0)]}, "a")
    assert solution.resistance_ohm["b"] > 0


def test_a_source_that_is_alone_returns_no_resistances():
    """Not an exception: a net whose only located terminal is its source is a
    fact about the board, and the check reports it as coverage."""
    solution = current.plane_resistance(
        box(0, 0, 40, 10), 1.0, {"a": [(1.0, 5.0)]}, "a")
    assert solution.resistance_ohm == {}


# -- which region of the pour gets solved ---------------------------------
#
# A pour on one layer is several disjoint polygons, and `plane_resistance`
# solves one of them. Picking the largest was the rule; on a real 6-layer
# board it returned **zero** feeds for the net with 49 terminals, because the
# source sat on the other piece. These pin the ordering that replaced it.

def _two_regions():
    """A big region carrying three terminals, a small one carrying the source.

    Deliberately the shape that broke: the source has the most pad copper *and*
    is on the region that is not the biggest.
    """
    return [box(0, 0, 40, 10), box(60, 0, 70, 10)]


def test_the_region_with_the_most_terminals_wins_not_the_largest():
    big, small = _two_regions()
    contacts = {"src": [(65.0, 5.0)], "a": [(2.0, 5.0)],
                "b": [(20.0, 5.0)], "c": [(38.0, 5.0)]}
    region, on_region = current._solve_region([big, small], contacts)
    assert region.area == big.area
    assert set(on_region) == {"a", "b", "c"}


def test_area_only_breaks_a_tie_in_terminal_count():
    big, small = _two_regions()
    contacts = {"a": [(2.0, 5.0)], "b": [(38.0, 5.0)],
                "c": [(61.0, 5.0)], "d": [(69.0, 5.0)]}
    region, on_region = current._solve_region([big, small], contacts)
    assert region.area == big.area          # 2 each, so the larger sheet wins
    assert set(on_region) == {"a", "b"}


def test_a_terminal_straddling_two_regions_keeps_only_the_solved_ones():
    """Its contacts on the other piece carry no current into this sheet, so
    counting them would overstate how well the terminal is stitched."""
    big, small = _two_regions()
    contacts = {"a": [(2.0, 5.0)], "b": [(20.0, 5.0)],
                "c": [(38.0, 5.0), (65.0, 5.0)]}
    _, on_region = current._solve_region([big, small], contacts)
    assert on_region["c"] == [(38.0, 5.0)]


def test_a_via_in_its_own_thermal_relief_is_still_on_the_region():
    """Region membership and the electrode search have to use one tolerance.

    A via on a pour sits in a clearance hole, so its centre is *outside* the
    polygon. Deciding membership by strict containment would rule it off the
    copper it is plainly connected to — and `plane_resistance` would then never
    be asked about a terminal it would happily have solved.
    """
    relief = Point(20.0, 5.0).buffer(0.3)
    pour = box(0, 0, 40, 10).difference(relief)
    assert not pour.contains(Point(20.0, 5.0))          # the trap

    _, on_region = current._solve_region([pour], {"a": [(20.0, 5.0)]})
    assert "a" in on_region

    # and the solve agrees: it finds copper for that electrode
    solution = current.plane_resistance(
        pour, 1.0, {"a": [(20.0, 5.0)], "b": [(39.0, 5.0)]}, "a")
    assert solution.resistance_ohm.get("b", 0) > 0


def test_a_contact_on_no_region_at_all_selects_nothing():
    """Not an exception and not a silent fallback to the biggest polygon: an
    empty result is what lets the caller say which of the two it was."""
    region, on_region = current._solve_region(_two_regions(),
                                              {"a": [(100.0, 100.0)]})
    assert region is None and on_region == {}


def test_choosing_the_largest_region_is_what_returned_nothing():
    """The regression itself, end to end through the solver.

    Same copper, same terminals, two choices of region. The largest-region rule
    hands `plane_resistance` a sheet with no source electrode on it, and it
    correctly returns nothing — the loss happened in the choice, not the solve.
    """
    big, small = _two_regions()
    contacts = {"src": [(65.0, 5.0)], "a": [(2.0, 5.0)],
                "b": [(20.0, 5.0)], "c": [(38.0, 5.0)]}

    largest_first = current.plane_resistance(big, 1.0, contacts, "src")
    assert largest_first.resistance_ohm == {}

    region, on_region = current._solve_region([big, small], contacts)
    source = max(on_region, key=lambda r: len(r))   # any terminal on it
    fixed = current.plane_resistance(region, 1.0, on_region, source)
    assert len(fixed.resistance_ohm) == 2
    assert all(v > 0 for v in fixed.resistance_ohm.values())


# -- vias -----------------------------------------------------------------

def test_via_capacity_matches_ipc2152_on_the_barrel():
    """A barrel is a conductor of width pi*d and thickness = plating."""
    import math
    expected = physics.current_capacity_a(math.pi * 0.5, 0.025, 10.0,
                                          internal=False)
    assert current.via_capacity_a(0.5, 25.0, 10.0) == pytest.approx(expected)
    # and the number itself, so a change in the constants is visible
    # (IPC-2221 gave 2.6 and 1.8)
    assert current.via_capacity_a(0.5) == pytest.approx(2.29, abs=0.02)
    assert current.via_capacity_a(0.3) == pytest.approx(1.71, abs=0.02)


def test_a_bigger_via_and_a_hotter_rise_both_carry_more():
    assert current.via_capacity_a(0.8) > current.via_capacity_a(0.3)
    assert current.via_capacity_a(0.5, rise_c=20.0) > current.via_capacity_a(0.5)


# -- the check ------------------------------------------------------------

def test_the_check_reports_coverage_even_with_nothing_to_say():
    """A power net with two terminals has no population to compare against,
    and that must be distinguishable from a net that compared clean."""
    from pcbreview.audit import check_current_capacity

    class _Board:
        pads, vias, zones, tracks = [], [], [], []
        copper_layers = ["F.Cu", "B.Cu"]
        stackup = None

        def net(self, code):
            raise KeyError(code)

        def tracks_on_net(self, key):
            return []

        def zone_fill(self, layer, net=None):
            return MultiPolygon()

        def copper_on_layer(self, layer, net=None):
            return MultiPolygon()

    findings = check_current_capacity(_Board(), {"VBUS"}, {"F.Cu": 1.0})
    # the board-wide width table plus the coverage statement; both `info`
    assert [f.severity for f in findings] == ["info", "info"]
    coverage = next(f for f in findings if "coverage" in f.measurement)
    assert coverage.measurement["coverage"]["power_nets"] == 1
    assert "VBUS" in coverage.measurement["coverage"][
        "nets_with_too_few_terminals"]


# -- §46: capacity variation along one net --------------------------------

class _Track:
    def __init__(self, net, width_mm, length_mm, layer="F.Cu"):
        self.net, self.width_mm, self.length_mm, self.layer = (
            net, width_mm, length_mm, layer)


class _ProfileBoard:
    def __init__(self, tracks, layers=("F.Cu", "In1.Cu", "B.Cu")):
        self.tracks = tracks
        self.copper_layers = list(layers)
        self.pads, self.vias, self.zones = [], [], []
        self.stackup = None

    def net(self, code):
        class _N:
            name = code
            # `Net.code` is what every consumer keys on; the old fake had only
            # a name, which was enough until the ampacity pass started asking
            # for this net's tracks.
            code_ = code
        _N.code = code
        return _N()

    def tracks_on_net(self, key):
        return [t for t in self.tracks if getattr(t, "net_name", None) == key]

    def zone_fill(self, layer, net=None):
        return MultiPolygon()

    def copper_on_layer(self, layer, net=None):
        return MultiPolygon()


OZ = {"F.Cu": 1.0, "In1.Cu": 0.5, "B.Cu": 1.0}


def test_a_net_of_one_width_has_no_spread():
    board = _ProfileBoard([_Track("/P", 0.5, 40.0)])
    assert current.width_profile(board, "/P", OZ) is None


def test_a_net_that_necks_reports_both_ends():
    board = _ProfileBoard([_Track("/P", 0.8, 30.0), _Track("/P", 0.2, 10.0)])
    profile = current.width_profile(board, "/P", OZ)
    assert profile.weakest_mm == 0.2 and profile.strongest_mm == 0.8
    assert profile.capacity_ratio > 1.0
    assert profile.total_mm == pytest.approx(40.0)


def test_a_pad_entry_is_not_a_section_of_the_conductor():
    """Without this every net reports a ratio, because the last fraction of a
    millimetre into an 0402 is always thinner."""
    board = _ProfileBoard([_Track("/P", 0.8, 40.0), _Track("/P", 0.2, 0.5)])
    assert current.width_profile(board, "/P", OZ) is None


def test_the_profile_takes_the_weakest_stretch_by_capacity_not_by_width():
    """A 0.45 mm stretch on an inner 0.5 oz layer carries less than a 0.3 mm
    one on an outer 1 oz layer: IPC-2152 capacity goes as W^0.575 t^0.5, so
    half the copper weight costs more than half again the width gives back.
    Taking the extremes by *width* reported a ratio below 1 on two real nets.

    (This test shared its name with the one further down until the
    IPC-2152 move, so it never ran.)"""
    board = _ProfileBoard([_Track("/P", 0.45, 20.0, layer="In1.Cu"),
                           _Track("/P", 0.3, 20.0, layer="F.Cu")])
    profile = current.width_profile(board, "/P", OZ)
    assert profile.weakest_mm == 0.45         # wider, and still the weak one
    assert profile.weakest_layer == "In1.Cu"
    assert profile.capacity_ratio >= 1.0


def test_the_reported_layer_is_the_one_that_set_the_limit():
    """The same width on two layers: the limit is the worse of them, and the
    layer named must be the one that produced it."""
    board = _ProfileBoard([_Track("/P", 0.4, 20.0, layer="F.Cu"),
                           _Track("/P", 0.4, 20.0, layer="In1.Cu"),
                           _Track("/P", 1.2, 40.0, layer="F.Cu")])
    profile = current.width_profile(board, "/P", OZ)
    assert profile.weakest_mm == 0.4 and profile.weakest_layer == "In1.Cu"


def test_the_spread_finding_fires_and_says_what_it_is_not():
    from pcbreview.audit import check_current_capacity

    board = _ProfileBoard([_Track("/P", 1.2, 40.0), _Track("/P", 0.2, 10.0)])
    findings = check_current_capacity(board, {"/P"}, OZ)
    hit = [f for f in findings if "along one net" in f.title]
    assert len(hit) == 1
    assert hit[0].measurement["capacity_ratio"] > 2.0
    assert any("conductor_neck" in line for line in hit[0].detail)


# -- the terminal floor, and why it is four rather than three -------------

def test_two_feeds_can_never_reach_the_reporting_threshold():
    """The arithmetic behind `current.MIN_TERMINALS = 4`, pinned as a property.

    With two feeds `a <= b` the median is their mean, so the worst ratio any
    imbalance can produce is `2b / (a + b)` — which approaches 2 and never
    reaches it. `FEED_RATIO_MINOR` is 3, so a two-feed net cannot produce a
    finding at any severity however bad it is, and a floor of three *feeds* is
    not a conservative choice but the only one that means anything.

    This is the derivation `MIN_TERMINALS = 4` rests on: one terminal is the
    source, so three terminals give two feeds and are declined by
    construction. Until it was measured, `coverage()` counted them as
    candidates — 11 of 33 across the five development boards.
    """
    import statistics

    from pcbreview.audit import FEED_MIN_TERMINALS, FEED_RATIO_MINOR

    worst = 0.0
    for exponent in range(0, 12):
        a, b = 1.0, float(10 ** exponent)
        worst = max(worst, b / statistics.median([a, b]))
    assert worst < 2.0
    assert worst < FEED_RATIO_MINOR

    # and the two floors agree: enough terminals to yield enough feeds
    assert current.MIN_TERMINALS == FEED_MIN_TERMINALS + 1


def test_a_three_terminal_net_is_reported_as_too_few_not_as_a_candidate():
    """The coverage lie this closed: counted as examined, declined by
    construction. A candidate count must not promise what the check cannot
    deliver."""
    class _Pad:
        def __init__(self, ref):
            self.ref, self.net, self.pos = ref, 1, (0.0, 0.0)
            self.is_plated_hole = False
            self.copper = box(0, 0, 1, 1)

    class _Board:
        vias, zones, tracks = [], [], []
        copper_layers = ["F.Cu", "B.Cu"]
        stackup = None
        # three terminals, each a 3-pad part so `_terminals` keeps it
        pads = [_Pad(ref) for ref in ("J1", "J1", "J1",
                                      "U1", "U1", "U1",
                                      "U2", "U2", "U2")]

        def net(self, code):
            class _Net:
                name = "VBUS"
            return _Net()

        def tracks_on_net(self, key):
            return []

        def zone_fill(self, layer, net=None):
            return MultiPolygon()

        def copper_on_layer(self, layer, net=None):
            return MultiPolygon()

    board = _Board()
    coverage = current.coverage(board, {"VBUS"})
    assert coverage["nets_examined"] == []
    assert coverage["nets_with_too_few_terminals"] == {"VBUS": 3}

    feeds, notes = current.worst_feed(board, "VBUS", {"F.Cu": 1.0})
    assert feeds == []
    assert "at most 2 feed(s) exist" in notes["skipped"]


# -- absolute ampacity: always computed, never gated behind a ratio --------

def _uniform_board(width_mm=0.6, layer="F.Cu", length_mm=40.0):
    """A net of ONE width — the case that produced no measurement at all."""
    class _T:
        def __init__(self):
            self.layer, self.width_mm = layer, width_mm
            self.length_mm, self.net_name = length_mm, "PWR"
            self.net = "PWR"          # `width_profile` reads `track.net`

    class _B:
        copper_layers = ["F.Cu", "In1.Cu", "B.Cu"]
        vias, zones, pads = [], [], []
        stackup = None
        tracks = [_T()]

        def net(self, code):
            class _N:
                name = code
            _N.code = code
            return _N()

        def tracks_on_net(self, key):
            return list(self.tracks)

        def zone_fill(self, layer, net=None):
            return MultiPolygon()
    return _B()


def test_a_uniform_net_gets_an_ampacity_where_it_used_to_get_nothing():
    """The whole point. `width_profile` returns None for a net of one width —
    fewer than two significant widths — so the absolute number was never
    computed at all, not merely discarded.

    Field report, 2026-08-05: four firing channels 0.600 mm along their entire
    length produced no measurement from either half of `current_capacity`, "for
    opposite reasons". A trace 3x too thin along 100% of its length read
    identically to a perfect one.
    """
    board = _uniform_board()
    assert current.width_profile(board, "PWR", {"F.Cu": 1.0}) is None
    measured = current.ampacity(board, "PWR", {"F.Cu": 1.0})
    assert measured.measured
    assert measured.weakest_a is not None and measured.weakest_a > 0
    assert measured.weakest_mm == 0.6
    assert measured.weakest_layer == "F.Cu"


def test_the_weakest_stretch_is_by_capacity_not_by_width():
    """A 0.45 mm run on an inner 0.5 oz layer carries less than a 0.3 mm run
    on an outer 1 oz layer — thinner copper, not the layer, under IPC-2152.
    Taking the extremes by width names the wrong stretch, and this is live on a
    real board here."""
    class _T:
        def __init__(self, layer, width, length):
            self.layer, self.width_mm = layer, width
            self.length_mm, self.net_name = length, "PWR"
            self.net = "PWR"

    board = _uniform_board()
    board.tracks = [_T("F.Cu", 0.3, 50.0), _T("In1.Cu", 0.45, 50.0)]
    measured = current.ampacity(board, "PWR",
                               {"F.Cu": 1.0, "In1.Cu": 0.5})
    assert measured.weakest_layer == "In1.Cu"      # NOT the 0.3 mm outer run
    assert measured.weakest_mm == 0.45
    outer = measured.by_width[("F.Cu", 0.3)][1]
    assert measured.weakest_a < outer


def test_a_higher_permitted_rise_carries_more_current():
    """IPC-2152 takes the rise as a parameter, which is why honouring a per-net
    `rise_c` is plumbing rather than new physics — and why a pulsed firing
    channel need not be judged against a continuous 10 C rise."""
    board = _uniform_board()
    cool = current.ampacity(board, "PWR", {"F.Cu": 1.0}, rise_c=10.0)
    warm = current.ampacity(board, "PWR", {"F.Cu": 1.0}, rise_c=60.0)
    assert warm.weakest_a > cool.weakest_a
    assert warm.rise_c == 60.0


def test_a_declared_current_over_capacity_is_ranked_and_a_silent_net_is_not():
    """Report always, rank only when told. the ruling, 2026-08-06."""
    from pcbreview.audit import _ampacity_findings
    from pcbreview.netclass import Declaration, NetIntent

    board = _uniform_board()
    # index 0 is the board-wide width table, which is always emitted
    table, quiet = _ampacity_findings(board, {"PWR"}, {"F.Cu": 1.0}, None,
                                      evidenced={"PWR"})
    assert "track width(s) on this board" in table.title
    assert quiet.severity == "info"
    assert "No current was declared" in quiet.title
    assert "ASK THE USER" in " ".join(quiet.detail)

    intent = NetIntent(declarations=[Declaration(
        match="PWR", net_class="power", note="test", current_a=5.0)])
    _, loud = _ampacity_findings(board, {"PWR"}, {"F.Cu": 1.0}, intent)
    assert loud.severity == "critical"
    assert "OVER its capacity" in loud.title
    assert loud.measurement["declared_a"] == 5.0
    assert loud.measurement["ratio_declared_to_available"] > 2.0


def test_a_declared_rise_turns_the_same_net_from_critical_into_a_pass():
    """The pulsed case, end to end: same copper, same current, a rise the
    designer states is acceptable."""
    from pcbreview.audit import _ampacity_findings
    from pcbreview.netclass import Declaration, NetIntent

    board = _uniform_board()
    strict = NetIntent(declarations=[Declaration(
        match="PWR", net_class="power", note="t", current_a=2.5)])
    lenient = NetIntent(declarations=[Declaration(
        match="PWR", net_class="power", note="t", current_a=2.5, rise_c=60.0)])
    assert _ampacity_findings(board, {"PWR"}, {"F.Cu": 1.0},
                              strict)[1].severity in ("major", "critical")
    assert _ampacity_findings(board, {"PWR"}, {"F.Cu": 1.0},
                              lenient)[1].severity == "info"
