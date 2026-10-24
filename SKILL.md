---
name: pcbreview
description: Deterministic design review of KiCad PCB layouts and schematics. Use whenever the user asks to review, audit, or check a PCB — a `.kicad_pcb` or `.kicad_sch` file, a board's return paths, impedance, crosstalk, current capacity, voltage drop, board temperature, junction temperatures, decoupling placement, or board-versus-schematic parity. Also use when the user shares a KiCad board and asks "what's wrong with this", "is this routed properly", "will this overheat", or "review my layout", or asks what can be checked on a PCB.
---

# pcbreview

## Quickstart — the whole flow, in order

Resolve `$PCBREVIEW_HOME` first (Step 0, below). Then, in order:

```bash
# 1. bind a fab stackup. The library first — one click, no transcription.
#    Only transcribe a screenshot when nothing shipped fits (§2).
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackups project/
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackups project/ use JLC04161H-7628

# 2. THE REVIEW. One command, both entry points, and it selects what must be
#    reported. Point it at the project FOLDER and it finds every input itself.
#    It also DRAWS the pictures and lists them at the end — show every one.
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.run project/ > review.txt 2>&1

# 3. read the datasheets YOURSELF, offer each value with its cite, then write
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.parts project/
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.parts project/ --pages C1234567.pdf
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.parts project/ \
  --read "C1234567:theta_ja=36,page=1,quote=RthJA ThermalResistance --- 36 C/W"
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.parts project/ --write --watts U3=0.62

# 4. ask the user the questions the run raised, then record their answers
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.facts project/
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.facts project/ \
  --declare "/FIRE_CH*=power,current=5.0,rise=60,note=igniter, ~1 s per flight"

# 4b. when the user states what a path carries: drop, vias, board temperature
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.ampacity project/ --template
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.ampacity project/ --validate
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.ampacity project/ --check-grid --images out/

# 5. re-run (the pictures are redrawn), then draw anything else you explain
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.run project/ > review-2.txt 2>&1
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.render board.kicad_pcb out/ NET --caption "what this shows"
```

**Two things the user sees from every session, whatever else happens:**
pictures of the work (§"Pictures"), and a short list of what else you can do
for them (§"Offer what else you can do"). Neither waits to be asked for.

**Step 2 is the review.** It runs the layout audit and the schematic audit,
applies the user's severity policy, and prints the report — every `critical`
and `major`, every group of five or more, every check that could not run, every
found-but-not-measured pair, every DRC error, every assumption, and the render
floor. **That selection is code, not judgement**, and it is the same on Tuesday
as it was on Monday. You do not triage it. You read it, ask the questions it
raises, and write the prose.

Four more things `run` takes, when you want them in the same pass:

```bash
--facts       the nets table and the shortlist
--problems    the findings grouped by shared cause — the shape a fix list wants
--markdown F  the report as Markdown, for the user to keep
--json F      the whole structure, for you to query
```

**Quote a `--declare` whole.** A net name starts with `/`, and a
POSIX-emulating shell on Windows rewrites a bare `/GND` into a path before
Python sees it. The declaration is refused rather than silently written, but
quoting avoids the round trip.

**On Windows** it is `python`, not `python3`, and both variables go on one line:

```powershell
$env:PCBREVIEW_HOME = "$HOME\.claude\skills\pcbreview"
$env:PYTHONPATH = $env:PCBREVIEW_HOME; python -m pcbreview.run project\
```

The lower-level entry points are all still there and are what you drop to when
a question is narrower than a review:

```bash
python3 -m pcbreview.audit board.kicad_pcb --net "/FIRE_CH1_DRAIN"   # one net, everything
python3 -m pcbreview.audit board.kicad_pcb --grouped                  # the layout audit alone
python3 -m pcbreview.schematic_audit board.kicad_sch                  # the schematic alone
python3 -m pcbreview.stackup_cli transcribe …                         # a stackup the library lacks
```

The object model, for the questions the report raises. **Full table in
`REFERENCE.md` — every plausible name that does *not* exist is listed there,
and guessing costs round trips.**

```python
from pcbreview.model import Board            # the only loader; never import extract
board = Board.from_kicad("board.kicad_pcb")
board.tracks / .vias / .pads / .zones / .footprints / .copper_layers / .outline
board.net(name_or_code); board.tracks_on_net(code); board.zone_fill(layer, net)
board.nets                                            # {code: Net} — the VALUES have .name
track.width_mm  track.length_mm  track.layer          # not .width
pad.ref  pad.number  pad.designator  pad.copper       # not .polygon, not .key
via.pos  via.drill_mm  via.width_on(layer)            # .pos is a plain TUPLE
fp.ref  fp.value  fp.pos                              # not .reference, not .x/.y
from pcbreview.schematic import netlist               # no Schematic.from_kicad
pin.reference  pin.number  pin.name  pin.key          # not pin.ref
finding.title  finding.detail  finding.measurement    # not .message
from pcbreview import pipeline                        # discover() + review(), as `run` uses
result = pipeline.review_path("project/")             # .report .layout .schematic .project
```

---

You are the front end, and there is now much less of you in the loop.

The user gives you a board, a schematic, some datasheets and a fab stackup.
**The tool** routes the files, binds the stackup, runs both audits, reads the
datasheets, selects what must be reported and asks the questions it cannot
answer. **You** answer the questions with the user, write the numbers only they
know, draw the offending copper, and say in a sentence what the findings have
in common.

The engine computes and selects; you ask and explain. Never re-derive a
conclusion it already computed, never compute one it didn't, and never decide
what appears in the report — that is `pcbreview/report/generate.py`, and the
whole reason it exists is that two sessions with identical inputs once reported
different things.

---

## Step 0 — locate the tool. Do this once, before anything else.

`pcbreview` is a plain Python package sitting beside this file, so **`python3
-m pcbreview.audit` only resolves if Python can see that directory** — and you
will be running from the user's project, not from here.

Resolve it once and then **prefix every single command with it**. Do not rely
on `export` persisting: most agent harnesses spawn a fresh shell per tool call,
so the working directory survives and exported variables do not. Both outside
sessions hit this and both ended up prefixing every command, which is the right
answer — just write the prefix out every time.

It honours `$PCBREVIEW_HOME` if the user set one, and otherwise finds the
install itself:

```bash
export PCBREVIEW_HOME="${PCBREVIEW_HOME:-$(ls -d "$HOME/.claude/skills/pcbreview" ".claude/skills/pcbreview" 2>/dev/null | head -1)}"; echo "$PCBREVIEW_HOME"
```

**Do not assume the environment variable is already set.** A shell profile
sets it for the user's own terminal, but the shell you run commands in does
not necessarily read that profile — so relying on it is exactly how the first
command of a session fails. Resolving it yourself costs one line and always
works.

If that prints an empty line, the skill is somewhere else. Ask the user where
they unzipped it, or look:

```bash
find "$HOME" -maxdepth 6 -name SKILL.md -path '*pcbreview*' 2>/dev/null | head
```

