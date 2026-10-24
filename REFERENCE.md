# pcbreview — technical reference

How each module works, what it was validated against, and where the numbers
come from. **This is the document to read before changing a threshold or
adding a check.** For what the tool does and how to run it, see
[README.md](README.md); for installing it, [INSTALL.md](INSTALL.md).

**Status: complete and packaged.** 30 checks over three entry points. The
suite is the contract — see README for the current count and what a clean
install prints.

What it is validated against, in order of independence from this code:

| | |
|---|---|
| geometry | **KiCad's own engine**, to machine epsilon, on every board tried |
| connectivity | **KiCad's DRC**, bidirectionally |
| netlist | pin-for-pin against **KiCad's own net assignment** on complete hierarchical designs |
| field solver | analytic limits, two independent closed forms (IPC-2141 stripline, Hammerstad-Jensen microstrip), and **the fab's own published impedance targets** |
| crosstalk | two **exact physical identities**, which hold to solver precision or not at all |
| current capacity | **IPC-2152's measured still-air chart** (Brooks's published fit), and a Laplace solve checked against a rectangle's closed form |
| power paths | closed form for every solver piece (strip, annulus, barrels, point source, transient), **IPC-2152's measured trace temperatures** on 90 cases, and energy balance on a real board |

**Two things this deliberately does not do.** It does not claim EMC pass or
fail — nothing here is a substitute for a chamber. And it does not decide what
a net is *for*: severity within a class is a ranking, not a verdict, and every
finding whose severity would move under intent says which datum it needs.

Two checks ship without ever having fired on real hardware —
`placement.check_crystal_loop` and `schematic_audit.check_missing_pullups`.
Both have synthetic coverage; neither has seen the case it exists for. README
says the same thing, and it is stated rather than hidden.

## Architecture

Two processes, because KiCad's bundled Python (3.9) has `pcbnew` but no
numpy/shapely, and the analysis side needs the opposite.

```
extract.py   under KiCad's Python 3.9  ──►  normalised JSON (mm)
     │  the ONLY file that imports pcbnew
     ▼
model.py     under your Python 3.13 (shapely, numpy, scipy)
stackup.py   Gate 1 — refuses to run without a validated stackup
topology.py  what a net is made of, and how it hangs together
reference.py reference planes, return paths, layer transitions
coupling.py  coupled runs and differential pairs
schematic.py whole-hierarchy netlist   intent.py  net roles, from the circuit
fields.py    2D field solver           physics.py closed forms, DC, timing
routed.py    impedance as routed       crosstalk.py NEXT/FEXT from mutual C, L
parity.py    board vs schematic, pin by pin
panel.py     is this one board or several? break-off panel detection
drc.py       wraps kicad-cli; we do not reimplement DRC
     ▼
audit.py     every check, ranked, with the evidence attached
     │
     ▼  the review layer — what a *report* is, as opposed to a list of findings
pipeline.py  routes a project folder, runs BOTH entry points, applies the policy
severity.py  the user's own `<board>.severity.yaml`; withholds, never destroys
report/      WHAT MUST BE REPORTED, as rules. `generate.py` selects; `render.py`
             prints text, Markdown or JSON; nothing downstream may add or drop
problems.py  the same findings grouped by shared cause — the fix-list shape
facts.py     the nets table, the shortlist and the question queue; writes
             `<board>.netintent.yaml` through the engine's own loader
dossier.py   everything known about one net or part, precomputed
datasheets.py  PDFs -> thermal figures by pattern, each with its page cite
reader.py      the five audits over a reading Claude offers. No model
parts.py     the parts table, and `<board>.thermal.yaml`
stackups.py  the shipped fab stackup library, and applying one to a board
render.py    one picture: KiCad's own plot, dimmed, with a finding's copper and
             evidence drawn on top in board millimetres
pictures.py  the pictures every review draws: the board, one per group, the
             worst findings, the power-path maps. Selection is code, like report/
run.py       the command an agent types: all of the above, once
```

**The split that matters is `audit.py` / `report/`.** The audit measures and
ranks; it does not decide what a person is told. That decision used to be the
agent's, and it was not reproducible — two sessions with identical inputs
reported different things. `report/generate.py` is those rules written down.
A renderer may reorder and a model may write prose above it; neither may add to
the selection or take from it.

Isolating `pcbnew` to one file matters: the SWIG bindings are deprecated and
due for removal in KiCad 11. Replacing `extract.py` with a pure S-expression
reader would leave every analysis untouched.

## Usage

`pcbreview.run` is the entry point. Everything else is the drill-down layer.

```bash
python3 -m pcbreview.run project/            # both audits, once, the report and its pictures
python3 -m pcbreview.run project/ --pictures review/img   # …pictures kept where you say
python3 -m pcbreview.run project/ --no-pictures           # …and none drawn
python3 -m pcbreview.stackups project/       # the fab stackup library
python3 -m pcbreview.facts project/          # nets, shortlist, questions; --declare
python3 -m pcbreview.parts project/          # datasheets read; --pages, --read, --write
```

```python
from pcbreview import pipeline
from pcbreview.report import render

result = pipeline.review_path("project/")    # discover() then review()
print(render.text(result.report))            # the same text `run` prints
render.markdown(result.report)               # …as Markdown
render.to_json(result.report)                # …as canonical JSON
result.report.severe                         # rule 1: every critical and major
result.report.could_not_run                  # rule 3: every blind check
result.layout.findings                       # the audit's own list, unfiltered
```

The audit alone, when the question is narrower than a review:

```bash
python3 -m pcbreview.audit board.kicad_pcb stackup.yaml [board.kicad_sch]
```

```python
from pcbreview.audit import audit_files

report = audit_files("board.kicad_pcb", "stackup.yaml")
print(report.report())                  # ranked findings with their evidence
report.of_severity("major")             # triage
report.by_check("crosstalk")            # one analysis at a time
[f.summary() for f in report.findings]  # machine-readable
```

Every finding is a number, the geometry it came from, and why it was believed:

```
[MAJOR   ] /UART2 RX — return current detours around 28.29 mm of its path
             coverage 37.2% of 45.04 mm (4 significant gap(s))
             worst gap 11.98 mm, off_plane, return detour 11.98 mm
             references GND (24.3 mm), +3.3V (18.1 mm)
             at (201.23, 89.97)
             basis: trace centreline intersected with the poured plane beneath
                    it; antipads credited as covered because the plane is
                    continuous around them, and each gap's cost is the shorter
                    way round its boundary, not its length
             intent: this net's edge rate — the loop area a detour encloses
                     only radiates at frequencies the signal contains
```

The lower layers, for drilling in:

```bash
python3 -m pcbreview.model "path/to/board.kicad_pcb"
```

