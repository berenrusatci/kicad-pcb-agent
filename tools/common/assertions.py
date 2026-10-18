"""An executable assertion language for the verification matrix.

WHAT WAS MISSING
--------------------------------------------------------------------------
``skills/dogrulama-matrisi`` already holds the mapping every rule needs:
rule -> level (A/B/C/D) -> tool -> loop (L0..L3) -> what to do on failure.
It is prose. A human reads it and decides whether the design obeys it,
which means the mapping is only as live as the last person who read it.

WHAT THIS IS NOT
--------------------------------------------------------------------------
It is NOT a second implementation of the checks. Re-deriving "is the
annular ring wide enough" here would give the repo two answers to one
question, which is the failure #51 and #54 were both about. An assertion
does not measure anything. It BINDS a claim to a location in a report some
tool already produced, and evaluates the binding. The tool remains the
single source of the measurement; this layer is the single source of the
CLAIM about it.

THE LEVEL DECIDES WHAT AN ASSERTION CAN ESTABLISH
--------------------------------------------------------------------------
The matrix's own taxonomy already says which rules a machine can settle,
so the runner obeys it instead of inventing a parallel notion:

  A / B  measurable from an artifact. An assertion at this level MUST carry
         an executable predicate; one that does not is ``UNBACKED_CLAIM`` -
         the matrix says the rule is file-measurable and nothing measures
         it.
  C      document gate. Evaluating it checks the RECORD (decision, source,
         date), never the engineering claim, and the result is labelled
         ``record_only``. The matrix calls dressing C up as A the most
         dangerous mistake in this repo: the line that reads "checked" and
         measured nothing.
  D      physical measurement. Without a measurement record the answer is
         ``MEASUREMENT_ABSENT`` -> NO_COVERAGE. The model was not
         validated; it was not invalidated either.

So a run's summary counts PASS results BY WHAT THEY ESTABLISH. Six passes
of which four are paperwork must not look like six measurements.

WHAT AN UNFALSIFIABLE ASSERTION IS WORTH
--------------------------------------------------------------------------
Nothing, and the matrix says so in its own acceptance criteria: no checker
counts as live without positive AND negative fault injection. Every
assertion therefore carries a ``falsifier`` - a mutation of the evidence
that must flip it to FAIL - and ``prove()`` applies it. Still passing is
``VACUOUS_ASSERTION``. Dropping to NO_COVERAGE is ``FALSIFIER_NO_COVERAGE``:
the injected fault destroyed the measurement rather than being caught by
it, which is a different and quieter kind of useless.

THREE MEASUREMENT TRAPS THIS LAYER CAN ACTUALLY DEFEND AGAINST
--------------------------------------------------------------------------
From ``references/measurement-error-classes.md``, the ones that live at the
comparison rather than inside a checker:

  (2) empty comparison. A selector that finds nothing yields
      ``PATH_ABSENT`` -> NO_COVERAGE. Never FAIL, never PASS: a field that
      is not in the report means the tool did not measure it, and
      comparing ``None`` is how ``0 == 0`` once got reported as agreement.
  (1) float boundary. ``(0.7-0.4)/2`` is ``0.1499999...`` and a 0.15
      threshold turns every exactly-on-the-limit item into a violation.
      Numeric operators therefore REQUIRE a ``tolerance`` - it may be 0,
      but somebody had to write it - and every result carries ``margin``
      so an on-the-boundary comparison is visible instead of silent.
  (3) lying units. A tool's unit label can be wrong, and no reader here
      can catch that. What this can catch is an unlabelled number being
      compared against a labelled threshold: declaring ``units`` without
      ``units_path`` is ``UNITS_UNBOUND`` and the assertion does not load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .findings import FAIL, NO_COVERAGE, PASS

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

# Levels, straight from skills/dogrulama-matrisi/SKILL.md section 1.
LEVEL_A = "A"   # measurable from the board/schematic file
LEVEL_B = "B"   # KiCad's own checker (DRC/ERC/parity)
LEVEL_C = "C"   # document gate - the information is not in any file
LEVEL_D = "D"   # physical measurement - unknowable without a board and a lab
LEVELS = (LEVEL_A, LEVEL_B, LEVEL_C, LEVEL_D)

MACHINE_LEVELS = (LEVEL_A, LEVEL_B)

LOOPS = ("L0", "L1", "L2", "L3")

# What a PASS at this level is entitled to claim. Kept separate from the
# status so a reader cannot mistake a complete filing for a measurement.
ESTABLISHES_MEASUREMENT = "measurement"
ESTABLISHES_RECORD_ONLY = "record_only"
ESTABLISHES_PHYSICAL = "physical_measurement"
ESTABLISHES_NOTHING = "nothing"

# --------------------------------------------------------------------------- #
# Coverage and authority - the two axes that A/B/C/D was carrying badly
# --------------------------------------------------------------------------- #
#
# The matrix grew three level strings that are not levels: ``A(kısmi)`` on the
# EPAD and edge-keepout rows, and ``A(screen)`` on the crosstalk NEXT row.
# ``matrix.py`` refuses to resolve them (PARTIAL_LEVEL) and they have sat
# unassertable ever since, because neither thing they express is an
# acquisition mechanism:
#
#   A(kısmi)  the tool measures PART of the rule       -> a COVERAGE statement
#   A(screen) the tool measures, but may not sign off  -> an AUTHORITY statement
#
# Collapsing either into plain ``A`` claims exactly the portion nobody
# automated. Leaving them as levels means a fifth and sixth level that answer a
# different question from the first four. So they become separate fields, and
# ``level`` goes back to meaning only "how was this evidence acquired".
#
# COVERAGE: how much of the matrix row the automated claim reaches.
COVERAGE_FULL = "full"
COVERAGE_PARTIAL = "partial"
COVERAGES = (COVERAGE_FULL, COVERAGE_PARTIAL)

# AUTHORITY: what a PASS is entitled to OPEN. Modelled on the grant ladder
# already proven in ``common/vendormodel.py`` (GRANT_SIGNOFF / GRANT_SCREEN_ONLY
# / GRANT_NONE), including its rule that authority is never defaulted - see
# vendormodel's own note that defaulting to the weakest tier "sounds safe" and
# is not, because it hides the decision instead of forcing it.
AUTHORITY_SIGNOFF = "signoff"
AUTHORITY_SCREEN_ONLY = "screen_only"
AUTHORITY_RECORD_ONLY = "record_only"
AUTHORITY_PHYSICAL_ACCEPTANCE = "physical_acceptance"
AUTHORITIES = (AUTHORITY_SIGNOFF, AUTHORITY_SCREEN_ONLY,
               AUTHORITY_RECORD_ONLY, AUTHORITY_PHYSICAL_ACCEPTANCE)

# For C and D the authority genuinely does follow from the level: a complete
# filing establishes a record and nothing more, and a lab measurement is a
# physical acceptance. Only the MACHINE levels have a real choice to make, so
# only they are forced to state it.
LEVEL_DEFAULT_AUTHORITY = {
    LEVEL_C: AUTHORITY_RECORD_ONLY,
    LEVEL_D: AUTHORITY_PHYSICAL_ACCEPTANCE,
}

# Run-level verdicts. SCREEN_ONLY is not new here: the matrix already uses
# it for tools whose output cannot be hash-bound (section 5, the drc.sh /
# measure.py / coupling.py decision).
GREEN = "GREEN"
RED = "RED"
SCREEN_ONLY = "SCREEN_ONLY"

# Reason codes. Every one of these is a distinct thing that went wrong; a
# caller that wants to branch on "why" gets to, instead of parsing prose.
PATH_ABSENT = "PATH_ABSENT"
UNBACKED_CLAIM = "UNBACKED_CLAIM"
MEASUREMENT_ABSENT = "MEASUREMENT_ABSENT"
RECORD_INCOMPLETE = "RECORD_INCOMPLETE"
EVIDENCE_MISSING = "EVIDENCE_MISSING"
STALE_EVIDENCE = "STALE_EVIDENCE"
EVIDENCE_UNBOUND = "EVIDENCE_UNBOUND"
UNIT_MISMATCH = "UNIT_MISMATCH"
UNITS_UNDECLARED = "UNITS_UNDECLARED"
TYPE_MISMATCH = "TYPE_MISMATCH"
VACUOUS_ASSERTION = "VACUOUS_ASSERTION"
FALSIFIER_NO_COVERAGE = "FALSIFIER_NO_COVERAGE"
UNASSERTED_RULE = "UNASSERTED_RULE"
ORPHAN_ASSERTION = "ORPHAN_ASSERTION"

# Load-time codes. These used to be bare string literals scattered through
# load_assertion while only UNBACKED_CLAIM was a constant; the asymmetry was
# invisible at the call site and made the set impossible to enumerate.
UNITS_UNBOUND = "UNITS_UNBOUND"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
BAD_COVERAGE = "BAD_COVERAGE"
BAD_AUTHORITY = "BAD_AUTHORITY"
RESIDUAL_UNDECLARED = "RESIDUAL_UNDECLARED"
COVERAGE_UNBOUND = "COVERAGE_UNBOUND"

# Evaluation-time codes for the coverage guard.
COVERAGE_UNDECLARED = "COVERAGE_UNDECLARED"
COVERAGE_NOT_A_COUNT = "COVERAGE_NOT_A_COUNT"
NOTHING_SCANNED = "NOTHING_SCANNED"

# Roll-up code: a passing measurement that is not entitled to sign anything off.
SCREEN_ONLY_AUTHORITY = "SCREEN_ONLY_AUTHORITY"


class AssertionSyntaxError(ValueError):
    """An assertion could not be loaded. Raised at LOAD time, never at
    evaluation time: a malformed assertion that only blows up when its
    evidence happens to be present is a rule that silently is not enforced
    on every run where the evidence is absent."""

    def __init__(self, assertion_id: str, code: str, detail: str):
        self.assertion_id = assertion_id
        self.code = code
        self.detail = detail
        super().__init__(f"{assertion_id}: {code} - {detail}")

    def to_dict(self) -> dict:
        return {"assertion": self.assertion_id, "issue": self.code,
                "detail": self.detail}


# --------------------------------------------------------------------------- #
# Selector - a deliberately small path language
# --------------------------------------------------------------------------- #

_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INDEX = re.compile(r"^\[(\d+)\]$")

MISSING = object()   # distinct from None, which is a legitimate JSON value


def parse_selector(text: str, assertion_id: str = "<selector>") -> Tuple[Any, ...]:
    """``summary.FAIL`` and ``checks[0].status`` and nothing more.

    No wildcards on purpose. ``checks[*].status`` reads as one assertion but
    evaluates over a set whose SIZE depends on the report, so the same
    assertion silently covers three items on one board and zero on the next
    - and zero would compare as vacuously true. Coverage that moves without
    the rule moving is the thing this whole layer exists to prevent.
    """
    if not isinstance(text, str) or not text.strip():
        raise AssertionSyntaxError(assertion_id, "BAD_SELECTOR",
                                   "selector must be a non-empty string")
    path: List[Any] = []
    for raw in text.split("."):
        if not raw:
            raise AssertionSyntaxError(assertion_id, "BAD_SELECTOR",
                                       f"empty path segment in {text!r}")
        # A segment may be name, name[0], or name[0][1].
        head, _, rest = raw.partition("[")
        if head:
            if not _SEGMENT.match(head):
                raise AssertionSyntaxError(
                    assertion_id, "BAD_SELECTOR",
                    f"{head!r} is not a plain key; this path language has no "
                    "wildcards, quotes or expressions")
            path.append(head)
        elif not rest:
            raise AssertionSyntaxError(assertion_id, "BAD_SELECTOR",
                                       f"empty path segment in {text!r}")
        if rest:
            for chunk in ("[" + rest).replace("][", "] [").split(" "):
                match = _INDEX.match(chunk)
                if not match:
                    raise AssertionSyntaxError(
                        assertion_id, "BAD_SELECTOR",
                        f"{chunk!r} is not a numeric index in {text!r}")
                path.append(int(match.group(1)))
    return tuple(path)


def select(document: Any, path: Sequence[Any]) -> Any:
    """Follow ``path``; return ``MISSING`` rather than None when it dead-ends.

    The distinction matters: a report may legitimately carry ``null`` (no
    threshold supplied, for instance), and folding that into "absent" would
    lose the difference between "the tool said nothing applies" and "the
    tool does not have this field at all".
    """
    cursor = document
    for step in path:
        if isinstance(step, int):
            if not isinstance(cursor, (list, tuple)) or step >= len(cursor):
                return MISSING
            cursor = cursor[step]
        else:
            if not isinstance(cursor, dict) or step not in cursor:
                return MISSING
            cursor = cursor[step]
    return cursor


def _set_path(document: Any, path: Sequence[Any], value: Any) -> bool:
    """Mutate ``document`` in place. False when the path does not exist."""
    if not path:
        return False
    cursor = document
    for step in path[:-1]:
        cursor = select(cursor, [step])
        if cursor is MISSING:
            return False
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(cursor, list) or last >= len(cursor):
            return False
        cursor[last] = value
        return True
    if not isinstance(cursor, dict) or last not in cursor:
        return False
    cursor[last] = value
    return True


def _remove_path(document: Any, path: Sequence[Any]) -> bool:
    if not path:
        return False
    cursor = document
    for step in path[:-1]:
        cursor = select(cursor, [step])
        if cursor is MISSING:
            return False
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(cursor, list) or last >= len(cursor):
            return False
        del cursor[last]
        return True
    if not isinstance(cursor, dict) or last not in cursor:
        return False
    del cursor[last]
    return True


# --------------------------------------------------------------------------- #
# Operators - a closed set, each total over its declared input type
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Operator:
    name: str
    numeric: bool          # does it compare magnitudes (=> tolerance, units)
    wants_sequence: bool   # does it read a length rather than a value
    describe: str
    apply: Callable[[Any, Any, float], Tuple[bool, Optional[float]]]


def _as_number(value: Any) -> Optional[float]:
    """bool is an int in Python; a True that compares >= 0.5 is not a
    measurement, so booleans are refused here rather than silently ranked."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _op_equals(value, expected, tolerance):
    left, right = _as_number(value), _as_number(expected)
    if left is None or right is None:
        return value == expected, None
    return abs(left - right) <= tolerance, left - right


