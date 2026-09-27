#!/usr/bin/env python3
"""Shared plumbing for the live-LLM demos (the previous copies hardcoded the author's home dir)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "anna-app" / "executas" / "pcb-designer"


def setup(provider: str = "gemini") -> None:
    """Make plugin.py / test_harness.py importable and pick a live LLM provider."""
    sys.path.insert(0, str(PLUGIN_DIR))
    os.environ.setdefault("PCB_AI_LLM_PROVIDER", provider)
    key = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
           "claude": "ANTHROPIC_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider)
    if key and not os.environ.get(key):
        sys.exit(f"Please set export {key}='your_api_key' before running this script.")


PROMPT = (
    "Design a 2-layer board, 40mm x 30mm, for an ESP32-C3-MINI-1 module. Power it from a USB-C "
    "connector through an AP2112K-3.3 LDO. Add a BME280 sensor, status LED, BOOT and RESET buttons, "
    "four M2 mounting holes, and an antenna copper keepout."
)