```python
from pcbreview.model import Board
from pcbreview.stackup import load_and_validate

board = Board.from_kicad("board.kicad_pcb")
stackup, warnings = load_and_validate("fixtures/four-layer-1.stackup.yaml",
                                      board.copper_layers)
board.bind_stackup(stackup)

board.tracks_on_net("USB_D+")           # exact geometry, arcs included
board.zone_fill("In1.Cu", net="GND")    # poured copper, islands and cutouts
board.copper_on_layer("F.Cu")           # tracks + pads + via lands + fill
stackup.dielectric_between("L1", "L2")  # (thickness_mm, effective epsilon_r)
```

## The object model — a cheat-sheet

Two independent outside sessions lost most of their failed tool calls to this,
guessing attribute names and reading tracebacks. Every field below is real; the
crossed-out ones are the guesses that cost them.

**Getting in.** `Board.from_kicad(path)` is the only loader. There is no
`Board.from_file`. Never `import pcbreview.extract` — it imports `pcbnew` at
module scope and fails under every interpreter you will run.

```python
from pcbreview.audit import audit_files      # NOT audit.run — no such thing
report = audit_files("board.kicad_pcb", "stackup.yaml", "board.kicad_sch")
```

| object | fields you will want | not |
|---|---|---|
| `Board` | `.tracks`, `.vias`, `.pads`, `.zones`, `.footprints`, `.nets`, `.copper_layers`, `.thickness_mm`, `.stackup`, `.outline` (a `MultiPolygon` — `.outline.area` is the board area) | ~~`.outline_area`~~ |
| | `.net(code_or_name)`, `.tracks_on_net(k)`, `.vias_on_net(k)`, `.pads_on_net(k)`, `.zones_on_net(k)` | |
| | `.nets` is `dict[int, Net]` — iterating gives **ints**. Use `.nets.values()` | ~~`for n in board.nets: n.code`~~ |
| `Track` | `.net` (int), `.net_name`, `.layer`, `.width_mm`, `.kind`, `.start`, `.end`, `.length_mm`, `.centreline`, `.copper` | |
| `Via` | `.net` (int), `.net_name`, `.pos` (**a plain tuple** — `v.pos[0]`, never `v.pos.x`), `.drill_mm`, `.layers`, `.width_on(layer)`, `.pad_on(layer)` | ~~`.pos.x`~~ |
| `Pad` | `.net`, `.net_name`, **`.ref`** (the footprint it belongs to), `.number`, **`.designator`** (`"C1.1"`), `.pos`, **`.copper`** (a shapely `Polygon`), `.copper_layers`, `.is_smd`, `.is_plated_hole` | ~~`.footprint`~~, ~~`.footprint_ref`~~, ~~`.reference`~~, ~~`.key`~~, ~~`.polygon`~~ |
| `Zone` | `.net`, `.net_name`, `.layers` (**list**), `.outline`, `.fill_on(layer)`, `.is_rule_area` | ~~`.layer`~~, ~~`._fills`~~, ~~`.polygon`~~, ~~`.area_mm2`~~ |
| `Track` (again) | `.width_mm`, not `.width` | ~~`.width`~~ |
| `Footprint` (again) | `.pos`, not `.position` or `.x`/`.y`; `.ref`, not `.reference` | ~~`.position`~~, ~~`.reference`~~ |
| `Via` (again) | `.width_on(layer)` / `.width_by_layer`, not a single diameter | ~~`.diameter_mm`~~ |
| `Footprint` | `.ref`, `.value`, `.lib_id`, `.pos`, `.pads` | ~~`.x`, `.y`~~ |
| `Finding` | `.check`, `.title` (str), `.severity`, `.score`, `.nets`, `.layers`, `.detail` (**`list[str]`**), `.basis`, `.assumption`, `.intent_needed`, `.measurement`, `.summary()` (**a method**) | ~~`.message`~~ |
| `AuditReport` | `.findings`, `.checks`, `.assumptions`, `.summary()`, `.report(grouped=True)`, `.net_classes`, `.net_class_map` | |
| `CheckResult` | `.name`, `.ran`, `.findings` (a **count**), `.note`, `.skipped_because` | ~~`.coverage`~~ |
| `Schematic` | `.components`, `.nets`, `.nets_by_name`, `.sheets`, `.component(ref)`, `.field_survey()` | |
| `PinRef` (a schematic pin) | **`.reference`** (the part), `.number`, `.name`, `.type`, `.sheet`, **`.key`** (`"U13.15"`) | ~~`.ref`~~, ~~`.component`~~, ~~`.designator`~~ |
| `Component` | `.reference`, `.value`, `.footprint`, `.lib_id`, `.pins`, `.description`, `.dnp`, `.mpn`, `.supplier_part` | |

**A pad and a schematic pin both know what part they belong to, and both were
missed.** An outside session reported *"nothing that names the footprint it
belongs to"* for `Pad` and got `'?'` for every schematic pin, and printed a
power path as `?.1, ?.2, ?.1` — reconstructing the topology from pin *names*
instead. The attributes were there the whole time: it is **`pad.ref`** and
**`pin.reference`**. Both also give the `"U13.15"` form findings print, under
**different names** — `pad.designator` on the board side, `pin.key` on the
schematic side. `getattr(p, "ref", "?")` on a schematic pin silently returns
the default, so the failure is invisible.

**And this table itself was wrong**, which is worth knowing before trusting
it: the `Pad` row documented a `.polygon` that has never existed — the pad
geometry is **`.copper`**. A cheat-sheet written to stop attribute-guessing
carried an invented attribute for months. `tests/test_model.py` now asserts
every attribute named in this table against the real classes, so the next one
fails a test instead of a review.

If you want that path in the user's own designators without writing any of
this, `python -m pcbreview.audit <board> <stackup> --net NET` prints it —
widths, layers, vias, poured area, pads by designator, and every finding
naming the net.

**`Zone.outline` and `Board.outline` are already shapely geometries.**
`Polygon(zone.outline)` is the natural thing to write and raises
`'MultiPolygon' object is not iterable`, which reads like corrupt board data
rather than "this is already a geometry". Use them directly.

**`Zone.area_mm2` raises on purpose.** A zone's copper is per layer, so there
is no single number; the property exists only to say so. A reviewer wrapped it
in a bare `try/except` and got a clean table of `0.0 mm²` for every pour on
every layer, which reads as "this board has no copper pours".

**Every optional input to `audit_files` has three states, not two.** `None`
means *find it beside the board*, a path means *use this one*, and **`False`
means "there is none, and do not look"**. The third is what pins a baseline to
its inputs: both manifests are auto-discovered, so a review session that leaves
a `<board>.thermal.yaml` behind silently changes every later run — one baseline
here moved 96 → 122 findings with nothing in the engine responsible. On the
six-layer board, auto-discovery gives 45 findings and full suppression gives
24. `tests/dump_audit.py` takes the literal `none` in those positions, and the
suppression is reported in `report.assumptions` rather than being silent.

**Parsing a schematic** is `from pcbreview.schematic import netlist;
netlist(path)`. There is no `Schematic.from_kicad`.

