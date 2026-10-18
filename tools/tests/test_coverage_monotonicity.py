#!/usr/bin/env python3
"""The class-level invariant behind five separately-patched bugs.

WHY THIS FILE IS NOT A PER-TOOL TEST
------------------------------------
The same defect was found and fixed one tool at a time, at least five times:
an unmeasured result (``NO_COVERAGE``) being folded into a clean verdict.
Every fix was correct and none of them stopped the next one, because each was
a patch to one expression rather than a statement about the class.

``common/findings.Finding`` already carries the single-check half of the rule
- ``scanned == 0`` may only be ``NO_COVERAGE`` - and enforces it in the
constructor so a hand-built finding cannot smuggle a scanned=0 PASS through.
This file carries the AGGREGATE half:

    COVERAGE MONOTONICITY
    Adding a NO_COVERAGE to a result set can never produce a PASS,
    and can never preserve one.

Two tiers, deliberately:

  * ``TheRule`` tests ``rollup_status``/``rollup_from_counts`` directly,
    including over generated combinations rather than hand-picked examples.
    A property is what stops the sixth variant.
  * ``TheCallers`` re-injects the exact historical fault into each tool that
    had it, through the tool's own producer rather than a copied report dict.
    A fixture built by hand is what let the original six selectors pass while
    pointing at paths no tool wrote.

Every test below FAILS on the code as it stood before 2026-08-10.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    PASS,
    Finding,
    acceptance_verdict,
    result,
    rollup_from_counts,
    rollup_status,
)
from scripts.production import contracts as con  # noqa: E402
from scripts.production import jlcpcb_dfm as jlc  # noqa: E402
from scripts.production import outputs as out  # noqa: E402


def finding(name: str, status: str) -> Finding:
    """One finding of each status, built the way the tools build them."""
    if status == PASS:
        return result(name, 1, [])
    if status == FAIL:
        return result(name, 1, [{"problem": "injected"}])
    return Finding(name, NO_COVERAGE, 0, [], "nothing to examine")


# --------------------------------------------------------------------------- #
class TheRule(unittest.TestCase):
    """rollup_status / rollup_from_counts, as a property not as examples."""

    def test_an_empty_set_is_not_a_pass(self):
        """"Nothing was checked" and "everything is fine" are different
        statements. This is the aggregate twin of scanned == 0."""
        self.assertEqual(rollup_status([]), NO_COVERAGE)
        self.assertEqual(rollup_from_counts(0, 0, 0), NO_COVERAGE)

    def test_adding_no_coverage_never_produces_or_preserves_a_pass(self):
        """The property itself, over every combination up to four findings.

        Hand-picked examples are what the previous five fixes had. The five
        bugs differed only in how many PASSes stood next to the NO_COVERAGE,
        which is exactly the axis a combinatorial check sweeps.
        """
        statuses = (PASS, FAIL, NO_COVERAGE)
        for size in range(1, 5):
            for combination in itertools.product(statuses, repeat=size):
                findings = [finding(f"c{i}", s)
                            for i, s in enumerate(combination)]
                verdict = rollup_status(findings)
                with self.subTest(combination=combination):
                    if NO_COVERAGE in combination and FAIL not in combination:
                        self.assertEqual(verdict, NO_COVERAGE)
                    if verdict == PASS:
                        self.assertNotIn(NO_COVERAGE, combination)
                        self.assertNotIn(FAIL, combination)

    def test_one_pass_beside_many_no_coverage_is_not_a_pass(self):
        """The literal shape of three of the five bugs."""
        findings = [finding("measured", PASS)]
        findings += [finding(f"unmeasured{i}", NO_COVERAGE) for i in range(99)]
        self.assertEqual(rollup_status(findings), NO_COVERAGE)
        self.assertEqual(rollup_from_counts(1, 0, 99), NO_COVERAGE)

    def test_a_measured_failure_still_wins(self):
        """Monotone in the other direction too: coverage gaps must not hide
        a FAIL either."""
        self.assertEqual(
            rollup_status([finding("a", PASS), finding("b", NO_COVERAGE),
                           finding("c", FAIL)]), FAIL)
        self.assertEqual(rollup_from_counts(5, 1, 5), FAIL)

    def test_counts_and_findings_agree_on_every_combination(self):
        """Two shapes of the same rule; if they can disagree there are two
        rules again, which is the state this whole file exists to end."""
        for passed in range(3):
            for failed in range(3):
                for uncovered in range(3):
                    findings = ([finding(f"p{i}", PASS) for i in range(passed)]
                                + [finding(f"f{i}", FAIL) for i in range(failed)]
                                + [finding(f"n{i}", NO_COVERAGE)
                                   for i in range(uncovered)])
                    with self.subTest(p=passed, f=failed, n=uncovered):
                        self.assertEqual(
                            rollup_status(findings),
                            rollup_from_counts(passed, failed, uncovered))


class ExclusionIsNotAnEscapeHatch(unittest.TestCase):
    """An exclusion is a recorded decision, not a way to reach PASS."""

    def test_an_explicitly_excluded_no_coverage_is_out_of_scope(self):
        findings = [finding("measured", PASS), finding("skipped", NO_COVERAGE)]
        self.assertEqual(rollup_status(findings, {"skipped"}), PASS)

    def test_a_measured_failure_cannot_be_excluded_away(self):
        """You may declare something out of scope BEFORE measuring it. You may
        not declare it out of scope because you did not like the answer."""
        findings = [finding("measured", PASS), finding("bad", FAIL)]
        self.assertEqual(rollup_status(findings, {"bad"}), FAIL)

    def test_excluding_everything_is_still_no_coverage(self):
        """Otherwise a gate could be talked into PASS by narrowing its scope
        to nothing, which is the empty-set vacuity in a costume."""
        findings = [finding("a", NO_COVERAGE), finding("b", NO_COVERAGE)]
        self.assertEqual(rollup_status(findings, {"a", "b"}), NO_COVERAGE)


class TheAcceptanceAxis(unittest.TestCase):
    """"The computation finished" and "a criterion was met" are two answers."""

    def test_no_criterion_is_no_coverage_not_a_pass(self):
        verdict = acceptance_verdict(applied=False, met=False)
        self.assertEqual(verdict["verdict"], NO_COVERAGE)
        self.assertTrue(verdict["verdict_reason"])

    def test_a_criterion_on_an_untrustworthy_value_is_no_coverage(self):
        """A limit compared against a number the solver disowned is not a
        measurement of anything."""
        self.assertEqual(
            acceptance_verdict(applied=True, met=True,
                               computed=False)["verdict"], NO_COVERAGE)

    def test_an_applied_criterion_gives_a_real_verdict(self):
        self.assertEqual(
            acceptance_verdict(applied=True, met=True)["verdict"], PASS)
        self.assertEqual(
            acceptance_verdict(applied=True, met=False)["verdict"], FAIL)


# --------------------------------------------------------------------------- #
class TheCallers(unittest.TestCase):
    """Each historical fault re-injected through the real producer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------- production/outputs.py
    def test_no_coverage_does_not_open_the_fabrication_gate(self):
        """The one that produced gerbers off an ungated board.

        ``gate_open = not any(FAIL)`` made NO_COVERAGE equivalent to PASS, so
        --skip-verification, --skip-board-checks and "no contracts supplied"
        each produced fabrication output from a gate that had measured
        nothing - while the schema described itself as "Fail-closed gate in
        front of the fabrication outputs".
        """
        findings = [out.check_report_gate({"violations": []}, "drc", "b",
                                          scanned=1),
                    out.check_contract_gates(None, None)]
        self.assertEqual(findings[0]["status"], PASS)
        self.assertEqual(findings[1]["status"], NO_COVERAGE)
        self.assertNotEqual(rollup_status(findings), PASS)

    def test_the_outputs_self_test_covers_its_own_gate_arithmetic(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "production" / "outputs.py"),
             "--self-test"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # ---------------------------------------------- production/contracts.py
    def test_an_empty_parts_contract_examined_nothing(self):
        """``for row in data.get("rows", [])`` over an empty list ran zero
        times, appended no issue, and returned a clean pass."""
        path = self.tmp / "parts.json"
        path.write_text(json.dumps({
            "schema_version": con.CONTRACT_SCHEMA_VERSION, "rows": []}),
            encoding="utf-8")
        status, issues, scanned = con.parts_gate_status(str(path))
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(scanned, 0)
        self.assertEqual(issues, [])

    def test_a_contract_with_no_rows_key_at_all_raises(self):
        """An absent row list is not an empty one, and neither passes."""
        path = self.tmp / "parts.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(con.ContractGateError):
            con.parts_gate_status(str(path))

    def test_the_boolean_view_refuses_to_answer_for_an_empty_contract(self):
        """NO_COVERAGE has no truthful bool. True was the original bug and
        False would read as "the parts are bad"."""
        path = self.tmp / "parts.json"
        path.write_text(json.dumps({
            "schema_version": con.CONTRACT_SCHEMA_VERSION, "rows": []}),
            encoding="utf-8")
        with self.assertRaises(con.ContractGateError):
            con.parts_gate_passes(str(path))

    def test_an_empty_contract_beside_a_clean_one_does_not_pass_the_gate(self):
        """The mixed case: outputs.py folded the unmeasured parts contract in
        beside a clean DRC contract and reported PASS for both."""
        parts = self.tmp / "parts.json"
        parts.write_text(json.dumps({
            "schema_version": con.CONTRACT_SCHEMA_VERSION, "rows": []}),
            encoding="utf-8")
        drc = self.tmp / "drc.json"
        con.write_drc_contract(
            con.build_drc_contract("b", {"violations": []}), str(drc))
        found = out.check_contract_gates(str(parts), str(drc))
        self.assertEqual(found["status"], NO_COVERAGE)
        self.assertIn("NOT MEASURED", found["detail"])

    # -------------------------------------------- production/jlcpcb_dfm.py
    def test_a_vendor_that_declined_is_not_a_pass(self):
        """check_dfm read only the violation list, so {"pass": false,
        "issues": []} - declined, with no itemised reason - came back PASS."""
        outcome = jlc.parse_dfm_response({"pass": False, "issues": []})
        self.assertIs(outcome.ok, False)
        with mock.patch.object(jlc, "submit_for_dfm",
                                        return_value=outcome):
            found = jlc.check_dfm("x.zip", allow_network=True)
        self.assertEqual(found["status"], FAIL)

    def test_a_response_with_no_verdict_says_nothing(self):
        """``ok = bool(raw.get("pass", not violations))`` inferred a pass from
        an empty issue list - absence read as zero, one more time."""
        outcome = jlc.parse_dfm_response({"issues": []})
        self.assertIsNone(outcome.ok)
        with mock.patch.object(jlc, "submit_for_dfm",
                                        return_value=outcome):
            found = jlc.check_dfm("x.zip", allow_network=True)
        self.assertEqual(found["status"], NO_COVERAGE)

    def test_a_vendor_pass_with_no_issues_is_still_a_pass(self):
        """The fix must not turn the ordinary clean answer into a refusal."""
        outcome = jlc.parse_dfm_response({"pass": True, "issues": []})
        with mock.patch.object(jlc, "submit_for_dfm",
                                        return_value=outcome):
            found = jlc.check_dfm("x.zip", allow_network=True)
        self.assertEqual(found["status"], PASS)


