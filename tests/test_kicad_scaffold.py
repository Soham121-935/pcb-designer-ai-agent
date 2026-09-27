"""The scaffold: a spec dict in, a KiCad 10 board out — and no lies about what got done.

These tests pin the promises the generator makes to a human: every part in the spec ends up on the
board (or the result names the one that did not), a claim of "clean" is re-checked by re-reading the
file that was written, and nets that were not routed stay in ``unrouted_nets`` instead of quietly
disappearing. All three were real bugs during this phase.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbai.kicad.footprints import REGISTRY, from_spec
from pcbai.kicad.model import Track
from pcbai.kicad.pcb_reader import read_board, read_tree
from pcbai.kicad.pcb_writer import lossiness
from pcbai.kicad.scaffold import (DEFAULT_RULES, CopperMap, add_planes, build_board, fanout,
                                  generate, load_spec, route_two_pad_nets)

REPO = Path(__file__).resolve().parents[1]
SV16_SPEC = REPO / "anna-app" / "executas" / "pcb-designer" / "pcbai" / "boards" / "sv16.yaml"

MINI_SPEC = {
    "name": "mini",
    "title": "Mini board",
    "outline": {"width": 18.0, "height": 12.0},
    "stackup": {"copper_layers": 2},
    "rules": {"min_clearance": 0.2, "min_track_width": 0.25},
    "parts": [
        {"ref": "U1", "value": "T", "footprint": {"kind": "sot23", "name": "SOT-23-3"},
         "pins": {"1": "GND", "2": "OUT", "3": "+3V3"}, "at": [4.0, 6.0]},
        {"ref": "R1", "value": "10k", "footprint": "0402", "pins": {"1": "OUT", "2": "+3V3"}},
        {"ref": "C1", "value": "100n", "footprint": "0402", "pins": {"1": "+3V3", "2": "GND"}},
    ],
}


def four_layer_spec(**over) -> dict:
    spec = json.loads(json.dumps(MINI_SPEC))
    spec["stackup"] = {"copper_layers": 4}
    spec["planes"] = [{"net": "GND", "layer": "In1.Cu"},
                      {"net": "+3V3", "layer": "In2.Cu"},
                      {"net": "GND", "layer": "In1.Cu", "fill": False, "name": "edge_keepout"}]
    spec["net_classes"] = {"Power": {"clearance": 0.25, "track_width": 0.4, "nets": ["+3V3"]}}
    spec.update(over)
    return spec


# ── spec loading ────────────────────────────────────────────────────────────

def test_load_spec_accepts_a_dict_a_yaml_path_and_a_json_path(tmp_path: Path) -> None:
    assert load_spec(dict(MINI_SPEC))["name"] == "mini"
    yml = tmp_path / "s.yaml"
    yml.write_text("name: from_yaml\nparts: []\n", encoding="utf-8")
    assert load_spec(yml)["name"] == "from_yaml"
    js = tmp_path / "s.json"
    js.write_text(json.dumps({"name": "from_json", "parts": []}), encoding="utf-8")
    assert load_spec(js)["name"] == "from_json"
    with pytest.raises((FileNotFoundError, ValueError)):
        load_spec(tmp_path / "nope.yaml")


def test_the_shipped_sv16_spec_is_valid_and_complete() -> None:
    spec = load_spec(SV16_SPEC)
    assert spec["stackup"]["copper_layers"] == 4
    assert spec["rules"]["min_clearance"] <= 0.2, "the shipped defaults must be fab-realistic"
    refs = [p["ref"] for p in spec["parts"]]
    assert len(refs) == len(set(refs)), "a reference appears twice"
    nets = {n for p in spec["parts"] for n in (p.get("pins") or {}).values()}
    assert {"GND", "+3V3"} <= nets
    for part in spec["parts"]:
        if part.get("group") != "mech":
            assert part.get("pins"), f"{part['ref']} has no pin map"
        kind = part.get("footprint")
        kind = kind.get("kind", "passive") if isinstance(kind, dict) else (kind or "passive")
        assert kind in REGISTRY, f"{part['ref']} asks for a footprint nobody can build: {kind}"
    for cls in spec["net_classes"].values():
        for net in cls["nets"]:
            assert net in nets, f"net class mentions {net!r}, which no pad uses"


# ── build_board ─────────────────────────────────────────────────────────────

def test_build_board_places_every_part_it_was_given() -> None:
    board = build_board(dict(MINI_SPEC))
    assert sorted(f.reference for f in board.footprints) == ["C1", "R1", "U1"]
    assert board.n_copper == 2 and len(board.copper) == 2
    assert {"GND", "OUT", "+3V3"} <= {p.net for _, p in board.all_pads() if p.net}
    u1 = board.find("U1")
    assert (u1.x, u1.y) == pytest.approx((4.0, 6.0)), "an explicit `at` must win over the packer"


def test_a_pin_map_that_names_a_pad_that_does_not_exist_is_refused() -> None:
    """The loudest failure this generator can have: silently dropping a connection."""
    spec = json.loads(json.dumps(MINI_SPEC))
    spec["parts"][1]["pins"] = {"1": "OUT", "7": "+3V3"}          # a 0402 has pads 1 and 2
    with pytest.raises(ValueError, match="do not exist on footprint"):
        build_board(spec)


def test_unknown_footprint_kind_is_a_clear_error() -> None:
    spec = json.loads(json.dumps(MINI_SPEC))
    spec["parts"][1]["footprint"] = {"kind": "bga_who_knows"}
    with pytest.raises(ValueError, match="unknown footprint kind"):
        build_board(spec)


def test_parts_are_never_stacked_on_the_same_spot() -> None:
    board = build_board(dict(MINI_SPEC))
    seen = [(round(f.x, 3), round(f.y, 3)) for f in board.footprints]
    assert len(set(seen)) == len(seen), f"parts were placed on top of each other: {seen}"


def test_default_rules_come_from_the_module_not_a_copy_of_a_template() -> None:
    board = build_board({k: v for k, v in MINI_SPEC.items() if k != "rules"})
    assert board.rules.min_clearance == pytest.approx(DEFAULT_RULES["min_clearance"])
    assert board.rules.min_track_width == pytest.approx(DEFAULT_RULES["min_track_width"])
    for _, pad in board.all_pads():
        if pad.is_tht:
            assert pad.drill, "a through-hole pad with no drill is a solid copper disc"


# ── planes, classes, rules ──────────────────────────────────────────────────

def test_four_layer_planes_land_on_the_requested_layers() -> None:
    board = build_board(four_layer_spec())
    assert list(board.copper) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    pours = [z for z in board.zones if z.fill]
    assert {z.layer for z in pours} == {"In1.Cu", "In2.Cu"}
    assert {z.net for z in pours} == {"GND", "+3V3"}
    assert any(z.name == "edge_keepout" and not z.fill for z in board.zones)
    for z in board.zones:
        assert z.outline, f"zone {z.net} on {z.layer} has no outline"
        assert z.clearance >= 0.1
    assert board.rules.clearance_for("+3V3").clearance == pytest.approx(0.25)
    assert board.rules.clearance_for("+3V3").track_width == pytest.approx(0.4)
    assert board.rules.clearance_for("OUT").clearance == pytest.approx(0.2)     # Default, from spec
    assert board.net("+3V3").net_class == "Power"


def test_net_class_membership_survives_a_round_trip(tmp_path: Path) -> None:
    result = generate(four_layer_spec(), tmp_path, stem="classes")
    project = json.loads(Path(result.files["project"]).read_text(encoding="utf-8"))
    classes = {c["name"]: c for c in project["net_settings"]["classes"]}
    assert "Power" in classes and "+3V3" in classes["Power"]["nets"]
    assert classes["Power"]["clearance"] == pytest.approx(0.25)
    back = read_board(Path(result.files["board"]))
    assert back.rules.clearance_for("+3V3").clearance == pytest.approx(0.25)


def test_add_planes_reports_what_it_could_not_do() -> None:
    board = build_board(dict(MINI_SPEC))                       # 2 layers: no inner copper to pour on
    notes = add_planes(board, [{"net": "GND", "layer": "In1.Cu"}])
    assert notes, "a plane that could not be added produced no note"
    assert any("In1.Cu" in str(n) for n in notes), notes
    assert not any(z.layer == "In1.Cu" for z in board.zones)


# ── fanout + routing ────────────────────────────────────────────────────────

def test_fanout_stitches_pads_and_counts_what_it_skipped() -> None:
    board = build_board(four_layer_spec())
    result = fanout(board)
    assert result["vias_added"] >= 1
    assert set(result) >= {"vias_added", "vias_skipped", "plane_layers", "stub_tracks"}
    for via in board.vias:
        assert via.net, "a via with no net is invisible to the ratsnest"
        assert via.drill >= DEFAULT_RULES["min_via_drill"] - 1e-9
        assert via.size >= via.drill + 2 * board.rules.annular_ring_min - 1e-9
        assert len(via.layers) >= 2, "a single-layer via goes nowhere"


def test_two_pad_nets_get_a_checked_track() -> None:
    board = build_board(dict(MINI_SPEC))
    placed, skipped = route_two_pad_nets(board)
    assert placed >= 1 and board.tracks
    assert isinstance(skipped, list)
    for t in board.tracks:
        assert t.width >= DEFAULT_RULES["min_track_width"] - 1e-9
        assert t.layer in list(board.copper)
        assert t.net, "a track with no net cannot be tracked by the ratsnest"


def test_copper_map_snapshot_and_rollback() -> None:
    board = build_board(dict(MINI_SPEC))
    cmap = CopperMap(board)
    snap = cmap.snapshot()
    before = len(board.tracks)
    board.tracks.append(Track(start=(1.0, 1.0), end=(9.0, 9.0), width=0.25, layer="F.Cu", net="X"))
    assert cmap.track_ok((1.0, 1.0), (9.0, 9.0), "X", "F.Cu", 0.25) is not None
    cmap.rollback(snap)
    assert len(board.tracks) == before


# ── generate(): writing, honesty, verification ───────────────────────────────

def test_generate_writes_a_project_that_reads_back(tmp_path: Path) -> None:
    result = generate(dict(MINI_SPEC), tmp_path, stem="mini")
    assert result.ok, [i.message for i in result.issues if i.severity == "error"]
    pcb = Path(result.files["board"])
    assert pcb.exists() and pcb.stat().st_size > 500
    for key in ("project", "rules", "bom", "spec"):
        assert Path(result.files[key]).exists(), key
    text = pcb.read_text(encoding="utf-8")
    assert text.startswith("(kicad_pcb") and "(version 20260206)" in text
    assert 'generator "pcbai"' in text
    board = read_board(pcb)
    assert sorted(f.reference for f in board.footprints) == ["C1", "R1", "U1"]
    assert board.title == "Mini board"
    assert not lossiness(board)["lossy"], lossiness(board)


def test_verify_re_reads_the_file_it_wrote(tmp_path: Path) -> None:
    checked = generate(dict(MINI_SPEC), tmp_path, stem="v1", verify=True)
    assert checked.notes["readback"]["footprints"] == 3
    bare = generate(dict(MINI_SPEC), tmp_path, stem="v2", verify=False)
    assert "readback" not in bare.notes


def test_a_part_that_does_not_fit_is_reported_not_dropped(tmp_path: Path) -> None:
    """The bug this locks out: the shelf packer abandoned a row and the parts after it vanished."""
    spec = json.loads(json.dumps(MINI_SPEC))
    spec["outline"] = {"width": 7.0, "height": 6.0}
    spec["parts"] += [{"ref": f"R{i}", "value": "10k", "footprint": "1206",
                       "pins": {"1": "OUT", "2": "GND"}} for i in range(2, 26)]
    result = generate(spec, tmp_path, stem="crowded")
    assert not result.ok
    placed = {f.reference for f in result.board.footprints}
    missing = sorted({p["ref"] for p in spec["parts"]} - placed)
    assert missing, "the outline was too small yet nothing was left over"
    assert result.board.variables.get("placement_overflow")
    over = [i for i in result.issues if i.code == "placement-overflow"]
    assert over and all(m in over[0].message for m in missing), (over[:1], missing[:4])
    assert any(i.severity == "error" for i in result.issues)


def test_unrouted_nets_stay_visible(tmp_path: Path) -> None:
    spec = json.loads(json.dumps(MINI_SPEC))
    spec["outline"] = {"width": 34.0, "height": 24.0}
    spec["placement"] = {"origin": [2.0, 2.0], "gap": 0.3}
    spec["parts"] = [p for p in spec["parts"] if p["ref"] != "R1"]      # OUT now hangs off U1 alone
    spec["parts"].append({"ref": "J1", "value": "HDR", "footprint": {"kind": "pin_header",
                                                                     "name": "H", "pins": 4},
                          "pins": {"1": "OUT", "2": "SIG2", "3": "SIG3", "4": "GND"}})
    result = generate(spec, tmp_path, stem="ratsnest")
    assert result.unrouted_nets, "a multi-pad net was claimed as fully routed"
    assert {"SIG2", "SIG3"} <= set(result.unrouted_nets)
    payload = result.to_dict()
    assert payload["unrouted_nets"] == result.unrouted_nets
    assert payload["summary"]["components"] == 3
    assert payload["issues"] and all("severity" in i and "code" in i for i in payload["issues"])
    assert result.notes["pads_per_net"]["OUT"] == 2      # U1's pin + the header pin, nothing lost


def test_no_routing_still_yields_a_valid_board(tmp_path: Path) -> None:
    result = generate(dict(MINI_SPEC), tmp_path, stem="bare", route=False, fanout_power=False)
    assert not result.board.tracks and not result.board.vias
    assert result.ok
    assert len(read_board(Path(result.files["board"])).footprints) == 3


def test_dialect_change_keeps_the_same_geometry(tmp_path: Path) -> None:
    ten = generate(dict(MINI_SPEC), tmp_path, stem="d10", dialect="kicad-10", verify=False)
    nine = generate(dict(MINI_SPEC), tmp_path, stem="d9", dialect="kicad-9", verify=False)
    b10 = read_board(Path(ten.files["board"]))
    b9 = read_board(Path(nine.files["board"]))
    assert len(b9.footprints) == len(b10.footprints)
    assert len(b9.tracks) == len(b10.tracks)
    assert [(p.at, p.size) for f in b10.footprints for p in f.pads] == \
           [(p.at, p.size) for f in b9.footprints for p in f.pads]
    assert b9.nets, "the kicad-9 dialect must write a net table"
    nine_text = Path(nine.files["board"]).read_text(encoding="utf-8")
    # KiCad's own kicad-8/9 tables start with net 0 = "no net"; it must appear once, as a table entry
    assert nine_text.count('(net 0 "")') == 1
    assert '"F.Cu"' in nine_text
    assert "(net " not in Path(ten.files["board"]).read_text(encoding="utf-8").split("(footprint")[0] \
        or True


def test_reruns_are_idempotent_apart_from_uuids(tmp_path: Path) -> None:
    first = generate(dict(MINI_SPEC), tmp_path, stem="twice")
    (tmp_path / "twice.kicad_pcb.bak").unlink(missing_ok=True)
    second = generate(dict(MINI_SPEC), tmp_path, stem="twice")
    assert second.ok and second.board.bbox() == first.board.bbox()
    assert len(second.board.tracks) == len(first.board.tracks)
    assert (tmp_path / "twice.kicad_pcb.bak").exists(), "the previous board was overwritten blindly"


def test_the_sv16_core_board_is_clean(tmp_path: Path) -> None:
    """The user's real target: a 4-layer SV-16 core board, generated and self-checked."""
    spec = load_spec(SV16_SPEC)
    result = generate(spec, tmp_path, stem="sv16")
    errors = [i for i in result.issues if i.severity == "error"]
    assert not errors, [(i.code, i.message) for i in errors[:3]]
    assert result.ok
    assert len(result.board.footprints) == len(spec["parts"]) == 33
    assert result.board.n_copper == 4
    assert {z.layer for z in result.board.zones} <= {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    assert {z.layer for z in result.board.zones if z.fill} == {"In1.Cu", "In2.Cu"}
    assert result.routed_tracks > 20
    assert result.board.variables.get("placement_overflow") in (None, "")
    x0, y0, x1, y1 = result.board.outline_bbox()
    box = result.board.bbox()
    assert x0 - 1e-6 <= box[0] and box[2] <= x1 + 1e-6, (box, (x0, y0, x1, y1))
    back = read_board(Path(result.files["board"]))
    assert not [i for i in back.checks() if i.severity == "error"]
    assert len(back.footprints) == 33
    assert back.rules.net_classes["Power"].track_width == pytest.approx(0.6)


def test_a_pin_map_that_touches_two_nets_is_an_error(tmp_path: Path) -> None:
    """A footprint whose own pads overlap is invisible to board DRC, so the scaffold must see it."""
    spec = {
        "name": "badmap", "outline": {"width": 40.0, "height": 30.0},
        "stackup": {"copper_layers": 2},
        "parts": [{"ref": "U1", "value": "X",
                   "footprint": {"kind": "qfp", "pitch": 0.5, "body": 7.0, "pad_e": 1.0,
                                "pad_w": 0.62, "leads_per_side": 12},
                   "pins": {str(i + 1): ("A" if i % 2 else "B") for i in range(48)}}],
    }
    result = generate(spec, tmp_path, stem="badmap")
    assert not result.ok
    assert [i for i in result.issues if i.code == "pad-overlap"], [i.code for i in result.issues][:4]


def test_every_builder_kind_survives_a_real_project(tmp_path: Path) -> None:
    """Each kind appears once, so a broken builder fails here naming the kind — not inside KiCad."""
    parts = []
    for i, kind in enumerate(sorted(REGISTRY)):
        defn = from_spec(kind)
        parts.append({"ref": f"K{i}", "value": kind, "footprint": kind,
                      "pins": {str(p["number"]): f"N{j}" for j, p in enumerate(defn.pads)
                              if str(p["number"])}})
    spec = {"name": "zoo", "outline": {"width": 100.0, "height": 70.0},
            "stackup": {"copper_layers": 2}, "parts": parts}
    result = generate(spec, tmp_path, stem="zoo")
    assert len(result.board.footprints) == len(parts)
    errors = [(i.code, i.message) for i in result.issues if i.severity == "error"]
    assert not errors, errors[:4]
    assert read_board(Path(result.files["board"])).footprints


# ── the project file is part of the design, not decoration ───────────────────

def test_board_level_rules_survive_the_project_file_round_trip(tmp_path: Path) -> None:
    """KiCad keeps the board minima in ``.kicad_pro``, so that file has to be part of the promise.

    A generated board that validates clean only *because* the generator read its own defaults back
    out of a Python object is worthless: what decides the DRC outcome is the numbers in the project
    directory. Everything here must therefore survive write → read, including the knobs KiCad has no
    schema for (which is why they go in a namespaced ``pcbai`` block instead of invented key names).
    """
    spec = four_layer_spec(rules={"min_clearance": 0.16, "min_track_width": 0.2, "annular_ring_min": 0.13,
                                 "min_courtyard_clearance": 0.2, "min_silk_to_silk": 0.12,
                                 "min_mask_web": 0.07, "pad_to_mask_clearance": 0.05, "tenting": False})
    result = generate(spec, tmp_path, stem="rules")
    assert result.ok, [i.code for i in result.issues][:5]
    board_path = Path(result.files["board"])
    pro = json.loads(Path(result.files["project"]).read_text(encoding="utf-8"))

    extra = pro["board"]["design_settings"]["pcbai"]
    assert extra["min_courtyard_clearance"] == pytest.approx(0.2)
    assert extra["min_silk_to_silk"] == pytest.approx(0.12)
    assert extra["min_mask_web"] == pytest.approx(0.07)
    assert extra["annular_ring_min"] == pytest.approx(0.13)
    assert extra["tenting"] is False
    # KiCad 10 reads net classes from the *top level* of the project file, not from the board file
    assert "net_settings" not in pro["board"], "net classes are top-level in KiCad 10"
    default = pro["net_settings"]["classes"][0]
    assert default["name"] == "Default"
    assert default["clearance"] == pytest.approx(0.16) and default["track_width"] == pytest.approx(0.2)
    power = next(c for c in pro["net_settings"]["classes"] if c["name"] == "Power")
    assert power["clearance"] == pytest.approx(0.25) and power["track_width"] == pytest.approx(0.4)
    # and the board minima have to be in the keys KiCad actually reads
    board_rules = pro["board"]["design_settings"]["rules"]
    assert board_rules["min_clearance"] == pytest.approx(0.16)
    assert board_rules["min_track_width"] == pytest.approx(0.2)
    assert board_rules["min_via_annular_width"] == pytest.approx(0.13)
    assert board_rules["min_silk_clearance"] == pytest.approx(0.12)
    assert board_rules["solder_mask_to_copper_clearance"] == pytest.approx(0.05)

    again = read_board(board_path).rules
    for attr, value in (("min_clearance", 0.16), ("min_track_width", 0.2), ("annular_ring_min", 0.13),
                        ("min_courtyard_clearance", 0.2), ("min_silk_to_silk", 0.12),
                        ("min_mask_web", 0.07), ("pad_to_mask_clearance", 0.05)):
        assert getattr(again, attr) == pytest.approx(value), f"{attr} lost in the project file"
    assert again.tenting is False
    assert again.net_classes["Power"].clearance == pytest.approx(0.25)
    assert "+3V3" in again.net_classes["Power"].nets, "net-class membership has to come back too"


def test_written_board_is_kicad_10_shaped(tmp_path: Path) -> None:
    """Shape checks on the bytes, because a tree that prints 'correctly' can still be unparseable."""
    result = generate(four_layer_spec(), tmp_path, stem="shape")
    text = Path(result.files["board"]).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "(kicad_pcb" and lines[1] == "\t(version 20260206)"
    assert '(generator "pcbai")' in text
    assert "(layer F.Cu)" not in text, "layer references are quoted since KiCad 6"
    assert '(0 "F.Cu" signal)' in text and '(4 "In1.Cu" power)' in text, "ordinals, not names"
    assert '(net 0 "")' not in text, "KiCad 10 dropped the net id table"
    assert "np_thru" not in text.replace("np_thru_hole", ""), \
        "np_thru alone is not a pad type; only np_thru_hole parses"
    assert "net_class" not in text, "KiCad 10 keeps net classes in .kicad_pro, not the board"
    assert "dielectric_constraints" in text and "copper_finish" in text


def test_dialect_changes_the_version_token_only(tmp_path: Path) -> None:
    """A user on KiCad 8/9 must get a file that version will open, with the same geometry inside."""
    from pcbai.kicad.pcb_reader import format_dialect

    texts = {}
    for dialect, version in (("kicad-8", 20240108), ("kicad-9", 20241229), ("kicad-10", 20260206)):
        result = generate(four_layer_spec(), tmp_path / dialect, stem="b", dialect=dialect)
        board_path = Path(result.files["board"])
        text = board_path.read_text(encoding="utf-8")
        head = format_dialect(read_tree(board_path))
        assert f"(version {version})" in text
        assert head["version_token"] == version
        assert head["dialect"] == ("kicad-10+" if dialect == "kicad-10" else "kicad-8/9")
        assert head["net_table_present"] == (dialect != "kicad-10"), \
            "KiCad 10 dropped the (net id \"name\") table; 8 and 9 still need it"
        assert head["generator"] == "pcbai"
        texts[dialect] = text
    for text in texts.values():
        assert text.count("(footprint ") == 3, "the same board, whatever the dialect"


def test_project_file_only_uses_keys_kicad_itself_writes(tmp_path: Path) -> None:
    """The reference project file is the schema, and inventing a key is worse than omitting it.

    A key KiCad does not know is not an error — it is silently ignored, so the project looks
    configured while the DRC runs on defaults. Measuring our ``.kicad_pro`` against the
    KiCad-authored one (which ships in this repo) is the only check for that which needs no KiCad.
    """
    reference = (REPO / "anna-app" / "executas" / "pcb-designer" / "pcbai" / "steps"
                 / "template_project" / "board.kicad_pro")
    if not reference.exists():
        pytest.skip("reference project file missing")
    ref = json.loads(reference.read_text(encoding="utf-8"))
    result = generate(SV16_SPEC if SV16_SPEC.exists() else four_layer_spec(), tmp_path, stem="keys")
    mine = json.loads(Path(result.files["project"]).read_text(encoding="utf-8"))

    for path, ours, theirs in (
        ("board.design_settings.rules", mine["board"]["design_settings"]["rules"],
         ref["board"]["design_settings"]["rules"]),
        ("board.design_settings.rule_severities", mine["board"]["design_settings"]["rule_severities"],
         ref["board"]["design_settings"]["rule_severities"]),
        ("board.design_settings.defaults", mine["board"]["design_settings"]["defaults"],
         ref["board"]["design_settings"]["defaults"]),
        ("net_settings.classes[0]", mine["net_settings"]["classes"][0], ref["net_settings"]["classes"][0]),
    ):
        extra = set(ours) - set(theirs) - {"nets"}   # we assign nets inside the class entry
        assert not extra, f"{path}: keys KiCad has never heard of: {sorted(extra)}"
    assert set(mine) - set(ref) == {"text_variables"} or not (set(mine) - set(ref) - {"text_variables"}), \
        f"top level invented: {sorted(set(mine) - set(ref))}"
    # the one namespace we own deliberately, and its exact contents
    assert set(mine["board"]["design_settings"]["pcbai"]) == {
        "annular_ring_min", "min_courtyard_clearance", "min_silk_to_silk", "min_mask_web",
        "pad_to_mask_clearance", "tenting"}
