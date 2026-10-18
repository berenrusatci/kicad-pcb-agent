#!/usr/bin/env python3
"""Decoupling coverage: finds EVERY IC power pin on a real board and measures
its distance to the nearest suitable decoupling capacitor.

backend: pcbnew  (lazy import, confined to the functions that read the board)

WHY THIS EXISTS ALONGSIDE ``measure_board.py``
-----------------------------------------------
``measure_board.py --decoupling REF.PAD:REF.PAD`` measures the pairs the
CALLER NAMES. That is the right tool when you already know which pin/cap
pair matters and want a precise number for a receipt.

It cannot answer the question this module answers: "is there an IC power pin
somewhere on this board that NOBODY thought to check?" A forgotten pin
silently falls outside coverage there - it is simply never named, so it
never appears as a violation, and the report looks clean. This module
DISCOVERS the pins from the board itself, so the coverage count is derived
from the design rather than from the operator's memory.

TWO DISTINCT VIOLATIONS, NOT ONE
--------------------------------
* ``capacitor_too_far`` - a suitable cap exists but sits beyond the limit.
* ``no_capacitor`` - no suitable cap exists on the board AT ALL.
These are different defects with different fixes (move a part vs. add one),
so they are never collapsed into a single "decoupling failed" count.

HEURISTIC BOUNDARIES - READ BEFORE TRUSTING A CLEAN RESULT
-----------------------------------------------------------
Everything here is PATTERN MATCHING over footprint names, net names and
value strings. It is a screen, not a netlist-accurate analysis:

* IC detection is by FOOTPRINT PACKAGE NAME (QFN/BGA/SOIC/...). A part in
  an unusual or custom-named footprint is NOT recognised as an IC and its
  pins are never scanned - it drops out of ``scanned``, it does not become
  a violation. Always read ``scanned`` alongside the verdict.
* SOT is deliberately split by pad count: SOT-23-3 is usually a transistor,
  SOT-23-5/6 is usually an LDO/ESD array. The threshold is a convention,
  not a rule of physics.
* Power pins are matched by NET NAME pattern (VCC/VDD/3V3/...). A rail with
  a project-specific name (e.g. ``VDD_CAM_A``) matches only because
  ``VDD`` appears as an underscore-separated token; a name like
  ``CAMERA_SUPPLY`` does NOT match and its pins are missed.
* "Suitable" means the capacitance parses AND falls in
  ``DEFAULT_VALUE_RANGE_F``. A cap whose value string cannot be parsed is
  DROPPED from the candidate list rather than guessed at.
* The distance is PAD CENTRE to FOOTPRINT ORIGIN, in straight line. It does
  not model the actual current loop (pin -> via -> plane -> cap), which is
  what really matters for inductance. A short centre distance is necessary
  but not sufficient.

Ported from the archive's ``pcbnew_koprusu.py::dekuplaj_mesafe_kontrolu``,
whose auto-discovery layer had no equivalent in this bundle.

Usage:
    decoupling_coverage.py <board.kicad_pcb> [--max-distance-mm 3.0]
                           [--json out.json]
    decoupling_coverage.py --self-test

Exit: 0 = no FAIL, 1 = at least one FAIL or NO_COVERAGE, 2 = run error.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, board_exit_code, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.tuning import decoupling_limits  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "decoupling_coverage"

# Package-name signatures that mean "this footprint holds an IC". Kept wider
# than just QFN/BGA on purpose: real boards put LDOs, ESD arrays and level
# shifters in SOIC/SSOP/DFN/SON packages, and a QFN/BGA-only list would miss
# most of the ICs that actually need decoupling.
IC_PACKAGE_SIGNATURES = ("QFN", "BGA", "SOIC", "SSOP", "TSSOP", "LQFP", "QFP",
                         "DFN", "SON")

# SOT is used both for 3-pin transistors and for 5/6-pin LDO/ESD parts, so it
# is split by pad count rather than by name alone.
SOT_SIGNATURE = "SOT"
SOT_MIN_PADS_FOR_IC = 5

# Net-name tokens that mean "this is a supply pin". Matched per
# underscore-separated token, so VDD_CAM matches on VDD.
POWER_NET_PATTERNS = (
    "VCC", "VDD", "3V3", "3.3V", "+3V3", "+3.3V", "5V", "+5V", "1V8", "+1V8",
    "1V2", "+1V2", "AVDD", "DVDD", "VBAT", "VBUS", "VIN",
)

# What counts as a DECOUPLING cap, as opposed to a bulk/filter cap. The band
# brackets the usual 100 nF target. Bulk electrolytics and small RF caps are
# deliberately excluded: they do not serve this role.
DEFAULT_VALUE_RANGE_F = (47e-9, 220e-9)

# The distance limit is READ, not written here. Three modules carried this
# quantity as a literal - this one at 3.0 mm, measure_board.py and
# ngspice.py at 1.5 mm - so two tools in this bundle could reach opposite
# verdicts about the same capacitor, and which you believed depended on
# which tool you ran. Both numbers were real but meant different things:
# 3.0 mm is the violation LIMIT (a datasheet-typical upper bound), 1.5 mm
# the placement TARGET. common/tuning.py now serves both.

_VALUE_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([pnuµm]?)F?\s*$", re.I)
_UNIT_MULTIPLIER = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
                    "": 1.0}


def parse_capacitance_f(value: str) -> float | None:
    """``"100nF"``/``"0.1uF"``/``"100n"``/``"22pF"`` -> farads.

    Returns None when the string cannot be parsed - NO VALUE IS INVENTED;
    the caller drops that capacitor from the candidate list rather than
    assuming what it might have been.
    """
    match = _VALUE_PATTERN.match(value or "")
    if not match:
        return None
    unit = (match.group(2) or "").lower()
    if unit not in _UNIT_MULTIPLIER:
        return None
    return float(match.group(1)) * _UNIT_MULTIPLIER[unit]


def is_ic_footprint(footprint_id: str, pad_count: int) -> bool:
    """Package-name heuristic; see the module docstring's boundary note."""
    name = (footprint_id or "").upper()
    if any(sig in name for sig in IC_PACKAGE_SIGNATURES):
        return True
    return SOT_SIGNATURE in name and pad_count >= SOT_MIN_PADS_FOR_IC


