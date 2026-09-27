"""Small, dependency-free footprint generator — enough geometry to build a real board skeleton.

Why generated instead of read from ``kicad-footprints``: this repo must work on a machine with no
KiCad install (the agent's own sandbox), and the agent must not *invent* copper. Every primitive here
is computed from the manufacturer's package drawing dimensions (IPC-7351 style, rounded to the values
KiCad's own libraries use), and each one records its source so a human can verify it in 30 seconds:

    >>> qfp("LQFP48", pitch=0.5, body=7.0, pad_e=1.5, pad_w=0.45, leads_per_side=12).pads[0].size
    (0.45, 1.5)

``to_sexp()`` renders the object in the board-file dialect so ``scaffold`` can drop footprints straight
into a ``.kicad_pcb``; ``to_mod_text()`` writes a standalone ``.kicad_mod`` for the Footprint Editor.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .model import Footprint, Graphic, Pad, new_uuid
from .sexp import Sexp, atom, dumps

__all__ = ["FootprintDef", "qfp", "tqfp", "lqfp", "soic", "tssop", "sot23", "sot23_5", "dfn",
           "passive", "crystal", "smd_inductor", "pin_header", "usb_c", "tact", "mounting_hole",
           "from_spec", "UnknownFootprintKind", "REGISTRY"]


@dataclass
class FootprintDef:
    """A footprint *template*: pads in local coordinates + the graphics that describe the body."""

    name: str
    pads: List[dict] = field(default_factory=list)       # dicts → Pad, so specs stay plain data
    graphics: List[dict] = field(default_factory=list)
    layer: str = "F.Cu"
    description: str = ""
    tags: str = ""
    source: str = ""                                      # where the dimensions came from
    attr: List[str] = field(default_factory=lambda: ["smd"])
    datasheet: str = ""

    # ── build ────────────────────────────────────────────────────────────────
    def instance(self, reference: str, value: str, *, x: float = 0.0, y: float = 0.0,
                 rot: float = 0.0, layer: Optional[str] = None,
                 pad_nets: Optional[Dict[str, str]] = None,
                 pad_pins: Optional[Dict[str, Tuple[str, str]]] = None) -> Footprint:
        """Place one instance. ``pad_nets`` maps pad number → net; ``pad_pins`` maps
        pad number → (pin function, pin type) for schematic-driven pads."""
        fp = Footprint(lib_id=f"pcbai:{self.name}", reference=reference, value=value, x=x, y=y,
                       rot=rot, layer=layer or self.layer, description=self.description,
                       tags=self.tags, datasheet=self.datasheet, attr=list(self.attr),
                       uuid=new_uuid())
        nets = pad_nets or {}
        pins = pad_pins or {}
        for spec in self.pads:
            pad = Pad(**{k: v for k, v in spec.items() if k != "net"})
            pad.net = nets.get(pad.number, spec.get("net", ""))
            fn, pt = pins.get(pad.number, (None, None))
            pad.pin_function, pad.pin_type = fn, pt
            fp.pads.append(pad)
        for spec in self.graphics:
            fp.graphics.append(Graphic(**spec))
        return fp

    # ── render ───────────────────────────────────────────────────────────────
    def check(self, pad_nets: Optional[Dict[str, str]] = None, *, min_gap: float = 0.05,
              assume_distinct: bool = False) -> List[dict]:
        """Footprint-level sanity: pads of different nets must not overlap or nearly touch.

        A *board* DRC skips this on purpose (see ``Board.checks``): a pad row at its natural pitch is
        correct, so flagging it would bury the real findings. What is still a defect — and only
        visible once the pin map is known — is a footprint whose assigned nets land on copper that
        touches, or on a sliver thin enough to bridge in reflow.

        ``pad_nets`` is the same mapping ``instance()`` takes, so a generated part can be judged
        before it is ever placed. ``assume_distinct=True`` pretends every pad is its own net, which
        is what you want while *authoring* a footprint with no net information yet.
        """
        inst = self.instance("CHK", "", x=0.0, y=0.0, pad_nets=pad_nets)
        if assume_distinct:
            for i, pad in enumerate(inst.pads):
                pad.net = f"__distinct_{i}"
        issues: List[dict] = []
        for issue in inst.check(min_gap=min_gap):
            issues.append({"severity": issue.severity, "code": issue.code, "message": issue.message,
                           "hint": issue.hint})
        court = [v for g in self.graphics if g.get("layer") == "F.CrtYd"
                 for v in (g.get("start"), g.get("end")) if v]
        if court:
            box = _pad_bbox_local(inst.pads)
            margin = 0.01
            if (min(p[0] for p in court) > box[0] + margin or max(p[0] for p in court) < box[2] - margin
                    or min(p[1] for p in court) > box[1] + margin
                    or max(p[1] for p in court) < box[3] - margin):
                issues.append({"severity": "warning", "code": "courtyard-too-small",
                               "message": f"{self.name}'s courtyard ({min(p[0] for p in court):.2f},"
                                          f"{min(p[1] for p in court):.2f})×("
                                          f"{max(p[0] for p in court):.2f},"
                                          f"{max(p[1] for p in court):.2f}) is inside its own pads "
                                          f"({box[0]:.2f},{box[1]:.2f})×({box[2]:.2f},{box[3]:.2f})",
                               "hint": "KiCad measures part-to-part spacing with the courtyard, so "
                                       "packing parts this way hides a collision at assembly"})
        for pad in self.pads:
            ptype = str(pad.get("type", ""))
            drill = pad.get("drill")
            if drill and "thru_hole" not in ptype and "np_thru_hole" not in ptype:
                issues.append({"severity": "warning", "code": "drill-on-smd-pad",
                               "pad": str(pad.get("number")),
                               "message": f"pad {pad.get('number')} is '{ptype}' but carries a drill",
                               "hint": "either the type or the drill is wrong; KiCad will not make "
                                       "a hole here"})
            size = pad.get("size") or (0.0, 0.0)
            d = max(drill) if isinstance(drill, (list, tuple)) else drill
            if d:
                ring = (min(size) - float(d)) / 2.0
                if ring < 0.1:
                    issues.append({"severity": "warning", "code": "annular-ring",
                                   "pad": str(pad.get("number")),
                                   "message": f"pad {pad.get('number')} leaves a {ring:.3f}mm "
                                              "annular ring",
                                   "hint": "grow the pad or shrink the drill (most fabs want "
                                           ">= 0.10 mm)"})
        return issues

    def to_sexp(self, *, reference: str = "", value: str = "", x: float = 0.0, y: float = 0.0,
                rot: float = 0.0, net_ids: Optional[Dict[str, int]] = None,
                dialect: str = "kicad-10") -> Sexp:
        from .pcb_writer import render_footprint
        return render_footprint(self.instance(reference or self.name, value, x=x, y=y, rot=rot),
                                dialect=dialect, net_ids=net_ids)

    def to_mod_text(self) -> str:
        """Standalone ``.kicad_mod`` for KiCad's Footprint Editor / a ``.pretty`` library."""
        from .pcb_writer import _effects, _stroke
        root = Sexp.form("footprint", atom(self.name))
        root.append(Sexp.form("version", 20260206))
        root.append(Sexp.form("generator", "pcbai"))
        root.append(Sexp.form("generator_version", "10.0"))
        root.append(Sexp.form("layer", atom(self.layer)))
        root.append(Sexp.form("uuid", atom(new_uuid())))
        if self.description:
            root.append(Sexp.form("descr", atom(self.description)))
        if self.tags:
            root.append(Sexp.form("tags", atom(self.tags)))
        root.append(Sexp.form("attr", *(self.attr + ["board_only"])))
        for spec in self.graphics:
            kind = spec.get("kind", "line")
            g = Graphic(**spec)
            if kind == "line":
                node = Sexp.form("fp_line", Sexp.form("start", *g.start), Sexp.form("end", *g.end),
                                 _stroke(g.width, g.stroke_type), Sexp.form("layer", atom(g.layer)))
            elif kind == "rect":
                node = Sexp.form("fp_rect", Sexp.form("start", *g.start),
                                 Sexp.form("end", g.start[0] + g.size[0], g.start[1] + g.size[1]),
                                 _stroke(g.width, g.stroke_type), Sexp.form("layer", atom(g.layer)))
            elif kind == "circle":
                node = Sexp.form("fp_circle", Sexp.form("center", *g.center),
                                 Sexp.form("end", g.center[0] + g.radius, g.center[1]),
                                 _stroke(g.width, g.stroke_type), Sexp.form("layer", atom(g.layer)))
            else:
                continue
            node.append(Sexp.form("uuid", atom(g.uuid)))
            root.append(node)
        for spec in self.pads:
            pad = Pad(**{k: v for k, v in spec.items() if k != "net"})
            node = Sexp.form("pad", str(pad.number), pad.type, pad.shape)
            if abs(pad.at[2]) > 1e-9:
                node.append(Sexp.form("at", pad.at[0], pad.at[1], pad.at[2]))
            else:
                node.append(Sexp.form("at", pad.at[0], pad.at[1]))
            node.append(Sexp.form("size", pad.size[0], pad.size[1]))
            if pad.drill is not None:
                if isinstance(pad.drill, (tuple, list)):
                    node.append(Sexp.form("drill", "oval", pad.drill[0], pad.drill[1]))
                else:
                    node.append(Sexp.form("drill", pad.drill))
            node.append(Sexp.form("layers", *[atom(l) for l in pad.layers]))
            if pad.shape == "roundrect":
                node.append(Sexp.form("roundrect_rratio", pad.roundrect_rratio or 0.25))
            node.append(Sexp.form("uuid", atom(pad.uuid)))
            root.append(node)
        root.append(_effects(1.27, 0.15))
        return dumps(root.beautify(indent="\t"), indent="\t")


