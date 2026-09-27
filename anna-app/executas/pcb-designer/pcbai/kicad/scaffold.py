"""Design spec (YAML/JSON/dict) → a KiCad 10 board that is *placeable, connected and checkable*.

This is the bridge between "the agent thought about the circuit" and "a file you can open in Pcbnew".
It is deliberately **not** an autorouter; it does the four things that make a hand-off to a human fast:

1. **Real copper** — footprints come from :mod:`pcbai.kicad.footprints`, so pad geometry is derived
   from package drawings instead of invented.
2. **Stackup** — 4-layer signal/GND/power/signal planes on ``In1.Cu``/``In2.Cu`` with the net-class
   clearances written into ``.kicad_pro`` where KiCad 10 reads them.
3. **Fanout** — every ground pad gets a stitching via to the GND plane, every power pad a via to its
   rail plus a decoupling cap placed on the same grid; short L-routes are attempted for 2-pad nets and
   *skipped with a report line* when something is in the way, rather than quietly crossing copper.
4. **Self-inspection** — :meth:`pcbai.kicad.model.Board.checks` runs before anything is written, so the
   tool refuses to claim a board is ready when its own clearance/outline/net rules fail.

The output is a *scaffold*: parts placed, planes defined, power fanned out, signals left as ratsnest
for interactive routing in KiCad. ``report()["unrouted_nets"]`` is the exact punch list.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.logger import get_logger
from .footprints import FootprintDef, from_spec
from .layers import copper_layers, expand_layers, layer_matches
from .model import (_pt_obb_gap, _seg_obb_gap, _seg_seg_distance, Board,
                    DesignRules, Footprint,
                    Graphic, Issue, NetClass, Pad, StackLayer, Track,
                    Via, Zone)
from .pcb_writer import write_project_files

__all__ = ["ScaffoldResult", "build_board", "generate", "load_spec", "DEFAULT_RULES"]

LOG = get_logger("kicad.scaffold")

XY = Tuple[float, float]

#: Baseline limits a mainstream 4-layer prototyper accepts (JLCPCB JLC04101H-7628: 5 mil trace /
#: 5 mil clearance outer, 0.3 mm drill, 0.6 mm via). They are a *starting point*, not a fab promise:
#: the numbers that matter are your fab's capability file — see AUDIT §11 Q4 and ``pcbai/rules``.
#: 0.127 mm is deliberately not rounded up to 0.2: at 0.5 mm QFP pitch the pad-to-pad gap is 0.05 mm
#: and a 0.2 mm fanout track cannot escape the pad ring at all, so a "safe" 0.2 makes the board
#: unroutable rather than safer.
DEFAULT_RULES: Dict[str, float] = {
    "min_clearance": 0.127, "min_track_width": 0.127, "min_via_drill": 0.3, "min_via_diameter": 0.6,
    "min_hole_to_hole": 0.5, "min_copper_to_edge": 0.2, "min_text_height": 1.0,
    "min_text_thickness": 0.15, "min_courtyard_clearance": 0.25, "annular_ring_min": 0.15,
    "min_silk_to_silk": 0.15, "pad_to_mask_clearance": 0.0,
}


def load_spec(source: Any) -> Dict[str, Any]:
    """Accept a path to ``.yaml``/``.json`` or an already-loaded dict."""
    if isinstance(source, dict):
        return source
    path = Path(source)
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml                                    # optional dependency, on purpose
        except ImportError as exc:                        # pragma: no cover
            raise RuntimeError("PyYAML is needed for .yaml specs (pip install pyyaml), or save the "
                               "spec as .json") from exc
        return yaml.safe_load(text)
    return json.loads(text)


@dataclass
class PartSpec:
    ref: str
    value: str
    footprint: FootprintDef
    pins: Dict[str, str] = field(default_factory=dict)
    group: str = "other"
    at: Optional[XY] = None
    rot: float = 0.0
    layer: str = "F.Cu"
    dnp: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "PartSpec":
        if "ref" not in d:
            raise ValueError(f"part {d!r} has no 'ref'")
        fp = from_spec(d.get("footprint", "passive"))
        pins = {str(k): str(v) for k, v in (d.get("pins") or {}).items()}
        known = {str(p["number"]) for p in fp.pads}
        unknown = sorted(set(pins) - known)
        if unknown:
            raise ValueError(
                f"part {d['ref']}: netlist references pads {unknown} that do not exist on footprint "
                f"{fp.name!r} (it has {sorted(known)}). Fix the footprint kind/size or the pin map — "
                "silently dropping a connection is how an agent ships an open circuit.")
        return cls(ref=d["ref"], value=str(d.get("value", "")), footprint=fp, pins=pins,
                   group=d.get("group", "other"), at=tuple(d["at"]) if d.get("at") else None,
                   rot=float(d.get("rot", 0.0)), layer=d.get("layer", "F.Cu"),
                   dnp=bool(d.get("dnp", False)))


@dataclass
class ScaffoldResult:
    board: Board
    issues: List[Issue]
    unrouted_nets: List[str]
    routed_tracks: int
    files: Dict[str, str] = field(default_factory=dict)
    notes: Dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "summary": self.board.summary(),
                "issues": [i.to_dict() for i in self.issues],
                "unrouted_nets": self.unrouted_nets, "routed_tracks": self.routed_tracks,
                "notes": dict(self.notes), "files": dict(self.files)}


# ─────────────────────────────────────────────────────────────────────────────
# placement
# ─────────────────────────────────────────────────────────────────────────────
def _part_half(fp: FootprintDef) -> Tuple[float, float]:
    xs, ys = [], []
    for pad in fp.pads:
        px, py = pad["at"][0], pad["at"][1]
        w, h = pad["size"]
        xs += [px - w / 2, px + w / 2]
        ys += [py - h / 2, py + h / 2]
    for g in fp.graphics:
        if g.get("layer", "").endswith("CrtYd"):
            for key in ("start", "end", "center"):
                if g.get(key):
                    xs.append(g[key][0])
                    ys.append(g[key][1])
            if g.get("kind") == "circle" and g.get("radius"):
                cx, cy = g["center"]
                xs += [cx - g["radius"], cx + g["radius"]]
                ys += [cy - g["radius"], cy + g["radius"]]
        else:
            for key in ("start", "end"):
                if g.get(key):
                    xs.append(g[key][0])
                    ys.append(g[key][1])
    if not xs:
        return 1.0, 1.0
    return (max(xs) - min(xs)) / 2.0 + 0.05, (max(ys) - min(ys)) / 2.0 + 0.05


def _part_area(fp: FootprintDef) -> float:
    hw, hh = _part_half(fp)
    return 4 * hw * hh


def _overlaps(a: Sequence[float], b: Sequence[float]) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _band(band_w: float, x0: float, x1: float, y0: float, y1: float,
           parts: List["PartSpec"], gap: float, board: "Board", overflow: List[str],
           blocked: List[Tuple[float, float, float, float]]) -> None:
    """Shelf packing inside one band, rows bottom-up, biggest parts first.

    ``blocked`` is everything already on the board; an obstacle is stepped *over* (never poked at)
    and parts that cannot be fitted anywhere in the band are returned through ``overflow`` so the
    caller can report them instead of the generator inventing a position.
    """
    del band_w                                                   # kept for callers/logs
    if not parts:
        return
    x, y, row_h = x0, y0, 0.0
    for part in sorted(parts, key=lambda p: -_part_area(p.footprint)):
        hw, hh = _part_half(part.footprint)
        w, h = 2 * hw, 2 * hh
        if x + w + 2 * gap > x1 and x > x0 + 1e-6:               # row is full → new shelf
            x, y, row_h = x0, y + row_h + 2 * gap, 0.0
        fits = False
        for _ in range(600):
            if y + h + 2 * gap > y1:
                break                                  # this band is full: record and carry on
            cand = (x, y, min(x1, x + w + 2 * gap), y + h + 2 * gap)
            hit = next((b for b in blocked if _overlaps(cand, b)), None)
            if hit is None:
                cx, cy = x + gap + hw, y + gap + hh
                fp = part.footprint.instance(part.ref, part.value, x=round(cx, 4), y=round(cy, 4),
                                             rot=part.rot, layer=part.layer,
                                             pad_nets=dict(part.pins))
                fp.dnp = part.dnp
                board.add_footprint(fp)
                blocked.append(cand)
                x, row_h = cand[2], max(row_h, h)
                fits = True
                break
            y = max(hit[3] + 1e-6, y + gap)                       # step completely over the obstacle
        if not fits:
            # never `return` here: dropping the *rest* of the band silently would lose parts without
            # a trace, which is the exact failure mode this generator must not have
            overflow.append(part.ref)


def _board_boxes(board: "Board", *, slack: float = 0.2) -> Iterable[Tuple[float, float, float, float]]:
    """Occupied rectangles of everything already on the board, padded by *slack*.

    ``Footprint.bbox`` covers pads *and* courtyard graphics, which is what KiCad and the pick-and-place
    file measure with, so this is the right thing to keep clear.
    """
    for fp in board.footprints:
        b = fp.bbox()
        yield (b[0] - slack, b[1] - slack, b[2] + slack, b[3] + slack)


def _ring_around(board: "Board", centre: Footprint, parts: List["PartSpec"], *, gap: float,
                 radius: float) -> List[str]:
    """Decoupling caps belong on a ring around the supply pins, facing the part.

    The footprint box is measured *after* the ring rotation is applied: a 0402 turned 45° is a
    diamond, and its axis-aligned half-extents are smaller than the space it really needs, which is
    how a cap ends up inside the MCU's courtyard by a fifth of a millimetre.
    """
    overflow: List[str] = []
    if not parts:
        return overflow
    cx, cy = centre.x, centre.y
    slack = max(0.2, gap / 2.0 + 0.05)
    blocked_ring = [b for b in _board_boxes(board, slack=slack)]
    box = board.outline_bbox() or (0.0, 0.0, 40.0, 30.0)
    n = len(parts)
    for i, part in enumerate(parts):
        ang = 2 * math.pi * i / n + math.pi / 4
        rot = math.degrees(ang) - 90.0             # long axis tangential → pads face the MCU
        hw, hh = _part_half(part.footprint)
        rad = math.radians(rot)
        cos_r, sin_r = abs(math.cos(rad)), abs(math.sin(rad))
        ehw = hw * cos_r + hh * sin_r
        ehh = hw * sin_r + hh * cos_r
        r = radius
        placed = False
        for _ in range(10):                          # nudge outwards along the same spoke
            px, py = (part.at if part.at is not None else
                      (cx + r * math.cos(ang), cy + r * math.sin(ang)))
            mine = (px - ehw - slack, py - ehh - slack, px + ehw + slack, py + ehh + slack)
            if any(_overlaps(mine, b) for b in blocked_ring):
                r += 0.8
                continue
            if not (box[0] + ehw <= px <= box[2] - ehw and box[1] + ehh <= py <= box[3] - ehh):
                r += 0.8                             # pushed past the edge: try further out? no room
                overflow.append(part.ref)
                break
            fp = part.footprint.instance(part.ref, part.value, x=round(px, 3), y=round(py, 3),
                                         rot=round(rot, 2), layer=part.layer, pad_nets=dict(part.pins))
            fp.dnp = part.dnp
            board.add_footprint(fp)
            blocked_ring.append(mine)
            placed = True
            break
        if not placed and part.ref not in overflow:
            overflow.append(part.ref)
    return overflow


def _place(board: Board, parts: Sequence[PartSpec], *, origin: XY = (6.0, 6.0),
           gap: float = 0.35, bands: Optional[Sequence[str]] = None) -> List[str]:
    """Zone-based placement. The MCU (and anything with an explicit ``at:``) is honoured first,
    decoupling goes on a ring around it, everything else is packed into one band per group.

    Returns the refs that did not fit — reported, never silently dropped somewhere off-board.
    """
    # pack inside the *board outline*, never inside the component bbox: at this point the board
    # is empty, so bbox() would be meaningless and its fallback silently drops parts
    box = board.outline_bbox() or board.bbox() or (0.0, 0.0, 40.0, 30.0)
    ox, oy = float(origin[0]), float(origin[1])
    x0, y0 = max(box[0] + gap, ox), max(box[1] + gap, oy)
    x1, y1 = max(x0 + gap, min(box[2] - gap, ox + (box[2] - box[0]))), box[3] - gap
    overflow: List[str] = []
    anchors = [p for p in parts if p.at is not None]
    ring = [p for p in parts if p.group == "decap" and p.at is None]
    rest = [p for p in parts if p.at is None and p.group != "decap"]

    for part in anchors:                            # spec-pinned positions win
        fp = part.footprint.instance(part.ref, part.value, x=part.at[0], y=part.at[1], rot=part.rot,
                                    layer=part.layer, pad_nets=dict(part.pins))
        fp.dnp = part.dnp
        board.add_footprint(fp)
    mcu_refs = {p.ref for p in anchors if p.group in ("mcu", "signal")}
    mcu = next((f for f in board.footprints if f.reference in mcu_refs and f.pads), None)
    if ring and mcu is not None:
        court = mcu.courtyard() or mcu.bbox()
        hw = max(court[2] - court[0], court[3] - court[1]) / 2.0
        overflow += _ring_around(board, mcu, ring, gap=gap, radius=float(hw) + 3.4)
    elif ring:
        rest = list(ring) + rest

    order = list(bands or []) or _group_order(rest)
    groups: Dict[str, List[PartSpec]] = {g: [p for p in rest if p.group == g] for g in order}
    groups = {g: v for g, v in groups.items() if v}
    if not groups:
        return overflow
    weight = {g: sum(math.sqrt(max(_part_area(p.footprint), 0.5)) for p in v) for g, v in groups.items()}
    total = sum(weight.values()) or 1.0
    cursor = x0
    blocked: List[Tuple[float, float, float, float]] = []
    for fp in board.footprints:                     # spec-pinned parts are obstacles
        b = fp.bbox()
        blocked.append((b[0] - gap, b[1] - gap, b[2] + gap, b[3] + gap))
    for idx, (g, members) in enumerate(groups.items()):
        bx0 = cursor
        bx1 = x1 if idx == len(groups) - 1 else min(x1, cursor + (x1 - x0) * weight[g] / total)
        _band(bx1 - bx0, bx0, bx1, y0, y1, members, gap, board, overflow, blocked)
        cursor = bx1
    if overflow:
        # a group ran out of room: retry those parts anywhere in the outline before giving up
        retry = [p for p in rest if p.ref in set(overflow)]
        overflow.clear()
        _band(x1 - x0, x0, x1, y0, y1, retry, gap, board, overflow, blocked)
    return overflow


def _group_order(parts: Sequence[PartSpec]) -> List[str]:
    seen: List[str] = []
    for p in parts:
        if p.group not in seen:
            seen.append(p.group)
    # power near the edge, debug/IO on the outside, everything else in the middle
    priority = {"power": 0, "clock": 1, "input": 2, "mcu": 3, "other": 4, "debug": 5, "io": 6,
                "mech": 7}
    return sorted(seen, key=lambda g: (priority.get(g, 4), g))

# ─────────────────────────────────────────────────────────────────────────────
# planes, fanout, simple routing
# ─────────────────────────────────────────────────────────────────────────────
def _inset(box: Sequence[float], margin: float) -> List[XY]:
    x0, y0, x1, y1 = box
    return [(x0 + margin, y0 + margin), (x1 - margin, y0 + margin), (x1 - margin, y1 - margin),
            (x0 + margin, y1 - margin)]


def add_planes(board: Board, planes: Sequence[dict]) -> List[str]:
    """Create the copper pours the spec asks for (usually GND on In1.Cu, the rail on In2.Cu)."""
    notes: List[str] = []
    box = board.outline_bbox() or board.bbox() or (0, 0, 40, 30)
    for spec in planes:
        layer = spec.get("layer", "In1.Cu")
        if layer not in board.copper:
            notes.append(f"plane on {layer} skipped: {layer} is not in this stackup")
            continue
        net = spec.get("net", "GND")
        board.net(net, net_class=spec.get("net_class", "Power" if net != "GND" else "Default"))
        keepout = not bool(spec.get("fill", True))
        zone = Zone(net="" if keepout else net, layer=layer,
                    outline=_inset(box, float(spec.get("margin", 0.3))),
                    name=str(spec.get("name") or f"{net}_plane"),
                    clearance=float(spec.get("clearance", board.rules.min_clearance)),
                    min_thickness=float(spec.get("min_thickness", 0.25)),
                    thermal_gap=float(spec.get("thermal_gap", 0.25)),
                    thermal_bridge_width=float(spec.get("thermal_bridge_width", 0.5)),
                    priority=int(spec.get("priority", 0)), fill=not keepout, keepout=keepout,
                    hatch_style="none" if not keepout else "full")
        board.zones.append(zone)
    return notes


class CopperMap:
    """Running index of every copper rectangle and segment on the board, with the rule that applies.

    The generator asks it "may I put this stub / via / track here?" *before* it emits geometry, so the
    output cannot contain a clearance violation against copper it added itself. Pads of the same net
    are not obstacles (they are the net), and nothing is checked across layers.
    """

    def __init__(self, board: Board) -> None:
        self.board = board
        # the pad as a *rotated* rect, so a legality test here measures the same thing Board.checks()
        # measures: a bounding box is bigger than a 45° pad and disagrees with the checker in the
        # permissive direction, which is how a generator ends up shipping its own violations
        self.pad_boxes: List[Tuple[Sequence[float], str, Tuple[float, ...]]] = []
        for fp in board.footprints:
            for pad in fp.pads:
                # a wildcard or empty list is expanded against *this* stack, so a header pad is a
                # real obstacle on the layer the router is about to use
                self.pad_boxes.append((pad.rect(fp), pad.net,
                                       expand_layers(pad.layers, board.copper)))
        self.segments: List[Tuple[XY, XY, float, str, str]] = []      # start, end, width, net, layer
        for t in board.tracks:
            self.segments.append((t.start, t.end, t.width, t.net, t.layer))
        self.disks: List[Tuple[XY, float, float, str, Tuple[str, ...]]] = []   # at, r, drill, net, layers
        for v in board.vias:
            self.disks.append((v.at, v.size / 2.0, v.drill, v.net, tuple(v.layers)))
        self.stubs = 0

    # ── rules ─────────────────────────────────────────────────────────────────
    def clearance(self, net: str, other: str = "") -> float:
        rules = self.board.rules
        need = max(rules.min_clearance, rules.clearance_for(net).clearance)
        if other and other != net:
            need = max(need, rules.clearance_for(other).clearance)   # KiCad: larger class wins
        return need

    def width(self, net: str) -> float:
        rules = self.board.rules
        return max(rules.clearance_for(net).track_width, rules.min_track_width)

    def on_board(self, x: float, y: float, margin: float) -> bool:
        box = self.board.outline_bbox() or self.board.bbox() or (0, 0, 100, 100)
        edge = self.board.rules.min_copper_to_edge
        return (box[0] + edge + margin <= x <= box[2] - edge - margin
                and box[1] + edge + margin <= y <= box[3] - edge - margin)

    def track_ok(self, a: XY, b: XY, net: str, layer: str, width: float) -> bool:
        """May a *width* mm track run from a to b on *layer* without touching another net's copper?"""
        for rect, onet, layers in self.pad_boxes:
            if onet == net or layer not in layers:        # already expanded, so plain membership
                continue
            if _seg_obb_gap(a, b, rect) < self.clearance(net, onet) + width / 2.0:
                return False
        for (s, e, w2, onet, olayer) in self.segments:
            if onet == net or olayer != layer:
                continue
            if _seg_seg_distance(a, b, s, e) < self.clearance(net, onet) + width / 2.0 + w2 / 2.0:
                return False
        for (at, r, _drill, onet, olayers) in self.disks:
            if onet == net or layer not in olayers:
                continue
            # a via is a disc: distance from the centre minus its radius, not a square around it
            if _seg_seg_distance(a, b, at, at) - r < self.clearance(net, onet) + width / 2.0:
                return False
        return True

    def via_ok(self, at: XY, net: str, size: float, drill: float,
               layers: Sequence[str]) -> bool:
        if not self.on_board(at[0], at[1], size / 2.0):
            return False
        for rect, onet, players in self.pad_boxes:
            if onet == net or not (set(layers) & set(players)):
                continue
            if _pt_obb_gap(at, rect) - size / 2.0 < self.clearance(net, onet):
                return False
        for (o_at, r, o_drill, onet, olayers) in self.disks:
            if not set(layers) & set(olayers):
                continue
            centre = math.hypot(at[0] - o_at[0], at[1] - o_at[1])
            if onet != net and centre < r + size / 2.0 + self.clearance(net, onet):
                return False
            if centre and centre < max(self.board.rules.min_hole_to_hole,
                                       (drill + o_drill) / 2.0 + 0.05):
                return False
        for (s, e, w2, onet, olayer) in self.segments:
            if onet == net or olayer not in layers:
                continue
            if _seg_seg_distance(s, e, at, at) - w2 / 2.0 < size / 2.0 + self.clearance(net, onet):
                return False
        return True

    # ── transactional helpers ─────────────────────────────────────────────────
    def snapshot(self) -> Tuple[int, int, int, int]:
        return (len(self.segments), len(self.disks), len(self.board.tracks), len(self.board.vias))

    def rollback(self, mark: Tuple[int, int, int, int]) -> None:
        """Undo copper emitted after *mark* (append-only, so truncation is exact)."""
        del self.board.tracks[mark[2]:]
        del self.board.vias[mark[3]:]
        del self.segments[mark[0]:]
        del self.disks[mark[1]:]

    # ── mutation ──────────────────────────────────────────────────────────────
    def add_track(self, a: XY, b: XY, net: str, layer: str, width: float) -> None:
        self.board.tracks.append(Track(start=a, end=b, net=net, width=width, layer=layer))
        self.segments.append((a, b, width, net, layer))

    def add_via(self, at: XY, net: str, size: float, drill: float,
                layers: Sequence[str]) -> None:
        via = Via(at=at, net=net, size=size, drill=drill, layers=tuple(layers))
        self.board.vias.append(via)
        self.disks.append((at, size / 2.0, drill, net, tuple(layers)))

    # ── one-step helpers used by fanout() and route_two_pad_nets() ────────────
    def route_stub(self, a: XY, b: XY, net: str, layer: str) -> bool:
        """Emit a stub when it is legal; returns whether it was drawn."""
        if a == b:
            return True
        if not self.track_ok(a, b, net, layer, self.width(net)):
            return False
        self.add_track(a, b, net, layer, self.width(net))
        self.stubs += 1
        return True

    def escape(self, fp: Footprint, pad: Pad, layer: str, *, size: float, drill: float,
               reach: float = 1.4) -> Optional[XY]:
        """Via (+ stub when needed) that brings *pad* onto *layer*; None if nothing is legal."""
        cx, cy, _ = pad.absolute(fp)
        own = "B.Cu" if fp.layer.startswith("B") else "F.Cu"
        if layer_matches(layer, pad.layers, fallback=(own, "B.Cu", "F.Cu")):
            return (cx, cy)      # through-hole pads are copper on both sides: no escape needed
        away = _away_from_pads(fp, pad)
        step = max(reach, (min(pad.size) + size) / 2.0 + self.clearance(pad.net) + 0.05)
        dirs = [(-away[0], -away[1]), (0.0, -1.0), (0.0, 1.0), (1.0, 0.0), (-1.0, 0.0),
                (0.72, 0.72), (-0.72, -0.72), (0.72, -0.72), (-0.72, 0.72)]
        for k in (1.0, 1.6, 2.2, 3.0):
            for dx, dy in dirs:
                at = (round(cx + dx * step * k, 4), round(cy + dy * step * k, 4))
                if not self.via_ok(at, pad.net, size, drill, (own, layer)):
                    continue
                if not self.route_stub((cx, cy), at, pad.net, own):
                    continue
                self.add_via(at, pad.net, size, drill, (own, layer))
                return at
        return None


