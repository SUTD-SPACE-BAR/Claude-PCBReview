# Installing pcbreview

Four steps, and they differ enough between macOS/Linux and Windows that the
two are written out separately below. Do not mix them: `mkdir -p`, `~` and
`unzip` do not exist in Windows PowerShell, and `Expand-Archive` does not
exist in a Unix shell.

**Before you start, know where the zip actually is.** Every command below
takes its full path, because a freshly opened terminal starts in your home
directory and the zip is almost certainly somewhere else — `unzip
pcbreview-skill.zip` failing with *cannot find or open* is that, and nothing
more. Wherever your file manager shows the zip, use that path.

You also need **KiCad installed**. The tool finds KiCad's own Python by
itself; you do not configure it.

---

## macOS and Linux

Open Terminal. You can paste these one at a time, in order.

**1 — unpack it into the skills directory.** Replace the path in quotes with
where your zip is. (In macOS Terminal you can type `unzip -o ` and then drag
the zip file onto the window to fill in the path.)

```bash
mkdir -p ~/.claude/skills && unzip -o "$HOME/Downloads/pcbreview-skill.zip" -d ~/.claude/skills
```

**2 — check that landed where Claude looks.** This must print a path, not
"No such file":

```bash
ls ~/.claude/skills/pcbreview/SKILL.md
```

**3 — install the Python dependencies.**

```bash
python3 -m pip install -r ~/.claude/skills/pcbreview/requirements.txt
```

**4 — optional: put the location in your shell profile.** Claude finds the
install by itself and does not need this, so skip it if you like. It is only
for when *you* want to run a command by hand:

```bash
echo 'export PCBREVIEW_HOME=$HOME/.claude/skills/pcbreview' >> ~/.zshrc && source ~/.zshrc
```

If your shell is bash rather than zsh, use `~/.bashrc` in both places. Note
that this only affects your own interactive terminal — a profile is not read
by every shell, which is exactly why the skill does not depend on it.

**Verify:**

```bash
cd ~/.claude/skills/pcbreview && python3 -m pytest tests/ -q
```

---

## Windows

Open **PowerShell** (not Command Prompt). Replace the zip path with yours.