# ─────────────────────────────────────────────────────────────────────────────
# primitives
# ─────────────────────────────────────────────────────────────────────────────
def _quad_pads(*, n_per_side: int, pitch: float, pad_w: float, pad_l: float, span: float,
               side_layers: Sequence[str], body_edge: float = math.inf) -> List[dict]:
    """Gull-wing pads on four sides, KiCad pin 1 at top-left, numbering counter-clockwise."""
    if n_per_side < 1 or pitch <= 0.0:
        raise ValueError(f"a quad pack needs >= 1 lead per side at a positive pitch "
                         f"(got {n_per_side} @ {pitch}mm)")
    # A row of leads has to fit along the body edge it sits on. Letting it overflow is how a spec
    # silently produces a footprint whose corner pads wrap around into the next row and touch.
    if (n_per_side - 1) * pitch > body_edge:
        raise ValueError(f"{n_per_side} leads at {pitch}mm need "
                         f"{(n_per_side - 1) * pitch + pad_w:.2f}mm of edge but the body is "
                         f"{body_edge:.2f}mm — reduce the count or the pitch, or grow the body")
    pads: List[dict] = []
    first = (span + pad_l) / 2.0
    for side in range(4):
        start = side * n_per_side
        for k in range(n_per_side):
            offset = (n_per_side - 1) * pitch / 2.0 - k * pitch
            if side == 0:                    # left, top → bottom
                x, y, rot = -first, offset, 270.0
            elif side == 1:                  # top, left → right
                x, y, rot = offset, -first, 0.0
            elif side == 2:                  # right, bottom → top
                x, y, rot = first, -offset, 90.0
            else:                            # bottom, right → left
                x, y, rot = -offset, first, 180.0
            # size order is (row-direction, radial): the side rotation already turns the pad's local
            # frame, so (pad_l, pad_w) here would lay every pad *along* the row and fuse the row into
            # one blob of copper — which is exactly what FootprintDef.check() now catches.
            pads.append({"number": str(start + k + 1), "type": "smd", "shape": "roundrect",
                         "at": (round(x, 4), round(y, 4), rot), "size": (pad_w, pad_l),
                         "layers": tuple(side_layers), "roundrect_rratio": 0.25})
    return pads


