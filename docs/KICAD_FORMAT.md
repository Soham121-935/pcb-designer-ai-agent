# The KiCad `.kicad_pcb` / `.kicad_pro` files pcbai writes

Target dialect: **KiCad 10** (`(version 20260206)`), with `kicad-9` (`20241229`) and `kicad-8`
(`20240108`) selectable through `dialect=` / `--dialect`. This document records *why* the writer
emits what it emits, so that a future change is made against the format rather than against
whichever test was last red.

Everything below was established from three sources, in this order of authority: the KiCad
developer docs for the s-expression formats, `pcb_io/kicad_sexpr` behaviour as recorded in
`include/layer_ids.h` and `pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.h` (the `SEXPR_BOARD_FILE_VERSION`
enum), and — where the docs were silent — a byte-level reading of the KiCad-authored project in
`pcbai/steps/template_project/`. Where we are *not* sure, that is stated out loud below, and the
`kicad-parity` CI job is the arbiter.

## 1. Layout of a board file

```lisp
(kicad_pcb
    (version 20260206)                     ; BOARD_VERSION[dialect]; must be the first child
    (generator "pcbai")
    (generator_version "10.0")
    (general (thickness 1.6) (legacy_teardrops no))
    (paper "A4")
    (title_block (title "…") (date "2026-09-27") (rev "0.1") (company "…") (comment 1 "…"))
    (layers (0 "F.Cu" signal) (4 "In1.Cu" power) (6 "In2.Cu" power) (2 "B.Cu" signal))
    (setup (stackup …) (copper_finish "HASL with lead free solder") (dielectric_constraints no)
           (pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no)
           (tenting (front yes) (back yes)) (pcbplotparams …))
    # net classes are NOT in the board file for KiCad 10 (see §3); a KiCad 8/9 file writes
    #   (net_class "Default" "Default" (clearance 0.127) …) here instead
    (footprint "pcbai:SV16_LQFP48_7x7_P0.5" …)
    (segment (start …) (end …) (width 0.6) (layer "F.Cu") (net "GND") (uuid …))
    (via (at …) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "GND") (uuid …))
    (zone (net "GND") (layer "In1.Cu") (name "GND_plane") (hatch edge 0.5)
          (connect_pads (clearance 0.25)) (fill (mode solid)) (polygon …))
    (gr_line (start …) (end …) (layer "Edge_Cuts") (width 0.1) (uuid …))
)
```

Indentation is a tab, and `dumps()` reproduces input bytes exactly — including `//` and `;`
comments — so reading a board back and writing it again is a no-op. Freshly built trees are
`.beautify()`-ed instead. That property is load-bearing: `apply_edit` round-trips a real KiCad
board through it, and a "prettifier" that reflows comments would silently corrupt user files.

## 2. Rules we follow, and the bug each one prevents

