"""Native KiCad tools: read, validate, generate and edit ``.kicad_pcb`` without KiCad.

These handlers sit on top of :mod:`pcbai.kicad` (S-expression parser → board model → writer) so the
agent can act on a board in *any* environment, including one with no ``pcbnew`` and no ``kicad-cli``.
Every mutating handler follows the same contract:

1. parse the file (never regex it),
2. mutate the model,
3. write through :mod:`pcbai.core.filesafe` (atomic + backup),
4. **re-read the file and re-validate it**, and only then report success,
5. refuse a write that would silently drop geometry the model cannot represent (:func:`lossiness`).

Informational handlers return the same envelope as the rest of :mod:`pcbai.agent.tools`
(``success/data/error/reason/warnings``) so the host never has to special-case them.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


from pcbai.core.logger import get_logger
from pcbai.kicad.footprints import REGISTRY as FOOTPRINT_REGISTRY
from pcbai.kicad.footprints import from_spec
from pcbai.kicad.model import Board, Issue, NetClass
from pcbai.kicad.pcb_reader import format_dialect, read_board, read_tree
from pcbai.kicad.pcb_writer import DIALECTS, lossiness, write_project_files
from pcbai.kicad.scaffold import DEFAULT_RULES, generate, load_spec

from .envelope import fail, ok, project_root

logger = get_logger("pcbai.kicad_tools")

#: Boards that need something only KiCad can decide. Surfaced as warnings, never hidden.
_KICAD_ONLY = ("a real DRC (clearance matrix, courtyard/paste rules, net-tie semantics), "
               "ratsnest autorouting, and gerber/drill export")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _find_board(path: Optional[str] = None) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve *path* to a ``.kicad_pcb``: explicit file, or the first board in a project dir."""
    if not path:
        path = os.getenv("PCB_AI_BOARD") or project_root()
    p = Path(path).expanduser()
    if p.is_dir():
        cands = sorted(p.glob("*.kicad_pcb"))
        if not cands:
            return None, f"no .kicad_pcb in {p}"
        return cands[0], None
    if not p.exists():
        return None, f"no such file: {p}"
    if p.suffix != ".kicad_pcb":
        return None, f"{p.name} is not a .kicad_pcb"
    return p, None


def _load(path: Optional[str] = None, *, merge_project: bool = True) -> Tuple[Optional[Board],
                                                                              Optional[Dict[str, Any]]]:
    """Board + its file path, or a failure envelope. Parse errors become actionable refusals."""
    board_path, err = _find_board(path)
    if err:
        return None, fail(err, "not-found")
    try:
        board = read_board(board_path, merge_project=merge_project)   # type: ignore[arg-type]
    except Exception as exc:  # SexpParseError, OSError, ValueError
        return None, fail(f"{board_path.name}: {exc}", "parse-error",
                          data={"path": str(board_path),
                                "hint": "the file does not parse; fix the s-expression before editing"})
    return board, {"path": str(board_path)}


def _severity_counts(issues: Sequence[Issue]) -> Dict[str, int]:
    """Severity tally that always carries all three keys.

    An empty dict for a clean board reads as "nothing was counted"; `{"error": 0, ...}` says the check
    ran and found nothing, which is the distinction a host acting on the result needs. A plain dict is
    used rather than ``Counter`` arithmetic because every Counter operator — ``+``, ``|``, ``&`` —
    *drops* non-positive entries, so the zeros this exists to keep evaporate.
    """
    out = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        out[issue.severity] = out.get(issue.severity, 0) + 1
    return out


def _issues_payload(issues: Sequence[Issue], limit: int = 60) -> List[Dict[str, Any]]:
    return [i.to_dict() for i in issues[:limit]] + (
        [{"severity": "info", "code": "truncated",
          "message": f"{len(issues) - limit} more findings not listed"}] if len(issues) > limit else [])


def _kicad_hint() -> List[str]:
    from pcbai.eda import backend
    caps = backend.capabilities()
    if caps.kicad_cli or caps.pcbnew:
        return []
    return [f"these checks are ours, not KiCad's: {_KICAD_ONLY} still need kicad-cli/pcbnew "
            "(see `capabilities` and the CI parity job)"]