class NoToolReDerivesTheRule(unittest.TestCase):
    """The linter tier: the rule cannot be re-derived by hand any more.

    The repo's own escalation rule is that a defect seen a second time earns
    a class-level invariant and a third time earns a linter. This is the
    third time. Nine tools had written some spelling of

        return 1 if report["summary"][FAIL] else 0

    which is ``common.findings.board_exit_code`` copied WITHOUT the exemption
    that makes that function's behaviour defensible - and each copy therefore
    let an unmeasured check leave through exit 0. A per-tool test would need
    a fixture per tool and would still miss the tenth copy; reading the
    source finds every one of them, including in tools written tomorrow.
    """

    #: ``board_exit_code`` is the ONE place this expression is allowed. Its
    #: docstring records exit 0 on NO_COVERAGE as via_stub's established
    #: sibling-tool contract, deliberately frozen and tracked as an open
    #: item. Tools that WANT that behaviour must call it rather than
    #: re-implement it, which is exactly what this allowlist enforces.
    ALLOWED = {("common/findings.py", "board_exit_code")}

    def test_no_script_decides_its_exit_code_from_the_fail_count_alone(self):
        import ast

        root = SCRIPTS.parent
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                try:
                    text = ast.unparse(node.value)
                except Exception:                       # pragma: no cover
                    continue
                # A verdict built from FAIL with no mention of NO_COVERAGE.
                if ("summary" not in text or "FAIL" not in text
                        or "NO_COVERAGE" in text):
                    continue
                enclosing = next(
                    (f.name for f in ast.walk(tree)
                     if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and f.lineno <= node.lineno
                     <= (f.end_lineno or f.lineno)), "?")
                if (relative, enclosing) in self.ALLOWED:
                    continue
                offenders.append(f"{relative}:{node.lineno} "
                                 f"in {enclosing}(): {text}")
        self.assertEqual(
            [], offenders,
            "these decide a verdict or exit code from the FAIL count alone, "
            "so a NO_COVERAGE check leaves as success. Use "
            "common.findings.rollup_status/rollup_from_counts, or call "
            "board_exit_code() if the frozen exemption is really what you "
            "want:\n  " + "\n  ".join(offenders))


