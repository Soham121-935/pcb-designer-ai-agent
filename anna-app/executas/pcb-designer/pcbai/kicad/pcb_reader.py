"""``.kicad_pcb`` → :class:`~pcbai.kicad.model.Board` reader (KiCad 6 → 10).

Two ways to look at a board file, both provided here:

* :func:`read_tree` — the raw :class:`~pcbai.kicad.sexp.Sexp` tree. Use this for **mutations**: it
  carries every byte of the original, so an edit only changes what you touched.
* :func:`read_board` — a typed :class:`Board`. Use this for **inspection**: nets, pads, rules, zones,
  outline, plus geometry checks that do not need ``pcbnew``.

Both dialects are handled: KiCad ≤ 9 references nets by code (``(net 4 "SPI_CLK")`` with a top-level
``(net N "name")`` table) while KiCad 10-dev onwards stores only the name. Whatever the file says, the
model keeps names, because an agent should never have to reason about an internal index.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ..core.logger import get_logger
from .layers import LAYER_ORDINALS, LayerSpec, is_copper
from .model import (Board, DesignRules, Footprint, Graphic, Net, NetClass, Pad, StackLayer, Track,
                    Via, Zone)
from .sexp import Sexp, parse

__all__ = ["read_tree", "write_tree", "read_board", "board_from_tree", "net_table", "layers_of",
           "format_dialect"]

LOG = get_logger("kicad.reader")


# ─────────────────────────────────────────────────────────────────────────────
# tree access
# ─────────────────────────────────────────────────────────────────────────────
def read_tree(path: Union[str, Path]) -> Sexp:
    return parse(Path(path).read_text(encoding="utf-8"))


def write_tree(path: Union[str, Path], tree: Sexp, *, indent: str = "\t") -> None:
    """Write a (possibly edited) tree. Formatting of untouched regions is preserved."""
    from ..core.filesafe import atomic_write_text
    from .sexp import dumps
    atomic_write_text(Path(path), dumps(tree, indent=indent))


def format_dialect(tree: Sexp) -> Dict[str, object]:
    """What KiCad wrote this file, as far as the header tells us."""
    version = tree.int("version", 0)
    return {
        "version_token": version,
        "generator": tree.scalar("generator", "?"),
        "generator_version": tree.scalar("generator_version", ""),
        "net_table_present": bool(tree.children("net")),
        "net_class_in_board": bool(tree.children("net_class")),
        "dialect": ("kicad-10+" if not tree.children("net")
                    else "kicad-8/9" if version >= 20240108 else "kicad-6/7"),
    }


def layers_of(tree: Sexp) -> Dict[str, int]:
    """``{"F.Cu": 0, "In1.Cu": 4, …}`` — the ordinals the file itself declares."""
    out: Dict[str, int] = {}
    node = tree.child("layers")
    if node is None:
        return out
    for entry in node.items[1:]:
        if entry.is_list() and len(entry.items) >= 2:
            out[entry.items[1].s()] = entry.items[0].i(-1)
    return out


def net_table(tree: Sexp) -> Dict[int, str]:
    return {c.items[0].i(): c.items[1].s() for c in tree.children("net") if len(c.items) > 1}


# ─────────────────────────────────────────────────────────────────────────────
# model
# ─────────────────────────────────────────────────────────────────────────────
def _stamp(node: Sexp) -> str:
    """KiCad 10 says ``uuid``, KiCad 7/8 said ``tstamp``; both mean "stable object id"."""
    for key in ("uuid", "tstamp"):
        child = node.child(key)
        if child is not None:
            return child.s("")
    return ""


def _net_ref(node: Sexp, table: Dict[int, str]) -> str:
    """Resolve ``(net 4 "SPI")`` / ``(net "SPI")`` / ``(net 4)`` — on *node* or a ``net`` child."""
    target = node if (node.is_list() and node.head == "net") else node.child("net")
    if target is None:
        return ""
    kids = [k for k in target.items[1:] if k.string is not None]
    for k in kids:
        if not k.s().isdigit():
            return k.s()
    for k in kids:                       # code-only reference (segment/via/zone in KiCad ≤ 9)
        return table.get(k.i(), "")
    return ""


def _point(node: Optional[Sexp], default: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
    if node is None:
        return default
    vals = [v.num() for v in node.items[1:] if v.string is not None]
    return (vals[0], vals[1]) if len(vals) >= 2 else default


def _vec(node: Optional[Sexp], n: int, default: float = 0.0) -> List[float]:
    if node is None:
        return [default] * n
    vals = [v.num() for v in node.items[1:] if v.string is not None]
    return (vals + [default] * n)[:n]


def _pad_from(node: Sexp, table: Dict[int, str]) -> Pad:
    toks = node.tokens()
    drill: Optional[Union[float, Tuple[float, float]]] = None
    d = node.child("drill")
    if d is not None:
        parts = d.items[1:]
        if parts and parts[0].s() == "oval" and len(parts) > 2:
            drill = (parts[1].num(), parts[2].num())
        elif len(parts) == 2 and all(p.string and p.string.replace(".", "", 1).isdigit() for p in parts):
            drill = (parts[0].num(), parts[1].num())
        elif parts:
            drill = parts[0].num(0.0) or None
    layers_node = node.child("layers")
    return Pad(number=toks[0] if toks else "",
               type=toks[1] if len(toks) > 1 else "smd",
               shape=toks[2] if len(toks) > 2 else "rect",
               at=tuple(_vec(node.child("at"), 3)),                    # type: ignore[arg-type]
               size=tuple(_vec(node.child("size"), 2)),                # type: ignore[arg-type]
               drill=drill,
               layers=tuple(layers_node.tokens()) if layers_node else (),
               net=_net_ref(node, table),
               roundrect_rratio=node.number("roundrect_rratio", 0.25) if node.has("roundrect_rratio")
               else None,
               pin_function=node.scalar("pinfunction", "") or None,
               pin_type=node.scalar("pintype", "") or None,
               solder_mask_margin=node.number("solder_mask_margin", 0.0) if node.has("solder_mask_margin")
               else None,
               zone_connect=node.int("zone_connect", 0) if node.has("zone_connect") else None,
               uuid=_stamp(node))


def _footprint_from(node: Sexp, table: Dict[int, str]) -> Footprint:
    at = _vec(node.child("at"), 3)
    fp = Footprint(lib_id=node.value(""), x=at[0], y=at[1], rot=at[2],
                   layer=node.scalar("layer", "F.Cu"),
                   description=node.scalar("descr", ""), tags=node.scalar("tags", ""),
                   path=node.scalar("path", ""), uuid=_stamp(node))
    attr = node.child("attr")
    if attr is not None:
        toks = attr.tokens()
        fp.attr = [t for t in toks if t in ("smd", "through_hole", "mixed")] or ["smd"]
        fp.dnp = "dnp" in toks
        fp.exclude_from_bom = "exclude_from_bom" in toks
    for prop in node.children("property"):
        name = prop.value()
        value = prop.items[2].s("") if len(prop.items) > 2 else ""
        if name == "Reference":
            fp.reference = value
        elif name == "Value":
            fp.value = value
        elif name == "Datasheet":
            fp.datasheet = value
        elif name == "Description":
            fp.description = fp.description or value
        elif name:
            fp.properties[name] = value
    for pad_node in node.children("pad"):
        fp.pads.append(_pad_from(pad_node, table))
    for kind in ("line", "rect", "circle", "text", "arc"):
        for g in node.children(f"fp_{kind}"):
            fp.graphics.append(_graphic_from(g, kind))
    return fp


def _graphic_from(node: Sexp, kind: str) -> Graphic:
    layer = node.scalar("layer", "")
    stroke = node.child("stroke")
    width = stroke.number("width", 0.12) if stroke is not None else 0.12
    stroke_type = stroke.scalar("type", "solid") if stroke is not None else "solid"
    if kind == "text":
        at = _vec(node.child("at"), 3)
        return Graphic("text", layer, at=(at[0], at[1]), rot=at[2], text=node.value(""),
                       width=width, uuid=_stamp(node))
    if kind == "circle":
        center = _point(node.child("center"))
        end = _point(node.child("end"))
        return Graphic("circle", layer, center=center, radius=abs(end[0] - center[0]), width=width,
                       uuid=_stamp(node))
    if kind == "rect":
        start, end = _point(node.child("start")), _point(node.child("end"))
        return Graphic("rect", layer, start=start, size=(end[0] - start[0], end[1] - start[1]),
                       width=width, uuid=_stamp(node))
    mid = _point(node.child("mid")) if node.child("mid") else None
    return Graphic(kind, layer, start=_point(node.child("start")), end=_point(node.child("end")),
                   center=mid, width=width, stroke_type=stroke_type, uuid=_stamp(node))


def _zone_from(node: Sexp, table: Dict[int, str]) -> Zone:
    poly = node.child("polygon")
    pts: List[Tuple[float, float]] = []
    if poly is not None:
        pts_node = poly.child("pts")
        for xy in (pts_node.children("xy") if pts_node is not None else poly.children("xy")):
            vals = [v.num() for v in xy.items[1:] if v.string is not None]
            if len(vals) >= 2:
                pts.append((vals[0], vals[1]))
    connect = node.child("connect_pads")
    fill = node.child("fill")
    hatch = node.child("hatch")
    return Zone(net=_net_ref(node, table), layer=node.scalar("layer", ""), outline=pts,
                name=node.scalar("name", ""), priority=node.int("priority", 0),
                min_thickness=node.number("min_thickness", 0.25),
                clearance=connect.number("clearance", 0.25) if connect is not None else 0.25,
                thermal_gap=fill.number("thermal_gap", 0.25) if fill is not None else 0.25,
                thermal_bridge_width=(fill.number("thermal_bridge_width", 0.5) if fill is not None
                                      else 0.5),
                fill=fill.truthy(True) if fill is not None else True,
                hatch_style=hatch.value("edge") if hatch is not None else "edge",
                hatch_spacing=(hatch.items[2].num(0.5) if hatch is not None and len(hatch.items) > 2
                               else 0.5),
                keepout=node.has("keepout"), uuid=_stamp(node))


_SKIP_TOKENS = frozenset({"net", "zone", "segment", "via", "footprint", "setup", "layers",
                          "general", "title_block", "version", "generator", "generator_version",
                          "paper", "embedded_fonts", "lib_symbols", "property", "net_class"})


def board_from_tree(tree: Sexp, *, source: str = "") -> Board:
    """Build a :class:`Board` from a parsed tree (never mutates the tree)."""
    table = net_table(tree)
    board = Board(nets=[Net(name) for name in table.values() if name], uuid=_stamp(tree))
    gen = tree.child("general")
    if gen is not None:
        board.thickness = gen.number("thickness", 1.6)
    board.paper = tree.scalar("paper", "A4")
    tb = tree.child("title_block")
    if tb is not None:
        board.title = tb.scalar("title", board.title)
        board.revision = tb.scalar("rev", board.revision)
        board.company = tb.scalar("company", "")
        comment = tb.child("comment")
        if comment is not None:
            board.comment = comment.s(board.comment)

    file_layers = layers_of(tree)
    if file_layers:
        copper = [n for n in file_layers if is_copper(n)]
        if copper:
            board.copper = _stack_order(copper)
            board.n_copper = len(board.copper)
        board.extra_layers = [LayerSpec(n, "user") for n in file_layers if not is_copper(n)]

    for fp_node in tree.children("footprint"):
        board.add_footprint(_footprint_from(fp_node, table))

    for node in tree.items:
        if node.string is not None:
            continue
        head = node.head
        if head in ("gr_line", "gr_rect", "gr_circle", "gr_text", "gr_arc", "gr_poly"):
            kind = {"gr_line": "line", "gr_rect": "rect", "gr_circle": "circle",
                    "gr_text": "text", "gr_arc": "arc", "gr_poly": "poly"}[head]
            graphic = _graphic_from(node, kind)
            (board.edge if graphic.layer == "Edge.Cuts" else board.graphics).append(graphic)
        elif head == "segment":
            board.tracks.append(Track(_point(node.child("start")), _point(node.child("end")),
                                      net=_net_ref(node, table), width=node.number("width", 0.25),
                                      layer=node.scalar("layer", "F.Cu"), uuid=_stamp(node)))
        elif head == "via":
            toks = node.tokens()
            board.vias.append(Via(_point(node.child("at")), net=_net_ref(node, table),
                                  size=node.number("size", 0.6), drill=node.number("drill", 0.3),
                                  layers=tuple(node.child("layers").tokens())
                                  if node.child("layers") else (),
                                  type=toks[0] if toks and toks[0] in ("blind", "micro", "through",
                                                                       "buried") else "through",
                                  uuid=_stamp(node)))
        elif head == "zone":
            board.zones.append(_zone_from(node, table))

    board.rules = _rules_from(tree, table)
    _stackup_from(tree, board)
    if source:
        board.variables["_source"] = str(source)
        board.variables["_dialect"] = str(format_dialect(tree)["dialect"])
    return board


def _stack_order(names: Sequence[str]) -> List[str]:
    """Front → back: F.Cu, In1.Cu … InN.Cu, B.Cu."""
    def key(name: str) -> int:
        if name == "F.Cu":
            return -1
        if name == "B.Cu":
            return 1000
        if name.startswith("In") and name.endswith(".Cu"):
            try:
                return int(name[2:-3])
            except ValueError:
                return 500
        return 900
    return sorted(names, key=key)


def _rules_from(tree: Sexp, table: Dict[int, str]) -> DesignRules:
    rules = DesignRules()
    setup = tree.child("setup")
    if setup is not None:
        rd = setup.child("rule_dimensions")
        if rd is not None:            # older KiCad copies the constraint table here; we accept both
            for token, attr in (("min_text_height", "min_text_height"),
                                ("min_text_thickness", "min_text_thickness"),
                                ("min_clearance", "min_clearance"), ("other_gap", "min_clearance"),
                                ("min_track_width", "min_track_width"),
                                ("min_hole_to_hole", "min_hole_to_hole"),
                                ("min_edge_clearance", "min_copper_to_edge"),
                                ("min_silk_to_silk_clearance", "min_silk_to_silk")):
                setattr(rules, attr, rd.number(token, getattr(rules, attr)))
        for token, attr in (("trace_min", "min_track_width"), ("clearance_min", "min_clearance"),
                            ("via_min_size", "min_via_diameter"), ("via_min_drill", "min_via_drill"),
                            ("hole_to_hole", "min_hole_to_hole"),
                            ("solder_mask_min_width", "min_mask_web"),
                            ("pad_to_mask_clearance", "pad_to_mask_clearance")):
            if setup.child(token) is not None:
                setattr(rules, attr, setup.number(token, getattr(rules, attr)))
        tent = setup.child("tenting")
        if tent is not None:
            rules.tenting = "no" not in tent.tokens()
    for node in tree.children("net_class"):        # KiCad ≤ 8 kept net classes in the board file
        name = node.value("Default") or "Default"
        cls = NetClass(name)
        cls.clearance = node.number("clearance", cls.clearance)
        cls.track_width = node.number("trace_width", cls.track_width)
        cls.via_diameter = node.number("via_dia", cls.via_diameter)
        cls.via_drill = node.number("via_drill", cls.via_drill)
        cls.nets = [n for n in (c.s() for c in node.children("add_net")) if n]
        rules.net_classes[name] = cls
        if name == "Default":
            rules.min_clearance = cls.clearance
            rules.min_track_width = cls.track_width
            rules.min_via_diameter = cls.via_diameter
            rules.min_via_drill = cls.via_drill
    return rules


def _stackup_from(tree: Sexp, board: Board) -> None:
    setup = tree.child("setup")
    stack = setup.child("stackup") if setup is not None else None
    if stack is None:
        return
    for node in stack.children("layer"):
        name = node.value("")
        type_token = node.scalar("type", "").lower()
        kind = "copper" if is_copper(name) or type_token == "copper" else (
            type_token or ("prepreg" if "pre" in type_token else "core"))
        board.stackup.append(StackLayer(name=name, kind=kind,
                                        thickness=node.number("thickness", 0.0),
                                        material=node.scalar("material", ""),
                                        epsilon_r=node.number("epsilon_r", 0.0)
                                        if node.has("epsilon_r") else None,
                                        loss_tangent=node.number("loss_tangent", 0.0)
                                        if node.has("loss_tangent") else None,
                                        id=LAYER_ORDINALS.get(name, 0)))


def rules_from_project(path: Union[str, Path]) -> Optional[DesignRules]:
    """Read the constraint table + net classes a project keeps in its ``.kicad_pro``.

    KiCad 10 stores net classes and board minimums in the project file, not the board file, so a
    ``.kicad_pcb`` alone cannot answer "is this clearance legal?" — without this merge the checker
    would fall back to the model defaults and disagree with both KiCad and the generator.
    """
    p = Path(path)
    pro = p.with_suffix(".kicad_pro")
    if not pro.exists():
        return None
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    board = data.get("board") if isinstance(data, dict) else None
    if not isinstance(board, dict):
        return None
    rules = DesignRules()
    ds = (board.get("design_settings") or {})
    cfg = ds.get("rules") or {}
    # KiCad 10's own key names, from board.design_settings.rules (verified against the reference
    # project's .kicad_pro — this is the list Board Setup writes, and anything else is ignored).
    for token, attr in (("min_clearance", "min_clearance"), ("min_track_width", "min_track_width"),
                        ("min_via_diameter", "min_via_diameter"),
                        ("min_through_hole_diameter", "min_via_drill"),
                        ("min_via_annular_width", "annular_ring_min"),
                        ("min_hole_to_hole", "min_hole_to_hole"),
                        ("min_copper_edge_clearance", "min_copper_to_edge"),
                        ("min_silk_clearance", "min_silk_to_silk"),
                        ("min_text_height", "min_text_height"),
                        ("min_text_thickness", "min_text_thickness"),
                        ("solder_mask_to_copper_clearance", "pad_to_mask_clearance"),
                        # names pcbai itself used before the project file was aligned with KiCad 10:
                        # old generated projects must keep validating the same way they did
                        ("clearance", "min_clearance"), ("edge_clearance", "min_copper_to_edge"),
                        ("hole_to_hole", "min_hole_to_hole"), ("silk_text_height", "min_text_height"),
                        ("silk_text_thickness", "min_text_thickness"),
                        ("silk_line_width", "min_text_thickness"),
                        ("solder_mask_bridge_clearance", "min_mask_web")):
        value = cfg.get(token)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(rules, attr, float(value))
    for token, attr in (("track_widths", "min_track_width"), ("via_drills", "min_via_drill"),
                        ("via_sizes", "min_via_diameter"), ("annular_widths", "annular_ring_min"),
                        ("microvia_drills", "min_via_drill"),
                        ("microvia_diameters", "min_via_diameter")):   # legacy array-shaped keys
        values = cfg.get(token) or []
        if values and isinstance(values[0], (int, float)) and values[0] > 0:
            setattr(rules, attr, float(values[0]))
    # ``design_settings.defaults`` is deliberately NOT scraped: those are the *drawing* defaults
    # (what width a new graphic gets), not constraint minima — treating them as rules made
    # board_outline_line_width 0.05 masquerade as a 0.05 mm minimum track width.
    extra = ((board.get("design_settings") or {}).get("pcbai")) or {}
    for token, attr in (("annular_ring_min", "annular_ring_min"),
                       ("min_courtyard_clearance", "min_courtyard_clearance"),
                       ("min_silk_to_silk", "min_silk_to_silk"), ("min_mask_web", "min_mask_web"),
                       ("pad_to_mask_clearance", "pad_to_mask_clearance")):
        if isinstance(extra.get(token), (int, float)):
            setattr(rules, attr, float(extra[token]))
    if isinstance(extra.get("tenting"), bool):
        rules.tenting = extra["tenting"]
    # KiCad 10 keeps net classes at the *top level* of the project file. ``board.net_settings`` is
    # read too because projects generated before this was fixed put them there; new files do not.
    net_settings = data.get("net_settings") or board.get("net_settings") or {}
    classes = net_settings.get("classes") or []
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        cls = NetClass(str(entry.get("name", "Default")))
        for key, attr in (("clearance", "clearance"), ("track_width", "track_width"),
                          ("via_diameter", "via_diameter"), ("via_drill", "via_drill"),
                          ("microvia_diameter", "microvia_diameter"),
                          ("microvia_drill", "microvia_drill"), ("diff_pair_gap", "diff_pair_gap"),
                          ("diff_pair_width", "diff_pair_width")):
            if isinstance(entry.get(key), (int, float)):
                setattr(cls, attr, float(entry[key]))
        for token, attr in (("annular_width", "annular_width"),
                            ("courtyard_clearance", "courtyard_clearance")):
            if isinstance(entry.get(token), (int, float)):
                setattr(cls, attr, float(entry[token]))
        cls.nets = [str(n) for n in (entry.get("nets") or [])]
        rules.net_classes[cls.name] = cls
        # a project written by KiCad has no pcbai block, so the Default class carries the board-wide
        # annular/courtyard numbers back into the model
        if cls.name == "Default":
            if isinstance(entry.get("annular_width"), (int, float)):
                rules.annular_ring_min = float(entry["annular_width"])
            if isinstance(entry.get("courtyard_clearance"), (int, float)):
                rules.min_courtyard_clearance = float(entry["courtyard_clearance"])
    return rules


def merge_project_rules(board: Board, path: Union[str, Path, None] = None) -> Board:
    """Apply :func:`rules_from_project` to *board* in place (no-op when no project file is found)."""
    if path is None:
        path = board.source
    found = rules_from_project(path) if path else None
    if found is not None:
        found_classes = found.net_classes
        board.rules = found if found_classes else board.rules
        board.variables["rules_source"] = str(Path(path).with_suffix(".kicad_pro").name)
    return board


def read_board(path: Union[str, Path], *, merge_project: bool = True) -> Board:
    """Parse a ``.kicad_pcb`` into a :class:`Board`. Raises :class:`SexpParseError` on garbage.

    With *merge_project* (default) a sibling ``.kicad_pro`` is consulted for the net classes and
    board minimums, exactly like KiCad does when it opens a project.
    """
    p = Path(path)
    tree = read_tree(p)
    if tree.head != "kicad_pcb":
        raise ValueError(f"{p.name}: root token is {tree.head!r}, expected 'kicad_pcb' — is this a "
                         "board file?")
    board = board_from_tree(tree, source=p)
    if merge_project:
        merge_project_rules(board, p)
    return board