`Zone.fill_on(layer)` returns a shapely `MultiPolygon` — the zone's actual
poured copper, holes and islands included. It is the number most reviews end up
wanting (*"the F.Cu GND zone is 29.5 mm² on an 861 mm² board"* was one board's
entire headline). Do not reach for `zone._fills`: that is raw JSON, and feeding
it to `unary_union` gives `One of the arguments is of incorrect type`.

`stackup.load_and_validate(path, board.copper_layers)` takes the **layer-name
list**, not the board. A `Board` raises `TypeError: object of type 'Board' has
no len()` from inside `validate`, which says nothing useful.

Grouping findings by shared cause:

```python
from pcbreview.grouping import report as grouped
print(grouped(report.findings, severities=("critical", "major", "minor")))
```

## Gate 1: the stackup

Physics results are only as good as the layer stack, and KiCad boards routinely
carry a placeholder stackup (10 um dielectrics, 0.19 mm total) that is silently
wrong rather than obviously wrong. `load_and_validate` therefore raises
`StackupGateError` rather than guessing.

Paste a JLCPCB impedance-calculator table straight in:

```python
from pcbreview.stackup import parse_jlc_table
stackup = parse_jlc_table(open("jlc_table.txt").read())
```

Checks: placeholder dielectrics (STK003), implausible total thickness (STK007),
sum-vs-finished-thickness mismatch (STK008), layer count and order against the
board (STK009–011), epsilon_r plausibility (STK005). `compare_to_board` warns
separately where the supplied stackup diverges from the board file's own —
plausibility is not correctness.

Fixtures carry the fab's impedance targets, which become validation data for
the field solver in Phase 4.

## Validation

`tests/kicad_truth.py` runs under KiCad's Python and dumps KiCad's own geometry
(zone fill areas, track lengths, pad areas, board outline). The test suite
rebuilds all of it through shapely and compares.

```bash
python3 tests/validate_all_boards.py     # every board under the parent dir
python3 -m pytest tests/ -q
```

Current agreement across all 8 boards:

| quantity | worst relative deviation |
|---|---|
| board outline area | 8e-16 |
| zone fill area (121 filled zones) | 2e-14 |
| pad copper area (3478 pads) | 8e-14 |
| track length by net | 6.6e-6 |

Areas agree to machine epsilon. The track-length residual is KiCad's own 1 nm
rounding of arc endpoints — arc length is computed analytically (`r·θ`) rather
than from the tessellated polyline, since chords systematically understate a
curve and length feeds skew budgets.

## Connectivity (`topology.py`)

Connectivity is derived from geometry — union-find over copper polygons that
intersect — not from KiCad's netlist. KiCad says which pads *should* be joined;
this works out whether copper actually does it.

Validated against `kicad-cli` DRC in both directions on every board: a net KiCad
reports broken must split for us, and one it does not report must not. **Zero
disagreements across 1052 nets on 7 boards.**

Two bugs that check caught, both of which would have produced confidently wrong
analysis:

- **Zone islands were being bridged.** A zone's fill on one layer is many
  disjoint polygons (one GND zone had 20). Adding the fill as a single item
  joined a via landing on island 8 to a track on island 7. Each island is now
  its own item.
- **Stale zone fills.** Boards are routinely saved without refilling; one board
  here had 0.00 mm² of saved fill against 7958 mm² after a refill. Extraction
  now refills in memory (never writing the board) and flags staleness.

## Return paths (`reference.py`)

For each track segment, the nearest copper layer above and below is resolved
through the stackup, and the trace's own footprint is intersected with the plane
copper there. Requires a bound stackup; raises without one.

**Significance is return-current detour, not gap length.** A trace crossing a
0.6 mm via antipad has no copper beneath it, but the plane is continuous all
around, so the return current detours by ~0.5 mm — irrelevant. A trace crossing
a plane split has nowhere to go. Counting both as "gaps" is why naive tools
drown users in false positives.

The detour is the shorter way round the hole boundary between where the trace
enters and leaves, validated against closed forms:

| case | computed | analytic |
|---|---|---|
| 0.9 mm antipad crossed centrally | 0.514 mm | π·d/2 − d = 0.514 |
| 20 mm void crossed centrally | 11.416 mm | π·r − 2r = 11.416 |
| 20 mm void, edge clip | 0.010 mm | ~0 |

Half the hole perimeter — the obvious wrong metric — charges 13–17 mm in via
fanout regions where antipads merge into one large void, making healthy boards
look catastrophic.

On the four-layer board this takes raw coverage of 55% on one I2C line (eleven antipad
crossings) to an effective 89%, and leaves 45 of 64 signal nets fully
referenced.

Multi-plane resolution works on the six-layer stack: `In3.Cu/+5V` and
`In2.Cu/VBAT` show up as reference planes for inner-layer segments that sit
nearer a power plane than either ground — exactly the sig/gnd/sig/pwr/gnd/sig
case. `find_transitions` classifies each layer change as `same_reference`,
`ground_to_ground` (needs a stitching via), `ground_to_power` (needs a
decoupling cap), or `unreferenced`, and reports the nearest candidate stitch.

## Coupling (`coupling.py`)

One primitive serves both differential pairs and crosstalk: how much of net A
runs close to net B, and how close.

Gaps are **edge to edge**, measured from the victim's centreline to the
aggressor's actual copper polygon less the victim's half-width — exact even
where either trace changes width.

Proximity alone is not coupling. A perpendicular crossing sits within 0.5 mm of
another trace for ~1.4 mm of travel, which survives any run-length filter. So
parallelism is tested from the gap's rate of change: for traces meeting at
angle θ, moving `ds` along one changes separation by `ds·sin(θ)`, so
`|d(gap)/ds|` *is* `sin(θ)` — 0 for parallel, 1 for a crossing. No extra
geometry queries; the gap array is already in hand.

```python
from pcbreview.coupling import analyse_pair, neighbours_of

pair = analyse_pair(board, "USB_D+", "USB_D-")
pair.coupled_fraction     # 0.666 — your isDifferentialPair() primitive
pair.skew_mm              # 0.016
pair.gap_consistency_mm   # 0.0

neighbours_of(board, "USB_D+")   # ranked crosstalk shortlist
```

Validated against the fab's own spec — both boards were routed to a
JLCPCB 90 Ω differential rule, and the measurement recovers it:

| board | coupled | mean gap | spread | skew |
|---|---|---|---|---|
| the four-layer board | 66.6% | **0.2032 mm** | 0.0 | 0.016 mm |
| the six-layer board | 20.6% | 0.2271 mm | 0.075 mm | 1.138 mm |

The four-layer board's USB pair holds JLC's specified 0.2032 mm spacing *exactly*, with
zero variation, over 7.9 mm. The six-layer board's reaches spec for only 1.9 mm of 9 mm and
wanders to 0.8 mm elsewhere — a real difference between the two layouts, not an
artefact.

