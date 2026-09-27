import os
import json
import sys
sys.path.insert(0, "/home/assalas/Downloads/pcb-designer-ai-agent/anna-app/executas/pcb-designer")
import plugin
from test_harness import AnnaLocalHarness

def main():
    print("Testing PCB Designer Agent with Gemini API...")
    
    os.environ["PCB_AI_LLM_PROVIDER"] = "gemini"
    # Ensure they have the API key
    if not os.environ.get("GEMINI_API_KEY"):
        print("Please set export GEMINI_API_KEY='your_api_key' before running this script.")
        sys.exit(1)
        
    harness = AnnaLocalHarness(mock_sampling=False) # Use real LLM provider
    
    prompt = "Design a 2-layer board, 40mm x 30mm, for an ESP32-C3-MINI-1 module. Power it from a USB-C connector through an AP2112K-3.3 LDO. Add a BME280 sensor, status LED, BOOT and RESET buttons, four M2 mounting holes, and an antenna copper keepout."
    
    print(f"Running full_pipeline with prompt:\n{prompt}\n")
    
    resp = harness.invoke_tool("full_pipeline", {"description": prompt})
    
    if resp["result"].get("success"):
        data = resp["result"]["data"]
        print("Pipeline succeeded!")
        print("\n--- Generated Engineering Report (via Gemini) ---")
        print(data.get("analysis_report", "No report generated."))
        print("\n-------------------------------------------------")
        print(f"Generated {len(data.get('bom', []))} BOM components.")
    else:
        print("Pipeline failed:", resp["result"].get("error"))

if __name__ == "__main__":
    main()
