"""KiCad S-expression reader/writer — the foundation of all PCB intelligence in this repo.

KiCad's file formats (``.kicad_pcb``, ``.kicad_sch``, ``.kicad_mod`` …) are S-expressions. This is a
dependency-free reader + writer that is **byte-faithful on round-trip**: it records, for every token
and sub-form, whether the source had a line break before it, so a file can be parsed, edited and
written back without reflowing the parts the agent did not touch. That property is what makes
in-place editing of a real board safe (see ``pcbai/core/filesafe.py`` and the Phase 5 mutation layer).

Deliberate choices
------------------
* Atoms stay raw ``str``; typed access is explicit (:meth:`Sexp.num`, :meth:`Sexp.i`). No guessing
  whether ``20260206`` or ``"10.0"`` is "a number".
* Quoting is remembered (:attr:`Sexp.quoted`) because KiCad is inconsistent on purpose
  (``(layer "F.Cu")`` vs ``(version 20260206)``); reproducing it keeps diffs minimal.
* ``parse()`` returns the **root form** (``tree.head == "kicad_pcb"``).
* Parse errors carry line/column plus a source excerpt so the agent can point at the broken region.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import IO, Any, Iterable, Iterator, List, Optional, Sequence, Union

__all__ = ["Sexp", "SexpParseError", "parse", "parse_all", "dumps", "parse_json", "dumps_json",
           "atom", "is_atom"]

_OPEN, _CLOSE = "(", ")"

#: forms that KiCad always prints with one child per line, regardless of length
_ALWAYS_BREAK = frozenset({
    "kicad_pcb", "kicad_sch", "footprint", "lib_symbols", "symbol", "setup", "net_class",
    "layer_stack", "design_settings", "title_block", "properties", "model", "embedded_fonts",
})


class SexpParseError(ValueError):
    """Raised on malformed S-expressions; message includes line/column + a source excerpt."""

    def __init__(self, message: str, *, line: int, col: int, offset: int, source: str) -> None:
        self.msg, self.line, self.col, self.offset = message, line, col, offset
        start = source.rfind("\n", 0, offset) + 1
        end = source.find("\n", offset)
        excerpt = source[start:end if end != -1 else len(source)]
        caret = " " * max(0, min(col, len(excerpt)) - 1) + "^"
        suffix = "" if f"line {line}" in message else f" (line {line}, col {col})"
        super().__init__(f"{message}{suffix}\n    {excerpt}\n    {caret}")


def _fmt_float(x: float) -> str:
    text = f"{x:.9f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


#: tokens KiCad writes bare: booleans, pad/package/zone enums, layer types, flags. Everything else
#: that a human could name (net names, layer names, uuids, paths, text) gets quoted — KiCad 6+ sets
#: ``QUOTE_ALL_STRINGS``, which is why ``(layer "F.Cu")`` is quoted but ``(pad "1" smd rect …)`` is not.
BARE_KEYWORDS = frozenset("""yes no true false smd thru_hole np_thru np_thru_hole connect rect roundrect circle
oval trapezoid custom chamfered solid default dash dot dash_dot dashdot edge full none signal power
jumper mixed user through blind buried micro via dnp exclude_from_bom exclude_from_pos_files
exclude_from_3d_files board_only allow_net_ties not_allowed allowed fixed auto manual hatch circle
graphic graphic3d none left right center top bottom italic bold up down mirrored rotated background
outline pos neg""".split())


def _numeric_like(text: str) -> bool:
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


_HEX = re.compile(r"0x[0-9A-Fa-f_]+")


def _is_bare(text: str) -> bool:
    """True when KiCad writes *text* without quotes (enum keywords, hex bitsets, booleans)."""
    return text in BARE_KEYWORDS or bool(_HEX.fullmatch(text)) or _numeric_like(text) and False


@dataclass
class Sexp:
    """One node: an atom (``string`` is set) or a form (``items`` is non-empty/None-string)."""

    items: List["Sexp"] = field(default_factory=list)
    string: Optional[str] = None
    quoted: bool = False
    line: int = 0
    col: int = 0
    nl: bool = False              # source had a line break immediately before this node
    leading: str = ""             # verbatim whitespace+comments before this node (replayed on write)
    close_nl: bool = False        # source had a line break before this form's ')'
    close_leading: str = ""       # verbatim whitespace+comments before that ')' 

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def atom(cls, value: Union[str, int, float, bool], *, quote: Optional[bool] = None) -> "Sexp":
        """Scalar token. A Python ``str`` is quoted unless it is a KiCad keyword or a hex bitset
        (KiCad 6+ ``QUOTE_ALL_STRINGS``); numbers and booleans never are."""
        if isinstance(value, bool):
            return cls(string="yes" if value else "no", quoted=False)
        if isinstance(value, float):
            return cls(string=_fmt_float(value), quoted=False)
        if isinstance(value, int):
            return cls(string=str(value), quoted=False)
        text = str(value)
        if quote is None:
            quote = not _is_bare(text)
        return cls(string=text, quoted=quote)

    @classmethod
    def form(cls, *children: Any) -> "Sexp":
        """Build a form: ``Sexp.form("net_class", "Power", …)`` or ``Sexp.form(list_of_nodes)``."""
        out: List[Sexp] = []
        for idx, c in enumerate(children):
            if idx == 0 and isinstance(c, str) and not isinstance(c, Sexp):
                out.append(Sexp.atom(c, quote=False))     # the form's keyword is always bare
                continue
            if isinstance(c, (list, tuple)) and not isinstance(c, Sexp):
                out.extend(Sexp.from_python(x) for x in c)
            elif isinstance(c, Sexp):
                out.append(c)
            else:
                out.append(Sexp.from_python(c))
        return cls(items=out)

    list = form  # backwards-friendly alias; never collapses nesting

    # ── predicates / access ──────────────────────────────────────────────────
    def is_atom(self) -> bool:
        return self.string is not None

    def is_list(self) -> bool:
        return self.string is None

    @property
    def head(self) -> Optional[str]:
        """Leading token of a form (``footprint`` for ``(footprint "lib:name" …)``)."""
        if self.items and self.items[0].string is not None:
            return self.items[0].string
        return None

    def __len__(self) -> int:
        return len(self.items) if self.string is None else len(self.string)

    def __iter__(self) -> Iterator["Sexp"]:
        return iter(self.items if self.string is None else [self])

    def __getitem__(self, idx: int) -> "Sexp":
        return self.items[idx]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Sexp({self.to_python()!r})"

    def get(self, idx: int, default: Optional["Sexp"] = None) -> "Sexp":
        if self.string is not None:
            return default if default is not None else Sexp(string="")
        if -len(self.items) <= idx < len(self.items):
            return self.items[idx]
        return default if default is not None else Sexp(string="")

    def child(self, name: str, *, start: int = 1) -> Optional["Sexp"]:
        for it in self.items[start:]:
            if it.string is None and it.head == name:
                return it
        return None

    def children(self, name: str, *, start: int = 1) -> List["Sexp"]:
        return [it for it in self.items[start:] if it.string is None and it.head == name]

    def has(self, name: str) -> bool:
        return self.child(name) is not None

    def tokens(self) -> List[str]:
        """Direct atom children (excluding the head token)."""
        return [it.string or "" for it in self.items[1:] if it.string is not None]

    # ── KiCad-specific lookups ────────────────────────────────────────────────
    def prop(self, name: str) -> Optional["Sexp"]:
        """``(property "Reference" "U1" (at …))`` — KiCad 7+ style."""
        for it in self.items:
            if it.string is None and it.head == "property" and len(it.items) > 1:
                if it.items[1].string == name:
                    return it
        return None

    def prop_value(self, name: str, default: str = "") -> str:
        p = self.prop(name)
        if p is None or len(p.items) < 3:
            return default
        return p.items[2].string if p.items[2].string is not None else default

    def attr(self, name: str) -> Optional["Sexp"]:
        """``(attr board_only exclude_from_pos_files)`` style lookup by keyword."""
        for it in self.items:
            if it.string is None and it.head == "attr" and name in it.tokens():
                return it
        return None

    def value(self, default: str = "") -> str:
        if len(self.items) > 1 and self.items[1].string is not None:
            return self.items[1].string
        return default

    # ── typed scalars ─────────────────────────────────────────────────────────
    def _scalar_text(self) -> Optional[str]:
        """Text of this node when it *is* a scalar, or of its single value
        (``(version 20260206)``, ``(generator "pcbnew")`` …). ``None`` otherwise."""
        if self.string is not None:
            return self.string
        if len(self.items) == 1 and self.items[0].string is not None:
            return self.items[0].string
        if len(self.items) == 2 and self.items[0].string is not None and self.items[1].string is not None:
            return self.items[1].string
        return None

    def scalar(self, name: str, default: str = "") -> str:
        """``node.child(name)`` read as a string, with *default* when absent."""
        child = self.child(name)
        return default if child is None else child.s(default)

    def number(self, name: str, default: float = 0.0) -> float:
        child = self.child(name)
        return default if child is None else child.num(default)

    def int(self, name: str, default: int = 0) -> int:
        child = self.child(name)
        return default if child is None else child.i(default)

    def flag(self, name: str, default: bool = False) -> bool:
        """``(locked yes)``-style boolean *child*; a bare ``(locked)`` counts as True."""
        child = self.child(name)
        if child is None:
            return default
        return child.truthy(True)

    def truthy(self, default: bool = False) -> bool:
        """Read *this* node as a boolean: ``yes``/``no``/``true``/``false``/bare marker."""
        if self.string is not None:
            return self.string.lower() in ("yes", "true", "1")
        toks = self.tokens()
        if not toks:
            return default if not self.items else True
        return toks[0].lower() in ("yes", "true", "1")

    def num(self, default: float = 0.0) -> float:
        txt = self._scalar_text()
        if txt in (None, ""):
            return default
        try:
            return float(txt)
        except ValueError:
            return default

    def i(self, default: int = 0) -> int:
        txt = self._scalar_text()
        if txt in (None, ""):
            return default
        try:
            return int(txt)
        except ValueError:
            try:
                return int(float(txt))
            except ValueError:
                return default

    def s(self, default: str = "") -> str:
        txt = self._scalar_text()
        return default if txt is None else txt

    def is_quoted(self) -> bool:
        return self.quoted

    # ── conversion ────────────────────────────────────────────────────────────
    def to_python(self) -> Any:
        """Plain Python: forms become lists, atoms become ``int``/``float``/``bool``/``str``.

        A *quoted* atom is always a string (``(generator_version "10.0")``), an unquoted numeric is a
        number, and ``yes``/``no`` are booleans — so the JSON view an agent sees can be reasoned about
        and :meth:`from_python` can rebuild the same tokens.
        """
        if self.string is not None:
            if self.quoted:
                return self.string
            low = self.string.lower()
            if low in ("yes", "no"):
                return low == "yes"
            try:
                text = self.string
                return int(text) if text.lstrip("-+").isdigit() else float(text)
            except ValueError:
                return self.string
        return [it.to_python() for it in self.items]

    @staticmethod
    def from_python(obj: Any) -> "Sexp":
        """``["net", 4, "SPI"]``-style nested lists → tree, with KiCad's quoting rules applied."""
        if isinstance(obj, Sexp):
            return obj
        if isinstance(obj, str):
            return Sexp.atom(obj)
        if isinstance(obj, (list, tuple)):
            items = [Sexp.from_python(o) for o in obj]
            if items and isinstance(obj[0], str):
                items[0] = Sexp.atom(obj[0], quote=False)
            return Sexp(items=items) if not items else Sexp(items=items)
        return Sexp.atom(obj)

    # ── writing ──────────────────────────────────────────────────────────────
    def to_string(self, *, indent: str = "\t", level: int = 0, inline_limit: int = 78) -> str:
        if self.string is not None:
            return f'"{self.string}"' if self.quoted else self.string
        if not self.items:
            return "()"
        rendered = [it.to_string(indent=indent, level=level + 1, inline_limit=inline_limit)
                    for it in self.items]
        one_line = "(" + " ".join(rendered) + ")"
        split = (self.close_nl or any(it.nl for it in self.items)
                 or any("\n" in r for r in rendered) or len(one_line) > inline_limit)
        if not split:
            return one_line
        pad = indent * (level + 1)
        out = "("
        for idx, (it, rendered_child) in enumerate(zip(self.items, rendered)):
            # replay the source's own layout (whitespace *and* comments); fall back to computed
            # padding for nodes the agent created, which have no recorded separator
            if it.leading:
                out += it.leading
            elif it.nl or "\n" in rendered_child or (idx and self.items[idx - 1].close_nl):
                out += "\n" + pad
            elif idx:
                out += " "
            out += rendered_child
        out += self.close_leading if self.close_leading else (
            "\n" + indent * level if self.close_nl else "")
        return out + ")"

    def beautify(self, *, indent: str = "\t", level: int = 0, inline_limit: int = 78) -> "Sexp":
        """Lay out a tree that was *built* (not parsed) the way KiCad prints files: list-like forms
        keep one child per line, short leaf forms collapse onto a single line. Parsed nodes are not
        touched by this (they already carry their source layout), which is why editing a real board
        never reflows the parts you did not change."""
        self._layout(indent, level, inline_limit)
        return self

    def _layout(self, indent: str, level: int, limit: int) -> bool:
        """Set this node's layout flags; return True when the form is split across lines.

        KiCad's own printer uses a purely structural rule — measured from a KiCad-10-authored board
        file: **a form breaks iff at least one of its children is a form**, and leading atoms stay on
        the head line. That is why ``(at 1 2)``/``(net 4 "X")`` never wrap while ``(property …)`` and
        ``(pad …)`` always do, regardless of length. The ``limit`` argument is only used for
        ``inline_limit``-style packing of repeated short children (the way KiCad packs ``(xy …)``
        lists inside a filled zone), which we never need for freshly built trees.
        """
        if self.string is not None:
            self.nl, self.leading = False, ""
            return False
        for child in self.items:
            child._layout(indent, level + 1, limit)
        split = any(c.is_list() for c in self.items[1:])
        pad = indent * (level + 1)
        for idx, child in enumerate(self.items):
            child.nl = split and idx > 0 and child.is_list()
            child.leading = ("\n" + pad) if child.nl else ""
        self.close_nl = split
        self.close_leading = ("\n" + indent * level) if split else ""
        return split

    def walk(self, names: Optional[Iterable[str]] = None) -> Iterator["Sexp"]:
        """Depth-first over sub-forms, optionally filtered by head token."""
        want = set(names) if names else None
        stack = list(reversed(self.items))
        while stack:
            node = stack.pop()
            if node.string is None:
                if want is None or node.head in want:
                    yield node
                stack.extend(reversed(node.items))

    def replace_child(self, name: str, new: "Sexp", *, start: int = 1) -> bool:
        """Replace the first sub-form named *name*. Returns False when absent."""
        for idx in range(start, len(self.items)):
            it = self.items[idx]
            if it.string is None and it.head == name:
                new.nl, new.leading = it.nl, it.leading
                self.items[idx] = new
                return True
        return False

    def append(self, new: "Sexp") -> "Sexp":
        """Append a child, laid out on its own line to match the form it joins."""
        new.nl = True
        new.leading = self.items[-1].leading if self.items and self.items[-1].leading else ""
        self.items.append(new)
        self.close_nl = True
        return self

    def insert_child(self, new: "Sexp", *, before: Optional[str] = None,
                     after: Optional[str] = None, at: Optional[int] = None) -> int:
        """Insert a sub-form, optionally relative to a named sibling. Returns the new index."""
        new.nl = True
        idx = at
        if idx is None:
            idx = len(self.items)
            for key, want in (("before", before), ("after", after)):
                if want is None:
                    continue
                for i, it in enumerate(self.items):
                    if it.string is None and it.head == want:
                        idx = i if key == "before" else i + 1
                        break
        self.items.insert(idx, new)
        return idx


