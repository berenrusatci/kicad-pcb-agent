#!/usr/bin/env python3
"""The promote.py -> gate_receipt.py round trip.

THE HEADLINE ASSERTION of this merge: the receipt promote.py writes is
validated GREEN, exit 0, by the repo's own gate_receipt.py with no --root and
no post-processing - and RED, exit 1, when the promotion is bad. The archived
producer and this validator were two halves of one story that spoke different
formats; these tests are what stops them drifting apart again.

Everything runs in a TemporaryDirectory. No canonical project is touched.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SCRIPTS, TOOLS  # noqa: E402

PROMOTE = SCRIPTS / "promote.py"
GATE_RECEIPT = SCRIPTS / "gate_receipt.py"

SCRATCH_ID = "20260101T000000000000Z"

#: A project whose decisions are CLOSED, not merely accepted: the evidence
#: ids resolve to canonical entries in this document's own catalogue and each
#: decision names its output artifact. Before the closure predicate existed
#: this fixture cited "E1" against an absent catalogue and declared no output
#: artifact, and the gate went GREEN anyway - which is the drift the predicate
#: was added to stop.
DECISIONS = {
    "schema_version": "pcb-decision-units/v1",
    "evidence": [
        {"evidence_id": "E1", "kind": "artifact", "status": "canonical",
         "source": "fabricator stackup report", "path": "docs/stackup.pdf"},
    ],
    "decisions": [
        {"decision_id": "D1", "question": "2 or 4 layers?",
         "status": "accepted", "depends_on": [], "constraints": ["C1"],
         "options": ["2", "4"], "required_evidence": ["stackup report"],
         "invalidation_triggers": ["stackup change"], "evidence_ids": ["E1"],
         "output_artifact": "artifacts/stackup.json"},
        {"decision_id": "D2", "question": "track width?",
         "status": "accepted", "depends_on": ["D1"], "constraints": ["C1"],
         "options": ["0.25"], "required_evidence": ["IPC-2152"],
         "invalidation_triggers": ["current change"], "evidence_ids": ["E1"],
         "output_artifact": "artifacts/track_width.json"},
    ],
}

VERIFIER = '''#!/usr/bin/env python3
"""Stand-in independent verifier: emits the checks/summary envelope."""
import argparse, pathlib, sys
sys.path.insert(0, {tools!r})
from common.findings import board_report, result
from common.report_io import emit, write_report

parser = argparse.ArgumentParser()
parser.add_argument("board")
parser.add_argument("--json", dest="json_path")
args = parser.parse_args()
text = pathlib.Path(args.board).read_text(encoding="utf-8")
report = board_report("fixture_verifier.py", args.board, [
    result("routed_copper_present", text.count("(segment"), [],
           "project contract: the board must carry routed copper"),
])
write_report(report, pathlib.Path(args.json_path) if args.json_path else None)
emit(report)
'''


def have(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


def have_kicad_cli() -> bool:
    return shutil.which("kicad-cli") is not None


REQUIREMENTS = unittest.skipUnless(
    have("pcbnew") and have_kicad_cli(),
    "promotion round trip needs pcbnew (to build the fixture board) and "
    "kicad-cli (to run the DRC gate)")


def build_project(root: Path, *, clean: bool = True,
                  decisions: dict | None = DECISIONS) -> Path:
    from clean_board_fixture import clean_routed_board, dirty_board
    scratch = root / ".scratch" / SCRATCH_ID
    scratch.mkdir(parents=True)
    board = scratch / "board.kicad_pcb"
    (clean_routed_board if clean else dirty_board)(board)
    if decisions is not None:
        (scratch / "decision_units.json").write_text(
            json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
    return scratch


def write_verifier(directory: Path) -> Path:
    path = directory / "fixture_verifier.py"
    path.write_text(VERIFIER.format(tools=str(TOOLS)), encoding="utf-8")
    return path


def run(*args: str) -> tuple[int, dict | None, str]:
    proc = subprocess.run([sys.executable, *args], text=True, encoding="utf-8",
                          capture_output=True, check=False)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


@REQUIREMENTS
class GreenRoundTripTests(unittest.TestCase):
    """One real promotion, then every field the schema demands of it."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="promote-green-")
        root = Path(cls.temp.name)
        cls.project = root / "project"
        cls.project.mkdir()
        build_project(cls.project)
        verifier = write_verifier(root)
        cls.exit_code, cls.report, cls.stderr = run(
            str(PROMOTE), str(cls.project), "--scratch-id", SCRATCH_ID,
            "--verifier", str(verifier), "--apply", "--atomic", "--no-git")
        cls.receipt_path = (Path(cls.report["receipt"])
                            if cls.report else None)
        cls.receipt = (json.loads(cls.receipt_path.read_text(encoding="utf-8"))
                       if cls.receipt_path else None)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    # ------------------------------------------------------------ headline
    def test_promotion_is_green(self):
        self.assertEqual(0, self.exit_code, self.stderr)
        self.assertEqual("GREEN", self.report["status"])

    def test_the_emitted_receipt_validates_green_with_no_root_argument(self):
        """THE acceptance test: producer output feeds the consumer as-is."""
        exit_code, validation, stderr = run(
            str(GATE_RECEIPT), str(self.receipt_path))
        self.assertEqual(0, exit_code, f"{stderr}\n{validation}")
        self.assertEqual("GREEN", validation["effective_status"], validation)
        self.assertEqual([], validation["reason_codes"])
        self.assertEqual([], validation["issues"])

    def test_promote_verified_its_own_receipt(self):
        self.assertEqual(
            {"exit_code": 0, "effective_status": "GREEN", "reason_codes": []},
            self.report["receipt_validation"])

    def test_the_bundle_is_self_contained(self):
        """Every path the receipt names resolves inside the receipt directory."""
        bundle = self.receipt_path.parent
        named = []
        for record in [self.receipt["tool"], *self.receipt["inputs"],
                       *self.receipt["rules"], *self.receipt["child_receipts"]]:
            named.append(record["path"])
        fault = self.receipt["fault_injection"]
        for key in ("fixture", "baseline_artifact", "injected_artifact"):
            named.append(fault[key]["path"])
        for record in (self.receipt["promotion"]["source_artifacts"]
                       + self.receipt["promotion"]["target_artifacts"]):
            named.append(record["path"])
        for relative in named:
            with self.subTest(path=relative):
                self.assertFalse(Path(relative).is_absolute())
                self.assertTrue((bundle / relative).is_file(),
                                f"{relative} missing from the bundle")

    # -------------------------------------------------- field-by-field shape
    def test_schema_version_is_canonical(self):
        self.assertEqual("gate-receipt/v1", self.receipt["schema_version"])

    def test_both_utc_spellings_are_present_and_agree(self):
        """The JSON Schema requires started_utc/finished_utc; the Python
        validator reads start_utc/end_utc. Both are emitted rather than
        picking one and failing the other."""
        self.assertEqual(self.receipt["start_utc"], self.receipt["started_utc"])
        self.assertEqual(self.receipt["end_utc"], self.receipt["finished_utc"])
        for key in ("start_utc", "end_utc", "started_utc", "finished_utc"):
            self.assertTrue(self.receipt[key].endswith("+00:00"),
                            f"{key} is not zero-offset UTC")

    def test_tool_identity_is_complete_and_hash_bound(self):
        tool = self.receipt["tool"]
        for key in ("name", "version", "path", "sha256"):
            self.assertTrue(tool.get(key), f"tool.{key} missing")
        self.assertEqual(64, len(tool["sha256"]))

    def test_argv_is_the_exact_command(self):
        argv = self.receipt["argv"]
        self.assertIn("--apply", argv)
        self.assertIn("--atomic", argv)
        self.assertIn(SCRATCH_ID, argv)

    def test_every_metric_leaf_is_a_finite_number(self):
        def leaves(value, prefix=""):
            if isinstance(value, dict):
                for key in sorted(value):
                    yield from leaves(value[key],
                                      f"{prefix}.{key}" if prefix else key)
            else:
                yield prefix, value
        for name, value in leaves(self.receipt["parsed_metrics"]):
            with self.subTest(metric=name):
                self.assertNotIsInstance(value, bool)
                self.assertIsInstance(value, (int, float))

    def test_every_metric_leaf_has_all_six_semantic_fields(self):
        required = ("definition", "units", "distance_semantics", "region",
                    "sampling_step", "geometry_model")
        semantics = self.receipt["metric_semantics"]
        for metric_id, spec in semantics.items():
            with self.subTest(metric=metric_id):
                for field in required:
                    self.assertTrue(spec.get(field),
                                    f"{metric_id}.{field} is empty")

    def test_semantics_and_metric_leaves_are_one_to_one(self):
        def leaf_ids(value, prefix=""):
            if isinstance(value, dict):
                for key in sorted(value):
                    yield from leaf_ids(value[key],
                                        f"{prefix}.{key}" if prefix else key)
            else:
                yield prefix
        self.assertEqual(set(leaf_ids(self.receipt["parsed_metrics"])),
                         set(self.receipt["metric_semantics"]))

    def test_count_metrics_do_not_claim_to_be_distances(self):
        """The validator rejects a chord model sold as arc-aware; the same
        honesty applies to a count sold as a measurement."""
        for spec in self.receipt["metric_semantics"].values():
            self.assertEqual("count", spec["units"])
            self.assertIn("not_a_distance", spec["distance_semantics"])

    def test_coverage_arithmetic_holds(self):
        coverage = self.receipt["coverage"]
        self.assertLessEqual(coverage["scanned"], coverage["eligible"])
        self.assertEqual(coverage["eligible"],
                         coverage["scanned"] + coverage["excluded"])
        self.assertGreater(coverage["scanned"], 0)
        self.assertTrue(coverage["reason"].strip())

    def test_the_excluded_gate_is_named_with_a_reason(self):
        """No schematic in the scratch copy: ERC is EXCLUDED, not skipped."""
        self.assertEqual(1, self.receipt["coverage"]["excluded"])
        self.assertIn("erc", self.receipt["coverage"]["reason"])
        self.assertIn("no schematic", self.receipt["coverage"]["reason"])
        self.assertNotIn("erc", self.receipt["parsed_metrics"]["gates"])

    def test_retention_is_declared_and_outlives_the_run(self):
        retention = self.receipt["retention"]
        self.assertTrue(retention["policy"])
        self.assertGreater(retention["expires_utc"], self.receipt["end_utc"])
        for item in ("receipt", "hashes", "metrics", "reproducibility_command",
                     "child_receipts", "logs", "scratch"):
            self.assertIn(item, retention["retain"])

    def test_the_drc_child_receipt_carries_the_gate_flags(self):
        child = json.loads(
            (self.receipt_path.parent / "drc_receipt.json").read_text(
                encoding="utf-8"))
        self.assertEqual("drc", child["gate_kind"])
        self.assertIn("--exit-code-violations", child["argv"])
        self.assertIn("--severity-all", child["argv"])
        self.assertEqual("GREEN", child["verdict"])
        self.assertEqual(0, child["parsed_metrics"]["violation_count"])

    def test_child_receipts_are_mandatory_and_hash_bound(self):
        self.assertTrue(self.receipt["child_receipts"])
        for child in self.receipt["child_receipts"]:
            self.assertTrue(child["mandatory"])
            self.assertEqual(64, len(child["sha256"]))

    # ---------------------------------------------------- fault injection
    def test_fault_injection_is_bound_to_the_tool_and_the_baseline(self):
        fault = self.receipt["fault_injection"]
        self.assertEqual("PASS", fault["status"])
        argv = fault["command"]["argv"]
        self.assertIn(self.receipt["tool"]["path"], argv)
        self.assertIn(fault["baseline_artifact"]["path"], argv)
        self.assertEqual(0, fault["command"]["exit_code"])

    def test_the_fault_baseline_is_a_receipt_input(self):
        baseline = self.receipt["fault_injection"]["baseline_artifact"]["sha256"]
        self.assertIn(baseline,
                      {item["sha256"] for item in self.receipt["inputs"]})

    def test_the_injected_observation_differs_from_the_baseline(self):
        fault = self.receipt["fault_injection"]
        self.assertNotEqual(fault["baseline_artifact"]["sha256"],
                            fault["injected_artifact"]["sha256"])

    def test_expected_failures_were_declared_before_the_run(self):
        bundle = self.receipt_path.parent
        definition = json.loads(
            (bundle / "fault_definition.json").read_text(encoding="utf-8"))
        self.assertTrue(definition["declared_before_running"])
        self.assertEqual(definition["expected_failed_metrics"],
                         self.receipt["fault_injection"]["expected_failed_metrics"])

    def test_expected_fault_metrics_are_real_metric_leaves(self):
        fault = self.receipt["fault_injection"]
        metrics = self.receipt["parsed_metrics"]
        for metric_id in fault["expected_failed_metrics"]:
            node = metrics
            for part in metric_id.split("."):
                node = node[part]
            self.assertIsInstance(node, int)

    # -------------------------------------------------------- promotion
    def test_promotion_block_proves_the_canonical_write(self):
        promotion = self.receipt["promotion"]
        self.assertEqual("GREEN", promotion["decision"])
        self.assertEqual([], promotion["reason_codes"])
        self.assertTrue(promotion["atomic"])
        self.assertIn("per_file", promotion["atomicity"])
        self.assertTrue(promotion["source_artifacts"])
        self.assertTrue(promotion["target_artifacts"])

    def test_the_canonical_board_really_matches_the_scratch_board(self):
        promotion = self.receipt["promotion"]
        source = promotion["source_artifacts"][0]["sha256"]
        target = promotion["target_artifacts"][0]["after_sha256"]
        self.assertEqual(source, target)
        canonical = Path(promotion["target_artifacts"][0]["canonical_path"])
        self.assertTrue(canonical.is_file())

    def test_archive_digests_are_still_emitted(self):
        """board/ruleset/command digests, kept from the archive receipt."""
        for key in ("board_sha256", "ruleset_sha256", "command_sha256"):
            self.assertEqual(64, len(self.receipt[key]), key)

    def test_the_ruleset_digest_is_derived_from_a_listed_manifest(self):
        manifest = json.loads(
            (self.receipt_path.parent / "ruleset.json").read_text(
                encoding="utf-8"))
        self.assertEqual(manifest["ruleset_sha256"],
                         self.receipt["ruleset_sha256"])
        self.assertIn("entries", manifest)


