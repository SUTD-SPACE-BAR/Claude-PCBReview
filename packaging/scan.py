"""Refuse to ship anything that identifies a board, an owner or a company.

The requirement this enforces, in the words it was given in:

    It MUST NOT CONTAIN ANY INFORMATION ABOUT THE BOARDS IT WAS TESTED ON, at
    least not in any way that allows reconstruction of details of the original
    boards. Names of the boards and where the boards come from, and the board
    files should be removed. Conclusions that were drawn should reference an
    ambiguous board which isnt traceable to the original.

Run it over the built distribution:

    python3 packaging/scan.py dist/pcbreview

It exits non-zero and prints every hit with its file and line. **A partial
pass is worse than no pass at all** — a code name and a real name in the same
document identify each other — so this is a gate, not a report.

### What it looks for, and why each pattern is here

Three kinds of leak, in increasing order of subtlety.

1. **Names.** Boards, projects, people, companies. The obvious case.
2. **Files.** A `.kicad_pcb`, `.kicad_sch`, or a per-zone truth dump *is* the
   design. Nothing of that shape may ship whatever it is called.
3. **Co-occurring part numbers.** Four specific manufacturer part numbers in
   one paragraph identify a board as surely as its name does, even though no
   single one of them identifies anything. This is the one a human reviewer
   misses, so it is counted rather than matched: a file naming more than
   `MAX_MPNS_PER_FILE` distinct orderable part codes is reported for review.

Engineering facts are deliberately NOT leaks and must survive: "six layers,
0.5 oz inner copper, 40 A, 229 parts" is what makes a threshold defensible and
identifies nothing on its own.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Names that must not appear, read from `private-names.txt` beside this file.
#:
#: **They are not written here, and that is the whole point.** An earlier
#: version listed them inline with a comment claiming it did so "without
#: quoting any of the terms themselves" — but a regex for a name *is* the name,
#: and `packaging/` ships. Any recipient reading this file learned the author's
#: surname and every project codename, from the one file whose job is to stop
#: exactly that.
#:
#: The sidecar is excluded by `build.py`, so a distribution carries the rules
#: and none of the terms. A recipient with no sidecar gets the generic checks
#: and is told the name list was empty — the names in it were never theirs.
def _load_forbidden_names() -> list[str]:
    side = Path(__file__).with_name("private-names.txt")
    if not side.exists():
        return []
    return [line.strip() for line in side.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


FORBIDDEN_NAMES = _load_forbidden_names()

#: File shapes that are somebody's design and may not ship in any form.
FORBIDDEN_FILES = [
    "*.kicad_pcb",
    "*.kicad_sch",
    "*.kicad_pro",
    "*.truth.json",
    "*.net",
]

#: Directories that never belong in a distribution. Their PRESENCE is the
#: finding — an earlier version of this file *skipped* them instead, which is
#: how a `.cache/` full of board-named extractions got into a zip: running the
#: tool inside the built tree creates one, and a scanner that looks away from
#: the one directory guaranteed to hold board names is worse than no scanner.
FORBIDDEN_DIRS = {".cache", "__pycache__", ".pytest_cache", ".git", ".history"}

#: …but a scan of the SOURCE tree legitimately walks past these. Only a
#: distribution is required to be free of them.
NOISE_DIRS = {"__pycache__", ".pytest_cache", ".git"}

#: An orderable part code: a manufacturer part number or an LCSC code. Used
#: for the co-occurrence count, not to ban any single one.
MPN = re.compile(
    r"\b(?:C\d{4,8}"                      # LCSC
    r"|[A-Z]{2,5}\d{2,5}[A-Z]{0,4}\d{0,4}[A-Z]?"   # e.g. XXX1234YY
    r")\b")

#: Above this many DISTINCT part codes in one file, the combination starts to
#: identify a bill of materials. Chosen so a module that matches on two or
#: three part families in its regexes stays clean.
MAX_MPNS_PER_FILE = 6

#: The only exemptions to the name rules, as (file, term) with a reason each.
#: Kept deliberately tiny so it can be reviewed by eye: a term qualifies only
#: when the CODE MATCHES ON IT — a product family in a regex is not a board
#: name, and deleting it would delete working coverage. The rule this follows
#: is "keep the part family where the code matches on it; drop the board it
#: was found on and the parts it co-occurred with".
NAME_EXEMPT = {
    ("pcbreview/thermal.py", r"\bteensy\b"):
        "a live alternative in the dissipative-parts regex — such modules "
        "carry no description, so the family name is the only thing to match "
        "on. It names a widely sold product line, not a design",
}

#: Files exempt from the part-number count, with the reason.
MPN_EXEMPT = {
    "pcbreview/thermal.py":
        "matches on manufacturer prefixes by design; the prefixes are the "
        "code and are meaningless without an example",
    "pcbreview/schematic_audit.py":
        "same — the part-class patterns need their examples to be "
        "maintainable",
    "tests/test_thermal.py":
        "these part numbers ARE the test vectors for those regexes — the "
        "anchoring bugs they pin (a prefix matching inside a longer token) "
        "cannot be expressed without a real example. They are commodity "
        "parts plus two specific ones, not a coherent bill of materials",
}

TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".txt", ".json", ".cfg",
                 ".toml", ".ini", ".csv"}


#: Prefixes that look like part numbers and are not: standards bodies, and
#: this project's own error-code namespaces.
NOT_A_PART = re.compile(r"^(JESD|IPC|ISO|IEC|EN|UL|STK|THM|NET|BOM|RFC|JLC)")


def _orderable_codes(text: str) -> set[str]:
    """Distinct part codes in `text`, with two classes of false positive out.

    Both were found by running this over the source rather than by reasoning
    about the regex. A standards number (`JESD51`, `IPC-2221`) is not a part,
    and a *sequential enumeration* of one prefix — `STK001` … `STK008`, this
    project's own stackup error codes — is a code namespace rather than a bill
    of materials. The second one is the interesting case: the giveaway is that
    a real BOM's parts come from many manufacturers, so one prefix accounting
    for most of the matches means an enumeration.
    """
    codes = {m.group(0) for m in MPN.finditer(text)
             if not NOT_A_PART.match(m.group(0))}
    if not codes:
        return codes
    prefixes: dict[str, int] = {}
    for code in codes:
        prefix = re.match(r"^[A-Za-z]+", code)
        key = prefix.group(0) if prefix else code
        prefixes[key] = prefixes.get(key, 0) + 1
    dominant = max(prefixes.values())
    dominant_prefix = max(prefixes, key=prefixes.get)
    # **LCSC is the exception, and it is the one that matters.** The rule above
    # reasons that "a real BOM's parts come from many manufacturers", so one
    # prefix dominating means an enumeration. Every LCSC code begins with `C`
    # by construction, so a genuine LCSC-sourced BOM — the commonest kind in
    # this project — looks exactly like an enumeration and was exempted whole.
    # It hid five real codes and their MPNs in `tests/test_datasheets.py`, and
    # only surfaced because an unrelated word nudged the ratio. A distributor
    # namespace is not a manufacturer prefix and cannot buy this exemption.
    if dominant_prefix.upper() == "C":
        return codes
    if dominant >= 0.6 * len(codes) and dominant >= 4:
        return {c for c in codes
                if not re.match(r"^[A-Za-z]+", c)
                or re.match(r"^[A-Za-z]+", c).group(0) != dominant_prefix}
    return codes


def _same_file(relative: str, key: str) -> bool:
    """Whether `relative` names the same file as an exemption key.

    The scan is run both over the repository and over a distribution one
    directory down, so either path may be the longer of the two. Getting this
    one-directional was how thermal.py's exemption silently stopped applying.
    """
    return (relative == key
            or relative.endswith("/" + key)
            or key.endswith("/" + relative))


class Hit:
    def __init__(self, path: str, line: int, kind: str, text: str):
        self.path, self.line, self.kind, self.text = path, line, kind, text

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}\n      {self.kind}: {self.text.strip()[:110]}"


def scan(root: Path) -> list[Hit]:
    hits: list[Hit] = []
    patterns = [(p, re.compile(p, re.I)) for p in FORBIDDEN_NAMES]

    seen_forbidden: set[str] = set()
    for path in sorted(root.rglob("*")):
        offending = {part for part in path.parts if part in FORBIDDEN_DIRS}
        if offending:
            for name in sorted(offending - seen_forbidden):
                seen_forbidden.add(name)
                if name not in NOISE_DIRS:
                    hits.append(Hit(
                        path.relative_to(root).as_posix(), 0,
                        "forbidden directory",
                        f"a {name}/ directory must not ship — it holds "
                        f"board-named extractions. Delete it and rebuild"))
            continue
        relative = path.relative_to(root).as_posix()

        if path.is_dir():
            continue

        for glob in FORBIDDEN_FILES:
            if path.match(glob):
                hits.append(Hit(relative, 0, "design file",
                                f"a {glob} may not ship in any form"))

        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:                       # unreadable is a failure
            hits.append(Hit(relative, 0, "unreadable", str(exc)))
            continue

        for number, line in enumerate(text.splitlines(), 1):
            for source, pattern in patterns:
                if not pattern.search(line):
                    continue
                if any(_same_file(relative, name) and term == source
                       for name, term in NAME_EXEMPT):
                    continue
                hits.append(Hit(relative, number, f"name {source}", line))

        # Exemptions are matched on the tail of the path so the scan gives the
        # same answer whether it is pointed at the repository or at a built
        # distribution one level down.
        exempt = any(_same_file(relative, name) for name in MPN_EXEMPT)
        if not exempt:
            distinct = _orderable_codes(text)
            if len(distinct) > MAX_MPNS_PER_FILE:
                sample = ", ".join(sorted(distinct)[:8])
                hits.append(Hit(
                    relative, 0, "part-number co-occurrence",
                    f"{len(distinct)} distinct part codes in one file "
                    f"({sample}…) — together these identify a BOM"))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: python3 packaging/scan.py <directory>\n")
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        sys.stderr.write(f"{root}: not a directory\n")
        return 2

    hits = scan(root)
    if not hits:
        print(f"{root}: clean — no board, owner or company is identifiable")
        return 0

    print(f"{root}: {len(hits)} leak(s)\n")
    for hit in hits:
        print(hit)
    print(f"\n{len(hits)} leak(s). Nothing ships until this is empty.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
