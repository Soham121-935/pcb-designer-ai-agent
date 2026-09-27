"""KiCad 10 canonical layer names, ordinals and roles.

The ordinals below are the ``PCB_LAYER_ID`` values that KiCad writes as the first token of each
``(layers (ORDINAL "name" type)…)`` entry. They were read straight out of KiCad's ``layer_ids.h``
(``master`` @ 2026-09, the enum KiCad keeps frozen "to ensure compatibility with legacy board
files") and cross-checked against ``pcbai/steps/template_project/board.kicad_pcb``, a real KiCad 10
board (``F.Cu``=0, ``B.Cu``=2, ``F.Mask``=1, ``F.SilkS``=5, ``Edge.Cuts``=25, ``F.Fab``=35,
``User.1``=39 — all match).  Note that KiCad 9 renumbered the enum: ``B.Cu`` is **2**, and inner
copper layers sit at ``In1.Cu``=4, ``In2.Cu``=6 … (``InN.Cu`` = 2·(N+1)), so a 4-layer board is
``0, 4, 6, 2`` — not the old ``0, 1, 2, 31``.

Why this module exists: every writer/reader in this package needs the same table, and a wrong
ordinal is one of those bugs that KiCad "fixes silently" by renumbering on load, which makes a
generated board differ from the source in ways nobody notices until the stackup is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["LAYER_ORDINALS", "LAYER_TYPES", "FRONT_BACK_PAIRS", "ordinal_for", "canonical_name", "copper_layers", "front_of", "back_of", "is_copper", "LayerSpec", "layer_matches", "layers_touch", "expand_layers"]

#: canonical name -> KiCad 10 layer ordinal (``PCB_LAYER_ID``)
LAYER_ORDINALS: Dict[str, int] = {
    "F.Cu": 0,
    "F.Mask": 1,
    "B.Cu": 2,
    "B.Mask": 3,
    "F.SilkS": 5,
    "B.SilkS": 7,
    "F.Adhes": 9,
    "B.Adhes": 11,
    "F.Paste": 13,
    "B.Paste": 15,
    "Dwgs.User": 17,
    "Cmts.User": 19,
    "Eco1.User": 21,
    "Eco2.User": 23,
    "Edge.Cuts": 25,
    "Margin": 27,
    "B.CrtYd": 29,
    "F.CrtYd": 31,
    "B.Fab": 33,
    "F.Fab": 35,
    "Rescue": 37,
    **{f"User.{n}": 37 + 2 * n for n in range(1, 46)},
    **{f"In{i}.Cu": 2 * (i + 1) for i in range(1, 31)},
}

#: canonical name -> the ``(layers (ord name TYPE))`` type token
LAYER_TYPES: Dict[str, str] = {
    "F.Cu": "signal", "B.Cu": "signal", **{f"In{i}.Cu": "power" for i in range(1, 31)},
    "F.Mask": "user", "B.Mask": "user", "F.SilkS": "user", "B.SilkS": "user",
    "F.Adhes": "user", "B.Adhes": "user", "F.Paste": "user", "B.Paste": "user",
    "Dwgs.User": "user", "Cmts.User": "user", "Eco1.User": "user", "Eco2.User": "user",
    "Edge.Cuts": "user", "Margin": "user", "F.CrtYd": "user", "B.CrtYd": "user",
    "F.Fab": "user", "B.Fab": "user",
    **{f"User.{n}": "user" for n in range(1, 46)},
}

#: human names KiCad shows in the layer manager (optional 4th token in ``(layers …)``)
LAYER_USER_NAMES: Dict[str, str] = {
    "F.Adhes": "F.Adhesive", "B.Adhes": "B.Adhesive", "F.SilkS": "F.Silkscreen",
    "B.SilkS": "B.Silkscreen", "Dwgs.User": "User.Drawings", "Cmts.User": "User.Comments",
    "Eco1.User": "User.Eco1", "Eco2.User": "User.Eco2", "F.CrtYd": "F.Courtyard",
    "B.CrtYd": "B.Courtyard",
}

#: front/back twins, in the order KiCad lists technical layers
FRONT_BACK_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("F.Mask", "B.Mask"), ("F.SilkS", "B.SilkS"), ("F.Adhes", "B.Adhes"), ("F.Paste", "B.Paste"),
)

#: technical layers every board declares, in ``(layers …)`` order
TECHNICAL_LAYERS: Tuple[str, ...] = (
    "F.Adhes", "B.Adhes", "F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask",
    "Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User", "Edge.Cuts", "Margin", "F.CrtYd",
    "B.CrtYd", "F.Fab", "B.Fab", "User.1", "User.2", "User.3", "User.4",
)


def ordinal_for(name: str) -> int:
    try:
        return LAYER_ORDINALS[name]
    except KeyError:  # user-renamed layer: KiCad still needs a number, so keep it stable
        return abs(hash(name)) % 2048 * 2


def canonical_name(name: str) -> Optional[str]:
    return name if name in LAYER_ORDINALS else None


def front_of(name: str) -> Optional[str]:
    return f"F.{name.split('.', 1)[1]}" if name.startswith("B.") else None


def back_of(name: str) -> Optional[str]:
    return f"B.{name.split('.', 1)[1]}" if name.startswith("F.") else None


def is_copper(name: str) -> bool:
    return name.endswith(".Cu") and (name.startswith(("F.", "B.", "In")))


def copper_layers(n: int) -> List[str]:
    """Canonical copper layer names for an *n*-layer stack, top to bottom."""
    if n < 2 or n % 2:
        raise ValueError(f"KiCad needs an even copper count >= 2, got {n}")
    mid = [f"In{i}.Cu" for i in range(1, n - 1)]
    return ["F.Cu", *mid, "B.Cu"]


@dataclass(frozen=True)
class LayerSpec:
    """One ``(layers (ORDINAL "NAME" TYPE "USER_NAME"))`` entry."""

    name: str
    type: str = ""
    user_name: str = ""

    @property
    def ordinal(self) -> int:
        return ordinal_for(self.name)

    @classmethod
    def for_board(cls, copper: Sequence[str], *, with_technical: bool = True) -> List["LayerSpec"]:
        out = [cls(c, LAYER_TYPES.get(c, "signal")) for c in copper]
        if with_technical:
            out += [cls(t, "user", LAYER_USER_NAMES.get(t, "")) for t in TECHNICAL_LAYERS]
        return out

    @property
    def is_copper(self) -> bool:
        return is_copper(self.name)

    @property
    def is_outer(self) -> bool:
        return self.name in ("F.Cu", "B.Cu")


def layer_matches(layer: str, patterns: Sequence[str], *, fallback: Sequence[str] = ()) -> bool:
    """Is *layer* covered by a pad/via ``(layers ...)`` list?

    KiCad writes wildcards here — a through-hole pad is ``(layers "*.Cu" "*.Mask")``, not two explicit
    copper layers — and a mechanical drill may carry no list at all. Reading those as "on no layer"
    hides the copper from every clearance test, so an empty list falls back to *fallback* and ``*.X``
    matches any layer ending in ``.X``.
    """
    pats = tuple(patterns) or tuple(fallback)
    for pat in pats:
        if pat == layer:
            return True
        if pat.startswith("*") and layer.endswith(pat[1:]):
            return True
    return False


def layers_touch(a: Sequence[str], b: Sequence[str], *, fallback: Sequence[str] = ()) -> bool:
    """Do two ``(layers ...)`` lists cover at least one common layer? (wildcards respected)"""
    left = tuple(a) or tuple(fallback)
    return any(layer_matches(x, b, fallback=fallback) for x in left)


def expand_layers(patterns: Sequence[str], copper: Sequence[str]) -> Tuple[str, ...]:
    """Concrete copper layers a (possibly wildcarded) pad layer list refers to."""
    return tuple(name for name in copper if layer_matches(name, patterns, fallback=copper))