def qfp(name: str = "QFP44_12x12_P0.8", *, pitch: float = 0.8, body: float = 10.0,
        pad_e: float = 1.2, pad_w: float = 0.55, leads_per_side: int = 11,
        description: str = "", source: str = "",
        pins: Optional[Sequence[str]] = None) -> FootprintDef:
    """Quad flat pack with gull-wing leads (LQFP/TQFP family).

    Defaults are a 0.8 mm-pitch QFP-44 so that ``from_spec("qfp")`` works from a bare spec; the
    specific parts (LQFP-48/64, TQFP-32) go through :func:`lqfp`/:func:`tqfp` with their own numbers.
    """
    half = body / 2.0
    pads = _quad_pads(n_per_side=leads_per_side, pitch=pitch, pad_w=pad_w, pad_l=pad_e,
                      span=body + pad_e, side_layers=("F.Cu", "F.Paste", "F.Mask"),
                      body_edge=body)
    if pins:
        for pad, pin in zip(pads, pins, strict=False):
            pad["pin_function"], pad["pin_type"] = pin, "input"
    silk = half + 0.35
    fab = half
    graphics = [
        {"kind": "line", "layer": "F.Fab", "start": (-fab, -fab), "end": (fab, -fab), "width": 0.1},
        {"kind": "line", "layer": "F.Fab", "start": (fab, -fab), "end": (fab, fab), "width": 0.1},
        {"kind": "line", "layer": "F.Fab", "start": (fab, fab), "end": (-fab, fab), "width": 0.1},
        {"kind": "line", "layer": "F.Fab", "start": (-fab, fab), "end": (-fab, -fab), "width": 0.1},
        {"kind": "line", "layer": "F.SilkS", "start": (-silk, -silk), "end": (silk, -silk), "width": 0.12},
        {"kind": "line", "layer": "F.SilkS", "start": (-silk, silk), "end": (silk, silk), "width": 0.12},
        {"kind": "circle", "layer": "F.SilkS", "center": (-fab + 0.25, -fab + 0.25), "radius": 0.3,
         "width": 0.12},
        {"kind": "circle", "layer": "F.Fab", "center": (0, 0), "radius": half * 0.35, "width": 0.1},
    ]
    # the courtyard goes round the *lead tips*, not the moulding: the row centre sits at
    # (body + 2·pad_e)/2 and each lead reaches pad_e/2 further, so a smaller courtyard overlaps every
    # neighbouring part on the pick-and-place and in the 3D view
    court = (body + 2.0 * pad_e) / 2.0 + pad_e / 2.0 + 0.25
    graphics += _box("F.CrtYd", court, court, 0.05)
    return FootprintDef(name=name, pads=pads, graphics=graphics, description=description,
                        source=source, tags="qfp smd",
                        datasheet="")


