"""Tests for the heat-path check — the layout half of `thermal`.

The two things this measures are the concrete layout cause of both `critical`
junction temperatures the tool has ever produced: **no via inside the thermal
tab**, and **far less copper than the plane JEDEC measured theta_JA over**.

The absolute pad-area threshold this started with is refuted here on purpose
(`test_a_power_lead_is_not_a_tab`): a TO-263-7's source leads are 3.6 mm^2 and
a VSON-10's exposed pad is 3.96 mm^2, so no absolute area separates them. The
ratio to the part's own median pad does, by nearly 4x.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import Point, box

from pcbreview import heatsink, thermal
from pcbreview.audit import check_heat_path


class _Net:
    def __init__(self, name):
        self.name = name


class _Pad:
    def __init__(self, ref, number, net, x, y, w, h, plated=False):
        self.ref, self.number, self.net_name = ref, str(number), net
        self.pos = (x, y)
        self.copper = box(x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        self.copper_layers = ["F.Cu"]
        self.is_plated_hole = plated
        self.net = net


class _Via:
    def __init__(self, net, x, y):
        self.net = net
        self.pos = (x, y)


class _Board:
    """Enough of `Board` for `heatsink`, with copper stated per layer."""

    def __init__(self, pads, vias, copper):
        self.pads, self.vias = pads, vias
        self._copper = copper
        self.copper_layers = sorted({key[0] for key in copper}) or ["F.Cu"]

    def net(self, code):
        return _Net(code)

    def copper_on_layer(self, layer, net):
        from shapely.geometry import MultiPolygon
        return self._copper.get((layer, net), MultiPolygon())


def _package(ref, tab_w, tab_h, lead_w, lead_h, leads=6, net="VIN",
             tab_net="OUT", plated=False):
    """A part with `leads` small pads and one tab, laid out clear of each other."""
    pads = [_Pad(ref, i + 1, net, 100 + 2 * i, 100, lead_w, lead_h)
            for i in range(leads)]
    pads.append(_Pad(ref, leads + 1, tab_net, 100, 110, tab_w, tab_h,
                     plated=plated))
    return pads


# -- what counts as a tab -------------------------------------------------

def test_a_power_lead_is_not_a_tab():
    """A TO-263-7 source lead is 0.9 x 4.0 = 3.6 mm^2 and a VSON-10 exposed pad
    is 1.65 x 2.4 = 3.96 mm^2, so **no absolute area separates them**. The
    first version of this check used 3 mm^2 and reported five source leads on a
    real board as thermal tabs."""
    d2pak = _package("Q1", 10.0, 9.5, 0.9, 4.0, leads=6)
    board = _Board(d2pak, [], {})
    tabs = heatsink.thermal_pads(board)
    assert [t.pad_number for t in tabs] == ["7"]
    assert tabs[0].area_mm2 == pytest.approx(95.0)


def test_a_small_exposed_pad_is_still_a_tab():
    """The other side of the same threshold: a VSON-10's 3.96 mm^2 EP is
    smaller than the D2PAK lead above and must still be found."""
    vson = _package("U1", 1.65, 2.4, 0.28, 0.85, leads=9)
    tabs = heatsink.thermal_pads(_Board(vson, [], {}))
    assert [t.pad_number for t in tabs] == ["10"]
    assert tabs[0].area_mm2 == pytest.approx(3.96, abs=0.01)


def test_a_package_with_no_tab_reports_none():
    """SOP-8 and SOT-23 have uniform leads; max/median is 1 and neither has a
    heat path to examine. This zero is the check's most common answer."""
    sop8 = [_Pad("U9", i + 1, "VCC", 100 + 2 * i, 100, 1.98, 0.65)
            for i in range(8)]
    assert heatsink.thermal_pads(_Board(sop8, [], {})) == []


# -- the path out of the tab ----------------------------------------------

def test_a_tab_with_no_via_inside_it_has_no_path_to_the_plane():
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    # vias on the right net but 2 mm clear of the tab
    vias = [_Via("OUT", 100 + 4, 110 + 4)]
    tab, = heatsink.thermal_pads(_Board(pads, vias, {}))
    assert tab.vias_in_pad == 0
    assert tab.has_path_to_plane is False
    assert tab.nearest_via_mm > 0


def test_a_via_inside_the_tab_is_a_path():
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    tab, = heatsink.thermal_pads(_Board(pads, [_Via("OUT", 100, 110)], {}))
    assert tab.vias_in_pad == 1
    assert tab.has_path_to_plane is True
    assert tab.nearest_via_mm == 0.0


def test_a_plated_tab_needs_no_separate_vias():
    """KiCad's `_ThermalVias` footprint variants make the exposed pad itself a
    grid of plated through-pads. That is the correct construction and there is
    no separate via to count."""
    pads = _package("U1", 4.0, 4.2, 0.6, 1.1, plated=True)
    tab, = heatsink.thermal_pads(_Board(pads, [], {}))
    assert tab.vias_in_pad == 0
    assert tab.is_plated is True
    assert tab.has_path_to_plane is True


