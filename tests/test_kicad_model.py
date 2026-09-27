"""The board model: geometry, the rule matrix, and the self-checks an agent must be able to trust.

A generator is only useful if its own validator catches its mistakes; these tests deliberately build
*broken* boards and assert both directions — the violation is found, and a legal board is clean.
"""
from __future__ import annotations

import math

import pytest

from pcbai.kicad.footprints import FootprintDef, passive, pin_header, qfp
from pcbai.kicad.model import (Board, DesignRules, Footprint, Graphic, Issue, Net, NetClass, Pad,
                               Track, Via, Zone, _obb_gap, _seg_seg_distance, new_uuid)


def pad(number: str, net: str, x: float, y: float, *, size=(0.6, 0.6), type_="smd",
        drill=None, layers=("F.Cu", "F.Paste", "F.Mask")) -> Pad:
    return Pad(number=number, type=type_, shape="rect", at=(x, y, 0.0), size=size, drill=drill,
               layers=layers, net=net)


def fp(ref: str, *pads: Pad, x: float = 0.0, y: float = 0.0, layer: str = "F.Cu") -> Footprint:
    return Footprint(lib_id="pcbai:Test", reference=ref, value=ref, x=x, y=y, layer=layer,
                     pads=list(pads))


def board_with(*footprints: Footprint, width: float = 20.0, height: float = 12.0) -> Board:
    board = Board(title="unit test")
    board.set_outline_rect(width, height)
    for pad_def in footprints:
        board.add_footprint(pad_def)
    for net in sorted({p.net for f in footprints for p in f.pads if p.net}):
        board.net(net)
    return board


def codes(issues) -> dict:
    out: dict = {}
    for issue in issues:
        out[issue.code] = out.get(issue.code, 0) + 1
    return out


# ── construction ─────────────────────────────────────────────────────────────
def test_copper_list_is_derived_from_the_layer_count() -> None:
    board = Board(n_copper=4)
    assert board.copper == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert len(board.checks()) == 0 or all(i.code != "stackup-count" for i in board.checks())


def test_a_two_layer_board_flagged_when_the_lists_disagree() -> None:
    board = Board(n_copper=4)
    board.copper = ["F.Cu", "B.Cu"]           # hand-edited: exactly the bug the check exists for
    assert "stackup-count" in codes(board.checks())


def test_odd_layer_counts_are_rejected() -> None:
    board = Board(n_copper=3, copper=["F.Cu", "In1.Cu", "B.Cu"])
    assert "stackup-parity" in codes(board.checks())


def test_board_needs_a_closed_outline() -> None:
    board = Board(title="no edge")
    found = codes(board.checks())
    assert "no-outline" in found
    board.set_outline_rect(10, 10)
    assert "no-outline" not in codes(board.checks())
    # now break it: an open chain must be reported
    board.edge = [Graphic("line", "Edge.Cuts", start=(0, 0), end=(10, 0)),
                  Graphic("line", "Edge.Cuts", start=(10, 0), end=(10, 10))]
    assert "outline-open" in codes(board.checks())


def test_outline_bbox_follows_the_edge_not_the_copper() -> None:
    board = board_with(fp("U1", pad("1", "GND", 40.0, 40.0)), width=20.0, height=12.0)
    assert board.outline_bbox() == pytest.approx((0.0, 0.0, 20.0, 12.0))
    assert board.bbox()[2] > 39.0          # the stray part grows bbox() but not the outline


def test_outline_rings_close_the_polygon() -> None:
    board = board_with()
    rings = board.outline_rings()
    assert len(rings) == 1 and len(rings[0]) >= 4
    xs = [p[0] for p in rings[0]]
    assert min(xs) == pytest.approx(0.0) and max(xs) == pytest.approx(20.0)


# ── clearance / shorts ───────────────────────────────────────────────────────
def test_pads_of_different_nets_touching_across_two_parts_is_a_short() -> None:
    board = board_with(fp("R1", pad("1", "A", 5.0, 5.0)), fp("R2", pad("1", "B", 5.2, 5.0)))
    issues = [i for i in board.checks() if i.code == "pad-clearance"]
    assert len(issues) == 1 and issues[0].severity == "error"
    assert "-0.400mm" in issues[0].message           # 0.4 mm of shared copper, from a 0.2 mm offset


