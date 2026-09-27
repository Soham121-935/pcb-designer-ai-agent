#!/usr/bin/env python3
"""Exercise the raw LLM-driven tools (requirements → BOM) against a live provider.

    export GEMINI_API_KEY=...
    python3 scripts/gemini_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _demo_common import PLUGIN_DIR, setup  # noqa: E402


def main() -> int:
    provider = os.environ.get("PCB_AI_LLM_PROVIDER", "gemini")
    setup(provider)
    sys.path.insert(0, str(PLUGIN_DIR))
    from test_harness import AnnaLocalHarness  # noqa: E402

    harness = AnnaLocalHarness(mock_sampling=False)
    rc = 0
    try:
        prompt = "Design a 2-layer board for an ESP32-C3-MINI-1 module with USB-C power."
        print(f"[e2e] provider={provider}\n--- parse_requirements({prompt!r}) ---")
        resp = harness.invoke_tool("parse_requirements", {"description": prompt}, timeout=180)
        result = resp.get("result", {})
        print(json.dumps(result, indent=2)[:1200])
        if not result.get("success"):
            print("!! parse_requirements failed")
            return 1
        reqs = result.get("data", {})
        if reqs.get("fallback"):
            print("!! LLM path failed; keyword fallback used (see plugin stderr)")

        print("\n--- generate_bom ---")
        resp = harness.invoke_tool("generate_bom", {"requirements_json": json.dumps(reqs)}, timeout=60)
        result = resp.get("result", {})
        bom = result.get("data", {}).get("bom", [])
        print(json.dumps(bom, indent=2)[:1500])
        if not result.get("success") or not bom:
            print("!! BOM empty — no keyword matched the catalog")
            rc = 1
    finally:
        harness.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
