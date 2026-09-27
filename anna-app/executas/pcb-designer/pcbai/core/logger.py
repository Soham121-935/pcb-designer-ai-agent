"""Logging for pcbai.

RULE: stdout is a protocol channel (the Anna plugin speaks JSON-RPC over it) and, for the CLI,
the user-facing payload. Therefore **all** diagnostics go to stderr — never print() to stdout
from library code.

`install_stderr_guard()` additionally redirects any leftover ``print(...)`` performed by
third-party modules (skidl, pdfminer, KiCad helpers) into stderr while the guard is installed,
so a single stray print can never corrupt a JSON-RPC stream.
"""
from __future__ import annotations

import io
import logging
import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional, TextIO

_NAME = "pcbai"


def _level_from_env() -> int:
    level_name = os.getenv("PCB_AI_LOG", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def get_logger(name: str = _NAME, level: Optional[int] = None) -> logging.Logger:
    """Return a stderr-backed logger. Level defaults to $PCB_AI_LOG."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(level if level is not None else _level_from_env())
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(fmt="[%(name)s] %(levelname)s | %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def log(msg: str) -> None:
    """Emit a plain diagnostic line on stderr (no JSON-RPC interference)."""
    sys.stderr.write(f"[pcb-designer] {msg}\n")
    sys.stderr.flush()


class _StderrPrintStream(io.TextIOBase):
    """A sys.stdout stand-in that forwards text to stderr (line buffered)."""

    def __init__(self, sink: TextIO) -> None:
        self._sink = sink
        self._buf = ""

    def writable(self) -> bool:  # pragma: no cover - trivial
        return True

    def write(self, s: str) -> int:  # noqa: D102
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._sink.write(line + "\n")
                self._sink.flush()
        return len(s)

    def flush(self) -> None:  # noqa: D102
        if self._buf.strip():
            self._sink.write(self._buf + "\n")
            self._sink.flush()
        self._buf = ""

    def isatty(self) -> bool:  # pragma: no cover - trivial
        return False


@contextmanager
def install_stderr_guard() -> Iterator[None]:
    """Route stray ``print()`` calls made while inside the block to stderr."""
    original = sys.stdout
    sys.stdout = _StderrPrintStream(sys.stderr)  # type: ignore[assignment]
    try:
        yield
    finally:
        try:
            sys.stdout.flush()
        except Exception:  # pragma: no cover - defensive
            pass
        sys.stdout = original


def file_logger(path: Optional[str] = None, level: Optional[int] = None) -> logging.Logger:
    """Logger that *also* appends to a file (used for RPC session logs). Missing file → stderr only."""
    logger = get_logger(_NAME, level)
    if not path:
        return logger
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        logger.addHandler(fh)
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning("cannot open log file %s: %s", path, exc)
    return logger