def _op_at_least(value, expected, tolerance):
    left, right = _as_number(value), _as_number(expected)
    if left is None or right is None:
        return None, None
    return left >= right - tolerance, left - right


def _op_at_most(value, expected, tolerance):
    left, right = _as_number(value), _as_number(expected)
    if left is None or right is None:
        return None, None
    return left <= right + tolerance, left - right


def _op_count_at_least(value, expected, tolerance):
    if not isinstance(value, (list, tuple, str)):
        return None, None
    right = _as_number(expected)
    if right is None:
        return None, None
    return len(value) >= right - tolerance, len(value) - right


def _op_count_at_most(value, expected, tolerance):
    if not isinstance(value, (list, tuple, str)):
        return None, None
    right = _as_number(expected)
    if right is None:
        return None, None
    return len(value) <= right + tolerance, len(value) - right


def _op_one_of(value, expected, tolerance):
    if not isinstance(expected, (list, tuple)):
        return None, None
    return value in list(expected), None


def _op_is_true(value, expected, tolerance):
    """Strictly ``True``. Not truthiness: a non-empty list of violations is
    truthy, and an assertion that accepted it would pass on the evidence of
    its own failure."""
    return value is True, None


OPERATORS: Dict[str, Operator] = {
    op.name: op for op in (
        Operator("equals", True, False,
                 "numeric within tolerance; exact for strings and booleans",
                 _op_equals),
        Operator("at_least", True, False, "value >= threshold - tolerance",
                 _op_at_least),
        Operator("at_most", True, False, "value <= threshold + tolerance",
                 _op_at_most),
        Operator("count_at_least", True, True,
                 "len(value) >= threshold - tolerance", _op_count_at_least),
        Operator("count_at_most", True, True,
                 "len(value) <= threshold + tolerance", _op_count_at_most),
        Operator("one_of", False, False, "value is a member of the given set",
                 _op_one_of),
        Operator("is_true", False, False,
                 "value is exactly True; truthiness is not accepted",
                 _op_is_true),
    )
}


