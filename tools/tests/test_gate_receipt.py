#!/usr/bin/env python3
"""Regression and fault-injection tests for gate_receipt.py.

All mutations happen in TemporaryDirectory copies of the staging fixtures.
No command reads from or writes to the live Obsidian vault.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import FIXTURES as _FIX, SCRIPTS, TOOLS  # noqa: E402

HERE = Path(__file__).resolve().parent
VALIDATOR = SCRIPTS / "gate_receipt.py"
FIXTURES = _FIX / "gate_receipt"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GateReceiptTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="gate-receipt-test-")
        self.root = Path(self.temp.name) / "fixtures"
        shutil.copytree(FIXTURES, self.root)
        self.positive_path = self.root / "usb_pair_positive.json"
        self.positive = json.loads(self.positive_path.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, data: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        return path

    def run_receipt(self, path: Path, *extra: str):
        run = subprocess.run(
            [sys.executable, str(VALIDATOR), str(path), *extra],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertTrue(run.stdout, run.stderr)
        return run.returncode, json.loads(run.stdout)

    def assert_status(self, path: Path, expected: str, exit_code: int = 1):
        actual_exit, report = self.run_receipt(path)
        self.assertEqual(exit_code, actual_exit, report)
        self.assertEqual(expected, report["effective_status"], report)
        return report

    def test_positive_real_usb_shape_is_green(self):
        exit_code, report = self.run_receipt(self.positive_path)
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])
        self.assertIn("LEGACY_SCHEMA_ALIAS",
                      {row["code"] for row in report["warnings"]})
        self.assertIn("LEGACY_SCHEMA_VERSION",
                      {row["code"] for row in report["warnings"]})
        self.assertEqual("usb-hs-pair-verifier-receipt/v1",
                         report["source_schema_version"])
        self.assertFalse(self.positive["provenance"]["synthetic"])

    def test_stale_input_fault_is_rejected(self):
        target = self.root / "usb_pair_metrics_minimal.json"
        target.write_text(target.read_text(encoding="utf-8") + "\n",
                          encoding="utf-8")
        report = self.assert_status(self.positive_path, "STALE")
        self.assertIn("HASH_MISMATCH", report["reason_codes"])

    def test_tool_hash_fault_is_rejected(self):
        data = copy.deepcopy(self.positive)
        data["tool"]["sha256"] = "0" * 64
        report = self.assert_status(self.write("tool_hash.json", data), "STALE")
        self.assertIn("HASH_MISMATCH", report["reason_codes"])

    def test_config_hash_fault_is_rejected(self):
        data = copy.deepcopy(self.positive)
        data["config"]["sha256"] = "f" * 64
        report = self.assert_status(self.write("config_hash.json", data), "STALE")
        self.assertIn("HASH_MISMATCH", report["reason_codes"])

    def test_zero_scope_is_no_coverage(self):
        data = copy.deepcopy(self.positive)
        data["coverage"] = {
            "scanned": 0, "eligible": 0, "excluded": 0,
            "reason": "fault injection: empty net selection",
        }
        report = self.assert_status(self.write("no_coverage.json", data),
                                    "NO_COVERAGE")
        self.assertIn("NO_COVERAGE", report["reason_codes"])

    def test_coverage_arithmetic_with_exclusions_is_green(self):
        data = copy.deepcopy(self.positive)
        data["coverage"] = {
            "scanned": 600, "eligible": 629, "excluded": 29,
            "reason": "29 endpoint samples intentionally excluded",
            "per_metric": {
                metric_id: {"scanned": 600, "eligible": 629, "excluded": 29}
                for metric_id in data["parsed_metrics"]
            },
        }
        exit_code, report = self.run_receipt(self.write("coverage_valid.json", data))
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])

    def test_inconsistent_coverage_arithmetic_is_red(self):
        data = copy.deepcopy(self.positive)
        data["coverage"] = {
            "scanned": 629, "eligible": 0, "excluded": 0,
            "reason": "adversarial inconsistent coverage",
        }
        report = self.assert_status(self.write("coverage_invalid.json", data), "RED")
        self.assertIn("INVALID_COVERAGE", report["reason_codes"])

    def test_inconsistent_per_metric_coverage_is_red(self):
        data = copy.deepcopy(self.positive)
        data["coverage"]["per_metric"] = {
            metric_id: {"scanned": 10, "eligible": 10, "excluded": 0}
            for metric_id in data["parsed_metrics"]
        }
        victim = next(iter(data["coverage"]["per_metric"].values()))
        victim.update({"scanned": 10, "eligible": 0, "excluded": 0})
        report = self.assert_status(self.write("metric_coverage_invalid.json", data),
                                    "RED")
        self.assertIn("INVALID_COVERAGE", report["reason_codes"])

    def test_missing_semantics_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        data.pop("metric_semantics")
        report = self.assert_status(self.write("missing_semantics.json", data),
                                    "INVALID_METRIC")
        self.assertIn("MISSING_METRIC_SEMANTICS", report["reason_codes"])

    def test_uncovered_metric_leaf_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        data["parsed_metrics"]["uncovered_metric"] = 123
        report = self.assert_status(self.write("uncovered_metric.json", data),
                                    "INVALID_METRIC")
        self.assertIn("MISSING_METRIC_SEMANTIC_MAPPING",
                      report["reason_codes"])

    def test_extra_semantic_mapping_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        spec = copy.deepcopy(next(iter(data["metric_semantics"].values())))
        data["metric_semantics"]["not_a_parsed_metric"] = spec
        report = self.assert_status(self.write("extra_semantic.json", data),
                                    "INVALID_METRIC")
        self.assertIn("EXTRA_METRIC_SEMANTIC_MAPPING",
                      report["reason_codes"])

    def test_hash_bound_semantic_ref_can_cover_one_leaf(self):
        data = copy.deepcopy(self.positive)
        metric_id = next(iter(data["metric_semantics"]))
        spec = data["metric_semantics"].pop(metric_id)
        ref_path = self.write("semantic_ref.json", {
            "metric_id": metric_id,
            "semantics": spec,
        })
        data["metric_semantic_refs"] = [{
            "metric_id": metric_id,
            "path": ref_path.name,
            "sha256": sha256(ref_path),
        }]
        exit_code, report = self.run_receipt(self.write("semantic_ref_green.json", data))
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])

    def test_string_semantic_ref_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        data.pop("metric_semantics")
        data["metric_semantic_refs"] = ["not-a-hash-bound-reference"]
        report = self.assert_status(self.write("string_semantic_ref.json", data),
                                    "INVALID_METRIC")
        self.assertIn("INVALID_SEMANTIC_REF", report["reason_codes"])

    def test_semantic_ref_hash_mismatch_is_stale(self):
        data = copy.deepcopy(self.positive)
        metric_id = next(iter(data["metric_semantics"]))
        spec = data["metric_semantics"].pop(metric_id)
        ref_path = self.write("semantic_ref_stale.json", {
            "metric_id": metric_id,
            "semantics": spec,
        })
        data["metric_semantic_refs"] = [{
            "metric_id": metric_id,
            "path": ref_path.name,
            "sha256": "0" * 64,
        }]
        report = self.assert_status(self.write("semantic_ref_bad_hash.json", data),
                                    "STALE")
        self.assertIn("HASH_MISMATCH", report["reason_codes"])

    def test_inline_and_ref_semantic_conflict_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        metric_id, spec = next(iter(data["metric_semantics"].items()))
        ref_path = self.write("semantic_ref_conflict.json", {
            "metric_id": metric_id,
            "semantics": spec,
        })
        data["metric_semantic_refs"] = [{
            "metric_id": metric_id,
            "path": ref_path.name,
            "sha256": sha256(ref_path),
        }]
        report = self.assert_status(self.write("semantic_conflict.json", data),
                                    "INVALID_METRIC")
        self.assertIn("DUPLICATE_SEMANTIC_MAPPING", report["reason_codes"])

    def test_centerline_as_edge_gap_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        first = next(iter(data["metric_semantics"].values()))
        first["distance_semantics"] = "centerline_to_centerline"
        report = self.assert_status(self.write("centerline_gap.json", data),
                                    "INVALID_METRIC")
        self.assertIn("CENTERLINE_AS_EDGE_GAP", report["reason_codes"])

    def test_chord_as_arc_is_invalid_metric(self):
        data = copy.deepcopy(self.positive)
        old_key = next(iter(data["metric_semantics"]))
        spec = data["metric_semantics"].pop(old_key)
        spec["geometry_model"] = "chord_only"
        data["metric_semantics"]["J1_U1.arc_aware_edge_gap_mm.p50"] = spec
        report = self.assert_status(self.write("chord_as_arc.json", data),
                                    "INVALID_METRIC")
        self.assertIn("CHORD_AS_ARC", report["reason_codes"])

    def test_empty_fault_proof_is_red(self):
        data = copy.deepcopy(self.positive)
        data["fault_injection"]["observed_failures"] = []
        report = self.assert_status(self.write("empty_fault.json", data), "RED")
        self.assertIn("EMPTY_FAULT_INJECTION", report["reason_codes"])

    def test_assurance_a_without_fault_injection_is_red(self):
        data = copy.deepcopy(self.positive)
        self.assertEqual("A", data["assurance_level"])
        data.pop("fault_injection")
        report = self.assert_status(
            self.write("assurance_a_missing_fault.json", data), "RED")
        self.assertIn("MISSING_FAULT_INJECTION", report["reason_codes"])
        self.assertNotIn("MISSING_ARTIFACT", report["reason_codes"])

    def test_require_fault_flag_without_fault_injection_is_red(self):
        data = copy.deepcopy(self.positive)
        data["assurance_level"] = "B"
        data.pop("fault_injection")
        path = self.write("flag_missing_fault.json", data)

        baseline_exit, baseline = self.run_receipt(path)
        self.assertEqual(0, baseline_exit, baseline)
        self.assertEqual("GREEN", baseline["effective_status"], baseline)

        forced_exit, forced = self.run_receipt(
            path, "--require-fault-injection")
        self.assertEqual(1, forced_exit, forced)
        self.assertEqual("RED", forced["effective_status"], forced)
        self.assertIn("MISSING_FAULT_INJECTION", forced["reason_codes"])
        self.assertNotIn("MISSING_ARTIFACT", forced["reason_codes"])

    def test_made_up_fault_proof_is_red(self):
        data = copy.deepcopy(self.positive)
        data["fault_injection"] = {
            "status": "PASS",
            "fixture": "no-real-fixture",
            "observed_failures": ["made-up"],
        }
        report = self.assert_status(self.write("fault_spoof.json", data), "RED")
        self.assertTrue({"MISSING_FAULT_ARTIFACT_BINDING",
                         "MISSING_FAULT_COMMAND",
                         "UNKNOWN_FAULT_METRIC_ID"}
                        <= set(report["reason_codes"]))

    def test_fault_baseline_must_bind_to_receipt_input(self):
        data = copy.deepcopy(self.positive)
        data["fault_injection"]["baseline_artifact"] = copy.deepcopy(data["config"])
        report = self.assert_status(self.write("fault_unbound_baseline.json", data),
                                    "RED")
        self.assertIn("UNBOUND_FAULT_BASELINE", report["reason_codes"])

    def test_fault_must_change_injected_artifact_hash(self):
        data = copy.deepcopy(self.positive)
        data["fault_injection"]["injected_artifact"] = copy.deepcopy(
            data["fault_injection"]["baseline_artifact"])
        report = self.assert_status(self.write("fault_same_artifact.json", data),
                                    "RED")
        self.assertIn("FAULT_DID_NOT_CHANGE_ARTIFACT", report["reason_codes"])

    def test_fault_fixture_content_must_match_receipt_expectations(self):
        data = copy.deepcopy(self.positive)
        victim = data["fault_injection"]["expected_failed_metrics"][0]
        fixture = self.write("fault_expectation_mismatch.json", {
            "expected_failed_metrics": [victim],
            "operation": "adversarial mismatch",
        })
        data["fault_injection"]["fixture"] = {
            "path": fixture.name,
            "sha256": sha256(fixture),
        }
        report = self.assert_status(self.write("fault_content_mismatch.json", data),
                                    "RED")
        self.assertIn("FAULT_FIXTURE_EXPECTATION_MISMATCH",
                      report["reason_codes"])

    def test_null_blockers_is_reported_not_crashed(self):
        """``"blockers": null`` is a PRESENT key, so ``.get(key, [])`` never
        fired its default and the reason-code loop raised TypeError - the
        validator died with a traceback instead of emitting the ERROR
        ``validate_blockers`` had already recorded.

        This is not a hypothetical shape:
        ``skills/pcb-orchestrator/references/gate_receipt.schema.json``
        publishes ``"blockers": {"type": ["array", "null"]}``, so a receipt
        written against the documented schema was the crashing input. A gate
        that dies is not a gate that said no - the caller sees exit 2 and no
        report, which reads as a broken tool rather than a refused receipt.
        """
        data = copy.deepcopy(self.positive)
        data["blockers"] = None
        report = self.assert_status(self.write("null_blockers.json", data),
                                    "ERROR", exit_code=2)
        self.assertIn("MISSING_FIELD", report["reason_codes"])
        self.assertIn("$.blockers", {row["path"] for row in report["issues"]})

    def test_mixed_mandatory_children_reduce_to_red(self):
        green_child = self.write("child_green.json", self.positive)
        red_data = copy.deepcopy(self.positive)
        red_data["verdict"] = "RED"
        red_data["blockers"] = [{"code": "GEOMETRY_FAIL",
                                  "message": "injected mandatory sub-gate failure"}]
        red_data["retention"]["retain"] = sorted(
            {"receipt", "child_receipts", "logs", "scratch"}
        )
        red_child = self.write("child_red.json", red_data)

        parent = copy.deepcopy(self.positive)
        parent["gate_id"] = "usb-composite"
        parent["gate_kind"] = "composite"
        parent["child_receipts"] = [
            {"path": green_child.name, "sha256": sha256(green_child),
             "verdict": "GREEN", "mandatory": True},
            {"path": red_child.name, "sha256": sha256(red_child),
             "verdict": "RED", "mandatory": True},
        ]
        report = self.assert_status(self.write("mixed_children.json", parent), "RED")
        self.assertIn("MANDATORY_CHILD_RED", report["reason_codes"])

    def test_advisory_red_child_remains_visible_but_not_gating(self):
        red_data = copy.deepcopy(self.positive)
        red_data["verdict"] = "RED"
        red_data["retention"]["retain"] = sorted(
            {"receipt", "child_receipts", "logs", "scratch"}
        )
        red_child = self.write("advisory_red.json", red_data)
        parent = copy.deepcopy(self.positive)
        parent["gate_kind"] = "composite"
        parent["child_receipts"] = [
            {"path": red_child.name, "sha256": sha256(red_child),
             "verdict": "RED", "mandatory": False}
        ]
        path = self.write("advisory_parent.json", parent)
        exit_code, report = self.run_receipt(path)
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])
        self.assertIn("ADVISORY_CHILD_RED",
                      {row["code"] for row in report["warnings"]})

    def test_child_path_escape_is_error(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(json.dumps(self.positive), encoding="utf-8")
        parent = copy.deepcopy(self.positive)
        parent["gate_kind"] = "composite"
        parent["child_receipts"] = [{
            "path": "../outside.json",
            "sha256": sha256(outside),
            "verdict": "GREEN",
            "mandatory": True,
        }]
        report = self.assert_status(self.write("path_escape.json", parent),
                                    "ERROR", exit_code=2)
        self.assertIn("PATH_OUTSIDE_ROOT", report["reason_codes"])

    def test_nested_child_resolves_own_relative_artifacts_inside_root(self):
        nested = self.root / "nested"
        nested.mkdir()
        for name in (
            "usb_pair_metrics_minimal.json", "provenance.json",
            "review_2l_rules.json", "usb_pair_verifier_fixture.py",
            "usb_pair_gap_fault_definition.json",
            "usb_pair_gap_fault_observation.json",
        ):
            shutil.copy2(self.root / name, nested / name)
        child = nested / "child.json"
        child.write_text(json.dumps(self.positive), encoding="utf-8")
        parent = copy.deepcopy(self.positive)
        parent["gate_kind"] = "composite"
        parent["child_receipts"] = [{
            "path": "nested/child.json",
            "sha256": sha256(child),
            "verdict": "GREEN",
            "mandatory": True,
        }]
        exit_code, report = self.run_receipt(self.write("nested_parent.json", parent))
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])

    def test_absolute_artifact_path_escape_is_error(self):
        outside = Path(self.temp.name) / "outside_rules.json"
        outside.write_text("{}\n", encoding="utf-8")
        data = copy.deepcopy(self.positive)
        data["config"] = {"path": str(outside), "sha256": sha256(outside)}
        report = self.assert_status(self.write("absolute_path_escape.json", data),
                                    "ERROR", exit_code=2)
        self.assertIn("PATH_OUTSIDE_ROOT", report["reason_codes"])

    def test_drc_exit_zero_does_not_hide_parsed_violations(self):
        data = copy.deepcopy(self.positive)
        data["gate_kind"] = "drc"
        data["argv"] = ["kicad-cli", "pcb", "drc", "board.kicad_pcb"]
        data["parsed_metrics"] = {"violation_count": 3}
        data["metric_semantics"] = {
            "violation_count": {
                "definition": "Violations parsed from the full DRC report",
                "units": "count",
                "distance_semantics": "not_applicable",
                "region": "whole_board",
                "sampling_step": "not_applicable",
                "geometry_model": "kicad_drc_native",
            }
        }
        report = self.assert_status(self.write("drc_false_zero.json", data), "RED")
        self.assertTrue({"MISSING_EXIT_CODE_VIOLATIONS", "PARSED_VIOLATIONS"}
                        <= set(report["reason_codes"]))

    def test_integer_metric_leaf_is_green(self):
        data = copy.deepcopy(self.positive)
        data["parsed_metrics"]["sample_count"] = 629
        data["metric_semantics"]["sample_count"] = {
            "definition": "Number of evaluated samples",
            "units": "count",
            "distance_semantics": "not_applicable",
            "region": "whole_gate",
            "sampling_step": "not_applicable",
            "geometry_model": "integer_counter",
        }
        exit_code, report = self.run_receipt(self.write("integer_metric.json", data))
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])

    def test_non_numeric_metric_leaf_types_are_invalid(self):
        invalid_values = {
            "list": [1, 2, "x"],
            "bool": True,
            "null": None,
            "string": "123",
            "nan": float("nan"),
        }
        spec = {
            "definition": "Adversarial typed metric",
            "units": "count",
            "distance_semantics": "not_applicable",
            "region": "whole_gate",
            "sampling_step": "not_applicable",
            "geometry_model": "scalar",
        }
        for label, value in invalid_values.items():
            with self.subTest(label=label):
                data = copy.deepcopy(self.positive)
                data["parsed_metrics"]["bad_metric"] = value
                data["metric_semantics"]["bad_metric"] = spec
                report = self.assert_status(
                    self.write(f"bad_metric_{label}.json", data), "INVALID_METRIC")
                expected = "INVALID_METRIC_NUMBER" if label == "nan" \
                    else "INVALID_METRIC_TYPE"
                self.assertIn(expected, report["reason_codes"])

    def test_green_promotion_contract(self):
        data = copy.deepcopy(self.positive)
        artifact = self.root / "usb_pair_metrics_minimal.json"
        data["gate_kind"] = "promotion"
        data["promotion"] = {
            "decision": "GREEN",
            "reason_codes": [],
            "scratch_path": "scratch/usb-hs-review",
            "source_artifacts": [
                {"role": "scratch_output", "path": artifact.name,
                 "sha256": sha256(artifact)}
            ],
            "target_artifacts": [
                {"role": "canonical_output", "path": artifact.name,
                 "sha256": sha256(artifact)}
            ],
            "commands": [
                {"argv": ["promote", "--atomic", artifact.name], "exit_code": 0}
            ],
        }
        path = self.write("promotion_green.json", data)
        exit_code, report = self.run_receipt(path)
        self.assertEqual(0, exit_code, report)
        self.assertEqual("GREEN", report["effective_status"])

    def test_red_promotion_requires_reason_codes(self):
        data = copy.deepcopy(self.positive)
        artifact = self.root / "usb_pair_metrics_minimal.json"
        data["gate_kind"] = "promotion"
        data["verdict"] = "RED"
        data["retention"]["retain"] = sorted(
            {"receipt", "child_receipts", "logs", "scratch"}
        )
        data["promotion"] = {
            "decision": "RED", "reason_codes": [],
            "scratch_path": "scratch/usb-hs-review",
            "source_artifacts": [
                {"role": "scratch_output", "path": artifact.name,
                 "sha256": sha256(artifact)}
            ],
            "target_artifacts": [
                {"role": "canonical_output", "path": artifact.name,
                 "sha256": sha256(artifact),
                 "before_sha256": sha256(artifact),
                 "after_sha256": sha256(artifact)}
            ],
            "commands": [{"argv": ["validate"], "exit_code": 1}],
        }
        report = self.assert_status(self.write("promotion_red.json", data), "RED")
        self.assertIn("MISSING_RED_REASONS", report["reason_codes"])

    def test_red_promotion_proves_canonical_unchanged(self):
        data = copy.deepcopy(self.positive)
        source = self.root / "usb_pair_metrics_minimal.json"
        canonical = self.write("canonical.json", {"version": "before"})
        canonical_hash = sha256(canonical)
        data["gate_kind"] = "promotion"
        data["verdict"] = "RED"
        data["retention"]["retain"] = sorted(
            {"receipt", "child_receipts", "logs", "scratch"}
        )
        data["promotion"] = {
            "decision": "RED", "reason_codes": ["L0_FAIL"],
            "scratch_path": "scratch/usb-hs-review",
            "source_artifacts": [
                {"role": "scratch_output", "path": source.name,
                 "sha256": sha256(source)}
            ],
            "target_artifacts": [
                {"role": "canonical_output", "path": canonical.name,
                 "sha256": canonical_hash,
                 "before_sha256": canonical_hash,
                 "after_sha256": canonical_hash}
            ],
            "commands": [{"argv": ["validate"], "exit_code": 1}],
        }
        report = self.assert_status(self.write("promotion_red_unchanged.json", data),
                                    "RED")
        self.assertNotIn("MISSING_RED_CANONICAL_HASH_PROOF", report["reason_codes"])
        self.assertNotIn("RED_PROMOTION_MODIFIED_CANONICAL", report["reason_codes"])

    def test_red_promotion_rejects_changed_canonical(self):
        data = copy.deepcopy(self.positive)
        source = self.root / "usb_pair_metrics_minimal.json"
        canonical = self.write("canonical_mutated.json", {"version": "before"})
        before_hash = sha256(canonical)
        canonical.write_text('{"version":"after"}\n', encoding="utf-8")
        after_hash = sha256(canonical)
        data["gate_kind"] = "promotion"
        data["verdict"] = "RED"
        data["retention"]["retain"] = sorted(
            {"receipt", "child_receipts", "logs", "scratch"}
        )
        data["promotion"] = {
            "decision": "RED", "reason_codes": ["L0_FAIL"],
            "scratch_path": "scratch/usb-hs-review",
            "source_artifacts": [
                {"role": "scratch_output", "path": source.name,
                 "sha256": sha256(source)}
            ],
            "target_artifacts": [
                {"role": "canonical_output", "path": canonical.name,
                 "sha256": after_hash,
                 "before_sha256": before_hash,
                 "after_sha256": after_hash}
            ],
            "commands": [{"argv": ["validate"], "exit_code": 1}],
        }
        report = self.assert_status(self.write("promotion_red_changed.json", data),
                                    "RED")
        self.assertIn("RED_PROMOTION_MODIFIED_CANONICAL", report["reason_codes"])

    def test_unknown_schema_is_error_exit_two(self):
        data = copy.deepcopy(self.positive)
        data["schema"] = "gate-receipt/v999"
        report = self.assert_status(self.write("unknown_schema.json", data),
                                    "ERROR", exit_code=2)
        self.assertIn("UNSUPPORTED_SCHEMA_VERSION", report["reason_codes"])

    def test_non_utc_timestamp_is_error(self):
        data = copy.deepcopy(self.positive)
        data["start_utc"] = "2026-07-29T14:20:26+03:00"
        report = self.assert_status(self.write("non_utc_time.json", data),
                                    "ERROR", exit_code=2)
        self.assertIn("INVALID_TIMESTAMP", report["reason_codes"])

    def test_conflicting_schema_alias_is_error(self):
        data = copy.deepcopy(self.positive)
        data["schema_version"] = "gate-receipt/v999"
        report = self.assert_status(self.write("schema_conflict.json", data),
                                    "ERROR", exit_code=2)
        self.assertIn("CONFLICTING_SCHEMA_VERSION", report["reason_codes"])

    def test_green_promotion_without_atomic_evidence_is_red(self):
        data = copy.deepcopy(self.positive)
        artifact = self.root / "usb_pair_metrics_minimal.json"
        data["gate_kind"] = "promotion"
        data["promotion"] = {
            "decision": "GREEN", "reason_codes": [],
            "scratch_path": "scratch/usb-hs-review",
            "source_artifacts": [
                {"role": "scratch_output", "path": artifact.name,
                 "sha256": sha256(artifact)}
            ],
            "target_artifacts": [
                {"role": "canonical_output", "path": artifact.name,
                 "sha256": sha256(artifact)}
            ],
            "commands": [{"argv": ["promote", artifact.name], "exit_code": 0}],
        }
        report = self.assert_status(self.write("promotion_non_atomic.json", data),
                                    "RED")
        self.assertIn("MISSING_ATOMIC_PROMOTION_EVIDENCE",
                      report["reason_codes"])

    def test_json_report_write_succeeds_before_stdout_green(self):
        output = self.root / "validator_report.json"
        exit_code, report = self.run_receipt(
            self.positive_path, "--json", str(output))
        self.assertEqual(0, exit_code, report)
        self.assertTrue(output.is_file())
        self.assertEqual("GREEN",
                         json.loads(output.read_text(encoding="utf-8"))[
                             "effective_status"])

    def test_json_report_write_error_is_structured_exit_two(self):
        output = self.root / "missing" / "validator_report.json"
        run = subprocess.run(
            [sys.executable, str(VALIDATOR), str(self.positive_path),
             "--json", str(output)],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(2, run.returncode, run.stdout + run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual("ERROR", report["effective_status"])
        self.assertEqual("GREEN", report["prior_effective_status"])
        self.assertIn("REPORT_WRITE_ERROR", report["reason_codes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
