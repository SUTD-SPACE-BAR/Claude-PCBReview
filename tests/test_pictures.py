"""Tests for the pictures a review draws by default.

The selection is the part that has to be right every time, so it is a pure
function and most of these test it directly: one picture per group, drawn
from the group's most severe drawable member; severe findings outside the
groups after that, criticals first and capped; schematic groups and netless
findings without geometry listed rather than guessed at.

Drawing itself needs KiCad. What is tested here without it is that a missing
`kicad-cli` is said rather than raised, that a dozen pictures plot each layer
set once, and where the pictures go.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import LineString, box

from pcbreview import pictures, render
from pcbreview.findings import Finding
from pcbreview.report.model import (LAYOUT, SCHEMATIC, FindingRef, GroupRef,
                                    Report)


def _finding(check, title, severity="major", nets=("/A",), geometry=None):
    return Finding(check=check, title=title, severity=severity, score=1.0,
                   nets=list(nets), geometry=geometry)


def _ref(finding, source=LAYOUT):
    return FindingRef(source=source, check=finding.check,
                      severity=finding.severity, title=finding.title,
                      score=finding.score, nets=tuple(finding.nets))


def _group(check, members, source=LAYOUT, label="", shared=()):
    return GroupRef(source=source, check=check, cause=check, label=label,
                    shared=tuple(shared), worst=members[0].severity,
                    members=tuple(members))


# -- selection -------------------------------------------------------------

def test_the_board_comes_first():
    planned, skipped = pictures.plan(Report(board="b"), [])
    assert [p.kind for p in planned] == ["board"]
    assert skipped == []


def test_one_picture_per_group_from_its_first_drawable_member():
    """A group is one cause; its picture is its most severe member that has
    copper or geometry to crop to. A member that names no net and carries no
    geometry is passed over rather than drawn at a guessed scale."""
    blind = _finding("return_path", "blind", nets=())
    first = _finding("return_path", "worst drawable", nets=("/SERVO4",))
    second = _finding("return_path", "next", nets=("/TX1",))
    group = _group("return_path", [_ref(blind), _ref(first), _ref(second)],
                   label="referenced to", shared=("GND",))
    planned, _ = pictures.plan(Report(board="b", groups=(group,)),
                               [blind, first, second])
    groups = [p for p in planned if p.kind == "group"]
    assert len(groups) == 1
    assert groups[0].finding is first
    assert "all 3 finding(s)" in groups[0].note
    assert "referenced to GND" in groups[0].note


def test_members_of_a_group_are_not_drawn_again_as_severe_findings():
    """Otherwise the severe allowance fills with crops of the biggest group,
    which is exactly the repetition grouping exists to remove."""
    members = [_finding("broadside", f"stacked {i}", nets=(f"/N{i}",))
               for i in range(6)]
    group = _group("broadside", [_ref(m) for m in members])
    alone = _finding("kelvin_sense", "sense joins far from the shunt",
                     nets=("/SENSE",))
    report = Report(board="b", groups=(group,),
                    severe=tuple(_ref(m) for m in members) + (_ref(alone),))
    planned, _ = pictures.plan(report, members + [alone])
    drawn = [p.finding for p in planned if p.finding is not None]
    assert drawn == [members[0], alone]


def test_criticals_are_drawn_before_majors_and_the_rest_are_counted():
    majors = [_finding("drc", f"major {i}", nets=(f"/M{i}",)) for i in range(5)]
    critical = _finding("parity", "critical one", severity="critical",
                        nets=("/C",))
    report = Report(board="b",
                    severe=tuple(_ref(m) for m in majors) + (_ref(critical),))
    planned, skipped = pictures.plan(report, majors + [critical], max_severe=3)
    drawn = [p.finding for p in planned if p.kind == "finding"]
    assert drawn == [critical, majors[0], majors[1]]
    assert any("3 more" in s.what for s in skipped)


def test_schematic_groups_are_not_drawn():
    member = _finding("decoupling", "no capacitor", severity="minor")
    group = _group("decoupling", [_ref(member, SCHEMATIC)] * 5, source=SCHEMATIC)
    planned, skipped = pictures.plan(Report(board="b", groups=(group,)), [member])
    assert [p.kind for p in planned] == ["board"]
    assert skipped == []


def test_a_netless_finding_without_geometry_is_listed_with_the_command():
    """A radius around a point would be an invented scale. The finding is
    named as not drawn, with the command that draws it once a region is
    chosen, so it is never silently absent."""
    netless = _finding("stackup_mismatch", "board stackup disagrees", nets=())
    planned, skipped = pictures.plan(
        Report(board="b", severe=(_ref(netless),)), [netless])
    assert [p.kind for p in planned] == ["board"]
    assert len(skipped) == 1
    assert "--around" in skipped[0].reason


def test_a_netless_finding_with_its_own_geometry_is_drawn():
    shaped = _finding("thermal", "hot part", nets=(),
                      geometry=box(10, 10, 12, 12))
    planned, _ = pictures.plan(Report(board="b", severe=(_ref(shaped),)),
                               [shaped])
    assert [p.finding for p in planned if p.finding] == [shaped]


def test_no_picture_highlights_more_nets_than_there_are_colours():
    """A broadside finding's victim and 39 aggressors, all highlighted, read
    as noise, and past five nets the colours repeat."""
    many = _finding("broadside", "stacked over 39",
                    nets=[f"/N{i}" for i in range(40)])
    planned, _ = pictures.plan(Report(board="b", severe=(_ref(many),)), [many])
    item = planned[1]
    assert item.nets == [f"/N{i}" for i in range(pictures.MAX_NETS)]
    assert pictures.MAX_NETS == len(render.HIGHLIGHTS)
    assert f"{pictures.MAX_NETS} of its 40 nets" in item.note


def test_poured_planes_are_never_highlighted():
    mixed = _finding("reference_split", "crosses", nets=("GND", "/SIG"))
    planned, _ = pictures.plan(Report(board="b", severe=(_ref(mixed),)),
                               [mixed], plane_nets={"GND"})
    assert planned[1].nets == ["/SIG"]


def test_a_finding_about_a_plane_alone_is_not_drawn_as_the_whole_plane():
    """The first version drew a parity finding about four GND exposed pads as
    the entire ground pour."""
    plane = _finding("parity", "4 symbol pins have no pad", nets=("GND",))
    planned, skipped = pictures.plan(Report(board="b", severe=(_ref(plane),)),
                                     [plane], plane_nets={"GND"})
    assert [p.kind for p in planned] == ["board"]
    assert "poured planes" in skipped[0].reason


def test_a_finding_with_many_nets_and_its_own_geometry_is_drawn_by_the_geometry():
    """A DRC rule type names every net its violations touch; the marks at the
    violations are the picture."""
    drc = _finding("drc", "solder_mask_bridge violation(s)",
                   nets=[f"/N{i}" for i in range(14)],
                   geometry=box(1, 1, 2, 2))
    planned, _ = pictures.plan(Report(board="b", severe=(_ref(drc),)), [drc])
    assert planned[1].nets == []
    assert "marks are the evidence" in planned[1].note


def test_names_sort_in_reading_order():
    findings = [_finding("drc", f"f{i}", nets=(f"/N{i}",)) for i in range(3)]
    planned, _ = pictures.plan(
        Report(board="b", severe=tuple(_ref(f) for f in findings)), findings)
    names = [p.name for p in planned]
    assert names == sorted(names)
    assert names[0] == "01-board"


# -- drawing ---------------------------------------------------------------

class _Result:
    """The parts of `pipeline.ReviewResult` that `draw()` reads."""
    def __init__(self, board=object()):
        self.board = board
        self.report = Report(board="b")
        self.all_layout_findings = []
        self.layout = None
        self.project = type("P", (), {"board": Path("/x/board.kicad_pcb")})()


def test_no_kicad_cli_is_said_rather_than_raised(monkeypatch):
    monkeypatch.setattr("pcbreview.drc.find_kicad_cli", lambda: None)
    drawn = pictures.draw(_Result())
    assert drawn.drawn == []
    assert "kicad-cli" in drawn.unavailable
    assert "none drawn" in pictures.text(drawn)


def test_no_kept_board_is_said_rather_than_raised():
    drawn = pictures.draw(_Result(board=None))
    assert "keep_board" in drawn.unavailable


def test_a_layer_set_is_plotted_once_per_board(monkeypatch, tmp_path):
    """The plot is the slow half of a picture and depends only on the board
    and the layers, so a dozen pictures on two layer sets cost two plots."""
    calls = []

    def fake_plot(pcb, layers, kicad_cli=None):
        calls.append(tuple(layers))
        return ('<svg viewBox="0 0 100 100" width="100mm" height="100mm">'
                '<title>t</title></svg>')

    monkeypatch.setattr(render, "plot_layers", fake_plot)
    monkeypatch.setattr(render, "rasterise", lambda *a, **k: False)
    monkeypatch.setattr(render, "net_geometry",
                        lambda board, net, layers: LineString([(1, 1), (5, 1)]).buffer(0.1))
    cache: dict = {}
    for index in range(3):
        for layers in (["F.Cu"], ["B.Cu"]):
            render.render(object(), "board.kicad_pcb", ["/A"], "c", tmp_path,
                          f"p{index}{layers[0]}", layers=layers,
                          plot_cache=cache)
    assert sorted(calls) == [("B.Cu", "Edge.Cuts"), ("F.Cu", "Edge.Cuts")]


def test_pictures_go_to_one_cache_folder_per_board(tmp_path):
    a = pictures.default_dir(tmp_path / "one" / "main.kicad_pcb")
    b = pictures.default_dir(tmp_path / "two" / "main.kicad_pcb")
    assert a != b
    assert a.parent == b.parent
    assert a.parent.name == "pictures" and a.parent.parent.name == "pcbreview"
    assert a.name.startswith("main-")


def test_a_new_run_removes_only_the_old_pictures(tmp_path):
    (tmp_path / "old.png").write_bytes(b"x")
    (tmp_path / "old.svg").write_text("x")
    (tmp_path / "notes.txt").write_text("keep me")
    pictures._empty(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


def test_the_printed_block_lists_every_picture_and_every_omission(tmp_path):
    drawn = pictures.Pictures(tmp_path, drawn=[
        pictures.Picture(tmp_path / "01-board.png", "board", "the board"),
        pictures.Picture(tmp_path / "02-group-x.png", "group", "MAJOR  t",
                         note="stands for all 6 finding(s)")],
        skipped=[pictures.Skipped("MAJOR  netless", "names no net")])
    printed = pictures.text(drawn)
    assert "2 drawn" in printed
    assert str(tmp_path / "02-group-x.png") in printed
    assert "stands for all 6" in printed
    assert "not drawn — 1" in printed and "names no net" in printed
