"""Tool layer usable **without** the Anna host.

Every function returns a uniform envelope::

    {"success": bool, "data": {...}, "error": str|None, "reason": str, "warnings": [...]}

Rules (from the project brief; enforced here, not just documented):
  * Informational tools are safe to call at any time and never write.
  * Mutating tools write through :mod:`pcbai.core.filesafe` (atomic write + backup) and **must**
    re-read what they wrote before reporting success. A tool that wrote a file but cannot parse it
    again returns ``success: false``.
  * `success: true` means the post-condition was verified — never merely "a file was written".
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from pcbai.core.config import settings
from pcbai.core.filesafe import UnsafeWriteError, atomic_write_text, check_writable
from pcbai.core.logger import get_logger
from pcbai.eda import backend

# ok/fail/project_root live in .envelope so the KiCad handlers can share them without a cycle; they
# stay importable from this module because that is where callers have always found them.
from .envelope import fail, ok, project_root  # noqa: F401

logger = get_logger("pcbai.tools")


# ─────────────────────────────────────────────────────────────────────────────
# Envelope helpers
# ─────────────────────────────────────────────────────────────────────────────

def backend_repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, *([".."] * 5)))


# ─────────────────────────────────────────────────────────────────────────────
# Informational tools (safe)
# ─────────────────────────────────────────────────────────────────────────────

def capabilities_tool(**_: Any) -> Dict[str, Any]:
    """What EDA backends are usable here. Drives every 'skipped vs passed' distinction."""
    caps = backend.capabilities()
    return ok(caps.to_dict(), warnings=caps.notes)


def list_project_files(path: Optional[str] = None, pattern: Optional[str] = None,
                       max_files: int = 400) -> Dict[str, Any]:
    """List candidate PCB project files under a directory (read-only)."""
    root = project_root(path)
    matches: List[Dict[str, Any]] = []
    exts = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_mod", ".kicad_sym", ".zip", ".csv", ".json")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in ("node_modules", "__pycache__", "build", "dist")]
        for name in filenames:
            if not name.endswith(exts):
                continue
            if pattern and pattern not in name:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
                size = st.st_size
            except OSError:
                size = -1
            matches.append({
                "path": os.path.relpath(full, root),
                "abs_path": full,
                "bytes": size,
                "kind": os.path.splitext(name)[1].lstrip("."),
            })
            if len(matches) >= max_files:
                return ok({"root": root, "files": matches, "truncated": True},
                          warnings=[f"stopped after {max_files} files"])
    matches.sort(key=lambda f: f["path"])
    return ok({"root": root, "files": matches, "truncated": False})


def read_project_file(path: str, max_bytes: int = 200_000) -> Dict[str, Any]:
    """Read a single file inside the project (read-only, size-capped)."""
    root = project_root()
    target = os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
    if not target.startswith(root + os.sep):
        return fail(f"path escapes the project root ({root}): {path}", "unsafe-path")
    if not os.path.isfile(target):
        return fail(f"no such file: {target}", "not-found")
    size = os.path.getsize(target)
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(max_bytes)
    warnings = []
    if size > max_bytes:
        warnings.append(f"truncated to {max_bytes} of {size} bytes")
    return ok({"path": target, "bytes": size, "content": text}, warnings=warnings)


def _top_level_tokens(text: str) -> Dict[str, int]:
    """Count direct children of the root `(kicad_pcb ...)` form by token name.

    Depth-aware, unlike `str.count()`, so `(net ...)` inside a pad is not mistaken for the
    board-level net table (the failure mode found in the shipped template board).
    """
    counts: Dict[str, int] = {}
    depth = 0
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "(":
                depth += 1
                if depth == 2:                       # direct child of kicad_pcb
                    j = i + 1
                    while j < n and text[j] in " \n\t\r":
                        j += 1
                    k = j
                    while k < n and text[k] not in " \n\t\r)":
                        k += 1
                    token = text[j:k]
                    if token:
                        counts[token] = counts.get(token, 0) + 1
            elif ch == ")":
                depth -= 1
        i += 1
    return counts


def inspect_pcb_file(path: str) -> Dict[str, Any]:
    """Report *format-level* facts about a .kicad_pcb file (read-only).

    Phase 2 scope: structural sanity only (version, generator, top-level block counts, whether a net
    table / design settings exist). The real geometry model is Phase 4 — this tool says
    "not implemented" rather than guessing.
    """
    if not os.path.isfile(path):
        return fail(f"no such file: {path}", "not-found")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(2_000_000)
    if not text.lstrip().startswith("(kicad_pcb"):
        return fail("file is not a KiCad PCB (missing '(kicad_pcb' header)", "not-a-pcb")
    top_level = _top_level_tokens(text)
    counts = {
        "footprints": top_level.get("footprint", 0),
        "segments": top_level.get("segment", 0),
        "vias": top_level.get("via", 0),
        "zones": top_level.get("zone", 0),
        "edge_cuts": top_level.get("gr_line", 0) + top_level.get("gr_rect", 0) * 4,
        "net_declarations": top_level.get("net", 0),   # top-level (net N "NAME") entries
        "setup_present": "setup" in top_level,
        "pads": text.count("(pad "),
        "lib_footprint_ids": text.count('(lib_id "'),
    }
    version_line = ""
    for line in text.splitlines()[:12]:
        if "version" in line:
            version_line = line.strip()
            break
    warnings: List[str] = []
    if counts["edge_cuts"] == 0:
        warnings.append("no Edge.Cuts geometry: the board has no outline — DRC and fab checks are meaningless")
    if counts["net_declarations"] == 0:
        warnings.append("no top-level net table: pads reference nets by name only, so KiCad cannot resolve connectivity")
    if not any(k == "net_class" for k in top_level):
        warnings.append("no net_class in (setup): every clearance rule is a factory default — "
                        "widths/vias cannot be trusted for power nets")
    if counts["segments"] == 0:
        warnings.append("no copper tracks: board is unrouted")
    if counts["footprints"] and counts["pads"] == 0:
        warnings.append("footprints present but zero pads: footprints are incomplete")
    return ok({
        "path": path,
        "header_version": version_line,
        "generator": "pcbnew" if 'generator "pcbnew"' in text else ("pcbai" if '"pcbai"' in text else "unknown"),
        **counts,
        "top_level_tokens": top_level,
        "model": "not implemented (Phase 4) — counts only, no geometry",
    }, warnings=warnings)


def create_backup(path: str, reason: str = "manual") -> Dict[str, Any]:
    """Explicitly snapshot one project file (safe, non-mutating to the original)."""
    try:
        check_writable(path)
    except UnsafeWriteError as exc:
        return fail(str(exc), "unsafe-path")
    if not os.path.isfile(path):
        return fail(f"no such file: {path}", "not-found")
    from pcbai.core.filesafe import atomic_write_bytes
    data = open(path, "rb").read()
    res = atomic_write_bytes(__import__("pathlib").Path(path), data, reason=f"backup-{reason}")
    return ok(res.to_dict(), warnings=["original bytes restored via .bak if a later edit fails"])


# ─────────────────────────────────────────────────────────────────────────────
# Generation tools (write into the project, verified)
# ─────────────────────────────────────────────────────────────────────────────

_FOOTPRINT_BUILDERS: Dict[str, str] = {
    "qfn": "pcbai.steps.footprint_qfn_qfp",
    "qfp": "pcbai.steps.footprint_qfn_qfp",
    "soic": "pcbai.steps.footprint_generator",
    "smd_rc": "pcbai.steps.footprint_generator",
    "bga": "pcbai.steps.footprint_bga",
    "dip": "pcbai.steps.footprint_dip",
    "usbc": "pcbai.steps.footprint_usbc",
    "header": "pcbai.steps.footprint_header",
    "custom": "pcbai.steps.footprint_custom",
}


def _build_footprint_content(ftype: str, params: Dict[str, Any]) -> str:
    import importlib

    name = params.get("name", f"FP_{ftype}")
    mod = importlib.import_module(_FOOTPRINT_BUILDERS[ftype])
    if ftype == "qfn":
        return mod.generate_qfn(mod.QfnParams(**params))
    if ftype == "qfp":
        return mod.generate_qfp(mod.QfpParams(**params))
    if ftype == "soic":
        return mod.generate_soic(mod.SoicParams(**params))
    if ftype == "smd_rc":
        return mod.generate_smd_rc(mod.SmdRcParams(**params))
    if ftype == "bga":
        return mod.generate_bga(mod.BgaParams(**params))
    if ftype == "dip":
        return mod.generate_dip(mod.DipParams(**params))
    if ftype == "usbc":
        return mod.generate_usbc(mod.UsbcParams(name=name))
    if ftype == "header":
        return mod.generate_header(mod.HeaderParams(**params))
    if ftype == "custom":
        return mod.generate_custom(mod.CustomParams(**params))
    raise ValueError(f"unsupported footprint type: {ftype}")


def _validate_kicad_mod(text: str) -> None:
    """Cheap structural check on a footprint before it is trusted: balanced parens + pads."""
    if text.count("(") != text.count(")"):
        raise ValueError(f"unbalanced parentheses ({text.count('(')} open vs {text.count(')')} close)")
    if "(pad " not in text and "(smd " not in text:
        raise ValueError("footprint contains no pads")
    if not (text.lstrip().startswith("(module ") or text.lstrip().startswith("(footprint ")):
        raise ValueError("footprint must start with (module ...) or (footprint ...)")


def generate_footprint_tool(footprint_type: str, params: Dict[str, Any],
                            output_dir: Optional[str] = None,
                            save: bool = True) -> Dict[str, Any]:
    """Generate a KiCad footprint, write it into the project, and **re-read** it to verify.

    Unlike the audited version (which generated into a temp dir that was immediately deleted), the
    file persists where the router expects it: ``<project>/footprints/``.
    """
    ftype = (footprint_type or "").lower()
    if ftype not in _FOOTPRINT_BUILDERS:
        return fail(f"unknown footprint type '{ftype}'; supported: {', '.join(sorted(_FOOTPRINT_BUILDERS))}",
                    "bad-args")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as exc:
            return fail(f"params must be a JSON object: {exc}", "bad-args")
    params = dict(params or {})
    name = params.get("name") or f"FP_{ftype}"
    params.setdefault("name", name)

    try:
        content = _build_footprint_content(ftype, params)
    except TypeError as exc:
        return fail(f"missing/invalid parameters for {ftype}: {exc}", "bad-args")
    except Exception as exc:
        return fail(f"footprint generation failed: {type(exc).__name__}: {exc}", "generator-error")

    warnings: List[str] = []
    path = None
    if save:
        outdir = output_dir or os.path.join(project_root(), "footprints")
        os.makedirs(outdir, exist_ok=True)
        target = os.path.join(outdir, f"{name}.kicad_mod")
        try:
            res = atomic_write_text(target, content, reason=f"footprint-{ftype}")
            path = res.path
        except Exception as exc:
            return fail(f"could not write footprint: {exc}", "write-error")
        try:
            with open(target, "r", encoding="utf-8") as fh:
                reread = fh.read()
        except OSError as exc:
            return fail(f"wrote footprint but cannot read it back: {exc}", "verify-failed")
        if reread != content:
            return fail("written footprint differs from generated content", "verify-failed",
                        data={"path": target})
        warnings.append("KiCad 5/6 dialect (module/fp_text) — upgrade to 'footprint/property' lands with Q3/KiCad-10 work")

    try:
        _validate_kicad_mod(content)
    except ValueError as exc:
        return fail(f"generated footprint is structurally invalid: {exc}", "validation-failed",
                    data={"path": path, "kicad_mod_content": content})
    pad_count = content.count("(pad ") + content.count("(smd ")
    warnings.append(f"{pad_count} pads")
    return ok({"footprint_type": ftype, "name": name, "path": path,
               "pads": pad_count, "kicad_mod_content": content}, warnings=warnings)


def generate_bom_tool(requirements: Any) -> Dict[str, Any]:
    from pcbai.steps.bom_generator import generate_bom

    if isinstance(requirements, str):
        try:
            requirements = json.loads(requirements)
        except json.JSONDecodeError as exc:
            return fail(f"requirements must be JSON: {exc}", "bad-args")
    bom = generate_bom(requirements or {})
    unresolved = [b.get("mpn", "?") for b in bom if str(b.get("package", "")).upper() == "UNKNOWN"]
    warnings = []
    if not bom:
        warnings.append("BOM is empty: no keyword matched the built-in catalog")
    if unresolved:
        warnings.append(f"no catalog entry for: {', '.join(unresolved)} (real sourcing is not implemented)")
    return ok({"bom": bom, "count": len(bom)}, warnings=warnings)


def parse_requirements_tool(description: str, use_llm: bool = True) -> Dict[str, Any]:
    from pcbai.steps.requirements_parser import parse_requirements

    if not (description or "").strip():
        return fail("description is required", "bad-args")
    reqs = parse_requirements(description)
    warnings = []
    if not reqs.get("keywords"):
        warnings.append("no component keywords recognised — downstream BOM will be empty")
    if reqs.get("source") == "keyword-fallback":
        warnings.append("no usable LLM (PCB_AI_LLM_PROVIDER); keyword fallback used — "
                        "no component counts, no rail/current analysis, no pin intent")
    return ok(reqs, warnings=warnings)


def full_pipeline_tool(description: str, output_dir: Optional[str] = None,
                       allow_template_copy: Optional[bool] = None) -> Dict[str, Any]:
    """Standalone pipeline (no host sampling). Reports mode/template_only truthfully."""
    from pcbai.steps.design_compiler import DesignNotGenerated, compile_design

    outdir = output_dir or os.path.join(settings.workdir, "pipeline")
    os.makedirs(outdir, exist_ok=True)
    try:
        result = compile_design(description or "", outdir, allow_template_copy=allow_template_copy)
    except DesignNotGenerated as exc:
        return fail(str(exc), exc.reason, data={"detail": exc.detail,
                                                "backend": backend.capabilities().to_dict()})
    except Exception as exc:
        logger.debug("pipeline traceback:\n%s", __import__("traceback").format_exc())
        return fail(f"{type(exc).__name__}: {exc}", "pipeline-exception")

    verified: Dict[str, Any] = {}
    for key in ("sch", "pcb"):
        path = result.get(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                body = fh.read()
            verified[key] = {"path": path, "bytes": len(body), "parsed": body.lstrip().startswith(
                "(kicad_sch" if key == "sch" else "(kicad_pcb")}
        except OSError as exc:
            return fail(f"pipeline reported {key} but the file is unreadable: {exc}", "artifact-missing",
                        data=result)
    if not all(v["parsed"] for v in verified.values()):
        return fail("pipeline produced files that do not parse as KiCad documents", "validation-failed",
                    data={"verified": verified, "mode": result["mode"]})
    return ok({**{k: result[k] for k in ("mode", "template_only", "bom", "gerbers", "zip", "backend")},
               "verified": verified, "paths": {k: result[k] for k in ("sch", "pcb")}},
              warnings=result.get("warnings", []))


def synthesize_netlist_tool(bom: Any) -> Dict[str, Any]:
    """BOM → netlist. Currently a placeholder: GND/VCC ref groups, **no pad-level connectivity**."""
    from pcbai.steps.schematic_synthesizer import synthesize_schematic

    if isinstance(bom, str):
        try:
            bom = json.loads(bom)
        except json.JSONDecodeError as exc:
            return fail(f"bom must be a JSON array: {exc}", "bad-args")
    netlist = synthesize_schematic(list(bom or []))
    return ok({"netlist": netlist}, warnings=[
        "placeholder netlist: nets hold component refs, not REF-PIN pairs — a router cannot consume "
        "this. Real connectivity arrives with the schematic/PCB model (Phase 4)."
    ], maturity="placeholder")


def route_pcb_tool(netlist: Any, output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Assemble a .kicad_pcb from a netlist via KiCad pcbnew. Honest about a missing backend."""
    from pcbai.steps.pcb_router import route_pcb

    if isinstance(netlist, str):
        try:
            netlist = json.loads(netlist)
        except json.JSONDecodeError as exc:
            return fail(f"netlist must be JSON: {exc}", "bad-args")
    outdir = output_dir or os.path.join(project_root(), "build")
    result = route_pcb(netlist or {"nets": [], "components": []}, outdir)
    if not result.get("ok"):
        return fail(result.get("status") or "board build failed",
                    str(result.get("reason") or "router-failed"),
                    data={k: v for k, v in result.items() if k != "netlist"})
    warnings = list(result.get("warnings", []))
    if not result.get("footprints_found"):
        warnings.append("no .kicad_mod files were present in <project>/footprints, so nothing could be placed")
    return ok({k: v for k, v in result.items() if k != "netlist"}, warnings=warnings)