def test_pads_inside_one_footprint_are_the_footprints_job_not_the_boards() -> None:
    """KiCad's board DRC never compares a part with itself (net ties exist); ``Footprint.check`` does.

    Without that split every 0.5 mm QFP would report its own pad row as a short — and the noise would
    teach everyone to ignore the real finding, which is a footprint with *overlapping* pads.
    """
    definition = FootprintDef("pcbai_bad:Tight", pads=[
        {"number": "1", "type": "smd", "shape": "rect", "at": (-0.45, 0.0, 0.0), "size": (1.0, 0.6),
         "layers": ("F.Cu", "F.Paste", "F.Mask")},
        {"number": "2", "type": "smd", "shape": "rect", "at": (0.45, 0.0, 0.0), "size": (1.0, 0.6),
         "layers": ("F.Cu", "F.Paste", "F.Mask")}])
    instance = definition.instance("C1", "100n", x=5.0, y=5.0, pad_nets={"1": "A", "2": "B"})
    board = board_with(instance)
    assert "pad-clearance" not in codes(board.checks())        # board level: silent, like KiCad
    assert sum(1 for d in definition.check({"1": "A", "2": "B"}) if d["code"] == "pad-overlap") == 1
    issues = instance.check()
    assert issues and issues[0].code == "pad-overlap" and "C1" in issues[0].message
    # a 0.5 mm-pitch QFP row is *meant* to be 0.05 mm apart: the footprint check stays quiet
    qfp_inst = qfp("Q", pitch=0.5, body=7.0, pad_e=1.0, pad_w=0.45,
                   leads_per_side=12).instance("U1", "SV16", x=0, y=0,
                                              pad_nets={str(i + 1): f"N{i}" for i in range(48)})
    assert qfp_inst.check() == []


def test_the_same_net_touching_itself_is_fine() -> None:
    board = board_with(fp("R1", pad("1", "A", 5.0, 5.0), pad("2", "A", 5.2, 5.0)))
    assert "pad-clearance" not in codes(board.checks())


def test_courtyard_overlap_is_reported_once_per_pair() -> None:
    a = passive("0603").instance("C1", "100n", x=5.0, y=5.0)
    b = passive("0603").instance("C2", "100n", x=5.2, y=5.0)
    board = board_with(a, b)
    assert codes(board.checks()).get("courtyard-overlap") == 1


def test_part_outside_the_outline_is_an_error() -> None:
    board = board_with(fp("U1", pad("1", "GND", 100.0, 5.0)))
    assert "pad-outside-outline" in codes(board.checks())


def test_track_too_thin_for_its_class() -> None:
    board = board_with(fp("R1", pad("1", "PWR", 5.0, 5.0)), fp("R2", pad("1", "PWR", 8.0, 5.0)))
    board.rules.net_classes["Power"] = NetClass("Power", clearance=0.25, track_width=0.6,
                                                nets=["PWR"])
    board.tracks.append(Track(start=(5.3, 5.0), end=(7.7, 5.0), net="PWR", width=0.2, layer="F.Cu"))
    assert "track-too-thin" in codes(board.checks())
    board.tracks[0].width = 0.6
    assert "track-too-thin" not in codes(board.checks())


def test_track_crossing_another_nets_pad_is_found() -> None:
    board = board_with(fp("R1", pad("1", "A", 4.0, 5.0)), fp("R2", pad("1", "B", 8.0, 5.0)))
    board.tracks.append(Track(start=(0.0, 5.0), end=(12.0, 5.0), net="A", width=0.2, layer="F.Cu"))
    assert "pad-clearance" in codes(board.checks())


def test_clearance_is_per_layer() -> None:
    """A bottom-layer track under a top-only pad is legal; the same track on F.Cu is not."""
    board = board_with(fp("R1", pad("1", "A", 4.0, 5.0)),
                       fp("R2", pad("1", "B", 8.0, 5.0, layers=("F.Cu", "F.Paste", "F.Mask"))))
    below = Track(start=(0.0, 5.0), end=(12.0, 5.0), net="A", width=0.2, layer="B.Cu")
    board.tracks.append(below)
    assert "pad-clearance" not in codes(board.checks())
    below.layer = "F.Cu"
    assert "pad-clearance" in codes(board.checks())


