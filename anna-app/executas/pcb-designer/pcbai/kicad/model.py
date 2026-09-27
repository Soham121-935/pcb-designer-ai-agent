"""Board-level data model for KiCad designs — the thing between "an LLM said put a cap here" and a
file KiCad can open.

:mod:`pcbai.kicad.sexp` knows how to read and write S-expressions without losing formatting; this
module knows what a *board* is: nets, footprints with pads, tracks, vias, copper zones, an outline,
and the design rules those objects have to respect. ``Board.checks()`` runs the geometry/rule
inspection that would otherwise need ``pcbnew``, using nothing but the standard library, so the agent
can be told *why* its own output is bad before a human ever opens KiCad.

Editing model used by this repo:

* **inspect** → :func:`pcbai.kicad.pcb_reader.read_board` (tree → :class:`Board`)
* **mutate an existing board** → edit the :class:`~pcbai.kicad.sexp.Sexp` tree (keeps every byte the
  agent did not touch) — see ``pcbai/core/filesafe.py``
* **create a board** → build a :class:`Board` and hand it to ``pcb_writer.write_board``
"""
from __future__ import annotations

import dataclasses

import math
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .layers import LayerSpec, copper_layers, is_copper, layer_matches, layers_touch,\
    ordinal_for

__all__ = ["XY", "Issue", "Net", "Pad", "Footprint", "Track", "Via", "Zone", "Graphic",
           "NetClass", "DesignRules", "StackLayer", "Board", "new_uuid"]

XY = Tuple[float, float]

#: how many pairwise geometric checks to run before giving up (keeps inspection bounded)
MAX_PAIRWISE = 4000


def new_uuid() -> str:
    return str(_uuid.uuid4())


@dataclass(frozen=True)
class Issue:
    """One finding from :meth:`Board.checks`. ``severity`` is ``error`` or ``warning``."""

    severity: str
    code: str
    message: str
    where: str = ""
    hint: str = ""

    def to_dict(self) -> Dict[str, str]:
        out = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.where:
            out["where"] = self.where
        if self.hint:
            out["hint"] = self.hint
        return out

    def __str__(self) -> str:  # pragma: no cover - console output
        loc = f" [{self.where}]" if self.where else ""
        tip = f" — {self.hint}" if self.hint else ""
        return f"{self.severity.upper()}: {self.code}{loc}: {self.message}{tip}"


# ─────────────────────────────────────────────────────────────────────────────
# primitives
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Net:
    name: str
    net_class: str = "Default"

    def __repr__(self) -> str:  # pragma: no cover
        return f"Net({self.name!r}, class={self.net_class!r})"


@dataclass
class Pad:
    """A footprint pad. ``at``/``size`` are in mm, relative to the footprint origin."""

    number: str
    type: str = "smd"                       # smd | thru_hole | np_thru_hole | connect
    shape: str = "rect"                      # rect | roundrect | circle | oval | custom
    at: Tuple[float, float, float] = (0.0, 0.0, 0.0)   # x, y, rotation (deg)
    size: Tuple[float, float] = (0.6, 0.6)
    drill: Optional[Union[float, Tuple[float, float]]] = None
    layers: Tuple[str, ...] = ("F.Cu", "F.Paste", "F.Mask")
    net: str = ""
    roundrect_rratio: Optional[float] = None
    pin_function: Optional[str] = None
    pin_type: Optional[str] = None
    solder_mask_margin: Optional[float] = None
    zone_connect: Optional[int] = None
    thermal_gap: Optional[float] = None
    thermal_bridge_width: Optional[float] = None
    uuid: str = field(default_factory=new_uuid)

    @property
    def is_tht(self) -> bool:
        return self.type in ("thru_hole", "connect")

    def absolute(self, fp: "Footprint") -> Tuple[float, float, float]:
        """Pad position in board coordinates, mirroring Y when the footprint is on B.Cu."""
        x, y, rot = self.at
        if fp.layer.startswith("B."):
            y, rot = -y, -rot
        rad = math.radians(fp.rot)
        cos, sin = math.cos(rad), math.sin(rad)
        return (fp.x + x * cos - y * sin, fp.y + x * sin + y * cos, fp.rot + rot)

    def corners(self, fp: "Footprint") -> List[XY]:
        """Bounding rectangle corners in board coordinates (rotation-aware, drill ignored)."""
        cx, cy, rot = self.absolute(fp)
        w, h = self.size
        rad = math.radians(rot)
        out: List[XY] = []
        for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
            out.append((cx + dx * math.cos(rad) - dy * math.sin(rad),
                        cy + dx * math.sin(rad) + dy * math.cos(rad)))
        return out

    def rect(self, fp: "Footprint") -> List[float]:
        """The pad as a rotated rectangle in board coordinates: ``[cx, cy, w, h, rot]``.

        Prefer this over :meth:`extent` whenever the answer decides whether copper is legal: extent
        is an axis-aligned *bounding* box, which for a rotated pad is bigger than the pad itself.
        """
        cx, cy, rot = self.absolute(fp)
        return [cx, cy, self.size[0], self.size[1], rot]

    def rect_local(self) -> List[float]:
        """The pad as a rotated rectangle in *footprint* coordinates (footprint rotation excluded).

        Two pads of the same part only ever move rigidly together, so their relative geometry is
        frame-independent — and comparing them locally is exact without any rotation math.
        """
        cx, cy, rot = self.at
        return [cx, cy, self.size[0], self.size[1], rot]

    def extent(self, fp: "Footprint") -> Tuple[float, float, float, float]:
        xs = [c[0] for c in self.corners(fp)]
        ys = [c[1] for c in self.corners(fp)]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Graphic:
    """A footprint- or board-level drawing primitive (fp_line/gr_line/fp_text/gr_text/…)."""

    kind: str                                 # line | rect | circle | text | arc
    layer: str = "F.Fab"
    start: Optional[XY] = None
    end: Optional[XY] = None
    center: Optional[XY] = None
    radius: float = 0.0
    size: Optional[XY] = None                 # rect: (dx, dy) from start
    text: str = ""
    at: Optional[XY] = None
    rot: float = 0.0
    width: float = 0.12
    stroke_type: str = "solid"
    uuid: str = field(default_factory=new_uuid)