def extract_package_from_pdf_tool(pdf_path: str) -> Dict[str, Any]:
    """Datasheet PDF → package dimensions (local regex extractor; no OCR/LLM here)."""
    from dataclasses import asdict

    from pcbai.steps.datasheet_package_extractor import extract_package_params_from_pdf

    if not os.path.isfile(pdf_path):
        return fail(f"no such file: {pdf_path}", "not-found")
    try:
        guess = extract_package_params_from_pdf(pdf_path)
    except Exception as exc:
        return fail(f"extraction failed: {type(exc).__name__}: {exc}", "extractor-error")
    data = asdict(guess)
    missing = [k for k, v in data.items() if v is None]
    warnings = []
    if missing:
        warnings.append(f"unresolved dimension fields: {', '.join(missing)} — verify against the "
                        "datasheet before generating a footprint")
    return ok(data, warnings=warnings)



# Registry of callables used by registry.py — kept in one place so the CLI, the Anna plugin and
# (Phase 3) the LLM loop all dispatch through the same code.
#: native KiCad handlers (no pcbnew): board reads/validates/edits through pcbai.kicad
from .kicad_tools import TOOL_FUNCTIONS as _KICAD_TOOLS  # noqa: E402


TOOL_FUNCTIONS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "capabilities": capabilities_tool,
    "list_project_files": list_project_files,
    "read_project_file": read_project_file,
    "inspect_pcb_file": inspect_pcb_file,
    "create_backup": create_backup,
    "generate_footprint": generate_footprint_tool,
    "generate_bom": generate_bom_tool,
    "parse_requirements": parse_requirements_tool,
    "full_pipeline": full_pipeline_tool,
    "synthesize_netlist": synthesize_netlist_tool,
    "route_pcb": route_pcb_tool,
    "extract_package_from_pdf": extract_package_from_pdf_tool,
    **_KICAD_TOOLS,
}
