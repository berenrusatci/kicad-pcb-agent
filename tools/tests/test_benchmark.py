#!/usr/bin/env python3
"""Tests for the benchmark harness (common/benchmark.py + scripts/benchmark.py).

This harness measures other tools, so its own failure modes are the dangerous
kind: a scoring bug here does not crash, it reports a wrong pass rate that
looks authoritative. The first real run produced three of them, and each has a
test below that would have caught it:

  * every findings-based tool scored as FAILING, because only the
    GREEN/RED/NO_COVERAGE vocabulary was read and not the PASS/FAIL one
  * twenty well-behaved self-tests scored as NONDETERMINISTIC, because
    "printed no JSON to compare" was collapsed into "differed between runs"
  * a problem that correctly EXPECTED a refusal was unscoreable, because
    NO_COVERAGE runs were excluded from the denominator unconditionally
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

from common.benchmark import (  # noqa: E402
    BenchmarkError,
    Problem,
    Run,
    aggregate,
    canonical_report,
    heat_map,
    load_problem_set,
    parse_problem,
    report_fingerprint,
    score_problem,
    status_from,
)

SCRIPT = SCRIPTS / "benchmark.py"
PROBLEM_SET = SCRIPTS.parent / "fixtures" / "benchmark" / "problems.json"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def make_run(status="GREEN", fingerprint="abc", exit_code=0, reasons=()):
    return Run(exit_code=exit_code, status=status, duration_ms=1.0,
               fingerprint=fingerprint, reason_codes=list(reasons))


class StatusVocabulary(unittest.TestCase):
    """BUG 1: only one of the two verdict vocabularies was read."""

    def test_a_gate_style_status_is_read(self):
        self.assertEqual(("GREEN", []),
                         status_from(0, json.dumps({"status": "GREEN"})))

    def test_a_findings_status_is_translated(self):
        """common/findings.py says PASS, the gates say GREEN."""
        payload = json.dumps({"findings": [{"status": "PASS"}]})
        self.assertEqual("GREEN", status_from(0, payload)[0])

    def test_a_findings_summary_histogram_is_read(self):
        """sparam emits {"PASS": 1, "FAIL": 0, "NO_COVERAGE": 0}."""
        payload = json.dumps({"summary": {"PASS": 1, "FAIL": 0,
                                          "NO_COVERAGE": 0}})
        self.assertEqual("GREEN", status_from(0, payload)[0])

    def test_a_failing_findings_summary_is_red(self):
        payload = json.dumps({"summary": {"PASS": 0, "FAIL": 1,
                                          "NO_COVERAGE": 0}})
        self.assertEqual("RED", status_from(1, payload)[0])

    def test_no_coverage_survives_the_exit_code(self):
        """RED and NO_COVERAGE both exit 1; the difference is the point."""
        payload = json.dumps({"status": "NO_COVERAGE"})
        self.assertEqual("NO_COVERAGE", status_from(1, payload)[0])

    def test_effective_status_wins_over_status(self):
        payload = json.dumps({"status": "GREEN", "effective_status": "RED"})
        self.assertEqual("RED", status_from(0, payload)[0])

    def test_reason_codes_are_carried_through(self):
        payload = json.dumps({"status": "RED",
                              "reason_codes": ["TARGET_NOT_MET"]})
        self.assertEqual(["TARGET_NOT_MET"], status_from(1, payload)[1])

    def test_non_json_output_with_exit_two_is_an_error(self):
        self.assertEqual("ERROR", status_from(2, "traceback...")[0])

    def test_non_json_output_with_exit_one_is_no_output(self):
        self.assertEqual("NO_OUTPUT", status_from(1, "something went wrong")[0])


class DeterminismIsTriState(unittest.TestCase):
    """BUG 2: 'nothing to compare' was scored as 'differed'."""

    PROBLEM = Problem(problem_id="p", tool="t", argv=["--x"])

    def test_identical_fingerprints_are_deterministic(self):
        score = score_problem(self.PROBLEM,
                              [make_run(fingerprint="a"),
                               make_run(fingerprint="a")])
        self.assertIs(True, score["deterministic"])

    def test_differing_fingerprints_are_not_deterministic(self):
        score = score_problem(self.PROBLEM,
                              [make_run(fingerprint="a"),
                               make_run(fingerprint="b")])
        self.assertIs(False, score["deterministic"])

    def test_absent_fingerprints_are_unknown_not_false(self):
        """A --self-test prints text, so there is nothing to compare."""
        score = score_problem(self.PROBLEM,
                              [make_run(fingerprint=None),
                               make_run(fingerprint=None)])
        self.assertIsNone(score["deterministic"])
        self.assertTrue(score["fingerprint_unreadable"])

    def test_the_aggregate_counts_the_three_states_separately(self):
        scores = [
            score_problem(self.PROBLEM, [make_run(fingerprint="a")] * 2),
            score_problem(self.PROBLEM, [make_run(fingerprint="a"),
                                         make_run(fingerprint="b")]),
            score_problem(self.PROBLEM, [make_run(fingerprint=None)] * 2),
        ]
        summary = aggregate(scores)
        self.assertEqual(1, summary["nondeterministic"])
        self.assertEqual(1, summary["determinism_unknown"])


class ExpectedRefusals(unittest.TestCase):
    """BUG 3: a problem that expects a refusal was unscoreable."""

    def test_an_expected_no_coverage_can_pass(self):
        problem = Problem(problem_id="p", tool="t", argv=["--x"],
                          expect="NO_COVERAGE")
        score = score_problem(problem, [make_run(status="NO_COVERAGE")] * 2)
        self.assertEqual(1.0, score["pass_rate"])
        self.assertEqual(2, score["covered_runs"])

    def test_an_expected_error_can_pass(self):
        problem = Problem(problem_id="p", tool="t", argv=["--x"],
                          expect="ERROR")
        score = score_problem(problem, [make_run(status="ERROR")] * 2)
        self.assertEqual(1.0, score["pass_rate"])

    def test_an_unexpected_no_coverage_is_still_excluded(self):
        """A missing dependency must not read as a failure."""
        problem = Problem(problem_id="p", tool="t", argv=["--x"],
                          expect="GREEN")
        score = score_problem(problem, [make_run(status="NO_COVERAGE")] * 2)
        self.assertEqual(0, score["covered_runs"])
        self.assertIsNone(score["pass_rate"])

    def test_a_wrong_verdict_fails_even_when_the_tool_ran(self):
        problem = Problem(problem_id="p", tool="t", argv=["--x"],
                          expect="RED")
        score = score_problem(problem, [make_run(status="GREEN")] * 2)
        self.assertEqual(0.0, score["pass_rate"])


class Fingerprinting(unittest.TestCase):
    def test_volatile_fields_are_stripped(self):
        first = {"status": "GREEN", "generated_at": "a", "duration_s": 1}
        second = {"status": "GREEN", "generated_at": "b", "duration_s": 2}
        self.assertEqual(canonical_report(first), canonical_report(second))

    def test_a_real_difference_survives(self):
        """The stripping must not be aggressive enough to hide an answer."""
        self.assertNotEqual(canonical_report({"z0_ohm": 50.0}),
                            canonical_report({"z0_ohm": 51.0}))

    def test_temp_paths_do_not_break_determinism(self):
        first = report_fingerprint(json.dumps({"out": "C:/tmp/aaa/r.json"}))
        second = report_fingerprint(json.dumps({"out": "C:/tmp/bbb/r.json"}))
        self.assertEqual(first, second)

    def test_nested_volatile_fields_are_stripped(self):
        first = {"grid": {"nodes": 10, "duration_ms": 5}}
        second = {"grid": {"nodes": 10, "duration_ms": 9}}
        self.assertEqual(canonical_report(first), canonical_report(second))

    def test_list_order_is_preserved(self):
        """Sorting a list would hide a genuinely reordered result."""
        self.assertNotEqual(canonical_report({"a": [1, 2]}),
                            canonical_report({"a": [2, 1]}))

    def test_non_json_output_has_no_fingerprint(self):
        self.assertIsNone(report_fingerprint("SELF-TEST PASS [x]"))

    def test_two_identically_broken_runs_are_not_deterministic(self):
        problem = Problem(problem_id="p", tool="t", argv=["--x"])
        score = score_problem(problem, [make_run(fingerprint=None)] * 2)
        self.assertIsNone(score["deterministic"])


class ProblemSetValidation(unittest.TestCase):
    def test_a_problem_needs_an_argv(self):
        with self.assertRaises(BenchmarkError):
            Problem(problem_id="p", tool="t", argv=[])

    def test_an_unknown_expectation_is_refused(self):
        with self.assertRaises(BenchmarkError):
            Problem(problem_id="p", tool="t", argv=["--x"], expect="MAYBE")

    def test_a_typo_in_a_key_is_refused(self):
        with self.assertRaises(BenchmarkError):
            parse_problem({"id": "p", "tool": "t", "argv": ["--x"],
                           "expects": "GREEN"})

    def test_a_missing_required_key_is_refused(self):
        with self.assertRaises(BenchmarkError):
            parse_problem({"id": "p", "tool": "t"})

    def test_an_empty_problem_set_is_refused(self):
        """A benchmark over nothing reports 100%."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"problems": []}), encoding="utf-8")
            with self.assertRaises(BenchmarkError):
                load_problem_set(path)

    def test_duplicate_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            path.write_text(json.dumps({"problems": [
                {"id": "same", "tool": "t", "argv": ["--x"]},
                {"id": "same", "tool": "t", "argv": ["--y"]}]}),
                encoding="utf-8")
            with self.assertRaises(BenchmarkError):
                load_problem_set(path)


