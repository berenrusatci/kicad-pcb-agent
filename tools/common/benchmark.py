"""Fixed problem set, repeated runs, and a measured pass rate.

WHY THIS EXISTS
---------------
Every tool in this bundle has tests, and the suite is green. That answers
"does each part behave as its author intended", which is NOT the same question
as "how often does the flow actually produce an answer, and does it produce
the SAME answer twice". Nothing here measured the second one at all.

The prompt for this came from outside: the APRIL AI Hub session on AI-driven
circuit design reported a pass-rate heat map over 56 problems run 5 times each
- 280 designs - specifically because an LLM's output is stochastic, and they
separated two axes: (a) did it produce an implementation at all, and (b) did
that implementation meet the stated constraint, with (b) a subset of (a).

OUR VERSION OF THAT QUESTION IS DIFFERENT, AND SHARPER
------------------------------------------------------
Their stochasticity is the model's. Ours should not exist: these tools are
deterministic solvers, so N repetitions of one problem must give byte-identical
reports. That makes DETERMINISM the load-bearing metric here rather than an
afterthought - a tool that answers differently on the second run is broken in
a way no single-run test can see, and several things in this bundle could
plausibly do it (dict ordering, temp-file names leaking into a report, wall
clock, an unseeded RNG, a hash of an absolute path).

So a problem here is scored on three separate axes, never collapsed into one
number:

  outcome      did the tool reach the status the problem set declares?
  determinism  were all N runs identical after volatile fields are removed?
  coverage     did it run at all, or was a dependency (ngspice, pcbnew,
               scikit-rf) absent? A missing dependency is NO_COVERAGE and is
               reported separately - counting it as a failure would make the
               pass rate a measure of what is installed.

WHAT THIS DELIBERATELY DOES NOT CLAIM
-------------------------------------
It does not measure "agent success rate". The agent's tool CHOICE is the
stochastic part of this system and it is not exercised here; this harness runs
a declared argv. Reporting a tool-level pass rate as if it were an end-to-end
agent figure would be exactly the kind of inflation the rest of this bundle
refuses, so the report names the scope explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

EXPECTED_OUTCOMES = ("GREEN", "RED", "NO_COVERAGE", "ERROR", "ANY")

# Fields that legitimately differ between two identical runs. Stripping them
# is what makes a determinism claim meaningful rather than trivially false;
# every entry here is a deliberate exemption, not a convenience.
VOLATILE_KEYS = frozenset({
    "generated_at",
    "duration_s",
    "duration_ms",
    "elapsed_s",
    "timestamp",
    "runtime_s",
    "touchstone_written",
    "written",
    "ngspice_version",      # the binary's version string, not the result
})

# Absolute paths differ per run (temp directories) without the answer
# differing. Matched anywhere in a string value.
_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\"',\s]+|/tmp/[^\"',\s]+")


class BenchmarkError(ValueError):
    """The problem set is malformed, or a run could not be scored."""


# --------------------------------------------------------------------------- #
# Problem set
# --------------------------------------------------------------------------- #
@dataclass
class Problem:
    """One declared invocation with a declared expectation."""

    problem_id: str
    tool: str
    argv: list[str]
    expect: str = "ANY"
    category: str = "uncategorised"
    timeout_s: int = 300
    note: str = ""

    def __post_init__(self) -> None:
        if not self.problem_id.strip():
            raise BenchmarkError("problem_id must be non-empty")
        if not self.tool.strip():
            raise BenchmarkError(f"{self.problem_id}: tool must be non-empty")
        if not self.argv:
            raise BenchmarkError(
                f"{self.problem_id}: argv must be non-empty; a problem with no "
                "arguments measures nothing")
        if self.expect not in EXPECTED_OUTCOMES:
            raise BenchmarkError(
                f"{self.problem_id}: expect must be one of {EXPECTED_OUTCOMES}, "
                f"got {self.expect!r}")
        if self.timeout_s <= 0:
            raise BenchmarkError(f"{self.problem_id}: timeout must be positive")


def parse_problem(raw: Mapping[str, Any]) -> Problem:
    known = {"id", "tool", "argv", "expect", "category", "timeout_s", "note"}
    unknown = set(raw) - known
    if unknown:
        raise BenchmarkError(
            f"unknown problem keys {sorted(unknown)}; a typo here would "
            "silently drop the setting it was meant to apply")
    for required in ("id", "tool", "argv"):
        if required not in raw:
            raise BenchmarkError(f"problem is missing {required!r}")
    return Problem(
        problem_id=str(raw["id"]),
        tool=str(raw["tool"]),
        argv=[str(item) for item in raw["argv"]],       # type: ignore[union-attr]
        expect=str(raw.get("expect", "ANY")),
        category=str(raw.get("category", "uncategorised")),
        timeout_s=int(raw.get("timeout_s", 300)),       # type: ignore[arg-type]
        note=str(raw.get("note", "")),
    )


def load_problem_set(path: Path) -> tuple[list[Problem], dict[str, Any]]:
    """Read and validate a problem set. Duplicate ids are refused."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read problem set {path}: {exc}") from exc
    if not isinstance(raw, Mapping) or "problems" not in raw:
        raise BenchmarkError("problem set needs a top-level 'problems' list")
    problems = [parse_problem(item) for item in raw["problems"]]
    if not problems:
        raise BenchmarkError(
            "the problem set is empty; a benchmark over nothing reports 100%")
    seen: set[str] = set()
    for problem in problems:
        if problem.problem_id in seen:
            raise BenchmarkError(f"duplicate problem id: {problem.problem_id}")
        seen.add(problem.problem_id)
    metadata = {key: value for key, value in raw.items() if key != "problems"}
    return problems, metadata


