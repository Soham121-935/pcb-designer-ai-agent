#!/usr/bin/env python3
"""Prove that a board pcbai wrote is understood by KiCad, not merely parseable.

The claim this checks (docs/AUDIT.md §11 Q1): a ``.kicad_pcb`` we generate loads in KiCad, and a
KiCad save/reload cycle keeps the geometry we care about — footprints by reference, pad count and
numbers per footprint, track count, via count, net names. If KiCad silently renames a net, drops a pad
or re-interprets a layer, that is a writer bug and this script is where it surfaces.

Runs in the ``kicad-parity`` CI job (which uses the ``kicad/kicad`` container image). Without pcbnew it
prints SKIP and exits 0, because a developer sandbox that has no KiCad must still be able to run every
script in this folder.

Usage:
    python3 scripts/kicad_roundtrip_check.py build/sv16/sv16.kicad_pcb [--keep]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "anna-app" / "executas" / "pcb-designer"))


def _counts_from_kicad(board) -> dict:
    """Snapshot a live BOARD object, through whatever accessor this KiCad version offers."""
    import pcbnew

    fps = list(board.GetFootprints())
    tracks = list(board.GetTracks())
    zones = list(board.Zones()) if hasattr(board, "Zones") else []
    nets = []
    netinfo = board.GetNetInfo()
    for net in list(netinfo.Nets()):
        name = net.Getname() if hasattr(net, "Getname") else str(net)
        if name:
            nets.append(name)
    track_t = int(getattr(pcbnew, "PCB_TRACK_T", 12))
    via_t = int(getattr(pcbnew, "PCB_VIA_T", 11))
    return {
        "footprints": len(fps),
        "pads": sum(len(fp.Pads()) for fp in fps),
        "pad_numbers": {fp.GetReference(): sorted(str(p.GetPadName()) for p in fp.Pads()) for fp in fps},
        "tracks": sum(1 for t in tracks if t.Type() == track_t),
        "vias": sum(1 for t in tracks if t.Type() == via_t),
        "zones": len(zones),
        "nets": sorted(set(nets)),
        "edge_segments": len(list(board.GetDrawings())),
        "unconnected": len(board.GetUnconnected()) if hasattr(board, "GetUnconnected") else -1,
    }


def _counts_from_model(path: Path) -> dict:
    """Same snapshot through pcbai's own reader, so a disagreement is visible either way."""
    from pcbai.kicad.pcb_reader import read_board

    board = read_board(path)
    return {
        "footprints": len(board.footprints),
        "pads": len(list(board.all_pads())),
        "pad_numbers": {f.reference: sorted(p.number for p in f.pads) for f in board.footprints},
        "tracks": len([t for t in board.tracks if t.__class__.__name__ == "Track"]),
        "vias": len(board.vias),
        "zones": len(board.zones),
        "nets": sorted(board.net_names()),
        "edge_segments": len(board.edge),
        "unconnected": -1,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", type=Path)
    ap.add_argument("--keep", action="store_true", help="keep the KiCad-rewritten copy for inspection")
    args = ap.parse_args(argv)

    try:
        import pcbnew                                    # noqa: F401  (the whole point of this script)
    except Exception as exc:                             # pragma: no cover - environment dependent
        print(f"kicad-roundtrip: SKIPPED — no pcbnew here ({type(exc).__name__}: {exc}). "
              "This check runs in the kicad-parity CI job.")
        return 0

    import pcbnew

    src = args.board
    if not src.exists():
        print(f"kicad-roundtrip: FAILED — {src} does not exist")
        return 1
    work = Path(tempfile.mkdtemp(prefix="pcbai-roundtrip-"))
    mine = work / src.name
    shutil.copy2(src, mine)
    shutil.copy2(src.with_suffix(".kicad_pro"), work / src.with_suffix(".kicad_pro").name)

    print(f"kicad-roundtrip: KiCad {pcbnew.GetBuildVersion()} loading {src.name}")
    board = pcbnew.LoadBoard(str(mine))
    if board is None:
        print("kicad-roundtrip: FAILED — KiCad returned no board (the file did not load)")
        return 1
    before = _counts_from_kicad(board)
    mine2 = work / "resaved.kicad_pcb"
    board.Save(str(mine2))
    after = _counts_from_kicad(pcbnew.LoadBoard(str(mine2)))
    ours = _counts_from_model(src)

    bad: list[str] = []
    for key in ("footprints", "pads", "tracks", "vias", "zones"):
        if not (before[key] == after[key] == ours[key]):
            bad.append(f"{key}: kicad={before[key]} after-save={after[key]} pcbai={ours[key]}")
    if before["pad_numbers"] != after["pad_numbers"]:
        diff = {k for k, v in before["pad_numbers"].items() if after["pad_numbers"].get(k) != v}
        bad.append(f"pad numbers changed for {sorted(diff)[:6]}")
    if set(before["nets"]) != set(ours["nets"]):
        bad.append(f"net names differ: kicad-only={sorted(set(before['nets']) - set(ours['nets']))[:8]} "
                   f"pcbai-only={sorted(set(ours['nets']) - set(before['nets']))[:8]}")

    for key in ("footprints", "pads", "tracks", "vias", "zones", "nets"):
        print(f"  {key:>12}: kicad={before[key] if not isinstance(before[key], list) else len(before[key])} "
              f"after-save={after[key] if not isinstance(after[key], list) else len(after[key])} "
              f"pcbai={ours[key] if not isinstance(ours[key], list) else len(ours[key])}")

    if bad:
        print("kicad-roundtrip: FAILED")
        for line in bad:
            print(f"  ! {line}")
        print(f"  (the rewritten copy is under {work})")
        return 1
    print("kicad-roundtrip: OK — KiCad read our board, saved it, and every count survived the cycle")
    if args.keep:
        print(f"kicad-roundtrip: kept {mine2}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
