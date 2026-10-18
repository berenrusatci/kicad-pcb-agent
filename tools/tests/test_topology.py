"""Tests for common/topology.py and scripts/topology_advise.py.

WHAT WOULD MAKE THIS TOOL WORTHLESS
--------------------------------------------------------------------------
Three failure modes, and the tests are written against them rather than
against the happy path:

  1. It becomes a suggestion mill - every shortfall produces every
     transformation, because nothing can rule one out. Guarded by requiring
     a non-empty exclusion list at load time and by testing that the
     exclusions actually fire.
  2. It starts predicting quantities. A magnitude here would be read as a
     result and there is no solver behind it. Guarded by refusing catalogue
     text that contains a unit, and by asserting that every offered proposal
     reports ``magnitude: null``.
  3. It claims territory an existing solver already owns, which is how a
     second implementation of one rule gets started - the #54 failure.
     Guarded by the ``already_owned`` entries and a test that they are
     reported rather than silently skipped.

There is also a boundary worth stating: this module never returns RED on a
design. It measures nothing, so it cannot find a defect; an excluded
proposal is a correct answer and an unaddressable shortfall is NO_COVERAGE.
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
from common.topology import (ALREADY_OWNED, CATALOGUE_ENTRIES,  # noqa: E402
                             COST_UNDECLARED, EXCLUDED, GREEN,
                             KIND_OWNED, KIND_TOPOLOGY, MAGNITUDE_CLAIMED,
                             NO_TRANSFORM_KNOWN, Observation,
                             TransformSyntaxError, UNCONDITIONAL_TRANSFORM,
                             UNFALSIFIABLE_PROPOSAL, UNVERIFIABLE_PROPOSAL,
                             default_catalogue, load_transform,
                             observations_from_report, propose, rollup,
                             uncovered)
from scripts.topology_advise import main, parse_context, read_catalogue  # noqa: E402

SCRIPT = TOOLS / "scripts" / "topology_advise.py"

WELL_FORMED = {
    "id": "t1",
    "title": "Add a thing",
    "adds": "one part",
    "metric": "some quantity",
    "direction": "decrease",
    "mechanism": "because of what the added element does",
    "triggers": [{"tool": "some_tool", "check": "some_check"}],
    "verified_by": "some_tool",
    "refuted_by": "the quantity does not move",
    "cost": ["one BOM line"],
    "excluded_when": [{"fact": "f", "equals": True, "because": "a reason"}],
}


def build(**overrides):
    entry = {k: v for k, v in {**WELL_FORMED, **overrides}.items()
             if v is not None}
    return load_transform(entry)


# --------------------------------------------------------------------------- #
# Load-time refusals
# --------------------------------------------------------------------------- #

class LoadRefusalTest(unittest.TestCase):
    def assertRefuses(self, code, **overrides):
        with self.assertRaises(TransformSyntaxError) as caught:
            build(**overrides)
        self.assertEqual(caught.exception.code, code)

    def test_a_proposal_with_no_verifier_does_not_load(self):
        # A proposal nobody can check is advice.
        self.assertRefuses(UNVERIFIABLE_PROPOSAL, verified_by=None)

    def test_a_proposal_with_no_refuting_observation_does_not_load(self):
        # A hypothesis that survives every outcome is advertising.
        self.assertRefuses(UNFALSIFIABLE_PROPOSAL, refuted_by=None)

    def test_a_proposal_with_no_cost_does_not_load(self):
        self.assertRefuses(COST_UNDECLARED, cost=None)

    def test_an_empty_cost_list_does_not_load(self):
        self.assertRefuses(COST_UNDECLARED, cost=[])

    def test_a_transformation_nothing_can_rule_out_does_not_load(self):
        # One that fits every shortfall fits none.
        self.assertRefuses(UNCONDITIONAL_TRANSFORM, excluded_when=None)

    def test_an_empty_exclusion_list_does_not_load(self):
        self.assertRefuses(UNCONDITIONAL_TRANSFORM, excluded_when=[])

    def test_a_metric_carrying_a_unit_does_not_load(self):
        self.assertRefuses(MAGNITUDE_CLAIMED, metric="return loss in dB")

    def test_a_mechanism_carrying_a_number_with_a_unit_does_not_load(self):
        self.assertRefuses(MAGNITUDE_CLAIMED,
                           mechanism="it drops the impedance by 3 ohm")

    def test_a_percentage_is_also_a_magnitude(self):
        self.assertRefuses(MAGNITUDE_CLAIMED, metric="ripple in %")

    def test_a_transformation_with_no_trigger_does_not_load(self):
        # Never proposed and never reviewed.
        self.assertRefuses("MISSING_TRIGGER", triggers=None)

    def test_a_trigger_naming_only_a_tool_does_not_load(self):
        # It would fire on every report that tool ever produced, including
        # its passing ones.
        self.assertRefuses("BAD_TRIGGER", triggers=[{"tool": "some_tool"}])

    def test_an_invalid_direction_does_not_load(self):
        self.assertRefuses("BAD_DIRECTION", direction="better")

    def test_an_exclusion_without_a_reason_does_not_load(self):
        self.assertRefuses("BAD_EXCLUSION",
                           excluded_when=[{"fact": "f", "equals": True}])

    def test_an_exclusion_without_a_value_does_not_load(self):
        self.assertRefuses("BAD_EXCLUSION",
                           excluded_when=[{"fact": "f", "because": "r"}])

    def test_an_owned_entry_must_name_its_owner(self):
        with self.assertRaises(TransformSyntaxError):
            load_transform({**WELL_FORMED, "kind": KIND_OWNED,
                            "verified_by": None, "owned_by": None})


class ShippedCatalogueTest(unittest.TestCase):
    """The refusals are only real if the shipped entries pass them too."""

    def setUp(self):
        self.catalogue = default_catalogue()

    def test_the_built_in_catalogue_loads(self):
        self.assertTrue(self.catalogue)

    def test_every_topology_entry_names_a_verifier_and_a_refutation(self):
        for transform in self.catalogue:
            if transform.kind != KIND_TOPOLOGY:
                continue
            with self.subTest(transform=transform.id):
                self.assertTrue(transform.verified_by.strip())
                self.assertTrue(transform.refuted_by.strip())

    def test_every_topology_entry_can_be_ruled_out(self):
        for transform in self.catalogue:
            if transform.kind != KIND_TOPOLOGY:
                continue
            with self.subTest(transform=transform.id):
                self.assertTrue(transform.excluded_when)

    def test_every_topology_entry_states_a_cost(self):
        for transform in self.catalogue:
            if transform.kind != KIND_TOPOLOGY:
                continue
            with self.subTest(transform=transform.id):
                self.assertTrue(transform.cost)

    def test_every_trigger_names_a_tool_that_exists_in_this_tree(self):
        # A trigger keyed on a tool nobody ships never fires, and a
        # catalogue entry that never fires looks like coverage.
        known = {"decoupling_coverage", "rail_budget", "emc_rules",
                 "stitch_density", "crosstalk_thinning", "loop_stability"}
        for transform in self.catalogue:
            for trigger in transform.triggers:
                with self.subTest(transform=transform.id, tool=trigger.tool):
                    self.assertIn(trigger.tool, known)

    def test_no_shipped_entry_carries_a_magnitude(self):
        for transform in self.catalogue:
            with self.subTest(transform=transform.id):
                self.assertIsNone(transform.to_dict()["magnitude"])

    def test_ids_are_unique(self):
        ids = [entry["id"] for entry in CATALOGUE_ENTRIES]
        self.assertEqual(len(ids), len(set(ids)))


# --------------------------------------------------------------------------- #
# Reading another tool's report
# --------------------------------------------------------------------------- #

class ObservationTest(unittest.TestCase):
    def report(self, status, violations=()):
        return {"tool": "decoupling_coverage", "reason_codes": ["X"],
                "findings": [{"check": "decoupling_coverage", "status": status,
                              "violations": list(violations), "detail": "d"}]}

    def test_a_failing_finding_becomes_a_shortfall(self):
        out = observations_from_report(self.report(FAIL))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].check, "decoupling_coverage")

    def test_a_no_coverage_finding_is_also_a_shortfall(self):
        self.assertEqual(len(observations_from_report(self.report(NO_COVERAGE))), 1)

    def test_a_passing_finding_is_not_a_shortfall(self):
        # Proposing against a passing check is how a tool starts
        # recommending work for its own sake.
        self.assertEqual(observations_from_report(self.report(PASS)), [])

    def test_violation_problem_strings_are_collected(self):
        out = observations_from_report(
            self.report(FAIL, [{"problem": "no_capacitor"},
                               {"problem": "no_capacitor"},
                               {"problem": "capacitor_too_far"}]))
        self.assertEqual(out[0].problems,
                         ("no_capacitor", "capacitor_too_far"))

    def test_report_level_reason_codes_reach_the_observation(self):
        out = observations_from_report(self.report(FAIL))
        self.assertEqual(out[0].reason_codes, ("X",))

    def test_a_report_with_no_findings_yields_nothing(self):
        self.assertEqual(observations_from_report({"tool": "x"}), [])


# --------------------------------------------------------------------------- #
# Matching and exclusion
# --------------------------------------------------------------------------- #

class ProposeTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = default_catalogue()
        self.decoupling = Observation(
            tool="decoupling_coverage", check="decoupling_coverage",
            status=FAIL, problems=("no_capacitor",))
        self.rail = Observation(
            tool="rail_budget", check="noise_within_budget", status=FAIL,
            reason_codes=("NOISE_BUDGET_EXCEEDED",))

    def offered(self, observation, context=None):
        return {p.transform.id
                for p in propose([observation], self.catalogue, context or {})
                if p.offered}

    def test_a_missing_capacitor_gets_a_second_stage(self):
        self.assertEqual(self.offered(self.decoupling),
                         {"add-decoupling-stage"})

    def test_a_capacitor_that_is_merely_too_far_gets_nothing(self):
        # The exclusion that carries the most weight: a part that EXISTS and
        # sits too far away is a placement problem, and a second BOM line
        # does not shorten the first one's loop.
        proposals = propose([self.decoupling], self.catalogue,
                            {"shortfall": "capacitor_too_far"})
        self.assertTrue(proposals)
        self.assertFalse(any(p.offered for p in proposals))
        self.assertEqual(proposals[0].reason, EXCLUDED)

    def test_an_exclusion_explains_itself(self):
        proposals = propose([self.decoupling], self.catalogue,
                            {"shortfall": "capacitor_too_far"})
        self.assertIn("placement problem", proposals[0].detail)

    def test_the_noise_term_picks_between_two_rail_entries(self):
        # Both trigger on the same reason code; only the context separates
        # them, which is precisely what stops this being a suggestion mill.
        self.assertEqual(self.offered(self.rail,
                                      {"dominant_noise_term": "deterministic"}),
                         {"add-lc-post-filter"})
        self.assertEqual(self.offered(self.rail,
                                      {"dominant_noise_term": "random"}),
                         {"add-bulk-stage"})

    def test_without_the_context_both_rail_entries_are_offered(self):
        # Honest: with no fact to discriminate, the tool does not guess.
        self.assertEqual(self.offered(self.rail),
                         {"add-lc-post-filter", "add-bulk-stage"})

    def test_a_split_plane_rules_out_the_stitching_row(self):
        # Stitching a split plane ties two references that were never one.
        pitch = Observation(tool="emc_rules", check="emc_via_stitching_pitch",
                            status=FAIL)
        self.assertEqual(self.offered(pitch), {"add-stitching-via-row"})
        self.assertEqual(self.offered(pitch, {"reference_plane_split": True}),
                         set())

    def test_an_unstitchable_guard_is_ruled_out(self):
        # An unstitched guard is the textbook way to make crosstalk worse
        # while appearing to address it.
        risk = Observation(tool="crosstalk_thinning", check="crosstalk",
                           status=FAIL, reason_codes=("CROSSTALK_RISK",))
        self.assertEqual(self.offered(risk), {"add-guard-trace"})
        self.assertEqual(self.offered(risk, {"guard_can_be_stitched": False}),
                         set())

    def test_a_conditionally_stable_loop_rules_out_the_feedforward_zero(self):
        loop = Observation(tool="loop_stability",
                           check="stability_margins_within_budget", status=FAIL,
                           reason_codes=("PHASE_MARGIN_BELOW_BUDGET",))
        self.assertEqual(self.offered(loop), {"add-feedforward-zero"})
        self.assertEqual(self.offered(loop, {"conditionally_stable": True}),
                         set())

    def test_a_parameter_change_is_reported_as_already_owned(self):
        spacing = Observation(tool="emc_rules", check="emc_3w_crosstalk",
                              status=FAIL)
        proposals = propose([spacing], self.catalogue)
        self.assertEqual(len(proposals), 1)
        self.assertFalse(proposals[0].offered)
        self.assertEqual(proposals[0].reason, ALREADY_OWNED)
        self.assertEqual(proposals[0].transform.owned_by, "crosstalk_thinning")

    def test_the_already_owned_entry_is_not_silently_skipped(self):
        # Silence reads as "no tool covers this" and sends somebody building
        # a second one.
        spacing = Observation(tool="emc_rules", check="emc_3w_crosstalk",
                              status=FAIL)
        self.assertTrue(propose([spacing], self.catalogue))

    def test_a_trigger_does_not_fire_for_another_tool(self):
        impostor = Observation(tool="somebody_else",
                               check="decoupling_coverage", status=FAIL,
                               problems=("no_capacitor",))
        self.assertEqual(propose([impostor], self.catalogue), [])

    def test_a_trigger_does_not_fire_without_its_problem_string(self):
        vague = Observation(tool="decoupling_coverage",
                            check="decoupling_coverage", status=FAIL)
        self.assertEqual(propose([vague], self.catalogue), [])

    def test_an_unknown_context_key_excludes_nothing(self):
        self.assertEqual(self.offered(self.decoupling, {"unrelated": True}),
                         {"add-decoupling-stage"})

    def test_an_exclusion_needs_the_exact_value(self):
        self.assertEqual(self.offered(self.decoupling,
                                      {"shortfall": "something_else"}),
                         {"add-decoupling-stage"})

    def test_no_offered_proposal_carries_a_magnitude(self):
        for observation in (self.decoupling, self.rail):
            for proposal in propose([observation], self.catalogue):
                if not proposal.offered:
                    continue
                with self.subTest(transform=proposal.transform.id):
                    self.assertIsNone(
                        proposal.to_dict()["predicted"]["magnitude"])

    def test_every_offered_proposal_names_its_verifier_and_cost(self):
        for proposal in propose([self.decoupling], self.catalogue):
            if not proposal.offered:
                continue
            payload = proposal.to_dict()
            self.assertTrue(payload["verify_with"])
            self.assertTrue(payload["refuted_if"])
            self.assertTrue(payload["cost"])


class CoverageTest(unittest.TestCase):
    def test_a_shortfall_nothing_addresses_is_listed(self):
        unknown = Observation(tool="nobody", check="whatever", status=FAIL)
        catalogue = default_catalogue()
        self.assertEqual(uncovered([unknown], propose([unknown], catalogue)),
                         [unknown])

    def test_an_addressed_shortfall_is_not_listed(self):
        known = Observation(tool="decoupling_coverage",
                            check="decoupling_coverage", status=FAIL,
                            problems=("no_capacitor",))
        catalogue = default_catalogue()
        self.assertEqual(uncovered([known], propose([known], catalogue)), [])

    def test_an_excluded_shortfall_still_counts_as_addressed(self):
        # The catalogue DID have something to say: that the fix is wrong here.
        known = Observation(tool="decoupling_coverage",
                            check="decoupling_coverage", status=FAIL,
                            problems=("no_capacitor",))
        catalogue = default_catalogue()
        proposals = propose([known], catalogue,
                            {"shortfall": "capacitor_too_far"})
        self.assertEqual(uncovered([known], proposals), [])


class RollupTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = default_catalogue()
        self.shortfall = Observation(tool="decoupling_coverage",
                                     check="decoupling_coverage", status=FAIL,
                                     problems=("no_capacitor",))

    def test_an_offered_proposal_is_green(self):
        self.assertEqual(
            rollup(propose([self.shortfall], self.catalogue), [])[0], GREEN)

    def test_an_unaddressable_shortfall_is_no_coverage(self):
        unknown = Observation(tool="nobody", check="x", status=FAIL)
        status, reasons = rollup([], [unknown])
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(reasons, [NO_TRANSFORM_KNOWN])

    def test_everything_excluded_is_no_coverage_not_green(self):
        proposals = propose([self.shortfall], self.catalogue,
                            {"shortfall": "capacitor_too_far"})
        status, reasons = rollup(proposals, [])
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(reasons, [EXCLUDED])

    def test_nothing_at_all_is_no_coverage(self):
        self.assertEqual(rollup([], [])[0], NO_COVERAGE)

    def test_this_module_never_returns_red_on_a_design(self):
        # It measures nothing, so it cannot find a defect.
        for proposals, gaps in (
                (propose([self.shortfall], self.catalogue), []),
                ([], [Observation(tool="x", check="y", status=FAIL)]),
                ([], [])):
            self.assertNotEqual(rollup(proposals, gaps)[0], "RED")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class ContextParsingTest(unittest.TestCase):
    def test_json_literals_stay_typed(self):
        # The string "false" would silently fail to exclude, and a guard that
        # cannot be stitched is the case that matters most.
        context = parse_context(["guard_can_be_stitched=false", "n=3"])
        self.assertIs(context["guard_can_be_stitched"], False)
        self.assertEqual(context["n"], 3)

    def test_a_bare_word_stays_a_string(self):
        self.assertEqual(parse_context(["dominant_noise_term=random"]),
                         {"dominant_noise_term": "random"})

    def test_a_malformed_pair_is_refused(self):
        with self.assertRaises(ValueError):
            parse_context(["no-equals-sign"])

    def test_an_empty_key_is_refused(self):
        with self.assertRaises(ValueError):
            parse_context(["=value"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir,
                                                            ignore_errors=True))

    def write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def decoupling_report(self):
        return self.write("decoupling.json", {
            "tool": "decoupling_coverage", "status": "RED",
            "reason_codes": [],
            "findings": [{"check": "decoupling_coverage", "status": FAIL,
                          "scanned": 4,
                          "violations": [{"problem": "no_capacitor"}],
                          "detail": "one pin uncovered"}]})

    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def report_for(self, argv):
        out = self.dir / "out.json"
        code = self.run_main(argv + ["--json", str(out)])
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_self_test_passes(self):
        self.assertEqual(self.run_main(["--self-test"]), 0)

    def test_list_catalogue_exits_clean(self):
        self.assertEqual(self.run_main(["--list-catalogue"]), 0)

    def test_report_is_required(self):
        with self.assertRaises(ValueError):
            self.run_main([])

    def test_a_malformed_report_argument_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_main(["--report", "no-equals-sign"])

    def test_a_shortfall_produces_an_offered_proposal(self):
        code, report = self.report_for(
            ["--report", f"d={self.decoupling_report()}"])
        self.assertEqual(report["status"], GREEN)
        self.assertEqual(code, 0)
        offered = [p for p in report["proposals"] if p["offered"]]
        self.assertEqual(len(offered), 1)
        self.assertEqual(offered[0]["transform"], "add-decoupling-stage")

    def test_the_report_never_carries_a_magnitude(self):
        _, report = self.report_for(
            ["--report", f"d={self.decoupling_report()}"])
        for proposal in report["proposals"]:
            if proposal["offered"]:
                self.assertIsNone(proposal["predicted"]["magnitude"])

    def test_the_report_declares_that_it_runs_no_solver(self):
        _, report = self.report_for(
            ["--report", f"d={self.decoupling_report()}"])
        self.assertTrue(report["runs_no_solver"])
        self.assertTrue(report["measures_nothing"])
        self.assertFalse(report["signoff"])

    def test_context_reaches_the_exclusions(self):
        code, report = self.report_for(
            ["--report", f"d={self.decoupling_report()}",
             "--context", "shortfall=capacitor_too_far"])
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertNotEqual(code, 0)
        self.assertEqual(report["proposals"][0]["issue"], EXCLUDED)

    def test_a_passing_report_yields_no_shortfall(self):
        clean = self.write("clean.json", {
            "tool": "decoupling_coverage", "status": "GREEN",
            "findings": [{"check": "decoupling_coverage", "status": PASS,
                          "scanned": 4, "violations": [], "detail": ""}]})
        code, report = self.report_for(["--report", f"d={clean}"])
        self.assertEqual(report["observations"], [])
        self.assertNotEqual(code, 0)

    def test_a_report_without_a_tool_name_is_a_failure(self):
        nameless = self.write("nameless.json", {
            "findings": [{"check": "c", "status": FAIL, "scanned": 1,
                          "violations": [], "detail": ""}]})
        _, report = self.report_for(["--report", f"d={nameless}"])
        self.assertEqual(report["status"], "RED")
        self.assertEqual(report["load_errors"][0]["issue"], "TOOL_UNNAMED")

    def test_an_unaddressable_shortfall_is_reported_not_dropped(self):
        alien = self.write("alien.json", {
            "tool": "some_other_tool",
            "findings": [{"check": "whatever", "status": FAIL, "scanned": 1,
                          "violations": [], "detail": ""}]})
        _, report = self.report_for(["--report", f"d={alien}"])
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertEqual(report["uncovered"][0]["tool"], "some_other_tool")

    def test_a_user_catalogue_goes_through_the_same_refusals(self):
        bad = self.write("cat.json", {
            "schema_version": "topology-catalogue/v1",
            "transforms": [{k: v for k, v in WELL_FORMED.items()
                            if k != "excluded_when"}]})
        catalogue, errors = read_catalogue(bad)
        self.assertEqual(catalogue, [])
        self.assertEqual(errors[0]["issue"], UNCONDITIONAL_TRANSFORM)

    def test_a_wrong_catalogue_schema_version_is_refused(self):
        bad = self.write("cat.json", {"schema_version": "nope",
                                      "transforms": [WELL_FORMED]})
        with self.assertRaises(ValueError):
            read_catalogue(bad)

    def test_a_broken_catalogue_makes_the_run_red(self):
        bad = self.write("cat.json", {
            "schema_version": "topology-catalogue/v1",
            "transforms": [{k: v for k, v in WELL_FORMED.items()
                            if k != "cost"}]})
        _, report = self.report_for(
            ["--report", f"d={self.decoupling_report()}",
             "--catalogue", str(bad)])
        self.assertEqual(report["status"], "RED")

    def test_the_report_validates_against_its_schema(self):
        try:
            import jsonschema
        except ImportError:                       # pragma: no cover
            self.skipTest("jsonschema is not installed")
        schema = json.loads((TOOLS / "schemas" /
                             "topology_proposal_report.schema.json")
                            .read_text(encoding="utf-8"))
        _, report = self.report_for(
            ["--report", f"d={self.decoupling_report()}"])
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