class ProducersNotFixtures(unittest.TestCase):
    """Run the real tools and read their real reports.

    ``test_assertions`` learned this the hard way: six shipped selectors named
    paths no tool wrote, and 135 tests missed it because every one of them
    built its own evidence dict. These call the CLI.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, script: Path, *args):
        out_path = self.tmp / "report.json"
        proc = subprocess.run(
            [sys.executable, str(script), *args, "--json", str(out_path)],
            capture_output=True, text=True, check=False)
        document = (json.loads(out_path.read_text(encoding="utf-8"))
                    if out_path.is_file() else None)
        return proc.returncode, document, proc.stderr

    def test_the_field_solver_separates_running_from_passing(self):
        """With no --target the solve really did complete, so the top-level
        status stays GREEN. What used to be missing is a machine-readable way
        to say that nothing was checked against it."""
        code, report, stderr = self.run_tool(
            SCRIPTS / "field_solver.py", "microstrip",
            "--w", "0.3", "--h", "0.2", "--t", "0.035", "--er", "4.3")
        self.assertIsNotNone(report, stderr)
        self.assertEqual(report["status"], "GREEN")
        self.assertIs(report["gate"]["applied"], False)
        self.assertEqual(report["gate"]["verdict"], NO_COVERAGE)
        self.assertTrue(report["gate"]["verdict_reason"])
        self.assertEqual(code, 0)

    def test_the_field_solver_gives_a_real_verdict_when_given_a_target(self):
        code, report, stderr = self.run_tool(
            SCRIPTS / "field_solver.py", "microstrip",
            "--w", "0.3", "--h", "0.2", "--t", "0.035", "--er", "4.3",
            "--target", "50")
        self.assertIsNotNone(report, stderr)
        self.assertIs(report["gate"]["applied"], True)
        self.assertIn(report["gate"]["verdict"], (PASS, FAIL))


if __name__ == "__main__":
    unittest.main()
