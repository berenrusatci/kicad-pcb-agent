#!/usr/bin/env python3
"""Tests for the L3 physical-evidence ingest (scripts/lab/physical_measurement.py).

There is no bench in CI, and that blocks measuring THIS board - it does not
block testing the custody chain, because the tool never touches an instrument
in the first place. Everything it does is decide whether a record someone else
wrote is admissible evidence for a named rule on a named unit, and every case
below has an answer known before the tool runs.

The refusals carry the weight, because each of them is a specific way a number
can look like evidence without being evidence:

  * a rule whose limit never arrived must never acquire one here
  * a record from a different board is not weaker evidence, it is none
  * an uncalibrated instrument read; it did not measure
  * a margin inside uncertainty + guard_band did not close the boundary
  * a Touchstone with no reference plane, port map or de-embed method is not
    an impedance result, it is a picture of a fixture
  * a pre-compliance screen never becomes a certification

and the one positive obligation: GREEN must be reachable, and must break the
moment a measured value crosses the limit.
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

from common.findings import coverage_rule_violations  # noqa: E402
from common.hashing import sha256_bytes  # noqa: E402

from lab.physical_measurement import (  # noqa: E402
    CONFIRM, GREEN, INCONCLUSIVE, INVALID_METRIC, INVALID_SPARAM,
    NO_COVERAGE, RED, SCREEN_ONLY, STALE,
    build_feedback, build_receipt, build_report, judge, rule_limit, worst,
)

SCRIPT = SCRIPTS / "lab" / "physical_measurement.py"

BOARD_HASH = "a" * 64
OTHER_HASH = "c" * 64
RAW_SHA = "b" * 64

UNIT = {
    "board_revision": "B",
    "board_hash": BOARD_HASH,
    "serial": "SN-0001",
    "lot": "LOT-2026-31",
    "first_article": {"path": "fai.json", "sha256": None, "verdict": "GREEN"},
    "accepted": True,
}

AUTHORITY = {
    "owner_skill": "empedans-stackup",
    "document_id": "impedance_rules.json@rev3",
    "revision": "rev3",
    "locator": "build/impedance_rules.json",
    "accessed": "2026-08-01",
}


def rule(**overrides) -> dict:
    """A TDR impedance row: owner empedans-stackup, 81-99 ohm, sourced."""
    base = {
        "rule_id": "IMP-DIFF-90",
        "owner": "empedans-stackup",
        "required": True,
        "measurement_class": "tdr",
        "campaign_stage": "characterisation",
        "limit": {"units": "ohm", "min": 81.0, "max": 99.0,
                  "authority": dict(AUTHORITY)},
    }
    base.update(overrides)
    return base


def record(**overrides) -> dict:
    base = {
        "measurement_id": "M-001",
        "rule_id": "IMP-DIFF-90",
        "evidence_origin": "PHYSICAL_MEASUREMENT",
        "measurement_class": "tdr",
        "board_revision": "B",
        "board_hash": BOARD_HASH,
        "serial": "SN-0001",
        "lot": "LOT-2026-31",
        "instrument_make": "Tektronix",
        "instrument_model": "DSA8300",
        "instrument_serial": "TK-9",
        "calibration_id": "CAL-2026-07-14",
        "calibration_date": "2026-07-14",
        "method": "TDR on impedance coupon, 35 ps rise",
        "setup": "SMA end-launch, 50 ohm, 2 m phase-stable cable",
        "reference_plane": "coupon launch pad, after SOLT",
        "measurement_zone": "coupon centre 20-40 mm",
        "environment": "23 C, 45 %RH",
        "load_state": "unpowered coupon",
        "baseline_id": "BL-COUPON-1",
        "port_map": [{"port": 1, "net": "COUPON_P"},
                     {"port": 2, "net": "COUPON_N"}],
        "reference_impedance": 50.0,
        "fixture": "SMA end-launch, torque 0.56 Nm",
        "deembed_method": "SOLT to launch pad; fixture de-embedded",
        "value": 90.0,
        "units": "ohm",
        "uncertainty": 1.5,
        "guard_band": 1.0,
        "raw_data_sha256": RAW_SHA,
        "compliance_class": "bench",
        "assessor": "lab@example.org",
        "timestamp": "2026-08-10T09:00:00Z",
    }
    base.update(overrides)
    return base


def report_for(rules, records, unit=None, raw_root=None) -> dict:
    return build_report(rules, records, unit or UNIT, [], raw_root=raw_root)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class CoverageArithmeticEverywhere(unittest.TestCase):
    """`scanned + excluded == eligible` in EVERY report the tool can emit.

    Not one scenario: the whole matrix of them. A gate that gets the counts
    right on the happy path and wrong on a refusal has the arithmetic exactly
    where it does not matter.
    """

    def _scenarios(self):
        no_limit = rule()
        no_limit.pop("limit")
        return {
            "green": ([rule()], [record()]),
            "red": ([rule()], [record(value=140.0)]),
            "no_records": ([rule()], []),
            "no_limit": ([no_limit], [record()]),
            "stale": ([rule()], [record(board_hash=OTHER_HASH)]),
            "inconclusive": ([rule()], [record(value=98.5, uncertainty=1.0,
                                               guard_band=1.0)]),
            "empty_universe": ([], []),
            "two_rows": ([rule(), rule(rule_id="THERM-1", owner="guc-isil",
                                       measurement_class="thermal")],
                         [record()]),
        }

    def test_every_report_satisfies_the_coverage_rule(self):
        for name, (rules, records) in self._scenarios().items():
            with self.subTest(scenario=name):
                report = report_for(rules, records)
                self.assertEqual([], coverage_rule_violations(
                    report["coverage"]), report["coverage"])
                coverage = report["coverage"]
                self.assertEqual(coverage["eligible"],
                                 coverage["scanned"] + coverage["excluded"])
                self.assertLessEqual(coverage["violations"],
                                     coverage["scanned"])

    def test_every_exclusion_carries_a_reason(self):
        for name, (rules, records) in self._scenarios().items():
            with self.subTest(scenario=name):
                report = report_for(rules, records)
                self.assertEqual(report["coverage"]["excluded"],
                                 len(report["exclusions"]))
                for item in report["exclusions"]:
                    self.assertTrue(item["reason"], item)

    def test_scanned_zero_is_never_green(self):
        for name, (rules, records) in self._scenarios().items():
            with self.subTest(scenario=name):
                report = report_for(rules, records)
                if report["coverage"]["scanned"] == 0:
                    self.assertNotEqual(GREEN, report["status"])
                    self.assertNotEqual(GREEN, report["gate"]["verdict"])


class InsideTheLimit(unittest.TestCase):
    def test_a_valid_measurement_inside_the_limit_is_green(self):
        report = report_for([rule()], [record()])
        self.assertEqual(GREEN, report["status"])
        self.assertEqual(GREEN, report["gate"]["verdict"])
        self.assertEqual({"eligible": 1, "scanned": 1, "excluded": 0,
                          "violations": 0}, report["coverage"])

    def test_green_still_carries_signoff_false(self):
        """Evidence sufficiency is not an accreditation."""
        report = report_for([rule()], [record()])
        self.assertIs(False, report["signoff"])

    def test_green_opens_no_feedback_edge(self):
        report = report_for([rule()], [record()])
        self.assertEqual([], build_feedback(report["rows"], UNIT))

    def test_the_manifest_names_the_limit_authority_not_a_local_number(self):
        report = report_for([rule()], [record()])
        authority = report["rows"][0]["limit_authority"]
        self.assertEqual("empedans-stackup", authority["owner_skill"])
        self.assertNotEqual("l3-bringup-karakterizasyon",
                            authority["owner_skill"])


class OutsideTheLimit(unittest.TestCase):
    def test_outside_the_limit_is_red(self):
        report = report_for([rule()], [record(value=140.0)])
        self.assertEqual(RED, report["status"])
        self.assertEqual(1, report["coverage"]["violations"])

    def test_red_opens_an_l2_edge_naming_the_upstream_owner(self):
        report = report_for([rule()], [record(value=140.0)])
        actions = build_feedback(report["rows"], UNIT)
        self.assertEqual(1, len(actions))
        self.assertEqual("empedans-stackup", actions[0]["to_skill"])
        self.assertEqual("L2", actions[0]["loop"])
        self.assertIs(False, actions[0]["writes_board"])

    def test_a_thermal_row_feeds_back_to_its_own_owner(self):
        """The edge follows the DECLARED owner, not a hard-coded one."""
        thermal = rule(rule_id="THERM-1", owner="guc-isil",
                       measurement_class="thermal",
                       limit={"units": "C", "max": 85.0,
                              "authority": dict(AUTHORITY,
                                                owner_skill="guc-isil")})
        hot = record(rule_id="THERM-1", measurement_class="thermal",
                     units="C", value=101.0, uncertainty=1.0, guard_band=1.0)
        report = report_for([thermal], [hot])
        actions = build_feedback(report["rows"], UNIT)
        self.assertEqual(RED, report["status"])
        self.assertEqual("guc-isil", actions[0]["to_skill"])

    def test_one_red_row_is_not_outvoted_by_green_rows(self):
        thermal = rule(rule_id="THERM-1", owner="guc-isil",
                       measurement_class="thermal",
                       limit={"units": "C", "max": 85.0,
                              "authority": dict(AUTHORITY,
                                                owner_skill="guc-isil")})
        hot = record(measurement_id="M-002", rule_id="THERM-1",
                     measurement_class="thermal", units="C", value=120.0)
        report = report_for([rule(), thermal], [record(), hot])
        self.assertEqual(RED, report["status"])


class FaultInjection(unittest.TestCase):
    """Perturb ONE measured value across the limit; GREEN must become RED."""

    def test_moving_a_value_across_the_limit_flips_green_to_red(self):
        clean = report_for([rule()], [record(value=90.0)])
        self.assertEqual(GREEN, clean["status"])

        injected = report_for([rule()], [record(value=140.0)])
        self.assertEqual(RED, injected["status"])
        self.assertEqual(1, injected["coverage"]["violations"])
        self.assertEqual(
            "empedans-stackup",
            build_feedback(injected["rows"], UNIT)[0]["to_skill"])

    def test_deleting_the_calibration_flips_green_to_no_coverage(self):
        broken = record()
        broken["calibration_id"] = ""
        report = report_for([rule()], [broken])
        self.assertEqual(NO_COVERAGE, report["status"])

    def test_deleting_the_limit_flips_green_to_no_coverage(self):
        stripped = rule()
        stripped.pop("limit")
        report = report_for([stripped], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])


class MissingEvidence(unittest.TestCase):
    def test_a_required_row_with_no_records_is_no_coverage_never_green(self):
        report = report_for([rule()], [])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertEqual(0, report["coverage"]["scanned"])
        self.assertEqual(1, report["coverage"]["eligible"])
        self.assertIn("RECORDS_ABSENT", report["reason_codes"])

    def test_records_for_another_rule_do_not_cover_this_one(self):
        report = report_for([rule()], [record(rule_id="SOMETHING-ELSE")])
        self.assertEqual(NO_COVERAGE, report["status"])

    def test_a_tool_report_does_not_become_physical_evidence(self):
        report = report_for([rule()],
                            [record(evidence_origin="TOOL_REPORT")])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("NOT_PHYSICAL_EVIDENCE", report["reason_codes"])

    def test_an_empty_rule_universe_is_no_coverage_not_a_vacuous_green(self):
        report = report_for([], [])
        self.assertEqual(NO_COVERAGE, report["status"])


class NoInventedThreshold(unittest.TestCase):
    def test_a_rule_with_no_limit_is_no_coverage(self):
        stripped = rule()
        stripped.pop("limit")
        report = report_for([stripped], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("LIMIT_NOT_SUPPLIED", report["reason_codes"])
        self.assertIsNone(report["rows"][0]["limit"])

    def test_a_limit_without_an_authority_binding_is_no_coverage(self):
        unsourced = rule(limit={"units": "ohm", "min": 81.0, "max": 99.0})
        report = report_for([unsourced], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("LIMIT_AUTHORITY_MISSING", report["reason_codes"])

    def test_an_incomplete_authority_is_no_coverage(self):
        partial = rule(limit={"units": "ohm", "min": 81.0, "max": 99.0,
                              "authority": {"owner_skill": "empedans-stackup"}})
        report = report_for([partial], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("LIMIT_AUTHORITY_MISSING", report["reason_codes"])

    def test_a_self_authored_limit_is_refused(self):
        """A gate that writes its own threshold measures itself."""
        tautology = rule(limit={
            "units": "ohm", "min": 81.0, "max": 99.0,
            "authority": dict(AUTHORITY,
                              owner_skill="l3-bringup-karakterizasyon")})
        report = report_for([tautology], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("SELF_AUTHORED_LIMIT", report["reason_codes"])

    def test_a_rule_with_no_owner_is_no_coverage(self):
        ownerless = rule(owner="")
        report = report_for([ownerless], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("OWNER_NOT_DECLARED", report["reason_codes"])

    def test_a_limit_with_neither_bound_is_no_coverage(self):
        boundless = rule(limit={"units": "ohm",
                                "authority": dict(AUTHORITY)})
        report = report_for([boundless], [record()])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("LIMIT_BOUNDS_ABSENT", report["reason_codes"])

    def test_rule_limit_never_returns_a_number_it_was_not_given(self):
        for broken in ({}, {"rule_id": "X"}, {"rule_id": "X", "owner": "y"},
                       {"rule_id": "X", "owner": "y", "limit": {}}):
            with self.subTest(rule=broken):
                limit, code, detail = rule_limit(broken)
                self.assertIsNone(limit)
                self.assertTrue(code)
                self.assertTrue(detail)


class UnitBinding(unittest.TestCase):
    def test_a_foreign_board_hash_is_stale(self):
        report = report_for([rule()], [record(board_hash=OTHER_HASH)])
        self.assertEqual(STALE, report["status"])
        self.assertIn("UNIT_BINDING_MISMATCH", report["reason_codes"])

    def test_a_foreign_serial_is_stale_too(self):
        report = report_for([rule()], [record(serial="SN-0002")])
        self.assertEqual(STALE, report["status"])

    def test_a_foreign_revision_is_stale_too(self):
        report = report_for([rule()], [record(board_revision="A")])
        self.assertEqual(STALE, report["status"])

    def test_a_stale_row_is_excluded_not_scanned(self):
        report = report_for([rule()], [record(board_hash=OTHER_HASH)])
        self.assertEqual(0, report["coverage"]["scanned"])
        self.assertEqual(1, report["coverage"]["excluded"])

    def test_a_malformed_board_hash_is_invalid_not_stale(self):
        report = report_for([rule()], [record(board_hash="not-a-digest")])
        self.assertEqual(INVALID_METRIC, report["status"])

    def test_an_unaccepted_first_article_forces_red(self):
        """Faults found on a unit whose assembly was never accepted get
        charged to the design (rule-conflicts.md §9)."""
        unaccepted = dict(UNIT, accepted=False)
        report = build_report([rule()], [record()], unaccepted, [])
        self.assertEqual(RED, report["status"])
        self.assertIn("ACCEPTED_UNIT_PRECONDITION_BROKEN",
                      report["reason_codes"])


class CalibrationAndSetup(unittest.TestCase):
    def test_no_calibration_id_is_no_coverage(self):
        broken = record()
        broken["calibration_id"] = ""
        report = report_for([rule()], [broken])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("UNCALIBRATED", report["reason_codes"])

    def test_no_calibration_date_is_no_coverage(self):
        broken = record()
        broken["calibration_date"] = ""
        report = report_for([rule()], [broken])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("UNCALIBRATED", report["reason_codes"])

    def test_uncalibrated_is_never_green(self):
        broken = record()
        broken.pop("calibration_id")
        report = report_for([rule()], [broken])
        self.assertNotEqual(GREEN, report["status"])

    def test_an_unwritten_setup_is_no_coverage(self):
        for field in ("method", "setup", "environment", "load_state"):
            with self.subTest(field=field):
                broken = record()
                broken[field] = ""
                report = report_for([rule()], [broken])
                self.assertEqual(NO_COVERAGE, report["status"])
                self.assertIn("SETUP_NOT_REPRODUCIBLE",
                              report["reason_codes"])

    def test_a_missing_assessor_is_not_silently_accepted(self):
        broken = record()
        broken["assessor"] = ""
        report = report_for([rule()], [broken])
        self.assertNotEqual(GREEN, report["status"])


class IntervalComparison(unittest.TestCase):
    """value +/- (uncertainty + guard_band), never the point estimate alone."""

    LIMIT = {"units": "ohm", "min": 81.0, "max": 99.0}

    def test_wholly_inside_is_green(self):
        verdict, _ = judge(90.0, 1.0, 1.0, self.LIMIT)
        self.assertEqual(GREEN, verdict)

    def test_wholly_outside_is_red(self):
        verdict, _ = judge(120.0, 1.0, 1.0, self.LIMIT)
        self.assertEqual(RED, verdict)

    def test_straddling_the_upper_edge_is_inconclusive(self):
        verdict, _ = judge(98.5, 1.0, 1.0, self.LIMIT)
        self.assertEqual(INCONCLUSIVE, verdict)

    def test_straddling_the_lower_edge_is_inconclusive(self):
        verdict, _ = judge(81.5, 1.0, 1.0, self.LIMIT)
        self.assertEqual(INCONCLUSIVE, verdict)

    def test_a_point_estimate_inside_but_a_band_outside_is_not_a_pass(self):
        """The exact case highspeed-routing §6 forbids reporting as PASS."""
        verdict, detail = judge(98.9, 0.5, 0.5, self.LIMIT)
        self.assertNotEqual(GREEN, verdict)
        self.assertIn("straddles", detail)

    def test_a_one_sided_limit_only_constrains_that_side(self):
        upper_only = {"units": "C", "max": 85.0}
        self.assertEqual(GREEN, judge(20.0, 1.0, 1.0, upper_only)[0])
        self.assertEqual(RED, judge(120.0, 1.0, 1.0, upper_only)[0])

    def test_the_whole_report_reports_inconclusive(self):
        report = report_for([rule()], [record(value=98.5, uncertainty=1.0,
                                              guard_band=1.0)])
        self.assertEqual(INCONCLUSIVE, report["status"])
        self.assertIn("LIMIT_INCONCLUSIVE", report["reason_codes"])

    def test_an_inconclusive_row_counts_as_scanned_but_not_a_violation(self):
        report = report_for([rule()], [record(value=98.5, uncertainty=1.0,
                                              guard_band=1.0)])
        self.assertEqual(1, report["coverage"]["scanned"])
        self.assertEqual(0, report["coverage"]["violations"])

    def test_a_missing_uncertainty_is_not_treated_as_zero(self):
        broken = record()
        broken.pop("uncertainty")
        report = report_for([rule()], [broken])
        self.assertNotEqual(GREEN, report["status"])

    def test_mismatched_units_are_invalid_not_compared(self):
        report = report_for([rule()], [record(units="mm")])
        self.assertEqual(INVALID_METRIC, report["status"])
        self.assertIn("UNIT_MISMATCH", report["reason_codes"])


class SParameterDiscipline(unittest.TestCase):
    """highspeed-routing/SKILL.md §6, enforced rather than restated."""

    def test_a_missing_port_map_is_invalid_sparam(self):
        broken = record()
        broken.pop("port_map")
        report = report_for([rule()], [broken])
        self.assertEqual(INVALID_SPARAM, report["status"])
        self.assertIn("SPARAM_METADATA_INCOMPLETE", report["reason_codes"])

    def test_a_missing_reference_impedance_is_invalid_sparam(self):
        broken = record()
        broken.pop("reference_impedance")
        self.assertEqual(INVALID_SPARAM,
                         report_for([rule()], [broken])["status"])

    def test_a_missing_deembed_method_is_invalid_sparam(self):
        broken = record()
        broken.pop("deembed_method")
        self.assertEqual(INVALID_SPARAM,
                         report_for([rule()], [broken])["status"])

    def test_every_sparam_field_is_individually_load_bearing(self):
        for field in ("reference_plane", "measurement_zone", "port_map",
                      "reference_impedance", "fixture", "deembed_method"):
            with self.subTest(field=field):
                broken = record()
                broken[field] = None
                report = report_for([rule()], [broken])
                self.assertIn(report["status"],
                              (INVALID_SPARAM, NO_COVERAGE))
                self.assertNotEqual(GREEN, report["status"])

    def test_a_non_positive_reference_impedance_is_refused(self):
        report = report_for([rule()], [record(reference_impedance=0.0)])
        self.assertEqual(INVALID_SPARAM, report["status"])

    def test_a_non_sparam_class_does_not_demand_the_sparam_block(self):
        thermal = rule(rule_id="THERM-1", owner="guc-isil",
                       measurement_class="thermal",
                       limit={"units": "C", "max": 85.0,
                              "authority": dict(AUTHORITY,
                                                owner_skill="guc-isil")})
        plain = record(rule_id="THERM-1", measurement_class="thermal",
                       units="C", value=60.0)
        for field in ("port_map", "reference_impedance", "deembed_method",
                      "measurement_zone", "fixture"):
            plain.pop(field, None)
        self.assertEqual(GREEN, report_for([thermal], [plain])["status"])

    def test_a_class_mismatch_is_invalid_metric(self):
        report = report_for([rule()], [record(measurement_class="thermal")])
        self.assertEqual(INVALID_METRIC, report["status"])
        self.assertIn("MEASUREMENT_CLASS_MISMATCH", report["reason_codes"])


class PreComplianceIsNeverCompliance(unittest.TestCase):
    def _emc(self):
        emc_rule = rule(rule_id="EMC-RE-30M", owner="emc-esd",
                        measurement_class="near_field",
                        limit={"units": "dBuV/m", "max": 40.0,
                               "authority": dict(AUTHORITY,
                                                 owner_skill="emc-esd")})
        emc_record = record(rule_id="EMC-RE-30M",
                            measurement_class="near_field",
                            units="dBuV/m", value=12.0,
                            compliance_class="pre_compliance")
        return emc_rule, emc_record

    def test_a_pre_compliance_record_is_screen_only(self):
        emc_rule, emc_record = self._emc()
        report = report_for([emc_rule], [emc_record])
        self.assertEqual(SCREEN_ONLY, report["status"])
        self.assertIn("PRE_COMPLIANCE_ONLY", report["reason_codes"])

    def test_a_comfortably_passing_screen_is_still_not_green(self):
        """Even 28 dB under the limit: a probe number and a lab limit are not
        the same quantity (emc-esd §8)."""
        emc_rule, emc_record = self._emc()
        emc_record["value"] = 1.0
        report = report_for([emc_rule], [emc_record])
        self.assertNotEqual(GREEN, report["status"])
        self.assertNotEqual(GREEN, report["gate"]["verdict"])

    def test_a_screen_does_not_close_the_row(self):
        emc_rule, emc_record = self._emc()
        report = report_for([emc_rule], [emc_record])
        self.assertEqual(0, report["coverage"]["scanned"])
        self.assertEqual(1, report["coverage"]["excluded"])

    def test_a_pre_compliance_record_without_a_baseline_is_no_coverage(self):
        emc_rule, emc_record = self._emc()
        emc_record["baseline_id"] = ""
        report = report_for([emc_rule], [emc_record])
        self.assertEqual(NO_COVERAGE, report["status"])
        self.assertIn("BASELINE_ABSENT", report["reason_codes"])

    def test_an_accredited_row_fed_bench_evidence_is_confirm(self):
        emc_rule, _ = self._emc()
        emc_rule["required_compliance_class"] = "accredited_lab"
        bench = record(rule_id="EMC-RE-30M", measurement_class="near_field",
                       units="dBuV/m", value=12.0, compliance_class="bench")
        report = report_for([emc_rule], [bench])
        self.assertEqual(CONFIRM, report["status"])
        self.assertIn("EVIDENCE_CLASS_BELOW_REQUIREMENT",
                      report["reason_codes"])

    def test_an_accredited_claim_without_the_lab_artifact_is_confirm(self):
        emc_rule, _ = self._emc()
        emc_rule["required_compliance_class"] = "accredited_lab"
        claimed = record(rule_id="EMC-RE-30M", measurement_class="near_field",
                         units="dBuV/m", value=12.0,
                         compliance_class="accredited_lab")
        report = report_for([emc_rule], [claimed])
        self.assertEqual(CONFIRM, report["status"])
        self.assertIn("LAB_ARTIFACT_ABSENT", report["reason_codes"])

    def test_an_undeclared_compliance_class_has_no_default(self):
        report = report_for([rule()], [record(compliance_class="")])
        self.assertNotEqual(GREEN, report["status"])


class RawDataCustody(unittest.TestCase):
    def test_a_malformed_raw_digest_is_invalid(self):
        report = report_for([rule()], [record(raw_data_sha256="nope")])
        self.assertEqual(INVALID_METRIC, report["status"])

    def test_a_matching_capture_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"! Touchstone stand-in\n"
            (Path(tmp) / "coupon.s2p").write_bytes(payload)
            good = record(raw_data_path="coupon.s2p",
                          raw_data_sha256=sha256_bytes(payload))
            report = report_for([rule()], [good], raw_root=Path(tmp))
            self.assertEqual(GREEN, report["status"])
            self.assertNotIn("RAW_DATA_NOT_VERIFIED", report["reason_codes"])

    def test_a_capture_that_does_not_match_its_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "coupon.s2p").write_bytes(b"a different sweep\n")
            report = report_for([rule()],
                                [record(raw_data_path="coupon.s2p")],
                                raw_root=Path(tmp))
            self.assertEqual(INVALID_METRIC, report["status"])
            self.assertIn("RAW_DATA_DIGEST_MISMATCH", report["reason_codes"])

    def test_an_absent_capture_is_no_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = report_for([rule()],
                                [record(raw_data_path="missing.s2p")],
                                raw_root=Path(tmp))
            self.assertEqual(NO_COVERAGE, report["status"])
            self.assertIn("RAW_DATA_ABSENT", report["reason_codes"])

    def test_an_unverified_run_says_so(self):
        report = report_for([rule()], [record()])
        self.assertIn("RAW_DATA_NOT_VERIFIED", report["reason_codes"])


class CampaignOrderAndNamespaces(unittest.TestCase):
    def test_the_campaign_stage_lives_in_domain_state_not_the_verdict(self):
        report = report_for([rule()], [record()])
        self.assertIn("domain_state", report)
        self.assertNotIn(report["domain_state"],
                         (GREEN, RED, NO_COVERAGE, SCREEN_ONLY, CONFIRM))
        self.assertIn(report["gate"]["verdict"], (GREEN, RED, NO_COVERAGE))

    def test_measuring_late_stages_before_the_early_one_is_flagged(self):
        power = rule(rule_id="RAIL-SEQ", owner="guc-isil",
                     measurement_class="rail_sequence",
                     campaign_stage="power_safe",
                     limit={"units": "ms", "max": 50.0,
                            "authority": dict(AUTHORITY,
                                              owner_skill="guc-isil")})
        report = report_for([power, rule()], [record()])
        self.assertIn("CAMPAIGN_ORDER_VIOLATION", report["reason_codes"])
        self.assertTrue(report["campaign"]["out_of_order"])

    def test_a_closed_campaign_reports_complete(self):
        report = report_for([rule()], [record()])
        self.assertEqual("CAMPAIGN_COMPLETE", report["campaign"]["reached"])

    def test_an_unmeasured_campaign_has_not_started(self):
        report = report_for([rule()], [])
        self.assertEqual("NOT_STARTED", report["campaign"]["reached"])

    def test_a_stage_with_no_rows_is_not_reported_closed(self):
        """'nothing was declared here' and 'this was proved safe' must not be
        the same word."""
        report = report_for([rule()], [record()])
        stages = report["campaign"]["stages"]
        self.assertEqual("no_rows", stages["power_safe"])
        self.assertEqual("closed", stages["characterisation"])

    def test_an_open_early_stage_holds_the_reached_marker(self):
        power = rule(rule_id="RAIL-SEQ", owner="guc-isil",
                     measurement_class="rail_sequence",
                     campaign_stage="power_safe",
                     limit={"units": "ms", "max": 50.0,
                            "authority": dict(AUTHORITY,
                                              owner_skill="guc-isil")})
        report = report_for([power, rule()], [record()])
        self.assertEqual("POWER_SAFE", report["campaign"]["reached"])


class RollupOrdering(unittest.TestCase):
    def test_red_beats_everything(self):
        self.assertEqual(RED, worst([GREEN, NO_COVERAGE, RED, SCREEN_ONLY]))

    def test_no_coverage_beats_green(self):
        self.assertEqual(NO_COVERAGE, worst([GREEN, GREEN, NO_COVERAGE]))

    def test_screen_only_beats_green(self):
        self.assertEqual(SCREEN_ONLY, worst([GREEN, SCREEN_ONLY]))

    def test_an_empty_set_is_no_coverage(self):
        self.assertEqual(NO_COVERAGE, worst([]))

    def test_all_green_is_green(self):
        self.assertEqual(GREEN, worst([GREEN, GREEN]))


class OptionalRows(unittest.TestCase):
    def test_an_optional_row_does_not_hold_the_gate(self):
        optional = rule(rule_id="OPT-1", required=False)
        report = report_for([rule(), optional], [record()])
        self.assertEqual(GREEN, report["status"])
        self.assertEqual(1, report["coverage"]["eligible"])
        self.assertIn("OPT-1", report["optional_rows"])


class CliContract(unittest.TestCase):
    def _write(self, tmp, rules, records, accepted=True):
        root = Path(tmp)
        (root / "rules.json").write_text(json.dumps(
            {"schema_version": "l3-rule-universe/v1", "rules": rules}),
            encoding="utf-8")
        (root / "records.json").write_text(json.dumps(
            {"schema_version": "measurement-record/v1", "records": records}),
            encoding="utf-8")
        (root / "fai.json").write_text(json.dumps({
            "schema_version": "first-article-report/v1",
            "verdict": "GREEN" if accepted else "RED",
            "unit": {"board_revision": "B", "board_hash": BOARD_HASH,
                     "serial": "SN-0001", "lot": "LOT-2026-31"}}),
            encoding="utf-8")
        return (root / "rules.json", root / "records.json", root / "fai.json")

    def _run(self, tmp, rules, records, accepted=True, extra=()):
        rules_path, records_path, fai_path = self._write(
            tmp, rules, records, accepted)
        out = Path(tmp) / "manifest.json"
        proc = run("--rules", str(rules_path), "--records", str(records_path),
                   "--accepted-unit", str(fai_path), "--json", str(out),
                   *extra)
        return proc, out

    def test_self_test_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)

    def test_a_green_campaign_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = self._run(tmp, [rule()], [record()])
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(GREEN, json.loads(
                out.read_text(encoding="utf-8"))["status"])

    def test_a_red_campaign_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = self._run(tmp, [rule()], [record(value=140.0)])
            self.assertEqual(1, proc.returncode)
            self.assertEqual(RED, json.loads(
                out.read_text(encoding="utf-8"))["status"])

    def test_no_coverage_exits_one_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, _ = self._run(tmp, [rule()], [])
            self.assertEqual(1, proc.returncode)

    def test_missing_inputs_exit_two(self):
        self.assertEqual(2, run().returncode)

    def test_an_unreadable_rule_universe_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            _r, records_path, fai_path = self._write(tmp, [rule()], [record()])
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            proc = run("--rules", str(broken), "--records", str(records_path),
                       "--accepted-unit", str(fai_path))
            self.assertEqual(2, proc.returncode)

    def test_an_unaccepted_first_article_blocks_the_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, out = self._run(tmp, [rule()], [record()], accepted=False)
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(RED, report["status"])
            self.assertIn("ACCEPTED_UNIT_PRECONDITION_BROKEN",
                          report["reason_codes"])

    def test_a_first_article_without_a_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules_path, records_path, fai_path = self._write(
                tmp, [rule()], [record()])
            fai_path.write_text(json.dumps(
                {"verdict": "GREEN",
                 "unit": {"board_revision": "B", "serial": "SN-0001",
                          "lot": "LOT-2026-31"}}), encoding="utf-8")
            proc = run("--rules", str(rules_path), "--records",
                       str(records_path), "--accepted-unit", str(fai_path))
            self.assertEqual(2, proc.returncode)

    def test_the_real_first_article_envelope_is_understood(self):
        """first-article-report/v1 puts the unit in `identity` and the verdict
        in `gate`. Refusing it over field placement would be a refusal about
        spelling, not about evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            rules_path, records_path, fai_path = self._write(
                tmp, [rule()], [record()])
            fai_path.write_text(json.dumps({
                "schema_version": "first-article-report/v1",
                "identity": {"board_revision": "B", "board_hash": BOARD_HASH,
                             "lot": "LOT-2026-31", "unit_serial": "SN-0001",
                             "package_binding": "BOUND"},
                "gate": {"verdict": "GREEN", "serial_release": True}}),
                encoding="utf-8")
            out = Path(tmp) / "manifest.json"
            proc = run("--rules", str(rules_path), "--records",
                       str(records_path), "--accepted-unit", str(fai_path),
                       "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(GREEN, report["status"])
            self.assertEqual("SN-0001", report["unit"]["serial"])
            self.assertEqual("BOUND",
                             report["unit"]["first_article"]["package_binding"])

    def test_a_held_serial_release_vetoes_the_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules_path, records_path, fai_path = self._write(
                tmp, [rule()], [record()])
            fai_path.write_text(json.dumps({
                "identity": {"board_revision": "B", "board_hash": BOARD_HASH,
                             "lot": "LOT-2026-31", "unit_serial": "SN-0001"},
                "gate": {"verdict": "GREEN", "serial_release": False}}),
                encoding="utf-8")
            out = Path(tmp) / "manifest.json"
            proc = run("--rules", str(rules_path), "--records",
                       str(records_path), "--accepted-unit", str(fai_path),
                       "--json", str(out))
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(RED, report["status"])
            self.assertIn("ACCEPTED_UNIT_PRECONDITION_BROKEN",
                          report["reason_codes"])

    def test_a_first_article_with_a_null_board_hash_is_refused(self):
        """identity.board_hash is nullable in first-article-report/v1. A null
        one cannot bind physical evidence to a design, so the campaign stops
        rather than measuring an anonymous board."""
        with tempfile.TemporaryDirectory() as tmp:
            rules_path, records_path, fai_path = self._write(
                tmp, [rule()], [record()])
            fai_path.write_text(json.dumps({
                "identity": {"board_revision": "B", "board_hash": None,
                             "lot": "LOT-2026-31", "unit_serial": "SN-0001"},
                "gate": {"verdict": "GREEN", "serial_release": True}}),
                encoding="utf-8")
            proc = run("--rules", str(rules_path), "--records",
                       str(records_path), "--accepted-unit", str(fai_path))
            self.assertEqual(2, proc.returncode)
            self.assertIn("board_hash", proc.stderr)

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, _ = self._run(tmp, [rule()], [record()])
            json.loads(proc.stdout)

    def test_the_receipt_and_feedback_files_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules_path, records_path, fai_path = self._write(
                tmp, [rule()], [record(value=140.0)])
            manifest = Path(tmp) / "manifest.json"
            receipt = Path(tmp) / "receipt.json"
            feedback = Path(tmp) / "feedback.json"
            run("--rules", str(rules_path), "--records", str(records_path),
                "--accepted-unit", str(fai_path),
                "--manifest", str(manifest), "--receipt", str(receipt),
                "--feedback", str(feedback))
            receipt_doc = json.loads(receipt.read_text(encoding="utf-8"))
            feedback_doc = json.loads(feedback.read_text(encoding="utf-8"))
            self.assertEqual("l3-gate-receipt/v1",
                             receipt_doc["schema_version"])
            self.assertEqual(RED, receipt_doc["verdict"])
            self.assertIs(False, receipt_doc["signoff"])
            self.assertEqual(64, len(receipt_doc["manifest"]["sha256"] or ""))
            self.assertEqual("empedans-stackup",
                             feedback_doc["actions"][0]["to_skill"])
            self.assertIs(False, feedback_doc["writes_board"])


class SchemaFiles(unittest.TestCase):
    def setUp(self):
        self.manifest_schema = json.loads(
            (SCHEMAS / "physical_measurement_manifest.schema.json")
            .read_text(encoding="utf-8"))
        self.receipt_schema = json.loads(
            (SCHEMAS / "l3_gate_receipt.schema.json")
            .read_text(encoding="utf-8"))
        self.record_schema = json.loads(
            (SCHEMAS.parent.parent / "skills" / "l3-bringup-karakterizasyon"
             / "references" / "measurement_record.schema.json")
            .read_text(encoding="utf-8"))

    def test_the_manifest_schema_pins_version_and_signoff(self):
        properties = self.manifest_schema["properties"]
        self.assertEqual("physical-measurement-manifest/v1",
                         properties["schema_version"]["const"])
        self.assertIs(False, properties["signoff"]["const"])

    def test_the_receipt_schema_pins_version_and_signoff(self):
        properties = self.receipt_schema["properties"]
        self.assertEqual("l3-gate-receipt/v1",
                         properties["schema_version"]["const"])
        self.assertIs(False, properties["signoff"]["const"])

    def test_the_receipt_requires_physical_measurement_origin(self):
        self.assertEqual(
            "PHYSICAL_MEASUREMENT",
            self.receipt_schema["properties"]["evidence_origin"]
            ["contains"]["const"])

    def test_no_new_verdict_is_introduced(self):
        """Every verdict this tool can emit is already in coverage-gate.md."""
        declared = set(self.manifest_schema["$defs"]["verdict"]["enum"])
        vocabulary = {
            "GREEN", "RED", "CONFIRM", "SCREEN_ONLY", "LOCAL_POLICY",
            "SIM_ONLY", "NO_COVERAGE", "INVALID_METRIC", "INVALID_COVERAGE",
            "INVALID_SPARAM", "INCONCLUSIVE", "STALE",
            "GEOMETRY_PRECHECK_PASS", "TBD", "API_YOK"}
        self.assertEqual(set(), declared - vocabulary)

    def test_the_authority_may_not_be_this_skill(self):
        authority = self.manifest_schema["$defs"]["authority"]
        self.assertEqual("l3-bringup-karakterizasyon",
                         authority["properties"]["owner_skill"]["not"]["const"])

    def test_every_emitted_reason_code_is_declared(self):
        declared = set(self.manifest_schema["$defs"]["reason_code"]["enum"])
        emitted = set()
        no_limit = rule()
        no_limit.pop("limit")
        scenarios = [
            ([rule()], [record()]),
            ([rule()], []),
            ([rule()], [record(value=140.0)]),
            ([no_limit], [record()]),
            ([rule()], [record(board_hash=OTHER_HASH)]),
            ([rule()], [record(units="mm")]),
            ([rule()], [record(measurement_class="thermal")]),
            ([rule()], [record(value=98.5, uncertainty=1.0, guard_band=1.0)]),
            ([rule(owner="")], [record()]),
            ([], []),
        ]
        for rules, records in scenarios:
            emitted |= set(report_for(rules, records)["reason_codes"])
        self.assertEqual(set(), emitted - declared)

    def test_a_real_report_carries_every_required_manifest_field(self):
        report = report_for([rule()], [record()])
        for key in self.manifest_schema["required"]:
            self.assertIn(key, report)

    def test_a_real_receipt_carries_every_required_field(self):
        report = report_for([rule()], [record()])
        receipt = build_receipt(report, None, 0)
        for key in self.receipt_schema["required"]:
            self.assertIn(key, receipt)

    def test_the_record_schema_forbids_a_local_limit_field(self):
        """The input record must not be able to carry its own threshold."""
        properties = self.record_schema["$defs"]["record"]["properties"]
        for forbidden in ("limit", "min", "max", "threshold", "spec"):
            self.assertNotIn(forbidden, properties)

    def test_the_record_schema_requires_the_custody_fields(self):
        required = set(self.record_schema["$defs"]["record"]["required"])
        for field in ("board_hash", "calibration_id", "calibration_date",
                      "method", "setup", "environment", "load_state",
                      "uncertainty", "guard_band", "raw_data_sha256",
                      "compliance_class", "assessor", "timestamp"):
            self.assertIn(field, required)

    def test_the_record_schema_pins_physical_measurement(self):
        origin = (self.record_schema["$defs"]["record"]["properties"]
                  ["evidence_origin"])
        self.assertEqual("PHYSICAL_MEASUREMENT", origin["const"])


class ItTakesNoMeasurement(unittest.TestCase):
    """The refusal is load-bearing, so it is asserted rather than trusted."""

    SOURCE = SCRIPT.read_text(encoding="utf-8")

    def test_it_declares_the_refusal_in_its_docstring(self):
        self.assertIn("takes no measurement", self.SOURCE)

    def test_it_imports_no_instrument_transport(self):
        for module in ("pyvisa", "visa", "serial", "usbtmc", "socket",
                       "requests", "urllib"):
            with self.subTest(module=module):
                self.assertNotIn(f"import {module}", self.SOURCE)

    def test_it_contains_no_limit_constant(self):
        """A default threshold anywhere in this module would decide rows on
        every board from a number nobody sourced."""
        for name in ("DEFAULT_LIMIT", "FALLBACK_LIMIT", "TYPICAL_",
                     "DEFAULT_TOLERANCE", "DEFAULT_MAX"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.SOURCE)


if __name__ == "__main__":
    unittest.main()
