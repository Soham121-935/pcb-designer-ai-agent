from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _repo_root() -> str:
    """Repo root, discovered once (works for both the installed package and a source checkout)."""
    env = os.getenv("PCB_AI_REPO_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))
    # <root>/anna-app/executas/pcb-designer/pcbai/core  ->  up 5
    return os.path.abspath(os.path.join(here, *([".."] * 5)))


@dataclass
class Settings:
    # General
    log_level: str = os.getenv("PCB_AI_LOG", "INFO")

    # LLM
    llm_provider: str = os.getenv("PCB_AI_LLM_PROVIDER", "openai")
    openai_api_key: Optional[str] = os.getenv("OPENAI_API_KEY")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    #: Blocking ceiling for the Anna `sampling/createMessage` reverse-RPC (seconds).
    sample_timeout_s: float = float(os.getenv("PCB_AI_SAMPLE_TIMEOUT", "60"))

    # Paths
    workdir: str = os.getenv("PCB_AI_WORKDIR", os.path.join(_repo_root(), "build"))

    # EDA tool backends (all optional — see pcbai/eda/backend.py)
    kicad_cli: str = os.getenv("PCB_AI_KICAD_CLI") or os.getenv("KICAD_CLI", "kicad-cli")
    kicad_lib_path: Optional[str] = os.getenv("PCB_AI_KICAD_LIB_PATH")
    kicad_footprint_dir: Optional[str] = os.getenv("PCB_AI_KICAD_FOOTPRINT_DIR")
    freerouting_jar: Optional[str] = os.getenv("PCB_AI_FREEROUTING_JAR")
    altium_api_key: Optional[str] = os.getenv("ALTIUM_API_KEY")
    cadence_api_key: Optional[str] = os.getenv("CADENCE_API_KEY")

    # Behaviour switches
    #: Legacy compatibility: `full_pipeline` copies the shipped ESP32-C3 template when the real
    #: (KiCad-backed) board generator cannot run. Always reported as `template_only: true`.
    allow_template_copy: bool = _truthy(os.getenv("PCB_AI_ALLOW_TEMPLATE_COPY"), default=True)
    #: Experimental naive router — off by default, produces shorts.
    enable_experimental_router: bool = _truthy(os.getenv("PCB_AI_EXPERIMENTAL_ROUTER"), default=False)


settings = Settings()