def fanout(board: Board, *, via_size: Optional[float] = None, via_drill: Optional[float] = None,
           stitch_pads: bool = True, power_nets: Sequence[str] = ("+3V3", "+5V", "VBUS"),
           ground_net: str = "GND", cmap: Optional[CopperMap] = None) -> Dict[str, object]:
    """Plane stitching: a via next to every ground pad, a via + stub on every power pad.

    Vias that would crowd the outline or another net's copper are skipped and counted, so the caller
    reports "24 of 31 ground pads stitched" instead of claiming a finished board.
    """
    rules = board.rules
    cmap = cmap or CopperMap(board)
    power = rules.net_classes.get("Power", NetClass("Power"))
    size = via_size or max(rules.min_via_diameter, power.via_diameter)
    drill = via_drill or max(rules.min_via_drill, power.via_drill)
    plane_layers = {z.layer for z in board.zones if z.fill and not z.keepout}
    made = skipped = 0
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net not in power_nets and not (pad.net == ground_net and stitch_pads):
                continue
            own = "B.Cu" if fp.layer.startswith("B") else "F.Cu"
            if pad.net == ground_net:
                target = "B.Cu" if "B.Cu" in board.copper else own
            else:
                target = next((z.layer for z in board.zones if z.net == pad.net and z.fill), own)
            if cmap.escape(fp, pad, target, size=size, drill=drill) is None:
                skipped += 1
            else:
                made += 1
    return {"vias_added": made, "vias_skipped": skipped, "plane_layers": sorted(plane_layers),
            "stub_tracks": cmap.stubs}


