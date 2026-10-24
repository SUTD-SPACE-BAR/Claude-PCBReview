"""The shipped fab stackup library — the design brief §2.2, Gate 1 turned into a list.

The load-bearing test here is `test_every_entry_reproduces_its_own_board`.
Three of this repository's four boards were transcribed by hand from the fab's
table and impedance calculator, months before any library existed, and each is
the acceptance record for its own stackup. If an entry cannot reproduce the file
somebody checked by hand, the library is not offering what it claims to.

Everything else is about the other half of the promise: that a library entry is
a transcription of something a fab published, never a guess, and that an entry
which cannot prove that is dropped rather than offered.
"""

from __future__ import annotations

import shutil

import pytest

from pcbreview import pipeline, stackups                                  # noqa: E402

from pcbreview.model import Board                            # noqa: E402


#: The three shipped entries and the board each was transcribed for. The
#: fourth board shares the two-layer stackup, which is the case the design brief §2.2
#: promises costs zero clicks the second time.
FOR_BOARD = {
    "simple": "JLC0216",
    "four_layer": "JLC04161H-7628",
    "six_layer": "JLC061611-7628",
}


@pytest.fixture(scope="module")
def library():
    """The SHIPPED library, named explicitly rather than resolved.

    `PCBREVIEW_STACKUPS` is a supported, documented feature — a directory of
    your own entries, loaded after the shipped ones — and a bare
    `stackups.load()` here picks it up. Every assertion below is about the
    shipped set by name, so on a machine that uses that feature two of them
    went red and made a healthy install look broken. Passing the directory is
    the fix rather than unsetting the variable: a module-scoped fixture that
    edits `os.environ` would hold that edit over every test in the file,
    which is how the `offer` test below came to pass for the wrong reason.
    """
    return stackups.load(stackups.LIBRARY_DIR)


# ---------------------------------------------------------------------------
# The library loads, and says so when it cannot
# ---------------------------------------------------------------------------

def test_the_shipped_library_loads_with_nothing_rejected(library):
    assert [r.as_dict() for r in library.rejected] == []
    assert {e.code for e in library.entries} == set(FOR_BOARD.values())


def test_a_users_own_entries_do_not_change_the_shipped_set(library, tmp_path):
    """`PCBREVIEW_STACKUPS` adds entries; it must never redefine "shipped".

    This is the failure the fixture above now prevents, pinned so that it
    cannot come back: a real user with a real local entry — the documented way
    to keep a transcription outside the install — saw two assertions about the
    shipped set go red, on a healthy install, because the variable leaked into
    a fixture that never meant to read it.
    """
    import os

    shutil.copy(stackups.LIBRARY_DIR / "JLC0216.rows.txt", tmp_path)
    (tmp_path / "LOCAL-9999.yaml").write_text(
        "code: LOCAL-9999\nfab: Somebody\ntitle: a local entry\n"
        "summary: an entry of the user's own\ncopper_layers: 2\n"
        "rows: JLC0216.rows.txt\n"
        # Quoted, and a block sequence: YAML flow style would split this on
        # its commas and hand `parse_target` the fragment `ohm=90`.
        "targets:\n  - \"ohm=90,kind=diff,layer=L1,bottom_ref=L2,"
        "width=0.4625,spacing=0.2032,to_copper=0.2032\"\n"
        "provenance: >-\n  Stands in for a real local entry, which is a"
        " transcription of a table\n  some fab published, kept outside the"
        " install so an upgrade cannot\n  wipe it.\n", encoding="utf-8")

    keep = os.environ.get(stackups.LIBRARY_ENV)
    os.environ[stackups.LIBRARY_ENV] = str(tmp_path)
    try:
        with_local = stackups.load()
    finally:
        if keep is None:
            os.environ.pop(stackups.LIBRARY_ENV, None)
        else:
            os.environ[stackups.LIBRARY_ENV] = keep

    # The local entry is offered alongside the shipped ones, and nothing is
    # rejected: this is the feature working, not a tolerated mess.
    assert [r.as_dict() for r in with_local.rejected] == []
    assert {e.code for e in with_local.entries} == (
        set(FOR_BOARD.values()) | {"LOCAL-9999"})

    # …and the fixture that says "shipped" still means exactly that.
    assert {e.code for e in library.entries} == set(FOR_BOARD.values())


