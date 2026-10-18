#!/usr/bin/env python3
"""Tests for the connector-current cross-check (scripts/connector_current.py).

The tool exists to stop one class of mistake: reading a connector's per-contact
CAPACITY as though it were the rail's DEMAND, or manufacturing a capacity that
was never read off a curve. So the tests are mostly refusals, and each one is a
case where a permissive version of the tool returns a confident PASS:

    no derating curve      -> the room-temperature rating is sitting right
                              there, and using it is optimistic, not safe
    no worst-case demand   -> nothing to compare, and 'no violations found'
                              reads exactly like 'nothing was wrong'
    incomplete map         -> the rails the map omits were never looked at
    N contacts             -> N x I_rated is not an upper bound

The happy path is tested too, because a gate that can only say no is not a
gate. Every number used here comes from tools/fixtures/connector_current/ and
is invented; the repo holds no real connector derating curve and no real rail
worst case, which is why the row this serves is still C->A.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import FIXTURES, SCHEMAS, SCRIPTS  # noqa: E402

import connector_current as mod  # noqa: E402

SCRIPT = SCRIPTS / "connector_current.py"
FIXTURE_DIR = FIXTURES / "connector_current"
SPEC_FIXTURE = FIXTURE_DIR / "connector_spec.fixture.json"
TREE_FIXTURE = FIXTURE_DIR / "rail_tree.fixture.json"
MAP_FIXTURE = FIXTURE_DIR / "connector_rail_map.fixture.json"

# The two links the fixtures declare.
GROUP_RAIL = "FIXTURE_5V_IN"      # 2 contacts, sourced group rating
SINGLE_RAIL = "FIXTURE_3V3"       # 1 contact, per-contact derated figure


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def finding(report: dict, check: str) -> dict:
    matches = [f for f in report["findings"] if f["check"] == check]
    if not matches:
        raise AssertionError(f"no finding named {check!r}")
    return matches[0]


def link_for(report: dict, rail: str) -> dict:
    matches = [entry for entry in report["links"] if entry["rail"] == rail]
    if not matches:
        raise AssertionError(f"no link for rail {rail!r}")
    return matches[0]


class CrossCheck:
    """A mutable copy of the three fixtures, written to a temp directory.

    Every negative case is the happy path with ONE thing taken away, so a
    failure names the missing input rather than a different fixture.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.spec = load(SPEC_FIXTURE)
        self.tree = load(TREE_FIXTURE)
        self.map = load(MAP_FIXTURE)

    # -- accessors into the fixture documents -------------------------------
    def connector(self, refdes: str) -> dict:
        for entry in self.spec["connectors"]:
            if entry["refdes"] == refdes:
                return entry
        raise AssertionError(f"fixture has no connector {refdes!r}")

    def link(self, rail: str) -> dict:
        for entry in self.map["links"]:
            if entry["rail"] == rail:
                return entry
        raise AssertionError(f"fixture map has no link for {rail!r}")

    def current(self, rail: str) -> dict:
        return self.connector(self.link(rail)["refdes"])["current"]

    # -- running ------------------------------------------------------------
    def paths(self) -> dict:
        written = {}
        for name, doc in (("connector_spec", self.spec),
                          ("rail_tree", self.tree), ("map", self.map)):
            path = self.dir / f"{name}.json"
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            written[name] = path
        return written

    def report(self, min_margin_a=None) -> dict:
        paths = self.paths()
        return mod.build_report(paths["connector_spec"], paths["rail_tree"],
                                paths["map"], min_margin_a)

    def run_cli(self, *extra) -> subprocess.CompletedProcess:
        paths = self.paths()
        return subprocess.run(
            [sys.executable, str(SCRIPT),
             "--connector-spec", str(paths["connector_spec"]),
             "--rail-tree", str(paths["rail_tree"]),
             "--map", str(paths["map"]), *extra],
            capture_output=True, text=True, encoding="utf-8", check=False)


class CaseBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.case = CrossCheck(Path(self._tmp.name))


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
class HappyPathTests(CaseBase):
    def test_a_fully_sourced_cross_check_is_green(self):
        report = self.case.report()
        self.assertEqual("GREEN", report["status"], report["reason_codes"])
        self.assertEqual([], report["reason_codes"])
        for entry in report["findings"]:
            with self.subTest(check=entry["check"]):
                self.assertEqual("PASS", entry["status"], entry["detail"])

    def test_every_link_carries_a_margin_and_both_provenances(self):
        report = self.case.report()
        for entry in report["links"]:
            with self.subTest(rail=entry["rail"]):
                self.assertEqual("PASS", entry["status"])
                self.assertIsNotNone(entry["capacity_a"])
                self.assertIsNotNone(entry["demand_a"])
                self.assertGreater(entry["margin_a"], 0)
                capacity = entry["provenance"]["capacity"]
                demand = entry["provenance"]["demand"]
                # WHICH document each number came from, on both sides.
                self.assertTrue(capacity["datasheet_locator"])
                self.assertEqual("datasheet_curve", capacity["derating_basis"])
                self.assertIsNotNone(capacity["ambient_c"])
                self.assertTrue(demand["source"]["document"])
                self.assertTrue(demand["source"]["revision"])
                self.assertTrue(demand["source"]["locator"])

    def test_demand_is_read_from_the_rail_tree_not_from_the_spec(self):
        """The independence claim, made checkable: move the rail tree's number
        and the report follows it, even though the spec still says the old
        one (which is then caught as a contradiction)."""
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = 0.25
        report = self.case.report()
        entry = link_for(report, SINGLE_RAIL)
        self.assertEqual(0.25, entry["demand_a"])
        self.assertEqual(0.22, entry["spec_declared_demand_a"])
        self.assertIn("rail_tree.rails", entry["provenance"]["demand"]["demand_from"])

    def test_the_group_capacity_is_not_n_times_the_per_contact_figure(self):
        """The fixture is built so a tool that multiplied would be visible:
        2 contacts x 2.1 A = 4.2 A, and the sourced group rating is 3.4 A."""
        report = self.case.report()
        entry = link_for(report, GROUP_RAIL)
        self.assertEqual(3.4, entry["capacity_a"])
        per_contact = entry["provenance"]["capacity"]["derated_a_per_contact"]
        self.assertNotEqual(per_contact * entry["contact_count"],
                            entry["capacity_a"])

    def test_the_rail_coverage_triple_is_arithmetically_valid(self):
        from common.findings import coverage_rule_violations
        report = self.case.report()
        self.assertEqual([], coverage_rule_violations(report["rail_coverage"]))
        self.assertEqual(3, report["rail_coverage"]["eligible"])
        self.assertEqual(2, report["rail_coverage"]["scanned"])
        self.assertEqual(1, report["rail_coverage"]["excluded"])

    def test_both_input_artifacts_are_hash_bound(self):
        report = self.case.report()
        for name in ("connector_spec", "rail_tree", "map"):
            with self.subTest(artifact=name):
                digest = report["inputs"][name]["sha256"]
                self.assertEqual(64, len(digest))

    def test_the_report_never_claims_signoff(self):
        report = self.case.report()
        self.assertFalse(report["signoff"])
        self.assertTrue(report["limits"])


