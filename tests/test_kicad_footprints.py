"""Footprint builders: geometry KiCad accepts and a fab can actually build.

These tests exist because the generator once wrote a QFP whose pads were rotated *into* the row
direction: every pad touched its neighbour, the board checker stayed silent (KiCad does not compare a
part with itself), and KiCad would have loaded it as one blob of copper. So the builders are judged by
the footprint-level checker here, at the source, rather than downstream.
"""

from __future__ import annotations

import pytest

from pcbai.kicad.footprints import (REGISTRY, FootprintDef, UnknownFootprintKind, crystal, from_spec,
                                   lqfp, mounting_hole, passive, pin_header, qfp, soic, sot23,
                                   sot23_5, tact, tqfp, usb_c)
from pcbai.kicad.sexp import dumps, parse

# every builder is exercised with a pin map that changes nets as it goes, which is the worst case for
# spacing: same-net neighbours would hide an overlap behind the "one net, one shape" rule
ALL_KINDS = sorted(REGISTRY)

PASSIVE_SIZES = {"0402": ((0.5, 0.6), 0.475), "0603": ((0.8, 0.95), 0.8),
                 "0805": ((1.0, 1.2), 1.025), "1206": ((1.2, 1.8), 1.4)}


def nets_for(defn: FootprintDef) -> dict:
    """Worst-case net map: neighbours differ, except the pins a real part ties to ground.

    USB-C's shell pads and its two GND pins *are* one net in the datasheet; pretending they differ
    would report a 0.035 mm "gap" that is intended to be fused copper.
    """
    out = {}
    for i, p in enumerate(defn.pads):
        num = str(p["number"])
        if num.startswith("S") or num.endswith("12") or i % 3 == 0:
            out[num] = "GND"
        else:
            out[num] = f"N{i}"
    return out


def test_every_registry_kind_builds_clean_copper() -> None:
    assert ALL_KINDS, "the footprint registry went empty"
    for kind in ALL_KINDS:
        defn = from_spec(kind)
        issues = defn.check(nets_for(defn))
        assert not [i for i in issues if i["severity"] == "error"], (kind, issues[:2])
        assert not [i for i in issues if i["code"] in ("pad-gap-tiny", "drill-on-smd-pad")], \
            (kind, issues[:2])
        assert defn.pads, kind


def test_every_footprint_text_parses_and_reprints_identically() -> None:
    for kind in ALL_KINDS:
        text = from_spec(kind).to_mod_text()
        tree = parse(text)
        assert tree.head == "footprint", (kind, tree.head)
        assert dumps(tree) == text, kind
        assert dumps(parse(dumps(tree))) == text, kind


def test_lqfp48_matches_the_package_drawing() -> None:
    defn = lqfp()
    assert len(defn.pads) == 48
    xs = [p["at"][0] for p in defn.pads]
    assert max(xs) == pytest.approx(4.5) and min(xs) == pytest.approx(-4.5)     # 7 mm body + 1 mm leads
    left = sorted((p["at"][1] for p in defn.pads if abs(p["at"][0] + 4.5) < 1e-6), reverse=True)
    assert len(left) == 12
    assert max(abs(left[i] - left[i + 1]) for i in range(len(left) - 1)) == pytest.approx(0.5)
    assert min(left) == pytest.approx(-2.75) and max(left) == pytest.approx(2.75)
    from pcbai.kicad.model import _obb_corners
    inst = defn.instance("U1", "X", x=0.0, y=0.0, pad_nets=nets_for(defn))
    tips = [abs(c[0]) for p in inst.pads for c in p.corners(inst)]      # copper only, no graphics
    assert max(tips) == pytest.approx(5.0)                    # lead tips 5.0 mm out, per the drawing
    assert max(abs(v) for v in inst.bbox()) >= 5.2            # courtyard is outside the copper
    assert all(_obb_corners(p.rect_local())[0] for p in inst.pads)
    assert [p["number"] for p in defn.pads[:2]] == ["1", "2"]
    p1 = defn.pads[0]
    assert p1["size"] == (0.45, 1.0), "row-direction width first, radial length second"
    assert p1["type"] == "smd" and "F.Paste" in p1["layers"] and "F.Mask" in p1["layers"]
    assert defn.check(nets_for(defn)) == []