def _pad_bbox_local(pads: Sequence[Pad]) -> Tuple[float, float, float, float]:
    """Axis-aligned box around *placed* pads, in footprint coordinates (rotation-aware)."""
    from .model import _obb_corners

    pts = [c for pad in pads for c in _obb_corners(pad.rect_local())]
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _court_box(items: Sequence[dict], *, margin: float = 0.25, floor: float = 0.0) -> List[dict]:
    """Courtyard rectangle that provably contains the pad copper (rotation tolerant, generous).

    Every builder used to hand-write these numbers, which is how a part gained a 5th row and a
    courtyard that no longer covered it. The courtyard is what KiCad and the pick-and-place file
    measure part-to-part spacing with, so it must never sit inside the copper.
    """
    hw = max((abs(p["at"][0]) + max(p["size"]) / 2.0) for p in items) + margin
    hh = max((abs(p["at"][1]) + max(p["size"]) / 2.0) for p in items) + margin
    return _box("F.CrtYd", max(hw, floor), max(hh, floor), 0.05)


def _box(layer: str, hw: float, hh: float, width: float, *, inset_x: float = 0.0) -> List[dict]:
    """Four corner-marked lines forming a rectangle on *layer*."""
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return [{"kind": "line", "layer": layer, "start": corners[i], "end": corners[(i + 1) % 4],
             "width": width} for i in range(4)]


def lqfp(name: str = "LQFP48", *, pitch: float = 0.5, body: float = 7.0, leads_per_side: int = 12,
         pad_e: float = 1.0, pad_w: float = 0.45, pins: Optional[Sequence[str]] = None) -> FootprintDef:
    """STM32-style 7×7 mm LQFP (1.0 mm pad, 0.45 mm wide) — the shape most 48-pin MCUs use."""
    return qfp(name, pitch=pitch, body=body, pad_e=pad_e, pad_w=pad_w, leads_per_side=leads_per_side,
               description=f"{name}, {4*leads_per_side}-pin {body:.1f}x{body:.1f}mm body, "
                           f"{pitch}mm pitch", source="ST/Kioxia LQFP drawings, IPC-7351B",
               pins=pins)


def tqfp(name: str = "TQFP48", **kw) -> FootprintDef:
    """Thin wrapper over :func:`lqfp` — JEDEC MS-026 covers both; only the moulding name differs."""
    return lqfp(name, **kw)


def soic(name: str = "SOIC-8_3.9x4.9mm_P1.27mm", *, pitch: float = 1.27, pads: int = 8,
         body_w: float = 3.9, body_l: float = 4.9, pad_w: float = 0.6,
         pad_l: float = 1.5) -> FootprintDef:
    """Small outline IC, gull wing, pads on two sides (SOIC/SOP)."""
    per_side = pads // 2
    span = (per_side - 1) * pitch
    half_span = span / 2.0
    out_pad_x = body_w / 2.0 + pad_l / 2.0 - 0.1
    items: List[dict] = []
    for i in range(per_side):
        y = -half_span + i * pitch
        items.append({"number": str(per_side - i), "type": "smd", "shape": "roundrect",
                      "at": (-out_pad_x, y, 0.0), "size": (pad_l, pad_w),
                      "layers": ("F.Cu", "F.Paste", "F.Mask"), "roundrect_rratio": 0.25})
    for i in range(per_side):
        y = -half_span + i * pitch
        items.append({"number": str(per_side + 1 + i), "type": "smd", "shape": "roundrect",
                      "at": (out_pad_x, -y, 0.0), "size": (pad_l, pad_w),
                      "layers": ("F.Cu", "F.Paste", "F.Mask"), "roundrect_rratio": 0.25})
    hw, hl = body_w / 2.0, body_l / 2.0
    court_x = out_pad_x + pad_l / 2.0 + 0.25
    graphics = _box("F.Fab", hw, hl, 0.1) + _box("F.SilkS", hw + 0.25, hl + 0.25, 0.12) \
        + _box("F.CrtYd", court_x, hl + 0.25, 0.05)
    graphics.append({"kind": "line", "layer": "F.SilkS", "start": (-hw - 0.25, -hl - 0.25),
                     "end": (-hw - 0.25, -hl + 0.3), "width": 0.12})
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description=f"{pads}-pin SO/SOIC, {body_w:.2f}x{body_l:.2f}mm body, "
                                    f"{pitch}mm pitch", source="NXP/ST drawings, IPC-7351B",
                        tags="soic smd")


