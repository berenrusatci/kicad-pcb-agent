#!/usr/bin/env python3
"""Tests for the Monte Carlo tolerance analysis (common/tolerance.py +
scripts/monte_carlo.py).

The load-bearing tests are the ones that check against a value derived
independently of the code under test:

  * the divider output distribution against the analytic propagation of
    variance, sigma_out = sqrt(2) * (V / 4R) * sigma_R
  * individual ngspice trials against hand-computed R2/(R1+R2) ratios
  * the finite-difference step against the grid pitch, which is the bug that
    made dZ/dw read as -0.16 ohm/mm while dZ/dt read as -1056

and the ones that pin the refusals: a tolerance with no sigma convention, a
biased capacitor with no vendor curve, and a linearisation that does not hold.
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

import numpy as np  # noqa: E402

import monte_carlo as mc  # noqa: E402
from common.tolerance import (  # noqa: E402
    DcBiasDerating,
    ParameterSpec,
    ToleranceError,
    _wilson_interval,
    parse_parameter,
    parse_specification,
    sample_parameters,
    summarise,
    yield_against_limits,
)

SCRIPT = SCRIPTS / "monte_carlo.py"

DIVIDER = "* resistive divider\nV1 in 0 DC 10\nR1 in out 1k\nR2 out 0 1k\n"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def ngspice_available() -> bool:
    from sim.ngspice import find_ngspice
    return find_ngspice(None) is not None


class DcBias(unittest.TestCase):
    """A class-II ceramic loses most of its value under bias. The curve is
    part-specific, so it is supplied, never derived."""

    CURVE = DcBiasDerating(
        points=((0.0, 1.0), (1.8, 0.72), (3.3, 0.48), (5.0, 0.30)),
        source="test fixture, not a real part")

    def test_interpolates_between_supplied_points(self):
        self.assertAlmostEqual(self.CURVE.retention_at(1.8), 0.72)
        midpoint = self.CURVE.retention_at((1.8 + 3.3) / 2)
        self.assertTrue(0.48 < midpoint < 0.72, midpoint)

    def test_clamps_and_never_extrapolates(self):
        """Beyond the vendor's plot is not knowledge we have."""
        self.assertAlmostEqual(self.CURVE.retention_at(-5.0), 1.0)
        self.assertAlmostEqual(self.CURVE.retention_at(500.0), 0.30)

    def test_a_single_point_is_not_a_curve(self):
        with self.assertRaises(ToleranceError):
            DcBiasDerating(points=((0.0, 1.0),), source="x")

    def test_an_unattributed_curve_is_refused(self):
        with self.assertRaises(ToleranceError):
            DcBiasDerating(points=((0.0, 1.0), (5.0, 0.3)), source="   ")

    def test_retention_outside_zero_to_one_is_refused(self):
        with self.assertRaises(ToleranceError):
            DcBiasDerating(points=((0.0, 1.0), (5.0, 1.4)), source="x")

    def test_derating_moves_the_effective_nominal(self):
        spec = ParameterSpec(name="C1", nominal=10e-6, dc_bias_volts=3.3,
                             dc_bias_curve=self.CURVE)
        self.assertAlmostEqual(spec.effective_nominal(), 10e-6 * 0.48)

    def test_bias_without_a_curve_is_a_coverage_gap_not_a_guess(self):
        spec = ParameterSpec(name="C1", nominal=10e-6, dc_bias_volts=3.3)
        self.assertTrue(spec.has_coverage_gap())
        self.assertEqual(spec.effective_nominal(), 10e-6)


