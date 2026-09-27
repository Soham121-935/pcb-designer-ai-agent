#!/usr/bin/env python3
"""Print the tool manifest the pcb-designer plugin exposes, and check the JSON-RPC handshake.

Usage:  python3 scripts/test_rpc.py            # describe + health
          python3 scripts/test_rpc.py full_pipeline '{"description": "..."}'
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "anna-app" / "executas" / "pcb-designer" / "plugin.py"


def python_cmd() -> list[str]:
    """Prefer the repo venv, then uv (as executa.json declares), then the running interpreter."""
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return [str(venv_py)]
    if shutil.which("uv"):
        return ["uv", "run", "--project", str(PLUGIN.parent), sys.executable]
    return [sys.executable]


def run(requests: list[dict], timeout: int = 240) -> list[dict]:
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.Popen(
        python_cmd() + [str(PLUGIN)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(ROOT),
    )
    try:
        out, err = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        print(f"!! plugin timed out after {timeout}s", file=sys.stderr)

    stdout, stderr, returncode = out, err, proc.returncode
    results = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"!! non-JSON on plugin stdout (protocol violation): {line[:120]}", file=sys.stderr)
    if (stderr or "").strip():
        print("--- plugin stderr ---", file=sys.stderr)
        print(stderr.strip()[-1200:], file=sys.stderr)
    if not results:
        print(f"!! no JSON-RPC responses on stdout (returncode {returncode})", file=sys.stderr)
    return results


def main() -> int:
    tool = sys.argv[1] if len(sys.argv) > 1 else None
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "health"},
        {"jsonrpc": "2.0", "id": 3, "method": "describe"},
    ]
    if tool:
        reqs.append({"jsonrpc": "2.0", "id": 4, "method": "invoke",
                     "params": {"tool": tool, "arguments": args, "context": {}}})
    reqs.append({"jsonrpc": "2.0", "id": 99, "method": "shutdown"})

    for resp in run(reqs):
        rid = resp.get("id")
        body = resp.get("result", resp.get("error"))
        if rid == 1:
            print(f"initialize: {body.get('serverInfo', {}).get('name')} "
                  f"protocol v{body.get('protocolVersion')} caps={list((body.get('capabilities') or {}).keys())}")
        elif rid == 3:
            names = [t["name"] for t in body.get("tools", [])]
            print(f"protocol {body.get('protocolVersion')} | tools({len(names)}): {', '.join(names)}")
        elif rid == 2:
            eda = body.get("eda_backend") or {}
            print(f"health: {body.get('status')} | pcbnew={eda.get('pcbnew')} "
                  f"kicad-cli={'yes' if eda.get('kicad_cli') else 'no'} degraded={body.get('degraded')}")
        else:
            print(json.dumps(body, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