def tssop(name: str = "TSSOP-20_4.4x6.5mm_P0.65mm", *, pitch: float = 0.65, pads: int = 20,
          body_w: float = 4.4, body_l: float = 6.5) -> FootprintDef:
    return soic(name, pitch=pitch, pads=pads, body_w=body_w, body_l=body_l, pad_w=0.45, pad_l=1.0)


def sot23(name: str = "SOT-23-3", *, pads: int = 3) -> FootprintDef:
    """SOT-23 (3 = transistor, 5/6 = regulators/multi-pin)."""
    multi = pads >= 5
    items = [
        {"number": "1", "type": "smd", "shape": "roundrect", "at": (-0.95, 1.0, 0.0),
         "size": (1.0, 0.6), "layers": ("F.Cu", "F.Paste", "F.Mask"), "roundrect_rratio": 0.25},
        {"number": "2", "type": "smd", "shape": "roundrect", "at": (-0.95, -1.0, 0.0),
         "size": (1.0, 0.6), "layers": ("F.Cu", "F.Paste", "F.Mask"), "roundrect_rratio": 0.25},
        {"number": "3", "type": "smd", "shape": "roundrect", "at": (0.95, 0.0, 180.0),
         "size": (1.0, 0.6), "layers": ("F.Cu", "F.Paste", "F.Mask"), "roundrect_rratio": 0.25},
    ]
    if pads >= 5:
        items += [{"number": "4", "type": "smd", "shape": "roundrect", "at": (0.95, -1.9, 180.0),
                   "size": (1.0, 0.6), "layers": ("F.Cu", "F.Paste", "F.Mask"),
                   "roundrect_rratio": 0.25},
                  {"number": "5", "type": "smd", "shape": "roundrect", "at": (0.95, 1.9, 180.0),
                   "size": (1.0, 0.6), "layers": ("F.Cu", "F.Paste", "F.Mask"),
                   "roundrect_rratio": 0.25}]
    if pads >= 6:
        items.append({"number": "6", "type": "smd", "shape": "roundrect", "at": (0.95, 0.0, 180.0),
                      "size": (1.0, 0.6), "layers": ("F.Cu", "F.Paste", "F.Mask"),
                      "roundrect_rratio": 0.25})
    hh = 1.9 if multi else 1.55
    # measured off the pads, so adding the 5th/6th row cannot leave the courtyard *inside* the copper
    pad_x = max(abs(p["at"][0]) + p["size"][0] / 2.0 for p in items)
    pad_y = max(abs(p["at"][1]) + p["size"][1] / 2.0 for p in items)
    graphics = _box("F.Fab", 1.45, hh, 0.1) + _box("F.SilkS", 1.5, max(hh, pad_y) + 0.1, 0.12) \
        + _box("F.CrtYd", max(1.75, pad_x + 0.25), max(hh + 0.25, pad_y + 0.25), 0.05)
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description=f"{name} SOT-23, {pads} pads", source="NXP SOT23 outline",
                        tags="sot23 smd")


def sot23_5(name: str = "SOT-23-5") -> FootprintDef:
    """SOT-23-5 (regulators, op-amps): two pads left, three right."""
    return sot23(name, pads=5)