def _away_from_pads(fp: Footprint, pad: Pad) -> Tuple[float, float]:
    """Unit direction pointing away from the closest other pads of the same footprint."""
    px, py, _ = pad.absolute(fp)
    dx = dy = 0.0
    for other in fp.pads:
        if other is pad:
            continue
        ox, oy, _ = other.absolute(fp)
        dx += px - ox
        dy += py - oy
    norm = math.hypot(dx, dy) or 1.0
    return (dx / norm, dy / norm)


def route_two_pad_nets(board: Board, *, prefer_layer: str = "B.Cu", escape_vias: bool = True,
                       cmap: Optional[CopperMap] = None) -> Tuple[int, List[str]]:
    """Route every two-pad net: checked escape at each end, then an L-run on one layer.

    Signal nets default to the bottom layer — that is what a first pass under an SMD MCU looks like
    and it keeps the run clear of the top-side pad ring. A net is drawn only when the geometry has
    been proven to satisfy the net class' clearance against every *other* net's copper on that layer;
    otherwise it is returned in ``skipped`` for the interactive router.
    """
    by_net: Dict[str, List[Tuple[Footprint, Pad]]] = {}
    for fp, pad in board.all_pads():
        if pad.net:
            by_net.setdefault(pad.net, []).append((fp, pad))
    rules = board.rules
    cmap = cmap or CopperMap(board)
    default = rules.net_classes.get("Default", NetClass("Default"))
    size = max(rules.min_via_diameter, default.via_diameter)
    drill = max(rules.min_via_drill, default.via_drill)
    routed = 0
    skipped: List[str] = []
    for net, members in sorted(by_net.items()):
        if len(members) != 2:
            continue
        (f1, p1), (f2, p2) = members
        if f1 is f2:
            continue                                    # intra-footprint nets are the schematic's job
        # Which layer can both ends reach? A pad can always be brought to the outer layer on its own
        # side, and a checked via brings it to the other side — so both outer layers are candidates
        # (inner plane layers are deliberately not used for signals by a scaffold).
        sides = {"F.Cu", "B.Cu"} & set(board.copper)
        layers = [ly for ly in (prefer_layer, "B.Cu", "F.Cu") if ly in sides]
        width = cmap.width(net)
        box = board.outline_bbox() or (0.0, 0.0, 100.0, 100.0)
        lane_lo = box[1] + board.rules.min_copper_to_edge + 0.35
        lane_hi = box[3] - board.rules.min_copper_to_edge - 0.35
        drawn = False
        for layer in layers:
            mark = cmap.snapshot()                      # escape copper is only kept if the net lands
            a = (cmap.escape(f1, p1, layer, size=size, drill=drill) if escape_vias
                 else p1.absolute(f1)[:2])
            b = (cmap.escape(f2, p2, layer, size=size, drill=drill) if escape_vias
                 else p2.absolute(f2)[:2])
            if a is None or b is None:
                cmap.rollback(mark)
                continue
            if a != b:
                mx, my = round((a[0] + b[0]) / 2.0, 4), round((a[1] + b[1]) / 2.0, 4)
                for path in ((a, (b[0], a[1]), b), (a, (a[0], b[1]), b),
                             (a, (mx, a[1]), (mx, b[1]), b), (a, (a[0], my), (b[0], my), b),
                             (a, (a[0], lane_lo), (b[0], lane_lo), b),
                             (a, (a[0], lane_hi), (b[0], lane_hi), b)):
                    segs = [seg for seg in zip(path, path[1:]) if seg[0] != seg[1]]
                    if not all(cmap.track_ok(s, e, net, layer, width) for s, e in segs):
                        continue
                    for s, e in segs:
                        cmap.add_track(s, e, net, layer, width)
                        routed += 1
                    drawn = True
                    break
            else:
                drawn = True
            if not drawn:
                cmap.rollback(mark)
            else:
                break
        if not drawn:
            skipped.append(net)
    return routed, skipped


