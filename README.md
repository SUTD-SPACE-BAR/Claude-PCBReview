# pcbreview

**A design review of your KiCad board, computed rather than guessed.**

Thirty checks measure your copper — return paths, impedance, crosstalk,
current capacity, junction temperatures, placement, board-versus-schematic
parity — and hand back a ranked list of findings, each carrying the number it
came from, the geometry it was measured on, and what would change its
severity.

You do not run it. **Claude runs it for you** — one command over your project
folder — and shows you the handful of things that matter with the offending
trace drawn in a picture. You supply the board, the schematic, which fab
stackup you ordered, and datasheets when it asks for them.

---

## What makes this different from a rule checker

KiCad's DRC already tells you what violates a design rule. This is not that,
and it does not reimplement it — where DRC is the right tool, it calls DRC.

What it does instead is **compute the physics**:

- a **2D electrostatic field solver** for the real routed cross-section, not a
  closed-form approximation of it, so an impedance answer accounts for solder
  mask, plane spacing, and the copper actually beside the trace;
- **crosstalk** from that solver's mutual capacitance and inductance, both
  same-layer and broadside — traces stacked on adjacent layers with nothing
  between them, which is the coupling people forget;
- **return paths**, by looking at the copper under the trace layer by layer,
  including what happens at every via;
- **current capacity** by eroding the conductor to find its narrowest real
  channel, and a Laplace solve for a plane's true two-terminal resistance;
- **junction temperatures** from datasheet numbers you supply, plus how far
  your board is from the JEDEC card those numbers assume.

Every conclusion is computed in code — and so is **what gets reported**. Which
findings must appear in a review is a set of rules in `pcbreview/report/`, not
a judgement call, because two sessions given identical inputs once reported
different things. The LLM's job is to ask you the questions the run raised,
write down the answers only you have, explain, and draw. It never eyeballs a
coordinate, and it never decides what to leave out.

## What it refuses to do

These are deliberate, and they are the reason to trust the rest.

**It refuses to run without a stackup.** Not KiCad's Board Setup — that is a
default nobody edits, and on real boards it has been wrong by more than 2x on
copper thickness and by 95–99% on dielectric heights. Everything KiCad shows
you (length tuning, impedance hints, the 3D view) is computed from those
numbers. So it ships a **library of fab stackups** — each one a transcription
of a table a fab published, with its provenance recorded — and picking the code
you ordered is the whole conversation. If yours is not in it, you send a
screenshot and Claude transcribes it. Either way the result is then **checked
against the fab's own impedance targets through the field solver** rather than
trusted. If a digit was misread, that check catches it.

**It never reads a thermal number out of a symbol field, and never follows a
datasheet URL found in one.** Measured across 782 symbols in eight real
designs: zero usable thermal fields, and two "datasheet" fields containing a
part number instead of a link. Thermal data comes from a PDF you hand over —
read by pattern-matching the datasheet's own tables first, and by Claude for
whatever those miss — and **every number carries the document and the page it
came from**. Any value Claude offers must also quote the line it read it out
of, and that quote is checked against that page, and against the symbol beside
it, before the value is kept. An unattributed thermal number is
indistinguishable from a guess.

**It does not claim EMC pass or fail.** Nothing here replaces a chamber.

**It does not decide what your nets are for.** It infers buses and rails from
manufacturer pin names, and a net it *cannot* classify keeps full severity —
custom symbols under-name their pins, so silence is not evidence that a net
does not matter. Where your judgement is needed the finding says so and names
the datum it wants. You tell Claude in a sentence, Claude records it, and that
record is the only thing that quietens anything.

**It never writes to your design, and by default it writes nothing at all.**
Reading a board does not modify it, and a test asserts the file's timestamp is
unchanged. Findings are printed and then gone. If you want them kept, it
offers to make a `review/` folder beside your board — every run's data, yours
to read, commit or delete — and that is the only thing it ever creates.