class CommittedProblemSet(unittest.TestCase):
    """The shipped set itself, as data."""

    def setUp(self):
        self.problems, self.metadata = load_problem_set(PROBLEM_SET)

    def test_it_loads_and_is_not_trivial(self):
        self.assertGreaterEqual(len(self.problems), 20)

    def test_every_tool_named_is_in_the_registry(self):
        registry = json.loads(
            (SCRIPTS.parent.parent / "config" / "tool_registry.json")
            .read_text(encoding="utf-8"))
        names = {tool["name"] for tool in registry["tools"]}
        for problem in self.problems:
            with self.subTest(problem=problem.problem_id):
                self.assertIn(problem.tool, names)

    def test_it_contains_fault_injections(self):
        """A set of only happy paths cannot tell a working gate from one
        wired to always pass."""
        refusals = [p for p in self.problems
                    if p.expect in ("RED", "NO_COVERAGE", "ERROR")]
        self.assertGreaterEqual(len(refusals), 3, [p.problem_id for p in refusals])

    def test_more_than_one_category_is_covered(self):
        categories = {problem.category for problem in self.problems}
        self.assertGreaterEqual(len(categories), 8, sorted(categories))


class Aggregation(unittest.TestCase):
    PROBLEM = Problem(problem_id="p", tool="t", argv=["--x"], category="c")

    def test_aggregating_nothing_is_refused(self):
        with self.assertRaises(BenchmarkError):
            aggregate([])

    def test_the_pass_rate_is_null_when_nothing_is_covered(self):
        """Null, not 0: 'we could not look' is not 'everything failed'."""
        score = score_problem(
            Problem(problem_id="p", tool="t", argv=["--x"], expect="GREEN"),
            [make_run(status="NO_COVERAGE")])
        self.assertIsNone(aggregate([score])["problem_pass_rate"])

    def test_reason_codes_are_histogrammed(self):
        score = score_problem(self.PROBLEM,
                              [make_run(reasons=["A"]), make_run(reasons=["A", "B"])])
        summary = aggregate([score])
        self.assertEqual(2, summary["reason_codes"]["A"])
        self.assertEqual(1, summary["reason_codes"]["B"])

    def test_the_heat_map_has_one_row_per_problem(self):
        scores = [score_problem(self.PROBLEM, [make_run()] * 2) for _ in range(3)]
        self.assertEqual(3, len(heat_map(scores)))