# ─────────────────────────────────────────────────────────────────────────────
# informational
# ─────────────────────────────────────────────────────────────────────────────
def inspect_pcb_tool(path: Optional[str] = None) -> Dict[str, Any]:
    """Board overview: dialect, stackup, counts, outline, verdict of our own checks."""
    board, meta = _load(path)
    if board is None and meta and meta.get("success") is False:
        return meta
    tree = read_tree(meta["path"])
    issues = board.checks()
    return ok({"summary": board.summary(), "format": format_dialect(tree),
               "issues": {"error": sum(1 for i in issues if i.severity == "error"),
                          "warning": sum(1 for i in issues if i.severity == "warning")},
               "generator": board.variables.get("generator", ""),
               "variables": dict(list(board.variables.items())[:20])},
              warnings=_kicad_hint(), path=meta["path"],
              report=board.report()["counts"])


def list_components_tool(path: Optional[str] = None, dnp: Optional[bool] = None,
                          pattern: Optional[str] = None) -> Dict[str, Any]:
    """Every component: ref, value, footprint, position, pad count, nets touched."""
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    rows = []
    for fp in board.footprints:
        if dnp is not None and bool(fp.dnp) != dnp:
            continue
        if pattern and pattern.lower() not in (fp.reference + fp.value + fp.lib_id).lower():
            continue
        nets = sorted({p.net for p in fp.pads if p.net})
        rows.append({"ref": fp.reference, "value": fp.value, "footprint": fp.lib_id,
                     "x": fp.x, "y": fp.y, "rot": fp.rot, "layer": fp.layer, "pads": len(fp.pads),
                     "dnp": bool(fp.dnp), "nets": nets})
    return ok({"count": len(rows), "components": rows}, warnings=_kicad_hint(), path=meta["path"])


def get_component_tool(ref: str, path: Optional[str] = None) -> Dict[str, Any]:
    """One component in detail: pads with net + geometry, courtyard, neighbours."""
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    fp = board.find(ref)
    if fp is None:
        return fail(f"no component '{ref}'", "not-found",
                    data={"known": sorted(f.reference for f in board.footprints if f.reference)[:40]})
    court = fp.courtyard()
    pads = []
    for pad in fp.pads:
        x, y, rot = pad.absolute(fp)
        pads.append({"pad": pad.number, "net": pad.net, "type": pad.type, "shape": pad.shape,
                     "x": round(x, 4), "y": round(y, 4), "rot": round(rot, 4),
                     "size": list(pad.size), "drill": pad.drill,
                     "layers": list(pad.layers)})
    box = fp.bbox()
    neighbours = []
    for other in board.footprints:
        if other is fp:
            continue
        ob = other.bbox()
        gap = max(box[0] - ob[2], ob[0] - box[2], box[1] - ob[3], ob[1] - box[3])
        if max(abs(other.x - fp.x), abs(other.y - fp.y)) < 12.0:
            neighbours.append({"ref": other.reference, "gap_mm": round(max(gap, 0.0), 3)})
    return ok({"ref": fp.reference, "value": fp.value, "footprint": fp.lib_id,
               "at": [fp.x, fp.y, fp.rot], "layer": fp.layer, "dnp": bool(fp.dnp),
               "bbox": list(box), "courtyard": list(court) if court else None,
               "attr": list(fp.attr), "description": fp.description, "datasheet": fp.datasheet,
               "pads": pads, "neighbours": sorted(neighbours, key=lambda n: n["gap_mm"])[:8]},
              warnings=_kicad_hint(), path=meta["path"])


