#!/usr/bin/env python3
"""Tests for the EMI/EMC rule engine.

Ported from the source pytest suite (``test_emi_emc_kural_motoru.py``) to
unittest. The source tool ran its self-tests on EVERY invocation, printed
"ÖZ-TEST PASS" onto stdout ahead of the JSON, took only optional flags (so a
bare run "succeeded" while auditing nothing) and used ``--json`` as a write
flag with a chatty confirmation line. All four are pinned here.
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

import emc_rules as er  # noqa: E402

SCRIPT = SCRIPTS / "emc_rules.py"

PROBLEM = {
    "trace_width_mm": 0.2,
    "dielectric_thickness_mm": 0.1,
    "knee_frequency_ghz": 5.0,
    "er_eff": 4.5,
    "trace_pairs": [
        {"trace_a": "D_P", "trace_b": "D_N",
         "width_a_mm": 0.2, "width_b_mm": 0.2, "center_to_center_mm": 0.9},
    ],
    "plane_pairs": [
        {"layer_pair": "F.Cu-In1.Cu", "dielectric_thickness_mm": 0.1,
         "power_setback_mm": 12.0, "ground_setback_mm": 5.0},
    ],
    "via_pitches_mm": [1.0, 1.2],
}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def write_problem(directory: str, problem: dict) -> Path:
    path = Path(directory) / "problem.json"
    path.write_text(json.dumps(problem, ensure_ascii=False), encoding="utf-8")
    return path


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        """Promote run_self_tests() into the real suite."""
        self.assertEqual([], er.run_self_tests())

    def test_fault_injection_proof_rejects_vacuous_boundary_tests(self):
        self.assertTrue(er.prove_tests_not_empty())

    def test_self_test_flag_exits_zero_without_a_problem_file(self):
        proc = run("--self-test")
        self.assertEqual(0, proc.returncode, proc.stderr)


class ThreeWRule(unittest.TestCase):
    def test_basic_multiplication(self):
        target = er.three_w(0.2, factor=3.0)
        self.assertAlmostEqual(0.6, target.min_center_to_center_mm)
        self.assertAlmostEqual(0.4, target.min_edge_to_edge_mm)

    def test_negative_width_is_rejected(self):
        with self.assertRaises(ValueError):
            er.three_w(-0.1)

    def test_factor_below_one_is_rejected(self):
        """A factor under 1.0 makes crosstalk WORSE, so it cannot be a target."""
        with self.assertRaises(ValueError):
            er.three_w(0.2, factor=0.5)

    def test_mixed_width_uses_the_mean(self):
        target = er.three_w_mixed_width(0.1, 0.3, factor=3.0)
        self.assertAlmostEqual(0.2, target.trace_width_mm)

    def test_check_catches_a_violation(self):
        finding = er.check_three_w(
            [er.TracePairMeasurement("D_P", "D_N", 0.2, 0.2, 0.3)])  # target 0.6
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual("D_P", finding["violations"][0]["a"])


class TwentyHRule(unittest.TestCase):
    def test_dielectric_must_be_positive(self):
        with self.assertRaises(ValueError):
            er.twenty_h_setback_mm(0)

    def test_reversed_plane_order_is_caught(self):
        """The power setback can be 'plenty big' yet still not be INSIDE GND;
        that must FAIL too, because it is a different root cause."""
        finding = er.check_twenty_h([er.PlanePairMeasurement(
            "F.Cu-In1.Cu", 0.1, power_setback_mm=10.0, ground_setback_mm=9.5)])
        self.assertEqual("FAIL", finding["status"])
        self.assertTrue(any("not far enough inside GND" in problem
                            for problem in finding["violations"][0]["problems"]))

    def test_setback_scales_with_dielectric(self):
        self.assertEqual(2 * er.twenty_h_setback_mm(0.1).min_setback_mm,
                         er.twenty_h_setback_mm(0.2).min_setback_mm)


class ViaStitching(unittest.TestCase):
    def test_higher_frequency_demands_a_tighter_pitch(self):
        low = er.stitching_max_pitch_mm(1.0)
        high = er.stitching_max_pitch_mm(10.0)
        self.assertLess(high.max_pitch_mm, low.max_pitch_mm)

    def test_check_catches_an_over_pitch(self):
        target = er.stitching_max_pitch_mm(5.0)
        finding = er.check_via_pitch([target.max_pitch_mm * 2], 5.0)
        self.assertEqual("FAIL", finding["status"])

    def test_non_positive_inputs_are_rejected(self):
        for kwargs in ({"knee_frequency_ghz": 0.0},
                       {"knee_frequency_ghz": 5.0, "er_eff": 0.0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    er.stitching_max_pitch_mm(**kwargs)


class EmptyInputIsNeverAPass(unittest.TestCase):
    def test_every_check_reports_no_coverage_when_empty(self):
        for finding in (er.check_three_w([]),
                        er.check_twenty_h([]),
                        er.check_via_pitch([], 5.0)):
            with self.subTest(check=finding["check"]):
                self.assertEqual("NO_COVERAGE", finding["status"])
                self.assertEqual(0, finding["scanned"])


class OverallStatus(unittest.TestCase):
    def test_precedence_fail_then_needs_human_then_pass(self):
        fail = er.Finding("x", "FAIL", 1, [{"m": 1}], "")
        empty = er.Finding("y", "NO_COVERAGE", 0, [], "")
        ok = er.Finding("z", "PASS", 1, [], "")
        self.assertEqual("FAIL", er.overall_status([fail, empty, ok]))
        self.assertEqual("NEEDS_HUMAN", er.overall_status([empty, ok]))
        self.assertEqual("PASS", er.overall_status([ok]))

    def test_only_pass_exits_zero(self):
        self.assertEqual(0, er.exit_code_for("PASS"))
        self.assertEqual(1, er.exit_code_for("FAIL"))
        self.assertEqual(1, er.exit_code_for("NEEDS_HUMAN"))


class CliContract(unittest.TestCase):
    def test_missing_positional_is_exit_2(self):
        """It used to print a self-test banner and exit 0."""
        proc = run()
        self.assertEqual(2, proc.returncode)
        self.assertNotIn("ÖZ-TEST", proc.stdout)

    def test_unreadable_positional_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(Path(tmp) / "missing.json"))
            self.assertEqual(2, proc.returncode)

    def test_invalid_json_is_exit_2_not_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(2, run(str(bad)).returncode)

    def test_clean_problem_exits_zero_and_stdout_is_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, PROBLEM)))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual("PASS", report["status"])
            self.assertEqual("emc-rules-report/v1", report["schema_version"])
            self.assertIs(False, report["signoff"])

    def test_violating_problem_exits_one(self):
        problem = dict(PROBLEM)
        problem["trace_pairs"] = [{"trace_a": "A", "trace_b": "B",
                                   "width_a_mm": 0.2, "width_b_mm": 0.2,
                                   "center_to_center_mm": 0.3}]
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, problem)))
            self.assertEqual(1, proc.returncode)
            self.assertEqual("FAIL", json.loads(proc.stdout)["status"])

    def test_no_measurements_is_needs_human_not_a_silent_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_problem(tmp, {"trace_width_mm": 0.2})
            proc = run(str(path))
            self.assertEqual(1, proc.returncode)
            self.assertEqual("NEEDS_HUMAN", json.loads(proc.stdout)["status"])

    def test_flags_override_the_problem_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_problem(tmp, PROBLEM)
            report = json.loads(run(str(path), "--trace-width-mm", "0.5").stdout)
            self.assertEqual(0.5, report["params"]["trace_width_mm"])
            self.assertAlmostEqual(
                1.5, report["targets"]["three_w"]["min_center_to_center_mm"])

    def test_json_flag_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            proc = run(str(write_problem(tmp, PROBLEM)), "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_stdout_is_pure_json_and_diagnostics_go_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, PROBLEM)))
            json.loads(proc.stdout)                     # must not raise
            self.assertIn("status=", proc.stderr)

    def test_self_test_does_not_run_during_a_normal_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, PROBLEM)))
            self.assertNotIn("SELF-TEST", proc.stdout)
            self.assertNotIn("SELF-TEST", proc.stderr)


class SchemaFile(unittest.TestCase):
    def test_schema_exists_and_pins_the_version(self):
        schema = json.loads(
            (SCHEMAS / "emc_rules.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("emc-rules-report/v1",
                         schema["properties"]["schema_version"]["const"])
        self.assertEqual(er.SCHEMA_VERSION,
                         schema["properties"]["schema_version"]["const"])


if __name__ == "__main__":
    unittest.main()
