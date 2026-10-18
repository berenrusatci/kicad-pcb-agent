#!/usr/bin/env python3
"""Tests for electrothermal coupling (common/electrothermal.py +
scripts/board/electrothermal.py).

Everything here is checked against closed form or against the physics:

  * resistivity at 20 C is the reference value, and 100 K of rise is +39.3%
    because alpha is 3.93e-3/K
  * the iteration and the analytic fixed point must agree
  * the loop gain G = I^2*R20*alpha*Rth decides whether a fixed point exists,
    and G = 1 must be exactly where maximum_stable_current_a lands

The runaway case gets the most attention, because it is the one the uncoupled
calculation gets confidently wrong: it evaluates the first step and reports a
comfortable temperature for a trace that has no steady state at all.
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

from common.electrothermal import (  # noqa: E402
    COPPER_ALPHA_PER_K,
    COPPER_RESISTIVITY_20C_OHM_M,
    ElectrothermalError,
    closed_form_temperature_c,
    loop_gain,
    maximum_stable_current_a,
    resistivity_at,
    single_pass_temperature_c,
    solve_equilibrium,
    trace_resistance_ohm,
)

SCRIPT = SCRIPTS / "board" / "electrothermal.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class Resistivity(unittest.TestCase):
    def test_the_reference_value_at_twenty_degrees(self):
        self.assertAlmostEqual(resistivity_at(20.0),
                               COPPER_RESISTIVITY_20C_OHM_M, places=20)

    def test_a_hundred_kelvin_rise_is_thirty_nine_point_three_percent(self):
        """alpha = 3.93e-3/K, so 100 K gives exactly 1.393."""
        self.assertAlmostEqual(
            resistivity_at(120.0) / resistivity_at(20.0), 1.393, places=9)

    def test_it_rises_monotonically(self):
        values = [resistivity_at(t) for t in (0.0, 25.0, 85.0, 125.0)]
        self.assertEqual(values, sorted(values))

    def test_an_out_of_domain_temperature_is_refused(self):
        """The linear model would give a negative resistivity far below zero;
        a model that returns one will eventually be handed that input."""
        with self.assertRaises(ElectrothermalError):
            resistivity_at(-500.0)

    def test_an_alternative_alpha_is_honoured(self):
        doubled = resistivity_at(120.0, alpha=2 * COPPER_ALPHA_PER_K)
        single = resistivity_at(120.0, alpha=COPPER_ALPHA_PER_K)
        self.assertGreater(doubled, single)


class TraceResistance(unittest.TestCase):
    def test_it_scales_with_length(self):
        base = trace_resistance_ohm(100.0, 0.25, 1.0, 20.0)
        self.assertAlmostEqual(
            trace_resistance_ohm(200.0, 0.25, 1.0, 20.0) / base, 2.0, places=12)

    def test_it_scales_inversely_with_width(self):
        base = trace_resistance_ohm(100.0, 0.25, 1.0, 20.0)
        self.assertAlmostEqual(
            trace_resistance_ohm(100.0, 0.50, 1.0, 20.0) / base, 0.5, places=12)

    def test_it_scales_inversely_with_copper_weight(self):
        base = trace_resistance_ohm(100.0, 0.25, 1.0, 20.0)
        self.assertAlmostEqual(
            trace_resistance_ohm(100.0, 0.25, 2.0, 20.0) / base, 0.5, places=12)

    def test_it_rises_with_temperature(self):
        self.assertGreater(trace_resistance_ohm(100.0, 0.25, 1.0, 100.0),
                           trace_resistance_ohm(100.0, 0.25, 1.0, 20.0))

    def test_a_zero_dimension_is_refused(self):
        for kwargs in ({"length_mm": 0.0}, {"width_mm": 0.0},
                       {"copper_oz": 0.0}):
            base = {"length_mm": 100.0, "width_mm": 0.25, "copper_oz": 1.0,
                    "temperature_c": 20.0}
            base.update(kwargs)
            with self.subTest(**kwargs):
                with self.assertRaises(ElectrothermalError):
                    trace_resistance_ohm(**base)


class LoopGain(unittest.TestCase):
    def test_it_scales_with_the_square_of_current(self):
        single = loop_gain(1.0, 0.05, 40.0)
        double = loop_gain(2.0, 0.05, 40.0)
        self.assertAlmostEqual(double / single, 4.0, places=12)

    def test_it_scales_linearly_with_thermal_resistance(self):
        self.assertAlmostEqual(
            loop_gain(2.0, 0.05, 80.0) / loop_gain(2.0, 0.05, 40.0),
            2.0, places=12)

    def test_zero_current_has_zero_gain(self):
        self.assertEqual(0.0, loop_gain(0.0, 0.05, 40.0))

    def test_the_maximum_stable_current_puts_the_gain_at_exactly_one(self):
        limit = maximum_stable_current_a(0.05, 40.0)
        self.assertAlmostEqual(1.0, loop_gain(limit, 0.05, 40.0), places=9)

    def test_a_negative_current_is_refused(self):
        with self.assertRaises(ElectrothermalError):
            loop_gain(-1.0, 0.05, 40.0)

    def test_a_non_positive_thermal_resistance_is_refused(self):
        with self.assertRaises(ElectrothermalError):
            loop_gain(1.0, 0.05, 0.0)


class Equilibrium(unittest.TestCase):
    def test_the_iteration_matches_the_closed_form(self):
        """The whole reason both are computed."""
        result = solve_equilibrium(2.0, 0.05, 40.0, 25.0)
        self.assertTrue(result.converged)
        self.assertLess(result.iteration_vs_closed_form_k, 1e-6)

    def test_zero_current_stays_at_ambient(self):
        result = solve_equilibrium(0.0, 0.05, 40.0, 30.0)
        self.assertAlmostEqual(30.0, result.temperature_c, places=12)
        self.assertAlmostEqual(0.0, result.rise_k, places=12)

    def test_the_coupled_answer_is_hotter_than_the_uncoupled_one(self):
        """Which is the point: the old number was optimistic."""
        coupled = solve_equilibrium(2.0, 0.05, 40.0, 25.0)
        uncoupled = single_pass_temperature_c(2.0, 0.05, 40.0, 25.0)
        self.assertGreater(coupled.temperature_c, uncoupled)

    def test_more_current_is_hotter(self):
        cool = solve_equilibrium(1.0, 0.05, 40.0)
        warm = solve_equilibrium(2.0, 0.05, 40.0)
        self.assertGreater(warm.temperature_c, cool.temperature_c)

    def test_a_higher_ambient_raises_the_equilibrium_more_than_one_to_one(self):
        """Because the trace is already hotter, its resistance is higher too."""
        low = solve_equilibrium(2.0, 0.05, 40.0, ambient_c=0.0)
        high = solve_equilibrium(2.0, 0.05, 40.0, ambient_c=40.0)
        self.assertGreater(high.temperature_c - low.temperature_c, 40.0)

    def test_a_runaway_loop_reports_no_temperature(self):
        result = solve_equilibrium(40.0, 0.05, 40.0)
        self.assertFalse(result.converged)
        self.assertIsNone(result.temperature_c)
        self.assertIsNone(result.closed_form_temperature_c)
        self.assertIn("no steady state", result.reason)

    def test_a_runaway_loop_is_refused_from_the_gain_not_from_iterating(self):
        """An unstable loop looks like a slowly converging one for the first
        several steps, so a max-iterations cutoff would report its last value
        as if it meant something."""
        result = solve_equilibrium(40.0, 0.05, 40.0)
        self.assertEqual(0, result.iterations)
        self.assertGreaterEqual(result.loop_gain, 1.0)

    def test_the_closed_form_returns_none_when_there_is_no_fixed_point(self):
        self.assertIsNone(closed_form_temperature_c(40.0, 0.05, 40.0, 25.0))

    def test_just_below_the_limit_still_converges(self):
        limit = maximum_stable_current_a(0.05, 40.0)
        result = solve_equilibrium(limit * 0.9, 0.05, 40.0)
        self.assertTrue(result.converged)
        self.assertLess(result.loop_gain, 1.0)

    def test_just_above_the_limit_does_not(self):
        limit = maximum_stable_current_a(0.05, 40.0)
        self.assertFalse(solve_equilibrium(limit * 1.01, 0.05, 40.0).converged)


class CliContract(unittest.TestCase):
    def test_self_test_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)

    def test_a_missing_thermal_resistance_is_refused(self):
        proc = run("--current-a", "3", "--resistance-20c-ohm", "0.05")
        self.assertEqual(2, proc.returncode)
        self.assertIn("thermal-resistance", proc.stderr)

    def test_a_missing_current_is_refused(self):
        self.assertEqual(2, run("--thermal-resistance-k-per-w", "40").returncode)

    def test_geometry_without_both_dimensions_is_refused(self):
        proc = run("--current-a", "3", "--thermal-resistance-k-per-w", "40",
                   "--length-mm", "60")
        self.assertEqual(2, proc.returncode)

    def test_a_stable_case_reports_both_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run("--current-a", "3", "--length-mm", "60",
                       "--width-mm", "0.5",
                       "--thermal-resistance-k-per-w", "45",
                       "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("GREEN", report["status"])
            self.assertTrue(report["equilibrium"]["converged"])
            self.assertGreater(report["uncoupled"]["coupling_error_k"], 0.0)

    def test_a_runaway_case_is_red_and_names_no_temperature(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run("--current-a", "12", "--length-mm", "60",
                       "--width-mm", "0.5",
                       "--thermal-resistance-k-per-w", "45",
                       "--json", str(out))
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("RED", report["status"])
            self.assertIn("THERMAL_RUNAWAY", report["reason_codes"])
            self.assertIsNone(report["equilibrium"]["temperature_c"])

    def test_a_rise_gate_can_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run("--current-a", "3", "--length-mm", "60",
                       "--width-mm", "0.5",
                       "--thermal-resistance-k-per-w", "45",
                       "--max-rise-k", "5", "--json", str(out))
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("RISE_EXCEEDED", report["reason_codes"])

    def test_the_resistance_source_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            run("--current-a", "3", "--resistance-20c-ohm", "0.05",
                "--thermal-resistance-k-per-w", "45", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("stated", report["inputs"]["resistance_source"])

    def test_stdout_is_pure_json(self):
        proc = run("--current-a", "3", "--resistance-20c-ohm", "0.05",
                   "--thermal-resistance-k-per-w", "45")
        json.loads(proc.stdout)


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(
            (SCHEMAS / "electrothermal_report.schema.json").read_text(
                encoding="utf-8"))

    def test_schema_pins_the_version(self):
        self.assertEqual("electrothermal-report/v1",
                         self.schema["properties"]["schema_version"]["const"])

    def test_the_temperature_is_nullable(self):
        """Null is the correct answer for a loop with no fixed point."""
        nullable = (self.schema["properties"]["equilibrium"]["properties"]
                    ["temperature_c"]["type"])
        self.assertIn("null", nullable)

    def test_reason_codes_emitted_are_declared(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        self.assertEqual({"THERMAL_RUNAWAY", "TEMPERATURE_EXCEEDED",
                          "RISE_EXCEEDED"}, declared)

    def test_a_real_report_satisfies_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            run("--current-a", "3", "--resistance-20c-ohm", "0.05",
                "--thermal-resistance-k-per-w", "45", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        for key in self.schema["required"]:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
