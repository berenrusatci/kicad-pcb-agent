#!/usr/bin/env python3
"""Execute the COMMITTED context-pack fixture end to end.

The bundle shipped this fixture and a README promising
``preflight READY, postflight ACCEPTED, FIXTURE-G1 exit_code=0``. In the old
tree it actually produced ``BLOCKED`` with seven errors, because every path in
the manifest carried a stale ``aiplanlama/`` prefix and the pinned
tool_registry hash was wrong.

Nothing caught it: all 21 test_context_pack tests synthesise their own
manifests in temp dirs, so the committed fixture was never executed. This
test executes it, so the promise and the artifact cannot drift apart again.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import FIXTURES, REPO, SCRIPTS  # noqa: E402

SCRIPT = SCRIPTS / "context_pack.py"
MANIFEST = FIXTURES / "context_pack" / "projecte_ready" / "context_manifest.json"
MANIFEST_REL = MANIFEST.relative_to(REPO)


def run(*extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(MANIFEST_REL), "--root", ".", *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
        check=False,
    )


class CommittedFixtureTests(unittest.TestCase):
    def test_manifest_paths_are_repo_relative_and_current(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for artifact in data["artifacts"]:
            with self.subTest(role=artifact["role"]):
                self.assertNotIn("aiplanlama", artifact["path"],
                                 "stale pre-split path prefix")
                self.assertTrue((REPO / artifact["path"]).is_file(),
                                f"missing artifact: {artifact['path']}")

    def test_preflight_is_ready(self):
        proc = run()
        report = json.loads(proc.stdout)
        self.assertEqual([], report["errors"], proc.stdout)
        self.assertEqual("READY", report["verdict"])
        self.assertEqual(0, proc.returncode)

    def test_postflight_is_accepted_and_the_gate_exits_zero(self):
        proc = run("--postflight")
        report = json.loads(proc.stdout)
        self.assertEqual([], report["errors"], proc.stdout)
        self.assertEqual("ACCEPTED", report["verdict"])
        self.assertEqual(0, proc.returncode)

        gates = {g["id"]: g for g in report["gate_results"]}
        self.assertIn("FIXTURE-G1", gates)
        self.assertEqual(0, gates["FIXTURE-G1"]["exit_code"])

    def test_fixture_binds_the_real_tool_registry(self):
        """The fixture must exercise registry binding, not a stub copy."""
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        registry = [a for a in data["artifacts"] if a["role"] == "tool_registry"]
        self.assertEqual(1, len(registry))
        self.assertEqual("config/tool_registry.json", registry[0]["path"])

    def test_a_tampered_artifact_is_detected(self):
        """FAULT INJECTION: the fixture must be hash-bound, not just present."""
        target = REPO / "tools/fixtures/context_pack/projecte_ready/design_intent.json"
        original = target.read_bytes()
        try:
            payload = json.loads(original.decode("utf-8"))
            payload["goals"] = payload.get("goals", []) + ["injected"]
            target.write_text(json.dumps(payload, indent=2) + "\n",
                              encoding="utf-8")
            report = json.loads(run().stdout)
            self.assertTrue(
                any("STALE" in e or "HASH" in e for e in report["errors"]),
                f"tamper not detected: {report['errors']}")
        finally:
            target.write_bytes(original)

    def test_fixture_is_restored_after_fault_injection(self):
        proc = run()
        self.assertEqual("READY", json.loads(proc.stdout)["verdict"])


if __name__ == "__main__":
    unittest.main()
