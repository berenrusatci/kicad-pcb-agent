#!/usr/bin/env python3
"""Plan the impedance test coupons, and refuse to ship a board without them.

RUNS BEFORE GERBER OUTPUT, ON PURPOSE
-------------------------------------
Correlating simulation against measurement happens after fabrication. The
decision that makes it possible happens before: a coupon must be on the panel.
Miss it and the board returns unmeasurable, and the only remedy is another fab
cycle. So this is an exit gate, not a report.

WHERE er_eff COMES FROM
-----------------------
The coupon length depends on propagation velocity, which depends on er_eff.
There are two honest ways to get it and one dishonest one:

  --solve          run the 2-D field solver on the declared cross-section.
                   Preferred, and the report records it as the source.
  --er-eff X       state it. Recorded as stated, not solved.
  (guess it)       not offered. er_eff for a microstrip is nowhere near er,
                   and defaulting it would silently set every coupon length.

WHAT THE GATE REFUSES
---------------------
  * a controlled-impedance class with no coupon at all
  * a coupon shorter than the TDR can resolve - the instrument reads a
    transition and reports a plausible impedance, which is worse than no
    reading because it looks like data
  * a coupon whose cross-section differs from the production one: it measures
    something, but not the thing the board is being validated for

Exit contract: 0 pass / 1 fail or NO_COVERAGE / 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.coupon import (  # noqa: E402
    DEFAULT_RESOLUTION_MULTIPLE,
    CouponError,
    ImpedanceClass,
    coverage,
    minimum_coupon_length_mm,
    parse_requirements,
    plan_coupon,
    propagation_velocity_mm_per_ps,
    tdr_resolution_mm,
)
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)

SCHEMA_VERSION = "coupon-plan-report/v1"

LIMITS = [
    "plans the coupon; does NOT lay it out, place it on the panel or emit "
    "Gerber - a human or the CAD step does that",
    "a coupon shares the stackup, not the routing: measuring it validates the "
    "cross-section, not the production net",
    "the TDR rise time is a required input and is never defaulted; a 20 ps "
    "head and a 200 ps head differ by 10x in the length they need",
    "probe launch, pad geometry and reference-via placement are "
    "instrument-specific and are not derived here",
    "er_eff is either solved or stated, and the report says which",
]


def _er_eff_for(requirement: ImpedanceClass, args: argparse.Namespace
                ) -> tuple[float, str]:
    if args.er_eff is not None:
        return args.er_eff, "stated via --er-eff"
    if not args.solve:
        raise CouponError(
            f"{requirement.name}: no er_eff. Pass --solve to compute it from "
            "the declared cross-section, or --er-eff to state it. It is not "
            "defaulted: er_eff is nowhere near er for a microstrip and "
            "guessing it would set every coupon length silently")

    from common.field2d import (
        coupled_microstrip, coupled_stripline, microstrip, solve_line,
        stripline,
    )
    shared = {"cells_per_feature": args.cells_per_feature,
              "max_nodes": args.max_nodes}
    if requirement.structure == "microstrip":
        section = microstrip(requirement.w_mm, requirement.t_mm,
                             requirement.h_mm, requirement.er, **shared)
        return solve_line(section).er_eff(), "field_solver 2-D solve"
    if requirement.structure == "coupled-microstrip":
        section = coupled_microstrip(requirement.w_mm, requirement.s_mm,
                                     requirement.t_mm, requirement.h_mm,
                                     requirement.er, **shared)
        # The odd mode is the one a differential coupon is measured in.
        return solve_line(section).er_eff_odd(), "field_solver 2-D solve (odd mode)"
    if requirement.structure == "stripline":
        section = stripline(requirement.w_mm, requirement.t_mm,
                            requirement.h_mm, requirement.er, **shared)
        return solve_line(section).er_eff(), "field_solver 2-D solve"
    section = coupled_stripline(requirement.w_mm, requirement.s_mm,
                                requirement.t_mm, requirement.h_mm,
                                requirement.er, **shared)
    return solve_line(section).er_eff_odd(), "field_solver 2-D solve (odd mode)"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requirements", nargs="?", type=Path,
                        help="JSON with an 'impedance_classes' list "
                             "(required unless --self-test)")
    parser.add_argument("--tdr-rise-time-ps", type=float,
                        dest="rise_time_ps",
                        help="TDR step rise time. REQUIRED: it sets the "
                             "resolution and therefore the coupon length")
    parser.add_argument("--resolution-multiple", type=float,
                        default=DEFAULT_RESOLUTION_MULTIPLE,
                        dest="resolution_multiple",
                        help="settled line to require, in resolution lengths "
                             f"(default {DEFAULT_RESOLUTION_MULTIPLE})")
    parser.add_argument("--length-margin", type=float, default=1.0,
                        dest="length_margin",
                        help="plan this multiple of the minimum length "
                             "(default 1.0, i.e. exactly the minimum)")
    parser.add_argument("--length-mm", type=float, dest="length_mm",
                        help="plan this length for every coupon instead of "
                             "deriving it; still checked against the minimum")

    source = parser.add_argument_group("where er_eff comes from")
    source.add_argument("--solve", action="store_true",
                        help="compute er_eff with the 2-D field solver")
    source.add_argument("--er-eff", type=float, dest="er_eff",
                        help="state er_eff instead of solving for it")
    source.add_argument("--cells-per-feature", type=int, default=3,
                        dest="cells_per_feature")
    source.add_argument("--max-nodes", type=int, default=80_000,
                        dest="max_nodes")

    parser.add_argument("--json", type=Path)
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def run_self_tests() -> None:
    """Derivations that follow from the physics, checked against it."""

    def velocity_matches_the_speed_of_light_in_vacuum() -> None:
        """er_eff = 1 must give c, in mm/ps: 0.299792458."""
        value = propagation_velocity_mm_per_ps(1.0)
        assert abs(value - 0.299792458) < 1e-9, value

    def velocity_halves_when_permittivity_quadruples() -> None:
        fast = propagation_velocity_mm_per_ps(1.0)
        slow = propagation_velocity_mm_per_ps(4.0)
        assert abs(fast / slow - 2.0) < 1e-12, (fast, slow)

    def resolution_is_half_the_rise_time_length() -> None:
        """The factor 2: the step goes out and the reflection comes back."""
        er_eff, rise = 4.0, 100.0
        expected = propagation_velocity_mm_per_ps(er_eff) * rise / 2.0
        assert abs(tdr_resolution_mm(er_eff, rise) - expected) < 1e-12

    def a_faster_edge_needs_a_shorter_coupon() -> None:
        fast = minimum_coupon_length_mm(4.0, 20.0)
        slow = minimum_coupon_length_mm(4.0, 200.0)
        assert fast < slow, (fast, slow)
        assert abs(slow / fast - 10.0) < 1e-9, slow / fast

    def the_multiple_scales_the_length_linearly() -> None:
        single = minimum_coupon_length_mm(4.0, 100.0, 1.0)
        double = minimum_coupon_length_mm(4.0, 100.0, 2.0)
        assert abs(double / single - 2.0) < 1e-12

    def a_short_coupon_is_flagged_not_accepted() -> None:
        requirement = ImpedanceClass(
            name="c", structure="microstrip", target_ohm=50.0,
            tolerance_percent=10.0, layer="F.Cu", w_mm=0.2, er=4.3, h_mm=0.1)
        coupon = plan_coupon(requirement, er_eff=3.1, er_eff_source="stated",
                             rise_time_ps=100.0, planned_length_mm=0.5)
        assert not coupon.long_enough
        assert coupon.warnings, "a too-short coupon produced no warning"

    def a_missing_class_is_reported_as_missing() -> None:
        requirement = ImpedanceClass(
            name="only", structure="microstrip", target_ohm=50.0,
            tolerance_percent=10.0, layer="F.Cu", w_mm=0.2, er=4.3, h_mm=0.1)
        report = coverage([requirement], [])
        assert report["missing_classes"] == ["only"], report
        assert not report["covered"]

    def a_negative_margin_is_refused() -> None:
        requirement = ImpedanceClass(
            name="c", structure="microstrip", target_ohm=50.0,
            tolerance_percent=10.0, layer="F.Cu", w_mm=0.2, er=4.3, h_mm=0.1)
        try:
            plan_coupon(requirement, 3.1, "stated", 100.0, length_margin=0.5)
        except CouponError:
            return
        raise AssertionError("a margin below 1.0 was accepted")

    tests = (
        ("velocity equals c at er_eff=1", velocity_matches_the_speed_of_light_in_vacuum),
        ("velocity halves as er quadruples", velocity_halves_when_permittivity_quadruples),
        ("resolution is half the edge length", resolution_is_half_the_rise_time_length),
        ("a faster edge needs less coupon", a_faster_edge_needs_a_shorter_coupon),
        ("the multiple scales linearly", the_multiple_scales_the_length_linearly),
        ("a short coupon is flagged", a_short_coupon_is_flagged_not_accepted),
        ("a missing class is reported", a_missing_class_is_reported_as_missing),
        ("a margin below one is refused", a_negative_margin_is_refused),
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
        except (AssertionError, CouponError, ValueError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    if args.requirements is None:
        parser.error("a requirements JSON is required; pass it positionally "
                     "or use --self-test")
    if args.rise_time_ps is None:
        parser.error(
            "--tdr-rise-time-ps is required. It sets the resolution and "
            "therefore the coupon length, and a coupon that is too short "
            "does not fail loudly - it reports a plausible impedance measured "
            "on a transition")

    try:
        raw = json.loads(args.requirements.read_text(encoding="utf-8"))
        requirements = parse_requirements(raw)
    except (OSError, json.JSONDecodeError, CouponError) as exc:
        parser.error(f"requirements: {exc}")

    coupons = []
    try:
        for requirement in requirements:
            er_eff, source = _er_eff_for(requirement, args)
            coupons.append(plan_coupon(
                requirement, er_eff, source, args.rise_time_ps,
                args.resolution_multiple, args.length_mm, args.length_margin))
    except CouponError as exc:
        parser.error(str(exc))

    report_coverage = coverage(requirements, coupons)

    status = "GREEN"
    reason_codes: list[str] = []
    if report_coverage["missing_classes"]:
        status = "RED"
        reason_codes.append("CLASS_WITHOUT_COUPON")
    if report_coverage["too_short_classes"]:
        status = "RED"
        reason_codes.append("COUPON_TOO_SHORT")

    total_length = sum(coupon.planned_length_mm for coupon in coupons)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "coupon_plan",
        "signoff": False,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "pre_gerber",
        "tdr": {
            "rise_time_ps": args.rise_time_ps,
            "resolution_multiple": args.resolution_multiple,
        },
        "requirements": [item.to_dict() for item in requirements],
        "coupons": [coupon.to_dict() for coupon in coupons],
        "coverage": report_coverage,
        "total_coupon_length_mm": total_length,
        "limits": LIMITS,
    }

    diag("")
    diag(f"coupon plan: {len(requirements)} impedance class(es), "
         f"TDR rise time {args.rise_time_ps:g} ps")
    for coupon in coupons:
        mark = "ok " if coupon.long_enough else "SHORT"
        diag(f"  {mark} {coupon.impedance_class:22s} {coupon.target_ohm:6.1f} ohm  "
             f"{coupon.planned_length_mm:7.2f} mm "
             f"(min {coupon.minimum_length_mm:.2f}, "
             f"res {coupon.resolution_mm:.2f}, er_eff {coupon.er_eff:.3f})")
    if report_coverage["missing_classes"]:
        diag(f"  MISSING COUPONS: {report_coverage['missing_classes']}")
    diag(f"  total coupon length {total_length:.2f} mm")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