class SelfTest(unittest.TestCase):
    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)


class CliContract(unittest.TestCase):
    def test_one_repetition_is_refused(self):
        """A single run cannot detect nondeterminism, which is the point."""
        proc = run("--repetitions", "1")
        self.assertEqual(2, proc.returncode)
        self.assertIn("nondeterminism", proc.stderr)

    def test_an_unmatched_filter_is_an_error(self):
        self.assertEqual(2, run("--only", "no-such-problem").returncode)

    def test_a_filtered_run_produces_a_valid_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "b.json"
            proc = run("--only", "ipc", "--quiet", "--json", str(out))
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("benchmark-report/v1", report["schema_version"])
            self.assertEqual("tool_invocations", report["scope"])
            self.assertTrue(report["problems"])

    def test_the_field_solver_problems_are_deterministic(self):
        """These emit JSON, so determinism is actually MEASURED here rather
        than reported unknown - and a solver that drifts is a defect."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "b.json"
            run("--only", "field-solve", "--quiet", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            measured = [p for p in report["problems"]
                        if p["deterministic"] is not None]
            self.assertTrue(measured, "no determinism was measurable")
            for problem in measured:
                with self.subTest(problem=problem["id"]):
                    self.assertIs(True, problem["deterministic"])

    def test_stdout_is_pure_json(self):
        proc = run("--only", "ipc", "--quiet")
        json.loads(proc.stdout)

    def test_one_uncovered_problem_beside_a_passing_one_is_no_coverage(self):
        """BUG 4, the same shape as the three in the module docstring: the
        rollup read ``covered_problems == 0`` and nothing else, so coverage
        only moved the verdict when the WHOLE set was uncovered. A set of two
        problems where one tool is missing scored GREEN - and the pass rate
        agreed, because it divides by covered problems. One unmeasured problem
        beside a measured clean one is NO_COVERAGE, never GREEN."""
        payload = {
            "schema_version": "pcb-benchmark-problems/v1",
            "problems": [
                {"id": "covered", "tool": "stackup-plan", "category": "stackup",
                 "argv": ["--self-test"], "expect": "GREEN"},
                {"id": "uncovered", "tool": "no-such-registered-tool",
                 "category": "stackup", "argv": ["--self-test"],
                 "expect": "GREEN"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            problems = Path(tmp) / "problems.json"
            problems.write_text(json.dumps(payload), encoding="utf-8")
            proc = run("--problems", str(problems), "--quiet")
        report = json.loads(proc.stdout)
        self.assertEqual(1, report["summary"]["fully_passing"])
        self.assertEqual(1, report["summary"]["uncovered_problems"])
        self.assertEqual(1.0, report["summary"]["problem_pass_rate"],
                         "the pass rate is over COVERED problems - this is "
                         "exactly why the status has to carry coverage")
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertIn("PROBLEM_UNCOVERED", report["reason_codes"])
        self.assertNotIn("NOTHING_COVERED", report["reason_codes"])
        self.assertEqual(1, proc.returncode)

    def test_a_fully_covered_passing_set_is_still_green(self):
        """The rollup tightened, it did not become unsatisfiable."""
        payload = {
            "schema_version": "pcb-benchmark-problems/v1",
            "problems": [
                {"id": "covered", "tool": "stackup-plan", "category": "stackup",
                 "argv": ["--self-test"], "expect": "GREEN"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            problems = Path(tmp) / "problems.json"
            problems.write_text(json.dumps(payload), encoding="utf-8")
            proc = run("--problems", str(problems), "--quiet")
        report = json.loads(proc.stdout)
        self.assertEqual("GREEN", report["status"], report["summary"])
        self.assertEqual([], report["reason_codes"])
        self.assertEqual(0, proc.returncode)


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(
            (SCHEMAS / "benchmark_report.schema.json").read_text(
                encoding="utf-8"))

    def test_schema_pins_the_version_and_scope(self):
        self.assertEqual("benchmark-report/v1",
                         self.schema["properties"]["schema_version"]["const"])
        self.assertEqual("tool_invocations",
                         self.schema["properties"]["scope"]["const"])

    def test_repetitions_must_be_at_least_two(self):
        self.assertEqual(
            2, self.schema["properties"]["repetitions"]["minimum"])

    def test_determinism_is_nullable_in_the_schema(self):
        nullable = (self.schema["properties"]["problems"]["items"]
                    ["properties"]["deterministic"]["type"])
        self.assertIn("null", nullable)

    def test_reason_codes_emitted_are_declared(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        self.assertEqual({"NOTHING_COVERED", "PROBLEM_UNCOVERED",
                          "NONDETERMINISTIC_TOOL", "PROBLEM_FAILED",
                          "PASS_RATE_BELOW_MINIMUM"},
                         declared)

    def test_a_real_report_satisfies_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "b.json"
            run("--only", "ipc", "--quiet", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        for key in self.schema["required"]:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
