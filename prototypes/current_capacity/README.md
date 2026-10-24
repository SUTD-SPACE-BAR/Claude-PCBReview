# Current capacity of tracks, planes and vias — §43 prototypes

Written during stress test #3 (the the four-layer motor controller Robot Brain, §43), because that board's
problems are conducted and thermal and **no check in `pcbreview` measures
either**. Both are validated against closed form; neither is wired into the
audit. §32.B9 is the home for them.

| file | what it does | its own check |
|---|---|---|
| `bottleneck.py` | narrowest copper channel between two pads, by binary search on erosion radius | a 1.5 mm strip reads 1.500; the same strip with a 0.6 mm notch reads 0.598 |
| `plane_solve.py` | a plane's DC resistance, by solving Laplace on the poured copper | `V = I·Rsheet·L/W` on a rectangle, **+0.38%** |
| `multilayer.py` | the erosion bottleneck across layers, with vias as links; `via_capacity_a` | — |
| `run_planes.py` | drives the above over that board's `+BATT` and `GND` planes | runs `selftest()` before it reports anything |

## The thing to know before using either

**The erosion bottleneck is the wrong measure for a poured plane.** On the
that board it read 5.5 mm for `+BATT`, which taken at face value says 3.4 A on
a bus carrying 40 A. Current flows *around* a via antipad rather than through
it, so a field of holes reads as a constriction and is not one. The Laplace
solve on the same plane says 2.0 mΩ end to end and 3.1 W — untidy, not broken.

Use erosion for a **track** or for a genuine isthmus, and the Laplace solve for
a **plane**. Where the two agree — as they did on that board's motor
channel 3, 0.49 mm and 40.4 mΩ against 6.9–15.0 mΩ for its seven siblings —
the constriction is real.

## The bug that made the self-tests mandatory

`run_planes.terminal_network` first merged each via cluster into one node by
rewriting its rows in place. That silently discards the KCL equations of every
cell but one, shrinking the electrode to a single 0.25 mm cell, whose spreading
resistance is enormous: the GND plane came out at **59 W**. Galerkin projection
(`A' = PᵀAP`) keeps every face conductance and gives 4.5 W. There was no
symptom other than an implausible number.
