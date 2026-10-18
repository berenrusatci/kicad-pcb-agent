#!/usr/bin/env python3
"""Tests for scripts/production/bom_lifecycle.py.

WHY THIS TOOL EXISTS AT ALL is itself the first thing worth pinning: the
archive module it replaces was rejected as "network dependent, never run",
and the skill that needed it was left pointing at a tool that did not
exist. The rejection was wrong twice over - the substance is offline, and
the skill's own schema says the network call belongs to the main agent, not
to the tool. So the tests below assert BOTH halves: the gates work, and the
tool never invents supply data it was not given.

The schema-conformance test is not decoration. Written by hand, this port
was schema-INVALID in two places (an unfiltered distributor payload leaking
extra keys into ``assembly_gates``, and a null ``supply_checked_at`` the
contract had no room for). Both were found by validating against the real
schema file, not by reading it.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "production"))

import bom_lifecycle as mod  # noqa: E402
from common.contract import ContractError  # noqa: E402

SCHEMA_PATH = REPO / "skills" / "bom-lifecycle" / "references" / "lifecycle_report.schema.json"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
FRESH = "2026-08-07T00:00:00Z"


def supply_doc(parts, checked_at=FRESH, queried_from="main_agent"):
    return {"supply_checked_at": checked_at,
            "provider": {"name": "nexar", "queried_from": queried_from, "api_reachable": True},
            "parts": parts}


class ReadBomTests(unittest.TestCase):
    def test_a_string_refdes_becomes_a_list(self):
        """The schema requires ``refdes`` to be an array with minItems 1."""
        self.assertEqual([["U1"]], [l["refdes"] for l in mod.read_bom([{"refdes": "U1", "mpn": "X"}])])

    def test_a_lines_wrapper_is_accepted(self):
        bom = mod.read_bom({"lines": [{"refdes": ["R1"], "mpn": "X"}]})
        self.assertEqual(1, len(bom))

    def test_a_line_without_an_mpn_is_kept_not_dropped(self):
        """Dropping it would shrink ``eligible`` and quietly improve the
        coverage ratio - the same silent-success shape as scanned=0 PASS."""
        bom = mod.read_bom([{"refdes": "R1"}])
        self.assertIsNone(bom[0]["mpn"])

    def test_a_malformed_line_raises_instead_of_reporting_empty(self):
        for bad in ([{"mpn": "X"}], [{"refdes": [], "mpn": "X"}], [{"refdes": "U1", "mpn": 7}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    mod.read_bom(bad)


class LifecycleGateTests(unittest.TestCase):
    def line(self, part, critical=False, supply=None):
        bom = mod.read_bom([{"refdes": "U1", "mpn": "PART", "critical": critical}])
        doc = supply if supply is not None else supply_doc({"PART": part})
        report, _ = mod.build_report(bom, doc, now=NOW)
        return report["lines"][0]

    def test_nrnd_eol_and_obsolete_are_all_red(self):
        """MASTER_RULEBOOK Bölüm 0: no prototype exception. NRND is the one
        that gets waved through in practice, so it is asserted explicitly."""
        for state in ("NRND", "EOL", "Obsolete"):
            with self.subTest(state=state):
                line = self.line({"lifecycle": state, "sources": ["a", "b"],
                                  "stock_qty": 50000,
                                  "pcn": {"last_time_buy": "2027-01-01"}})
                self.assertEqual("RED", line["status"])

    def test_an_active_well_sourced_part_is_green(self):
        line = self.line({"lifecycle": "Active", "stock_qty": 500000,
                          "sources": ["digikey", "mouser"]})
        self.assertEqual("GREEN", line["status"])
        self.assertEqual(0.0, line["risk_score"])

    def test_no_supply_data_is_confirm_never_green(self):
        """FAULT INJECTION: the failure this whole tool is shaped around -
        an unqueried part reading as a checked one."""
        line = self.line({}, supply=None)
        self.assertEqual("CONFIRM", line["status"])
        self.assertIsNone(line["risk_score"], "unmeasured risk is null, not zero")

    def test_unknown_lifecycle_still_costs_risk(self):
        line = self.line({"lifecycle": "Unknown", "stock_qty": 500000,
                          "sources": ["a", "b"]})
        self.assertEqual("CONFIRM", line["status"])
        self.assertEqual(0.4, line["risk_score"])

    def test_an_unrecognised_lifecycle_string_degrades_to_unknown(self):
        line = self.line({"lifecycle": "probably fine", "sources": ["a", "b"],
                          "stock_qty": 500000})
        self.assertEqual("Unknown", line["lifecycle"])

    def test_a_critical_part_needs_two_sources(self):
        line = self.line({"lifecycle": "Active", "stock_qty": 500000, "sources": ["only-one"]},
                         critical=True)
        self.assertEqual("RED", line["status"])


class RiskScoreTests(unittest.TestCase):
    def test_stacked_risks_are_clamped_but_not_forgotten(self):
        """The archive scorer let the weights exceed 1.0 on purpose; the
        schema caps the field at 1. The emitted number obeys the contract,
        the raw sum stays in the reasons."""
        score, reasons = mod.risk_score(
            {"critical": True},
            {"lifecycle": "EOL", "stock_qty": 5, "sources": ["one"], "lead_time_weeks": 52})
        self.assertEqual(1.0, score)
        self.assertTrue(any("ham skor" in r for r in reasons), reasons)
        self.assertTrue(any("1.8" in r or "1.6" in r for r in reasons), reasons)

    def test_no_data_scores_none_not_zero(self):
        score, reasons = mod.risk_score({}, {})
        self.assertIsNone(score)
        self.assertTrue(reasons)

    def test_each_factor_contributes_its_documented_weight(self):
        base, _ = mod.risk_score({}, {"lifecycle": "Active", "stock_qty": 999999,
                                      "sources": ["a", "b"]})
        low_stock, _ = mod.risk_score({}, {"lifecycle": "Active", "stock_qty": 5,
                                           "sources": ["a", "b"]})
        single, _ = mod.risk_score({}, {"lifecycle": "Active", "stock_qty": 999999,
                                        "sources": ["a"]})
        self.assertEqual(0.0, base)
        self.assertEqual(0.3, low_stock)
        self.assertEqual(0.3, single)


class AlternateVerdictTests(unittest.TestCase):
    FULL_PROVENANCE = {field: "x" for field in mod.PROVENANCE_FIELDS}
    SAME = {"pin_function_and_order": "same", "land_pattern": "same",
            "abs_max_and_operating": "same_or_better", "power_dissipation": "same_or_better",
            "thermal_path": "same_or_better"}

    def test_a_fully_evidenced_drop_in_is_green(self):
        alt = mod.judge_alternate({"mpn": "X", "provenance": self.FULL_PROVENANCE,
                                   "compatibility": self.SAME})
        self.assertEqual("GREEN", alt["verdict"])
        self.assertFalse(alt["opens_footprint_feedback"])

    def test_a_missing_provenance_field_caps_at_confirm(self):
        """Schema $comment: any null here caps the alternate at CONFIRM.
        Never PASS an unsourced or unrevisioned candidate to production."""
        provenance = dict(self.FULL_PROVENANCE, datasheet_revision=None)
        alt = mod.judge_alternate({"mpn": "X", "provenance": provenance,
                                   "compatibility": self.SAME})
        self.assertEqual("CONFIRM", alt["verdict"])
        self.assertIn("datasheet_revision", alt["verdict_reason"])

    def test_a_different_land_pattern_is_red_and_opens_feedback(self):
        alt = mod.judge_alternate({"mpn": "X", "provenance": self.FULL_PROVENANCE,
                                   "compatibility": dict(self.SAME, land_pattern="different")})
        self.assertEqual("RED", alt["verdict"])
        self.assertTrue(alt["opens_footprint_feedback"])

    def test_silence_about_compatibility_is_not_compatibility(self):
        """An empty candidate must not inherit "same" by omission."""
        alt = mod.judge_alternate({"mpn": "X"})
        self.assertEqual("CONFIRM", alt["verdict"])
        self.assertEqual("unknown", alt["compatibility"]["pin_function_and_order"])

    def test_high_risk_with_no_accepted_candidate_is_needs_human(self):
        bom = mod.read_bom([{"refdes": "U1", "mpn": "PART"}])
        report, _ = mod.build_report(bom, supply_doc({"PART": {
            "lifecycle": "NRND", "stock_qty": 10, "sources": ["one"],
            "alternates": [{"mpn": "MAYBE"}]}}), now=NOW)
        advice = mod.alternate_advice(dict(report["lines"][0], _risk_reasons=[]))
        self.assertIn("NEEDS_HUMAN", advice)


class PcnGateTests(unittest.TestCase):
    def test_a_published_pdn_is_red(self):
        self.assertTrue(mod.pcn_violations({"pdn_id_or_none": "PDN-42"}, "Active"))

    def test_eol_without_buy_dates_is_red(self):
        self.assertTrue(mod.pcn_violations(
            {"pdn_id_or_none": None, "last_time_buy": None, "last_ship": None}, "EOL"))

    def test_eol_with_a_known_buy_window_is_not_a_pcn_violation(self):
        """The line is still RED for being EOL - but for the RIGHT reason.
        A known last-time-buy is plannable; an unknown one is not."""
        self.assertEqual([], mod.pcn_violations(
            {"pdn_id_or_none": None, "last_time_buy": "2027-01-01", "last_ship": None}, "EOL"))


class StalenessTests(unittest.TestCase):
    def test_the_quote_threshold_is_tighter_than_review(self):
        eight_days_old = "2026-07-30T12:00:00Z"
        self.assertIsNotNone(mod.staleness(eight_days_old, "quote", NOW))
        self.assertIsNone(mod.staleness(eight_days_old, "review", NOW))

    def test_a_missing_timestamp_is_stale_not_fresh(self):
        self.assertIsNotNone(mod.staleness(None, "review", NOW))

    def test_an_unparsable_timestamp_is_stale_not_fresh(self):
        self.assertIsNotNone(mod.staleness("last tuesday", "review", NOW))


class CoverageAndStatusTests(unittest.TestCase):
    def bom(self):
        return mod.read_bom([
            {"refdes": "U1", "mpn": "EOLPART", "critical": True},
            {"refdes": ["C1", "C2"], "mpn": "GOODPART"},
            {"refdes": "R1", "mpn": None},
        ])

    def test_an_mpn_less_line_is_excluded_not_scanned(self):
        report, _ = mod.build_report(self.bom(), supply_doc({
            "EOLPART": {"lifecycle": "Active", "stock_qty": 99999, "sources": ["a", "b"]},
            "GOODPART": {"lifecycle": "Active", "stock_qty": 99999, "sources": ["a", "b"]},
        }), now=NOW)
        self.assertEqual({"eligible": 3, "scanned": 2, "excluded": 1, "violations": 0},
                         report["coverage"])
        self.assertEqual("NO_COVERAGE", report["lines"][2]["status"])

    def test_nothing_scannable_is_no_coverage_not_green(self):
        bom = mod.read_bom([{"refdes": "R1"}, {"refdes": "R2"}])
        report, _ = mod.build_report(bom, supply_doc({}), now=NOW)
        self.assertEqual("NO_COVERAGE", report["status"])

    def test_one_red_line_makes_the_report_red(self):
        report, _ = mod.build_report(self.bom(), supply_doc({
            "EOLPART": {"lifecycle": "EOL", "stock_qty": 5, "sources": ["a"]},
            "GOODPART": {"lifecycle": "Active", "stock_qty": 99999, "sources": ["a", "b"]},
        }), now=NOW)
        self.assertEqual("RED", report["status"])

    def test_fresh_and_clean_is_green(self):
        bom = mod.read_bom([{"refdes": "C1", "mpn": "GOODPART"}])
        report, extra = mod.build_report(bom, supply_doc({
            "GOODPART": {"lifecycle": "Active", "stock_qty": 99999, "sources": ["a", "b"]},
        }), now=NOW)
        self.assertEqual("GREEN", report["status"])
        self.assertIsNone(extra["stale_reason"])

    def test_stale_data_downgrades_a_clean_report(self):
        bom = mod.read_bom([{"refdes": "C1", "mpn": "GOODPART"}])
        report, extra = mod.build_report(bom, supply_doc({
            "GOODPART": {"lifecycle": "Active", "stock_qty": 99999, "sources": ["a", "b"]},
        }, checked_at="2026-01-01T00:00:00Z"), now=NOW)
        self.assertEqual("STALE", report["status"])
        self.assertIn("günlük", extra["stale_reason"])

    def test_an_unknown_provider_origin_is_rejected(self):
        with self.assertRaises(ContractError):
            mod.build_report([], supply_doc({}, queried_from="somewhere"), now=NOW)


class SchemaConformanceTests(unittest.TestCase):
    """Validated against the REAL schema file the skill ships, in every
    state the tool can end in. Two genuine defects were found this way."""

    def setUp(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:  # pragma: no cover - environment without jsonschema
            self.skipTest("jsonschema not installed")
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def validate(self, report):
        import jsonschema
        jsonschema.validate(report, self.schema)

    def test_a_full_report_conforms(self):
        bom = mod.read_bom([{"refdes": "U1", "mpn": "PART", "critical": True}])
        report, _ = mod.build_report(bom, supply_doc({"PART": {
            "lifecycle": "EOL", "stock_qty": 12, "sources": ["mouser"],
            "assembly_gates": {"msl_level": "3", "moq": 4000, "packaging": "reel"},
            "pcn": {"pdn_id_or_none": "PDN-1", "last_time_buy": "2026-12-01"},
            "alternates": [{"mpn": "ALT", "compatibility": {"land_pattern": "different"}}],
        }}), now=NOW)
        self.validate(report)

    def test_a_report_with_no_supply_data_conforms(self):
        """``provider.queried_from`` already had an "unavailable" state; the
        timestamp had no null option to match it. The schema was internally
        inconsistent, and inventing a timestamp to satisfy it would have
        recorded a check that never happened."""
        bom = mod.read_bom([{"refdes": "U1", "mpn": "PART"}])
        report, _ = mod.build_report(bom, None)
        self.assertIsNone(report["supply_checked_at"])
        self.validate(report)

    def test_extra_distributor_keys_do_not_leak_into_the_artifact(self):
        """A real API payload carries far more keys than this contract, and
        every block is ``additionalProperties: false``."""
        bom = mod.read_bom([{"refdes": "U1", "mpn": "PART"}])
        report, _ = mod.build_report(bom, supply_doc({"PART": {
            "lifecycle": "Active", "stock_qty": 99999, "sources": ["a", "b"],
            "assembly_gates": {"msl_level": "3", "median_price_1000": 0.42,
                               "octopart_uid": "abc"},
        }}), now=NOW)
        self.assertEqual({"msl_level": "3"}, report["lines"][0]["assembly_gates"])
        self.validate(report)


class CliTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())

    def test_a_red_report_exits_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bom_path = Path(tmp) / "bom.json"
            bom_path.write_text(json.dumps([{"refdes": "U1", "mpn": "PART"}]), encoding="utf-8")
            supply_path = Path(tmp) / "supply.json"
            supply_path.write_text(json.dumps(supply_doc(
                {"PART": {"lifecycle": "EOL", "stock_qty": 1, "sources": ["a"]}})),
                encoding="utf-8")
            out = Path(tmp) / "report.json"
            code = mod.main([str(bom_path), "--supply", str(supply_path),
                             "--now", "2026-08-07T12:00:00Z", "--json", str(out)])
            self.assertEqual(1, code)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("RED", written["status"])


if __name__ == "__main__":
    unittest.main()