# --------------------------------------------------------------------------- #
# The assertion
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Falsifier:
    """The mutation that must flip this assertion to FAIL.

    ``set`` injects a fault into the evidence. ``remove`` deletes the field,
    which does NOT prove anything: removing a measurement makes the check
    lose coverage rather than catch a defect. It is kept in the language and
    reported as ``FALSIFIER_NO_COVERAGE`` rather than forbidden, because the
    distinction between "my check caught it" and "my check went blind" is
    exactly what somebody writing falsifiers needs to be shown.
    """

    operation: str            # "set" | "remove"
    path: Tuple[Any, ...]
    value: Any = None
    raw_path: str = ""

    def to_dict(self) -> dict:
        payload = {"operation": self.operation, "path": self.raw_path}
        if self.operation == "set":
            payload["value"] = self.value
        return payload


@dataclass(frozen=True)
class Assertion:
    id: str
    rule: str                 # the matrix row this comes from, verbatim
    level: str
    loop: str
    on_fail: str              # the matrix's fifth column, finally machine-read
    source: str               # where the rule text was transcribed from
    evidence: Optional[str] = None       # named evidence report
    selector: Optional[str] = None
    path: Tuple[Any, ...] = ()
    operator: Optional[str] = None
    expected: Any = None
    tolerance: float = 0.0
    units: Optional[str] = None
    units_path: Tuple[Any, ...] = ()
    units_selector: Optional[str] = None
    falsifier: Optional[Falsifier] = None
    record: Optional[dict] = None        # level C filing
    measurement: Optional[dict] = None   # level D measurement record
    notes: str = ""
    # The two axes level was carrying badly. See the COVERAGE / AUTHORITY
    # block at the top of this module.
    coverage: str = COVERAGE_FULL
    residual_evidence: Tuple[str, ...] = ()
    authority: str = AUTHORITY_SIGNOFF
    # The coverage guard: which field of the report proves the check actually
    # examined something. Without it, "no violations" and "nothing was looked
    # at" are the same number.
    coverage_selector: Optional[str] = None
    coverage_path: Tuple[Any, ...] = ()
    coverage_min: int = 1
    coverage_waiver: str = ""

    @property
    def executable(self) -> bool:
        return self.operator is not None and self.evidence is not None

    def establishes(self) -> str:
        """What KIND of evidence this is. Derived from level, and only level.

        Deliberately NOT settable from the assertion set. A rule author who
        writes ``establishes: record_only`` on a level-A assertion is trying to
        say "this tool measures something but must not sign it off" - that is
        an AUTHORITY statement, and it now has a field of its own. Before it
        did not, and the loader silently discarded the key, so the set file
        read as though the downgrade had taken effect while the run went on
        treating the claim as a full measurement.
        """
        if self.level in MACHINE_LEVELS:
            return ESTABLISHES_MEASUREMENT
        if self.level == LEVEL_C:
            return ESTABLISHES_RECORD_ONLY
        if self.level == LEVEL_D:
            return ESTABLISHES_PHYSICAL
        return ESTABLISHES_NOTHING

    def signs_off(self) -> bool:
        """Whether a PASS here may close its rule on its own."""
        return self.authority in (AUTHORITY_SIGNOFF,
                                  AUTHORITY_PHYSICAL_ACCEPTANCE)

    def to_dict(self) -> dict:
        payload = {
            "id": self.id, "rule": self.rule, "level": self.level,
            "loop": self.loop, "on_fail": self.on_fail, "source": self.source,
            "executable": self.executable,
            "establishes": self.establishes(),
            "coverage": self.coverage,
            "authority": self.authority,
        }
        if self.residual_evidence:
            payload["residual_evidence"] = list(self.residual_evidence)
        if self.coverage_selector:
            payload["coverage_selector"] = self.coverage_selector
            payload["coverage_min"] = self.coverage_min
        if self.coverage_waiver:
            payload["coverage_waiver"] = self.coverage_waiver
        if self.evidence:
            payload["evidence"] = self.evidence
        if self.selector:
            payload["selector"] = self.selector
        if self.operator:
            payload["operator"] = self.operator
            payload["expected"] = self.expected
            if OPERATORS[self.operator].numeric:
                payload["tolerance"] = self.tolerance
        if self.units:
            payload["units"] = self.units
            payload["units_selector"] = self.units_selector
        if self.falsifier:
            payload["falsifier"] = self.falsifier.to_dict()
        if self.notes:
            payload["notes"] = self.notes
        return payload