# --------------------------------------------------------------------------- #
# NO_COVERAGE: the derating basis
# --------------------------------------------------------------------------- #
class DeratingBasisTests(CaseBase):
    def test_missing_derating_basis_is_no_coverage(self):
        self.case.current(SINGLE_RAIL)["derating_basis"] = None
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        entry = link_for(report, SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.DERATING_BASIS_ABSENT, entry["reason_codes"])
        self.assertIsNone(entry["capacity_a"])
        self.assertIsNone(entry["margin_a"])
        self.assertEqual("NO_COVERAGE",
                         finding(report, "connector_capacity_is_derated_and_sourced")["status"])

    def test_unknown_derating_basis_is_no_coverage_too(self):
        self.case.current(SINGLE_RAIL)["derating_basis"] = "unknown"
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.DERATING_BASIS_ABSENT, entry["reason_codes"])

    def test_a_local_adjacency_screen_is_not_a_datasheet_curve(self):
        """connector_spec.schema.json marks adjacent_contact_screen
        LOCAL_POLICY. A local screen can rule a connector out; it cannot sign
        one off, so it does not close this row."""
        self.case.current(SINGLE_RAIL)["derating_basis"] = "adjacent_contact_screen"
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.DERATING_BASIS_IS_LOCAL_SCREEN, entry["reason_codes"])
        self.assertIsNone(entry["capacity_a"])

    def test_a_derating_with_no_ambient_is_no_coverage(self):
        """A curve is a function of temperature; 'derated' with no ambient
        does not say derated to what."""
        self.case.current(SINGLE_RAIL)["ambient_c"] = None
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.AMBIENT_ABSENT, entry["reason_codes"])

    def test_capacity_without_a_datasheet_locator_is_unsourced(self):
        self.case.connector("J2")["datasheet_locator"] = None
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.CAPACITY_UNSOURCED, entry["reason_codes"])

    def test_a_derated_figure_above_its_own_rating_fails(self):
        """Not a missing input: a stated one that cannot be true. Derating
        removes capacity."""
        self.case.current(SINGLE_RAIL)["derated_a_per_contact"] = 0.9
        report = self.case.report()
        self.assertEqual("RED", report["status"])
        self.assertIn(mod.DERATED_EXCEEDS_RATED,
                      link_for(report, SINGLE_RAIL)["reason_codes"])


# --------------------------------------------------------------------------- #
# THE substitution this tool exists to refuse
# --------------------------------------------------------------------------- #
class RoomTemperatureRatingTests(CaseBase):
    """konnektor SKILL.md §1 and §3b: with no derating curve the room
    temperature rating is NOT usable. It is measured on a single contact, on a
    bare test board, at 20-25 C with a 30 C rise. Substituting it is the
    optimistic move, not the conservative one, and optimistic is the direction
    that ships."""

    def setUp(self):
        super().setUp()
        current = self.case.current(SINGLE_RAIL)
        self.rated = current["rated_a_per_contact"]
        current["derated_a_per_contact"] = None
        current["derating_basis"] = None

    def test_the_rating_is_not_used_as_capacity(self):
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIsNone(entry["capacity_a"])
        self.assertIsNone(entry["margin_a"])
        self.assertNotEqual(self.rated, entry["capacity_a"])

    def test_the_refusal_is_stated_rather_than_left_implicit(self):
        """A reader must see the substitution was available and declined, not
        wonder why a number sitting right there was ignored."""
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertIn(mod.ROOM_TEMPERATURE_NOT_SUBSTITUTED, entry["reason_codes"])
        self.assertIn(mod.DERATED_CAPACITY_ABSENT, entry["reason_codes"])
        # ...and the rating is still echoed, so the reader can see WHICH
        # number was refused.
        self.assertEqual(self.rated,
                         entry["provenance"]["capacity"]["rated_a_per_contact"])

    def test_the_rating_appears_nowhere_as_a_capacity_or_a_margin(self):
        """Fault injection against a future edit: no field anywhere in the
        report may carry the room-temperature number as an ANSWER."""
        report = self.case.report()
        for entry in report["links"]:
            for key in ("capacity_a", "demand_a", "margin_a"):
                with self.subTest(rail=entry["rail"], key=key):
                    self.assertNotEqual(self.rated, entry[key])
        self.assertNotEqual(
            self.rated,
            link_for(report, SINGLE_RAIL)["provenance"]["capacity"]["capacity_a"])

    def test_the_run_is_never_green(self):
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        capacity = finding(report, "connector_capacity_is_derated_and_sourced")
        self.assertEqual("NO_COVERAGE", capacity["status"])
        self.assertIn("room-temperature", capacity["detail"])


