#!/usr/bin/env python3
"""Intra-pair skew in PICOSECONDS, derived from a measure_board report.

WHY THIS EXISTS
---------------
``skills/dogrulama-matrisi/SKILL.md`` requires intra/inter-pair skew in **ps**.
``board/measure_board.py`` measures it in **mm** - it counts copper, which is
the only thing a board file actually contains. Nothing in this repo closed the
gap, so the matrix row was being answered with a millimetre number wearing a
picosecond label. A millimetre is not a picosecond until a velocity says so,
and the whole question is where that velocity comes from.

WHERE THE VELOCITY COMES FROM
-----------------------------
Exactly the two honest routes ``coupon_plan.py`` offers, and the same third
one it refuses:

  --solve                     run the 2-D field solver (``common/field2d.py``)
                              on the declared cross-section. The report records
                              it as solved.
  --er-eff X                  state it, WITH --er-eff-source naming where the
                              number came from. Recorded as stated, with the
                              provenance string attached.
  (default it)                not offered.

The refused third route is not hypothetical here. ``common/escape_geom.py``
carries ``FR4_MICROSTRIP_VELOCITY_MM_NS = 167.6`` and its own docstring says
it is "a single typical FR4 microstrip number (eps_eff ~3.2). Real velocity is
stack-up and frequency dependent." Wiring that into this tool as a default
would set EVERY skew verdict on EVERY board from one unmeasured constant, and
every one of them would look like an answer. This module therefore does not
import ``escape_geom`` at all, and the test suite asserts that it cannot.

WHAT IT REFUSES TO DO
---------------------
  * convert without a velocity - no er_eff, no ps, no report
  * convert a pair that has no copper. ``measure_board`` reports
    ``skew_mm = |0 - 0| = 0.0`` for an unrouted pair, which reads as a
    PERFECTLY matched pair. That is the loudest silent pass in the chain, and
    here it becomes NO_COVERAGE with the reason attached.
  * judge a measurement against a budget nobody sourced. Without
    ``--budget-ps`` AND ``--budget-source`` the metric is emitted with
    ``limit: null`` and verdict INCONCLUSIVE: recorded, and honestly unjudged.
    There is no default budget, and INCONCLUSIVE is not a pass (exit 1).

OUTPUT
------
``length-report/v1``
(``skills/highspeed-routing/references/length_report.schema.json``), emitted
verbatim - that schema is ``additionalProperties: false`` at every level, so
this tool adds no keys of its own. ``board_sha256`` is copied from the parent
report's ``design.sha256``, which is what ties the derived picoseconds back to
the board the millimetres were measured on. The parent millimetre value, the
parent report path and the er_eff provenance travel in
``semantics.metric_definition``; ``semantics.epsilon_eff`` and
``semantics.velocity_mm_per_ns`` carry the conversion input as numbers.

CLI:
    skew_timing.py MEASURE_REPORT.json
        [--pair P_NET:N_NET ...]
        (--er-eff X --er-eff-source "TEXT" | --solve --structure S ...)
        [--budget-ps PS --budget-source "TEXT"]
        [--json REPORT]

Exit codes: 0 GREEN, 1 RED / NO_COVERAGE / INCONCLUSIVE, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.coupon import (  # noqa: E402
    CouponError, propagation_velocity_mm_per_ps,
)
from common.hashing import sha256_file  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_ERROR, diag, emit, error_report, run_cli, status_exit_code,
    write_report,
)

SCHEMA_VERSION = "length-report/v1"
TOOL_VERSION = "skew-timing/v1"
TOOL_NAME = "skew_timing.py"
LOCATOR = "tools/scripts/board/skew_timing.py"
METRIC_ID = "intra_pair_skew_ps"
REPORT_KIND = "length"
UNITS = "ps"

SELF = Path(__file__).resolve()
DIGEST = re.compile(r"^[0-9a-f]{64}$")

STRUCTURES = ("microstrip", "coupled-microstrip", "stripline",
              "coupled-stripline")

# The refusal, in one place so the CLI and the library say the same thing.
NO_VELOCITY = (
    "no er_eff, so no conversion. Pass --solve to compute it from the "
    "declared cross-section, or --er-eff together with --er-eff-source to "
    "state it and say where it came from. It is not defaulted: a millimetre "
    "of copper is not a picosecond until a velocity says so, er_eff is "
    "nowhere near er for a microstrip, and the one typical FR4 constant in "
    "this repo (escape_geom.FR4_MICROSTRIP_VELOCITY_MM_NS) would silently "
    "set every skew verdict on the board from a number nobody measured"
)


class SkewTimingError(RuntimeError):
    """The derivation could not be performed."""


# --------------------------------------------------------------------------- #
# Conversion input: solved, stated, or refused
# --------------------------------------------------------------------------- #
def _er_eff_for(args: argparse.Namespace) -> tuple[float, str]:
    """(er_eff, source) - or raise. There is no third return."""
    if args.er_eff is not None:
        if not (args.er_eff_source or "").strip():
            raise SkewTimingError(
                "--er-eff was given without --er-eff-source. A stated er_eff "
                "with no stated origin is indistinguishable from a guess, and "
                "the report has a field for the origin precisely so it cannot "
                "be omitted")
        return args.er_eff, f"stated via --er-eff: {args.er_eff_source.strip()}"
    if not args.solve:
        raise SkewTimingError(NO_VELOCITY)

    missing = [name for name, value in (("--w-mm", args.w_mm),
                                        ("--t-mm", args.t_mm),
                                        ("--h-mm", args.h_mm),
                                        ("--er", args.er)) if value is None]
    if args.structure is None:
        missing.insert(0, "--structure")
    if args.structure in ("coupled-microstrip", "coupled-stripline") \
            and args.s_mm is None:
        missing.append("--s-mm")
    if missing:
        raise SkewTimingError(
            "--solve needs the cross-section it is supposed to solve; "
            f"missing {', '.join(missing)}. None of these are defaulted: the "
            "solver would happily return an er_eff for a geometry that is not "
            "the board's")

    from common.field2d import (  # noqa: PLC0415 - optional numpy dependency
        coupled_microstrip, coupled_stripline, microstrip, solve_line,
        stripline,
    )
    shared = {"cells_per_feature": args.cells_per_feature,
              "max_nodes": args.max_nodes}
    if args.structure == "microstrip":
        section = microstrip(args.w_mm, args.t_mm, args.h_mm, args.er,
                             **shared)
        return solve_line(section).er_eff(), "field_solver 2-D solve"
    if args.structure == "stripline":
        section = stripline(args.w_mm, args.t_mm, args.h_mm, args.er, **shared)
        return solve_line(section).er_eff(), "field_solver 2-D solve"
    if args.structure == "coupled-microstrip":
        section = coupled_microstrip(args.w_mm, args.s_mm, args.t_mm,
                                     args.h_mm, args.er, **shared)
    else:
        section = coupled_stripline(args.w_mm, args.s_mm, args.t_mm,
                                    args.h_mm, args.er, **shared)
    # A differential pair propagates in the odd mode; the even-mode er_eff
    # would be the wrong velocity for an intra-pair skew question.
    return (solve_line(section).er_eff_odd(),
            "field_solver 2-D solve (odd mode)")


# --------------------------------------------------------------------------- #
# The parent report
# --------------------------------------------------------------------------- #
def load_parent(path: Path) -> dict:
    try:
        parent = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SkewTimingError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SkewTimingError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(parent, dict):
        raise SkewTimingError(f"{path}: expected a report object")
    return parent


def parent_board_sha256(parent: dict) -> str:
    """The digest that binds these picoseconds to a board, or a refusal.

    ``length-report/v1`` makes ``board_sha256`` required and pins it to 64 hex
    characters. A derived report with no digest cannot be checked against the
    board it describes; inventing one would make it look checkable.
    """
    digest = ((parent.get("design") or {}).get("sha256"))
    if not isinstance(digest, str) or not DIGEST.match(digest):
        raise SkewTimingError(
            "the parent report carries no usable design.sha256. Without it "
            "the derived report cannot be bound to a board, and "
            "length-report/v1 requires the field. Re-run measure_board.py "
            "against the board file to produce one")
    return digest


def parent_pairs(parent: dict, requested: Sequence[str]) -> dict:
    """``measurements.pair_skew_mm`` filtered to the requested pairs."""
    skews = (parent.get("measurements") or {}).get("pair_skew_mm")
    if not isinstance(skews, dict) or not skews:
        raise SkewTimingError(
            "the parent report has no measurements.pair_skew_mm. Re-run "
            "measure_board.py with at least one --pair P_NET:N_NET; there is "
            "nothing here to convert")
    if not requested:
        return dict(skews)
    unknown = [spec for spec in requested if spec not in skews]
    if unknown:
        raise SkewTimingError(
            f"pair(s) {unknown} are not in the parent report "
            f"(it measured {sorted(skews)}). This tool converts measurements; "
            "it cannot produce one that was never taken")
    return {spec: skews[spec] for spec in requested}


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #
def skew_ps(skew_mm: float, er_eff: float) -> float:
    """skew_ps = skew_mm / v_p, with v_p = c / sqrt(er_eff) in mm/ps.

    ``propagation_velocity_mm_per_ps`` is the repo's one velocity function
    (``common/coupon.py``); it is exact for a TEM mode and it raises rather
    than accepting a non-positive er_eff.
    """
    return skew_mm / propagation_velocity_mm_per_ps(er_eff)


def _semantics(spec: str, entry: dict, er_eff: float, source: str,
               parent_path: Path) -> dict:
    p_mm = entry.get("p_mm")
    n_mm = entry.get("n_mm")
    return {
        "metric_definition": (
            "intra-pair skew in time: skew_ps = skew_mm / v_p, "
            "v_p = c / sqrt(er_eff). "
            f"parent_metric=measurements.pair_skew_mm[{spec}].skew_mm; "
            f"parent_skew_mm={entry.get('skew_mm')}; "
            f"parent_p_mm={p_mm}; parent_n_mm={n_mm}; "
            f"parent_report={parent_path}; parent_tool=measure_board.py; "
            f"er_eff_source={source}. "
            "One quasi-static TEM velocity is applied to the whole net: layer "
            "changes, vias, dispersion and copper roughness are not modelled, "
            "so a pair that does not run entirely in the declared "
            "cross-section is converted with a velocity it never had."),
        "distance_semantics": "centerline",
        # measure_board keeps PCB_ARC in its track filter and uses
        # GetLength(), which returns arc length rather than the chord. Both
        # fields are therefore knowable, not assumed: a chord-measuring parent
        # would have to say segment_only / arc_included false here.
        "geometry_model": "true_arc",
        "arc_included": True,
        "approximation": True,
        "epsilon_eff": er_eff,
        "velocity_mm_per_ns": propagation_velocity_mm_per_ps(er_eff) * 1000.0,
    }


def build_metrics(pairs: dict, er_eff: float, source: str, parent_path: Path,
                  budget_ps: float | None, budget_source: str | None
                  ) -> tuple[list[dict], list[str]]:
    metrics: list[dict] = []
    excluded_reasons: list[str] = []
    for spec in sorted(pairs):
        entry = pairs[spec] if isinstance(pairs[spec], dict) else {}
        semantics = _semantics(spec, entry, er_eff, source, parent_path)
        metric = {
            "metric_id": METRIC_ID,
            "net_or_pair": spec,
            "region": "whole",
            "semantics": semantics,
            "limit": budget_ps,
            "units": UNITS,
            "measured": None,
            "verdict": "NO_COVERAGE",
            "verdict_reason": None,
        }

        p_mm = entry.get("p_mm")
        n_mm = entry.get("n_mm")
        raw_mm = entry.get("skew_mm")
        unmeasurable = (
            not isinstance(p_mm, (int, float))
            or not isinstance(n_mm, (int, float))
            or not isinstance(raw_mm, (int, float))
            or p_mm <= 0 or n_mm <= 0)

        if unmeasurable:
            reason = (
                f"{spec}: the parent measurement has no copper on one or both "
                f"nets (p_mm={p_mm}, n_mm={n_mm}). measure_board reports "
                "skew_mm = |0 - 0| = 0.0 there, which reads as a perfectly "
                "matched pair; it is an unrouted pair. Not converted")
            metric["verdict_reason"] = reason
            excluded_reasons.append(reason)
            metrics.append(metric)
            continue

        metric["measured"] = round(skew_ps(float(raw_mm), er_eff), 4)
        if budget_ps is None:
            metric["verdict"] = "INCONCLUSIVE"
            metric["verdict_reason"] = (
                "recorded and not judged: no skew budget was supplied. Pass "
                "--budget-ps together with --budget-source. This tool has no "
                "default budget - one would decide the verdict of every pair "
                "on every board from a number nobody sourced")
        elif metric["measured"] > budget_ps:
            metric["verdict"] = "FAIL"
            metric["verdict_reason"] = (
                f"{metric['measured']} ps > budget {budget_ps} ps "
                f"(budget source: {budget_source})")
        else:
            metric["verdict"] = "PASS"
            metric["verdict_reason"] = (
                f"{metric['measured']} ps <= budget {budget_ps} ps "
                f"(budget source: {budget_source}). uncertainty and guard_band "
                "are unset: no er_eff tolerance was supplied, so this margin "
                "carries no stated error bar")
        metrics.append(metric)
    return metrics, excluded_reasons


def overall_status(metrics: Sequence[dict]) -> str:
    verdicts = {m["verdict"] for m in metrics}
    if "FAIL" in verdicts:
        return "RED"
    if "NO_COVERAGE" in verdicts:
        return "NO_COVERAGE"
    if "INCONCLUSIVE" in verdicts:
        return "INCONCLUSIVE"
    return "GREEN"


def build_report(parent: dict, parent_path: Path, requested: Sequence[str],
                 er_eff: float, source: str, budget_ps: float | None,
                 budget_source: str | None, argv: Sequence[str]) -> dict:
    board_sha256 = parent_board_sha256(parent)
    pairs = parent_pairs(parent, requested)
    metrics, excluded_reasons = build_metrics(
        pairs, er_eff, source, parent_path, budget_ps, budget_source)

    scanned = sum(1 for m in metrics if m["measured"] is not None)
    status = overall_status(metrics)
    report = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "board_sha256": board_sha256,
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "sha256": sha256_file(SELF),
            "argv": [LOCATOR, *[str(a) for a in argv]],
            "exit_code": status_exit_code(status),
        },
        "metrics": metrics,
        "coverage": {
            "eligible": len(metrics),
            "scanned": scanned,
            "excluded": len(metrics) - scanned,
            "violations": sum(1 for m in metrics if m["verdict"] == "FAIL"),
            "excluded_reasons": excluded_reasons,
        },
        "status": status,
    }
    return report


# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", type=Path,
                        help="a measure_board.py report JSON (the mm evidence)")
    parser.add_argument("--pair", action="append", default=[], dest="pairs",
                        help="convert only this P_NET:N_NET; repeatable. "
                             "Default: every pair in the parent report")

    source = parser.add_argument_group(
        "where the propagation velocity comes from (one is REQUIRED)")
    source.add_argument("--solve", action="store_true",
                        help="compute er_eff with the 2-D field solver")
    source.add_argument("--er-eff", type=float, dest="er_eff",
                        help="state er_eff instead of solving for it; "
                             "requires --er-eff-source")
    source.add_argument("--er-eff-source", dest="er_eff_source",
                        help="where the stated er_eff came from (stackup "
                             "report, fab datasheet + date, TDR coupon ...). "
                             "Recorded verbatim in the derived report")

    section = parser.add_argument_group("cross-section for --solve")
    section.add_argument("--structure", choices=STRUCTURES)
    section.add_argument("--w-mm", type=float, dest="w_mm")
    section.add_argument("--s-mm", type=float, dest="s_mm",
                         help="edge-to-edge gap; coupled structures only")
    section.add_argument("--t-mm", type=float, dest="t_mm")
    section.add_argument("--h-mm", type=float, dest="h_mm",
                         help="dielectric height (microstrip) or total "
                              "plane-to-plane spacing b (stripline)")
    section.add_argument("--er", type=float)
    section.add_argument("--cells-per-feature", type=int, default=3,
                         dest="cells_per_feature")
    section.add_argument("--max-nodes", type=int, default=80_000,
                         dest="max_nodes")

    budget = parser.add_argument_group(
        "the budget, if there is a sourced one (optional, never defaulted)")
    budget.add_argument("--budget-ps", type=float, dest="budget_ps",
                        help="intra-pair skew budget in ps; requires "
                             "--budget-source")
    budget.add_argument("--budget-source", dest="budget_source",
                        help="where the budget came from (PHY datasheet "
                             "section + revision, standard clause, ...)")

    parser.add_argument("--json", type=Path, dest="json_path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.budget_ps is not None and not (args.budget_source or "").strip():
        parser.error(
            "--budget-ps requires --budget-source. An unsourced budget is not "
            "a limit, it is a preference, and it would be indistinguishable "
            "from one in the report")
    if args.budget_source and args.budget_ps is None:
        parser.error("--budget-source was given without --budget-ps")

    try:
        er_eff, source = _er_eff_for(args)
    except (SkewTimingError, CouponError) as exc:
        parser.error(str(exc))

    try:
        parent = load_parent(args.report)
        report = build_report(parent, Path(args.report), args.pairs, er_eff,
                              source, args.budget_ps,
                              (args.budget_source or "").strip() or None, argv)
    except (SkewTimingError, CouponError) as exc:
        emit(error_report(SCHEMA_VERSION, "PARENT_REPORT_UNUSABLE", str(exc)))
        diag(f"ERROR: {exc}")
        return EXIT_ERROR

    diag("")
    diag(f"skew timing: {len(report['metrics'])} pair(s) from {args.report}")
    diag(f"  board sha256 {report['board_sha256'][:12]}...  "
         f"er_eff {er_eff:.4f} ({source})")
    for metric in report["metrics"]:
        shown = ("        -" if metric["measured"] is None
                 else f"{metric['measured']:9.2f}")
        diag(f"  {metric['verdict']:13s} {metric['net_or_pair']:24s} "
             f"{shown} ps")
    if args.budget_ps is None:
        diag("  no budget supplied: the measurements are recorded, not judged")
    diag(f"  status: {report['status']}")

    write_report(report, args.json_path)
    emit(report)
    return status_exit_code(report["status"])


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
