#!/usr/bin/env python3
"""EMI/EMC design-rule engine: 3W, 20H and the lambda/20 via-stitching pitch.

Port of ``emi_emc_kural_motoru.py``. Three widely cited high-speed design
rules are computed AND measured input is checked against them, so the tool
answers "does this board obey the rule?", not merely "what is the target?".

THE THREE RULES
---------------
1. **3W rule (crosstalk)** — the CENTRE-TO-CENTRE distance between two
   high-speed traces must be at least three times the trace width
   (``three_w``). This is the shortcut cited from Henry Ott's "Noise
   Reduction Techniques in Electronic Systems" and from many fab/EMC
   application notes; 3W centre-to-centre means the EDGE-TO-EDGE gap is 2W
   (distance - width = 2W). This module returns BOTH definitions so the
   caller cannot confuse them — the "3W or 2W?" mix-up is the single most
   common mistake made with this rule.
2. **20H rule (edge radiation / fringing)** — the VCC/power plane must be
   pulled back INSIDE the board edge relative to the GND plane; the setback
   must be at least 20x the dielectric thickness between the two planes
   (``twenty_h_setback_mm``). The literature (Mark Montrose) notes 20H for
   ~70% field reduction and ~100H for ~98%; the default here is the
   conservative 20H and ``factor`` can be raised to 100.
3. **Via stitching pitch** — the maximum edge GND via pitch is
   ``wavelength / 20`` (``stitching_max_pitch_mm``). This is PHYSICALLY THE
   SAME formula the board-side stitching checker uses; the speed of light
   is imported from ``common.units`` (single source of truth) rather than
   re-typed. The difference is one of coverage: a pcbnew-based checker
   MEASURES real via positions from a board file, while
   ``check_via_pitch`` here audits an ALREADY-MEASURED pitch list handed in
   from outside. The two are complements, not substitutes — neither
   replaces the other.

HONESTY NOTE (the point of this module)
---------------------------------------
3W / 20H / lambda-20 are NOT numbers published by FCC or CISPR themselves.
They are DESIGN HEURISTICS that became common practice in EMC engineering,
aimed at reducing field/coupling. Real FCC Part 15 / CISPR 32 compliance can
only be proven by measurement in an accredited EMC lab. This module is an
A-level (measurable from the given input) pre-compliance layer; it DOES NOT
REPLACE a lab test, and no output of it may be reported as compliance.

Usage:
    python emc_rules.py <problem.json> [--json out.json]
                        [--trace-width-mm 0.2] [--dielectric-thickness-mm 0.1]
                        [--knee-frequency-ghz 5.0] [--er-eff 4.5]
                        [--self-test]

The positional JSON file carries the geometry/frequency parameters and any
measurements to audit; the flags above are OVERRIDES for the scalars in it.

Exit codes: 0 = every check PASSed, 1 = a check FAILed or had NO_COVERAGE,
2 = the tool could not produce an answer.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    Finding,
    result,
    summarise,
)
from common.report_io import (  # noqa: E402
    EXIT_FAIL,
    EXIT_OK,
    diag,
    emit,
    run_cli,
    write_report,
)
from common.units import C_MPS  # noqa: E402

SCHEMA_VERSION = "emc-rules-report/v1"
TOOL_NAME = "emc_rules"

DEFAULT_THREE_W_FACTOR = 3.0
DEFAULT_TWENTY_H_FACTOR = 20.0
DEFAULT_STITCHING_FACTOR = 20.0
DEFAULT_ER_EFF = 4.5

THREE_W_CHECK = "emc_3w_crosstalk"
TWENTY_H_CHECK = "emc_20h_edge_radiation"
VIA_PITCH_CHECK = "emc_via_stitching_pitch"


# --------------------------------------------------------------------------- #
# 1) 3W rule - crosstalk avoidance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ThreeWTarget:
    trace_width_mm: float
    factor: float
    min_center_to_center_mm: float
    min_edge_to_edge_mm: float
    source_note: str = (
        "3W rule (Henry Ott / common EMC application notes, a REPRESENTATIVE "
        "heuristic) — NOT a table value published by FCC or CISPR."
    )


def three_w(trace_width_mm: float,
            factor: float = DEFAULT_THREE_W_FACTOR) -> ThreeWTarget:
    """Minimum centre-to-centre AND edge-to-edge spacing per the 3W rule.

    ``min_edge_to_edge_mm = min_center_to_center_mm - trace_width_mm``
    (assuming two traces of equal width). For two traces of DIFFERENT width
    use ``three_w_mixed_width``.
    """
    if trace_width_mm <= 0:
        raise ValueError(
            f"three_w: trace_width_mm must be positive, got {trace_width_mm!r}")
    if factor < 1.0:
        raise ValueError(
            "three_w: factor must be >= 1.0 (below 1.0 makes crosstalk WORSE), "
            f"got {factor!r}")
    center = round(trace_width_mm * factor, 4)
    return ThreeWTarget(
        trace_width_mm=trace_width_mm,
        factor=factor,
        min_center_to_center_mm=center,
        min_edge_to_edge_mm=round(center - trace_width_mm, 4),
    )


def three_w_mixed_width(width_a_mm: float, width_b_mm: float,
                        factor: float = DEFAULT_THREE_W_FACTOR) -> ThreeWTarget:
    """3W target for two traces of different width, via the MEAN width.

    Common practical approximation: the mean sits between "each trace wants
    3x its own width", i.e. the most conservative shared point that is still
    defensible without a field solver.
    """
    mean = (width_a_mm + width_b_mm) / 2.0
    target = three_w(mean, factor)
    edge = round(target.min_center_to_center_mm - (width_a_mm + width_b_mm) / 2.0, 4)
    return ThreeWTarget(
        trace_width_mm=mean,
        factor=factor,
        min_center_to_center_mm=target.min_center_to_center_mm,
        min_edge_to_edge_mm=edge,
        source_note=target.source_note
        + f" (mixed width: A={width_a_mm}mm, B={width_b_mm}mm, mean used)",
    )


@dataclass(frozen=True)
class TracePairMeasurement:
    trace_a: str
    trace_b: str
    width_a_mm: float
    width_b_mm: float
    center_to_center_mm: float


def check_three_w(measurements: Sequence[TracePairMeasurement],
                  factor: float = DEFAULT_THREE_W_FACTOR) -> Finding:
    """Audit measured (or hand-entered) trace pairs against the 3W target."""
    violations = []
    for m in measurements:
        target = three_w_mixed_width(m.width_a_mm, m.width_b_mm, factor)
        if m.center_to_center_mm < target.min_center_to_center_mm:
            violations.append({
                "a": m.trace_a,
                "b": m.trace_b,
                "measured_center_to_center_mm": round(m.center_to_center_mm, 4),
                "min_center_to_center_mm": target.min_center_to_center_mm,
                "shortfall_mm": round(
                    target.min_center_to_center_mm - m.center_to_center_mm, 4),
            })
    return result(
        THREE_W_CHECK,
        len(measurements),
        violations,
        f"3W rule (factor={factor}); centre-to-centre spacing audited.",
    )


# --------------------------------------------------------------------------- #
# 2) 20H rule - edge radiation / fringing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TwentyHTarget:
    dielectric_thickness_mm: float
    factor: float
    min_setback_mm: float
    source_note: str = (
        "20H rule (Mark Montrose, 'EMC and the Printed Circuit Board') — "
        "targets ~70% field reduction; ~100H (factor=100) is cited for ~98%."
    )


def twenty_h_setback_mm(dielectric_thickness_mm: float,
                        factor: float = DEFAULT_TWENTY_H_FACTOR) -> TwentyHTarget:
    """How far INSIDE the board edge the power plane must sit versus GND."""
    if dielectric_thickness_mm <= 0:
        raise ValueError(
            "twenty_h_setback_mm: dielectric_thickness_mm must be positive, "
            f"got {dielectric_thickness_mm!r}")
    if factor < 1.0:
        raise ValueError(
            f"twenty_h_setback_mm: factor must be >= 1.0, got {factor!r}")
    return TwentyHTarget(
        dielectric_thickness_mm=dielectric_thickness_mm,
        factor=factor,
        min_setback_mm=round(dielectric_thickness_mm * factor, 4),
    )


@dataclass(frozen=True)
class PlanePairMeasurement:
    """``power_setback_mm``: power-plane distance from the board edge.
    ``ground_setback_mm``: GND-plane distance from the board edge.

    The 20H rule requires the power plane to be further INSIDE than GND, not
    just far from the edge in absolute terms — ``check_twenty_h`` therefore
    performs BOTH tests.
    """

    layer_pair: str
    dielectric_thickness_mm: float
    power_setback_mm: float
    ground_setback_mm: float


def check_twenty_h(measurements: Sequence[PlanePairMeasurement],
                   factor: float = DEFAULT_TWENTY_H_FACTOR) -> Finding:
    """Report two distinct violation kinds inside one check:

      (a) the power setback is below the absolute 20H target,
      (b) the power plane is not far enough inside GND (setback delta).

    These are DIFFERENT root causes — one means "the 20H calculation was
    never done", the other "it was done but power/GND got swapped" (a common
    mistake). Merging them would hide the second.
    """
    violations = []
    for m in measurements:
        target = twenty_h_setback_mm(m.dielectric_thickness_mm, factor)
        problems = []
        if m.power_setback_mm < target.min_setback_mm:
            problems.append(
                f"power setback {m.power_setback_mm}mm < target "
                f"{target.min_setback_mm}mm")
        delta = m.power_setback_mm - m.ground_setback_mm
        if delta < target.min_setback_mm:
            problems.append(
                f"power-GND setback delta {round(delta, 4)}mm < target "
                f"{target.min_setback_mm}mm (power plane is not far enough "
                "inside GND)")
        if problems:
            violations.append({
                "layer_pair": m.layer_pair,
                "power_setback_mm": m.power_setback_mm,
                "ground_setback_mm": m.ground_setback_mm,
                "min_setback_mm": target.min_setback_mm,
                "problems": problems,
            })
    return result(
        TWENTY_H_CHECK,
        len(measurements),
        violations,
        f"20H rule (factor={factor}); power/GND plane edge setback audited.",
    )


# --------------------------------------------------------------------------- #
# 3) Via stitching pitch - wavelength / 20
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StitchingTarget:
    knee_frequency_ghz: float
    er_eff: float
    wavelength_mm: float
    factor: float
    max_pitch_mm: float
    source_note: str = (
        "lambda/20 target — the edge GND via pitch keeps fringing fields from "
        "radiating off the board edge by staying under 1/20 of the wavelength "
        "derived from the knee frequency. Physically identical to the "
        "board-side stitching target; C_MPS comes from common.units (single "
        "source of truth)."
    )


def stitching_max_pitch_mm(knee_frequency_ghz: float,
                           er_eff: float = DEFAULT_ER_EFF,
                           factor: float = DEFAULT_STITCHING_FACTOR) -> StitchingTarget:
    """Maximum edge/transition via pitch as ``wavelength / factor``.

    ``knee_frequency_ghz`` is f_knee — derived from the signal EDGE RATE, NOT
    the clock frequency. Feeding a clock frequency here understates the
    required via density; that discipline is the caller's responsibility and
    is deliberately not guessed at here.
    """
    if knee_frequency_ghz <= 0:
        raise ValueError(
            "stitching_max_pitch_mm: knee_frequency_ghz must be positive, "
            f"got {knee_frequency_ghz!r}")
    if er_eff <= 0:
        raise ValueError(
            f"stitching_max_pitch_mm: er_eff must be positive, got {er_eff!r}")
    if factor <= 0:
        raise ValueError(
            f"stitching_max_pitch_mm: factor must be positive, got {factor!r}")
    velocity = C_MPS / math.sqrt(er_eff)
    wavelength_mm = (velocity / (knee_frequency_ghz * 1e9)) * 1000.0
    return StitchingTarget(
        knee_frequency_ghz=knee_frequency_ghz,
        er_eff=er_eff,
        wavelength_mm=round(wavelength_mm, 4),
        factor=factor,
        max_pitch_mm=round(wavelength_mm / factor, 4),
    )


def check_via_pitch(measured_pitches_mm: Sequence[float],
                    knee_frequency_ghz: float,
                    er_eff: float = DEFAULT_ER_EFF,
                    factor: float = DEFAULT_STITCHING_FACTOR) -> Finding:
    """Audit an ALREADY-MEASURED list of consecutive via pitches.

    The list is expected to come from a tool that read a real board; this
    function does not open a board file and therefore cannot invent coverage
    — an empty list is NO_COVERAGE, never a silent PASS.
    """
    target = stitching_max_pitch_mm(knee_frequency_ghz, er_eff, factor)
    violations = [
        {"measured_mm": round(pitch, 4),
         "max_mm": target.max_pitch_mm,
         "excess_mm": round(pitch - target.max_pitch_mm, 4)}
        for pitch in measured_pitches_mm
        if pitch > target.max_pitch_mm
    ]
    return result(
        VIA_PITCH_CHECK,
        len(measured_pitches_mm),
        violations,
        f"f_knee={knee_frequency_ghz}GHz, er_eff={er_eff} -> "
        f"lambda={target.wavelength_mm}mm, max pitch="
        f"{target.max_pitch_mm}mm (lambda/{factor}).",
    )


def overall_status(findings: Sequence[Finding]) -> str:
    """FAIL > NEEDS_HUMAN (NO_COVERAGE) > PASS — the same merge discipline
    the sibling rule engines use. NO_COVERAGE never collapses into PASS."""
    if any(f["status"] == FAIL for f in findings):
        return "FAIL"
    if any(f["status"] == NO_COVERAGE for f in findings):
        return "NEEDS_HUMAN"
    return "PASS"


def exit_code_for(status: str) -> int:
    """0 only when every check PASSed.

    DELIBERATE DIVERGENCE from ``common.findings.board_exit_code``, which
    exits 0 on NO_COVERAGE for backwards compatibility with the four pcbnew
    board checkers. This tool is new, has no such caller contract, and its
    whole reason to exist is that "nothing was measured" must not read as
    success — so NEEDS_HUMAN exits 1 and the caller must look at ``status``.
    """
    return EXIT_OK if status == "PASS" else EXIT_FAIL


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def _number(container: dict, key: str, where: str) -> float:
    if key not in container:
        raise ValueError(f"{where}: missing required field '{key}'")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: '{key}' must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{where}: '{key}' must be finite, got {value!r}")
    return float(value)


def _text(container: dict, key: str, where: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return value


def load_problem(path: Path) -> dict:
    """Read the positional JSON problem file. UTF-8, object at the root."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        problem = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(problem, dict):
        raise ValueError(f"{path}: the root of the problem file must be an object")
    return problem