def dfn(name: str = "DFN-8_2x2mm_P0.5mm", *, pads: int = 8, pitch: float = 0.5,
        body_w: float = 2.0, body_l: float = 2.0, thermal: bool = True,
        pad_l: float = 0.4, pad_w: float = 0.25, ep_clear: float = 0.15) -> FootprintDef:
    """Leadless side-flat package (DFN/UDFN/QFN-ish): two short rows + an optional exposed pad.

    ``pads`` counts the *signal* pads, so a DFN-8 is 4 + 4 with the exposed pad numbered 9 — the
    numbering KiCad's own DFN libraries use. The exposed pad is deliberately left without a net: it
    is GND on most regulators and SW on a buck, and baking a net in here is how two nets get merged.
    """
    if pads % 2:
        raise ValueError(f"a DFN has the same number of pads on each side, got {pads}")
    per_side = pads // 2
    half = (per_side - 1) * pitch / 2.0
    if per_side * pitch > body_l + pitch:
        raise ValueError(f"{per_side} pads at {pitch}mm do not fit along a {body_l}mm side")
    out_x = body_w / 2.0 - 0.15                      # pads hug the package edge, slightly inboard
    ep_half_x = out_x - pad_l / 2.0 - ep_clear       # the EP must not touch the side pads
    ep_half_y = min(body_l / 2.0 - 0.15, half + pad_w / 2.0)
    if ep_half_x <= 0.0 or ep_half_y <= 0.0:
        raise ValueError(f"no room for an exposed pad between pads at x=±{out_x - pad_l / 2:.3f}mm")
    items: List[dict] = []
    for i in range(per_side):
        items.append({"number": str(i + 1), "type": "smd", "shape": "rect",
                      "at": (-out_x, half - i * pitch, 0.0), "size": (pad_l, pad_w),
                      "layers": ("F.Cu", "F.Paste", "F.Mask")})
    for i in range(per_side):
        items.append({"number": str(per_side + 1 + i), "type": "smd", "shape": "rect",
                      "at": (out_x, -half + i * pitch, 180.0), "size": (pad_l, pad_w),
                      "layers": ("F.Cu", "F.Paste", "F.Mask")})
    if thermal:
        items.append({"number": str(pads + 1), "type": "smd", "shape": "rect",
                      "at": (0.0, 0.0, 0.0), "size": (round(2 * ep_half_x, 4),
                                                      round(2 * ep_half_y, 4)),
                      "layers": ("F.Cu", "F.Mask")})
    court_x = out_x + pad_l / 2.0 + 0.25
    court_y = max(half + pad_w / 2.0, body_l / 2.0) + 0.25
    graphics = _box("F.Fab", body_w / 2.0, body_l / 2.0, 0.1) \
        + _box("F.SilkS", body_w / 2.0 + 0.1, body_l / 2.0 + 0.1, 0.12) \
        + _box("F.CrtYd", max(court_x, court_y), max(court_x, court_y), 0.05)
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description=f"{name}, {pads}+EP ({body_w}x{body_l}mm body, {pitch}mm pitch)",
                        source="IPC-7351B", tags="dfn qfn smd")


def passive(size: str = "0603") -> FootprintDef:
    """Chip R/C/L. Metric name in, matching imperial pad stack out (KiCad's naming)."""
    table = {
        "0201": dict(body=(0.6, 0.3), pad=(0.3, 0.35), gap=0.3, silk=(0.5, 0.4), name="R/C_0201"),
        "0402": dict(body=(1.0, 0.5), pad=(0.5, 0.6), gap=0.45, silk=(0.8, 0.8), name="C_0402"),
        "0603": dict(body=(1.6, 0.8), pad=(0.8, 0.95), gap=0.80, silk=(1.4, 0.8), name="C_0603"),
        "0805": dict(body=(2.0, 1.25), pad=(1.0, 1.2), gap=1.05, silk=(1.8, 1.0), name="C_0805"),
        "1206": dict(body=(3.2, 1.6), pad=(1.2, 1.8), gap=1.6, silk=(2.9, 1.6), name="C_1206"),
    }
    spec = table.get(size, table["0603"])
    bw, bh = spec["body"]
    px = spec["gap"] / 2.0 + spec["pad"][0] / 2.0
    items = [{"number": "1", "type": "smd", "shape": "rect", "at": (-px, 0.0, 0.0),
              "size": spec["pad"], "layers": ("F.Cu", "F.Paste", "F.Mask")},
             {"number": "2", "type": "smd", "shape": "rect", "at": (px, 0.0, 180.0),
              "size": spec["pad"], "layers": ("F.Cu", "F.Paste", "F.Mask")}]
    sw, sh = spec["silk"]
    graphics = _box("F.Fab", bw / 2.0, bh / 2.0, 0.1)
    graphics += [{"kind": "line", "layer": "F.SilkS", "start": (-sw / 2.0, -sh / 2.0),
                  "end": (-sw / 2.0, sh / 2.0), "width": 0.12},
                 {"kind": "line", "layer": "F.SilkS", "start": (sw / 2.0, -sh / 2.0),
                  "end": (sw / 2.0, sh / 2.0), "width": 0.12}]
    court_x = px + spec["pad"][0] / 2.0 + 0.25
    graphics += _box("F.CrtYd", court_x, max(bh, spec["pad"][1]) / 2.0 + 0.25, 0.05)
    return FootprintDef(name=f"{spec['name'].split('_')[0]}_{size}", pads=items, graphics=graphics,
                        description=f"Chip {size} ({'1608' if size=='0603' else 'metric'}), "
                                    "unpolarised", source="KiCad generic Discret.pretty", tags="smd rc")