**1 — unpack it into the skills directory:**

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.claude\skills" | Out-Null
Expand-Archive -Path "$HOME\Downloads\pcbreview-skill.zip" -DestinationPath "$HOME\.claude\skills" -Force
```

**2 — check that landed where Claude looks:**

```powershell
Test-Path "$HOME\.claude\skills\pcbreview\SKILL.md"
```

That must print `True`.

**3 — install the Python dependencies.** Windows usually has `python` rather
than `python3`; if `python` opens the Microsoft Store, use `py` instead:

```powershell
python -m pip install -r "$HOME\.claude\skills\pcbreview\requirements.txt"
```

**4 — optional: set the location permanently.** Claude finds the install by
itself and does not need this; it is only for running a command by hand.
**Close and reopen PowerShell afterwards**, because the current window will
not see it:

```powershell
[Environment]::SetEnvironmentVariable("PCBREVIEW_HOME", "$HOME\.claude\skills\pcbreview", "User")
```

**Verify**, in a *new* PowerShell window:

```powershell
cd "$HOME\.claude\skills\pcbreview"; python -m pytest tests/ -q
```

**Windows has been run.** The skill was installed and exercised on a real
Windows machine in August 2026 — this section is no longer theoretical, and
the KiCad-finding globs resolve against a real installation.

**Two Windows-specific defects survived that run and were fixed afterwards**,
which is worth knowing when judging how much the run proves. `find_pdfs`
searched both `Datasheets` and `datasheets`; those are one directory on a
case-insensitive filesystem, so every datasheet was read twice and half of
them reported as belonging to no part. And twelve `read_text`/`write_text`
calls had no explicit encoding, so on a cp1252 locale an SVG caption with an
em-dash or a stackup row with `±` would have raised `UnicodeEncodeError`.
Neither is reachable without a project laid out a particular way, which is why
a working run did not find them. If something fails, the "When something goes
wrong" section below is where to look first.

---

## What a correct install prints

**`782 passed, 88 skipped`**, or `777 passed, 93 skipped` without the optional
`pdfplumber`. If you see *failures*, the section at the bottom of this page
covers the ones that have actually happened.

Those 782 need no board of your own. They are closed-form physics — analytic
limits, IPC-2221, exact crosstalk identities, a Laplace solve checked against
a rectangle — plus every refusal the tool makes. That is a deliberate choice:
a fresh install has to be verifiable by someone holding nothing but the zip.

**The skips are not a problem and they are not confined to one file.** Three
kinds, and every one of them names itself in its skip message:

| skip | what switches it on |
|---|---|
| *no board installed for the role '…'* — the bulk of them, across `test_any_board.py`, `test_facts.py`, `test_severity.py`, `test_cli.py` and `boards.py` | register a board of yours: `PCBREVIEW_TEST_BOARDS`, further down this page |
| *pdfplumber is not installed* — 5, in `test_datasheets.py` and `test_reader.py` | `python3 -m pip install pdfplumber`; without it the datasheet-table stage is skipped and says so |
| *set `PCBREVIEW_TEST_LCSC`* — 1 | `PCBREVIEW_TEST_LCSC="C1234567=SOMEMPN"` checks the catalogue against a part you actually have. The codes stay in your environment because five real ones in one file are a bill of materials |
| *every installed board has a stackup* — 1 | register a board under the role `no_stackup` to exercise Gate 1 |

A skip that names none of those **would** be a packaging bug, and the build
refuses to produce a zip that has one.

## Upgrading to a newer zip

Replace the whole `pcbreview` directory rather than unzipping over it — a stale
module left behind by a rename is the kind of failure that looks like a bug in
your board.

**If you keep a backup, keep it somewhere else.** Anything with a `SKILL.md` in
`~/.claude/skills/` is registered as a skill, so
`~/.claude/skills/pcbreview.backup-…` becomes a *second* skill with a
byte-identical description, and which one gets picked is then arbitrary.

```bash
mkdir -p ~/.claude/skill-backups
mv ~/.claude/skills/pcbreview ~/.claude/skill-backups/pcbreview-$(date +%Y%m%d)
cd ~/.claude/skills && unzip -q ~/Downloads/pcbreview-skill.zip
```

Then re-run the check in *"What a correct install prints"* above; the numbers
move with each release, and the zip's own `README.md` carries the current ones.

## Installing for one project instead of every project

Put the directory in `.claude/skills/` inside that project rather than in your
home directory, and point `PCBREVIEW_HOME` at it. Everything else is the same.

---

## The one genuinely awkward thing: there are two Pythons

This trips everybody once, so it is worth two minutes.

**KiCad ships its own Python interpreter**, and it is the only one in the
world that can `import pcbnew` and read a `.kicad_pcb`. It has no numpy, no
scipy and no shapely, and you cannot usefully add them to it.

**The analysis side needs exactly the opposite** — numpy, scipy, shapely — and
does not need `pcbnew` at all.

So there are two interpreters and they cannot be merged. Exactly one file,
`pcbreview/extract.py`, runs under KiCad's Python; it converts the board to
JSON in millimetres and hands it over. Everything else runs under yours.

**You do not have to configure this.** The tool finds KiCad's interpreter by
itself: it globs the per-platform install locations and then *probes* each
candidate by actually running `import pcbnew`, because on Linux the distro
package puts `pcbnew` on the system Python and nothing in the path says so.

Where it looks:

| platform | KiCad's Python | `kicad-cli` |
|---|---|---|
| macOS | `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/*/bin/python3` | `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli` |
| Windows | `C:\Program Files\KiCad\*\bin\python.exe` | `C:\Program Files\KiCad\*\bin\kicad-cli.exe` |
| Linux | `/usr/lib/kicad/bin/python3`, `/usr/bin/python3`, snap, flatpak | `/usr/bin/kicad-cli`, snap, flatpak |

If yours is somewhere else, one environment variable fixes it and the error
message says so:

```bash
export PCBREVIEW_KICAD_PYTHON=/path/to/kicad/bin/python3
```

```bash
export PCBREVIEW_KICAD_CLI=/path/to/kicad-cli
```

**Honesty about platforms: this has only ever been run on macOS.** The Windows
and Linux paths above are educated guesses, written by reading how each
distribution lays itself out, and never executed on a real install of either.
They probe rather than assume, and both have the environment-variable escape
hatch, so the failure mode should be a clear error rather than a wrong answer
— but if you are the first person to run this on Windows or Linux, expect to
set one variable, and the paths above are where to start looking.

## Requirements

- **Python 3.10 or newer** for the analysis side. Validated on 3.13.
- **KiCad 7 or newer.** Board geometry is read through `pcbnew`; version-
  specific API differences are handled, but a KiCad far older than 7 will not
  work.
- The Python packages in `requirements.txt` — shapely 2.0+, numpy, scipy,
  scikit-rf, PyYAML, sexpdata, networkx, rtree, pytest. Floors rather than
  pins; the header of that file lists the exact versions this was validated
  against.

**`kicad-cli` is optional but wanted.** Without it the tool still works; it
loses KiCad's own DRC, and the check that reports which nets are left
unconnected degrades to a bare count and says that it did.

**A Chromium-family browser is optional.** The renderer produces SVG always,
and uses a headless browser only to rasterise a PNG alongside it. No browser
means no PNG, which is a normal outcome and not an error. `PCBREVIEW_BROWSER`
overrides the search.

## Reading datasheets — `python -m pcbreview.parts`

Optional, and it degrades in stages rather than failing:

| you have | you get |
|---|---|
| nothing extra | part identity from your local LCSC catalogue, if you have one |
| `pip install pdfplumber` | thermal figures read out of the datasheet's own tables, each with the page it came from, **and** `--pages`, which prints a document's text so Claude can read the rest |

**There is no model to install and none is wanted.** Whatever the two
deterministic stages miss, Claude reads and offers with `--read`, and the same
five audits check it: a page cite is required, the page must exist, the quoted
line must really be on it and contain the number, the value must pass the same
range checks, and the line's symbol must not name a different quantity.

An earlier version of this shipped a local vision model for scanned PDFs. It is
gone, and the measurement is why: on the scanned document it existed for, six
identical runs returned a value zero times, a seventh returned one, and that
one cited the wrong page. Claude has the document and the schematic and does
this properly.

An LCSC catalogue is looked for at `~/jlc-trawler-data/jlcpcb-components.sqlite3`;
`PCBREVIEW_LCSC_CATALOGUE` moves it or switches it off. Readings are cached by
the PDF's content hash, beside the solve cache, so a datasheet is read once ever
— including the part a person read.

## The solve cache

The field solver is 85% of a review, and every cross-section it solves is
cached on disk so a second review of the same board — or a first review of the
next board on the same stackup — reuses almost all of it. It is on by default
and needs no setup.

| | |
|---|---|
| where | `%LOCALAPPDATA%\pcbreview\solves.sqlite3` on Windows, `~/Library/Caches/pcbreview/` on macOS, `$XDG_CACHE_HOME` or `~/.cache/pcbreview/` elsewhere |
| how big | bounded at 100 000 entries, roughly 20 MB, least-recently-used pruned |
| what is in it | `sha256 -> a few floats`. **No geometry**: not a trace width, not a layer height, not a net name. That is deliberate, and it is why one cache can safely be shared across projects |
| moving it | `PCBREVIEW_SOLVE_CACHE=/some/path` |
| switching it off | `PCBREVIEW_SOLVE_CACHE=off`, or `--no-solve-cache` for one run |
| deleting it | delete the file. The next review is simply slower |

The key is a hash of the whole cross-section, the mesh parameters, **the
solver's own source** and the numpy/scipy versions, so upgrading anything that
could change an answer starts a fresh cache rather than serving a stale one.
`python -m pcbreview.audit <board> --verify-cache` re-solves everything the
cache served and exits non-zero if any of it disagrees.

## The shipped stackup library

`python -m pcbreview.stackups <project>` lists the fab stackups that fit a
board, and `use <CODE>` writes one and verifies it. They live inside the
package at `pcbreview/stackup_library/`.

Adding your own takes two files — the fab's layer table verbatim, and a small
YAML naming its impedance targets and where it came from. Put them in a
directory of your own and point `PCBREVIEW_STACKUPS` at it; they load beside
the shipped ones. `pcbreview/stackup_library/README.md` states the rules, and
the load-time checks enforce the mechanical half of them: an entry whose rows
do not parse, whose targets do not parse, or which claims a layer count its own
table contradicts is **reported as rejected, not silently dropped**.

A local entry may not shadow a shipped code. An entry is a transcription of
something a fab published, and a local file quietly standing in for one would
break the only guarantee the library makes.

## Running the tests against your own boards

The shipped suite is board-independent by construction. There is a second,
smaller set of **contract tests** in `tests/test_any_board.py` that assert on
the shape of the output rather than on any particular design — geometry
agreeing with KiCad's own engine to machine epsilon, every finding carrying a
number and a basis, the audit never writing to your file. Those are worth
running against your own board.

Point them at it with a YAML file:

```yaml
# my-boards.yaml
four_layer:
  pcb: ~/designs/sensor/sensor.kicad_pcb
  stackup: ~/designs/sensor/sensor.stackup.yaml
no_stackup:
  pcb: ~/designs/scratch/scratch.kicad_pcb
```

```bash
PCBREVIEW_TEST_BOARDS=my-boards.yaml python3 -m pytest tests/test_any_board.py -q
```

`tests/boards.py` lists every role and what each one has to provide. Roles you
do not list simply skip, and the skip message tells you how to switch them on.

Two more things worth running once:

```bash
python3 tests/validate_all_boards.py ~/designs
```

re-measures every board it finds against KiCad's geometry engine — outline
area, every zone fill, every net's track length, every pad's copper — and
prints the worst relative disagreement per board. It should be at machine
epsilon. This borrows nothing from the analysis code and is the strongest
check available.

```bash
PCBREVIEW_TEST_BOARDS=my-boards.yaml python3 tests/mutation_check.py
```

breaks one promise at a time in a throwaway copy and checks the contract test
guarding it goes red. All ten mutations should be caught. Run it if you change
anything in `findings.py` or `audit.py`.

## When something goes wrong

**`unzip: cannot find or open pcbreview-skill.zip`** — the zip is not in the
directory your terminal opened in, which is your home directory. Give the full
path to wherever the file actually is. This is the commonest failure and it is
not a problem with the tool.

**`mkdir: illegal option -- p`, or `unzip` / `mkdir` "not recognized"** — you
are in Windows PowerShell or Command Prompt running the macOS commands. Use
the Windows section above.

**`ModuleNotFoundError: No module named 'pcbreview'`** — Python cannot see
the install directory. Run the command with the path spelled out:

```bash
PYTHONPATH="$HOME/.claude/skills/pcbreview" python3 -m pcbreview.audit
```

If that works, the earlier failure was an unset or wrong `PCBREVIEW_HOME` and
nothing else. Note that a shell profile is not read by every shell, so a
variable that works in your terminal may be absent elsewhere — which is why
`SKILL.md` resolves the path itself rather than trusting it.

**`python3: command not found` on Windows** — use `python`, or `py` if that
opens the Microsoft Store.

**Claude does not seem to know about the skill** — confirm
`~/.claude/skills/pcbreview/SKILL.md` exists (step 2), and start a new Claude
session. Skills are discovered at session start.

**`no Python with pcbnew found`** — KiCad is not installed where expected. Set
`PCBREVIEW_KICAD_PYTHON`. The error lists everything it tried.

**An assertion about `wxApp` on stderr, on every run.** Harmless. `pcbnew`
prints `assert "traits" failed … create wxApp` when imported outside the GUI,
and it does not affect anything. Do not filter it out with a blanket
`grep -v assert`, though — that also deletes real error messages containing
the word, which has hidden a genuine failure here before.

**A zone-fill number that changes between identical runs.** KiCad's zone
filler is not perfectly reproducible on every board: some pours polygonise two
different ways, roughly one run in five, differing in the last few decimal
places. The extraction is exact; the variation is upstream of it. If a
comparison disagrees, re-run before believing it.

**The audit seems to hang.** It has not. Crosstalk and broadside coupling
dominate the runtime and are minutes on a large dense board. Every check
reports its own elapsed time when the run finishes.

**`the audit needs a validated fabrication stackup`** — working as intended.
See README on why a stackup is not optional and how to produce one.