# --------------------------------------------------------------------------- #
# NO_COVERAGE: the demand side
# --------------------------------------------------------------------------- #
class DemandTests(CaseBase):
    def test_absent_worst_case_demand_is_no_coverage(self):
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = None
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        entry = link_for(report, SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.DEMAND_ABSENT, entry["reason_codes"])
        self.assertIsNone(entry["demand_a"])
        self.assertEqual("NO_COVERAGE",
                         finding(report, "rail_demand_is_present_and_sourced")["status"])

    def test_an_absent_demand_does_not_borrow_the_specs_own_number(self):
        """connector_spec still declares 0.22 A for this rail. Reading it
        would be a document checked against itself."""
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = None
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual(0.22, entry["spec_declared_demand_a"])
        self.assertIsNone(entry["demand_a"])
        self.assertIsNone(entry["margin_a"])

    def test_unsourced_demand_is_no_coverage(self):
        """rail-tree/v1 pins seven fields and forbids others, so there is
        nowhere in it to record provenance; the map must carry it, and a map
        that does not is not a source."""
        del self.case.link(SINGLE_RAIL)["demand"]["source"]
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.DEMAND_UNSOURCED, entry["reason_codes"])
        self.assertIsNone(entry["demand_a"])

    def test_a_partial_source_is_not_a_source(self):
        del self.case.link(SINGLE_RAIL)["demand"]["source"]["revision"]
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertIn(mod.DEMAND_UNSOURCED, entry["reason_codes"])
        self.assertIn("revision",
                      entry["provenance"]["demand"]["source_missing_fields"])

    def test_a_rail_the_tree_does_not_contain_is_not_silently_skipped(self):
        self.case.link(SINGLE_RAIL)["rail"] = "FIXTURE_NOT_A_RAIL"
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        entry = link_for(report, "FIXTURE_NOT_A_RAIL")
        self.assertIn(mod.RAIL_NOT_IN_TREE, entry["reason_codes"])


# --------------------------------------------------------------------------- #
# NO_COVERAGE: the mapping
# --------------------------------------------------------------------------- #
class MappingCompletenessTests(CaseBase):
    def test_a_rail_that_is_neither_mapped_nor_excluded_is_no_coverage(self):
        self.case.map["excluded_rails"] = []
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        mapping = finding(report, "mapping_accounts_for_every_rail")
        self.assertEqual("NO_COVERAGE", mapping["status"])
        self.assertEqual(["FIXTURE_1V8"],
                         [v["rail"] for v in mapping["violations"]])

    def test_the_other_links_still_report_their_own_verdicts(self):
        """An incomplete map must not blank the rails it DOES cover - the
        report has to say which part was judged."""
        self.case.map["excluded_rails"] = []
        report = self.case.report()
        self.assertEqual("PASS", link_for(report, SINGLE_RAIL)["status"])
        self.assertEqual("NO_COVERAGE", report["status"])

    def test_an_exclusion_without_a_reason_does_not_load(self):
        self.case.map["excluded_rails"][0]["reason"] = ""
        with self.assertRaises(mod.ConnectorCurrentError) as caught:
            self.case.report()
        self.assertIn("reason", str(caught.exception))

    def test_a_rail_both_mapped_and_excluded_is_a_broken_map(self):
        self.case.map["excluded_rails"].append(
            {"rail": SINGLE_RAIL, "reason": "FIXTURE: contradicts the link"})
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        issues = {v["issue"] for v in
                  finding(report, "mapping_accounts_for_every_rail")["violations"]}
        self.assertIn(mod.MAPPING_INCOMPLETE, issues)

    def test_a_power_connector_no_link_mentions_is_no_coverage(self):
        self.case.map["links"] = [self.case.link(SINGLE_RAIL)]
        self.case.map["excluded_rails"].append(
            {"rail": GROUP_RAIL, "reason": "FIXTURE: deliberately unmapped"})
        report = self.case.report()
        connectors = finding(report,
                             "mapping_accounts_for_every_power_connector")
        self.assertEqual("NO_COVERAGE", connectors["status"])
        self.assertEqual([mod.POWER_CONNECTOR_UNMAPPED],
                         [v["issue"] for v in connectors["violations"]])

    def test_a_connector_whose_role_is_unknown_is_not_skipped(self):
        """Role comes from interface_contract.json. Unknown means nobody can
        say whether it carries a rail, which is not the same as no."""
        self.case.connector("J2")["role"] = "unknown"
        self.case.map["links"] = [self.case.link(GROUP_RAIL)]
        self.case.map["excluded_rails"].append(
            {"rail": SINGLE_RAIL, "reason": "FIXTURE: link removed for this case"})
        report = self.case.report()
        issues = {v["issue"] for v in report["connector_coverage_violations"]}
        self.assertIn(mod.CONNECTOR_ROLE_UNKNOWN, issues)

    def test_an_empty_map_is_no_coverage_not_a_clean_run(self):
        self.case.map["links"] = []
        self.case.map["excluded_rails"] = [
            {"rail": name, "reason": "FIXTURE: nothing mapped in this case"}
            for name in self.case.tree["rails"]]
        report = self.case.report()
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual("NO_COVERAGE",
                         finding(report, "demand_within_connector_capacity")["status"])