def test_every_entry_carries_its_provenance(library):
    """The one field that cannot be inferred and must never be blank.

    The library's whole value is that it is not guesswork, and provenance is
    the only thing in the file that says so.
    """
    for entry in library.entries:
        assert len(entry.provenance) > 60, entry.code
        assert entry.fab


def test_layer_count_is_taken_from_the_rows_not_the_yaml(tmp_path):
    """An entry that miscounts its own table is rejected, not trusted."""
    shutil.copy(stackups.LIBRARY_DIR / "JLC0216.rows.txt", tmp_path)
    (tmp_path / "wrong.yaml").write_text(
        "code: WRONG\nrows: JLC0216.rows.txt\ncopper_layers: 4\n"
        "provenance: a test\ntargets: [ohm=90,kind=diff,layer=L1,"
        "bottom_ref=L2,width=0.4,spacing=0.2,to_copper=0.2]\n",
        encoding="utf-8")
    loaded = stackups.load(tmp_path)
    assert loaded.entries == []
    assert "describes 2" in loaded.rejected[0].reason


def test_an_entry_without_provenance_is_rejected(tmp_path):
    shutil.copy(stackups.LIBRARY_DIR / "JLC0216.rows.txt", tmp_path)
    (tmp_path / "bare.yaml").write_text(
        "code: BARE\nrows: JLC0216.rows.txt\ncopper_layers: 2\n"
        "targets: [ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.4,"
        "spacing=0.2,to_copper=0.2]\n", encoding="utf-8")
    loaded = stackups.load(tmp_path)
    assert loaded.entries == []
    assert "provenance" in loaded.rejected[0].reason


def test_a_missing_rows_file_is_rejected_with_its_name(tmp_path):
    (tmp_path / "gone.yaml").write_text(
        "code: GONE\nrows: nowhere.rows.txt\ncopper_layers: 2\n"
        "provenance: a test\ntargets: [ohm=90,kind=diff,layer=L1,"
        "bottom_ref=L2,width=0.4,spacing=0.2,to_copper=0.2]\n",
        encoding="utf-8")
    loaded = stackups.load(tmp_path)
    assert loaded.entries == []
    assert "nowhere.rows.txt" in loaded.rejected[0].reason


def test_a_target_that_does_not_parse_is_rejected(tmp_path):
    """`to_copper` omitted is the mistake `parse_target` exists to refuse."""
    shutil.copy(stackups.LIBRARY_DIR / "JLC0216.rows.txt", tmp_path)
    (tmp_path / "sloppy.yaml").write_text(
        "code: SLOPPY\nrows: JLC0216.rows.txt\ncopper_layers: 2\n"
        "provenance: a test\n"
        "targets: [ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.4,"
        "spacing=0.2]\n", encoding="utf-8")
    loaded = stackups.load(tmp_path)
    assert loaded.entries == []
    assert "to_copper" in loaded.rejected[0].reason


def test_an_absent_library_directory_is_empty_not_an_error(tmp_path):
    assert stackups.load(tmp_path / "nothing-here").entries == []


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count,expected", [
    (2, ["JLC0216"]), (4, ["JLC04161H-7628"]), (6, ["JLC061611-7628"]),
    (8, []),
])
def test_entries_are_filtered_by_copper_layer_count(library, count, expected):
    assert [e.code for e in library.for_layers(count)] == expected


# ---------------------------------------------------------------------------
# The one that matters
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("board_key", sorted(FOR_BOARD))
def test_every_entry_reproduces_its_own_board(board_key, boards, tmp_path,
                                              library):
    """The library must write the file that was checked by hand, byte for byte.

    Each of these `.stackup.yaml` files was transcribed from the fab's table
    and impedance calculator before any of this existed, and is the record the
    board's whole review rests on. Producing something merely equivalent would
    not do: `review.fingerprint` is a hash over bytes, and a stackup that
    round-trips differently would report every board as changed.
    """
    source = boards[board_key]
    committed = source.with_name(source.stem + ".stackup.yaml")
    expected = committed.read_text(encoding="utf-8")

    # `FOR_BOARD` is one person's roster, and `boards.py` promises roles rather
    # than names — "nothing is keyed on a particular design". A board of your
    # own filling the same role was very likely built on a *different* fab
    # stackup, and asserting a byte-identical file against the wrong entry told
    # its owner the library was broken. Read the code out of the file the board
    # actually carries, and skip when it is not one of the shipped three.
    import yaml
    bound = (yaml.safe_load(expected) or {}).get("name")
    if bound not in {e.code for e in library.entries}:
        pytest.skip(
            f"{source.name} is bound to {bound!r}, which is not a shipped "
            f"entry — nothing here can reproduce a transcription the library "
            f"does not have. Register a board built on one of "
            f"{sorted(e.code for e in library.entries)} under {board_key!r} to "
            f"exercise this.")

    board = tmp_path / source.name
    shutil.copy2(source, board)
    entry = library.get(bound)

    applied = stackups.apply(entry, board, Board.from_kicad(board).copper_layers)

    assert applied.path.read_text(encoding="utf-8") == expected
    assert applied.replaced is None
    assert applied.verification["ran"]
    assert applied.verification["ok"], applied.verification["report"]


