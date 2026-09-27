"""EDA backend detection (single source of truth for "what can we actually do here?").

Design decisions (docs/AUDIT.md §11 / Q1):
  * The pure-Python S-expression path is the PRIMARY substrate — it must never hard-depend on KiCad.
  * ``pcbnew`` + ``kicad-cli`` are an OPTIONAL *parity/validation accelerator*. When they are absent the
    system must say so explicitly ("skipped", backend="kicad-missing") — it must NEVER report a silent pass.

Nothing in this module raises: probing must be safe to call from anywhere, including during import.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from typing import Dict, List, Optional

from pcbai.core.logger import get_logger

logger = get_logger("pcbai.eda")

#: Where distro packages put the KiCad python bindings (checked in order).
_LIB_SEARCH_CANDIDATES: List[str] = [
    "/usr/lib/kicad/lib/python3/dist-packages",
    "/usr/lib/x86_64-linux-gnu/kicad/lib/python3/dist-packages",
    "/usr/lib/kicad-python3/dist-packages",
    "/usr/local/lib/kicad/lib/python3/dist-packages",
    "/Applications/Kicad/lib/python3/dist-packages",
]

#: Where KiCad installs its footprint libraries.
_FOOTPRINT_ROOT_CANDIDATES: List[str] = [
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    "/Applications/Kicad/share/kicad/footprints",
]

_SYMBOL_ROOT_CANDIDATES: List[str] = [
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
]


def _env_paths(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [p for p in raw.split(os.pathsep) if p.strip()]


@lru_cache(maxsize=1)
def lib_search_paths() -> List[str]:
    """Candidate dirs to prepend to sys.path so ``import pcbnew`` works."""
    return _env_paths("PCB_AI_KICAD_LIB_PATH") + _LIB_SEARCH_CANDIDATES


def register_lib_paths() -> List[str]:
    """Add existing KiCad binding dirs to sys.path. Returns the paths actually added."""
    added: List[str] = []
    for path in lib_search_paths():
        if not os.path.isdir(path) or path in sys.path:
            continue
        sys.path.insert(0, path)
        added.append(path)
    return added


def _import_pcbnew():
    """Import pcbnew, trying KiCad's binding dirs first. Returns module or None."""
    if "pcbnew" in sys.modules:
        return sys.modules["pcbnew"]
    for path in lib_search_paths():
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        return importlib.util.find_spec("pcbnew") and __import__("pcbnew")
    except Exception as exc:  # pragma: no cover - depends on host KiCad build
        logger.debug("pcbnew import failed: %s", exc)
        return None


def has_pcbnew() -> bool:
    """True when the KiCad python bindings are importable in this process."""
    return _import_pcbnew() is not None


def pcbnew():
    """Return the pcbnew module, or raise RuntimeError with an actionable message."""
    mod = _import_pcbnew()
    if mod is None:
        raise RuntimeError(
            "KiCad python bindings (pcbnew) are not available. Install KiCad >= 8 "
            "(Debian/Ubuntu: `apt install kicad python3-kicad`) or point PCB_AI_KICAD_LIB_PATH "
            "at the directory containing pcbnew. Pure-Python inspection/modification works without it; "
            "this feature (KiCad parity validation, native board authoring) does not."
        )
    return mod


def kicad_cli() -> Optional[str]:
    """Absolute path to kicad-cli, or None."""
    override = os.getenv("PCB_AI_KICAD_CLI") or os.getenv("KICAD_CLI")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return shutil.which(override)
    return shutil.which("kicad-cli")


def has_kicad_cli() -> bool:
    return kicad_cli() is not None


def kicad_version() -> Optional[str]:
    """`kicad-cli --version` output, or None."""
    cli = kicad_cli()
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip() or None
    except Exception as exc:  # pragma: no cover - host dependent
        logger.debug("kicad-cli --version failed: %s", exc)
        return None


def footprint_lib_root() -> Optional[str]:
    """Directory holding *.pretty footprint libraries, or None."""
    for cand in _env_paths("PCB_AI_KICAD_FOOTPRINT_DIR") + _FOOTPRINT_ROOT_CANDIDATES:
        if os.path.isdir(cand):
            return cand
    return None


def symbol_lib_root() -> Optional[str]:
    for cand in _env_paths("PCB_AI_KICAD_SYMBOL_DIR") + _SYMBOL_ROOT_CANDIDATES:
        if os.path.isdir(cand):
            return cand
    return None


def freerouting_jar() -> Optional[str]:
    """Path to a Freerouting jar, if the user has one (used for DSN/SES routing)."""
    for cand in _env_paths("PCB_AI_FREEROUTING_JAR") + [
        os.path.expanduser("~/bin/freerouting.jar"),
        "/usr/local/share/freerouting/freerouting.jar",
    ]:
        if os.path.isfile(cand):
            return cand
    return None


@dataclass
class EdaCapabilities:
    """What this machine can do for us. Serialized into tool output so reports are honest."""

    pcbnew: bool = False
    kicad_cli: Optional[str] = None
    kicad_version: Optional[str] = None
    footprint_libs: Optional[str] = None
    symbol_libs: Optional[str] = None
    freerouting: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def can_validate_with_kicad(self) -> bool:
        return self.kicad_cli is not None

    @property
    def can_author_native(self) -> bool:
        return bool(self.pcbnew and self.footprint_libs)

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["can_validate_with_kicad"] = self.can_validate_with_kicad
        d["can_author_native"] = self.can_author_native
        return d


def capabilities(refresh: bool = False) -> EdaCapabilities:
    """Detect capabilities (cached per process)."""
    global _CAPS
    if _CAPS is not None and not refresh:
        return _CAPS
    caps = EdaCapabilities(
        pcbnew=has_pcbnew(),
        kicad_cli=kicad_cli(),
        kicad_version=kicad_version(),
        footprint_libs=footprint_lib_root(),
        symbol_libs=symbol_lib_root(),
        freerouting=freerouting_jar(),
    )
    if not caps.pcbnew:
        caps.notes.append("pcbnew unavailable: KiCad-native authoring/parity checks skipped")
    if not caps.kicad_cli:
        caps.notes.append("kicad-cli unavailable: DRC/ERC/Gerber parity checks skipped (reported as 'skipped', not 'passed')")
    if not caps.footprint_libs:
        caps.notes.append("KiCad footprint libraries not found: only locally generated .kicad_mod footprints usable")
    _CAPS = caps
    return caps


_CAPS: Optional[EdaCapabilities] = None
