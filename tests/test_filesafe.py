"""File-safety layer: atomic writes, backups, rollback, protected paths (brief §7/§9)."""
from __future__ import annotations

from pathlib import Path

import pytest

from pcbai.core.filesafe import (FileTransaction, UnsafeWriteError, atomic_write_text, check_writable)


def test_atomic_write_creates_backup_of_previous_content(tmp_path: Path):
    target = tmp_path / "board.kicad_pcb"
    target.write_text("(kicad_pcb original)", encoding="utf-8")
    res = atomic_write_text(target, "(kicad_pcb new)")
    assert target.read_text(encoding="utf-8") == "(kicad_pcb new)"
    assert res.backup and Path(res.backup).read_text(encoding="utf-8") == "(kicad_pcb original)"
    assert res.history_copy and Path(res.history_copy).exists()


def test_repeated_writes_keep_history(tmp_path: Path):
    target = tmp_path / "b.kicad_pcb"
    atomic_write_text(target, "v0")          # creation: nothing to back up
    for i in range(1, 4):
        atomic_write_text(target, f"v{i}", reason=f"edit{i}")
    hist = sorted(p.name for p in (tmp_path / ".history").glob("*"))
    assert len(hist) == 3, "every overwrite of an existing file must leave an independent history entry"
    assert all("edit" in name for name in hist)


def test_temp_file_left_behind_is_cleaned_up(tmp_path: Path):
    target = tmp_path / "b.kicad_sch"
    atomic_write_text(target, "x")
    assert not list(tmp_path.glob(".*.tmp*"))


def test_check_writable_refuses_unknown_extensions(tmp_path: Path):
    (tmp_path / "firmware.bin").write_bytes(b"\x00")
    with pytest.raises(UnsafeWriteError):
        check_writable(tmp_path / "firmware.bin")
    with pytest.raises(UnsafeWriteError):
        check_writable(tmp_path / "board.kicad_pcb.bak")      # never edit backups in place
    with pytest.raises(UnsafeWriteError):
        check_writable(tmp_path / ".history" / "board.kicad_pcb")


def test_transaction_rolls_back_when_validation_fails(tmp_path: Path):
    target = tmp_path / "board.kicad_pcb"
    target.write_text("(kicad_pcb good)", encoding="utf-8")

    def validator(path: str) -> None:
        if "good" not in Path(path).read_text(encoding="utf-8"):
            raise ValueError("corrupt")

    tx = FileTransaction(str(tmp_path), reason="test")
    with pytest.raises(UnsafeWriteError):
        tx.write("board.kicad_pcb", "(kicad_pcb broken)", validator=validator)
    tx.finish()
    assert target.read_text(encoding="utf-8") == "(kicad_pcb good)", "failed edit must not survive"


def test_transaction_restores_on_missing_commit(tmp_path: Path):
    target = tmp_path / "board.kicad_pcb"
    target.write_text("ORIGINAL", encoding="utf-8")
    tx = FileTransaction(str(tmp_path), reason="abandoned")
    tx.write("board.kicad_pcb", "EDITED")
    tx.finish()  # no commit() → rollback
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_transaction_commit_persists(tmp_path: Path):
    target = tmp_path / "board.kicad_pcb"
    target.write_text("ORIGINAL", encoding="utf-8")
    tx = FileTransaction(str(tmp_path), reason="kept")
    tx.write("board.kicad_pcb", "EDITED")
    record = tx.commit()
    tx.finish()
    assert target.read_text(encoding="utf-8") == "EDITED"
    assert record["files"][0]["path"].endswith("board.kicad_pcb")


def test_transaction_cannot_escape_its_root(tmp_path: Path):
    (tmp_path / "proj").mkdir()
    outside = tmp_path / "outside.kicad_pcb"
    outside.write_text("DO NOT TOUCH", encoding="utf-8")
    tx = FileTransaction(str(tmp_path / "proj"), reason="x")
    with pytest.raises(UnsafeWriteError):
        tx.write("../outside.kicad_pcb", "x")
    assert outside.read_text(encoding="utf-8") == "DO NOT TOUCH"


def test_transaction_requires_existing_root(tmp_path: Path):
    with pytest.raises(UnsafeWriteError):
        FileTransaction(str(tmp_path / "nope"), reason="x")
