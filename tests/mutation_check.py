"""Prove the contract tests fail when the contract is broken.

    python3 tests/mutation_check.py            # needs a board registered

A test that has never failed has never been shown to work. Everything else in
this suite validates *the tool* against something independent — KiCad's
geometry engine, a closed form, a fab's published numbers. This validates *the
tests*: it breaks one promise at a time in a throwaway copy of the tree and
checks that the test guarding that promise goes red, and that it is that test
rather than an unrelated collapse.

It exists because the contract tests in `test_any_board.py` are the ones most
likely to rot into decoration. They assert on the *shape* of a finding rather
than on a number, and an assertion about shape passes very easily for the
wrong reason — a board that happens to produce no finding above `info` will
satisfy "every finding above info carries a measurement" while checking
nothing at all.

**A skip is scored as a harness error, not as a survival.** That distinction
is the whole reliability of this script: the first version of it copied the
tree into a temporary directory, the copied tree could no longer find any
board, every test skipped, pytest exited 0, and all six mutations were
reported as having survived. Six out of six surviving is a broken harness, not
six useless tests.

Needs a board — set `PCBREVIEW_TEST_BOARDS` (see `tests/boards.py`). With none
registered it says so and stops, rather than printing a table of nonsense.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

SOURCE = pathlib.Path(__file__).resolve().parents[1]

#: (label, file, anchor, replacement, the test that must catch it)
#:
#: Each replacement has to be a real change to a value something reads. An
#: unused module-level constant is not a mutation, and scoring one as
#: "survived" is how this script lied on its first run.
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("a finding ships with no basis",
     "pcbreview/findings.py",
     '    basis: str = ""',
     '    basis: str = ""\n\n'
     '    def __post_init__(self):\n'
     '        self.basis = ""',
     "test_every_finding_carries_a_number_and_a_basis"),

    ("a skipped check does not say why",
     "pcbreview/findings.py",
     "    skipped_because: str | None = None",
     "    skipped_because: str | None = None\n\n"
     "    def __post_init__(self):\n"
     "        self.skipped_because = None",
     "test_no_check_is_silently_skipped"),

    ("a finding above info ships with no measurement",
     "pcbreview/findings.py",
     "    measurement: dict = field(default_factory=dict)",
     "    measurement: dict = field(default_factory=dict)\n\n"
     "    def __post_init__(self):\n"
     "        if self.severity != 'info':\n"
     "            self.measurement = {}",
     "test_every_finding_above_info_carries_a_measurement"),

    ("an assumption is blank rather than stated",
     "pcbreview/findings.py",
     "    assumption: str | None = None",
     "    assumption: str | None = None\n\n"
     "    def __post_init__(self):\n"
     "        if self.assumption is not None:\n"
     "            self.assumption = '   '",
     "test_an_assumption_is_stated_rather_than_taken"),

    ("findings come back unranked",
     "pcbreview/audit.py",
     "    findings.sort(key=lambda f: f.rank)",
     "    findings.sort(key=lambda f: -f.rank[0])",
     "test_the_ranking_is_monotone_in_severity_then_score"),

    ("Gate 1 stops refusing a board with no stackup",
     "pcbreview/audit.py",
     "    if board.stackup is None:\n        raise AuditError(",
     "    if False:\n        raise AuditError(",
     "test_gate_one_refuses_a_board_with_no_stackup"),

    ("the audit writes to the user's board file",
     "pcbreview/audit.py",
     "    started = time.time()",
     "    started = time.time()\n"
     "    if pcb_path:\n"
     "        import pathlib as _p; _p.Path(pcb_path).touch()",
     "test_auditing_does_not_modify_the_board_file"),

    # The anchor is the `if` rather than the `raise`, because the refusal now
    # has a legitimate escape — `radius_mm` crops around a netless finding's
    # location instead. Anchoring on the guard mutates the whole refusal away
    # including that escape, which is the promise being tested.
    #
    # This entry earned its keep the day the escape was added: the old anchor
    # quoted the `raise` line verbatim, that line was reworded, and the script
    # reported `SKIPPED — anchor not found; the source moved under this
    # script`. Scored as a harness error rather than as a survival, exactly as
    # §47.3 requires — a mutation that cannot be applied proves nothing, and
    # calling it "caught" would have been the same class of lie this whole
    # round is about.
    ("the renderer stops refusing a finding that names no net",
     "pcbreview/render.py",
     "    if not nets and region is None:",
     "    if False:",
     "test_a_finding_that_names_a_net_can_be_drawn"),

    ("zone areas drift from KiCad's by one part in a million",
     "pcbreview/model.py",
     '        self.kicad_measurements: dict = data.get("kicad_measurements", {})',
     '        self.kicad_measurements: dict = data.get("kicad_measurements", {})\n'
     '        for _k in list(self.kicad_measurements.get("zone_area", {})):\n'
     '            self.kicad_measurements["zone_area"][_k] *= 1.000001',
     "test_the_geometry_agrees_with_kicad"),

    ("track lengths drift from KiCad's by one part in ten thousand",
     "pcbreview/model.py",
     '        self.kicad_measurements: dict = data.get("kicad_measurements", {})',
     '        self.kicad_measurements: dict = data.get("kicad_measurements", {})\n'
     '        for _k in list(self.kicad_measurements.get("track_len", {})):\n'
     '            self.kicad_measurements["track_len"][_k] *= 1.0001',
     "test_the_geometry_agrees_with_kicad"),
]

CAUGHT = "caught"
SURVIVED = "*** SURVIVED — nothing guards this ***"
NO_ANCHOR = "SKIPPED — anchor not found; the source moved under this script"
HARNESS = "HARNESS ERROR — the test skipped, so no board reached it"


def _pin_registry(destination: pathlib.Path) -> str:
    """Write the *source* tree's resolved boards out as absolute paths.

    The mutated copy lives in a temporary directory, so any registry that
    resolves boards relatively — which this repository's own development one
    does — finds nothing there. Pinning absolute paths before copying is what
    makes the copy see the same boards as the original, and skipping that step
    is what made the first version of this script report every mutation as
    survived.
    """
    import yaml

    from tests import boards

    pinned = {}
    for role in boards.any_installed():
        entry = boards.REGISTRY[role]
        pinned[role] = {"pcb": str(entry.pcb.resolve()), "note": entry.note}
        if entry.stackup:
            pinned[role]["stackup"] = str(entry.stackup.resolve())
    destination.write_text(yaml.safe_dump(pinned))
    return str(destination)


def _run(work: pathlib.Path, test: str, registry: str) -> tuple[str, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/test_any_board.py::{test}",
         "-q", "--no-header", "-p", "no:randomly"],
        cwd=work, capture_output=True, text=True,
        env=dict(os.environ, PCBREVIEW_TEST_BOARDS=registry))
    output = completed.stdout + completed.stderr
    if " skipped" in output and " failed" not in output and " passed" not in output:
        return HARNESS, output
    if completed.returncode == 0:
        return SURVIVED, output
    return CAUGHT, output


def main() -> int:
    sys.path.insert(0, str(SOURCE))
    from tests import boards

    if not boards.any_installed():
        print("no board is registered, so there is nothing to mutate against.")
        print("Set PCBREVIEW_TEST_BOARDS — see tests/boards.py.")
        return 2

    results = []
    with tempfile.TemporaryDirectory() as shared:
        registry = _pin_registry(pathlib.Path(shared) / "boards.yaml")
        for label, relative, anchor, replacement, test in MUTATIONS:
            with tempfile.TemporaryDirectory() as temporary:
                work = pathlib.Path(temporary) / "repo"
                shutil.copytree(SOURCE, work, ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", "dist", ".cache"))
                target = work / relative
                text = target.read_text()
                if anchor not in text:
                    results.append((label, test, NO_ANCHOR))
                    continue
                target.write_text(text.replace(anchor, replacement, 1))
                verdict, _ = _run(work, test, registry)
                results.append((label, test, verdict))

    width = max(len(label) for label, _, _ in results) + 2
    print(f"\n{'mutation':{width}s} verdict")
    print("-" * (width + 46))
    for label, test, verdict in results:
        print(f"{label:{width}s} {verdict}")
        print(f"{'':{width}s}   guarded by {test}")

    bad = [r for r in results if r[2] != CAUGHT]
    if bad:
        print(f"\n{len(bad)} of {len(results)} not caught. A mutation that "
              f"survives means the test named beside it is decoration.")
        return 1
    print(f"\nall {len(results)} mutations caught — the contract tests fail "
          f"when the contract is broken.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
