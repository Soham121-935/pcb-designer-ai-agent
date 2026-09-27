import os
import json
import sys
sys.path.insert(0, "/home/assalas/Downloads/pcb-designer-ai-agent/anna-app/executas/pcb-designer")
from test_harness import AnnaLocalHarness

def main():
    print("Testing PCB Designer Agent E2E with Gemini API...")
    
    os.environ["PCB_AI_LLM_PROVIDER"] = "gemini"
    if not os.environ.get("GEMINI_API_KEY"):
        print("Please set export GEMINI_API_KEY='your_api_key' before running this script.")
        sys.exit(1)
        
    # Set mock_sampling=False so it hits the REAL Gemini API via plugin's get_provider() instead of the mock harness
    harness = AnnaLocalHarness(mock_sampling=False)
    
    print("\n--- Testing Parse Requirements ---")
    prompt = "Design a 2-layer board for an ESP32-C3-MINI-1 module with USB-C power."
    resp = harness.invoke_tool("parse_requirements", {"description": prompt})
    reqs = resp["result"]["data"]
    print(json.dumps(reqs, indent=2))
    
    print("\n--- Testing Generate BOM ---")
    resp = harness.invoke_tool("generate_bom", {"requirements_json": json.dumps(reqs)})
    bom = resp["result"]["data"]["bom"]
    print(json.dumps(bom, indent=2))
    
    harness.close()

if __name__ == "__main__":
    main()