Then every command below is prefixed with it:

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.audit board.kicad_pcb stackup.yaml
```

`ModuleNotFoundError: No module named 'pcbreview'` means one thing only: that
path is empty or wrong. It is never a broken install, and never a reason to
reinstall anything.

**On Windows**, the same idea in PowerShell:

```powershell
$env:PCBREVIEW_HOME = "$HOME\.claude\skills\pcbreview"
$env:PYTHONPATH = $env:PCBREVIEW_HOME; python -m pcbreview.audit board.kicad_pcb stackup.yaml
```

**Every example below says `python3`; on Windows that is `python`.** `python3`
there usually hits the Microsoft Store stub and fails confusingly. And set both
variables on the same line as the command, for the reason above.

**If KiCad is installed more than once** — which it is for anyone who upgraded
9 → 10 — say which one to use, and the matching `kicad-cli` is taken from
beside it automatically:

```powershell
$env:PCBREVIEW_KICAD_PYTHON = "C:\Program Files\KiCad\10.0\bin\python.exe"
```

---

## The flow

### 0. Take the files in whatever order they arrive

**The user attaches things in the order that suits them, not in the order you
want them.** A board, a stackup code, three datasheets, a note about what the
firing channels do — all at once, in any order, some of it prose rather than a
file. That is the normal case.

**Never decide what a file is from its position in the message or from an
assumed convention.** A `.yaml` could be a stackup, a thermal manifest or a
net-intent manifest, and the top-level key says which (`layers:`, `parts:`,
`nets:`). A `.kicad_sch` handed over first is still the schematic.

You do not have to sort them out yourself. Put everything in one folder, point
the tool at the folder, and read the inputs card it prints — §1.

### 1. Collect — and insist on the stackup

Ask for, in order of how much they change the answer:

| | |
|---|---|
| the board | `.kicad_pcb`. Required. |
| **the stackup** | **the fab stackup code they ordered** — `JLC04161H-7628`, and so on. If it is one this skill ships, that is the whole conversation. Otherwise a screenshot of the fab's stackup / impedance-calculator table. Required either way: the audit refuses without one. |
| the schematic | `.kicad_sch`, the root sheet. Without it, ground and differential pairs fall back to net-name guesses, six checks cannot run at all, and `pcbreview.parts` cannot attach a single datasheet to a designator. Push for it. |
| datasheets | PDFs. Drop them in the project folder; `pcbreview.parts` reads them. |
| the BOM | `.csv`, optional. |

**Put them in one folder and point the tool at the folder.** `pcbreview.run`,
`pcbreview.facts`, `pcbreview.parts` and `pcbreview.stackups` all take a
project directory and route what is in it by *reading* each file, so a `.yaml`
lands in the right slot whatever it is called and a `production/` folder full
of CSVs does not turn into three BOMs. The run prints what it decided, every
time:

```
inputs
  board      board.kicad_pcb
  stackup    board.stackup.yaml
  schematic  board.kicad_sch
  BOM        bom.csv
  datasheet  C1234567.pdf
  note: 2 other .csv file(s) were left alone (designators.csv, positions.csv)
```

**Read that block.** It is the one place a mis-routed file shows up, and a
silent reassignment would be worse than a refusal. Two files claiming the same
slot is refused by name rather than guessed at.

**Prose is an input too.** "The firing channels fire for about a second" and
"the 5 V rail feeds the actuators" are facts no file carries and no geometry
implies. They belong in the net-intent manifest — `pcbreview.facts --declare`
writes it — not only in your reply.

**Insist on the stackup and do not work around its absence.** Not KiCad's
Board Setup — that is an unedited default, and on real boards it has been
wrong by over 2x on copper thickness and 95–99% on dielectric heights. Not a
guess from the layer count. Not "assume 1.6 mm FR4". The refusal is the
feature: every impedance, coupling and return-path number depends on real
dielectric heights, and without them the report would be confident nonsense
rather than obviously absent.

### 2. Bind a stackup — the library first, transcription only if it must be

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackups project/
```

lists the fab stackups this skill ships that have **this board's copper layer
count**, each with its provenance, and names the ones that do not fit and why.
Every entry is a transcription of something a fab published — never an
estimate, and an entry that cannot prove that is dropped at load rather than
offered.

**Show the user the list and let them choose.** Which stackup a board is being
built on is a fact about an order somebody placed; nothing here knows it, and
picking the only candidate for them is how a board gets reviewed against the
wrong dielectric. Then:

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackups project/ use JLC04161H-7628
```

writes `<board>.stackup.yaml` and **immediately re-solves every impedance
target from its own stated geometry against the number the fab published.**
That verification is the point of the command, not a nicety: it is what catches
a misread dielectric before it becomes a whole review of wrong impedances.

**Judge the residual against the solver's known bias, not against zero.** It
reads about −1.5% versus JLCPCB, most likely unmodelled solder-mask thickness,
so a target landing at exactly 0.0% would be as suspicious as one at +16%. The
panel says so itself. Anything the report marks `[OK]` is fine.

A stackup already beside the board is **never quietly replaced** — `use`
refuses, and `--replace` copies the old one aside first. It may be a
transcription somebody checked by hand against a screenshot, and losing that to
a mis-click is only noticed much later, when a number is wrong for a reason
nobody can find.

**When nothing shipped fits**, transcribe the fab's own table. Never
hand-author a dielectric constant; the transcriber derives ε_r from the glass
style in the material string.

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackup_cli init board.kicad_pcb
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackup_cli transcribe board.kicad_pcb rows.txt \
  --out board.stackup.yaml --name "JLC04161H-7628" \
  --target ohm=50,kind=se,layer=L1,bottom_ref=L2,width=0.3424,to_copper=0.508 \
  --target ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.2672,spacing=0.2032,to_copper=none
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackup_cli verify board.kicad_pcb board.stackup.yaml
```

**`--out` is what creates the file.** Without it the YAML goes to stdout and
nothing is written, so `verify` would have nothing to read.

**The targets are the check on your transcription.** `verify` solves each one
and compares against the fab's own stated impedance. **Anything inside ±3% is
`[OK]`** — that is the number to judge by. The often-quoted −1.4% to −1.7% is
the residual for a *single-ended* target on a 4-layer stackup, which is what it
was calibrated on; coplanar differential targets land elsewhere and **a
positive residual is normal there**. Do not re-read the screenshot over a sign.

**On a 2-layer JLC stackup, expect `STK006` on the core row and do not treat it
as a transcription mistake.** Their 2-layer calculator names the core by
thickness and copper weight only and never states a glass style, so there is
nothing to derive ε_r from and the default is used. This makes transcribing the
impedance target matter **more** on 2-layer boards, not less: the target is the
only thing that constrains the core.

Two transcription rules that are easy to get wrong and silent when wrong:

- **`to_copper=none` is not the same as omitting `to_copper`.** "Non coplanar"
  in the calculator is a physical difference, not a missing field. Omitting it
  is refused, deliberately, because an absent key cannot be told apart from a
  forgotten one.
- **Transcribe every row.** Stopping halfway down a six-layer screenshot
  produces a plausible-looking four-layer board. This is refused against the
  board's actual layer count, but only if you gave the real board.

A stackup you transcribe is worth keeping. `PCBREVIEW_STACKUPS` points at a
directory of your own entries, loaded beside the shipped ones — see
`pcbreview/stackup_library/README.md` for the two files an entry needs.

