# prototypes

Proofs of concept that have since been built properly. Kept only where the
image or the measurement is still worth looking at.

- `broadside-example.png` — the render that proved §22's overlay approach: a
  finding drawn as the copper it is about. `/MAIN TX1` on In3.Cu running
  13.17 mm directly over `/SERVO6` on In2.Cu, 0.109 mm apart vertically and
  0.100 mm laterally, with no plane between them.

`finding_render.py` lived here and is now `pcbreview/render.py` — cross-platform,
tested, and wired to `Finding.geometry` so each check can draw its own evidence
rather than just a net highlight. See §29 of `HANDOFF.md`.
