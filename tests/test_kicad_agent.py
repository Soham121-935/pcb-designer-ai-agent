"""The agent surface: native KiCad-file tools, the safety gates, and the standalone CLI.

Nothing here needs KiCad installed, which is the point of this layer: the model can answer "what is on
this board and is it legal" and can edit a board the model wrote, while every message that *overstates*
confidence is refused. The CLI is tested through click's runner because that is what the host (Anna, or
a human at a terminal) actually invokes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pcbai.agent import kicad_tools
from pcbai.agent.cli import main as cli_main
from pcbai.agent.registry import dispatch, registry

from pcbai.kicad.scaffold import generate

SPEC = {
    "name": "agentboard",
    "title": "Agent test board",
    "outline": {"width": 34.0, "height": 24.0},
    "stackup": {"copper_layers": 2},
    "placement": {"origin": [2.0, 2.0], "gap": 0.3},
    "parts": [
        {"ref": "U1", "value": "MCU", "footprint": {"kind": "sot23", "name": "SOT-23-3"},
         "pins": {"1": "GND", "2": "SIG", "3": "+3V3"}, "at": [6.0, 8.0]},
        {"ref": "R1", "value": "10k", "footprint": "0402", "pins": {"1": "SIG", "2": "+3V3"}},
        {"ref": "C1", "value": "100n", "footprint": "0402", "pins": {"1": "+3V3", "2": "GND"}},
        {"ref": "J1", "value": "HDR", "footprint": {"kind": "pin_header", "name": "HDR", "pins": 2},
         "pins": {"1": "SIG", "2": "GND"}},
    ],
}


@pytest.fixture(scope="module")
def board_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("agentboard")
    result = generate(dict(SPEC), out, stem="board", keep_history=False)
    assert result.ok, [i.message for i in result.issues if i.severity == "error"]
    return out


@pytest.fixture(scope="module")
def board_path(board_dir: Path) -> str:
    return str(board_dir / "board.kicad_pcb")


def _ok(payload: dict) -> dict:
    assert payload.get("success") is True, payload
    assert "warnings" in payload
    return payload["data"]


REPO_TEMPLATE = Path(__file__).resolve().parents[1] / "anna-app" / "executas" / "pcb-designer" / \
    "pcbai" / "steps" / "template_project" / "board.kicad_pcb"


def test_generate_scaffold_tool_writes_a_project(tmp_path: Path) -> None:
    payload = dispatch("generate_scaffold", {"spec": dict(SPEC), "out_dir": str(tmp_path),
                                             "stem": "fromtool"}, allow_mutating=True, confirm=True)
    data = _ok(payload)
    board = Path(data["files"]["board"])
    assert board.exists() and "(kicad_pcb" in board.read_text(encoding="utf-8")
    assert data["ok"] is True
    assert (tmp_path / "fromtool.kicad_pro").exists()
    assert (tmp_path / "fromtool.kicad_dru").exists()
    assert "unrouted_nets" in data and isinstance(data["unrouted_nets"], list)



# ── read-only tools ─────────────────────────────────────────────────────────

def test_registry_exposes_the_native_kicad_tools() -> None:
    names = set(registry())
    assert set(kicad_tools.TOOL_FUNCTIONS) <= names
    for tool in ("inspect_pcb", "list_components", "validate_board", "apply_edit", "parse_sexp"):
        assert tool in names
    specs = registry()
    for tool in kicad_tools.MUTATING_TOOLS:
        assert specs[tool].mutating is True
    for tool in ("inspect_pcb", "get_nets", "validate_board"):
        assert specs[tool].mutating is False


def test_inspect_and_list_components(board_path: str) -> None:
    payload = dispatch("inspect_pcb", {"path": board_path})
    data = _ok(payload)
    assert data["summary"]["components"] == 4
    assert data["summary"]["copper_layers"] == 2
    assert data["summary"]["pads"] == 9
    assert payload["path"].endswith("board.kicad_pcb")
    comps = _ok(dispatch("list_components", {"path": board_path}))["components"]
    assert {c["ref"] for c in comps} == {"U1", "R1", "C1", "J1"}
    assert all(c["footprint"].startswith("pcbai:") for c in comps)
    assert next(c for c in comps if c["ref"] == "U1")["nets"] == ["+3V3", "GND", "SIG"]


def test_get_component_and_nets(board_path: str) -> None:
    one = _ok(dispatch("get_component", {"path": board_path, "ref": "U1"}))
    assert one["ref"] == "U1" and one["footprint"].endswith("SOT-23-3")
    assert one["at"] == pytest.approx((6.0, 8.0, 0.0))
    assert sorted(p["net"] for p in one["pads"]) == ["+3V3", "GND", "SIG"]
    assert one["courtyard"] and one["bbox"]
    nets = _ok(dispatch("get_nets", {"path": board_path}))["nets"]
    by_name = {n["net"]: n for n in nets}
    assert by_name["GND"]["pads"] == 3 and by_name["SIG"]["pads"] == 3
    assert all(n["likely_unconnected"] >= 0 for n in nets), "a negative 'unconnected' count is a bug"
    assert by_name["+3V3"]["net_class"] == "Default"
    undrouted = _ok(dispatch("get_nets", {"path": board_path, "only_undrouted": True}))["nets"]
    assert all(n["pads"] > 1 for n in undrouted)
    assert all(n["likely_unconnected"] > 0 for n in undrouted)
    assert "members" in undrouted[0]


def test_board_outline_and_design_rules(board_path: str, board_dir: Path) -> None:
    outline = _ok(dispatch("get_board_outline", {"path": board_path}))
    assert outline["bbox"] == pytest.approx((0.0, 0.0, 34.0, 24.0), abs=1e-6)
    assert outline["segments"] >= 4 and outline["size_mm"] == pytest.approx((34.0, 24.0))
    assert outline["outside_outline"] == []
    rules = _ok(dispatch("get_design_rules", {"path": board_path}))
    assert rules["rules"]["min_clearance"] == pytest.approx(0.127)
    assert rules["rules"]["annular_ring_min"] == pytest.approx(0.15)
    assert rules["rules"]["min_courtyard_clearance"] == pytest.approx(0.25)
    assert rules["rules_source"].endswith("board.kicad_pro")
    assert rules["net_classes"]["Default"]["clearance"] == pytest.approx(0.127)
    assert rules["stackup"] and rules["stackup"][0]["name"] == "air"


def test_validate_board_reports_its_own_limits(board_path: str) -> None:
    payload = dispatch("validate_board", {"path": board_path})
    data = _ok(payload)
    assert data["ok"] is True
    assert data["counts"] == {"error": 0, "warning": 0, "info": 0}, \
        "a clean board must report zeroed counts, not an empty dict"
    assert data["issues"] == [] and data["summary"]["pads"] == 9, \
        "a validation that looked at nothing must not claim success"
    assert data["summary"]["components"] == 4
    assert any("kicad" in w.lower() for w in payload["warnings"]), payload["warnings"]
    assert "clearance matrix" in json.dumps(payload["warnings"]).lower()


def test_parse_sexp_selector_and_footprint_tools(board_path: str) -> None:
    data = _ok(dispatch("parse_sexp", {"path": board_path, "selector": ["setup", "stackup"]}))
    assert data["selected"] == "setup/stackup"
    assert data["node"][0] == "stackup"
    text = json.dumps(data)
    assert "epsilon_r" in text and "Copper" in text
    bad = dispatch("parse_sexp", {"path": board_path, "selector": ["nope", "nada"]})
    assert not bad["success"] or not bad["data"].get("node")
    kinds = _ok(dispatch("list_footprint_kinds", {}))["kinds"]
    assert {"lqfp", "usb_c", "0402"} <= set(kinds)
    fp = _ok(dispatch("inspect_footprint", {"spec": "lqfp"}))
    assert fp["pad_count"] == 48 and fp["pads"][0]["size"] == [0.45, 1.0]
    assert fp["issues"] == [], fp["issues"][:2]
    assert fp["sexp_preview"].startswith("(footprint")
    missing = dispatch("inspect_footprint", {"spec": "no_such_thing"})
    assert not missing["success"]
    assert "unknown footprint kind" in missing["error"]


def test_find_board_from_a_directory(board_dir: Path) -> None:
    """Every tool accepts a project directory, so a host can point it at the workspace root."""
    data = _ok(dispatch("inspect_pcb", {"path": str(board_dir)}))
    assert data["summary"]["components"] == 4


# ── the gates ───────────────────────────────────────────────────────────────

def test_unknown_unimplemented_and_mutating_gates() -> None:
    unknown = dispatch("flatten_board", {})
    assert unknown["reason"] == "unknown-tool" and "unknown tool" in unknown["error"]
    stubs = [n for n, spec in registry().items() if spec.maturity.startswith("not-implemented")]
    assert stubs, "the registry lost its honest placeholders"
    assert dispatch(stubs[0], {}, allow_mutating=True, confirm=True)["reason"] == "not-implemented"
    gated = dispatch("write_board", {"path": "/tmp/whatever.kicad_pcb"}, allow_mutating=False)
    assert gated["reason"] == "needs-mutation-approval"
    unconfirmed = dispatch("write_board", {"path": "/tmp/whatever.kicad_pcb"}, confirm=False)
    assert unconfirmed["reason"] == "needs-confirmation"
    bad = dispatch("get_component", {"path": "/tmp/nope.kicad_pcb", "ref": "X"})
    assert not bad["success"]
    assert bad["reason"] in ("not-found", "bad-args") or bad["error"]["code"] == "not-found"


def test_apply_edit_dry_run_then_write(board_path: str, board_dir: Path) -> None:
    original = Path(board_path).read_text(encoding="utf-8")
    edit = {"op": "move_component", "ref": "R1", "x": 26.0, "y": 18.0}
    preview = dispatch("apply_edit", {"path": board_path, "edits": [edit], "dry_run": True},
                       allow_mutating=True, confirm=True)
    data = _ok(preview)
    assert data["applied"][0]["op"] == "move_component"
    assert data["dry_run"] is True
    assert data["lossiness"]["lossy"] is False, data["lossiness"]
    assert data["would_have_errors"] == []
    assert Path(board_path).read_text(encoding="utf-8") == original, "a dry run wrote the file"

    # confirm=True clears the policy gate; dry_run=False is what says "yes, actually write it"
    written = dispatch("apply_edit", {"path": board_path, "edits": [edit], "dry_run": False},
                       allow_mutating=True, confirm=True)
    after = _ok(written)
    assert after["dry_run"] is False
    assert Path(board_path).read_text(encoding="utf-8") != original
    assert after["verified"] is True and after["written"] is True, after
    assert after["readback"]["components"] == 4 and after["readback"]["pads"] == 9
    assert (board_dir / "board.kicad_pcb.bak").exists(), "the previous board was overwritten blindly"
    moved = _ok(dispatch("get_component", {"path": board_path, "ref": "R1"}))
    assert moved["at"][:2] == pytest.approx((26.0, 18.0))
    assert after["readback"]["pads"] == 9


def test_an_edit_that_would_break_the_board_is_refused(board_path: str) -> None:
    """Tightening min_clearance past copper that is already there must not be written silently."""
    before = Path(board_path).read_text(encoding="utf-8")
    payload = dispatch("apply_edit", {"path": board_path, "dry_run": False, "edits": [
        {"op": "set_rule", "field": "min_clearance", "value": 5.0}]},
        allow_mutating=True, confirm=True)
    assert not payload["success"]
    assert payload["reason"] in ("validation-failed", "edit-invalidates-board"), payload["reason"]
    assert payload["data"]["written"] is False
    assert payload["warnings"], "a refusal with no reason is useless to an agent"
    assert Path(board_path).read_text(encoding="utf-8") == before, "a refused edit still wrote"


def test_apply_edit_rejects_typos_and_missing_refs(board_path: str) -> None:
    payload = _ok(dispatch("apply_edit", {"path": board_path, "dry_run": True, "edits": [
        {"op": "move_component", "ref": "U1", "dx": 1.0, "dy": 0.0},
        {"op": "nudge_component", "ref": "U1"},                 # not an op
        {"op": "move_component", "ref": "U99", "dx": 1.0},       # not a part
    ]}, allow_mutating=True, confirm=True))
    assert len(payload["applied"]) == 1, payload["applied"]
    why = json.dumps(payload["rejected"])
    assert "nudge_component" in why and "U99" in why


def test_apply_edit_refuses_a_board_the_model_cannot_round_trip(tmp_path: Path) -> None:
    """A board KiCad wrote carries tokens the model does not store; editing it would quietly drop them."""
    template = REPO_TEMPLATE.read_text(encoding="utf-8") if REPO_TEMPLATE.exists() else None
    if template is None:
        pytest.skip("reference project not shipped in this checkout")
    board = tmp_path / "foreign.kicad_pcb"
    board.write_text(template, encoding="utf-8")
    payload = dispatch("apply_edit", {"path": str(board), "dry_run": True,
                                     "edits": [{"op": "set_rule", "field": "min_clearance",
                                                "value": 0.2}]},
                       allow_mutating=True, confirm=True)
    assert not payload["success"], "a lossy edit on a KiCad-authored board was allowed silently"
    assert "lossy" in json.dumps(payload).lower()
    allowed = dispatch("apply_edit", {"path": str(board), "dry_run": True, "allow_lossy": True,
                                      "edits": [{"op": "set_rule", "field": "min_clearance",
                                                 "value": 0.2}]},
                       allow_mutating=True, confirm=True)
    assert allowed["success"], allowed
    assert allowed["data"]["lossiness"]["lossy"] is True, allowed["data"]["lossiness"]



# ── the CLI ─────────────────────────────────────────────────────────────────

def _run(*argv: str):
    return CliRunner().invoke(cli_main, list(argv))


def test_cli_lists_and_describes_tools(tmp_path: Path) -> None:
    res = _run("tools")
    assert res.exit_code == 0, res.output
    assert "inspect_pcb" in res.output and "apply_edit" in res.output
    res = _run("describe", "get_nets")
    assert res.exit_code == 0
    assert json.loads(res.output)["name"] == "get_nets"
    assert _run("describe", "no_such_tool").exit_code != 0


def test_cli_scaffold_validate_inspect_and_nets(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    res = _run("scaffold", str(spec), "--out", str(tmp_path / "out"), "--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["success"], payload
    board = Path(payload["data"]["files"]["board"])
    assert board.exists()

    for cmd, key in (("validate", "counts"), ("inspect", "summary"), ("rules", "rules")):
        res = _run(cmd, str(board), "--json")
        assert res.exit_code == 0, res.output
        assert key in json.dumps(json.loads(res.output)["data"]), cmd

    res = _run("nets", str(board), "--undrouted", "--json")
    assert res.exit_code == 0
    body = json.loads(res.output)["data"]
    assert isinstance(body["nets"], list)

    res = _run("scaffold", str(spec), "--out", str(tmp_path / "dry"), "--dry-run", "--json")
    assert res.exit_code == 0
    assert not (tmp_path / "dry" / "board.kicad_pcb").exists(), "--dry-run wrote a board"


def test_cli_edit_is_a_dry_run_until_written(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(SPEC), encoding="utf-8")
    out = tmp_path / "out2"
    board = json.loads(_run("scaffold", str(spec), "--out", str(out), "--json").output)["data"] \
        ["files"]["board"]
    op = '{"op":"move_component","ref":"R1","x":26.0,"y":18.0}'
    before = Path(board).read_text(encoding="utf-8")
    res = _run("edit", board, "--op", op, "--json")
    assert res.exit_code == 0, res.output
    assert Path(board).read_text(encoding="utf-8") == before, "edit without --write changed the file"
    res = _run("edit", board, "--op", op, "--write", "--json")
    assert res.exit_code == 0, res.output
    assert Path(board).read_text(encoding="utf-8") != before
    data = json.loads(res.output)["data"]
    assert data["applied"][0]["op"] == "move_component"
    bad = _run("edit", board, "--op", "not json", "--json")
    assert bad.exit_code != 0
    assert "valid JSON" in bad.output
    assert _run("edit", board, "--json").exit_code != 0        # no --op at all


def test_cli_rejects_a_nonexistent_board(tmp_path: Path) -> None:
    res = _run("validate", str(tmp_path / "nothing.kicad_pcb"), "--json")
    body = res.output
    assert res.exit_code != 0 or '"success": false' in body, body[:400]
    assert "not-found" in body or "no board" in body, body[:400]