def test_qfp_pads_do_not_run_into_each_other_at_any_pitch() -> None:
    """The bug this locks out: pad size order vs the side rotation fusing a whole pad row."""
    for pitch, pad_w, body in ((0.4, 0.35, 7.0), (0.5, 0.45, 7.0), (0.65, 0.6, 9.0), (0.8, 0.7, 10.0)):
        n = int(body / pitch)                                     # as many leads as the edge takes
        defn = qfp(f"Q{pitch}", pitch=pitch, body=body, pad_e=1.0, pad_w=pad_w, leads_per_side=n)
        codes = [i["code"] for i in defn.check(nets_for(defn))]
        assert "pad-overlap" not in codes and "pad-gap-tiny" not in codes, (pitch, codes[:3])


def test_a_lead_row_that_cannot_fit_the_body_is_refused() -> None:
    """12 leads at 0.8 mm need 9.5 mm of a 7 mm body edge: the corners would wrap and short."""
    with pytest.raises(ValueError, match="do not fit|need .* of edge"):
        qfp("NOPE", pitch=0.8, body=7.0, pad_e=1.0, pad_w=0.7, leads_per_side=12)
    with pytest.raises(ValueError, match="positive pitch"):
        qfp("NOPE2", pitch=0.0, body=7.0, pad_e=1.0, pad_w=0.45, leads_per_side=0)


def test_a_pad_stack_too_fat_for_its_pitch_is_an_error() -> None:
    defn = qfp("TOO_FAT", pitch=0.5, body=7.0, pad_e=1.0, pad_w=0.62, leads_per_side=12)
    issues = defn.check(nets_for(defn))
    assert [i for i in issues if i["code"] == "pad-overlap"], issues[:2]
    assert all(i["severity"] == "error" for i in issues if i["code"] == "pad-overlap")


def test_passive_pads_match_the_ipc_sizes() -> None:
    for size, (pad_size, offset) in PASSIVE_SIZES.items():
        defn = passive(size)
        assert len(defn.pads) == 2
        assert defn.pads[0]["size"] == pad_size, size
        assert defn.pads[0]["at"] == pytest.approx((-offset, 0.0, 0.0)), size
        assert defn.pads[1]["at"][0] == pytest.approx(offset), size
        assert "F.Paste" in defn.pads[0]["layers"]
        assert defn.check({"1": "A", "2": "B"}) == []


def test_tht_parts_have_a_drill_and_no_paste() -> None:
    header = pin_header("HDR-2", pins=2)
    assert header.pads[0]["type"] == "thru_hole"
    assert header.pads[0]["drill"] == pytest.approx(1.0)
    assert "F.Paste" not in header.pads[0]["layers"]
    assert header.check({"1": "A", "2": "B"}) == []
    assert len(pin_header(pins=8).pads) == 8
    ys = [p["at"][1] for p in pin_header(pins=3).pads]
    assert abs(ys[0] - ys[1]) == pytest.approx(2.54) and abs(ys[1] - ys[2]) == pytest.approx(2.54)


def test_mounting_hole_is_copper_free_but_spelled_the_kicad_way() -> None:
    """`np_thru` is a common shorthand KiCad's parser rejects; only `np_thru_hole` loads."""
    hole = mounting_hole()
    pad = hole.pads[0]
    assert pad["type"] == "np_thru_hole" and pad["number"] == ""
    assert "np_thru circle" not in hole.to_mod_text()
    assert pad["drill"] == pytest.approx(3.2)
    assert pad["layers"] == ("F.Cu", "B.Cu")     # no annular ring, but KiCad still wants the layers
    assert hole.check({"": "GND"}) == []


def test_two_sided_and_specialised_builders() -> None:
    assert len(soic().pads) == 8 and soic().check(nets_for(soic())) == []
    assert [p["at"][0] < 0 for p in sot23().pads[:2]] == [True, True]
    assert sot23().pads[2]["at"][0] > 0 and sot23_5().check(nets_for(sot23_5())) == []
    assert len(tqfp().pads) == 48 and len(usb_c().pads) == 28
    assert len(from_spec({"kind": "dfn", "pads": 8}).pads) == 9   # 4 + 4 + exposed pad "9"
    assert len(crystal().pads) == 2 and crystal().check({"1": "OSC_IN", "2": "OSC_OUT"}) == []
    assert {p["number"] for p in tact().pads} == {"1", "2", "3", "4", "SH1", "SH2"}