# -- copper against JEDEC's card ------------------------------------------

def test_attached_copper_is_measured_against_one_jesd51_7_plane():
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    copper = {("F.Cu", "OUT"): box(90, 100, 110, 130)}   # 600 mm2
    tab, = heatsink.thermal_pads(_Board(pads, [], copper))
    assert tab.attached_copper_mm2 == pytest.approx(600.0)
    assert tab.jedec_fraction == pytest.approx(600.0 / heatsink.JESD51_7_PLANE_MM2)
    assert heatsink.JESD51_7_PLANE_MM2 == pytest.approx(76.2 * 114.3)


def test_copper_not_touching_the_tab_is_not_counted():
    """Copper on the same net elsewhere on the board conducts nothing to this
    tab unless it is the same island."""
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    from shapely.geometry import MultiPolygon
    copper = {("F.Cu", "OUT"): MultiPolygon([box(98, 108, 102, 112),
                                             box(200, 200, 260, 260)])}
    tab, = heatsink.thermal_pads(_Board(pads, [], copper))
    assert tab.attached_copper_mm2 == pytest.approx(16.0)


# -- the check ------------------------------------------------------------

MANIFEST = """
parts:
  Q1:
    theta_ja_c_per_w: 40.0
    t_junction_max_c: 150.0
    dissipation_w: 4.0
    source: "vendor spec p.2"
"""


def _manifest(tmp_path, text):
    path = tmp_path / "t.yaml"
    path.write_text(text)
    return thermal.load(path)


def test_no_manifest_means_no_finding_at_all(tmp_path):
    """Nothing on the board is known to dissipate, so there is nothing to say.
    Same rule as `check_thermal`: absence is reported, never guessed at."""
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    assert check_heat_path(_Board(pads, [], {}), None) == []


def test_a_hot_part_with_no_via_in_its_tab_is_reported(tmp_path):
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    copper = {("F.Cu", "OUT"): box(98, 108, 102, 112)}
    findings = check_heat_path(_Board(pads, [], copper),
                              _manifest(tmp_path, MANIFEST))
    hit = [f for f in findings if "no via inside it" in f.title]
    assert len(hit) == 1
    assert hit[0].severity == "major"            # 4 W is well over 1 W
    assert hit[0].measurement["vias_in_pad"] == 0
    assert hit[0].measurement["dissipation_w"] == 4.0


def test_a_hot_part_on_a_starved_tab_is_reported_separately(tmp_path):
    """Q3 on the board that prompted this has no via in its tab and 116% of a
    JEDEC plane; Q1 has neither. The two are different repairs, so they are
    different findings."""
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    copper = {("F.Cu", "OUT"): box(98, 108, 102, 112)}   # 16 mm2, 0.2%
    findings = check_heat_path(_Board(pads, [], copper),
                              _manifest(tmp_path, MANIFEST))
    starved = [f for f in findings if "of the plane its datasheet" in f.title]
    assert len(starved) == 1
    assert starved[0].severity == "major"
    assert starved[0].measurement["jedec_plane_fraction"] < 0.01


def test_a_well_heatsunk_part_produces_only_the_coverage_line(tmp_path):
    """The load-bearing zero: a via in the tab and copper comparable to JEDEC's
    card is the correct construction and must be silent."""
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    copper = {("F.Cu", "OUT"): box(60, 70, 160, 170)}    # 10000 mm2, over the tab
    board = _Board(pads, [_Via("OUT", 100, 110)], copper)
    findings = check_heat_path(board, _manifest(tmp_path, MANIFEST))
    assert [f.severity for f in findings] == ["info"]
    assert findings[0].measurement["coverage"]["thermal_pads"] == 1
    assert findings[0].measurement["coverage"]["pads_with_no_path_to_plane"] == 0


def test_a_cool_part_with_a_bad_tab_is_not_reported(tmp_path):
    """0.1 W through a starved tab is not a defect, and saying so on every
    small part would bury the two that matter."""
    text = MANIFEST.replace("dissipation_w: 4.0", "dissipation_w: 0.1")
    pads = _package("Q1", 4.0, 4.2, 0.6, 1.1)
    copper = {("F.Cu", "OUT"): box(98, 108, 102, 112)}
    findings = check_heat_path(_Board(pads, [], copper),
                               _manifest(tmp_path, text))
    assert [f.severity for f in findings] == ["info"]


def test_parts_outside_the_manifest_are_not_examined(tmp_path):
    """Run over a whole board this reports capacitor and connector pads, which
    are not heat sources. The manifest is the only list that knows what
    dissipates."""
    pads = (_package("Q1", 4.0, 4.2, 0.6, 1.1)
            + _package("C1", 4.4, 2.5, 4.4, 2.5, leads=1, tab_net="GND"))
    board = _Board(pads, [], {})
    examined = heatsink.coverage(board, {"Q1"})
    assert examined["restricted_to_references"] == ["Q1"]
    assert examined["thermal_pads"] == 1
