"""The pipeline: discovery, both entry points, and the refill.

The live audits here ask for the cheapest boards the registry has — a real
audit of a large board is minutes, and re-auditing one in a unit test would
make the suite unusable while proving nothing the small ones do not.

Marked `slow` so `-m "not slow"` gives a sub-second run.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from pcbreview import pipeline
from pcbreview.extraction import default_backend, refill_to_temp
from pcbreview.report import render


def test_an_extraction_backend_is_available():
    backend = default_backend()
    usable, why = backend.available()
    assert usable, why
    assert backend.name == "pcbnew"


# ---------------------------------------------------------------------------
# Job 2 — working out which file is what
# ---------------------------------------------------------------------------

def test_a_project_folder_routes_every_slot(boards):
    project = pipeline.discover(boards["simple"].parent)
    assert project.board == boards["simple"]
    assert project.schematic is not None
    assert project.stackup is not None and project.stackup.exists()
    assert project.bom is not None and project.bom.name == "bom.csv"
    assert len(project.datasheets) == 3


def test_a_production_folder_does_not_route_as_three_boms(boards):
    """`inputs.classify` calls every `.csv` a BOM, and `positions.csv` even
    parses as one. `bom.find_bom` is the tie-break, and it is also what the
    audit itself would have used.

    The precondition is a property of the *folder*, not of the role: `tiny` is
    "any small board", and a board of your own that was never exported for
    fabrication has no `production/` to disambiguate. Skipped rather than
    failed, so a working install does not look broken.
    """
    project = pipeline.discover(boards["tiny"].parent)
    if project.bom is None or project.bom.name != "bom.csv":
        pytest.skip(
            "the board registered as 'tiny' has no exported `bom.csv` beside "
            "it, so there is no ambiguity for `bom.find_bom` to resolve. "
            "Register a board whose project has been exported for fabrication "
            "(bom.csv next to positions.csv and designators.csv).")
    assert any("not a BOM" in note for note in project.notes)


def test_the_root_sheet_is_chosen_not_a_child_sheet(boards):
    """One board's folder, many `.kicad_sch` — the root is the one chosen.

    A real project folder may hold a *second* project in a subdirectory, and
    `discover` then refuses rather than picking one, which is the behaviour
    `test_two_boards_in_one_folder_is_refused` exists to pin. That refusal is
    right and it is not what this test is about, so it is a skip.
    """
    folder = boards["six_layer"].parent
    try:
        project = pipeline.discover(folder)
    except pipeline.PipelineError as refusal:
        if "boards under" not in str(refusal):
            raise
        pytest.skip(
            f"{folder.name} holds more than one .kicad_pcb, so `discover` "
            f"refuses it — correctly, and that is a different test. Register a "
            f"six-layer board whose folder holds one project: {refusal}")
    assert project.schematic == boards["six_layer"].with_suffix(".kicad_sch")


def test_pointing_at_a_board_file_works_too(boards):
    assert (pipeline.discover(boards["simple"]).board
            == pipeline.discover(boards["simple"].parent).board)


def test_two_boards_in_one_folder_is_refused(tmp_path, boards):
    for name in ("a.kicad_pcb", "b.kicad_pcb"):
        shutil.copy2(boards["simple"], tmp_path / name)
    with pytest.raises(pipeline.PipelineError, match="2 boards"):
        pipeline.discover(tmp_path)


def test_a_folder_with_no_board_is_refused(tmp_path):
    with pytest.raises(pipeline.PipelineError, match="no .kicad_pcb"):
        pipeline.discover(tmp_path)


def test_a_board_with_no_stackup_is_refused_not_invented(tmp_path, boards):
    """Gate 1. A stackup is never guessed at."""
    shutil.copy2(boards["simple"], tmp_path / "board.kicad_pcb")
    project = pipeline.discover(tmp_path)
    assert project.stackup is None
    with pytest.raises(pipeline.PipelineError, match="do not invent one"):
        pipeline.review(project)


# ---------------------------------------------------------------------------
# The refill — proven headless, and never on the user's file
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_refill_writes_a_copy_and_leaves_the_original_alone(boards):
    board = boards["simple"]
    before = (board.stat().st_mtime_ns, board.stat().st_size)
    with tempfile.TemporaryDirectory() as workdir:
        result = refill_to_temp(board, workdir)
        assert result.ok, result.why_not
        assert result.board.parent == Path(workdir)
        assert result.board.exists() and result.board != board
        assert result.drc_json is not None and result.drc_json.exists()
    assert (board.stat().st_mtime_ns, board.stat().st_size) == before, (
        "the user's board was modified — engine rule 1")


# ---------------------------------------------------------------------------
# Both entry points, end to end
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("name", ["tiny", "simple"])
def test_a_full_review_runs_both_entry_points(boards, name):
    result = pipeline.review_path(boards[name].parent)
    assert result.schematic_state == "reviewed"
    assert result.report.schematic_reviewed
    assert result.schematic is not None
    assert sum(1 for c in result.layout.checks if c.ran) >= 17
    assert len(result.schematic.checks) == 8


@pytest.mark.slow
def test_rule_3_fires_on_a_live_run(boards):
    """The rule the baselines cannot cover: it reads `CheckResult`."""
    result = pipeline.review_path(boards["tiny"].parent)
    gaps = result.report.could_not_run
    assert gaps, "no check reported zero coverage on a board that has several"
    # *Which* check comes back blind is a property of the board — a board that
    # routes controlled-impedance nets has an impedance check that ran, and
    # naming `impedance` here failed on one. What rule 3 promises is that a
    # zero is reported as blind and says which check and why, so assert that.
    for gap in gaps:
        assert gap.check, gap
        assert gap.reason in {
            "skipped", "zero_population", "no_input", "unread"}, gap
        assert gap.statement, gap
    # And at least one is the shape rule 3 exists for: a check that ran over an
    # empty population, which reads like a pass and is not one.
    assert any(g.reason in {"zero_population", "no_input"} for g in gaps), gaps


@pytest.mark.slow
def test_the_inputs_the_audit_used_are_the_ones_discovery_reported(boards):
    """Every optional input is pinned, never auto-discovered a second time.

    Two runs have measured a different *input* while everyone believed they
    were measuring different *code* (`audit_files`' docstring, §47.9, §52.4).
    """
    result = pipeline.review_path(boards["simple"].parent)
    assumptions = " ".join(result.report.assumptions)
    assert "suppressed auto-discovery of: thermal, net-intent" in assumptions
    assert "BOM" not in assumptions.split("suppressed auto-discovery of:")[1][:80]


@pytest.mark.slow
def test_two_pipeline_runs_produce_the_same_report(boards):
    """Phase 1's acceptance criterion, on a real board."""
    first = pipeline.review_path(boards["simple"].parent).report
    second = pipeline.review_path(boards["simple"].parent).report
    assert render.to_json(first) == render.to_json(second)
    assert render.text(first) == render.text(second)


@pytest.mark.slow
def test_a_review_without_a_schematic_says_so(boards, tmp_path):
    board = tmp_path / boards["simple"].name
    shutil.copy2(boards["simple"], board)
    shutil.copy2(boards["simple"].with_suffix(".stackup.yaml"),
                 board.with_suffix(".stackup.yaml"))
    result = pipeline.review_path(tmp_path)
    assert result.schematic_state == "not_supplied"
    assert not result.report.schematic_reviewed
    assert "NOT" in render.text(result.report)
    entry = [e for e in result.report.entry_points
             if e.name == "schematic_audit"][0]
    assert "no schematic was supplied" in entry.statement