@pytest.mark.slow
def test_the_solver_agrees_with_the_fab_on_every_shipped_entry(boards, tmp_path,
                                                               library):
    """Every target, re-solved from its own geometry, near the known residual.

    This solver reads consistently low against JLCPCB — about −1.5%, most
    likely unmodelled solder mask — so the test is that each target lands
    *near that*, not near zero. A target that came back at 0.0% would be as
    suspicious as one at +16%.
    """
    for board_key, code in sorted(FOR_BOARD.items()):
        source = boards[board_key]
        board = tmp_path / f"{board_key}-{source.name}"
        shutil.copy2(source, board)
        applied = stackups.apply(library.get(code), board,
                                 Board.from_kicad(board).copper_layers)
        targets = applied.verification["targets"]
        assert targets, f"{code} verified nothing"
        for target in targets:
            assert target["verdict"] == "ok", target["text"]
            assert -0.05 < target["error"] < 0.0, (
                f"{code}: {target['text']} — this solver reads low against "
                f"JLCPCB; a positive or large residual means the transcription "
                f"moved")


# ---------------------------------------------------------------------------
# Not losing somebody's hand-checked transcription
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_an_existing_stackup_is_never_replaced_without_being_asked(
        boards, tmp_path, library):
    source = boards["simple"]
    board = tmp_path / source.name
    shutil.copy2(source, board)
    copper = Board.from_kicad(board).copper_layers
    entry = library.get("JLC0216")

    stackups.apply(entry, board, copper)
    with pytest.raises(stackups.LibraryError, match="already exists"):
        stackups.apply(entry, board, copper)


@pytest.mark.slow
def test_overwriting_copies_the_old_one_aside_first(boards, tmp_path, library):
    source = boards["simple"]
    board = tmp_path / source.name
    shutil.copy2(source, board)
    copper = Board.from_kicad(board).copper_layers

    destination = stackups.destination(board)
    destination.write_text("name: hand written\nlayers: []\n", encoding="utf-8")

    applied = stackups.apply(library.get("JLC0216"), board, copper,
                             overwrite=True)
    assert applied.replaced is not None
    assert applied.replaced.read_text(encoding="utf-8") == (
        "name: hand written\nlayers: []\n")
    assert "JLC0216" in applied.path.read_text(encoding="utf-8")


@pytest.mark.slow
def test_a_second_overwrite_does_not_clobber_the_first_backup(
        boards, tmp_path, library):
    source = boards["simple"]
    board = tmp_path / source.name
    shutil.copy2(source, board)
    copper = Board.from_kicad(board).copper_layers
    stackups.destination(board).write_text("name: first\n", encoding="utf-8")

    first = stackups.apply(library.get("JLC0216"), board, copper,
                           overwrite=True)
    second = stackups.apply(library.get("JLC0216"), board, copper,
                            overwrite=True)
    assert first.replaced != second.replaced
    assert first.replaced.read_text(encoding="utf-8") == "name: first\n"