# --------------------------------------------------------------------------- #
# FAIL: demand exceeds capacity
# --------------------------------------------------------------------------- #
class DemandExceedsCapacityTests(CaseBase):
    def test_a_rail_drawing_more_than_the_contact_carries_fails(self):
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = 0.45
        self.case.connector("J2")["current"]["rail_cross_check"][
            "rail_worst_case_a"] = 0.45
        report = self.case.report()
        self.assertEqual("RED", report["status"])
        entry = link_for(report, SINGLE_RAIL)
        self.assertEqual("FAIL", entry["status"])
        self.assertIn(mod.DEMAND_EXCEEDS_CAPACITY, entry["reason_codes"])
        self.assertLess(entry["margin_a"], 0)
        self.assertEqual("FAIL",
                         finding(report, "demand_within_connector_capacity")["status"])

    def test_the_failure_names_both_documents(self):
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = 0.45
        self.case.connector("J2")["current"]["rail_cross_check"][
            "rail_worst_case_a"] = 0.45
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual(0.3, entry["capacity_a"])
        self.assertEqual(0.45, entry["demand_a"])
        self.assertTrue(entry["provenance"]["capacity"]["datasheet_locator"])
        self.assertTrue(entry["provenance"]["demand"]["source"]["document"])

    def test_a_thin_margin_is_a_separate_finding_and_only_when_asked_for(self):
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = 0.29
        self.case.connector("J2")["current"]["rail_cross_check"][
            "rail_worst_case_a"] = 0.29
        passing = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("PASS", passing["status"])
        thin = link_for(self.case.report(min_margin_a=0.1), SINGLE_RAIL)
        self.assertEqual("FAIL", thin["status"])
        self.assertIn(mod.MARGIN_THIN, thin["reason_codes"])

    def test_no_margin_is_assumed_when_none_is_given(self):
        report = self.case.report()
        self.assertIsNone(report["params"]["min_margin_a"])
        self.assertIn("SIGN",
                      finding(report, "demand_within_connector_capacity")["detail"])


# --------------------------------------------------------------------------- #
# The spec's own pre-written cross-check is a claim, not a source
# --------------------------------------------------------------------------- #
class SpecDeclaredCrossCheckTests(CaseBase):
    def test_a_spec_block_that_disagrees_with_the_rail_tree_fails(self):
        self.case.current(SINGLE_RAIL)["rail_cross_check"][
            "rail_worst_case_a"] = 0.05
        report = self.case.report()
        self.assertEqual("RED", report["status"])
        entry = link_for(report, SINGLE_RAIL)
        self.assertIn(mod.SPEC_BLOCK_CONTRADICTS, entry["reason_codes"])
        self.assertEqual(0.22, entry["demand_a"])       # the rail tree wins
        self.assertEqual(0.05, entry["spec_declared_demand_a"])

    def test_a_spec_that_declared_nothing_is_no_coverage_for_that_check(self):
        for entry in self.case.spec["connectors"]:
            entry["current"]["rail_cross_check"] = None
        report = self.case.report()
        self.assertEqual(
            "NO_COVERAGE",
            finding(report, "connector_spec_declared_cross_check_agrees")["status"])
        # ...and the comparison itself still ran on its own two sources.
        self.assertEqual(
            "PASS", finding(report, "demand_within_connector_capacity")["status"])
        self.assertEqual("NO_COVERAGE", report["status"])

    def test_the_report_states_where_each_side_came_from(self):
        report = self.case.report()
        self.assertEqual("rail_tree.rails[].i_max_a",
                         report["independence"]["demand_from"])
        self.assertEqual("connector_spec.connectors[].current",
                         report["independence"]["capacity_from"])