### 3. Run the review — one command, both entry points

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.run project/ > review.txt 2>&1
```

**This is the whole review.** It runs the layout audit *and* the schematic
audit — they check disjoint things and neither runs the other's checks — then
applies the user's severity policy and prints the selection. The report's
section 0 states, in the one place a reader cannot miss, whether each entry
point ran: a review that stays silent about the schematic reads as saying the
schematic is clean, and one board shipped with `/SCL` and `/SDA` each driven by
two output pins because of exactly that.

**It writes nothing** unless you ask it to, and it reads `<board>.stackup.yaml`,
`<board>.thermal.yaml`, `<board>.netintent.yaml`, `<board>.severity.yaml` and
the BOM from beside the board.

Exit codes, and they are load-bearing:

| | |
|---|---|
| `0` | reviewed |
| `1` | **reviewed and refused** — Gate 1, a board that cannot be read. A correct outcome; the message says what to do. Do not "fix" the board over one |
| `2` | the command was not understood — a bad path, a bad flag |

#### Reading a big one

The report starts with a `CONTENTS` block giving each section's size, so you
know what you have before scrolling it. On a dense board it is genuinely large
— one four-layer board selects **278 findings into section 1 and 11 groups into
section 2**, and section 1 alone is 1 800 lines.

Those 278 are 11 facts repeated. So on any board where section 1 runs past
about forty:

1. read **section 0** (what ran), **section 2** (the causes), and
   **sections 3, 3b, 4 and 5** — that is the whole shape of the review and it
   is a few hundred lines;
2. read the **render floor** and the **questions**;
3. then `grep` section 1 for the cause you are chasing, rather than reading it
   through.

**You still report all of it.** Grepping is how you read the file, not a
licence to leave findings out — lead with the cause and list its consequences
in full, exactly as section 2 does. `--problems` prints the same findings
grouped by cause and by check if you want that shape without the report around
it.

#### Never pipe it through `tail`

**The head of the output is the part you cannot afford to lose.** The inputs
card, the entry-point states and every `critical` are in the first forty lines,
because the report is printed in rule order. Two outside sessions piped the
older command through `tail` and one was a step from reporting "no criticals"
on a board that has one. Redirect to a file and read it from the top;
`head -80 review.txt` is the right first look.

#### What it costs

**Usually under a minute; the field solver is 85% of it.** Measured: 39 s for
an 87 × 82 mm 4-layer board with 1033 tracks; well under a minute for a
185 × 59 mm 4-layer with 1045 tracks. One pathological case exists — a
37 × 23 mm *two-layer* board with 325 tracks took 125 s, because a 2-layer
board has no plane between its two signal layers and every net couples to every
other. **Do not background the first run.** The pictures add about half a
minute: 27 of them took 30 s on a 140 x 45 mm four-layer board, most of it one
browser screenshot each.

**A second run is much cheaper — but only the solving is.** Every cross-section
the solver computes is cached on disk, keyed by a hash of the geometry, the
solver's own source and the numeric library versions, so a re-run of an
unchanged board reuses almost every solve: 3–5x faster, measured. The cache is
shared between boards on the same stackup, so the second board of a project
starts warm. It holds `sha256 -> capacitance matrices` and no geometry at all,
which is why sharing it is safe.

What is *not* cached is the geometry walk, KiCad's DRC and the parity and
placement checks — seconds, not minutes, but not zero. **So the second run is
cheap, not free.** Re-run after the user answers something, not to re-read
something you already have in `review.txt`.

`--no-solve-cache` turns the cache off for a run. `--verify-cache` re-solves
everything the cache served and exits non-zero if any of it disagrees; use it
if you ever have cause to doubt a number, and never routinely.

#### The older commands, and when to drop to them

`pcbreview.run` replaced a three-command flow — a grouped triage pass, an
`--all` coverage pass, and a separate schematic audit — and you should not go
back to it. Everything those three printed is in the one report, selected by
rule instead of by judgement.

Drop to `pcbreview.audit` when the question is narrower than a review:

```bash
python3 -m pcbreview.audit board.kicad_pcb --net "/FIRE_CH1_DRAIN"   # one net
python3 -m pcbreview.audit board.kicad_pcb --all                      # every info finding
python3 -m pcbreview.audit board.kicad_pcb --grouped --review         # and record the run
```

**`--all` and `--grouped` are two passes, never one command.** `--all` adds
every `info`, and on a real board most `info` findings are coverage statements
— so `--all --grouped` produces a grouped view that is mostly `[INFO]` headings
and buries the thing you were grouping to find. `--help` says so too; one
session ran the audit three times before working it out.

`--review` writes a `review/` folder beside the board and is how you get
run-to-run comparison (`N resolved, N new, N changed severity`). If the board
is in a git repository, offer to add `review/` to `.gitignore`.

**Run `pcbreview.schematic_audit` bare. `--report` emits no findings at all** —
it is a descriptive dump and is useful for reading a design, not for reviewing
it. Two outside sessions ran `--report`, searched its output for a findings
section, found none, and one was a step from reporting the schematic as clean.

### 4. What the report already decided — and what is left for you

Two Opus 5 sessions reviewed the same board with the same prompt and the same
datasheet, and reported **different things**. The engine did not vary — same
inputs give byte-identical findings, and that is tested. The *selection* did,
because nothing constrained it.

That selection is now code. `pcbreview/report/generate.py` implements these
rules and `pcbreview.run` prints the result, so **there is no triage left to do
by hand**:

| section | what is in it |
|---|---|
| 0 | **both entry points**, each with whether it ran and over what. A schematic that was not reviewed is a reported state with a reason, never an omission |
| 1 | **every `critical` and every `major`.** No exceptions and no summarising away |
| 2 | **every group of 5 or more**, by shared cause, each heading followed by *every* finding under it |
| 3 | **every check with zero coverage**, rendered as *could not run*. `impedance … (1 fab target(s), 0 net(s) examined)` is a blind check, not a clean one |
| 3b | **every found/measured pair that disagrees.** A non-zero count can mean "did not look", and it is far harder to spot because the number reads as evidence. One board reported `esd_clamps: 11` and no findings while thirteen of its fourteen clamped lines had never been compared |
| 4 | **every DRC error**, from KiCad's own rule engine — rule types *and* violation counts, because one rule type can carry fourteen violations |
| 5 | **every assumption the run made** |
| render floor | the largest group, plus every net a `critical` or `major` names |

Every section prints even when it is empty, and that is deliberate: *"no check
was blind"* is a result, and a missing section is indistinguishable from a rule
that never ran. A `rule trace:` line at the bottom says how many items each rule
contributed, so you can see that rule 3b found nothing rather than was not run.

**Report all of it.** Judgement decides **order and prose** — what to lead
with, what to explain, what to recommend. It does not decide what appears. If
you leave something out, you have made the review unrepeatable again, and the
generator existing has bought nothing.

**If you think a finding is insignificant, say so *and* report it.** That is
what section 1 means by no exceptions.

#### The three things a program cannot do, which are your job

1. **Ask the questions the run raised.** Every finding carries `intent_needed`
   — the datum that would settle its severity — and `run` prints them
   deduplicated, with the evidence attached, under `QUESTIONS FOR THE USER`.
   **That block is your worklist.** §"The net-intent manifest" is how the
   answers get written down.
2. **Supply the numbers only the designer has.** What a net actually carries,
   for how long, and what a part actually dissipates. None of these is in any
   file and no measurement produces them.
3. **Say what the findings have in common.** *"These 27 findings are all the
   +3V3/GND reference boundary; fixing it fixes all of them"* is the sentence
   the user needs, and the list under it is what lets them check you. The
   grouping key is a shared **measurement**, not a shared explanation — writing
   the explanation is §5.

`--problems` prints the same findings a second way, grouped by cause and by
check, which is the shape a fix list wants. Use it when the user asks *"what do
I change first"*; the report itself is in rule order, which is right for *"is
this the same review as yesterday"* and wrong for that question.

### Pictures — every run draws them, and you show them

**`pcbreview.run` draws the review's pictures itself**, after the report, and
ends its output with a `PICTURES` block listing each file in the order to show
it. What it draws, chosen by code like everything else in the report
(`pictures.py`):

1. **the board** — top copper, whole, so every crop after it has a context;
2. **one picture per group of five or more**, from the group's most severe
   member that has copper to draw. Its note says how many findings it stands
   for;
3. **every `critical`, then every `major`** outside those groups, up to twelve.
   The rest are counted, never dropped silently;
4. **a temperature map per scenario and a current map per path**, whenever a
   power-path manifest is declared.

**Show every one of them to the user, beside the finding it illustrates.** Use
whatever your harness gives you for putting a file in front of the user — a
send-file or attachment tool if you have one; otherwise a Markdown image with
the full path, `![caption](/full/path/02-group-return_path.png)`, and the path
itself. "Pictures were written to a folder" is not showing them.

**Look at each picture yourself before you describe it.** Open the PNG. If it
does not show what its caption claims — a highlight off the edge of the crop,
a map that is all one colour — say so and draw a better one; never describe the
picture you expected.

They go to the OS cache, one folder per board, emptied at the start of each
run so a picture of an older revision can never be shown as this one. Nothing
is written beside the board. `--pictures review/img` keeps them where the user
wants them; `--no-pictures` is for when the user asks for text only.

**Then draw what the conversation needs.** The run draws the picture of each
cause; you draw the rest — the net the user asks about, a finding you explain
that has no picture yet, a part they point at. Do not hand-render every
finding: a dense board has three hundred, and that is minutes spent on
pictures nobody looks at. The report's **render floor** (the largest group,
plus every net a `critical` or a `major` names) is the minimum you should be
able to show on request.

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.render board.kicad_pcb out/ NET_NAME [NET_NAME…] \
  --caption "what this picture shows" --layers F.Cu,In1.Cu
```

`--caption` prints in a band at the **top** of the image and is where the
explanation goes — it wraps to as many lines as it needs, so write a sentence.
`--layers` restricts the drawing. Output filenames are built from every net's
leaf name plus a digest covering the nets, the layers, the caption and the
region, so two renders never overwrite each other.

**A finding that names no net can be drawn too, by region:**

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.render board.kicad_pcb out/ \
  --around 367,42,12 --layers F.Cu,In1.Cu,In2.Cu \
  --caption "the +BATT plane necks to 0.755 mm2 of copper here, carrying 26 A"