def test_net_class_clearance_is_the_larger_of_the_two_classes() -> None:
    board = board_with(fp("R1", pad("1", "SLOW", 4.0, 5.0, size=(0.4, 0.4))),
                       fp("R2", pad("1", "FAST", 4.65, 5.0, size=(0.4, 0.4))))
    board.rules.net_classes["Power"] = NetClass("Power", clearance=0.4, track_width=0.6,
                                                nets=["SLOW"])
    board.rules.min_clearance = 0.127
    gap = 4.65 - 0.2 - (4.0 + 0.2)                       # 0.25 mm edge to edge
    assert gap == pytest.approx(0.25)
    issues = [i for i in board.checks() if i.code == "pad-clearance"]
    assert issues and "0.400" in issues[0].message        # the Power class wins


def test_segment_distance_helper_agrees_with_pythagoras() -> None:
    assert _seg_seg_distance((0, 0), (10, 0), (0, 3), (10, 3)) == pytest.approx(3.0)
    assert _seg_seg_distance((0, 0), (0, 10), (5, 5), (10, 5)) == pytest.approx(5.0)
    # crossing segments are 0 apart
    assert _seg_seg_distance((0, 0), (10, 10), (0, 10), (10, 0)) == pytest.approx(0.0)


# ── pads, vias, zones ────────────────────────────────────────────────────────
def test_tht_pad_without_drill_is_rejected() -> None:
    bad = Pad(number="1", type="thru_hole", shape="circle", at=(5, 5, 0), size=(1.7, 1.7),
              drill=None, layers=("F.Cu", "B.Cu"), net="GND")
    board = board_with(fp("J1", bad))
    assert "tht-no-drill" in codes(board.checks())


def test_invented_pad_types_are_rejected() -> None:
    """KiCad's parser refuses unknown pad tokens, so the model must not accept them either."""
    weird = Pad(number="1", type="through_hole", shape="circle", at=(5, 5, 0), size=(1.7, 1.7),
                drill=(1.0, 1.0), layers=("F.Cu", "B.Cu"), net="GND")
    board = board_with(fp("J1", weird))
    assert "pad-type" in codes(board.checks())


def test_thin_annular_ring_is_flagged_against_the_boards_own_rule() -> None:
    tight = Pad(number="1", type="thru_hole", shape="circle", at=(5, 5, 0), size=(1.05, 1.05),
                drill=(0.8, 0.8), layers=("F.Cu", "B.Cu"), net="GND")
    board = board_with(fp("J1", tight))
    assert "annular-ring" not in codes(board.checks())         # 0.125 mm ring beats the 0.10 default
    board.rules.annular_ring_min = 0.15                        # a tighter fab, same board
    assert "annular-ring" in codes(board.checks())


def test_via_rules() -> None:
    board = board_with(fp("R1", pad("1", "A", 5.0, 5.0)))
    board.vias.append(Via(at=(6.0, 6.0), net="A", size=0.5, drill=0.2, layers=("F.Cu", "B.Cu")))
    found = codes(board.checks())
    assert found.get("via-drill-too-small") == 1               # 0.2 < 0.3
    board.rules.annular_ring_min = 0.2
    assert "via-ring-too-thin" in codes(board.checks())       # ring 0.15 < 0.20
    board.vias[0] = Via(at=(6.0, 6.0), net="A", size=0.6, drill=0.3, layers=("F.Cu", "In9.Cu"))
    assert "via-layer-missing" in codes(board.checks())


def test_zone_needs_a_closed_outline_and_a_known_layer() -> None:
    board = board_with()
    board.zones.append(Zone(net="GND", layer="In1.Cu", outline=[(1, 1), (2, 1)]))
    found = codes(board.checks())
    assert found.get("zone-outline") == 1
    board.zones[0].outline = [(1, 1), (19, 1), (19, 11), (1, 11)]
    assert "zone-outline" not in codes(board.checks())
    board.zones[0].layer = "In7.Cu"          # not in a 2-layer stackup
    assert any(i.severity == "error" and "layer" in i.code for i in board.checks())


