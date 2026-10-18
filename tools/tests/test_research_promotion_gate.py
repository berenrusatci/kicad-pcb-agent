#!/usr/bin/env python3
"""Regression and fault-injection tests for research_promotion_gate.py."""
from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SCRIPTS  # noqa: E402

HERE = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "research_promotion_gate.py"


class ResearchPromotionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="research-promotion-")
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "_research").mkdir(parents=True)
        (self.vault / "Skills").mkdir()
        (self.vault / "Skills" / "references").mkdir()
        (self.vault / "Config").mkdir()
        (self.vault / "Skills" / "SKILL-orchestrator.md").write_text(
            "# Orchestrator\n", encoding="utf-8")
        self.validation_fixture = self.vault / "Skills" / "references" / "validation.json"
        self.validation_fixture.write_text('{"fixture":"positive"}\n', encoding="utf-8")
        script_sha = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        (self.vault / "Config" / "tool_registry.json").write_text(json.dumps({
            "schema_version": "pcb-tool-registry/v1",
            "tools": [{
                "name": "research-promotion-gate",
                "kind": "local",
                "locator": str(SCRIPT),
                "version": "pcb-research-promotion-report/v2",
                "sha256": script_sha,
            }],
        }), encoding="utf-8")
        self.claim_id = "TEST-C1"
        self.write_logs(include_decision=True, include_evidence=True)
        self.write_note(self.good_source())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def good_source(self) -> str:
        return (
            "[RFC 8785 — JSON Canonicalization Scheme, June 2020, "
            "§3.1](https://www.rfc-editor.org/rfc/rfc8785.html)"
            " · erişim 2026-08-03"
        )

    def good_validation(self) -> str:
        digest = hashlib.sha256(self.validation_fixture.read_bytes()).hexdigest()
        return f"fixture: Skills/references/validation.json sha256:{digest}"

    def write_logs(self, *, include_decision: bool, include_evidence: bool) -> None:
        decision_value = "KABUL" if include_decision else "ADAY"
        decision_claim = self.claim_id if include_decision else "OTHER"
        evidence_value = self.claim_id if include_evidence else "OTHER"
        decision = (
            "| Tarih | Claim ID | Karar | Gerekçe |\n|---|---|---|---|\n"
            f"| 2026-08-03 | {decision_claim} | {decision_value} | test |\n"
        )
        evidence = (
            "| Claim ID | Kaynak | Hedef |\n|---|---|---|\n"
            f"| {evidence_value} | source | target |\n"
        )
        (self.vault / "02-Karar-Gunlugu.md").write_text(decision, encoding="utf-8")
        (self.vault / "03-Kanit-ve-Kaynak-Haritasi.md").write_text(
            evidence, encoding="utf-8")

    def write_note(self, source: str, *, level_loop: str = "C / L1",
                   claim_id: str | None = None,
                   validation: str | None = None) -> None:
        claim_id = claim_id or self.claim_id
        validation = self.good_validation() if validation is None else validation
        content = f"""---
tags: [research]
durum: kabul-edildi
guncelleme: 2026-08-03
---

# Test research

Erişim tarihi: 2026-08-03.

| claim_id | İddia | Kaynak | Güven | Kapsam | Doğrulama | Hedef | Karar | Seviye/Döngü |
|---|---|---|---|---|---|---|---|---|
| {claim_id} | Canonical JSON gerekir. | {source} | yüksek | receipt | {validation} | `SKILL-orchestrator` | KABUL | {level_loop} |
"""
        (self.vault / "_research" / "R-test.md").write_text(content, encoding="utf-8")

    def run_gate(self, *extra: str):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault), *extra],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertTrue(proc.stdout, proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def test_complete_accepted_claim_is_green(self):
        code, report = self.run_gate()
        self.assertEqual(0, code, report)
        self.assertEqual("GREEN", report["effective_status"])
        self.assertEqual(1, report["summary"]["accepted_claims"])
        self.assertEqual("pcb-research-promotion-report/v2", report["schema_version"])
        self.assertEqual(hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                         report["receipt"]["tool"]["sha256"])
        self.assertTrue(report["receipt"]["argv"])
        self.assertEqual(3, len(report["receipt"]["inputs"]))

    def test_missing_source_locator_fault_is_red(self):
        # Fault injection: the same accepted claim loses only its section/page
        # locator.  The gate must reject it, proving the check is not empty.
        self.write_note(
            "[RFC 8785 — JSON Canonicalization Scheme, June 2020]"
            "(https://www.rfc-editor.org/rfc/rfc8785.html)"
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertEqual("RED", report["effective_status"])
        self.assertIn("MISSING_SOURCE_LOCATOR",
                      {item["code"] for item in report["issues"]})

    def test_access_date_is_not_document_revision_fault(self):
        # Fault injection: a precise locator and access date must not disguise
        # an undated/unnumbered document as a revision-qualified source.
        self.write_note(
            "[Official document — §3.1]"
            "(https://www.rfc-editor.org/example) · erişim 2026-08-03"
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_DOCUMENT_ID_OR_REVISION",
                      {item["code"] for item in report["issues"]})

    def test_missing_decision_log_fault_is_red(self):
        self.write_logs(include_decision=False, include_evidence=True)
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_DECISION_LOG_ENTRY",
                      {item["code"] for item in report["issues"]})

    def test_missing_evidence_map_fault_is_red(self):
        self.write_logs(include_decision=True, include_evidence=False)
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_EVIDENCE_MAP_ENTRY",
                      {item["code"] for item in report["issues"]})

    def test_missing_level_and_loop_fault_is_red(self):
        self.write_note(self.good_source(), level_loop="belirsiz")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("MISSING_VERIFICATION_LEVEL", codes)
        self.assertIn("MISSING_VERIFICATION_LOOP", codes)

    def test_policy_age_fault_is_stale(self):
        path = self.vault / "_research" / "R-test.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "erişim 2026-08-03", "erişim 2026-01-01"),
            encoding="utf-8",
        )
        code, report = self.run_gate("--max-source-age-days", "0")
        self.assertEqual(1, code, report)
        self.assertEqual("STALE", report["effective_status"])
        self.assertIn("SOURCE_AGE_EXCEEDED",
                      {item["code"] for item in report["issues"]})

    def test_candidate_defects_are_advisory_not_promotion(self):
        path = self.vault / "_research" / "R-test.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("durum: kabul-edildi", "durum: taslak")
        text = text.replace("| KABUL |", "| ADAY |")
        text = text.replace(self.good_source(), "kaynak yok")
        path.write_text(text, encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(0, code, report)
        self.assertEqual("GREEN", report["effective_status"])
        self.assertGreater(report["summary"]["advisories"], 0)

    def test_duplicate_claim_id_fault_is_red(self):
        original = (self.vault / "_research" / "R-test.md").read_text(encoding="utf-8")
        (self.vault / "_research" / "R-duplicate.md").write_text(
            original, encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("DUPLICATE_CLAIM_ID",
                      {item["code"] for item in report["issues"]})

    def test_note_wide_access_date_does_not_cover_claim(self):
        self.write_note(
            "[RFC 8785 — JSON Canonicalization Scheme, June 2020, §3.1]"
            "(https://www.rfc-editor.org/rfc/rfc8785.html)"
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_ACCESS_DATE",
                      {item["code"] for item in report["issues"]})

    def test_claim_id_substring_does_not_satisfy_logs(self):
        other = self.claim_id + "0"
        (self.vault / "02-Karar-Gunlugu.md").write_text(
            "| Tarih | Karar |\n|---|---|\n"
            f"| 2026-08-03 | {other} KABUL |\n", encoding="utf-8")
        (self.vault / "03-Kanit-ve-Kaynak-Haritasi.md").write_text(
            "| Claim ID | Kaynak |\n|---|---|\n"
            f"| {other} | source |\n", encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("MISSING_DECISION_LOG_ENTRY", codes)
        self.assertIn("MISSING_EVIDENCE_MAP_ENTRY", codes)

    def test_claim_mentioned_in_other_evidence_cell_is_red(self):
        (self.vault / "03-Kanit-ve-Kaynak-Haritasi.md").write_text(
            "| Claim ID | Kaynak | Hedef |\n|---|---|---|\n"
            f"| OTHER | discussion {self.claim_id} | target |\n",
            encoding="utf-8",
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_EVIDENCE_MAP_ENTRY",
                      {item["code"] for item in report["issues"]})

    def test_claim_mentioned_in_other_decision_reason_is_red(self):
        (self.vault / "02-Karar-Gunlugu.md").write_text(
            "| Tarih | Claim ID | Karar | Gerekçe |\n|---|---|---|---|\n"
            f"| 2026-08-03 | OTHER | KABUL | discussion {self.claim_id} |\n",
            encoding="utf-8",
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_DECISION_LOG_ENTRY",
                      {item["code"] for item in report["issues"]})

    def test_negative_decision_log_phrase_does_not_accept_claim(self):
        (self.vault / "02-Karar-Gunlugu.md").write_text(
            "| Tarih | Karar |\n|---|---|\n"
            f"| 2026-08-03 | {self.claim_id} KABUL DEĞİL |\n",
            encoding="utf-8",
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MISSING_DECISION_LOG_ENTRY",
                      {item["code"] for item in report["issues"]})

    def test_negative_acceptance_phrase_is_not_accepted(self):
        path = self.vault / "_research" / "R-test.md"
        text = path.read_text(encoding="utf-8").replace(
            "| KABUL |", "| KABUL DEĞİL |")
        path.write_text(text, encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(2, code, report)
        self.assertIn("UNKNOWN_DECISION_STATUS",
                      {item["code"] for item in report["issues"]})

    def test_malformed_claim_row_is_red(self):
        path = self.vault / "_research" / "R-test.md"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n| BROKEN-C2 | too | few | cells |\n")
        # The appended row is outside the table, so inject inside it instead.
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "| TEST-C1 | Canonical JSON gerekir.",
            "| BROKEN-C2 | too | few |\n| TEST-C1 | Canonical JSON gerekir.",
        )
        path.write_text(text, encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("MALFORMED_CLAIM_ROW",
                      {item["code"] for item in report["issues"]})

    def test_accepted_then_candidate_duplicate_is_red(self):
        original = (self.vault / "_research" / "R-test.md").read_text(
            encoding="utf-8")
        candidate = original.replace("durum: kabul-edildi", "durum: taslak")
        candidate = candidate.replace("| KABUL |", "| ADAY |")
        (self.vault / "_research" / "R-z-candidate.md").write_text(
            candidate, encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        duplicate = [item for item in report["issues"]
                     if item["code"] == "DUPLICATE_CLAIM_ID"]
        self.assertEqual("RED", duplicate[0]["status"])

    def test_partial_target_resolution_is_red(self):
        path = self.vault / "_research" / "R-test.md"
        text = path.read_text(encoding="utf-8").replace(
            "`SKILL-orchestrator`", "`SKILL-orchestrator`, `missing.py`")
        path.write_text(text, encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("TARGET_NOT_FOUND",
                      {item["code"] for item in report["issues"]})

    def test_invalid_and_future_access_dates_are_blocking(self):
        path = self.vault / "_research" / "R-test.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("erişim 2026-08-03", "erişim 2026-99-99"),
                        encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(2, code, report)
        self.assertIn("INVALID_ACCESS_DATE",
                      {item["code"] for item in report["issues"]})
        path.write_text(original.replace("erişim 2026-08-03", "erişim 2099-01-01"),
                        encoding="utf-8")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("FUTURE_ACCESS_DATE",
                      {item["code"] for item in report["issues"]})

    def test_access_date_is_bound_to_access_token(self):
        self.write_note(
            "[RFC 8785, updated 2026-01-01, §3.1]"
            "(https://www.rfc-editor.org/rfc/rfc8785.html) "
            "· erişim 2026-08-03"
        )
        code, report = self.run_gate()
        self.assertEqual(0, code, report)
        self.assertEqual("2026-08-03", report["claims"][0]["accessed_at"])

    def test_validation_placeholder_is_red(self):
        self.write_note(self.good_source(), validation="TBD")
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("VALIDATION_PLACEHOLDER",
                      {item["code"] for item in report["issues"]})

    def test_validation_missing_fixture_is_red(self):
        self.write_note(
            self.good_source(),
            validation=("fixture: Skills/references/missing.json sha256:" + "0" * 64),
        )
        code, report = self.run_gate()
        self.assertEqual(1, code, report)
        self.assertIn("VALIDATION_PATH_NOT_FOUND",
                      {item["code"] for item in report["issues"]})

    def test_stored_report_tamper_is_stale_then_canonical_is_green(self):
        stored = self.vault / "_inbox" / "report.json"
        code, report = self.run_gate("--json", str(stored))
        self.assertEqual(0, code, report)
        payload = json.loads(stored.read_text(encoding="utf-8"))
        payload["summary"]["advisories"] = 999
        stored.write_text(json.dumps(payload), encoding="utf-8")

        code, report = self.run_gate("--verify-report", str(stored))
        self.assertEqual(1, code, report)
        self.assertEqual("STALE", report["effective_status"])
        self.assertIn("STORED_REPORT_TAMPERED",
                      {item["code"] for item in report["issues"]})

        code, report = self.run_gate()
        self.assertEqual(0, code, report)
        self.assertEqual("GREEN", report["effective_status"])

    def test_stored_report_input_change_is_stale(self):
        stored = self.vault / "_inbox" / "report.json"
        code, report = self.run_gate("--json", str(stored))
        self.assertEqual(0, code, report)

        note = self.vault / "_research" / "R-test.md"
        note.write_text(
            note.read_text(encoding="utf-8") + "\n<!-- changed after receipt -->\n",
            encoding="utf-8",
        )
        code, report = self.run_gate("--verify-report", str(stored))
        self.assertEqual(1, code, report)
        self.assertEqual("STALE", report["effective_status"])
        self.assertIn("STORED_REPORT_INPUT_STALE",
                      {item["code"] for item in report["issues"]})

    def test_report_cannot_overwrite_skill_file(self):
        protected = self.vault / "Skills" / "SKILL-orchestrator.md"
        before = protected.read_text(encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault),
             "--json", str(protected)],
            text=True, encoding="utf-8", capture_output=True, check=False,
        )
        self.assertEqual(2, proc.returncode, proc.stdout)
        self.assertEqual(before, protected.read_text(encoding="utf-8"))
        report = json.loads(proc.stdout)
        self.assertEqual("REPORT_WRITE_ERROR", report["issues"][0]["code"])


if __name__ == "__main__":
    unittest.main()
