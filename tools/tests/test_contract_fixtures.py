#!/usr/bin/env python3
"""Read-only contract regression fixtures for promoted A1/A2 rules."""

from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import tempfile
import unittest


import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _paths import FIXTURES as _FIX, SCHEMAS, TOOLS  # noqa: E402

sys.path.insert(0, str(TOOLS))
from common.kicad_cli import KicadCliError, _require_cli  # noqa: E402

ROOT = SCHEMAS
FIXTURES = _FIX / "contract"
EXAMPLES = _FIX / "references"


def parity_issues(schema: dict, fixture: dict) -> list[str]:
    required = set(schema["$defs"]["rail"]["required"])
    producer = set(fixture["producer_fields"])
    consumer = set(fixture["consumer_fields"])
    issues = []
    if producer != required:
        issues.append("PRODUCER_SCHEMA_MISMATCH")
    if consumer != required:
        issues.append("CONSUMER_SCHEMA_MISMATCH")
    if producer != consumer:
        issues.append("PRODUCER_CONSUMER_MISMATCH")
    return issues


def coverage_status(coverage: dict) -> str:
    if coverage["scanned"] + coverage["excluded"] != coverage["eligible"]:
        return "INVALID_COVERAGE"
    if coverage["violations"] > coverage["scanned"]:
        return "INVALID_COVERAGE"
    if coverage["scanned"] == 0:
        return "NO_COVERAGE"
    return "RED" if coverage["violations"] else "GREEN"


class ContractFixturesTest(unittest.TestCase):
    def test_rail_tree_producer_consumer_parity(self) -> None:
        schema = json.loads((ROOT / "rail_tree.schema.json").read_text())
        fixture = json.loads((FIXTURES / "rail_tree_parity.json").read_text())
        self.assertEqual([], parity_issues(schema, fixture))

    def test_rail_tree_old_field_fault_is_detected(self) -> None:
        schema = json.loads((ROOT / "rail_tree.schema.json").read_text())
        fixture = json.loads((FIXTURES / "rail_tree_parity.json").read_text())
        broken = copy.deepcopy(fixture)
        broken["consumer_fields"].remove("ipc2221_screen_width_mm")
        broken["consumer_fields"].append("min_trace_width_mm")
        self.assertIn("CONSUMER_SCHEMA_MISMATCH", parity_issues(schema, broken))

    def test_esd_profile_example_is_explicitly_tbd(self) -> None:
        schema = json.loads((ROOT / "esd_test_profile.schema.json").read_text())
        example = json.loads((EXAMPLES / "esd_test_profile.example.json").read_text())
        self.assertEqual(set(schema["required"]), set(example))
        self.assertEqual("TBD", example["status"])
        self.assertIsNone(example["severity"])

    def test_fab_profile_example_is_explicitly_tbd(self) -> None:
        schema = json.loads((ROOT / "fab_capabilities.schema.json").read_text())
        example = json.loads((EXAMPLES / "fab_capabilities.example.json").read_text())
        self.assertEqual(set(schema["required"]), set(example))
        self.assertEqual("TBD", example["status"])
        self.assertIsNone(example["vendor"])
        self.assertIsNone(example["limits"]["min_trace_mm"])

    def test_zero_scan_fault_is_no_coverage(self) -> None:
        self.assertEqual(
            "NO_COVERAGE",
            coverage_status({"eligible": 3, "scanned": 0, "excluded": 3, "violations": 0}),
        )
        self.assertEqual(
            "INVALID_COVERAGE",
            coverage_status({"eligible": 3, "scanned": 2, "excluded": 0, "violations": 0}),
        )

    def test_erc_violation_requires_nonzero_exit_and_json_issue(self) -> None:
        schematic = FIXTURES / "erc_unconnected_resistor.kicad_sch"
        try:
            cli = _require_cli()
        except KicadCliError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            report = pathlib.Path(tmp) / "erc.json"
            proc = subprocess.run(
                [
                    cli, "sch", "erc", "--format", "json",
                    "--severity-all", "--exit-code-violations",
                    "-o", str(report), str(schematic),
                ],
                check=False,
                capture_output=True,
                text=True,
                # kicad-cli is a real external binary, not one of this repo's
                # own UTF-8-forced tools - its own console messages follow the
                # OS locale (observed: Turkish cp1254), which is not valid
                # UTF-8. errors="replace" keeps this test from crashing on
                # kicad-cli's own diagnostic text; only the exit code and the
                # JSON report file are actually asserted on below.
                encoding="utf-8", errors="replace",
            )
            self.assertNotEqual(0, proc.returncode, proc.stdout + proc.stderr)
            payload = json.loads(report.read_text())
            violations = [
                issue
                for sheet in payload.get("sheets", [])
                for issue in sheet.get("violations", [])
            ]
            self.assertIn("pin_not_connected", {item.get("type") for item in violations})


if __name__ == "__main__":
    unittest.main()