def parse_trace_pairs(problem: dict) -> list[TracePairMeasurement]:
    out = []
    for index, item in enumerate(problem.get("trace_pairs", []) or []):
        where = f"trace_pairs[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        out.append(TracePairMeasurement(
            trace_a=_text(item, "trace_a", where),
            trace_b=_text(item, "trace_b", where),
            width_a_mm=_number(item, "width_a_mm", where),
            width_b_mm=_number(item, "width_b_mm", where),
            center_to_center_mm=_number(item, "center_to_center_mm", where),
        ))
    return out


def parse_plane_pairs(problem: dict,
                      default_thickness_mm: float) -> list[PlanePairMeasurement]:
    out = []
    for index, item in enumerate(problem.get("plane_pairs", []) or []):
        where = f"plane_pairs[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        thickness = (_number(item, "dielectric_thickness_mm", where)
                     if "dielectric_thickness_mm" in item else default_thickness_mm)
        out.append(PlanePairMeasurement(
            layer_pair=_text(item, "layer_pair", where),
            dielectric_thickness_mm=thickness,
            power_setback_mm=_number(item, "power_setback_mm", where),
            ground_setback_mm=_number(item, "ground_setback_mm", where),
        ))
    return out


def parse_via_pitches(problem: dict) -> list[float]:
    out = []
    for index, value in enumerate(problem.get("via_pitches_mm", []) or []):
        where = f"via_pitches_mm[{index}]"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: must be a number, got {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"{where}: must be finite, got {value!r}")
        out.append(float(value))
    return out


