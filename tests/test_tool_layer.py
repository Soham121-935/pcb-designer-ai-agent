"""Standalone tool layer: registry policy, footprint write+verify, structural inspection.

Everything here must pass with no KiCad and no API keys.
"""
from __future__ import annotations

import json
from pathlib import Path

from pcbai.agent import tools as T
from pcbai.agent.registry import dispatch, registry


# ── registry / policy ────────────────────────────────────────────────────────
def test_every_implemented_tool_has_a_handler():
    specs = registry()
    for name, spec in specs.items():
        if spec.maturity.startswith("not-implemented"):
            continue
        assert T.TOOL_FUNCTIONS.get(name), f"{name} is advertised but has no callable"


def test_planned_tools_are_refused_with_phase_pointer():
    res = dispatch("move_component", {"ref": "C12"})
    assert res["success"] is False
    assert res["reason"] == "not-implemented"
    assert "Phase" in res["error"]


def test_unknown_tool_is_rejected():
    res = dispatch("frobnicate_board")
    assert res["success"] is False and res["reason"] == "unknown-tool"


def test_mutating_tool_requires_confirmation():
    refused = dispatch("generate_footprint", {"footprint_type": "header", "params": {"name": "X"}})
    assert refused["success"] is False and refused["reason"] == "needs-confirmation"
    allowed = dispatch("generate_footprint", {"footprint_type": "header",
                                              "params": {"name": "HDR_TEST", "pins": 2, "pitch": 2.54,
                                                         "pad_dia": 1.7, "drill_dia": 1.0}},
                       confirm=True)
    assert allowed["success"] is True


def test_mutations_can_be_globally_disabled():
    res = dispatch("generate_footprint", {"footprint_type": "header"}, allow_mutating=False)
    assert res["success"] is False and res["reason"] == "needs-mutation-approval"


# ── footprint generation: write, read back, verify ───────────────────────────
def test_generate_footprint_writes_parses_and_pads(tmp_path: Path):
    res = T.generate_footprint_tool("qfp", {
        "name": "TQFP144_SV16", "pins": 144, "pitch": 0.5,
        "body_l": 20, "body_w": 20, "pad_l": 1.2, "pad_w": 0.25},
        output_dir=str(tmp_path))
    assert res["success"] is True
    written = Path(res["data"]["path"])
    assert written.name == "TQFP144_SV16.kicad_mod"
    on_disk = written.read_text(encoding="utf-8")
    assert on_disk == res["data"]["kicad_mod_content"], "re-read must match generated content"
    assert on_disk.count("(pad ") == 144
    assert on_disk.count("(") == on_disk.count(")")


def test_generate_footprint_bad_args_are_not_fabricated(tmp_path: Path):
    """QFP-144 needs 4-side distribution; an unsupported pin count must be a clear error."""
    res = T.generate_footprint_tool("qfp", {"name": "ODD", "pins": 13, "pitch": 0.5,
                                            "body_l": 5, "body_w": 5, "pad_l": 1, "pad_w": 0.4},
                                    output_dir=str(tmp_path))
    assert res["success"] is False
    assert res["reason"] in ("bad-args", "generator-error")


def test_unknown_footprint_type_lists_supported():
    res = T.generate_footprint_tool("soic-never", {})
    assert res["success"] is False and "qfn" in res["error"]


def test_footprint_json_string_params_accepted(tmp_path: Path):
    res = T.generate_footprint_tool("smd_rc", json.dumps(
        {"name": "R_0603_T", "body_l": 1.6, "body_w": 0.8, "pad_l": 0.9, "pad_w": 0.8, "gap": 0.8}),
        output_dir=str(tmp_path))
    assert res["success"] is True and res["data"]["pads"] == 2


