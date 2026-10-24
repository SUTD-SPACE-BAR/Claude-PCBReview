# The shipped fab stackup library

Gate 1 refuses to review a board without a validated fabrication stackup, and it
is right to: every impedance, coupling and reference-plane number in the report
depends on real dielectric heights. Until now the only way past it was to
transcribe the fab's table by hand — which, for a board built on a stackup
somebody had already transcribed, is typing out an answer that exists.

This directory is that wall turned into a list. Each entry is one fab stackup
already transcribed and **verified against the field solver**, so binding it is
one command and no typing:

```bash
python -m pcbreview.stackups <project>              # what fits this board
python -m pcbreview.stackups <project> use JLC0216  # write it, and verify it
```

## What an entry is

Two files, and the split is deliberate.

| file | what it is |
|---|---|
| `<code>.rows.txt` | **the fab's own layer table, pasted verbatim.** Tabs, header line, mil and mm columns, exactly as it comes off the fab's site. Diffable against a screenshot by eye |
| `<code>.yaml` | what the table cannot say: the impedance targets, the provenance, and a pointer back to the rows file |

Nothing is duplicated between them. `stackup.parse_jlc_table` reads the rows and
derives ε_r **from the glass style in the material string** — `7628` is not
`3313` — rather than from anyone's memory, which is why the table is kept as a
table rather than pre-parsed into numbers.

## What an entry may and may not contain

**Only what a fab actually published.** An entry is a transcription, never an
estimate, and every field has to be traceable to a document:

* `rows` — pasted from the fab's stackup table. If a value is not in that table
  it does not go in the file.
* `targets` — from the fab's **impedance calculator**, in the same
  `key=value` form `stackup_cli transcribe --target` takes, so there is one
  parser and no second format to keep in step. `to_copper=none` is how a
  non-coplanar target is written, and leaving it out is an error rather than a
  default — `stackup_cli.parse_target` explains why at length, and a real
  six-layer transcription is the mistake it is explaining.
* `finished_thickness_mm` — **only if the fab's table states it.** Two of these
  three say it in their header line; `JLC0216` does not, so its entry says
  `null` rather than the layer sum. The sum is a different quantity and calling
  it the finished thickness would be inventing a tolerance nobody quoted.
* `provenance` — where the rows and the targets came from, in plain words. An
  entry without one is not usable, because the whole value of the library is
  that it is not guesswork.

**Adding a stackup does not need code.** Drop the two files in;
`pcbreview/stackups.py` picks them up, validates them at load, and reports —
with a reason — any entry whose rows do not parse or whose targets do not. A
rejected entry is *listed as rejected*, never silently absent: this file
promises that two files are all it takes, so the tool owes an answer when they
were not enough.

**Keeping your own entries outside the install.** Point `PCBREVIEW_STACKUPS` at
a directory of your own; it is loaded after this one. A local entry may **not**
reuse a shipped code — it is reported as rejected instead — because an entry is
a transcription of something a fab published, and a local file quietly standing
in for one would break the only guarantee this library makes.

Two of the three entries here state a finished thickness because their fab's
header line does; the third does not, and says `null`.

## Verification is not optional and not implicit

Picking an entry writes `<board>.stackup.yaml` and then immediately runs
`stackup_cli.verify` on it: every target is re-solved from its own stated
geometry and compared to the number the fab published. That is the check that
catches a misread dielectric, and it is shown before any review runs.

The solver reads consistently **low** against JLCPCB — about −1.5%, most
likely unmodelled solder-mask thickness — so a residual near that is a pass and
a residual far from it is the thing to look at. `stackup_cli.diagnose` says
which field would reconcile it.

## Regenerating a board's stackup by hand

The library is a convenience over a command that still works:

```bash
python -m pcbreview.stackup_cli transcribe board.kicad_pcb JLC04161H-7628.rows.txt --out board.stackup.yaml --name JLC04161H-7628 --target ohm=90,kind=diff,layer=L1,bottom_ref=L2,width=0.2703,spacing=0.2032,to_copper=0.2032
```
