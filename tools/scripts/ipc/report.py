"""Report envelopes shared by the IPC tools.

Two shapes, because the IPC tools split cleanly into two kinds:

``calc_report``  a CALCULATOR answers a question ("how wide must this trace
                 be?"). There is no PASS/FAIL, so it always exits 0 on a
                 successful computation and 2 on bad input. Schema:
                 ``ipc-calc-report/v1``.

``check_report`` a RULE ENGINE measures supplied observations against a
                 standard's limits and emits ``common.findings.Finding``
                 objects, so scanned==0 is NO_COVERAGE and never a silent
                 PASS. Schema: ``ipc-check-report/v1``.

Both carry ``limits``: the honesty notes that the archive kept only in
module docstrings, where a machine consumer could never see them. Losing
those notes is how a screening number turns into a fake compliance claim.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import FAIL, NO_COVERAGE, summarise  # noqa: E402

CALC_SCHEMA = "ipc-calc-report/v1"
CHECK_SCHEMA = "ipc-check-report/v1"


def calc_report(tool: str, standard: str, inputs: dict[str, Any],
                results: dict[str, Any], limits: Sequence[str],
                *, model_class: str, signoff: bool,
                **extra: Any) -> dict[str, Any]:
    """Envelope for a calculator run."""
    report: dict[str, Any] = {
        "schema_version": CALC_SCHEMA,
        "tool": tool,
        "standard": standard,
        "model_class": model_class,
        "signoff": signoff,
        "inputs": inputs,
        "results": results,
        "limits": list(limits),
    }
    report.update(extra)
    return report


def check_report(tool: str, standard: str, subject: str,
                 params: dict[str, Any], findings: Iterable,
                 limits: Sequence[str], **extra: Any) -> dict[str, Any]:
    """Envelope for a rule-engine run.

    ``verdict`` is the combined answer and deliberately has three values,
    not two. The archive's ``genel_sonuc`` established this and it is worth
    keeping: a check that received no observations is ``NEEDS_HUMAN``, not
    ``PASS``. Silently treating "nothing was measured" as success is the
    failure mode the whole findings contract exists to prevent.
    """
    findings = list(findings)
    summary = summarise(findings)
    if summary[FAIL]:
        verdict = "FAIL"
    elif summary[NO_COVERAGE]:
        verdict = "NEEDS_HUMAN"
    else:
        verdict = "PASS"
    report: dict[str, Any] = {
        "schema_version": CHECK_SCHEMA,
        "tool": tool,
        "standard": standard,
        "subject": subject,
        "params": params,
        "checks": findings,
        "summary": summary,
        "verdict": verdict,
        "limits": list(limits),
    }
    report.update(extra)
    return report


def check_exit_code(report: dict[str, Any]) -> int:
    """0 only for a real PASS.

    NOTE — this DIFFERS from ``common.findings.board_exit_code``, on
    purpose. board_exit_code returns 0 for NO_COVERAGE because via_stub's
    published contract promised that to its callers and changing it would
    be a caller-visible policy break. These IPC tools are NEW to the bundle
    and have no such legacy, so they take the stricter reading that the
    rest of the bundle argues for: an empty check is not a success.
    """
    return 0 if report["verdict"] == "PASS" else 1
