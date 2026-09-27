"""pcb_router: optional backend, real status, and no stdout leakage into the RPC channel."""
from __future__ import annotations

import json
from pathlib import Path

from pcbai.eda import backend
from pcbai.steps import pcb_router


def test_route_pcb_without_kicad_returns_structured_failure(tmp_path: Path):
    res = pcb_router.route_pcb({"nets": [{"name": "GND", "pins": []}], "components": []},
                              str(tmp_path / "build"))
    assert res["ok"] is False
    assert res["reason"] == "backend-unavailable"
    assert res["backend"] == ("pcbnew" if backend.capabilities().pcbnew else "pcbnew-missing")
    assert any("pcbnew" in w for w in res["warnings"])


def test_route_pcb_creates_the_footprints_dir_it_reads(tmp_path: Path):
    """D5: the audited version loaded footprints from a directory nothing ever created."""
    pcb_router.route_pcb({"nets": [], "components": []}, str(tmp_path / "build"))
    assert (tmp_path / "build" / pcb_router.FOOTPRINTS_SUBDIR).is_dir()


def test_route_pcb_writes_netlist_json_not_xml(tmp_path: Path):
    pcb_router.route_pcb({"nets": [], "components": []}, str(tmp_path / "build"))
    build = tmp_path / "build"
    assert (build / "netlist.json").is_file()
    assert not (build / "netlist.xml").exists()
    json.loads((build / "netlist.json").read_text(encoding="utf-8"))


def test_fp_lib_table_points_at_the_local_footprints(tmp_path: Path):
    pcb_router.route_pcb({"nets": [], "components": []}, str(tmp_path / "build"))
    table = (tmp_path / "build" / "fp-lib-table").read_text(encoding="utf-8")
    assert "${KIPRJMOD}/footprints" in table


def test_generated_child_script_is_valid_python_and_logs_to_stderr():
    """D3: the child script must never write to stdout (it is inherited by the RPC server)."""
    script = pcb_router._generate_pcbnew_script(
        "/tmp/n.json", "/tmp/b.kicad_pcb", "/tmp/fp",
        experimental_router=False, outline={"width_mm": 60.0, "height_mm": 40.0})
    compile(script, "<child>", "exec")          # raises SyntaxError if broken
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("print(") or " print(" in stripped.replace("_log(", ""):
            assert "file=sys.stderr" in stripped or "_log(" in stripped, stripped
