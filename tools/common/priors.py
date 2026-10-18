"""Empirical priors over remembered fixes: which one has actually worked.

WHAT THIS IS, AND WHAT IT REFUSES TO BE
---------------------------------------
Queue item #63 asks for a "learning layer", with Vivado ML Edition, Cadence
Spectre FMC and SECDA-DSE's cost-model database as the precedents. Two of
those three are trained models. This is NOT one, and saying so is the whole
design:

  * There is no training corpus here. A model fitted to a handful of records
    from one board would be a confident-looking function of noise, and this
    bundle's standing rule is that a number nobody measured does not get
    reported as if somebody had.
  * The thing SECDA-DSE actually contributed - the part that is transferable
    without a dataset - is the COST-MODEL DATABASE: record what each attempt
    cost and what it achieved, and let the accumulated record rank the next
    attempt. That is measurement, not inference, and it is the prerequisite
    for anything else. #69 exists for the same reason: you cannot improve
    what you have not measured.
  * The APRIL session named explainability as the third limitation of LLMs in
    hardware design. A frequency table with a confidence interval is the
    opposite of a black box: every ranking here can be traced to the exact
    records that produced it, and the report carries them.

So this layer estimates, per (error class, fix), the observed success rate
with a Wilson interval, and ranks candidate fixes by the LOWER BOUND of that
interval rather than by the point estimate.

WHY THE LOWER BOUND, NOT THE MEAN
---------------------------------
One success out of one attempt is a 100% success rate. So is forty out of
forty. Ranking by the mean puts them level and lets a single lucky record
outrank a well-evidenced fix - which is exactly how a "learning" layer starts
recommending noise. The Wilson lower bound at 95% is 0.21 for 1/1 and 0.91 for
40/40, so evidence strength enters the ordering instead of being discarded.

COLD START IS A REFUSAL, NOT A GUESS
------------------------------------
Below ``min_attempts`` observations a fix is reported as INSUFFICIENT_EVIDENCE
and is not ranked at all. An empty memory therefore produces no
recommendation rather than an arbitrary one, and the caller can tell the
difference between "nothing has worked" and "nothing has been tried".

CALIBRATION IS MEASURED, NOT ASSUMED
------------------------------------
``calibration`` bins the predictions and compares each bin's predicted rate
against what actually happened in it. A ranking that is never checked this way
cannot be distinguished from a plausible-looking one, and the gap it reports
is the honest statement of how much the ranking is worth so far.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# Reused rather than reimplemented: the same interval that keeps the Monte
# Carlo yields honest keeps these success rates honest.
from .tolerance import _wilson_interval

SUCCESS_OUTCOME = "solved"
FAILURE_OUTCOME = "failed"
NEUTRAL_OUTCOME = "needs_human"

# A fix seen fewer times than this is not ranked. Not a tuning knob reached
# for lightly: lowering it trades a refusal for a guess.
DEFAULT_MIN_ATTEMPTS = 3

# A fix whose success rate could plausibly be below this is not recommended
# even when it has been seen enough times.
DEFAULT_MIN_LOWER_BOUND = 0.10


class PriorError(ValueError):
    """The evidence cannot support the requested estimate."""


@dataclass
class Evidence:
    """The observed record for one (error class, fix) pair."""

    kind: str
    fix: str
    successes: int = 0
    failures: int = 0
    needs_human: int = 0
    signatures: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)

    @property
    def attempts(self) -> int:
        """Successes and failures only.

        ``needs_human`` is deliberately excluded from the denominator: it
        records that the fix was not decided, not that it was tried and lost.
        Counting it as a failure would penalise honest escalation, which is
        the behaviour this bundle most wants to keep.
        """
        return self.successes + self.failures

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.attempts if self.attempts else None

    def interval(self) -> tuple[float, float]:
        if not self.attempts:
            raise PriorError(f"{self.kind}/{self.fix}: no attempts to bound")
        return _wilson_interval(self.successes, self.attempts)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "fix": self.fix,
            "successes": self.successes,
            "failures": self.failures,
            "needs_human": self.needs_human,
            "attempts": self.attempts,
            "success_rate": self.success_rate,
            "distinct_signatures": len(self.signatures),
            "projects": sorted(self.projects),
        }
        if self.attempts:
            low, high = self.interval()
            payload["ci95_low"] = low
            payload["ci95_high"] = high
        return payload


def _occurrences(record: Mapping[str, object]) -> int:
    """How many observations one stored record stands for. Absent means 1."""
    try:
        value = int(record.get("occurrences", 1))       # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def gather_evidence(records: Iterable[Mapping[str, object]]
                    ) -> dict[tuple[str, str], Evidence]:
    """Fold remembered records into per-(kind, fix) evidence.

    Keyed on the FIX text rather than the signature on purpose: the question
    a prior answers is "does this fix work", and a fix that works across many
    distinct signatures is better evidenced than one that worked forty times
    on the same violation. ``distinct_signatures`` carries that difference
    into the report instead of letting repetition inflate the count.
    """
    evidence: dict[tuple[str, str], Evidence] = {}
    for record in records:
        fix = str(record.get("fix", "")).strip()
        if not fix:
            # A record with no fix says an error was seen, not that anything
            # was tried; it cannot support a recommendation.
            continue
        kind = str(record.get("kind", "OTHER"))
        key = (kind, fix)
        entry = evidence.setdefault(key, Evidence(kind=kind, fix=fix))
        # The store DEDUPLICATES: one record can stand for many observations,
        # and its occurrence counter is the only place the frequency survives.
        # Counting records instead would make a fix that worked forty times
        # look exactly like one that worked once, which is the difference this
        # whole layer exists to see.
        weight = _occurrences(record)
        outcome = str(record.get("outcome", NEUTRAL_OUTCOME))
        if outcome == SUCCESS_OUTCOME:
            entry.successes += weight
        elif outcome == FAILURE_OUTCOME:
            entry.failures += weight
        else:
            entry.needs_human += weight
        signature = str(record.get("signature", ""))
        if signature:
            entry.signatures.add(signature)
        project = str(record.get("project", ""))
        if project:
            entry.projects.add(project)
    return evidence


def rank_fixes(evidence: Mapping[tuple[str, str], Evidence],
               kind: str | None = None,
               min_attempts: int = DEFAULT_MIN_ATTEMPTS,
               min_lower_bound: float = DEFAULT_MIN_LOWER_BOUND
               ) -> dict[str, list[dict[str, object]]]:
    """Rank by the Wilson LOWER BOUND, and say what was withheld and why.

    Three lists, never merged: what is recommended, what has been tried too
    few times to rank, and what has been tried enough and does not work.
    """
    if min_attempts < 1:
        raise PriorError("min_attempts must be at least 1")

    recommended: list[dict[str, object]] = []
    insufficient: list[dict[str, object]] = []
    discouraged: list[dict[str, object]] = []

    for entry in evidence.values():
        if kind is not None and entry.kind != kind:
            continue
        payload = entry.to_dict()
        if entry.attempts < min_attempts:
            payload["withheld_because"] = "INSUFFICIENT_EVIDENCE"
            payload["attempts_needed"] = min_attempts - entry.attempts
            insufficient.append(payload)
            continue
        low, _high = entry.interval()
        payload["rank_score"] = low
        if low < min_lower_bound:
            payload["withheld_because"] = "OBSERVED_FAILURE_RATE"
            discouraged.append(payload)
        else:
            recommended.append(payload)

    recommended.sort(key=lambda item: (-float(item["rank_score"]),      # type: ignore[arg-type]
                                       -int(item["attempts"])))         # type: ignore[arg-type]
    insufficient.sort(key=lambda item: -int(item["attempts"]))          # type: ignore[arg-type]
    discouraged.sort(key=lambda item: float(item["rank_score"]))        # type: ignore[arg-type]
    return {
        "recommended": recommended,
        "insufficient_evidence": insufficient,
        "discouraged": discouraged,
    }


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
def calibration(predictions: Sequence[float], outcomes: Sequence[bool],
                bins: int = 5) -> dict[str, object]:
    """Predicted rate vs observed rate, binned.

    A ranking nobody checks this way is indistinguishable from one that looks
    plausible. ``max_gap`` is the honest headline: it is how far the layer's
    stated confidence has been from what actually happened.

    ``brier`` is the mean squared error of the predictions; unlike accuracy it
    punishes confident wrong answers harder than hedged ones, which is the
    behaviour that matters when a recommendation drives a re-route.
    """
    if len(predictions) != len(outcomes):
        raise PriorError(
            f"predictions and outcomes differ in length: "
            f"{len(predictions)} vs {len(outcomes)}")
    if not predictions:
        raise PriorError("no predictions to calibrate")
    if bins < 1:
        raise PriorError("bins must be at least 1")
    for value in predictions:
        if not 0.0 <= value <= 1.0:
            raise PriorError(f"prediction {value} is outside [0, 1]")

    buckets: list[dict[str, object]] = []
    max_gap = 0.0
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        selected = [
            (prediction, outcome)
            for prediction, outcome in zip(predictions, outcomes)
            if (low <= prediction < high) or (index == bins - 1
                                              and prediction == 1.0)
        ]
        if not selected:
            buckets.append({"bin_low": low, "bin_high": high, "count": 0,
                            "predicted": None, "observed": None, "gap": None})
            continue
        predicted = sum(p for p, _ in selected) / len(selected)
        observed = sum(1 for _, o in selected if o) / len(selected)
        gap = abs(predicted - observed)
        max_gap = max(max_gap, gap)
        buckets.append({
            "bin_low": low, "bin_high": high, "count": len(selected),
            "predicted": predicted, "observed": observed, "gap": gap,
        })

    brier = sum((p - (1.0 if o else 0.0)) ** 2
                for p, o in zip(predictions, outcomes)) / len(predictions)
    return {
        "samples": len(predictions),
        "bins": buckets,
        "max_gap": max_gap,
        "brier": brier,
        # A single bucket with everything in it cannot show miscalibration,
        # so the report says how many buckets actually had data.
        "populated_bins": sum(1 for b in buckets if b["count"]),
    }


def leave_one_out_predictions(records: Sequence[Mapping[str, object]],
                              min_attempts: int = 1
                              ) -> tuple[list[float], list[bool]]:
    """Honest self-evaluation: predict each record from all the OTHERS.

    Scoring the priors on the records that built them would report near
    perfect calibration for any amount of data, which is the classic way a
    learning layer flatters itself. Holding each record out makes the check
    mean something.

    Records whose fix has too little OTHER evidence are skipped rather than
    predicted at a default rate, and the caller sees how many were skipped.
    """
    predictions: list[float] = []
    outcomes: list[bool] = []
    for index, held_out in enumerate(records):
        outcome = str(held_out.get("outcome", NEUTRAL_OUTCOME))
        if outcome not in (SUCCESS_OUTCOME, FAILURE_OUTCOME):
            continue
        # Hold out ONE observation, not the whole deduplicated record: a
        # record standing for forty observations must leave thirty-nine
        # behind, otherwise the best-evidenced fixes are the ones this check
        # silently refuses to score.
        rest: list[Mapping[str, object]] = []
        for position, record in enumerate(records):
            if position != index:
                rest.append(record)
                continue
            remaining = _occurrences(record) - 1
            if remaining >= 1:
                reduced = dict(record)
                reduced["occurrences"] = remaining
                rest.append(reduced)
        evidence = gather_evidence(rest)
        key = (str(held_out.get("kind", "OTHER")),
               str(held_out.get("fix", "")).strip())
        entry = evidence.get(key)
        if entry is None or entry.attempts < min_attempts:
            continue
        rate = entry.success_rate
        if rate is None:
            continue
        predictions.append(rate)
        outcomes.append(outcome == SUCCESS_OUTCOME)
    return predictions, outcomes


def evidence_summary(evidence: Mapping[tuple[str, str], Evidence]
                     ) -> dict[str, object]:
    """Totals, plus the numbers that say how much the ranking is worth."""
    entries = list(evidence.values())
    attempts = sum(entry.attempts for entry in entries)
    successes = sum(entry.successes for entry in entries)
    return {
        "distinct_fixes": len(entries),
        "total_attempts": attempts,
        "total_successes": successes,
        "total_needs_human": sum(entry.needs_human for entry in entries),
        "overall_success_rate": (successes / attempts) if attempts else None,
        "kinds": sorted({entry.kind for entry in entries}),
        "fixes_with_enough_evidence": sum(
            1 for entry in entries if entry.attempts >= DEFAULT_MIN_ATTEMPTS),
    }