# ── project file tools ───────────────────────────────────────────────────────
def test_list_project_files_finds_kicad_files(tmp_path: Path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "board.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    (tmp_path / "project" / "ignoreme.bin").write_bytes(b"\x00")
    res = T.list_project_files(path=str(tmp_path / "project"))
    names = [f["path"] for f in res["data"]["files"]]
    assert names == ["board.kicad_pcb"]


def test_read_project_file_blocks_path_escape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PCB_AI_PROJECT", str(tmp_path / "project"))
    (tmp_path / "project").mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("nope", encoding="utf-8")
    res = T.read_project_file("../outside.txt")
    assert res["success"] is False and res["reason"] == "unsafe-path"


# ── structural inspection (pre-Phase-4 sanity checks) ─────────────────────────
def test_inspect_reports_missing_net_table_and_outline(tmp_path: Path):
    from tests.conftest import make_board_kicad_pcb

    bad = tmp_path / "legacy.kicad_pcb"
    bad.write_text(make_board_kicad_pcb(with_net_table=False, tracks=0), encoding="utf-8")
    res = T.inspect_pcb_file(str(bad))
    assert res["success"] is True
    joined = " ".join(res["warnings"])
    assert "net table" in joined
    assert "unrouted" in joined
    assert res["data"]["net_declarations"] == 0


def test_inspect_clean_tiny_board_has_no_net_table_warning(tiny_board: Path):
    res = T.inspect_pcb_file(str(tiny_board))
    assert res["success"] is True and res["data"]["net_declarations"] == 3  # "" + VDD_3V3 + GND
    assert not any("net table" in w for w in res["warnings"])


def test_inspect_rejects_non_pcb(tmp_path: Path):
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    res = T.inspect_pcb_file(str(f))
    assert res["success"] is False and res["reason"] == "not-a-pcb"


def test_template_board_missing_net_table_is_a_known_gap(legacy_template_board: Path):
    """Regression guard for an audit finding: the shipped reference board has no net table.

    When Phase 4 replaces the template with a properly generated project, update this test.
    """
    res = T.inspect_pcb_file(str(legacy_template_board))
    assert res["success"] is True
    assert res["data"]["footprints"] == 21
    assert res["data"]["zones"] == 3, "two GND pours + one rule area"
    assert res["data"]["segments"] == 9, "only 9 hand-drawn copper segments: not a routed board"
    assert res["data"]["edge_cuts"] == 4, "outline exists (40x30 rectangle)"
    assert res["data"]["net_declarations"] == 0
    assert any("net table" in w for w in res["warnings"])


# ── BOM / requirements ───────────────────────────────────────────────────────
def test_generate_bom_flags_unmatched_keywords():
    res = T.generate_bom_tool({"keywords": ["mcu", "flux-capacitor"]})
    assert res["success"] is True
    assert any("flux-capacitor" in w for w in res["warnings"])


def test_parse_requirements_reports_degraded_llm():
    res = T.parse_requirements_tool("ESP32 with USB-C and a buck converter", use_llm=False)
    assert res["success"] is True
    assert {"mcu", "usb", "buck"} & set(res["data"]["keywords"])


def test_capabilities_never_claims_kicad_it_does_not_have():
    from pcbai.eda import backend

    res = T.capabilities_tool()
    caps = backend.capabilities()
    assert res["data"]["pcbnew"] == caps.pcbnew
    if not caps.kicad_cli:
        assert any("kicad-cli unavailable" in n for n in res["data"]["notes"] or res["warnings"])


def test_create_backup_produces_a_restorable_copy(tiny_board: Path):
    before = tiny_board.read_text(encoding="utf-8")
    res = T.create_backup(str(tiny_board), reason="test")
    assert res["success"] is True
    backup = Path(res["data"]["backup"])
    assert backup.exists() and backup.read_text(encoding="utf-8") == before
    # the agent can restore itself after a bad edit
    tiny_board.write_text("(kicad_pcb corrupted", encoding="utf-8")
    tiny_board.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
    assert tiny_board.read_text(encoding="utf-8") == before


def test_create_backup_refuses_protected_targets(tmp_path: Path):
    (tmp_path / "firmware.bin").write_bytes(b"\x00")
    res = T.create_backup(str(tmp_path / "firmware.bin"))
    assert res["success"] is False and res["reason"] == "unsafe-path"