# The four fields every assertion carries regardless of level. ``on_fail``
# is in the list because the matrix's fifth column is the only part that
# tells a reader what to DO, and an assertion without it reports a problem
# nobody knows how to act on.
REQUIRED_FIELDS = ("id", "rule", "level", "loop", "on_fail", "source")

RECORD_FIELDS = ("decision", "source", "date")
MEASUREMENT_FIELDS = ("instrument", "setup", "baseline", "date")

# Every key the loader understands. Enumerated because the loader used to
# accept anything and read only what it recognised: a rule author's field
# vanished with no diagnostic, and the set file kept reading as though it had
# been honoured. That is how ``establishes: record_only`` came to look like a
# working authority downgrade when nothing read it. Keys beginning with ``$``
# are documentation (``$comment``) and are allowed through untouched.
KNOWN_FIELDS = frozenset(REQUIRED_FIELDS) | {
    "operator", "evidence", "selector", "expected", "tolerance",
    "units", "units_selector", "falsifier", "record", "measurement", "notes",
    "coverage", "residual_evidence", "authority",
    "coverage_selector", "coverage_min", "coverage_waiver",
}

# Keys that are computed, never declared. Listed separately so the refusal can
# say what the author probably meant instead of just "unknown field".
DERIVED_FIELDS = {
    "establishes": (
        "'establishes' is derived from 'level' and cannot be set. Writing "
        "'record_only' here on a level A/B rule means 'this tool measures "
        "something but must not sign it off' - that is 'authority', which is "
        "a real field: use \"authority\": \"screen_only\""),
    "executable": (
        "'executable' is derived from whether the assertion carries an "
        "operator and an evidence name"),
}


