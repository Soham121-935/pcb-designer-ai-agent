#!/usr/bin/env python3
"""Compare a KiCad DRC JSON report with what pcbai's own checker claimed.

This runs in the ``kicad-parity`` CI job, where KiCad is installed. Locally it is a no-op that says so,
because the sandbox this project is developed in has no KiCad (and pretending otherwise is the failure
mode docs/AUDIT.md §11 Q1 exists to prevent).

Why it is not simply "DRC exit code 0":
    * The scaffold does **not** finish routing. KiCad therefore always reports unconnected pads, and a
      gate that failed on those would be red forever and teach nobody anything.
    * What the generator *does* claim is that the copper it emitted obeys the clearance, width, drill,
      annular-ring, edge and courtyard rules of the project's own ``.kicad_pro``. Those are exactly the
      categories this script treats as fatal.
    * Categories KiCad can see and we deliberately do not (3D model collisions from the real libraries,
      schematic parity, lib footprint names) are printed as ``notes`` and never fail the run.

Usage:
    python3 scripts/kicad_parity_report.py build/sv16/sv16-drc.json \
        [--ours build/sv16/ours.json]

``--ours`` is optional: pass the JSON from ``pcbai-agent validate board.kicad_pcb --json`` to get a
side-by-side count instead of just the KiCad column.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

#: KiCad DRC violation types whose absence the generator actually asserts.
FATAL = {
    "clearance",
    "shorting_items",
    "copper_edge_clearance",
    "track_width",
    "via_annular_width",
    "via_min_drill",
    "hole_to_hole",
    "microvia_annular_width",
    "microvia_min_drill",
    "solder_mask_bridge",
    "track_segment_length",
    "pad_area",
    "decap",
    "annular_width",
    "hole_clearance",
    "courtyards_overlap",
}

#: Real findings, but not something this generator promises yet.
NOTES = {
    "unconnected_items",
    "items_not_connected",
    "schematic_parity",
    "lib_footprint_issues",
    "lib_footprint_mismatch",
    "missing_footprint",
    "no_footprint_symbols",
    "footprint_filter",
    "sheet_instances",
    "orientation",
    "drill_out_of_range",
    "silk_overlap",
    "silk_over_copper",
    "silk_to_paste_dist_ratio",
    "footprint_placement_quadrant",
    "isolated_copper",
    "unnatural_connections",
}


def _load(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"kicad-parity: {path} is not JSON ({exc}); reporting the raw head instead")
        print("  " + path.read_text(encoding="utf-8")[:400].replace("\n", " | "))
        return "unparsable"


def _violations(doc: object) -> list[dict]:
    out: list[dict] = []
    if isinstance(doc, dict):
        for key in ("violations", "unconnected_items"):
            for item in doc.get(key) or []:
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("_source", key)
                    out.append(item)
    elif isinstance(doc, list):                      # some builds emit a bare list
        out = [i for i in doc if isinstance(i, dict)]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", nargs="?", default=None, type=Path,
                    help="kicad-cli pcb drc --format json output")
    ap.add_argument("--ours", type=Path, default=None, help="pcbai-agent validate --json output")
    ap.add_argument("--max-examples", type=int, default=12)
    args = ap.parse_args(argv)

    if args.report is None or not args.report.exists():
        print("kicad-parity: SKIPPED — no DRC report at "
              f"{args.report or '(none given)'}. KiCad is not installed here; `make test` is the "
              "honest gate and the kicad-parity CI job is where DRC/gerber checks run.")
        return 0
    doc = _load(args.report)
    if doc == "unparsable":
        return 2
    items = _violations(doc)
    kinds = Counter(str(i.get("type") or i.get("error") or "?") for i in items)
    fatal = {k: n for k, n in kinds.items() if k in FATAL}
    notes = {k: n for k, n in kinds.items() if k in NOTES}
    note_total = sum(notes.values())
    unknown = {k: n for k, n in kinds.items() if k not in FATAL and k not in NOTES}

    print(f"kicad-parity: {len(items)} KiCad finding(s) across {len(kinds)} category(ies)"
          + (f" — {note_total} of them are notes this gate does not enforce" if note_total else ""))
    for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        label = "FATAL" if kind in FATAL else ("note" if kind in NOTES else "other")
        print(f"  [{label:>5}] {kind}: {count}")
    if args.ours:
        ours = _load(args.ours)
        if isinstance(ours, dict):
            data = ours.get("data", ours)
            counts = data.get("counts") or {}
            print(f"kicad-parity: pcbai said error={counts.get('error')} "
                  f"warning={counts.get('warning')} "
                  f"(categories: {Counter(i.get('code') for i in data.get('issues') or [])})")

    shown = 0
    for item in items:
        if str(item.get("type")) in FATAL and shown < args.max_examples:
            shown += 1
            where = ", ".join(str(x) for x in (item.get("items") or [])[:2])
            print(f"    ! {item.get('type')}: {item.get('description', '')[:110]} @ {where[:70]}")

    if fatal:
        print(f"kicad-parity: FAILED — KiCad found {sum(fatal.values())} violation(s) in categories "
              "the generator claims to respect. Our checker is wrong somewhere; fix the checker or the "
              "generator, never this script's thresholds.")
        return 1
    if unknown:
        print(f"kicad-parity: {sum(unknown.values())} finding(s) in categories this script does not "
              f"know yet ({sorted(unknown)}) — classified as notes; review and assign them.")
    print("kicad-parity: OK (no clearance/short/width/drill/annular/courtyard findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