@REQUIREMENTS
class RedPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="promote-red-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.verifier = write_verifier(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def promote(self, *extra: str):
        return run(str(PROMOTE), str(self.project), "--scratch-id", SCRATCH_ID,
                   "--verifier", str(self.verifier), "--no-git", *extra)

    def assert_receipt_red(self, report):
        exit_code, validation, stderr = run(
            str(GATE_RECEIPT), str(report["receipt"]))
        self.assertEqual(1, exit_code, f"{stderr}\n{validation}")
        self.assertEqual("RED", validation["effective_status"], validation)
        return validation

    def test_a_dirty_board_is_red_and_the_receipt_agrees(self):
        build_project(self.project, clean=False)
        exit_code, report, _stderr = self.promote()
        self.assertEqual(1, exit_code)
        self.assertEqual("RED", report["status"])
        self.assertIn("DRC_NOT_CLEAN", report["reason_codes"])
        validation = self.assert_receipt_red(report)
        self.assertIn("MANDATORY_CHILD_RED", validation["reason_codes"])

    def test_an_open_decision_is_red(self):
        decisions = copy.deepcopy(DECISIONS)
        decisions["decisions"][1]["status"] = "ready"
        build_project(self.project, decisions=decisions)
        exit_code, report, _stderr = self.promote()
        self.assertEqual(1, exit_code)
        self.assertIn("DECISIONS_OPEN", report["reason_codes"])
        self.assertEqual(1, report["blockers"][0]["message"].count("D2"))
        self.assert_receipt_red(report)

    def test_an_accepted_decision_without_evidence_is_red(self):
        """`accepted` is a human's decision; the gate passes on `closed`.

        This exact document used to promote GREEN: the gate read `status` and
        nothing else, so a decision could be hand-marked accepted with no
        evidence at all and the promotion looked clean.
        """
        decisions = copy.deepcopy(DECISIONS)
        decisions["evidence"] = []
        build_project(self.project, decisions=decisions)
        exit_code, report, _stderr = self.promote()
        self.assertEqual(1, exit_code)
        self.assertIn("DECISIONS_ACCEPTED_NOT_CLOSED", report["reason_codes"])
        self.assertNotIn("DECISIONS_OPEN", report["reason_codes"],
                         "these decisions are not open, they are unclosed")
        message = next(b["message"] for b in report["blockers"]
                       if b["code"] == "DECISIONS_ACCEPTED_NOT_CLOSED")
        self.assertIn("EVIDENCE_NOT_IN_CATALOGUE", message)
        self.assert_receipt_red(report)

    def test_the_receipt_carries_the_decision_debt_vector(self):
        decisions = copy.deepcopy(DECISIONS)
        decisions["decisions"][1]["output_artifact"] = ""
        build_project(self.project, decisions=decisions)
        _exit, report, _stderr = self.promote()
        receipt = json.loads(Path(report["receipt"]).read_text(encoding="utf-8"))
        metrics = receipt["parsed_metrics"]["gates"]["decisions"]
        self.assertEqual(1, metrics["closed"])
        self.assertEqual(1, metrics["accepted_but_not_closed"])
        self.assertEqual(1, metrics["accepted_output_artifact_missing"])
        self.assertEqual(0, metrics["accepted_evidence_missing"])
        for key in metrics:
            self.assertNotIn("score", key,
                             "6.9 forbids a weighted decision-debt score")

    def test_no_verifier_is_no_coverage_not_a_pass(self):
        build_project(self.project)
        exit_code, report, _stderr = run(
            str(PROMOTE), str(self.project), "--scratch-id", SCRATCH_ID,
            "--no-git")
        self.assertEqual(1, exit_code)
        self.assertEqual("NO_COVERAGE", report["gates"]["contract"])
        self.assertIn("CONTRACT_NO_COVERAGE", report["reason_codes"])
        self.assert_receipt_red(report)

    def test_a_project_with_no_decisions_is_blocked_not_skipped(self):
        """The archive printed 'bu kapi atlaniyor' and continued."""
        build_project(self.project, decisions=None)
        exit_code, report, _stderr = self.promote()
        self.assertEqual(1, exit_code)
        self.assertIn("NO_DECISION_UNITS", report["reason_codes"])
        codes = {b["code"] for b in report["blockers"]}
        self.assertIn("FAULT_INJECTION_NOT_PROVEN", codes,
                      "a project with no accepted decision cannot prove the "
                      "decision gate works, so A level must not go green")
        self.assert_receipt_red(report)

    def test_a_red_promotion_leaves_the_canonical_board_untouched(self):
        build_project(self.project, clean=False)
        from clean_board_fixture import clean_routed_board
        canonical = self.project / "board.kicad_pcb"
        clean_routed_board(canonical)
        before = canonical.read_bytes()
        exit_code, report, _stderr = self.promote("--apply", "--atomic")
        self.assertEqual(1, exit_code)
        self.assertEqual(before, canonical.read_bytes())
        promotion = json.loads(
            Path(report["receipt"]).read_text(encoding="utf-8"))["promotion"]
        self.assertEqual("RED", promotion["decision"])
        self.assertTrue(promotion["reason_codes"])
        for target in promotion["target_artifacts"]:
            self.assertEqual(target["before_sha256"], target["after_sha256"],
                             "a RED promotion must prove it wrote nothing")

    def test_assurance_level_b_skips_fault_injection(self):
        build_project(self.project, decisions=None)
        _exit, report, _stderr = self.promote("--assurance-level", "B")
        receipt = json.loads(Path(report["receipt"]).read_text(encoding="utf-8"))
        self.assertNotIn("fault_injection", receipt)
        self.assertEqual("B", receipt["assurance_level"])


