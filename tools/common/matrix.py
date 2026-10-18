"""Read the verification matrix as data instead of as prose.

WHY PARSE A MARKDOWN TABLE
--------------------------------------------------------------------------
``skills/dogrulama-matrisi/SKILL.md`` section 2 is the authority on which
rules exist, what level each sits at, which tool measures it, in which loop
it runs, and what to do when it fails. Item #66's whole complaint is that
this mapping exists and is not executable. Re-typing those rows into a
second file would make them executable and make them WRONG the first time
somebody edits one copy - the #51 failure, one rule in three places with
two different numbers. So the matrix stays the single source and this
module reads it.

WHAT THE PARSER REFUSES TO SMOOTH OVER
--------------------------------------------------------------------------
The level column is not always a single letter, and the ambiguity is real
rather than sloppy:

  ``C->A``      today it is a document gate, the intent is to make it
                file-measurable. Which one is true right now is a decision,
                and a parser that silently picks one is making an
                engineering call in a regex.
  ``A(kısmi)``  the tool measures part of the rule; the rest is manual. An
                assertion written against it as plain A overclaims exactly
                the portion nobody automated.
  ``A(screen)`` the tool measures, but its clean result may not close the
                rule on its own.

``C->A`` is still reported as it stands - ``AMBIGUOUS_LEVEL`` - and is not
mapped onto the executable taxonomy: which of the two levels holds today is a
decision, and picking one here would make it in a regex.

The qualified cells are different, and stopped being unresolvable on
2026-08-10. Their qualifier was never a level: ``kısmi`` is a statement about
COVERAGE and ``screen`` one about AUTHORITY, and the assertion language had
neither field, so the rows could not be expressed and sat unasserted. Both
fields exist now, so a qualified cell resolves to a base level plus a
requirement on the assertion - see ``partial_requirement``. An assertion that
declares the axis claims the row; one written as plain ``A`` is still refused
with ``PARTIAL_LEVEL``, for exactly the original reason.

MATCHING IS EXACT, NEVER FUZZY
--------------------------------------------------------------------------
An assertion names its rule by the row's text. Matching normalises markdown
emphasis, backticks and whitespace and then demands equality. Fuzzy
matching would quietly re-point an assertion at a neighbouring row when
somebody rewords one, and a rule silently checking the wrong thing is the
failure mode this entire layer exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# The matrix's own column headings. Parsing keys off these rather than off
# position, so a column inserted upstream is noticed instead of shifting
# every field silently by one.
RULE_HEADS = ("kural",)
LEVEL_HEADS = ("sev", "seviye")
TOOL_HEADS = ("araç", "arac")
LOOP_HEADS = ("döngü", "dongu")
FAIL_HEADS = ("fail'de", "failde", "fail")

AMBIGUOUS_LEVEL = "AMBIGUOUS_LEVEL"
PARTIAL_LEVEL = "PARTIAL_LEVEL"
UNPARSED_LEVEL = "UNPARSED_LEVEL"

# What the qualifier in a cell like ``A(kısmi)`` or ``A(screen)`` actually
# says. Neither is a level: one is a statement about how much of the rule the
# tool reaches, the other about what its clean result may close. Until
# 2026-08-10 the assertion language could express neither, so these rows were
# unassertable by construction and sat at UNASSERTED_RULE - the matrix was
# right to refuse them, and the refusal had nowhere to go.
#
# Now that ``coverage`` and ``authority`` are real assertion fields, the
# qualifier stops being an unresolvable level and becomes a REQUIREMENT ON THE
# ASSERTION: the row may be claimed, but only by an assertion that declares
# the axis the qualifier names. Writing it as plain A still overclaims exactly
# the portion nobody automated, and is still refused.
QUALIFIER_AXES = {
    "kismi": ("coverage", "partial"),
    "kısmi": ("coverage", "partial"),
    "partial": ("coverage", "partial"),
    "screen": ("authority", "screen_only"),
}


def partial_requirement(level_raw: str) -> Optional[Tuple[str, str, str]]:
    """``(base_level, axis, required_value)`` for a qualified cell, else None.

    None covers both "not a qualified cell at all" and "qualified with a word
    this table does not know" - the second stays PARTIAL_LEVEL, because
    inventing an axis for an unrecognised qualifier is the regex-makes-the-
    engineering-call failure this module was written to avoid.
    """
    match = _PARTIAL_LEVEL.match(strip_markdown(level_raw).upper())
    if not match:
        return None
    base, qualifier = match.groups()
    axis = QUALIFIER_AXES.get(qualifier.strip().lower())
    if axis is None:
        return None
    return (base, axis[0], axis[1])

# Underscore is deliberately NOT stripped. It looks like emphasis and is not:
# GFM does not treat intra-word underscores as emphasis, and this document is
# full of identifiers that carry them. The first real run proved the cost -
# ``PWR_FLAG`` became ``PWRFLAG`` and ``sch_wire.py`` became ``schwire.py``,
# which would have made every assertion naming a tool fail to match its row.
_EMPHASIS = re.compile(r"[*`]+")
_WHITESPACE = re.compile(r"\s+")
_SIMPLE_LEVEL = re.compile(r"^[ABCD]$")
_PARTIAL_LEVEL = re.compile(r"^([ABCD])\s*\(([^)]*)\)$")
_COMPOUND_LEVEL = re.compile(r"^([ABCD])\s*(?:->|→|-)\s*([ABCD])$")


def strip_markdown(text: str) -> str:
    """Remove emphasis and backticks, collapse whitespace. Nothing else -
    in particular no case folding, so the returned text is still readable
    in a report."""
    return _WHITESPACE.sub(" ", _EMPHASIS.sub("", text)).strip()


def rule_key(text: str) -> str:
    """The identity an assertion binds to. Normalised, then compared for
    equality - never for similarity."""
    return strip_markdown(text).casefold()


@dataclass(frozen=True)
class MatrixRow:
    rule: str                 # cleaned row text
    key: str                  # normalised identity
    level_raw: str            # exactly what the cell said
    level: Optional[str]      # A/B/C/D when unambiguous, else None
    level_issue: Optional[str]
    level_note: str
    tool: str
    loop: str
    on_fail: str
    section: str
    line: int

    def to_dict(self) -> dict:
        payload = {"rule": self.rule, "level_raw": self.level_raw,
                   "tool": self.tool, "loop": self.loop,
                   "on_fail": self.on_fail, "section": self.section,
                   "line": self.line}
        if self.level:
            payload["level"] = self.level
        if self.level_issue:
            payload["level_issue"] = self.level_issue
            payload["level_note"] = self.level_note
        return payload


def classify_level(cell: str) -> Tuple[Optional[str], Optional[str], str]:
    """(level, issue, note). A cell this cannot resolve resolves to nothing."""
    text = strip_markdown(cell).upper().replace(" ", "")
    if _SIMPLE_LEVEL.match(text):
        return text, None, ""
    compound = _COMPOUND_LEVEL.match(text)
    if compound:
        now, target = compound.groups()
        return None, AMBIGUOUS_LEVEL, (
            f"the cell reads {strip_markdown(cell)!r}: level {now} today, "
            f"level {target} intended. Which one holds right now is a "
            "decision, and picking one here would make it in a regex")
    partial = _PARTIAL_LEVEL.match(strip_markdown(cell).upper())
    if partial:
        base, qualifier = partial.groups()
        return None, PARTIAL_LEVEL, (
            f"level {base} covers only part of this rule ({qualifier.lower()}); "
            "an assertion written as plain "
            f"{base} would claim the portion nobody automated")
    return None, UNPARSED_LEVEL, (
        f"{strip_markdown(cell)!r} is not a level this parser recognises")


def _split_row(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(
        cell and set(cell) <= set("-: ") for cell in cells)


def _column_index(headers: Sequence[str], names: Sequence[str]) -> Optional[int]:
    lowered = [strip_markdown(h).casefold() for h in headers]
    for index, header in enumerate(lowered):
        if header in names:
            return index
    return None


def parse_matrix(text: str) -> Tuple[List[MatrixRow], List[dict]]:
    """Return (rows, problems).

    Every table whose header carries a rule column AND a level column is a
    matrix table; anything else in the document is left alone. A table that
    has the rule column but not the level column IS a problem worth
    reporting - the matrix's first sentence is that a rule without a level
    is not a rule.
    """
    rows: List[MatrixRow] = []
    problems: List[dict] = []
    section = ""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            index += 1
            continue
        if not line.strip().startswith("|"):
            index += 1
            continue

        headers = _split_row(line)
        if index + 1 >= len(lines) or not _is_separator(_split_row(lines[index + 1])):
            index += 1
            continue

        rule_col = _column_index(headers, RULE_HEADS)
        level_col = _column_index(headers, LEVEL_HEADS)
        if rule_col is None:
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                index += 1
            continue
        if level_col is None:
            problems.append({
                "issue": "NO_LEVEL_COLUMN", "section": section,
                "line": index + 1,
                "detail": "this table lists rules with no level column; the "
                          "matrix's own rule is that a rule without a level "
                          "is not a rule and cannot be called applied"})
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                index += 1
            continue

        tool_col = _column_index(headers, TOOL_HEADS)
        loop_col = _column_index(headers, LOOP_HEADS)
        fail_col = _column_index(headers, FAIL_HEADS)

        index += 2
        while index < len(lines) and lines[index].strip().startswith("|"):
            cells = _split_row(lines[index])
            if len(cells) <= max(rule_col, level_col):
                problems.append({"issue": "SHORT_ROW", "section": section,
                                 "line": index + 1,
                                 "detail": "row has fewer cells than the header"})
                index += 1
                continue

            def cell(column: Optional[int]) -> str:
                if column is None or column >= len(cells):
                    return ""
                return strip_markdown(cells[column])

            rule_text = cell(rule_col)
            if not rule_text:
                index += 1
                continue
            level, issue, note = classify_level(cells[level_col])
            rows.append(MatrixRow(
                rule=rule_text, key=rule_key(rule_text),
                level_raw=strip_markdown(cells[level_col]),
                level=level, level_issue=issue, level_note=note,
                tool=cell(tool_col), loop=cell(loop_col),
                on_fail=cell(fail_col), section=section, line=index + 1))
            index += 1
    return rows, problems


def read_matrix(path: Path | str) -> Tuple[List[MatrixRow], List[dict]]:
    return parse_matrix(Path(path).read_text(encoding="utf-8"))


@dataclass
class Coverage:
    """Which matrix rows have an assertion, and which assertions have a row."""

    rows: List[MatrixRow]
    unasserted: List[MatrixRow]
    orphans: List[dict]
    matched: List[dict]
    problems: List[dict]

    def to_dict(self) -> dict:
        return {
            "rules_parsed": len(self.rows),
            "matched": self.matched,
            "unasserted": [row.to_dict() for row in self.unasserted],
            "orphan_assertions": self.orphans,
            "parse_problems": self.problems,
        }


def cross_reference(rows: Sequence[MatrixRow], assertions) -> Coverage:
    """Match assertions to rows by exact normalised rule text.

    A row with no assertion is ``UNASSERTED_RULE``; an assertion naming no
    row is ``ORPHAN_ASSERTION``. Both directions matter: the first is a rule
    nothing enforces, the second is enforcement pointed at a rule that no
    longer exists - which passes forever and looks like coverage.
    """
    by_key: Dict[str, List[MatrixRow]] = {}
    for row in rows:
        by_key.setdefault(row.key, []).append(row)

    matched: List[dict] = []
    orphans: List[dict] = []
    claimed: set = set()

    for assertion in assertions:
        key = rule_key(assertion.rule)
        candidates = by_key.get(key)
        if not candidates:
            orphans.append({
                "assertion": assertion.id, "rule": assertion.rule,
                "issue": "ORPHAN_ASSERTION",
                "detail": "no matrix row carries this rule text. Matching is "
                          "exact by design: a fuzzy match would re-point the "
                          "assertion at a neighbouring row the moment somebody "
                          "rewords one"})
            continue
        claimed.add(key)
        row = candidates[0]
        entry = {"assertion": assertion.id, "rule": row.rule,
                 "matrix_level": row.level_raw,
                 "assertion_level": assertion.level,
                 "matrix_loop": row.loop, "assertion_loop": assertion.loop,
                 "section": row.section}
        requirement = partial_requirement(row.level_raw)
        if row.level and row.level != assertion.level:
            entry["issue"] = "LEVEL_DISAGREEMENT"
            entry["detail"] = (
                f"the matrix says level {row.level}, the assertion claims "
                f"{assertion.level}; the level decides what a pass is allowed "
                "to establish, so the two cannot differ")
        elif requirement:
            base, axis, expected = requirement
            entry["matrix_requires"] = {"level": base, axis: expected}
            entry[f"assertion_{axis}"] = getattr(assertion, axis, None)
            if assertion.level != base:
                entry["issue"] = "LEVEL_DISAGREEMENT"
                entry["detail"] = (
                    f"the matrix qualifies this row as {row.level_raw!r}, whose "
                    f"base level is {base}; the assertion claims "
                    f"{assertion.level}")
            elif getattr(assertion, axis, None) != expected:
                entry["issue"] = PARTIAL_LEVEL
                entry["detail"] = (
                    f"the matrix qualifies this row as {row.level_raw!r}. That "
                    f"qualifier is a {axis} statement, not a level: the "
                    f"assertion must declare {axis}={expected!r} to claim this "
                    f"row. Written as plain {base} it claims the portion "
                    "nobody automated")
            # else: the qualifier is satisfied. The row is properly claimed and
            # carries no issue - the first time these rows have been claimable.
        elif row.level_issue:
            entry["issue"] = row.level_issue
            entry["detail"] = row.level_note
        if row.loop and assertion.loop and row.loop != assertion.loop:
            entry.setdefault("issue", "LOOP_DISAGREEMENT")
            entry.setdefault(
                "detail",
                f"the matrix runs this in {row.loop}, the assertion declares "
                f"{assertion.loop}")
        matched.append(entry)

    unasserted = [row for row in rows if row.key not in claimed]
    return Coverage(rows=list(rows), unasserted=unasserted, orphans=orphans,
                    matched=matched, problems=[])