## Schematic (`schematic.py`)

Severity is meaningless without intent, and intent lives in the schematic. This
resolves a whole hierarchical design — not one sheet at a time — into nets.

```python
from pcbreview.schematic import netlist

sch = netlist("board.kicad_sch")
sch.nets_by_name["/UART2 RX"].pins     # every pin, across every sheet
sch.net_of("U18", "43")                # what a given pin is on
sch.sheets                             # every *placement*, with its own designators
```

Three things make a hierarchy more than a loop over files:

- **Designators are per placement.** Four a repeated sheets share one file; each
  symbol carries four designators keyed by sheet-instance path, and the
  `Reference` property holds only whichever was edited last.
- **Net identity is scoped.** A local label means a different net in each
  placement; a global label or power symbol means the same net design-wide.
- **Nets cross the boundary** between a child's hierarchical labels and the
  matching pins on the parent's sheet symbol, and neither file says so.

Measured against KiCad's own net assignment, read back from the fabricated
boards:

| board | sheets | pins | nets | exact | split | merged |
|---|---|---|---|---|---|---|
| the six-layer board | 15 | 876 | 229 | 229 | 0 | 0 |
| the four-layer board | 3 | 352 | 97 | 97 | 0 | 0 |

Names are scored separately from the partition, because they are not equally
important. Every net a *designer* named gets KiCad's exact name, sheet-path
prefix included — 99/99 and 55/55. Names KiCad generated for nets nobody named
(`Net-(U18-V_BCKP)`) are best-effort: the tie-break among equal-priority pins
lives inside KiCad's connection graph, and none of the obvious candidates
reproduces it (over the six-layer board's 57 multi-pin unnamed nets: lowest pin UUID 21,
highest 30, alphabetical 24). Check `net.source == "auto"` before relying on
one.

The two pin-level disagreements with the boards are real design facts rather
than parse failures, and the tests assert them by name: the processor module footprint
has 47 pads for 48 symbol pins (`U1.15`, its 3V3 output, is not broken out),
and the four-layer board's buck-boost converter thermal stitching and
card-socket shield tabs are
footprint pads with no symbol pin, netted to GND in the board editor.

```bash
python3 tests/score_hierarchy.py board.kicad_pcb top.kicad_sch
```

## Intent (`intent.py`)

Geometry cannot rank findings. A mediocre return path is a defect on a
1.575 GHz GPS feed and irrelevant on I2C, and the layout does not say which is
which. Two conclusions used to be drawn from net *names*; both now come from
the circuit.

```python
from pcbreview.intent import infer

intent = infer(schematic, board)     # board net names, matched by pads
intent.ground_nets                   # {'GND'}
intent.role_of("GND").evidence       # 'common node of decoupling for 7 supplies (...)'
intent.pairs                         # includes CANH/CANL
```

**Ground, from decoupling topology.** Which nets are rails at all is exact —
a net driven by a power symbol. Which rail is *ground* is the node every
supply's decoupling capacitors return to: the rail that is the common side of
two or more different supplies' capacitors. On these boards the separation is
total (GND bridges 7 and 5 other rails; every other rail bridges 1 or 0) and no
name is consulted to reach it. A test scrambles every net name and checks the
answer does not move.

The symbol's `lib_id` looked like a better signal and is not — on these boards
`power:+3.3V` is placed for `+5V`, `VBAT`, `VBUS` and an auxiliary rail, the
symbol reused
with the value edited.

**Differential pairs, from pin names.** A pair is two nets arriving at one
device on pins the *device* calls two halves of a signal. That is a property of
the manufacturer's symbol, so it survives whatever the designer named the nets:

| board | net-name heuristic | from pins |
|---|---|---|
| the four-layer board | USB only | USB, plus the processor module's USB host pins |
| the six-layer board | USB only | USB, **a second bus**, the processor module's host pins, a current-sense amplifier's inputs |

The six-layer board's bus transceiver is the case the old heuristic could not
reach: the nets are
unnamed — KiCad calls them `Net-(U24-CANH)`/`Net-(U24-CANL)` — but U24's pins
are `CANH` and `CANL`. It measures 12.1% coupled with 5.7 mm of skew, i.e.
routed as two single-ended traces. Rails are excluded, which removes a
battery's `+`/`-` pins without a special case for batteries.

Every result carries its evidence and a confidence. This is the *proposer* half
of Gate 2: deterministic code proposes, Claude adjudicates, deterministic code
audits.

## Field solver (`fields.py`)