def crystal(name: str = "Crystal_SMD_3215-2Pin_3.2x1.5mm", *, l: float = 3.2, w: float = 1.5,
            pad_l: float = 1.2, pad_w: float = 1.0, pitch: float = 2.2) -> FootprintDef:
    items = [{"number": "1", "type": "smd", "shape": "roundrect", "at": (-pitch / 2.0, 0, 0.0),
              "size": (pad_l, pad_w), "layers": ("F.Cu", "F.Paste", "F.Mask"),
              "roundrect_rratio": 0.25},
             {"number": "2", "type": "smd", "shape": "roundrect", "at": (pitch / 2.0, 0, 180.0),
              "size": (pad_l, pad_w), "layers": ("F.Cu", "F.Paste", "F.Mask"),
              "roundrect_rratio": 0.25}]
    graphics = _box("F.Fab", l / 2.0, w / 2.0, 0.1) + _box("F.SilkS", l / 2.0 + 0.2, w / 2.0 + 0.2, 0.12) \
        + _box("F.CrtYd", pitch / 2.0 + pad_l / 2.0 + 0.25, w / 2.0 + 0.25, 0.05)
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description=f"SMD crystal {l}x{w}mm", source="Abracon/EPSON 3215 outlines",
                        tags="crystal oscillator smd")


def smd_inductor(name: str = "L_0805", *, size: str = "0805") -> FootprintDef:
    """Power inductor on a chip pad stack (same copper as an 0805 resistor)."""
    out = passive(size)
    out.name = name
    out.description = f"Power inductor, {size} pad stack"
    out.tags = "inductor smd"
    return out


def pin_header(name: str = "Connector_PinHeader_2.54mm_1x04_Pitch2.54mm", *, pins: int = 4,
               pitch: float = 2.54, pad: float = 1.7, drill: float = 1.0,
               female: bool = False) -> FootprintDef:
    """Single row header (or female header) on a 2.54 mm grid, pad 1 at the bottom."""
    items = []
    for i in range(pins):
        items.append({"number": str(i + 1), "type": "thru_hole",
                      "shape": "rect" if i == 0 else "circle",
                      "at": (0.0, -(i - (pins - 1) / 2.0) * pitch, 0.0),
                      "size": (pad, pad), "drill": drill,
                      "layers": ("*.Cu", "*.Mask") if not female else ("F.Cu", "F.Mask")})
    hw = pad / 2.0 + 0.15
    hl = (pins - 1) * pitch / 2.0 + pitch / 2.0 + 0.25
    graphics = _box("F.Fab", hw, hl, 0.1) + _box("F.SilkS", hw + 0.12, hl, 0.12) \
        + _box("F.CrtYd", hw + 0.25, hl + 0.25, 0.05)
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description=f"{pins}-pin {pitch}mm header", source="KiCad Connector.pretty",
                        tags="header through_hole", attr=["through_hole"])


def usb_c(name: str = "USB_C_GCT_77300_24P") -> FootprintDef:
    """USB-C receptacle, mid-mount, 24 signal pads + 4 shell pads.

    Pad stack: two rows of 12 on 0.65 mm pitch (0.35 × 1.05 mm copper, 1.8 mm row spacing), which is
    what the GCT/Amphenol 16-pin & 24-pin mid-mount parts use. VBUS is A4/A9/B4/B9, GND is
    A1/A12/B1/B12, CC/SBU are A5/A8/B5/B8 — pin functions come from the USB-C spec, not a guess.
    """
    pitch = 0.65
    rows = {"A": -0.90, "B": 0.90}
    order = {1: "GND", 2: "", 3: "", 4: "VBUS", 5: "CC", 6: "D+", 7: "D-", 8: "SBU", 9: "VBUS",
             10: "", 11: "", 12: "GND"}
    size = (0.35, 1.05)
    items: List[dict] = []
    for side, y in rows.items():
        for i in range(12):
            x = (i - 5.5) * pitch
            big = order[i + 1] in ("VBUS", "GND")
            items.append({"number": f"{side}{i + 1}", "type": "smd", "shape": "rect",
                          "at": (round(x, 4), y, 0.0),
                          "size": (0.55, 1.25) if big else size,
                          "layers": ("F.Cu", "F.Paste", "F.Mask")})
    for i, (sx, sy) in enumerate(((-4.32, -2.06), (4.32, -2.06), (-4.32, 2.06), (4.32, 2.06))):
        items.append({"number": f"S{i + 1}", "type": "smd", "shape": "rect", "at": (sx, sy, 0.0),
                      "size": (1.45, 1.0), "layers": ("F.Cu", "F.Mask")})
    graphics = _box("F.Fab", 4.47, 3.31, 0.1) + _box("F.SilkS", 4.62, 3.46, 0.12) \
        + _court_box(items, margin=0.25, floor=3.7)
    graphics.append({"kind": "line", "layer": "F.SilkS", "start": (-4.62, 3.46), "end": (4.62, 3.46),
                     "width": 0.12})
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description="USB Type-C receptacle, 24-pin mid-mount (no SS pairs)",
                        source="USB-C spec Table 3-2 pinout; GCT 773001011YPL001 drawing",
                        tags="usb usb-c smd",
                        datasheet="https://www.gct.com/resource-file/773001011YPL001")