```

`--around x,y,r` and `--bbox x0,y0,x1,y1` crop to a box in board millimetres
instead of to a net's copper, so `thermal`, `placement` and `stackup_mismatch`
— which name no net and are precisely the findings that most want a picture —
are no longer unillustratable. A finding's `location` is the natural centre.
You may name nets **as well**, in which case they are still highlighted and
only the crop changes; with no net named nothing is dimmed, because dimming
exists to make a highlight stand out and there is none.

`render_finding` still refuses a netless finding by default — pass `radius_mm=`
to crop around its `location` instead. The refusal is deliberate: guessing a
crop scale for someone else's board is the kind of invented number this project
refuses, so you have to say how much of the board to show. The run lists those
findings under *not drawn* for the same reason; if one is a `critical` or a
`major` and carries a `location`, choose the region and draw it with
`--around`.

**Rendering matters most for the nets nobody named.** A board is full of
`Net-(U1-USB_D+)`, `Net-(R12-Pad2)`, `Net-(C29-Pad2)`. Telling a user
"`Net-(C29-Pad2)` has a problem" is useless. Showing them the trace is not.
That is the strongest reason the renderer exists — do not skip it because a
net "has no real name".

### Everything about one net, in one command

When the conversation turns to a particular net — and it always does — this is
the artefact:

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.audit board.kicad_pcb --net "/FIRE_CH1_DRAIN"
```

Widths and lengths per layer with each one's ampacity, the weakest stretch **by
capacity rather than by width**, via count and barrel ampacity, poured area per
layer, every pad **in the user's own designators** (`J8.1, J9.1, JP3.2`), and
every finding that names the net. Nothing in it is a new measurement; the value
is that it arrives together. Do not assemble this by hand. **Draw the net as
well** and show both — the user asked about copper, so show them the copper.

### 5. Explain

Every finding already carries `basis` (what was computed, from what geometry),
`assumption` (anything taken rather than measured) and `intent_needed` (the
datum that would settle its severity). Your job is to turn those into prose
and a recommendation — not to re-derive the conclusion.

Lead with what the user should act on. A ranked list is not a report. Put each
picture beside the cause it shows, as §"Pictures" says.

**End every report with what you can do next** — three to six offers chosen
from this run, as §"Offer what else you can do" describes. Every check that
could not run is one: the datasheets that would give temperatures, the
currents that would give thermal maps, the BOM that would give assembler
parity.

---

## Offer what else you can do

Most users do not know what this can check, so they ask for less than it can
give. Offer, as suggestions, at two moments.

**When the user's ask is open** — "can you help with my PCB?", "what can you
do?", a board with no question attached — show the menu below before anything
else, then ask for the board and the stackup it was ordered on. Show the whole
list once; keep each line to what the user would *get*, not which command
gives it:

- **Review a board end to end** — layout and schematic together: return
  paths, impedance, crosstalk, current capacity, placement, board against
  schematic, and KiCad's own DRC, with a picture of every problem
- **Check a fab stackup** against the fab's own impedance figures, and against
  the stackup saved in your KiCad file
- **Voltage drop, via current and thermal maps** for the power paths you name,
  at the currents you give
- **Junction temperatures** from the datasheets you send, each figure cited to
  its page
- **Everything about one net** — widths, current capacity, vias, every finding
  on it — with a picture
- **A picture of any net, part or area** of the board
- **Board, schematic and BOM compared**, so what the assembler builds matches
  what you drew
- **What changed since the last review** — fixed, new, and changed severity
- **Turn down a check** that does not apply to your design, on the record

**After every report**, end with three to six offers chosen from *this* run —
not the whole menu again. In order of how much they would add:

