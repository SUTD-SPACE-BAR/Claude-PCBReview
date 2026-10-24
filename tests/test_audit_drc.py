"""Tests for the `drc` check — KiCad's own rule violations, as findings.

Board-free: `check_drc` is exercised through a fabricated `DrcReport`, which is
the same shape `drc.run_drc` returns. That keeps these in the shipping suite,
where they guard behaviour a user's own board depends on.

The check exists because the data was already being computed and discarded.
`run_drc` has always parsed every violation with its severity; `board_state`
used only the unconnected list, and only ran the DRC when the ratsnest count
was already non-zero. So a board with nothing unconnected never ran a DRC at
all, and `board_state  0 finding(s)` read as a clean bill of health on a board
with six DRC errors — two of them a 0.1125 mm clearance on a switching node.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pcbreview import audit as audit_module
from pcbreview.drc import DrcItem, DrcReport, Violation


def _violation(kind, severity, description, items=()):
    return Violation(type=kind, severity=severity, description=description,
                     items=[DrcItem(description=d, pos=p, uuid=None)
                            for d, p in items])


def _report(violations):
    return DrcReport(violations=list(violations), unconnected=[],
                     schematic_parity=[], kicad_version="10.0.1",
                     source="fabricated")


@pytest.fixture(autouse=True)
def _clear_cache():
    audit_module._DRC_CACHE.clear()
    yield
    audit_module._DRC_CACHE.clear()


def _run(violations, path="/fabricated/board.kicad_pcb"):
    audit_module._DRC_CACHE[path] = (_report(violations), None)
    return audit_module.check_drc(path)


CLEARANCE = _violation(
    "clearance", "error",
    "Clearance violation (netclass Default clearance 0.2000 mm; actual 0.1125 mm)",
    [("Pad 2 [/SW] of L1", (10.0, 20.0)), ("PTH pad A1 [GND] of J1", (10.1, 20.0))])


def test_one_finding_per_rule_not_one_per_violation():
    """21 silkscreen texts under the minimum height are one decision."""
    findings = _run([_violation("text_height", "warning", "Text height out of range",
                                [(f"Text 'R{n}'", (float(n), 0.0))])
                     for n in range(21)] + [CLEARANCE])
    assert len(findings) == 2
    by_check = {f.measurement["type"]: f for f in findings}
    assert by_check["text_height"].measurement["count"] == 21
    assert by_check["clearance"].measurement["count"] == 1


def test_an_error_is_major_and_a_warning_is_minor():
    findings = {f.measurement["type"]: f for f in _run([
        CLEARANCE,
        _violation("silk_over_copper", "warning", "Silkscreen clipped by solder mask",
                   [("Text 'U1'", (1.0, 1.0))])])}
    assert findings["clearance"].severity == "major"
    assert findings["silk_over_copper"].severity == "minor"


def test_severity_outranks_count():
    """Straight count ranked 20 cosmetic courtyard violations above 8
    copper-edge-clearance errors on a real board, which is backwards: one is a
    redraw and the other is a fab problem."""
    findings = {f.measurement["type"]: f for f in _run(
        [_violation("malformed_courtyard", "warning", "self-intersecting",
                    [("Footprint U11", (1.0, 1.0))]) for _ in range(20)]
        + [CLEARANCE for _ in range(2)])}
    assert findings["clearance"].score > findings["malformed_courtyard"].score


def test_a_violation_the_user_already_excluded_is_not_re_raised():
    assert _run([_violation("clearance", "exclusion", "dismissed in the board",
                            [("Pad 1", (0.0, 0.0))])]) == []


def test_identical_lines_are_collapsed_with_a_count():
    """KiCad reports one violation per offending polygon, so a single
    malformed courtyard arrives as twenty identical entries."""
    finding, = _run([_violation("malformed_courtyard", "error",
                                "Footprint has malformed courtyard",
                                [("Footprint U11", (106.44, 91.67))])
                     for _ in range(20)])
    repeated = [line for line in finding.detail if "(x20)" in line]
    assert len(repeated) == 1


def test_the_nets_are_carried_so_the_renderer_can_draw_them():
    """SKILL.md's render rule is "every finding you report that names a net"."""
    finding, = _run([CLEARANCE])
    assert finding.nets == ["/SW", "GND"]
    assert finding.location == (10.0, 20.0)


def test_a_drc_that_could_not_run_says_so_rather_than_passing():
    """A zero from a check that never ran is the failure mode this whole
    project's coverage lines exist to prevent."""
    audit_module._DRC_CACHE["/nope.kicad_pcb"] = (None, "kicad-cli not found")
    finding, = audit_module.check_drc("/nope.kicad_pcb")
    assert finding.severity == "info"
    assert "could not be run" in finding.title
    assert any("not a pass" in line for line in finding.detail)


def test_a_clean_board_produces_no_drc_findings():
    assert _run([]) == []