# --------------------------------------------------------------------------- #
# Grouping: N x I_rated does not load
# --------------------------------------------------------------------------- #
class GroupingTests(CaseBase):
    def test_n_times_rated_is_refused_by_name(self):
        for policy in mod.REFUSED_POLICIES:
            with self.subTest(policy=policy):
                case = CrossCheck(Path(self._tmp.name))
                case.link(GROUP_RAIL)["capacity"]["policy"] = policy
                with self.assertRaises(mod.ConnectorCurrentError) as caught:
                    case.report()
                message = str(caught.exception)
                self.assertIn("not offered", message)
                self.assertIn("sheds current", message)

    def test_an_unlabelled_policy_does_not_load(self):
        self.case.link(GROUP_RAIL)["capacity"]["policy"] = "whatever"
        with self.assertRaises(mod.ConnectorCurrentError) as caught:
            self.case.report()
        self.assertIn("no default", str(caught.exception))

    def test_a_group_rating_without_a_source_is_no_coverage(self):
        del self.case.link(GROUP_RAIL)["capacity"]["source"]
        entry = link_for(self.case.report(), GROUP_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.GROUP_RATING_UNSOURCED, entry["reason_codes"])
        self.assertIsNone(entry["capacity_a"])

    def test_a_group_policy_without_a_figure_is_no_coverage(self):
        self.case.link(GROUP_RAIL)["capacity"]["group_rated_a"] = None
        entry = link_for(self.case.report(), GROUP_RAIL)
        self.assertIn(mod.GROUP_RATING_ABSENT, entry["reason_codes"])
        self.assertIsNone(entry["capacity_a"])

    def test_two_contacts_under_a_single_contact_policy_contradict(self):
        link = self.case.link(SINGLE_RAIL)
        link["contact_group"]["contacts"] = ["11", "13"]
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.SINGLE_CONTACT_CONTRADICTS_GROUP, entry["reason_codes"])
        self.assertIsNone(entry["capacity_a"])

    def test_a_multi_contact_group_needs_a_parallel_pin_assumption(self):
        self.case.current(GROUP_RAIL)["parallel_pin_assumption"] = None
        entry = link_for(self.case.report(), GROUP_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.PARALLEL_ASSUMPTION_UNDECLARED, entry["reason_codes"])

    def test_an_empty_contact_list_does_not_load(self):
        self.case.link(GROUP_RAIL)["contact_group"]["contacts"] = []
        with self.assertRaises(mod.ConnectorCurrentError) as caught:
            self.case.report()
        self.assertIn("not defaulted to 1", str(caught.exception))

    def test_a_duplicated_contact_does_not_inflate_the_group(self):
        self.case.link(GROUP_RAIL)["contact_group"]["contacts"] = ["1", "1"]
        with self.assertRaises(mod.ConnectorCurrentError):
            self.case.report()


