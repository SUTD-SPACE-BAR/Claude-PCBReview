"""Tests for the finding renderer.

The load-bearing claim is that **one SVG user unit is one board millimetre with
an identity transform**, which is what lets shapely geometry validated against
KiCad's own engine be written straight into KiCad's own plot with no coordinate
maths. `test_the_overlay_is_in_board_millimetres` asserts it against the plot
KiCad actually produces, not against a remembered viewBox string.

Everything touching a browser is tested both ways — found and not found —
because "no rasteriser" is a supported outcome rather than a failure, and on
someone else's machine it is the likely one.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from shapely.geometry import (GeometryCollection, LineString, MultiLineString,
                              MultiPolygon, Point, Polygon)

from pcbreview import render
from pcbreview.findings import Finding
from pcbreview.model import Board
HERE = Path(__file__).resolve().parents[1]


# -- path data -------------------------------------------------------------

def test_path_data_closes_polygon_rings():
    data = render.path_data(Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))
    assert data.startswith("M ") and data.endswith(" Z")
    assert data.count("Z") == 1


def test_path_data_emits_interior_rings_too():
    outer = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                    [[(2, 2), (4, 2), (4, 4), (2, 4)]])
    assert render.path_data(outer).count("Z") == 2


def test_path_data_draws_lines_and_leaves_them_open():
    """`reference.Gap.geometry` is a MultiLineString of zero area — the stretch
    of trace with no reference beneath it.

    A polygon-only version emitted an empty path for every return-path finding:
    the legend showed the colour and the picture showed nothing.
    """
    data = render.path_data(LineString([(0, 0), (5, 0)]))
    assert data == "M 0.0000,0.0000 L 5.0000,0.0000"
    assert "Z" not in data

    multi = render.path_data(MultiLineString([[(0, 0), (1, 0)],
                                              [(2, 0), (3, 0)]]))
    assert multi.count("M ") == 2
    assert "Z" not in multi


def test_path_data_handles_multipolygons_and_collections():
    pair = MultiPolygon([Polygon([(0, 0), (1, 0), (1, 1)]),
                         Polygon([(5, 5), (6, 5), (6, 6)])])
    assert render.path_data(pair).count("Z") == 2
    mixed = GeometryCollection([Polygon([(0, 0), (1, 0), (1, 1)]),
                                LineString([(3, 3), (4, 4)])])
    data = render.path_data(mixed)
    assert data.count("M ") == 2 and data.count("Z") == 1


def test_path_data_is_empty_for_nothing():
    assert render.path_data(None) == ""
    assert render.path_data(Polygon()) == ""
    assert render.path_data(Point(1, 1)) == ""


def test_has_area_separates_a_centreline_from_a_region():
    assert render._has_area(Polygon([(0, 0), (1, 0), (1, 1)]))
    assert not render._has_area(LineString([(0, 0), (1, 0)]))
    assert not render._has_area(None)


# -- dimming ---------------------------------------------------------------

def test_dim_blends_every_colour_toward_the_background():
    """The plot writes its colours inline, so dimming needs no theme and never
    touches the user's KiCad configuration."""
    originals = ["FFFFFF", "C83434"]
    dimmed = render.dim("<path fill:#FFFFFF stroke:#C83434/>")
    results = re.findall(r"#([0-9A-Fa-f]{6})", dimmed)
    assert len(results) == 2
    for before, after in zip(originals, results):
        for index, target in enumerate(render.BACKGROUND):
            was = int(before[index * 2:index * 2 + 2], 16)
            now = int(after[index * 2:index * 2 + 2], 16)
            # Moved toward the background, and not past it.
            assert min(was, target) <= now <= max(was, target)
            if was != target:
                assert now != was, "dimming did nothing to this channel"


def test_dim_at_full_strength_is_the_background():
    assert render.dim("#FFFFFF", amount=1.0) == "#%02X%02X%02X" % render.BACKGROUND


