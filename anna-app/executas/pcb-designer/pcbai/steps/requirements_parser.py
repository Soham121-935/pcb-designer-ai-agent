from __future__ import annotations

import json
import re
from typing import Dict

from pcbai.core.logger import get_logger
from pcbai.llm.provider import get_provider

logger = get_logger("pcbai.requirements")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(raw: str) -> Dict:
    """Pull the first JSON object from a model reply (handles prose and ``` fences)."""
    text = (raw or "").strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in model response: {text[:120]!r}")
    return json.loads(text[start : end + 1])


SYSTEM_PROMPT = """\
You are a hardware engineering assistant. Extract structured component requirements from a natural language description.
Return ONLY a valid JSON object with this schema:
{
  "keywords": ["list", "of", "component", "types"],
  "voltage": "string or null",
  "current": "string or null",
  "connectivity": ["wifi", "bluetooth", etc.],
  "mcu": "preferred MCU family or null",
  "notes": "any extra constraints"
}

Component keyword examples: mcu, buck, lipo, usb, wifi, bluetooth, adc, opamp, led, relay, sensor, motor, display.
"""


def parse_requirements(natural_text: str) -> Dict:
    """Use LLM to extract structured requirements from a natural language description.

    Falls back to keyword matching if the LLM is unavailable.
    """
    try:
        provider = get_provider()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": natural_text},
        ]
        raw = provider.chat(messages, temperature=0.1, max_tokens=300)

        # Extract JSON from response (handle markdown code fences)
        raw = raw.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = extract_json(raw)
        result.setdefault("notes", natural_text.strip())
        result["source"] = "llm"
        return result

    except Exception as e:
        # Fallback: simple keyword match
        logger.warning("LLM unavailable (%s); using keyword fallback", e)
        lower = natural_text.lower()
        keywords = [w for w in [
            "bluetooth", "wifi", "usb", "buck", "lipo", "mcu", "sd", "ldo", "esp32",
            "adc", "opamp", "led", "relay", "sensor", "motor", "display"
        ] if w in lower]
        return {"keywords": keywords, "notes": natural_text.strip(), "source": "keyword-fallback"}