# ─────────────────────────────────────────────────────────────────────────────
# top level
# ─────────────────────────────────────────────────────────────────────────────
def _coerce_rule(attr: str, value: object, current: object) -> object:
    """Apply a spec rule with the *field's* type, not blanket ``float()``.

    ``tenting`` is a bool: coercing it to 0.0/1.0 writes ``0.0`` into ``.kicad_pro``'s ``pcbai``
    block, which the reader (correctly) ignores because KiCad's own severities are booleans — so the
    project would say "no tenting" in the board file and "yes" in the project file. Same trap for any
    int-valued knob added later.
    """
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(value))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"rules.{attr} needs a number, got {value!r}") from None


def build_board(spec: Dict[str, Any]) -> Board:
    """Spec dict → :class:`Board` (no routing yet)."""
    n_cu = int(spec.get("stackup", {}).get("copper_layers", 4))
    board = Board(title=str(spec.get("title", spec.get("name", "pcbai board"))),
                  revision=str(spec.get("revision", "0.1")),
                  company=str(spec.get("company", "")),
                  comment=str(spec.get("note", "Generated by pcbai")),
                  n_copper=n_cu, copper=copper_layers(n_cu),
                  thickness=float(spec.get("stackup", {}).get("thickness", 1.6)))
    outline = spec.get("outline") or {}
    if outline.get("polygon"):
        board.set_outline_polygon([(float(p[0]), float(p[1])) for p in outline["polygon"]])
    else:
        board.set_outline_rect(float(outline.get("width", 45.0)), float(outline.get("height", 35.0)),
                              origin=tuple(outline.get("origin", (0.0, 0.0))))

    rules = DesignRules(**{k: v for k, v in DEFAULT_RULES.items()})
    for key, value in (spec.get("rules") or {}).items():
        if hasattr(rules, key) and key != "net_classes":
            setattr(rules, key, _coerce_rule(key, value, getattr(rules, key)))
    # the Default class is derived *after* the project rules, from the effective numbers: build it
    # first and a spec that tightens `min_clearance` still routes and checks at the generic default,
    # i.e. the edit a user makes to the rules file silently does nothing
    rules.net_classes = {"Default": NetClass("Default", clearance=rules.min_clearance,
                                             track_width=rules.min_track_width,
                                             via_diameter=rules.min_via_diameter,
                                             via_drill=rules.min_via_drill)}
    for name, cfg in (spec.get("net_classes") or {}).items():
        cls = NetClass(name)
        for key in ("clearance", "track_width", "via_diameter", "via_drill", "microvia_diameter",
                    "microvia_drill", "diff_pair_gap", "diff_pair_width"):
            if key in cfg:
                setattr(cls, key, float(cfg[key]))
        cls.nets = [str(n) for n in cfg.get("nets", [])]
        cls.description = str(cfg.get("description", ""))
        rules.net_classes[name] = cls
        for net in cls.nets:
            board.net(net, net_class=name)
    board.rules = rules
    board.stackup = _stackup(spec, n_cu)
    parts = [PartSpec.from_dict(item) for item in spec.get("parts", [])]
    placement = spec.get("placement") or {}
    overflow = _place(board, parts, origin=tuple(placement.get("origin", (6.0, 6.0))),
                      gap=float(placement.get("gap", 0.35)), bands=placement.get("bands"))
    # whatever the packer reported, plus whatever it forgot to report: a spec part that is not on the
    # board is a bug in the generator, and it must show up as an error rather than a smaller board
    placed = {f.reference for f in board.footprints}
    missing = sorted({p.ref for p in parts} - placed)
    for ref in missing:
        if ref not in overflow:
            overflow.append(ref)
    if overflow:
        board.variables["placement_overflow"] = ",".join(sorted(set(overflow)))
    for note in add_planes(board, spec.get("planes") or []):
        LOG.warning("plane skipped: %s", note)
        board.variables[f"plane_note_{len(board.variables)}"] = note
    for label in spec.get("silkscreen") or []:
        board.graphics.append(Graphic("text", str(label.get("layer", "F.SilkS")),
                                      at=(float(label["x"]), float(label["y"])),
                                      text=str(label.get("text", "")),
                                      rot=float(label.get("rot", 0.0)),
                                      width=float(label.get("width", 0.15))))
    return board


