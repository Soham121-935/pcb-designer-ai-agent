"""File-safety primitives: backups, atomic writes, transactional edits.

The agent must never corrupt a working PCB project because one AI action failed
(docs/AUDIT.md §7 of the task brief). Every mutating tool goes through :func:`safe_write`
or :class:`FileTransaction`.

Guarantees
----------
* **atomic replace** — content is written to a sibling temp file, fsynced, then ``os.replace``d.
  A crash mid-write leaves the previous file intact.
* **backup before overwrite** — the previous bytes are copied to ``<file>.bak`` plus an
  append-only ``.history/<timestamp>-<reason>`` copy, so history survives multiple edits.
* **rollback** :meth:`FileTransaction.commit` is not called → the original file is restored.
* **no clobber of an unparsable result** :func:`validate_after_write` lets the caller prove the
  file still parses before the transaction is considered good.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pcbai.core.logger import get_logger

logger = get_logger("pcbai.safety")

#: Files the agent is allowed to overwrite in place. Anything else needs `force=True`.
DEFAULT_ALLOWED_SUFFIXES = (
    ".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_prl", ".kicad_mod", ".kicad_sym",
    ".json", ".csv", ".txt", ".md", ".yaml", ".yml", ".net", ".xml",
)


class UnsafeWriteError(RuntimeError):
    """Raised when a write is refused (protected file, missing backup, invalid result)."""


@dataclass
class WriteResult:
    path: str
    changed: bool
    bytes_written: int = 0
    backup: Optional[str] = None
    history_copy: Optional[str] = None
    message: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "changed": self.changed,
            "bytes_written": self.bytes_written,
            "backup": self.backup,
            "history_copy": self.history_copy,
            "message": self.message,
        }


def _history_dir(target: Path) -> Path:
    return target.parent / ".history"


def atomic_write_bytes(target: Path, data: bytes, *, keep_history: bool = True,
                       reason: str = "write") -> WriteResult:
    """Atomically write *data* to *target*, backing up whatever was there."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    backup_path: Optional[str] = None
    hist_path: Optional[str] = None

    if existed:
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        backup_path = str(backup)
        if keep_history:
            hdir = _history_dir(target)
            hdir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            stamp_path = hdir / f"{stamp}-{reason}-{target.name}"
            n = 0
            while stamp_path.exists():
                n += 1
                stamp_path = hdir / f"{stamp}-{n}-{reason}-{target.name}"
            shutil.copy2(target, stamp_path)
            hist_path = str(stamp_path)

    tmp = target.parent / f".{target.name}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        finally:
            # restore the backup if we somehow clobbered
            if existed and backup_path and not target.exists():
                shutil.copy2(backup_path, target)
        raise
    return WriteResult(
        path=str(target),
        changed=(not existed) or (target.stat().st_size != len(data)),
        bytes_written=len(data),
        backup=backup_path,
        history_copy=hist_path,
        message="created" if not existed else "updated",
    )


def atomic_write_text(target, text: str, *, reason: str = "write", **kw) -> WriteResult:
    return atomic_write_bytes(Path(target), text.encode("utf-8"), reason=reason, **kw)


def check_writable(target, *, allowed_suffixes=DEFAULT_ALLOWED_SUFFIXES, force: bool = False) -> Path:
    """Guard-rail before any mutating operation. Raises UnsafeWriteError on refusal."""
    p = Path(target)
    if p.is_dir():
        raise UnsafeWriteError(f"refusing to write to a directory: {p}")
    suffix = p.suffix.lower()
    if not force and allowed_suffixes and suffix not in allowed_suffixes:
        raise UnsafeWriteError(
            f"refusing to modify '{p.name}': extension {suffix or '(none)'} is not in the "
            f"allowed set {list(allowed_suffixes)}. Pass force=True only if you are sure."
        )
    # Never let us write our own backup/history files as project files.
    if p.name.endswith(".bak") or ".history" in p.parts:
        raise UnsafeWriteError(f"refusing to write into backup/history area: {p}")
    return p


def validate_after_write(path, validator: Optional[Callable[[str], None]]) -> Optional[str]:
    """Run *validator* against the freshly written file. Returns None if OK, else the error text."""
    if validator is None:
        return None
    try:
        validator(str(path))
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


@dataclass
class FileTransaction:
    """Backup → write → validate → (commit | rollback).

    Usage::

        tx = FileTransaction(project_dir, reason="move-C12")
        try:
            tx.write("board.kicad_pcb", new_text, validator=loadable_pcb)
        finally:
            tx.finish()          # rolls back anything left uncommitted

    A transaction only becomes durable when :meth:`commit` succeeds for every file it touched.
    """

    root: str
    reason: str = "edit"
    validator: Optional[Callable[[str], None]] = None
    results: List[WriteResult] = field(default_factory=list)
    _opened: Dict[str, Optional[bytes]] = field(default_factory=dict, repr=False)
    _committed: bool = False

    def __post_init__(self) -> None:
        self.root = str(Path(self.root).resolve())
        if not os.path.isdir(self.root):
            raise UnsafeWriteError(f"transaction root does not exist: {self.root}")

    # ── bookkeeping ───────────────────────────────────────────────────────────
    def _abs(self, rel: str) -> Path:
        p = (Path(self.root) / rel).resolve()
        if not str(p).startswith(self.root + os.sep) and p != Path(self.root):
            raise UnsafeWriteError(f"path escapes project root: {rel}")
        return p

    def snapshot(self, rel: str) -> None:
        """Remember current bytes so they can be restored on rollback."""
        p = self._abs(rel)
        if rel not in self._opened:
            self._opened[rel] = p.read_bytes() if p.exists() else None

    # ── operations ────────────────────────────────────────────────────────────
    def write(self, rel: str, text: str, *, validator=None, force: bool = False) -> WriteResult:
        self.snapshot(rel)
        target = check_writable(self._abs(rel), force=force)
        res = atomic_write_bytes(target, text.encode("utf-8"), reason=self.reason)
        err = validate_after_write(target, validator or self.validator)
        if err:
            self.rollback_one(rel)
            raise UnsafeWriteError(f"post-write validation failed for {rel}; rolled back. {err}")
        self.results.append(res)
        return res

    def rollback_one(self, rel: str) -> bool:
        original = self._opened.get(rel, None)
        target = self._abs(rel)
        if original is None:
            if target.exists():
                target.unlink()
            return True
        target.write_bytes(original)
        return True

    def rollback(self) -> List[str]:
        """Restore every touched file. Safe to call more than once."""
        restored = []
        for rel in list(self._opened):
            try:
                self.rollback_one(rel)
                restored.append(rel)
            except Exception as exc:  # pragma: no cover - disk full etc.
                logger.error("rollback of %s failed: %s", rel, exc)
        self.results = []
        return restored

    def commit(self) -> Dict[str, object]:
        """Mark the transaction durable (files are already atomic; this returns the audit record)."""
        self._committed = True
        return {
            "reason": self.reason,
            "files": [r.to_dict() for r in self.results],
            "rolled_back": False,
        }

    def finish(self) -> None:
        """Call in a finally block: rolls back if commit() never ran."""
        if not self._committed:
            restored = self.rollback()
            if restored:
                logger.warning("transaction '%s' rolled back: %s", self.reason, ", ".join(restored))
