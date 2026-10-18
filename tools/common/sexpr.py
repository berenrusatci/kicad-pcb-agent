"""KiCad S-expression reading and token emission.

Hoisted from sch_wire. via_stub used to carry a SECOND, independent
balanced-paren scanner (``balanced_form``/``direct_layer_forms``/``prop``);
that parser has been deleted and via_stub now uses ``block`` and ``prop``
from here, so a KiCad format change breaks one parser, not two.
"""

from __future__ import annotations

import re

GRID = 1.27          # KiCad schematic connectivity grid (mm); 2.54 preferred
STUB = 2.54          # default short wire leaving a pin


def parse_sexpr(text: str, start: int = 0):
    """Parse text into a nested list tree. Atoms stay as ``str``."""
    tokens = re.finditer(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text[start:])
    stack: list[list] = []
    root = None
    for m in tokens:
        tok = m.group(0)
        if tok == "(":
            node: list = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            root = stack.pop()
            if not stack:
                return root
        else:
            if stack:
                stack[-1].append(tok)
    return root


def unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == '"':
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


def q(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def num(v: float) -> str:
    """Emit a grid-safe number.

    Two decimals, no negative zero. Float residue like 195.57999999999998
    separates a wire end from its pin and produces a silent disconnection.
    """
    v = round(float(v) + 0.0, 2)
    return f"{v:g}"


def find(node, key: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def find_all(node, key: str) -> list:
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def block(text: str, start: int) -> str:
    """Return the balanced-paren block beginning at ``text[start]``.

    Counts depth and skips parentheses inside quoted strings; a naive regex
    truncates S-expressions.
    """
    depth, i = 0, start
    while True:
        if text[i] == '"':                       # skip parens inside a string
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1


def block_at(text: str, start: int) -> str | None:
    """Like ``block`` but tolerant: returns None on a truncated/absent block."""
    if start < 0 or start >= len(text) or text[start] != "(":
        return None
    try:
        return block(text, start)
    except IndexError:
        return None


def prop(form: str, name: str) -> str | None:
    """First value of a ``(name value)`` sub-form inside ``form``."""
    m = re.search(r'\(' + re.escape(name) + r'\s+("(?:[^"\\]|\\.)*"|[^\s()]+)',
                  form)
    if not m:
        return None
    return unquote(m.group(1))


def subforms(form: str, name: str) -> list[str]:
    """Every direct-or-nested ``(name ...)`` block inside ``form``, as text."""
    out: list[str] = []
    for m in re.finditer(r'\(' + re.escape(name) + r'\b', form):
        text_block = block_at(form, m.start())
        if text_block is not None:
            out.append(text_block)
    return out


def direct_children(form: str, name: str) -> list[str]:
    """Only the DIRECT child ``(name ...)`` blocks of ``form``, as text.

    Distinct from ``subforms``, which also matches nested occurrences. The
    stackup parser needs this stricter form: a ``(layer ...)`` nested inside
    another layer's sub-form is not a stackup layer.
    """
    out: list[str] = []
    depth = quoted = escaped = 0
    quoted, escaped = False, False
    i = 0
    needle = "(" + name
    while i < len(form):
        ch = form[i]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            i += 1
            continue
        if ch == '"':
            quoted = True
            i += 1
            continue
        if ch == "(":
            depth += 1
            if depth == 2 and (form.startswith(needle + " ", i)
                               or form.startswith(needle + "(", i)):
                child = block_at(form, i)
                if child is None:
                    raise ValueError(f"unterminated ({name} ...) form")
                out.append(child)
                i += len(child)
                depth -= 1
                continue
        elif ch == ")":
            depth -= 1
        i += 1
    return out


def snap(v: float, grid: float = GRID) -> float:
    return round(v / grid) * grid
