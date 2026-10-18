"""Tests for common/railbudget.py and scripts/board/rail_budget.py.

The subject of both halves is a property no ERC and no DRC can see: a
netlist proves an enable pin is CONNECTED, never that the rail attached to
it can turn the regulator on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.findings import NO_COVERAGE  # noqa: E402
from common.railbudget import (ALWAYS_ON, BUDGET_EXCEEDED,  # noqa: E402
                               EN_BELOW_THRESHOLD, EN_MARGIN_THIN,
                               EN_THRESHOLD_UNDECLARED, NO_BUDGET,
                               ORDER_CONTRADICTS_WIRING,
                               UNKNOWN_ENABLE_SOURCE, NoiseContributor, Rail,
                               attenuate_by_psrr, check_enable_margins,
                               check_noise_budget, compare_declared_order,
                               enable_order, sum_contributors)
from scripts.board.rail_budget import (build_contributors,  # noqa: E402
                                       enable_finding, load_rails, main,
                                       noise_finding, order_finding,
                                       parse_declared_order)

SCRIPT = TOOLS / "scripts" / "board" / "rail_budget.py"

RAILS = [Rail("VIN", 3.3),
         Rail("V1V8", 1.8, "VIN", 1.1),
         Rail("V1V2", 1.2, "V1V8", 1.1),
         Rail("V2V8", 2.8, "V1V8", 1.1)]


class EnableMarginTest(unittest.TestCase):
    def test_always_on_rails_have_no_enable_path_to_check(self):
        checks = check_enable_margins(RAILS)
        self.assertNotIn("VIN", [c.rail for c in checks])
        self.assertEqual(len(checks), 3)

    def test_a_comfortable_margin_reports_nothing(self):
        checks = {c.rail: c for c in check_enable_margins(RAILS)}
        self.assertEqual(checks["V1V8"].codes, [])
        self.assertAlmostEqual(checks["V1V8"].margin_v, 3.3 - 1.1)

    def test_a_source_below_the_threshold_is_caught(self):
        # Connected, ERC-clean, and the regulator never turns on.
        rails = [Rail("V1V2", 1.2), Rail("V2V8", 2.8, "V1V2", 1.4)]
        codes = check_enable_margins(rails)[0].codes
        self.assertIn(EN_BELOW_THRESHOLD, codes)

    def test_a_thin_margin_is_a_different_finding_from_a_negative_one(self):
        rails = [Rail("V1V8", 1.8), Rail("V1V2", 1.2, "V1V8", 1.6)]
        thin = check_enable_margins(rails, min_margin_v=0.5)[0]
        self.assertIn(EN_MARGIN_THIN, thin.codes)
        self.assertNotIn(EN_BELOW_THRESHOLD, thin.codes)
        self.assertAlmostEqual(thin.margin_v, 0.2)

    def test_without_a_declared_budget_only_the_sign_is_checked(self):
        rails = [Rail("V1V8", 1.8), Rail("V1V2", 1.2, "V1V8", 1.6)]
        self.assertEqual(check_enable_margins(rails)[0].codes, [])

    def test_an_undeclared_threshold_is_reported_not_assumed(self):
        rails = [Rail("VIN", 3.3), Rail("V", 1.0, "VIN")]
        check = check_enable_margins(rails)[0]
        self.assertIn(EN_THRESHOLD_UNDECLARED, check.codes)
        self.assertIsNone(check.margin_v)

    def test_an_enable_source_that_is_not_a_rail_is_caught(self):
        rails = [Rail("V", 1.0, "GHOST", 0.5)]
        self.assertIn(UNKNOWN_ENABLE_SOURCE, check_enable_margins(rails)[0].codes)


class EnableOrderTest(unittest.TestCase):
    def test_tiers_follow_the_enable_graph(self):
        self.assertEqual(enable_order(RAILS),
                         [["VIN"], ["V1V8"], ["V1V2", "V2V8"]])

    def test_rails_in_one_tier_are_not_ordered_against_each_other(self):
        # The graph does not order them, so claiming an order would invent a
        # sequence the hardware never promised.
        tiers = enable_order(RAILS)
        self.assertIn("V1V2", tiers[2])
        self.assertIn("V2V8", tiers[2])

    def test_a_cycle_is_refused_rather_than_partially_sorted(self):
        with self.assertRaises(ValueError) as ctx:
            enable_order([Rail("A", 1.0, "B", 0.5), Rail("B", 1.0, "A", 0.5)])
        self.assertIn("ENABLE_CYCLE", str(ctx.exception))

    def test_all_always_on_is_a_single_tier(self):
        self.assertEqual(enable_order([Rail("A", 1.0), Rail("B", 2.0)]),
                         [["A", "B"]])


class DeclaredOrderTest(unittest.TestCase):
    def test_a_matching_document_produces_no_violation(self):
        self.assertEqual(
            compare_declared_order(RAILS, [["VIN"], ["V1V8"], ["V1V2", "V2V8"]]),
            [])

    def test_an_inverted_document_is_caught(self):
        violations = compare_declared_order(RAILS, [["V1V2"], ["V1V8"], ["VIN"]])
        self.assertTrue(violations)
        self.assertEqual(violations[0]["issue"], ORDER_CONTRADICTS_WIRING)

    def test_a_document_finer_than_the_hardware_is_not_a_contradiction(self):
        # The graph puts V1V2 and V2V8 in one tier. A document ordering them
        # states an intent the hardware permits; only an INVERSION is a
        # contradiction.
        self.assertEqual(
            compare_declared_order(RAILS,
                                   [["VIN"], ["V1V8"], ["V1V2"], ["V2V8"]]),
            [])

    def test_a_rail_the_document_forgot_is_reported(self):
        violations = compare_declared_order(RAILS, [["VIN"], ["V1V8"]])
        self.assertTrue(any("V1V2" in v.get("rails", []) for v in violations))

    def test_a_rail_the_document_invented_is_reported(self):
        violations = compare_declared_order(
            RAILS, [["VIN"], ["V1V8"], ["V1V2", "V2V8"], ["GHOST"]])
        self.assertTrue(any("GHOST" in v.get("rails", []) for v in violations))


class NoiseSummingTest(unittest.TestCase):
    def test_deterministic_terms_add_linearly(self):
        total = sum_contributors([NoiseContributor("a", 0.003),
                                  NoiseContributor("b", 0.004)])
        self.assertAlmostEqual(total["total_v"], 0.007)

    def test_random_terms_add_in_quadrature(self):
        total = sum_contributors([NoiseContributor("a", 0.003, "random"),
                                  NoiseContributor("b", 0.004, "random")])
        self.assertAlmostEqual(total["total_v"], 0.005)

    def test_the_two_families_are_combined_linearly(self):
        # Pessimistic on purpose: nothing here knows their relative phase.
        total = sum_contributors([NoiseContributor("d", 0.003),
                                  NoiseContributor("r", 0.004, "random")])
        self.assertAlmostEqual(total["total_v"], 0.007)

    def test_summing_a_deterministic_term_as_random_would_understate_it(self):
        # The dangerous direction, which is why kind is not cosmetic.
        as_random = sum_contributors([NoiseContributor("a", 0.003, "random"),
                                      NoiseContributor("b", 0.004, "random")])
        as_deterministic = sum_contributors([NoiseContributor("a", 0.003),
                                             NoiseContributor("b", 0.004)])
        self.assertLess(as_random["total_v"], as_deterministic["total_v"])

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            NoiseContributor("a", 0.001, "vibes")

    def test_a_negative_contribution_is_rejected(self):
        with self.assertRaises(ValueError):
            NoiseContributor("a", -0.001)


class PsrrTest(unittest.TestCase):
    def test_attenuation_follows_the_decibel_definition(self):
        self.assertAlmostEqual(
            attenuate_by_psrr(0.1, 20.0, ripple_hz=1000.0, psrr_at_hz=1000.0),
            0.01)

    def test_a_frequency_gap_is_refused_rather_than_interpolated(self):
        # A regulator can lose tens of dB between 1 kHz and a switching
        # residue in the megahertz; reusing the low-frequency figure is
        # optimistic by orders of magnitude.
        with self.assertRaises(ValueError) as ctx:
            attenuate_by_psrr(0.05, 60.0, ripple_hz=2e6, psrr_at_hz=1e3)
        self.assertIn("PSRR", str(ctx.exception))

    def test_a_nearby_frequency_is_allowed_within_the_stated_tolerance(self):
        value = attenuate_by_psrr(0.05, 60.0, ripple_hz=1.5e3, psrr_at_hz=1e3)
        self.assertGreater(value, 0)

    def test_non_positive_frequencies_are_rejected(self):
        with self.assertRaises(ValueError):
            attenuate_by_psrr(0.05, 60.0, ripple_hz=0.0, psrr_at_hz=1e3)


class NoiseBudgetTest(unittest.TestCase):
    def test_a_fitting_budget_has_positive_headroom(self):
        entry = check_noise_budget("V", [NoiseContributor("a", 0.001)], 0.005)
        self.assertEqual(entry.codes, [])
        self.assertAlmostEqual(entry.headroom_v, 0.004)

    def test_an_exceeded_budget_is_flagged(self):
        entry = check_noise_budget("V", [NoiseContributor("a", 0.01)], 0.005)
        self.assertIn(BUDGET_EXCEEDED, entry.codes)
        self.assertLess(entry.headroom_v, 0)

    def test_an_absent_budget_still_produces_the_sum(self):
        entry = check_noise_budget("V", [NoiseContributor("a", 0.01)], None)
        self.assertIn(NO_BUDGET, entry.codes)
        self.assertAlmostEqual(entry.summary["total_v"], 0.01)
        self.assertIsNone(entry.headroom_v)


class FindingTest(unittest.TestCase):
    def test_no_enable_paths_at_all_is_no_coverage(self):
        checks = check_enable_margins([Rail("A", 1.0), Rail("B", 2.0)])
        self.assertEqual(enable_finding(checks, None)["status"], NO_COVERAGE)

    def test_undeclared_thresholds_make_the_check_no_coverage(self):
        checks = check_enable_margins([Rail("VIN", 3.3), Rail("V", 1.0, "VIN")])
        self.assertEqual(enable_finding(checks, None)["status"], NO_COVERAGE)

    def test_no_declared_order_is_no_coverage(self):
        self.assertEqual(order_finding(RAILS, None)["status"], NO_COVERAGE)

    def test_no_noise_entries_is_no_coverage(self):
        self.assertEqual(noise_finding([])["status"], NO_COVERAGE)

    def test_an_unbudgeted_rail_makes_the_noise_check_no_coverage(self):
        entry = check_noise_budget("V", [NoiseContributor("a", 0.01)], None)
        self.assertEqual(noise_finding([entry])["status"], NO_COVERAGE)


class DocumentTest(unittest.TestCase):
    def test_loads_a_well_formed_rail_set(self):
        rails = load_rails({"rails": [
            {"name": "VIN", "voltage_v": 3.3},
            {"name": "V", "voltage_v": 1.8, "enable_source": "VIN",
             "en_threshold_v": 1.1}]})
        self.assertEqual(rails[0].enable_source, ALWAYS_ON)
        self.assertEqual(rails[1].en_threshold_v, 1.1)

    def test_rejects_malformed_rail_sets(self):
        for doc in ({}, {"rails": []}, {"rails": [{}]},
                    {"rails": [{"name": "A"}]},
                    {"rails": [{"name": "A", "voltage_v": 1},
                               {"name": "A", "voltage_v": 2}]}):
            with self.assertRaises(ValueError):
                load_rails(doc)

    def test_contributors_are_attenuated_when_psrr_is_given(self):
        built = build_contributors([
            {"name": "input ripple", "volts": 0.1, "psrr_db": 20.0,
             "at_hz": 1000.0, "ripple_hz": 1000.0}])
        self.assertAlmostEqual(built[0].volts, 0.01)

    def test_declared_order_notation(self):
        self.assertEqual(parse_declared_order("V1V8;V1V2,V2V8"),
                         [["V1V8"], ["V1V2", "V2V8"]])
        self.assertIsNone(parse_declared_order(None))
        with self.assertRaises(ValueError):
            parse_declared_order("V1V8;;V1V2")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_tmp_rails.json"
        self.path.write_text(json.dumps({
            "rails": [
                {"name": "VIN", "voltage_v": 3.3},
                {"name": "V1V8", "voltage_v": 1.8, "enable_source": "VIN",
                 "en_threshold_v": 1.1},
                {"name": "V1V2", "voltage_v": 1.2, "enable_source": "V1V8",
                 "en_threshold_v": 1.1}],
            "noise": [
                {"rail": "V1V2", "budget_v": 0.012, "contributors": [
                    {"name": "regulator noise", "volts": 7e-6, "kind": "random"},
                    {"name": "input ripple", "volts": 0.05,
                     "kind": "deterministic", "psrr_db": 60,
                     "at_hz": 1000, "ripple_hz": 1000}]}]}),
            encoding="utf-8")

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_self_test_passes(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_fully_declared_run_is_green(self):
        self.assertEqual(main(["--rails", str(self.path),
                               "--declared-order", "VIN;V1V8;V1V2"]), 0)

    def test_without_a_declared_order_the_run_is_not_green(self):
        self.assertEqual(main(["--rails", str(self.path)]), 1)

    def test_an_inverted_declared_order_is_red(self):
        self.assertEqual(main(["--rails", str(self.path),
                               "--declared-order", "V1V2;V1V8;VIN"]), 1)

    def test_missing_rails_argument_is_an_error(self):
        with self.assertRaises(ValueError):
            main([])

    def test_report_is_deterministic_and_carries_the_tiers(self):
        out = Path(__file__).resolve().parent / "_tmp_rail_report.json"
        argv = ["--rails", str(self.path), "--declared-order", "VIN;V1V8;V1V2",
                "--json", str(out)]
        try:
            main(argv)
            first = out.read_text(encoding="utf-8")
            main(argv)
            self.assertEqual(first, out.read_text(encoding="utf-8"))
            report = json.loads(first)
            self.assertEqual(report["schema_version"], "rail-budget/v1")
            self.assertEqual(report["enable_tiers"],
                             [["VIN"], ["V1V8"], ["V1V2"]])
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