def test_keepout_zone_needs_no_net_and_pours_do() -> None:
    board = board_with()
    ring = [(1, 1), (19, 1), (19, 11), (1, 11)]
    board.zones.append(Zone(net="", layer="In1.Cu", outline=ring, fill=False, keepout=True,
                            hatch_style="full"))
    board.zones.append(Zone(net="GND", layer="In1.Cu", outline=ring, fill=True))
    errs = [i for i in board.checks() if i.severity == "error"]
    assert not any("keepout" in i.message for i in errs)


# ── nets ─────────────────────────────────────────────────────────────────────
def test_single_pad_and_empty_nets_are_warnings_not_errors() -> None:
    board = board_with(fp("R1", pad("1", "LONELY", 5.0, 5.0)))
    board.net("NEVER_TOUCHED")
    issues = board.checks()
    assert "single-pad-net" in codes(issues)
    assert "empty-net" in codes(issues)
    assert not any(i.severity == "error" for i in issues)


def test_a_net_table_that_disagrees_with_the_pads_is_an_error() -> None:
    """KiCad 10 derives nets from pads, so only a board that *has* a table can be wrong about one."""
    board = Board(title="kicad-8 style net table")
    board.set_outline_rect(20, 12)
    board.add_footprint(fp("R1", pad("1", "MYSTERY", 5.0, 5.0)))
    assert "undeclared-net" not in codes(board.checks())       # add_footprint keeps the table in step
    board.nets = [Net("OTHER_NET")]                            # a table that has lost the entry
    board._net_index = {"OTHER_NET": 0}
    assert "undeclared-net" in codes(board.checks())
    board.nets.append(Net("MYSTERY"))
    board._net_index["MYSTERY"] = 1
    assert "undeclared-net" not in codes(board.checks())


def test_duplicate_references_are_caught() -> None:
    board = board_with(fp("R1", pad("1", "A", 4.0, 5.0)), fp("R1", pad("1", "A", 9.0, 5.0)))
    assert "duplicate-reference" in codes(board.checks())


def test_duplicate_uuids_are_caught() -> None:
    a, b = fp("R1", pad("1", "A", 4.0, 5.0)), fp("R2", pad("1", "A", 9.0, 5.0))
    b.uuid = a.uuid = new_uuid()
    assert "duplicate-uuid" in codes(board_with(a, b).checks())


# ── rules helpers ────────────────────────────────────────────────────────────
def test_clearance_for_falls_back_to_default() -> None:
    rules = DesignRules(min_clearance=0.2, min_track_width=0.2)
    rules.net_classes["Power"] = NetClass("Power", clearance=0.4, track_width=0.8, nets=["+3V3"])
    assert rules.clearance_for("+3V3").name == "Power"
    assert rules.clearance_for("SOME_SIGNAL").name == "Default"
    assert rules.clearance_for("SOME_SIGNAL").clearance == pytest.approx(0.2)


def test_summary_and_report_shapes_are_stable() -> None:
    board = board_with(fp("R1", pad("1", "A", 4.0, 5.0), pad("2", "A", 6.0, 5.0)))
    summary = board.summary()
    for key in ("title", "copper_layers", "components", "pads", "tracks", "vias", "zones",
                "board_size_mm", "design_rules"):
        assert key in summary, key
    report = board.report()
    assert set(report) >= {"counts", "issues"}
    assert report["counts"]["error"] == 0
    assert all(set(i) >= {"severity", "code", "message"} for i in report["issues"])


def test_to_json_from_json_round_trip_keeps_the_verdict() -> None:
    board = board_with(fp("R1", pad("1", "A", 4.0, 5.0), pad("2", "A", 6.0, 5.0)))
    board.tracks.append(Track(start=(4.3, 5.0), end=(5.7, 5.0), net="A", width=0.25, layer="F.Cu"))
    board.vias.append(Via(at=(5.0, 6.0), net="A", size=0.6, drill=0.3, layers=("F.Cu", "B.Cu")))
    board.zones.append(Zone(net="A", layer="In1.Cu", outline=[(1, 1), (19, 1), (19, 11), (1, 11)]))
    clone = Board.from_json(board.to_json())
    assert clone.summary() == board.summary()
    assert [i.code for i in clone.checks()] == [i.code for i in board.checks()]
    assert len(list(clone.all_pads())) == 2