def _require_str(entry: dict, key: str, assertion_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AssertionSyntaxError(assertion_id, "MISSING_FIELD",
                                   f"{key!r} must be a non-empty string")
    return value


def load_assertion(entry: dict) -> Assertion:
    """Build one assertion, refusing anything that would evaluate ambiguously.

    Every refusal here is a refusal to load, so a broken rule cannot lie
    dormant until the day its evidence shows up.
    """
    if not isinstance(entry, dict):
        raise AssertionSyntaxError("<unknown>", "NOT_AN_OBJECT",
                                   "each assertion must be a JSON object")
    assertion_id = entry.get("id") if isinstance(entry.get("id"), str) else "<unnamed>"

    for key in entry:
        if key.startswith("$") or key in KNOWN_FIELDS:
            continue
        if key in DERIVED_FIELDS:
            raise AssertionSyntaxError(assertion_id, UNKNOWN_FIELD,
                                       DERIVED_FIELDS[key])
        raise AssertionSyntaxError(
            assertion_id, UNKNOWN_FIELD,
            f"{key!r} is not a field this loader reads. It was accepted and "
            "ignored before, which meant a rule author could add a field, see "
            "it sitting in the set file, and never learn that nothing acted on "
            f"it. Known fields: {sorted(KNOWN_FIELDS)}")

    for key in REQUIRED_FIELDS:
        _require_str(entry, key, assertion_id)

    level = entry["level"]
    if level not in LEVELS:
        raise AssertionSyntaxError(assertion_id, "BAD_LEVEL",
                                   f"level must be one of {list(LEVELS)}, "
                                   f"got {level!r}")
    loop = entry["loop"]
    if loop not in LOOPS:
        raise AssertionSyntaxError(assertion_id, "BAD_LOOP",
                                   f"loop must be one of {list(LOOPS)}, "
                                   f"got {loop!r}")

    operator = entry.get("operator")
    evidence = entry.get("evidence")
    selector = entry.get("selector")
    path: Tuple[Any, ...] = ()

    if operator is not None:
        if operator not in OPERATORS:
            raise AssertionSyntaxError(
                assertion_id, "UNKNOWN_OPERATOR",
                f"{operator!r} is not in the closed set {sorted(OPERATORS)}; "
                "this language has no expression escape hatch, because a rule "
                "that cannot be diffed cannot be reviewed")
        if not isinstance(evidence, str) or not evidence.strip():
            raise AssertionSyntaxError(assertion_id, "MISSING_FIELD",
                                       "an executable assertion needs an "
                                       "'evidence' name to read from")
        path = parse_selector(selector, assertion_id)
        if "expected" not in entry and operator != "is_true":
            raise AssertionSyntaxError(assertion_id, "MISSING_FIELD",
                                       f"operator {operator!r} needs 'expected'")

    # Level A and B claim the rule is measurable from a file. If nothing
    # measures it, the matrix row is the defect - not the design.
    if level in MACHINE_LEVELS and operator is None:
        raise AssertionSyntaxError(
            assertion_id, UNBACKED_CLAIM,
            f"level {level} says this rule is measurable from an artifact, "
            "but the assertion carries no operator - the claim is unbacked")

    tolerance = 0.0
    if operator is not None and OPERATORS[operator].numeric:
        if "tolerance" not in entry:
            raise AssertionSyntaxError(
                assertion_id, "MISSING_TOLERANCE",
                f"operator {operator!r} compares magnitudes, so 'tolerance' is "
                "required. It may be 0 - the point is that a boundary policy "
                "was chosen rather than inherited from float arithmetic")
        raw = entry["tolerance"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise AssertionSyntaxError(assertion_id, "BAD_TOLERANCE",
                                       "tolerance must be a number >= 0")
        tolerance = float(raw)

    units = entry.get("units")
    units_selector = entry.get("units_selector")
    units_path: Tuple[Any, ...] = ()
    if units is not None:
        if not isinstance(units, str) or not units.strip():
            raise AssertionSyntaxError(assertion_id, "BAD_UNITS",
                                       "units must be a non-empty string")
        if not isinstance(units_selector, str) or not units_selector.strip():
            raise AssertionSyntaxError(
                assertion_id, UNITS_UNBOUND,
                "declaring 'units' without 'units_selector' compares a "
                "labelled threshold against an unlabelled number; a tool "
                "reporting mm-scaled-by-100 while saying 'mm' has already "
                "cost this repo a session")
        units_path = parse_selector(units_selector, assertion_id)
    elif units_selector is not None:
        raise AssertionSyntaxError(assertion_id, UNITS_UNBOUND,
                                   "'units_selector' without 'units' checks a "
                                   "label against nothing")

    coverage, residual, authority = _load_coverage_and_authority(
        entry, assertion_id, level)
    coverage_selector, coverage_path, coverage_min, coverage_waiver = (
        _load_coverage_guard(entry, assertion_id, level, operator))

    falsifier = _load_falsifier(entry.get("falsifier"), assertion_id,
                                required=operator is not None)

    record = entry.get("record")
    if level == LEVEL_C and record is not None and not isinstance(record, dict):
        raise AssertionSyntaxError(assertion_id, "BAD_RECORD",
                                   "'record' must be an object")
    measurement = entry.get("measurement")
    if level == LEVEL_D and measurement is not None and not isinstance(measurement, dict):
        raise AssertionSyntaxError(assertion_id, "BAD_MEASUREMENT",
                                   "'measurement' must be an object")

    return Assertion(
        id=entry["id"], rule=entry["rule"], level=level, loop=loop,
        on_fail=entry["on_fail"], source=entry["source"],
        evidence=evidence, selector=selector, path=path,
        operator=operator, expected=entry.get("expected"),
        tolerance=tolerance, units=units, units_path=units_path,
        units_selector=units_selector, falsifier=falsifier,
        record=record, measurement=measurement,
        notes=entry.get("notes", "") or "",
        coverage=coverage, residual_evidence=residual, authority=authority,
        coverage_selector=coverage_selector, coverage_path=coverage_path,
        coverage_min=coverage_min, coverage_waiver=coverage_waiver,
    )


def _load_coverage_and_authority(entry: dict, assertion_id: str,
                                 level: str) -> Tuple[str, Tuple[str, ...], str]:
    """The two axes, validated together because they constrain each other."""
    coverage = entry.get("coverage", COVERAGE_FULL)
    if coverage not in COVERAGES:
        raise AssertionSyntaxError(
            assertion_id, BAD_COVERAGE,
            f"coverage must be one of {list(COVERAGES)}, got {coverage!r}")

    raw_residual = entry.get("residual_evidence", [])
    if not isinstance(raw_residual, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_residual):
        raise AssertionSyntaxError(
            assertion_id, RESIDUAL_UNDECLARED,
            "'residual_evidence' must be a list of non-empty strings")
    residual = tuple(raw_residual)

    # "Partial" without saying what is missing is worse than "full": it looks
    # like humility while conveying nothing, and no reader can tell whether
    # the uncovered part is a rounding detail or the whole failure mode.
    if coverage == COVERAGE_PARTIAL and not residual:
        raise AssertionSyntaxError(
            assertion_id, RESIDUAL_UNDECLARED,
            "coverage 'partial' must name what it does NOT cover, in "
            "'residual_evidence'. The uncovered portion is the part a reader "
            "would otherwise assume was checked")
    if coverage == COVERAGE_FULL and residual:
        raise AssertionSyntaxError(
            assertion_id, BAD_COVERAGE,
            "coverage 'full' cannot also list 'residual_evidence' - if "
            "something is left over, the coverage is 'partial'")

    authority = entry.get("authority")
    if authority is None:
        if level in MACHINE_LEVELS:
            # No default, on purpose, and only for the machine levels: C and D
            # genuinely derive theirs. vendormodel.py made this argument first
            # - defaulting to the weakest tier sounds safe, but it means the
            # question was never asked, and the NEXT screening row is exactly
            # what that costs.
            raise AssertionSyntaxError(
                assertion_id, BAD_AUTHORITY,
                f"level {level} assertions must state 'authority' explicitly: "
                f"{list(AUTHORITIES)}. There is no default. A closed-form "
                "screen and a field solver both produce a number; only one of "
                "them may close a sign-off rule, and the assertion is the only "
                "place that difference can be written down")
        authority = LEVEL_DEFAULT_AUTHORITY[level]
    if authority not in AUTHORITIES:
        raise AssertionSyntaxError(
            assertion_id, BAD_AUTHORITY,
            f"authority must be one of {list(AUTHORITIES)}, got {authority!r}")
    if level in MACHINE_LEVELS and authority == AUTHORITY_PHYSICAL_ACCEPTANCE:
        raise AssertionSyntaxError(
            assertion_id, BAD_AUTHORITY,
            "'physical_acceptance' belongs to level D; a file-measurable rule "
            "cannot accept hardware")
    return coverage, residual, authority


def _load_coverage_guard(entry: dict, assertion_id: str, level: str,
                         operator: Optional[str],
                         ) -> Tuple[Optional[str], Tuple[Any, ...], int, str]:
    """Bind the count that proves the check examined something.

    This is the framework-level half of the EPAD/edge finding. The assertion

        metrics.epad_paste_coverage at_most 0

    passes on a board with no exposed pads at all: an empty check reports zero
    violations because it has an empty violation list, not because it looked
    and found none. The predicate is true and the rule is unchecked.

    A measurement claim therefore has to bind its own denominator. Either name
    the field that carries the scanned count, or state in writing why the
    artifact has none - what is not allowed is leaving the question unasked.
    """
    selector = entry.get("coverage_selector")
    waiver = entry.get("coverage_waiver", "")
    if not isinstance(waiver, str):
        raise AssertionSyntaxError(assertion_id, COVERAGE_UNBOUND,
                                   "'coverage_waiver' must be a string")

    raw_min = entry.get("coverage_min", 1)
    if isinstance(raw_min, bool) or not isinstance(raw_min, int) or raw_min < 1:
        raise AssertionSyntaxError(
            assertion_id, COVERAGE_UNBOUND,
            "'coverage_min' must be an integer >= 1; a minimum of zero is the "
            "vacuity this field exists to prevent")

    if selector is not None and waiver:
        raise AssertionSyntaxError(
            assertion_id, COVERAGE_UNBOUND,
            "an assertion cannot both bind a coverage count and waive it")

    if selector is None:
        # Only executable machine-level rules are forced to answer. A level C
        # filing has no scanned count, and a rule with no operator measures
        # nothing in the first place.
        if level in MACHINE_LEVELS and operator is not None and not waiver:
            raise AssertionSyntaxError(
                assertion_id, COVERAGE_UNBOUND,
                "a level A/B assertion must bind 'coverage_selector' to the "
                "field proving the check examined something, or state a "
                "'coverage_waiver' explaining why its artifact carries no such "
                "count. Without one, 'no violations' and 'nothing was "
                "examined' are the same number")
        return None, (), raw_min, waiver

    if not isinstance(selector, str) or not selector.strip():
        raise AssertionSyntaxError(assertion_id, COVERAGE_UNBOUND,
                                   "'coverage_selector' must be a non-empty "
                                   "string")
    return selector, parse_selector(selector, assertion_id), raw_min, waiver


def _load_falsifier(raw: Any, assertion_id: str, *,
                    required: bool) -> Optional[Falsifier]:
    if raw is None:
        if required:
            raise AssertionSyntaxError(
                assertion_id, "MISSING_FALSIFIER",
                "an executable assertion must say what would break it. An "
                "assertion nothing can falsify is not a check; the matrix's "
                "own acceptance criteria refuse to call such a checker live")
        return None
    if not isinstance(raw, dict):
        raise AssertionSyntaxError(assertion_id, "BAD_FALSIFIER",
                                   "'falsifier' must be an object")
    operation = raw.get("operation")
    if operation not in ("set", "remove"):
        raise AssertionSyntaxError(assertion_id, "BAD_FALSIFIER",
                                   "falsifier.operation must be 'set' or "
                                   "'remove'")
    raw_path = raw.get("path")
    path = parse_selector(raw_path, assertion_id)
    if operation == "set" and "value" not in raw:
        raise AssertionSyntaxError(assertion_id, "BAD_FALSIFIER",
                                   "a 'set' falsifier needs a 'value'")
    return Falsifier(operation=operation, path=path,
                     value=raw.get("value"), raw_path=raw_path)


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

@dataclass
class Evidence:
    """One tool report, plus what it is bound to.

    ``binds`` is optional and its absence is not fatal - but it is not free
    either. An assertion resting on a report nobody can tie to the artifact
    it supposedly measured is a screening result, which is precisely the
    call the matrix already made for drc.sh, measure.py and coupling.py.
    """

    name: str
    document: dict
    path: str = ""
    artifact: Optional[str] = None       # the file the report measured
    artifact_digest: Optional[str] = None  # its digest, computed now
    hash_path: Tuple[Any, ...] = ()
    hash_selector: Optional[str] = None

    @property
    def bound(self) -> bool:
        return bool(self.hash_selector) and self.artifact_digest is not None

    def freshness(self) -> Tuple[bool, str]:
        """(fresh, detail). An unbound evidence source is not stale - it is
        unbound, which is a different and separately reported thing."""
        if not self.bound:
            return True, "no hash binding declared"
        recorded = select(self.document, self.hash_path)
        if recorded is MISSING:
            return False, (f"the report carries no digest at "
                           f"{self.hash_selector!r}, so it cannot be tied to "
                           f"{self.artifact!r}")
        if not isinstance(recorded, str) or recorded.lower() != self.artifact_digest:
            return False, (f"the report was produced from a different "
                           f"{self.artifact!r}: recorded {recorded!r}, current "
                           f"{self.artifact_digest!r}")
        return True, "digest matches the artifact on disk"


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    assertion: Assertion
    status: str                      # PASS / FAIL / NO_COVERAGE
    establishes: str
    reason: Optional[str] = None
    detail: str = ""
    observed: Any = None
    margin: Optional[float] = None
    evidence_bound: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = {
            "assertion": self.assertion.id,
            "rule": self.assertion.rule,
            "level": self.assertion.level,
            "loop": self.assertion.loop,
            "status": self.status,
            "establishes": self.establishes,
            "coverage": self.assertion.coverage,
            "authority": self.assertion.authority,
            "evidence_bound": self.evidence_bound,
            "detail": self.detail,
        }
        # Carried into every outcome, not just the assertion definition, so a
        # reader of the run report can see what a PASS did NOT cover without
        # having to go back to the set file.
        if self.assertion.residual_evidence:
            payload["residual_evidence"] = list(self.assertion.residual_evidence)
        if self.reason:
            payload["reason"] = self.reason
        if self.observed is not MISSING:
            payload["observed"] = self.observed
        if self.margin is not None:
            payload["margin"] = self.margin
        if self.status == FAIL:
            payload["on_fail"] = self.assertion.on_fail
        payload.update(self.extra)
        return payload


def _no_coverage(assertion: Assertion, reason: str, detail: str,
                 **extra) -> Outcome:
    return Outcome(assertion=assertion, status=NO_COVERAGE,
                   establishes=ESTABLISHES_NOTHING, reason=reason,
                   detail=detail, observed=MISSING, extra=extra)


def _coverage_guard(assertion: Assertion,
                    source: Evidence) -> Optional[Outcome]:
    """None when the check demonstrably examined something; NO_COVERAGE else.

    Runs BEFORE the predicate, because a comparison against a report that
    scanned nothing is not a failed check - it is an absent one, and letting
    it reach the operator is how ``at_most 0`` came to mean ``PASS`` on a
    board with no exposed pads.
    """
    if assertion.coverage_selector is None:
        return None
    scanned = select(source.document, assertion.coverage_path)
    if scanned is MISSING:
        return _no_coverage(
            assertion, COVERAGE_UNDECLARED,
            f"{assertion.coverage_selector!r} is not in {source.name!r}, so "
            "the report cannot show that anything was examined; the predicate "
            "would be comparing against an empty scan",
            evidence=source.name)
    if isinstance(scanned, bool) or not isinstance(scanned, int):
        return _no_coverage(
            assertion, COVERAGE_NOT_A_COUNT,
            f"{assertion.coverage_selector!r} holds "
            f"{type(scanned).__name__}, and a coverage denominator must be an "
            "integer count", evidence=source.name, observed_coverage=scanned)
    if scanned < assertion.coverage_min:
        return _no_coverage(
            assertion, NOTHING_SCANNED,
            f"{assertion.coverage_selector} = {scanned}, below the required "
            f"minimum of {assertion.coverage_min}: the check ran but examined "
            "nothing, so its clean result says nothing about this rule",
            evidence=source.name, observed_coverage=scanned)
    return None


def evaluate(assertion: Assertion,
             evidence: Dict[str, Evidence]) -> Outcome:
    """Settle one assertion. Never raises for a data problem - a rule that
    cannot be evaluated is a reported NO_COVERAGE, not a crashed run that
    takes the other rules down with it."""
    if assertion.level == LEVEL_C:
        return _evaluate_record(assertion)
    if assertion.level == LEVEL_D:
        return _evaluate_measurement(assertion)

    source = evidence.get(assertion.evidence)
    if source is None:
        return _no_coverage(
            assertion, EVIDENCE_MISSING,
            f"no report was supplied under the name {assertion.evidence!r}; "
            "the rule is declared measurable and was not measured")

    fresh, freshness_detail = source.freshness()
    if not fresh:
        return _no_coverage(
            assertion, STALE_EVIDENCE,
            f"{freshness_detail}. A rule checked against a report from a "
            "different artifact is worse than an unchecked rule: it reads as "
            "evidence")

    return _evaluate_predicate(assertion, source)


def _evaluate_predicate(assertion: Assertion, source: Evidence) -> Outcome:
    uncovered = _coverage_guard(assertion, source)
    if uncovered is not None:
        return uncovered

    value = select(source.document, assertion.path)
    if value is MISSING:
        return _no_coverage(
            assertion, PATH_ABSENT,
            f"{assertion.selector!r} is not in {source.name!r}; the field is "
            "absent, which means the tool did not measure it - comparing that "
            "against a threshold is the empty comparison that once reported "
            "0 == 0 as agreement",
            evidence=source.name)

    if assertion.units is not None:
        declared = select(source.document, assertion.units_path)
        if declared is MISSING:
            return _no_coverage(
                assertion, UNITS_UNDECLARED,
                f"the assertion compares in {assertion.units!r} but "
                f"{source.name!r} declares no unit at "
                f"{assertion.units_selector!r}", evidence=source.name)
        if declared != assertion.units:
            return _no_coverage(
                assertion, UNIT_MISMATCH,
                f"the assertion compares in {assertion.units!r}, the report "
                f"declares {declared!r}; the numbers are not the same quantity",
                evidence=source.name, declared_units=declared)

    operator = OPERATORS[assertion.operator]
    verdict, margin = operator.apply(value, assertion.expected,
                                     assertion.tolerance)
    if verdict is None:
        kind = "a sequence" if operator.wants_sequence else "a number"
        return _no_coverage(
            assertion, TYPE_MISMATCH,
            f"{assertion.selector!r} holds {type(value).__name__}, and "
            f"operator {assertion.operator!r} needs {kind}",
            evidence=source.name)

    detail = (f"{assertion.selector} = {value!r} "
              f"{assertion.operator} {assertion.expected!r}")
    if operator.numeric and margin is not None:
        detail += f" (margin {margin:+g}, tolerance {assertion.tolerance:g})"
    return Outcome(assertion=assertion, status=PASS if verdict else FAIL,
                   establishes=assertion.establishes(), detail=detail,
                   observed=value, margin=margin,
                   evidence_bound=source.bound,
                   extra={"evidence": source.name})


def _evaluate_record(assertion: Assertion) -> Outcome:
    """Level C. What gets checked is the FILING, and the result says so.

    The matrix is blunt about this: dressing a document gate up as a
    measurement is the most dangerous error in the repo, because the line
    reads "checked" and nothing was measured. So a complete record passes -
    the document gate is a real gate - but it passes as ``record_only``, and
    the run summary counts it separately from anything a tool measured.
    """
    record = assertion.record
    if not isinstance(record, dict):
        return _no_coverage(
            assertion, RECORD_INCOMPLETE,
            "level C closes with a signed record; none was supplied")
    missing = [f for f in RECORD_FIELDS
               if not str(record.get(f, "")).strip()]
    if missing:
        return Outcome(
            assertion=assertion, status=FAIL,
            establishes=ESTABLISHES_NOTHING, reason=RECORD_INCOMPLETE,
            detail=f"the record is missing {missing}; a level C item does not "
                   "close without a source and a date",
            observed=MISSING, extra={"missing_fields": missing})
    return Outcome(
        assertion=assertion, status=PASS,
        establishes=ESTABLISHES_RECORD_ONLY,
        detail=f"decision recorded {record['date']} from {record['source']!r}. "
               "This establishes that the decision was made and sourced, NOT "
               "that the design achieves it - nothing here was measured",
        observed=MISSING, extra={"record": dict(record)})


def _evaluate_measurement(assertion: Assertion) -> Outcome:
    """Level D. No board, no lab, no verdict - and no pretending otherwise."""
    measurement = assertion.measurement
    if not isinstance(measurement, dict):
        return _no_coverage(
            assertion, MEASUREMENT_ABSENT,
            "level D needs a physical measurement; none was supplied. The "
            "rule is not validated and it is not invalidated either")
    missing = [f for f in MEASUREMENT_FIELDS
               if not str(measurement.get(f, "")).strip()]
    if missing:
        return _no_coverage(
            assertion, MEASUREMENT_ABSENT,
            f"the measurement record is missing {missing}; a reading without "
            "its setup and baseline is not repeatable, so it is not evidence",
            missing_fields=missing)
    passed = measurement.get("pass")
    if passed is not True and passed is not False:
        return _no_coverage(
            assertion, MEASUREMENT_ABSENT,
            "the measurement record carries no explicit pass/fail; this layer "
            "does not judge a physical reading on its behalf")
    return Outcome(
        assertion=assertion, status=PASS if passed else FAIL,
        establishes=ESTABLISHES_PHYSICAL,
        detail=f"{measurement['instrument']} on {measurement['date']}, "
               f"baseline {measurement['baseline']!r}",
        observed=MISSING, extra={"measurement": dict(measurement)})


# --------------------------------------------------------------------------- #
# Falsifiability proof
# --------------------------------------------------------------------------- #

@dataclass
class Proof:
    assertion: Assertion
    proven: bool
    reason: Optional[str]
    detail: str
    mutated_status: Optional[str] = None

    def to_dict(self) -> dict:
        payload = {"assertion": self.assertion.id, "proven": self.proven,
                   "detail": self.detail}
        if self.reason:
            payload["reason"] = self.reason
        if self.mutated_status:
            payload["mutated_status"] = self.mutated_status
        return payload


def prove(assertion: Assertion, evidence: Dict[str, Evidence]) -> Optional[Proof]:
    """Inject the declared fault and require the assertion to catch it.

    Returns None for an assertion with no falsifier (levels C and D, which
    have no evidence document to mutate). For everything else the contract
    is exact: the mutated evaluation must be FAIL. Anything else means the
    rule would have stayed green through the very defect it names.
    """
    if assertion.falsifier is None or not assertion.executable:
        return None
    source = evidence.get(assertion.evidence)
    if source is None:
        return Proof(assertion, False, EVIDENCE_MISSING,
                     f"no evidence named {assertion.evidence!r} to mutate; "
                     "falsifiability was not demonstrated")

    import copy
    mutated_doc = copy.deepcopy(source.document)
    falsifier = assertion.falsifier
    if falsifier.operation == "set":
        applied = _set_path(mutated_doc, falsifier.path, falsifier.value)
    else:
        applied = _remove_path(mutated_doc, falsifier.path)
    if not applied:
        return Proof(assertion, False, PATH_ABSENT,
                     f"the falsifier targets {falsifier.raw_path!r}, which the "
                     "evidence does not contain; a mutation that changes "
                     "nothing proves nothing")

    mutated = Evidence(name=source.name, document=mutated_doc,
                       path=source.path, artifact=source.artifact,
                       artifact_digest=source.artifact_digest,
                       hash_path=source.hash_path,
                       hash_selector=source.hash_selector)
    # The mutation edits the report, so a declared hash binding would now
    # correctly call it stale and mask the real question. Freshness is
    # already settled on the unmutated document by evaluate(); here we are
    # asking only whether the predicate can be broken.
    mutated.hash_selector = None
    outcome = _evaluate_predicate(assertion, mutated)

    if outcome.status == FAIL:
        return Proof(assertion, True, None,
                     f"injecting {falsifier.raw_path} flipped the assertion to "
                     "FAIL; the check is live", mutated_status=FAIL)
    if outcome.status == NO_COVERAGE:
        return Proof(assertion, False, FALSIFIER_NO_COVERAGE,
                     f"injecting {falsifier.raw_path} made the assertion "
                     f"NO_COVERAGE ({outcome.reason}) rather than FAIL - the "
                     "fault destroyed the measurement instead of being caught "
                     "by it, which is a quieter kind of useless",
                     mutated_status=NO_COVERAGE)
    return Proof(assertion, False, VACUOUS_ASSERTION,
                 f"injecting {falsifier.raw_path} left the assertion PASSing; "
                 "nothing this rule names can break it, so it is decoration "
                 "rather than a check", mutated_status=PASS)


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

def establishment_summary(outcomes: Sequence[Outcome]) -> Dict[str, int]:
    """PASS counted by what it establishes.

    A run that is green on four filings and two measurements must not read
    like one that is green on six measurements, and a single PASS count
    makes those identical.
    """
    counts = {ESTABLISHES_MEASUREMENT: 0, ESTABLISHES_RECORD_ONLY: 0,
              ESTABLISHES_PHYSICAL: 0}
    for outcome in outcomes:
        if outcome.status == PASS and outcome.establishes in counts:
            counts[outcome.establishes] += 1
    return counts


def authority_summary(outcomes: Sequence[Outcome]) -> Dict[str, Any]:
    """PASS counted by what it is entitled to OPEN, and by what it covers.

    ``establishment_summary`` answers "how was this known"; this answers "what
    may be done with it". A run that is green on six full sign-off
    measurements and one that is green on six partial screens are the same
    number under every counter this module had before.
    """
    by_authority = {name: 0 for name in AUTHORITIES}
    by_coverage = {name: 0 for name in COVERAGES}
    residual: List[str] = []
    for outcome in outcomes:
        if outcome.status != PASS:
            continue
        by_authority[outcome.assertion.authority] += 1
        by_coverage[outcome.assertion.coverage] += 1
        residual.extend(outcome.assertion.residual_evidence)
    return {
        "by_authority": by_authority,
        "by_coverage": by_coverage,
        # The work a green run still leaves on the table. Deliberately a flat
        # list a human can read, because its whole job is to be noticed.
        "residual_evidence": sorted(set(residual)),
    }


def rollup(outcomes: Sequence[Outcome]) -> Tuple[str, List[str]]:
    """RED > NO_COVERAGE > SCREEN_ONLY > GREEN.

    SCREEN_ONLY sits between the two because it is a real result that
    cannot be tied to the artifact it describes; NO_COVERAGE outranks it
    because not knowing is a stronger statement of ignorance than knowing
    without a binding.

    Two distinct things land on SCREEN_ONLY, and the reason code says which:

      EVIDENCE_UNBOUND       the result is trustworthy but cannot be tied to
                             the artifact it describes (no hash binding)
      SCREEN_ONLY_AUTHORITY  the result IS bound, and the tool that produced
                             it is not entitled to close this rule alone

    The second is the crosstalk NEXT case. ``crosstalk_thinning.py`` computes a
    real NEXT number and stamps ``signoff: false`` on all four of its return
    paths - it is a closed-form screen, models no FEXT, and is not a field
    solver. Its clean result is worth having and is not a sign-off, and before
    this there was no way to say both.
    """
    reasons: List[str] = []
    failures = [o for o in outcomes if o.status == FAIL]
    if failures:
        return RED, sorted({o.reason or o.assertion.id for o in failures})
    uncovered = [o for o in outcomes if o.status == NO_COVERAGE]
    if uncovered:
        return NO_COVERAGE, sorted({o.reason or PATH_ABSENT for o in uncovered})
    unbound = [o for o in outcomes if not o.evidence_bound
               and o.establishes == ESTABLISHES_MEASUREMENT]
    screening = [o for o in outcomes
                 if o.status == PASS and not o.assertion.signs_off()
                 and o.establishes == ESTABLISHES_MEASUREMENT]
    if unbound or screening:
        if unbound:
            reasons.append(EVIDENCE_UNBOUND)
        if screening:
            reasons.append(SCREEN_ONLY_AUTHORITY)
        return SCREEN_ONLY, sorted(set(reasons))
    if not outcomes:
        return NO_COVERAGE, ["NO_ASSERTIONS"]
    return GREEN, reasons
