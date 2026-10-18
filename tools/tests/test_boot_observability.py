"""Tests for common/observability.py and scripts/board/boot_observability.py.

THE ONE TEST THAT JUSTIFIES THE TOOL
--------------------------------------------------------------------------
``test_full_coverage_by_count_is_still_caught``: a plan where EVERY rail
carries an observation, and two failures are still indistinguishable. Any
tool that counted "rails with test points" reports 4/4 and green. This one
reports RED and names the pair.

Everything else here defends the distinctions that make that possible:

  * power-good is not a rail measurement (a regulator asserts PG into a
    rail held down by a downstream short);
  * ambiguity lives INSIDE a tier, because rails in different tiers are
    already separated in time - the later tier never starts;
  * one real measurement in an ambiguous tier of two resolves BOTH, because
    the other becomes distinguishable by elimination. This is the case a
    naive implementation gets wrong in the safe direction, reporting a gap
    that is not there and training readers to ignore the finding;
  * a reset is its own failure kind, because a reset released early gives a
    powered dead board that looks nothing like a missing rail.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.findings import FAIL, NO_COVERAGE, PASS  # noqa: E402
from common.observability import (ENABLE_UNOBSERVED, GREEN,  # noqa: E402
                                  INDISTINGUISHABLE, OBS_ENABLE, OBS_PG,
                                  OBS_RAIL, OBS_RESET, PG_ONLY, RED,
                                  RESET_RAIL_UNKNOWN, RESET_UNOBSERVABLE,
                                  UNOBSERVED_RAIL, Observation,
                                  ObservabilityError, check_resets,
                                  diagnosability, failure_classes,
                                  load_observation,
                                  observations_from_testpoint_plan,
                                  rail_observability, rollup,
                                  unobserved_enable_edges)
from common.railbudget import Rail, enable_order  # noqa: E402
from scripts.board.boot_observability import main, read_rails  # noqa: E402

SCRIPT = TOOLS / "scripts" / "board" / "boot_observability.py"

RAILS = [Rail("5V", 5.0),
         Rail("3V3", 3.3, enable_source="5V"),
         Rail("1V8", 1.8, enable_source="3V3"),
         Rail("1V2", 1.2, enable_source="3V3")]


def analyse(observations, rails=None, resets=()):
    rails = list(rails or RAILS)
    tiers = enable_order(rails)
    states = rail_observability(rails, observations, tiers)
    classes = failure_classes(rails, states, tiers)
    checks = check_resets(list(resets), rails, observations)
    return states, classes, checks, rollup(states, classes, checks)


# --------------------------------------------------------------------------- #
# Observation kinds
# --------------------------------------------------------------------------- #

class ObservationLoadTest(unittest.TestCase):
    def test_an_unlabelled_kind_does_not_load(self):
        # No default in either direction: guessing "rail" inflates coverage,
        # guessing "pg" hides real coverage.
        with self.assertRaises(ObservabilityError):
            load_observation({"net": "3V3"})

    def test_an_invented_kind_does_not_load(self):
        with self.assertRaises(ObservabilityError):
            load_observation({"net": "3V3", "kind": "testpoint"})

    def test_an_empty_net_does_not_load(self):
        with self.assertRaises(ObservabilityError):
            load_observation({"net": "   ", "kind": OBS_RAIL})

    def test_a_valid_observation_loads(self):
        obs = load_observation({"net": " 3V3 ", "kind": OBS_RAIL})
        self.assertEqual((obs.net, obs.kind), ("3V3", OBS_RAIL))


class TestpointPlanReadingTest(unittest.TestCase):
    def plan(self, *points):
        return {"testpoints": list(points)}

    def test_a_power_rail_point_proves_the_rail(self):
        out = observations_from_testpoint_plan(
            self.plan({"net": "3V3", "category": "power_rail"}))
        self.assertEqual(out[0].kind, OBS_RAIL)

    def test_a_pmic_enable_pg_point_does_not(self):
        out = observations_from_testpoint_plan(
            self.plan({"net": "3V3", "category": "pmic_enable_pg"}))
        self.assertEqual(out[0].kind, OBS_PG)

    def test_an_uncategorised_point_is_ignored_rather_than_assumed(self):
        # Guessing would inflate coverage with points whose meaning nobody
        # recorded.
        self.assertEqual(observations_from_testpoint_plan(
            self.plan({"net": "3V3"})), [])

    def test_an_unknown_category_is_ignored(self):
        self.assertEqual(observations_from_testpoint_plan(
            self.plan({"net": "3V3", "category": "mystery"})), [])

    def test_a_plan_with_no_testpoints_yields_nothing(self):
        self.assertEqual(observations_from_testpoint_plan({}), [])


# --------------------------------------------------------------------------- #
# Rail observability
# --------------------------------------------------------------------------- #

class RailObservabilityTest(unittest.TestCase):
    def test_an_unobserved_rail_is_a_failure(self):
        states, _, _, _ = analyse([])
        self.assertTrue(all(s.status == FAIL for s in states))
        self.assertTrue(all(s.reason == UNOBSERVED_RAIL for s in states))

    def test_a_measured_rail_passes(self):
        states, _, _, _ = analyse([Observation(r.name, OBS_RAIL)
                                   for r in RAILS])
        self.assertTrue(all(s.status == PASS for s in states))

    def test_pg_alone_is_not_a_rail_measurement(self):
        # A regulator can assert PG into a rail that is shorted downstream
        # and current-limiting: PG high, rail low, bench sees nothing wrong.
        states, _, _, _ = analyse([Observation(r.name, OBS_PG) for r in RAILS])
        self.assertTrue(all(s.status == NO_COVERAGE for s in states))
        self.assertTrue(all(s.reason == PG_ONLY for s in states))
        self.assertFalse(any(s.proven for s in states))

    def test_pg_alongside_a_rail_measurement_still_passes(self):
        observations = [Observation("5V", OBS_RAIL), Observation("5V", OBS_PG)]
        states, _, _, _ = analyse(observations)
        five = [s for s in states if s.rail == "5V"][0]
        self.assertEqual(five.status, PASS)
        self.assertEqual(five.kinds, (OBS_PG, OBS_RAIL))

    def test_an_enable_observation_does_not_prove_the_rail_it_gates(self):
        states, _, _, _ = analyse([Observation("3V3", OBS_ENABLE)])
        three = [s for s in states if s.rail == "3V3"][0]
        self.assertFalse(three.proven)

    def test_each_rail_records_its_tier(self):
        states, _, _, _ = analyse([])
        by_rail = {s.rail: s.tier for s in states}
        self.assertEqual(by_rail, {"5V": 0, "3V3": 1, "1V8": 2, "1V2": 2})


# --------------------------------------------------------------------------- #
# Distinguishable failure classes - the point of the tool
# --------------------------------------------------------------------------- #

class FailureClassTest(unittest.TestCase):
    def test_full_coverage_by_count_is_still_caught(self):
        # THE test. Every rail has an observation; a tool counting "rails
        # with test points" says 4/4 and green. Two failures remain
        # indistinguishable.
        observations = [Observation("5V", OBS_RAIL), Observation("3V3", OBS_RAIL),
                        Observation("1V8", OBS_PG), Observation("1V2", OBS_PG)]
        states, classes, _, (status, reasons) = analyse(observations)
        self.assertTrue(all(s.kinds for s in states))     # nothing is bare
        self.assertEqual(status, RED)
        self.assertIn(INDISTINGUISHABLE, reasons)
        ambiguous = [c for c in classes if c.ambiguous]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(set(ambiguous[0].rails), {"1V2", "1V8"})

    def test_one_measurement_resolves_a_tier_of_two_by_elimination(self):
        # The case a naive implementation gets wrong in the "safe"
        # direction: it reports 1V2 as ambiguous when it is the only
        # unobserved rail left in its tier, and a finding that cries wolf
        # trains readers to ignore it.
        observations = [Observation("5V", OBS_RAIL), Observation("3V3", OBS_RAIL),
                        Observation("1V8", OBS_RAIL)]
        _, classes, _, _ = analyse(observations)
        self.assertFalse(any(c.ambiguous for c in classes))

    def test_but_elimination_does_not_make_the_run_green(self):
        # 1V2 is still not measured. Distinguishable is not the same as
        # observed, and collapsing the two would report a board as fully
        # instrumented when one rail can only be inferred.
        observations = [Observation("5V", OBS_RAIL), Observation("3V3", OBS_RAIL),
                        Observation("1V8", OBS_RAIL)]
        _, _, _, (status, _) = analyse(observations)
        self.assertEqual(status, RED)   # 1V2 has NOTHING at all

    def test_pg_only_in_the_last_slot_is_no_coverage_not_red(self):
        observations = [Observation("5V", OBS_RAIL), Observation("3V3", OBS_RAIL),
                        Observation("1V8", OBS_RAIL), Observation("1V2", OBS_PG)]
        _, classes, _, (status, reasons) = analyse(observations)
        self.assertFalse(any(c.ambiguous for c in classes))
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(reasons, [PG_ONLY])

    def test_rails_in_different_tiers_are_never_ambiguous_together(self):
        # They are separated in time: the later tier never starts. With
        # NOTHING observable, all four rails are blind - yet they must still
        # resolve into three classes (one per tier), not one class of four.
        tier_of = {"5V": 0, "3V3": 1, "1V8": 2, "1V2": 2}
        _, classes, _, _ = analyse([])
        self.assertEqual(len(classes), 3)
        for group in classes:
            with self.subTest(rails=group.rails):
                self.assertEqual(len({tier_of[r] for r in group.rails}), 1)

    def test_a_fully_measured_board_has_one_class_per_rail(self):
        observations = [Observation(r.name, OBS_RAIL) for r in RAILS]
        states, classes, _, (status, _) = analyse(observations)
        self.assertEqual(status, GREEN)
        self.assertEqual(len(classes), len(RAILS))
        self.assertTrue(all(len(c.rails) == 1 for c in classes))

    def test_diagnosability_reports_both_numbers(self):
        observations = [Observation("5V", OBS_RAIL), Observation("3V3", OBS_RAIL),
                        Observation("1V8", OBS_PG), Observation("1V2", OBS_PG)]
        states, classes, _, _ = analyse(observations)
        counts = diagnosability(states, classes)
        self.assertEqual(counts["rails"], 4)
        self.assertEqual(counts["rails_proven"], 2)
        self.assertEqual(counts["rails_pg_only"], 2)
        self.assertEqual(counts["ambiguous_classes"], 1)
        self.assertLess(counts["distinguishable_classes"], counts["rails"])


# --------------------------------------------------------------------------- #
# Reset
# --------------------------------------------------------------------------- #

class ResetTest(unittest.TestCase):
    def measured(self):
        return [Observation(r.name, OBS_RAIL) for r in RAILS]

    def test_an_unobservable_reset_is_its_own_failure(self):
        _, _, checks, (status, reasons) = analyse(
            self.measured(), resets=[{"net": "nRST", "waits_for": ["1V2"]}])
        self.assertEqual(checks[0].status, FAIL)
        self.assertEqual(checks[0].reason, RESET_UNOBSERVABLE)
        self.assertIn(RESET_UNOBSERVABLE, reasons)
        self.assertEqual(status, RED)

    def test_an_observable_reset_over_measured_rails_passes(self):
        observations = self.measured() + [Observation("nRST", OBS_RESET)]
        _, _, checks, (status, _) = analyse(
            observations, resets=[{"net": "nRST", "waits_for": ["1V2"]}])
        self.assertEqual(checks[0].status, PASS)
        self.assertEqual(status, GREEN)

    def test_an_observable_reset_over_an_unmeasured_rail_is_half_a_comparison(self):
        observations = [Observation("5V", OBS_RAIL),
                        Observation("3V3", OBS_RAIL),
                        Observation("1V8", OBS_RAIL),
                        Observation("1V2", OBS_PG),
                        Observation("nRST", OBS_RESET)]
        _, _, checks, _ = analyse(
            observations, resets=[{"net": "nRST", "waits_for": ["1V2"]}])
        self.assertEqual(checks[0].status, NO_COVERAGE)
        self.assertEqual(checks[0].reason, UNOBSERVED_RAIL)

    def test_a_reset_waiting_on_an_undeclared_rail_is_refused(self):
        observations = self.measured() + [Observation("nRST", OBS_RESET)]
        _, _, checks, _ = analyse(
            observations, resets=[{"net": "nRST", "waits_for": ["0V9"]}])
        self.assertEqual(checks[0].status, FAIL)
        self.assertEqual(checks[0].reason, RESET_RAIL_UNKNOWN)

    def test_a_reset_without_a_net_is_refused(self):
        with self.assertRaises(ObservabilityError):
            check_resets([{"waits_for": []}], RAILS, [])

    def test_a_reset_waiting_on_nothing_still_needs_to_be_visible(self):
        _, _, checks, _ = analyse(self.measured(), resets=[{"net": "nRST"}])
        self.assertEqual(checks[0].reason, RESET_UNOBSERVABLE)


# --------------------------------------------------------------------------- #
# Enable edges
# --------------------------------------------------------------------------- #

class EnableEdgeTest(unittest.TestCase):
    def test_an_enable_driven_by_an_observed_rail_is_not_a_gap(self):
        observations = [Observation(r.name, OBS_RAIL) for r in RAILS]
        self.assertEqual(unobserved_enable_edges(RAILS, observations), [])

    def test_an_invisible_enable_source_is_reported(self):
        edges = unobserved_enable_edges(RAILS, [])
        self.assertTrue(edges)
        self.assertTrue(all(e["issue"] == ENABLE_UNOBSERVED for e in edges))

    def test_always_on_rails_have_no_enable_edge(self):
        edges = unobserved_enable_edges([Rail("5V", 5.0)], [])
        self.assertEqual(edges, [])

    def test_a_pg_observation_on_the_source_is_enough_for_the_edge(self):
        # The edge question is "did the gate open", and PG answers that even
        # though it does not prove the rail itself.
        edges = unobserved_enable_edges(
            [Rail("3V3", 3.3, enable_source="5V")],
            [Observation("5V", OBS_PG)])
        self.assertEqual(edges, [])


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

class RollupTest(unittest.TestCase):
    def test_an_empty_board_is_no_coverage(self):
        status, reasons = rollup([], [], [])
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(reasons, ["NO_RAILS_DECLARED"])

    def test_red_reports_every_distinct_reason(self):
        _, _, _, (status, reasons) = analyse(
            [], resets=[{"net": "nRST", "waits_for": ["1V2"]}])
        self.assertEqual(status, RED)
        self.assertEqual(sorted(reasons),
                         sorted([UNOBSERVED_RAIL, INDISTINGUISHABLE,
                                 RESET_UNOBSERVABLE]))

    def test_this_tool_is_allowed_to_be_red(self):
        # Unlike topology_advise: an unobservable rail is a defect in the
        # artifacts being read, not a shortfall somebody else measured.
        _, _, _, (status, _) = analyse([])
        self.assertEqual(status, RED)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir,
                                                            ignore_errors=True))

    def write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def rails_file(self, resets=()):
        return self.write("rails.json", {
            "rails": [{"name": "5V", "voltage_v": 5.0},
                      {"name": "3V3", "voltage_v": 3.3,
                       "enable_source": "5V"},
                      {"name": "1V8", "voltage_v": 1.8,
                       "enable_source": "3V3"},
                      {"name": "1V2", "voltage_v": 1.2,
                       "enable_source": "3V3"}],
            "resets": list(resets)})

    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def report_for(self, argv):
        out = self.dir / "out.json"
        code = self.run_main(argv + ["--json", str(out)])
        return code, json.loads(out.read_text(encoding="utf-8"))

    def all_measured(self):
        return ["--observation", "5V=rail_voltage",
                "--observation", "3V3=rail_voltage",
                "--observation", "1V8=rail_voltage",
                "--observation", "1V2=rail_voltage"]

    def test_self_test_passes(self):
        self.assertEqual(self.run_main(["--self-test"]), 0)

    def test_rails_is_required(self):
        with self.assertRaises(ValueError):
            self.run_main([])

    def test_a_rail_without_a_voltage_is_refused(self):
        path = self.write("bad.json", {"rails": [{"name": "5V"}]})
        with self.assertRaises(ValueError):
            read_rails(path)

    def test_an_empty_rail_list_is_refused(self):
        path = self.write("bad.json", {"rails": []})
        with self.assertRaises(ValueError):
            read_rails(path)

    def test_a_malformed_observation_argument_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_main(["--rails", str(self.rails_file()),
                           "--observation", "no-equals-sign"])

    def test_an_enable_cycle_is_refused_rather_than_ordered(self):
        path = self.write("cycle.json", {
            "rails": [{"name": "A", "voltage_v": 1.0, "enable_source": "B"},
                      {"name": "B", "voltage_v": 1.0, "enable_source": "A"}]})
        with self.assertRaises(ValueError):
            self.run_main(["--rails", str(path)])

    def test_a_fully_measured_board_is_green_and_exits_zero(self):
        """Fully measured now means the RESET too. This fixture used to
        declare none and still expect GREEN, which is what
        reset_finding()'s own text calls out: 'a board with no declared reset
        is not a board with no reset'. The rollup ignored that finding, so
        the test passed for the wrong reason."""
        code, report = self.report_for(
            ["--rails", str(self.rails_file(
                [{"net": "nRST", "waits_for": ["1V2"]}]))]
            + self.all_measured() + ["--observation", "nRST=reset_signal"])
        self.assertEqual(report["status"], GREEN)
        self.assertEqual(code, 0)
        self.assertEqual(report["diagnosability"]["ambiguous_classes"], 0)

    def test_an_undeclared_reset_is_no_coverage_not_green(self):
        """Every rail measurable, no reset declared: the rails really are
        observable and the reset sequence really is not. That is precisely
        NO_COVERAGE, and it used to report GREEN because the rollup only
        looked at the FAIL count."""
        code, report = self.report_for(
            ["--rails", str(self.rails_file())] + self.all_measured())
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertNotEqual(code, 0)
        reset = [f for f in report["findings"]
                 if f["check"] == "reset_sequence_observable"][0]
        self.assertEqual(reset["status"], NO_COVERAGE)

    def test_the_ambiguous_case_is_red_and_names_the_pair(self):
        code, report = self.report_for(
            ["--rails", str(self.rails_file()),
             "--observation", "5V=rail_voltage",
             "--observation", "3V3=rail_voltage",
             "--observation", "1V8=power_good",
             "--observation", "1V2=power_good"])
        self.assertEqual(report["status"], RED)
        self.assertNotEqual(code, 0)
        ambiguous = [c for c in report["failure_classes"] if c["ambiguous"]]
        self.assertEqual(sorted(ambiguous[0]["rails"]), ["1V2", "1V8"])

    def test_the_tiers_come_from_the_enable_graph(self):
        _, report = self.report_for(
            ["--rails", str(self.rails_file())] + self.all_measured())
        self.assertEqual(report["power_up_tiers"],
                         [["5V"], ["3V3"], ["1V2", "1V8"]])

    def test_a_testpoint_plan_supplies_observations(self):
        """The point under test is that the plan is read as observations at
        all. It needs a declared+probed reset for the same reason as
        test_a_fully_measured_board_is_green_and_exits_zero."""
        plan = self.write("plan.json", {"testpoints": [
            {"net": "5V", "category": "power_rail"},
            {"net": "3V3", "category": "power_rail"},
            {"net": "1V8", "category": "power_rail"},
            {"net": "1V2", "category": "power_rail"}]})
        code, report = self.report_for(
            ["--rails", str(self.rails_file(
                [{"net": "nRST", "waits_for": ["1V2"]}])),
             "--testpoint-plan", str(plan),
             "--observation", "nRST=reset_signal"])
        self.assertEqual(report["status"], GREEN)
        self.assertEqual(code, 0)

    def test_a_testpoint_plan_of_pg_points_alone_is_not_green(self):
        plan = self.write("plan.json", {"testpoints": [
            {"net": name, "category": "pmic_enable_pg"}
            for name in ("5V", "3V3", "1V8", "1V2")]})
        _, report = self.report_for(
            ["--rails", str(self.rails_file()),
             "--testpoint-plan", str(plan)])
        self.assertNotEqual(report["status"], GREEN)

    def test_the_report_declares_it_does_not_check_physical_access(self):
        _, report = self.report_for(
            ["--rails", str(self.rails_file())] + self.all_measured())
        self.assertFalse(report["checks_physical_access"])
        self.assertEqual(report["phase"], "early_schematic")
        self.assertFalse(report["signoff"])

    def test_a_reset_is_reported_separately_from_the_rails(self):
        code, report = self.report_for(
            ["--rails", str(self.rails_file(
                [{"net": "nRST", "waits_for": ["1V2"]}]))] + self.all_measured())
        self.assertEqual(report["status"], RED)
        rails_finding = [f for f in report["findings"]
                         if f["check"] == "rails_observable"][0]
        reset_finding = [f for f in report["findings"]
                         if f["check"] == "reset_sequence_observable"][0]
        self.assertEqual(rails_finding["status"], PASS)
        self.assertEqual(reset_finding["status"], FAIL)

    def test_the_report_validates_against_its_schema(self):
        try:
            import jsonschema
        except ImportError:                       # pragma: no cover
            self.skipTest("jsonschema is not installed")
        schema = json.loads((TOOLS / "schemas" /
                             "boot_observability_report.schema.json")
                            .read_text(encoding="utf-8"))
        _, report = self.report_for(
            ["--rails", str(self.rails_file(
                [{"net": "nRST", "waits_for": ["1V2"]}]))]
            + self.all_measured() + ["--observation", "nRST=reset_signal"])
        jsonschema.validate(report, schema)


class SubprocessTest(unittest.TestCase):
    def test_self_test_passes_as_a_subprocess(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--self-test"],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SELF-TEST PASS", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
