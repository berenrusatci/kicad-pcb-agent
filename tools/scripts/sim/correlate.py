#!/usr/bin/env python3
"""Correlate a measured Touchstone against a simulated one.

THE FIRST NUMBER IN THIS BUNDLE THAT COULD SHOW A MODEL IS WRONG
----------------------------------------------------------------
Everything upstream is self-consistent. ``field_solver`` reports a bounded
grid error, and that bound says the NUMERICS converged - not that the answer
matches copper. Nothing here has ever been compared against a physical board.
This is where that comparison happens:

    coupon plan  ->  fabricate  ->  measure  ->  correlate  ->  adjust er
      (coupon_plan.py)               (VNA)       (this)        (by hand)

WHY THIS EXISTS BEFORE ANY BOARD DOES
-------------------------------------
Item #67 was blocked on "no board, so no measurement". That is true of the
DATA and false of the TOOL. A VNA exports Touchstone, this bundle already
reads and writes Touchstone, so the comparison machinery is fully exercisable
today against a network built from a known model - and the day a real .s4p
arrives from the instrument, nothing about this changes. What cannot be done
without a board is correlating THIS board, and that is reported as
NO_COVERAGE rather than papered over.

DE-EMBEDDING IS DECLARED, NEVER INFERRED
----------------------------------------
A launch that was not de-embedded moves both the magnitude and the delay, and
no amount of staring at the residual separates that from a wrong er. So
--de-embedded is a statement the caller makes about the measurement, and an
undeclared measurement is reported with the residual marked as
fixture-contaminated rather than quietly accepted as a model error.

Exit contract: 0 pass / 1 fail or NO_COVERAGE / 2 could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from common.correlate import (  # noqa: E402
    METRICS,
    MIN_SHARED_POINTS,
    Band,
    CorrelationError,
    common_grid,
    correlate,
    residual,
    suggest_er_correction,
)
from common.findings import acceptance_verdict  # noqa: E402
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)
from common import tline as tl  # noqa: E402

SCHEMA_VERSION = "correlation-report/v1"

LIMITS = [
    "a small residual says the model and the measurement AGREE, not that "
    "either is right: a launch de-embedded to the wrong reference plane can "
    "move both",
    "de-embedding status is declared by the caller and never inferred from "
    "the data",
    "no interpolation: only frequencies present in BOTH sweeps are compared, "
    "because resampling a measurement invents points that were never taken",
    "restricted to the declared band; a residual quoted where the instrument "
    "never swept is not a statement about the model",
    "an er correction derived from a delay error is a POINTER: the same error "
    "can come from a wrong length or an un-de-embedded fixture",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--measured", type=Path,
                        help="Touchstone from the instrument")
    parser.add_argument("--simulated", type=Path,
                        help="Touchstone from the model")
    parser.add_argument("--band-start-hz", type=float, dest="band_start_hz",
                        help="lower edge of the comparison band")
    parser.add_argument("--band-stop-hz", type=float, dest="band_stop_hz",
                        help="upper edge of the comparison band")
    parser.add_argument("--port-out", type=int, default=1, dest="port_out")
    parser.add_argument("--port-in", type=int, default=0, dest="port_in")
    parser.add_argument("--metrics", nargs="+", default=["magnitude_db"],
                        choices=list(METRICS))
    parser.add_argument("--grid-tolerance-hz", type=float, default=1.0,
                        dest="grid_tolerance_hz",
                        help="how close two sweep points must be to count as "
                             "the same frequency (default 1 Hz)")
    parser.add_argument("--de-embedded", action="store_true",
                        dest="de_embedded",
                        help="declare that the fixture has been de-embedded "
                             "from the measurement. Never inferred")

    gate = parser.add_argument_group("gate")
    gate.add_argument("--max-magnitude-residual-db", type=float,
                      dest="max_magnitude_residual_db")
    gate.add_argument("--max-delay-residual-ps", type=float,
                      dest="max_delay_residual_ps")

    feedback = parser.add_argument_group("model feedback")
    feedback.add_argument("--assumed-er-eff", type=float,
                          dest="assumed_er_eff",
                          help="with group_delay_ps, report the er_eff that "
                               "would have reproduced the measured delay")

    parser.add_argument("--json", type=Path)
    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def _known_network(delay_scale: float = 1.0, loss_db_scale: float = 1.0,
                   points: int = 41):
    """A network from a known model, standing in for an instrument file."""
    frequency = tl.frequency_grid(1e8, 1e10, points)
    spec = tl.LineSpec(z0_ohm=50.0, er_eff=3.1 * delay_scale ** 2,
                       length_mm=100.0, tan_delta=0.01 * loss_db_scale)
    return tl.single_line_network(frequency, spec, z0_port=50.0)


def run_self_tests() -> None:
    """Exercised against networks whose disagreement is known in advance."""

    def a_network_against_itself_has_zero_residual() -> None:
        network = _known_network()
        report = correlate(network, network, Band(1e8, 1e10))
        assert report["sufficient"], report
        magnitude = report["residuals"]["magnitude_db"]
        assert abs(magnitude["rms"]) < 1e-12, magnitude

    def a_known_loss_difference_shows_up_signed() -> None:
        """A lossier measurement than the model reads NEGATIVE, not just big:
        the sign is what separates a biased model from a noisy one."""
        measured = _known_network(loss_db_scale=2.0)
        simulated = _known_network(loss_db_scale=1.0)
        report = correlate(measured, simulated, Band(1e9, 1e10))
        assert report["residuals"]["magnitude_db"]["mean"] < -0.05, report

    def a_delay_difference_is_detected() -> None:
        measured = _known_network(delay_scale=1.10)
        simulated = _known_network(delay_scale=1.00)
        report = correlate(measured, simulated, Band(1e9, 1e10),
                           metrics=("group_delay_ps",))
        delay = report["residuals"]["group_delay_ps"]
        assert delay["mean"] > 1.0, delay

    def disjoint_sweeps_refuse_rather_than_interpolate() -> None:
        measured = _known_network(points=41)
        simulated = tl.single_line_network(
            tl.frequency_grid(2.0e10, 3.0e10, 41),
            tl.LineSpec(50.0, 3.1, 100.0), z0_port=50.0)
        report = correlate(measured, simulated, Band(1e8, 1e10))
        assert not report["sufficient"], report
        assert "shared frequency point" in report["reason"]

    def the_band_restricts_what_is_compared() -> None:
        network = _known_network()
        narrow = correlate(network, network, Band(5e9, 1e10))
        wide = correlate(network, network, Band(1e8, 1e10))
        assert (narrow["overlap"]["shared_points"]
                < wide["overlap"]["shared_points"])

    def an_er_correction_follows_the_square_of_the_delay_ratio() -> None:
        result = suggest_er_correction(measured_delay_ps=110.0,
                                       simulated_delay_ps=100.0,
                                       assumed_er_eff=3.0)
        assert abs(result["implied_er_eff"] - 3.0 * 1.1 ** 2) < 1e-12, result

    def mismatched_port_counts_are_refused() -> None:
        two_port = _known_network()
        four_port = tl.coupled_line_network(
            tl.frequency_grid(1e8, 1e10, 41),
            tl.LineSpec(45.0, 3.0, 100.0), tl.LineSpec(60.0, 3.2, 100.0))
        try:
            correlate(two_port, four_port, Band(1e8, 1e10))
        except CorrelationError:
            return
        raise AssertionError("networks of different sizes were compared")

    tests = (
        ("self-correlation is exactly zero", a_network_against_itself_has_zero_residual),
        ("a loss difference is signed", a_known_loss_difference_shows_up_signed),
        ("a delay difference is detected", a_delay_difference_is_detected),
        ("disjoint sweeps refuse", disjoint_sweeps_refuse_rather_than_interpolate),
        ("the band restricts the comparison", the_band_restricts_what_is_compared),
        ("er correction is the squared ratio", an_er_correction_follows_the_square_of_the_delay_ratio),
        ("mismatched port counts refuse", mismatched_port_counts_are_refused),
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
        if not tl.HAVE_SKRF:
            diag("SELF-TEST SKIPPED: scikit-rf is not installed")
            return 1
        try:
            run_self_tests()
        except (AssertionError, CorrelationError, ValueError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    reason_codes: list[str] = []

    # A missing measurement is the expected state until a board exists. It is
    # NO_COVERAGE - not a failure, and emphatically not a pass.
    if args.measured is None or not args.measured.is_file():
        report = {
            "schema_version": SCHEMA_VERSION, "tool": "correlate",
            "signoff": False, "status": "NO_COVERAGE",
            "effective_status": "NO_COVERAGE",
            "reason_codes": ["MEASUREMENT_ABSENT"],
            "detail": (
                "no measured Touchstone. Until a board is fabricated and a "
                "coupon is measured there is nothing to correlate against, "
                "and the model remains unfalsified rather than confirmed."),
            "limits": LIMITS,
        }
        write_report(report, args.json)
        emit(report)
        diag("no measurement: the model is unfalsified, not validated")
        return 1

    if args.simulated is None:
        parser.error("--simulated is required alongside --measured")
    for edge in ("band_start_hz", "band_stop_hz"):
        if getattr(args, edge) is None:
            parser.error(
                f"--{edge.replace('_', '-')} is required: a residual quoted "
                "over a band the instrument never swept is not a statement "
                "about the model")

    try:
        measured = tl.read_touchstone(args.measured)
        simulated = tl.read_touchstone(args.simulated)
        band = Band(args.band_start_hz, args.band_stop_hz)
        outcome = correlate(measured, simulated, band, args.port_out,
                            args.port_in, tuple(args.metrics),
                            args.grid_tolerance_hz)
    except (CorrelationError, FileNotFoundError,
            tl.TransmissionLineError) as exc:
        parser.error(str(exc))

    status = "GREEN"
    if not outcome["sufficient"]:
        status = "NO_COVERAGE"
        reason_codes.append("INSUFFICIENT_OVERLAP")
    if not args.de_embedded:
        # Not a failure: plenty of useful comparisons are made on raw
        # measurements. But the residual then contains the fixture, and a
        # reader must not mistake it for a model error.
        reason_codes.append("FIXTURE_NOT_DE_EMBEDDED")

    gate: dict[str, object] = {"applied": False}
    if outcome["sufficient"]:
        residuals = outcome["residuals"]
        checks: dict[str, object] = {}
        if (args.max_magnitude_residual_db is not None
                and "magnitude_db" in residuals):
            worst = abs(residuals["magnitude_db"]["worst"])
            within = worst <= args.max_magnitude_residual_db
            checks["magnitude_db"] = {
                "worst_abs": worst,
                "limit": args.max_magnitude_residual_db, "within": within}
            if not within:
                status = "RED"
                reason_codes.append("MAGNITUDE_RESIDUAL_EXCEEDED")
        if (args.max_delay_residual_ps is not None
                and "group_delay_ps" in residuals):
            worst = abs(residuals["group_delay_ps"]["worst"])
            within = worst <= args.max_delay_residual_ps
            checks["group_delay_ps"] = {
                "worst_abs": worst,
                "limit": args.max_delay_residual_ps, "within": within}
            if not within:
                status = "RED"
                reason_codes.append("DELAY_RESIDUAL_EXCEEDED")
        if checks:
            gate = {"applied": True, "checks": checks}

    # Enough overlap to compare reported GREEN on its own. That says the
    # correlation ran, not that the model matches within a stated tolerance -
    # and with no --max-*-residual-* the two were the same word. Note the
    # subtler case this also covers: a limit supplied for a metric that is
    # absent from `residuals` leaves `checks` empty, so the gate was never
    # applied even though the caller believed they had set one.
    gate.update(acceptance_verdict(
        applied=bool(gate["applied"]),
        met=all(check["within"]                    # type: ignore[index]
                for check in gate.get("checks", {}).values()),  # type: ignore[union-attr]
        computed=bool(outcome["sufficient"]),
        missing_reason="no residual limit was applied: either none was "
                       "supplied, or the metric it names is absent from the "
                       "computed residuals"))

    feedback = None
    if (args.assumed_er_eff is not None and outcome["sufficient"]
            and "group_delay_ps" in outcome["residuals"]):
        try:
            from common.correlate import extract
            _shared, measured_index, simulated_index = common_grid(
                np.asarray(measured.f, dtype=float),
                np.asarray(simulated.f, dtype=float), band,
                args.grid_tolerance_hz)
            measured_delay = float(np.mean(extract(
                measured, args.port_out, args.port_in, measured_index,
                "group_delay_ps")))
            simulated_delay = float(np.mean(extract(
                simulated, args.port_out, args.port_in, simulated_index,
                "group_delay_ps")))
            feedback = suggest_er_correction(measured_delay, simulated_delay,
                                             args.assumed_er_eff)
        except (CorrelationError, ValueError) as exc:
            feedback = {"unavailable": str(exc)}

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "correlate",
        "signoff": False,
        "status": status,
        "effective_status": status,
        "reason_codes": sorted(set(reason_codes)),
        "measured": str(args.measured),
        "simulated": str(args.simulated),
        "de_embedded": bool(args.de_embedded),
        "port_out": args.port_out,
        "port_in": args.port_in,
        "overlap": outcome["overlap"],
        "residuals": outcome["residuals"],
        "sufficient": outcome["sufficient"],
        "gate": gate,
        "model_feedback": feedback,
        "limits": LIMITS,
    }
    if not outcome["sufficient"]:
        report["detail"] = outcome.get("reason", "")

    diag("")
    diag(f"correlate: {outcome['overlap']['shared_points']} shared point(s) "
         f"in {band.start_hz / 1e9:.3f}-{band.stop_hz / 1e9:.3f} GHz")
    for metric, values in outcome["residuals"].items():
        diag(f"  {metric}: mean {values['mean']:+.4f}  "
             f"rms {values['rms']:.4f}  worst {values['worst']:+.4f} "
             f"@ {values['worst_frequency_hz'] / 1e9:.3f} GHz")
    if not args.de_embedded:
        diag("  NOTE: fixture not declared de-embedded; the residual "
             "contains the launch")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
