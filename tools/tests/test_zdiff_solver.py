#!/usr/bin/env python3
"""Tests for the differential impedance solver.

zdiff_solver had no external tests. Worse, ``run_self_tests()`` executed on
EVERY invocation and a run with no target printed "self-tests completed" and
exited 0 - success while doing no work. Both are pinned here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

import zdiff_solver as zs  # noqa: E402
from common.si_models import ModelDomainError  # noqa: E402

SCRIPT = SCRIPTS / "zdiff_solver.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        """Promote run_self_tests() into the real suite."""
        zs.run_self_tests()

    def test_fault_injection_proof_rejects_a_wrong_coefficient(self):
        """The reference test must be provably non-empty."""
        self.assertTrue(zs._prove_test_not_empty())

    def test_self_test_flag_exits_zero(self):
        proc = run("--self-test")
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_deprecated_selftest_spelling_still_works(self):
        self.assertEqual(0, run("--selftest").returncode)


class Monotonicity(unittest.TestCase):
    def test_zdiff_increases_with_dielectric_height(self):
        self.assertGreater(zs.zdiff_microstrip(0.4, 0.4, 0.035, 1.6, 4.5),
                           zs.zdiff_microstrip(0.4, 0.4, 0.035, 0.8, 4.5))

    def test_zdiff_decreases_with_width(self):
        self.assertLess(zs.zdiff_microstrip(0.5, 0.4, 0.035, 1.6, 4.5),
                        zs.zdiff_microstrip(0.3, 0.4, 0.035, 1.6, 4.5))

    def test_zdiff_increases_with_gap(self):
        self.assertGreater(zs.zdiff_microstrip(0.4, 0.6, 0.035, 1.6, 4.5),
                           zs.zdiff_microstrip(0.4, 0.2, 0.035, 1.6, 4.5))


class DomainGuard(unittest.TestCase):
    def test_out_of_domain_geometry_raises(self):
        """The guard kept from this tool and given to crosstalk_thinning."""
        with self.assertRaises(ModelDomainError):
            zs.z0_microstrip(w=5.0, t=0.035, h=0.05, er=4.5)

    def test_non_positive_inputs_are_rejected(self):
        for kwargs in ({"w": 0, "t": 0.035, "h": 1.6, "er": 4.5},
                       {"w": 0.4, "t": 0.035, "h": -1.0, "er": 4.5}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    zs.z0_microstrip(**kwargs)


class CliContract(unittest.TestCase):
    def test_missing_target_is_exit_2_not_a_silent_success(self):
        """It used to print 'self-tests completed' and exit 0."""
        proc = run()
        self.assertEqual(2, proc.returncode)
        self.assertNotIn("self-tests completed", proc.stdout)

    def test_target_is_positional(self):
        proc = run("90")
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(90.0, report["target_ohm"])

    def test_deprecated_target_flag_still_works(self):
        self.assertEqual(0, run("--target", "90").returncode)

    def test_output_carries_schema_and_signoff_labelling(self):
        """The README always claimed these; the code never emitted them."""
        report = json.loads(run("90").stdout)
        self.assertEqual("zdiff-solve-report/v1", report["schema_version"])
        self.assertEqual("exploratory_closed_form", report["model_class"])
        self.assertIs(False, report["signoff"])

    def test_stdout_is_pure_json_and_the_table_goes_to_stderr(self):
        proc = run("90")
        json.loads(proc.stdout)                      # must not raise
        self.assertIn("reachable span", proc.stderr)

    def test_json_flag_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "z.json"
            proc = run("90", "--json", str(out))
            self.assertEqual(0, proc.returncode)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_self_test_does_not_run_during_a_normal_solve(self):
        """Self-tests are opt-in now; a solve must not print their output."""
        proc = run("90")
        self.assertNotIn("SELF-TEST PASS", proc.stdout)
        self.assertNotIn("SELF-TEST PASS", proc.stderr)


if __name__ == "__main__":
    unittest.main()
