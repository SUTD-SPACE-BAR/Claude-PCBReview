"""The four commands an agent actually types.

`run`, `stackups`, `facts` and `parts` are the whole surface this tool now
presents. Everything under them is covered by its own module's tests; what is
covered here is the layer nothing else touches — argument parsing, exit codes,
and the sentence printed when a run refuses.

**Exit codes are load-bearing and are asserted everywhere here.** A refusal is
a correct outcome — Gate 1 refusing a board with no stackup is the feature —
so `1` means *reviewed and refused* and `2` means *could not be understood*.
An agent that cannot tell those apart will either re-run a command that will
never work or "fix" a board that was never broken.
"""

from __future__ import annotations

import json
import shutil

import pytest

from pcbreview import facts, parts, run, stackups


# ---------------------------------------------------------------------------
# Usage, before anything expensive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", [run, stackups, facts, parts])
def test_help_prints_usage_and_exits_zero(module, capsys):
    assert module.main(["x", "--help"]) == 0
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("module", [run, stackups, facts, parts])
def test_no_arguments_is_a_usage_error_not_a_traceback(module, capsys):
    assert module.main(["x"]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("module", [run, stackups, facts, parts])
def test_a_path_that_does_not_exist_is_named(module, tmp_path, capsys):
    assert module.main(["x", str(tmp_path / "nowhere")]) == 2
    assert "not found" in capsys.readouterr().err


def test_a_file_that_is_not_a_board_says_what_it_wanted(tmp_path, capsys):
    stray = tmp_path / "notes.txt"
    stray.write_text("hello", encoding="utf-8")
    assert run.main(["run", str(stray)]) == 2
    assert ".kicad_pcb" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Gate 1 — refusing, and saying what to do about it
# ---------------------------------------------------------------------------

@pytest.fixture
def stuck(boards, tmp_path):
    """A board and a schematic, no stackup. Exactly what Gate 1 refuses."""
    source = boards["simple"]
    project = tmp_path / "project"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_sch"):
        shutil.copy2(source.with_suffix(suffix),
                     project / (source.stem + suffix))
    return project


def test_a_board_with_no_stackup_is_refused_with_the_library_named(stuck,
                                                                   capsys):
    """The refusal is right; handing back a chore instead of an answer was not.

    Gate 1 must refuse — every impedance, coupling and return-path number
    depends on real dielectric heights. What changed is that the message names
    the shipped stackups that fit this board instead of telling the user to go
    and transcribe one.
    """
    assert run.main(["run", str(stuck)]) == 1
    message = capsys.readouterr().err
    assert "no validated fabrication stackup" in message
    # The codes are looked up rather than typed. `boards.py` promises roles and
    # not names — "nothing is keyed on a particular design" — and a literal
    # `JLC0216` here quietly broke that: it is the right answer only while the
    # board filling the `simple` role is two-layer, which it is in one person's
    # registry and need not be in anybody else's. Pointed at a four-layer
    # `simple` board this test failed on a working install.
    from pcbreview.model import Board
    layers = len(Board.from_kicad(next(stuck.glob("*.kicad_pcb"))).copper_layers)
    fits = [e.code for e in stackups.load().for_layers(layers)]
    assert fits, f"no shipped entry has {layers} copper layers to offer"
    assert any(code in message for code in fits), "the library was not consulted"
    assert "do not pick for them" in message


def test_facts_refuses_the_same_board_the_same_way(stuck, capsys):
    assert facts.main(["facts", str(stuck)]) == 1
    assert "pcbreview.stackups" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `run` — the whole review, once
# ---------------------------------------------------------------------------

@pytest.fixture
def project(boards, tmp_path):
    """A throwaway copy of a whole project folder. Nothing here writes to the
    registry's own files."""
    source = boards["simple"].parent
    root = tmp_path / source.name
    shutil.copytree(source, root,
                    ignore=shutil.ignore_patterns(".history", "*-backups",
                                                  "review"))
    return root


@pytest.mark.slow
def test_run_prints_the_report_the_questions_and_the_inputs(project, capsys):
    assert run.main(["run", str(project)]) == 0
    out = capsys.readouterr().out
    # The inputs card: the one place a mis-routed file shows up.
    assert "inputs" in out and ".kicad_pcb" in out
    # Every rule, always, including the ones that found nothing — a missing
    # section is indistinguishable from a rule that never ran.
    for heading in ("0. WHAT WAS REVIEWED",
                    "1. EVERY CRITICAL AND MAJOR",
                    "2. EVERY GROUP OF",
                    "3. CHECKS THAT COULD NOT RUN",
                    "3b. FOUND BUT NOT MEASURED",
                    "4. EVERY DRC ERROR",
                    "5. EVERY ASSUMPTION",
                    "RENDER FLOOR"):
        assert heading in out, f"{heading} is missing from the report"
    # Both entry points ran. Silence about the schematic reads as "clean".
    assert "schematic_audit" in out
    assert "QUESTIONS FOR THE USER" in out
    assert "rule trace:" in out


@pytest.mark.slow
def test_run_writes_the_json_and_markdown_it_was_asked_for(project, tmp_path):
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"
    assert run.main(["run", str(project), "--json", str(out_json),
                     "--markdown", str(out_md), "--quiet"]) == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["board"]
    assert "entry_points" in payload and payload["entry_points"]
    assert out_md.read_text(encoding="utf-8").startswith("# Deterministic "
                                                         "review")


@pytest.mark.slow
def test_two_runs_of_the_same_project_select_the_same_things(project, capsys):
    """The defect this whole structure exists to close: two sessions with
    identical inputs reporting different things."""
    assert run.main(["run", str(project), "--json", "-", "--quiet"]) == 0
    first = capsys.readouterr().out
    assert run.main(["run", str(project), "--json", "-", "--quiet"]) == 0
    assert capsys.readouterr().out == first


@pytest.mark.slow
def test_questions_only_skips_the_report(project, capsys):
    assert run.main(["run", str(project), "--questions-only"]) == 0
    out = capsys.readouterr().out
    assert "QUESTIONS FOR THE USER" in out
    assert "DETERMINISTIC REVIEW" not in out


@pytest.mark.slow
def test_facts_and_problems_are_appended_when_asked_for(project, capsys):
    assert run.main(["run", str(project), "--facts", "--problems",
                     "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "NETS — what the engine inferred" in out
    assert "PROBLEMS — the same findings" in out
    assert "BY SHARED CAUSE" in out


# ---------------------------------------------------------------------------
# `facts` — the nets table and the shortlist
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facts_leads_with_the_shortlist(project, capsys):
    assert facts.main(["facts", str(project)]) == 0
    out = capsys.readouterr().out
    assert "SHORTLIST" in out
    assert "net-intent manifest" in out
    # The full table is behind a flag: a 229-row dump is how a shortlist stops
    # being read.
    assert "--all-nets" in out or "every net a finding names" in out


@pytest.mark.slow
def test_all_nets_prints_more_rows_than_the_shortlist(project, capsys):
    facts.main(["facts", str(project)])
    short = capsys.readouterr().out
    facts.main(["facts", str(project), "--all-nets"])
    full = capsys.readouterr().out
    assert len(full.splitlines()) >= len(short.splitlines())
    assert "more net(s)" not in full


# ---------------------------------------------------------------------------
# `parts` — the datasheets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_parts_lists_every_designator_with_a_reason(project, capsys):
    assert parts.main(["parts", str(project)]) == 0
    out = capsys.readouterr().out
    assert "PARTS —" in out
    assert "BLOCKED:" in out or "with enough read to be written" in out


def test_parts_needs_a_schematic_and_says_why(boards, tmp_path, capsys):
    """A board file carries footprints, not the LCSC codes that link a part to
    its document. Without a schematic there is nothing to attach a PDF to."""
    board = boards["simple"]
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(board, project / board.name)
    assert parts.main(["parts", str(project)]) == 2
    assert "no schematic" in capsys.readouterr().err


@pytest.mark.parametrize("argv,because", [
    (["parts", ".", "--watts"], "the value is missing"),
    (["parts", ".", "--watts", "U1"], "there is no `=`"),
    (["parts", ".", "--watts", "U1=lots"], "that is not a number"),
    (["parts", ".", "--ambient", "warm"], "that is not a number"),
])
def test_a_malformed_option_is_refused_before_any_work(argv, because, capsys):
    assert parts.main(argv) == 2
    assert capsys.readouterr().err