1. each check under *could not run*, as the thing that would unblock it
   (`thermal` → "send the datasheets for U3, U7 and Q2 and I'll give junction
   temperatures"; `power_path` → "tell me what the battery and servo rails
   carry and I'll map the drop and the heat"; `bom_parity` → "add the BOM");
2. the question that would change the most severities, asked as an offer;
3. "everything about" the net behind the worst finding, with its picture;
4. keeping the report and pictures in a `review/` folder beside the board, if
   there is not one already — and, if there is, what changed since last time.

Suggestions are offers, not a to-do list the user must answer, and they never
replace reporting what the run found.

---

## A second review, when the manifests are already there

Everything above is written for a cold start. **If the board already has
`<board>.stackup.yaml`, `<board>.thermal.yaml`, `<board>.netintent.yaml` or a
`review/` folder beside it, an earlier session wrote them and the flow is
different.** A session with no guidance here invented a policy, and said in as
many words that a different session would have invented a different one — so
this is the policy:

**Look before you run anything** — and the cheapest look is the run itself:
`pcbreview.run`'s inputs card lists every manifest it found and section 5 lists
every one it read. If you want it before committing to a review:

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.stackups project/   # what is bound, and from where
ls -la "$(dirname board.kicad_pcb)" | grep -E 'stackup|thermal|netintent|severity|review'
```

1. **Read every manifest you find, and say in your report that you did**,
   naming each file. They are inputs, and a review that does not say which
   inputs it had is not reproducible. `report.assumptions` lists them too.
2. **Do not regenerate a manifest that exists.** The stackup was transcribed
   from a screenshot you may not have, the thermal entries came from PDFs that
   were handed over in a session you cannot see, and the net-intent manifest is
   the user's own judgement. Re-deriving any of them silently replaces evidence
   with inference.
3. **Merge, never rename, into an existing `<board>.netintent.yaml`.** The
   audit prints `proposed.netintent.yaml`, which is a *generated inference over
   every net*. Renaming it over a curated manifest destroys the one thing in
   this tool that is the user's. The run now says so itself when the file
   exists — but check anyway.
4. **A manifest can be wrong, and saying so is your job.** If a net-intent
   entry demotes something this run flags as `major`, report the finding *and*
   the declaration that quietened it. Demotion is the only thing anywhere that
   lowers a severity.
5. **Say what changed.** With a `review/` folder, each run prints
   `N resolved, N new, N changed severity` against the previous one, and which
   artefacts moved. Lead the report with that — on a second review it is more
   useful than the ranked list.

### Reading `review/latest.json`

`--review` writes `review/latest.json`, a copy of the newest run. It is the
cheap way to triage without re-parsing console output. The schema, exactly:

```
{"when": "2026-08-04T14-22-01", "board": "…",
 "artefacts": {"board": "<sha>", "schematic": "<sha>", …},
 "summary": {"board", "stackup", "findings", "by_severity",
             "checks_run", "checks_skipped", "seconds"},
 "findings": [ … ]}
```

Each entry in `findings` has these keys and no others:

`check`, `severity`, `score`, **`title`**, `nets`, `layers`, `location`,
`measurement`, `basis`, `assumption`, `intent_needed`, `confidence`.

**The headline is `title`. There is no `message` key** — a reporter guessed
that one and got a `KeyError`. `geometry` is deliberately absent: it is
un-serialisable and it is the coordinates this tool exists to keep out of your
reasoning.

```python
import json, collections
run = json.load(open("review/latest.json"))
print(run["summary"]["by_severity"])
print(collections.Counter(f["check"] for f in run["findings"]
                          if f["severity"] in ("critical", "major")))
for f in run["findings"]:
    if f["severity"] == "critical":
        print(f["check"], "|", f["title"], "|", f["nets"])
```

---

## The thermal loop

Thermal is **off by default and that is correct.** With no manifest the check
emits one `info` finding listing the parts worth a datasheet. That output is
the point: it is what tells you which PDFs to ask for.

So the loop is: run once, read the shortlist, ask the user for exactly those
datasheets, **let `pcbreview.parts` read them**, supply the one number it
cannot, and run again.

### `pcbreview.parts` — two stages of code, then you

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.parts project/
```

One row per part that dissipates: what it is, what its datasheet states, **the
page each number came from**, and — for the ones that cannot be written yet —
exactly what is missing and why.

Two deterministic stages fill what they can:

1. **the LCSC catalogue.** Datasheets are usually named for their LCSC code
   (`C1234567.pdf`) and schematic symbols usually carry one, so a part's class,
   package and ratings resolve with nothing else needed. Looked for at
   `~/jlc-trawler-data/jlcpcb-components.sqlite3`; `PCBREVIEW_LCSC_CATALOGUE`
   moves it.
2. **the datasheet's own tables.** `pdfplumber` plus patterns on `RθJA` /
   `RthJA` / `Theta-JA` and `°C/W`. Every value carries its page.

**You are the third stage.** There is no local model here — the app this came
from grew one because it has no Claude in it, and on the one case a model is
actually needed for (a scanned page) it answered about one time in seven, with
a wrong page cite when it did. You have the document. So:

```bash
# what is still open, and which document to open
python3 -m pcbreview.parts project/

# the document's text, page by page — no PDF tooling of your own needed
python3 -m pcbreview.parts project/ --pages C1234567.pdf

# offer a value, with the page and the exact line you read it from
python3 -m pcbreview.parts project/ \
  --read "C1234567:theta_ja=36,page=1,quote=RthJA ThermalResistance --- 36 C/W"
```

**Quote the line exactly as `--pages` printed it**, run-together spacing and
all — the check compares against that text, not against a rendered page.

**Five audits stand over what you offer, and they did not change when the
reader did.** A value must (1) carry a page cite, (2) cite a page that exists,
(3) quote a line that is really on it *and contains the number*, (4) pass the
same range checks the table path uses, and (5) not quote a line whose **symbol**
names a different quantity — `RθJC` beside a value offered as θJA is refused,
because one real vendor labels a row `RθJC` and calls it "Junction to Ambient"
in the prose, and 3 °C/W to still air does not exist for a 3 mm package.

A refused value is **kept and shown** with its reason, not dropped silently: a
field that stayed empty and a field that was offered 3.0 and refused need
different work from you.

**`--pages` printing zero characters on every page means it is a scan.** Open
the PDF and read the image yourself, then offer it with `--vision`. That marks
the values `vision` rather than `read`, because audit 3 cannot run without
text — a real weakening, recorded rather than smoothed over.

Readings are cached by the PDF's **content hash**, so a datasheet is read once
ever — including the part you read.

**Then write the manifest:**

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.parts project/ --write --watts U3=0.62
```

`--watts` is the one number nothing can read. **`dissipation_w` is not in the
datasheet** — it is an operating-point calculation: *0.62 W is (5.0 − 3.3) ×
365 mA at the worst-case load*. Work it out with the user, or leave it out.
Leaving it out is a real answer: the part is written `dissipation_stated:
false`, the loader's own third state, and the check then reports **how much the
part can dissipate** before it reaches its rating rather than reading a missing
load as nought watts.

The manifest is validated by `thermal.load` **before** it lands — a file this
wrote that the audit then refused would be worse than none — and a hand-written
one is copied aside once before the first overwrite, because a YAML round trip
does not keep comments and the comments are where the reasoning lives.

**Writing it makes the review on screen stale.** The manifest is an *input* to
the audit, so re-run `pcbreview.run`; do not patch findings in your head.

### What `parts` cannot do, and what you do about it

**A blocked row is a specific question with the evidence attached.** Read them
and put each to the user by designator. Four shapes, and each needs a different
answer:

| the row says | what you do |
|---|---|
| *no datasheet for `C…` was supplied* | ask for that PDF, by LCSC code |
| *this symbol carries no LCSC code* | ask the user what the part is. Nothing can resolve it |
| *this document has no text layer at all* | it is a scan. `--pages` it to confirm, read the image yourself, then `--read … --vision` |
| *no designator to attach it to* | its filename carries no LCSC code that anything on the board matches. If you can see which part it is, `--attach <pdf>=<REF>` records that; if it belongs to no part on this board, say so and do not drop it |
| *this part dissipates and has no junction* | an inductor, a shunt, a fuse. `thermal.load` has no entry shape for it and **no check here computes its temperature** — size it from its own datasheet and say so in the review |

That last one is not hypothetical: on one project, of three datasheets and four
semiconductors, the **one document that read completely belonged to no part on
the board**. One of three documents and zero of four parts, and the two counts
point at different work.

### What is in the parts table, and why that is not a designator prefix

Two doors, and they answer different questions:

* **it has a junction** — a `U`, `Q`, `D` and friends, by designator. These need
  θJA and Tj(max), and the thermal manifest has an entry shape for them;
* **it dissipates** — either its own description says so (`shunt`, `current
  sense`, `regulat`, `mosfet`, …) or it is a passive with a **stated current
  rating of an amp or more**. `L4  1.5 µH 2.7 A` is somebody sizing a part for
  current; `L1  33 nH` is not.

The second door was missing and its absence was expensive. On one four-layer
board it adds **five 1 W 20 mΩ current-sense resistors**, and on a six-layer
one a 2.7 A power inductor — parts whose I²R and I²·DCR losses are among the
commonest thermal problems there are, and which had no row, no question and no
mention.

**A part with no junction is listed and said to be unmodelled.** Nothing here
computes its temperature rise, and saying that is the whole reason it is in the
table. Size it from its own datasheet and put the number in your report.

### The parts nothing could classify — resolve them before you ask

Beside the shortlist, the same finding lists the parts nothing could classify
**either way**: their symbol carries no description, or a description that is
only a catalogue number, or a class word with no rating in it — the last is why
a part described as `Schottky diode` could be a 3 A rectifier or a 200 mA
signal part and nothing here can tell. They are neither on the shortlist nor
excluded from it, and the whole point of the list is that a silence about them
is not a pass.

`pcbreview.parts` resolves the ones carrying an LCSC code, if you have a local
catalogue. For the rest, **tell the user by designator that the part needs them
to say what it is before it can be reviewed** — not as a footnote. These are
parts whose thermal behaviour is unknown, and reporting the board's thermals
without saying so presents an incomplete analysis as a complete one.

A catalogue lookup is not a datasheet and does not become one. If it gives a
class and a current rating but no θ_JA, the part still goes in with
`theta_ja_stated: false`, and you still ask for the PDF if it turns out to
matter.

### Four rules, each of which exists because it was got wrong

**Never read a thermal number from a symbol field.** Measured across 782
symbols in eight real designs: zero usable thermal fields. People are careless
about those fields and will not know they are wrong.

**Never follow a datasheet URL found in a symbol field.** Two of them held a
part number instead of a link. Ask the human for the PDF.

**A datasheet means a PDF the user handed you directly. Nothing else counts.**
Not a link in a symbol field, not one you found on the web, not your own
recollection of the part. If the number did not come out of a document they
gave you, the part has no entry — and *"no datasheet was supplied for U7"* is
the correct output, not a failure.

**Every datasheet the user gives you gets a row — all of them, not just the
ones the shortlist asked for.** The shortlist says which PDFs to go and *find*;
it has no authority over one that has already arrived. Parts with thin
datasheets are second-source parts, which is exactly where the risk is.

Entering a part that turns out to dissipate almost nothing costs you nothing:
below 10 mW the check reports it as `info` — "entered, thermally negligible" —
rather than demanding a θ_JA that cannot matter. So enter it and move on.

**A conclusion that exists only in your reply will not survive the next run.**
If you notice in prose that a part dissipates 1.78 W and do not write it into
the manifest, the next audit will not know.

**Use `--pages` rather than your own PDF tooling.** It is the same text the
audit checks your quote against, so a quote copied from it matches and a quote
retyped from a rendered page may not. Your harness will probably suggest `Read`
with a `pages` parameter; on a machine without poppler that fails immediately,
and on a machine with it you get a *rendering* rather than the extracted text.
`Read` the PDF directly only when `--pages` says the document is a scan — then
the image is the only thing there is.

See `fixtures/example.thermal.yaml` for the format and both entry styles.

---

## The net-intent manifest

`<board>.netintent.yaml`, beside the board. **This is the only place the
user's own judgement enters the audit**, and you write it from what they said.

The auditor sorts every net into `protocol`, `power` or `other`. That
inference is good at buses and rails and blind to anything else — on a real
board, more than half of all nets land in `other` for no better reason than
that nothing identified them.

**So an unidentified net is ranked at full severity, deliberately.** Custom
symbols under-name their pins constantly, and absence of evidence is not
evidence that a net does not matter. Nothing in the tool lowers a severity on
its own; the only thing that does is the user's own declaration here. Say so
if they ask why the list is long: the alternative was quietly demoting half
the board on no evidence.

What classification still decides is which nets reach the **power-scoped**
checks — `current_capacity` and `heat_path`. Those need real evidence of being
power: a pin name, a rail, a track over 0.2 mm wide, or the user saying so.

The standard case the inference gets wrong is a firing channel or any power
*output* that no power symbol drives — structurally invisible, and often the
most safety-relevant net on the board.

### Ask, then write it down with `pcbreview.facts`

**The questions are already written for you.** `pcbreview.run` ends with a
`QUESTIONS FOR THE USER` block: every finding's `intent_needed`, deduplicated
by (check, question) with the nets unioned and the evidence attached. On one
board that turns 103 rows into about a dozen — and each one is a question the
engine knows how to ask because the check that raised it knows what would
settle it.

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.facts project/
```

prints the same thing beside the **shortlist**: the nets nothing identified
that findings actually name. On a 229-net board that is 9 to 13 rows, and they
are the only rows where an answer changes a severity. `--all-nets` prints the
rest; `--measured` fills in what the copper actually carries, which is worth
having before you ask somebody what a net *must* carry.

**Ask about those nets, by name, before you draw conclusions.** Asking after
you have written the report is a wasted round trip — in one case on a
deployment burn wire.

Then record the answer:

```bash
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.facts project/ \
  --declare "/FIRE_CH*=power,current=5.0,rise=60,note=igniter, ~1 s per flight" \
  --declare "/TP*=other,note=test points, probed once at bring-up"
```

The spec is `net=class` plus optional `,current=`, `,rise=` and `,note=`; the
note is taken off the end, so its commas are its own. The classes are `power`,
`protocol` and `other`.

What `--declare` guarantees, and why each clause is there:

* **the merge is safe.** An exact match updates in place; a net covered by a
  glob gets its own line inserted *above* that glob, so declaring one of eight
  `/FIRE_CH*` nets does not silently re-declare the other seven;
* **it is validated by the engine's own loader before it lands**, so a file the
  audit would refuse never becomes your file;
* **a hand-written manifest is copied aside once** before the first overwrite,
  because a YAML round trip drops the comments the reasoning lives in;
* **a declaration matching no net on the board is refused, not written.** It
  would write cleanly, load cleanly and change nothing — so the next review
  reports the same severity and the user concludes their file was ignored.
  `--force` overrides.

**On Windows, quote the whole argument or a POSIX-emulating shell will eat the
leading slash.** Git Bash rewrites a bare `/GND` into `C:/Program
Files/Git/GND` before Python sees it. The refusal above catches it and says so,
but quoting is cheaper: `--declare "/GND=power"`, or `MSYS_NO_PATHCONV=1`.

**Writing marks the review stale.** The manifest is an *input* to the audit, so
re-run `pcbreview.run`; the facts table shows an edit immediately and still
reports `declared: false` until a run has actually read it, which is the honest
state and not a bug.

**You may write a first cut yourself, provided every inferred entry says so in
its `note`.** The user correcting a concrete file beats them answering an
abstract question. Infer **promotions only** — never infer a demotion, because
that is the one direction that hides findings.

```bash
--declare "/BURN_A=power,note=INFERRED, NOT STATED BY THE USER: runs to a 2-pin JST labelled BURN over ~110 mm of 0.25 mm track. Confirm or delete this entry."
```

Then ask about the specific nets, by name, with what you inferred and why.

Patterns are `fnmatch` against **board** net names — schematic and board net
names do not round-trip through KiCad, so match on what the board calls them.
First match wins. A pattern matching nothing is reported rather than ignored,
and `--declare` refuses one outright.

The file itself is plain YAML and you may hand-edit it; `--declare` is the
route that cannot get the merge wrong. See `fixtures/example.netintent.yaml`.


### Current, and the temperature rise it is allowed to cause

**Ampacity is now reported for every power net, always.** With no declared
current it is an `info` — *"this net carries 1.65 A at a 10 C rise"* — because
that is a fact about the copper. Declare the current and it becomes a verdict.

```yaml
nets:
  - match: "/FIRE_CH?_DRAIN"
    net_class: power
    current_a: 5.0            # what it actually carries
    rise_c: 60                # what rise is acceptable ON THIS NET
    note: "igniter firing current, ~1 s, once per flight"
```

`current_a` is **the one fact only the designer can supply**, and it is what
turns a number into a pass or a fail. Ask for it on anything that carries
power.

**`rise_c` defaults to 10 C and the default is deliberately strict.** Every net
is judged against a continuous 10 C rise unless the user has said it may run
hotter. That will produce false positives on pulsed conductors, and that is the
intended trade — the ruling, in the user's words: *"We must ALWAYS prefer false
positives and pestering the user with questions and clarifications, than
silently pass something."*

**So when a net might not be continuous, ask.** A firing channel, a burn wire,
an inrush path, a solenoid — infer what you can from the schematic and the net
name, and where you cannot, put the question to the user:

> `/FIRE_CH1_DRAIN` is 0.600 mm throughout, which carries 1.65 A continuously at a
> 10 °C rise — a third of the 5 A you mentioned. If these fire briefly rather
> than continuously, how long, and what rise is acceptable? At 60 °C the same
> copper carries about 4 A.

Then write the answer down. **Continuous nets need no `rise_c`** — the 10 C
default is the right judgement for them.

One thing this does *not* model: a genuine thermal transient. A 1 s pulse on a
thin outer trace is a substantial fraction of the way to steady state, so
`rise_c` is a stated allowance, not a solved cooling curve. Say so when you use
it.

**A `/` inside a net's own name is written `{slash}`, and your pattern must be
too.** KiCad escapes it that way in hierarchical labels, sheet pins *and* board
net names, and nothing here unescapes it. So a pin the schematic draws as
`SHDN/VIO` is `…SHDN{slash}VIO` on the board, and:

```yaml
- match: "*SHDN/VIO*"        # matches NOTHING, silently
- match: "*SHDN{slash}VIO*"  # correct
```

Every real design has some: any pin labelled with two functions
(`PTB4/IRQ11`, `SHDN/VIO`, `20/A6`) becomes a net name carrying `{slash}`. The
leading `/` of a *sheet path* is a real slash and stays a real slash; only a
`/` that was part of the label's own text is escaped. When in doubt, read the
name out of the run's own output and paste it rather than retyping it.

**It demotes as well as promotes, and demotion is now load-bearing** — it is
the only thing anywhere that lowers a severity. Test points, spare nets and
nets the user has told you they do not care about are legitimate things to
quieten, and this is the only way to do it:

```yaml
nets:
  - match: "/TP*"
    net_class: other
    note: "test points, probed once at bring-up"
```

See `fixtures/example.netintent.yaml`.

---

## Power paths — drop, via current and board temperature at a stated current

`current_capacity` compares a net's feeds with each other and needs no
current. The moment the user says **what a path carries** — "the servo rail
pulls 6 A", "we might draw near the connector's maximum", "will this get hot
in the bay" — the questions become absolute, and this is the tool for them.

```bash
# a skeleton listing every power net's pads by part — PRINTED, never written
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.ampacity project/ --template

# write <board>.powerpaths.yaml from what the user said, then check it
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.ampacity project/ --validate

# the analysis: ~1 min for a dozen paths on a six-layer board
PYTHONPATH="$PCBREVIEW_HOME" python3 -m pcbreview.ampacity project/ \
  --check-grid --images out/ > ampacity.txt 2>&1
```

`pcbreview.run` picks the manifest up on its own, beside the board, and its
findings land in the main report as check `power_path`. Without one, the
report lists `power_path` under *checks that could not run* — the same
treatment `thermal` gets, for the same reason: **a current is intent**.

**`pcbreview.run` also draws the maps** — a temperature map for each scenario,
in the environment the board's temperature is judged in (`enclosed` unless one
is declared), and a current-density map for each path — and
lists them with the review's other pictures. **Show both every time you report
a power path.** The temperature map is where the heat is; the current map is
why, with the busiest vias marked. Say which environment the temperature map
was drawn in, because it is one end of the bracket, not the board's
temperature.

