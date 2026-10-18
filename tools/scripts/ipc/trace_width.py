#!/usr/bin/env python3
"""IPC-2152-informed minimum trace width for a given current.

WHAT THIS IS
------------
The IPC-2221 closed form is used as the CORE model:

    A_mils2 = (I / (k * dT^0.44)) ^ (1/0.725)      k = 0.048 outer / 0.024 inner

on top of which an explicit, tunable inner-layer derating factor is applied.

WHAT THIS IS NOT — read before trusting a number
------------------------------------------------
Real IPC-2152 (2009) replaces IPC-2221's single closed form with a FAMILY OF
CURVES produced by 2-D thermal modelling (board thickness, adjacent copper
pour, airflow, inner/outer differences). Those curves are published as
charts/tables inside the paid standard and cannot be digitised here. This
tool does NOT stand in for them. It:

  1. uses the IPC-2221 closed form as its core (imported from
     ``common.si_models`` — the bundle's single source for that formula, so
     the k/b/c constants cannot silently fork), and
  2. applies on top an observation cited across many independent secondary
     sources (vendor app notes, PCB calculator documentation): the IPC-2152
     curves demand MORE margin on inner layers than IPC-2221's simple
     k-halving assumption. That is exposed as ``--derating`` (default 1.25),
     a deliberately conservative starting point and NOT an exact IPC-2152
     constant.

The output is therefore a FIRST ESTIMATE / screening number. For
current-critical or safety-critical work (UL/IEC compliance), cross-check
against the purchased IPC-2152 curve, a vendor calculator such as the Saturn
PCB Toolkit, or a real thermal measurement, BEFORE sign-off.

CLI
---
    trace_width.py CURRENT_A [--delta-t 10] [--copper-oz 1.0]
                             [--derating 1.25] [--json OUT] [--self-test]

    exit 0  width computed
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
from common.si_models import (  # noqa: E402
    MODEL_CLASS,
    SIGNOFF,
    ipc2221_min_width_mm,
)
from common.units import MM_TO_MIL, OZ_TO_MM  # noqa: E402
from scripts.ipc.report import CALC_SCHEMA, calc_report  # noqa: E402

SCHEMA_VERSION = CALC_SCHEMA
TOOL_NAME = "trace_width"
STANDARD = "IPC-2152 (IPC-2221 core + inner-layer derating)"

# Conservative starting point reflecting the widely-cited observation that
# IPC-2152 wants more inner-layer margin than IPC-2221's k-halving. NOT an
# exact IPC-2152 constant — see the module docstring.
DEFAULT_INNER_DERATING = 1.25

LIMITS = (
    "IPC-2221 closed form is the core model; the full IPC-2152 curve family "
    "(board thickness, adjacent copper, airflow) is NOT modelled.",
    "The inner-layer derating factor is a conservative secondary-source "
    "observation, not a published IPC-2152 constant.",
    "Screening estimate only. Cross-check against the purchased IPC-2152 "
    "curve or a thermal measurement before production sign-off.",
)


class LayerType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


@dataclass(frozen=True)
class TraceWidthResult:
    """One width computation.

    ``inner_derating`` is embedded in the RESULT, not just accepted as a
    parameter, so that any report or ``.kicad_dru`` comment derived from it
    always shows which factor was actually applied.
    """

    width_mm: float
    cross_section_mils2: float
    layer_type: str
    current_a: float
    delta_t_c: float
    copper_oz: float
    inner_derating: float


def min_trace_width(current_a: float, delta_t_c: float, layer_type: LayerType,
                    copper_oz: float = 1.0,
                    inner_derating: float = DEFAULT_INNER_DERATING,
                    ) -> TraceWidthResult:
    """Minimum trace width in mm for the given current / rise / layer.

    Input validation is DELIBERATELY stricter than the underlying
    ``common.si_models`` helper: this function's output is fed straight into
    a DRC rule, and a zero-width rule silently means "accept anything".
    """
    if current_a <= 0:
        raise ValueError(f"current_a must be positive, got {current_a!r}")
    if delta_t_c <= 0:
        raise ValueError(f"delta_t_c must be positive, got {delta_t_c!r}")
    if copper_oz <= 0:
        raise ValueError(f"copper_oz must be positive, got {copper_oz!r}")
    if inner_derating < 1.0:
        raise ValueError(
            "inner_derating must be >= 1.0 (an inner layer NEVER needs less "
            f"margin than an outer one), got {inner_derating!r}")

    external = layer_type is LayerType.EXTERNAL
    base_mm = ipc2221_min_width_mm(
        current_a, delta_t_c, copper_oz,
        "microstrip" if external else "stripline")

    applied = 1.0 if external else inner_derating
    width_mm = base_mm * applied

    # Recover the cross-section for the report, in the IPC formula's own unit.
    thickness_mils = copper_oz * OZ_TO_MM * MM_TO_MIL
    cross_section_mils2 = (width_mm * MM_TO_MIL) * thickness_mils

    return TraceWidthResult(
        width_mm=round(width_mm, 4),
        cross_section_mils2=round(cross_section_mils2, 2),
        layer_type=layer_type.value,
        current_a=current_a,
        delta_t_c=delta_t_c,
        copper_oz=copper_oz,
        inner_derating=applied,
    )


def compare_layers(current_a: float, delta_t_c: float, copper_oz: float = 1.0,
                   inner_derating: float = DEFAULT_INNER_DERATING,
                   ) -> dict[str, TraceWidthResult]:
    """Both layer types at once — what a design-rules table or a pair of
    ``.kicad_dru`` netclass rules actually needs."""
    return {
        "external": min_trace_width(current_a, delta_t_c, LayerType.EXTERNAL,
                                    copper_oz, inner_derating),
        "internal": min_trace_width(current_a, delta_t_c, LayerType.INTERNAL,
                                    copper_oz, inner_derating),
    }


# --------------------------------------------------------------------------- #
# Self-test + fault injection
# --------------------------------------------------------------------------- #
def _prove_test_not_empty() -> bool:
    """FAULT INJECTION: force the derating below 1.0 (0.5), which would make
    an inner layer come out NARROWER than an outer one — physically wrong,
    since an inner layer sheds heat less easily and always needs an equal or
    wider trace. A ValueError is required. If the function accepts it
    silently, the input validation is empty and every test above it is
    worthless."""
    try:
        min_trace_width(5.0, 10.0, LayerType.INTERNAL, inner_derating=0.5)
    except ValueError:
        return True
    return False


def run_self_tests() -> list[str]:
    failures: list[str] = []

    results = compare_layers(10.0, 10.0)
    if results["internal"].width_mm < results["external"].width_mm:
        failures.append(
            f"inner layer ({results['internal'].width_mm} mm) came out narrower "
            f"than outer ({results['external'].width_mm} mm) — physically wrong")

    low = min_trace_width(1.0, 10.0, LayerType.EXTERNAL)
    high = min_trace_width(5.0, 10.0, LayerType.EXTERNAL)
    if not high.width_mm > low.width_mm:
        failures.append(
            f"width did not grow with current: {low.width_mm} -> {high.width_mm}")

    thin = min_trace_width(5.0, 10.0, LayerType.EXTERNAL, copper_oz=1.0)
    thick = min_trace_width(5.0, 10.0, LayerType.EXTERNAL, copper_oz=2.0)
    if not thick.width_mm < thin.width_mm:
        failures.append(
            f"width did not shrink with thicker copper: "
            f"{thin.width_mm} -> {thick.width_mm}")

    for bad in ({"current_a": 0.0}, {"current_a": -1.0},
                {"delta_t_c": 0.0}, {"copper_oz": -1.0}):
        kwargs = {"current_a": 1.0, "delta_t_c": 10.0,
                  "layer_type": LayerType.EXTERNAL, "copper_oz": 1.0}
        kwargs.update(bad)
        try:
            min_trace_width(**kwargs)
        except ValueError:
            pass
        else:
            failures.append(f"invalid input {bad} was not rejected")

    if not _prove_test_not_empty():
        failures.append(
            "fault injection did not fire: the inner/outer comparison may be empty")

    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("current", type=float, nargs="?",
                        help="current the trace must carry, A "
                             "(required unless --self-test)")
    parser.add_argument("--delta-t", type=float, default=10.0,
                        help="permitted temperature rise, degC (default 10)")
    parser.add_argument("--copper-oz", type=float, default=1.0,
                        help="copper weight, oz (default 1.0)")
    parser.add_argument("--derating", type=float, default=DEFAULT_INNER_DERATING,
                        help="inner-layer derating factor, >= 1.0 "
                             f"(default {DEFAULT_INNER_DERATING})")
    parser.add_argument("--json", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --self-test is OPT-IN. The archive ran it on EVERY invocation and
    # printed "SELF-TEST PASS" to stdout, which both wasted work and
    # corrupted the machine-readable channel.
    if args.self_test:
        failures = run_self_tests()
        for failure in failures:
            diag(f"SELF-TEST FAIL: {failure}")
        if failures:
            return 2
        diag("SELF-TEST PASS")
        return 0

    if args.current is None:
        # The archive printed a hint here and returned 0 — reporting success
        # while doing no work. Missing required input is exit 2.
        parser.error("current is required; pass it positionally "
                     "(e.g. `trace_width.py 2.5`) or use --self-test")

    try:
        results = compare_layers(args.current, args.delta_t,
                                 args.copper_oz, args.derating)
    except ValueError as exc:
        parser.error(str(exc))

    report = calc_report(
        TOOL_NAME, STANDARD,
        inputs={
            "current_a": args.current,
            "delta_t_c": args.delta_t,
            "copper_oz": args.copper_oz,
            "inner_derating": args.derating,
        },
        results={name: asdict(value) for name, value in results.items()},
        limits=LIMITS,
        model_class=MODEL_CLASS,
        signoff=SIGNOFF,
    )
    write_report(report, args.json)
    emit(report)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