The differentiator — nothing in the prior art has one. Laplace's equation
solved on the cross-section with variable permittivity, twice: with the real
dielectrics for **C**, and with vacuum for **C_air**, then
**L = μ₀ε₀·C_air⁻¹** (Paul's generalised capacitance-matrix method). The
magnetic problem is never solved, and no per-conductor inductance is ever
quoted.

Finite-volume box integration on a graded rectilinear mesh, with grid lines
pinned to every conductor edge and dielectric interface so no cell face
straddles a material boundary in the direction of its flux. Charges come from
the *reaction* on the Dirichlet rows — assemble over every node, solve the free
ones, then `Q = (Aφ)` per conductor — which is exactly discretely
charge-conserving.

```python
from pcbreview.fields import differential_z, microstrip_z0, JLC_SOLDER_MASK

microstrip_z0(0.3424, 0.2104, 0.035, 4.6, gap_to_pour_mm=0.508).z0_ohm
differential_z(0.2672, 0.2032, 0.2104, 0.035, 4.6,
               gap_to_pour_mm=0.2032, mask=JLC_SOLDER_MASK).z_differential_ohm
```

### Why it exists

scikit-rf's CPW model does not converge to microstrip as the gap widens, so it
cannot be used on these boards:

| gap to pour | this solver | scikit-rf CPW |
|---|---|---|
| 0.508 mm | 50.6 Ω | 55.1 Ω |
| 5 mm (24× the dielectric height) | **51.1 Ω** | **60.4 Ω** |
| plain microstrip, no pour | 51.1 Ω | — |

At 5 mm the pour cannot physically matter and the answer must be the microstrip
one. Trusting the closed form would report the user's GPS traces as 20%
mismatched when they are not.

### Validation, in order of independence

| test | result |
|---|---|
| parallel-plate limit (wide trace) | within 5% of `ε₀ε_r·W/h` |
| stripline ε_eff (must be exactly ε_r) | agrees to 1e-6 |
| `sqrt(L/C)` vs Z₀ from capacitances | agrees to 1e-9 |
| mesh refinement, 3× node count | 0.5% spread |
| outer boundary at 10× / 20× / 40× h | 1% spread |
| IPC-2141 symmetric stripline, 3 geometries | within 2% |
| Hammerstad-Jensen microstrip | within 1% |
| **JLCPCB's four impedance targets** | **−1.4% to −1.7%** |

The fab residual is a *uniform* offset across two boards, two stackups, two
dielectric constants and both target kinds — the signature of one unmodelled
parameter rather than of a solver error. It is not tuned out.

### Solder mask is not optional

Mask is applied conformally and ends up thinner over copper than over the bare
substrate beside it, so it is three horizontal bands, not one block:

| model | error on the four fab targets |
|---|---|
| no mask | +1.3% … +4.8% |
| one block filling the gap | −3.5% … −4.9% |
| bands, at published thicknesses | **−1.4% … −1.7%** |

Both thicknesses are used as fabs publish them (20 µm over substrate, 10 µm
over copper, ε_r 3.8) rather than fitted. Mask is opt-in via `mask=`, so a
comparison against a bare-copper closed form is never silently given one.

## Routed impedance (`routed.py`)

The field solver answers "what is the impedance of *this* cross-section". The
fab's target answers "what is the impedance of the cross-section you were
*supposed* to route". This measures the one that exists, 25 µm at a time, and
solves it.

```python
from pcbreview.intent import infer
from pcbreview.routed import audit_against_targets

intent = infer(schematic, board)
for result in audit_against_targets(board, intent.ground_nets,
                                    pairs=[(p.positive, p.negative)
                                           for p in intent.pairs]):
    print(result.report())
```

```
/USB_D+ / /USB_D- (differential)
  designed for 90 ohm at 0.2032 mm to pour, solved 87.1 ohm
  routed 9.57 mm, 87.1-107.3 ohm, mean 91.6
  +5.2% against the same solver's answer for the intended cross-section
  1.36 mm of 9.57 more than 10% away from it
      5.11 mm on F.Cu: w=0.2750, s=0.2000, clearance 1.2500 -> 89.7 ohm
      0.60 mm on F.Cu: w=0.2750, s=0.2000, clearance 0.2000 -> 87.1 ohm
  ! 2.42 mm routed too far apart to be a pair; not included in the impedance
```

No DRC can produce that — every clearance passes. No closed form can either,
since the coplanar models break down at exactly these gaps.

Three things keep it honest:

- **Clearance is measured to *ground* copper**, not to any copper. A signal
  trace running alongside changes the impedance too, but that is coupling and
  belongs to `coupling.py`, not to the coplanar return.
- **Deviation is measured against the solver's own answer for the intended
  cross-section**, not against the fab's number. The solver reads ~1.5% below
  the fab; comparing to the raw target would charge the layout for a modelling
  offset. Against the target the four-layer board's GPS feed looks 2.6% low; against the
  intended geometry it is within 0.5%, which is the true statement.
- **Where a pair's halves separate they are not a pair there**, so that length
  is reported as uncoupled rather than solved as a structure that does not
  exist.

Geometry is quantised to 25 µm and one solve is cached per distinct
cross-section, so a 160-sample trace costs six solves, not 160.

Results on the two fabricated boards:

| net | routed | mean | vs intended |
|---|---|---|---|
| the four-layer board's receiver RF feed (1.575 GHz) | 4.00 mm | 48.7 Ω | **+0.4%** |
| the four-layer board USB pair | 9.57 mm | 91.6 Ω | +5.2% |
| the six-layer board USB pair | 3.30 mm coupled of 9.35 | 92.9 Ω | +3.5% |

The GPS feed — the one net on the four-layer board where impedance genuinely matters — is
routed to its intended impedance within half a percent.

**Which layer is the reference is measured, not assumed.** The adjacent copper
layer often is not one: on the six-layer board a trace on In2.Cu has another *signal* layer
0.109 mm below it and the ground plane 0.55 mm above, and modelling it against
the near one is most of a factor of two out. `layer_context` searches outward
for the nearest layer that actually pours copper over the net, and carries the
coverage it found alongside the answer. Measured over both boards, real planes
cover 79–100% of a net's footprint and signal layers 0–32%, so the discriminator
has a wide margin.

## Crosstalk (`crosstalk.py`)

The mutual terms of the capacitance and inductance matrices, taken along the
geometry as routed. From the standard weak-coupling result (Paul; Johnson &
Graham ch. 5):

```
k_b = (1/4)(Cm/Cs + Lm/Ls)      backward, toward the victim's near end
k_f = (1/2)(Cm/Cs - Lm/Ls)      forward, toward its far end
```

The two behave differently, and separating them is the point:

- **Backward crosstalk does not accumulate.** The near-end waveform is a scan of
  `k_b` along the line — noise arriving at *t* was generated at `z = v t / 2` —
  smoothed by a window of `v t_r / 2`. A longer coupled run does not couple
  harder, it couples for longer. Letting `t_r → 0` gives an upper bound over
  every edge rate, which is the one number here that assumes nothing about the
  signal.
- **Forward crosstalk does accumulate**, coherently, so it is reported as an
  integral with units of time; FEXT as a fraction of the swing is that divided
  by the rise time.

```
/BUS6 <- /BUS5
  36.12 mm coupled of 51.02 (71%) on In2.Cu, closest 0.300 mm
  NEXT 10.92% saturated (upper bound over all edge rates), 3.941
       coefficient-mm integrated; FEXT +0.00 ps of dV/dt
    at 500 ps edges: NEXT 10.9%, FEXT +0.0%
    at 2000 ps edges: NEXT 2.8% (below saturation), FEXT +0.0%
```

That `FEXT +0.00` is not a rounding artefact. That bus is stripline between
two ground planes, so the medium is homogeneous, the electric and magnetic
coupling cancel exactly, and far-end crosstalk vanishes — the textbook result,
falling out of routed geometry.

**Reported to ranking accuracy, not spec-compliance accuracy**, and the output
says so. Terminations are assumed matched at both ends and real CMOS is not (a
high-impedance receiver roughly doubles the near-end figure); the edge rate is
an assumption until the intent manifest supplies one; only same-layer coupling
is measured.

### Validation

Two identities that hold *exactly* rather than a comparison against a rule of
thumb, because every published crosstalk formula differs by factors of two and
any of them returns a believable percentage:

| check | result |
|---|---|
| stripline (homogeneous) has no forward crosstalk | `k_f` = 2e-16 |
| `2 k_b` vs the coupled-line factor `(Ze−Zo)/(Ze+Zo)`, homogeneous | agrees to 1e-9 |
| the same, inhomogeneous (microstrip) | within 0.4% |
| mutual C vs the parallel-plate limit `ε₀t/s` | 1.06–1.11×, from above |
| coupling as the traces separate / touch | → 0.0003, → 0.93 |
| coefficients under 3.3× mesh refinement | 0.55% spread |

Johnson & Graham's `1/(1 + (D/H)²)` rule of thumb was checked and tracks this
solver to within 25% in the middle of its band — but it has no width term at
all, and two geometries with the same D/H and different w/h disagree with it by
a factor of 1.6 in opposite directions. That is evidence about the rule of
thumb, not about the solver, so nothing asserts on it.

### Broadside coupling — traces stacked on adjacent layers

Side-by-side coupling is set by a clearance the designer chose. Broadside
coupling is set by the prepreg the fab chose, and the only thing the layout
varies is how far apart the two centrelines sit laterally. Different quantity,
different remedy, so it is a separate survey and a separate check:

```python
crosstalk.analyse_broadside(board, "/UART1 TX", "/BUS6")
crosstalk.broadside_survey(board, ground_nets)
```

This matters more than it sounds on a six-layer board. The six-layer board's In2 and In3 are
**adjacent signal layers 0.109 mm apart with no plane between them**:

| geometry, 0.2 mm traces | `k_b` |
|---|---|
| stacked, zero lateral offset | **0.316** |
| side by side at the board's own 0.2 mm design clearance | 0.156 |

Stacked couples about twice as hard as the tightest legal side-by-side spacing.
On the six-layer board, `/UART1 TX` runs **13.17 mm directly over `/BUS6` at 0.100 mm
lateral offset**, which the same-layer survey cannot see at all because on its
own layer that net has no close neighbour. The four-layer board, whose inner layers are
poured, produces nothing above `info`.

Held to the same identities:

| check | result |
|---|---|
| buried between planes: forward crosstalk must vanish | `k_f` = 2e-15 |
| `2 k_b` vs `(Ze−Zo)/(Ze+Zo)`, three offsets | agrees to 1e-15 |
| reciprocity with unequal widths, victim/aggressor swapped | 3e-15 |
| far-offset limit vs the victim's own stripline `Z₀` | 3.7e-4 |
| coefficients under 3.3× mesh refinement | 0.11% spread |

## Break-off panels (`panel.py`)

A board section joined by mouse-bites or a routed slot, snapped off after
assembly, looks to every continuity check like a catastrophe: nets cross it
unrouted and no plane covers the channel. On the six-layer board that one feature
accounted for a large share of the report and a reviewer dismissed the same
findings every run.

The code does not decide that a divided board is panelised — it cannot, and a
two-part board could equally be an error. It states the geometry exactly:

```python
division = panel.partition(board)
division.crossing_nets()          # nets with copper on both sides
```

A copper region counts above **both** 5% of the board's copper and 50 mm². Over
all eight boards the populations do not overlap — the six-layer board's panel is 15.98%, and
every other board's second region is a mounting-hole annulus at ≤2.31%.

The check that this is more than a tuned threshold: **twelve nets have copper on
both sides of the six-layer board's channel, and KiCad's own DRC reports exactly twelve
unconnected items on that board.** Same twelve, from connected components of
copper rather than from the ratsnest. The channel measures 1.70 mm at
(197.4, 109.7) and the detached region is x 198.2–222.2 — the right-hand GPS
section.

The audit reports the partition once, at `info`, and moves `return_path`,
`reference_split`, `layer_transition` and `differential_pair` findings on
crossing nets to `info` too, keeping every number and recording what the
severity was. Coupling and impedance are *not* demoted: the copper that exists
still couples and still has a cross-section. The six-layer board went from 25 major / 32 minor
to **20 / 23**.

## Board/schematic parity (`parity.py`)

KiCad's ERC sees one side and its DRC the other; nothing checks that the two
describe the same circuit, because KiCad built one from the other. They diverge
when a pad is netted by hand in the board editor, or when a symbol carries a pin
its footprint has nowhere to put.

Both fabricated boards have examples and all are intentional, which is exactly
why the check separates the ones that change the circuit from the ones that
change only the drawing:

| board | asymmetries | circuit-changing |
|---|---|---|
| the four-layer board | 13 | 9 — buck-boost converter thermal stitching and card-socket shield tabs, netted to GND on the board with no symbol pin behind them |
| the six-layer board | 7 | 0 — spare module pads with no net, and one processor-module 3V3 output the footprint does not break out and the schematic does not connect either |

## The audit (`audit.py`)

The entry point, and the reason everything above it exists. One call runs every
check the board's data supports and returns findings in priority order. Ten
checks: `board_state`, `panelisation`, `impedance`, `return_path` (with
`reference_split`), `layer_transition`, `differential_pair`, `crosstalk`,
`broadside`, `conductor_neck`, `parity`. A full audit is 40–50 s per board.

**Severity is provisional, and that is the design claim rather than an
apology.** A 40 mm run with a mediocre return path is a defect on a 1.575 GHz
GPS feed and irrelevant on an I2C line, and nothing in the layout says which is
which. So every check assigns severity from a stated threshold on a computed
quantity, every finding whose severity would move under intent carries
`intent_needed` naming the datum, and `score` is "how many times over its own
threshold this is" — the only cross-check ordering that means anything before
the manifest exists.

Two of those thresholds are *derived* rather than chosen:

- the stitching-via and bridging-capacitor distance is a twentieth of the knee
  wavelength, so it follows the audit's edge rate — 14 mm at 1 ns, 4 mm at
  300 ps. Running the six-layer board at 300 ps rather than 5 ns changes which transitions are
  findings, which is the point.
- a narrow stretch is a `conductor_neck` only if the net's copper graph, rebuilt
  without it, strands pads. Nearly every poured rail has a thin track somewhere
  and most are shorted out by the pour a millimetre later; the cut test is what
  separates a bottleneck from noise.

What it finds on two working boards:

| board | major | minor | notable |
|---|---|---|---|
| the four-layer board | 2 | 6 | a via onto B.Cu with no plane covering it; a serial receive line crossing between a ground and a power reference |
| the six-layer board | 20 | 23 | `/UART1 TX` stacked 13 mm over `/BUS6` on the layer beside it; a bus pair at 12% coupled with 5.7 mm of skew; four ground↔power transitions with no bridging capacitor within 14 mm |

The six-layer board's counts came down from 25/32 when the break-off panel started being
reported as itself rather than as fourteen broken return paths.

The GPS feed on the four-layer board produces nothing above `info`, which is the check
that matters: a tool that cried wolf on the one net where impedance genuinely
does would not be worth running.

## Power paths (`dcflow.py`, `boardheat.py`, `ampacity.py`, `powerpaths.py`)

`current_capacity` asks whether one feed of a net is worse than its siblings,
and needs no current. `power_path` asks the absolute questions once someone
has said what a path carries, in `<board>.powerpaths.yaml`. Four modules:

| module | what it is |
|---|---|
| `powerpaths.py` | the manifest: paths (net, source pads, sinks with currents, allowed rise, pulse length), scenarios, environment, ambient, mounts. Refuses unknown keys, unit-suffixed numbers and dangling scenario names |
| `dcflow.py` | 2.5D DC current flow over one net's copper on every layer, joined by plated barrels |
| `boardheat.py` | board temperature: one conducting sheet per copper layer, laminate and barrels between, both faces cooled; steady and transient |
| `ampacity.py` | the check (`power_path`), the CLI and the maps |

### The current-flow model

Each layer's copper of the net — `Board.copper_on_layer`, so pours, tracks,
pads and via lands — is rasterised onto one shared grid (default 0.1 mm,
coarsened only above 1.5 M cells, and the pitch is always reported). Face
conductance is ``t / rho`` per square, harmonic-mean across a cell pair, with
``rho`` at 20 C or at a supplied temperature (``alpha = 0.00393``).

**Barrels** join consecutive layers they pass, each segment
``sigma * pi * d * plating / L`` with ``L`` the stackup distance between the
layers' mid-planes; plating defaults to `current.VIA_PLATING_UM` (25 um) and is
stated on every finding. Plated holes are barrels too. **Terminals and via
lands are merged into single nodes by Galerkin projection**, the same way
`current.plane_resistance` merges an electrode; a through-hole terminal is
merged across every layer, because its pin and solder join them far better
than its plating. Copper not joined to the source is dropped before the
solve; a sink on another island is refused by name, as is a pair of terminals
sharing copper.

### The heat model

On a 0.25 mm grid over the outline, one sheet per copper layer. Lateral
conductance per cell is ``k_cu * t * f + k_xy * (half the laminate above and
below)``, with the copper fraction ``f`` supersampled 4 x 4 so a 0.2 mm track
conducts. Neighbouring sheets are joined through the laminate (``k_z``) and,
at each barrel's cell, through its plating. The outer sheets lose heat at a
linear surface coefficient per face; optional mounts add a stated conductance
to ambient at a footprint.

| constant | value | source |
|---|---|---|
| `K_COPPER_W_MK` | 385 | handbook |
| `K_LAMINATE_XY_W_MK` / `_Z_` | 0.49 / 0.3 | in-plane: **IPC-2152's measured value** for its FR-4 test vehicles (0.0124 W/in-C). The datasheet 0.8 it replaced made every trace about a third cooler than IPC-2152 measures. Through-plane: datasheet 0.25-0.40; a single trace's rise moves 2% between 0.3 and 0.49 |
| `C_COPPER_J_M3K` / `C_LAMINATE_` | 8960 x 385 / 1850 x 1100 | handbook |
| `ENVIRONMENTS` | enclosed 5, still_air 8.1, forced_air 30 W/m^2K per face | still_air **fitted to IPC-2152's baseline** with the laminate above (see Validation); enclosed a stated worse case, 1.09-1.63x IPC-2152's rise; forced_air ~1-2 m/s, textbook |

**Temperatures are only ever reported as a bracket over `ENVIRONMENTS`.**
With an environment declared, every verdict is taken there. Without one, a
conductor's **own rise** is judged in `still_air` (`RISE_VERDICT_ENVIRONMENT`)
— where IPC-2152 defines a rise and the +10 C default comes from — and the
**board's temperature** against absolute limits in `enclosed`
(`DEFAULT_VERDICT_ENVIRONMENT`), the worst, under the standing preference for
a false positive over a silent pass. Each finding asks which it really is.

Superposition is used deliberately and is exact because the model is linear:
one factorisation per environment serves every path and part as a separate
load; a part's *neighbour* heating is the total field minus its own; and a
pulse's end-of-pulse field on any steady background is that background plus
the pulse's own transient from zero, so each pulse is solved once.

### Validation

| case | against | result |
|---|---|---|
| strip, one layer | ``rho L / (W t)`` | +1.02%, +0.51%, +0.26% at 0.2, 0.1, 0.05 mm |
| annulus, 0.5 oz | ``rho ln(r2/r1) / (2 pi t)`` | +1.67%, +0.76% at 0.1, 0.05 mm |
| two layers through one barrel | strip + tube + strip in series | within 2% at 0.05 mm |
| four symmetric vias | equal shares | within 1% |
| two sinks | each sink's own drop | within 3% |
| any solve | I^2R against I x V | within 0.5% |
| uniform heating | ``P / (h A)`` | exact |
| two sheets, top heated | the 1-D network by hand | exact |
| point source on a sheet | ``P K0(r/l) / (2 pi k t)`` | within 0.1% at 5, 10, 20 mm |
| lumped transient at ``t = tau`` | ``1 - exp(-1)`` | -0.07% |
| IPC-2152 baseline: one trace, 1.78 mm board, no planes, still air; 1-3 oz, 10-400 mil, +10 to +45 C | Brooks's fits to IPC-2152's charts | outer 0.86-1.19x (54 cases, mean 1.00 — the fit); inner 0.93-1.50x (36 cases, hot side) |
| IPC-2221's working point: 1 mm, 1 oz, 2.39 A | IPC-2152: +13.1 C | lands within 20%; the first version read **+8.7 C**, a third too cool on every case |
| a six-layer board, 12 declared paths | energy balance at every sink | within 2% |
| the same, grid halved 0.1 -> 0.05 mm | drop | moved 0.3% to 7.1%, always downward |
| the same, `--coupled` | hot copper | loss up 9-12% with the board at up to +22 C |

What none of this establishes: that a real board's surroundings match any one
environment, and that its parts shed heat only through their pads. It has not
been compared with a thermocouple on a real board.

### Thresholds

| finding | minor / major / critical | why |
|---|---|---|
| drop over `max_drop_mv` | 1x / 1.5x / 3x | only when the path declares a limit; otherwise the drop is reported as a fact |
| busiest barrel over IPC-2152 at the path's rise | 1x / 1.5x / 2.5x | IPC treats a barrel as a long conductor; a real one is 1.6 mm between two heat sinks and runs cooler, so major starts at 1.5x |
| a path's **own** copper rise over its `rise_c` | 1x / 2x / 4x | the IPC sense of "rise"; 4x the 10 C default is 40 C from current alone |
| the board, absolute | — / 90 C / 105 C | FR-4 laminates 105-130 C continuous, solder mask and most board-mount connectors 85-105 C; major leaves room for the environment's error |
| neighbour heating under a part | 10 C | moves a junction estimate by more than its own arithmetic |
| grid change on halving | 10% | `--check-grid` only |

A pulse shorter than 50 ms (`SHORT_PULSE_S`) judges its barrels against the
adiabatic bound ``J^2 rho t / (rho_m c)`` instead of IPC: a barrel's thermal
time constant along 1.6 mm of copper is ~6 ms, so shorter than that it has
not reached equilibrium, and longer it effectively has. Every pulse finding
also quotes the adiabatic bound at the path's worst spot, beside the modelled
rise, so a reader can see how much the spreading is doing.

### Cost

Measured on a 45 x 75 mm six-layer board with 12 paths: **63 s** for the
analysis, of which the heat model is most. SuperLU's default column ordering
took 50 s per factorisation of the 337k-unknown heat matrix; a symmetric
minimum-degree ordering with the layers interleaved cell by cell takes 11.6 s,
identical to 1e-14. Three environments plus one pulse stepper are four
factorisations. `--check-grid` (a second current solve per path at half the
pitch) and `--coupled` (the continuous paths re-solved hot, and the heat
re-run) added 27 s between them. A whole-board ground path is the largest
current solve, ~6 s at 750k cells. Inside `pcbreview.run` the check added
~70 s to a 21 s review of the same board.

## Pictures (`pictures.py`, `render.py`)

`pcbreview.run` draws a review's pictures by default and lists them after the
questions. It used to be an instruction in `SKILL.md` to render the findings
reported; an instruction is followed when it is remembered, so the drawing
moved into the command the way the selection moved into `report/`.

What is drawn, in reading order: the board's top copper; one picture per
layout group of five or more, from its most severe member with something to
draw; every `critical` then `major` outside those groups, up to `MAX_SEVERE`
(12); and, when power paths are declared, a temperature map per scenario and a
current map per path, taken from the `power_path` check's own result
(`AuditReport.power_paths_result`) rather than a second solve.

Three rules keep a picture readable, each from a picture that was not:

| rule | why |
|---|---|
| at most `MAX_NETS` nets highlighted, one per `render.HIGHLIGHTS` colour | a broadside victim with 39 aggressors drew as a tangle; six nets showed two in white |
| poured-plane nets are never highlighted; a finding about planes alone, with no geometry, is not drawn | a parity finding about four `GND` exposed pads drew as the whole ground pour |
| a finding naming more nets than colours, with geometry, is drawn by the geometry alone | a DRC rule type touches dozens of nets; `drc` findings now carry a mark (`DRC_MARK_MM`) at every violation |

A finding with no net left to draw and no geometry is listed as not drawn,
with the `--around` command that draws it: a radius around a point would be an
invented scale. Pictures go to the OS cache, one folder per board, emptied at
the start of each run; nothing is written beside the board unless
`--pictures DIR` says where. One picture failing is listed and never fails
the review.

**Cost.** `render.render(plot_cache=)` plots each layer set once per run
instead of once per picture. On a 140 x 45 mm four-layer board the review took
138 s and 168 s with its 27 pictures; the browser screenshot is most of a
picture once its plot is cached.

## Physics (`physics.py`)

Microstrip comes from **scikit-rf** (Hammerstad-Jensen + Kirschning-Jansen
dispersion + causal Djordjević-Svensson dielectric), not a reimplementation.
Stripline is IPC-2141/Wadell, implemented here since scikit-rf lacks it.
Current capacity is **IPC-2152**, the measured successor to IPC-2221's chart,
through Brooks's published fit to its still-air external charts
(`dT = 215.3 I^2 W^-1.15 t^-1.0`, mils; "Trace Currents and Temperatures
Revisited", Eq. 3-2). One curve serves inner and outer layers, as IPC-2152's
universal chart does; its data has inner traces running cooler, so that errs
safe for them. No plane, board-thickness or material modifier is applied —
planes would only add capacity. On a board under 3 x 3 in the audit states, as
an assumption, that IPC-2152 recommends its conservative chart (IPC-2221's
internal curve) and gives that figure.

This replaced IPC-2221, which an earlier version kept "deliberately" as the
conservative chart. It is not, for a bare outer trace: IPC-2152 allows ~25%
less current than IPC-2221 for a wide one (3.92 A against 5.28 A at 3 mm, 1 oz,
+10 C) and somewhat more for a narrow one (0.83 A against 0.74 A at 0.2 mm),
and dropped IPC-2221's external chart as safe only on boards over 3 x 3 in with
planes. IPC-2221's internal curve, half the external, turned out to describe a
conductor in free air. `physics.ipc2221_current_a` keeps the old formula for
comparison.

Validated against JLCPCB's own single-ended targets:

| board | JLC target | modelled | error |
|---|---|---|---|
| the four-layer board (4L) | 50 Ω @ w=0.3424 | 51.32 Ω | +2.6% |
| the six-layer board (6L) | 50 Ω @ w=0.1565 | 52.15 Ω | +4.3% |

Both read slightly *high*, which is the expected direction: JLC's target is
coplanar, and the side pour it assumes pulls Z₀ down a few percent that a plain
microstrip model does not see.

**Coplanar structures are not solved here.** Back-solving ε_r from these
targets gives 4.88 and 4.69 against material values of 4.6 and 4.25 — the
difference is coplanar loading, not dielectric constant, and calibrating ε_r
that way would be right only for traces at JLC's exact 0.508 mm clearance.
`coplanar_clearance()` measures the gap actually routed so the Phase 4 field
solver can be pointed at real geometry.

**The transmission-line gate** (`is_transmission_line`) is what keeps the audit
from generating noise:

| signal | rise time | critical length | 30–40 mm trace |
|---|---|---|---|
| I2C 400 kHz | 500 ns | 14 m | lumped |
| SPI 1 MHz | 50 ns | 1.4 m | lumped |
| USB FS | 4 ns | 112 mm | lumped |
| GPS 1.575 GHz | 300 ps | 8.4 mm | **transmission line** |

Results carry their assumptions: `ImpedanceResult.warnings` records
out-of-range geometry, offset-stripline approximation, and the skin-depth limit
below which conductor loss is understated (Z₀ stays valid).

## Design rules

- **The model exposes geometry, not conclusions.** Analyses build on it; it
  makes no judgements.
- **Never mutate the user's board.** `run_drc(save_board=True)` exists but is
  never the default, and is covered by a test.
- **Never write into the user's project tree.** Extraction caches live in
  `.cache/` here, keyed by a hash of the board path.
- **Cross-validate against KiCad** wherever KiCad has an opinion.

## Design-note citations in the source

Docstrings here cite `§N` and phrases like *"the design brief §2.6"*. Those
point at development records — a handoff log and a set of design notes — which
are **not part of the distribution**, because they name the boards this was
built against and the distribution must not.

They are kept because a threshold with a reason attached is worth more than a
threshold, and the citation says the reason was written down rather than
invented. Read them as provenance, not as links; every sentence around them
stands on its own.

## Requirements

- **KiCad 9 or 10**, for `pcbnew` and `kicad-cli`.
- **Your own Python 3.10+**, for everything else:
  ```bash
  python3 -m pip install -r requirements.txt
  ```

Two interpreters, and they cannot be merged: KiCad's bundled Python has
`pcbnew` and no numpy/shapely, and every analysis downstream of `extract.py`
needs the opposite. `extract.py` is the only file that imports `pcbnew`, and it
runs as a subprocess under KiCad's interpreter.

KiCad's Python is found automatically — explicit argument, then
`$PCBREVIEW_KICAD_PYTHON`, then per-platform locations, each *probed* with
`import pcbnew` rather than merely checked for existence. Set the environment
variable if you have an unusual install:

```bash
export PCBREVIEW_KICAD_PYTHON=/path/to/kicad/bin/python3
```

Only verified on macOS so far. `kicad-cli` is found the same way, via `PATH`.
