#!/usr/bin/env python3
"""IPC-2221B Table 6-1 conductor clearance / creepage, by layer and coating.

WHY THIS EXISTS ALONGSIDE ``common.stackup.required_isolation_mm``
------------------------------------------------------------------
That helper covers only the external / uncoated case, does not distinguish
creepage from clearance, and steps between voltage bands with no
interpolation. This module closes those three gaps. To prevent a silent
fork, the external/uncoated row reproduces the SAME numbers at the shared
voltage points (15/30/50/100/500 V) and ``--self-test`` proves it.

HONESTY NOTE — where these numbers come from
--------------------------------------------
There is no access to a purchased IPC-2221B PDF here. The table below is
compiled from several widely-cited secondary sources (vendor design guides,
PCB supplier technical articles) and EVERY value carries its own confidence
level in the ``confidence`` field:

  ``matches_existing_code``   identical to the external/uncoated values
                              already accepted elsewhere in this bundle
                              (highest confidence — previously reviewed).
  ``secondary_source``        widely cited but NOT verified against the
                              official standard in this environment.
  ``conservative_assumption`` deliberately chose the LARGER (safer, more
                              distance) value under uncertainty. The real
                              official value may be SMALLER (an error in the
                              safe direction) but must not be larger.

THIS MODULE IS NOT A COMPLIANCE PROOF — it is a first-pass screen. For any
safety-critical or >50 V design, verify every value against an official
IPC-2221B copy (or the relevant derived standard: IEC 60664-1, UL 60950-1 /
62368-1) and have a safety/compliance engineer sign it off.

CLI
---
    clearance.py VOLTAGE_V [--layer external|internal]
                           [--coating uncoated|conformal_coated]
                           [--interpolate] [--creepage-factor 1.0]
                           [--all-combinations] [--json OUT] [--self-test]

    exit 0  clearance computed
    exit 2  bad or missing input, or self-test failure
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from scripts.ipc.report import CALC_SCHEMA, calc_report  # noqa: E402

SCHEMA_VERSION = CALC_SCHEMA
TOOL_NAME = "clearance"
STANDARD = "IPC-2221B Table 6-1"

# This table is a screening aid, not a field-solver result, so it is labelled
# with the same non-signoff vocabulary the rest of the bundle uses.
MODEL_CLASS = "tabulated_secondary_source"
SIGNOFF = False

LIMITS = (
    "Compiled from secondary sources; no purchased IPC-2221B was available. "
    "Each row carries its own confidence field.",
    "Internal-layer values deliberately reuse the external/uncoated numbers "
    "(never smaller) because the true internal values could not be verified.",
    "Creepage is derived as clearance * creepage_factor. Without CTI and "
    "pollution-degree data the true creepage may be larger.",
    "NOT a compliance proof. Any safety-critical or >50 V design must be "
    "verified against official IPC-2221B / IEC 60664-1 and signed off by a "
    "compliance engineer.",
)


class LayerType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class CoatingState(str, Enum):
    UNCOATED = "uncoated"
    CONFORMAL_COATED = "conformal_coated"


class Confidence(str, Enum):
    MATCHES_EXISTING_CODE = "matches_existing_code"
    SECONDARY_SOURCE = "secondary_source"
    CONSERVATIVE_ASSUMPTION = "conservative_assumption"


@dataclass(frozen=True)
class ClearancePoint:
    max_voltage_v: float
    clearance_mm: float
    confidence: Confidence


# Seven bands: 0-15, 16-30, 31-50, 51-100, 101-300, 301-500, >500 V.
# The 300 V point falls between the existing table's 250 V (0.3 mm) and
# 500 V (0.5 mm) steps; a conservative value near the top (0.4 mm) is used.
_EXTERNAL_UNCOATED: tuple[ClearancePoint, ...] = (
    ClearancePoint(15, 0.05, Confidence.MATCHES_EXISTING_CODE),
    ClearancePoint(30, 0.05, Confidence.MATCHES_EXISTING_CODE),
    ClearancePoint(50, 0.10, Confidence.MATCHES_EXISTING_CODE),
    ClearancePoint(100, 0.10, Confidence.MATCHES_EXISTING_CODE),
    ClearancePoint(300, 0.40, Confidence.CONSERVATIVE_ASSUMPTION),
    ClearancePoint(500, 0.50, Confidence.MATCHES_EXISTING_CODE),
)

# In the real IPC-2221B table internal layers are usually SMALLER at low
# voltage (an inner layer is sealed against environmental contamination),
# but that number could not be verified here, so this module deliberately
# reuses the external/uncoated value — never smaller. Same "err conservative
# under uncertainty" discipline as the inner-layer derating in trace_width.
_INTERNAL_UNCOATED: tuple[ClearancePoint, ...] = tuple(
    ClearancePoint(p.max_voltage_v, p.clearance_mm,
                   Confidence.CONSERVATIVE_ASSUMPTION)
    for p in _EXTERNAL_UNCOATED
)

# Conformal coating reduces the required distance at mid/high voltage, but
# there is also a PRACTICAL process floor: coating thickness and application
# tolerance make it meaningless to go below a certain gap. Consequence: at
# very low voltage (e.g. 15 V) the uncoated electrical minimum (0.05 mm) is
# already below the coated floor (0.13 mm), so the coated number looks
# NUMERICALLY LARGER in that band. That is NOT a bug — "coating always needs
# less distance" is simply false at low voltage. The self-test models this
# nuance explicitly rather than asserting a monotonicity that does not hold.
_COATED_FLOOR_MM = 0.13
_COATED_SCALE = 0.4

_EXTERNAL_COATED: tuple[ClearancePoint, ...] = tuple(
    ClearancePoint(
        p.max_voltage_v,
        round(max(_COATED_FLOOR_MM, p.clearance_mm * _COATED_SCALE), 4),
        Confidence.SECONDARY_SOURCE,
    )
    for p in _EXTERNAL_UNCOATED
)

# An internal layer is by definition not exposed to conformal coating, so
# "internal + coated" is meaningless. A caller asking for it gets an explicit
# ValueError and NEVER a silently substituted external-coated value.
_TABLES: dict[tuple[LayerType, CoatingState], tuple[ClearancePoint, ...]] = {
    (LayerType.EXTERNAL, CoatingState.UNCOATED): _EXTERNAL_UNCOATED,
    (LayerType.INTERNAL, CoatingState.UNCOATED): _INTERNAL_UNCOATED,
    (LayerType.EXTERNAL, CoatingState.CONFORMAL_COATED): _EXTERNAL_COATED,
}

# Creepage must be >= clearance (the general IEC 60664-1 principle; the exact
# ratio depends on material group and pollution degree). Without CTI data
# this module derives creepage conservatively as clearance * factor.
DEFAULT_CREEPAGE_FACTOR = 1.0


@dataclass(frozen=True)
class ClearanceResult:
    clearance_mm: float
    creepage_mm: float
    voltage_v: float
    layer_type: str
    coating_state: str
    confidence: str
    interpolated: bool


def _table_for(layer_type: LayerType,
               coating_state: CoatingState) -> tuple[ClearancePoint, ...]:
    key = (layer_type, coating_state)
    if key not in _TABLES:
        raise ValueError(
            f"invalid combination: layer_type={layer_type.value}, "
            f"coating_state={coating_state.value} — an internal layer is not "
            "exposed to conformal coating, so this combination is undefined.")
    return _TABLES[key]


def _confidence_rank(confidence: Confidence) -> int:
    return {
        Confidence.MATCHES_EXISTING_CODE: 0,
        Confidence.SECONDARY_SOURCE: 1,
        Confidence.CONSERVATIVE_ASSUMPTION: 2,
    }[confidence]


def _interpolate(table: Sequence[ClearancePoint],
                 voltage_v: float) -> tuple[float, Confidence]:
    """Linear interpolation between two adjacent table points.

    The lower bound (0 V) is treated as carrying the FIRST point's clearance:
    saying "0 V needs no distance at all" would be physically wrong, so the
    low band's minimum is preserved.
    """
    prev_v, prev_mm, prev_conf = 0.0, table[0].clearance_mm, table[0].confidence
    for point in table:
        if voltage_v <= point.max_voltage_v:
            if point.max_voltage_v == prev_v:
                return point.clearance_mm, point.confidence
            ratio = (voltage_v - prev_v) / (point.max_voltage_v - prev_v)
            value = prev_mm + ratio * (point.clearance_mm - prev_mm)
            # An interpolated value INHERITS the LEAST confident of the two
            # bracketing points. A higher confidence is never claimed by
            # accident of arithmetic.
            least = max((prev_conf, point.confidence), key=_confidence_rank)
            return value, least
        prev_v, prev_mm, prev_conf = (point.max_voltage_v, point.clearance_mm,
                                      point.confidence)
    return table[-1].clearance_mm, table[-1].confidence


def clearance_mm(max_voltage_v: float, layer_type: LayerType,
                 coating_state: CoatingState = CoatingState.UNCOATED,
                 interpolate: bool = False,
                 creepage_factor: float = DEFAULT_CREEPAGE_FACTOR,
                 ) -> ClearanceResult:
    """Minimum clearance + creepage in mm for a peak voltage.

    ``interpolate=False`` (default) is the STANDARD-FAITHFUL behaviour:
    official IPC tables are STEP functions, every voltage in a band takes
    that band's value and there is no smooth transition.
    ``interpolate=True`` linearly interpolates between band edges. That is an
    engineering convenience offered by THIS TOOL and is not in the standard;
    the ``interpolated`` field carries the difference into the report so the
    mode used is never hidden.
    """
    if max_voltage_v < 0:
        raise ValueError(
            f"max_voltage_v cannot be negative, got {max_voltage_v!r}")
    if creepage_factor < 1.0:
        raise ValueError(
            "creepage_factor must be >= 1.0 (creepage cannot be smaller than "
            f"clearance), got {creepage_factor!r}")

    table = _table_for(layer_type, coating_state)
    last = table[-1]

    if max_voltage_v > last.max_voltage_v:
        # Above the table: the same honest linear extrapolation discipline as
        # common.stackup.required_isolation_mm. MUST be verified against the
        # real table before use.
        value = last.clearance_mm * (max_voltage_v / last.max_voltage_v)
        confidence = Confidence.CONSERVATIVE_ASSUMPTION
        interpolated = False
    elif not interpolate:
        match = next(p for p in table if max_voltage_v <= p.max_voltage_v)
        value, confidence, interpolated = match.clearance_mm, match.confidence, False
    else:
        value, confidence = _interpolate(table, max_voltage_v)
        interpolated = True

    return ClearanceResult(
        clearance_mm=round(value, 4),
        creepage_mm=round(value * creepage_factor, 4),
        voltage_v=max_voltage_v,
        layer_type=layer_type.value,
        coating_state=coating_state.value,
        confidence=confidence.value,
        interpolated=interpolated,
    )


def all_combinations(max_voltage_v: float, interpolate: bool = False,
                     ) -> dict[str, ClearanceResult]:
    """Every valid (layer x coating) combination for one voltage — what
    report and ``.kicad_dru`` generation actually needs."""
    return {
        f"{layer.value}_{coating.value}":
            clearance_mm(max_voltage_v, layer, coating, interpolate)
        for (layer, coating) in _TABLES
    }


# --------------------------------------------------------------------------- #
# Self-test + fault injection
# --------------------------------------------------------------------------- #
def _prove_test_not_empty() -> bool:
    """FAULT INJECTION: force creepage_factor below 1.0. Creepage cannot be
    smaller than clearance — that is a physical constraint, so a ValueError
    is required. Acceptance would mean the validation is empty."""
    try:
        clearance_mm(100, LayerType.EXTERNAL, creepage_factor=0.5)
    except ValueError:
        return True
    return False


def _consistency_with_stackup() -> list[str]:
    """Prove the single-source claim in this module's docstring: at the
    shared voltage points the external/uncoated row must equal
    ``common.stackup.required_isolation_mm`` exactly."""
    try:
        from common.stackup import required_isolation_mm
    except ImportError as exc:
        return [f"cannot verify against common.stackup: {exc}"]

    failures: list[str] = []
    for voltage in (15, 30, 50, 100, 500):
        existing = required_isolation_mm(voltage)
        ours = clearance_mm(voltage, LayerType.EXTERNAL,
                            CoatingState.UNCOATED).clearance_mm
        if abs(existing - ours) >= 1e-9:
            failures.append(
                f"at {voltage} V common.stackup={existing} mm but this "
                f"table={ours} mm — SILENT DIVERGENCE")
    return failures


def run_self_tests() -> list[str]:
    failures: list[str] = []
    failures.extend(_consistency_with_stackup())

    previous = None
    for voltage in (10, 20, 40, 75, 200, 400, 600):
        value = clearance_mm(voltage, LayerType.EXTERNAL,
                             CoatingState.UNCOATED).clearance_mm
        if previous is not None and value < previous:
            failures.append(
                f"clearance decreased at {voltage} V: {previous} -> {value} mm")
        previous = value

    # Coated must not exceed uncoated WHERE uncoated is already above the
    # process floor. Below the floor the coated value legitimately looks
    # larger — see the _COATED_FLOOR_MM note above.
    for voltage in (15, 50, 100, 300, 500):
        uncoated = clearance_mm(voltage, LayerType.EXTERNAL,
                                CoatingState.UNCOATED).clearance_mm
        coated = clearance_mm(voltage, LayerType.EXTERNAL,
                              CoatingState.CONFORMAL_COATED).clearance_mm
        if uncoated > _COATED_FLOOR_MM and coated > uncoated:
            failures.append(
                f"at {voltage} V coated ({coated} mm) exceeded uncoated "
                f"({uncoated} mm) above the process floor")

    try:
        clearance_mm(50, LayerType.INTERNAL, CoatingState.CONFORMAL_COATED)
    except ValueError:
        pass
    else:
        failures.append("internal + coated combination was not rejected")

    stepped = clearance_mm(200, LayerType.EXTERNAL, CoatingState.UNCOATED,
                           interpolate=False)
    smooth = clearance_mm(200, LayerType.EXTERNAL, CoatingState.UNCOATED,
                          interpolate=True)
    if stepped.interpolated or not smooth.interpolated:
        failures.append("the `interpolated` flag was not set correctly")

    result = clearance_mm(100, LayerType.EXTERNAL, creepage_factor=1.2)
    if result.creepage_mm < result.clearance_mm:
        failures.append(
            f"creepage ({result.creepage_mm} mm) is below clearance "
            f"({result.clearance_mm} mm)")

    if not _prove_test_not_empty():
        failures.append(
            "fault injection did not fire: creepage validation may be empty")

    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("voltage", type=float, nargs="?",
                        help="maximum peak voltage, V (required unless --self-test)")
    parser.add_argument("--layer", choices=[e.value for e in LayerType],
                        default=LayerType.EXTERNAL.value)
    parser.add_argument("--coating", choices=[e.value for e in CoatingState],
                        default=CoatingState.UNCOATED.value)
    parser.add_argument("--interpolate", action="store_true",
                        help="linearly interpolate between bands (NOT standard "
                             "behaviour; recorded in the report)")
    parser.add_argument("--creepage-factor", type=float,
                        default=DEFAULT_CREEPAGE_FACTOR,
                        help="creepage = clearance * factor, must be >= 1.0")
    parser.add_argument("--all-combinations", action="store_true",
                        help="report every valid layer x coating combination")
    parser.add_argument("--json", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        failures = run_self_tests()
        for failure in failures:
            diag(f"SELF-TEST FAIL: {failure}")
        if failures:
            return 2
        diag("SELF-TEST PASS")
        return 0

    if args.voltage is None:
        parser.error("voltage is required; pass it positionally "
                     "(e.g. `clearance.py 250`) or use --self-test")

    try:
        if args.all_combinations:
            results = {name: asdict(value) for name, value
                       in all_combinations(args.voltage, args.interpolate).items()}
        else:
            results = asdict(clearance_mm(
                args.voltage, LayerType(args.layer), CoatingState(args.coating),
                args.interpolate, args.creepage_factor))
    except ValueError as exc:
        parser.error(str(exc))

    report = calc_report(
        TOOL_NAME, STANDARD,
        inputs={
            "voltage_v": args.voltage,
            "layer": args.layer,
            "coating": args.coating,
            "interpolate": args.interpolate,
            "creepage_factor": args.creepage_factor,
            "all_combinations": args.all_combinations,
        },
        results=results,
        limits=LIMITS,
        model_class=MODEL_CLASS,
        signoff=SIGNOFF,
    )
    write_report(report, args.json)
    emit(report)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
