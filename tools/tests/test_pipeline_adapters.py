#!/usr/bin/env python3
"""Tests for scripts/pipeline_adapters.py.

The point of this module is that it FORWARDS to already-tested tools rather
than reimplementing them - these tests check the forwarding is correct
(right function called, right shape returned), not the underlying tools'
own logic (that is each tool's own test file's job).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS, TOOLS  # noqa: E402,F401

import scripts.pipeline_adapters as pa  # noqa: E402

SCRIPT = SCRIPTS / "pipeline_adapters.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class NoHardcodedSequence(unittest.TestCase):
    """The module's central claim: no function calls another."""

    def test_none_of_the_adapters_import_each_other(self):
        import ast
        import inspect

        source = inspect.getsource(pa)
        tree = ast.parse(source)
        adapter_names = {"check_environment", "check_schematic_erc",
                         "check_thermal", "plan_placement", "check_drc",
                         "produce_outputs", "recommend_via_size"}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in adapter_names:
                calls = {n.func.id for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                overlap = calls & adapter_names
                self.assertEqual(set(), overlap,
                                 f"{node.name} calls another adapter: {overlap}")


class CheckEnvironment(unittest.TestCase):
    def test_reduces_check_all_tools_output(self):
        env = pa.check_environment()
        self.assertIn("critical_ok", env)
        self.assertIn("missing_optional", env)
        self.assertIsInstance(env["statuses"], list)
        self.assertTrue(env["statuses"])

    def test_a_missing_critical_tool_is_reflected_not_exercised(self):
        fake_statuses = [
            pa.ToolStatus("KiCad CLI", False, "not found", "install it"),
            pa.ToolStatus("ngspice", True, "found", ""),
        ]
        with mock.patch.object(pa, "check_all_tools",
                               return_value=fake_statuses):
            env = pa.check_environment()
        self.assertFalse(env["critical_ok"])
        self.assertEqual([], env["missing_optional"])

    def test_a_missing_optional_tool_does_not_block_critical_ok_not_exercised(self):
        fake_statuses = [
            pa.ToolStatus("KiCad CLI", True, "found", ""),
            pa.ToolStatus("ngspice", False, "not found", "install it"),
        ]
        with mock.patch.object(pa, "check_all_tools",
                               return_value=fake_statuses):
            env = pa.check_environment()
        self.assertTrue(env["critical_ok"])
        self.assertEqual(["ngspice"], env["missing_optional"])


class CheckSchematicErc(unittest.TestCase):
    def test_forwards_to_the_real_erc_helpers_not_exercised(self):
        raw = {"sheets": [{"violations": [
            {"severity": "error", "type": "x", "description": "bad"},
            {"severity": "warning", "type": "y", "description": "meh"},
        ]}]}
        with mock.patch.object(pa, "run_erc", return_value=raw) as fake_run:
            finding = pa.check_schematic_erc("b.kicad_sch")
        fake_run.assert_called_once()
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, len(finding["violations"]))

    def test_a_clean_schematic_passes_not_exercised(self):
        with mock.patch.object(pa, "run_erc", return_value={"sheets": []}):
            finding = pa.check_schematic_erc("b.kicad_sch")
        self.assertEqual("PASS", finding["status"])


class CheckThermal(unittest.TestCase):
    def test_reaches_the_real_thermal_bridge_function(self):
        """No components -> thermal_bridge's own NO_COVERAGE path, proving
        this reached the real function and not a stub."""
        finding = pa.check_thermal([], [])
        self.assertEqual("NO_COVERAGE", finding["status"])


class CheckDrc(unittest.TestCase):
    def test_forwards_to_drc_gate_not_exercised(self):
        fake_report = {"status": "GREEN"}
        with mock.patch(
                "scripts.drc_gate.gate", return_value=fake_report) as fake_gate:
            outcome = pa.check_drc("b.kicad_pcb")
        fake_gate.assert_called_once()
        self.assertEqual(fake_report, outcome)


class ProduceOutputs(unittest.TestCase):
    def test_runs_drc_erc_and_board_checks_in_order_not_exercised(self):
        calls = []
        with mock.patch("scripts.production.outputs.run_drc",
                        side_effect=lambda *a, **k: calls.append("drc") or {}), \
             mock.patch("scripts.production.outputs.run_erc",
                        side_effect=lambda *a, **k: calls.append("erc") or {}), \
             mock.patch("scripts.production.outputs.check_report_gate",
                        side_effect=lambda raw, source, subject:
                            {"check": f"{source}_gate", "status": "PASS",
                             "scanned": 1, "violations": [], "detail": ""}), \
             mock.patch("scripts.production.outputs.check_board_checks",
                        return_value={"check": "board_checks",
                                     "status": "NO_COVERAGE", "scanned": 0,
                                     "violations": [], "detail": ""}):
            findings = pa.produce_outputs("b.kicad_pcb", "b.kicad_sch",
                                          out_dir="/tmp/does-not-matter")
        self.assertEqual(["drc", "erc"], calls)
        self.assertEqual(3, len(findings))
        self.assertEqual("drc_gate", findings[0]["check"])
        self.assertEqual("erc_gate", findings[1]["check"])


class RecommendViaSize(unittest.TestCase):
    def test_reaches_the_real_via_current_function(self):
        from common.stackup import fab_profiles
        profile_key = sorted(fab_profiles())[0]
        outcome = pa.recommend_via_size(1.0, profile_key)
        self.assertGreater(outcome.recommended_hole_mm, 0)

    def test_fault_injection_an_unknown_fab_profile_is_refused(self):
        with self.assertRaises(ValueError):
            pa.recommend_via_size(1.0, "NOT_A_REAL_FAB_PROFILE")


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        self.assertEqual([], pa.self_test())

    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)

    def test_running_with_no_flags_is_an_error_not_a_silent_pass(self):
        proc = run()
        self.assertEqual(2, proc.returncode)


if __name__ == "__main__":
    unittest.main()