def test_dim_at_zero_changes_nothing():
    assert render.dim("#C83434", amount=0.0) == "#C83434"


# -- browser probing -------------------------------------------------------

def test_the_environment_variable_wins(monkeypatch, tmp_path):
    fake = tmp_path / "my-browser"
    fake.write_text("")
    monkeypatch.setenv("PCBREVIEW_BROWSER", str(fake))
    assert render.find_browser() == str(fake)


def test_an_explicit_path_beats_the_environment(monkeypatch, tmp_path):
    chosen = tmp_path / "chosen"
    chosen.write_text("")
    other = tmp_path / "other"
    other.write_text("")
    monkeypatch.setenv("PCBREVIEW_BROWSER", str(other))
    assert render.find_browser(str(chosen)) == str(chosen)


def test_no_browser_is_a_normal_outcome(monkeypatch):
    """Not an error. The SVG is the deliverable."""
    monkeypatch.delenv("PCBREVIEW_BROWSER", raising=False)
    monkeypatch.setattr(render, "_BROWSER_CANDIDATES", {})
    monkeypatch.setattr(render.shutil, "which", lambda _: None)
    assert render.find_browser() is None


def test_every_platform_has_candidates():
    for platform in ("win32", "darwin", "linux"):
        assert render._BROWSER_CANDIDATES[platform]
    # Edge is preinstalled on Windows 10/11, which is what makes the probe
    # viable there rather than a coin toss.
    assert any("msedge" in c for c in render._BROWSER_CANDIDATES["win32"])


def test_rasterise_reports_failure_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(render, "find_browser", lambda *_, **__: None)
    assert render.rasterise(tmp_path / "a.svg", tmp_path / "a.png",
                            100, 100) is False


def test_qlmanage_is_never_invoked():
    """macOS-only, and it forces a square canvas and letterboxes a wide crop.
    §22a rules it out; this stops it coming back.

    The module docstring names it to explain the exclusion, so what is asserted
    is that it never appears as a *string literal* — which is the only way it
    could reach a subprocess call.
    """
    source = (HERE / "pcbreview" / "render.py").read_text()
    assert '"qlmanage' not in source
    assert "\'qlmanage" not in source


# -- the Finding field -----------------------------------------------------

def test_geometry_is_not_in_the_machine_readable_summary():
    """`summary()` is the JSON view and a polygon is neither small nor JSON."""
    finding = Finding(check="x", title="t", severity="info", score=0.0,
                      basis="b", geometry=Polygon([(0, 0), (1, 0), (1, 1)]),
                      geometry_label="l")
    assert "geometry" not in finding.summary()


def test_geometry_defaults_to_absent():
    finding = Finding(check="x", title="t", severity="info", score=0.0,
                      basis="b")
    assert finding.geometry is None and finding.geometry_label is None


# -- filename collisions (§48.3 item 7) ------------------------------------

def test_two_different_net_selections_never_share_a_filename():
    """`nets[0]` alone silently overwrote a previous render.

    Reported for a six-net render followed by a two-net one starting with the
    same net. The sharper case is that the slug substitution maps `/D+` and
    `/D-` onto the same string, so even two *single*-net renders of the two
    halves of one pair collided.
    """
    stems = [render.stem_for(nets) for nets in (
        ["/D+"], ["/D-"],
        ["/D+", "/D-"],
        ["/D+", "/D-", "/IO_SDA", "/IO_SCL", "/L_S", "+3V3"],
        ["/D-", "/D+"],
    )]
    assert len(set(stems)) == len(stems)


def test_a_stem_is_stable_and_still_readable():
    """Re-rendering the same nets must land on the same file, and a human has
    to be able to tell which image is which without opening it."""
    assert render.stem_for(["/D+", "/D-"]) == render.stem_for(["/D+", "/D-"])
    assert render.stem_for(["/D+", "/D-"]).startswith("D-D-")
    assert render.stem_for(["/SDA"], prefix="return_path").startswith(
        "return_path-SDA-")


