#!/usr/bin/env python3
"""Read stability margins off a loop-gain response.

WHAT THIS DOES AND DOES NOT MODEL
--------------------------------------------------------------------------
It models nothing. A regulator vendor's compensation network is theirs, and
this tool does not reproduce, approximate or guess it. It reads a sweep
somebody else produced - an ngspice AC analysis, or a network analyser
looking at an injection transformer - and does arithmetic on it.

That distinction is what makes the tool usable without the vendor's
application note in hand: locating a 0 dB crossing and measuring the phase
there is not proprietary. What IS specific - the acceptance thresholds -
are inputs. Without them the margins are computed and reported but nothing
calls them acceptable, and the run is NO_COVERAGE rather than green.

THE FOUR SILENT ERRORS IT REFUSES TO MAKE
--------------------------------------------------------------------------
Explained in full in ``common/loopgain.py``; in short:

  * A loop gain can cross 0 dB MORE THAN ONCE, and the loop is only as good
    as the worst of those crossings. Reporting "the" phase margin discards
    the others.
  * Phase can pass -180 degrees while gain is still above 0 dB. That loop is
    stable as built and unstable if the gain FALLS - which is the direction
    it moves as parts age and as ceramics lose value to DC bias. A phase
    margin at crossover cannot see it.
  * Phase arrives WRAPPED. A margin computed from wrapped phase is
    confidently wrong rather than obviously wrong, so phase is unwrapped and
    the wrap count is reported as evidence that it happened.
  * A sweep that never reaches 0 dB has no margin to report. That is not a
    pass and it is not an extrapolation problem; the band was too narrow or
    the response is not the loop you think it is.

Input formats: CSV with a ``freq_hz,mag_db,phase_deg`` header, or JSON
``{"sweep": [[f, mag_db, phase_deg], ...]}``. ngspice's AC output converts
to either.

Usage:
    loop_stability.py --sweep loop.csv [--min-phase-margin-deg 45]
                      [--min-gain-margin-db 10] [--json stability.json]
    loop_stability.py --self-test

Exit codes: 0 = GREEN, 1 = RED or NO_COVERAGE, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, NO_COVERAGE, result, summarise  # noqa: E402
from common.loopgain import (COARSE_SAMPLING, CONDITIONALLY_STABLE,  # noqa: E402
                             GAIN_MARGIN_LOW, MULTIPLE_CROSSOVERS,
                             NO_CROSSOVER, NO_PHASE_CROSSOVER,
                             PHASE_MARGIN_LOW, analyse)
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "loop-stability/v1"
TOOL_NAME = "loop_stability"

REQUIRED_COLUMNS = ("freq_hz", "mag_db", "phase_deg")


def read_sweep(path: Path) -> List[List[float]]:
    """Read a sweep from CSV or JSON.

    The CSV header is REQUIRED and its column names are checked. A headerless
    three-column file is ambiguous - magnitude and phase are both plain
    numbers, and swapping them yields a response that parses cleanly and
    reports meaningless margins.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        doc = json.loads(text)
        sweep = doc.get("sweep") if isinstance(doc, dict) else doc
        if not isinstance(sweep, list):
            raise ValueError(f"{path}: expected a 'sweep' array")
        return [[float(v) for v in row] for row in sweep]

    reader = csv.reader(line for line in text.splitlines() if line.strip())
    rows = list(reader)
    if not rows:
        raise ValueError(f"{path}: empty file")
    header = [cell.strip().lower() for cell in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ValueError(
            f"{path}: CSV header must name {list(REQUIRED_COLUMNS)}; missing "
            f"{missing}. A headerless file is ambiguous - magnitude and phase "
            "are both plain numbers, and swapping them parses cleanly.")
    index = [header.index(c) for c in REQUIRED_COLUMNS]
    return [[float(row[i]) for i in index] for row in rows[1:]]


def margin_finding(report, min_pm: Optional[float],
                   min_gm: Optional[float]) -> Finding:
    check = "stability_margins_within_budget"
    if not report.crossings:
        return Finding(check, NO_COVERAGE, 0, [],
                       f"the response never reaches 0 dB between "
                       f"{report.band_hz[0]:g} and {report.band_hz[1]:g} Hz; "
                       "there is no margin to report and this is not a pass")
    if min_pm is None and min_gm is None:
        return Finding(check, NO_COVERAGE, len(report.crossings), [],
                       f"worst phase margin {report.worst_phase_margin_deg:.1f} "
                       "deg computed, but no --min-phase-margin-deg or "
                       "--min-gain-margin-db was declared, so nothing judges it")
    violations = []
    codes = report.reason_codes(min_pm, min_gm)
    if PHASE_MARGIN_LOW in codes:
        violations.append({"issue": PHASE_MARGIN_LOW,
                           "worst_deg": report.worst_phase_margin_deg,
                           "budget_deg": min_pm,
                           "at_hz": min(report.crossings,
                                        key=lambda c: c.phase_margin_deg).freq_hz})
    if GAIN_MARGIN_LOW in codes:
        violations.append({"issue": GAIN_MARGIN_LOW,
                           "worst_db": report.worst_gain_margin_db,
                           "budget_db": min_gm})
    return result(check, len(report.crossings), violations,
                  f"{len(report.crossings)} gain crossing(s) judged against "
                  "the declared budget")


def shape_finding(report) -> Finding:
    """Things that are wrong with the RESPONSE regardless of any budget."""
    check = "response_shape"
    violations = []
    codes = report.reason_codes()
    if MULTIPLE_CROSSOVERS in codes:
        violations.append({
            "issue": MULTIPLE_CROSSOVERS,
            "count": len(report.crossings),
            "margins_deg": [c.phase_margin_deg for c in report.crossings],
            "detail": "the loop is only as good as its worst crossing; a "
                      "single reported phase margin would have hidden the rest"})
    if CONDITIONALLY_STABLE in codes:
        violations.append({
            "issue": CONDITIONALLY_STABLE,
            "detail": "phase passes -180 deg while gain is still above 0 dB. "
                      "This loop is stable as built and unstable if the gain "
                      "FALLS - as parts age, as ceramics lose value to DC bias, "
                      "or at light load. A phase margin at crossover cannot "
                      "see this."})
    if COARSE_SAMPLING in codes:
        worst = max(report.crossings, key=lambda c: c.bracket_decades)
        violations.append({
            "issue": COARSE_SAMPLING,
            "bracket_hz": list(worst.bracket_hz),
            "bracket_decades": worst.bracket_decades,
            "detail": "a crossing located between samples this far apart is "
                      "not known to the precision it is printed with"})
    scanned = len(report.crossings) + len(report.phase_crossings)
    if scanned == 0:
        return Finding(check, NO_COVERAGE, 0, [],
                       "the response crosses neither 0 dB nor -180 deg in band")
    return result(check, scanned, violations,
                  f"{len(report.crossings)} gain and "
                  f"{len(report.phase_crossings)} phase crossing(s) examined")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read stability margins off a loop-gain response.")
    parser.add_argument("--sweep", type=Path,
                        help="loop gain sweep (.csv with a header, or .json)")
    parser.add_argument("--min-phase-margin-deg", type=float,
                        help="acceptance threshold; never defaulted")
    parser.add_argument("--min-gain-margin-db", type=float,
                        help="acceptance threshold; never defaulted")
    parser.add_argument("--max-bracket-decades", type=float, default=0.25,
                        help="how far apart the samples bracketing a crossing "
                             "may be before the location is called imprecise")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.sweep:
        raise ValueError("--sweep is required")

    report = analyse(read_sweep(args.sweep))
    findings = [margin_finding(report, args.min_phase_margin_deg,
                               args.min_gain_margin_db),
                shape_finding(report)]
    counts = summarise(findings)
    if counts.get("FAIL"):
        status = "RED"
    elif counts.get(NO_COVERAGE):
        status = "NO_COVERAGE"
    else:
        status = "GREEN"
    reason_codes = report.reason_codes(args.min_phase_margin_deg,
                                       args.min_gain_margin_db,
                                       args.max_bracket_decades)

    out = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "simulation",
        "models_the_loop": False,
        "sweep": str(args.sweep),
        "budget": {"min_phase_margin_deg": args.min_phase_margin_deg,
                   "min_gain_margin_db": args.min_gain_margin_db},
        **report.to_dict(),
        "findings": findings,
        "coverage": summarise(findings),
    }

    diag("")
    diag(f"loop stability: {report.band_hz[0]:g} .. {report.band_hz[1]:g} Hz, "
         f"{report.wraps_removed} phase wrap(s) removed")
    for crossing in report.crossings:
        diag(f"  crossover {crossing.freq_hz:>12.4g} Hz  "
             f"phase margin {crossing.phase_margin_deg:+7.1f} deg  "
             f"({crossing.direction}, bracket "
             f"{crossing.bracket_decades:.3f} decades)")
    for crossing in report.phase_crossings:
        flag = "  <-- gain still above 0 dB" if crossing.above_unity else ""
        diag(f"  -180 deg  {crossing.freq_hz:>12.4g} Hz  "
             f"gain margin {crossing.gain_margin_db:+7.1f} dB{flag}")
    if not report.crossings:
        diag("  no 0 dB crossing in band - nothing to report, and not a pass")
    diag(f"  status: {status}")

    write_report(out, args.json)
    emit(out)
    return status_exit_code(status)


