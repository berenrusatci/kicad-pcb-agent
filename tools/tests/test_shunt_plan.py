"""Tests for common/shunt.py and scripts/shunt_plan.py.

The tests that matter here are the REFUSALS. A planner that always emits a
value looks identical to a working one until the day the value it emits is
unusable, so most of what follows drives the tool into a corner and asserts
that it declines rather than rounds.
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
from common.shunt import (DOWNSTREAM_OF_BYPASS, E24, E96,  # noqa: E402
                          UPSTREAM_OF_BYPASS, current_uncertainty_pct,
                          preferred_values, select_shunt, sense_net_name,
                          shunt_window, size_rail, split_preview,
                          testpoint_names)
from scripts.shunt_plan import (build_parser, check_load_isolation,  # noqa: E402
                                check_uncertainty_budget, main,
                                parse_rail_specs, read_netlist_nets,
                                verify_rail_claims)

SCRIPT = TOOLS / "scripts" / "shunt_plan.py"

NETLIST = """(export (version "E")
  (nets
    (net (code "1") (name "VDD_1V2D") (class "Default")
      (node (ref "U303") (pin "5") (pinfunction "OUT") (pintype "power_out"))
      (node (ref "C1") (pin "1") (pintype "passive"))
      (node (ref "J1") (pin "27") (pintype "passive")))
    (net (code "2") (name "VDD_1V8IO") (class "Default")
      (node (ref "U302") (pin "5") (pintype "power_out"))
      (node (ref "U303") (pin "1") (pintype "power_in"))
      (node (ref "J1") (pin "26") (pintype "passive")))
    (net (code "3") (name "LONELY") (class "Default")
      (node (ref "U9") (pin "1") (pintype "power_out")))))