def is_power_net(net_name: str) -> bool:
    """Token-wise match, so ``VDD_CAM_A`` counts but ``CAMERA_SUPPLY``
    does not (see the module docstring's boundary note)."""
    name = (net_name or "").strip().upper()
    if not name:
        return False
    tokens = name.replace("+", "").split("_")
    wanted = {p.replace("+", "") for p in POWER_NET_PATTERNS}
    return any(token in wanted for token in tokens)


def find_capacitor_candidates(board, value_range_f=DEFAULT_VALUE_RANGE_F) -> list[dict]:
    """Every footprint that looks like a decoupling cap AND whose value
    parses into ``value_range_f``."""
    from common.units import xy
    low, high = value_range_f
    candidates: list[dict] = []
    for footprint in board.GetFootprints():
        reference = footprint.GetReference() or ""
        footprint_id = (str(footprint.GetFPID().GetLibItemName())
                        if hasattr(footprint, "GetFPID") else "")
        if not (footprint_id.upper().startswith("C_")
                or reference.upper().startswith("C")):
            continue
        farads = parse_capacitance_f(footprint.GetValue())
        if farads is None or not (low <= farads <= high):
            continue
        candidates.append({"ref": reference,
                           "position_mm": xy(footprint.GetPosition()),
                           "value_f": farads})
    return candidates


