#!/usr/bin/env python3
"""Run the fixed problem set N times and report a measured pass rate.

The suite says every part behaves as its author intended. This says how often
the FLOW produces an answer, and whether it produces the SAME answer twice.
Those are different questions and only the first one had an answer here.

Three axes, never collapsed into one number:

  outcome      did the tool reach the status the problem set declares?
  determinism  were the N runs identical once volatile fields are stripped?
  coverage     did it run at all, or was a dependency absent?

DETERMINISM IS THE ONE THAT MATTERS MOST HERE. The APRIL AI Hub session that
prompted this ran 56 problems five times each because an LLM's output is
stochastic. These tools are deterministic solvers, so the same input must give
a byte-identical report - and if it does not, something is leaking into the
answer (dict ordering, a temp-file name, the wall clock, an unseeded RNG) and
no single-run test can see it.

SCOPE, STATED SO IT IS NOT OVERREAD: this measures TOOLS, not the agent. The
agent's tool choice is the stochastic part of this system and it is not
exercised here; the harness runs a declared argv. A tool-level pass rate is
not an end-to-end success rate and the report says so.

Exit contract: 0 pass / 1 fail or NO_COVERAGE / 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.benchmark import (  # noqa: E402
    BenchmarkError,
    Problem,
    aggregate,
    canonical_report,
    heat_map,
    load_problem_set,
    report_fingerprint,
    run_once,
    score_problem,
    status_from,
)
from common.findings import (  # noqa: E402
    FAIL, NO_COVERAGE, PASS, rollup_from_counts,
)
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)

SCHEMA_VERSION = "benchmark-report/v1"

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO = TOOLS_DIR.parent
DEFAULT_PROBLEMS = TOOLS_DIR / "fixtures" / "benchmark" / "problems.json"
REGISTRY = REPO / "config" / "tool_registry.json"

LIMITS = [
    "measures TOOLS, not the agent: the harness runs a declared argv, so this "
    "is not an end-to-end agent success rate",
    "the pass rate is over COVERED runs only; a problem whose dependency is "
    "absent is NO_COVERAGE and is excluded, so the number does not measure "
    "which optional binaries happen to be installed - read it beside "
    "summary.covered_problems/uncovered_problems, and note that the STATUS "
    "does not share that blind spot: any uncovered problem makes the verdict "
    "NO_COVERAGE, so problems dropped out of the ratio cannot hide in it",
    "determinism is checked after stripping declared volatile fields "
    "(timestamps, durations, absolute paths, binary version strings); the "
    "exemption list is in common/benchmark.VOLATILE_KEYS",
    "a problem set is a sample: it says what these invocations do, not what "
    "every invocation would do",
]


def _resolve_locators() -> dict[str, Path]:
    """Registry name -> script path. The registry is the only source."""
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read the tool registry: {exc}") from exc
    resolved: dict[str, Path] = {}
    for tool in registry.get("tools", []):
        if tool.get("kind") != "local":
            continue
        locator = str(tool.get("locator", "")).replace("\\", "/")
        if locator:
            resolved[str(tool.get("name"))] = REPO / locator
    return resolved


def _execute(problems: Sequence[Problem], repetitions: int,
             locators: dict[str, Path], only: str | None,
             progress: bool) -> list[dict]:
    scores: list[dict] = []
    selected = [p for p in problems
                if only is None or only in (p.problem_id, p.category, p.tool)]
    if not selected:
        raise BenchmarkError(f"no problem matches --only {only!r}")

    for index, problem in enumerate(selected, start=1):
        script = locators.get(problem.tool)
        if script is None or not script.is_file():
            # An unregistered or missing tool is a NO_COVERAGE problem, not a
            # crash: the benchmark reports what it could not run.
            scores.append({
                "id": problem.problem_id, "tool": problem.tool,
                "category": problem.category, "expect": problem.expect,
                "runs": 0, "statuses": [], "covered_runs": 0,
                "matched_runs": 0, "pass_rate": None, "coverage_rate": 0.0,
                "deterministic": True, "distinct_fingerprints": 0,
                "fingerprint_unreadable": False,
                "duration_ms": {"min": 0.0, "median": 0.0, "max": 0.0},
                "reason_codes": {"TOOL_NOT_FOUND": 1},
                "note": f"{problem.tool} is not a registered local tool",
            })
            continue
        runs = [run_once(problem, script, REPO) for _ in range(repetitions)]
        score = score_problem(problem, runs)
        scores.append(score)
        if progress:
            mark = ("ok " if score["pass_rate"] == 1.0
                    else "--" if score["pass_rate"] is None else "FAIL")
            drift = ("  NONDETERMINISTIC" if score["deterministic"] is False
                     else "  det?" if score["deterministic"] is None else "")
            diag(f"  [{index}/{len(selected)}] {mark} {problem.problem_id} "
                 f"({score['matched_runs']}/{score['covered_runs']}, "
                 f"{score['duration_ms']['median']:.0f} ms){drift}")
    return scores


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--problems", type=Path, default=DEFAULT_PROBLEMS)
    parser.add_argument("--repetitions", type=int, default=2,
                        help="runs per problem. 2 is the minimum that can "
                             "detect nondeterminism at all (default 2)")
    parser.add_argument("--only",
                        help="run one problem id, category or tool")
    parser.add_argument("--min-problem-pass-rate", type=float,
                        dest="min_problem_pass_rate",
                        help="fail below this fraction of fully-passing "
                             "problems, e.g. 1.0")
    parser.add_argument("--allow-nondeterminism", action="store_true",
                        dest="allow_nondeterminism",
                        help="do not fail on a differing repeat run. Off by "
                             "default: for deterministic solvers a drift is a "
                             "defect, not a tolerance")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress per-problem progress on stderr")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def run_self_tests() -> None:
    """Checks on the harness itself, not on the tools it runs."""

    def volatile_fields_are_stripped() -> None:
        first = {"status": "GREEN", "generated_at": "2026-01-01", "value": 1}
        second = {"status": "GREEN", "generated_at": "2026-12-31", "value": 1}
        assert canonical_report(first) == canonical_report(second)

    def a_real_difference_survives_canonicalisation() -> None:
        """The stripping must not be so aggressive that it hides an answer."""
        first = {"status": "GREEN", "value": 1}
        second = {"status": "GREEN", "value": 2}
        assert canonical_report(first) != canonical_report(second)

    def absolute_paths_do_not_break_determinism() -> None:
        first = report_fingerprint(json.dumps({"out": "C:/tmp/aaa/r.json"}))
        second = report_fingerprint(json.dumps({"out": "C:/tmp/bbb/r.json"}))
        assert first == second, "temp paths leaked into the fingerprint"

    def non_json_output_has_no_fingerprint() -> None:
        """Two identically-broken runs must not read as deterministic."""
        assert report_fingerprint("not json at all") is None

    def the_report_body_beats_the_exit_code() -> None:
        """RED and NO_COVERAGE both exit 1; the difference is the point."""
        status, _ = status_from(1, json.dumps({"status": "NO_COVERAGE"}))
        assert status == "NO_COVERAGE", status
        status, _ = status_from(1, json.dumps({"status": "RED"}))
        assert status == "RED", status

    def an_empty_problem_set_is_refused() -> None:
        """A benchmark over nothing would report 100%."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"problems": []}), encoding="utf-8")
            try:
                load_problem_set(path)
            except BenchmarkError:
                return
        raise AssertionError("an empty problem set was accepted")

    tests = (
        ("volatile fields are stripped", volatile_fields_are_stripped),
        ("a real difference survives", a_real_difference_survives_canonicalisation),
        ("temp paths do not break determinism", absolute_paths_do_not_break_determinism),
        ("non-JSON output has no fingerprint", non_json_output_has_no_fingerprint),
        ("report body beats exit code", the_report_body_beats_the_exit_code),
        ("an empty problem set is refused", an_empty_problem_set_is_refused),
    )
    for name, test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"SELF-TEST FAIL [{name}]: {exc}", file=sys.stderr)
            raise
        else:
            print(f"SELF-TEST PASS [{name}]")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            run_self_tests()
        except (AssertionError, BenchmarkError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    if args.repetitions < 2:
        parser.error("--repetitions must be at least 2: one run cannot "
                     "detect nondeterminism, which is the point of this tool")

    try:
        problems, metadata = load_problem_set(args.problems)
        locators = _resolve_locators()
        diag(f"benchmark: {len(problems)} problems x {args.repetitions} runs")
        scores = _execute(problems, args.repetitions, locators, args.only,
                          not args.quiet)
        summary = aggregate(scores)
    except BenchmarkError as exc:
        parser.error(str(exc))

    # WAS: `status = "GREEN"` followed by `if summary["covered_problems"] == 0:
    # status = "NO_COVERAGE"`. That is the all-or-nothing rollup: coverage only
    # moved the verdict when EVERY problem was uncovered. 19 of 20 tools
    # present and one missing (an unregistered tool, or a dependency that is
    # not installed on this machine) reported GREEN, even though
    # aggregate() had already published ``uncovered_problems`` and
    # ``uncovered_ids`` right next to it and nothing read them.
    # ``--min-problem-pass-rate`` could not catch it either, because
    # ``problem_pass_rate`` divides by COVERED problems only. So a benchmark
    # could lose a tool from the set and still print a clean bill of health.
    # The rule is now common.findings.rollup_from_counts - the same three
    # lines as rollup_status, for a caller that holds counts rather than
    # findings - and ANY uncovered problem demotes the verdict to NO_COVERAGE.
    # RED still wins over NO_COVERAGE (a measured failure outranks an
    # unmeasured one), which is why the two RED branches below reassign.
    status = {PASS: "GREEN", FAIL: "RED", NO_COVERAGE: "NO_COVERAGE"}[
        rollup_from_counts(
            passed=summary["fully_passing"],
            failed=summary["failing"] + summary["partially_passing"],
            no_coverage=summary["uncovered_problems"])]
    reason_codes: list[str] = []

    if summary["covered_problems"] == 0:
        reason_codes.append("NOTHING_COVERED")
    elif summary["uncovered_problems"]:
        # Distinct code, because it is a distinct situation: NOTHING_COVERED
        # means the harness measured nothing at all, PROBLEM_UNCOVERED means
        # it measured most of the set and lost part of it. Both are
        # NO_COVERAGE; only the second one used to be invisible.
        reason_codes.append("PROBLEM_UNCOVERED")
    if summary["nondeterministic"] and not args.allow_nondeterminism:
        status = "RED"
        reason_codes.append("NONDETERMINISTIC_TOOL")
    if summary["failing"] or summary["partially_passing"]:
        status = "RED"
        reason_codes.append("PROBLEM_FAILED")
    if (args.min_problem_pass_rate is not None
            and summary["problem_pass_rate"] is not None
            and summary["problem_pass_rate"] < args.min_problem_pass_rate):
        status = "RED"
        reason_codes.append("PASS_RATE_BELOW_MINIMUM")

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "benchmark",
        "scope": "tool_invocations",
        "signoff": False,
        "status": status,
        "effective_status": status,
        "reason_codes": sorted(set(reason_codes)),
        "repetitions": args.repetitions,
        "problem_set": str(args.problems),
        "problem_set_metadata": metadata,
        "summary": summary,
        "heat_map": heat_map(scores),
        "problems": scores,
        "limits": LIMITS,
    }

    diag("")
    diag(f"  problems           {summary['problems']}"
         f"  (covered {summary['covered_problems']}, "
         f"uncovered {summary['uncovered_problems']})"
         + (f"  {summary['uncovered_ids']}"
            if summary["uncovered_ids"] else ""))
    diag(f"  fully passing      {summary['fully_passing']}")
    diag(f"  partially passing  {summary['partially_passing']}")
    diag(f"  failing            {summary['failing']}")
    diag(f"  nondeterministic   {summary['nondeterministic']}"
         + (f"  {summary['nondeterministic_ids']}"
            if summary["nondeterministic_ids"] else ""))
    diag(f"  determinism n/a    {summary['determinism_unknown']}"
         "  (no JSON report to compare - self-test mode prints text)")
    if summary["problem_pass_rate"] is not None:
        diag(f"  problem pass rate  "
             f"{summary['problem_pass_rate'] * 100:.1f}%")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
