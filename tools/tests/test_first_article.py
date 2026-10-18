#!/usr/bin/env python3
"""Tests for scripts/production/first_article.py - the as-built acceptance gate.

WHAT THESE TESTS ARE EVIDENCE OF, AND WHAT THEY ARE NOT
-------------------------------------------------------
Every fixture here is a STRUCTURED assembler/AOI/X-ray/MSL record. No test
feeds this tool an image, because the tool has no code path that reads one -
that is the point of the design, and ``test_no_image_is_ever_opened`` asserts
it against the module source rather than trusting the docstring.

So these tests prove the DECISION logic: which structured record combination
opens serial production and which one holds it. They prove nothing about
whether a particular AOI machine or X-ray operator was right. That judgement
belongs to the vendor and is carried, hash-bound, in the evidence; this gate
reports it and never re-judges it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCHEMAS, SCRIPTS  # noqa: E402

PRODUCTION = SCRIPTS / "production"
TOOL = PRODUCTION / "first_article.py"

from common.findings import coverage_rule_violations  # noqa: E402
from scripts.production import first_article as fa  # noqa: E402


def evidence(**overrides) -> dict:
    """The clean, conforming first article, with targeted damage applied."""
    doc = fa._clean_evidence()
    doc.update(overrides)
    return doc


def verdict_of(doc, **kwargs) -> str:
    return fa.evaluate(doc, **kwargs)["gate"]["verdict"]


class CleanFirstArticle(unittest.TestCase):
    def test_full_evidence_with_no_deviations_is_green(self):
        report = fa.evaluate(evidence())
        self.assertEqual(report["gate"]["verdict"], "GREEN")
        self.assertEqual(report["gate"]["verdict_reasons"], [])
        self.assertTrue(report["gate"]["serial_release"])

    def test_green_names_which_release_it_means(self):
        """'Release' is ambiguous - Gerber release, accepted-first-article
        release, product release. The report pins its own meaning so a reader
        cannot borrow the word for a claim this gate never made."""
        self.assertEqual(fa.evaluate(evidence())["gate"]["release_meaning"],
                         "accepted first article")

    def test_green_is_not_a_reliability_qualification(self):
        """Stated in the schema and the docstring; asserted here so the claim
        cannot quietly widen. GREEN carries no reliability metric at all."""
        report = fa.evaluate(evidence())
        self.assertNotIn("reliability", json.dumps(report["metrics"]))
        self.assertEqual(report["gate"]["evidence_origin"],
                         ["PHYSICAL_MEASUREMENT", "DESIGN_ARTIFACT",
                          "TOOL_REPORT"])

    def test_the_gate_does_not_validate_its_own_receipt(self):
        self.assertEqual(fa.evaluate(evidence())["gate"]["closed_by"],
                         "tools/scripts/gate_receipt.py")


class AsBuiltConformance(unittest.TestCase):
    def test_fault_injection_one_wrong_orientation_turns_green_into_red(self):
        """THE defect this whole gate exists for. It passes DRC, it passes the
        CPL format check, and the board is dead. Flipping exactly one record
        in an otherwise-GREEN article must flip the verdict."""
        clean = evidence()
        self.assertEqual(verdict_of(clean), "GREEN")

        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90      # was 270
        report = fa.evaluate(damaged)

        self.assertEqual(report["gate"]["verdict"], "RED")
        self.assertFalse(report["gate"]["serial_release"])
        violation = report["by_check"]["as_built_orientation"]
        self.assertEqual(violation["status"], "FAIL")
        self.assertEqual(violation["violations"], 1)

    def test_the_orientation_violation_routes_feedback_to_cpl_assembly(self):
        """This gate classifies and routes; it never rewrites the CPL."""
        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        self.assertEqual(fa.evaluate(damaged)["feedback_edges"],
                         ["cpl-assembly"])

    def test_the_delta_takes_the_shorter_way_round(self):
        near = evidence()
        near["refdes_universe"][0]["expected_rotation_deg"] = 0
        near["as_built"][0]["rotation_deg"] = 359.5
        self.assertEqual(
            fa.evaluate(near, rotation_tolerance_deg=1.0)
            ["by_check"]["as_built_orientation"]["status"], "PASS")

    def test_a_wrong_value_is_red(self):
        damaged = evidence()
        damaged["as_built"][1]["value"] = "1N4001"
        report = fa.evaluate(damaged)
        self.assertEqual(report["gate"]["verdict"], "RED")
        self.assertEqual(report["by_check"]["as_built_value"]["violations"], 1)

    def test_a_dnp_part_that_was_fitted_is_red(self):
        damaged = evidence()
        damaged["refdes_universe"][1]["dnp"] = True
        report = fa.evaluate(damaged)
        self.assertEqual(report["gate"]["verdict"], "RED")
        problems = {v["problem"] for v in
                    report["checks"][2]["violations"]}
        self.assertIn("DNP_PART_FITTED", problems)

    def test_a_missing_populated_part_is_red(self):
        damaged = evidence()
        damaged["as_built"][0]["present"] = False
        self.assertEqual(verdict_of(damaged), "RED")


class ReleasedPackageBinding(unittest.TestCase):
    def test_a_released_package_hash_mismatch_is_red_and_blocks_serial(self):
        """Same declared revision, different package bytes: the unit was built
        from something that is not the package on record under that name."""
        damaged = evidence()
        damaged["identity"]["released_package_sha256"] = "c" * 64
        report = fa.evaluate(damaged)
        self.assertEqual(report["gate"]["verdict"], "RED")
        self.assertFalse(report["gate"]["serial_release"])
        self.assertEqual(report["identity"]["package_binding"], "HASH_MISMATCH")
        self.assertTrue(report["blockers"])

    def test_evidence_from_another_board_revision_is_stale(self):
        damaged = evidence()
        damaged["identity"]["board_revision"] = "rev-B"
        report = fa.evaluate(damaged)
        self.assertEqual(report["gate"]["verdict"], "STALE")
        self.assertEqual(report["identity"]["package_binding"],
                         "REVISION_MISMATCH")

    def test_a_single_evidence_record_from_another_revision_is_stale(self):
        """One AOI export carried over from the previous spin is enough. The
        refdes all still exist, so nothing else notices."""
        damaged = evidence()
        damaged["as_built"][0]["evidence"]["board_revision"] = "rev-B"
        self.assertEqual(verdict_of(damaged), "STALE")

    def test_a_missing_built_from_digest_is_never_green(self):
        """The assembler never said what it built from, and that is NOT
        assumed to match.

        The verdict is NO_COVERAGE rather than CONFIRM because the binding
        check examined nothing - "there is no record" and "there is a record
        that needs interpretation" are different states and the report says
        which one it is. The CONFIRM reason (a vendor record is what would
        close this) is still named, prefixed, in verdict_reasons, so a reader
        gets both facts from one run.
        """
        damaged = evidence()
        damaged["identity"]["released_package_sha256"] = None
        report = fa.evaluate(damaged)
        self.assertEqual(report["identity"]["package_binding"], "UNBOUND")
        self.assertEqual(report["gate"]["verdict"], "NO_COVERAGE")
        self.assertFalse(report["gate"]["serial_release"])
        self.assertTrue(any("CONFIRM" in reason
                            for reason in report["gate"]["verdict_reasons"]),
                        report["gate"]["verdict_reasons"])

    def test_the_package_on_disk_is_rehashed_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "gerbers.zip"
            package.write_bytes(b"not the released bytes")
            report = fa.evaluate(evidence(), verify_package=str(package))
        self.assertEqual(report["gate"]["verdict"], "RED")
        problems = {v["problem"]
                    for v in report["checks"][0]["violations"]}
        self.assertIn("RELEASED_PACKAGE_ON_DISK_MISMATCH", problems)


class NoCoverage(unittest.TestCase):
    def test_a_required_universe_with_zero_records_is_no_coverage_not_green(self):
        """Two eligible refdes, no AOI/assembler record at all."""
        blank = evidence(as_built=[])
        report = fa.evaluate(blank)
        self.assertEqual(report["gate"]["verdict"], "NO_COVERAGE")
        self.assertNotEqual(report["gate"]["verdict"], "GREEN")
        self.assertEqual(report["coverage"]["scanned"], 0)
        self.assertFalse(report["gate"]["serial_release"])

    def test_a_bga_with_no_xray_is_never_green(self):
        """Optical evidence does not reach a hidden joint at all, so an AOI
        PASS on a BGA says nothing about the balls under it."""
        bga = evidence()
        bga["refdes_universe"][0]["hidden_joint"] = True
        report = fa.evaluate(bga)
        self.assertIn(report["gate"]["verdict"], ("NO_COVERAGE", "CONFIRM"))
        self.assertNotEqual(report["gate"]["verdict"], "GREEN")
        self.assertEqual(report["coverage_by_class"]["hidden_joint_xray"],
                         {"eligible": 1, "scanned": 0, "excluded": 1,
                          "violations": 0})

    def test_a_bga_with_an_xray_pass_can_be_green(self):
        """The negative test above must be about the missing X-ray and not
        about hidden_joint being an unconditional blocker."""
        bga = evidence()
        bga["refdes_universe"][0]["hidden_joint"] = True
        bga["xray_records"] = [{
            "refdes": "U1", "result": "PASS", "void_pct": 8.0,
            "acceptance_standard": "IPC-7095 Class 2",
            "evidence": fa._evidence_ref("XR-1", "XRAY")}]
        self.assertEqual(verdict_of(bga), "GREEN")

    def test_a_vendor_xray_fail_is_red_and_is_not_re_judged(self):
        bga = evidence()
        bga["refdes_universe"][0]["hidden_joint"] = True
        bga["xray_records"] = [{
            "refdes": "U1", "result": "FAIL", "void_pct": 41.0,
            "acceptance_standard": "IPC-7095 Class 2",
            "evidence": fa._evidence_ref("XR-1", "XRAY")}]
        report = fa.evaluate(bga)
        self.assertEqual(report["gate"]["verdict"], "RED")
        violation = report["by_check"]["hidden_joint_xray"]
        self.assertEqual(violation["status"], "FAIL")

    def test_an_empty_hidden_joint_population_is_an_exclusion_not_a_gap(self):
        """Zero X-ray records over zero BGAs is a fact about the released
        package. Reading it as NO_COVERAGE would jam the gate shut on every
        board with no BGA, and a gate that can never open gets bypassed."""
        report = fa.evaluate(evidence())
        excluded = {e["check"] for e in
                    report["coverage_ledger"]["excluded_checks"]}
        self.assertIn("hidden_joint_xray", excluded)
        for entry in report["coverage_ledger"]["excluded_checks"]:
            self.assertTrue(entry["reason"].strip(),
                            "an exclusion with no reason is a silent pass")

    def test_a_moisture_sensitive_part_with_no_handling_record_is_not_green(self):
        msl = evidence()
        msl["refdes_universe"][0]["msl_level"] = "3"
        report = fa.evaluate(msl)
        self.assertNotEqual(report["gate"]["verdict"], "GREEN")
        self.assertEqual(
            report["coverage_by_class"]["msl_handling_record"]["scanned"], 0)

    def test_floor_life_exceeded_without_a_bake_is_red(self):
        msl = evidence()
        msl["refdes_universe"][0]["msl_level"] = "3"
        msl["msl_records"] = [{
            "refdes": "U1", "msl_level": "3",
            "floor_life_hours_declared": 168, "floor_life_hours_used": 400,
            "bake_performed": False,
            "evidence": fa._evidence_ref("MSL-1", "ASSEMBLER_RECORD")}]
        report = fa.evaluate(msl)
        self.assertEqual(report["gate"]["verdict"], "RED")
        problems = {v["problem"]
                    for v in report["by_check"] and
                    [c for c in report["checks"]
                     if c["check"] == "msl_handling_record"][0]["violations"]}
        self.assertIn("FLOOR_LIFE_EXCEEDED_NO_BAKE", problems)

    def test_the_msl_level_disagreement_routes_back_to_bom_lifecycle(self):
        """bom-lifecycle owns the level; this gate only reports that the
        assembler handled the part at a different one."""
        msl = evidence()
        msl["refdes_universe"][0]["msl_level"] = "3"
        msl["msl_records"] = [{
            "refdes": "U1", "msl_level": "5",
            "evidence": fa._evidence_ref("MSL-1", "ASSEMBLER_RECORD")}]
        self.assertIn("bom-lifecycle", fa.evaluate(msl)["feedback_edges"])

    def test_a_panelised_build_with_no_depanel_trial_is_not_green(self):
        panel = evidence(panelised=True)
        self.assertNotEqual(verdict_of(panel), "GREEN")

    def test_an_unstated_panelisation_is_not_read_as_no_panel(self):
        panel = evidence(panelised=None)
        self.assertNotEqual(verdict_of(panel), "GREEN")

    def test_a_missing_fab_acceptance_is_not_green(self):
        self.assertNotEqual(verdict_of(evidence(fab_dfm_acceptance=None)),
                            "GREEN")


class ScreenOnly(unittest.TestCase):
    def test_photo_only_evidence_with_no_signoff_standard_is_screen_only(self):
        """A photograph is not an acceptance criterion. Somebody looked;
        nobody said what 'good' was."""
        photo = evidence()
        for row in photo["as_built"]:
            row["evidence"] = fa._evidence_ref(row["refdes"], "PHOTO", None)
        report = fa.evaluate(photo)
        self.assertEqual(report["gate"]["verdict"], "SCREEN_ONLY")
        self.assertFalse(report["gate"]["serial_release"])

    def test_a_photo_with_a_named_standard_is_an_acceptance(self):
        photo = evidence()
        for row in photo["as_built"]:
            row["evidence"] = fa._evidence_ref(row["refdes"], "PHOTO",
                                               "IPC-A-610 Class 2")
        self.assertEqual(verdict_of(photo), "GREEN")

    def test_screen_only_never_outranks_a_measured_failure(self):
        photo = evidence()
        for row in photo["as_built"]:
            row["evidence"] = fa._evidence_ref(row["refdes"], "PHOTO", None)
        photo["as_built"][0]["rotation_deg"] = 90
        self.assertEqual(verdict_of(photo), "RED")


class Deviations(unittest.TestCase):
    def test_an_unresolved_deviation_blocks_serial_release(self):
        doc = evidence(deviations=[{
            "id": "DEV-1", "refdes": "C4", "description": "tombstoned",
            "disposition": "open", "approver": None, "resolved": False,
            "feedback_target": "dfm"}])
        report = fa.evaluate(doc)
        self.assertEqual(report["gate"]["verdict"], "RED")
        self.assertIn("dfm", report["feedback_edges"])

    def test_a_disposition_with_nobody_s_name_on_it_is_not_a_disposition(self):
        doc = evidence(deviations=[{
            "id": "DEV-1", "description": "x", "disposition": "use_as_is",
            "approver": "", "resolved": True}])
        self.assertEqual(verdict_of(doc), "RED")

    def test_a_resolved_and_approved_deviation_does_not_block(self):
        doc = evidence(deviations=[{
            "id": "DEV-1", "description": "x", "disposition": "rework",
            "approver": "QA-7", "resolved": True}])
        self.assertEqual(verdict_of(doc), "GREEN")


class CoverageArithmetic(unittest.TestCase):
    """``scanned + excluded == eligible`` in EVERY emitted report.

    A report whose own arithmetic is broken cannot be argued with, so this is
    checked on the failing reports too - not only on the clean one.
    """

    CASES = {
        "clean": {},
        "no_records": {"as_built": []},
        "no_fab_acceptance": {"fab_dfm_acceptance": None},
        "panelised": {"panelised": True},
    }

    def _all_blocks(self, report):
        yield "coverage", report["coverage"]
        yield from report["coverage_by_class"].items()

    def test_every_coverage_block_of_every_report_satisfies_the_rule(self):
        for name, override in self.CASES.items():
            report = fa.evaluate(evidence(**override))
            for label, block in self._all_blocks(report):
                with self.subTest(case=name, block=label):
                    self.assertEqual([], coverage_rule_violations(block), block)
                    self.assertEqual(block["scanned"] + block["excluded"],
                                     block["eligible"])

    def test_a_damaged_report_still_publishes_valid_coverage(self):
        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        damaged["identity"]["released_package_sha256"] = "c" * 64
        report = fa.evaluate(damaged)
        self.assertEqual(report["gate"]["verdict"], "STALE"
                         if report["identity"]["package_binding"]
                         == "REVISION_MISMATCH" else "RED")
        for _, block in self._all_blocks(report):
            self.assertEqual([], coverage_rule_violations(block))

    def test_violations_counts_scanned_refdes_only(self):
        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        block = fa.evaluate(damaged)["coverage"]
        self.assertEqual(block["violations"], 1)
        self.assertLessEqual(block["violations"], block["scanned"])

    def test_an_unaccounted_refdes_is_folded_and_raised_never_only_folded(self):
        """The fold keeps the arithmetic honest; the simultaneous violation
        stops the fold from reading as a decision. One without the other is a
        silent pass in either direction."""
        orphan = evidence()
        orphan["as_built"] = orphan["as_built"][:1]
        report = fa.evaluate(orphan)

        self.assertEqual(report["coverage"],
                         {"eligible": 2, "scanned": 1, "excluded": 1,
                          "violations": 0})
        ledger = report["coverage_ledger"]["unaccounted_folded_into_excluded"]
        self.assertEqual([e["refdes"] for e in ledger], ["D1"])
        problems = {v["problem"] for c in report["checks"]
                    if c["check"] == "as_built_presence"
                    for v in c["violations"]}
        self.assertIn("UNACCOUNTED_REFDES", problems)
        self.assertNotEqual(report["gate"]["verdict"], "GREEN")

    def test_every_per_class_fold_states_its_reason_too(self):
        """The per-class triples must not be the one place in the report
        where something is excluded and nobody says why."""
        bga = evidence()
        bga["refdes_universe"][0]["hidden_joint"] = True
        bga["refdes_universe"][1]["msl_level"] = "3"
        report = fa.evaluate(bga)
        folds = report["coverage_ledger"]["class_unrecorded"]
        self.assertEqual([e["refdes"] for e in folds["hidden_joint_xray"]],
                         ["U1"])
        self.assertEqual([e["refdes"] for e in folds["msl_handling_record"]],
                         ["D1"])
        for entries in folds.values():
            for entry in entries:
                self.assertTrue(entry["reason"].strip())

    def test_a_symmetric_passive_with_no_angle_does_not_park_the_gate(self):
        """A gate that is permanently amber over a non-question is a gate
        nobody reads. expected_rotation_deg=null means there is no orientation
        expectation, so an absent as-built angle is not a missing measurement.
        """
        doc = evidence()
        doc["refdes_universe"][1]["expected_rotation_deg"] = None
        doc["refdes_universe"][1]["polar"] = False
        doc["as_built"][1].pop("rotation_deg")
        self.assertEqual(verdict_of(doc), "GREEN")

    def test_a_polar_part_with_no_recorded_angle_is_not_green(self):
        """The mirror of the test above: the released package DOES declare an
        expectation here, so silence is a missing measurement."""
        doc = evidence()
        doc["as_built"][0].pop("rotation_deg")
        self.assertNotEqual(verdict_of(doc), "GREEN")

    def test_an_explicit_exclusion_carries_its_reason(self):
        doc = evidence()
        doc["excluded"] = [{"refdes": "D1", "reason": "not fitted on the NPI "
                                                      "build, per WO-4412",
                            "approver": "QA-7"}]
        doc["as_built"] = doc["as_built"][:1]
        report = fa.evaluate(doc)
        self.assertEqual(report["coverage"]["excluded"], 1)
        self.assertEqual(
            report["coverage_ledger"]["unaccounted_folded_into_excluded"], [])
        self.assertEqual(report["gate"]["verdict"], "GREEN")

    def test_an_exclusion_with_no_reason_is_not_an_exclusion(self):
        doc = evidence()
        doc["excluded"] = [{"refdes": "D1", "reason": ""}]
        doc["as_built"] = doc["as_built"][:1]
        report = fa.evaluate(doc)
        self.assertEqual(
            [e["refdes"] for e in
             report["coverage_ledger"]["unaccounted_folded_into_excluded"]],
            ["D1"])
        self.assertNotEqual(report["gate"]["verdict"], "GREEN")


class OutOfScopeDeclaration(unittest.TestCase):
    """The conservative answer to the open question the source report left:
    is serial production genuinely this gate's release target, or only
    prototype/NPI? Undecided - so the gate is mandatory-by-default."""

    def test_declaring_the_gate_out_of_scope_records_it_and_never_passes(self):
        report = fa.evaluate(evidence(),
                             out_of_scope_reason="prototype-only, no serial "
                                                 "build planned for rev-C")
        self.assertEqual(report["gate"]["verdict"], "NO_COVERAGE")
        self.assertFalse(report["gate"]["serial_release"])
        self.assertIn("first_article_gate",
                      {e["check"] for e in
                       report["coverage_ledger"]["excluded_checks"]})

    def test_there_is_no_flag_that_turns_an_unrun_gate_green(self):
        blank = evidence(as_built=[])
        self.assertEqual(
            fa.evaluate(blank, out_of_scope_reason="prototype only")
            ["gate"]["verdict"], "NO_COVERAGE")


class EvidenceDiscipline(unittest.TestCase):
    def test_no_image_is_ever_opened(self):
        """The hard rule, asserted against the source rather than the prose.

        The tool must not import an image library and must not read the
        evidence locator. A locator is a pointer for a human auditor; the
        DECISION always comes from the structured record beside it.
        """
        import ast
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("PIL", "cv2", "numpy", "skimage", "imageio",
                          "matplotlib"):
            self.assertNotIn(forbidden, imported,
                             f"{forbidden} would let this tool form its own "
                             f"opinion about a picture")
        source = TOOL.read_text(encoding="utf-8")
        self.assertNotIn('["locator"]', source,
                         "the evidence locator must never be dereferenced")

    def test_an_evidence_block_with_no_digest_does_not_scan_anything(self):
        doc = evidence()
        doc["as_built"][0]["evidence"].pop("sha256")
        report = fa.evaluate(doc)
        self.assertEqual(report["coverage"]["scanned"], 1)
        self.assertNotEqual(report["gate"]["verdict"], "GREEN")

    def test_an_unknown_evidence_type_is_not_usable(self):
        usable, reason = fa.evidence_usable(
            {"id": "X", "type": "VIBES", "locator": "p", "sha256": "a" * 64,
             "recorded_utc": "2026-08-10T09:00:00Z"})
        self.assertFalse(usable)
        self.assertIn("VIBES", reason)

    def test_a_short_digest_is_refused(self):
        usable, _ = fa.evidence_usable(
            {"id": "X", "type": "AOI", "locator": "p", "sha256": "abc",
             "recorded_utc": "2026-08-10T09:00:00Z"})
        self.assertFalse(usable)


class EvidenceLoading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_unsupported_evidence_schema_raises_it_does_not_read_best_effort(self):
        path = self.tmp / "ev.json"
        path.write_text(json.dumps({"schema_version": "fai-evidence/v2"}),
                        encoding="utf-8")
        with self.assertRaises(fa.EvidenceError):
            fa.load_evidence(str(path))

    def test_a_missing_refdes_universe_raises(self):
        path = self.tmp / "ev.json"
        path.write_text(json.dumps({
            "schema_version": fa.EVIDENCE_SCHEMA_VERSION,
            "identity": {}, "released_package": {}}), encoding="utf-8")
        with self.assertRaises(fa.EvidenceError):
            fa.load_evidence(str(path))

    def test_broken_json_raises_rather_than_returning_empty(self):
        path = self.tmp / "ev.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(fa.EvidenceError):
            fa.load_evidence(str(path))


class ReportContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.schema = json.loads(
            (SCHEMAS / "first_article_report.schema.json")
            .read_text(encoding="utf-8"))

    def tearDown(self):
        self._tmp.cleanup()

    def write_evidence(self, doc) -> str:
        path = self.tmp / "fai_evidence.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def test_the_report_matches_the_pinned_schema_keys(self):
        proc = self.run_tool(self.write_evidence(evidence()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(report), sorted(self.schema["required"]))
        self.assertEqual(report["schema_version"],
                         self.schema["properties"]["schema_version"]["const"])

    def test_the_module_and_the_schema_agree_on_the_vocabulary(self):
        """A verdict the tool can produce but the schema forbids (or the
        reverse) is a contract that drifted without anything going red."""
        self.assertEqual(
            sorted(fa.VERDICTS),
            sorted(self.schema["properties"]["gate"]["properties"]
                   ["verdict"]["enum"]))

    def test_the_verdict_stays_inside_the_closed_vocabulary(self):
        allowed = set(self.schema["properties"]["gate"]["properties"]
                      ["verdict"]["enum"])
        cases = [
            evidence(),
            evidence(as_built=[]),
            evidence(panelised=True),
            evidence(fab_dfm_acceptance=None),
        ]
        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        cases.append(damaged)
        stale = evidence()
        stale["identity"]["board_revision"] = "rev-B"
        cases.append(stale)
        for index, doc in enumerate(cases):
            with self.subTest(case=index):
                self.assertIn(fa.evaluate(doc)["gate"]["verdict"], allowed)

    def test_exit_zero_means_green_and_nothing_else(self):
        green = self.run_tool(self.write_evidence(evidence()))
        self.assertEqual(green.returncode, 0)

        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        red = self.run_tool(self.write_evidence(damaged))
        self.assertEqual(red.returncode, 1)

        blank = self.run_tool(self.write_evidence(evidence(as_built=[])))
        self.assertEqual(blank.returncode, 1,
                         "NO_COVERAGE must not leave through exit 0")

    def test_stdout_is_json_only_and_diagnostics_go_to_stderr(self):
        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        proc = self.run_tool(self.write_evidence(damaged))
        json.loads(proc.stdout)                       # raises if not pure JSON
        self.assertIn("SERIAL BUILD BLOCKED", proc.stderr)

    def test_a_missing_positional_input_exits_two_with_no_stdout(self):
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_an_unreadable_evidence_file_exits_two(self):
        proc = self.run_tool(str(self.tmp / "nope.json"))
        self.assertEqual(proc.returncode, 2)

    def test_the_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_tool("--self-test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_the_three_declared_outputs_are_written(self):
        report_path = self.tmp / "first_article_report.json"
        receipt_path = self.tmp / "assembly_acceptance_receipt.json"
        log_path = self.tmp / "manufacturing_deviation_log.json"
        proc = self.run_tool(self.write_evidence(evidence()),
                             "--json", str(report_path),
                             "--receipt-out", str(receipt_path),
                             "--deviation-log-out", str(log_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for path in (report_path, receipt_path, log_path):
            self.assertTrue(path.is_file(), path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["evidence"]["sha256"],
                         json.loads(proc.stdout)["evidence"]["sha256"])

    def test_the_deviation_log_exists_even_when_there_is_nothing_in_it(self):
        """A log that only appears when something went wrong cannot be used to
        prove that nothing did."""
        log_path = self.tmp / "manufacturing_deviation_log.json"
        self.run_tool(self.write_evidence(evidence()),
                      "--deviation-log-out", str(log_path))
        log = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(log["deviations"], [])
        self.assertEqual(log["gate_verdict"], "GREEN")

    def test_the_acceptance_receipt_carries_the_gate_receipt_v1_fields(self):
        receipt_path = self.tmp / "assembly_acceptance_receipt.json"
        self.run_tool(self.write_evidence(evidence()),
                      "--receipt-out", str(receipt_path))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for field in ("gate_id", "gate_kind", "verdict", "start_utc",
                      "end_utc", "tool", "inputs", "config", "argv",
                      "exit_code", "parsed_metrics", "metric_semantics",
                      "coverage", "blockers", "retention"):
            self.assertIn(field, receipt)
        self.assertEqual(receipt["schema_version"], "gate-receipt/v1")
        self.assertEqual([], coverage_rule_violations(receipt["coverage"]))
        self.assertIn("FRAGMENT", receipt["$comment"],
                      "the receipt must say plainly that this tool did not "
                      "validate it")

    def test_the_acceptance_receipt_validates_through_gate_receipt(self):
        """End-to-end: the artifact this gate publishes is one the repo's own
        validator can close. Without this the receipt shape is a guess."""
        receipt_path = self.tmp / "assembly_acceptance_receipt.json"
        self.run_tool(self.write_evidence(evidence()),
                      "--receipt-out", str(receipt_path))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "gate_receipt.py"),
             str(receipt_path), "--root", str(Path(self.tmp.anchor))],
            capture_output=True, text=True, encoding="utf-8", cwd=str(REPO))
        report = json.loads(proc.stdout)
        self.assertEqual(report["effective_status"], "GREEN",
                         json.dumps(report["issues"], indent=2))

    def test_a_red_run_produces_a_receipt_that_cannot_validate_green(self):
        damaged = evidence()
        damaged["as_built"][0]["rotation_deg"] = 90
        receipt_path = self.tmp / "assembly_acceptance_receipt.json"
        self.run_tool(self.write_evidence(damaged),
                      "--receipt-out", str(receipt_path))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["verdict"], "RED")
        self.assertTrue(receipt["blockers"])
        self.assertNotEqual(receipt["exit_code"], 0)


class SkillContract(unittest.TestCase):
    """The skill and the tool must not drift apart."""

    SKILL = REPO / "skills" / "ilk-urun-montaj-kabulu" / "SKILL.md"
    EVIDENCE_SCHEMA = (REPO / "skills" / "ilk-urun-montaj-kabulu"
                       / "references" / "fai_evidence.schema.json")

    def test_the_skill_and_its_reference_schema_exist(self):
        self.assertTrue(self.SKILL.is_file())
        self.assertTrue(self.EVIDENCE_SCHEMA.is_file())

    def test_the_evidence_schema_version_matches_the_tool(self):
        schema = json.loads(self.EVIDENCE_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"],
                         fa.EVIDENCE_SCHEMA_VERSION)

    def test_the_skill_declares_the_open_question_rather_than_deciding_it(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("Açık soru / varsayım", text)
        self.assertIn("first-article", text)

    def test_the_skill_names_the_tool_the_registry_will_carry(self):
        import re
        text = self.SKILL.read_text(encoding="utf-8")
        match = re.search(r"^tools:\s*\[(.*?)\]", text, re.M | re.S)
        self.assertIsNotNone(match, "the skill must publish a tools: list")
        named = {x.strip() for x in match.group(1).split(",") if x.strip()}
        self.assertIn("first-article", named)
        self.assertIn("gate-receipt", named)

    def test_the_skill_does_not_claim_ownership_of_another_skill_s_rule(self):
        """Conflict boundaries, asserted rather than promised: this skill must
        not write a DFM threshold, edit the CPL, or redefine an MSL level."""
        text = self.SKILL.read_text(encoding="utf-8").lower()
        self.assertIn("cpl-assembly", text)
        self.assertIn("bom-lifecycle", text)
        self.assertIn("dfm", text)


if __name__ == "__main__":
    unittest.main()