def test_issue_to_dict_omits_empty_optionals() -> None:
    assert Issue("warning", "code-x", "msg").to_dict() == {"severity": "warning", "code": "code-x",
                                                           "message": "msg"}
    assert Issue("error", "c", "m", "U1", "do this").to_dict()["where"] == "U1"


def test_layer_missing_helper_knows_the_stackup() -> None:
    board = Board(n_copper=4)
    board.copper = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert not board.layer_missing("In2.Cu")
    assert board.layer_missing("In5.Cu")
    assert not board.layer_missing("Edge.Cuts")


@pytest.mark.parametrize("n,expect", [(2, ["F.Cu", "B.Cu"]),
                                      (4, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])])
def test_footprint_and_net_helpers_see_the_same_copper(n: int, expect: list) -> None:
    board = Board(n_copper=n)
    assert board.copper == expect


def test_real_footprints_instantiate_with_their_pad_nets() -> None:
    definition = qfp("QFP48", pitch=0.5, body=7.0, pad_e=1.0, pad_w=0.45, leads_per_side=12)
    pins = {str(i + 1): f"N{i}" for i in range(48)}
    part = definition.instance("U1", "SV16", x=10.0, y=10.0, pad_nets=pins)
    assert len(part.pads) == 48
    assert part.pad_nets()[:2] == ["N0", "N1"]
    header = pin_header(pins=4).instance("J1", "HDR", x=2.0, y=2.0,
                                        pad_nets={"1": "A", "2": "B", "3": "B", "4": "C"})
    board = board_with(part, header)
    assert "undeclared-net" not in codes(board.checks())


def test_geometry_math_is_finite_on_degenerate_input() -> None:
    assert _seg_seg_distance((0, 0), (0, 0), (1, 1), (1, 1)) == pytest.approx(math.sqrt(2))


def test_obb_gap_is_exact_where_bounding_boxes_are_not() -> None:
    """A 45°-rotated pad's axis-aligned box is a diamond's shell: using it invents shorts."""
    # axis-aligned pair: the answer is the arithmetic everyone would do by hand
    assert _obb_gap((0, 0, 1.0, 0.45, 0.0), (1.25, 0, 1.0, 0.45, 0.0)) == pytest.approx(0.25)
    assert _obb_gap((0, 0, 1.0, 0.45, 0.0), (0.7, 0, 1.0, 0.45, 0.0)) == pytest.approx(-0.3,
                                                                                        abs=1e-6)
    assert _obb_gap((0, 0, 1.0, 0.45, 0.0), (0, 0, 1.0, 0.45, 0.0)) == pytest.approx(-0.45, abs=1e-6)
    # rotated pair, offset along the pads' *width*: bounding boxes say they touch, truth is 0.87
    assert _obb_gap((0, 0, 2.0, 0.4, 45.0), (-0.9, 0.9, 2.0, 0.4, 45.0)) == pytest.approx(0.873,
                                                                                           abs=1e-2)
    assert _obb_gap((0, 0, 2.0, 0.4, 45.0), (-0.9, 0.9, 2.0, 0.4, 45.0)) > 0.2


def test_rotated_footprints_do_not_invent_clearance_violations() -> None:
    """A part placed at 45° must not be judged by the box around it — the bug that made this project's
    generated boards look short everywhere."""
    from pcbai.kicad.model import _rect_gap
    long_pad = {"number": "1", "type": "smd", "shape": "rect", "at": (0.0, 0.0, 45.0),
                "size": (2.0, 0.4), "layers": ("F.Cu",)}
    other = dict(long_pad, number="2")
    a = FootprintDef("pcbai_t:A", pads=[long_pad]).instance("TP1", "a", x=10.0, y=10.0, rot=0.0,
                                                            pad_nets={"1": "A"})
    b = FootprintDef("pcbai_t:B", pads=[other]).instance("TP2", "b", x=9.1, y=10.9, rot=0.0,
                                                          pad_nets={"2": "B"})
    board = board_with(a, b)
    assert _rect_gap(a.pads[0].extent(a), b.pads[0].extent(b)) < 0.0      # what the box test claimed
    assert "pad-clearance" not in codes(board.checks())                  # what the copper does
    b.x, b.y = 10.0, 10.0                                                 # now they really do touch
    assert "pad-clearance" in codes(board_with(a, b).checks())