def resolve_params(problem: dict, args: argparse.Namespace) -> dict[str, float]:
    """Problem-file scalars, with the CLI flags acting as overrides."""

    def pick(flag_value, key: str, fallback: float) -> float:
        if flag_value is not None:
            return float(flag_value)
        if key in problem:
            return _number(problem, key, "problem")
        return fallback

    params = {
        "trace_width_mm": pick(args.trace_width_mm, "trace_width_mm", 0.2),
        "dielectric_thickness_mm": pick(
            args.dielectric_thickness_mm, "dielectric_thickness_mm", 0.1),
        "knee_frequency_ghz": pick(
            args.knee_frequency_ghz, "knee_frequency_ghz", 5.0),
        "er_eff": pick(args.er_eff, "er_eff", DEFAULT_ER_EFF),
        "three_w_factor": pick(None, "three_w_factor", DEFAULT_THREE_W_FACTOR),
        "twenty_h_factor": pick(None, "twenty_h_factor", DEFAULT_TWENTY_H_FACTOR),
        "stitching_factor": pick(None, "stitching_factor", DEFAULT_STITCHING_FACTOR),
    }
    return params


def build_report(problem: dict, params: dict[str, float], source: str) -> dict:
    """Compute the three targets and audit every supplied measurement."""
    trace_pairs = parse_trace_pairs(problem)
    plane_pairs = parse_plane_pairs(problem, params["dielectric_thickness_mm"])
    via_pitches = parse_via_pitches(problem)

    three_w_target = three_w(params["trace_width_mm"], params["three_w_factor"])
    twenty_h_target = twenty_h_setback_mm(
        params["dielectric_thickness_mm"], params["twenty_h_factor"])
    stitching_target = stitching_max_pitch_mm(
        params["knee_frequency_ghz"], params["er_eff"], params["stitching_factor"])

    findings = [
        check_three_w(trace_pairs, params["three_w_factor"]),
        check_twenty_h(plane_pairs, params["twenty_h_factor"]),
        check_via_pitch(via_pitches, params["knee_frequency_ghz"],
                        params["er_eff"], params["stitching_factor"]),
    ]
    status = overall_status(findings)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "input": source,
        "model_class": "design_heuristic",
        "signoff": False,
        "status": status,
        "params": params,
        "targets": {
            "three_w": {
                "trace_width_mm": three_w_target.trace_width_mm,
                "factor": three_w_target.factor,
                "min_center_to_center_mm": three_w_target.min_center_to_center_mm,
                "min_edge_to_edge_mm": three_w_target.min_edge_to_edge_mm,
                "source_note": three_w_target.source_note,
            },
            "twenty_h": {
                "dielectric_thickness_mm": twenty_h_target.dielectric_thickness_mm,
                "factor": twenty_h_target.factor,
                "min_setback_mm": twenty_h_target.min_setback_mm,
                "source_note": twenty_h_target.source_note,
            },
            "via_stitching": {
                "knee_frequency_ghz": stitching_target.knee_frequency_ghz,
                "er_eff": stitching_target.er_eff,
                "wavelength_mm": stitching_target.wavelength_mm,
                "factor": stitching_target.factor,
                "max_pitch_mm": stitching_target.max_pitch_mm,
                "source_note": stitching_target.source_note,
            },
        },
        "checks": list(findings),
        "summary": summarise(findings),
    }


