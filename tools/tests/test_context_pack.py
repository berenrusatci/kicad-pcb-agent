#!/usr/bin/env python3
import hashlib
import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

SCRIPT = SCRIPTS / "context_pack.py"


class _ContextPackFixture:
    """Manifest + artifact scaffolding shared by the test classes below.

    Deliberately NOT a TestCase: subclassing a populated TestCase to reuse its
    setUp re-runs every one of its test methods under the subclass's name too,
    which doubles the runtime and reports each result twice.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write_json("intent.json", {
            "schema_version": "pcb-design-intent/v1", "goals": ["camera board"],
            "constraints": [{"id": "C1", "statement": "One CSI port", "source": "datasheet"}],
            "non_goals": ["PCB routing"], "open_questions": []
        })
        self.write_json("decision.json", {
            "schema_version": "pcb-decision-units/v1", "evidence": [], "decisions": [{
                "decision_id": "D1", "question": "Which bridge?", "owner_skill": "pcb-orchestrator",
                "status": "ready", "depends_on": [], "constraints": ["C1"],
                "options": ["fpga", "multi-soc"], "required_evidence": ["datasheet"],
                "evidence_ids": [], "output_artifact": "decisions/D1.json", "invalidation_triggers": []
            }]
        })
        self.write_json("tools.json", {
            "schema_version": "pcb-tool-registry/v1", "tools": [{
                "name": "web", "kind": "builtin", "locator": "web", "version": "runtime",
                "modes": ["read"], "available": True, "preconditions": []
            }]
        })
        (self.root / "verify.py").write_text("import sys\nraise SystemExit(0)\n", encoding="utf-8")
        self.manifest = {
            "schema_version": "pcb-context-pack/v1",
            "task": {
                "id": "T1", "objective": "Select bridge architecture", "scope": ["CSI bridge"],
                "non_goals": ["PCB routing"], "decision_ids": ["D1"], "owner_skill": "pcb-orchestrator",
                "mutation_policy": "read_only", "allowed_actions": ["read", "research"],
                "forbidden_actions": ["write board"]
            },
            "budget": {"max_files": 3, "max_bytes": 8192},
            "artifacts": [
                self.artifact("design_intent", "intent.json"),
                self.artifact("decision_unit", "decision.json"),
                self.artifact("tool_registry", "tools.json")
            ],
            "constraints": [{"id": "C1", "statement": "One CSI port", "source": "datasheet"}],
            "open_questions": [],
            "tools": [{"name": "web", "purpose": "official docs", "mode": "read", "preconditions": []}],
            "output_contract": {
                "artifact_path": "out.json", "schema_version": "decision-result/v1",
                "required_claim_fields": ["claim_id", "statement", "status", "evidence_ids", "confidence", "scope"]
            },
            "gates": [{
                "id": "G1", "validator": {"path": "verify.py", "sha256": self.digest("verify.py")},
                "argv": ["python3", "verify.py", "out.json"], "expected_exit": 0
            }]
        }

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, name, value):
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def digest(self, name):
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def artifact(self, role, name, required=True):
        return {
            "role": role, "path": name, "sha256": self.digest(name), "required": required,
            "purpose": f"Provide {role} for D1", "decision_ids": ["D1"]
        }

    def refresh(self, role, name):
        for i, artifact in enumerate(self.manifest["artifacts"]):
            if artifact["role"] == role:
                self.manifest["artifacts"][i] = self.artifact(role, name)
                return

    def run_manifest(self, postflight=False):
        self.write_json("manifest.json", self.manifest)
        argv = ["python3", str(SCRIPT), str(self.root / "manifest.json"), "--root", str(self.root)]
        if postflight:
            argv.append("--postflight")
        proc = subprocess.run(argv, text=True, encoding="utf-8", capture_output=True)
        return proc.returncode, json.loads(proc.stdout)

class ContextPackTest(_ContextPackFixture, unittest.TestCase):
    def test_ready(self):
        code, report = self.run_manifest()
        self.assertEqual((0, "READY"), (code, report["verdict"]))

    def test_hash_fault_blocks(self):
        self.write_json("decision.json", {"changed": True})
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any(x.startswith("STALE_ARTIFACT") for x in report["errors"]))

    def test_blocker_downgrade_blocks(self):
        intent = json.loads((self.root / "intent.json").read_text())
        intent["open_questions"] = [{"id": "Q1", "question": "Moving scene?", "blocking": True, "owner": "user"}]
        self.write_json("intent.json", intent); self.refresh("design_intent", "intent.json")
        self.manifest["open_questions"] = [{"id": "Q1", "question": "Moving scene?", "blocking": False, "owner": "user"}]
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertIn("INTENT_QUESTION_MISMATCH:Q1", report["errors"])

    def test_duplicate_core_role_is_error(self):
        self.write_json("intent2.json", json.loads((self.root / "intent.json").read_text()))
        self.manifest["budget"]["max_files"] = 4
        self.manifest["artifacts"].append(self.artifact("design_intent", "intent2.json"))
        code, report = self.run_manifest()
        self.assertEqual((2, "ERROR"), (code, report["verdict"]))

    def test_accepted_decision_without_evidence_is_blocked(self):
        doc = json.loads((self.root / "decision.json").read_text())
        doc["decisions"][0]["status"] = "accepted"
        self.write_json("decision.json", doc); self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any("accepted decision lacks evidence" in x for x in report["errors"]))

    def test_accepted_decision_with_fake_evidence_is_blocked(self):
        doc = json.loads((self.root / "decision.json").read_text())
        doc["decisions"][0].update({"status": "accepted", "evidence_ids": ["FAKE"], "invalidation_triggers": ["input change"]})
        self.write_json("decision.json", doc); self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any("references missing evidence" in x for x in report["errors"]))

    def test_accepted_decision_with_unbound_evidence_is_blocked(self):
        doc = json.loads((self.root / "decision.json").read_text())
        doc["evidence"] = [{"evidence_id": "E1", "source": "invented datasheet", "status": "canonical"}]
        doc["decisions"][0].update({"status": "accepted", "evidence_ids": ["E1"], "invalidation_triggers": ["input change"]})
        self.write_json("decision.json", doc); self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any("evidence E1.kind" in x for x in report["errors"]))

    def test_accepted_but_not_closed_is_named_as_such(self):
        doc = json.loads((self.root / "decision.json").read_text())
        doc["decisions"][0]["status"] = "accepted"
        self.write_json("decision.json", doc); self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any("accepted decision is not closed" in x
                            for x in report["errors"]), report["errors"])

    def test_a_closed_decision_is_accepted_by_the_pack(self):
        doc = json.loads((self.root / "decision.json").read_text())
        self.write_json("evidence.json", {"measured": 1})
        doc["evidence"] = [{"evidence_id": "E1", "kind": "artifact",
                            "source": "measurement", "status": "canonical",
                            "path": "evidence.json",
                            "sha256": self.digest("evidence.json")}]
        doc["decisions"][0].update({"status": "accepted", "evidence_ids": ["E1"],
                                    "invalidation_triggers": ["input change"]})
        self.write_json("decision.json", doc); self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual(0, code, report["errors"])

    def test_the_pack_and_the_promotion_gate_share_one_closure_verdict(self):
        """These two used to disagree: the gate accepted evidence-free
        decisions the pack rejected, so one document passed one tool and
        failed the other. They must now answer from the same function."""
        sys.path.insert(0, str(SCRIPTS.parent))
        from common.decisions import decision_gate, parse_decision_document

        doc = json.loads((self.root / "decision.json").read_text())
        for status in ("ready", "accepted"):
            with self.subTest(status=status):
                doc["decisions"][0]["status"] = status
                units, evidence = parse_decision_document(doc)
                gate = decision_gate(units, evidence)
                self.write_json("decision.json", doc)
                self.refresh("decision_unit", "decision.json")
                code, _report = self.run_manifest()
                # The pack refuses exactly what the gate calls unclosed-but-
                # accepted; an OPEN decision is legitimate context, not an error.
                self.assertEqual(bool(gate["accepted_but_not_closed"]),
                                 code != 0)

    def test_decision_constraint_must_be_in_manifest(self):
        intent = json.loads((self.root / "intent.json").read_text())
        intent["constraints"].append({"id": "C2", "statement": "Do not route", "source": "user"})
        self.write_json("intent.json", intent); self.refresh("design_intent", "intent.json")
        self.manifest["constraints"] = [{"id": "C2", "statement": "Do not route", "source": "user"}]
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertIn("DECISION_CONSTRAINT_MISSING:D1:C1", report["errors"])

    def test_decision_cycle_is_blocked(self):
        doc = json.loads((self.root / "decision.json").read_text())
        doc["decisions"][0]["depends_on"] = ["D1"]
        self.write_json("decision.json", doc); self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any("dependency cycle" in x for x in report["errors"]))

    def test_unregistered_tool_blocks(self):
        self.manifest["tools"][0]["name"] = "made_up_tool"
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertIn("UNREGISTERED_TOOL:made_up_tool", report["errors"])

    def test_unrelated_artifact_decision_blocks(self):
        self.manifest["artifacts"][0]["decision_ids"] = ["OTHER"]
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any(x.startswith("ARTIFACT_UNRELATED_DECISION") for x in report["errors"]))

    def test_optional_core_blocks(self):
        self.manifest["artifacts"][0]["required"] = False
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertIn("CORE_ARTIFACT_NOT_REQUIRED:design_intent", report["errors"])

    def test_invalid_utf8_is_structured_error(self):
        (self.root / "intent.json").write_bytes(b"\xff\xfe")
        self.refresh("design_intent", "intent.json")
        code, report = self.run_manifest()
        self.assertEqual((1, "BLOCKED"), (code, report["verdict"]))
        self.assertTrue(any(x.startswith("INVALID_CORE_ARTIFACT") for x in report["errors"]))

    def test_bool_budget_is_error(self):
        self.manifest["budget"]["max_bytes"] = True
        code, report = self.run_manifest()
        self.assertEqual((2, "ERROR"), (code, report["verdict"]))

    def test_argv_item_type_is_error(self):
        self.manifest["gates"][0]["argv"] = ["python3", {}]
        code, report = self.run_manifest()
        self.assertEqual((2, "ERROR"), (code, report["verdict"]))

    def test_path_escape_is_error(self):
        self.manifest["artifacts"][0]["path"] = "../intent.json"
        code, report = self.run_manifest()
        self.assertEqual((2, "ERROR"), (code, report["verdict"]))

    def test_output_path_escape_is_error(self):
        self.manifest["output_contract"]["artifact_path"] = "../out.json"
        code, report = self.run_manifest()
        self.assertEqual((2, "ERROR"), (code, report["verdict"]))

    def test_valid_postflight(self):
        self.write_json("evidence.json", {"measured": 1})
        self.write_json("out.json", {
            "schema_version": "decision-result/v1",
            "evidence": [{"evidence_id": "E1", "kind": "artifact", "source": "measurement", "status": "canonical", "path": "evidence.json", "sha256": self.digest("evidence.json")}],
            "claims": [{"claim_id": "CL1", "statement": "One CSI port", "status": "accepted", "evidence_ids": ["E1"], "confidence": "high", "scope": "selected SoC"}]
        })
        code, report = self.run_manifest(postflight=True)
        self.assertEqual((0, "ACCEPTED"), (code, report["verdict"]))
        self.assertEqual(0, report["gate_results"][0]["exit_code"])

    def test_postflight_claim_without_evidence_rejected(self):
        self.write_json("out.json", {
            "schema_version": "decision-result/v1", "evidence": [],
            "claims": [{"claim_id": "CL1", "statement": "Unsupported", "status": "accepted", "evidence_ids": [], "confidence": "high", "scope": "board"}]
        })
        code, report = self.run_manifest(postflight=True)
        self.assertEqual((1, "REJECTED"), (code, report["verdict"]))
        self.assertIn("CLAIM_WITHOUT_EVIDENCE:CL1", report["errors"])

    def test_gate_failure_rejects_postflight(self):
        (self.root / "verify.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        self.manifest["gates"][0]["validator"]["sha256"] = self.digest("verify.py")
        self.write_json("out.json", {"schema_version": "decision-result/v1", "evidence": [], "claims": [{"claim_id": "CL1", "statement": "Open", "status": "unverified", "evidence_ids": [], "confidence": "low", "scope": "board"}]})
        code, report = self.run_manifest(postflight=True)
        self.assertEqual((1, "REJECTED"), (code, report["verdict"]))
        self.assertIn("GATE_EXIT_MISMATCH:G1", report["errors"])

    def test_gate_validator_must_be_real_entrypoint(self):
        self.manifest["gates"][0]["argv"] = [
            "python3", "-c", "import sys; sys.exit(0)", "verify.py", "out.json"
        ]
        code, report = self.run_manifest()
        self.assertEqual((1, "BLOCKED"), (code, report["verdict"]))
        self.assertIn("GATE_ARGV_VALIDATOR_NOT_ENTRYPOINT:G1", report["errors"])


class ByteBudgetExemptionTest(_ContextPackFixture, unittest.TestCase):
    """The byte budget bounds the DECISION context, not the toolchain.

    config/tool_registry.json is generated, grows by roughly 574 bytes per
    tool, and was 97% of the real fixture's pack. Counting it against the same
    limit made the budget track how many tools exist: it was raised
    20000 -> 40000 once for that reason and filled again at 67 tools. It is
    now exempt from the BYTE budget only - it is still hash-bound, still
    required, and still counted in the reported totals.
    """

    def _inflate(self, name, entries):
        """Grow the tool registry without changing its shape."""
        self.write_json(name, {
            "schema_version": "pcb-tool-registry/v1",
            "tools": [{
                "name": "web", "kind": "builtin", "locator": "web",
                "version": "runtime", "modes": ["read"], "available": True,
                "preconditions": [],
            }] + [{
                # builtin, not local: a local tool must carry a sha256, and
                # the point here is registry SIZE, not registry content.
                "name": f"filler-{i}", "kind": "builtin",
                "locator": f"filler-{i}", "version": "runtime",
                "modes": ["read"], "available": True,
                "preconditions": ["x" * 200],
            } for i in range(entries)],
        })
        self.refresh("tool_registry", name)

    def test_an_oversized_registry_does_not_trip_the_budget(self):
        self._inflate("tools.json", 200)
        self.assertGreater((self.root / "tools.json").stat().st_size,
                           self.manifest["budget"]["max_bytes"])
        code, report = self.run_manifest()
        self.assertEqual((0, "READY"), (code, report["verdict"]), report)
        self.assertNotIn("BYTE_BUDGET_EXCEEDED", report["errors"])

    def test_an_oversized_decision_artifact_still_trips_the_budget(self):
        """The limit must still bite on the thing it was written for."""
        decision = json.loads((self.root / "decision.json").read_text())
        decision["padding"] = "x" * 20000
        self.write_json("decision.json", decision)
        self.refresh("decision_unit", "decision.json")
        code, report = self.run_manifest()
        self.assertEqual((1, "BLOCKED"), (code, report["verdict"]))
        self.assertIn("BYTE_BUDGET_EXCEEDED", report["errors"])

    def test_the_report_separates_budgeted_from_exempt_bytes(self):
        self._inflate("tools.json", 50)
        _code, report = self.run_manifest()
        budget = report["budget"]
        self.assertEqual(budget["bytes"],
                         budget["budgeted_bytes"] + budget["exempt_bytes"])
        self.assertEqual(["tool_registry"], budget["exempt_roles"])
        self.assertGreater(budget["exempt_bytes"], budget["budgeted_bytes"])

    def test_the_registry_is_still_bound_by_hash(self):
        """Exempt from the BYTE budget is not exempt from integrity."""
        self.write_json("tools.json", {"schema_version": "pcb-tool-registry/v1",
                                       "tools": []})
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any(x.startswith("STALE_ARTIFACT")
                            for x in report["errors"]), report["errors"])

    def test_the_registry_is_still_a_required_core_artifact(self):
        self.manifest["artifacts"] = [
            a for a in self.manifest["artifacts"]
            if a["role"] != "tool_registry"
        ]
        code, report = self.run_manifest()
        self.assertEqual(1, code)
        self.assertTrue(any("tool_registry" in x for x in report["errors"]),
                        report["errors"])


if __name__ == "__main__":
    unittest.main()
