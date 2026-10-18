"""Tests for common/loopgain.py and scripts/sim/loop_stability.py.

Every assertion here is against something known independently of the code:
a closed-form two-pole response, a hand-built sweep whose crossings can be
counted by eye, or a refusal the module promises to make.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.findings import NO_COVERAGE  # noqa: E402
from common.loopgain import (COARSE_SAMPLING, CONDITIONALLY_STABLE,  # noqa: E402
                             MULTIPLE_CROSSOVERS, NO_CROSSOVER,
                             NO_PHASE_CROSSOVER, PHASE_MARGIN_LOW,
                             analyse, find_gain_crossings,
                             is_conditionally_stable, load_samples,
                             unwrap_phase)
from scripts.sim.loop_stability import (_synthetic_two_pole, main,  # noqa: E402
                                        margin_finding, read_sweep,
                                        shape_finding)

SCRIPT = TOOLS / "scripts" / "sim" / "loop_stability.py"


def two_pole_sweep(points: int = 141, dc_gain_db: float = 60.0):
    rows = []
    for step in range(points):
        freq = 10.0 ** (step / 20.0)
        mag, phase = _synthetic_two_pole(freq, dc_gain_db)
        rows.append([freq, mag, phase])
    return rows


class SampleLoadingTest(unittest.TestCase):
    def test_rejects_a_sweep_shorter_than_two_points(self):
        with self.assertRaises(ValueError):
            load_samples([[1.0, 0.0, 0.0]])

    def test_rejects_a_non_positive_frequency(self):
        with self.assertRaises(ValueError):
            load_samples([[0.0, 1.0, 0.0], [1.0, -1.0, 0.0]])

    def test_rejects_an_unsorted_axis_rather_than_sorting_it(self):
        # Two concatenated sweeps are the usual cause, and interpolating
        # across the seam invents a crossing the data does not contain.
        with self.assertRaises(ValueError):
            load_samples([[10.0, 1.0, 0.0], [1.0, -1.0, 0.0]])

    def test_rejects_a_duplicated_frequency(self):
        with self.assertRaises(ValueError):
            load_samples([[1.0, 1.0, 0.0], [1.0, -1.0, 0.0]])

    def test_rejects_a_short_row(self):
        with self.assertRaises(ValueError):
            load_samples([[1.0, 1.0], [2.0, -1.0]])


class UnwrapTest(unittest.TestCase):
    def test_a_monotone_phase_is_left_alone(self):
        samples = load_samples([[1.0, 1.0, -10.0], [2.0, 0.0, -20.0]])
        unwrapped, wraps = unwrap_phase(samples)
        self.assertEqual(wraps, 0)
        self.assertEqual(unwrapped, [-10.0, -20.0])

    def test_a_wrap_is_removed_and_counted(self):
        samples = load_samples([[1.0, 1.0, -170.0], [2.0, 0.0, 170.0]])
        unwrapped, wraps = unwrap_phase(samples)
        self.assertEqual(wraps, 1)
        self.assertAlmostEqual(unwrapped[1], -190.0)

    def test_wrapping_does_not_change_the_margin(self):
        # The whole reason unwrapping exists: a margin from wrapped phase is
        # confidently wrong rather than obviously wrong.
        rows = two_pole_sweep()
        plain = analyse(rows)
        wrapped = analyse([[f, m, ((p + 180.0) % 360.0) - 180.0]
                           for f, m, p in rows])
        self.assertAlmostEqual(plain.crossings[0].phase_margin_deg,
                               wrapped.crossings[0].phase_margin_deg, places=6)


class TwoPoleTest(unittest.TestCase):
    def setUp(self):
        self.report = analyse(two_pole_sweep())

    def test_one_crossover_at_the_closed_form_frequency(self):
        self.assertEqual(len(self.report.crossings), 1)
        expected = 10.0 * (10.0 ** (60.0 / 20.0))
        self.assertAlmostEqual(self.report.crossings[0].freq_hz / expected,
                               1.0, delta=0.05)

    def test_two_poles_never_reach_minus_180_degrees(self):
        # They only approach it, so there is no gain-margin crossing at all -
        # which the report must say rather than inventing one.
        self.assertEqual(self.report.phase_crossings, [])
        self.assertIsNone(self.report.worst_gain_margin_db)
        self.assertIn(NO_PHASE_CROSSOVER, self.report.reason_codes())

    def test_margin_matches_the_arctangent_it_came_from(self):
        crossing = self.report.crossings[0]
        _, phase = _synthetic_two_pole(crossing.freq_hz)
        self.assertAlmostEqual(crossing.phase_margin_deg, phase + 180.0, places=1)

    def test_lowering_the_dc_gain_moves_the_crossover_down(self):
        lower = analyse(two_pole_sweep(dc_gain_db=40.0))
        self.assertLess(lower.crossings[0].freq_hz,
                        self.report.crossings[0].freq_hz)


class MultipleCrossingTest(unittest.TestCase):
    def setUp(self):
        # Built so the WORST crossing is the middle one. With the worst
        # sitting first, a reader that only ever looked at the first
        # crossing would agree with a correct one by accident, and the test
        # would prove nothing.
        self.rows = [[1.0, 20.0, -95.0], [10.0, -5.0, -160.0],
                     [100.0, 5.0, -175.0], [1000.0, -30.0, -110.0]]
        self.report = analyse(self.rows)

    def test_every_crossing_is_found(self):
        self.assertEqual(len(self.report.crossings), 3)

    def test_the_worst_crossing_wins(self):
        self.assertEqual(self.report.worst_phase_margin_deg,
                         min(c.phase_margin_deg for c in self.report.crossings))

    def test_multiple_crossovers_is_reported(self):
        self.assertIn(MULTIPLE_CROSSOVERS, self.report.reason_codes())

    def test_direction_is_recorded_for_each(self):
        directions = [c.direction for c in self.report.crossings]
        self.assertEqual(directions, ["falling", "rising", "falling"])

    def test_a_first_crossing_only_reader_would_have_disagreed(self):
        first = self.report.crossings[0].phase_margin_deg
        last = self.report.crossings[-1].phase_margin_deg
        worst = self.report.worst_phase_margin_deg
        self.assertEqual(worst, self.report.crossings[1].phase_margin_deg)
        self.assertGreater(first, worst)
        self.assertGreater(last, worst)


class ConditionalStabilityTest(unittest.TestCase):
    def test_phase_past_180_while_gain_is_up_is_flagged(self):
        report = analyse([[1.0, 40.0, -90.0], [10.0, 30.0, -200.0],
                          [100.0, 20.0, -170.0], [1000.0, -20.0, -160.0]])
        self.assertTrue(is_conditionally_stable(report.phase_crossings))
        self.assertIn(CONDITIONALLY_STABLE, report.reason_codes())

    def test_a_plain_loop_is_not_flagged(self):
        report = analyse(two_pole_sweep())
        self.assertFalse(is_conditionally_stable(report.phase_crossings))

    def test_the_crossing_records_that_gain_was_above_unity(self):
        report = analyse([[1.0, 40.0, -90.0], [10.0, 30.0, -200.0],
                          [100.0, 20.0, -170.0], [1000.0, -20.0, -160.0]])
        above = [c for c in report.phase_crossings if c.above_unity]
        self.assertTrue(above)
        self.assertGreater(above[0].mag_db, 0.0)


class NoCrossingTest(unittest.TestCase):
    def test_a_response_that_never_reaches_zero_db_has_no_margin(self):
        report = analyse([[1.0, 40.0, -20.0], [10.0, 39.0, -25.0]])
        self.assertEqual(report.crossings, [])
        self.assertIsNone(report.worst_phase_margin_deg)
        self.assertIn(NO_CROSSOVER, report.reason_codes())

    def test_and_that_is_no_coverage_not_a_pass(self):
        report = analyse([[1.0, 40.0, -20.0], [10.0, 39.0, -25.0]])
        self.assertEqual(margin_finding(report, 45.0, None)["status"],
                         NO_COVERAGE)


class BracketTest(unittest.TestCase):
    def test_a_coarsely_bracketed_crossing_is_flagged(self):
        # One decade between the samples either side of the crossing: the
        # location is not known to the precision it prints with.
        report = analyse([[1.0, 20.0, -100.0], [10.0, -20.0, -140.0]])
        self.assertIn(COARSE_SAMPLING, report.reason_codes())
        self.assertAlmostEqual(report.crossings[0].bracket_decades, 1.0)

    def test_a_tightly_bracketed_crossing_is_not(self):
        report = analyse(two_pole_sweep())
        self.assertNotIn(COARSE_SAMPLING, report.reason_codes())


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.report = analyse(two_pole_sweep())

    def test_no_declared_budget_is_no_coverage(self):
        self.assertEqual(margin_finding(self.report, None, None)["status"],
                         NO_COVERAGE)

    def test_a_met_budget_passes(self):
        self.assertEqual(margin_finding(self.report, 45.0, None)["status"],
                         "PASS")

    def test_an_unmet_budget_fails_and_names_the_frequency(self):
        finding = margin_finding(self.report, 179.0, None)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["issue"], PHASE_MARGIN_LOW)
        self.assertIn("at_hz", finding["violations"][0])

    def test_shape_findings_are_independent_of_any_budget(self):
        multi = analyse([[1.0, 20.0, -100.0], [10.0, -5.0, -170.0],
                         [100.0, 5.0, -120.0], [1000.0, -30.0, -150.0]])
        self.assertEqual(shape_finding(multi)["status"], "FAIL")


class ReadSweepTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(__file__).resolve().parent
        self.csv = self.dir / "_tmp_loop.csv"
        self.json = self.dir / "_tmp_loop.json"

    def tearDown(self):
        self.csv.unlink(missing_ok=True)
        self.json.unlink(missing_ok=True)

    def test_csv_with_a_header_is_read(self):
        self.csv.write_text("freq_hz,mag_db,phase_deg\n1,20,-100\n10,-20,-140\n",
                            encoding="utf-8")
        self.assertEqual(read_sweep(self.csv), [[1.0, 20.0, -100.0],
                                                [10.0, -20.0, -140.0]])

    def test_a_headerless_csv_is_refused(self):
        # Magnitude and phase are both plain numbers; swapping them parses
        # cleanly and produces meaningless margins.
        self.csv.write_text("1,20,-100\n10,-20,-140\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_sweep(self.csv)

    def test_column_order_does_not_matter_when_named(self):
        self.csv.write_text("phase_deg,freq_hz,mag_db\n-100,1,20\n-140,10,-20\n",
                            encoding="utf-8")
        self.assertEqual(read_sweep(self.csv), [[1.0, 20.0, -100.0],
                                                [10.0, -20.0, -140.0]])

    def test_json_is_read(self):
        self.json.write_text(json.dumps({"sweep": [[1, 20, -100], [10, -20, -140]]}),
                             encoding="utf-8")
        self.assertEqual(read_sweep(self.json), [[1.0, 20.0, -100.0],
                                                 [10.0, -20.0, -140.0]])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.csv = Path(__file__).resolve().parent / "_tmp_cli_loop.csv"
        rows = ["freq_hz,mag_db,phase_deg"]
        for freq, mag, phase in two_pole_sweep():
            rows.append(f"{freq!r},{mag!r},{phase!r}")
        self.csv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def tearDown(self):
        self.csv.unlink(missing_ok=True)

    def test_self_test_passes(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_met_budget_exits_zero(self):
        self.assertEqual(main(["--sweep", str(self.csv),
                               "--min-phase-margin-deg", "45"]), 0)

    def test_no_budget_does_not_exit_zero(self):
        self.assertEqual(main(["--sweep", str(self.csv)]), 1)

    def test_an_unmet_budget_exits_one(self):
        self.assertEqual(main(["--sweep", str(self.csv),
                               "--min-phase-margin-deg", "179"]), 1)

    def test_missing_sweep_is_an_error(self):
        with self.assertRaises(ValueError):
            main([])

    def test_report_is_deterministic_and_declares_it_models_nothing(self):
        out = Path(__file__).resolve().parent / "_tmp_loop_report.json"
        argv = ["--sweep", str(self.csv), "--min-phase-margin-deg", "45",
                "--json", str(out)]
        try:
            main(argv)
            first = out.read_text(encoding="utf-8")
            main(argv)
            self.assertEqual(first, out.read_text(encoding="utf-8"))
            report = json.loads(first)
            self.assertEqual(report["schema_version"], "loop-stability/v1")
            self.assertFalse(report["models_the_loop"])
            self.assertEqual(len(report["gain_crossings"]), 1)
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
