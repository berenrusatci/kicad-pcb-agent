"""The PASS / FAIL / NO_COVERAGE finding container and coverage arithmetic.

``Finding`` and ``result`` were duplicated verbatim in dfm_emc_check,
mask_dam, stitch_density and via_stub.

``coverage_status`` is the SINGLE implementation of the coverage rule that
used to exist in three places:
  - gate_receipt.validate_coverage        (Python reimplementation)
  - references/test_contract_fixtures.coverage_status (second copy)
  - schemas/metric_coverage.schema.json   ($comment prose, unenforced)

The rule text lives in ``schemas/metric_coverage.schema.json``; see
``coverage_rule_from_schema`` for the loader that keeps the two in sync
without requiring a ``jsonschema`` dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

from common.hashing import sha256_file

PASS = "PASS"
FAIL = "FAIL"
NO_COVERAGE = "NO_COVERAGE"
INVALID_COVERAGE = "INVALID_COVERAGE"
GREEN = "GREEN"
RED = "RED"

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
METRIC_COVERAGE_SCHEMA = SCHEMA_DIR / "metric_coverage.schema.json"


class Finding(dict):
    """One check outcome. Serialises directly to the report JSON."""

    def __init__(self, check, status, scanned, violations, detail):
        # Enforced HERE, not only in result(), so that a hand-built Finding
        # cannot smuggle in a scanned=0 PASS. "Nothing was examined" is the
        # single most dangerous silent failure a gate can report as success.
        if scanned == 0 and status != NO_COVERAGE:
            raise ValueError(
                f"{check}: scanned=0 cannot carry status={status} — "
                f"an empty check is {NO_COVERAGE}. Use result().")
        super().__init__(
            check=check,
            status=status,          # PASS / FAIL / NO_COVERAGE
            scanned=scanned,        # how many items were examined (0 => empty check)
            violations=violations,  # list
            detail=detail,
        )


def result(check, scanned, violations, detail="") -> Finding:
    """Build a Finding. scanned == 0 is NO_COVERAGE, never a silent PASS."""
    if scanned == 0:
        return Finding(check, NO_COVERAGE, 0, [], detail or "incelenecek öğe yok")
    return Finding(check, FAIL if violations else PASS, scanned, violations, detail)


def summarise(findings) -> dict[str, int]:
    """Count findings per status, always emitting all three keys."""
    return {
        status: sum(1 for f in findings if f["status"] == status)
        for status in (PASS, FAIL, NO_COVERAGE)
    }


def by_check_map(findings) -> dict[str, dict]:
    """``checks`` keyed by check name, carrying status AND scanned.

    The assertion selector grammar addresses ``a.b`` paths and numeric indices
    only - it cannot pick the entry named "epad_paste_coverage" out of a list.
    That left ``metrics`` (violation counts) as the only name-keyed door into a
    report, and a violation count cannot distinguish "looked and found none"
    from "looked at nothing": an empty check reports zero violations because
    its violation list is empty.

    Keying the findings by name restores the distinction. Because
    ``Finding.__init__`` refuses to build a ``scanned == 0`` PASS, a single
    ``by_check.<name>.status equals PASS`` already carries the conjunction
    "ran, examined something, found nothing wrong".

    Shared by ``board_report`` and by the hand-built envelopes (measure_board)
    so a rule author does not have to know which tool assembled its report.

    ONE THING THIS CANNOT CHECK FOR YOU. ``scanned`` must be the population the
    check actually examined. ``check_teardrops`` set it to the board's total
    ZONE count while measuring teardrop zones, so on a board with three plain
    zones and no teardrops at all it reported ``scanned == 3`` - and a coverage
    guard bound to it would have opened, because something was indeed scanned,
    just not the thing the rule is about. A denominator from the wrong
    population is a guard that reports the same green as a real one. When a
    check counts a subset, ``scanned`` is the size of that subset.
    """
    return {
        f["check"]: {
            "status": f["status"],
            "scanned": f["scanned"],
            "violations": len(f["violations"]),
        }
        for f in findings
    }


# --------------------------------------------------------------------------- #
# Monotone rollup - the ONE aggregate verdict rule
# --------------------------------------------------------------------------- #
# ``Finding`` enforces "scanned == 0 is NO_COVERAGE" for a SINGLE check. The
# aggregate side had no such guard, and three tools re-derived it by hand and
# got it wrong the same way: they branched on FAIL only, so a set holding one
# PASS and any number of NO_COVERAGE rolled up to PASS.
#
#   production/outputs.py  gate_open = not any(f["status"] == FAIL ...)
#   board/measure_board.py NO_COVERAGE only when ALL checks were NO_COVERAGE
#   promote.py             run_verifiers: PASS whenever passed > 0
#
# The property the rule must have is COVERAGE MONOTONICITY: adding a
# NO_COVERAGE to a result set can never produce a PASS and can never preserve
# one. That is the aggregate twin of the single-check invariant above, and it
# is the same ordering ``assert_matrix.overall()`` and ``assertions.py`` use
# (RED > NO_COVERAGE > GREEN).
def rollup_status(findings, excluded: set[str] | frozenset[str] = frozenset()) -> str:
    """The aggregate verdict over a set of findings. PASS is the hard case.

    FAIL if anything FAILed; otherwise NO_COVERAGE if anything was not
    measured; PASS only when every finding was measured and clean. An empty
    set is NO_COVERAGE - "nothing was checked" is not "everything is fine".

    ``excluded`` names checks the caller has deliberately and *recordably*
    placed out of scope (the caller must write that decision, with a reason,
    into its report - see ``production/outputs.py --exclude``). An excluded
    NO_COVERAGE is skipped; an excluded FAIL is NOT, because a measured
    failure cannot be excluded after the fact. Excluding every finding still
    yields NO_COVERAGE: a gate cannot be talked into PASS by narrowing its own
    scope to nothing.
    """
    findings = list(findings)
    if any(f["status"] == FAIL for f in findings):
        return FAIL
    in_scope = [f for f in findings if f["check"] not in excluded]
    if not in_scope:
        return NO_COVERAGE
    if any(f["status"] == NO_COVERAGE for f in in_scope):
        return NO_COVERAGE
    return PASS


def acceptance_verdict(*, applied: bool, met: bool, computed: bool = True,
                       missing_reason: str = "") -> dict:
    """The acceptance axis of an analysis report, as a ``gate`` fragment.

    ``field_solver``, ``electrothermal``, ``correlate`` and ``monte_carlo``
    each start at ``status = "GREEN"`` and only lower it if their own
    computation misbehaves. That is a truthful statement about the
    computation, but it was the ONLY status they published, so a run given no
    --target / no temperature limit / no residual limit reported the same
    GREEN as a run that met a real specification. ``gate.applied = false`` was
    beside it, which meant a careful reader could tell them apart and nothing
    machine-readable could.

    So the two questions get two answers:

      * top-level ``status``   - did the computation complete and behave?
      * ``gate.verdict``       - was an acceptance criterion satisfied?

    With no criterion supplied, the second answer is NO_COVERAGE. Not GREEN:
    a threshold nobody stated cannot have been met. This does NOT relax the
    "no invented numbers" rule - it is the opposite, it stops the absence of a
    sourced limit from reading as a pass.

    ``computed`` is False when the computation itself did not produce a
    trustworthy number (no convergence, grid error too large): an acceptance
    check evaluated on an untrustworthy value is not a measurement either.
    """
    if not applied:
        return {"verdict": NO_COVERAGE,
                "verdict_reason": missing_reason or
                "no acceptance criterion was supplied"}
    if not computed:
        return {"verdict": NO_COVERAGE,
                "verdict_reason": "the computation did not produce a "
                                  "trustworthy value, so the acceptance "
                                  "criterion could not be evaluated"}
    return {"verdict": PASS if met else FAIL}


def rollup_from_counts(passed: int, failed: int, no_coverage: int) -> str:
    """``rollup_status`` for a caller that only has per-status counts.

    ``promote.run_verifiers`` sums the ``summary`` blocks of sub-reports and
    never holds the findings themselves, so it needs the rule in this shape.
    Same three lines, same order, no second definition of the semantics.
    """
    if failed:
        return FAIL
    if no_coverage or not passed:
        return NO_COVERAGE
    return PASS


# --------------------------------------------------------------------------- #
# Coverage arithmetic - one implementation, rule sourced from the schema
# --------------------------------------------------------------------------- #
def coverage_required_fields() -> list[str]:
    """The pinned field list, read from metric_coverage.schema.json."""
    schema = json.loads(METRIC_COVERAGE_SCHEMA.read_text(encoding="utf-8"))
    return list(schema["required"])


# The rule names below are the SINGLE definition of the coverage contract.
# Every consumer (gate_receipt's issue reporter, the contract fixtures, and
# coverage_status here) branches on these, so the rule exists once.
SCANNED_EXCEEDS_ELIGIBLE = "SCANNED_EXCEEDS_ELIGIBLE"
PARTS_DO_NOT_SUM = "PARTS_DO_NOT_SUM"
VIOLATIONS_EXCEED_SCANNED = "VIOLATIONS_EXCEED_SCANNED"
NOT_A_COUNT = "NOT_A_COUNT"


def _is_count(value) -> bool:
    return (not isinstance(value, bool)) and isinstance(value, int) and value >= 0


def coverage_rule_violations(coverage: dict) -> list[str]:
    """Which coverage rules a block breaks. Empty list means valid.

    Rule, from ``schemas/metric_coverage.schema.json`` ($comment plus the
    ``required``/``minimum`` constraints):

        every count is a non-negative integer
        scanned <= eligible
        scanned + excluded == eligible
        violations <= scanned        (when violations is present)

    Returns rule names, not verdicts, so a caller that wants rich per-field
    issues (gate_receipt) and a caller that just wants a status
    (coverage_status) can share one implementation.
    """
    if not isinstance(coverage, dict):
        return [NOT_A_COUNT]
    broken: list[str] = []
    counts = {}
    for key in ("eligible", "scanned", "excluded"):
        value = coverage.get(key)
        if not _is_count(value):
            broken.append(NOT_A_COUNT)
            continue
        counts[key] = value
    if len(counts) != 3:
        return broken or [NOT_A_COUNT]
    if counts["scanned"] > counts["eligible"]:
        broken.append(SCANNED_EXCEEDS_ELIGIBLE)
    if counts["scanned"] + counts["excluded"] != counts["eligible"]:
        broken.append(PARTS_DO_NOT_SUM)
    violations = coverage.get("violations")
    if violations is not None:
        if not _is_count(violations):
            broken.append(NOT_A_COUNT)
        elif violations > counts["scanned"]:
            broken.append(VIOLATIONS_EXCEED_SCANNED)
    return broken


def coverage_arithmetic_errors(coverage: dict) -> list[str]:
    """Back-compatible wrapper returning INVALID_COVERAGE reason codes."""
    return ["INVALID_COVERAGE"] * len(coverage_rule_violations(coverage))


def coverage_status(coverage: dict) -> str:
    """INVALID_COVERAGE / NO_COVERAGE / RED / GREEN for a coverage block."""
    if coverage_rule_violations(coverage):
        return INVALID_COVERAGE
    if coverage["scanned"] == 0:
        return NO_COVERAGE
    return RED if coverage.get("violations") else GREEN


# --------------------------------------------------------------------------- #
# The board-check report envelope, shared by the four pcbnew checkers
# --------------------------------------------------------------------------- #
BOARD_REPORT_SCHEMA = "board-check-report/v1"


def board_report(tool: str, board_path: str, findings,
                 params: dict | None = None,
                 schema_version: str = BOARD_REPORT_SCHEMA) -> dict:
    """Envelope for a board-checker run.

    Adds the ``schema_version`` and ``tool`` fields that the four board tools
    used to omit entirely, and moves tool-specific scalars into a nested
    ``params`` object instead of scattering them at the top level.

    ``design`` and ``metrics`` joined the envelope on 2026-08-09, both for the
    same reason: an assertion could not reach these reports.

      * ``design.sha256`` - without a digest the report cannot be tied to the
        board it measured, so anything resting on it is SCREEN_ONLY forever.
        ``measure_board.py`` had exactly this hole; it turned out to be all
        TEN board checkers, because they share this envelope.
      * ``metrics`` - ``checks`` is a LIST, and the assertion selector
        grammar addresses ``a.b`` paths. A rule could name the tool and still
        have no way to point at the one check it cares about. ``metrics`` is
        that map: check name -> how many violations it found.

    ``metrics`` deliberately carries the violation COUNT and not the status:
    a count of 0 with ``scanned == 0`` is not a pass, and callers that need
    that distinction must read ``checks``. The count is the thing a threshold
    can compare against; the verdict stays where the coverage lives.

    ...which was true and not enough, and it was found to be not enough twice
    over, independently, from opposite ends. Both halves are below because
    they fix different directions of the same hole.

    NEGATIVE HALF - the comparison must not succeed. The count for a
    NO_COVERAGE check used to be ``0``, and the selector grammar addresses
    ``a.b`` paths, so the first assertion pointed at ``metrics.<anything>``
    would have compared ``0 == 0`` and reported PASS on a check that examined
    nothing. Not hypothetical: ``drc_gate.py`` shipped exactly that bug on
    ``parity_difference_count`` and fixed it by writing ``null`` instead of
    ``0`` when parity was not requested. The same rule belongs here, in the
    envelope the other board checkers share, rather than being rediscovered
    per tool: an unmeasured metric is ``null``, and null does not compare
    equal to a threshold.

    POSITIVE HALF - coverage must be assertable in its own right. Making the
    metric null stops a false PASS, but it leaves a rule author with no way to
    SAY "this check ran and examined something", which is what a gate actually
    wants to claim. "Callers must read ``checks``" was an instruction nobody
    could follow: ``checks`` is a LIST and the selector grammar has only
    ``a.b`` paths and numeric indices, so it meant ``checks[3].status`` - an
    index that silently re-points at a different check the moment a tool
    reorders its findings. ``by_check`` is the same data keyed by check name,
    carrying the two fields ``metrics`` cannot: ``status`` and ``scanned``.
    Because ``Finding.__init__`` refuses to build a ``scanned == 0`` PASS, the
    single predicate ``by_check.<name>.status equals PASS`` means "this check
    ran, examined at least one item, and found nothing wrong".

    Why both: null alone turns the vacuous PASS into a TYPE_MISMATCH, which is
    correct but reads as a rule that is broken rather than a check that is
    empty. ``by_check`` alone would leave every existing ``metrics.*`` binding
    still comparing 0 to 0. Together the wrong assertion cannot pass and the
    right assertion can be written.

    ``by_check`` is deliberately NOT called ``coverage``: a ``metric_coverage``
    block is an ``eligible``/``scanned``/``excluded`` triple that satisfies
    ``coverage_rule_violations``, and this map is not that shape. Naming it
    ``coverage`` would invite ``coverage_status(report["coverage"])``, which
    would be wrong.
    """
    findings = list(findings)
    report = {
        "schema_version": schema_version,
        "tool": tool,
        "board": board_path,
        "params": params or {},
        "metrics": {f["check"]: (None if f["status"] == NO_COVERAGE
                                 else len(f["violations"]))
                    for f in findings},
        "by_check": by_check_map(findings),
        "checks": findings,
        "summary": summarise(findings),
    }
    path = Path(board_path)
    if path.is_file():
        report["design"] = {"path": str(path), "sha256": sha256_file(path)}
    else:
        # Loud rather than absent: a checker that cannot hash its input still
        # produces a report, and the reader must be able to see that the
        # binding is missing instead of inferring it from a lack of field.
        report["design"] = {"path": str(board_path), "sha256": None}
    return report


def board_exit_code(report: dict) -> int:
    """0 when no check FAILed, 1 otherwise.

    DELIBERATELY UNCHANGED: NO_COVERAGE still exits 0. That is arguably a
    silent pass at the exit-code level and contradicts the bundle's own
    "scanned=0 => never GREEN" principle, but via_stub's docstring documents
    exit 0 for NO_COVERAGE as the established sibling-tool contract, so
    flipping it is a caller-visible policy change and not part of this
    refactor. Callers must check ``summary["NO_COVERAGE"]`` as well as the
    exit code. Tracked as an open item in the README.
    """
    return 1 if report["summary"][FAIL] else 0