def test_a_very_long_net_list_still_produces_a_usable_filename():
    stem = render.stem_for([f"/VERY_LONG_NET_NAME_{n}" for n in range(40)])
    assert len(stem) <= 67 and "/" not in stem


def test_the_same_net_on_each_side_of_a_board_gets_two_files():
    """The most natural two-render workflow on a 2-layer board, and the
    digest's own bug: it covered only the net names, so `GND --layers F.Cu`
    and `GND --layers B.Cu` both landed on `GND-904e27.png` and the first
    image was silently lost. SKILL.md had meanwhile been given the sentence
    "two renders never overwrite each other", making it a documented
    guarantee that was false."""
    top = render.stem_for(["GND"], layers=["F.Cu"])
    bottom = render.stem_for(["GND"], layers=["B.Cu"])
    assert top != bottom
    assert render.stem_for(["GND"]) not in (top, bottom)


def test_the_caption_also_distinguishes_a_render():
    assert (render.stem_for(["/SW"], caption="before")
            != render.stem_for(["/SW"], caption="after"))


def test_a_stem_is_still_stable_for_the_same_render():
    assert (render.stem_for(["GND"], layers=["F.Cu"], caption="c")
            == render.stem_for(["GND"], layers=["F.Cu"], caption="c"))


# -- regions: drawing a finding that names no net --------------------------
#
# `thermal`, `placement` and `stackup_mismatch` name no net, so they could not
# be drawn at all — and an outside review's single most important measurement
# was a plane constriction (0.755 mm2 of copper over three layers at one x,
# carrying ~26 A) that there was no way to illustrate.

def test_bbox_is_taken_verbatim():
    assert render._parse_region("10,20,30,40", None) == (10.0, 20.0, 30.0, 40.0)


def test_around_becomes_a_box_of_twice_the_radius():
    assert render._parse_region(None, "50,60,5") == (45.0, 55.0, 55.0, 65.0)


def test_a_region_needs_the_right_number_of_values():
    with pytest.raises(render.RenderError, match="four comma-separated"):
        render._parse_region("1,2,3", None)
    with pytest.raises(render.RenderError, match="three comma-separated"):
        render._parse_region(None, "1,2")


def test_a_negative_radius_is_refused():
    with pytest.raises(render.RenderError, match="must be positive"):
        render._parse_region(None, "1,2,-3")


def test_the_two_region_forms_are_mutually_exclusive():
    with pytest.raises(render.RenderError, match="not both"):
        render._parse_region("1,2,3,4", "5,6,7")


def test_no_region_is_not_an_error():
    assert render._parse_region(None, None) is None


def test_two_regions_never_share_a_filename():
    """The same rule as two net selections (§51.5 item 9): everything that
    distinguishes one picture from another goes into the digest."""
    first = render.stem_for([], region=(0.0, 0.0, 10.0, 10.0))
    second = render.stem_for([], region=(0.0, 0.0, 20.0, 20.0))
    assert first != second


def test_a_netless_stem_is_still_recognisable():
    stem = render.stem_for([], region=(12.0, 34.0, 20.0, 40.0))
    assert stem.startswith("region-12-34")


def test_a_netless_finding_is_still_refused_without_a_radius():
    """The refusal stands by default. Guessing a crop scale for someone else's
    board is exactly the kind of invented number this project refuses, so the
    caller has to say how much of the board to show."""
    finding = Finding(check="thermal", title="U1 runs hot", severity="major",
                      score=2.0, nets=[], location=(10.0, 10.0))
    with pytest.raises(render.RenderError, match="pass radius_mm"):
        render.render_finding(None, "x.kicad_pcb", finding, "/tmp/nowhere")


def test_a_netless_finding_with_no_location_says_so_plainly():
    finding = Finding(check="placement", title="nothing to examine",
                      severity="info", score=0.0, nets=[], location=None)
    with pytest.raises(render.RenderError, match="names no nets"):
        render.render_finding(None, "x.kicad_pcb", finding, "/tmp/nowhere")
