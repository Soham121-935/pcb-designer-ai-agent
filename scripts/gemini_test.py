#!/usr/bin/env python3
"""full_pipeline against a live LLM — prints the report AND what the pipeline really did.

    export GEMINI_API_KEY=...   # or PCB_AI_LLM_PROVIDER=openai / claude + its key
    python3 scripts/gemini_test.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _demo_common import PLUGIN_DIR, PROMPT, setup  # noqa: E402


def main() -> int:
    provider = os.environ.get("PCB_AI_LLM_PROVIDER", "gemini")
    setup(provider)
    sys.path.insert(0, str(PLUGIN_DIR))
    from test_harness import AnnaLocalHarness  # noqa: E402

    print(f"[demo] provider={provider} — running full_pipeline with:\n{PROMPT}\n")
    harness = AnnaLocalHarness(mock_sampling=False)
    try:
        resp = harness.invoke_tool("full_pipeline", {"description": PROMPT}, timeout=240)
        result = resp.get("result", {})
        if not result.get("success"):
            print("Pipeline reported failure (this is the honest outcome):")
            print(json.dumps(result, indent=2)[:1500])
            return 1
        data = result.get("data", {})
        mode = data.get("mode", "unknown")
        print(f"Build mode: {mode}  (template_only={data.get('template_only')})")
        for w in data.get("warnings", []):
            print(f"  warning: {w}")
        report = data.get("analysis_report")
        print("\n--- Engineering report ---")
        print(report if report else "(no report generated)")
        print("--- End report ---")
        print(f"BOM components: {len(data.get('bom', []))}")
        if mode == "template":
            print("\nNOTE: no design was produced from the prompt — the shipped ESP32-C3 reference "
                  "board was copied. Generic board generation arrives with the PCB model (Phase 4/5).")
        return 0
    finally:
        harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