def get_nets_tool(path: Optional[str] = None, min_pads: Optional[int] = None,
                  only_undrouted: bool = False) -> Dict[str, Any]:
    """Nets with pad counts, net class, and how much copper each already has."""
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    members: Dict[str, List[str]] = {}
    for fp, pad in board.all_pads():
        if pad.net:
            members.setdefault(pad.net, []).append(f"{fp.reference}.{pad.number}")
    per_net_tracks = Counter(t.net for t in board.tracks)
    per_net_vias = Counter(v.net for v in board.vias)
    rows = []
    for net in sorted(set(members) | set(board.net_names()) | set(per_net_tracks)):
        pads = members.get(net, [])
        if min_pads is not None and len(pads) < int(min_pads):
            continue
        # a net is "done" when every pad can be reached from one copper blob; we cannot compute
        # connectivity for real, so the honest proxy is: at least one track/via per extra pad
        # "how many pads still look unconnected" can never be negative: a plane-connected pad simply
        # counts as connected, and a negative number here would read as a bug in the report
        undrouted = max(0, max(0, len(pads) - 1)
                        - (per_net_tracks.get(net, 0) + per_net_vias.get(net, 0)))
        if only_undrouted and undrouted <= 0:
            continue
        rows.append({"net": net, "pads": len(pads), "members": pads[:12],
                     "net_class": board.rules.clearance_for(net).name,
                     "tracks": per_net_tracks.get(net, 0), "vias": per_net_vias.get(net, 0),
                     "likely_unconnected": undrouted,
                     "in_zones": [z.layer for z in board.zones if z.net == net]})
    counts = {"single_pad": sum(1 for r in rows if r["pads"] == 1),
              "multi_pad": sum(1 for r in rows if r["pads"] > 1),
              "suspect": sum(1 for r in rows if r["likely_unconnected"] > 0)}
    return ok({"count": len(rows), "counts": counts, "nets": rows},
              warnings=_kicad_hint(), path=meta["path"])


def get_board_outline_tool(path: Optional[str] = None) -> Dict[str, Any]:
    """Edge.Cuts geometry: rings, size, area, and every object outside the board."""
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    box = board.outline_bbox()
    outside = []
    if box:
        for fp in board.footprints:
            b = fp.bbox()
            if b[0] < box[0] or b[1] < box[1] or b[2] > box[2] or b[3] > box[3]:
                outside.append(fp.reference or fp.lib_id)
    rings = board.outline_rings() if hasattr(board, "outline_rings") else []
    return ok({"segments": len(board.edge), "rings": [[list(p) for p in r] for r in rings],
               "bbox": list(box) if box else None,
               "size_mm": [round(box[2] - box[0], 4), round(box[3] - box[1], 4)] if box else None,
               "outside_outline": outside}, warnings=_kicad_hint(), path=meta["path"])


def get_design_rules_tool(path: Optional[str] = None) -> Dict[str, Any]:
    """The constraint table, plus where each number came from (board file vs project file)."""
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    rules = board.rules
    return ok({"rules": rules.to_dict(), "net_classes": {n: c.to_dict() for n, c in
                                                          sorted(rules.net_classes.items())},
               "stackup": [s.to_dict() for s in board.stackup],
               "thickness_mm": board.thickness, "copper_layers": board.copper,
               "rules_source": board.variables.get("rules_source", "board file only"),
               "defaults": dict(DEFAULT_RULES)},
              warnings=_kicad_hint() + ([
                  "minima came from the .kicad_pcb alone; KiCad keeps net classes in .kicad_pro"]
                  if board.variables.get("rules_source") is None else []),
              path=meta["path"])


def validate_board_tool(path: Optional[str] = None, *, as_json: bool = False) -> Dict[str, Any]:
    """Full self-check: geometry, rules, layer stack, net sanity. ``data.ok`` is the verdict."""
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    issues = board.checks()
    counts = _severity_counts(issues)
    data = {"ok": not counts["error"], "counts": dict(counts), "summary": board.summary(),
            "issues": _issues_payload(issues)}
    return ok(data, warnings=_kicad_hint(), path=meta["path"])