def _stackup(spec: Dict[str, Any], n_cu: int) -> List[StackLayer]:
    cfg = spec.get("stackup") or {}
    return StackLayer.default_stackup(n_cu, core_count=int(cfg.get("core_count", 1)),
                                      prepreg=float(cfg.get("prepreg", 0.214)),
                                      core=float(cfg.get("core", 1.069)),
                                      copper_mm=float(cfg.get("copper_outer_mm", 0.05)),
                                      mask_mm=float(cfg.get("mask", 0.01)),
                                      epsilon_r=float(cfg.get("epsilon_r", 4.4)),
                                      loss_tangent=float(cfg.get("loss_tangent", 0.017)))


def generate(spec: Any, out_dir: Path, *, stem: Optional[str] = None, dialect: str = "kicad-10",
             route: bool = True, fanout_power: bool = True, keep_history: bool = True,
             verify: bool = True) -> ScaffoldResult:
    """Build, self-check, optionally fan out + route, then write the project files."""
    data = load_spec(spec) if not isinstance(spec, dict) else spec
    stem = stem or str(data.get("name", "board")).replace(" ", "-")
    board = build_board(data)
    notes: Dict[str, object] = {}
    cmap = CopperMap(board)                     # one index for every copper decision below
    before = len(board.tracks)
    if fanout_power:
        notes["fanout"] = fanout(board, power_nets=tuple(data.get("fanout", {}).get("power_nets",
                                                                                    ("+3V3", "+5V", "VBUS"))),
                                 ground_net=str(data.get("fanout", {}).get("ground_net", "GND")),
                                 cmap=cmap)
    notes["fanout_tracks"] = len(board.tracks) - before
    unrouted: List[str] = []
    if route:
        before = len(board.tracks)
        n, unrouted = route_two_pad_nets(board, cmap=cmap)
        notes["signal_tracks"] = len(board.tracks) - before
        notes["tracks_routed"] = n
    report = board.report()
    issues = [Issue(**{k: v for k, v in d.items()}) for d in report["issues"]]
    # a placed footprint's own pads: KiCad's board DRC will not look at these, so nothing else would
    for part_fp in board.footprints:
        issues += part_fp.check()
    missing = str(board.variables.get("placement_overflow", ""))
    if missing:
        # a part that never got placed is not a warning: the board is not the board the spec asked
        # for, and "ok" must never mean "the geometry we happened to emit is legal"
        issues.append(Issue("error", "placement-overflow",
                            f"{len(missing.split(','))} part(s) did not fit on a "
                            f"{board.outline_bbox()[2] - board.outline_bbox()[0]:.0f}×"
                            f"{board.outline_bbox()[3] - board.outline_bbox()[1]:.0f} mm outline: "
                            f"{missing}", hint="grow `outline`, drop parts, or merge groups"))
    files: Dict[str, str] = {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = write_project_files(board, out, stem=stem, dialect=dialect, keep_history=keep_history,
                                reason=f"scaffold {stem}")
    files["spec"] = str(out / f"{stem}.spec.json")
    from ..core.filesafe import atomic_write_text
    atomic_write_text(Path(files["spec"]), json.dumps(data, indent=2, sort_keys=True) + "\n",
                      keep_history=keep_history, reason="record the spec that produced this board")
    by_net: Dict[str, int] = {}
    for _fp, pad in board.all_pads():      # only the pad's net matters for this count
        if pad.net:
            by_net[pad.net] = by_net.get(pad.net, 0) + 1
    notes["pads_per_net"] = dict(sorted(by_net.items()))
    notes["unrouted_nets"] = sorted(set(unrouted) | {n for n, c in by_net.items() if c == 1})
    if verify:
        # re-read the file we just wrote and re-run the checks on *that*: catches a writer that
        # silently drops or reinterprets geometry, which a model-only check cannot see
        from .pcb_reader import read_board
        back = read_board(files["board"])
        back_err = [i for i in back.checks() if i.severity == "error"]
        notes["readback"] = {"footprints": len(back.footprints), "pads": len(list(back.all_pads())),
                             "tracks": len(back.tracks), "vias": len(back.vias),
                             "zones": len(back.zones), "errors": [str(i) for i in back_err]}
        for i in back_err[:20]:
            issues.append(Issue("error", f"written-{i.code}",
                                f"{i.message} (found only after reading the written file back)",
                                i.where, "the writer or the reader is wrong, not the design"))
    LOG.info("scaffold %s: %d footprints, %d tracks, %d vias, %d issues (%d errors)", stem,
             len(board.footprints), len(board.tracks), len(board.vias), len(issues),
             report["counts"]["error"])
    return ScaffoldResult(board=board, issues=issues, unrouted_nets=notes["unrouted_nets"],
                          routed_tracks=len(board.tracks), files=files, notes=notes)