| Rule | Why |
|---|---|
| Layer **ordinals** in `(layers …)`, canonical **names** everywhere else | `layer_ids.h` fixes the ordinals for legacy compatibility: `F_Cu=0`, `F_Mask=1`, `B_Cu=2`, `B_Mask=3`, `F_SilkS=5`, `InN_Cu=2*(N+1)` (`In1.Cu=4`, `In2.Cu=6`, …), `Edge_Cuts=25`, `F_CrtYd=31`, `F_Fab=35`. KiCad 9 renumbered the *display* order, not these ids; using names in `(layers …)` is rejected. |
| `(layers "F.Cu")` quoted; `(pad "1" smd roundrect …)` bare | KiCad 6+ sets `QUOTE_ALL_STRINGS`, but pad/package/zone enums and layer *type* tokens are written bare. `sexp.BARE_KEYWORDS` is the allow-list; get it wrong and KiCad's parser errors out with a caret pointing at an unrelated token. |
| Non-plated holes are `(pad "" np_thru_hole circle …)` | **This one was a real bug.** We used to emit `(pad "" np_thru circle …)`, which is not in KiCad's grammar at all — KiCad accepts only `thru_hole`, `np_thru_hole`, `smd`, `connect`, `footprint_area`. A generated board with mounting holes therefore failed to load. Found by diffing our output against the KiCad-authored template, not by a test we had written: the checker *also* used to flag KiCad's own `np_thru_hole` pads as invalid, so both directions were wrong at once. |
| Pads that live on inner layers use wildcards: `(layers "*.Cu" "*.Mask")` for THT | KiCad expands `*.Cu` at load time. Anything that inspects pad layers must go through `layers.layer_matches/layers_touch/expand_layers`, never `in` or `set()`, or a perfectly legal board reads as "pad with no copper". |
| `(net "GND")` on tracks/vias/zones, **no net table** | From `20251028` KiCad 10 stopped writing the `(net 5 "GND")` id table into boards; nets are referenced by name. We write names, and `(net 0 "")` appears only in `kicad-8/9` dialect output. |
| `(attr smd board_only)` / `(attr through_hole)` are bare flags after the pad type | Same grammar as pads; `board_only`, `exclude_from_bom`, `exclude_from_pos_files`, `dnp` belong to `(attr …)` of a footprint, not to a pad. |
| `(at x y rot)` with rot in degrees, `tstamp`-free, `uuid` per object | `20210126` put rotation into `(at …)`; since `20240108` every object carries a UUID and `tstamp` is gone. `model.new_uuid()` v4 strings, one per object, stable across a read/write cycle. |
| Coordinates up to 9 decimals | KiCad writes 6 for mm files. Ours are legal (it parses doubles and snaps on import) — deliberately left alone rather than risk a rounding mismatch between what we validate and what we write. |
| `(setup …)` contains **only tokens the reference file also contains** | We never emit a `(setup)` key we have not seen KiCad write. Inventing tokens (e.g. `rule_dimensions` sub-keys) is how a writer ends up with a file KiCad "opens" by dropping half of it. |

## 3. Where board-level design rules actually live (and the `pcbai` block)

In a KiCad 10 project, **everything** the *Board Setup → Design Rules → Constraints* dialog shows —
`min_clearance`, `min_track_width`, annular width, hole-to-hole, copper-to-edge, silk, mask and the
severities — lives in **`.kicad_pro`** (`board.design_settings`), and net classes in its top-level
`net_settings`; `(net_class …)` inside `.kicad_pcb` is the KiCad 8/9 shape, and `.kicad_dru` holds
custom rules. A `.kicad_pcb` alone therefore cannot answer "is this clearance legal?". Consequences we designed around:

* `read_board(path)` with `merge_project=True` pulls `.kicad_pro` (and `.kicad_dru` if present) in,
  because validating from the `.kicad_pcb` alone means validating with the wrong numbers.
* The exact key set is copied from the KiCad-authored reference project, not from prose docs,
  because an invented key is worse than a missing one: KiCad ignores it silently, DRCs the board
  against its own defaults, and our checker still believes the project's numbers were applied.
  (This is exactly what our writer used to do — `design_settings.rules` carried `"clearance"`,
  `"track_widths"`, `"via_sizes"`, which KiCad has never heard of.) So `board.design_settings.rules`
  now carries KiCad's real `min_*` names: `min_clearance`, `min_track_width`, `min_via_diameter`,
  `min_through_hole_diameter` (← `min_via_drill`), `min_via_annular_width` (← `annular_ring_min`),
  `min_hole_to_hole`, `min_copper_edge_clearance`, `min_silk_clearance` (← `min_silk_to_silk`),
  `min_text_height`, `min_text_thickness`, `solder_mask_to_copper_clearance`
  (← `pad_to_mask_clearance`) — and net classes go in the **top-level** `net_settings.classes[]`,
  which is where KiCad 10 looks (`board.net_settings` is not a thing). KiCad keys we do not model
  (`min_hole_clearance`, `min_resolved_spokes`, `max_error`, …) keep KiCad's own values rather than a
  guess. `pcb_writer` filters its own output through `KICAD10_RULE_KEYS` / `KICAD10_SEVERITY_KEYS`,
  and `tests/test_kicad_scaffold.py::test_project_file_only_uses_keys_kicad_itself_writes` diffs our
  project file against the reference one — a parity check that needs no KiCad installed.