def tact(name: str = "SW_Push_6x6mm", *, pad: Tuple[float, float] = (1.0, 1.9)) -> FootprintDef:
    items = []
    for i, (x, y) in enumerate(((-3.25, -2.25), (3.25, -2.25), (3.25, 2.25), (-3.25, 2.25))):
        items.append({"number": str(i + 1), "type": "smd", "shape": "roundrect", "at": (x, y, 0.0),
                      "size": pad, "layers": ("F.Cu", "F.Paste", "F.Mask"), "roundrect_rratio": 0.25})
    for i, (x, y) in enumerate(((-3.5, 0.0), (3.5, 0.0))):
        items.append({"number": f"SH{i + 1}", "type": "smd", "shape": "rect", "at": (x, y, 90.0),
                      "size": (1.0, 1.6), "layers": ("F.Cu", "F.Mask")})
    graphics = _box("F.Fab", 3.1, 3.1, 0.1) + _box("F.SilkS", 3.4, 3.4, 0.12) \
        + _court_box(items, margin=0.25, floor=3.7)
    return FootprintDef(name=name, pads=items, graphics=graphics, description="6x6mm tactile switch",
                        source="C&K KSC9 outline", tags="switch smd")


def mounting_hole(name: str = "MountingHole_3.2mm", *, drill: float = 3.2, pad: float = 5.0,
                  plated: bool = False) -> FootprintDef:
    # KiCad's grammar knows smd | thru_hole | np_thru_hole | connect — "np_thru" (no _hole) is a
    # common shorthand that the parser rejects, so never write it
    ptype = "thru_hole" if plated else "np_thru_hole"
    shape = "circle"
    items = [{"number": "1" if plated else "", "type": ptype, "shape": shape,
              "at": (0.0, 0.0, 0.0), "size": (pad, pad), "drill": drill,
              "layers": ("*.Cu", "*.Mask") if plated else ("F.Cu", "B.Cu"),
              "net": "GND" if plated else ""}]
    graphics = _box("F.CrtYd", pad / 2.0 + 0.25, pad / 2.0 + 0.25, 0.05)
    graphics.append({"kind": "circle", "layer": "F.Fab", "center": (0.0, 0.0), "radius": pad / 2.0,
                     "width": 0.1})
    return FootprintDef(name=name, pads=items, graphics=graphics,
                        description=f"{drill}mm mounting hole{' (plated to GND)' if plated else ''}",
                        source="mechanical", tags="mounting hole",
                        attr=["through_hole" if plated else "smd"])


#: name → factory, so a spec file can say ``footprint: {kind: lqfp, ...}``
REGISTRY: Dict[str, object] = {
    "qfp": qfp, "lqfp": lqfp, "tqfp": tqfp, "soic": soic, "tssop": tssop, "sot23": sot23,
    "sot23_5": sot23_5, "dfn": dfn, "passive": passive, "0402": lambda: passive("0402"),
    "0603": lambda: passive("0603"), "0805": lambda: passive("0805"), "1206": lambda: passive("1206"),
    "crystal": crystal, "inductor": smd_inductor, "header": pin_header, "pin_header": pin_header,
    "usb_c": usb_c, "tact": tact, "mounting_hole": mounting_hole,
}


class UnknownFootprintKind(ValueError, KeyError):
    """A spec named a footprint kind that does not exist.

    Both base classes on purpose: a spec typo is a *value* error for a human reading the CLI, and
    callers that already guard the registry lookup with ``except KeyError`` keep working.
    """


def from_spec(spec: Union[dict, str]) -> FootprintDef:
    """``{"kind": "lqfp", "leads_per_side": 12, …}`` → :class:`FootprintDef`.

    Unknown kinds raise :class:`UnknownFootprintKind` with the list of supported ones, rather than
    silently inventing a footprint: a made-up pad stack is the fastest way to ship a board that
    cannot be assembled.
    """
    if isinstance(spec, str):
        spec = {"kind": spec}
    if not isinstance(spec, dict):
        raise ValueError(f"a footprint spec must be a mapping or a kind string, not "
                         f"{type(spec).__name__}: {spec!r}")
    kind = spec.get("kind", "passive")
    if kind not in REGISTRY:
        raise UnknownFootprintKind(f"unknown footprint kind {kind!r}; supported: "
                                   f"{', '.join(sorted(REGISTRY))}")
    factory = REGISTRY[kind]
    if isinstance(factory, FootprintDef):
        return factory
    # every remaining key is a constructor argument; ``pins`` legitimately means "pin function
    # names" for a QFP and "pad count" for a header, so it must not be filtered out here
    args = {k: v for k, v in spec.items() if k != "kind"}
    return factory(**args) if args else factory()