# --------------------------------------------------------------------------- #
# Self-test + fault injection (OPT-IN, never on a normal run)
# --------------------------------------------------------------------------- #
def prove_tests_not_empty() -> bool:
    """FAULT INJECTION for all three rules: place a measurement just below /
    just above the target and prove the check really FAILs there while it
    PASSes exactly on the boundary. Without this, the checks could be
    vacuous and every run would look green."""
    outcomes = []

    target = three_w(0.2)
    exact = check_three_w([TracePairMeasurement(
        "A", "B", 0.2, 0.2, target.min_center_to_center_mm)])
    short = check_three_w([TracePairMeasurement(
        "A", "B", 0.2, 0.2, target.min_center_to_center_mm - 0.01)])
    outcomes.append(exact["status"] == "PASS" and short["status"] == FAIL)

    h_target = twenty_h_setback_mm(0.1)
    exact_h = check_twenty_h([PlanePairMeasurement(
        "F.Cu-In1.Cu", 0.1, h_target.min_setback_mm + 5.0, 5.0)])
    short_h = check_twenty_h([PlanePairMeasurement(
        "F.Cu-In1.Cu", 0.1, h_target.min_setback_mm - 0.01, 0.0)])
    outcomes.append(exact_h["status"] == "PASS" and short_h["status"] == FAIL)

    s_target = stitching_max_pitch_mm(5.0)
    exact_s = check_via_pitch([s_target.max_pitch_mm], 5.0)
    over_s = check_via_pitch([s_target.max_pitch_mm + 0.5], 5.0)
    outcomes.append(exact_s["status"] == "PASS" and over_s["status"] == FAIL)

    return all(outcomes)