class CliContractTests(unittest.TestCase):
    """No KiCad needed: argument handling and the self-test."""

    def test_missing_positional_exits_2(self):
        exit_code, _report, stderr = run(str(PROMOTE))
        self.assertEqual(2, exit_code)
        self.assertIn("PROJECT_DIR is required", stderr)

    def test_apply_without_atomic_exits_2(self):
        exit_code, _report, stderr = run(str(PROMOTE), "/nonexistent", "--apply")
        self.assertEqual(2, exit_code)
        self.assertIn("--atomic", stderr)

    def test_a_missing_project_directory_exits_2(self):
        exit_code, report, _stderr = run(str(PROMOTE), "/nonexistent-project")
        self.assertEqual(2, exit_code)
        self.assertEqual("ERROR", report["status"])

    def test_self_test_passes_and_is_opt_in(self):
        exit_code, report, _stderr = run(str(PROMOTE), "--self-test")
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", report["self_test"], report)

    def test_the_fault_harness_refuses_to_claim_an_unprovable_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "decision_units.json"
            baseline.write_text(json.dumps(
                {"schema_version": "pcb-decision-units/v1", "decisions": []}),
                encoding="utf-8")
            exit_code, report, _stderr = run(
                str(PROMOTE), "--fault-inject-decisions", str(baseline))
            self.assertEqual(1, exit_code)
            self.assertEqual("NO_COVERAGE", report["status"])
            self.assertEqual([], report["observed_failed_metrics"])

    def test_the_fault_harness_fires_on_a_real_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "decision_units.json"
            baseline.write_text(json.dumps(DECISIONS), encoding="utf-8")
            exit_code, report, _stderr = run(
                str(PROMOTE), "--fault-inject-decisions", str(baseline))
            self.assertEqual(0, exit_code)
            self.assertEqual("PASS", report["status"])
            self.assertEqual({"gates.decisions.accepted",
                              "gates.decisions.closed",
                              "gates.decisions.unaccepted"},
                             set(report["observed_failed_metrics"]))
            self.assertEqual(baseline.read_text(encoding="utf-8"),
                             json.dumps(DECISIONS),
                             "the harness must not modify the baseline file")



class ScratchIntegrationTests(unittest.TestCase):
    """promote.py must use common/scratch.py, not a second copy of it."""

    def test_the_shared_scratch_module_is_the_one_in_use(self):
        sys.path.insert(0, str(TOOLS))
        import common.scratch as scratch
        import importlib.util
        spec = importlib.util.spec_from_file_location("promote_under_test",
                                                      PROMOTE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scratch.scratch_root(tmp),
                             module.scratch_root(Path(tmp)))
            self.assertEqual(scratch.list_scratches(tmp),
                             module.list_scratches(Path(tmp)))

    def test_a_path_outside_the_scratch_root_is_refused(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("promote_under_test2",
                                                      PROMOTE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(module.PromotionError):
                module.assert_inside_scratch(Path(tmp), tmp)



if __name__ == "__main__":
    unittest.main()