@dataclass
class Footprint:
    """A component instance. ``x``/``y``/``rot`` place it; pads are stored relative to the origin."""

    lib_id: str
    reference: str = ""
    value: str = ""
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    layer: str = "F.Cu"
    pads: List[Pad] = field(default_factory=list)
    graphics: List[Graphic] = field(default_factory=list)
    attr: List[str] = field(default_factory=lambda: ["smd"])
    description: str = ""
    tags: str = ""
    datasheet: str = ""
    dnp: bool = False
    exclude_from_bom: bool = False
    properties: Dict[str, str] = field(default_factory=dict)
    uuid: str = field(default_factory=new_uuid)
    path: str = ""

    @property
    def name(self) -> str:
        return self.lib_id.split(":")[-1]

    def add_pad(self, pad: Pad) -> Pad:
        self.pads.append(pad)
        return pad

    def check(self, *, min_gap: float = 0.05) -> List[Issue]:
        """Spacing between *this part's own* pads, as a placed footprint should present it.

        KiCad's board DRC does not compare a footprint's pads with each other, so this is the check
        that catches the mistake a generator actually makes: a pin map that puts two nets on pads
        which touch, or a pad stack too fat for its own pitch. Overlap is an error; a gap under
        *min_gap* is a warning, because 0.05 mm is exactly what a fine-pitch row looks like.
        """
        out: List[Issue] = []
        pads = self.pads
        for i in range(len(pads)):
            for j in range(i + 1, len(pads)):
                a, b = pads[i], pads[j]
                if not a.net or not b.net or a.net == b.net:
                    continue
                if not layers_touch(a.layers, b.layers, fallback=("F.Cu", "B.Cu")):
                    continue        # pads that share no copper cannot touch
                gap = _obb_gap(a.rect_local(), b.rect_local())
                if gap + 1e-6 >= min_gap:      # a row at exactly its pitch is not a finding
                    continue
                if gap < 0.0:
                    out.append(Issue("error", "pad-overlap",
                                     f"{self.reference}.{a.number} ('{a.net}') and "
                                     f"{self.reference}.{b.number} ('{b.net}') overlap by "
                                     f"{-gap:.3f}mm", self.reference,
                                     "two nets must not share copper: fix the footprint or the pin "
                                     "map (a net-tie footprint is the exception, and it is declared)"))
                else:
                    out.append(Issue("warning", "pad-gap-tiny",
                                     f"{self.reference}'s pads {a.number} and {b.number} are "
                                     f"{gap:.3f}mm apart", self.reference,
                                     "normal for a fine-pitch row; a bridge risk on anything wider"))
        # pad type / drill / annular-ring findings deliberately live in ``Board.checks``: that is
        # where the stackup and the per-class rules are known, and repeating them here would show the
        # same defect twice in two wordings.
        return out

    def pad_nets(self) -> List[str]:
        """Distinct non-empty net names on this footprint's pads, in pad order."""
        seen: List[str] = []
        for pad in self.pads:
            if pad.net and pad.net not in seen:
                seen.append(pad.net)
        return seen

    def bbox(self) -> Tuple[float, float, float, float]:
        if not self.pads and not self.graphics:
            return self.x - 1, self.y - 1, self.x + 1, self.y + 1
        xs: List[float] = []
        ys: List[float] = []
        for pad in self.pads:
            x0, y0, x1, y1 = pad.extent(self)
            xs += [x0, x1]
            ys += [y0, y1]
        for g in self.graphics:
            for pt in self._graphic_points(g):
                xs.append(pt[0])
                ys.append(pt[1])
        return min(xs), min(ys), max(xs), max(ys)

    def _graphic_points(self, g: Graphic) -> List[XY]:
        pts = [p for p in (g.start, g.end, g.center, g.at) if p]
        if g.kind == "rect" and g.start and g.size:
            x, y = g.start
            dx, dy = g.size
            pts = [(x, y), (x + dx, y), (x + dx, y + dy), (x, y + dy)]
        elif g.kind == "circle" and g.center:
            cx, cy = g.center
            pts = [(cx - g.radius, cy - g.radius), (cx + g.radius, cy + g.radius)]
        return [_local_to_board(p, self) for p in pts]

    def courtyard(self) -> Optional[Tuple[float, float, float, float]]:
        boxes = [self._graphic_bbox(g) for g in self.graphics if g.layer.endswith("CrtYd")]
        boxes = [b for b in boxes if b]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def _graphic_bbox(self, g: Graphic) -> Optional[Tuple[float, float, float, float]]:
        pts = self._graphic_points(g)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)


def _local_to_board(p: XY, fp: Footprint) -> XY:
    x, y = p
    if fp.layer.startswith("B."):
        y = -y
    rad = math.radians(fp.rot)
    return (fp.x + x * math.cos(rad) - y * math.sin(rad), fp.y + x * math.sin(rad) + y * math.cos(rad))


@dataclass
class Track:
    """A copper segment on one layer."""

    start: XY
    end: XY
    net: str
    width: float = 0.2
    layer: str = "F.Cu"
    uuid: str = field(default_factory=new_uuid)

    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass
class Via:
    at: XY
    net: str
    size: float = 0.6
    drill: float = 0.3
    layers: Tuple[str, str] = ("F.Cu", "B.Cu")
    type: str = "through"
    uuid: str = field(default_factory=new_uuid)


@dataclass
class Zone:
    """A copper pour (plane) or a keepout, defined by a closed outline."""

    net: str
    layer: str = "In1.Cu"
    outline: List[XY] = field(default_factory=list)
    name: str = ""
    priority: int = 0
    min_thickness: float = 0.25
    clearance: float = 0.25
    thermal_gap: float = 0.25
    thermal_bridge_width: float = 0.5
    fill: bool = True
    hatch_style: str = "none" if fill else "full"
    hatch_spacing: float = 0.5
    keepout: bool = False
    filled_polygon: List[XY] = field(default_factory=list)
    uuid: str = field(default_factory=new_uuid)