def _synthetic_two_pole(f_hz: float, dc_gain_db: float = 60.0,
                        p1: float = 10.0, p2: float = 1e5):
    """A textbook two-pole response, used only to exercise the reader.

    This is NOT a regulator model and is never used to judge a design; it
    exists so the tests have a response whose margins are known in closed
    form, which is the only way to prove the reader reads correctly.
    """
    mag = dc_gain_db
    phase = 0.0
    for pole in (p1, p2):
        mag -= 10.0 * math.log10(1.0 + (f_hz / pole) ** 2)
        phase -= math.degrees(math.atan(f_hz / pole))
    return mag, phase


def _self_test() -> int:
    # A response whose margins follow from closed form.
    rows = []
    for step in range(0, 141):
        freq = 10.0 ** (step / 20.0)          # 1 Hz .. 1 MHz, 20/decade
        mag, phase = _synthetic_two_pole(freq)
        rows.append([freq, mag, phase])
    report = analyse(rows)
    assert len(report.crossings) == 1, [c.freq_hz for c in report.crossings]
    crossing = report.crossings[0]
    # Two poles can never reach -180 deg, only approach it, so the margin is
    # strictly positive and the loop has no gain-margin crossing at all.
    assert crossing.phase_margin_deg > 0, crossing
    assert report.phase_crossings == [], report.phase_crossings
    # Closed form: the second pole is far above crossover, so crossover sits
    # near the frequency where the single-pole roll-off hits 0 dB.
    expected = 10.0 * (10.0 ** (60.0 / 20.0))
    assert abs(crossing.freq_hz / expected - 1.0) < 0.05, crossing.freq_hz

    # Wrapped phase must not change the answer.
    wrapped = [[f, m, ((p + 180.0) % 360.0) - 180.0] for f, m, p in rows]
    rewrapped = analyse(wrapped)
    assert abs(rewrapped.crossings[0].phase_margin_deg
               - crossing.phase_margin_deg) < 1e-6, rewrapped.crossings[0]

    # Two crossings: the worst one must win.
    two = [[1.0, 20.0, -100.0], [10.0, -5.0, -170.0],
           [100.0, 5.0, -120.0], [1000.0, -30.0, -150.0]]
    multi = analyse(two)
    assert len(multi.crossings) == 3, [c.freq_hz for c in multi.crossings]
    assert multi.worst_phase_margin_deg == min(
        c.phase_margin_deg for c in multi.crossings)
    assert MULTIPLE_CROSSOVERS in multi.reason_codes()

    # Conditional stability: phase past -180 while gain is still up.
    conditional = [[1.0, 40.0, -90.0], [10.0, 30.0, -200.0],
                   [100.0, 20.0, -170.0], [1000.0, -20.0, -160.0]]
    cond = analyse(conditional)
    assert CONDITIONALLY_STABLE in cond.reason_codes(), cond.reason_codes()

    # A sweep with no crossing reports none, and it is not a pass.
    flat = analyse([[1.0, 40.0, -20.0], [10.0, 39.0, -25.0]])
    assert flat.crossings == []
    assert NO_CROSSOVER in flat.reason_codes()
    assert margin_finding(flat, 45.0, None)["status"] == NO_COVERAGE

    # An undeclared budget is NO_COVERAGE even though the margin exists.
    assert margin_finding(report, None, None)["status"] == NO_COVERAGE

    # An unsorted axis is refused rather than sorted.
    try:
        analyse([[10.0, 1.0, 0.0], [1.0, -1.0, 0.0]])
    except ValueError as exc:
        assert "increase" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unsorted frequency axis was accepted")

    assert NO_PHASE_CROSSOVER in report.reason_codes()

    diag("SELF-TEST PASS: crossover matches closed form, wrapped phase gives "
         "the same margin, all crossings found and the worst wins, "
         "conditional stability detected, no-crossing is NO_COVERAGE, "
         "undeclared budget is NO_COVERAGE, unsorted axis refused")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