### What it computes, and from what

* **DC current flow** over the path's copper on *every* layer at once —
  pours, tracks, pads — joined by every via and plated hole as a plated
  barrel. Copper thickness and layer spacing come from the bound stackup.
  Out of it: the drop to each sink, the I²R, **each via's share of the
  current**, and where the loss concentrates.
* **Board temperature** from that loss and from the thermal manifest's
  `dissipation_w`, through a conduction model with one sheet per copper
  layer, the laminate between them, every via as a vertical heat path, and
  the two faces cooling to ambient. Steady state, and a transient for pulses.

### Writing the manifest

```yaml
environment: enclosed        # enclosed | still_air | forced_air | W/m^2K
ambient_c: 40
paths:
  - name: servo_rail
    net: "+VSERVO"
    from: [L2.2]                               # U3.17-24 is a range
    to:
      - {pads: [J10.2], current_a: 6.0}
    rise_c: 20                                 # allowed; default 10
  - name: igniter
    net: "/FIRE_OUT"
    from: [Q3.5]
    to: [{pads: [J7.3], current_a: 7.6}]
    pulse_s: 1.0
    rise_c: 60
scenarios:
  - {name: flight, paths: [servo_rail]}
```

Five rules, each of which changes a number:

1. **One path per net, with every load of that net as a sink.** Two paths on
   one net are solved apart and their heat added, which misses the cross term
   of `(I1 + I2)²`; the run says so when it happens.