# ---------------------------------------------------------------------------
# What the launch page is handed
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_offer_names_the_board_the_candidates_and_what_does_not_fit(boards):
    source = boards["six_layer"]
    offered = stackups.offer(source, source.with_name(
        source.stem + ".stackup.yaml"))

    assert offered["board"]["layer_count"] == 6

    # Every entry is accounted for, split by the one thing that decides it —
    # and the split is computed, not typed. `PCBREVIEW_STACKUPS` may add
    # entries of the user's own, and a literal list here said a board with one
    # of those was being offered the wrong thing.
    effective = stackups.load()
    assert ({c["code"] for c in offered["candidates"]}
            | {u["code"] for u in offered["unavailable"]}
            == {e.code for e in effective.entries})
    assert ([c["code"] for c in offered["candidates"]]
            == [e.code for e in effective.for_layers(6)])
    # Not hidden — counted, with the reason.
    assert all("copper layers" in u["why_not"] for u in offered["unavailable"])
    assert "JLC061611-7628" in {c["code"] for c in offered["candidates"]}, (
        "the shipped six-layer entry must be offered to a six-layer board")
    # What is bound is a property of the board, not of this file: a six-layer
    # board of your own is on whatever six-layer stackup you had made. The
    # assertion is that `offer` reports it, not that it is any one code.
    import yaml
    bound = (yaml.safe_load(source.with_name(source.stem + ".stackup.yaml")
                            .read_text(encoding="utf-8")) or {}).get("name")
    assert offered["current"]["name"] == bound
    # `offer` resolves the library the way the CLI does — shipped entries plus
    # whatever `PCBREVIEW_STACKUPS` adds — so ask the same question it asked.
    assert offered["current"]["from_library"] is (
        bound in {e.code for e in stackups.load().entries})


@pytest.mark.slow
def test_offer_reports_no_current_stackup_rather_than_inventing_one(
        boards, tmp_path):
    source = boards["simple"]
    board = tmp_path / source.name
    shutil.copy2(source, board)
    offered = stackups.offer(board, None)
    assert offered["current"] is None
    assert [c["code"] for c in offered["candidates"]] == ["JLC0216"]


def test_offer_on_an_unreadable_board_says_why_and_offers_nothing(tmp_path):
    """Never a guess. A board that cannot be read gets no candidate list."""
    board = tmp_path / "not-really.kicad_pcb"
    board.write_text("(this is not a board)", encoding="utf-8")
    offered = stackups.offer(board, None)
    assert offered["candidates"] == []
    assert offered["why_not"]


# ---------------------------------------------------------------------------
# Over HTTP, on a project that is stuck at Gate 1
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_a_project_with_no_stackup_is_unstuck_from_the_command_line(boards,
                                                                    tmp_path,
                                                                    capsys):
    """The whole point, end to end.

    A folder holding a board and a schematic and no stackup is exactly what
    Gate 1 refuses, and the answer used to be *"go and transcribe your fab's
    table"* — a task handed back in place of an answer. Here the library is
    listed, one entry is chosen, and the project comes back reviewable.

    Driven through `main()` rather than through the functions it calls,
    because the argument parsing and the exit codes are the part an agent
    actually meets, and they are the part nothing else covers.
    """
    source = boards["simple"]
    project = tmp_path / "project"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_sch"):
        shutil.copy2(source.with_suffix(suffix),
                     project / (source.stem + suffix))

    argv = ["stackups", str(project)]
    assert stackups.main(argv) == 0
    listing = capsys.readouterr().out
    assert "JLC0216" in listing
    # Every candidate is offered and none is chosen. A tool that picked would
    # be inventing which board somebody is having built.
    assert "ASK THE USER WHICH" in listing
    assert "does not fit" in listing, "the ones that cannot be used are named"

    # A code that is not in the library is refused, not guessed at.
    assert stackups.main(["stackups", str(project), "use", "NOPE"]) == 2

    assert stackups.main(argv + ["use", "JLC0216"]) == 0
    applied = capsys.readouterr().out
    assert "VERDICT: usable" in applied
    written = project / (source.stem + ".stackup.yaml")
    assert written.exists()

    # Byte-identical to the transcription that was checked by hand. Equivalent
    # would not do: `review.fingerprint` hashes bytes, so a stackup that round
    # -tripped differently would report every board as changed.
    assert (written.read_bytes()
            == source.with_suffix(".stackup.yaml").read_bytes())

    # A second `use` refuses rather than overwriting what it just wrote.
    assert stackups.main(argv + ["use", "JLC0216"]) == 2
    assert "already exists" in capsys.readouterr().err

    # …and with --replace it writes, keeping the old one.
    assert stackups.main(argv + ["use", "JLC0216", "--replace"]) == 0
    assert (project / (source.stem + ".stackup.yaml.replaced-1")).exists()

    # The project is now reviewable: that is the whole claim.
    assert pipeline.discover(project).stackup is not None
    assert stackups.main(argv + ["verify"]) == 0
