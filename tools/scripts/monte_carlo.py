#!/usr/bin/env python3
"""Monte Carlo tolerance analysis - circuit values and cross-section geometry.

Closes the gap that every other number in this toolchain shares: they are all
single-point nominals. A fab quotes controlled impedance as +-10% and a vendor
quotes a resistor as +-1%, but nothing here could answer "what fraction of
boards lands inside the window?".

Two subjects:

  circuit    Perturb component values in a SPICE netlist and re-run ngspice.
  impedance  Perturb cross-section geometry and material, and propagate
             through the 2-D field solver.

WHAT WAS MEASURED, NOT ASSUMED
------------------------------
The queue item (#57) proposed driving this from "ngspice's own agauss /
foreach / alter". Probing the installed ngspice-46 showed that is only
half right, and the half that is wrong fails silently:

  * ``agauss(mu, sigma, n)`` inside a ``.control`` block is rejected with
    "no function as agauss with that arity" - it is a netlist-side function,
    not a control-language one. A first attempt that used it ran the loop the
    right number of times and produced the SAME nominal answer every trial,
    which looks exactly like a working Monte Carlo with a well-behaved
    circuit.
  * ``alter <ref> = <literal>`` does work, and persists across trials.
  * ``reset`` REVERTS pending alters, so it must not appear between trials.
  * The first line of a netlist is the title and is discarded, so a netlist
    assembled without one loses its first component.

So the draws are made in Python from a seeded generator - which also buys
reproducibility and lets non-Gaussian, part-specific effects such as MLCC
DC-bias derating be modelled at all - and applied through ``alter``, the one
mechanism that was verified against the real binary. Trial values are
checked against hand-computed expectations in the test suite.

Exit contract: 0 pass / 1 fail or NO_COVERAGE / 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from common.findings import acceptance_verdict  # noqa: E402
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)
from common.tolerance import (  # noqa: E402
    ParameterSpec,
    ToleranceError,
    parse_specification,
    sample_parameters,
    summarise,
    yield_against_limits,
)

SCHEMA_VERSION = "monte-carlo-report/v1"

LIMITS = [
    "parameters are drawn INDEPENDENTLY; parts from one reel do track each "
    "other, so matched pairs are treated pessimistically and absolute "
    "accuracy optimistically",
    "circuit mode supports the operating point (.op) only: one scalar per "
    "trial. Sweep analyses produce a table per trial and are not aggregated "
    "here",
    "impedance mode linearises the field solve about the nominal geometry; "
    "the linearisation is checked against full solves and the residual is "
    "reported as linearisation_check",
    "a Monte Carlo yield is an estimate from a finite sample; the reported "
    "Wilson 95% interval is the honest width, not the point estimate",
]


# --------------------------------------------------------------------------- #
# circuit subject
# --------------------------------------------------------------------------- #
def build_monte_carlo_netlist(netlist: str, draws: dict[str, np.ndarray],
                              watched: Sequence[str], trials: int) -> str:
    """Assemble one netlist whose control block runs every trial.

    One ngspice invocation for the whole study rather than N process starts:
    on Windows the process start dominates a small ``.op`` by a wide margin.
    """
    if not watched:
        raise ToleranceError(
            "at least one watched node is required: a Monte Carlo that does "
            "not know what it measures is not a report")

    body = netlist.rstrip()
    lowered = body.lower()
    if lowered.endswith(".end"):
        body = body[:-len(".end")].rstrip()

    first = body.splitlines()[0].strip() if body.splitlines() else ""
    if not first.startswith("*"):
        # SPICE discards the first line as the title. A netlist that starts
        # with a component silently loses it - verified against ngspice-46.
        body = "* monte carlo (title line inserted by monte_carlo.py)\n" + body

    watch = " ".join(f"v({n})" if not n.lower().startswith("v(") else n
                     for n in watched)

    lines = [body, ".control"]
    for trial in range(trials):
        for name in sorted(draws):
            lines.append(f"alter {name} = {draws[name][trial]:.10g}")
        lines.append("op")
        lines.append(f"print {watch}")
    lines += [".endc", ".end", ""]
    return "\n".join(lines)


def run_circuit_study(args: argparse.Namespace,
                      specs: list[ParameterSpec]) -> dict[str, object]:
    from sim.ngspice import find_ngspice, ngspice_version, parse_output
    import subprocess

    netlist_text = Path(args.netlist).read_text(encoding="utf-8")
    draws = sample_parameters(specs, args.trials, args.seed)
    generated = build_monte_carlo_netlist(netlist_text, draws, args.watch,
                                          args.trials)

    if args.emit_netlist:
        Path(args.emit_netlist).write_text(generated, encoding="utf-8")

    binary = find_ngspice(args.ngspice)
    if binary is None:
        return {
            "status": "NO_COVERAGE",
            "reason_codes": ["NGSPICE_ABSENT"],
            "ngspice_version": "ABSENT",
            "detail": "ngspice is not installed; no result is fabricated",
        }

    work = Path(args.emit_netlist) if args.emit_netlist else None
    if work is None:
        import tempfile
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".cir", delete=False, encoding="utf-8")
        try:
            handle.write(generated)
        finally:
            handle.close()
        work = Path(handle.name)

    try:
        proc = subprocess.run([binary, "-b", str(work)], capture_output=True,
                              text=True, timeout=args.timeout,
                              stdin=subprocess.DEVNULL)
    finally:
        if not args.emit_netlist:
            work.unlink(missing_ok=True)

    parsed = parse_output((proc.stdout or "") + "\n" + (proc.stderr or ""))

    series: dict[str, list[float]] = {}
    for node in args.watch:
        key = node if node.lower().startswith("v(") else f"v({node})"
        values = [value for _, value in parsed.data.get(key.lower(), [])
                  or parsed.data.get(key, [])]
        series[key] = values

    measured = {node: len(values) for node, values in series.items()}
    short = {node: count for node, count in measured.items()
             if count != args.trials}
    if short:
        # Fewer results than trials means some runs failed. Reporting the mean
        # of the survivors would quietly turn a convergence failure into a
        # narrower distribution - the exact opposite of the truth.
        return {
            "status": "NO_COVERAGE",
            "reason_codes": ["TRIAL_COUNT_MISMATCH"],
            "ngspice_version": ngspice_version(args.ngspice),
            "detail": f"expected {args.trials} results per node, got {short}",
            "ngspice_stderr_tail": (proc.stderr or "")[-800:],
        }

    return {
        "status": "GREEN",
        "reason_codes": [],
        "ngspice_version": ngspice_version(args.ngspice),
        "series": series,
        "draws": {name: [float(v) for v in values]
                  for name, values in draws.items()},
    }


# --------------------------------------------------------------------------- #
# impedance subject
# --------------------------------------------------------------------------- #
IMPEDANCE_PARAMETERS = ("w", "s", "t", "h", "er")


GEOMETRIC_PARAMETERS = ("w", "s", "t", "h")


def _build_impedance_section(geometry: str, values: dict[str, float],
                             max_nodes: int, cells_per_feature: int):
    from common.field2d import (
        coupled_microstrip, coupled_stripline, microstrip, stripline,
    )
    shared = {"cells_per_feature": cells_per_feature, "max_nodes": max_nodes}
    if geometry == "microstrip":
        return microstrip(values["w"], values["t"], values["h"],
                          values["er"], **shared), "single"
    if geometry == "coupled-microstrip":
        return coupled_microstrip(values["w"], values["s"], values["t"],
                                  values["h"], values["er"], **shared), "diff"
    if geometry == "stripline":
        return stripline(values["w"], values["t"], values["h"],
                         values["er"], **shared), "single"
    if geometry == "coupled-stripline":
        return coupled_stripline(values["w"], values["s"], values["t"],
                                 values["h"], values["er"], **shared), "diff"
    raise ToleranceError(f"unsupported geometry for impedance study: {geometry}")


def _solve_impedance(geometry: str, values: dict[str, float],
                     max_nodes: int, cells_per_feature: int) -> float:
    from common.field2d import solve_line
    section, kind = _build_impedance_section(geometry, values, max_nodes,
                                             cells_per_feature)
    result = solve_line(section)
    return result.z_diff_ohm() if kind == "diff" else result.z0_ohm()


def _gradient_step(name: str, nominal_value: float, fraction: float,
                   pitch_mm: float, pitch_multiple: float) -> float:
    """Finite-difference step, floored at a few grid cells for lengths.

    A geometric step SMALLER than the grid pitch is quantised away when the
    cross-section is rasterised: the perturbed geometry is byte-identical to
    the nominal one, the solver returns the same impedance, and the gradient
    comes out as approximately zero. That is not a small sensitivity, it is a
    measurement that never happened - and it is silent.

    Observed: with w = 0.09 mm and a 2% step (1.8 um) against a 5 um pitch,
    dZ/dw was reported as -0.16 ohm/mm while dZ/dt was -1056 ohm/mm. Widening
    a trace obviously moves impedance far more than that.
    """
    step = abs(nominal_value) * fraction
    if name in GEOMETRIC_PARAMETERS:
        step = max(step, pitch_multiple * pitch_mm)
    return max(step, 1e-9)


def run_impedance_study(args: argparse.Namespace,
                        specs: list[ParameterSpec]) -> dict[str, object]:
    """Linearise the field solve, then run the Monte Carlo on the linear model.

    A full solve costs seconds; 10 000 of them would cost hours. One nominal
    solve plus one perturbed solve per varying parameter gives the gradient,
    and the linear model is then evaluated for free. The approximation is not
    taken on faith - ``linearisation_check`` re-solves a few of the sampled
    geometries in full and reports the worst disagreement.
    """
    by_name = {spec.name: spec for spec in specs}
    unknown = set(by_name) - set(IMPEDANCE_PARAMETERS)
    if unknown:
        raise ToleranceError(
            f"impedance mode understands {IMPEDANCE_PARAMETERS}, got extra "
            f"{sorted(unknown)}")

    nominal = {name: by_name[name].effective_nominal() if name in by_name
               else getattr(args, name)
               for name in IMPEDANCE_PARAMETERS}
    for name, value in nominal.items():
        if value is None and name in ("w", "h", "er", "t"):
            raise ToleranceError(
                f"impedance mode needs a nominal for {name}: put it in the "
                f"specification or pass --{name}")
    if args.geometry.startswith("coupled") and nominal["s"] is None:
        raise ToleranceError("coupled geometry needs a nominal for s")

    solve = lambda values: _solve_impedance(          # noqa: E731
        args.geometry, values, args.max_nodes, args.cells_per_feature)

    nominal_section, _ = _build_impedance_section(
        args.geometry, nominal, args.max_nodes, args.cells_per_feature)
    pitch_mm = nominal_section.dx_mm

    nominal_impedance = solve(nominal)
    solves = 1

    gradients: dict[str, float] = {}
    steps: dict[str, float] = {}
    varying = [name for name in IMPEDANCE_PARAMETERS
               if name in by_name and by_name[name].distribution != "fixed"]
    for name in varying:
        step = _gradient_step(name, nominal[name], args.gradient_step_fraction,
                              pitch_mm, args.gradient_step_pitch_multiple)
        perturbed = dict(nominal)
        perturbed[name] = nominal[name] + step
        gradients[name] = (solve(perturbed) - nominal_impedance) / step
        steps[name] = step
        solves += 1

    draws = sample_parameters([by_name[name] for name in varying],
                              args.trials, args.seed)
    impedances = np.full(args.trials, nominal_impedance, dtype=float)
    for name in varying:
        impedances = impedances + gradients[name] * (
            draws[name] - nominal[name])

    check = _check_linearisation(args, nominal, varying, draws, impedances,
                                 solve, args.linearisation_checks)
    solves += check["full_solves"]

    report = check["report"]
    status, reason_codes = "GREEN", []
    worst = report.get("worst_error_percent")
    if worst is None:
        status = "NO_COVERAGE"
        reason_codes.append("LINEARISATION_UNCHECKED")
    elif worst > args.max_linearisation_error_percent:
        # The linear model is the whole basis for turning 10 field solves into
        # 5000 trials. When it does not hold, the distribution is not the
        # circuit's - refusing beats reporting a confident yield built on it.
        status = "NO_COVERAGE"
        reason_codes.append("LINEARISATION_UNRELIABLE")

    return {
        "status": status,
        "reason_codes": reason_codes,
        "series": {"z_ohm": [float(v) for v in impedances]},
        "draws": {name: [float(v) for v in values]
                  for name, values in draws.items()},
        "nominal_impedance_ohm": nominal_impedance,
        "nominal_geometry": {k: v for k, v in nominal.items() if v is not None},
        "sensitivity_ohm_per_unit": gradients,
        "gradient_step_mm": steps,
        "grid_pitch_mm": pitch_mm,
        "field_solves": solves,
        "linearisation_check": report,
    }


def _check_linearisation(args, nominal, varying, draws, predicted, solve,
                         count: int) -> dict[str, object]:
    """Re-solve a few sampled geometries in full and compare."""
    if count <= 0 or not varying:
        return {"full_solves": 0,
                "report": {"checked": 0,
                           "note": "linearisation not checked"}}

    rng = np.random.default_rng(args.seed + 1)
    indices = rng.choice(len(predicted), size=min(count, len(predicted)),
                         replace=False)
    worst = 0.0
    samples = []
    for index in indices:
        values = dict(nominal)
        for name in varying:
            values[name] = float(draws[name][index])
        actual = solve(values)
        error = abs(actual - predicted[index]) / abs(actual) * 100.0
        worst = max(worst, error)
        samples.append({
            "trial": int(index),
            "linear_ohm": round(float(predicted[index]), 4),
            "full_solve_ohm": round(actual, 4),
            "error_percent": round(error, 4),
        })
    return {
        "full_solves": len(indices),
        "report": {
            "checked": len(indices),
            "worst_error_percent": round(worst, 4),
            "samples": samples,
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("subject", nargs="?", choices=("circuit", "impedance"),
                        help="what to vary (required unless --self-test)")
    parser.add_argument("--spec", type=Path,
                        help="JSON tolerance specification "
                             '({"parameters": {"R1": {...}}})')
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260807,
                        help="RNG seed; recorded in the report so a verdict "
                             "reproduces exactly")

    circuit = parser.add_argument_group("circuit subject")
    circuit.add_argument("--netlist", type=Path)
    circuit.add_argument("--watch", nargs="+", default=[],
                         help="nodes to measure, e.g. out vdd")
    circuit.add_argument("--ngspice", help="explicit ngspice binary")
    circuit.add_argument("--timeout", type=int, default=600)
    circuit.add_argument("--emit-netlist", type=Path, dest="emit_netlist",
                         help="write the generated netlist here and keep it")

    impedance = parser.add_argument_group("impedance subject")
    impedance.add_argument("--geometry", default="coupled-microstrip",
                           choices=("microstrip", "coupled-microstrip",
                                    "stripline", "coupled-stripline"))
    for name in IMPEDANCE_PARAMETERS:
        impedance.add_argument(f"--{name}", type=float,
                               help=f"nominal {name} when it is not varying")
    impedance.add_argument("--max-nodes", type=int, default=120_000,
                           dest="max_nodes")
    impedance.add_argument("--cells-per-feature", type=int, default=3,
                           dest="cells_per_feature")
    impedance.add_argument("--gradient-step-fraction", type=float, default=0.02,
                           dest="gradient_step_fraction")
    impedance.add_argument("--gradient-step-pitch-multiple", type=float,
                           default=3.0, dest="gradient_step_pitch_multiple",
                           help="floor the geometric finite-difference step at "
                                "this many grid cells, so the perturbation "
                                "survives rasterisation (default 3)")
    impedance.add_argument("--linearisation-checks", type=int, default=5,
                           dest="linearisation_checks",
                           help="full solves used to verify the linear model")
    impedance.add_argument("--max-linearisation-error-percent", type=float,
                           default=2.0, dest="max_linearisation_error_percent",
                           help="refuse to report a yield when the linear "
                                "model disagrees with a full solve by more "
                                "than this (default 2)")

    gate = parser.add_argument_group("gate")
    gate.add_argument("--lower-limit", type=float, dest="lower_limit")
    gate.add_argument("--upper-limit", type=float, dest="upper_limit")
    gate.add_argument("--min-yield", type=float, default=None, dest="min_yield",
                      help="fail below this yield fraction, e.g. 0.99")

    parser.add_argument("--json", type=Path)
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def run_self_tests() -> None:
    """Checks that do not need ngspice or a long solve."""

    def wilson_interval_brackets_the_point_estimate() -> None:
        from common.tolerance import _wilson_interval
        low, high = _wilson_interval(95, 100)
        assert low < 0.95 < high, (low, high)

    def a_perfect_sample_still_has_an_interval() -> None:
        """100/100 must NOT report certainty."""
        from common.tolerance import _wilson_interval
        low, high = _wilson_interval(100, 100)
        assert low < 1.0, f"100/100 reported a lower bound of {low}"

    def sampling_is_reproducible() -> None:
        spec = ParameterSpec(name="R1", nominal=1000.0, distribution="normal",
                             sigma_percent=1.0)
        first = sample_parameters([spec], 50, seed=42)["R1"]
        second = sample_parameters([spec], 50, seed=42)["R1"]
        assert np.array_equal(first, second), "same seed gave different draws"

    def tolerance_without_a_sigma_rule_is_refused() -> None:
        try:
            ParameterSpec(name="R1", nominal=1000.0,
                          distribution="normal_from_tolerance",
                          tolerance_percent=1.0)
        except ToleranceError:
            return
        raise AssertionError(
            "normal_from_tolerance accepted a missing sigma_divisor")

    def title_line_is_inserted() -> None:
        built = build_monte_carlo_netlist(
            "V1 in 0 DC 5\nR1 in out 1k\n.end",
            {"R1": np.array([1000.0])}, ["out"], 1)
        assert built.splitlines()[0].startswith("*"), built.splitlines()[0]
        assert "V1 in 0 DC 5" in built

    tests = (
        ("wilson interval brackets p", wilson_interval_brackets_the_point_estimate),
        ("100/100 is not certainty", a_perfect_sample_still_has_an_interval),
        ("sampling is reproducible", sampling_is_reproducible),
        ("tolerance needs a sigma rule", tolerance_without_a_sigma_rule_is_refused),
        ("title line is inserted", title_line_is_inserted),
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
        except (AssertionError, ToleranceError, ValueError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    if args.subject is None:
        parser.error("subject is required; pass 'circuit' or 'impedance' "
                     "positionally, or use --self-test")
    if args.spec is None:
        parser.error("--spec is required")
    if args.subject == "circuit" and args.netlist is None:
        parser.error("circuit subject requires --netlist")

    try:
        specification = json.loads(args.spec.read_text(encoding="utf-8"))
        specs = parse_specification(specification)
    except (OSError, json.JSONDecodeError, ToleranceError) as exc:
        parser.error(f"specification: {exc}")

    coverage_gaps = [spec.name for spec in specs if spec.has_coverage_gap()]

    try:
        if args.subject == "circuit":
            outcome = run_circuit_study(args, specs)
        else:
            outcome = run_impedance_study(args, specs)
    except ToleranceError as exc:
        parser.error(str(exc))

    status = str(outcome.get("status", "NO_COVERAGE"))
    reason_codes = list(outcome.get("reason_codes", []))
    if coverage_gaps:
        status = "NO_COVERAGE"
        reason_codes.append("PARAMETER_COVERAGE_GAP")

    statistics: dict[str, object] = {}
    gate: dict[str, object] = {"applied": False}
    series = outcome.get("series", {})               # type: ignore[assignment]
    # Statistics are reported even when the verdict is NO_COVERAGE: the
    # numbers are still the best description of what was computed, and hiding
    # them makes a coverage gap indistinguishable from a crash. The GATE is
    # what stays shut.
    for node, values in series.items():              # type: ignore[union-attr]
        statistics[node] = summarise(values)

    if status == "GREEN":
        if args.lower_limit is not None or args.upper_limit is not None:
            gate = {"applied": True, "nodes": {}}
            worst = 1.0
            for node, values in series.items():      # type: ignore[union-attr]
                measured = yield_against_limits(
                    values, args.lower_limit, args.upper_limit)
                gate["nodes"][node] = measured       # type: ignore[index]
                worst = min(worst, measured["yield_fraction"])
            gate["worst_yield_fraction"] = worst
            if args.min_yield is not None:
                gate["min_yield"] = args.min_yield
                if worst < args.min_yield:
                    status = "RED"
                    reason_codes.append("YIELD_BELOW_MINIMUM")

    # Same split as field_solver / electrothermal / correlate: the top-level
    # status says the trials ran and the parameters were covered, while
    # gate.verdict says whether a stated acceptance criterion was met. With
    # no --lower-limit/--upper-limit there is no criterion, and with limits
    # but no --min-yield there is a yield number with nothing to compare it
    # against - both are NO_COVERAGE on this axis, not GREEN.
    gate.update(acceptance_verdict(
        applied=bool(gate["applied"]) and args.min_yield is not None,
        met=(args.min_yield is not None
             and float(gate.get("worst_yield_fraction", 0.0)) >= args.min_yield),
        computed=status != "NO_COVERAGE",
        missing_reason=(
            "no --lower-limit/--upper-limit was supplied, so no yield was "
            "computed" if not gate["applied"] else
            "a yield was computed but no --min-yield was supplied, so there "
            "is no criterion it could have met")))

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "monte_carlo",
        "subject": args.subject,
        "model_class": ("numerical_2d_fdm_linearised"
                        if args.subject == "impedance" else "spice_transient"),
        "signoff": False,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "trials": args.trials,
        "seed": args.seed,
        "parameters": [spec.describe() for spec in specs],
        "parameter_coverage_gaps": coverage_gaps,
        "statistics": statistics,
        "gate": gate,
        "limits": LIMITS,
    }
    for key in ("ngspice_version", "detail", "nominal_impedance_ohm",
                "nominal_geometry", "sensitivity_ohm_per_unit",
                "gradient_step_mm", "grid_pitch_mm",
                "field_solves", "linearisation_check",
                "ngspice_stderr_tail"):
        if key in outcome:
            report[key] = outcome[key]

    diag("")
    diag(f"monte carlo [{args.subject}] {args.trials} trials, seed {args.seed}")
    for node, stats in statistics.items():
        diag(f"  {node}: mean={stats['mean']:.6g} sd={stats['std']:.3g} "
             f"p01={stats['p01']:.6g} p99={stats['p99']:.6g}")
    if gate.get("applied"):
        diag(f"  worst yield: {gate['worst_yield_fraction'] * 100:.2f}%")
    if coverage_gaps:
        diag(f"  COVERAGE GAP: {', '.join(coverage_gaps)}")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