2. **Terminals are pads, and the right pads.** `from` is where the current
   enters the net (a regulator's output pins, a connector's power pin), `to`
   is where it leaves. A wrong pad is refused by name with the pads that part
   *does* have on the net — read the list rather than guessing again.
3. **A ground return is a path too.** The return from a 6 A load to its
   regulator's ground pin is often the hottest via on the board.
4. **Ask what runs together.** Without `scenarios` every continuous path runs
   at once, which is the worst case the declared currents allow — correct as
   a default, pessimistic as a description of flight.
5. **A pulse is `pulse_s`, not a smaller current.** It is solved as a
   transient on top of the continuous load, and its vias are judged as a pulse.
6. **Switching current is given as RMS.** The solve is DC, and I²R is right
   for any waveform only at its RMS value. In a buck, the copper from the
   switch pin to the inductor carries the inductor current all the time —
   about `I_out` RMS, a little more with ripple — while the input pins carry
   pulses of it, about `I_out * sqrt(D)` RMS rather than the average
   `I_out * D`. Say in the path's `note` which one you used.

**Maximum-current questions** ("what if we draw the connector's rating") are
paths too: one path per connector at its contact rating times its power pins,
**capped by what the regulator behind it can deliver**, which the run does not
know — say which of the two is the limit.

### Temperatures are a bracket. Report them as one.

Every temperature is solved in three environments — `enclosed` (5 W/m²K per
face), `still_air` (8.1), `forced_air` (30) — and printed as a bracket.
**`still_air` is fitted to IPC-2152's measured baseline**, so it is the IPC
answer; `enclosed` is a stated worse case and `forced_air` a textbook one.
With an environment declared in the manifest, every verdict is taken there.
Without one, the two temperatures below are judged in different places, and
each finding asks which environment it really is:

* a conductor's **own rise** in `still_air` — where IPC-2152 defines a rise
  and where the default +10 °C allowance comes from;
* the **board's temperature** against its absolute limits in `enclosed`, the
  worst case.

**Never quote one of the three as "the board temperature".** Say the bracket
and which end the verdict used: *"+8 °C in a sealed bay, +5 °C in open still
air, +3 °C with airflow — the board judged at the sealed-bay end because you
have not said where it sits."* `heatsink.py` explains why: the surroundings
move this more than anything on the board does.

Two temperatures per path, and they answer different questions:

* **its own current** heating its own copper — the IPC sense of "rise", and
  what `rise_c` is judged against, in still air;
* **the whole board** — copper loss plus part dissipation, absolute, against
  laminate and connector limits (major at 90 °C, critical at 105 °C).

A path whose own heating is +2 °C can sit on a board that is +20 °C from
everything else. Report both, and do not let one stand in for the other.

**A part's own rise under its pads is not a junction temperature** and must
not be added to `theta_JA * P` — `theta_JA` already contains the board under
it. What *does* belong in a junction estimate is the `neighbour` finding: how
much everything else raises the board the part sits on.

### What it does not model — say this when you report a temperature

Packages as solids (heat enters at the pads; nothing leaves the package top),
conduction out through harness wires and connectors, an enclosure's walls
warming, and — unless `--coupled` is given — copper resistance rising with
temperature (about 4% per 10 °C; `--coupled` re-solves hot). It has never been
compared with a thermocouple on a real board. **A bench measurement settles
what this estimates**: load the path, read the drop with a meter and the
temperature with a probe or an IR camera, and believe those over this.

### How far to trust the numbers

* Resistance and drop: closed-form strip and annulus to 0.5% and 1.7% at the
  default 0.1 mm grid. On a real board `--check-grid` halves the pitch and
  reports the change for every path — 0.3% to 7% on a six-layer board, and
  always downward, because a coarse grid can only overstate resistance.
* Via currents: exact sharing in symmetric cases; on a real board each share
  carries the grid's error.
* Temperatures: the solver is exact against closed form, and `still_air`
  reproduces IPC-2152's measured baseline — a single trace on a 1.78 mm board
  with no planes — within 0.86-1.19x on 54 outer-trace cases and 0.93-1.50x
  (hot, the safe side) on 36 inner ones. **The environment is not known**,
  which is the whole point of the bracket. IPC-2221's working point (1 mm,
  1 oz, 2.39 A, +10 °C) now comes out near IPC-2152's +13.1 °C; it used to
  read +8.7 °C, a third too cool.
* Current capacity, everywhere — tracks, necks, via barrels — is IPC-2152's
  still-air chart, one curve for inner and outer layers, with no credit for
  planes. On a board under 3 x 3 in the run says IPC-2152 would use its
  conservative chart instead, and gives that number.

`REFERENCE.md`, *Power paths*, has the full validation table and where every
threshold came from.

---

## The severity policy — when the user says a check is not their problem

`<board>.severity.yaml`, beside the board. KiCad's *Violation Severity* page,
for this tool. `pcbreview.run` reads it, applies it after both audits and
before the report is selected, and says in the assumptions what it withheld.

```yaml
# severity policy, written by pcbreview.
enabled: true

checks:
  field_hygiene: ignore      # "our symbol library is a known mess; stop telling me"
```

**There are exactly two levels, `report` and `ignore`, and that is the
interesting part.** Setting a severity by hand is deliberately not possible:
the engine computes severity from a threshold on a measured quantity, so a
severity somebody typed would be a number with nothing behind it. An earlier
version did allow it; policies still carrying `major:` or `minor:` are read and
those lines are **dropped rather than honoured**, and the run says so.

Four things about it, and each one is why it is safe:

* **`ignore` still runs the check and still measures.** It withholds the
  findings from the ranking. **It is not a pass**, and the count is stated —
  *"12 finding(s) are held out of the ranking by your severity policy"* —
  rather than a shorter list being shown quietly;
* **nothing is destroyed.** `enabled: false` restores every withheld finding at
  once without deleting a line of the file;
* **it needs no re-measurement.** A policy withholds; it computes nothing. The
  audit is not re-run because of one — though `pcbreview.run` is a fresh
  process, so in practice you do re-run, and the solve cache makes that cheap;
* **a policy that will not parse is NOT silently treated as absent.** The run
  says the policy could not be read and was not applied, and every finding is
  at the severity its check computed. A review that looks like it honoured the
  user's settings and did not is the worst outcome available here.

**Write one only when the user asks**, and say in your report that a policy was
in force and what it held back. It and a net-intent declaration are the only
two things in this tool that quieten a finding, and both exist because the user
said so — never because a finding looked unimportant to you.

---

## Going beyond the CLIs

The audit runs every layout check in one call and no measurement anywhere is
produced by you. But triage raises side questions — which nets carry pours,
was this pair detected — and those are answered in process:

```python
from pcbreview.model import Board
board = Board.from_kicad("board.kicad_pcb")     # the only loader you need
```

**Read `REFERENCE.md`'s *"The object model — a cheat-sheet"* before you write
the second line.** Every attribute name on `Board`, `Footprint`, `Pad`, `Track`,
`Via`, `Zone` and `Net` is in that table, along with the plausible-looking names
that do not exist. It is short. `fp.reference` cost one session three round
trips when `fp.ref` is in that table twice — and an `AttributeError` here looks
like a broken install rather than a typo.

`Board.from_kicad(path)` shells out to KiCad's own Python, caches the
extraction and returns a plain `Board`. There is no `Board.from_file`.

**Never `import pcbreview.extract`.** It imports `pcbnew` at module scope, so
it fails under every interpreter you will actually run — and the traceback,
`ModuleNotFoundError: No module named 'pcbnew'`, looks exactly like the
broken-install failure below while having a completely different cause.

`stackup.load_and_validate(path, board.copper_layers)` takes the **layer name
list**, not the board. Passing a `Board` raises `TypeError: object of type
'Board' has no len()` from inside `validate`, which says nothing useful.

---

## Rules that are not obvious and that you will otherwise get wrong

**The report is the artefact of record; your prose sits above it.** The order
`pcbreview.run` prints in is *rule* order, which is right for *"is this the
same review as it was yesterday"* and wrong for *"what do I change first"*. So
lead with the cause and the recommendation, and put the report under it — do
not reorder or abridge the report itself.

**One run, not three.** The old flow was a triage pass, a coverage pass and a
schematic audit; `pcbreview.run` is all three and it selects. Re-run when an
*input* changed — a manifest was written, the user answered something, the
board moved — and not to re-read something already in `review.txt`.

**Exit code `1` is a refusal, not a crash.** Gate 1 refusing a board with no
stackup is the feature working. `2` means the command was not understood. Read
stderr before deciding a board is broken.