# ─────────────────────────────────────────────────────────────────────────────
# rules
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class NetClass:
    """Design rules for a group of nets. KiCad 10 stores these in ``.kicad_pro`` (``net_settings``)."""

    name: str = "Default"
    clearance: float = 0.2
    track_width: float = 0.2
    via_diameter: float = 0.6
    via_drill: float = 0.3
    microvia_diameter: float = 0.3
    microvia_drill: float = 0.1
    diff_pair_gap: float = 0.25
    diff_pair_width: float = 0.2
    nets: List[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, object]:
        """The class as the *board model* sees it (``to_pro_dict`` is the ``.kicad_pro`` shape)."""
        return {"name": self.name, "description": self.description, "nets": list(self.nets),
                "clearance": self.clearance, "track_width": self.track_width,
                "via_diameter": self.via_diameter, "via_drill": self.via_drill,
                "microvia_diameter": self.microvia_diameter, "microvia_drill": self.microvia_drill,
                "diff_pair_gap": self.diff_pair_gap, "diff_pair_width": self.diff_pair_width}

    def to_pro_dict(self) -> Dict[str, object]:
        return {
            "bus_width": 12.0, "clearance": self.clearance, "diff_pair_gap": self.diff_pair_gap,
            "diff_pair_via_gap": self.diff_pair_via_gap if hasattr(self, "diff_pair_via_gap")
            else self.diff_pair_gap, "diff_pair_width": self.diff_pair_width, "line_style": 0,
            "microvia_diameter": self.microvia_diameter, "microvia_drill": self.microvia_drill,
            "name": self.name, "nets": list(self.nets), "pcb_color": "rgba(0, 0, 0, 0.000)",
            "priority": 2147483647,
            "schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": self.track_width,
            "tuning_profile": "", "via_diameter": self.via_diameter, "via_drill": self.via_drill,
            "wire_width": 6,
        }


@dataclass
class DesignRules:
    """Board-wide minimums. Fab limits are deliberately per-project (see docs/AUDIT.md §11 Q4):
    edit ``rules`` in the scaffold's YAML rather than assuming a vendor."""

    min_clearance: float = 0.2
    min_track_width: float = 0.2
    min_via_drill: float = 0.3
    min_via_diameter: float = 0.6
    min_hole_to_hole: float = 0.5
    min_copper_to_edge: float = 0.3
    min_silk_to_silk: float = 0.15
    min_text_height: float = 1.0
    min_text_thickness: float = 0.15
    min_courtyard_clearance: float = 0.0
    min_mask_web: float = 0.05
    pad_to_mask_clearance: float = 0.0
    annular_ring_min: float = 0.1
    tenting: bool = True
    net_classes: Dict[str, NetClass] = field(default_factory=lambda: {"Default": NetClass()})

    def clearance_for(self, net_name: str) -> NetClass:
        """The net class that owns *net_name* (falls back to Default).

        A named class always wins over Default, whatever order the two were registered in — that is
        how KiCad resolves it, and a scaffold that sets Power/USB rules must be able to trust them.
        """
        for cls in self.net_classes.values():
            if cls.name != "Default" and net_name in cls.nets:
                return cls
        default = self.net_classes.get("Default")
        if default is not None and net_name in default.nets:
            return default
        for cls in self.net_classes.values():
            if net_name in cls.nets:
                return cls
        return default or NetClass()

    def class_for(self, net: Optional[Net]) -> NetClass:
        if net is None:
            return self.net_classes["Default"]
        return self.net_classes.get(net.net_class, self.net_classes["Default"])

    def to_dict(self) -> Dict[str, object]:
        out = {k: v for k, v in self.__dict__.items() if k != "net_classes"}
        out["net_classes"] = {n: {"clearance": c.clearance, "track_width": c.track_width,
                                  "via_diameter": c.via_diameter, "via_drill": c.via_drill,
                                  "nets": list(c.nets)} for n, c in self.net_classes.items()}
        return out


@dataclass
class StackLayer:
    """One entry of the physical stackup (copper or dielectric)."""

    name: str                                 # canonical copper layer, or "core"/"prepreg"/"air"
    kind: str = "copper"                      # copper | prepreg | core | soldermask | air
    thickness: float = 0.035
    material: str = "Copper"
    epsilon_r: Optional[float] = None
    loss_tangent: Optional[float] = None
    id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.kind.capitalize(), "kind": self.kind,
                "thickness_mm": self.thickness, "material": self.material,
                "epsilon_r": self.epsilon_r, "loss_tangent": self.loss_tangent, "id": self.id}

    @staticmethod
    def default_stackup(n_copper: int = 4, *, core_count: int = 1,
                        prepreg: float = 0.214, core: float = 1.069,
                        copper_mm: float = 0.035, mask_mm: float = 0.01,
                        epsilon_r: float = 4.4, loss_tangent: float = 0.017) -> List["StackLayer"]:
        """A conventional 4-layer signal/GND/power/signal JLC-style stackup, generalised to *n*."""
        cu = copper_layers(n_copper)
        out: List[StackLayer] = [StackLayer("air", "air", 0.0, "Air")]
        for idx, layer in enumerate(cu):
            out.append(StackLayer(layer, "copper", copper_mm, "Copper", None, None,
                                  ordinal_for(layer)))
            if idx == 0:
                for _ in range(core_count):
                    out.append(StackLayer("core", "core", core, "FR4", epsilon_r, loss_tangent))
            elif idx < len(cu) - 1:
                for _ in range(max(1, core_count)):
                    out.append(StackLayer("prepreg", "prepreg", prepreg, "Prepreg FR4", epsilon_r,
                                          loss_tangent))
        out += [StackLayer("F.Mask", "soldermask", mask_mm, "Green Solder Mask"),
                StackLayer("B.Mask", "soldermask", mask_mm, "Green Solder Mask"),
                StackLayer("air", "air", 0.0, "Air")]
        # KiCad lists front-side stack entries then back-side; keep mask entries adjacent to copper
        front, back = out[1:1], out[1:]
        del front, back
        return out