def check_decoupling_coverage(board, max_distance_mm: float | None = None,
                              value_range_f=DEFAULT_VALUE_RANGE_F) -> Finding:
    """Every discovered IC power pin against the nearest suitable cap.

    ``scanned`` counts the power pins actually FOUND, so a board where the
    heuristics recognise nothing reports NO_COVERAGE rather than a clean
    pass - "we looked and found nothing to check" and "everything is fine"
    are different answers.
    """
    # None means "read the project rule". The limit and the target are
    # different numbers with different jobs - see common/tuning.py.
    limit_mm, target_mm = decoupling_limits()
    if max_distance_mm is None:
        max_distance_mm = limit_mm

    from common.units import xy
    candidates = find_capacitor_candidates(board, value_range_f)

    scanned = 0
    violations: list[dict] = []
    for footprint in board.GetFootprints():
        footprint_id = (str(footprint.GetFPID().GetLibItemName())
                        if hasattr(footprint, "GetFPID") else "")
        pads = list(footprint.Pads())
        if not is_ic_footprint(footprint_id, len(pads)):
            continue
        for pad in pads:
            net_name = pad.GetNetname()
            if not is_power_net(net_name):
                continue
            scanned += 1
            pin_xy = xy(pad.GetPosition())
            label = f"{footprint.GetReference()}.{pad.GetNumber()}"

            if not candidates:
                violations.append({
                    "problem": "no_capacitor",
                    "pin": label, "net": net_name, "pin_position_mm": pin_xy,
                    "message": (
                        f"no decoupling capacitor in the "
                        f"{value_range_f[0] * 1e9:.0f}-{value_range_f[1] * 1e9:.0f}nF "
                        "band exists anywhere on this board"),
                })
                continue

            nearest = min(candidates, key=lambda c: math.dist(c["position_mm"], pin_xy))
            distance = round(math.dist(nearest["position_mm"], pin_xy), 4)
            if distance > max_distance_mm:
                violations.append({
                    "problem": "capacitor_too_far",
                    "pin": label, "net": net_name, "pin_position_mm": pin_xy,
                    "nearest_capacitor": nearest["ref"],
                    "nearest_capacitor_position_mm": nearest["position_mm"],
                    "distance_mm": distance, "limit_mm": max_distance_mm,
                })

    return result(
        "decoupling_coverage", scanned, violations,
        f"limit {max_distance_mm}mm (placement target {target_mm}mm), "
        f"suitable candidates found: {len(candidates)}. "
        "Power pins are DISCOVERED from the board, not supplied by the caller; "
        "read 'scanned' - a part in an unrecognised footprint is never scanned "
        "and therefore never becomes a violation.")


def self_test() -> list[str]:
    """Pure logic on the heuristics. Does NOT need pcbnew or a board."""
    problems: list[str] = []

    for text, expected in (("100nF", 100e-9), ("0.1uF", 0.1e-6), ("100n", 100e-9),
                           ("22pF", 22e-12), ("4.7uF", 4.7e-6)):
        got = parse_capacitance_f(text)
        if got is None or abs(got - expected) > expected * 1e-9:
            problems.append(f"parse_capacitance_f({text!r}) -> {got}, want {expected}")

    # FAULT INJECTION: an unparsable value must be dropped, never guessed.
    for bad in ("DNP", "", "100 ohm", "abc"):
        if parse_capacitance_f(bad) is not None:
            problems.append(f"parse_capacitance_f({bad!r}) invented a value")

    if not is_ic_footprint("QFN-48-1EP_7x7mm", 49):
        problems.append("a QFN was not recognised as an IC")
    if is_ic_footprint("R_0402_1005Metric", 2):
        problems.append("a resistor was recognised as an IC")
    # SOT is split by pad count, not by name.
    if is_ic_footprint("SOT-23", 3):
        problems.append("a 3-pin SOT transistor was recognised as an IC")
    if not is_ic_footprint("SOT-23-5", 5):
        problems.append("a 5-pin SOT LDO was NOT recognised as an IC")

    if not is_power_net("VDD_CAM_A"):
        problems.append("VDD_CAM_A was not matched as a power net")
    if is_power_net("SPI_MOSI"):
        problems.append("SPI_MOSI was matched as a power net")
    if is_power_net(""):
        problems.append("an empty net name was matched as a power net")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--max-distance-mm", type=float,
                        default=None)
    parser.add_argument("--min-value-nf", type=float,
                        default=DEFAULT_VALUE_RANGE_F[0] * 1e9)
    parser.add_argument("--max-value-nf", type=float,
                        default=DEFAULT_VALUE_RANGE_F[1] * 1e9)
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: decoupling_coverage")
        return 0

    if not args.board:
        parser.error("the board path is required")

    import pcbnew
    board = pcbnew.LoadBoard(args.board)
    finding = check_decoupling_coverage(
        board, args.max_distance_mm,
        (args.min_value_nf * 1e-9, args.max_value_nf * 1e-9))

    report = board_report(TOOL_NAME, args.board, [finding], params={
        "max_distance_mm": args.max_distance_mm,
        "value_range_nf": [args.min_value_nf, args.max_value_nf],
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
