#!/usr/bin/env python3
"""Tests for the crosstalk NEXT screen and thinning decision.

The engine previously had no external test file - only an in-process
``_self_test()`` that nothing ran in CI, plus indirect coverage from the MCP
protocol tests. The self-test is promoted to a real test case here, and the
CLI contract changes are pinned.
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

import crosstalk_thinning as ct  # noqa: E402

SCRIPT = SCRIPTS / "crosstalk_thinning.py"

BASE = dict(trace_width_mm=0.20, spacing_mm=0.20, dielectric_height_mm=0.20,
            coupled_length_mm=40, rise_time_ps=300)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class SelfTest(unittest.TestCase):
    def test_builtin_self_test_passes(self):
        """Promote the in-process fault-injection suite into the real suite."""
        proc = run("--self-test")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("SELF-TEST PASS", proc.stdout)


class Decisions(unittest.TestCase):
    def test_tight_coupling_is_crosstalk_risk_and_never_thins(self):
        r = ct.analyze(trace_width_mm=0.20, spacing_mm=0.10,
                       dielectric_height_mm=0.20, coupled_length_mm=60,
                       rise_time_ps=200)
        self.assertEqual("CROSSTALK_RISK", r["status"])
        self.assertEqual("INCREASE_SPACING", r["action"])
        self.assertEqual(0.20, r["suggested_width_mm"])
        self.assertGreater(r["suggested_spacing_mm"], 0.10)

    def test_missing_em_inputs_is_no_coverage_not_pass(self):
        r = ct.analyze(trace_width_mm=0.20, spacing_mm=0.20)
        self.assertEqual("NO_COVERAGE", r["status"])
        self.assertEqual("GATHER_EM_INPUTS", r["action"])

    def test_impedance_controlled_net_is_never_thinned(self):
        r = ct.analyze(**BASE, impedance_controlled=True)
        self.assertNotEqual("SAFE_TO_THIN", r["status"])
        self.assertEqual(BASE["trace_width_mm"], r["suggested_width_mm"])

    def test_discontinuous_reference_plane_falls_to_no_coverage(self):
        r = ct.analyze(**BASE, reference_plane_continuous=False)
        self.assertEqual("NO_COVERAGE", r["status"])

    def test_every_result_is_labelled_non_signoff(self):
        for kwargs in (BASE, dict(trace_width_mm=0.2, spacing_mm=0.1,
                                  dielectric_height_mm=0.2,
                                  coupled_length_mm=60, rise_time_ps=200)):
            r = ct.analyze(**kwargs)
            self.assertEqual("exploratory_closed_form", r["model_class"])
            self.assertIs(False, r["signoff"])

    def test_action_is_present_for_every_status(self):
        self.assertEqual(
            {"SAFE_TO_THIN", "CROSSTALK_RISK", "THINNING_BLOCKED",
             "NO_COVERAGE"},
            set(ct.ACTION_BY_STATUS),
        )


class SharedImpedanceModel(unittest.TestCase):
    def test_out_of_domain_geometry_yields_none_not_a_negative_impedance(self):
        """The old copy had no domain guard and returned a NEGATIVE Z0."""
        out = ct.impedance_z0_ohm(w_mm=5.0, h_mm=0.05, er=4.3, t_mm=0.035,
                                  layer_type="microstrip")
        self.assertIsNone(out["z0_ohm"])
        self.assertTrue(any("NO_COVERAGE" in n for n in out["notes"]))

    def test_microstrip_matches_zdiff_solver_single_ended_model(self):
        """One IPC-2141 implementation: both tools must agree exactly."""
        from common.si_models import z0_microstrip

        out = ct.impedance_z0_ohm(w_mm=0.2, h_mm=0.2, er=4.3, t_mm=0.035,
                                  layer_type="microstrip")
        self.assertAlmostEqual(round(z0_microstrip(0.2, 0.035, 0.2, 4.3), 2),
                               out["z0_ohm"], places=6)


class CliContract(unittest.TestCase):
    def test_json_flag_writes_and_input_json_reads(self):
        """--json used to READ here and WRITE everywhere else. Now it writes."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            proc = run("--trace-width-mm", "0.20", "--spacing-mm", "0.20",
                       "--dielectric-height-mm", "0.20",
                       "--coupled-length-mm", "40", "--rise-time-ps", "300",
                       "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertTrue(out.is_file(), "--json must WRITE the report")
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written, json.loads(proc.stdout))

    def test_input_json_reads_the_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.json"
            src.write_text(json.dumps(BASE), encoding="utf-8")
            proc = run("--input-json", str(src))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(0.20, json.loads(proc.stdout)["input"]["trace_width_mm"])

    def test_missing_required_inputs_is_exit_2(self):
        proc = run("--dielectric-height-mm", "0.2")
        self.assertEqual(2, proc.returncode)

    def test_exit_codes_follow_the_gate_contract(self):
        # SAFE_TO_THIN / THINNING_BLOCKED => 0 (no crosstalk problem)
        ok = run("--trace-width-mm", "0.20", "--spacing-mm", "0.20",
                 "--dielectric-height-mm", "0.20",
                 "--coupled-length-mm", "40", "--rise-time-ps", "300")
        self.assertIn(json.loads(ok.stdout)["status"],
                      ("SAFE_TO_THIN", "THINNING_BLOCKED"))
        self.assertEqual(0, ok.returncode)

        # CROSSTALK_RISK => 1
        risk = run("--trace-width-mm", "0.20", "--spacing-mm", "0.10",
                   "--dielectric-height-mm", "0.20",
                   "--coupled-length-mm", "60", "--rise-time-ps", "200")
        self.assertEqual("CROSSTALK_RISK", json.loads(risk.stdout)["status"])
        self.assertEqual(1, risk.returncode)

        # NO_COVERAGE => 1, never 0
        gap = run("--trace-width-mm", "0.20", "--spacing-mm", "0.20")
        self.assertEqual("NO_COVERAGE", json.loads(gap.stdout)["status"])
        self.assertEqual(1, gap.returncode)

    def test_stdout_is_pure_json(self):
        proc = run("--trace-width-mm", "0.20", "--spacing-mm", "0.20",
                   "--dielectric-height-mm", "0.20",
                   "--coupled-length-mm", "40", "--rise-time-ps", "300")
        json.loads(proc.stdout)      # must not raise


if __name__ == "__main__":
    unittest.main()