# ─────────────────────────────────────────────────────────────────────────────
# board
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Board:
    """Everything needed to write (or reason about) a ``.kicad_pcb``."""

    title: str = "pcb-agent board"
    revision: str = "1.0"
    company: str = ""
    comment: str = "Generated by pcbai"
    paper: str = "A4"
    thickness: float = 1.6
    n_copper: int = 2
    copper: List[str] = field(default_factory=lambda: copper_layers(2))
    nets: List[Net] = field(default_factory=list)
    footprints: List[Footprint] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    edge: List[Graphic] = field(default_factory=list)      # Edge.Cuts segments
    graphics: List[Graphic] = field(default_factory=list)  # other board-level drawings
    rules: DesignRules = field(default_factory=DesignRules)
    stackup: List[StackLayer] = field(default_factory=list)
    extra_layers: List[LayerSpec] = field(default_factory=list)
    uuid: str = field(default_factory=new_uuid)
    variables: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep ``copper`` and ``n_copper`` from drifting apart (the #1 stackup bug in generated files)."""
        if len(self.copper) != self.n_copper:
            if self.copper == copper_layers(2) or not self.copper:
                if self.n_copper >= 2 and self.n_copper % 2 == 0:
                    self.copper = copper_layers(self.n_copper)
            else:
                self.n_copper = len(self.copper)

    # ── construction ─────────────────────────────────────────────────────────
    def find(self, reference: str) -> Optional["Footprint"]:
        """Footprint by reference (case-insensitive, ``U1`` matches ``Ref**U1`` style too)."""
        want = str(reference).strip().lower()
        for fp in self.footprints:
            if fp.reference.lower() == want:
                return fp
        for fp in self.footprints:
            if want and fp.reference.lower().startswith(want):
                return fp
        return None

    def net(self, name: str, *, net_class: str = "Default") -> Net:
        for n in self.nets:
            if n.name == name:
                if net_class != "Default" or n.net_class == "Default":
                    n.net_class = net_class
                return n
        obj = Net(name, net_class)
        self.nets.append(obj)
        # Default owns "everything nobody claimed", so it must not *list* nets: a name in
        # Default.nets would win the lookup below and hide the real class.
        if net_class != "Default":
            cls = self.rules.net_classes.setdefault(net_class, NetClass(net_class))
            if name not in cls.nets:
                cls.nets.append(name)
        return obj

    def add_footprint(self, fp: Footprint) -> Footprint:
        self.footprints.append(fp)
        for pad in fp.pads:
            if pad.net:
                self.net(pad.net)
        return fp

    def set_outline_rect(self, width: float, height: float, *, origin: XY = (0.0, 0.0)) -> None:
        x, y = origin
        corners = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
        self.edge = [Graphic("line", "Edge.Cuts", start=corners[i], end=corners[(i + 1) % 4],
                             width=0.1) for i in range(4)]

    def set_outline_polygon(self, pts: Sequence[XY], *, width: float = 0.1) -> None:
        self.edge = [Graphic("line", "Edge.Cuts", start=pts[i], end=pts[(i + 1) % len(pts)],
                             width=width) for i in range(len(pts))]

    # ── queries ──────────────────────────────────────────────────────────────
    def all_pads(self) -> Iterable[Tuple[Footprint, Pad]]:
        for fp in self.footprints:
            for pad in fp.pads:
                yield fp, pad

    def pads_on_net(self, name: str) -> List[Tuple[Footprint, Pad]]:
        return [(fp, p) for fp, p in self.all_pads() if p.net == name]

    def net_names(self) -> List[str]:
        """Every *named* net referenced on the board (KiCad's unassigned net 0 is skipped)."""
        names = {p.net for _, p in self.all_pads() if p.net}
        names |= {t.net for t in self.tracks if t.net}
        names |= {v.net for v in self.vias if v.net}
        names |= {z.net for z in self.zones if z.net}
        return sorted(names | {n.name for n in self.nets if n.name})

    def outline_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """Bounding box of the Edge.Cuts geometry only (the *board*, not the parts on it)."""
        pts: List[XY] = []
        for g in self.edge:
            pts += [p for p in (g.start, g.end) if p]
            if g.kind == "rect" and g.start and g.size:
                pts += [(g.start[0] + g.size[0], g.start[1]), (g.start[0], g.start[1] + g.size[1]),
                        (g.start[0] + g.size[0], g.start[1] + g.size[1])]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        pts: List[XY] = []
        for g in self.edge:
            pts += [p for p in (g.start, g.end) if p]
        for fp, pad in self.all_pads():
            cx, cy, _ = pad.absolute(fp)
            pts.append((cx, cy))
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def summary(self) -> Dict[str, object]:
        return {
            "title": self.title, "revision": self.revision, "company": self.company,
            "copper_layers": self.n_copper, "layers": list(self.copper),
            "thickness_mm": self.thickness, "nets": len(self.net_names()),
            "components": len(self.footprints), "pads": sum(len(f.pads) for f in self.footprints),
            "tracks": len(self.tracks), "vias": len(self.vias), "zones": len(self.zones),
            "outline_segments": len(self.edge),
            "board_bbox_mm": [round(v, 3) for v in self.bbox()] if self.bbox() else None,
            "board_size_mm": ([round(self.bbox()[2] - self.bbox()[0], 3),
                               round(self.bbox()[3] - self.bbox()[1], 3)] if self.bbox() else None),
            "design_rules": self.rules.to_dict(),
        }

    # ── self-check: what a real DRC would tell you ────────────────────────────
    def checks(self) -> List[Issue]:
        """Geometry + rule inspection that needs no KiCad. Never a substitute for ``kicad-cli drc``,
        but good enough to catch the mistakes an agent actually makes."""
        out: List[Issue] = []
        add = out.append

        if self.n_copper != len(self.copper):
            add(Issue("error", "stackup-count", f"copper list has {len(self.copper)} layers but "
                      f"n_copper={self.n_copper}", hint="set both from the same source"))
        if self.n_copper < 2 or self.n_copper % 2:
            add(Issue("error", "stackup-parity", f"{self.n_copper} copper layers: KiCad needs an "
                      "even count >= 2"))

        # outline: must be closed and connected, or nothing else can be judged
        outline_issue = self._check_outline()
        out += outline_issue
        outline = self._outline_polygons()

        refs: Dict[str, int] = {}
        uuids: Dict[str, str] = {}
        for fp in self.footprints:
            refs[fp.reference] = refs.get(fp.reference, 0) + 1
            prev = uuids.get(fp.uuid)
            if prev:
                add(Issue("error", "duplicate-uuid", f"footprints {prev} and {fp.reference} share "
                          f"uuid {fp.uuid}", fp.reference, "give every object its own uuid"))
            uuids[fp.uuid] = fp.reference or fp.lib_id
            if not fp.pads:
                add(Issue("error", "padless-footprint", f"{fp.reference or fp.lib_id} has no pads",
                          fp.reference, "a footprint with zero pads is invisible to routing"))
            declared = {n.name for n in self.nets if n.name}
            for pad in fp.pads:
                if declared and pad.net and pad.net not in declared:
                    # KiCad 10 derives nets from pads, so this only bites files that *have* a net
                    # table (kicad-8/9 dialects), where a missing entry is a corrupt file
                    add(Issue("error", "undeclared-net",
                              f"{fp.reference}.{pad.number} references net '{pad.net}' which the "
                              "board's net table does not declare", fp.reference,
                              "write the net into the table (pcb_writer does this for the "
                              "kicad-8/9 dialects) or fix the pad's net name"))
                if not pad.layers:
                    add(Issue("warning", "pad-layers-empty",
                              f"{fp.reference}.{pad.number or '?'} has no (layers …) at all",
                              fp.reference,
                              "KiCad defaults this to every copper layer; say what you mean, or the "
                              "clearance checks have nothing to work with"))
                if not pad.drill and pad.type in ("thru_hole", "connect", "np_thru_hole",
                                                  "np_thru"):
                    add(Issue("error", "tht-no-drill",
                              f"{fp.reference}.{pad.number} is {pad.type} but has no drill",
                              fp.reference, "a hole with no drill is a solid copper disc: nothing "
                                             "goes through the board here"))
                if pad.type not in ("smd", "thru_hole", "np_thru_hole", "connect", "np_thru",
                                    "identical", "nestellated"):
                    add(Issue("error", "pad-type",
                              f"{fp.reference}.{pad.number or '?'} has pad type {pad.type!r}, which "
                              "KiCad does not accept (smd | thru_hole | connect | np_thru)",
                              fp.reference, "KiCad's parser rejects unknown pad types — the whole "
                              "footprint would fail to load"))
                if not pad.is_tht and pad.drill and "np_thru" not in pad.type:
                    add(Issue("warning", "smd-drill",
                              f"{fp.reference}.{pad.number or '?'} is '{pad.type}' but has a drill",
                              fp.reference))
                ring = (min(pad.size) - (pad.drill if isinstance(pad.drill, (int, float))
                                         else max(pad.drill or (0, 0)))) / 2
                if pad.is_tht and pad.drill and ring < self.rules.annular_ring_min:
                    add(Issue("warning", "annular-ring",
                              f"{fp.reference}.{pad.number} annular ring {ring:.3f}mm < "
                              f"{self.rules.annular_ring_min}mm", fp.reference,
                              "grow the pad or shrink the drill"))
            if self.layer_missing(fp.layer):
                add(Issue("error", "unknown-layer", f"{fp.reference} sits on '{fp.layer}' which is "
                          "not in the layer stack", fp.reference))
            if outline:
                self._check_inside(outline, fp, add)

        for ref, count in refs.items():
            if count > 1:
                add(Issue("error", "duplicate-reference", f"reference '{ref or '(empty)'}' used by "
                          f"{count} footprints", ref, "re-annotate: every refdes must be unique"))
            if not ref:
                add(Issue("warning", "missing-reference", "a footprint has no Reference property"))

        # nets that connect nothing, or only one pad
        by_net: Dict[str, List[Tuple[Footprint, Pad]]] = {}
        for fp, pad in self.all_pads():
            if pad.net:
                by_net.setdefault(pad.net, []).append((fp, pad))
        for name, members in sorted(by_net.items()):
            if len(members) == 1:
                add(Issue("warning", "single-pad-net",
                          f"net '{name}' has exactly one pad ({members[0][0].reference}."
                          f"{members[0][1].number})", name,
                          "either it needs a second connection or it is a no-connect"))
        for name in self.net_names():
            if name and name not in by_net and not any(z.net == name for z in self.zones):
                add(Issue("warning", "empty-net", f"net '{name}' is declared but no pad connects "
                          "to it", name))

        out += self._check_tracks(by_net)
        out += self._check_zones(outline)
        out += self._check_courtyards()
        return out

    def layer_missing(self, name: str) -> bool:
        known = set(self.copper) | {s.name for s in self.extra_layers} | {
            "F.Cu", "B.Cu", "Edge.Cuts", "F.Mask", "B.Mask", "F.SilkS", "B.SilkS", "F.Fab",
            "B.Fab", "F.CrtYd", "B.CrtYd", "F.Paste", "B.Paste", "Dwgs.User", "Cmts.User",
            "Eco1.User", "Eco2.User", "Margin"}
        return name not in known

    def _check_outline(self) -> List[Issue]:
        out: List[Issue] = []
        if not self.edge:
            return [Issue("error", "no-outline", "board has no Edge.Cuts geometry",
                          hint="KiCad refuses to plot/fabricate without a closed outline")]
        segs = [(g.start, g.end) for g in self.edge if g.start and g.end]
        endpoints: Dict[XY, int] = {}
        for a, b in segs:
            for p in (a, b):
                key = (round(p[0], 4), round(p[1], 4))
                endpoints[key] = endpoints.get(key, 0) + 1
        open_ends = [p for p, n in endpoints.items() if n == 1]
        if open_ends:
            out.append(Issue("error", "outline-open",
                             f"Edge.Cuts has {len(open_ends)} dangling endpoint(s), e.g. "
                             f"{open_ends[0]}", hint="every segment end must meet another segment "
                             "start exactly once"))
        return out

    def _outline_polygons(self) -> List[List[XY]]:
        """Chain the Edge.Cuts segments into closed rings (best effort)."""
        segs = [[g.start, g.end] for g in self.edge if g.start and g.end]
        if not segs:
            return []
        key = lambda p: (round(p[0], 4), round(p[1], 4))
        rings: List[List[XY]] = []
        used = [False] * len(segs)
        for i, seg in enumerate(segs):
            if used[i]:
                continue
            ring = [seg[0], seg[1]]
            used[i] = True
            for _ in range(len(segs)):
                if key(ring[-1]) == key(ring[0]):
                    break
                nxt = next((j for j, s in enumerate(segs) if not used[j]
                            and key(s[0]) == key(ring[-1])), None)
                if nxt is None:
                    nxt = next((j for j, s in enumerate(segs) if not used[j]
                                and key(s[1]) == key(ring[-1])), None)
                    if nxt is None:
                        break
                    ring.append(segs[nxt][0])
                else:
                    ring.append(segs[nxt][1])
                used[nxt] = True
            if key(ring[-1]) == key(ring[0]) and len(ring) > 3:
                rings.append(ring[:-1])
        return rings

    def outline_rings(self) -> List[List[XY]]:
        """Edge.Cuts chains closed into rings (the polygon(s) the board is cut along)."""
        return self._outline_polygons()

    @staticmethod
    def _point_in_poly(p: XY, poly: Sequence[XY]) -> bool:
        x, y = p
        inside = False
        j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _check_inside(self, rings: List[List[XY]], fp: Footprint, add) -> None:
        for pad in fp.pads:
            cx, cy, _ = pad.absolute(fp)
            for r in rings:
                if not self._point_in_poly((cx, cy), r):
                    add(Issue("error", "pad-outside-outline",
                              f"{fp.reference}.{pad.number} at ({cx:.2f}, {cy:.2f}) is outside the "
                              "board outline", fp.reference, "move the part or grow the outline"))
                    break

    def _check_tracks(self, by_net: Dict[str, List[Tuple[Footprint, Pad]]]) -> List[Issue]:
        """Track/via sanity + the clearance matrix KiCad would apply (edge to edge)."""
        out: List[Issue] = []
        add = out.append
        rules = self.rules
        for i, t in enumerate(self.tracks):
            need_w = max(rules.clearance_for(t.net).track_width, rules.min_track_width)
            if t.width + 1e-9 < need_w:
                add(Issue("error", "track-too-thin",
                          f"track #{i} on '{t.net}' is {t.width:.3f}mm wide; the rule for that net "
                          f"class is {need_w:.3f}mm", t.net,
                          "widen the track (or lower the rule) — power nets are where an agent "
                          "most often leaves 0.2 mm traces"))
            if not is_copper(t.layer):
                add(Issue("error", "track-on-noncopper", f"track #{i} lives on '{t.layer}'", t.net,
                          "tracks must be on a copper layer"))
            elif t.layer not in self.copper:
                add(Issue("error", "track-layer-missing",
                          f"track #{i} uses '{t.layer}' which is not in this stackup", t.net))
            if t.net and t.net not in by_net:
                add(Issue("warning", "track-without-pads",
                          f"track #{i} belongs to net '{t.net}' which has no pads", t.net))
            if t.start == t.end:
                add(Issue("error", "zero-length-track", f"track #{i} start == end", t.net))
        for j, v in enumerate(self.vias):
            if v.drill + 1e-9 < rules.min_via_drill:
                add(Issue("error", "via-drill-too-small",
                          f"via #{j} at ({v.at[0]:.2f}, {v.at[1]:.2f}) drills {v.drill:.3f}mm < the "
                          f"{rules.min_via_drill:.3f}mm rule", v.net,
                          "check the fab house minimum before relaxing this"))
            ring = (v.size - v.drill) / 2
            if ring + 1e-9 < rules.annular_ring_min:
                add(Issue("error", "via-ring-too-thin",
                          f"via #{j} at ({v.at[0]:.2f}, {v.at[1]:.2f}) has a {ring:.3f}mm annular "
                          f"ring (< {rules.annular_ring_min:.3f}mm)", v.net,
                          "grow the via pad or shrink the drill"))
            span = set(v.layers)
            if span and "*" not in "".join(span) and span - set(self.copper):
                add(Issue("error", "via-layer-missing",
                          f"via #{j} spans {sorted(span)} which this stackup does not enable", v.net))

        # copper-to-copper clearance: track/track and track/pad, edge to edge
        copper_tracks = [t for t in self.tracks if is_copper(t.layer)]
        pad_boxes = [(fp, pad) for fp, pad in self.all_pads() if pad.net]
        if len(copper_tracks) <= MAX_PAIRWISE:
            for i in range(len(copper_tracks)):
                for j in range(i + 1, len(copper_tracks)):
                    a, b = copper_tracks[i], copper_tracks[j]
                    if a.layer != b.layer or a.net == b.net:
                        continue        # joined same-net copper is a corner, not a violation
                    centre = _seg_seg_distance(a.start, a.end, b.start, b.end)
                    gap = centre - (a.width + b.width) / 2.0      # KiCad measures copper edges
                    need = max(rules.min_clearance, rules.clearance_for(a.net).clearance,
                               rules.clearance_for(b.net).clearance)
                    if gap + 1e-9 < need:
                        add(Issue("error", "clearance",
                                  f"tracks {i} ('{a.net}') and {j} ('{b.net}') on {a.layer} are "
                                  f"{gap:.3f}mm apart edge-to-edge; rule is {need:.3f}mm"
                                  + (" — they touch, i.e. a short" if centre <= 1e-9 else ""),
                                  a.net, "move one, or route the second on another layer"))
        if len(copper_tracks) * max(1, len(pad_boxes)) <= MAX_PAIRWISE * 40:
            for i, t in enumerate(copper_tracks):
                for fp, pad in pad_boxes:
                    if pad.net == t.net or not layer_matches(t.layer, pad.layers,
                                                              fallback=tuple(self.copper)):
                        continue
                    gap = _seg_obb_gap(t.start, t.end, pad.rect(fp))
                    # KiCad's matrix takes the *larger* class clearance of the two objects, not the
                    # track's own — otherwise a 0.127 mm signal track could be parked on a power pad.
                    need = max(rules.min_clearance, rules.clearance_for(t.net).clearance,
                               rules.clearance_for(pad.net).clearance)
                    if gap + 1e-9 < need:
                        add(Issue("error", "pad-clearance",
                                  f"track #{i} ('{t.net}') passes {gap:.3f}mm from "
                                  f"{fp.reference}.{pad.number} ('{pad.net}'); rule is {need:.3f}mm",
                                  fp.reference, "clearance violation between a track and a pad of "
                                  "another net"))
        # pad-to-pad spacing across different nets
        pads = [(fp, p) for fp, p in self.all_pads() if p.net]
        if len(pads) <= MAX_PAIRWISE:
            for i in range(len(pads)):
                for j in range(i + 1, len(pads)):
                    f1, p1 = pads[i]
                    f2, p2 = pads[j]
                    if p1.net == p2.net:
                        continue
                    if not layers_touch(p1.layers, p2.layers, fallback=tuple(self.copper)):
                        continue        # different layers, and a via cannot join them here
                    gap = _obb_gap(p1.rect(f1), p2.rect(f2))
                    need = max(rules.min_clearance, rules.clearance_for(p1.net).clearance,
                               rules.clearance_for(p2.net).clearance)
                    if gap + 1e-9 >= need:
                        continue
                    if f1 is f2:
                        # KiCad's board DRC deliberately never compares two pads of the same
                        # footprint: fine-pitch parts are *supposed* to sit 0.05 mm apart in a row,
                        # and net-tie/jumper footprints connect pads on purpose. Judging that belongs
                        # to FootprintDef.check() (and `kicad-cli fp validate`), where reporting it
                        # is actionable instead of an 89-line wall of noise.
                        continue
                    add(Issue("error", "pad-clearance",
                              f"{f1.reference}.{p1.number} ('{p1.net}') and "
                              f"{f2.reference}.{p2.number} ('{p2.net}') are {gap:.3f}mm apart; "
                              f"rule is {need:.3f}mm", f1.reference,
                              "different nets must not touch — that is a short"))
        return out

    def _check_zones(self, rings: List[List[XY]]) -> List[Issue]:
        out: List[Issue] = []
        for i, z in enumerate(self.zones):
            if not is_copper(z.layer):
                out.append(Issue("error", "zone-layer", f"zone #{i} ('{z.net}') is on '{z.layer}'; "
                                 "zones must live on copper", z.net))
            if is_copper(z.layer) and z.layer not in self.copper:
                out.append(Issue("error", "zone-layer-missing", f"zone #{i} uses '{z.layer}' which "
                                 "is not enabled in this stackup (this board pours on "
                                 f"{', '.join(self.copper)})", z.net,
                                 "add the layer to the stackup or move the pour"))
            if len(z.outline) < 3:
                out.append(Issue("error", "zone-outline", f"zone #{i} needs >= 3 outline points",
                                 z.net))
            if z.net and z.net not in self.net_names():
                out.append(Issue("error", "zone-net-undeclared", f"zone #{i} belongs to unknown net "
                                 f"'{z.net}'", z.net))
            if z.min_thickness < 0.05:
                out.append(Issue("warning", "zone-thickness", f"zone #{i} min_thickness "
                                 f"{z.min_thickness}mm is below most fabs' 0.05mm sliver limit",
                                 z.net))
            if z.fill and rings:
                inside = [self._point_in_poly(p, rings[0]) for p in z.outline]
                if not any(inside):
                    out.append(Issue("warning", "zone-outside", f"zone #{i} lies entirely outside "
                                     "the board outline", z.net))
                elif not all(inside):
                    out.append(Issue("warning", "zone-crosses-edge", f"zone #{i} crosses the board "
                                     "edge; KiCad will clip it", z.net))
        return out

    def _check_courtyards(self) -> List[Issue]:
        out: List[Issue] = []
        boxes = [(fp, fp.courtyard()) for fp in self.footprints]
        boxes = [(fp, b) for fp, b in boxes if b]
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                (f1, b1), (f2, b2) = boxes[i], boxes[j]
                if f1.layer[0] != f2.layer[0]:
                    continue      # front vs back courtyards never collide
                gap = _rect_gap(b1, b2)
                if gap + 1e-9 < self.rules.min_courtyard_clearance:
                    out.append(Issue("warning", "courtyard-overlap",
                                     f"{f1.reference} and {f2.reference} courtyards overlap by "
                                     f"{-gap:.3f}mm", f1.reference,
                                     "assembly/3D clearance; KiCad flags it as a courtyard violation"))
        return out

    # ── report helpers ───────────────────────────────────────────────────────
    def to_json(self) -> Dict[str, Any]:
        """Whole board as JSON-ready data (the agent's structured-diff format).

        Lossless for everything :mod:`pcbai.kicad` models; the writer, not this dump, is the format
        reference — round-tripping a *KiCad-authored* board through here drops whatever we do not
        model, which is exactly what :func:`pcbai.kicad.pcb_writer.lossiness` reports.
        """
        data = dataclasses.asdict(self)
        if data.get("source") is not None:
            data["source"] = str(data["source"])      # a Path is not JSON-dumpable
        return data

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Board":
        """Inverse of :meth:`to_json`. Unknown keys are ignored, so a hand-written dict works too."""
        def build(kind, raw):
            names = {f.name for f in dataclasses.fields(kind)}
            return kind(**{k: v for k, v in raw.items() if k in names})

        board = cls(**{k: v for k, v in data.items()
                       if k in {f.name for f in dataclasses.fields(cls)}
                       and k not in ("footprints", "nets", "zones", "tracks", "vias", "graphics",
                                     "edge", "stackup", "rules", "extra_layers")})
        board.footprints = [build(Footprint, fp_raw) for fp_raw in data.get("footprints", [])]
        for fp_raw, fp in zip(data.get("footprints", []), board.footprints):
            fp.pads = [build(Pad, p) for p in fp_raw.get("pads", [])]
            fp.graphics = [build(Graphic, g) for g in fp_raw.get("graphics", [])]
        board.nets = [build(Net, n) if isinstance(n, dict) else Net(str(n))
                      for n in data.get("nets", [])]
        board.tracks = [build(Track, t) for t in data.get("tracks", [])]
        board.vias = [build(Via, v) for v in data.get("vias", [])]
        board.zones = [build(Zone, z) for z in data.get("zones", [])]
        board.graphics = [build(Graphic, g) for g in data.get("graphics", [])]
        board.edge = [build(Graphic, g) for g in data.get("edge", [])]
        board.stackup = [build(StackLayer, s_raw) for s_raw in data.get("stackup", [])]
        rules = DesignRules()
        rules_raw = data.get("rules") or {}
        for key, value in rules_raw.items():
            if key == "net_classes":
                rules.net_classes = {str(n): build(NetClass, dict(c, name=str(n)))
                                     for n, c in value.items()}
            elif hasattr(rules, key):
                setattr(rules, key, value)
        board.rules = rules
        board.__post_init__()
        return board

    def report(self) -> Dict[str, object]:
        issues = self.checks()
        return {
            "ok": not any(i.severity == "error" for i in issues),
            "summary": self.summary(),
            "issues": [i.to_dict() for i in issues],
            "counts": {
                "error": sum(1 for i in issues if i.severity == "error"),
                "warning": sum(1 for i in issues if i.severity == "warning"),
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# tiny geometry
# ─────────────────────────────────────────────────────────────────────────────
def _pt_seg_gap(p: XY, a: XY, b: XY) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def _seg_seg_distance(a1: XY, a2: XY, b1: XY, b2: XY) -> float:
    """Exact distance between two segments; 0.0 when they touch or cross."""
    if _segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(_pt_seg_gap(a1, b1, b2), _pt_seg_gap(a2, b1, b2),
               _pt_seg_gap(b1, a1, a2), _pt_seg_gap(b2, a1, a2))


def _orient(p: XY, q: XY, r: XY) -> float:
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _on_segment(p: XY, q: XY, r: XY) -> bool:
    return (min(p[0], q[0]) - 1e-9 <= r[0] <= max(p[0], q[0]) + 1e-9
            and min(p[1], q[1]) - 1e-9 <= r[1] <= max(p[1], q[1]) + 1e-9)


def _segments_intersect(a1: XY, a2: XY, b1: XY, b2: XY) -> bool:
    d1, d2 = _orient(b1, b2, a1), _orient(b1, b2, a2)
    d3, d4 = _orient(a1, a2, b1), _orient(a1, a2, b2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    if abs(d1) < 1e-12 and _on_segment(b1, b2, a1):
        return True
    if abs(d2) < 1e-12 and _on_segment(b1, b2, a2):
        return True
    if abs(d3) < 1e-12 and _on_segment(a1, a2, b1):
        return True
    if abs(d4) < 1e-12 and _on_segment(a1, a2, b2):
        return True
    return False


def _obb_corners(rect: Sequence[float]) -> List[XY]:
    """Corners of a rotated rectangle ``(cx, cy, w, h, rot_deg)``."""
    cx, cy, w, h, rot = rect
    rad = math.radians(rot)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    out: List[XY] = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        out.append((cx + dx * cos_r - dy * sin_r, cy + dx * sin_r + dy * cos_r))
    return out


def _polygons_intersect(a: Sequence[XY], b: Sequence[XY]) -> bool:
    """Separating-axis test for two convex polygons (their edges only: enough for rectangles)."""
    for poly in (a, b):
        for i in range(len(poly)):
            ax1, ay1 = poly[i]
            ax2, ay2 = poly[(i + 1) % len(poly)]
            nx, ny = -(ay2 - ay1), (ax2 - ax1)
            norm = math.hypot(nx, ny) or 1.0
            nx, ny = nx / norm, ny / norm
            pa = [px * nx + py * ny for px, py in a]
            pb = [px * nx + py * ny for px, py in b]
            if max(pa) < min(pb) - 1e-9 or max(pb) < min(pa) - 1e-9:
                return False
    return True


def _obb_gap(a: Sequence[float], b: Sequence[float]) -> float:
    """Signed edge-to-edge distance between two rotated rectangles.

    Positive = the clearance between them; negative = how far they overlap (approximated as the
    smallest push along the four face normals that would separate them). Axis-aligned bounding boxes
    are *not* used, because a part rotated 45° has a fat AABB and the whole board would then read as
    a pile of shorts — which is how a generator ends up arguing with its own checker.
    """
    ca, cb = _obb_corners(a), _obb_corners(b)
    if _polygons_intersect(ca, cb):
        deepest = math.inf
        for poly in (ca, cb):
            for i in range(len(poly)):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % len(poly)]
                nx, ny = -(y2 - y1), (x2 - x1)
                norm = math.hypot(nx, ny) or 1.0
                nx, ny = nx / norm, ny / norm
                pa = [px * nx + py * ny for px, py in ca]
                pb = [px * nx + py * ny for px, py in cb]
                deepest = min(deepest, max(0.0, min(max(pa) - min(pb), max(pb) - min(pa))))
        return 0.0 if deepest == math.inf else -deepest
    best = math.inf
    for poly, other in ((ca, cb), (cb, ca)):
        for i in range(4):
            ax1, ay1 = poly[i]
            ax2, ay2 = poly[(i + 1) % 4]
            for k in range(4):
                bx1, by1 = other[k]
                bx2, by2 = other[(k + 1) % 4]
                best = min(best, _seg_seg_distance((ax1, ay1), (ax2, ay2), (bx1, by1), (bx2, by2)))
    return best


def _rect_to_obb(box: Sequence[float]) -> List[float]:
    x0, y0, x1, y1 = box
    return [(x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0, 0.0]


def _seg_obb_gap(a: XY, b: XY, rect: Sequence[float], *, steps: int = 24) -> float:
    """Distance from a segment to a rotated rectangle (exact enough for clearance work)."""
    corners = _obb_corners(rect)
    if _segment_in_rect_poly(a, b, corners):
        return 0.0
    best = math.inf
    for i in range(4):
        best = min(best, _seg_seg_distance(a, b, corners[i], corners[(i + 1) % 4]))
    return best


def _segment_in_rect_poly(a: XY, b: XY, poly: Sequence[XY]) -> bool:
    """True when the segment touches or crosses the polygon."""
    if any(_point_in_polygon(p, poly) for p in (a, b)):
        return True
    for i in range(len(poly)):
        if _segments_intersect(a, b, poly[i], poly[(i + 1) % len(poly)]):
            return True
    return False


def _point_in_polygon(p: XY, poly: Sequence[XY]) -> bool:
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _seg_rect_gap(a1: XY, a2: XY, box: Sequence[float]) -> float:
    """Exact distance from a segment to an axis-aligned box; 0.0 when they touch or overlap.

    The shortest link is usually *not* at either endpoint — a track passing beside a pad is closest
    somewhere in the middle — so the answer is taken over all four edges. An earlier version measured
    from the endpoints only, which overestimated the gap and let the router emit copper that its own
    checker then flagged: the generator and the DRC have to agree on the geometry, in both directions.
    """
    x0, y0, x1, y1 = box
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if _point_in_box(a1, box) or _point_in_box(a2, box):
        return 0.0
    for i in range(4):
        if _segments_intersect(a1, a2, corners[i], corners[(i + 1) % 4]):
            return 0.0
    return min(_seg_seg_distance(a1, a2, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _pt_obb_gap(p: XY, rect: Sequence[float]) -> float:
    """Distance from a point to a rotated rectangle ``(cx, cy, w, h, rot)``, 0.0 inside it."""
    corners = _obb_corners(rect)
    if _point_in_polygon(p, corners):
        return 0.0
    return min(_seg_seg_distance(p, p, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _point_in_box(p: XY, box: Sequence[float]) -> bool:
    return box[0] - 1e-9 <= p[0] <= box[2] + 1e-9 and box[1] - 1e-9 <= p[1] <= box[3] + 1e-9


def _pt_box_gap(p: XY, box: Sequence[float]) -> float:
    dx = max(box[0] - p[0], p[0] - box[2], 0.0)
    dy = max(box[1] - p[1], p[1] - box[3], 0.0)
    return math.hypot(dx, dy)


def _rect_gap(r1: Sequence[float], r2: Sequence[float]) -> float:
    """Gap between two axis-aligned boxes; negative when they overlap."""
    dx = max(r2[0] - r1[2], r1[0] - r2[2])
    dy = max(r2[1] - r1[3], r1[1] - r2[3])
    if dx <= 0 and dy <= 0:
        return -min(-dx, -dy)
    return math.hypot(max(dx, 0.0), max(dy, 0.0))
