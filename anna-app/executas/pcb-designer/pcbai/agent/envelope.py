"""Shared result envelope for every tool handler.

Kept in its own module so both :mod:`pcbai.agent.tools` and :mod:`pcbai.agent.kicad_tools` can use it
without importing each other (the tool registry is allowed to load either one first).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from pcbai.core.config import settings

def ok(data: Any, warnings: Optional[List[str]] = None, **extra) -> Dict[str, Any]:
    """Success envelope. `warnings` is always present (possibly []) so callers never KeyError."""
    out: Dict[str, Any] = {"success": True, "data": data, "error": None, "warnings": list(warnings or [])}
    out.update(extra)
    return out


def fail(error: str, reason: str = "error", data: Any = None,
         warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"success": False, "data": data, "error": str(error), "reason": reason,
            "warnings": list(warnings or [])}


def project_root(explicit: Optional[str] = None) -> str:
    """Directory the agent is currently working in.

    Priority: explicit argument → $PCB_AI_PROJECT → $PCB_AI_WORKDIR/demo-project.
    Real work (e.g. the SV-16 board) must be pointed at a checked-in project dir with
    PCB_AI_PROJECT; generated scratch then stays under build/ instead of polluting the repo.
    """
    root = explicit or os.getenv("PCB_AI_PROJECT") or os.path.join(settings.workdir, "demo-project")
    os.makedirs(root, exist_ok=True)
    return os.path.abspath(root)




__all__ = ["ok", "fail", "project_root"]
