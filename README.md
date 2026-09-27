# PCB Designer AI Agent

End-to-End PCB Design Assistant powered by LLMs.

This is a **universal** AI hardware agent built in Python. It supports any major LLM provider (Claude, Gemini, OpenAI) as well as local models (Ollama, LM Studio) to autonomously generate schematics and PCB layouts from natural language.

*Note: We have also tested and ported this agent as a seamless, browser-based hardware design environment for **Anna OS**! (See deployment section below).* 

## How it works

The agent takes a natural-language description (e.g., "Design a board with an ESP32, an IMU sensor, a LiPo battery charger, a USB-C port, a 3.3V LDO, and an SD card slot"), and automatically:
1. Parses the requirements using an LLM.
2. Maps keywords to real physical components (BOM generation).
3. Synthesizes a schematic netlist.
4. Generates standard IPC footprint geometries (SOIC, LQFP, SOT-223, USB-C, etc.).
5. Renders a complete, routing-ready `.kicad_pcb` board file!

## Getting Started (Universal Usage)

The agent runs as a standalone JSON-RPC service. You can pipe a prompt directly to it.


### System Requirements (For Dynamic Routing)

If you are using the raw, dynamic LLM auto-routing tools, you **must** have KiCad installed on your system.
* KiCad 6.0 or newer
* The `pcbnew` Python bindings (usually located at `/usr/lib/kicad/lib/python3/dist-packages`)
* KiCad footprint libraries installed at `/usr/share/kicad/footprints/`

*Note: For the Anna OS cloud deployment, we bypass this requirement by bundling a pre-compiled template project directly into the PyInstaller executable.*

### 1. Configure your LLM Provider

The agent is model-agnostic. You must set environment variables to tell the agent which API to use. 

**To use Anthropic (Claude):**
```bash
export PCB_AI_LLM_PROVIDER=claude
export ANTHROPIC_API_KEY=your_api_key_here
export PCB_AI_MODEL=claude-3-5-sonnet-20240620
```

**To use Gemini:**
```bash
export PCB_AI_LLM_PROVIDER=gemini
export GEMINI_API_KEY=your_api_key_here
export PCB_AI_MODEL=gemini-3.6-flash
```

**To use OpenAI:**
```bash
export PCB_AI_LLM_PROVIDER=openai
export OPENAI_API_KEY=your_api_key_here
export PCB_AI_MODEL=gpt-4o-mini
```

**To use Local Models (LM Studio / Ollama):**
```bash
export PCB_AI_LLM_PROVIDER=lmstudio  # or ollama
# Ensure your local server is running on port 1234 (LM Studio) or 11434 (Ollama)
```

### 2. Run the Agent Locally

You have several ways to test the agent locally depending on what you want to evaluate:

**A. Test the Anna App Pipeline (Static PCB Template + LLM Report)**
Because Anna OS cloud sandboxes lack KiCad dependencies, the main `full_pipeline` copies a pre-generated, perfectly routed ESP32-C3 PCB template, but uses the LLM to generate the final Engineering Analysis Report.
```bash
python3 gemini_test.py
```

**B. Test the Raw AI Tools (Dynamic Pipeline)**
If you want to see the LLM's true intelligence in breaking down requirements and generating a dynamic BOM/Netlist:
```bash
python3 gemini_e2e.py
```

**C. RPC Testing**
You can interact with the agent using the provided `test_rpc.py` script:
```bash
python3 test_rpc.py
```

This will execute the agent pipeline end-to-end and output the generated BOM, Netlist, and Board files in JSON format!

## Anna OS Integration

While the agent is completely universal, it can also be deployed seamlessly to the Anna OS App Store.

```bash
cd anna-app
anna-app dev
```
This spins up a local UI at `http://localhost:5173`. When deployed this way, if no API keys are provided, it can gracefully fallback to using Anna OS quota/tokens.

## Running the Reef Evaluation Harness

We include a local testing harness powered by [Reef](https://github.com/reef-ai) to evaluate how well different LLMs extract footprint package parameters from PDF datasheets.

```bash
cd reef_harness
reef serve -c serve.yaml
```

## Contributing

PRs welcome. Priority areas:
- Datasheet parsers and CV feature extractors
- SKiDL schematic reference circuit templates
- Freerouting DSN/SES integration

## License

Dual-licensed:
- Non-commercial, open-source use granted under the Custom License in `LICENSE`.
- Commercial/enterprise use requires prior written authorisation from the author. Contact: assalas@tutamail.com.
