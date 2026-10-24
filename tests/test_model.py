"""Tests for the board model.

The important tests here cross-validate our shapely reconstruction against
KiCad's own geometry engine (`tests/kicad_truth.py` produces the reference).
If zone fill areas or track lengths drift from KiCad's numbers, every downstream
analysis is built on sand.
"""

import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview.model import Board, BoardError, arc_points, resolve_arc
from pcbreview.stackup import load_stackup


# -- arc geometry ---------------------------------------------------------

def test_resolve_arc_recovers_a_known_quarter_circle():
    """Quarter circle of radius 10 centred on the origin, start (10,0)."""
    start = (10.0, 0.0)
    mid = (10 / math.sqrt(2), 10 / math.sqrt(2))
    end = (0.0, 10.0)
    arc = resolve_arc(start, mid, end)
    assert arc is not None
    assert arc.centre[0] == pytest.approx(0.0, abs=1e-9)
    assert arc.centre[1] == pytest.approx(0.0, abs=1e-9)
    assert arc.radius == pytest.approx(10.0)
    assert abs(arc.sweep) == pytest.approx(math.pi / 2)
    assert arc.length == pytest.approx(10 * math.pi / 2)


def test_resolve_arc_handles_the_major_arc():
    """Same endpoints, midpoint on the far side -> 270 degrees, not 90."""
    start = (10.0, 0.0)
    mid = (-10.0, 0.0)
    end = (0.0, 10.0)
    arc = resolve_arc(start, mid, end)
    assert abs(arc.sweep) == pytest.approx(3 * math.pi / 2)


def test_resolve_arc_rejects_collinear_points():
    assert resolve_arc((0, 0), (1, 1), (2, 2)) is None


def test_arc_tessellation_stays_within_sagitta_tolerance():
    start, mid, end = (10.0, 0.0), (10 / math.sqrt(2), 10 / math.sqrt(2)), (0.0, 10.0)
    points = arc_points(start, mid, end, tolerance=0.002)
    # Every tessellated point must sit on the true circle.
    for x, y in points:
        assert math.hypot(x, y) == pytest.approx(10.0, abs=1e-9)
    # Chord length understates the arc, but only slightly.
    chord_length = sum(
        math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))
    true_length = 10 * math.pi / 2
    assert chord_length < true_length
    assert (true_length - chord_length) / true_length < 1e-4


def test_arc_endpoints_are_preserved_exactly():
    start, mid, end = (1.234, 5.678), (2.0, 6.5), (3.5, 6.9)
    points = arc_points(start, mid, end)
    assert points[0] == start
    assert points[-1] == end


def test_missing_board_raises():
    with pytest.raises(BoardError, match="not found"):
        Board.from_kicad("/nonexistent/board.kicad_pcb")


# -- packaging: finding KiCad's own Python ---------------------------------