def run_self_tests() -> list[str]:
    """Return a list of failure messages; an empty list means PASS.

    Never prints. The CLI routes the messages to stderr via ``diag``.
    """
    errors: list[str] = []

    # 1. 3W: centre-to-centre - width == edge-to-edge (definition coherence).
    target = three_w(0.25)
    if abs(target.min_edge_to_edge_mm
           - (target.min_center_to_center_mm - 0.25)) > 1e-9:
        errors.append("three_w: edge-to-edge / centre-to-centre definitions disagree")

    # 2. 3W: factor < 1.0 must be rejected.
    try:
        three_w(0.2, factor=0.5)
    except ValueError:
        pass
    else:
        errors.append("three_w: factor < 1.0 was not rejected")

    # 3. 20H: setback must scale linearly with the dielectric thickness.
    thin = twenty_h_setback_mm(0.1)
    thick = twenty_h_setback_mm(0.2)
    if thick.min_setback_mm != 2 * thin.min_setback_mm:
        errors.append("twenty_h_setback_mm: setback does not scale with dielectric")

    # 4. Stitching: a higher frequency must demand a TIGHTER via pitch.
    low_f = stitching_max_pitch_mm(1.0)
    high_f = stitching_max_pitch_mm(10.0)
    if not high_f.max_pitch_mm < low_f.max_pitch_mm:
        errors.append("stitching_max_pitch_mm: pitch does not shrink at higher f")

    # 5. Empty measurement sets must be NO_COVERAGE, never a silent PASS.
    for finding in (check_three_w([]), check_twenty_h([]), check_via_pitch([], 5.0)):
        if finding["status"] != NO_COVERAGE:
            errors.append(
                f"{finding['check']}: empty input counted as {finding['status']} "
                "(NO_COVERAGE expected)")

    # 6. overall_status precedence: FAIL > NEEDS_HUMAN > PASS.
    fail_f = Finding("x", FAIL, 1, [{"m": 1}], "")
    empty_f = Finding("y", NO_COVERAGE, 0, [], "")
    pass_f = Finding("z", "PASS", 1, [], "")
    if overall_status([fail_f, empty_f]) != "FAIL":
        errors.append("overall_status: FAIL precedence broken")
    if overall_status([empty_f, pass_f]) != "NEEDS_HUMAN":
        errors.append("overall_status: NEEDS_HUMAN precedence broken")
    if overall_status([pass_f]) != "PASS":
        errors.append("overall_status: PASS case broken")

    # 7. Fault injection across all three rules.
    if not prove_tests_not_empty():
        errors.append("fault injection did not break: one of the 3W/20H/stitching "
                      "boundary tests may be vacuous")

    return errors


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("problem", type=Path, nargs="?",
                        help="JSON problem file with the geometry/frequency "
                             "parameters and the measurements to audit "
                             "(required unless --self-test)")
    parser.add_argument("--trace-width-mm", type=float, default=None,
                        help="override trace_width_mm from the problem file")
    parser.add_argument("--dielectric-thickness-mm", type=float, default=None,
                        help="override dielectric_thickness_mm")
    parser.add_argument("--knee-frequency-ghz", type=float, default=None,
                        help="override knee_frequency_ghz (f_knee, NOT the clock)")
    parser.add_argument("--er-eff", type=float, default=None,
                        help="override er_eff")
    parser.add_argument("--json", type=Path,
                        help="also WRITE the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the built-in self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --self-test is OPT-IN. In the source it ran on EVERY invocation and
    # printed "ÖZ-TEST PASS" to stdout, which both corrupted the machine
    # readable output and made a no-input run look like a success.
    if args.self_test:
        errors = run_self_tests()
        for message in errors:
            diag(f"SELF-TEST FAIL: {message}")
        return EXIT_FAIL if errors else EXIT_OK

    if args.problem is None:
        # Missing required input is an ERROR (exit 2 via argparse), never a
        # hint printed on stdout followed by exit 0.
        parser.error("a JSON problem file is required; pass it positionally "
                     "(e.g. `emc_rules.py problem.json`) or use --self-test")

    problem = load_problem(args.problem)
    params = resolve_params(problem, args)
    report = build_report(problem, params, str(args.problem))

    diag(f"status={report['status']} summary={report['summary']}")
    write_report(report, args.json)
    emit(report)
    return exit_code_for(report["status"])


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
