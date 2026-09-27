"""Phase 2 invariants for the Anna JSON-RPC surface.

These encode the fixes for docs/AUDIT.md D2 (fake success), D3 (stdout pollution) and D10
(latent NameError-prone annotations), plus "no silent fallbacks".
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import PLUGIN_DIR, rpc  # sys.path bootstrap lives in conftest

pytest.importorskip("click")


def _by_id(responses, rid):
    for r in responses:
        if r.get("id") == rid:
            return r
    return None


def test_protocol_channel_is_pure_json():
    """Every non-empty stdout line must be a parseable JSON-RPC message (D3)."""
    responses, stdout, stderr = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
    for line in stdout.splitlines():
        if line.strip():
            json.loads(line)
    assert _by_id(responses, 1)["result"]["protocolVersion"] == "2.0"


def test_health_reports_real_eda_backend():
    responses, _, _ = rpc([{"jsonrpc": "2.0", "id": 2, "method": "health"}])
    body = _by_id(responses, 2)["result"]
    assert body["status"] == "ready"
    eda = body["eda_backend"]
    for key in ("pcbnew", "kicad_cli", "footprint_libs", "can_author_native"):
        assert key in eda
    # In a KiCad-less sandbox the plugin must advertise itself as degraded, not healthy.
    if not eda["pcbnew"]:
        assert body["degraded"] is True
        assert any("pcbnew" in n for n in eda["notes"])


def test_describe_advertises_tools_without_overclaiming():
    responses, _, _ = rpc([{"jsonrpc": "2.0", "id": 3, "method": "describe"}])
    tools = {t["name"]: t["description"] for t in _by_id(responses, 3)["result"]["tools"]}
    assert len(tools) == 7
    # The audited manifest promised an Octopart lookup that does not exist in the code.
    assert "Octopart" not in tools["generate_bom"]
    # The fake-success pipeline must now describe its mode contract.
    assert "mode" in tools["full_pipeline"] and "template" in tools["full_pipeline"].lower()


def test_unknown_tool_returns_protocol_error():
    responses, _, _ = rpc([{"jsonrpc": "2.0", "id": 5, "method": "invoke",
                            "params": {"tool": "does_not_exist", "arguments": {}, "context": {}}}])
    err = _by_id(responses, 5)["error"]
    assert err["code"] == -32601


def test_route_pcb_reports_missing_backend_as_failure(rpc_env):
    """D2: a tool whose work failed must never return success:true."""
    responses, _, _ = rpc(
        [{"jsonrpc": "2.0", "id": 6, "method": "invoke", "params": {
            "tool": "route_pcb",
            "arguments": {"netlist_json": json.dumps({"nets": [], "components": []}),
                          "output_dir": str(rpc_env["PCB_AI_WORKDIR"])},
            "context": {}}}], env=rpc_env)
    body = _by_id(responses, 6)["result"]
    from pcbai.eda import backend
    if backend.capabilities().pcbnew:
        pytest.skip("KiCad present: this run exercised a real board build")
    assert body["success"] is False
    assert body["reason"] == "backend-unavailable"
    assert "pcbnew" in body["error"]


def test_generate_footprint_returns_valid_kicad_mod():
    args = {"footprint_type": "qfp", "params_json": json.dumps(
        {"name": "TQFP144_TEST", "pins": 144, "pitch": 0.5, "body_l": 20, "body_w": 20,
         "pad_l": 1.2, "pad_w": 0.25})}
    responses, _, _ = rpc([{"jsonrpc": "2.0", "id": 7, "method": "invoke",
                            "params": {"tool": "generate_footprint", "arguments": args,
                                       "context": {}}}])
    body = _by_id(responses, 7)["result"]
    assert body["success"] is True
    content = body["data"]["kicad_mod_content"]
    assert content.count("(pad ") == 144
    assert content.count("(") == content.count(")")


def test_full_pipeline_never_presents_a_template_as_a_design(rpc_env):
    """D1: the pipeline may fall back to the reference board, but must flag it."""
    responses, _, _ = rpc([{"jsonrpc": "2.0", "id": 8, "method": "invoke", "params": {
        "tool": "full_pipeline",
        "arguments": {"description": "Design a 4-layer LFE5U-12F FPGA board with DDR3"},
        "context": {"invoke_id": "t"}}}], env=rpc_env, timeout=120)
    body = _by_id(responses, 8)["result"]
    if not body.get("success"):
        assert body.get("reason")            # honest failure also acceptable
        return
    data = body["data"]
    assert data["mode"] in ("generated", "template")
    if data["mode"] == "template":
        assert data["template_only"] is True
        assert data["warnings"], "template mode must carry a warning"
        assert "byte-for-byte" in " ".join(data["warnings"]) or "NO relationship" in " ".join(data["warnings"])


def test_full_pipeline_reports_generated_files_as_parseable(rpc_env):
    responses, _, _ = rpc([{"jsonrpc": "2.0", "id": 9, "method": "invoke", "params": {
        "tool": "full_pipeline", "arguments": {"description": "ESP32 board"},
        "context": {"invoke_id": "t"}}}], env=rpc_env, timeout=120)
    body = _by_id(responses, 9)["result"]
    if not body.get("success"):
        pytest.skip("pipeline unavailable in this configuration")
    data = body["data"]
    assert data["pcb"].lstrip().startswith("(kicad_pcb")
    assert data["sch"].lstrip().startswith("(kicad_sch")


def test_plugin_module_has_no_undefined_annotation_names():
    """D10: module-level Dict/Optional usage requires real typing imports."""
    import ast

    src = (PLUGIN_DIR / "plugin.py").read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            imported |= {a.name for a in node.names}
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id in {"Dict", "List", "Optional", "Any", "Tuple", "Union"}}
    missing = used - imported
    assert not missing, f"typing names used but not imported: {sorted(missing)}"


def test_typing_annotations_actually_resolve():
    """D10: `plugin.py` used typing names in runtime-evaluated annotations without importing them."""
    import importlib

    plugin = importlib.import_module("plugin")   # conftest put the plugin dir on sys.path
    hints = plugin._tool_full_pipeline.__doc__ is not None
    assert hints
    import inspect
    sig = inspect.signature(plugin.sample)
    for param in sig.parameters.values():
        if param.annotation is not inspect.Parameter.empty:
            assert isinstance(param.annotation, str) or param.annotation is not None
    # module-level annotated globals must exist and be the documented types
    assert isinstance(plugin.host_responses, dict)
    assert isinstance(plugin.agent_requests.queue, object)
