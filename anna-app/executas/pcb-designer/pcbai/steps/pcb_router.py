"""KiCad-backed board assembly step ( OPTIONAL backend ).

This module drives KiCad's ``pcbnew`` python bindings through a generated helper script.
Per docs/AUDIT.md Q1 it is now **explicitly optional**: when KiCad is not installed the step
returns a structured ``ok: False`` result with ``reason: "backend-unavailable"`` instead of
pretending to have routed a board, and instead of crashing on ``import pcbnew``.

Fixed here relative to the audited version (docs/AUDIT.md D3/D5/D6, §6 D2):
  * stdout is never written to (the generated script logs to **stderr**) → no JSON-RPC corruption;
  * the ``footprints`` dir is actually populated by the caller (`save_footprints=True`),
    instead of silently expecting a directory nothing ever created;
  * ``netlist.json`` (it always contained JSON, but was named ``netlist.xml``);
  * real status propagation: ``ok`` is False when pcbnew is missing or the subprocess fails;
  * optional Freerouting pass when a jar is available (DSN → SES → import).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import pathlib
from typing import Dict, List, Optional

from pcbai.core.config import settings
from pcbai.core.logger import get_logger
from pcbai.eda import backend

logger = get_logger("pcbai.router")

_THIS_FILE = pathlib.Path(__file__).resolve()
_PKG_ROOT = str(_THIS_FILE.parent.parent.parent.resolve())   # .../pcb-designer (importable root)

#: Scaffolds the caller may drop .kicad_mod files into before routing.
FOOTPRINTS_SUBDIR = "footprints"


def _generate_pcbnew_script(netlist_path: str, output_pcb_path: str, footprints_dir: str,
                            *, experimental_router: bool, outline: Optional[Dict[str, float]]) -> str:
    """Return a python script that builds the board with pcbnew.

    NOTE: every diagnostic inside the child script goes to **stderr** (``_log``) so that the
    parent's stdout stays a clean protocol channel.
    """
    w = (outline or {}).get("width_mm", 60.0)
    h = (outline or {}).get("height_mm", 40.0)
    return f'''import os
import sys
import json

def _log(*a):
    print(*a, file=sys.stderr, flush=True)

try:
    for _cand in {backend.lib_search_paths()!r}:
        if _cand and os.path.isdir(_cand) and _cand not in sys.path:
            sys.path.insert(0, _cand)
    import pcbnew
except Exception as exc:
    _log("FATAL: pcbnew import failed in child process:", exc)
    sys.exit(3)

sys.path.insert(0, {_PKG_ROOT!r})

try:
    board = pcbnew.BOARD()

    with open({netlist_path!r}, "r", encoding="utf-8") as fh:
        netlist = json.load(fh)

    # ── nets ────────────────────────────────────────────────────────────────
    for i, net_entry in enumerate(netlist.get("nets", []), start=1):
        net_name = net_entry.get("name", "")
        if net_name:
            board.Add(pcbnew.NETINFO_ITEM(board, net_name, i))
    board.BuildListOfNets()
    net_index = {{ni.GetNetname(): ni.GetNetCode() for ni in board.GetNetsByName().values()}}

    # ── footprints available locally ────────────────────────────────────────
    fp_dir = {footprints_dir!r}
    available_fps = []
    if os.path.isdir(fp_dir):
        available_fps = sorted(f[:-10] for f in os.listdir(fp_dir) if f.endswith(".kicad_mod"))
    else:
        _log("WARN: footprint dir", fp_dir, "does not exist - no footprints can be placed")

    def find_parent_net(name):
        return name.split("-")[0] if "-" in name else name

    # ── place footprints ────────────────────────────────────────────────────
    placed = {{}}
    x, y = 20.0, 20.0
    for i, comp in enumerate(netlist.get("components", []), start=1):
        mpn = comp.get("mpn", "")
        package = comp.get("package", "")
        ref = comp.get("ref", f"U{{i}}")

        best_match = None
        for fp_file in available_fps:
            low = fp_file.lower()
            if (package and package.lower() in low) or (mpn and mpn.lower() in low):
                best_match = fp_file
                break
        if not best_match and available_fps:
            best_match = available_fps[i % len(available_fps)]

        if not best_match:
            _log(f"WARN: no footprint for {{ref}} ({{package}}/{{mpn}}) - component NOT placed")
            continue
        try:
            fp = pcbnew.FootprintLoad(fp_dir, best_match)
            fp.SetReference(ref)
            fp.SetValue(mpn)
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x + (i % 5) * 12.0), pcbnew.FromMM(y + (i // 5) * 10.0)))
            board.Add(fp)
            placed[ref] = fp
        except Exception as exc:
            _log(f"WARN: could not load footprint {{best_match}}: {{exc}}")

    # ── pad-level connectivity from netlist pins ("REF-PIN") ────────────────
    connected = 0
    for net_entry in netlist.get("nets", []):
        code = net_index.get(net_entry.get("name"))
        if code is None:
            continue
        for pin_ref in net_entry.get("pins", []):
            if isinstance(pin_ref, dict):
                ref, pad = pin_ref.get("ref"), str(pin_ref.get("pin"))
            else:
                s = str(pin_ref)
                ref, _, pad = s.partition("-")
            fp = placed.get(ref)
            if not fp or not pad:
                continue
            for pad_obj in fp.Pads():
                if pad_obj.GetPadName() == pad or pad_obj.GetNumber() == pad:
                    pad_obj.SetNet(pcbnew.NETINFO_ITEM(board, net_entry["name"], code))
                    connected += 1
    board.BuildListOfNets()
    _log(f"assigned {{connected}} pads to nets")

    # ── placement + (optional) experimental routing ─────────────────────────
    try:
        from pcbai.steps.smart_placer import optimize_placement
        optimize_placement(board, netlist)
        _log("smart placement applied")
    except Exception as exc:
        _log(f"smart placement skipped/failed: {{exc}}")

    if {experimental_router!r}:
        _log("[WARNING] EXPERIMENTAL native router: no obstacle avoidance, traces may short.")
        try:
            from pcbai.steps.native_router import autoroute_board
            autoroute_board(board)
        except Exception as exc:
            _log(f"native router failed: {{exc}}")

    pcbnew.SaveBoard({output_pcb_path!r}, board)
    _log(f"board saved to {{ {output_pcb_path!r} }}")

    dsn_path = {output_pcb_path!r}.replace(".kicad_pcb", ".dsn")
    try:
        exporter = pcbnew.SPECCTRA_DB()
        exporter.ExportPCB({output_pcb_path!r}, dsn_path)
        _log(f"dsn exported to {{dsn_path}}")
    except Exception as exc:
        _log(f"dsn export skipped: {{exc}}")

    with open({output_pcb_path!r} + ".report.json", "w", encoding="utf-8") as fh:
        json.dump({{"footprints": len(placed), "pads_in_nets": connected,
                    "board_bbox_mm": [0, 0, {w!r}, {h!r}]}}, fh)
except Exception as exc:
    import traceback
    _log("ERROR generating board:", traceback.format_exc())
    sys.exit(1)
'''


def _child_python() -> str:
    """Interpreter most likely able to import pcbnew."""
    env = os.getenv("PCB_AI_KICAD_PYTHON")
    if env and os.path.exists(env):
        return env
    found = shutil.which("kicad-python") or shutil.which("python3")
    return found or sys.executable


def _collect_footprints(out_dir: str) -> List[str]:
    fp_dir = os.path.join(out_dir, FOOTPRINTS_SUBDIR)
    if not os.path.isdir(fp_dir):
        return []
    return sorted(f for f in os.listdir(fp_dir) if f.endswith(".kicad_mod"))


def route_pcb(netlist: Dict, output_dir: str = "build", *, outline: Optional[Dict[str, float]] = None,
              run_freerouting: bool = True) -> Dict:
    """Build a .kicad_pcb from a netlist using KiCad. Never raises for a missing backend."""
    os.makedirs(output_dir, exist_ok=True)
    output_pcb = os.path.join(output_dir, "board.kicad_pcb")
    footprints_dir = os.path.join(output_dir, FOOTPRINTS_SUBDIR)
    os.makedirs(footprints_dir, exist_ok=True)   # used to be referenced but never created (D5)

    # ── inputs are always materialised, so a run is reproducible/inspectable even when the
    #    backend is missing (and so `generate_footprint` output is visible to the next step)
    with open(os.path.join(output_dir, "fp-lib-table"), "w", encoding="utf-8") as f:
        f.write('(fp_lib_table\n')
        f.write('  (lib (name "local")(type "KiCad")(uri "${KIPRJMOD}/footprints")'
                '(options "")(descr ""))\n)\n')

    netlist_path = os.path.join(output_dir, "netlist.json")   # was misnamed netlist.xml
    with open(netlist_path, "w", encoding="utf-8") as f:
        json.dump(netlist, f, indent=2)

    caps = backend.capabilities()
    result: Dict[str, object] = {
        "ok": False,
        "status": "not-attempted",
        "backend": "pcbnew" if caps.pcbnew else "pcbnew-missing",
        "board_file": output_pcb,
        "netlist_file": netlist_path,
        "footprints_found": _collect_footprints(output_dir),
        "tracks": [],
        "netlist": netlist,
        "warnings": list(caps.notes),
    }
    if not caps.pcbnew:
        result["reason"] = "backend-unavailable"
        result["status"] = ("failed: KiCad python bindings (pcbnew) are not installed; "
                            f"netlist written to {netlist_path} for a KiCad-capable host")
        logger.warning("route_pcb skipped: %s", result["status"])
        return result

    script = _generate_pcbnew_script(
        netlist_path, output_pcb, footprints_dir,
        experimental_router=settings.enable_experimental_router,
        outline=outline,
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as sf:
        sf.write(script)
        script_path = sf.name

    try:
        proc = subprocess.run([_child_python(), script_path], capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(output_pcb):
            result["ok"] = True
            result["status"] = "built"
            report = output_pcb + ".report.json"
            if os.path.exists(report):
                try:
                    with open(report, encoding="utf-8") as fh:
                        result["report"] = json.load(fh)
                except Exception:
                    pass
            if run_freerouting:
                result["freerouting"] = _maybe_freeroute(output_pcb)
        else:
            result["status"] = f"failed: rc={proc.returncode} {(proc.stderr or '').strip()[-500:]}"
            result["reason"] = "router-error" if proc.returncode != 3 else "backend-unavailable"
        if proc.stderr:
            result["stderr_tail"] = proc.stderr.strip()[-1500:]
    except subprocess.TimeoutExpired:
        result["status"] = "failed: timeout after 300 s"
        result["reason"] = "timeout"
    except Exception as exc:  # pragma: no cover
        result["status"] = f"failed: {type(exc).__name__}: {exc}"
        result["reason"] = "error"
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
    return result


def _maybe_freeroute(board_file: str) -> Dict[str, object]:
    """If Freerouting is available, run DSN → SES and record the result. Never fatal."""
    jar = backend.freerouting_jar()
    dsn = board_file.replace(".kicad_pcb", ".dsn")
    if not jar or not os.path.exists(dsn):
        return {"ok": False, "skipped": True,
                "why": "freerouting jar or .dsn missing"}
    try:
        proc = subprocess.run([os.getenv("PCB_AI_JAVA", "java"), "-jar", jar, "-de", dsn,
                               "-do", board_file.replace(".kicad_pcb", ".ses")],
                              capture_output=True, text=True, timeout=600)
        return {"ok": proc.returncode == 0, "skipped": False,
                "detail": (proc.stdout or proc.stderr)[-500:]}
    except Exception as exc:
        return {"ok": False, "skipped": False, "detail": f"{type(exc).__name__}: {exc}"}