# --------------------------------------------------------------------------- #
# Input handling
# --------------------------------------------------------------------------- #
class InputTests(CaseBase):
    def test_a_schema_version_this_tool_does_not_read_is_refused(self):
        self.case.tree["schema_version"] = "rail-tree/v2"
        with self.assertRaises(mod.ConnectorCurrentError) as caught:
            self.case.report()
        self.assertIn("schema_version", str(caught.exception))

    def test_a_refdes_the_spec_does_not_contain_is_no_coverage(self):
        self.case.link(SINGLE_RAIL)["refdes"] = "J99"
        entry = link_for(self.case.report(), SINGLE_RAIL)
        self.assertEqual("NO_COVERAGE", entry["status"])
        self.assertIn(mod.REFDES_NOT_IN_SPEC, entry["reason_codes"])

    def test_a_duplicated_link_does_not_load(self):
        self.case.map["links"].append(dict(self.case.link(SINGLE_RAIL)))
        with self.assertRaises(mod.ConnectorCurrentError) as caught:
            self.case.report()
        self.assertIn("twice", str(caught.exception))


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #
class CliTests(CaseBase):
    def test_green_run_exits_zero_and_prints_json_on_stdout(self):
        proc = self.case.run_cli()
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual("GREEN", report["status"])
        self.assertEqual("connector-current-report/v1", report["schema_version"])
        self.assertIn("connector_current:", proc.stderr)

    def test_a_failing_run_exits_one(self):
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = 9.0
        self.case.connector("J2")["current"]["rail_cross_check"][
            "rail_worst_case_a"] = 9.0
        proc = self.case.run_cli()
        self.assertEqual(1, proc.returncode)
        self.assertEqual("RED", json.loads(proc.stdout)["status"])

    def test_no_coverage_also_exits_one(self):
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = None
        proc = self.case.run_cli()
        self.assertEqual(1, proc.returncode)
        self.assertEqual("NO_COVERAGE", json.loads(proc.stdout)["status"])

    def test_a_report_is_written_when_asked(self):
        target = Path(self._tmp.name) / "report.json"
        proc = self.case.run_cli("--json", str(target))
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("GREEN", load(target)["status"])

    def test_missing_inputs_exit_two_rather_than_running_on_one_document(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--connector-spec", str(SPEC_FIXTURE)],
            capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(2, proc.returncode)
        self.assertIn("--rail-tree", proc.stdout)

    def test_self_test_passes(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--self-test"],
            capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("SELF-TEST PASS", proc.stderr)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class SchemaTests(CaseBase):
    REPORT_SCHEMA = SCHEMAS / "connector_current_report.schema.json"
    MAP_SCHEMA = SCHEMAS / "connector_rail_map.schema.json"

    def _validator(self):
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - optional dependency
            self.skipTest("jsonschema is not installed")
        return jsonschema

    def test_the_report_validates(self):
        jsonschema = self._validator()
        jsonschema.validate(self.case.report(), load(self.REPORT_SCHEMA))

    def test_a_no_coverage_report_validates_too(self):
        jsonschema = self._validator()
        self.case.tree["rails"][SINGLE_RAIL]["i_max_a"] = None
        jsonschema.validate(self.case.report(), load(self.REPORT_SCHEMA))

    def test_the_map_fixture_validates(self):
        jsonschema = self._validator()
        jsonschema.validate(load(MAP_FIXTURE), load(self.MAP_SCHEMA))

    def test_the_fixtures_validate_against_the_contracts_they_claim(self):
        jsonschema = self._validator()
        from _paths import REPO
        spec_schema = (REPO / "skills" / "konnektor" / "references"
                       / "connector_spec.schema.json")
        jsonschema.validate(load(SPEC_FIXTURE), load(spec_schema))
        jsonschema.validate(load(TREE_FIXTURE),
                            load(SCHEMAS / "rail_tree.schema.json"))

    def test_the_required_keys_match_what_the_tool_emits(self):
        """Runs with or without jsonschema: a schema that has drifted from
        the emitter is worse than none, because it reads as a contract."""
        schema = load(self.REPORT_SCHEMA)
        report = self.case.report()
        self.assertEqual([], sorted(set(schema["required"]) - set(report)))
        self.assertEqual([], sorted(set(report) - set(schema["properties"])))
        link_schema = schema["$defs"]["link"]
        for entry in report["links"]:
            self.assertEqual([], sorted(set(link_schema["required"]) - set(entry)))
            self.assertEqual([], sorted(set(entry) - set(link_schema["properties"])))


# --------------------------------------------------------------------------- #
# Fixtures are labelled
# --------------------------------------------------------------------------- #
class FixtureLabellingTests(unittest.TestCase):
    """The repo holds NO real connector derating curve and NO real rail worst
    case. Anything here that looked like one would be read as one."""

    def test_every_fixture_says_it_is_a_fixture(self):
        for path in (SPEC_FIXTURE, TREE_FIXTURE, MAP_FIXTURE):
            with self.subTest(fixture=path.name):
                self.assertIn("FIXTURE", path.read_text(encoding="utf-8"))

    def test_no_rail_name_could_be_mistaken_for_a_project_rail(self):
        for name in load(TREE_FIXTURE)["rails"]:
            self.assertTrue(name.startswith("FIXTURE_"), name)

    def test_no_mpn_could_be_ordered(self):
        for entry in load(SPEC_FIXTURE)["connectors"]:
            self.assertIn("FIXTURE", entry["mpn"])


if __name__ == "__main__":
    unittest.main()