def parse_sexp_tool(path: str, selector: Optional[Sequence[Any]] = None, max_depth: int = 3,
                    max_nodes: int = 300) -> Dict[str, Any]:
    """Look at the raw s-expression: whole tree (shallow) or a subtree addressed by path.

    ``selector`` is a list of ``head`` or ``head:index`` steps, e.g. ``["setup", "stackup"]`` or
    ``["footprint:2", "pad:0"]`` — enough for an agent to confirm what a token really says.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return fail(f"no such file: {p}", "not-found")
    try:
        tree = read_tree(p)
    except Exception as exc:
        return fail(f"{p.name}: {exc}", "parse-error")
    node = tree
    walked: List[str] = []
    for step in selector or []:
        token, _, idx = str(step).partition(":")
        kids = [c for c in node.items[1:] if c.head == token]
        if not kids:
            return fail(f"no '{token}' under {'/'.join(walked) or tree.head}", "selector-missed",
                        data={"siblings": sorted({c.head for c in node.items[1:] if c.head})})
        node = kids[int(idx or 0)]
        walked.append(str(step))
    return ok({"path": str(p), "selected": "/".join(walked) or "(root)",
               "node": node.to_python(), "max_depth": max_depth, "max_nodes": max_nodes},
              children=sorted({c.head for c in node.items[1:] if c.head}))


def list_footprint_kinds_tool() -> Dict[str, Any]:
    """What the placement layer can build natively (kind → description + generator)."""
    kinds: Dict[str, Any] = {}
    for name, builder in sorted(FOOTPRINT_REGISTRY.items()):
        try:
            definition = builder()
        except TypeError as exc:          # needs arguments (e.g. qfp wants a pitch)
            kinds[name] = {"requires": str(exc)[:110],
                           "doc": (builder.__doc__ or "").strip().splitlines()[0][:160]}
            continue
        kinds[name] = {"description": definition.description, "source": definition.source,
                       "datasheet": definition.datasheet, "tags": definition.tags,
                       "pads": len(definition.pads)}
    return ok({"kinds": sorted(FOOTPRINT_REGISTRY),
               "generic_size_codes": ["0201", "0402", "0603", "0805", "1206"],
               "registry": kinds},
              warnings=["a generated footprint is a *starting point*: its courtyard and paste "
                        "steps still come from the datasheet you must read"])


def inspect_footprint_tool(spec: Any) -> Dict[str, Any]:
    """Preview a footprint from a spec (``"0603"`` or ``{kind: qfp, pins: 48, pitch: 0.5}``)."""
    try:
        definition = from_spec(spec)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", "bad-footprint-spec",
                    data={"kinds": sorted(FOOTPRINT_REGISTRY)})
    fp = definition.instance("REF", "VAL", x=0.0, y=0.0)
    from pcbai.kicad.sexp import dumps
    snippet = dumps(definition.to_sexp(), indent="  ", inline_limit=70)
    return ok({"name": definition.name, "description": definition.description,
               "source": definition.source, "tags": definition.tags,
               "datasheet": definition.datasheet, "pad_count": len(definition.pads),
               "pads": [{"number": p["number"], "type": p["type"], "shape": p.get("shape"),
                         "at": list(p["at"]), "size": list(p["size"]),
                         "drill": p.get("drill")} for p in definition.pads[:80]],
               "bbox": list(fp.bbox()), "graphics": len(definition.graphics),
               "sexp_preview": snippet[:1200], "sexp_lines": snippet.count("\n") + 1,
               "issues": definition.check(assume_distinct=True)},
              warnings=["the geometry is generated from IPC/millimetre conventions, not from the "
                        "vendor's drawing — check it before ordering stencils"])


# ─────────────────────────────────────────────────────────────────────────────
# mutating (registry enforces approval + confirmation for these)
# ─────────────────────────────────────────────────────────────────────────────
def generate_scaffold_tool(spec: Any, out_dir: Optional[str] = None, stem: Optional[str] = None,
                           dialect: str = "kicad-10", route: bool = True,
                           fanout_power: bool = True, dry_run: bool = False,
                           keep_history: bool = True) -> Dict[str, Any]:
    """Design spec (YAML/JSON path or dict) → KiCad project: placement, planes, fanout, safe tracks.

    ``dry_run=true`` builds and validates without writing, which is what an agent should do before it
    asks for approval.
    """
    out = Path(out_dir or os.path.join(project_root(), "build", "scaffold"))
    try:
        data = load_spec(spec) if not isinstance(spec, dict) else spec
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", "bad-spec",
                    data={"hint": "spec must be a .yaml/.json path or a dict with `parts`"})
    if dry_run:
        from pcbai.kicad.scaffold import build_board
        board = build_board(data)
        issues = board.checks()
        counts = _severity_counts(issues)
        return ok({"dry_run": True, "summary": board.summary(), "counts": dict(counts),
                   "issues": _issues_payload(issues),
                   "placement_overflow": board.variables.get("placement_overflow", ""),
                   "would_write": [str(out / f"{stem or data.get('name', 'board')}{s}"
                                          ) for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru",
                                                      "-bom.csv")]},
                  warnings=["dry run: nothing was written"])
    try:
        result = generate(data, out, stem=stem, dialect=dialect, route=route,
                          fanout_power=fanout_power, keep_history=keep_history)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", "generation-failed")
    payload = result.to_dict()
    payload["next_steps"] = [
        "open the board in KiCad and use the ratsnest (G) for the nets listed in `unrouted_nets`",
        "confirm the part pin maps against the SV-16 datasheet — the placeholder map is marked in "
        "the spec header",
        "run `kicad-cli pcb drc` and export gerbers before ordering (CI does this automatically)",
    ]
    if not result.ok:
        return fail("generated board has errors", "validation-failed", data=payload,
                    warnings=[i.message for i in result.issues if i.severity == "error"][:10])
    return ok(payload, warnings=[i.message for i in result.issues][:12])


def write_board_tool(board_spec: Any, out_dir: Optional[str] = None, stem: str = "board",
                     dialect: str = "kicad-10", reason: str = "agent write") -> Dict[str, Any]:
    """Write a full board from a dict (``{"nets": …, "footprints": …}`` or scaffold spec) to disk.

    ``board_spec`` is interpreted as a scaffold spec when it has ``parts``, otherwise as a raw
    :class:`Board` JSON dump (``Board.to_json``). Both paths end with a re-read + validate.
    """
    out = Path(out_dir or os.path.join(project_root(), "build", "scaffold"))
    data = board_spec
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = load_spec(data)
    try:
        if isinstance(data, dict) and data.get("footprints"):
            board = Board.from_json(data)
            out.mkdir(parents=True, exist_ok=True)
            files = write_project_files(board, out, stem=stem, dialect=dialect, reason=reason)
        else:
            result = generate(data, out, stem=stem, dialect=dialect)
            board, files = result.board, result.files
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}", "write-failed")
    issues = [i for i in board.checks() if i.severity == "error"]
    back = read_board(files["board"])
    back_errors = [i for i in back.checks() if i.severity == "error"]
    if back_errors:
        return fail("written file does not re-read clean", "verify-failed",
                    data={"files": files, "errors": _issues_payload(back_errors)},
                    warnings=sorted({str(i) for i in back_errors})[:10])
    return ok({"files": files, "summary": board.summary(),
               "pre_write_errors": len(issues), "readback": back.summary()},
              warnings=[str(i) for i in issues][:10], verified=True)


EDIT_OPS = ("move_component", "rotate_component", "set_dnp", "set_rule", "set_net_class",
            "assign_net", "rename_net", "set_track_width", "delete_tracks", "delete_vias",
            "add_keepout", "set_outline", "set_layer_count", "add_component")


def apply_edit_tool(edits: Sequence[Dict[str, Any]], path: Optional[str] = None,
                    dry_run: bool = True, allow_lossy: bool = False,
                    dialect: Optional[str] = None) -> Dict[str, Any]:
    """Apply a list of model-level edits to a board, write, then re-read and re-validate.

    This is the only sanctioned way for the agent to touch an existing board: no regex, no string
    surgery. Ops::

        {"op": "move_component", "ref": "U1", "x": 30, "y": 20}          # or "dx"/"dy"
        {"op": "rotate_component", "ref": "U1", "angle_deg": 90}
        {"op": "set_dnp", "ref": "C1", "dnp": true}
        {"op": "set_rule", "field": "min_clearance", "value": 0.15}
        {"op": "set_net_class", "name": "Power", "clearance": 0.25, "track_width": 0.6,
         "nets": ["+3V3", "+5V"]}
        {"op": "assign_net", "ref": "R1", "pad": "1", "net": "NET_5V"}
        {"op": "rename_net", "from": "SDA", "to": "I2C_SDA"}
        {"op": "set_track_width", "net": "VBUS", "width": 0.6}           # resizes existing tracks
        {"op": "delete_tracks", "net": "VBUS"}                             # or "index": 12
        {"op": "delete_vias", "net": "GND"}
        {"op": "add_keepout", "layer": "In1.Cu", "margin": 0.5}
        {"op": "set_outline", "width": 50, "height": 40}                  # or "polygon": [[x,y]…]
        {"op": "set_layer_count", "copper": 4}
        {"op": "add_component", "ref": "R9", "value": "10k", "footprint": "0402", "at": [3, 4],
         "pins": {"1": "VDD", "2": "GND"}}

    Unknown/typo'd ops and unreachable refs are reported in ``data.rejected`` — never guessed at.
    """
    board, meta = _load(path)
    if board is None:
        return meta or fail("no board", "not-found")
    board_path = Path(meta["path"])
    dialect = dialect or format_dialect(read_tree(board_path)).get("dialect", "kicad-10") \
        .replace("+", "")
    if dialect not in DIALECTS:
        dialect = "kicad-10"
    applied: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for edit in edits or []:
        op = str(edit.get("op", ""))
        if op not in EDIT_OPS:
            rejected.append({"edit": edit, "why": f"unknown op {op!r}; usable ops: "
                                                  f"{', '.join(EDIT_OPS)}"})
            continue
        try:
            note = _apply_one(board, edit)
        except ValueError as exc:
            rejected.append({"edit": edit, "why": str(exc)})
            continue
        applied.append(note or {"op": op})
    if not applied:
        return fail("nothing to do", "no-op-applied",
                    data={"rejected": rejected, "hint": "check the refs/fields in each edit"})
    loss = lossiness(board, board_path, dialect=dialect)
    if loss["lossy"] and not allow_lossy:
        return fail("this board contains geometry the model does not reproduce; writing it back "
                    "would drop data", "needs-lossy-approval",
                    data={"lossiness": loss, "hint": "re-call with allow_lossy=true only when the "
                                                     "dropped items are meant to go, or edit the "
                                                     "board in KiCad instead"})
    issues = [i for i in board.checks() if i.severity == "error"]
    payload = {"applied": applied, "rejected": rejected, "path": str(board_path),
               "dry_run": dry_run, "lossiness": loss,
               "would_have_errors": [str(i) for i in issues],
               "summary": board.summary()}
    if dry_run:
        return ok({**payload, "written": False},
                  warnings=["dry run: nothing was written"] + [str(i) for i in issues][:8])
    if issues:
        return fail("edits produce a board that fails our checks", "validation-failed",
                    data={**payload, "written": False}, warnings=[str(i) for i in issues][:12])
    out_files = write_project_files(board, board_path.parent, stem=board_path.stem,
                                   dialect=dialect, reason="agent apply_edit")
    back = read_board(out_files["board"])
    back_issues = [i for i in back.checks() if i.severity == "error"]
    if back_issues:
        return fail("edits written but the file fails validation on re-read", "verify-failed",
                    data={**payload, "written": True, "files": out_files,
                          "readback_errors": _issues_payload(back_issues)})
    warns = [] if not rejected else [f"{len(rejected)} of {len(list(edits or []))} edit(s) were "
                                     "rejected; see data.rejected"]
    if loss["lossy"]:
        warns.append("written with allow_lossy=true: dropped "
                     f"{loss['dropped_top_level'] or loss['dropped_in_footprints']}")
    return ok({**payload, "written": True, "files": out_files,
               "readback": back.summary(), "verified": True}, warnings=warns)


def _apply_one(board: Board, edit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    op = str(edit["op"])
    rules = board.rules

    def find_fp(ref: str):
        fp = board.find(ref)
        if fp is None:
            raise ValueError(f"no component '{ref}' on this board")
        return fp

    if op == "move_component":
        fp = find_fp(str(edit["ref"]))
        dx, dy = float(edit.get("dx", 0.0)), float(edit.get("dy", 0.0))
        fp.x = round(float(edit["x"]) if "x" in edit else fp.x + dx, 4)
        fp.y = round(float(edit["y"]) if "y" in edit else fp.y + dy, 4)
        return {"op": op, "ref": fp.reference, "at": [fp.x, fp.y]}
    if op == "rotate_component":
        fp = find_fp(str(edit["ref"]))
        step = float(edit.get("angle_deg", 90.0))
        fp.rot = round((fp.rot + step) % 360.0, 4) if "angle_deg" in edit else float(edit["rot"])
        return {"op": op, "ref": fp.reference, "rot": fp.rot}
    if op == "set_dnp":
        fp = find_fp(str(edit["ref"]))
        fp.dnp = bool(edit.get("dnp", True))
        return {"op": op, "ref": fp.reference, "dnp": fp.dnp}
    if op == "set_rule":
        field = str(edit["field"])
        if not hasattr(rules, field) or field == "net_classes":
            raise ValueError(f"unknown rule field {field!r}; e.g. min_clearance, min_track_width, "
                             "min_via_drill, min_copper_to_edge")
        setattr(rules, field, float(edit["value"]))
        return {"op": op, "field": field, "value": getattr(rules, field)}
    if op == "set_net_class":
        name = str(edit.get("name", "Default"))
        cls = rules.net_classes.get(name) or NetClass(name)
        for key in ("clearance", "track_width", "via_diameter", "via_drill", "microvia_diameter",
                    "microvia_drill", "diff_pair_gap", "diff_pair_width"):
            if key in edit:
                setattr(cls, key, float(edit[key]))
        if "nets" in edit:
            cls.nets = [str(n) for n in edit["nets"]]
            for net in cls.nets:
                board.net(net, net_class=name)
        rules.net_classes[name] = cls
        return {"op": op, "class": name, "clearance": cls.clearance, "track_width": cls.track_width,
                "nets": len(cls.nets)}
    if op == "assign_net":
        fp = find_fp(str(edit["ref"]))
        pad_no, net = str(edit["pad"]), str(edit["net"])
        pad = next((p for p in fp.pads if str(p.number) == pad_no), None)
        if pad is None:
            raise ValueError(f"{fp.reference} has no pad '{pad_no}' "
                             f"(pads: {', '.join(str(p.number) for p in fp.pads[:24])})")
        pad.net = net
        board.net(net, net_class=rules.clearance_for(net).name)
        return {"op": op, "pad": f"{fp.reference}.{pad_no}", "net": net}
    if op == "rename_net":
        old, new = str(edit["from"]), str(edit["to"])
        n = 0
        for fp in board.footprints:
            for pad in fp.pads:
                if pad.net == old:
                    pad.net = new
                    n += 1
        for t in board.tracks:
            if t.net == old:
                t.net = new
                n += 1
        for v in board.vias:
            if v.net == old:
                v.net = new
                n += 1
        for z in board.zones:
            if z.net == old:
                z.net = new
                n += 1
        if not n:
            raise ValueError(f"net '{old}' is not used anywhere on this board")
        board.net(new)
        return {"op": op, "from": old, "to": new, "objects": n}
    if op == "set_track_width":
        net, width = str(edit["net"]), float(edit["width"])
        n = 0
        for t in board.tracks:
            if t.net == net:
                t.width = width
                n += 1
        cls = rules.net_classes.get(rules.clearance_for(net).name)
        if cls is not None:
            cls.track_width = max(cls.track_width, width)
        return {"op": op, "net": net, "width": width, "tracks": n}
    if op in ("delete_tracks", "delete_vias"):
        kind = "tracks" if op == "delete_tracks" else "vias"
        items = getattr(board, kind)
        if "index" in edit:
            idx = int(edit["index"])
            if not 0 <= idx < len(items):
                raise ValueError(f"{kind} index {idx} out of range (0…{len(items) - 1})")
            del items[idx]
            return {"op": op, "removed": 1, "index": idx}
        net = str(edit.get("net", ""))
        keep = [x for x in items if x.net != net]
        removed = len(items) - len(keep)
        if not removed:
            raise ValueError(f"no {kind} on net '{net}'")
        setattr(board, kind, keep)
        return {"op": op, "net": net, "removed": removed}
    if op == "add_keepout":
        from pcbai.kicad.model import Zone
        box = board.outline_bbox() or board.bbox() or (0, 0, 40, 30)
        margin = float(edit.get("margin", 0.5))
        layer = str(edit.get("layer", board.copper[-1] if board.copper else "In1.Cu"))
        zone = Zone(net="", layer=layer, name=str(edit.get("name", "agent_keepout")),
                    outline=[(box[0] + margin, box[1] + margin), (box[2] - margin, box[1] + margin),
                             (box[2] - margin, box[3] - margin), (box[0] + margin, box[3] - margin)],
                    clearance=float(edit.get("clearance", rules.min_clearance)), fill=False,
                    keepout=True, hatch_style="full")
        board.zones.append(zone)
        return {"op": op, "layer": layer, "margin": margin}
    if op == "set_outline":
        if edit.get("polygon"):
            board.set_outline_polygon([(float(p[0]), float(p[1])) for p in edit["polygon"]])
        else:
            board.set_outline_rect(float(edit["width"]), float(edit["height"]),
                                  origin=tuple(edit.get("origin", (0.0, 0.0))))
        box = board.outline_bbox()
        return {"op": op, "size": [box[2] - box[0], box[3] - box[1]]}
    if op == "set_layer_count":
        from pcbai.kicad.layers import copper_layers as _cl
        n = int(edit.get("copper", 4))
        board.n_copper, board.copper = n, _cl(n)
        return {"op": op, "copper_layers": n, "layers": list(board.copper)}
    if op == "add_component":
        definition = from_spec(edit.get("footprint", "0402"))
        at = edit.get("at")
        fp = definition.instance(str(edit["ref"]), str(edit.get("value", "")),
                                 x=float(at[0]) if at else 0.0, y=float(at[1]) if at else 0.0,
                                 rot=float(edit.get("rot", 0.0)),
                                 layer=str(edit.get("layer", "F.Cu")),
                                 pad_nets={str(k): str(v) for k, v in (edit.get("pins") or {}).items()})
        board.add_footprint(fp)
        for net in fp.pad_nets():
            board.net(net)
        return {"op": op, "ref": fp.reference, "footprint": fp.lib_id, "pads": len(fp.pads)}
    raise ValueError(f"op {op!r} is listed but not implemented (bug in pcbai.agent.kicad_tools)")


# ─────────────────────────────────────────────────────────────────────────────
# binding
# ─────────────────────────────────────────────────────────────────────────────
TOOL_FUNCTIONS: Dict[str, Any] = {
    "inspect_pcb": inspect_pcb_tool,
    "list_components": list_components_tool,
    "get_component": get_component_tool,
    "get_nets": get_nets_tool,
    "get_board_outline": get_board_outline_tool,
    "get_design_rules": get_design_rules_tool,
    "validate_board": validate_board_tool,
    "parse_sexp": parse_sexp_tool,
    "list_footprint_kinds": list_footprint_kinds_tool,
    "inspect_footprint": inspect_footprint_tool,
    "generate_scaffold": generate_scaffold_tool,
    "write_board": write_board_tool,
    "apply_edit": apply_edit_tool,
}

#: tools that write, so the CLI and the host can enforce one flag instead of a list
MUTATING_TOOLS = frozenset({"generate_scaffold", "write_board", "apply_edit"})