"""


class PreferredValueTest(unittest.TestCase):
    def test_series_tables_are_the_right_size_and_sorted(self):
        # A transcription slip in a hardcoded standard table is silent: the
        # tool still returns *a* value, just not a purchasable one.
        self.assertEqual(len(E24), 24)
        self.assertEqual(len(E96), 96)
        self.assertEqual(list(E24), sorted(E24))
        self.assertEqual(list(E96), sorted(E96))
        self.assertEqual(len(set(E96)), 96)

    def test_bounds_are_inclusive(self):
        values = preferred_values(0.1, 1.0, "E24")
        self.assertIn(0.1, values)
        self.assertIn(1.0, values)

    def test_walks_multiple_decades(self):
        values = preferred_values(0.01, 10.0, "E24")
        self.assertIn(0.01, values)
        self.assertIn(0.47, values)
        self.assertIn(10.0, values)

    def test_empty_range_is_empty_not_an_error(self):
        self.assertEqual(preferred_values(2.0, 1.0, "E24"), [])

    def test_rejects_unknown_series_and_non_positive_bounds(self):
        with self.assertRaises(ValueError):
            preferred_values(0.1, 1.0, "E12")
        with self.assertRaises(ValueError):
            preferred_values(0.0, 1.0, "E24")


class WindowTest(unittest.TestCase):
    def test_bounds_come_from_the_stated_formulas(self):
        window = shunt_window(0.3, 0.012, 10e-6, 1e-3)
        self.assertAlmostEqual(window.r_max_ohm, 0.012 / 0.3)
        self.assertAlmostEqual(window.r_min_ohm, 10e-6 / 1e-3)
        self.assertFalse(window.is_empty)

    def test_tightening_the_droop_budget_closes_the_window(self):
        window = shunt_window(0.3, 0.001, 10e-6, 1e-3)
        self.assertTrue(window.is_empty)
        self.assertIsNone(select_shunt(window, "E24"))

    def test_demanding_finer_resolution_closes_the_window(self):
        window = shunt_window(0.3, 0.012, 10e-6, 1e-6)
        self.assertTrue(window.is_empty)

    def test_a_non_empty_window_can_still_hold_no_preferred_value(self):
        # The distinction matters: "the physics allows it but you cannot buy
        # it" is a different problem from "the physics forbids it", and they
        # have different fixes.
        window = shunt_window(1.0, 0.01021, 10.2e-6, 1e-3)
        self.assertFalse(window.is_empty)
        self.assertIsNone(select_shunt(window, "E24"))
        # E96 is denser; if it finds one it must still be inside the window.
        finer = select_shunt(window, "E96")
        if finer is not None:
            self.assertGreaterEqual(finer, window.r_min_ohm)
            self.assertLessEqual(finer, window.r_max_ohm)

    def test_selection_prefers_the_smallest_droop(self):
        window = shunt_window(0.3, 0.3, 10e-6, 1e-3)
        low = select_shunt(window, "E24", prefer="lowest")
        high = select_shunt(window, "E24", prefer="highest")
        self.assertLess(low, high)

    def test_rejects_non_positive_inputs(self):
        for kwargs in ({"full_scale_current_a": 0.0}, {"max_droop_v": -1.0},
                       {"meter_resolution_v": 0.0}, {"current_resolution_a": 0.0}):
            base = dict(full_scale_current_a=0.3, max_droop_v=0.012,
                        meter_resolution_v=10e-6, current_resolution_a=1e-3)
            base.update(kwargs)
            with self.assertRaises(ValueError):
                shunt_window(**base)


class UncertaintyTest(unittest.TestCase):
    def test_terms_combine_in_quadrature(self):
        value = current_uncertainty_pct(1.0, 0.5, 0.01, kelvin=True)
        self.assertAlmostEqual(value, math.sqrt(1.0 ** 2 + 0.5 ** 2))

    def test_parasitic_term_dominates_a_small_shunt(self):
        # 3 mohm of copper against a 10 mohm element is 30%, and it is this
        # number - not the resistor's 1% tolerance - that decides whether
        # the measurement answers anything.
        value = current_uncertainty_pct(1.0, 0.5, 0.01, parasitic_ohm=0.003)
        self.assertGreater(value, 29.0)
        self.assertLess(value, 31.0)

    def test_a_larger_shunt_dilutes_the_parasitic_term(self):
        small = current_uncertainty_pct(1.0, 0.5, 0.01, parasitic_ohm=0.003)
        large = current_uncertainty_pct(1.0, 0.5, 0.1, parasitic_ohm=0.003)
        self.assertLess(large, small / 5)

    def test_undeclared_parasitic_returns_none_not_a_flattering_number(self):
        self.assertIsNone(current_uncertainty_pct(1.0, 0.5, 0.01))

    def test_kelvin_drops_the_term_entirely(self):
        self.assertIsNotNone(current_uncertainty_pct(1.0, 0.5, 0.01, kelvin=True))


class RailSizingTest(unittest.TestCase):
    def test_a_sized_rail_reports_every_derived_quantity(self):
        rail = size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                         UPSTREAM_OF_BYPASS, package_power_w=0.0625,
                         parasitic_ohm=0.003)
        self.assertIsNotNone(rail.shunt_ohm)
        self.assertAlmostEqual(rail.droop_at_full_scale_v,
                               rail.shunt_ohm * 0.3)
        self.assertAlmostEqual(rail.power_at_full_scale_w,
                               0.3 ** 2 * rail.shunt_ohm)
        self.assertAlmostEqual(rail.achieved_current_resolution_a,
                               10e-6 / rail.shunt_ohm)

    def test_infeasible_rail_is_an_outcome_not_an_exception(self):
        rail = size_rail("VDD", "U1", "5", 0.3, 0.0001, 10e-6, 1e-3,
                         UPSTREAM_OF_BYPASS)
        self.assertIsNone(rail.shunt_ohm)
        self.assertIn("SHUNT_WINDOW_EMPTY", rail.reason_codes)
        self.assertIsNone(rail.droop_at_full_scale_v)

    def test_undeclared_package_rating_is_flagged_not_assumed(self):
        rail = size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                         UPSTREAM_OF_BYPASS, kelvin=True)
        self.assertIn("PACKAGE_POWER_NOT_DECLARED", rail.reason_codes)

    def test_exceeded_package_rating_is_flagged(self):
        rail = size_rail("VDD", "U1", "5", 2.0, 2.0, 10e-6, 1e-3,
                         UPSTREAM_OF_BYPASS, package_power_w=1e-6, kelvin=True)
        self.assertIn("PACKAGE_POWER_EXCEEDED", rail.reason_codes)

    def test_downstream_placement_is_refused_by_default(self):
        rail = size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                         DOWNSTREAM_OF_BYPASS, kelvin=True,
                         package_power_w=0.0625)
        self.assertIn("SHUNT_DOWNSTREAM_OF_BYPASS", rail.reason_codes)

    def test_downstream_placement_can_be_overridden(self):
        rail = size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                         DOWNSTREAM_OF_BYPASS, kelvin=True,
                         package_power_w=0.0625, allow_downstream=True)
        self.assertNotIn("SHUNT_DOWNSTREAM_OF_BYPASS", rail.reason_codes)

    def test_rejects_an_unknown_position(self):
        with self.assertRaises(ValueError):
            size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3, "somewhere")


class NetSplitTest(unittest.TestCase):
    def test_only_the_regulator_pin_stays_on_the_source_side(self):
        preview = split_preview("VDD", [("U1", "5"), ("C1", "1"), ("J1", "2")],
                                "U1", "5")
        self.assertEqual(preview["stays_on_source_side"], [{"ref": "U1", "pin": "5"}])
        self.assertEqual(preview["moved_node_count"], 2)
        self.assertEqual(preview["load_side_net"], sense_net_name("VDD"))

    def test_testpoints_are_named_by_side(self):
        src, load = testpoint_names("VDD")
        self.assertNotEqual(src, load)
        self.assertIn("SRC", src)
        self.assertIn("LOAD", load)


class NetlistVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_shunt_netlist.net"
        self.tmp.write_text(NETLIST, encoding="utf-8")
        self.nets = read_netlist_nets(self.tmp)

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_reads_nodes_across_lines(self):
        self.assertEqual(self.nets["VDD_1V2D"],
                         [("U303", "5"), ("C1", "1"), ("J1", "27")])

    def test_a_true_claim_passes_and_yields_a_split(self):
        finding, splits = verify_rail_claims({"VDD_1V2D": ("U303", "5")}, self.nets)
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(splits["VDD_1V2D"]["moved_node_count"], 2)

    def test_a_pin_that_is_not_on_the_net_fails(self):
        finding, splits = verify_rail_claims({"VDD_1V2D": ("U303", "1")}, self.nets)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["issue"], "regulator_pin_not_on_net")
        self.assertEqual(splits, {})

    def test_a_net_that_does_not_exist_fails(self):
        finding, _ = verify_rail_claims({"NOPE": ("U1", "1")}, self.nets)
        self.assertEqual(finding["violations"][0]["issue"], "net_not_in_netlist")

    def test_a_single_node_net_fails_rather_than_splitting_into_a_dead_net(self):
        finding, splits = verify_rail_claims({"LONELY": ("U9", "1")}, self.nets)
        self.assertEqual(finding["violations"][0]["issue"], "single_node_net")
        self.assertNotIn("LONELY", splits)

    def test_no_netlist_is_no_coverage_never_a_silent_pass(self):
        finding, splits = verify_rail_claims({"VDD_1V2D": ("U303", "5")}, None)
        self.assertEqual(finding["status"], NO_COVERAGE)
        self.assertEqual(splits, {})


class BudgetGateTest(unittest.TestCase):
    def _rails(self, **kwargs):
        return [size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                          UPSTREAM_OF_BYPASS, package_power_w=0.0625, **kwargs)]

    def test_no_budget_is_no_coverage_even_though_the_number_exists(self):
        finding = check_uncertainty_budget(self._rails(parasitic_ohm=0.003), None)
        self.assertEqual(finding["status"], NO_COVERAGE)
        self.assertIn("30", finding["detail"])

    def test_over_budget_fails_and_names_the_dominant_term(self):
        finding = check_uncertainty_budget(self._rails(parasitic_ohm=0.003), 5.0)
        self.assertEqual(finding["status"], "FAIL")
        self.assertIn("parasitic", finding["violations"][0]["detail"])

    def test_within_budget_passes(self):
        finding = check_uncertainty_budget(self._rails(kelvin=True), 5.0)
        self.assertEqual(finding["status"], "PASS")

    def test_rails_without_computable_uncertainty_are_no_coverage(self):
        finding = check_uncertainty_budget(self._rails(), 5.0)
        self.assertEqual(finding["status"], NO_COVERAGE)


class LoadIsolationTest(unittest.TestCase):
    def setUp(self):
        self.rails = [size_rail("VDD_1V8IO", "U302", "5", 0.3, 0.018, 10e-6,
                                1e-3, UPSTREAM_OF_BYPASS, kelvin=True,
                                package_power_w=0.0625)]
        self.splits = {"VDD_1V8IO": split_preview(
            "VDD_1V8IO",
            [("U302", "5"), ("U303", "1"), ("C1", "1"), ("J1", "26")],
            "U302", "5")}

    def test_extra_active_loads_downstream_fail(self):
        finding = check_load_isolation(self.rails, self.splits,
                                       {"VDD_1V8IO": ("J1", "26")})
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["also_downstream"], ["U303"])

    def test_bypass_caps_and_testpoints_do_not_count_as_extra_loads(self):
        splits = {"VDD_1V8IO": split_preview(
            "VDD_1V8IO",
            [("U302", "5"), ("C1", "1"), ("C2", "1"), ("TP1", "1"), ("J1", "26")],
            "U302", "5")}
        finding = check_load_isolation(self.rails, splits,
                                       {"VDD_1V8IO": ("J1", "26")})
        self.assertEqual(finding["status"], "PASS")

    def test_no_declared_intent_is_no_coverage(self):
        finding = check_load_isolation(self.rails, self.splits, {})
        self.assertEqual(finding["status"], NO_COVERAGE)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_cli_netlist.net"
        self.tmp.write_text(NETLIST, encoding="utf-8")

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def _base(self):
        return ["--rail", "VDD_1V2D=U303:5", "--netlist", str(self.tmp),
                "--full-scale-current-a", "0.3", "--max-droop-mv", "12",
                "--meter-resolution-uv", "10", "--current-resolution-ma", "1",
                "--package-power-w", "0.0625", "--kelvin"]

    def test_self_test_passes(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _fully_declared(self):
        return self._base() + ["--max-uncertainty-pct", "5",
                               "--rail-intended-load", "VDD_1V2D=J1:27"]

    def test_green_run_exits_zero(self):
        self.assertEqual(main(self._fully_declared()), 0)

    def test_an_undeclared_budget_is_not_green(self):
        # The sizing succeeds and every number is printed; the run is still
        # not green, because nothing said what would have been acceptable.
        code = main(self._base() + ["--rail-intended-load", "VDD_1V2D=J1:27"])
        self.assertEqual(code, 1)

    def test_an_undeclared_measurement_target_is_not_green(self):
        code = main(self._base() + ["--max-uncertainty-pct", "5"])
        self.assertEqual(code, 1)

    def test_missing_required_sizing_input_is_an_error_not_a_default(self):
        with self.assertRaises(ValueError):
            main(["--rail", "VDD_1V2D=U303:5", "--max-droop-mv", "12",
                  "--meter-resolution-uv", "10", "--current-resolution-ma", "1"])

    def test_downstream_override_without_a_reason_is_refused(self):
        with self.assertRaises(ValueError):
            main(self._base() + ["--position", "downstream_of_bypass",
                                 "--allow-downstream-of-bypass"])

    def test_malformed_rail_spec_is_rejected(self):
        for bad in (["VDD"], ["VDD=U1"], ["=U1:5"], ["VDD=U1:"]):
            with self.assertRaises(ValueError):
                parse_rail_specs(bad)

    def test_no_rail_at_all_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_rail_specs([])

    def test_override_for_an_undeclared_rail_is_rejected(self):
        with self.assertRaises(ValueError):
            main(self._base() + ["--rail-max-droop-mv", "GHOST=5"])

    def test_report_is_schema_shaped_and_deterministic(self):
        out = Path(__file__).resolve().parent / "_tmp_report.json"
        try:
            main(self._fully_declared() + ["--json", str(out)])
            first = out.read_text(encoding="utf-8")
            main(self._fully_declared() + ["--json", str(out)])
            second = out.read_text(encoding="utf-8")
            # Two runs of a deterministic solver must be byte-identical; a
            # single-run test cannot see dict ordering or a stray timestamp.
            self.assertEqual(first, second)
            report = json.loads(first)
            self.assertEqual(report["schema_version"], "shunt-plan/v1")
            self.assertFalse(report["mutates_schematic"])
            self.assertEqual(report["phase"], "early_schematic")
            self.assertEqual(len(report["injections"][0]["testpoints"]), 2)
            self.assertEqual(report["injections"][0]["net_split"]["load_side_net"],
                             "VDD_1V2D_SENSE")
        finally:
            out.unlink(missing_ok=True)

    def test_parser_exposes_both_positions(self):
        parser = build_parser()
        args = parser.parse_args(["--position", "downstream_of_bypass"])
        self.assertEqual(args.position, DOWNSTREAM_OF_BYPASS)


if __name__ == "__main__":
    unittest.main()