* What KiCad cannot express at all — courtyard *clearance* (KiCad has a courtyard-overlap severity
  but no distance knob), the mask-web minimum, and a board-level tenting mirror — goes in a
  **namespaced block** next to KiCad's own keys:

  ```json
  "board": {"design_settings": {
      "defaults": {…}, "rule_severities": {…}, "rules": {…},
      "pcbai": {"annular_ring_min": 0.15, "min_courtyard_clearance": 0.25,
                "min_silk_to_silk": 0.15, "min_mask_web": 0.05,
                "pad_to_mask_clearance": 0.0, "tenting": true}}}
  ```

  KiCad ignores unknown keys, so the file stays loadable; `rules_from_project()` reads KiCad's real
  `min_*` names first, then the legacy names older pcbai projects used, then `pcbai` for the
  leftovers. Every knob therefore survives our own write→read cycle exactly — `tenting` as a JSON
  **bool**, not `0.0`: a float there is ignored on read, and the board file (`(setup (tenting
  (front no) …))`) and the project file would then disagree about the same design.
* Fab limits (this project's deferred decision) are per-project by construction: a spec's `rules:`
  block is applied *before* the `Default` class is derived, so a net class can never be built from
  stale defaults.

## 4. What we deliberately do **not** round-trip

`pcb_writer.lossiness(board, source)` compares a KiCad-authored file with what we can rebuild and
lists the keys we drop. On the reference template it reports:

```json
{"lossy": true,
 "dropped_in_footprints": {"duplicate_pad_numbers_are_jumpers": 21, "embedded_fonts": 21,
                           "model": 17, "fp_poly": 4, "zone": 1}}
```

so `apply_edit` refuses to touch a KiCad-authored board unless the caller passes `allow_lossy`
(the tool refuses first, and says what it would have lost). For boards pcbai wrote itself
`lossy` is `false` (measured: 31 footprints compared, none dropped) — that is the only case where
native editing is safe, and the tool knows the difference. Dropped items are, in order of pain:
3-D model references, `fp_poly` graphics, per-footprint embedded font settings, jumper pad
definitions and KiCad-10 zone properties.

## 5. How the parity claim is tested

`make test` never touches KiCad — it is fully deterministic offline. The claim "this loads in
KiCad and is electrically sane" is tested in the **`kicad-parity`** CI job
(`.github/workflows/ci.yml`, container `kicad/kicad:latest`), which:

1. generates the SV-16 project with `pcbai.kicad.scaffold.generate(...)`;
2. runs `kicad-cli pcb drc --format json --severity-error --severity-warning` on it;
3. `scripts/kicad_parity_report.py` fails the job if KiCad reports *any* violation in a category the
   generator actually asserts (clearance, shorts, copper-to-edge, width, drill, annular ring,
   hole-to-hole, mask bridge, courtyard overlap) — unconnected pads are *expected*, since the
   scaffold does not finish routing, and are reported as notes;
4. `scripts/kicad_roundtrip_check.py` loads our file in `pcbnew`, saves it, re-loads it, and
   compares footprint/pad/track/via/zone counts and net names with our own reader's view;
5. exports Gerbers for all four copper layers plus mask and silk, and an excellon drill set with a
   map, refusing to pass if a claimed layer produces nothing.

Both scripts exit `0` with a `SKIPPED` line when KiCad is absent, so they are honest locally and
strict in CI. If a future run of step 3 goes red, the fix belongs in the checker or the writer —
never in that script's category list.
