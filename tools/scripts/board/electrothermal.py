#!/usr/bin/env python3
"""Coupled electrothermal operating point for a trace, with runaway detection.

WHAT WAS UNCOUPLED
------------------
``current_density.py`` solves the current distribution at a FIXED resistivity.
``thermal_bridge.py`` estimates a temperature. Neither told the other
anything, so a trace was solved at its 20 C resistance and the temperature
that followed came from a power that was therefore too low.

WHAT THE COUPLING BUYS
----------------------
Not a slightly better number - a failure mode the uncoupled calculation cannot
express. The loop

    R(T) = R20*(1 + alpha*(T-20)) ;  P = I^2*R(T) ;  T = Ta + P*Rth

has gain G = I^2 * R20 * alpha * Rth, and for G >= 1 it has NO steady state:
the trace runs away. A single-pass calculation evaluates the first step and
reports a comfortable temperature for exactly that case.

So this reports G, refuses to name a temperature when G >= 1, and prints the
single-pass answer alongside the coupled one so the size of the old error is
visible rather than silently corrected.

Rth IS REQUIRED. It depends on the stackup, plane coupling, airflow and
neighbours; the whole loop gain is proportional to it, so guessing it would
set the answer.

Exit contract: 0 pass / 1 fail or NO_COVERAGE / 2 could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.electrothermal import (  # noqa: E402
    COPPER_ALPHA_PER_K,
    COPPER_RESISTIVITY_20C_OHM_M,
    ElectrothermalError,
    closed_form_temperature_c,
    loop_gain,
    maximum_stable_current_a,
    resistivity_at,
    single_pass_temperature_c,
    solve_equilibrium,
    trace_resistance_ohm,
)
from common.findings import acceptance_verdict  # noqa: E402
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)

SCHEMA_VERSION = "electrothermal-report/v1"

LIMITS = [
    "DC only: no skin effect, no proximity effect, no AC redistribution",
    "one lumped temperature for the whole trace; a real trace has a gradient "
    "and its hottest point is above this average",
    "thermal resistance is a REQUIRED input and is never derived: the loop "
    "gain is proportional to it, so guessing it would set the answer",
    "the resistivity model is linear in temperature; the closed form is only "
    "valid while that holds, which is why the iteration is run alongside it "
    "and the two are required to agree",
    "a stable loop gain is not a pass: it says a steady state EXISTS, not "
    "that its temperature is acceptable",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--current-a", type=float, dest="current_a",
                        help="steady current through the trace")
    parser.add_argument("--thermal-resistance-k-per-w", type=float,
                        dest="thermal_resistance",
                        help="copper-to-ambient thermal resistance, K/W. "
                             "REQUIRED: the loop gain is proportional to it")
    parser.add_argument("--ambient-c", type=float, default=25.0,
                        dest="ambient_c")

    geometry = parser.add_argument_group(
        "trace geometry (or give --resistance-20c-ohm directly)")
    geometry.add_argument("--length-mm", type=float, dest="length_mm")
    geometry.add_argument("--width-mm", type=float, dest="width_mm")
    geometry.add_argument("--copper-oz", type=float, default=1.0,
                          dest="copper_oz")
    geometry.add_argument("--resistance-20c-ohm", type=float,
                          dest="resistance_20c_ohm")

    material = parser.add_argument_group("material")
    material.add_argument("--alpha-per-k", type=float,
                          default=COPPER_ALPHA_PER_K, dest="alpha_per_k",
                          help=f"temperature coefficient of resistance "
                               f"(default {COPPER_ALPHA_PER_K} for annealed "
                               "copper)")
    material.add_argument("--rho-20-ohm-m", type=float,
                          default=COPPER_RESISTIVITY_20C_OHM_M,
                          dest="rho_20_ohm_m")

    gate = parser.add_argument_group("gate")
    gate.add_argument("--max-temperature-c", type=float,
                      dest="max_temperature_c",
                      help="fail when the converged temperature exceeds this")
    gate.add_argument("--max-rise-k", type=float, dest="max_rise_k",
                      help="fail when the temperature RISE exceeds this")

    parser.add_argument("--json", type=Path)
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def run_self_tests() -> None:
    """Checked against closed form and against the physics, not a recording."""

    def resistivity_is_the_reference_value_at_twenty(self=None) -> None:
        value = resistivity_at(20.0)
        assert abs(value - COPPER_RESISTIVITY_20C_OHM_M) < 1e-20, value

    def resistivity_rises_with_temperature() -> None:
        assert resistivity_at(100.0) > resistivity_at(20.0)

    def a_hundred_kelvin_rise_is_about_forty_percent() -> None:
        """alpha = 3.93e-3/K, so 100 K gives 1 + 0.393."""
        ratio = resistivity_at(120.0) / resistivity_at(20.0)
        assert abs(ratio - 1.393) < 1e-9, ratio

    def the_iteration_matches_the_closed_form() -> None:
        """The whole reason the iteration is still run."""
        result = solve_equilibrium(current_a=2.0, resistance_20c_ohm=0.05,
                                   thermal_resistance_k_per_w=40.0,
                                   ambient_c=25.0)
        assert result.converged, result.reason
        assert result.iteration_vs_closed_form_k < 1e-6, \
            result.iteration_vs_closed_form_k

    def a_runaway_loop_is_refused_not_iterated() -> None:
        """G >= 1 has no fixed point; reporting the last iterate would be a
        confident answer where the physics has none."""
        result = solve_equilibrium(current_a=40.0, resistance_20c_ohm=0.05,
                                   thermal_resistance_k_per_w=40.0)
        assert not result.converged
        assert result.temperature_c is None
        assert result.loop_gain >= 1.0, result.loop_gain

    def the_coupled_answer_is_hotter_than_the_uncoupled_one() -> None:
        """Which is the entire point: the old number was optimistic."""
        coupled = solve_equilibrium(current_a=2.0, resistance_20c_ohm=0.05,
                                    thermal_resistance_k_per_w=40.0,
                                    ambient_c=25.0)
        uncoupled = single_pass_temperature_c(2.0, 0.05, 40.0, 25.0)
        assert coupled.temperature_c > uncoupled, (coupled.temperature_c,
                                                   uncoupled)

    def the_maximum_stable_current_is_where_the_gain_reaches_one() -> None:
        limit = maximum_stable_current_a(0.05, 40.0)
        assert abs(loop_gain(limit, 0.05, 40.0) - 1.0) < 1e-9

    def zero_current_leaves_the_trace_at_ambient() -> None:
        result = solve_equilibrium(current_a=0.0, resistance_20c_ohm=0.05,
                                   thermal_resistance_k_per_w=40.0,
                                   ambient_c=25.0)
        assert abs(result.temperature_c - 25.0) < 1e-12, result.temperature_c

    def resistance_follows_the_geometry() -> None:
        """Twice the length is twice the resistance; twice the width, half."""
        base = trace_resistance_ohm(100.0, 0.25, 1.0, 20.0)
        longer = trace_resistance_ohm(200.0, 0.25, 1.0, 20.0)
        wider = trace_resistance_ohm(100.0, 0.50, 1.0, 20.0)
        assert abs(longer / base - 2.0) < 1e-12
        assert abs(wider / base - 0.5) < 1e-12

    tests = (
        ("resistivity is the reference at 20 C", resistivity_is_the_reference_value_at_twenty),
        ("resistivity rises with T", resistivity_rises_with_temperature),
        ("100 K gives +39.3%", a_hundred_kelvin_rise_is_about_forty_percent),
        ("iteration matches closed form", the_iteration_matches_the_closed_form),
        ("a runaway loop is refused", a_runaway_loop_is_refused_not_iterated),
        ("coupled is hotter than uncoupled", the_coupled_answer_is_hotter_than_the_uncoupled_one),
        ("max stable current is where G=1", the_maximum_stable_current_is_where_the_gain_reaches_one),
        ("zero current stays at ambient", zero_current_leaves_the_trace_at_ambient),
        ("resistance follows geometry", resistance_follows_the_geometry),
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
        except (AssertionError, ElectrothermalError, ValueError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    if args.current_a is None:
        parser.error("--current-a is required")
    if args.thermal_resistance is None:
        parser.error(
            "--thermal-resistance-k-per-w is required. The loop gain is "
            "proportional to it, so a default would set the answer")

    try:
        if args.resistance_20c_ohm is not None:
            resistance_20c = args.resistance_20c_ohm
            resistance_source = "stated via --resistance-20c-ohm"
        else:
            if args.length_mm is None or args.width_mm is None:
                parser.error("give --resistance-20c-ohm, or both --length-mm "
                             "and --width-mm")
            resistance_20c = trace_resistance_ohm(
                args.length_mm, args.width_mm, args.copper_oz, 20.0,
                args.rho_20_ohm_m, args.alpha_per_k)
            resistance_source = (
                f"geometry: {args.length_mm} x {args.width_mm} mm, "
                f"{args.copper_oz} oz")

        equilibrium = solve_equilibrium(
            args.current_a, resistance_20c, args.thermal_resistance,
            args.ambient_c, args.alpha_per_k)
        uncoupled = single_pass_temperature_c(
            args.current_a, resistance_20c, args.thermal_resistance,
            args.ambient_c)
        stable_current = maximum_stable_current_a(
            resistance_20c, args.thermal_resistance, args.alpha_per_k)
    except ElectrothermalError as exc:
        parser.error(str(exc))

    status = "GREEN"
    reason_codes: list[str] = []
    if not equilibrium.converged:
        status = "RED"
        reason_codes.append("THERMAL_RUNAWAY")

    gate: dict[str, object] = {"applied": False}
    if equilibrium.converged and (args.max_temperature_c is not None
                                  or args.max_rise_k is not None):
        checks: dict[str, object] = {}
        if args.max_temperature_c is not None:
            within = equilibrium.temperature_c <= args.max_temperature_c
            checks["temperature_c"] = {
                "value": equilibrium.temperature_c,
                "limit": args.max_temperature_c, "within": within}
            if not within:
                status = "RED"
                reason_codes.append("TEMPERATURE_EXCEEDED")
        if args.max_rise_k is not None:
            within = equilibrium.rise_k <= args.max_rise_k
            checks["rise_k"] = {"value": equilibrium.rise_k,
                                "limit": args.max_rise_k, "within": within}
            if not within:
                status = "RED"
                reason_codes.append("RISE_EXCEEDED")
        gate = {"applied": True, "checks": checks}

    # A converged solve reported GREEN whatever temperature it settled at,
    # because the only limits it could be judged against are the ones the
    # caller passes in. That GREEN means "the electrothermal loop converged",
    # not "this rail runs cool enough" - and nothing in the report said so in
    # a form a gate consumer could read. gate.verdict is that second axis.
    gate.update(acceptance_verdict(
        applied=bool(gate["applied"]),
        met=all(check["within"]                    # type: ignore[index]
                for check in gate.get("checks", {}).values()),  # type: ignore[union-attr]
        computed=equilibrium.converged,
        missing_reason="neither --max-temperature-c nor --max-rise-k was "
                       "supplied, so no thermal limit was applied"))

    coupling_error_k = (equilibrium.temperature_c - uncoupled
                        if equilibrium.converged else None)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "electrothermal",
        "signoff": False,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "inputs": {
            "current_a": args.current_a,
            "thermal_resistance_k_per_w": args.thermal_resistance,
            "ambient_c": args.ambient_c,
            "alpha_per_k": args.alpha_per_k,
            "rho_20_ohm_m": args.rho_20_ohm_m,
            "resistance_20c_ohm": resistance_20c,
            "resistance_source": resistance_source,
        },
        "equilibrium": equilibrium.to_dict(),
        "uncoupled": {
            "temperature_c": uncoupled,
            "coupling_error_k": coupling_error_k,
            "note": (
                "what the uncoupled calculation reports: resistance at 20 C, "
                "one step. The difference is the error the toolchain has been "
                "carrying"),
        },
        "maximum_stable_current_a": stable_current,
        "gate": gate,
        "limits": LIMITS,
    }

    diag("")
    diag(f"electrothermal: I={args.current_a} A, R20={resistance_20c:.6g} ohm, "
         f"Rth={args.thermal_resistance} K/W")
    diag(f"  loop gain           {equilibrium.loop_gain:.4f} "
         f"({'stable' if equilibrium.loop_gain < 1 else 'RUNAWAY'})")
    if equilibrium.converged:
        diag(f"  coupled temperature {equilibrium.temperature_c:.2f} C "
             f"(rise {equilibrium.rise_k:.2f} K, "
             f"{equilibrium.iterations} iterations)")
        diag(f"  uncoupled would say {uncoupled:.2f} C "
             f"-> understated by {coupling_error_k:.2f} K")
    else:
        diag(f"  {equilibrium.reason}")
    diag(f"  runaway current     {stable_current:.2f} A")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
