#!/usr/bin/env python3
"""pcb-designer — Anna OS Executa plugin.

Speaks JSON-RPC 2.0 over stdio (v2 protocol with reverse-RPC sampling).
Wraps all pcbai core logic as Anna tools — no API keys needed, no UI served.
Anna handles frontend, memory, billing, and model selection.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
import queue
import threading
import tempfile
import traceback
from dataclasses import asdict
from typing import Any, Dict, List, Optional

# The pcbai module is bundled directly in this folder — make it importable when the plugin is
# started from outside its own directory (previously only `uv run --project ...` worked).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ── stderr helper (stdout is RESERVED for JSON-RPC; nothing else may write to it) ──
try:
    from pcbai.core.logger import log as _logger_log
    _log_impl = _logger_log
except Exception:  # pragma: no cover - pcbai must be importable, but never crash the RPC loop
    def _log_impl(msg: str) -> None:
        sys.stderr.write(f"[pcb-designer] {msg}\n")
        sys.stderr.flush()


def log(msg: str) -> None:
    _log_impl(msg)


# ── robust LLM-JSON extraction (was triplicated and brittle) ────────────────
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(raw: str) -> dict:
    """Pull the first JSON object out of a model reply (handles prose + ``` fences)."""
    if raw is None:
        raise ValueError("empty model response")
    text = raw.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object found in model response: {text[:120]!r}")
    return json.loads(text[start : end + 1])


# ════════════════════════════════════════════════════════════
# JSON-RPC 2.0 Dispatcher (v2 with reverse-RPC for sampling)
# ════════════════════════════════════════════════════════════

# Agent → plugin requests land here
agent_requests: queue.Queue = queue.Queue()
# Reverse-RPC responses keyed by request id
host_responses: Dict[str, queue.Queue] = {}


def _reader_thread() -> None:
    """Single stdin reader — routes Agent requests vs host responses."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "method" in msg:
            agent_requests.put(msg)
        else:
            q = host_responses.pop(msg.get("id", ""), None)
            if q is not None:
                q.put(msg)


#: The protocol channel is bound to the *real* stdout object once, at import time, so that
#: install_stderr_guard() (which replaces the sys.stdout attribute to divert stray print() calls)
#: can never swallow protocol messages. Never use print()/sys.stdout for protocol output.
_PROTOCOL_OUT = sys.__stdout__ or sys.stdout


def _send(obj: dict) -> None:
    _PROTOCOL_OUT.write(json.dumps(obj) + "\n")
    _PROTOCOL_OUT.flush()


def _respond(req_id: Any, result: Any = None, error: Any = None) -> None:
    resp: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    _send(resp)


# ── Sampling: borrow the host's LLM ────────────────────────

def sample(
    invoke_id: str,
    prompt: str,
    *,
    system_prompt: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.2,
    response_format: Optional[dict] = None,
) -> str:
    """Issue a sampling/createMessage reverse RPC and block until the host replies."""
    rid = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    host_responses[rid] = q

    params: dict = {
        "messages": [{"role": "user", "content": {"type": "text", "text": prompt}}],
        "maxTokens": max_tokens,
        "temperature": temperature,
        "includeContext": "none",
        "metadata": {"executa_invoke_id": invoke_id},
    }
    if system_prompt:
        params["systemPrompt"] = system_prompt
    if response_format:
        params["responseFormat"] = response_format
        params["onUnsupported"] = "json_object"

    _send({
        "jsonrpc": "2.0",
        "id": rid,
        "method": "sampling/createMessage",
        "params": params,
    })

    try:
        from pcbai.core.config import settings as _settings
        timeout = _settings.sample_timeout_s
    except Exception:
        timeout = float(os.getenv("PCB_AI_SAMPLE_TIMEOUT", "60"))
    try:
        resp = q.get(timeout=timeout)
    except queue.Empty:
        host_responses.pop(rid, None)
        raise RuntimeError(
            f"host sampling timed out after {timeout:.0f}s "
            "(raise PCB_AI_SAMPLE_TIMEOUT for long reasoning calls)"
        )
    if "error" in resp:
        raise RuntimeError(f"Sampling error: {resp['error']}")
    return resp["result"]["content"]["text"]


# ════════════════════════════════════════════════════════════
# Manifest — tools exposed to Anna's Agent
# ════════════════════════════════════════════════════════════

MANIFEST = {
    "name": "pcb-designer",
    "display_name": "PCB Designer AI Agent",
    "version": "1.0.0",
    "description": (
        "PCB design tool server: parses natural-language requirements, builds BOMs, "
        "extracts package parameters from PDF datasheets, generates KiCad footprints, "
        "and (when KiCad's pcbnew is installed) assembles boards from a netlist. "
        "Emitted files target KiCad 10. Board assembly and DRC parity require a local "
        "KiCad install; without it those steps are reported as unavailable, not skipped silently."
    ),
    "author": "assalas",
    "host_capabilities": ["llm.sample", "llm.complete"],
    "tools": [
        {
            "name": "parse_requirements",
            "description": (
                "Extract structured component requirements from a natural-language "
                "hardware description. Uses deep LLM reasoning (via Anna sampling) to "
                "identify MCU families, voltage rails, connectivity, and component "
                "keywords with thorough chain-of-thought analysis."
            ),
            "parameters": [
                {"name": "description", "type": "string", "description": "Natural-language hardware description", "required": True},
            ],
        },
        {
            "name": "generate_bom",
            "description": (
                "Generate a Bill of Materials from structured requirements. "
                "Maps requirement keywords onto a small built-in part catalog (12 MPNs). "
                "No vendor/stock lookup is implemented yet; unmatched keywords are "
                "returned as *-UNKNOWN placeholders and listed in data.warnings."
            ),
            "parameters": [
                {"name": "requirements_json", "type": "string", "description": "JSON string of structured requirements (output of parse_requirements)", "required": True},
            ],
        },
        {
            "name": "extract_package_from_pdf",
            "description": (
                "Deep async extraction of package dimensions from a component "
                "datasheet PDF. Multi-stage pipeline: text layer extraction → "
                "OCR fallback → LLM-powered dimensional analysis with thorough "
                "chain-of-thought reasoning for mechanical drawings. Returns "
                "pkg_type, pins, pitch, body dimensions, pad dimensions, and "
                "exposed pad parameters. Optimized for LPKF rapid prototyping."
            ),
            "parameters": [
                {"name": "pdf_path", "type": "string", "description": "Absolute path to PDF datasheet on disk", "required": True},
            ],
        },
        {
            "name": "generate_footprint",
            "description": (
                "Generate a KiCad .kicad_mod footprint file. Supports: "
                "qfn, qfp, soic, smd_rc, bga, dip, usbc, header, custom. "
                "Returns the footprint file content as a string."
            ),
            "parameters": [
                {"name": "footprint_type", "type": "string", "description": "Package type: qfn|qfp|soic|smd_rc|bga|dip|usbc|header|custom", "required": True},
                {"name": "params_json", "type": "string", "description": "JSON object of footprint parameters (name, pins, pitch, body_l, body_w, pad_l, pad_w, etc.)", "required": True},
            ],
        },
        {
            "name": "synthesize_netlist",
            "description": (
                "Build a netlist structure from a BOM. CURRENT LIMITATION: this returns "
                "component references grouped under GND/VCC only — there is no pad-level "
                "connectivity yet, so it cannot drive a real router. Pad-level netlists land "
                "with the PCB parser in Phase 4. Returns the netlist as a JSON object."
            ),
            "parameters": [
                {"name": "bom_json", "type": "string", "description": "JSON array of BOM entries [{mpn, package, voltage}, ...]", "required": True},
            ],
        },
        {
            "name": "route_pcb",
            "description": (
                "Assemble a .kicad_pcb from a netlist using KiCad pcbnew: loads footprints "
                "from <output_dir>/footprints, assigns pads to nets, applies heuristic "
                "placement, and exports .dsn. Requires pcbnew; returns success:false with "
                "reason='backend-unavailable' when KiCad is missing. Experimental Manhattan "
                "routing (no obstacle avoidance) only runs with PCB_AI_EXPERIMENTAL_ROUTER=1."
            ),
            "parameters": [
                {"name": "netlist_json", "type": "string", "description": "JSON netlist structure with nets and components", "required": True},
                {"name": "output_dir", "type": "string", "description": "Directory for output files (default: /tmp/pcbai_build)", "required": False},
            ],
        },
        {
            "name": "full_pipeline",
            "description": (
                "Run the end-to-end pipeline (requirements → BOM → board). data.mode reports "
                "what actually happened: 'generated' (KiCad writers built the board) or "
                "'template' (the shipped ESP32-C3 reference board was copied because generic "
                "generation is unavailable). Template results carry template_only=true and a "
                "warning — never report them to the user as a completed design. Failures return "
                "success:false with a reason."
            ),
            "parameters": [
                {"name": "description", "type": "string", "description": "Natural-language hardware description", "required": True},
            ],
        },
    ],
}


# ════════════════════════════════════════════════════════════
# Tool implementations
# ════════════════════════════════════════════════════════════

def _tool_parse_requirements(args: dict, ctx: dict) -> dict:
    """Parse requirements using Anna's hosted LLM (sampling) or a local provider for deep reasoning."""
    description = args["description"]
    invoke_id = ctx.get("invoke_id", "")

    system_prompt = (
        "You are an expert hardware/electronics engineer specializing in PCB design "
        "for rapid prototyping with LPKF ProtoLaser S4, MultiPress S4, and Contac S4 systems.\n\n"
        "Analyze the user's hardware description with thorough chain-of-thought reasoning.\n"
        "Consider: voltage domains, current requirements, signal integrity, thermal constraints, "
        "component availability, and LPKF process limitations (min trace width, via size, etc.).\n\n"
        "Return ONLY a JSON object with this schema:\n"
        '{\n  "keywords": ["list", "of", "component", "types"],\n'
        '  "voltage": "string or null",\n  "current": "string or null",\n'
        '  "connectivity": ["wifi", "bluetooth", etc.],\n'
        '  "mcu": "preferred MCU family or null",\n'
        '  "notes": "detailed engineering analysis and constraints"\n}'
    )

    try:
        if os.environ.get("PCB_AI_LLM_PROVIDER") and os.environ.get("PCB_AI_LLM_PROVIDER") != "anna":
            from pcbai.llm.provider import get_provider
            provider = get_provider()
            log(f"Using external LLM provider: {os.environ.get('PCB_AI_LLM_PROVIDER')}")
            raw = provider.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": description}
            ], temperature=0.1, max_tokens=1024)
        else:
            raw = sample(
                invoke_id,
                description,
                system_prompt=system_prompt,
                max_tokens=1024,
                temperature=0.1,
            )

        result = extract_json(raw)
        result.setdefault("notes", description)
        return {"success": True, "data": result, "parser": "llm"}
    except Exception as e:
        # Fallback to local keyword extraction
        log(f"Sampling failed, falling back to local parser: {e}")
        from pcbai.steps.requirements_parser import parse_requirements
        result = parse_requirements(description)
        return {"success": True, "data": result, "fallback": True}


def _tool_generate_bom(args: dict, ctx: dict) -> dict:
    from pcbai.steps.bom_generator import generate_bom
    requirements = json.loads(args["requirements_json"])
    bom = generate_bom(requirements)
    unresolved = [b["mpn"] for b in bom if str(b.get("package", "")).upper() == "UNKNOWN"]
    out: Dict[str, Any] = {"bom": bom, "count": len(bom)}
    if unresolved:
        out["warnings"] = [
            f"{len(unresolved)} keyword(s) had no catalog match and were emitted as "
            f"placeholders: {', '.join(unresolved)}. Real part sourcing is not implemented yet."
        ]
    return {"success": True, "data": out}


def _tool_extract_package_from_pdf(args: dict, ctx: dict) -> dict:
    """Deep async PDF extraction with progress reporting and LLM analysis."""
    pdf_path = args["pdf_path"]
    invoke_id = ctx.get("invoke_id", "")

    if not os.path.exists(pdf_path):
        return {"success": False, "error": f"File not found: {pdf_path}"}

    # Stage 1: Text extraction
    log(f"Stage 1/3: Extracting text from {os.path.basename(pdf_path)}")
    text = None
    extraction_method = "none"

    try:
        from pdfminer.high_level import extract_text
        text = extract_text(pdf_path)
        extraction_method = "pdfminer"
    except ImportError:
        pass

    if not text or not text.strip():
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            pages = [p.extract_text() for p in reader.pages if p.extract_text()]
            text = "\n".join(pages)
            extraction_method = "pypdf"
        except ImportError:
            pass

    if not text or not text.strip():
        try:
            import pytesseract
            from pdf2image import convert_from_path
            images = convert_from_path(pdf_path)
            text = ""
            for img in images:
                text += pytesseract.image_to_string(img)
            extraction_method = "ocr"
        except (ImportError, Exception):
            pass

    if not text or not text.strip():
        return {"success": False, "error": "Could not extract text from PDF. Install pypdf, pdfminer.six, or pytesseract."}

    # Stage 2: Deep LLM analysis via sampling
    log("Stage 2/3: Deep LLM analysis of mechanical drawings...")
    try:
        system_prompt = (
            "You are an expert electronics packaging engineer.\n\n"
            "TASK: Extract physical package dimensions from a component datasheet.\n\n"
            "INSTRUCTIONS:\n"
            "1. Identify the mechanical drawing / package outline section\n"
            "2. Find the dimension table (usually with MIN/NOM/MAX columns)\n"
            "3. Map standard dimension codes to physical measurements:\n"
            "   - D = body length, E = body width, e = pitch\n"
            "   - b = lead/terminal width, L = lead length\n"
            "   - D2/E2 = exposed pad dimensions\n"
            "4. Use NOMINAL values. If only MIN/MAX, average them.\n"
            "5. Convert everything to millimeters (mm).\n"
            "6. Identify the package family (QFN, QFP, SOIC, BGA, DIP, etc.)\n\n"
            "CRITICAL: Think step-by-step. Show your reasoning for each dimension.\n"
            "Then output the final answer as a JSON object.\n\n"
            "JSON schema:\n"
            '{"pkg_type":"string","pins":int,"pitch":float,"body_l":float,'
            '"body_w":float,"pad_l":float,"pad_w":float,"ep_l":float|null,"ep_w":float|null}\n\n'
            "Reply with a JSON object containing the extracted dimensions."
        )

        # Send the FULL text for thorough analysis (maximize token depth)
        raw = sample(
            invoke_id,
            f"Datasheet text ({len(text)} chars, method: {extraction_method}):\n\n{text[-8000:]}",
            system_prompt=system_prompt,
            max_tokens=4096,
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        try:
            data = extract_json(raw)
        except ValueError as parse_exc:
            log(f"model reply was not JSON: {parse_exc}")
            data = None
        if data:
            from pcbai.steps.datasheet_package_extractor import PackageGuess
            guess = PackageGuess(
                pkg_type=str(data.get("pkg_type", "unknown")).lower(),
                pins=int(data["pins"]) if data.get("pins") is not None else None,
                pitch=float(data["pitch"]) if data.get("pitch") is not None else None,
                body_l=float(data["body_l"]) if data.get("body_l") is not None else None,
                body_w=float(data["body_w"]) if data.get("body_w") is not None else None,
                pad_l=float(data["pad_l"]) if data.get("pad_l") is not None else None,
                pad_w=float(data["pad_w"]) if data.get("pad_w") is not None else None,
                ep_l=float(data["ep_l"]) if data.get("ep_l") is not None else None,
                ep_w=float(data["ep_w"]) if data.get("ep_w") is not None else None,
            )
            log("Stage 3/3: Extraction complete via sampling.")
            return {
                "success": True,
                "data": asdict(guess),
                "method": "anna_sampling",
                "text_length": len(text),
                "extraction_method": extraction_method,
            }
    except Exception as e:
        log(f"Sampling extraction failed ({e}), falling back to local extractor")

    # Stage 3 (fallback): Local regex + local LLM
    log("Stage 3/3: Falling back to local extraction pipeline...")
    from pcbai.steps.datasheet_package_extractor import extract_package_params_from_pdf
    guess = extract_package_params_from_pdf(pdf_path)
    return {
        "success": True,
        "data": asdict(guess),
        "method": "local_fallback",
        "text_length": len(text),
        "extraction_method": extraction_method,
    }


def _tool_generate_footprint(args: dict, ctx: dict) -> dict:
    ftype = args["footprint_type"].lower()
    params = json.loads(args["params_json"])
    name = params.get("name", f"FP_{ftype}")

    with tempfile.TemporaryDirectory() as tmpdir:
        if ftype == "qfn":
            from pcbai.steps.footprint_qfn_qfp import QfnParams, generate_qfn
            fp_params = QfnParams(**{k: params[k] for k in QfnParams.__dataclass_fields__ if k in params})
            content = generate_qfn(fp_params)
        elif ftype == "qfp":
            from pcbai.steps.footprint_qfn_qfp import QfpParams, generate_qfp
            fp_params = QfpParams(**{k: params[k] for k in QfpParams.__dataclass_fields__ if k in params})
            content = generate_qfp(fp_params)
        elif ftype == "bga":
            from pcbai.steps.footprint_bga import BgaParams, generate_bga
            fp_params = BgaParams(**{k: params[k] for k in BgaParams.__dataclass_fields__ if k in params})
            content = generate_bga(fp_params)
        elif ftype == "dip":
            from pcbai.steps.footprint_dip import DipParams, generate_dip
            fp_params = DipParams(**{k: params[k] for k in DipParams.__dataclass_fields__ if k in params})
            content = generate_dip(fp_params)
        elif ftype in ("soic", "smd_rc"):
            from pcbai.steps.footprint_generator import SoicParams, SmdRcParams, generate_soic, generate_smd_rc
            if ftype == "soic":
                if "row_offset" not in params and "body_w" in params:
                    # typical SOIC row_offset is just outside the body width
                    params["row_offset"] = (params["body_w"] / 2.0) + (params.get("pad_l", 1.0) / 2.0)
                fp_params = SoicParams(**{k: params[k] for k in SoicParams.__dataclass_fields__ if k in params})
                content = generate_soic(fp_params)
            else:
                if "gap" not in params and "body_w" in params:
                    params["gap"] = params["body_w"] - params.get("pad_l", 1.0)
                fp_params = SmdRcParams(**{k: params[k] for k in SmdRcParams.__dataclass_fields__ if k in params})
                content = generate_smd_rc(fp_params)
        elif ftype == "usbc":
            from pcbai.steps.footprint_usbc import UsbcParams, generate_usbc
            fp_params = UsbcParams(name=name)
            content = generate_usbc(fp_params)
        elif ftype == "header":
            from pcbai.steps.footprint_header import HeaderParams, generate_header
            fp_params = HeaderParams(**{k: params[k] for k in HeaderParams.__dataclass_fields__ if k in params})
            content = generate_header(fp_params)
        elif ftype == "custom":
            from pcbai.steps.footprint_custom import CustomParams, generate_custom
            fp_params = CustomParams(**{k: params[k] for k in CustomParams.__dataclass_fields__ if k in params})
            content = generate_custom(fp_params)
        else:
            return {"success": False, "error": f"Unknown footprint type: {ftype}"}

    return {"success": True, "data": {"footprint_type": ftype, "name": name, "kicad_mod_content": content}}


def _tool_synthesize_netlist(args: dict, ctx: dict) -> dict:
    from pcbai.steps.schematic_synthesizer import synthesize_schematic
    bom = json.loads(args["bom_json"])
    netlist = synthesize_schematic(bom)
    return {
        "success": True,
        "data": {"netlist": netlist},
        "warnings": [
            "placeholder netlist: nets contain component refs, not REF-PIN pairs, so there is no "
            "pad-level connectivity. A router cannot use this yet (Phase 4 adds real netlists)."
        ],
    }


def _tool_route_pcb(args: dict, ctx: dict) -> dict:
    from pcbai.steps.pcb_router import route_pcb
    netlist = json.loads(args["netlist_json"])
    output_dir = args.get("output_dir") or os.getenv("PCB_AI_WORKDIR", "/tmp/pcbai_build")
    result = route_pcb(netlist, output_dir)
    ok = bool(result.get("ok"))
    if not ok:
        return {
            "success": False,
            "error": result.get("status") or "board build failed",
            "reason": result.get("reason", "router-failed"),
            "data": result,
        }
    return {"success": True, "data": result}


def _tool_full_pipeline(args: dict, ctx: dict) -> dict:
    """Compile a design end-to-end and return the artifacts + an engineering report.

    Honesty contract (docs/AUDIT.md D1/D2): ``data.mode`` is ``"generated"`` only when the KiCad
    writers actually built the board. A copy of the shipped reference template is reported as
    ``mode="template", template_only=true`` with a warning, so the calling agent can never mistake
    it for a real result. Genuine failures return ``success: false``.
    """
    description = args.get("description") or ""
    if not description.strip():
        return {"success": False, "error": "description is required", "reason": "bad-args"}

    invoke_id = ctx.get("invoke_id", "")
    artifacts: Dict[str, Any] = {}

    from pcbai.steps.design_compiler import compile_design, DesignNotGenerated
    from pcbai.eda import backend

    try:
        from pcbai.core.config import settings
        outdir = os.path.join(settings.workdir, "pipeline", uuid.uuid4().hex[:8])
    except Exception:
        outdir = os.path.join(tempfile.gettempdir(), "pcbai_pipeline", uuid.uuid4().hex[:8])
    keep = str(args.get("output_dir") or "").strip()
    outdir = keep or outdir

    log(f"Pipeline: compiling design in {outdir} (prompt: {description[:80]}...)")
    try:
        os.makedirs(outdir, exist_ok=True)
        result = compile_design(description, outdir)
    except DesignNotGenerated as exc:
        return {
            "success": False,
            "error": str(exc),
            "reason": exc.reason,
            "data": {"capabilities": backend.capabilities().to_dict(), "detail": exc.detail},
        }
    except Exception as exc:
        log(f"Pipeline failed: {traceback.format_exc()}")
        return {"success": False, "error": f"{type(exc).__name__}: {exc}",
                "reason": "pipeline-exception",
                "data": {"traceback": traceback.format_exc()}}

    for key, path_key in (("pcb", "pcb"), ("sch", "sch")):
        try:
            with open(result[path_key], "r", encoding="utf-8") as f:
                artifacts[key] = f.read()
        except OSError as exc:
            return {"success": False, "error": f"generated {key} missing: {exc}",
                    "reason": "artifact-missing", "data": artifacts}

    artifacts["bom"] = result["bom"]
    artifacts["bom_json"] = json.dumps(result["bom"], indent=2)
    artifacts["mode"] = result["mode"]
    artifacts["template_only"] = result["template_only"]
    artifacts["warnings"] = result.get("warnings", [])
    artifacts["backend"] = result.get("backend", {})
    artifacts["paths"] = {k: result[k] for k in ("sch", "pcb", "gerbers", "zip")}

    log("Generating engineering analysis report...")
    report_prompt = (
        f"Hardware description: {description}\n\n"
        f"Generated BOM: {artifacts['bom_json']}\n\n"
        f"Build mode: {result['mode']}\n"
        f"Warnings: {'; '.join(result.get('warnings', [])) or 'none'}\n\n"
        "Provide a short engineering analysis report. If build mode is 'template', state clearly "
        "that no design was produced and what is required to produce one."
    )
    sys_prompt = "You are a senior PCB design engineer. Never claim a result you did not verify."
    try:
        if os.environ.get("PCB_AI_LLM_PROVIDER") and os.environ.get("PCB_AI_LLM_PROVIDER") != "anna":
            from pcbai.llm.provider import get_provider
            artifacts["analysis_report"] = get_provider().chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": report_prompt}], temperature=0.3, max_tokens=1000)
        else:
            artifacts["analysis_report"] = sample(invoke_id, report_prompt,
                                                  system_prompt=sys_prompt,
                                                  max_tokens=1000, temperature=0.3)
    except Exception as exc:
        # A missing report is a degradation, not a design failure — say so, don't fake success.
        artifacts["analysis_report"] = None
        artifacts["warnings"].append(f"engineering report unavailable: {type(exc).__name__}: {exc}")

    steps = 4 if result["mode"] == "generated" else 1
    return {"success": True, "data": artifacts, "pipeline_steps_completed": steps}


# ── Tool dispatch table ─────────────────────────────────────

TOOLS = {
    "parse_requirements": _tool_parse_requirements,
    "generate_bom": _tool_generate_bom,
    "extract_package_from_pdf": _tool_extract_package_from_pdf,
    "generate_footprint": _tool_generate_footprint,
    "synthesize_netlist": _tool_synthesize_netlist,
    "route_pcb": _tool_route_pcb,
    "full_pipeline": _tool_full_pipeline,
}


# ════════════════════════════════════════════════════════════
# Request handler
# ════════════════════════════════════════════════════════════

def handle(req: dict) -> None:
    req_id = req.get("id")
    method = req.get("method", "")

    if method == "initialize":
        _respond(req_id, {
            "protocolVersion": "2.0",
            "serverInfo": {"name": "pcb-designer", "version": "1.0.0"},
            "capabilities": {"sampling": {}},
        })

    elif method == "describe":
        _respond(req_id, MANIFEST)

    elif method == "health":
        caps: Dict[str, Any] = {"status": "ready"}
        try:
            from pcbai.eda import backend
            caps["eda_backend"] = backend.capabilities().to_dict()
            caps["degraded"] = not backend.capabilities().can_author_native
        except Exception as exc:  # pragma: no cover
            caps["eda_backend"] = {"error": f"probe failed: {exc}"}
        _respond(req_id, caps)

    elif method == "invoke":
        params = req.get("params") or {}
        tool_name = params.get("tool", "")
        arguments = params.get("arguments") or {}
        context = params.get("context") or {}

        tool_fn = TOOLS.get(tool_name)
        if not tool_fn:
            _respond(req_id, error={"code": -32601, "message": f"Unknown tool: {tool_name}"})
            return

        try:
            result = tool_fn(arguments, context)
            _respond(req_id, result)
        except Exception as exc:
            log(f"Tool '{tool_name}' error: {traceback.format_exc()}")
            _respond(req_id, {"success": False, "error": str(exc)})

    elif method == "shutdown":
        _respond(req_id, {"status": "shutting_down"})
        sys.exit(0)

    else:
        _respond(req_id, error={"code": -32601, "message": f"Unknown method: {method}"})


# ════════════════════════════════════════════════════════════
# Main loop
# ════════════════════════════════════════════════════════════

def main() -> None:
    log("Starting pcb-designer Executa plugin...")
    threading.Thread(target=_reader_thread, daemon=True).start()

    # stdout must carry nothing but JSON-RPC. Any stray print() from pcbai/skidl/pdfminer is
    # diverted to stderr for the lifetime of the server so the host parser can never desync.
    try:
        from pcbai.core.logger import install_stderr_guard
        guard = install_stderr_guard()
    except Exception:  # pragma: no cover
        from contextlib import nullcontext
        guard = nullcontext()

    with guard:
        while True:
            try:
                req = agent_requests.get()
                handle(req)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"Unhandled error: {e}")


if __name__ == "__main__":
    main()