# --------------------------------------------------------------------------- #
# Canonicalisation for the determinism check
# --------------------------------------------------------------------------- #
def canonical_report(payload: Any) -> Any:
    """Strip the fields that may legitimately differ between two runs."""
    if isinstance(payload, Mapping):
        return {key: canonical_report(value)
                for key, value in sorted(payload.items())
                if key not in VOLATILE_KEYS}
    if isinstance(payload, list):
        return [canonical_report(item) for item in payload]
    if isinstance(payload, str):
        return _PATH_PATTERN.sub("<path>", payload)
    return payload


def report_fingerprint(stdout: str) -> str | None:
    """SHA-256 of the canonicalised report, or None when stdout is not JSON.

    None rather than hashing the raw text: a tool that failed to emit JSON has
    no comparable answer, and hashing its error text would report two
    identically-broken runs as "deterministic".
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    canonical = json.dumps(canonical_report(payload), sort_keys=True,
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class Run:
    exit_code: int
    status: str
    duration_ms: float
    fingerprint: str | None
    reason_codes: list[str] = field(default_factory=list)
    stderr_tail: str = ""


# This bundle has TWO verdict vocabularies. The gate-style tools emit
# GREEN/RED/NO_COVERAGE directly; the ones built on common/findings.py emit
# PASS/FAIL/NO_COVERAGE inside a findings list. Reading only the first
# vocabulary scored every findings-based tool as a failure - measured, on the
# first real run of this harness, against sparam.
FINDINGS_VOCABULARY = {
    "PASS": "GREEN",
    "FAIL": "RED",
    "NO_COVERAGE": "NO_COVERAGE",
    "ERROR": "ERROR",
}


def status_from(exit_code: int, stdout: str) -> tuple[str, list[str]]:
    """The tool's own verdict, preferred over its exit code.

    The exit code collapses RED and NO_COVERAGE into 1, and the difference
    between "we looked and it failed" and "we could not look" is the whole
    point of the coverage axis, so the report body wins when it has one.
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ("ERROR" if exit_code == 2 else
                "NO_OUTPUT" if exit_code else "GREEN"), []
    if not isinstance(payload, Mapping):
        return "NO_OUTPUT", []
    reasons = [str(code) for code in (payload.get("reason_codes") or [])]

    declared = (payload.get("effective_status") or payload.get("status"))
    if isinstance(declared, str):
        return FINDINGS_VOCABULARY.get(declared, declared), reasons

    summary = payload.get("summary")
    if isinstance(summary, Mapping):
        if "green" in summary:
            return ("GREEN" if summary["green"] else "RED"), reasons
        # A findings summary is a status histogram: {"PASS": 1, "FAIL": 0, ...}
        if any(key in summary for key in FINDINGS_VOCABULARY):
            if summary.get("FAIL"):
                return "RED", reasons
            if summary.get("ERROR"):
                return "ERROR", reasons
            if summary.get("PASS"):
                return "GREEN", reasons
            if summary.get("NO_COVERAGE"):
                return "NO_COVERAGE", reasons

    findings = payload.get("findings")
    if isinstance(findings, list) and findings:
        first = findings[0]
        if isinstance(first, Mapping) and isinstance(first.get("status"), str):
            return FINDINGS_VOCABULARY.get(first["status"],
                                           first["status"]), reasons
    return ("ERROR" if exit_code == 2 else "RED" if exit_code else "GREEN"), \
        reasons


