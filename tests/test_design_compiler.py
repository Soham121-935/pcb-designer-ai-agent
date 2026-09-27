"""design_compiler: no silent template copying, and no fabricated success (audit D1/D2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbai.steps.design_compiler import DesignNotGenerated, compile_design
from pcbai.eda import backend


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Deterministic: never reach for LM Studio/localhost during tests."""
    monkeypatch.setenv("PCB_AI_LLM_PROVIDER", "dummy")


def test_template_fallback_is_reported_as_such(tmp_path: Path):
    out = tmp_path / "proj"
    res = compile_design("Design a 4-layer LFE5U-12F FPGA board", str(out),
                         allow_template_copy=True)
    caps = backend.capabilities()
    if res["mode"] == "template":
        assert res["template_only"] is True
        assert any("byte-for-byte" in w for w in res["warnings"])
        assert any("NO relationship" in w for w in res["warnings"])
        assert not caps.can_author_native, "template mode must only happen when KiCad is missing"
    else:
        assert res["mode"] == "generated"
        assert res["template_only"] is False
    assert Path(res["pcb"]).exists() and Path(res["sch"]).exists()


def test_three_prompts_do_not_silently_share_one_output(tmp_path: Path):
    """The audited bug: every prompt produced the identical board with no indication why.

    With the fix, identical bytes are still possible (template mode) but only when the result is
    explicitly labelled template_only — a 'generated' result that ignores the prompt is a bug.
    """
    results = []
    for i, prompt in enumerate(["ESP32-C3 sensor board", "ATmega328 LED blinker", "TP4056 LiPo charger"]):
        results.append(compile_design(prompt, str(tmp_path / f"p{i}"), allow_template_copy=True))
    pcb_hashes = {hash(Path(r["pcb"]).read_bytes()) for r in results}
    for r in results:
        if r["mode"] == "generated":
            assert len(pcb_hashes) == len(results), "generated boards must differ per prompt"
        else:
            assert r["template_only"] is True


def test_template_disabled_raises_with_actionable_message(tmp_path: Path):
    if backend.capabilities().can_author_native:
        pytest.skip("KiCad available: generation succeeds, no fallback needed")
    with pytest.raises(DesignNotGenerated) as exc:
        compile_design("anything", str(tmp_path / "x"), allow_template_copy=False)
    assert exc.value.reason in ("backend-unavailable", "generation-failed")
    msg = str(exc.value)
    assert "PCB_AI_ALLOW_TEMPLATE_COPY" in msg and "KiCad" in msg


def test_bom_is_written_from_the_prompt_dependant_stage(tmp_path: Path):
    res = compile_design("board with mcu, usb and buck", str(tmp_path / "b"), allow_template_copy=True)
    bom_path = tmp_path / "b" / "bom.json"
    assert bom_path.exists()
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    assert isinstance(bom, list)
    assert res["bom"] == bom


def test_zip_excludes_backups_and_history(tmp_path: Path):
    out = tmp_path / "z"
    compile_design("mcu", str(out), allow_template_copy=True)
    (out / "board.kicad_pcb.bak").write_text("junk", encoding="utf-8")
    (out / ".history").mkdir(exist_ok=True)
    (out / ".history" / "old.kicad_pcb").write_text("junk", encoding="utf-8")
    res = compile_design("mcu", str(out), allow_template_copy=True)
    import zipfile
    with zipfile.ZipFile(res["zip"]) as zf:
        names = zf.namelist()
    assert "board.kicad_pcb" in names
    assert not any(n.endswith(".bak") or n.startswith(".history") for n in names)


def test_backend_field_documents_the_environment(tmp_path: Path):
    res = compile_design("mcu", str(tmp_path / "b2"), allow_template_copy=True)
    assert "pcbnew" in res["backend"] and "footprint_libs" in res["backend"]