# ─────────────────────────────────────────────────────────────────────────────
# Reader
# ─────────────────────────────────────────────────────────────────────────────
def parse(source: Union[str, bytes, IO[str]]) -> Sexp:
    """Parse a KiCad S-expression document and return its root form."""
    text = _as_text(source)
    nodes = parse_all(text)
    if not nodes:
        raise SexpParseError("empty document", line=1, col=1, offset=0, source=text)
    root = nodes[0]
    if root.is_atom():
        raise SexpParseError(
            "not an S-expression document (expected a leading '('). .kicad_pro and .json files are "
            "JSON — use parse_json() for those.", line=root.line, col=root.col, offset=0, source=text)
    if len(nodes) > 1:
        second = nodes[1]
        raise SexpParseError(f"unexpected second top-level form '{second.head or second.s()}'",
                             line=second.line, col=second.col, offset=0, source=text)
    return root


def parse_all(source: Union[str, bytes, IO[str]]) -> List[Sexp]:
    """Parse every top-level form (a normal KiCad file has exactly one)."""
    text = _as_text(source)
    stack: List[Sexp] = []
    out: List[Sexp] = []
    pending_nl = False
    i, n = 0, len(text)
    line, col = 1, 1

    def err(msg: str) -> SexpParseError:
        return SexpParseError(msg, line=line, col=col, offset=i, source=text)

    def take_ws() -> None:
        """Consume whitespace + comments; newline crossings and comment text are remembered."""
        nonlocal i, line, col, pending_nl
        while i < n:
            c = text[i]
            if c == "\n":
                line += 1
                col = 1
                pending_nl = True
                i += 1
            elif c in " \t\r":
                col += 1
                i += 1
            elif c == ";" or text.startswith("#!", i):
                end_of_line = text.find("\n", i)
                end_of_line = n if end_of_line == -1 else end_of_line
                i = end_of_line
            elif text.startswith("#<", i):
                close = text.find(">", i)
                if close == -1:
                    raise err("unterminated #< block comment")
                line += text.count("\n", i, close)
                i = close + 1
            else:
                break

    while i < n:
        ws_start = i
        take_ws()
        if i >= n:
            break
        ws = text[ws_start:i]
        c = text[i]
        nl, pending_nl = pending_nl, False      # comment text itself lives in `ws`, replayed verbatim
        if c == _OPEN:
            stack.append(Sexp(items=[], line=line, col=col, nl=nl, leading=ws))
            i += 1
            col += 1
            continue
        if c == _CLOSE:
            if not stack:
                raise err("unbalanced ')' — extra closing parenthesis")
            node = stack.pop()
            # `nl`/`ws` here describe what preceded this ')' — that is the form's close
            node.close_nl, node.close_leading = nl, ws
            i += 1
            col += 1
            if stack:
                stack[-1].items.append(node)
            else:
                out.append(node)
            continue
        if c == '"':
            j, buf = i + 1, []
            str_line = line
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append("\n" if nxt == "n" else nxt)
                    j += 2
                    continue
                if ch == '"':
                    break
                if ch == "\n":
                    line += 1
                buf.append(ch)
                j += 1
            if j >= n:
                raise err("unterminated string literal")
            tok = Sexp(string="".join(buf), quoted=True, line=str_line, col=col, nl=nl, leading=ws)
            col += (j - i + 1)
            i = j + 1
            if stack:
                stack[-1].items.append(tok)
            else:
                out.append(tok)
            continue
        j = i
        while j < n and text[j] not in ' \t\r\n()";':
            j += 1
        tok_text = text[i:j]
        node = Sexp(string=tok_text, quoted=False, line=line, col=col, nl=nl, leading=ws)
        col += j - i
        i = j
        if stack:
            stack[-1].items.append(node)
        else:
            out.append(node)
    if stack:
        top = stack[-1]
        raise err(f"unexpected end of file: {len(stack)} unclosed '(' (innermost opened at line "
                  f"{top.line}, col {top.col}) — a missing ')'")
    return out