def run_once(problem: Problem, script: Path, cwd: Path) -> Run:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, str(script), *problem.argv],
            capture_output=True, text=True, encoding="utf-8",
            timeout=problem.timeout_s, cwd=str(cwd), check=False)
    except subprocess.TimeoutExpired:
        elapsed = (time.perf_counter() - started) * 1000.0
        return Run(exit_code=124, status="TIMEOUT", duration_ms=elapsed,
                   fingerprint=None,
                   stderr_tail=f"timed out after {problem.timeout_s}s")
    elapsed = (time.perf_counter() - started) * 1000.0
    status, reasons = status_from(proc.returncode, proc.stdout)
    return Run(exit_code=proc.returncode, status=status, duration_ms=elapsed,
               fingerprint=report_fingerprint(proc.stdout),
               reason_codes=reasons, stderr_tail=(proc.stderr or "")[-400:])


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
NO_COVERAGE_STATUSES = frozenset({"NO_COVERAGE", "SKIPPED"})


def score_problem(problem: Problem, runs: Sequence[Run]) -> dict[str, Any]:
    """Three independent axes. They are never collapsed into one number."""
    if not runs:
        raise BenchmarkError(f"{problem.problem_id}: no runs to score")

    statuses = [run.status for run in runs]
    # NO_COVERAGE is normally excluded from the denominator, because "the
    # dependency is not installed" should not read as "the tool failed". But
    # when the problem EXPECTS a refusal, that same status is the correct
    # answer - excluding it made a correctly-refusing tool unscoreable, with
    # pass_rate None and the problem filed under "uncovered".
    expects_refusal = problem.expect in ("NO_COVERAGE", "ERROR")
    if expects_refusal:
        covered = list(runs)
    else:
        covered = [run for run in runs
                   if run.status not in NO_COVERAGE_STATUSES]

    if problem.expect == "ANY":
        matched = [run for run in covered if run.status not in ("ERROR",
                                                                "NO_OUTPUT",
                                                                "TIMEOUT")]
    else:
        matched = [run for run in covered if run.status == problem.expect]

    fingerprints = {run.fingerprint for run in runs}
    unreadable = any(run.fingerprint is None for run in runs)
    # TRI-STATE, and the third state matters. A tool invoked in --self-test
    # mode prints human text rather than a JSON report, so there is nothing to
    # compare: that is UNKNOWN, not "nondeterministic". Collapsing the two
    # flagged 20 perfectly well-behaved tools on the first real run - the gate
    # would have been reporting a defect for every self-test in the set.
    deterministic: bool | None
    if unreadable:
        deterministic = None
    else:
        deterministic = len(fingerprints) == 1

    durations = sorted(run.duration_ms for run in runs)
    reason_histogram: dict[str, int] = {}
    for run in runs:
        for code in run.reason_codes:
            reason_histogram[code] = reason_histogram.get(code, 0) + 1

    return {
        "id": problem.problem_id,
        "tool": problem.tool,
        "category": problem.category,
        "expect": problem.expect,
        "runs": len(runs),
        "statuses": statuses,
        "covered_runs": len(covered),
        "matched_runs": len(matched),
        # Pass rate is over COVERED runs only; dividing by all runs would make
        # the number depend on which optional binaries happen to be installed.
        "pass_rate": (len(matched) / len(covered)) if covered else None,
        "coverage_rate": len(covered) / len(runs),
        "deterministic": deterministic,
        "distinct_fingerprints": len(fingerprints),
        "fingerprint_unreadable": unreadable,
        "duration_ms": {
            "min": durations[0],
            "median": durations[len(durations) // 2],
            "max": durations[-1],
        },
        "reason_codes": reason_histogram,
        "note": problem.note,
    }


def aggregate(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Roll the per-problem scores up, keeping the axes separate."""
    if not scores:
        raise BenchmarkError("nothing to aggregate")

    covered = [score for score in scores if score["covered_runs"]]
    fully_passing = [score for score in covered if score["pass_rate"] == 1.0]
    partially = [score for score in covered
                 if score["pass_rate"] not in (None, 0.0, 1.0)]
    failing = [score for score in covered if score["pass_rate"] == 0.0]
    nondeterministic = [score for score in scores
                        if score["deterministic"] is False]
    determinism_unknown = [score for score in scores
                           if score["deterministic"] is None]
    uncovered = [score for score in scores if not score["covered_runs"]]

    by_category: dict[str, dict[str, int]] = {}
    for score in scores:
        bucket = by_category.setdefault(
            str(score["category"]),
            {"problems": 0, "fully_passing": 0, "nondeterministic": 0,
             "determinism_unknown": 0, "uncovered": 0})
        bucket["problems"] += 1
        if score in fully_passing:
            bucket["fully_passing"] += 1
        if score["deterministic"] is False:
            bucket["nondeterministic"] += 1
        if score["deterministic"] is None:
            bucket["determinism_unknown"] += 1
        if not score["covered_runs"]:
            bucket["uncovered"] += 1

    reason_histogram: dict[str, int] = {}
    for score in scores:
        for code, count in score["reason_codes"].items():
            reason_histogram[code] = reason_histogram.get(code, 0) + count

    return {
        "problems": len(scores),
        "covered_problems": len(covered),
        "uncovered_problems": len(uncovered),
        "fully_passing": len(fully_passing),
        "partially_passing": len(partially),
        "failing": len(failing),
        "nondeterministic": len(nondeterministic),
        "nondeterministic_ids": [str(s["id"]) for s in nondeterministic],
        "determinism_unknown": len(determinism_unknown),
        "determinism_unknown_ids": [str(s["id"]) for s in determinism_unknown],
        "uncovered_ids": [str(s["id"]) for s in uncovered],
        # DENOMINATOR: covered problems, NOT all problems. An uncovered
        # problem leaves the ratio entirely rather than counting as a loss,
        # which is deliberate (see score_problem: "the dependency is not
        # installed" must not read as "the tool failed") but is easy to
        # OVERREAD: 19 of 20 tools present and one missing gives 1.0, and
        # --min-problem-pass-rate therefore cannot see a shrinking problem
        # set. The definition is left alone; the coverage axis is published
        # separately and must be read WITH it - covered_problems,
        # uncovered_problems and uncovered_ids are right here, and
        # benchmark.py's status now demotes to NO_COVERAGE when
        # uncovered_problems is non-zero, which is where that gap is caught.
        "problem_pass_rate": (len(fully_passing) / len(covered)
                              if covered else None),
        "by_category": by_category,
        "reason_codes": reason_histogram,
    }


def heat_map(scores: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The compact per-problem view: the shape the APRIL session reported.

    Kept as structured rows rather than rendered characters so a consumer can
    sort or filter it; a picture is not a report.
    """
    return [
        {
            "id": score["id"],
            "category": score["category"],
            "passed": score["matched_runs"],
            "of": score["covered_runs"],
            "deterministic": score["deterministic"],
        }
        for score in scores
    ]