def test_courtyard_clears_the_copper_on_every_kind() -> None:
    """A courtyard inside the pads is a fab/assembly problem KiCad will not tell you at write time."""
    for kind in ALL_KINDS:
        defn = from_spec(kind)
        inst = defn.instance("X1", "TEST", x=0.0, y=0.0, pad_nets=nets_for(defn))
        pads = inst.bbox()
        court = [g for g in defn.graphics if g.get("layer") == "F.CrtYd"]
        assert court, f"{kind} has no courtyard at all"
        pts = [v for g in court for v in (g.get("start") or (), g.get("end") or ()) if v]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        assert min(xs) <= pads[0] + 1e-9 and max(xs) >= pads[2] - 1e-9, (kind, min(xs), pads)
        assert min(ys) <= pads[1] + 1e-9 and max(ys) >= pads[3] - 1e-9, (kind, min(ys), pads)


def test_from_spec_accepts_shorthand_and_rejects_junk() -> None:
    assert from_spec("0603").pads[0]["size"] == (0.8, 0.95)
    assert len(from_spec({"kind": "lqfp", "pad_w": 0.5, "leads_per_side": 12}).pads) == 48
    assert from_spec("0402").name != from_spec("1206").name
    with pytest.raises(UnknownFootprintKind, match="unknown footprint kind"):
        from_spec({"kind": "bga1234"})
    with pytest.raises(ValueError, match="mapping or a kind string"):
        from_spec(42)                       # type: ignore[arg-type]
    with pytest.raises(KeyError):            # the old caller contract still holds
        from_spec({"kind": "nope"})
    with pytest.raises(TypeError):           # a bad constructor argument is *not* swallowed
        from_spec({"kind": "lqfp", "not_a_real_option": 1})


def test_names_say_what_the_pads_are() -> None:
    """A footprint id is what a human reads in KiCad; a default that lies about the part is a trap."""
    assert from_spec("0603").name == "C_0603"
    assert lqfp().name == "LQFP48" and tqfp().name == "TQFP48"
    assert len(lqfp().pads) == len(tqfp().pads) == 48
    assert from_spec({"kind": "lqfp", "name": "SV16_LQFP48_7x7_P0.5"}).name == "SV16_LQFP48_7x7_P0.5"
    assert '"' not in lqfp().name and " " not in lqfp().name     # quoted field in the .kicad_mod


def test_instance_places_pads_and_carries_properties() -> None:
    defn = soic()
    inst = defn.instance("U7", "OPA", x=12.0, y=8.0, rot=90.0, pad_nets={"1": "GND", "8": "+3V3"})
    by_number = {p.number: p for p in inst.pads}
    assert inst.reference == "U7" and inst.rot == pytest.approx(90.0)
    assert by_number["1"].net == "GND" and by_number["8"].net == "+3V3"
    assert inst.properties == {}                    # Reference/Value live in their own fields
    assert '"Reference" "U7"' in dumps(defn.to_sexp(reference="U7", value="OPA"))
    cx, cy, rot = by_number["1"].absolute(inst)
    assert rot == pytest.approx(by_number["1"].at[2] + 90.0)
    assert (round(cx, 3), round(cy, 3)) != (12.0, 8.0)      # rotated, so the pad is off the origin
    assert inst.uuid and inst.uuid != soic().instance("U8", "", x=0, y=0).uuid


def test_sexp_form_can_be_edited_and_stays_printable() -> None:
    tree = passive("0603").to_sexp(reference="C9", value="100n")
    text = dumps(tree)
    assert '(property "Reference" "C9"' in text and '(property "Value" "100n"' in text
    pad = tree.child("pad")
    assert pad.child("size").get(1).num() == pytest.approx(0.8)
    assert pad.child("size").get(2).num() == pytest.approx(0.95)
    assert pad.replace_child("size", parse("(size 0.9 1.05)"))
    assert parse(dumps(tree)).child("pad").child("size").get(1).num() == pytest.approx(0.9)
