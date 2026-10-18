#!/usr/bin/env python3
"""Approximate differential-impedance solver for PCB stackup exploration.

All dimensions are in millimetres and impedances are in ohms.  These closed
form equations are useful for early trade studies; fabrication sign-off still
requires the actual laminate data and a 2-D/3-D field solver.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.si_models import (  # noqa: E402
    DEFAULT_H_CANDIDATES,
    MODEL_CLASS,
    SIGNOFF,
    ModelDomainError,
    require_positive as _require_positive,
    solve_for_target,
    sweep_stackups,
    z0_microstrip,
    zdiff_microstrip,
    zdiff_stripline,
)

SCHEMA_VERSION = "zdiff-solve-report/v1"

def _reference_is_within_ten_percent(calculator=zdiff_microstrip) -> None:
    measured_ohm = 184.0
    calculated_ohm = calculator(0.4, 0.4, 0.035, 1.6, 4.5)
    deviation = abs(calculated_ohm - measured_ohm) / measured_ohm
    assert deviation <= 0.10, (
        "1.6 mm FR4 reference mismatch: "
        f"calculated={calculated_ohm:.2f} ohm, measured={measured_ohm:.2f} ohm, "
        f"deviation={deviation * 100:.2f}%"
    )


def _prove_test_not_empty() -> bool:
    """Prove that the reference test rejects a deliberately wrong coefficient."""

    def deliberately_wrong(w: float, s: float, t: float, h: float, er: float) -> float:
        _require_positive(w=w, s=s, t=t, h=h, er=er)
        wrong_z0 = 60.0 / math.sqrt(er + 1.41) * math.log(
            5.98 * h / (0.8 * w + t)
        )
        return 2.0 * wrong_z0 * (1.0 - 0.347 * math.exp(-2.9 * s / h))

    try:
        _reference_is_within_ten_percent(deliberately_wrong)
    except AssertionError:
        print(
            "FAULT-INJECTION: observed=FAIL, proof=PASS "
            "(deliberately wrong 60/87 coefficient was rejected)"
        )
        return True
    print(
        "FAULT-INJECTION: observed=PASS, proof=FAIL "
        "(reference test did not reject the wrong coefficient)"
    )
    return False


def run_self_tests() -> None:
    """Run the mandatory numerical-reference and monotonicity tests."""

    def reference_test() -> None:
        _reference_is_within_ten_percent()

    def h_monotonicity_test() -> None:
        low = zdiff_microstrip(0.4, 0.4, 0.035, 0.8, 4.5)
        high = zdiff_microstrip(0.4, 0.4, 0.035, 1.6, 4.5)
        assert high > low, f"Zdiff did not increase with h: {low:.3f} -> {high:.3f}"

    def w_monotonicity_test() -> None:
        narrow = zdiff_microstrip(0.3, 0.4, 0.035, 1.6, 4.5)
        wide = zdiff_microstrip(0.5, 0.4, 0.035, 1.6, 4.5)
        assert wide < narrow, f"Zdiff did not decrease with w: {narrow:.3f} -> {wide:.3f}"

    def s_monotonicity_test() -> None:
        close = zdiff_microstrip(0.4, 0.2, 0.035, 1.6, 4.5)
        far = zdiff_microstrip(0.4, 0.6, 0.035, 1.6, 4.5)
        assert far > close, f"Zdiff did not increase with s: {close:.3f} -> {far:.3f}"

    tests = (
        ("reference", reference_test),
        ("h increases Zdiff", h_monotonicity_test),
        ("w decreases Zdiff", w_monotonicity_test),
        ("s increases Zdiff", s_monotonicity_test),
    )
    for name, test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"SELF-TEST FAIL [{name}]: {exc}", file=sys.stderr)
            raise
        else:
            print(f"SELF-TEST PASS [{name}]")

    try:
        assert _prove_test_not_empty(), "fault-injection proof itself failed"
    except AssertionError as exc:
        print(f"SELF-TEST FAIL [fault injection]: {exc}", file=sys.stderr)
        raise


def _format_table(report: dict[str, object]) -> str:
    header = (
        " h (mm) | status      | best W (mm) | best S (mm) | "
        "Zdiff (ohm) | error (%) | reachable span (ohm)"
    )
    lines = [header, "-" * len(header)]
    for row in report["stackups"]:  # type: ignore[index]
        best = row["best"]
        if best is None:
            best_columns = "     -      |      -      |      -      |     -     "
        else:
            best_columns = (
                f"{best['w_mm']:11.3f} | {best['s_mm']:11.3f} | "
                f"{best['zdiff_ohm']:11.2f} | {best['error_percent']:9.3f}"
            )
        low = row["reachable_min_ohm"]
        high = row["reachable_max_ohm"]
        span = "-" if low is None else f"{low:.2f} .. {high:.2f}"
        lines.append(
            f"{row['h_mm']:7.3f} | {row['status']:11s} | "
            f"{best_columns} | {span}"
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=float, nargs="?",
                        help="target differential impedance, ohms "
                             "(required unless --self-test)")
    parser.add_argument("--target", dest="target_flag", type=float,
                        help=argparse.SUPPRESS)   # deprecated flag form
    parser.add_argument("--er", type=float, default=4.5, help="relative permittivity")
    parser.add_argument("--t", type=float, default=0.035, help="copper thickness, mm")
    parser.add_argument("--fab-min-w", type=float, default=0.127, help="minimum trace width, mm")
    parser.add_argument("--fab-min-s", type=float, default=0.127, help="minimum trace gap, mm")
    parser.add_argument("--w-min", type=float, default=0.05, help="search width lower bound, mm")
    parser.add_argument("--w-max", type=float, default=2.0, help="search width upper bound, mm")
    parser.add_argument("--s-min", type=float, default=0.05, help="search gap lower bound, mm")
    parser.add_argument("--s-max", type=float, default=2.0, help="search gap upper bound, mm")
    parser.add_argument("--step", type=float, default=0.005, help="W/S grid step, mm")
    parser.add_argument("--json", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run self-tests and exit")
    parser.add_argument("--selftest", dest="self_test", action="store_true",
                        help=argparse.SUPPRESS)   # deprecated spelling
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    target = args.target if args.target is not None else args.target_flag

    # --self-test is OPT-IN. It used to run on every invocation, and a run
    # with no target printed "self-tests completed" and exited 0 - a tool
    # reporting success while doing no work.
    if args.self_test:
        try:
            run_self_tests()
        except (AssertionError, ValueError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    if target is None:
        # Missing required input is an ERROR (exit 2 via argparse), not a
        # silent success.
        parser.error("target impedance is required; "
                     "pass it positionally (e.g. `zdiff_solver.py 90`) "
                     "or use --self-test")

    try:
        report = sweep_stackups(
            target_ohm=target,
            er=args.er,
            t=args.t,
            w_range=(args.w_min, args.w_max, args.step),
            s_range=(args.s_min, args.s_max, args.step),
            fab_min_w=args.fab_min_w,
            fab_min_s=args.fab_min_s,
        )
    except ValueError as exc:
        parser.error(str(exc))

    # Every output carries the schema and the non-signoff labelling that this
    # tool's own README always claimed it did but the code never emitted.
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "zdiff_solver",
        "model_class": MODEL_CLASS,
        "signoff": SIGNOFF,
        **report,
    }

    # Human table goes to stderr; stdout stays machine-readable JSON only.
    diag("")
    diag(_format_table(report))
    write_report(report, args.json)
    emit(report)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
