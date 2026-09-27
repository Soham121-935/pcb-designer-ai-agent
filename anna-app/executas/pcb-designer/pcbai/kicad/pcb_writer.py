"""``Board`` → ``.kicad_pcb`` / ``.kicad_pro`` / ``.kicad_dru`` writers for KiCad 10.

Dialect facts used here were read out of KiCad's own sources (2026-09) rather than guessed, because a
third-party generator that "looks like" KiCad output but is subtly wrong wastes a whole debugging
session in the GUI:

=======================================================  ==========================================
Fact                                                    Source
=======================================================  ==========================================
``(version 20260206)`` is KiCad 10.0's last board        ``pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.h``
version token; 11.0-dev starts at 20260410                ``SEXPR_BOARD_FILE_VERSION`` list
KiCad **stopped writing netcodes** in ``20251028``        "Stop writing netcodes; they're an internal
(object nets are referenced by name)                      implementation detail" → no ``(net 0 "")`` table
Layer ordinals are the KiCad 9+ enum: ``F.Cu``=0,         ``include/layer_ids.h`` (see :mod:`.layers`)
``B.Cu``=2, ``In1.Cu``=4, ``In2.Cu``=6 …
Board objects carry ``(uuid "…")``, not ``(tstamp …)``    real KiCad 10 board in
``net_class`` lives in ``.kicad_pro`` →                   ``pcbai/steps/template_project/board.kicad_pcb``
``net_settings.classes``, not in the board file           ditto (``net_class count == 0``)
=======================================================  ==========================================

Use ``dialect="kicad-9"`` to emit the older shape (net table + ``(net N "name")`` + ``tstamp``) — the
only reason that exists is that KiCad 9.x is still what many fab-adjacent tools ship.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..core.filesafe import atomic_write_text
from ..core.logger import get_logger
from .layers import LAYER_TYPES, copper_layers, ordinal_for
from .model import Board, Footprint, Graphic, NetClass, Pad, Track, Via, Zone, DesignRules
from .sexp import parse as _sexp_parse
from .sexp import Sexp, atom, dumps

def lossiness(board: Board, source: Any = None, *, dialect: Dialect = "kicad-10",
              max_footprints: int = 200) -> Dict[str, Any]:
    """What a model -> file rewrite would *drop* from ``source``.

    The model is a projection of the file format, so re-writing a board we did not author can lose
    tokens (3D ``model`` includes, layers we ignore, KiCad's private extras). Callers consult this
    before writing an edit, so the loss is reported instead of being silent.

    *source* may be a path, raw text, or an already-parsed tree; ``None`` means "compare against
    ourselves", which is the identity case and must come out clean.
    """
    from collections import Counter

    mine = render_board(board, dialect=dialect)
    if isinstance(source, Sexp):
        tree = source
    elif source is None:
        return {"lossy": False, "dropped_top_level": {}, "dropped_in_footprints": {},
                "footprints_not_in_model": 0,
                "footprints_compared": sum(1 for c in mine.items[1:] if c.head == "footprint"),
                "note": "compared against our own render: nothing to lose"}
    elif isinstance(source, Path) or (isinstance(source, str) and len(source) > 240):
        text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        tree = _sexp_parse(text)
    else:
        tree = _sexp_parse(Path(str(source)).read_text(encoding="utf-8"))

    def tally(node: Sexp) -> "Counter":
        return Counter(child.head for child in node.items[1:] if child.head)

    dropped = tally(tree) - tally(mine)
    fp_dropped: Counter = Counter()
    theirs = [c for c in tree.items[1:] if c.head == "footprint"]
    ours = [c for c in mine.items[1:] if c.head == "footprint"]
    for a, b in zip(theirs[:max_footprints], ours[:max_footprints]):
        fp_dropped += tally(a) - tally(b)
    extra_footprints = max(0, len(theirs) - len(ours))
    out: Dict[str, Any] = {
        "lossy": bool(dropped or fp_dropped or extra_footprints),
        "dropped_top_level": {k: v for k, v in dropped.items() if k},
        "dropped_in_footprints": {k: v for k, v in fp_dropped.items() if k},
        "footprints_not_in_model": extra_footprints,
        "footprints_compared": min(len(theirs), len(ours), max_footprints),
    }
    if out["lossy"]:
        out["hint"] = ("the board holds tokens this model does not reproduce, so writing it back "
                       "would delete them. Either extend the model, or mutate the tree with "
                       "pcb_reader.read_tree/write_tree (token-preserving) instead of rewriting it.")
    return out


__all__ = ["Dialect", "render_board", "write_board", "project_json", "write_project_files",
           "design_rules_text", "BOARD_VERSION", "DIALECTS"]

LOG = get_logger("kicad.writer")

#: board-file ``version`` token per dialect (KiCad's own date-stamped format version)
BOARD_VERSION = {"kicad-10": 20260206, "kicad-9": 20241229, "kicad-8": 20240108}
Dialect = str
DIALECTS: Tuple[str, ...] = tuple(BOARD_VERSION)

#: KiCad 10's own plot defaults; kept verbatim so "File → Plot" works on a generated board.
_PCB_PLOT_PARAMS: Sequence[Tuple[str, object]] = (
    ("layerselection", "0x00000000_00000000_55555555_5755f5ff"),
    ("plot_on_all_layers_selection", "0x00000000_00000000_00000000_00000000"),
    ("disableapertmacros", "no"), ("usegerberextensions", "no"), ("usegerberattributes", "yes"),
    ("usegerberadvancedattributes", "yes"), ("creategerberjobfile", "yes"),
    ("dashed_line_dash_ratio", 12.0), ("dashed_line_gap_ratio", 3.0), ("svgprecision", 4),
    ("plotframeref", "no"), ("mode", 1), ("useauxorigin", "no"),
    ("pdf_front_fp_property_popups", "yes"), ("pdf_back_fp_property_popups", "yes"),
    ("pdf_metadata", "yes"), ("pdf_single_document", "no"), ("dxfpolygonmode", "yes"),
    ("dxfimperialunits", "yes"), ("dxfusepcbnewfont", "yes"), ("psnegative", "no"),
    ("psa4output", "no"), ("plot_black_and_white", "yes"), ("sketchpadsonfab", "no"),
    ("plotpadnumbers", "no"), ("hidednponfab", "no"), ("sketchdnponfab", "yes"),
    ("crossoutdnponfab", "yes"), ("subtractmaskfromsilk", "no"), ("outputformat", 1),
    ("mirror", "no"), ("drillshape", 1), ("scaleselection", 1), ("outputdirectory", ""),
)


# ─────────────────────────────────────────────────────────────────────────────
# object renderers
# ─────────────────────────────────────────────────────────────────────────────
def _xy(x: float, y: float) -> Sexp:
    return Sexp.form("at", x, y)


def _effects(size: float = 1.0, thickness: float = 0.15, *, justify: Optional[str] = None) -> Sexp:
    font = Sexp.form("font", Sexp.form("size", size, size), Sexp.form("thickness", thickness))
    eff = Sexp.form("effects", font)
    if justify:
        eff.append(Sexp.form("justify", justify))
    return eff


def _stroke(width: float, kind: str = "solid") -> Sexp:
    return Sexp.form("stroke", Sexp.form("width", width), Sexp.form("type", kind))


def _drill(pad: Pad) -> Optional[Sexp]:
    if pad.drill is None:
        return None
    if isinstance(pad.drill, (tuple, list)):
        return Sexp.form("drill", "oval", float(pad.drill[0]), float(pad.drill[1]))
    return Sexp.form("drill", float(pad.drill))


def render_pad(pad: Pad, *, dialect: Dialect, net_ids: Optional[Dict[str, int]] = None) -> Sexp:
    node = Sexp.form("pad", str(pad.number), pad.type, pad.shape)
    if len(pad.at) == 3 and abs(pad.at[2]) > 1e-9:
        node.append(Sexp.form("at", pad.at[0], pad.at[1], pad.at[2]))
    else:
        node.append(Sexp.form("at", pad.at[0], pad.at[1]))
    node.append(Sexp.form("size", pad.size[0], pad.size[1]))
    d = _drill(pad)
    if d is not None:
        node.append(d)
    node.append(Sexp.form("layers", *[atom(l) for l in pad.layers]))
    if pad.shape == "roundrect":
        node.append(Sexp.form("roundrect_rratio", pad.roundrect_rratio
                              if pad.roundrect_rratio is not None else 0.25))
    if pad.net:
        if net_ids is not None:                       # KiCad <= 9 references nets by code
            node.append(Sexp.form("net", net_ids.get(pad.net, 0), atom(pad.net)))
        else:                                         # KiCad >= 10-dev: name only
            node.append(Sexp.form("net", atom(pad.net)))
    if pad.pin_function:
        node.append(Sexp.form("pinfunction", atom(pad.pin_function)))
    if pad.pin_type:
        node.append(Sexp.form("pintype", atom(pad.pin_type)))
    if pad.solder_mask_margin is not None:
        node.append(Sexp.form("solder_mask_margin", pad.solder_mask_margin))
    if pad.zone_connect is not None:
        node.append(Sexp.form("zone_connect", pad.zone_connect))
    if pad.thermal_gap is not None:
        node.append(Sexp.form("thermal_gap", pad.thermal_gap))
    if pad.thermal_bridge_width is not None:
        node.append(Sexp.form("thermal_bridge_width", pad.thermal_bridge_width))
    node.append(Sexp.form("uuid" if dialect == "kicad-10" else "tstamp", atom(pad.uuid)))
    return node


def _graphic(g: Graphic, *, prefix: str, dialect: Dialect, inside_fp: bool) -> Sexp:
    """Render one drawing primitive (``fp_*`` inside a footprint or ``gr_*`` at board level)."""
    kind = {"line": "line", "rect": "rect", "circle": "circle", "text": "text",
            "arc": "arc"}.get(g.kind, g.kind)
    node = Sexp.form(f"{prefix}_{kind}")
    if kind == "text":
        node.append(atom(g.text))
        at = g.at or (0.0, 0.0)
        node.append(Sexp.form("at", at[0], at[1], g.rot) if g.rot else Sexp.form("at", at[0], at[1]))
    elif kind == "line":
        node.append(Sexp.form("start", g.start[0], g.start[1]))
        node.append(Sexp.form("end", g.end[0], g.end[1]))
    elif kind == "rect":
        node.append(Sexp.form("start", g.start[0], g.start[1]))
        node.append(Sexp.form("end", g.start[0] + g.size[0], g.start[1] + g.size[1]))
    elif kind == "circle":
        node.append(Sexp.form("center", g.center[0], g.center[1]))
        node.append(Sexp.form("end", g.center[0] + g.radius, g.center[1]))
    elif kind == "arc":
        node.append(Sexp.form("start", g.start[0], g.start[1]))
        node.append(Sexp.form("mid", g.center[0], g.center[1]))
        node.append(Sexp.form("end", g.end[0], g.end[1]))
    if kind != "text":
        node.append(_stroke(g.width, g.stroke_type))
    node.append(Sexp.form("layer" if not inside_fp or kind != "text" else "layer", atom(g.layer)))
    if kind == "text":
        node.append(_effects(1.0, 0.15))
    node.append(Sexp.form("uuid" if dialect == "kicad-10" else "tstamp", atom(g.uuid)))
    return node


def render_footprint(fp: Footprint, *, dialect: Dialect,
                     net_ids: Optional[Dict[str, int]] = None) -> Sexp:
    node = Sexp.form("footprint", atom(fp.lib_id))
    node.append(Sexp.form("layer", atom(fp.layer)))
    node.append(Sexp.form("uuid" if dialect == "kicad-10" else "tstamp", atom(fp.uuid)))
    node.append(Sexp.form("at", fp.x, fp.y, fp.rot) if fp.rot else Sexp.form("at", fp.x, fp.y))
    if fp.description:
        node.append(Sexp.form("descr", atom(fp.description)))
    if fp.tags:
        node.append(Sexp.form("tags", atom(fp.tags)))
    if fp.path:
        node.append(Sexp.form("path", atom(fp.path)))
    if fp.dnp:
        node.append(Sexp.form("attr", "through_hole" if "through_hole" in fp.attr else "smd",
                              "board_only", "dnp"))
    for name, value, layer, offset, hide in (
        ("Reference", fp.reference, f"{'B' if fp.layer.startswith('B') else 'F'}.SilkS",
         (0.0, -1.0 * (max(p.size[1] for p in fp.pads) / 2 + 1.0 if fp.pads else 1.0)), False),
        ("Value", fp.value, f"{'B' if fp.layer.startswith('B') else 'F'}.Fab",
         (0.0, 1.0 * (max(p.size[1] for p in fp.pads) / 2 + 1.0 if fp.pads else 1.0)), False),
        ("Datasheet", fp.datasheet, "F.Fab", (0.0, 0.0), True),
        ("Description", fp.description, "F.Fab", (0.0, 0.0), True),
    ):
        prop = Sexp.form("property", atom(name), atom(value), Sexp.form("at", offset[0], offset[1], 0),
                         Sexp.form("layer", atom(layer)))
        if hide:
            prop.append(Sexp.form("hide", "yes"))
        if name in ("Datasheet", "Description"):
            prop.append(Sexp.form("unlinked"))
        prop.append(_effects(1.27, 0.15))
        prop.append(Sexp.form("uuid", atom(_uuid_of(f"{fp.uuid}:{name}"))))
        node.append(prop)
    for extra_name, extra in fp.properties.items():
        if extra_name in {"Reference", "Value", "Datasheet", "Description"}:
            continue
        node.append(Sexp.form("property", atom(extra_name), atom(extra), Sexp.form("at", fp.x, fp.y, 0),
                             Sexp.form("layer", "F.Fab"), Sexp.form("hide", "yes"),
                             _effects(1.27, 0.15),
                             Sexp.form("uuid", atom(_uuid_of(f"{fp.uuid}:{extra_name}")))))
    if fp.attr:
        attr = [a for a in fp.attr if a in ("smd", "through_hole")] or ["smd"]
        flags = list(attr)
        if fp.dnp:
            flags.append("dnp")
        if fp.exclude_from_bom:
            flags.append("exclude_from_bom")
        flags.append("board_only") if not fp.path else None
        node.append(Sexp.form("attr", *flags))
    for g in fp.graphics:
        node.append(_graphic(g, prefix="fp", dialect=dialect, inside_fp=True))
    for pad in fp.pads:
        node.append(render_pad(pad, dialect=dialect, net_ids=net_ids))
    return node


def _uuid_of(seed: str) -> str:
    """Deterministic uuid5-style id from a seed, so regenerating a board does not churn every uuid."""
    import uuid as _u
    return str(_u.uuid5(_u.NAMESPACE_URL, "pcbai:" + seed))


def render_track(t: Track, *, dialect: Dialect, net_ids: Optional[Dict[str, int]] = None) -> Sexp:
    node = Sexp.form("segment", Sexp.form("start", t.start[0], t.start[1]),
                     Sexp.form("end", t.end[0], t.end[1]), Sexp.form("width", t.width),
                     Sexp.form("layer", atom(t.layer)))
    if net_ids is not None:
        node.append(Sexp.form("net", net_ids.get(t.net, 0)))
    else:
        node.append(Sexp.form("net", atom(t.net)))
    node.append(Sexp.form("uuid" if dialect == "kicad-10" else "tstamp", atom(t.uuid)))
    return node


def render_via(v: Via, *, dialect: Dialect, net_ids: Optional[Dict[str, int]] = None) -> Sexp:
    node = Sexp.form("via")
    if dialect != "kicad-10":
        node.append(atom(v.type) if v.type != "through" else atom("through"))
    else:
        node.append(Sexp.form("type", atom(v.type)))
    node.append(Sexp.form("at", v.at[0], v.at[1]))
    node.append(Sexp.form("size", v.size))
    node.append(Sexp.form("drill", v.drill))
    node.append(Sexp.form("layers", *[atom(l) for l in v.layers]))
    if net_ids is not None:
        node.append(Sexp.form("net", net_ids.get(v.net, 0)))
    else:
        node.append(Sexp.form("net", atom(v.net)))
    node.append(Sexp.form("uuid" if dialect == "kicad-10" else "tstamp", atom(v.uuid)))
    return node


def render_zone(z: Zone, *, dialect: Dialect, net_ids: Optional[Dict[str, int]] = None) -> Sexp:
    node = Sexp.form("zone")
    if net_ids is not None:
        node.append(Sexp.form("net", net_ids.get(z.net, 0)))
        node.append(Sexp.form("net_name", atom(z.net)))
    else:
        node.append(Sexp.form("net", atom(z.net)))
    node.append(Sexp.form("layer", atom(z.layer)))
    node.append(Sexp.form("uuid" if dialect == "kicad-10" else "tstamp", atom(z.uuid)))
    if z.name:
        node.append(Sexp.form("name", atom(z.name)))
    if z.priority:
        node.append(Sexp.form("priority", z.priority))
    node.append(Sexp.form("hatch", atom(z.hatch_style if not z.fill else "edge"), z.hatch_spacing))
    node.append(Sexp.form("connect_pads", Sexp.form("clearance", z.clearance)))
    node.append(Sexp.form("min_thickness", z.min_thickness))
    if z.keepout:
        node.append(Sexp.form("fill", "no"))
        node.append(Sexp.form("keepout", Sexp.form("tracks", "not_allowed"),
                             Sexp.form("vias", "not_allowed"), Sexp.form("copperpour", "not_allowed")))
    else:
        node.append(Sexp.form("fill", "yes" if z.fill else "no", Sexp.form("thermal_gap", z.thermal_gap),
                             Sexp.form("thermal_bridge_width", z.thermal_bridge_width)))
    node.append(Sexp.form("polygon", Sexp.form("pts", *[Sexp.form("xy", x, y) for x, y in z.outline])))
    return node


# ─────────────────────────────────────────────────────────────────────────────
# board
# ─────────────────────────────────────────────────────────────────────────────
def render_board(board: Board, *, dialect: Dialect = "kicad-10",
                 generator: str = "pcbai") -> Sexp:
    """Build the full ``(kicad_pcb …)`` tree for *board*."""
    if dialect not in BOARD_VERSION:
        raise ValueError(f"unknown dialect {dialect!r}; expected one of {', '.join(DIALECTS)}")
    net_ids = ({n: i for i, n in enumerate([""] + sorted(board.net_names()))}
               if dialect != "kicad-10" else None)
    root = Sexp.form("kicad_pcb")
    root.append(Sexp.form("version", BOARD_VERSION[dialect]))
    root.append(Sexp.form("generator", atom(generator)))
    if dialect != "kicad-8":
        root.append(Sexp.form("generator_version", atom(dialect.split("-")[1] + ".0")))
    root.append(Sexp.form("general", Sexp.form("thickness", board.thickness),
                         Sexp.form("legacy_teardrops", "no")))
    root.append(Sexp.form("paper", atom(board.paper)))
    tb = Sexp.form("title_block", Sexp.form("title", atom(board.title)),
                   Sexp.form("date", atom(_dt.date.today().isoformat())),
                   Sexp.form("rev", atom(board.revision)))
    if board.company:
        tb.append(Sexp.form("company", atom(board.company)))
    if board.comment:
        tb.append(Sexp.form("comment", 1, atom(board.comment)))
    root.append(tb)

    layers = board.copper or copper_layers(max(2, board.n_copper))
    layers_node = Sexp.form("layers")
    for name in layers:
        layers_node.append(Sexp.form(ordinal_for(name), atom(name),
                                     LAYER_TYPES.get(name, "signal")))
    for spec in board.extra_layers:
        layers_node.append(Sexp.form(spec.ordinal, atom(spec.name), spec.type or "user",
                                     *[atom(spec.user_name)] if spec.user_name else []))
    root.append(layers_node)
    root.append(_render_setup(board, dialect))

    if net_ids is not None:                    # KiCad <= 9 still wants the netcode table
        for name, idx in sorted(net_ids.items(), key=lambda kv: kv[1]):
            root.append(Sexp.form("net", idx, atom(name)))
    for fp in board.footprints:
        root.append(render_footprint(fp, dialect=dialect, net_ids=net_ids))
    for g in board.edge:
        root.append(_graphic(g, prefix="gr", dialect=dialect, inside_fp=False))
    for g in board.graphics:
        root.append(_graphic(g, prefix="gr", dialect=dialect, inside_fp=False))
    for t in board.tracks:
        root.append(render_track(t, dialect=dialect, net_ids=net_ids))
    for v in board.vias:
        root.append(render_via(v, dialect=dialect, net_ids=net_ids))
    for z in board.zones:
        root.append(render_zone(z, dialect=dialect, net_ids=net_ids))
    root.append(Sexp.form("embedded_fonts", "no"))
    return root.beautify(indent=_indent_for(dialect))


def _indent_for(dialect: Dialect) -> str:  # KiCad indents board files with tabs, always
    return "\t"


def _render_setup(board: Board, dialect: Dialect) -> Sexp:
    r = board.rules
    setup = Sexp.form("setup")
    if board.stackup:
        stackup = Sexp.form("stackup")
        for layer in board.stackup:
            if layer.kind in ("core", "prepreg", "air"):
                node = Sexp.form("layer", atom(layer.name if layer.kind == "air" else layer.kind),
                                 Sexp.form("type", atom(layer.kind.capitalize())))
            else:
                node = Sexp.form("layer", atom(layer.name), Sexp.form("type", "Copper"))
            if layer.thickness:
                node.append(Sexp.form("thickness", layer.thickness))
            if layer.material:
                node.append(Sexp.form("material", atom(layer.material)))
            if layer.epsilon_r is not None:
                node.append(Sexp.form("epsilon_r", layer.epsilon_r))
            if layer.loss_tangent is not None:
                node.append(Sexp.form("loss_tangent", layer.loss_tangent))
            stackup.append(node)
        setup.append(stackup)
        setup.append(Sexp.form("copper_finish", atom("HASL with lead free solder")))
        setup.append(Sexp.form("dielectric_constraints", "no"))
    # No rule_dimensions block: the KiCad-10 reference file in steps/template_project has none, and
    # inventing tokens inside (setup …) is how a third-party writer gets its file rejected. The
    # board-wide minimums are recorded in .kicad_pro under board.design_settings.rules, which is
    # where KiCad's Board Setup dialog reads them from anyway.
    setup.append(Sexp.form("pad_to_mask_clearance", r.pad_to_mask_clearance))
    setup.append(Sexp.form("allow_soldermask_bridges_in_footprints", "no"))
    setup.append(Sexp.form("tenting", Sexp.form("front", "yes" if r.tenting else "no"),
                           Sexp.form("back", "yes" if r.tenting else "no")))
    plot = Sexp.form("pcbplotparams")
    for key, value in _PCB_PLOT_PARAMS:
        plot.append(Sexp.form(key, value))
    setup.append(plot)
    return setup


def board_text(board: Board, *, dialect: Dialect = "kicad-10") -> str:
    return dumps(render_board(board, dialect=dialect), indent=_indent_for(dialect))


def write_board(board: Board, path: Union[str, Path], *, dialect: Dialect = "kicad-10",
                keep_history: bool = True, reason: str = "write board") -> "Sexp":
    """Atomically write ``path`` (``.history/`` backup of any previous version is kept)."""
    text = board_text(board, dialect=dialect)
    atomic_write_text(Path(path), text, keep_history=keep_history, reason=reason)
    LOG.info("wrote board %s (%d bytes, %d footprints, %d copper layers)", path, len(text),
             len(board.footprints), board.n_copper)
    return render_board(board, dialect=dialect)


# ─────────────────────────────────────────────────────────────────────────────
# companion project files
# ─────────────────────────────────────────────────────────────────────────────
#: ``board.design_settings.rules`` keys a KiCad-10 project file contains, measured from
#: ``pcbai/steps/template_project/board.kicad_pro``. Anything not in here is invisible to KiCad, so
#: ``_rules_json`` may not emit it — the same list is asserted in tests/test_kicad_scaffold.py.
KICAD10_RULE_KEYS = frozenset("""max_error min_clearance min_connection min_copper_edge_clearance
min_groove_width min_hole_clearance min_hole_to_hole min_microvia_diameter min_microvia_drill
min_resolved_spokes min_silk_clearance min_text_height min_text_thickness min_through_hole_diameter
min_track_width min_via_annular_width min_via_diameter solder_mask_to_copper_clearance
use_height_for_length_calcs""".split())

#: ... and the same for ``board.design_settings.rule_severities``.
KICAD10_SEVERITY_KEYS = frozenset("""annular_width clearance connection_width copper_edge_clearance
copper_sliver courtyards_overlap creepage diff_pair_gap_out_of_range
diff_pair_uncoupled_length_too_long drill_out_of_range duplicate_footprints extra_footprint footprint
footprint_filters_mismatch footprint_symbol_field_mismatch footprint_symbol_mismatch
footprint_type_mismatch hole_clearance hole_to_hole holes_co_located invalid_outline isolated_copper
item_on_disabled_layer items_not_allowed length_out_of_range lib_footprint_issues
lib_footprint_mismatch malformed_courtyard microvia_drill_out_of_range mirrored_text_on_front_layer
missing_courtyard missing_footprint missing_tuning_profile net_conflict
nonmirrored_text_on_back_layer npth_inside_courtyard padstack pth_inside_courtyard shorting_items
silk_edge_clearance silk_over_copper silk_overlap skew_out_of_range solder_mask_bridge starved_thermal
text_height text_on_edge_cuts text_thickness through_hole_pad_without_hole too_many_vias track_angle
track_dangling track_not_centered_on_via track_on_post_machined_layer track_segment_length
track_width tracks_crossing tuning_profile_track_geometries unconnected_items unresolved_variable
via_dangling zones_intersect""".split())


def _rules_json(rules: DesignRules) -> Dict[str, object]:
    """Board-wide minima in ``board.design_settings.rules`` — KiCad 10's real key names.

    KiCad reads *these* keys, so the mapping has to be exact: a plausible-looking name that KiCad
    does not know is silently ignored, and the board then gets DRC'd against KiCad's own defaults
    while our checker believes it enforced the project's. (This function used to invent names such as
    ``"clearance"``/``"track_widths"``/``"via_sizes"`` here — the file parsed, and none of the numbers
    applied.) Keys KiCad has that our model does not (``min_hole_clearance``, ``min_resolved_spokes``,
    ``max_error``, …) keep KiCad's own defaults instead of a guessed value; the numbers we cannot
    express at all live in the ``pcbai`` block below.
    """
    out: Dict[str, object] = {
        "max_error": 0.005,                        # KiCad: length-calc tolerance
        "min_clearance": rules.min_clearance,
        "min_connection": rules.min_clearance,     # pad/via connection width, same floor
        "min_copper_edge_clearance": rules.min_copper_to_edge,
        "min_groove_width": 0.0,                   # routed slots: not modelled
        "min_hole_clearance": 0.25,                # hole-to-copper: not modelled, keep KiCad's
        "min_hole_to_hole": rules.min_hole_to_hole,
        "min_microvia_diameter": rules.min_via_diameter,
        "min_microvia_drill": rules.min_via_drill,
        "min_resolved_spokes": 2,
        "min_silk_clearance": rules.min_silk_to_silk,
        "min_text_height": rules.min_text_height,
        "min_text_thickness": rules.min_text_thickness,
        "min_through_hole_diameter": rules.min_via_drill,
        "min_track_width": rules.min_track_width,
        "min_via_annular_width": rules.annular_ring_min,
        "min_via_diameter": rules.min_via_diameter,
        "solder_mask_to_copper_clearance": rules.pad_to_mask_clearance,
        "use_height_for_length_calcs": True,
    }
    unknown = set(out) - KICAD10_RULE_KEYS
    if unknown:                                    # a typo here is a silent no-op in KiCad
        LOG.warning("rules: keys KiCad 10 does not know (%s) — dropped", ", ".join(sorted(unknown)))
        out = {k: v for k, v in out.items() if k in KICAD10_RULE_KEYS}
    return out


def _severities_json(rules: DesignRules) -> Dict[str, str]:
    """``rule_severities``: which KiCad checks are errors/warnings for this project.

    Only names from :data:`KICAD10_SEVERITY_KEYS` are written. The error/warning split mirrors
    :meth:`Board.checks`: anything that would stop a board from being built is an error here, anything
    that is our opinion about quality (silk, isolated copper, courtyard *clearance* — KiCad only has a
    severity for the overlap check itself) is a warning.
    """
    sev = {key: "error" for key in KICAD10_SEVERITY_KEYS}
    for key in ("courtyards_overlap", "silk_overlap", "silk_over_copper", "silk_edge_clearance",
                "isolated_copper", "copper_sliver", "starved_thermal", "text_height", "text_thickness",
                "holes_co_located", "drill_out_of_range", "track_angle", "track_segment_length",
                "too_many_vias", "footprint_symbol_field_mismatch", "lib_footprint_mismatch",
                "missing_courtyard", "zones_intersect",
                "track_not_centered_on_via", "mirrored_text_on_front_layer",
                "nonmirrored_text_on_back_layer", "npth_inside_courtyard", "pth_inside_courtyard"):
        sev[key] = "warning"
    for key in ("footprint_filters_mismatch", "footprint_type_mismatch", "missing_tuning_profile",
                "unresolved_variable", "tuning_profile_track_geometries", "length_out_of_range",
                "skew_out_of_range", "diff_pair_uncoupled_length_too_long", "duplicate_footprints",
                "extra_footprint", "lib_footprint_issues", "missing_footprint", "item_on_disabled_layer",
                "connection_width", "creepage", "via_dangling", "track_dangling"):
        sev[key] = "ignore"
    # the four that must never be softened: they are how a bare board becomes a shorted board
    for key in ("clearance", "shorting_items", "copper_edge_clearance", "annular_width"):
        sev[key] = "error"
    return {k: v for k, v in sorted(sev.items()) if k in KICAD10_SEVERITY_KEYS}


def project_json(board: Board) -> Dict[str, object]:
    """``.kicad_pro`` with the KiCad 10 layout: net classes and stackup live *here*, not in the board."""
    classes = [c.to_pro_dict() for c in board.rules.net_classes.values()]
    if "Default" not in board.rules.net_classes:
        classes.insert(0, NetClass("Default").to_pro_dict())
    # KiCad assigns nets *inside* each class entry (no separate assignment table), and Default owns
    # whatever the named classes did not claim.
    claimed = {n for c in board.rules.net_classes.values() if c.name != "Default" for n in c.nets}
    leftovers = sorted(n for n in board.net_names() if n not in claimed)
    if "Default" in board.rules.net_classes:
        board.rules.net_classes["Default"].nets = leftovers
    layers_json = _stackup_json(board)
    # KiCad 10 keeps annular width and courtyard clearance *board-wide* (design_settings.rules and the
    # courtyard DRC rule), not per net class — adding fake per-class keys here would look authoritative
    # in our repo and do nothing in KiCad, so the only mirror is the namespaced ``pcbai`` block below.
    return {
        "board": {
            "design_settings": {
                # Only the defaults we can derive from the model; KiCad fills the rest of
                # ``design_settings.defaults`` with its own on load (missing keys are migrated,
                # invented keys are not — see the docstring of _rules_json).
                "defaults": {
                    "board_outline_line_width": 0.05,
                    "copper_line_width": board.rules.min_track_width,
                    "copper_text_size_h": board.rules.min_text_height,
                    "copper_text_size_v": board.rules.min_text_height,
                    "copper_text_thickness": board.rules.min_text_thickness,
                    "courtyard_line_width": 0.05,
                    "fab_line_width": 0.1,
                    "silk_line_width": board.rules.min_text_thickness,
                    "silk_text_size_h": board.rules.min_text_height,
                    "silk_text_size_v": board.rules.min_text_height,
                    "silk_text_thickness": board.rules.min_text_thickness,
                    "zones": {"min_clearance": board.rules.min_clearance,
                              "min_thickness": max(board.rules.min_track_width, 0.25)},
                },
                "rule_severities": _severities_json(board.rules),
                "rules": _rules_json(board.rules),
                # our own knobs, in our own namespace: KiCad ignores unknown members here, and this
                # is what lets `validate` after a re-read use the *same* numbers the generator used
                "pcbai": {
                    "annular_ring_min": board.rules.annular_ring_min,
                    "min_courtyard_clearance": board.rules.min_courtyard_clearance,
                    "min_silk_to_silk": board.rules.min_silk_to_silk,
                    "min_mask_web": board.rules.min_mask_web,
                    "pad_to_mask_clearance": board.rules.pad_to_mask_clearance,
                    "tenting": board.rules.tenting,
                },
            },
            "layer_pairs": [],
            "layer_presets": [],
            "viewports": [],
            "stackup": layers_json,
        },
        "boards": [{"assigned_variant_uuids": {"1": []}}],
        "cvpcb": {"equivalents": {}, "footprint_filters": [], "meta": {"version": 3}},
        "libraries": {"project": {"fp_dir": "", "project_files_first": False, "symbol_dir": ""},
                      "version_key": 1},
        "meta": {"filename": "", "version": 3},
        "net_settings": {"classes": classes, "meta": {"version": 5}, "net_colors": None,
                         "netclass_assignments": None, "netclass_patterns": []},
        "pcbnew": {"last_paths": {"gencad": "", "idf": "", "netlist": "", "plot": "gerbers/",
                                  "pos_files": "", "report": "", "specctra_dsn": "", "step": "",
                                  "svg": "", "vrml": ""},
                   "page_layout_descr_file": ""},
        "schematic": {"annotate_start_num": 1, "drawing": {"default_line_width": 0.254,
                                                            "font_size_delta": 0.254},
                      "legacy_lib_dir": "", "legacy_lib_list": {}, "meta": {"version": 1}},
        "sheets": [],
        "text_variables": dict(board.variables),
        "tuning_profiles": [],
    }


def _stackup_json(board: Board) -> Dict[str, object]:
    """KiCad stores the *editable* stackup in ``.kicad_pro``; mirror :meth:`StackLayer.default_stackup`."""
    layers: List[Dict[str, object]] = []
    for i, layer in enumerate(board.stackup):
        entry: Dict[str, object] = {
            "castellated": True, "color": "#c8c8c8" if layer.kind == "copper" else "#a0a0a0",
            "dielectric_lines": 0, "edge_plated": False, "fill_type": "none", "grp": layer.kind,
            "hidden": False, "id": layer.id or ordinal_for(layer.name), "material": layer.material,
            "name": layer.name, "repeat_by": 0, "role": "", "stack_effects": "ignore",
            "thickness": layer.thickness, "type": layer.kind,
        }
        if layer.kind == "copper":
            entry["role"] = "signal" if layer.name in ("F.Cu", "B.Cu") else "power"
            entry["type"] = "copper"
        if layer.epsilon_r is not None:
            entry["epsilon_r"] = layer.epsilon_r
            entry["loss_tangent"] = layer.loss_tangent
        if i == 0:
            entry["type"] = "unknown"
        layers.append(entry)
    return {"layers": layers, "predef_id": "", "stackup_3d_show_plane_dielectrics": True,
            "stackup_3d_show_signal_layers": False, "state": "modified"}


def design_rules_text(board: Board) -> str:
    """``.kicad_dru`` — custom rules. KiCad 10 still reads this file; net classes live in the project."""
    lines = ["(version 1.0)"]
    for name, cls in board.rules.net_classes.items():
        if name == "Default":
            continue
        lines.append(f'(rule "{name}_clearance" (condition "A.NetClass == \'{name}\'")'
                     f' (constraint clearance (min {cls.clearance}mm)))')
        lines.append(f'(rule "{name}_width" (condition "A.NetClass == \'{name}\'")'
                     f' (constraint track_width (min {cls.track_width}mm)))')
    lines.append(f'(rule "edge_clearance" (constraint courtyard_to_board_edge '
                 f"(allow_overrides) (min {board.rules.min_copper_to_edge}mm)))")
    return "\n".join(lines) + "\n"


def write_project_files(board: Board, out_dir: Union[str, Path], *, stem: str = "board",
                        dialect: Dialect = "kicad-10", keep_history: bool = True,
                        reason: str = "write project") -> Dict[str, str]:
    """Write ``<stem>.kicad_pcb`` + ``.kicad_pro`` + ``.kicad_dru`` (+ a BOM csv) into *out_dir*."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "board": str(out / f"{stem}.kicad_pcb"),
        "project": str(out / f"{stem}.kicad_pro"),
        "rules": str(out / f"{stem}.kicad_dru"),
        "bom": str(out / f"{stem}-bom.csv"),
    }
    write_board(board, paths["board"], dialect=dialect, keep_history=keep_history, reason=reason)
    atomic_write_text(Path(paths["project"]),
                      json.dumps(project_json(board), indent=2, ensure_ascii=False) + "\n",
                      keep_history=keep_history, reason=reason)
    atomic_write_text(Path(paths["rules"]), design_rules_text(board), keep_history=keep_history,
                      reason=reason)
    atomic_write_text(Path(paths["bom"]), bom_csv(board), keep_history=keep_history, reason=reason)
    return paths


def bom_csv(board: Board) -> str:
    rows = ["Reference,Value,Footprint,Quantity,MPN,DNP,Description"]
    for fp in sorted(board.footprints, key=lambda f: f.reference or "~"):
        desc = (fp.description or "").replace('"', "'")
        rows.append(f'{fp.reference},{fp.value},{fp.lib_id},1,,'
                    f'{"DNP" if fp.dnp else ""},"{desc}"')
    return "\n".join(rows) + "\n"
