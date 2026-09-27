"""End-to-end design compilation: prompt → BOM → schematic → PCB → Gerbers → zip.

Audited behaviour (docs/AUDIT.md D1): ``compile_design`` ignored its ``prompt`` argument entirely and
returned a **copy of the shipped ESP32-C3 template** for every prompt whatsoever. That is now an
explicit, reported fallback rather than a silent lie:

* ``mode="generated"``  — the KiCad writers actually ran (requires ``pcbnew``; see pcbai/eda/backend.py)
* ``mode="template"``   — the ESP32-C3 reference template was copied because generation was not
                          possible **and** ``PCB_AI_ALLOW_TEMPLATE_COPY`` (default on) permits it.
                          Callers must surface ``template_only=True`` to the user.
* otherwise             — :class:`DesignNotGenerated` is raised with an actionable message.

The template path remains byte-for-byte compatible with the previous output so the existing Anna
UI keeps working (backward compatibility), while the API now says what really happened.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from typing import Dict, Optional

from pcbai.core.config import settings
from pcbai.core.logger import get_logger
from pcbai.eda import backend
from pcbai.steps.kicad_pcb_writer import generate_pcb
from pcbai.steps.kicad_schematic_writer import generate_schematic

logger = get_logger("pcbai.compiler")


class DesignNotGenerated(RuntimeError):
    """Raised when no board could be produced and template fallback is disabled."""

    def __init__(self, message: str, *, reason: str, detail: Optional[Dict] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}


def template_dir() -> str:
    if hasattr(sys, "_MEIPASS"):  # PyInstaller bundle (Anna binary distribution)
        return os.path.join(sys._MEIPASS, "pcbai", "steps", "template_project")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "template_project")


def template_available() -> bool:
    return os.path.isfile(os.path.join(template_dir(), "board.kicad_pcb"))


def _copy_template(output_dir: str) -> None:
    src = template_dir()
    for item in os.listdir(src):
        s, d = os.path.join(src, item), os.path.join(output_dir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def _zip_contents(output_dir: str, zip_path: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(output_dir):
            dirs[:] = [d for d in dirs if d not in (".history", "__pycache__")]
            for name in files:
                if name.endswith(".bak") or name == os.path.basename(zip_path):
                    continue
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, output_dir))


def compile_design(prompt: str, output_dir: str, *, allow_template_copy: Optional[bool] = None,
                   title: str = "PCB AI design") -> Dict:
    """Compile *prompt* into a project directory. Returns a dict describing what really happened.

    Keys: bom, sch, pcb, gerbers, zip, mode, template_only, warnings, backend.
    """
    if allow_template_copy is None:
        allow_template_copy = settings.allow_template_copy

    os.makedirs(output_dir, exist_ok=True)
    warnings: list[str] = []
    caps = backend.capabilities()
    mode = "generated"
    gen_detail: Dict[str, object] = {}

    bom_path = os.path.join(output_dir, "bom.json")
    sch_path = os.path.join(output_dir, "schematic.kicad_sch")
    pcb_path = os.path.join(output_dir, "board.kicad_pcb")
    gerbers_dir = os.path.join(output_dir, "gerbers")
    zip_path = os.path.join(output_dir, "pcb_project.zip")

    # ── 1. requirements + BOM (the one genuinely prompt-dependent stage) ────
    bom: list = []
    try:
        from pcbai.steps.requirements_parser import parse_requirements
        from pcbai.steps.bom_generator import generate_bom

        reqs = parse_requirements(prompt or "")
        bom = generate_bom(reqs) if reqs else []
    except Exception as exc:  # LLM offline is not fatal for the template path
        warnings.append(f"BOM stage degraded: {type(exc).__name__}: {exc}")
        reqs = {"keywords": [], "notes": (prompt or "").strip()}

    if bom:
        with open(bom_path, "w", encoding="utf-8") as fh:
            json.dump(bom, fh, indent=2)

    # ── 2. real board generation (KiCad writers) ─────────────────────────────
    if caps.can_author_native:
        try:
            generate_schematic(sch_path)
            ok_pcb = generate_pcb(pcb_path, project_name=os.path.basename(output_dir))
            gen_detail = {"schematic": sch_path, "pcb": pcb_path, "pcb_writer_ok": bool(ok_pcb)}
            if not ok_pcb:
                raise RuntimeError("kicad_pcb_writer.generate_pcb() returned False")
            warnings.append(
                "board was produced by the ESP32-C3 reference generator, which is hardcoded to that "
                "design; it is NOT derived from the prompt (generic generation lands in Phase 4/5)"
            )
            if not os.path.isdir(gerbers_dir):
                from pcbai.steps.gerber_exporter import export_gerbers
                export_gerbers(pcb_path, gerbers_dir)
        except Exception as exc:
            mode = "failed"
            gen_detail = {"error": f"{type(exc).__name__}: {exc}"}
            logger.warning("native generation failed: %s", exc)
    else:
        mode = "failed"
        gen_detail = {"reason": "backend-unavailable", "capabilities": caps.to_dict()}

    # ── 3. documented template fallback (kept for UI/back-compat) ────────────
    if mode != "generated":
        if not (allow_template_copy and template_available()):
            raise DesignNotGenerated(
                "No board could be generated and the template fallback is disabled. "
                "Install KiCad (>=8) with python bindings for the native writers, or set "
                "PCB_AI_ALLOW_TEMPLATE_COPY=1 to receive the ESP32-C3 reference template explicitly.",
                reason=gen_detail.get("reason", "generation-failed"),
                detail=gen_detail,
            )
        _copy_template(output_dir)
        mode = "template"
        warnings.insert(0, (
            "TEMPLATE MODE: this project is a byte-for-byte copy of the shipped ESP32-C3 reference "
            "board and has NO relationship to the request text. Do not treat it as a design result."
        ))

    if not os.path.isfile(bom_path):
        with open(bom_path, "w", encoding="utf-8") as fh:
            json.dump(bom, fh, indent=2)
    _zip_contents(output_dir, zip_path)

    return {
        "bom": _load_bom(bom_path),
        "requirements": reqs,
        "sch": sch_path,
        "pcb": pcb_path,
        "gerbers": gerbers_dir,
        "zip": zip_path,
        "mode": mode,
        "template_only": mode == "template",
        "title": title,
        "warnings": warnings,
        "backend": caps.to_dict(),
        "generation": gen_detail,
    }


def _load_bom(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else data.get("bom", [])
    except Exception:
        return []
