"""Shared fixtures for the pcb-agent test suite.

Tests must run with no KiCad, no network and no API keys (docs/AUDIT.md §7/Q1). Anything that
needs KiCad is skipped explicitly rather than silently passing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "anna-app" / "executas" / "pcb-designer"
PLUGIN = PLUGIN_DIR / "plugin.py"

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


# ── tiny synthetic KiCad files (so tests do not depend on the ESP32 template) ──
def make_board_kicad_pcb(*, with_net_table: bool = True, tracks: int = 2) -> str:
    """A *structurally* minimal .kicad_pcb: outline, net table, two footprints, a couple of tracks."""
    nets = ""
    if with_net_table:
        nets = '  (net 0 "")\n  (net 1 "VDD_3V3")\n  (net 2 "GND")\n'
    pad_net = ' (net 1 "VDD_3V3")' if with_net_table else ' (net "VDD_3V3")'
    pad_gnd = ' (net 2 "GND")' if with_net_table else ' (net "GND")'
    track_block = ""
    for i in range(tracks):
        track_block += (
            f"  (segment (start {10 + i} 10) (end {14 + i} 10) (width 0.25) "
            f'(layer "F.Cu") {"(net 1)" if with_net_table else chr(34) + "VDD_3V3" + chr(34)}\n'
            f"   )\n"
        )
    return f"""(kicad_pcb
  (version 20260206)
  (generator "pcbai")
  (generator_version "10.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
{nets}  (gr_line (start 0 0) (end 40 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 40 0) (end 40 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 40 20) (end 0 20) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (gr_line (start 0 20) (end 0 0) (stroke (width 0.1)) (layer "Edge.Cuts"))
  (footprint "pkg:SOIC8" (layer "F.Cu") (at 10 10)
    (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS"))
    (property "Value" "MP1584" (at 0 3 0) (layer "F.Fab"))
    (pad 1 smd rect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask"){pad_net})
    (pad 2 smd rect (at -2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask"){pad_gnd})
  )
  (footprint "pkg:R0402" (layer "F.Cu") (at 20 10)
    (property "Reference" "R1" (at 0 -1 0) (layer "F.SilkS"))
    (pad 1 smd rect (at -0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask"){pad_net})
    (pad 2 smd rect (at 0.5 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask"){pad_gnd})
  )
{track_block})
"""


@pytest.fixture
def tiny_board(tmp_path: Path) -> Path:
    f = tmp_path / "board.kicad_pcb"
    f.write_text(make_board_kicad_pcb(), encoding="utf-8")
    return f


@pytest.fixture
def legacy_template_board() -> Path:
    """The shipped reference board — known to be missing a top-level net table (audit finding)."""
    return PLUGIN_DIR / "pcbai" / "steps" / "template_project" / "board.kicad_pcb"


# ── RPC helpers ──────────────────────────────────────────────────────────────
def rpc(requests: List[Dict], *, timeout: int = 90, env: Dict[str, str] | None = None):
    """Speak JSON-RPC to plugin.py over stdio; return (parsed_responses, stdout, stderr)."""
    payload = "".join(json.dumps(r) + "\n" for r in requests + [{"jsonrpc": "2.0", "id": "bye",
                                                                "method": "shutdown"}])
    run_env = {**os.environ, "PCB_AI_LLM_PROVIDER": "dummy", "PYTHONPATH": str(PLUGIN_DIR),
               **(env or {})}
    proc = subprocess.run([sys.executable, str(PLUGIN)], input=payload, capture_output=True,
                          text=True, cwd=str(REPO), env=run_env, timeout=timeout)
    responses = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))  # raises on a protocol violation — that's the point
    return responses, proc.stdout, proc.stderr


@pytest.fixture
def rpc_env(tmp_path: Path) -> Dict[str, str]:
    """Isolated work/project dirs so tests never touch the repo tree."""
    return {"PCB_AI_WORKDIR": str(tmp_path / "build"), "PCB_AI_PROJECT": str(tmp_path / "project")}
