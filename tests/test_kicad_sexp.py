"""S-expression reader/writer: the guarantee is *byte-exact* replay of what KiCad wrote.

These tests are why :mod:`pcbai.kicad` may claim to edit real KiCad files — a file we did not author
must survive parse → (optional edit) → print unchanged, and anything we build from scratch has to
come out in KiCad's own layout so a human reading the diff sees only the change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pcbai.kicad.sexp import Sexp, SexpParseError, dumps, parse, parse_all

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "anna-app" / "executas" / "pcb-designer" / "pcbai" / "steps" / "template_project"


# ── round trip ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["board.kicad_pcb", "schematic.kicad_sch"])
def test_template_files_round_trip_byte_identical(name: str) -> None:
    text = (TEMPLATE / name).read_text(encoding="utf-8")
    tree = parse(text)
    out = dumps(tree)
    assert out == text, "our printer changed a file KiCad wrote — layout or quoting regressed"


def test_generated_board_reparses_identically(tmp_path: Path) -> None:
    """The writer's output is also stable under parse → print (printer and parser agree)."""
    from pcbai.kicad.scaffold import generate, load_spec

    spec = load_spec(TEMPLATE.parent.parent / "boards" / "sv16.yaml")
    result = generate(spec, tmp_path, stem="rt", route=False)
    text = Path(result.files["board"]).read_text(encoding="utf-8")
    assert dumps(parse(text)) == text


def test_comments_survive_a_round_trip() -> None:
    src = '(kicad_pcb\n\t; a note from the designer\n\t(version 20260206)\n)\n'
    assert dumps(parse(src)) == src


def test_editing_one_node_leaves_the_rest_of_the_file_alone() -> None:
    text = (TEMPLATE / "board.kicad_pcb").read_text(encoding="utf-8")
    tree = parse(text)
    setup = tree.child("setup")
    assert setup is not None
    before_lines = dumps(tree).splitlines()
    assert before_lines == text.splitlines()
    setup.append(Sexp.form("pad_to_mask_clearance", 0.05))
    after_lines = dumps(tree).splitlines()
    assert len(after_lines) == len(before_lines) + 1
    # everything KiCad wrote keeps its exact bytes; only the appended line is new
    fresh = [line for line in after_lines if "pad_to_mask_clearance 0.05" in line]
    assert len(fresh) == 1, fresh
    assert [line for line in after_lines if line not in fresh] == before_lines


# ── quoting rules ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("token,expected", [
    ("F.Cu", '"F.Cu"'),                # dotted layer names are quoted
    ("signal", "signal"),             # KiCad keyword: bare
    ("yes", "yes"),
    ("no", "no"),
    ("0x0000_ffff", "0x0000_ffff"),   # bitsets stay bare
    ("", '""'),
    ('he said "hi"', '"he said "hi""'),
])
def test_atom_quoting_rules(token: str, expected: str) -> None:
    assert dumps(Sexp.form("probe", token)).strip() == f"(probe {expected})"


def test_version_like_strings_stay_quoted_but_numbers_do_not() -> None:
    assert dumps(Sexp.form("generator_version", "10.0")).strip() == '(generator_version "10.0")'
    assert dumps(Sexp.form("version", 20260206)).strip() == "(version 20260206)"
    assert dumps(Sexp.form("at", -1.0, 2.5)).strip() == "(at -1 2.5)"


# ── layout rule (beautify): break a form iff a child after the head is a form ─
def test_short_atom_only_forms_stay_inline_after_beautify() -> None:
    node = Sexp.form("at", 1.0, 2.0).beautify(indent="\t")
    assert dumps(node).strip() == "(at 1 2)"
    net = Sexp.form("net", 4, "USB_DP").beautify(indent="\t")
    assert dumps(net).strip() == '(net 4 "USB_DP")'


def test_forms_containing_forms_break_after_beautify() -> None:
    layers = Sexp.form("layers", Sexp.form(0, "F.Cu", "signal"),
                       Sexp.form(2, "B.Cu", "signal")).beautify(indent="\t")
    text = dumps(layers)
    assert text.startswith("(layers\n\t(0 \"F.Cu\" signal)\n\t(2 \"B.Cu\" signal)\n)\n"), text