class SpecValidation(unittest.TestCase):
    def test_normal_from_tolerance_demands_an_explicit_sigma_rule(self):
        """'1%' does not say whether that is 1 sigma or 3 sigma."""
        with self.assertRaises(ToleranceError):
            ParameterSpec(name="R1", nominal=1000.0,
                          distribution="normal_from_tolerance",
                          tolerance_percent=1.0)

    def test_sigma_divisor_changes_the_spread_it_implies(self):
        one = ParameterSpec(name="R", nominal=1000.0,
                            distribution="normal_from_tolerance",
                            tolerance_percent=1.0, sigma_divisor=1.0)
        three = ParameterSpec(name="R", nominal=1000.0,
                              distribution="normal_from_tolerance",
                              tolerance_percent=1.0, sigma_divisor=3.0)
        self.assertAlmostEqual(one.sigma_value() / three.sigma_value(), 3.0)

    def test_normal_needs_a_sigma(self):
        with self.assertRaises(ToleranceError):
            ParameterSpec(name="R1", nominal=1.0, distribution="normal")

    def test_uniform_needs_a_tolerance(self):
        with self.assertRaises(ToleranceError):
            ParameterSpec(name="R1", nominal=1.0, distribution="uniform")

    def test_unknown_distribution_is_refused(self):
        with self.assertRaises(ToleranceError):
            ParameterSpec(name="R1", nominal=1.0, distribution="lognormal")

    def test_a_typo_in_a_key_is_refused_not_ignored(self):
        """A silently ignored key disables the variation it was meant to add."""
        with self.assertRaises(ToleranceError):
            parse_parameter("R1", {"nominal": 1000.0, "distribution": "normal",
                                   "sigma_pcnt": 1.0})

    def test_an_empty_specification_is_refused(self):
        with self.assertRaises(ToleranceError):
            parse_specification({"parameters": {}})

    def test_minimum_above_maximum_is_refused(self):
        with self.assertRaises(ToleranceError):
            ParameterSpec(name="R1", nominal=1.0, minimum=5.0, maximum=2.0)


class Sampling(unittest.TestCase):
    def test_the_same_seed_reproduces_the_draws_exactly(self):
        spec = ParameterSpec(name="R1", nominal=1000.0, distribution="normal",
                             sigma_percent=1.0)
        first = sample_parameters([spec], 200, seed=11)["R1"]
        second = sample_parameters([spec], 200, seed=11)["R1"]
        self.assertTrue(np.array_equal(first, second))

    def test_different_seeds_give_different_draws(self):
        spec = ParameterSpec(name="R1", nominal=1000.0, distribution="normal",
                             sigma_percent=1.0)
        first = sample_parameters([spec], 200, seed=11)["R1"]
        second = sample_parameters([spec], 200, seed=12)["R1"]
        self.assertFalse(np.array_equal(first, second))

    def test_fixed_parameters_do_not_move(self):
        spec = ParameterSpec(name="s", nominal=0.15, distribution="fixed")
        values = sample_parameters([spec], 50, seed=1)["s"]
        self.assertTrue(np.all(values == 0.15))

    def test_the_sample_mean_and_sigma_track_the_specification(self):
        spec = ParameterSpec(name="R1", nominal=1000.0, distribution="normal",
                             sigma=10.0)
        values = sample_parameters([spec], 20_000, seed=3)["R1"]
        self.assertAlmostEqual(float(np.mean(values)), 1000.0, delta=1.0)
        self.assertAlmostEqual(float(np.std(values, ddof=1)), 10.0, delta=0.5)

    def test_bounds_clip_and_the_clipping_is_reported(self):
        spec = ParameterSpec(name="R1", nominal=1000.0, distribution="normal",
                             sigma=100.0, minimum=990.0)
        values = spec.sample(np.random.default_rng(5), 2000)
        self.assertGreaterEqual(float(np.min(values)), 990.0)
        self.assertGreater(spec.clipped_fraction(values), 0.1)

    def test_zero_trials_is_refused(self):
        spec = ParameterSpec(name="R1", nominal=1.0)
        with self.assertRaises(ToleranceError):
            sample_parameters([spec], 0, seed=1)