## What you hand over

| | | |
|---|---|---|
| the board | `.kicad_pcb` | required |
| the schematic | `.kicad_sch` | strongly recommended — without it, ground and differential pairs fall back to net-name guesses, and several checks cannot run at all |
| **the stackup** | the fab stackup code you ordered, or a screenshot of your fab's stackup/impedance table | **required.** Nothing else is accepted |
| datasheets | PDFs | for the parts it asks about, after the first run |
| the BOM | `.csv` | optional; compares what the assembler will build against the schematic |

Anything you say about which nets matter — "the firing channels are the ones
to watch" — Claude writes into a manifest so it survives the next run.

## What comes back

Findings ranked by severity, each with the measurement, the derivation, and
what was assumed rather than measured. For anything that names a trace, a
rendered picture of that trace on the board — which matters most for the nets
KiCad auto-named, where "`Net-(C29-Pad2)` has a problem" is useless and a
picture is not.

A full audit is roughly 30 seconds on a moderate four-layer board and a few
minutes on a large dense one; the field solver is 85% of that. It is not hung.

**A second review is 3–5x faster.** Every cross-section the solver computes is
cached on disk and reused — across runs, and across boards built on the same
stackup, so the second board of a project starts warm. The cache holds hashes
and capacitance matrices and no geometry at all, which is why sharing it is
safe.

## Honesty about coverage

Two checks ship having **never fired on real hardware**: the crystal-loop
placement check (it has still never measured a real oscillator loop — the one board with a crystal used an integrated-load-capacitor resonator, which has its own path now) and the open-drain
pull-up check (across seven designs, exactly one pin was typed open-drain, and
it had its pull-up). Both have synthetic tests. Neither has seen the case it
was written for. Shipping a check that has never seen hardware is defensible;
shipping one silently is not.

More generally: **read the coverage line before believing a zero.** Every
check reports what it had to look at. A check with nothing to look at reports
zero findings, and that is not the same as a clean board — one check went four
boards before anyone noticed it was blind.

## Installing and running

See [INSTALL.md](INSTALL.md). Short version: unzip, `pip install -r
requirements.txt` on your own Python, and have KiCad installed — the tool
finds KiCad's bundled Python itself.

Then verify the install:

```bash
python3 -m pytest tests/ -q
```

A correct install prints **`734 passed, 87 skipped`** — `729 passed, 92
skipped` if you have not installed the optional `pdfplumber`. Those 734 need
no board of yours: they are closed-form physics — analytic limits, IPC-2221,
exact crosstalk identities, a Laplace solve against a rectangle — plus the
tool's own refusals. The skips are the opt-in tests, which run against a board
*you* register, plus the handful that want `pdfplumber`; each one says which
in its own skip message. See [INSTALL.md](INSTALL.md) and
[tests/boards.py](tests/boards.py).

The commands, for reference. You will not normally type any of them — Claude
does. They need the install directory on `PYTHONPATH`, because `pcbreview` is
a plain package and you will be running from your own project:

```bash
export PCBREVIEW_HOME=~/.claude/skills/pcbreview
```

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.audit board.kicad_pcb stackup.yaml [schematic.kicad_sch] [thermal.yaml]
```

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.schematic_audit top.kicad_sch --report
```

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackup_cli verify board.kicad_pcb stackup.yaml
```

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.render board.kicad_pcb out/ NET_NAME
```

## Going deeper

- [REFERENCE.md](REFERENCE.md) — how each module works, what it was validated
  against, and where every threshold came from. Read this before changing one.
- [tests/](tests/) — the validation, shipped in full and meant to be run and
  extended. `tests/mutation_check.py` checks that the contract tests actually
  fail when the contract is broken.
- [tests/validate_all_boards.py](tests/validate_all_boards.py) — point it at a
  directory of your own boards and it re-measures every one against KiCad's
  geometry engine. This is the strongest check available and it borrows
  nothing from this code.