def test_beautify_nests_indentation() -> None:
    root = Sexp.form("kicad_pcb", Sexp.form("version", 20260206),
                     Sexp.form("setup", Sexp.form("pad_to_mask_clearance", 0.0))).beautify(indent="  ")
    assert dumps(root) == ('(kicad_pcb\n  (version 20260206)\n  (setup\n    (pad_to_mask_clearance 0)\n'
                           "  )\n)\n")


# ── accessors ────────────────────────────────────────────────────────────────
def test_lookup_helpers() -> None:
    zone = parse('(zone (net 3 "GND") (layer "In1.Cu") (priority 2) (fill yes)\n\t(hatch edge 0.5)\n)')
    assert zone.head == "zone"
    assert zone.child("net").value() == "3"            # net code is the first atom
    assert zone.child("net").tokens() == ["3", "GND"]  # …and the name follows it
    assert zone.number("priority") == 2.0
    assert zone.int("priority") == 2
    assert zone.flag("fill") is True
    assert zone.scalar("layer") == "In1.Cu"
    assert zone.has("hatch") and not zone.has("nope")
    assert zone.children("xy") == []
    assert zone.get(99).string == ""                   # out of range → empty atom, never IndexError


def test_property_and_attr_lookups() -> None:
    fp = parse('(footprint "lib:x" (property "Reference" "U1" (at 1 2 0)) '
               '(property "Value" "SV16") (attr smd board_only exclude_from_pos_files))')
    assert fp.prop_value("Reference") == "U1"
    assert fp.prop_value("Missing", default="?") == "?"
    assert fp.attr("board_only") is not None and fp.attr("solder_mask_finish") is None


def test_structural_edits_are_written_back() -> None:
    tree = parse('(footprint "lib:x"\n\t(version 20260206)\n\t(layer "F.Cu")\n)\n')
    assert tree.replace_child("version", Sexp.form("version", 20260207)) is True
    assert tree.replace_child("nope", Sexp.form("nope", 1)) is False
    tree.insert_child(Sexp.form("attr", "smd"), after="layer")
    out = dumps(tree)
    assert "(version 20260207)" in out
    assert parse(out).int("version") == 20260207


def test_append_then_beautify_only_the_new_subtree() -> None:
    tree = parse('(board\n\t(paper "A4")\n)\n')
    added = Sexp.form("setup", Sexp.form("stackup", Sexp.form("layer", "F.Cu")))
    tree.append(added)
    added.beautify(indent="\t", level=1)
    assert dumps(tree) == ('(board\n\t(paper "A4")\n\t(setup\n\t\t(stackup\n\t\t\t(layer "F.Cu")\n'
                           "\t\t)\n\t)\n)\n")


# ── python view ──────────────────────────────────────────────────────────────
def test_to_python_types_atoms_but_keeps_quoted_strings() -> None:
    data = parse('(x 1 "two" (y 3) (z) (g "10.0") (f yes) (n -2.5))').to_python()
    assert data == ["x", 1, "two", ["y", 3], ["z"], ["g", "10.0"], ["f", True], ["n", -2.5]]


def test_from_python_reapplies_kicad_quoting() -> None:
    text = dumps(Sexp.from_python(["net", 4, "SPI0_SCK", ["at", 1.5, 2.0]]))
    assert text.strip() == '(net 4 "SPI0_SCK" (at 1.5 2))'


def test_walk_yields_descendants_only() -> None:
    assert [n.head for n in parse("(a (b (c)) (d))").walk()] == ["b", "c", "d"]
    assert list(parse("(a)").walk()) == []


# ── errors ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("src,needle", [
    ("(a (b)) )", "unbalanced"),
    ("(a (b)", "unclosed"),
    ('(a "oops', "unterminated"),
])
def test_parse_errors_point_at_the_problem(src: str, needle: str) -> None:
    with pytest.raises(SexpParseError) as exc:
        parse(src)
    assert needle in str(exc.value).lower()
    assert exc.value.line >= 1 and exc.value.col >= 1
    assert "^" in str(exc.value)          # every message carries a source excerpt + caret


def test_parse_all_reads_every_root_form() -> None:
    assert [f.head for f in parse_all("(a 1)\n(b 2)\n; trailing\n")] == ["a", "b"]


def test_sexp_repr_is_useful_in_failure_messages() -> None:
    assert "net" in repr(parse('(net 1 "GND")'))
