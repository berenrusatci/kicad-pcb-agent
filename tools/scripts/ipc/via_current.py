#!/usr/bin/env python3
"""IPC-2221-based via drill / pad diameter for a required current.

WHY THIS IS A SEPARATE LAYER ON TOP OF THE TRACE-WIDTH FORMULA
---------------------------------------------------------------
The IPC-2221 current->cross-section formula assumes a FLAT trace: a
rectangular section, width x thickness. A via's current-carrying section is
a different geometry — the hole is usually hollow (or filled with a
non-conductor), and the current is carried by the CYLINDRICAL plating wall:

    Area ~= pi * hole_diameter * plating_thickness     (thin-wall approximation)

This module does NOT re-derive or copy the IPC-2221 formula. It calls
``common.si_models.ipc2221_min_width_mm`` (the bundle's single source for
the k/b/c constants), inverts it to recover the required cross-section in
mils^2, then solves that area for the hole diameter under the cylindrical
geometry. The constants therefore cannot silently fork between the two
modules. Fab limits come from ``common.stackup.fab_profiles()`` for the same
reason — they are not hardcoded here.

HONESTY NOTE
------------
This is an ADAPTATION of the simplified IPC-2221 current/area formula to via
geometry. A via-specific official curve set (modelling hole fill material,
plating thickness tolerance, thermal-via versus signal-via behaviour) is NOT
implemented. For critical or high-current via arrays (power vias, thermal
vias), treat this as a FIRST ESTIMATE and verify against the fab's own DFM
tool or a field solver before production.

MATHEMATICAL NOTE — ``via_count`` is ALWAYS 1 in this model
------------------------------------------------------------
The "hole smaller than the fab minimum" branch computes
``ceil(required_area / single_via_area)``, which is faithful to the formula
as specified. But at a fixed plating thickness, Area = pi * D * t is LINEAR
in diameter (not D^2). So whenever the computed diameter is already below
the minimum, the required area is — by that same linearity — always less
than the area a single minimum-diameter via provides. One via raised to the
manufacturable floor is ALWAYS sufficient, and ``via_count`` can never
exceed 1 in this branch.

A genuine multi-via (N>1) stitching scenario CANNOT be produced by this
formula. It would need either a model where via area grows quadratically
with diameter, or a different trigger entirely (e.g. "diameter is above the
minimum but a single via exceeds an aspect-ratio or thermal limit"). Rather
than silently "fixing" the specified formula, this module documents the
mathematical boundary here and pins it in the test suite.

CLI
---
    via_current.py CURRENT_A --fab PROFILE [--delta-t 10] [--plating-oz 1.0]
                             [--json OUT] [--self-test]

    exit 0  diameters computed
    exit 2  bad or missing input, or self-test failure
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.si_models import MODEL_CLASS, SIGNOFF, ipc2221_min_width_mm  # noqa: E402
from common.stackup import FabProfile, fab_profiles  # noqa: E402
from common.units import MM_TO_MIL, OZ_TO_MM  # noqa: E402
from scripts.ipc.report import CALC_SCHEMA, calc_report  # noqa: E402

SCHEMA_VERSION = CALC_SCHEMA
TOOL_NAME = "via_current"
STANDARD = "IPC-2221 (adapted to cylindrical via plating)"

LIMITS = (
    "Adaptation of the IPC-2221 trace formula to via geometry; no "
    "via-specific official curve set is implemented.",
    "Thin-wall approximation: current is assumed carried entirely by the "
    "cylindrical plating (Area = pi * D * t). Hole fill and plating "
    "tolerance are not modelled.",
    "via_count can never exceed 1 in this model — see the module docstring. "
    "A real multi-via stitching decision needs a different trigger.",
    "Screening estimate only; verify against the fab's DFM tool before "
    "production for power or thermal vias.",
)


def _oz_to_mils(copper_oz: float) -> float:
    """Copper weight -> plating thickness in mils, via the bundle's shared
    constants so this file cannot fork from common.units."""
    return copper_oz * OZ_TO_MM * MM_TO_MIL


def required_area_mils2(current_a: float, delta_t_c: float,
                        copper_oz: float) -> float:
    """Recover the required cross-section by CALLING the shared IPC-2221
    helper and inverting it — the formula is never copied.

    A via carries current on the outer surface too, so the external-layer
    k constant is always used.
    """
    if current_a <= 0:
        raise ValueError(f"current_a must be positive, got {current_a!r}")
    if delta_t_c <= 0:
        raise ValueError(f"delta_t_c must be positive, got {delta_t_c!r}")
    if copper_oz <= 0:
        raise ValueError(f"plating_oz must be positive, got {copper_oz!r}")

    width_mm = ipc2221_min_width_mm(current_a, delta_t_c, copper_oz, "microstrip")
    # ipc2221_min_width_mm: width_mils = area_mils2 / thickness_mils
    return (width_mm * MM_TO_MIL) * _oz_to_mils(copper_oz)


def via_hole_diameter_mm(current_a: float, delta_t_c: float = 10.0,
                         plating_oz: float = 1.0) -> float:
    """Solve the required area for the hole diameter under cylindrical
    plating geometry:

        hole_diameter_mils = area_mils2 / (pi * plating_thickness_mils)
    """
    area = required_area_mils2(current_a, delta_t_c, plating_oz)
    diameter_mils = area / (math.pi * _oz_to_mils(plating_oz))
    return diameter_mils / MM_TO_MIL


def pad_diameter_mm(hole_diameter_mm: float, min_annular_ring_mm: float) -> float:
    """pad = hole + 2 * annular ring (one ring on each side)."""
    if hole_diameter_mm <= 0:
        raise ValueError(
            f"hole_diameter_mm must be positive, got {hole_diameter_mm!r}")
    if min_annular_ring_mm < 0:
        raise ValueError(
            f"min_annular_ring_mm cannot be negative, got {min_annular_ring_mm!r}")
    return hole_diameter_mm + 2 * min_annular_ring_mm


@dataclass(frozen=True)
class ViaCurrentResult:
    current_a: float
    delta_t_c: float
    plating_oz: float
    fab_profile: str

    computed_hole_mm: float
    computed_hole_mils: float
    recommended_hole_mm: float
    recommended_hole_mils: float
    pad_mm: float
    pad_mils: float

    below_manufacturable_floor: bool
    stitching_suggested: bool
    via_count: int
    detail: str


def recommend_via(current_a: float, profile: FabProfile,
                  delta_t_c: float = 10.0,
                  plating_oz: float = 1.0) -> ViaCurrentResult:
    """Recommend hole and pad diameters, raising the hole to the fab's
    manufacturable floor when the computed value falls below it."""
    computed_mm = via_hole_diameter_mm(current_a, delta_t_c, plating_oz)
    below = computed_mm < profile.min_hole_mm

    if not below:
        recommended_mm = computed_mm
        via_count = 1
        stitching = False
        detail = (
            f"Hesaplanan delik çapı ({computed_mm:.4f} mm) {profile.name} "
            f"üretilebilirlik sınırının ({profile.min_hole_mm} mm) üstünde — "
            "tekil via yeterli.")
    else:
        recommended_mm = profile.min_hole_mm
        stitching = True
        area = required_area_mils2(current_a, delta_t_c, plating_oz)
        single_via_area = (math.pi * (profile.min_hole_mm * MM_TO_MIL)
                           * _oz_to_mils(plating_oz))
        via_count = max(1, math.ceil(area / single_via_area))
        detail = (
            f"Hesaplanan delik çapı ({computed_mm:.4f} mm) {profile.name} "
            f"üretilebilirlik sınırının ({profile.min_hole_mm} mm) ALTINDA — "
            "delik çapı üretilebilirlik tabanına yükseltildi. Bu modelde "
            f"via_count her zaman 1'dir (hesaplanan: {via_count}); "
            "bkz. modül docstring'indeki matematiksel not.")

    pad_mm = pad_diameter_mm(recommended_mm, profile.min_annular_ring_mm)

    return ViaCurrentResult(
        current_a=current_a,
        delta_t_c=delta_t_c,
        plating_oz=plating_oz,
        fab_profile=profile.name,
        computed_hole_mm=round(computed_mm, 4),
        computed_hole_mils=round(computed_mm * MM_TO_MIL, 2),
        recommended_hole_mm=round(recommended_mm, 4),
        recommended_hole_mils=round(recommended_mm * MM_TO_MIL, 2),
        pad_mm=round(pad_mm, 4),
        pad_mils=round(pad_mm * MM_TO_MIL, 2),
        below_manufacturable_floor=below,
        stitching_suggested=stitching,
        via_count=via_count,
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# Self-test + fault injection
# --------------------------------------------------------------------------- #
def _prove_test_not_empty() -> bool:
    """FAULT INJECTION: a zero current must be rejected. If it silently
    produced a 0 mm hole, every downstream DRC rule built from this output
    would mean "accept anything"."""
    try:
        via_hole_diameter_mm(0.0)
    except ValueError:
        return True
    return False


def run_self_tests() -> list[str]:
    failures: list[str] = []
    profiles = fab_profiles()
    if not profiles:
        return ["common.stackup.fab_profiles() returned nothing"]
    profile = next(iter(profiles.values()))

    low = via_hole_diameter_mm(1.0)
    high = via_hole_diameter_mm(5.0)
    if not high > low:
        failures.append(
            f"hole diameter did not grow with current: {low} -> {high}")

    thin = via_hole_diameter_mm(5.0, plating_oz=1.0)
    thick = via_hole_diameter_mm(5.0, plating_oz=2.0)
    if not thick < thin:
        failures.append(
            f"hole diameter did not shrink with thicker plating: {thin} -> {thick}")

    pad = pad_diameter_mm(0.3, 0.13)
    if abs(pad - 0.56) > 1e-9:
        failures.append(f"pad = hole + 2*ring failed: expected 0.56, got {pad}")

    # Pin the documented mathematical boundary: the floor branch can never
    # ask for more than one via.
    tiny = recommend_via(0.05, profile)
    if not tiny.below_manufacturable_floor:
        failures.append("0.05 A did not land below the manufacturable floor")
    elif tiny.via_count != 1:
        failures.append(
            f"via_count must always be 1 in this model, got {tiny.via_count} "
            "— the documented linearity argument has been broken")

    for bad in ({"current_a": 0.0}, {"current_a": -1.0}, {"delta_t_c": 0.0},
                {"copper_oz": 0.0}):
        kwargs = {"current_a": 1.0, "delta_t_c": 10.0, "copper_oz": 1.0}
        kwargs.update(bad)
        try:
            required_area_mils2(**kwargs)
        except ValueError:
            pass
        else:
            failures.append(f"invalid input {bad} was not rejected")

    if not _prove_test_not_empty():
        failures.append(
            "fault injection did not fire: input validation may be empty")

    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("current", type=float, nargs="?",
                        help="current the via must carry, A "
                             "(required unless --self-test)")
    parser.add_argument("--fab", help="fab DFM profile key "
                                      "(see common.stackup.fab_profiles)")
    parser.add_argument("--delta-t", type=float, default=10.0,
                        help="permitted temperature rise, degC (default 10)")
    parser.add_argument("--plating-oz", type=float, default=1.0,
                        help="via plating thickness, oz (default 1.0)")
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

    if args.current is None:
        parser.error("current is required; pass it positionally "
                     "(e.g. `via_current.py 2.0 --fab JLCPCB_STANDARD`) "
                     "or use --self-test")

    profiles = fab_profiles()
    if args.fab is None:
        parser.error(f"--fab is required; choose one of {sorted(profiles)}")
    if args.fab not in profiles:
        parser.error(f"unknown fab profile {args.fab!r}; "
                     f"valid choices are {sorted(profiles)}")

    try:
        result = recommend_via(args.current, profiles[args.fab],
                               args.delta_t, args.plating_oz)
    except ValueError as exc:
        parser.error(str(exc))

    report = calc_report(
        TOOL_NAME, STANDARD,
        inputs={
            "current_a": args.current,
            "fab": args.fab,
            "delta_t_c": args.delta_t,
            "plating_oz": args.plating_oz,
        },
        results=asdict(result),
        limits=LIMITS,
        model_class=MODEL_CLASS,
        signoff=SIGNOFF,
    )
    write_report(report, args.json)
    emit(report)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
