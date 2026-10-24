"""The Facts tab — the tables, the writer, and the edit-then-re-run loop.

The property this file exists to hold is the one §29.6 states: **no new
inference.** The Nets table's classes and the shortlist must be the engine's
own, byte for byte, and the question queue must be nothing but findings that
already carry an `intent_needed`. A test that merely checked the payload had
the right *shape* would pass just as happily over a reimplementation, which is
the failure §0A.2's warning describes — *a partial reimplementation can only
falsify a decline, never confirm a measurement.* So the assertions here compare
against the engine's own values rather than against expected constants.

The other half is that writing the user's manifest is safe. Everything that
writes runs against a **copy** of a board folder in a temp directory; nothing
in this file touches `pcb_examples/`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbreview import dossier as dossier_module
from pcbreview import facts, pipeline
from pcbreview import netclass as netclass_module
from pcbreview.audit import PROPOSED_INTENT_NAME


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def simple_review(boards):
    """A real review of the cheapest board the registry has."""
    result = pipeline.review_path(boards["simple"], keep_board=True)
    index = dossier_module.build_index(result.board, result.layout,
                                       result.schematic)
    return result, index


@pytest.fixture(scope="module")
def simple_facts(simple_review):
    result, index = simple_review
    return facts.build(result, index)


@pytest.fixture
def board_copy(boards, tmp_path):
    """A throwaway copy of that board's folder, routed.

    `review/` is left behind deliberately: a copy carrying a previous run's
    `proposed.netintent.yaml` is exactly the situation in which writing the
    wrong file would go unnoticed.
    """
    source = boards["simple"].parent
    root = tmp_path / source.name
    shutil.copytree(source, root,
                    ignore=shutil.ignore_patterns(".history", "*-backups"))
    return pipeline.discover(root)


# ---------------------------------------------------------------------------
# The shortlist is the engine's — §29.3 and §29.6
# ---------------------------------------------------------------------------

def test_the_shortlist_is_the_engines_own_predicate(simple_review,
                                                    simple_facts):
    """Equal to what `audit.propose_intent` would list, computed independently.

    Written out longhand here on purpose. `facts.build` calls
    `netclass.shortlist`, so comparing against that function would only prove
    it calls itself; this is the predicate as `propose_intent` had it written
    before the extraction, so the two agreeing means the extraction preserved
    it.
    """
    result, _ = simple_review
    classes = result.layout.net_class_map
    named = {net for finding in result.layout.findings
             for net in finding.nets if net}
    expected = sorted(
        kind.net for kind in classes.by_net.values()
        if kind.net_class == "other" and kind.net in named
        and not kind.declared
        and not netclass_module.is_placeholder(kind.net))
    assert simple_facts["shortlist"] == expected
    assert expected, "this board has a shortlist; a zero would be vacuous"


def test_the_shortlist_matches_what_the_engine_itself_proposes(simple_review,
                                                               simple_facts):
    """The live entries of `netclass.propose`'s document are the same nets.

    The document is what a user would have got by hand before this tab, so a
    disagreement here is a user seeing two different answers to one question.
    """
    result, _ = simple_review
    classes = result.layout.net_class_map
    named = {net for finding in result.layout.findings
             for net in finding.nets if net}
    document = netclass_module.propose(classes, named_by_findings=named)
    live = [line.split('"')[1] for line in document.splitlines()
            if line.startswith("  - match:")]
    assert live == simple_facts["shortlist"]


def test_a_declared_net_leaves_the_shortlist(board_copy):
    """Declaring a net answers it, and it stops being asked about.

    Re-proposing a net the user has already ruled on is what made one session
    believe their manifest had been ignored (`netclass.propose`'s own comment).
    """
    before = _facts_for(board_copy)
    assert before["shortlist"], "nothing to declare — the test proves nothing"
    net = before["shortlist"][0]

    facts.write_intent(board_copy, [facts.Edit(net=net, net_class="other")])
    after = _facts_for(pipeline.discover(board_copy.root))

    assert net not in after["shortlist"]
    assert set(after["shortlist"]) == set(before["shortlist"]) - {net}
    row = next(r for r in after["nets"] if r["net"] == net)
    assert row["declared"] is True
    assert row["match"] == net and row["covered_by_pattern"] is False


def _facts_for(project):
    result = pipeline.review(project, keep_board=True)
    index = dossier_module.build_index(result.board, result.layout,
                                       result.schematic)
    return facts.build(result, index)


# ---------------------------------------------------------------------------
# The Nets table quotes the engine, and computes nothing
# ---------------------------------------------------------------------------

def test_every_net_row_quotes_the_engines_class_and_evidence(simple_review,
                                                             simple_facts):
    result, _ = simple_review
    classes = result.layout.net_class_map
    assert len(simple_facts["nets"]) == len(classes.by_net)
    for row in simple_facts["nets"]:
        kind = classes.of(row["net"])
        assert kind is not None
        assert row["net_class"] == kind.net_class
        assert row["kind"] == kind.kind
        # The engine's sentence, not a paraphrase of it.
        assert row["evidence"] == kind.evidence
        assert row["declared"] == bool(kind.declared)
        assert row["current_a"] == kind.current_a
        assert row["rise_c"] == kind.rise_c


def test_a_rows_ampacity_is_the_dossiers_and_not_recomputed(simple_review,
                                                            simple_facts):
    """The same number the inspector shows, from the same object.

    `dossier.py`'s docstring records what happens when a second view computes
    its own: an earlier draft *"immediately disagreed with the finding by
    0.02 A"*.
    """
    _, index = simple_review
    checked = 0
    for row in simple_facts["nets"]:
        measured = getattr(index.nets.get(row["net"]), "ampacity", None)
        if measured is None or measured.weakest_a is None:
            assert row["carries_a"] is None
            continue
        assert row["carries_a"] == round(measured.weakest_a, 2)
        checked += 1
    assert checked, "no net had an ampacity; the comparison was vacuous"


def test_the_shortlist_is_pinned_to_the_top(simple_facts):
    """the design brief §2.5 pins it there, and it is the only part anyone must read."""
    flags = [row["shortlisted"] for row in simple_facts["nets"]]
    assert flags[:len(simple_facts["shortlist"])] == [True] * len(
        simple_facts["shortlist"])
    assert not any(flags[len(simple_facts["shortlist"]):])


# ---------------------------------------------------------------------------
# The question queue is not a model — §29.4 item 3, §9
# ---------------------------------------------------------------------------

def test_every_question_is_a_findings_own_intent_needed(simple_review,
                                                        simple_facts):
    """Nothing is generated. Every question string came off a finding.

    This is the assertion that would fail first if a model were ever put here,
    which is why it compares against the set of strings the engine wrote rather
    than against a count.
    """
    result, _ = simple_review
    reports = [result.layout]
    if result.schematic is not None:
        reports.append(result.schematic)
    engine_asked = {f.intent_needed for report in reports
                    for f in report.findings if f.intent_needed}
    assert engine_asked, "no finding asked for intent; the test is vacuous"
    asked = {q["question"] for q in simple_facts["questions"]}
    assert asked == engine_asked


def test_a_question_carries_the_evidence_that_makes_it_answerable(
        simple_facts):
    """§29.4: the question is specific because the engine had the measurement."""
    for question in simple_facts["questions"]:
        assert question["findings"], "a question with no finding attached"
        for finding in question["findings"]:
            assert finding["check"] == question["check"]
            assert finding["basis"], "a finding with no derivation"


def test_findings_asking_the_same_thing_are_one_question(simple_facts):
    """One row per (check, question) — 103 rows was a worse queue than 13."""
    keys = [(q["check"], q["question"]) for q in simple_facts["questions"]]
    assert len(keys) == len(set(keys))


def test_a_question_naming_no_net_is_shown_and_not_given_a_box(simple_facts):
    """Absence is never a pass (§2.10 principle 5), and a dead box is worse.

    `bom_parity` asks whether parts were left off deliberately. That is a real
    question the engine needs answered and a net-intent declaration cannot
    answer it, so it is listed, marked unanswerable, and told why.
    """
    unanswerable = [q for q in simple_facts["questions"]
                    if not q["answerable"]]
    assert unanswerable, "this board has some; a zero is suspicious"
    for question in unanswerable:
        assert question["why_not_answerable"]
    for question in simple_facts["questions"]:
        if question["answerable"]:
            assert question["undeclared_nets"]


def test_answering_a_question_closes_it_without_waiting_for_a_rerun(
        board_copy):
    """A net declared on disk stops being asked about immediately.

    `NetKind.declared` is what the *run* read, so keying this on the run would
    leave the user answering a question and watching it stay open until the
    re-run — the same "my file was ignored" reading `propose`'s own comment
    records. It reads the manifest, like the Nets table does.
    """
    result = pipeline.review(board_copy, keep_board=True)
    index = dossier_module.build_index(result.board, result.layout,
                                       result.schematic)
    before = facts.build(result, index)
    asked = next((q for q in before["questions"] if q["answerable"]), None)
    assert asked, "nothing answerable on this board; the test proves nothing"
    net = asked["undeclared_nets"][0]

    facts.write_intent(board_copy, [facts.Edit(net=net, net_class="other")])

    # **The same `result`, rebuilt.** Nothing has re-run, so `NetKind.declared`
    # for this net is still False — which is exactly what makes this test
    # meaningful. A re-review here would set it and the assertion would pass
    # whichever source the queue had used.
    after = facts.build(result, index)

    same = next(q for q in after["questions"]
                if q["question"] == asked["question"])
    assert net not in same["undeclared_nets"]
    # ...and the finding is still there, because nothing has re-measured.
    assert len(same["findings"]) == len(asked["findings"])
    assert result.layout.net_class_map.of(net).declared is False


def test_a_demoted_finding_keeps_its_pre_demotion_severity(board_copy):
    """§29.6: the engine never hides what it lowered, and neither does this."""
    before = _facts_for(board_copy)
    net = before["shortlist"][0]
    facts.write_intent(board_copy, [facts.Edit(net=net, net_class="other")])
    after = _facts_for(pipeline.discover(board_copy.root))

    demoted = [f for q in after["questions"] for f in q["findings"]
               if f["demoted_from"]]
    for finding in demoted:
        assert finding["demoted_by"], "lowered by nobody"
        assert finding["severity"] != finding["demoted_from"]


# ---------------------------------------------------------------------------
# Writing the manifest — §29.4 item 1
# ---------------------------------------------------------------------------

def test_it_writes_the_users_manifest_and_never_the_proposal(board_copy):
    proposed = board_copy.root / "review" / PROPOSED_INTENT_NAME
    before = proposed.read_bytes() if proposed.exists() else None

    written = facts.write_intent(
        board_copy, [facts.Edit(net="GND", net_class="power",
                                note="ground", current_a=2.0)])

    target = board_copy.board.with_suffix(".netintent.yaml")
    assert written["path"] == str(target) and target.exists()
    # The machine's inference is not touched, and never becomes the user's word.
    assert (proposed.read_bytes() if proposed.exists() else None) == before


def test_what_it_writes_is_what_the_engine_will_read(board_copy):
    """Validated by `load_intent` itself, not by a second opinion here."""
    facts.write_intent(board_copy, [
        facts.Edit(net="GND", net_class="power", note="the ground plane",
                   current_a=3.5, rise_c=20.0),
        facts.Edit(net="Net-(Q3-G)", net_class="other"),
    ])
    target = board_copy.board.with_suffix(".netintent.yaml")
    loaded = netclass_module.load_intent(target)
    by_match = {d.match: d for d in loaded.declarations}
    assert by_match["GND"].net_class == "power"
    assert by_match["GND"].current_a == 3.5
    assert by_match["GND"].rise_c == 20.0
    assert by_match["GND"].note == "the ground plane"
    assert by_match["Net-(Q3-G)"].net_class == "other"
    # And discovery routes it, so the next run actually reads it.
    assert pipeline.discover(board_copy.root).netintent == target


@pytest.mark.parametrize("edit,because", [
    (facts.Edit(net="X", net_class="other", rise_c=30.0),
     "a permitted rise with no current changes nothing"),
    (facts.Edit(net="X", net_class="banana"), "not a class"),
    (facts.Edit(net="X", net_class="other", current_a=-1), "not positive"),
    (facts.Edit(net="X", net_class="other", current_a="5A"), "not a number"),
])
def test_an_invalid_edit_is_refused_and_the_file_is_untouched(
        board_copy, edit, because):
    facts.write_intent(board_copy, [facts.Edit(net="GND", net_class="other")])
    target = board_copy.board.with_suffix(".netintent.yaml")
    before = target.read_bytes()

    with pytest.raises(facts.IntentError):
        facts.write_intent(board_copy, [edit])

    assert target.read_bytes() == before, because
    assert not list(target.parent.glob("*.writing")), "a temp file was left"


def test_declaring_a_net_power_needs_no_justification(board_copy):
    """The user may assert what their own net is for, without explaining it.

    The engine's rule requiring a `note` on `power`/`protocol` was removed —
    it described a cap that stopped existing on 2026-08-03, and it asked for a
    justification to request *more* scrutiny while `other`, the one class that
    lowers a severity, asked for none. `netclass.load_intent` has the account.
    """
    written = facts.write_intent(board_copy,
                                 [facts.Edit(net="GND", net_class="power")])
    assert written["written"]
    target = board_copy.board.with_suffix(".netintent.yaml")
    declaration = netclass_module.load_intent(target).declarations[0]
    assert declaration.net_class == "power"
    assert declaration.note is None
    # ...and the audit reads it back without complaint.
    assert pipeline.discover(board_copy.root).netintent == target


def test_an_exact_match_goes_above_the_glob_that_covered_it(board_copy):
    """Editing one net must not re-declare the seven others a pattern covers."""
    target = board_copy.board.with_suffix(".netintent.yaml")
    target.write_text('nets:\n  - match: "Net-*"\n    net_class: other\n',
                      encoding="utf-8")

    facts.write_intent(board_copy, [facts.Edit(net="Net-(Q3-G)",
                                               net_class="protocol",
                                               note="it is a bus after all")])

    declarations = netclass_module.load_intent(target).declarations
    assert [d.match for d in declarations] == ["Net-(Q3-G)", "Net-*"]
    # First match wins, so the exact one governs its net and the glob still
    # governs every other net it matched.
    intent = netclass_module.load_intent(target)
    assert intent.find("Net-(Q3-G)").net_class == "protocol"
    assert intent.find("Net-(D1-A2)").net_class == "other"


def test_a_hand_written_manifest_is_copied_before_the_first_overwrite(
        board_copy):
    """A YAML round trip drops comments, and that is where the reasoning is."""
    target = board_copy.board.with_suffix(".netintent.yaml")
    target.write_text(
        "# I wrote this by hand and the comment is the point\n"
        'nets:\n  - match: "GND"\n    net_class: other\n', encoding="utf-8")

    first = facts.write_intent(board_copy,
                               [facts.Edit(net="Net-(Q3-G)",
                                           net_class="other")])
    assert first["backup"], "a hand-written file was overwritten with no copy"
    assert "the comment is the point" in Path(first["backup"]).read_text(
        encoding="utf-8")

    # Ours now, so a second edit does not litter the folder with copies.
    second = facts.write_intent(board_copy,
                                [facts.Edit(net="GND", net_class="other")])
    assert second["backup"] is None


def test_a_manifest_that_will_not_parse_is_never_overwritten(board_copy):
    """The tab is where you fix that; destroying it first is not a fix."""
    target = board_copy.board.with_suffix(".netintent.yaml")
    broken = "nets:\n  - match: 'GND'\n    net_class: nonsense\n"
    target.write_text(broken, encoding="utf-8")

    with pytest.raises(facts.IntentError):
        facts.write_intent(board_copy, [facts.Edit(net="GND",
                                                   net_class="other")])
    assert target.read_text(encoding="utf-8") == broken


def test_a_note_with_units_survives_the_round_trip(board_copy):
    """`cp1252` is the default encoding here and does not raise, it mangles.

    A designer writing "85 °C" or "220 µF" in a note is ordinary, and the
    corrupted text would travel into the finding detail that quotes it.
    """
    note = "fires the solenoid: 5 A at 85 °C into a 220 µF bank"
    facts.write_intent(board_copy, [facts.Edit(
        net="GND", net_class="power", note=note, current_a=5.0)])
    target = board_copy.board.with_suffix(".netintent.yaml")
    assert netclass_module.load_intent(target).declarations[0].note == note


def test_removing_a_declaration_puts_the_net_back_in_play(board_copy):
    facts.write_intent(board_copy, [facts.Edit(net="GND", net_class="other")])
    target = board_copy.board.with_suffix(".netintent.yaml")
    assert [d.match for d in netclass_module.load_intent(target).declarations
            ] == ["GND"]

    facts.write_intent(board_copy, [facts.Edit(net="GND", net_class="other",
                                               remove=True)])
    assert netclass_module.load_intent(target).declarations == []


# ---------------------------------------------------------------------------
# The whole loop, on a real board — declare, re-run, and see it measured
# ---------------------------------------------------------------------------

def _demotable(result):
    """A net whose declaration would lower a finding, and that finding's check.

    Chosen from the run rather than hard-coded, so the test states the property
    it depends on instead of a net name a board edit could invalidate.
    """
    order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    cap = order[netclass_module.OTHER_SEVERITY_CAP]
    for finding in result.layout.findings:
        nets = list(finding.nets or ())
        # One net, so declaring it is enough: `apply` lowers a finding only
        # when EVERY net it names was declared unimportant.
        if (len(nets) == 1 and finding.check not in netclass_module.PART_CHECKS
                and order.get(finding.severity, 9) < cap):
            return nets[0], finding.check, finding.severity
    pytest.skip(
        "no finding on the board registered as 'tiny' could be demoted by a "
        "declaration: this needs one finding that names exactly ONE net, is "
        "not a part check, and sits above "
        f"{netclass_module.OTHER_SEVERITY_CAP!r}. A board where everything "
        "naming a net is already at or below the cap is the '0 lowered is "
        "correct' case, not a defect — register a busier small board under "
        "'tiny' to exercise this. It was a hard failure, which told a user "
        "with a perfectly good board that something was broken.")


@pytest.fixture
def demotable_copy(boards, tmp_path):
    """A throwaway copy of a board where a declaration can actually demote.

    **This needs a board carrying a single-net major, not the smallest one**, and
    the reason is a measurement worth recording: on that board *nothing a user
    can declare would lower anything*. Every finding naming a net is already at
    or below `OTHER_SEVERITY_CAP`, so `netclass.apply` correctly caps zero of
    them — the "0 lowered is correct" case `declaration_report` exists to
    explain. This board has nets carrying a `major` finding, which is what
    makes the demotion observable at all.
    """
    source = boards["tiny"].parent
    root = tmp_path / source.name
    shutil.copytree(source, root,
                    ignore=shutil.ignore_patterns(".history", "*-backups",
                                                  "review"))
    return pipeline.discover(root)


@pytest.mark.slow
def test_an_edit_writes_the_manifest_and_a_rerun_measures_it(demotable_copy):
    """The whole loop, on a real board.

    The assertion that matters is the last one: the finding really is demoted
    by the re-run, and it says what it was before. If the writer had patched
    the loaded report instead, everything above would still pass.
    """
    before_result = pipeline.review(demotable_copy)
    net, check, was = _demotable(before_result)
    before = facts.build(before_result, None)
    assert before["manifest"]["exists"] is False
    row = next(r for r in before["nets"] if r["net"] == net)
    assert row["findings"] > 0, "declaring this net would change nothing"

    outcome = facts.write_intent(demotable_copy,
                                 [facts.Edit(net=net, net_class="other")])
    assert outcome["written"] and outcome["declarations"] == 1

    # The manifest reads back immediately, even though nothing has re-run —
    # and the *review* has NOT moved, because no run has happened since. Both
    # halves matter: an edit that vanished until a re-run reads as ignored, and
    # a review that silently absorbed one would be describing a run that never
    # happened.
    pending = facts.build(before_result, None)
    assert pending["manifest"]["exists"] is True
    assert [d["match"] for d in pending["manifest"]["declarations"]] == [net]
    assert pending["shortlist"] == before["shortlist"]
    pending_row = next(r for r in pending["nets"] if r["net"] == net)
    assert pending_row["match"] == net, "the edit is not visible"
    assert pending_row["declared"] is False, "the review claims to have read it"

    # The re-run. `discover` again, because the manifest is a new input and the
    # routing that produced `demotable_copy` predates it.
    after_result = pipeline.review(pipeline.discover(demotable_copy.root))
    after = facts.build(after_result, None)
    after_row = next(r for r in after["nets"] if r["net"] == net)
    assert after_row["declared"] is True
    assert after_row["net_class"] == "other"
    assert after["manifest"]["in_use"] is True

    demoted = [f for f in after_result.layout.findings
               if net in (f.nets or ())
               and f.measurement.get("severity_before_netclass")]
    assert demoted, (
        f"declaring {net} lowered nothing — either the net had no capped "
        f"finding or the re-run did not read the manifest")
    assert any(f.check == check for f in demoted), (
        f"the {check} finding that was {was} before is not among the demoted")
    for finding in demoted:
        assert finding.severity == netclass_module.OTHER_SEVERITY_CAP
        assert finding.measurement["severity_before_netclass"] != (
            finding.severity)
        # The engine never hides what it lowered, and neither does the report.
        assert any("severity lowered from" in line for line in finding.detail)


@pytest.mark.slow
def test_one_declaration_does_not_demote_a_finding_naming_other_nets(
        demotable_copy):
    """`apply` lowers a finding only when EVERY net it names was declared.

    On this board `drc` names several nets and `return_path` names one, and
    both can be `major` on the same net — so declaring that one net must lower
    the second and leave the first alone. One undeclared net is enough.
    """
    first = pipeline.review(demotable_copy)
    net, _, _ = _demotable(first)
    multi = [f for f in first.layout.findings
             if net in (f.nets or ()) and len(f.nets) > 1
             and f.check not in netclass_module.PART_CHECKS]
    if not multi:
        pytest.skip("no multi-net finding names this net")

    facts.write_intent(demotable_copy,
                       [facts.Edit(net=net, net_class="other")])
    after = pipeline.review(pipeline.discover(demotable_copy.root))

    for finding in after.layout.findings:
        if net in (finding.nets or ()) and len(finding.nets) > 1:
            assert not finding.measurement.get("severity_before_netclass"), (
                f"{finding.check} names {sorted(finding.nets)} and only "
                f"{net} was declared, but it was lowered anyway")


# ---------------------------------------------------------------------------
# The command line over it
# ---------------------------------------------------------------------------

def test_a_declaration_naming_no_net_is_refused_before_it_is_written(
        board_copy, capsys):
    """The silent failure this guard exists for.

    A declaration for a net the board does not have writes cleanly, loads
    cleanly and changes nothing — so the next review reports the same severity
    and the user concludes their manifest was ignored. It is also what a
    POSIX-emulating shell on Windows does to a bare `/GND`.
    """
    assert facts.main(["facts", str(board_copy.root),
                       "--declare", "/no-such-net=other"]) == 2
    assert "match no net" in capsys.readouterr().err
    assert not netclass_module.default_intent_path(board_copy.board).exists()

    # …and `--force` writes it anyway, for the case where the user knows
    # something the board file does not say yet.
    assert facts.main(["facts", str(board_copy.root), "--force",
                       "--declare", "/no-such-net=other"]) == 0
    assert netclass_module.default_intent_path(board_copy.board).exists()


def test_a_bus_member_is_not_denied_because_of_its_brackets(monkeypatch):
    """KiCad writes a bus member as `/DATA[0]`, and `[0]` is an fnmatch
    character class — so the net's own name matches nothing by glob.

    Without the exact-match arm, the guard tells a user that a net they are
    looking at on their own board does not exist, which is worse than the
    silent no-op it was added to prevent.
    """
    class _Net:
        def __init__(self, name):
            self.name = name

    class _Board:
        nets = {0: _Net("/DATA[0]"), 1: _Net("/GND")}

        @staticmethod
        def from_kicad(_path):
            return _Board()

    import pcbreview.model as model_module
    monkeypatch.setattr(model_module, "Board", _Board)

    assert facts.nets_not_on("board.kicad_pcb", ["/DATA[0]"]) == []
    assert facts.nets_not_on("board.kicad_pcb", ["/DATA*"]) == []
    assert facts.nets_not_on("board.kicad_pcb", ["/NOPE"]) == ["/NOPE"]


@pytest.mark.parametrize("spec,because", [
    ("no-equals-sign", "there is no net class at all"),
    ("/A=nonsense", "that is not one of the engine's classes"),
    ("/A=other,current=lots", "a current has to be a number"),
    ("/A=other,volts=5", "there is no such field"),
])
def test_a_declaration_the_engine_would_refuse_is_refused_here(spec, because):
    with pytest.raises(facts.IntentError):
        facts.parse_declaration(spec)


def test_a_note_keeps_its_commas():
    """The note is taken off the end first, so a comma inside it is not a field
    separator. Notes are prose and prose has commas in it."""
    edit = facts.parse_declaration(
        "/A=other,current=1.65,note=fires for 1 s, then not again")
    assert edit.current_a == 1.65
    assert edit.note == "fires for 1 s, then not again"
