#!/usr/bin/env python3
"""Tests for measurement-vs-simulation correlation (common/correlate.py +
scripts/sim/correlate.py).

There is no fabricated board, so there is no real measurement. That blocks
correlating THIS board; it does not block testing the machinery, because a VNA
exports Touchstone and this bundle already writes Touchstone. Every test here
builds a "measurement" from a model whose disagreement with the reference is
known in advance, so the residual has a right answer to be checked against.

The refusals matter more than the agreements:

  * disjoint sweeps must refuse rather than interpolate, because resampling a
    measurement invents points that were never taken
  * three shared points must not become a headline residual
  * an un-de-embedded fixture must be flagged, because no residual can
    separate a launch from a wrong er
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

from common import tline as tl  # noqa: E402
from common.correlate import (  # noqa: E402
    MIN_SHARED_POINTS,
    Band,
    CorrelationError,
    common_grid,
    correlate,
    extract,
    residual,
    suggest_er_correction,
)

SCRIPT = SCRIPTS / "sim" / "correlate.py"

requires_skrf = unittest.skipUnless(tl.HAVE_SKRF, "scikit-rf is not installed")


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def network(delay_scale=1.0, loss_scale=1.0, points=41,
            start=1e8, stop=1e10):
    frequency = tl.frequency_grid(start, stop, points)
    spec = tl.LineSpec(z0_ohm=50.0, er_eff=3.1 * delay_scale ** 2,
                       length_mm=100.0, tan_delta=0.01 * loss_scale)
    return tl.single_line_network(frequency, spec, z0_port=50.0)


class BandValidation(unittest.TestCase):
    def test_a_reversed_band_is_refused(self):
        with self.assertRaises(CorrelationError):
            Band(1e10, 1e8)

    def test_a_zero_start_is_refused(self):
        with self.assertRaises(CorrelationError):
            Band(0.0, 1e9)

    def test_the_mask_selects_only_in_band_points(self):
        band = Band(2e9, 4e9)
        frequency = np.array([1e9, 2e9, 3e9, 4e9, 5e9])
        self.assertEqual(3, int(np.count_nonzero(band.mask(frequency))))


@requires_skrf
class ExactAgreement(unittest.TestCase):
    def test_a_network_against_itself_is_exactly_zero(self):
        same = network()
        report = correlate(same, same, Band(1e8, 1e10))
        self.assertTrue(report["sufficient"])
        self.assertLess(abs(report["residuals"]["magnitude_db"]["rms"]), 1e-12)
        self.assertLess(abs(report["residuals"]["magnitude_db"]["worst"]), 1e-12)

    def test_phase_agrees_with_itself_too(self):
        same = network()
        report = correlate(same, same, Band(1e9, 1e10),
                           metrics=("phase_deg",))
        self.assertLess(abs(report["residuals"]["phase_deg"]["rms"]), 1e-9)


@requires_skrf
class KnownDisagreement(unittest.TestCase):
    def test_a_lossier_measurement_reads_negative(self):
        """The SIGN is the finding: a biased model and a noisy one differ."""
        report = correlate(network(loss_scale=2.0), network(loss_scale=1.0),
                           Band(1e9, 1e10))
        self.assertLess(report["residuals"]["magnitude_db"]["mean"], -0.05)

    def test_a_less_lossy_measurement_reads_positive(self):
        report = correlate(network(loss_scale=0.5), network(loss_scale=1.0),
                           Band(1e9, 1e10))
        self.assertGreater(report["residuals"]["magnitude_db"]["mean"], 0.01)

    def test_a_slower_measurement_shows_a_longer_delay(self):
        report = correlate(network(delay_scale=1.10), network(delay_scale=1.0),
                           Band(1e9, 1e10), metrics=("group_delay_ps",))
        self.assertGreater(report["residuals"]["group_delay_ps"]["mean"], 1.0)

    def test_the_worst_point_reports_where_it_happened(self):
        report = correlate(network(loss_scale=3.0), network(loss_scale=1.0),
                           Band(1e9, 1e10))
        worst = report["residuals"]["magnitude_db"]
        self.assertGreaterEqual(worst["worst_frequency_hz"], 1e9)
        self.assertLessEqual(worst["worst_frequency_hz"], 1e10)

    def test_mean_absolute_never_below_the_signed_magnitude(self):
        report = correlate(network(loss_scale=2.0), network(loss_scale=1.0),
                           Band(1e9, 1e10))
        values = report["residuals"]["magnitude_db"]
        self.assertGreaterEqual(values["mean_absolute"], abs(values["mean"]))


@requires_skrf
class GridDiscipline(unittest.TestCase):
    def test_disjoint_sweeps_refuse_rather_than_interpolate(self):
        measured = network(start=1e8, stop=1e10)
        simulated = network(start=2e10, stop=3e10)
        report = correlate(measured, simulated, Band(1e8, 1e10))
        self.assertFalse(report["sufficient"])
        self.assertIn("shared frequency point", report["reason"])

    def test_too_few_shared_points_do_not_become_a_headline(self):
        measured = tl.single_line_network(
            tl.frequency_grid(1e9, 1.2e9, 3), tl.LineSpec(50.0, 3.1, 100.0))
        simulated = tl.single_line_network(
            tl.frequency_grid(1e9, 1.2e9, 3), tl.LineSpec(50.0, 3.1, 100.0))
        report = correlate(measured, simulated, Band(1e9, 1.2e9))
        self.assertFalse(report["sufficient"])
        self.assertEqual({}, report["residuals"])

    def test_the_minimum_is_stated_not_hidden(self):
        self.assertGreaterEqual(MIN_SHARED_POINTS, 2)

    def test_only_in_band_points_are_shared(self):
        measured = network(points=41)
        shared, _m, _s = common_grid(np.asarray(measured.f, dtype=float),
                                     np.asarray(measured.f, dtype=float),
                                     Band(5e9, 1e10))
        self.assertTrue(bool(np.all(shared >= 5e9)))
        self.assertTrue(bool(np.all(shared <= 1e10)))

    def test_a_narrower_band_shares_fewer_points(self):
        same = network()
        narrow = correlate(same, same, Band(5e9, 1e10))
        wide = correlate(same, same, Band(1e8, 1e10))
        self.assertLess(narrow["overlap"]["shared_points"],
                        wide["overlap"]["shared_points"])

    def test_mismatched_port_counts_are_refused(self):
        two = network()
        four = tl.coupled_line_network(
            tl.frequency_grid(1e8, 1e10, 41),
            tl.LineSpec(45.0, 3.0, 100.0), tl.LineSpec(60.0, 3.2, 100.0))
        with self.assertRaises(CorrelationError):
            correlate(two, four, Band(1e8, 1e10))


class ResidualMaths(unittest.TestCase):
    def test_an_unknown_metric_is_refused(self):
        with self.assertRaises(CorrelationError):
            residual("vibes", np.array([1.0]), np.array([1.0]),
                     np.array([1.0]))

    def test_an_empty_comparison_is_refused(self):
        with self.assertRaises(CorrelationError):
            residual("magnitude_db", np.array([]), np.array([]), np.array([]))

    def test_a_constant_offset_shows_as_mean_and_rms(self):
        values = residual("magnitude_db", np.array([1.0, 2.0, 3.0]),
                          np.array([1.0, 1.0, 1.0]),
                          np.array([0.5, 0.5, 0.5]))
        self.assertAlmostEqual(0.5, values.mean)
        self.assertAlmostEqual(0.5, values.rms)

    def test_a_symmetric_error_has_zero_mean_but_nonzero_rms(self):
        """Exactly the case the signed mean must not hide."""
        values = residual("magnitude_db", np.array([1.0, 2.0]),
                          np.array([1.0, -1.0]), np.array([0.0, 0.0]))
        self.assertAlmostEqual(0.0, values.mean)
        self.assertAlmostEqual(1.0, values.rms)


class ErCorrection(unittest.TestCase):
    def test_it_follows_the_square_of_the_delay_ratio(self):
        result = suggest_er_correction(110.0, 100.0, 3.0)
        self.assertAlmostEqual(3.0 * 1.1 ** 2, result["implied_er_eff"])

    def test_equal_delays_imply_no_change(self):
        result = suggest_er_correction(100.0, 100.0, 3.0)
        self.assertAlmostEqual(0.0, result["change_percent"])

    def test_a_non_positive_delay_is_refused(self):
        with self.assertRaises(CorrelationError):
            suggest_er_correction(0.0, 100.0, 3.0)

    def test_it_always_carries_its_caveat(self):
        """The same delay error can be a wrong length or a live fixture."""
        self.assertIn("fixture", suggest_er_correction(110.0, 100.0, 3.0)["caveat"])


@requires_skrf
class SelfTest(unittest.TestCase):
    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)


@requires_skrf
class CliContract(unittest.TestCase):
    def _pair(self, tmp, loss_scale=1.0):
        measured = Path(tmp) / "measured.s2p"
        simulated = Path(tmp) / "simulated.s2p"
        tl.write_touchstone(network(loss_scale=loss_scale), measured)
        tl.write_touchstone(network(loss_scale=1.0), simulated)
        return measured, simulated

    def test_no_measurement_is_no_coverage_not_a_pass(self):
        """The expected state until a board exists."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run("--json", str(out))
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("NO_COVERAGE", report["status"])
            self.assertIn("MEASUREMENT_ABSENT", report["reason_codes"])
            self.assertIn("unfalsified", report["detail"])

    def test_a_missing_band_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured, simulated = self._pair(tmp)
            proc = run("--measured", str(measured), "--simulated",
                       str(simulated))
            self.assertEqual(2, proc.returncode)
            self.assertIn("band", proc.stderr)

    def test_identical_files_correlate_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured, simulated = self._pair(tmp)
            out = Path(tmp) / "r.json"
            proc = run("--measured", str(measured), "--simulated",
                       str(simulated), "--band-start-hz", "1e9",
                       "--band-stop-hz", "1e10", "--de-embedded",
                       "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("GREEN", report["status"])
            self.assertLess(
                abs(report["residuals"]["magnitude_db"]["rms"]), 1e-6)

    def test_an_undeclared_fixture_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured, simulated = self._pair(tmp)
            out = Path(tmp) / "r.json"
            run("--measured", str(measured), "--simulated", str(simulated),
                "--band-start-hz", "1e9", "--band-stop-hz", "1e10",
                "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("FIXTURE_NOT_DE_EMBEDDED", report["reason_codes"])
            self.assertIs(False, report["de_embedded"])

    def test_a_magnitude_gate_can_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured, simulated = self._pair(tmp, loss_scale=4.0)
            out = Path(tmp) / "r.json"
            proc = run("--measured", str(measured), "--simulated",
                       str(simulated), "--band-start-hz", "1e9",
                       "--band-stop-hz", "1e10", "--de-embedded",
                       "--max-magnitude-residual-db", "0.01",
                       "--json", str(out))
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("RED", report["status"])
            self.assertIn("MAGNITUDE_RESIDUAL_EXCEEDED", report["reason_codes"])

    def test_a_magnitude_gate_can_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured, simulated = self._pair(tmp)
            out = Path(tmp) / "r.json"
            proc = run("--measured", str(measured), "--simulated",
                       str(simulated), "--band-start-hz", "1e9",
                       "--band-stop-hz", "1e10", "--de-embedded",
                       "--max-magnitude-residual-db", "0.5",
                       "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIs(True, report["gate"]["checks"]["magnitude_db"]["within"])

    def test_model_feedback_is_offered_with_a_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured = Path(tmp) / "m.s2p"
            simulated = Path(tmp) / "s.s2p"
            tl.write_touchstone(network(delay_scale=1.05), measured)
            tl.write_touchstone(network(delay_scale=1.0), simulated)
            out = Path(tmp) / "r.json"
            run("--measured", str(measured), "--simulated", str(simulated),
                "--band-start-hz", "1e9", "--band-stop-hz", "1e10",
                "--metrics", "group_delay_ps", "--de-embedded",
                "--assumed-er-eff", "3.1", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            feedback = report["model_feedback"]
            self.assertGreater(feedback["implied_er_eff"], 3.1)
            self.assertIn("caveat", feedback)

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            measured, simulated = self._pair(tmp)
            proc = run("--measured", str(measured), "--simulated",
                       str(simulated), "--band-start-hz", "1e9",
                       "--band-stop-hz", "1e10")
            json.loads(proc.stdout)


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(
            (SCHEMAS / "correlation_report.schema.json").read_text(
                encoding="utf-8"))

    def test_schema_pins_the_version_and_signoff(self):
        self.assertEqual("correlation-report/v1",
                         self.schema["properties"]["schema_version"]["const"])
        self.assertIs(False, self.schema["properties"]["signoff"]["const"])

    def test_reason_codes_emitted_are_declared(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        self.assertEqual(
            {"MEASUREMENT_ABSENT", "INSUFFICIENT_OVERLAP",
             "FIXTURE_NOT_DE_EMBEDDED", "MAGNITUDE_RESIDUAL_EXCEEDED",
             "DELAY_RESIDUAL_EXCEEDED"}, declared)

    def test_the_signed_mean_is_required_on_every_residual(self):
        required = (self.schema["properties"]["residuals"]
                    ["additionalProperties"]["required"])
        self.assertIn("mean", required)
        self.assertIn("mean_absolute", required)

    def test_a_real_report_satisfies_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            run("--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        for key in self.schema["required"]:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