class Statistics(unittest.TestCase):
    def test_summary_reports_tails_not_just_the_centre(self):
        stats = summarise(np.linspace(0.0, 100.0, 1001))
        self.assertAlmostEqual(stats["p50"], 50.0, places=6)
        self.assertAlmostEqual(stats["p01"], 1.0, places=6)
        self.assertAlmostEqual(stats["p99"], 99.0, places=6)

    def test_non_finite_samples_are_discarded_and_counted(self):
        stats = summarise([1.0, 2.0, float("nan"), 3.0])
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["discarded_non_finite"], 1)

    def test_an_all_nan_sample_is_refused_rather_than_summarised(self):
        with self.assertRaises(ToleranceError):
            summarise([float("nan"), float("nan")])

    def test_yield_counts_both_tails_separately(self):
        measured = yield_against_limits([1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                                        lower=3, upper=8)
        self.assertEqual(measured["passes"], 6)
        self.assertEqual(measured["failures_below"], 2)
        self.assertEqual(measured["failures_above"], 2)

    def test_a_yield_needs_at_least_one_limit(self):
        with self.assertRaises(ToleranceError):
            yield_against_limits([1.0, 2.0])

    def test_a_perfect_sample_still_reports_uncertainty(self):
        """100/100 is not proof of 100% yield."""
        low, high = _wilson_interval(100, 100)
        self.assertLess(low, 1.0)
        self.assertAlmostEqual(high, 1.0, places=9)

    def test_the_interval_narrows_as_trials_grow(self):
        narrow = _wilson_interval(9500, 10_000)
        wide = _wilson_interval(95, 100)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_the_interval_brackets_the_point_estimate(self):
        low, high = _wilson_interval(70, 100)
        self.assertLess(low, 0.70)
        self.assertGreater(high, 0.70)


class NetlistAssembly(unittest.TestCase):
    """SPICE discards the first line as the title - verified against
    ngspice-46, where a netlist starting with a component silently lost it."""

    def test_a_title_line_is_inserted_when_missing(self):
        built = mc.build_monte_carlo_netlist(
            "V1 in 0 DC 5\nR1 in out 1k\n.end",
            {"R1": np.array([1000.0])}, ["out"], 1)
        self.assertTrue(built.splitlines()[0].startswith("*"))
        self.assertIn("V1 in 0 DC 5", built)

    def test_an_existing_title_is_left_alone(self):
        built = mc.build_monte_carlo_netlist(
            DIVIDER, {"R1": np.array([1000.0])}, ["out"], 1)
        self.assertEqual("* resistive divider", built.splitlines()[0])

    def test_one_alter_and_one_print_per_trial(self):
        trials = 4
        built = mc.build_monte_carlo_netlist(
            DIVIDER,
            {"R1": np.arange(trials, dtype=float) + 1000.0,
             "R2": np.arange(trials, dtype=float) + 2000.0},
            ["out"], trials)
        self.assertEqual(built.count("alter R1 ="), trials)
        self.assertEqual(built.count("alter R2 ="), trials)
        self.assertEqual(built.count("print "), trials)

    def test_reset_is_never_emitted(self):
        """reset REVERTS pending alters, so it must not appear between trials."""
        built = mc.build_monte_carlo_netlist(
            DIVIDER, {"R1": np.array([1.0, 2.0])}, ["out"], 2)
        self.assertNotIn("reset", built)

    def test_watching_nothing_is_refused(self):
        with self.assertRaises(ToleranceError):
            mc.build_monte_carlo_netlist(DIVIDER, {"R1": np.array([1.0])},
                                         [], 1)

    def test_bare_node_names_become_voltage_probes(self):
        built = mc.build_monte_carlo_netlist(
            DIVIDER, {"R1": np.array([1.0])}, ["out"], 1)
        self.assertIn("print v(out)", built)


@unittest.skipUnless(ngspice_available(), "ngspice is not installed")
class AgainstRealNgspice(unittest.TestCase):
    """Not a mock. These run the installed binary."""

    def _run_divider(self, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.json"
            netlist = Path(tmp) / "d.cir"
            out = Path(tmp) / "r.json"
            spec.write_text(json.dumps({"parameters": {
                "R1": {"nominal": 1000, "distribution": "normal",
                       "sigma": 10.0},
                "R2": {"nominal": 1000, "distribution": "normal",
                       "sigma": 10.0}}}), encoding="utf-8")
            netlist.write_text(DIVIDER, encoding="utf-8")
            proc = run("circuit", "--spec", str(spec), "--netlist",
                       str(netlist), "--watch", "out", "--trials", "300",
                       "--json", str(out), *extra)
            report = json.loads(out.read_text(encoding="utf-8"))
            return proc, report

    def test_trials_match_hand_computed_divider_ratios(self):
        """The strongest check: each ngspice trial against R2/(R1+R2)."""
        spec = ParameterSpec(name="R1", nominal=1000.0, distribution="normal",
                             sigma=50.0)
        other = ParameterSpec(name="R2", nominal=1000.0, distribution="normal",
                              sigma=50.0)
        draws = sample_parameters([spec, other], 8, seed=7)
        built = mc.build_monte_carlo_netlist(DIVIDER, draws, ["out"], 8)

        from sim.ngspice import find_ngspice, parse_output
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mc.cir"
            path.write_text(built, encoding="utf-8")
            proc = subprocess.run([find_ngspice(None), "-b", str(path)],
                                  capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=180)
        parsed = parse_output((proc.stdout or "") + "\n" + (proc.stderr or ""))
        measured = [value for _, value in parsed.data["v(out)"]]
        self.assertEqual(8, len(measured))
        for index, value in enumerate(measured):
            r1, r2 = draws["R1"][index], draws["R2"][index]
            self.assertAlmostEqual(value, 10.0 * r2 / (r1 + r2), places=4)

    def test_the_spread_matches_analytic_variance_propagation(self):
        """sigma_out = sqrt(2) * (V / 4R) * sigma_R for a matched divider."""
        _, report = self._run_divider()
        measured = report["statistics"]["v(out)"]["std"]
        expected = (2 ** 0.5) * (10.0 / (4 * 1000.0)) * 10.0
        self.assertAlmostEqual(measured, expected, delta=0.15 * expected)
        self.assertAlmostEqual(report["statistics"]["v(out)"]["mean"], 5.0,
                               delta=0.01)

    def test_a_met_yield_is_green_and_exit_zero(self):
        proc, report = self._run_divider(
            ("--lower-limit", "4.9", "--upper-limit", "5.1",
             "--min-yield", "0.95"))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("GREEN", report["status"])
        self.assertTrue(report["gate"]["applied"])

    def test_a_missed_yield_is_red_and_exit_one(self):
        proc, report = self._run_divider(
            ("--lower-limit", "4.999", "--upper-limit", "5.001",
             "--min-yield", "0.999"))
        self.assertEqual(1, proc.returncode)
        self.assertEqual("RED", report["status"])
        self.assertIn("YIELD_BELOW_MINIMUM", report["reason_codes"])

    def test_the_report_records_the_seed_and_the_ngspice_version(self):
        _, report = self._run_divider()
        self.assertIn("seed", report)
        self.assertIn("ngspice", report["ngspice_version"].lower())

    def test_the_same_seed_reproduces_the_whole_report(self):
        _, first = self._run_divider()
        _, second = self._run_divider()
        self.assertEqual(first["statistics"], second["statistics"])


class GradientStep(unittest.TestCase):
    """The bug: a geometric step below the grid pitch is rasterised away, the
    perturbed geometry is identical to the nominal, and the sensitivity comes
    back as approximately zero - silently."""

    def test_geometric_steps_are_floored_at_a_multiple_of_the_pitch(self):
        step = mc._gradient_step("w", nominal_value=0.09, fraction=0.02,
                                 pitch_mm=0.005, pitch_multiple=3.0)
        self.assertGreaterEqual(step, 0.015)

    def test_a_fraction_larger_than_the_floor_is_kept(self):
        step = mc._gradient_step("h", nominal_value=1.6, fraction=0.02,
                                 pitch_mm=0.005, pitch_multiple=3.0)
        self.assertAlmostEqual(step, 1.6 * 0.02)

    def test_non_geometric_parameters_are_not_floored_by_the_pitch(self):
        """er is dimensionless; a grid pitch in mm says nothing about it."""
        step = mc._gradient_step("er", nominal_value=4.3, fraction=0.02,
                                 pitch_mm=0.005, pitch_multiple=3.0)
        self.assertAlmostEqual(step, 4.3 * 0.02)

    def test_the_step_is_never_zero(self):
        step = mc._gradient_step("er", nominal_value=0.0, fraction=0.02,
                                 pitch_mm=0.0, pitch_multiple=3.0)
        self.assertGreater(step, 0.0)


class ImpedanceStudy(unittest.TestCase):
    """Small budgets: this exercises the wiring, not the field solver, which
    test_field2d covers."""

    SPEC = {"parameters": {
        "w": {"nominal": 0.20, "distribution": "normal", "sigma": 0.005},
        "t": {"nominal": 0.035, "distribution": "fixed"},
        "h": {"nominal": 0.10, "distribution": "fixed"},
        "er": {"nominal": 4.3, "distribution": "fixed"}}}

    def _run(self, extra=(), spec=None):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "s.json"
            out = Path(tmp) / "r.json"
            spec_path.write_text(json.dumps(spec or self.SPEC),
                                 encoding="utf-8")
            proc = run("impedance", "--spec", str(spec_path), "--geometry",
                       "microstrip", "--trials", "500", "--max-nodes",
                       "40000", "--linearisation-checks", "2",
                       "--json", str(out), *extra)
            return proc, json.loads(out.read_text(encoding="utf-8"))

    def test_widening_the_trace_has_a_strongly_negative_sensitivity(self):
        """Before the step fix this read as -0.16 ohm/mm."""
        _, report = self._run()
        self.assertLess(report["sensitivity_ohm_per_unit"]["w"], -50.0)

    def test_the_step_used_is_reported_alongside_the_pitch(self):
        _, report = self._run()
        self.assertIn("grid_pitch_mm", report)
        self.assertGreaterEqual(report["gradient_step_mm"]["w"],
                                3.0 * report["grid_pitch_mm"])

    def test_thousands_of_trials_cost_a_handful_of_solves(self):
        _, report = self._run()
        self.assertLess(report["field_solves"], 20)
        self.assertEqual(500, report["trials"])

    def test_the_linear_model_is_centred_on_the_full_solve(self):
        _, report = self._run()
        self.assertAlmostEqual(report["statistics"]["z_ohm"]["mean"],
                               report["nominal_impedance_ohm"], delta=1.0)

    def test_an_unmet_linearisation_ceiling_forces_no_coverage(self):
        proc, report = self._run(
            ("--max-linearisation-error-percent", "0.0000001",))
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertIn("LINEARISATION_UNRELIABLE", report["reason_codes"])

    def test_statistics_survive_a_no_coverage_verdict(self):
        """A coverage gap must not look like a crash."""
        _, report = self._run(
            ("--max-linearisation-error-percent", "0.0000001",))
        self.assertIn("z_ohm", report["statistics"])
        self.assertIs(False, report["gate"]["applied"])

    def test_a_parameter_the_geometry_does_not_have_is_refused(self):
        spec = {"parameters": {"w": {"nominal": 0.2},
                               "bogus": {"nominal": 1.0}}}
        proc, _unused = None, None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            proc = run("impedance", "--spec", str(path), "--geometry",
                       "microstrip", "--trials", "10")
        self.assertEqual(2, proc.returncode)


class CoverageGaps(unittest.TestCase):
    def test_a_biased_capacitor_without_a_curve_forces_no_coverage(self):
        spec = {"parameters": {
            "w": {"nominal": 0.2, "distribution": "normal", "sigma": 0.005},
            "t": {"nominal": 0.035, "distribution": "fixed"},
            "h": {"nominal": 0.10, "distribution": "fixed", "dc_bias_volts": 3.3},
            "er": {"nominal": 4.3, "distribution": "fixed"}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            out = Path(tmp) / "r.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            proc = run("impedance", "--spec", str(path), "--geometry",
                       "microstrip", "--trials", "100", "--max-nodes",
                       "40000", "--linearisation-checks", "1",
                       "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertIn("PARAMETER_COVERAGE_GAP", report["reason_codes"])
        self.assertIn("h", report["parameter_coverage_gaps"])


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        mc.run_self_tests()

    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)


class CliContract(unittest.TestCase):
    def test_missing_subject_is_exit_2(self):
        self.assertEqual(2, run().returncode)

    def test_missing_spec_is_exit_2(self):
        self.assertEqual(2, run("circuit").returncode)

    def test_circuit_without_a_netlist_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps({"parameters": {"R1": {"nominal": 1}}}),
                            encoding="utf-8")
            self.assertEqual(2, run("circuit", "--spec", str(path)).returncode)

    def test_a_malformed_specification_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(2, run("impedance", "--spec", str(path)).returncode)


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(
            (SCHEMAS / "monte_carlo_report.schema.json").read_text(
                encoding="utf-8"))

    def test_schema_pins_the_version_and_signoff(self):
        self.assertEqual("monte-carlo-report/v1",
                         self.schema["properties"]["schema_version"]["const"])
        self.assertIs(False, self.schema["properties"]["signoff"]["const"])

    def test_seed_is_required_because_a_yield_must_reproduce(self):
        self.assertIn("seed", self.schema["required"])

    def test_reason_codes_emitted_are_declared(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        emitted = {
            "NGSPICE_ABSENT", "TRIAL_COUNT_MISMATCH", "PARAMETER_COVERAGE_GAP",
            "YIELD_BELOW_MINIMUM", "LINEARISATION_UNRELIABLE",
            "LINEARISATION_UNCHECKED",
        }
        self.assertEqual(emitted, declared)

    def test_a_real_report_satisfies_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            out = Path(tmp) / "r.json"
            path.write_text(json.dumps(ImpedanceStudy.SPEC), encoding="utf-8")
            run("impedance", "--spec", str(path), "--geometry", "microstrip",
                "--trials", "100", "--max-nodes", "40000",
                "--linearisation-checks", "1", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        for key in self.schema["required"]:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
