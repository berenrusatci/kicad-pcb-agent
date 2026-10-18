#!/usr/bin/env python3
"""Tests for the empirical-prior layer (common/priors.py + memory/priors.py).

A ranking layer fails quietly. It does not crash; it puts the wrong fix first
and looks authoritative doing it. So the tests here are mostly about what the
layer REFUSES to do:

  * a single lucky record must not outrank a well-evidenced fix
  * an empty or thin memory must produce no recommendation at all
  * needs_human must not be charged as a failure
  * calibration must be leave-one-out, or it flatters itself for any data

The occurrence counter is tested hardest, because the store DEDUPLICATES: it
was keeping one record for forty observations, so a fix that had worked forty
times was indistinguishable on disk from one that worked once, and nothing
could rank them.
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

from common.priors import (  # noqa: E402
    PriorError,
    calibration,
    evidence_summary,
    gather_evidence,
    leave_one_out_predictions,
    rank_fixes,
)
from scripts.memory.error_memory import ErrorMemory, Record, parse_markdown  # noqa: E402

SCRIPT = SCRIPTS / "memory" / "priors.py"


def run(*args, store=None):
    import os
    env = dict(os.environ)
    if store is not None:
        env["PCB_AGENT_MEMORY_DIR"] = str(store)
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, check=False)


def record(fix, outcome="solved", kind="DRC", occurrences=1, signature="sig"):
    return {"kind": kind, "fix": fix, "outcome": outcome,
            "occurrences": occurrences, "signature": signature,
            "project": "p"}


class OccurrenceCounting(unittest.TestCase):
    """The store deduplicates; the counter is where frequency survives."""

    def test_a_repeat_increments_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ErrorMemory(Path(tmp) / "m.md")
            entry = Record(kind="DRC", message="Clearance at (1,2)",
                           root_cause="tight", fix="widen", outcome="solved")
            self.assertTrue(memory.remember(entry))
            self.assertFalse(memory.remember(entry))
            self.assertFalse(memory.remember(entry))
            stored = memory.records()
            self.assertEqual(1, len(stored))
            self.assertEqual(3, stored[0].occurrences)

    def test_the_boolean_contract_is_unchanged(self):
        """Callers that only ask 'was this new' still get the same answer."""
        with tempfile.TemporaryDirectory() as tmp:
            memory = ErrorMemory(Path(tmp) / "m.md")
            entry = Record(kind="DRC", message="m", root_cause="r",
                           fix="f", outcome="solved")
            self.assertIs(True, memory.remember(entry))
            self.assertIs(False, memory.remember(entry))

    def test_a_different_outcome_is_a_separate_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ErrorMemory(Path(tmp) / "m.md")
            for outcome in ("solved", "failed"):
                memory.remember(Record(kind="DRC", message="m",
                                       root_cause="r", fix="f",
                                       outcome=outcome))
            self.assertEqual(2, len(memory.records()))

    def test_an_older_file_without_the_counter_reads_as_one(self):
        """Backward compatibility: existing memories must stay loadable."""
        legacy = (
            # Leading newline: parse_markdown splits on "\n## ", and a real
            # file always has the HEADER in front of the first record.
            "\n## DRC - abc\n"
            "- **kind:** DRC\n- **signature:** abc\n- **message:** m\n"
            "- **root_cause:** r\n- **fix:** f\n- **outcome:** solved\n"
            "- **project:** p\n- **recorded:** 2026-01-01\n- **tags:** \n\n")
        parsed = parse_markdown(legacy)
        self.assertEqual(1, len(parsed))
        self.assertEqual(1, parsed[0].occurrences)

    def test_a_malformed_counter_does_not_lose_the_record(self):
        """A hand-edited file is allowed; a typo must cost one frequency,
        not the whole memory."""
        broken = (
            "\n## DRC - abc\n"
            "- **kind:** DRC\n- **signature:** abc\n- **message:** m\n"
            "- **fix:** f\n- **outcome:** solved\n"
            "- **occurrences:** twelve\n- **tags:** \n\n")
        parsed = parse_markdown(broken)
        self.assertEqual(1, len(parsed))
        self.assertEqual(1, parsed[0].occurrences)

    def test_evidence_uses_the_counter_not_the_record_count(self):
        evidence = gather_evidence([record("f", occurrences=40)])
        self.assertEqual(40, evidence[("DRC", "f")].successes)

    def test_a_rewrite_preserves_every_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ErrorMemory(Path(tmp) / "m.md")
            for index in range(4):
                memory.remember(Record(kind="DRC", message=f"m{index}",
                                       root_cause="r", fix=f"f{index}",
                                       outcome="solved"))
            memory.remember(Record(kind="DRC", message="m0", root_cause="r",
                                   fix="f0", outcome="solved"))
            self.assertEqual(4, len(memory.records()))


class RankingRefusals(unittest.TestCase):
    def test_one_of_one_does_not_outrank_forty_of_forty(self):
        """The reason the ranking uses a lower bound at all."""
        evidence = gather_evidence([
            record("lucky", occurrences=1),
            record("proven", occurrences=40),
        ])
        ranked = rank_fixes(evidence, min_attempts=1)
        self.assertEqual("proven", ranked["recommended"][0]["fix"])

    def test_an_empty_memory_recommends_nothing(self):
        self.assertEqual([], rank_fixes(gather_evidence([]))["recommended"])

    def test_a_thin_record_is_withheld_with_a_reason(self):
        ranked = rank_fixes(gather_evidence([record("f")]), min_attempts=3)
        self.assertEqual([], ranked["recommended"])
        withheld = ranked["insufficient_evidence"][0]
        self.assertEqual("INSUFFICIENT_EVIDENCE", withheld["withheld_because"])
        self.assertEqual(2, withheld["attempts_needed"])

    def test_a_consistently_failing_fix_is_discouraged(self):
        ranked = rank_fixes(
            gather_evidence([record("bad", outcome="failed", occurrences=8)]),
            min_attempts=3)
        self.assertEqual([], ranked["recommended"])
        self.assertEqual("bad", ranked["discouraged"][0]["fix"])
        self.assertEqual("OBSERVED_FAILURE_RATE",
                         ranked["discouraged"][0]["withheld_because"])

    def test_needs_human_is_not_charged_as_a_failure(self):
        evidence = gather_evidence([
            record("f", outcome="solved", occurrences=3),
            record("f", outcome="needs_human", occurrences=9),
        ])
        entry = evidence[("DRC", "f")]
        self.assertEqual(3, entry.attempts)
        self.assertEqual(1.0, entry.success_rate)
        self.assertEqual(9, entry.needs_human)

    def test_a_record_with_no_fix_supports_nothing(self):
        self.assertEqual({}, gather_evidence([record("")]))

    def test_the_kind_filter_separates_vocabularies(self):
        evidence = gather_evidence([
            record("f", kind="DRC", occurrences=5),
            record("f", kind="ERC", occurrences=5),
        ])
        ranked = rank_fixes(evidence, kind="ERC", min_attempts=1)
        self.assertEqual(1, len(ranked["recommended"]))
        self.assertEqual("ERC", ranked["recommended"][0]["kind"])

    def test_distinct_signatures_are_tracked_separately_from_count(self):
        """Forty hits on one signature is weaker than forty across many."""
        evidence = gather_evidence([
            record("f", occurrences=20, signature="a"),
            record("f", occurrences=20, signature="b"),
        ])
        self.assertEqual(2, len(evidence[("DRC", "f")].signatures))

    def test_min_attempts_below_one_is_refused(self):
        with self.assertRaises(PriorError):
            rank_fixes(gather_evidence([record("f")]), min_attempts=0)


class Calibration(unittest.TestCase):
    def test_overconfidence_is_detected(self):
        report = calibration([0.9] * 20, [True] * 10 + [False] * 10, bins=5)
        self.assertGreater(report["max_gap"], 0.3)

    def test_an_honest_predictor_scores_well(self):
        report = calibration([1.0] * 10 + [0.0] * 10,
                             [True] * 10 + [False] * 10, bins=5)
        self.assertLess(report["max_gap"], 1e-9)
        self.assertLess(report["brier"], 1e-9)

    def test_a_prediction_of_one_lands_in_the_last_bin(self):
        """The boundary case: 1.0 is not < 1.0 and would otherwise vanish."""
        report = calibration([1.0], [True], bins=5)
        self.assertEqual(1, report["samples"])
        self.assertEqual(1, report["populated_bins"])

    def test_a_prediction_outside_zero_to_one_is_refused(self):
        with self.assertRaises(PriorError):
            calibration([1.5], [True])

    def test_mismatched_lengths_are_refused(self):
        with self.assertRaises(PriorError):
            calibration([0.5, 0.5], [True])

    def test_calibrating_nothing_is_refused(self):
        with self.assertRaises(PriorError):
            calibration([], [])

    def test_one_populated_bin_is_reported_as_such(self):
        """It cannot show miscalibration, so it must not read as evidence."""
        report = calibration([0.5] * 6, [True] * 3 + [False] * 3, bins=5)
        self.assertEqual(1, report["populated_bins"])


class LeaveOneOut(unittest.TestCase):
    def test_a_lone_observation_cannot_be_predicted(self):
        predictions, outcomes = leave_one_out_predictions([record("f")])
        self.assertEqual([], predictions)
        self.assertEqual([], outcomes)

    def test_a_record_is_never_scored_against_itself(self):
        """A perfect record predicted from 39 others is still 1.0, but the
        held-out one must not be part of that 39."""
        predictions, _outcomes = leave_one_out_predictions(
            [record("f", occurrences=40)])
        self.assertTrue(predictions)
        self.assertEqual(1.0, predictions[0])

    def test_a_deduplicated_record_leaves_its_other_observations_behind(self):
        """Holding out the whole record would refuse to score exactly the
        best-evidenced fixes."""
        predictions, _ = leave_one_out_predictions(
            [record("f", occurrences=10)], min_attempts=5)
        self.assertTrue(predictions, "the best-evidenced fix was skipped")

    def test_needs_human_records_are_not_predicted(self):
        predictions, _ = leave_one_out_predictions(
            [record("f", outcome="needs_human", occurrences=5)])
        self.assertEqual([], predictions)

    def test_a_mixed_history_produces_an_intermediate_prediction(self):
        predictions, outcomes = leave_one_out_predictions([
            record("f", outcome="solved", occurrences=6),
            record("f", outcome="failed", occurrences=4),
        ])
        self.assertTrue(predictions)
        self.assertTrue(all(0.0 < value < 1.0 for value in predictions),
                        predictions)
        self.assertIn(True, outcomes)
        self.assertIn(False, outcomes)


class Summary(unittest.TestCase):
    def test_totals_use_the_counter(self):
        summary = evidence_summary(gather_evidence([
            record("a", occurrences=7),
            record("b", outcome="failed", occurrences=3),
        ]))
        self.assertEqual(2, summary["distinct_fixes"])
        self.assertEqual(10, summary["total_attempts"])
        self.assertEqual(7, summary["total_successes"])
        self.assertAlmostEqual(0.7, summary["overall_success_rate"])

    def test_an_empty_memory_has_a_null_rate_not_zero(self):
        """Null, not 0: 'nothing tried' is not 'everything failed'."""
        self.assertIsNone(
            evidence_summary(gather_evidence([]))["overall_success_rate"])


class SelfTest(unittest.TestCase):
    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)


class CliContract(unittest.TestCase):
    def _memory(self, tmp):
        memory = ErrorMemory(Path(tmp) / "error_memory.md")
        for index in range(9):
            memory.remember(Record(kind="DRC", message=f"Clearance at ({index},1)",
                                   root_cause="tight",
                                   fix="raise clearance", outcome="solved"))
        for index in range(6):
            memory.remember(Record(kind="DRC", message=f"Clearance near U{index}",
                                   root_cause="unknown",
                                   fix="move the part", outcome="failed"))
        return memory

    def test_an_empty_memory_is_no_coverage_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run("--json", str(out), store=tmp)
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("NO_COVERAGE", report["status"])
            self.assertIn("EMPTY_MEMORY", report["reason_codes"])

    def test_a_populated_memory_ranks_the_evidenced_fix_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._memory(tmp)
            out = Path(tmp) / "r.json"
            proc = run("--min-attempts", "3", "--json", str(out), store=tmp)
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("GREEN", report["status"])
            recommended = report["ranking"]["recommended"]
            self.assertEqual("raise clearance", recommended[0]["fix"])

    def test_the_failing_fix_is_discouraged_not_recommended(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._memory(tmp)
            out = Path(tmp) / "r.json"
            run("--min-attempts", "3", "--json", str(out), store=tmp)
            report = json.loads(out.read_text(encoding="utf-8"))
            discouraged = {item["fix"] for item in report["ranking"]["discouraged"]}
            recommended = {item["fix"] for item in report["ranking"]["recommended"]}
            self.assertIn("move the part", discouraged)
            self.assertNotIn("move the part", recommended)

    def test_a_high_min_attempts_withholds_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._memory(tmp)
            out = Path(tmp) / "r.json"
            proc = run("--min-attempts", "500", "--json", str(out), store=tmp)
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("NO_COVERAGE", report["status"])
            self.assertIn("NO_FIX_WITH_ENOUGH_EVIDENCE", report["reason_codes"])

    def test_a_calibration_ceiling_can_fail_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._memory(tmp)
            out = Path(tmp) / "r.json"
            proc = run("--min-attempts", "3", "--max-calibration-gap", "0.0",
                       "--json", str(out), store=tmp)
            report = json.loads(out.read_text(encoding="utf-8"))
            if report["calibration"] and report["calibration"]["max_gap"] > 0:
                self.assertEqual(1, proc.returncode)
                self.assertEqual("RED", report["status"])
                self.assertIn("CALIBRATION_GAP_EXCEEDED", report["reason_codes"])

    def test_the_model_class_is_not_overclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._memory(tmp)
            out = Path(tmp) / "r.json"
            run("--json", str(out), store=tmp)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("observed_frequency", report["model_class"])
            self.assertIs(False, report["signoff"])

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._memory(tmp)
            json.loads(run(store=tmp).stdout)


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(
            (SCHEMAS / "prior_report.schema.json").read_text(encoding="utf-8"))

    def test_schema_pins_the_version_and_model_class(self):
        self.assertEqual("prior-report/v1",
                         self.schema["properties"]["schema_version"]["const"])
        self.assertEqual("observed_frequency",
                         self.schema["properties"]["model_class"]["const"])

    def test_reason_codes_emitted_are_declared(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        self.assertEqual({"EMPTY_MEMORY", "NO_FIX_WITH_ENOUGH_EVIDENCE",
                          "CALIBRATION_GAP_EXCEEDED",
                          "CALIBRATION_NOT_POSSIBLE",
                          "CALIBRATION_UNINFORMATIVE"}, declared)

    def test_the_three_ranking_lists_are_required(self):
        required = self.schema["properties"]["ranking"]["required"]
        self.assertEqual({"recommended", "insufficient_evidence",
                          "discouraged"}, set(required))

    def test_a_real_report_satisfies_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ErrorMemory(Path(tmp) / "error_memory.md")
            memory.remember(Record(kind="DRC", message="m", root_cause="r",
                                   fix="f", outcome="solved"))
            out = Path(tmp) / "r.json"
            run("--json", str(out), store=tmp)
            report = json.loads(out.read_text(encoding="utf-8"))
        for key in self.schema["required"]:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