def _inline(node: Sexp) -> str:
    """Rendered text of *node* without any of its leading whitespace (layout decisions only)."""
    return node.to_string().lstrip()




def _as_text(source: Union[str, bytes, IO[str]]) -> str:
    data = source.read() if hasattr(source, "read") else source  # type: ignore[union-attr]
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# Writer
# ─────────────────────────────────────────────────────────────────────────────
def dumps(node: Union[Sexp, Sequence[Any]], *, indent: str = "\t", inline_limit: int = 110) -> str:
    """Serialise a tree. Output is byte-identical to the parsed source when nothing was edited."""
    if not isinstance(node, Sexp):
        node = Sexp.from_python(list(node))
    body = node.to_string(indent=indent, inline_limit=inline_limit)
    return (body + "\n") if body.endswith("\n") else body + "\n"


def parse_json(source: Union[str, bytes, IO[str]]) -> Any:
    """``.kicad_pro`` (and friends) are JSON, not S-expressions."""
    return json.loads(_as_text(source))


def dumps_json(obj: Any, *, indent: int = 2) -> str:
    return json.dumps(obj, indent=indent, ensure_ascii=False) + "\n"


def atom(value: Union[str, int, float, bool], *, quote: Optional[bool] = None) -> Sexp:
    return Sexp.atom(value, quote=quote)


def is_atom(node: Sexp) -> bool:
    return node.is_atom()
