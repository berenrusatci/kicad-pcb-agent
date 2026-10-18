#!/usr/bin/env python3
"""Rank remembered fixes by what has actually worked, and measure the ranking.

``error_memory.py`` answers "have we seen this before" and returns three
unordered buckets. It cannot answer the next question - "of the fixes that
have worked, which should we try FIRST" - because it does not count anything.

This adds that counting layer, and the check that keeps it honest.

NOT A TRAINED MODEL, DELIBERATELY
---------------------------------
Queue item #63 cites Vivado ML Edition and Spectre FMC. Both are fitted
models with a corpus behind them. There is no corpus here, and a model fitted
to a handful of records from one board would be a confident-looking function
of noise. What transfers WITHOUT a dataset is SECDA-DSE's cost-model database:
record what each attempt achieved and let the accumulated record order the
next one. That is measurement, and it is also the prerequisite for anything
fitted later.

Every ranking is traceable to the records that produced it - the report
carries the counts - which is the opposite of the explainability problem the
APRIL session named as the third limitation of LLMs in hardware design.

THE THREE THINGS THAT STOP THIS FLATTERING ITSELF
-------------------------------------------------
  1. Ranked by the Wilson LOWER BOUND, not the mean. 1/1 and 40/40 are both a
     100% success rate; their lower bounds are 0.21 and 0.91, so evidence
     strength enters the ordering instead of being thrown away.
  2. Cold start is a REFUSAL. Below --min-attempts a fix is reported as
     INSUFFICIENT_EVIDENCE and is not ranked, so an empty memory produces no
     recommendation rather than an arbitrary one.
  3. Calibration is measured LEAVE-ONE-OUT. Scoring the priors on the records
     that built them would report near-perfect calibration for any amount of
     data; each record is predicted from the others instead.

Exit contract: 0 pass / 1 fail or NO_COVERAGE / 2 could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.priors import (  # noqa: E402
    DEFAULT_MIN_ATTEMPTS,
    DEFAULT_MIN_LOWER_BOUND,
    PriorError,
    calibration,
    evidence_summary,
    gather_evidence,
    leave_one_out_predictions,
    rank_fixes,
)
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)
from scripts.memory.error_memory import ErrorMemory, CHECK_KINDS  # noqa: E402
from scripts.memory.store import VaultWriteRefused, error_memory_path  # noqa: E402

SCHEMA_VERSION = "prior-report/v1"

LIMITS = [
    "counts observed outcomes; it is NOT a trained model and does not "
    "generalise beyond the fixes that have actually been recorded",
    "ranked by the Wilson 95% lower bound, so a single lucky record cannot "
    "outrank a well-evidenced fix",
    "below --min-attempts a fix is INSUFFICIENT_EVIDENCE and is not ranked: "
    "an empty memory yields no recommendation, not an arbitrary one",
    "needs_human outcomes are excluded from the denominator; they record an "
    "undecided attempt, not a lost one",
    "calibration is leave-one-out, and is only meaningful once more than one "
    "bin is populated",
    "the memory is a record of THIS project's history; a fix that has worked "
    "here is not evidence about a different stackup or fab",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", help="memory directory (default from "
                                        "PCB_AGENT_MEMORY_DIR or var/memory)")
    parser.add_argument("--kind", choices=CHECK_KINDS,
                        help="restrict to one check kind")
    parser.add_argument("--min-attempts", type=int,
                        default=DEFAULT_MIN_ATTEMPTS, dest="min_attempts",
                        help=f"observations needed before a fix is ranked "
                             f"(default {DEFAULT_MIN_ATTEMPTS})")
    parser.add_argument("--min-lower-bound", type=float,
                        default=DEFAULT_MIN_LOWER_BOUND,
                        dest="min_lower_bound",
                        help="a fix whose Wilson lower bound is under this is "
                             f"discouraged (default {DEFAULT_MIN_LOWER_BOUND})")
    parser.add_argument("--calibration-bins", type=int, default=5,
                        dest="calibration_bins")
    parser.add_argument("--max-calibration-gap", type=float,
                        dest="max_calibration_gap",
                        help="fail when the measured predicted-vs-observed "
                             "gap exceeds this")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def run_self_tests() -> None:
    """Properties that must hold whatever the memory happens to contain."""

    def evidence_ignores_records_with_no_fix() -> None:
        evidence = gather_evidence([
            {"kind": "DRC", "fix": "", "outcome": "solved"},
            {"kind": "DRC", "fix": "widen the track", "outcome": "solved"},
        ])
        assert len(evidence) == 1, evidence

    def needs_human_is_not_a_failure() -> None:
        evidence = gather_evidence([
            {"kind": "DRC", "fix": "f", "outcome": "solved"},
            {"kind": "DRC", "fix": "f", "outcome": "needs_human"},
        ])
        entry = evidence[("DRC", "f")]
        assert entry.attempts == 1, entry.attempts
        assert entry.success_rate == 1.0, entry.success_rate

    def one_of_one_does_not_outrank_forty_of_forty() -> None:
        """The reason the ranking uses the lower bound at all."""
        records = ([{"kind": "DRC", "fix": "lucky", "outcome": "solved"}]
                   + [{"kind": "DRC", "fix": "proven", "outcome": "solved"}] * 40)
        ranked = rank_fixes(gather_evidence(records), min_attempts=1)
        order = [item["fix"] for item in ranked["recommended"]]
        assert order[0] == "proven", order

    def an_empty_memory_recommends_nothing() -> None:
        ranked = rank_fixes(gather_evidence([]))
        assert ranked["recommended"] == [], ranked

    def a_thin_record_is_withheld_not_ranked() -> None:
        records = [{"kind": "DRC", "fix": "f", "outcome": "solved"}]
        ranked = rank_fixes(gather_evidence(records), min_attempts=3)
        assert not ranked["recommended"]
        assert ranked["insufficient_evidence"][0]["withheld_because"] == \
            "INSUFFICIENT_EVIDENCE"

    def a_fix_that_keeps_failing_is_discouraged() -> None:
        records = [{"kind": "DRC", "fix": "bad", "outcome": "failed"}] * 8
        ranked = rank_fixes(gather_evidence(records), min_attempts=3)
        assert not ranked["recommended"]
        assert ranked["discouraged"][0]["fix"] == "bad"

    def calibration_detects_overconfidence() -> None:
        """Predict 0.9 every time and be right half the time."""
        report = calibration([0.9] * 20, [True] * 10 + [False] * 10, bins=5)
        assert report["max_gap"] > 0.3, report["max_gap"]

    def calibration_rewards_an_honest_predictor() -> None:
        report = calibration([1.0] * 10 + [0.0] * 10,
                             [True] * 10 + [False] * 10, bins=5)
        assert report["max_gap"] < 1e-9, report["max_gap"]
        assert report["brier"] < 1e-9, report["brier"]

    def leave_one_out_does_not_score_a_record_against_itself() -> None:
        """A single record has no OTHER evidence, so it is skipped."""
        predictions, outcomes = leave_one_out_predictions(
            [{"kind": "DRC", "fix": "f", "outcome": "solved"}])
        assert predictions == [] and outcomes == []

    tests = (
        ("records with no fix are ignored", evidence_ignores_records_with_no_fix),
        ("needs_human is not a failure", needs_human_is_not_a_failure),
        ("1/1 does not outrank 40/40", one_of_one_does_not_outrank_forty_of_forty),
        ("an empty memory recommends nothing", an_empty_memory_recommends_nothing),
        ("a thin record is withheld", a_thin_record_is_withheld_not_ranked),
        ("a failing fix is discouraged", a_fix_that_keeps_failing_is_discouraged),
        ("calibration detects overconfidence", calibration_detects_overconfidence),
        ("calibration rewards honesty", calibration_rewards_an_honest_predictor),
        ("leave-one-out skips a lone record",
         leave_one_out_does_not_score_a_record_against_itself),
    )
    for name, test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"SELF-TEST FAIL [{name}]: {exc}", file=sys.stderr)
            raise
        else:
            print(f"SELF-TEST PASS [{name}]")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            run_self_tests()
        except (AssertionError, PriorError, ValueError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    try:
        path = error_memory_path(args.store)
    except VaultWriteRefused as exc:
        parser.error(str(exc))

    memory = ErrorMemory(path)
    records = [record.to_dict() for record in memory.records()]
    if args.kind:
        records = [record for record in records
                   if record.get("kind") == args.kind]

    evidence = gather_evidence(records)
    summary = evidence_summary(evidence)

    status = "GREEN"
    reason_codes: list[str] = []

    if not records:
        # An empty memory is NOT a failure and NOT a pass: nothing was
        # learned because nothing was recorded, and the caller must be able
        # to tell that from "everything we tried failed".
        status = "NO_COVERAGE"
        reason_codes.append("EMPTY_MEMORY")

    try:
        ranked = rank_fixes(evidence, args.kind, args.min_attempts,
                            args.min_lower_bound)
    except PriorError as exc:
        parser.error(str(exc))

    if status == "GREEN" and not ranked["recommended"]:
        status = "NO_COVERAGE"
        reason_codes.append("NO_FIX_WITH_ENOUGH_EVIDENCE")

    calibration_report: dict[str, object] | None = None
    predictions, outcomes = leave_one_out_predictions(records)
    if predictions:
        calibration_report = calibration(predictions, outcomes,
                                         args.calibration_bins)
        if (args.max_calibration_gap is not None
                and float(calibration_report["max_gap"]) >     # type: ignore[arg-type]
                args.max_calibration_gap):
            status = "RED"
            reason_codes.append("CALIBRATION_GAP_EXCEEDED")
        if calibration_report["populated_bins"] < 2:
            # One populated bin cannot show miscalibration at all, so the
            # number must not be read as evidence that the ranking is good.
            reason_codes.append("CALIBRATION_UNINFORMATIVE")
    else:
        reason_codes.append("CALIBRATION_NOT_POSSIBLE")

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "priors",
        "model_class": "observed_frequency",
        "signoff": False,
        "status": status,
        "effective_status": status,
        "reason_codes": sorted(set(reason_codes)),
        "memory_path": str(path),
        "records_considered": len(records),
        "kind_filter": args.kind,
        "thresholds": {
            "min_attempts": args.min_attempts,
            "min_lower_bound": args.min_lower_bound,
        },
        "evidence": summary,
        "ranking": ranked,
        "calibration": calibration_report,
        "limits": LIMITS,
    }

    diag("")
    diag(f"priors: {len(records)} records, "
         f"{summary['distinct_fixes']} distinct fixes")
    diag(f"  recommended            {len(ranked['recommended'])}")
    diag(f"  insufficient evidence  {len(ranked['insufficient_evidence'])}")
    diag(f"  discouraged            {len(ranked['discouraged'])}")
    for item in ranked["recommended"][:5]:
        diag(f"    {item['rank_score']:.3f}  "
             f"{item['successes']}/{item['attempts']}  {item['fix'][:60]}")
    if calibration_report:
        diag(f"  calibration gap        "
             f"{calibration_report['max_gap']:.3f} over "
             f"{calibration_report['populated_bins']} populated bin(s)")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
