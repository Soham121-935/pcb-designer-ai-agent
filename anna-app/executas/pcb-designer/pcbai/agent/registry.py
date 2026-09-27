"""Tool registry: names, safety class, parameter schemas, dispatch.

The classification is the enforcement point for the "safe informational vs mutating" split in the
project brief. A future LLM loop (Phase 3) must consult `is_mutating` before auto-executing, and
`dispatch` refuses mutating tools unless the caller opts in explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pcbai.agent.tools import TOOL_FUNCTIONS


@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str
    params: Dict[str, str] = field(default_factory=dict)       # name -> "type[: description]"
    required: List[str] = field(default_factory=list)
    mutating: bool = False                                      # writes into the project tree
    needs_kicad: bool = False                                   # degraded/skipped without KiCad
    maturity: str = "implemented"                               # implemented | placeholder | not-implemented

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "params": self.params,
            "required": self.required,
            "safety": "mutating" if self.mutating else "read-only",
            "needs_kicad": self.needs_kicad,
            "maturity": self.maturity,
        }


#: Tools that the roadmap promises but the code does not have yet. They are registered so the agent
#: gets a precise, actionable "not implemented (Phase N)" instead of inventing a shell command.
PLANNED: Dict[str, ToolSpec] = {
    "inspect_project": ToolSpec("inspect_project", "Summarise a whole KiCad project (pcb+sch+pro)",
                               {"path": "string: project dir or .kicad_pcb"}, mutating=False,
                               maturity="not-implemented (Phase 4)"),
    "inspect_schematic": ToolSpec("inspect_schematic", "Schematic model: symbols, pins, nets",
                                  {"path": "string"}, maturity="not-implemented (Phase 4)"),
    "add_component": ToolSpec("add_component", "Place a new component from a footprint",
                              {"footprint": "string", "ref": "string", "at": "array[x, y]"},
                              mutating=True, maturity="not-implemented (Phase 5)"),
    "move_component": ToolSpec("move_component", "Move a component (absolute mm or delta)",
                              {"ref": "string", "x": "number", "y": "number", "delta": "boolean"},
                              mutating=True, maturity="not-implemented (Phase 5)"),
    "rotate_component": ToolSpec("rotate_component", "Rotate a component about its origin",
                                {"ref": "string", "angle_deg": "number"}, mutating=True,
                                maturity="not-implemented (Phase 5)"),
    "add_track": ToolSpec("add_track", "Draw a copper segment on a net",
                          {"net": "string", "from": "array", "to": "array", "width_mm": "number"},
                          mutating=True, maturity="not-implemented (Phase 5)"),
    "route_net": ToolSpec("route_net", "Route all unrouted pads of one net", {"net": "string"},
                          mutating=True, needs_kicad=True, maturity="not-implemented (Phase 5)"),
    "run_drc": ToolSpec("run_drc", "KiCad's own DRC (needs kicad-cli); use validate_board for the "
                                 "built-in geometry checks", {"path": "string"}, needs_kicad=True,
                        maturity="not-implemented (Phase 6); validate_board covers the same "
                                 "checks without KiCad"),
    "run_erc": ToolSpec("run_erc", "Schematic connectivity/electrical findings",
                        {"path": "string"}, maturity="not-implemented (Phase 6)"),
}


IMPLEMENTED: Dict[str, ToolSpec] = {
    # ── native KiCad model tools (no pcbnew, no KiCad install) ─────────────────
    "inspect_pcb": ToolSpec("inspect_pcb", "Parse a .kicad_pcb natively: format dialect, stackup, "
                            "counts, rule verdict", {"path": "string: .kicad_pcb or project dir"}),
    "list_components": ToolSpec("list_components", "ref/value/footprint/position/pads/nets per part",
                                {"path": "string", "dnp": "boolean", "pattern": "string"}),
    "get_component": ToolSpec("get_component", "One part: every pad with net+geometry, courtyard, "
                               "nearest neighbours", {"ref": "string e.g. U1",
                               "path": "string"}, required=["ref"]),
    "get_nets": ToolSpec("get_nets", "Nets with pad counts, class, tracks/vias and a connectivity "
                         "proxy", {"path": "string", "min_pads": "integer",
                         "only_undrouted": "boolean"}),
    "get_board_outline": ToolSpec("get_board_outline", "Edge.Cuts rings, size, area, parts outside",
                                  {"path": "string"}),
    "get_design_rules": ToolSpec("get_design_rules", "Board minimums + net classes (board file and, "
                                 "when present, .kicad_pro)", {"path": "string"}),
    "validate_board": ToolSpec("validate_board", "Our full check set: clearance, shorts, drills, "
                               "annular rings, courtyards, outline, nets, zones",
                               {"path": "string"}),
    "parse_sexp": ToolSpec("parse_sexp", 'Raw s-expression, or a subtree addressed by a selector '
                           'such as ["setup", "stackup"]',
                          {"path": "string", "selector": "array", "max_depth": "integer"},
                          required=["path"]),
    "list_footprint_kinds": ToolSpec("list_footprint_kinds", "Footprint builders we can generate "
                                     "from data, with their sources"),
    "inspect_footprint": ToolSpec("inspect_footprint", "Preview a footprint spec: pads, bbox, the "
                                  "actual s-expression", {"spec": "string|object"},
                                  required=["spec"]),
    "generate_scaffold": ToolSpec("generate_scaffold", "Design spec (YAML/JSON/dict) → 4-layer "
                                  "KiCad project: placement, planes, fanout, checked tracks",
                                  {"spec": "string|object", "out_dir": "string",
                                   "stem": "string", "dialect": "kicad-8|kicad-9|kicad-10",
                                   "route": "boolean", "dry_run": "boolean"},
                                  required=["spec"], mutating=True),
    "write_board": ToolSpec("write_board", "Write a board dict / Board.to_json dump and verify by "
                            "re-reading", {"board_spec": "object|string", "out_dir": "string",
                            "stem": "string", "dialect": "string"}, required=["board_spec"],
                            mutating=True),
    "apply_edit": ToolSpec("apply_edit", "Model-level edits (move/set_rule/assign_net/…) written "
                           "back with re-read verification", {"edits": "array of {op, …}",
                           "path": "string", "dry_run": "boolean",
                           "allow_lossy": "boolean"}, required=["edits"], mutating=True),

    "capabilities": ToolSpec("capabilities", "Report usable EDA backends (pcbnew/kicad-cli/libs)"),
    "list_project_files": ToolSpec("list_project_files", "List KiCad/BOM files in the working project",
                                   {"path": "string: optional dir", "pattern": "string: optional name filter"}),
    "read_project_file": ToolSpec("read_project_file", "Read one project file (size-capped, read-only)",
                                  {"path": "string", "max_bytes": "integer"}),
    "inspect_pcb_file": ToolSpec("inspect_pcb_file", "Structural sanity of a .kicad_pcb (counts, missing "
                                 "net table / outline / tracks)", {"path": "string"}),
    "create_backup": ToolSpec("create_backup", "Snapshot a project file before an edit",
                              {"path": "string", "reason": "string"}, mutating=True),
    "generate_footprint": ToolSpec("generate_footprint", "Build a .kicad_mod, save it, re-read and verify",
                                  {"footprint_type": "string: qfn|qfp|soic|smd_rc|bga|dip|usbc|header|custom",
                                   "params": "object: name, pins, pitch, body_l/w, pad_l/w, ...",
                                   "output_dir": "string", "save": "boolean"},
                                  required=["footprint_type"], mutating=True),
    "generate_bom": ToolSpec("generate_bom", "Map requirement keywords to the built-in part catalog",
                            {"requirements": "object"}, required=["requirements"]),
    "parse_requirements": ToolSpec("parse_requirements", "NL description → structured requirements",
                                  {"description": "string", "use_llm": "boolean"},
                                  required=["description"]),
    "full_pipeline": ToolSpec("full_pipeline", "End-to-end compile; reports mode=generated|template",
                             {"description": "string", "output_dir": "string",
                              "allow_template_copy": "boolean"}, required=["description"],
                             mutating=True, needs_kicad=True),
    "route_pcb": ToolSpec("route_pcb", "Assemble a board with KiCad pcbnew (pad-level netlist required)",
                         {"netlist": "object", "output_dir": "string"}, required=["netlist"],
                         mutating=True, needs_kicad=True, maturity="implemented (requires REF-PIN netlist)"),
    "synthesize_netlist": ToolSpec("synthesize_netlist", "BOM → netlist (GND/VCC grouping only)",
                                  {"bom": "array"}, required=["bom"], maturity="placeholder (Phase 4)"),
    "extract_package_from_pdf": ToolSpec("extract_package_from_pdf", "Datasheet PDF → package dimensions",
                                        {"pdf_path": "string"}, required=["pdf_path"],
                                        maturity="implemented (weak: regex extractor, no OCR here)"),
}


def registry() -> Dict[str, ToolSpec]:
    tools: Dict[str, ToolSpec] = dict(IMPLEMENTED)
    tools.update(PLANNED)
    return tools


def describe_all() -> List[Dict[str, Any]]:
    return [spec.describe() for spec in sorted(registry().values(), key=lambda s: s.name)]


def resolve(name: str) -> Optional[Callable[..., Dict[str, Any]]]:
    return TOOL_FUNCTIONS.get(name)


def dispatch(name: str, arguments: Optional[Dict[str, Any]] = None, *,
             allow_mutating: bool = True, confirm: bool = False) -> Dict[str, Any]:
    """Single entry point for tool execution, enforcing the safety split.

    * unknown tool              → reason "unknown-tool" (+ a pointer to the closest planned tool)
    * planned/not-implemented   → reason "not-implemented" with the phase that provides it
    * mutating without allow    → reason "needs-mutation-approval"  (dry_run=true is not mutating)
    * mutating without confirm  → reason "needs-confirmation"
    """
    from pcbai.agent.tools import fail, ok  # local import: keep registry importable standalone

    args = dict(arguments or {})
    spec = registry().get(name)
    if spec is None:
        near = [t for t in registry() if name.lower()[:4] in t.lower()]
        return fail(f"unknown tool '{name}'", "unknown-tool",
                    data={"hint": near[:5] or "run `pcbai-agent tools` for the full list"})

    if spec.maturity.startswith("not-implemented"):
        return fail(
            f"tool '{name}' is not implemented yet ({spec.maturity}). "
            "Do NOT improvise with raw file edits or shell commands — report the gap to the user.",
            "not-implemented", data={"spec": spec.describe()})

    fn = resolve(name)
    if fn is None:
        return fail(f"tool '{name}' has no handler bound", "not-wired", data={"spec": spec.describe()})

    # A call that declares dry_run=True cannot write, so it is not a mutation as far as policy goes.
    # Handlers that honour this: generate_scaffold, apply_edit, write_board (they return before any
    # filesafe call). Anything else that ignores dry_run still gets gated by `spec.mutating`.
    mutating = spec.mutating and args.get("dry_run") is not True

    if mutating and not allow_mutating:
        return fail(f"'{name}' mutates project files and was refused by policy", "needs-mutation-approval",
                    data={"spec": spec.describe(),
                          "hint": "pass dry_run=true to inspect the result without writing"})
    if mutating and spec.name != "create_backup" and not confirm:
        return fail(f"'{name}' writes into the project; re-call with confirm=true", "needs-confirmation",
                    data={"spec": spec.describe()})

    try:
        result = fn(**args)
    except TypeError as exc:
        return fail(f"bad arguments for '{name}': {exc}", "bad-args",
                    data={"params": spec.params, "required": spec.required})
    except Exception as exc:  # tools should catch their own; this is the backstop
        return fail(f"{type(exc).__name__}: {exc}", "tool-exception")
    if not isinstance(result, dict) or "success" not in result:
        return ok(result, warnings=[f"tool '{name}' returned a non-envelope value; wrapped it"])
    return result