def test_kicad_python_is_discovered_rather_than_hardcoded():
    """This was one hardcoded macOS path and is the first thing that breaks for
    anyone else — the same failure `drc.find_kicad_cli` already avoids."""
    from pcbreview.model import find_kicad_python
    found = find_kicad_python()
    assert Path(found).exists()
    # It must actually be able to import pcbnew, not merely exist: on Linux the
    # distro package puts pcbnew on the system Python and the path cannot say so.
    probe = subprocess.run([found, "-c", "import pcbnew"],
                           capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr


def test_a_missing_explicit_interpreter_falls_through_to_discovery():
    """An explicit path that does not exist must not mask a working install."""
    from pcbreview.model import find_kicad_python
    assert Path(find_kicad_python("/definitely/not/here")).exists()


def test_the_error_names_the_environment_variable_that_fixes_it(monkeypatch):
    """The failure a stranger hits first, so it has to say what to do."""
    from pcbreview import model
    monkeypatch.setattr(model, "_KICAD_PYTHON_GLOBS", {})
    monkeypatch.delenv("PCBREVIEW_KICAD_PYTHON", raising=False)
    with pytest.raises(model.BoardError, match="PCBREVIEW_KICAD_PYTHON"):
        model.find_kicad_python()


# -- REFERENCE.md's object-model table must describe the real classes -------

#: The "not" column carries two different warnings and only one of them is
#: "this does not exist". These three DO exist and the table is telling you
#: not to reach for them: `Zone._fills` is raw JSON, `Zone.area_mm2` raises on
#: purpose because a zone's copper is per layer, and `Zone.polygon` is the
#: same mistake by another name. Listing them here rather than loosening the
#: assertion keeps the other half of the check sharp.
EXISTS_BUT_DO_NOT_USE = {
    ("Zone", "_fills"),
    ("Zone", "area_mm2"),
}


def test_every_attribute_the_cheat_sheet_names_actually_exists():
    """The cheat-sheet exists to stop attribute-guessing, and it had a guess
    in it: the `Pad` row named a `.polygon` that has never existed — the pad
    geometry is `.copper`. An outside session lost most of a report to this
    class of mistake (2026-08-05), and a document that is wrong about the API
    is worse than no document, because it is trusted.

    So the table is checked against the classes. Attributes in the "not"
    column are asserted ABSENT, which is the half that rots silently: an
    attribute added later would make a documented warning a lie.
    """
    import dataclasses
    import inspect
    import re
    from pathlib import Path

    from pcbreview import model, schematic as schematic_module

    known = {
        "Board": model.Board, "Track": model.Track, "Via": model.Via,
        "Pad": model.Pad, "Zone": model.Zone, "Footprint": model.Footprint,
        "PinRef": schematic_module.PinRef,
        "Component": schematic_module.Component,
        "Schematic": schematic_module.Schematic,
    }

    def present(cls, attribute):
        # Three ways an attribute can be real, and all three are in use here:
        # a property or method on the class, a dataclass field (which does not
        # exist on the class at all), and a plain `self.x = ...` in `__init__`
        # — which is how `Board` exposes `.tracks`.
        if hasattr(cls, attribute):
            return True
        try:
            if any(f.name == attribute for f in dataclasses.fields(cls)):
                return True
        except TypeError:
            pass
        try:
            source = inspect.getsource(cls)
        except (OSError, TypeError):
            return False
        return re.search(rf"\bself\.{re.escape(attribute)}\s*[:=]",
                         source) is not None

    text = (Path(__file__).resolve().parents[1] / "REFERENCE.md").read_text(encoding="utf-8")
    table = text[text.index("## The object model"):]
    table = table[:table.index("\n## ")]

    attribute = re.compile(r"`\.([A-Za-z_][A-Za-z_0-9]*)")
    checked = 0
    for line in table.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = cells[0].strip("`").split("`")[0].strip()
        # The `X (again)` rows are prose clarifications — "`.pos`, not
        # `.position` or `.x`/`.y`" — and every attribute they name is already
        # in that class's primary row. Parsing them means parsing English, so
        # they are skipped and nothing is lost.
        if "(again)" in cells[0]:
            continue
        name = name.split(" (")[0].strip("` ")
        cls = known.get(name)
        if cls is None:
            continue
        for match in attribute.finditer(cells[1]):
            found = match.group(1)
            # The middle column is prose as well as a list, and some rows read
            # "`.width_mm`, not `.width`". A name introduced by "not" is being
            # warned against, not documented.
            if cells[1][max(0, match.start() - 5):match.start()].endswith("not "):
                continue
            assert present(cls, found), (
                f"REFERENCE.md says {name}.{found} exists and it does not")
            checked += 1
        if len(cells) > 2:
            # Only a bare `~~`.name`~~`. Compound entries like `~~`.pos.x`~~`
            # warn about an attribute of the VALUE (`pos` is a plain tuple),
            # not about the attribute itself, and asserting `Via.pos` absent
            # would be wrong — it is very much present.
            for absent in re.findall(r"~~`\.?([A-Za-z_][A-Za-z_0-9]*)`~~",
                                     cells[2]):
                if (name, absent) in EXISTS_BUT_DO_NOT_USE:
                    continue
                assert not present(cls, absent), (
                    f"REFERENCE.md warns {name}.{absent} does not exist, "
                    f"but it does now — the warning has become a lie")
                checked += 1
    # not vacuous: the table really was parsed
    assert checked > 40, f"only {checked} attributes checked — table not parsed"


def test_no_subprocess_in_the_package_can_block_forever():
    """Every `subprocess.run` must carry a timeout.

    `model.py`'s two calls — the KiCad-Python probe and the board extraction —
    were unbounded until 2026-08-06, which made them the only calls in the
    package that could hang indefinitely. `drc.py` and `render.py` have always
    had ceilings.

    The symptom is unmistakable once you know it: a run here stalled for forty
    minutes having used **2.85 seconds of CPU**. An infinite loop burns a core;
    a process blocked on a subprocess burns nothing. A hang with no ceiling is
    indistinguishable from a dead machine, and it takes the whole suite and
    every gate down with it.

    Board-free, so it ships and guards for the user too.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "pcbreview"
    unbounded = []
    checked = 0
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if "subprocess" not in name:
                continue
            if name.split(".")[-1] not in ("run", "Popen", "call",
                                           "check_output"):
                continue
            checked += 1
            if not any(k.arg == "timeout" for k in node.keywords):
                unbounded.append(f"{path.name}:{node.lineno}  {name}")

    assert checked >= 5, f"only {checked} subprocess calls found — scan broke"
    assert not unbounded, (
        "these subprocess calls have no timeout and can block forever:\n  "
        + "\n  ".join(unbounded))