**Read the coverage line before reporting a zero.** Every check says what it
had to look at, and each coverage line carries the number that makes a zero
readable: `impedance … (1 fab target(s), 0 net(s) examined)` means one target
was loaded and no net matched it — a blind check, not a pass. `differential_
pair … (0 pair(s) found …)` likewise. If coverage is zero, say the check could
not run, not that it passed.

**`verify` names which field it thinks you misread.** A failing impedance
target now prints `this residual is consistent with: …` — the single-field
perturbations that would reconcile it, including a flip of `kind` and of
`is_coplanar`, which no width or gap correction reaches. More than one usually
reconciles, which is itself the answer: go back to the screenshot rather than
applying the first one.

**Report `intent_needed` as a question to the user, not as a caveat to bury.**
When a finding says its severity depends on what a net is for, ask. A correct
measurement whose significance is nil is worse than no measurement, because it
costs the user attention. And when they answer, write it into the net-intent
manifest (see above) rather than only into your reply.

**`board_state: 0 findings` is not a clean board.** It measures connectivity
and file state. Rule violations — clearance, courtyard, silkscreen,
hole-to-hole — are the separate `drc` check, which reports KiCad's own engine.
Read both. A board with nothing unconnected and six DRC errors is a real and
common shape, and two of those errors were pads 0.11 mm apart on a switching
node.

**`render` is also the right way to get any picture of this board** — not just
findings. Do not hand-roll a `kicad-cli` SVG export: its default scale is
3.78 px/mm, so a 37 mm board comes out 140 px wide and you will end up
rewriting the width/height attributes and driving headless Chrome yourself.
`render` has done all of that. `--caption` carries the explanation and
`--layers` restricts the drawing; both are in the filename digest, so two
renders never overwrite each other.

**Never quote the item description from a `board_state` finding.** KiCad names
one of the two items either side of a missing connection and *which one varies
between identical runs*. Net names and per-net counts are stable; item text is
not. Quote the nets and counts.

**Re-run before believing that a number changed.** KiCad's zone filler is not
perfectly reproducible — roughly one run in five, some pours polygonise
differently in the last few decimals. If a value moved and nothing you did
should have moved it, run it again.

**`check_crystal_loop` has still never measured a real discrete-load
oscillator loop.** One board has had a crystal: a 3-pin ceramic resonator with
*integrated* load capacitors, which has its own measurement path (terminal
lengths, their mismatch, and the centre ground pad's distance to a plane via)
and its own thresholds — and those thresholds are judgement, stated as such on
the finding. The discrete two-capacitor loop-area path is still synthetic-only.

**`check_missing_pullups` has never fired either** — one open-drain pin across
seven designs, and it had its pull-up.

If either fires, say plainly that it is the first time this check has seen this
case on real hardware, and that the finding deserves a human's eye.

**A candidate that produces no finding at all is now impossible in
`placement`.** Every path out of `crystal_loop`, `kelvin_sense`, `esd_position`
and `regulator_feedback` emits something — a measurement, a "measured and
within limits", or a "NOT measured" naming the reason and what that reason is
normally expected for.

This started with crystals, where `crystals=1` read as proof the check had run
when it had silently declined. Auditing the other three by tracing which
`continue`s actually execute on five real boards found worse: one board
reported **`esd_clamps: 11` and no findings at all** while thirteen of its
fourteen clamped lines had never been compared — no connector pad sat on them,
so "is the clamp upstream of what it protects" had no answer. Read the
`_measured` count beside every count; where they differ, something was found
and not measured.

**Beware the reverse, too:** an `info` reading "measured, within limits" is a
positive statement that the check ran and found nothing wrong. It is not
filler, and it is the only thing distinguishing a clean result from a skipped
one.

**Do not claim EMC pass or fail.** Nothing here replaces a chamber. Crosstalk
and coupling numbers are a ranking of what to look at.

**Do not modify the user's board.** Nothing in the tool writes to a
`.kicad_pcb` and you should not either. Recommend changes; do not make them.

---

## What the checks are

Thirty-one, over three entry points. `python3 -m pcbreview.audit` needs a
stackup; the schematic entry point never touches that gate.

**Layout** — `board_state`, `drc` (KiCad's own rule engine, reported not
reimplemented), `stackup_mismatch`, `panelisation`, `impedance`,
`return_path`, `reference_split`, `layer_transition`, `differential_pair`,
`crosstalk`, `broadside`, `conductor_neck`, `current_capacity`, `heat_path`,
`power_path` (needs `<board>.powerpaths.yaml`), `thermal`, `parity`,
`bom_parity`, `placement`.

**Schematic** — `pin_type_coverage`, `unconnected_power_pin`, `floating_pin`,
`output_conflict`, `missing_pullup`, `decoupling`, `dnp_dependency`,
`field_hygiene`.

**Placement** (needs board *and* schematic) — `decoupling_loop`,
`regulator_feedback`, `kelvin_sense`, `esd_position`, `crystal_loop`.

`REFERENCE.md` says what each one measures, what it was validated against, and
where its thresholds came from. Read it before you explain a threshold to the
user, and before you ever suggest changing one.

---

## Files you write, and files you never ask the user to write

You cause all of these to be written, and mostly by running a command rather
than by authoring YAML. **The user never edits one.**

| file | written by | from |
|---|---|---|
| `<board>.stackup.yaml` | `pcbreview.stackups … use CODE`, or `stackup_cli transcribe` | the fab's published table |
| `<board>.thermal.yaml` | `pcbreview.parts … --write` | the PDFs they supply, what you `--read` out of them, plus your `--watts` |
| `<board>.datasheets.yaml` | `pcbreview.parts … --attach` | which document belongs to which part, where the filename does not say |
| `<board>.netintent.yaml` | `pcbreview.facts … --declare` | what they say about which nets matter |
| `<board>.powerpaths.yaml` | you, from `pcbreview.ampacity … --template`, then `--validate` | which pads each current runs between, how much, for how long, and where the board sits |
| `<board>.severity.yaml` | by hand, rarely | the user asking for a check to be re-levelled or ignored |

**Prefer the command to hand-authoring in every case.** Each of those writers
validates through the engine's own loader before the file lands, keeps a copy
of anything hand-written, and refuses the specific mistake that is silent when
made by hand — a stackup that does not describe this board, a thermal entry the
audit would reject, a net-intent declaration matching nothing.

The BOM `<board>.csv` is the one file they produce — it is whatever their
KiCad exporter or fabrication toolkit already emits, and it is compared
against the schematic as a third opinion.

---

## If something fails

**`no validated fabrication stackup`** — working as intended, and the message
already names the shipped stackups that fit this board. Show the user that list
and let them choose; `python3 -m pcbreview.stackups <project>` prints it again
with each entry's provenance. Only transcribe a screenshot when nothing fits,
and never invent one.

**A command exits `1` rather than `0`** — that is *reviewed and refused*, and
it is a correct outcome with a sentence attached. `2` is *the command was not
understood*: a bad path, a bad flag, a declaration naming no net. Do not "fix"
a board over an exit code of 2.

**`no Python with pcbnew found`** — KiCad is not where the tool looked. The
error lists what it tried; `PCBREVIEW_KICAD_PYTHON` fixes it. See INSTALL.md.

**`pcbnew … could not load <board>` naming two version numbers** — the KiCad
that was found is **older than the board's file format**. It is never a broken
install and never a corrupt board. Point at the newer one:

```bash
PCBREVIEW_KICAD_PYTHON="/path/to/KiCad/10.0/bin/python3"
```

The matching `kicad-cli` is then taken from beside it automatically, so
rendering and DRC follow. `PCBREVIEW_KICAD_CLI` overrides that separately if
you need it.

**`AttributeError: 'NoneType' object has no attribute 'Zones'`** — the same
thing, from an older build of this skill that did not check. Same fix.

**`kicad-cli could not plot …: Failed to load board`** — again the same thing,
this time for the renderer: a `kicad-cli` older than the board. The error now
says so; set `PCBREVIEW_KICAD_PYTHON` and the sibling `kicad-cli` is used.

**A stderr line about `wxApp` and a failed `traits` assertion** — harmless,
printed by `pcbnew` on import outside the GUI. Ignore it, but do not filter
stderr with a blanket `grep -v assert`: real errors contain that word too.

**`verify` reports a target off by more than 3%** — you misread the
screenshot. Do not proceed; re-read it.
