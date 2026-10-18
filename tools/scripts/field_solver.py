#!/usr/bin/env python3
"""2-D cross-section field solver CLI - the numerical impedance gate.

Solves the actual electrostatic problem for a PCB cross-section instead of
evaluating the IPC-2141/Wadell closed form, and reports BOTH so the bias is
visible rather than assumed. ``common/si_models.py`` documents that bias as
-29.94%..+2.41% against the published Polar comparison; this tool measures it
for the specific geometry in front of it.

Exit contract (``common/report_io.py``):
  0  the answer was produced (no --target), or it met --target
  1  RED / NO_COVERAGE - answer produced but it fails the target, or the
     solve did not converge well enough to be allowed to answer
  2  the tool could not produce an answer at all

STILL NOT A SIGN-OFF TOOL. ``signoff`` stays false and ``signoff_blockers``
says exactly what is missing: real laminate data at frequency, loss and
roughness, and correlation against a measured coupon. What changed is that the
error is now BOUNDED by grid refinement instead of being an unknown offset.
"""

from __future__ import annotations

import argparse
import math
import sys
import json
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.field2d import (  # noqa: E402
    HAVE_SCIPY,
    MODEL_CLASS,
    SIGNOFF,
    CrossSection,
    FieldSolverError,
    coax,
    coax_analytic_z0_ohm,
    FlexLayer,
    FlexStackup,
    coupled_flex_microstrip,
    coupled_microstrip,
    coupled_stripline,
    flex_microstrip,
    microstrip,
    richardson_estimate,
    solve_line,
    stripline,
)
from common.findings import acceptance_verdict  # noqa: E402
from common.report_io import diag, emit, run_cli, status_exit_code, write_report  # noqa: E402
from common.si_models import (  # noqa: E402
    ModelDomainError,
    z0_microstrip,
    z0_stripline,
    zdiff_microstrip,
    zdiff_stripline,
)

SCHEMA_VERSION = "field-solver-report/v1"

COUPLED = {"coupled-microstrip", "coupled-stripline",
           "coupled-flex-microstrip"}

SIGNOFF_BLOCKERS = [
    "laminate er is a single nominal value; no frequency dispersion, no "
    "loss tangent, no fibre-weave variation",
    "conductor is a perfect conductor: no skin effect, no surface roughness",
    "quasi-static solve; no propagation, no radiation, no via transitions",
    "no correlation against a measured impedance coupon from the real fab",
]

LIMITS = [
    "numerical 2-D FDM, quasi-static and lossless",
    "uniform grid: curved boundaries are staircased, rectangles are exact",
    "outer box is a perfect electric wall at a finite distance "
    "(see grid.box_margin_factor)",
    "grid error is estimated by Richardson extrapolation over two "
    "resolutions and reported as grid.grid_error_percent",
]


# --------------------------------------------------------------------------- #
# Geometry dispatch
# --------------------------------------------------------------------------- #
def _build_section(args: argparse.Namespace, cells_per_feature: int
                   ) -> CrossSection:
    shared = {
        "cells_per_feature": cells_per_feature,
        "max_nodes": args.max_nodes,
    }
    if args.geometry == "coax":
        return coax(args.a, args.b, args.er,
                    cells_across_inner=cells_per_feature * 4,
                    max_nodes=args.max_nodes)
    if args.geometry == "microstrip":
        return microstrip(args.w, args.t, args.h, args.er,
                          box_margin_factor=args.box_margin_factor,
                          cover_mm=args.cover, cover_er=args.cover_er,
                          **shared)
    if args.geometry == "coupled-microstrip":
        return coupled_microstrip(args.w, args.s, args.t, args.h, args.er,
                                  box_margin_factor=args.box_margin_factor,
                                  cover_mm=args.cover, cover_er=args.cover_er,
                                  **shared)
    if args.geometry == "stripline":
        return stripline(args.w, args.t, args.b_spacing, args.er,
                         box_margin_factor=args.box_margin_factor, **shared)
    if args.geometry == "coupled-stripline":
        return coupled_stripline(args.w, args.s, args.t, args.b_spacing,
                                 args.er,
                                 box_margin_factor=args.box_margin_factor,
                                 **shared)
    if args.geometry in ("flex-microstrip", "coupled-flex-microstrip"):
        stackup = _flex_stackup(args)
        if args.geometry == "flex-microstrip":
            return flex_microstrip(args.w, stackup,
                                   box_margin_factor=args.box_margin_factor,
                                   **shared)
        return coupled_flex_microstrip(args.w, args.s, stackup,
                                       box_margin_factor=args.box_margin_factor,
                                       **shared)
    raise FieldSolverError(f"unknown geometry {args.geometry!r}")


def _flex_stackup(args: argparse.Namespace) -> FlexStackup:
    """Build the flex stack from a JSON file, never from CLI defaults.

    There is no ``--pi-er`` flag on purpose. A permittivity typed at a shell
    prompt arrives with no citation, and a flex er is not a constant of the
    material class: it depends on the film grade, the adhesive system and the
    frequency it was measured at. The stack therefore comes from a file whose
    every layer names its source, and the loader refuses a layer without one.
    """
    if args.stackup_region:
        source, _, region = args.stackup_region.rpartition(":")
        if not source:
            raise FieldSolverError(
                "--stackup-region wants FILE:REGION, e.g. "
                "layout/stackup.json:cable-tail")
        return _flex_stackup_from_region(source, region)
    if not args.flex_stackup:
        raise FieldSolverError(
            f"{args.geometry} needs --flex-stackup FILE.json: a flex stack's "
            "permittivities must arrive with their sources, so they are not "
            "CLI flags")
    path = Path(args.flex_stackup)
    if not path.is_file():
        raise FieldSolverError(f"flex stackup not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FieldSolverError(f"{path}: not JSON: {exc}") from exc

    def layer(key: str, required: bool) -> FlexLayer | None:
        raw = payload.get(key)
        if raw is None:
            if required:
                raise FieldSolverError(f"{path}: '{key}' is required")
            return None
        missing = [f for f in ("thickness_mm", "er", "source") if f not in raw]
        if missing:
            raise FieldSolverError(
                f"{path}: layer '{key}' is missing {missing}. 'source' is not "
                "optional - an er without a citation cannot be told apart "
                "from one that was measured.")
        return FlexLayer(name=raw.get("name", key),
                         thickness_mm=float(raw["thickness_mm"]),
                         er=float(raw["er"]), source=str(raw["source"]))

    if "copper_thickness_mm" not in payload:
        raise FieldSolverError(f"{path}: 'copper_thickness_mm' is required")
    return FlexStackup(
        base=layer("base", True),                      # type: ignore[arg-type]
        copper_thickness_mm=float(payload["copper_thickness_mm"]),
        coverlay_adhesive=layer("coverlay_adhesive", False),
        coverlay_film=layer("coverlay_film", False),
        adhesiveless=bool(payload.get("adhesiveless", True)))


def _flex_stackup_from_region(path: str, region_name: str) -> FlexStackup:
    """Build a FlexStackup from a PROJECT stackup.json flex region.

    The project file is the single source of truth for materials, so the
    solver reads er from it rather than carrying any of its own. There is
    still no default anywhere in this path: a region whose layers have no
    ``epsilon_r``/``src`` is refused, and a region marked CONFIRM is refused
    by name so the reason reaches the caller instead of a KeyError.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    regions = (payload.get("flex_regions") or {}).get("regions") or {}
    if region_name not in regions:
        raise FieldSolverError(
            f"{path}: no flex region {region_name!r}; known: {sorted(regions)}")
    region = regions[region_name]
    if region.get("status") == "CONFIRM":
        raise FieldSolverError(
            f"flex region {region_name!r} is status CONFIRM: its materials are "
            f"not in this repository yet. {region.get('needs', '')} Solving it "
            "would mean inventing the permittivities, which is the one thing "
            "this solver must not do.")

    ordered = region.get("ordered") or []
    if not ordered:
        raise FieldSolverError(f"flex region {region_name!r} has no layers")

    def layer(entry: dict) -> FlexLayer:
        missing = [k for k in ("thickness_mm", "epsilon_r", "src")
                   if entry.get(k) in (None, "")]
        if missing:
            raise FieldSolverError(
                f"flex region {region_name!r}, layer {entry.get('kind')!r} is "
                f"missing {missing}. 'src' is not optional: an er without a "
                "citation cannot be told apart from one that was measured.")
        return FlexLayer(name=str(entry.get("kind")),
                         thickness_mm=float(entry["thickness_mm"]),
                         er=float(entry["epsilon_r"]), source=str(entry["src"]))

    by_kind: dict[str, list[dict]] = {}
    for entry in ordered:
        by_kind.setdefault(str(entry.get("kind")), []).append(entry)

    copper = by_kind.get("adhesiveless_copper") or by_kind.get("copper")
    if not copper:
        raise FieldSolverError(
            f"flex region {region_name!r} has no copper layer")
    copper_mm = copper[0].get("thickness_mm") or region.get("copper_thickness_mm")
    if not copper_mm:
        raise FieldSolverError(
            f"flex region {region_name!r}: copper thickness is not given")

    bases = [layer(e) for e in by_kind.get("polyimide_base", [])]
    if not bases:
        raise FieldSolverError(
            f"flex region {region_name!r} has no polyimide_base layer")
    adhesive = by_kind.get("coverlay_adhesive")
    film = by_kind.get("coverlay_film")
    return FlexStackup(
        base=bases[-1],                       # the one touching the copper
        base_underlayers=tuple(bases[:-1]),   # asymmetric, plane side first
        copper_thickness_mm=float(copper_mm),
        coverlay_adhesive=layer(adhesive[0]) if adhesive else None,
        coverlay_film=layer(film[0]) if film else None,
        adhesiveless=bool(region.get("adhesiveless", True)))


def _primary_impedance(args: argparse.Namespace, result) -> tuple[str, float]:
    """The quantity a --target is compared against, and its name."""
    if args.geometry in COUPLED:
        return "z_diff_ohm", result.z_diff_ohm()
    return "z0_ohm", result.z0_ohm()


def _closed_form(args: argparse.Namespace) -> dict[str, object]:
    """The IPC-2141/Wadell answer for the same geometry, plus its own caveat.

    Reported next to the numerical answer on purpose. The delta between them
    IS the finding - the archive shipped only the closed form and called the
    difference an unknown.
    """
    try:
        if args.geometry == "microstrip":
            value = z0_microstrip(args.w, args.t, args.h, args.er)
        elif args.geometry == "coupled-microstrip":
            value = zdiff_microstrip(args.w, args.s, args.t, args.h, args.er)
        elif args.geometry == "stripline":
            value = z0_stripline(args.w, args.t, args.b_spacing, args.er)
        elif args.geometry == "coupled-stripline":
            value = zdiff_stripline(args.w, args.s, args.t, args.b_spacing,
                                    args.er)
        elif args.geometry == "coax":
            value = coax_analytic_z0_ohm(args.a, args.b, args.er)
            return {
                "available": True,
                "model": "exact analytic coaxial solution",
                "value_ohm": round(value, 4),
                "exact": True,
            }
        else:
            return {"available": False, "reason": "no closed form for this geometry"}
    except (ModelDomainError, ValueError) as exc:
        return {"available": False, "reason": f"out of model domain: {exc}"}
    return {
        "available": True,
        "model": "IPC-2141/Wadell closed form (common.si_models)",
        "value_ohm": round(value, 4),
        "exact": False,
    }


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _coax_reference_within(tolerance_percent: float,
                           eta0_over_two_pi: float | None = None) -> None:
    """Solve a coax numerically and compare against its exact analytic Z0.

    Coax is the reference geometry because its solution is exact, closed, and
    contains no fitted coefficient - validating against it invents nothing.
    It is also the solver's WORST case: the two circles are staircased onto a
    square grid, while the microstrip and stripline geometries this tool is
    actually used for are rectangles that land on the grid exactly.
    """
    a_mm, b_mm, er = 0.5, 1.5, 4.3
    section = coax(a_mm, b_mm, er, cells_across_inner=48, max_nodes=200_000)
    numerical = solve_line(section).z0_ohm()

    if eta0_over_two_pi is None:
        exact = coax_analytic_z0_ohm(a_mm, b_mm, er)
    else:
        exact = eta0_over_two_pi / math.sqrt(er) * math.log(b_mm / a_mm)

    deviation = abs(numerical - exact) / exact * 100.0
    assert deviation <= tolerance_percent, (
        f"coax reference mismatch: numerical={numerical:.4f} ohm, "
        f"exact={exact:.4f} ohm, deviation={deviation:.3f}% "
        f"> {tolerance_percent}%")


def _prove_test_not_empty() -> bool:
    """Prove the coax reference rejects a deliberately wrong eta0/2pi."""
    try:
        _coax_reference_within(2.0, eta0_over_two_pi=70.0)
    except AssertionError:
        print("FAULT-INJECTION: observed=FAIL, proof=PASS "
              "(deliberately wrong eta0/2pi=70 was rejected)")
        return True
    print("FAULT-INJECTION: observed=PASS, proof=FAIL "
          "(coax reference did not reject the wrong constant)")
    return False


def run_self_tests() -> None:
    """Numerical-reference, physical-invariant and monotonicity tests."""

    def coax_reference() -> None:
        _coax_reference_within(2.0)

    def homogeneous_er_eff_is_exact() -> None:
        """er_eff == er is an identity in a homogeneous medium.

        It holds independently of the mesh, so it tests the dielectric
        handling and the C/C0 extraction without any tolerance argument.
        """
        section = coax(0.5, 1.5, 4.3, cells_across_inner=16, max_nodes=60_000)
        er_eff = solve_line(section).er_eff()
        assert abs(er_eff - 4.3) < 1e-6, f"er_eff={er_eff!r} should equal 4.3"

    def microstrip_er_eff_is_bracketed() -> None:
        """Mixed air/laminate: 1 < er_eff < er, always."""
        section = microstrip(0.2, 0.035, 0.1, 4.3, cells_per_feature=3,
                             max_nodes=90_000)
        er_eff = solve_line(section).er_eff()
        assert 1.0 < er_eff < 4.3, f"er_eff={er_eff!r} outside (1, 4.3)"

    def wider_trace_lowers_impedance() -> None:
        narrow = solve_line(microstrip(0.15, 0.035, 0.1, 4.3,
                                       cells_per_feature=3,
                                       max_nodes=90_000)).z0_ohm()
        wide = solve_line(microstrip(0.30, 0.035, 0.1, 4.3,
                                     cells_per_feature=3,
                                     max_nodes=90_000)).z0_ohm()
        assert wide < narrow, f"Z0 did not fall with width: {narrow} -> {wide}"

    def odd_mode_is_below_even_mode() -> None:
        result = solve_line(coupled_microstrip(0.1, 0.15, 0.035, 0.1, 4.3,
                                               cells_per_feature=3,
                                               max_nodes=120_000))
        odd, even = result.z_odd_ohm(), result.z_even_ohm()
        assert odd < even, f"Z_odd {odd} should be below Z_even {even}"

    tests = (
        ("coax vs exact analytic", coax_reference),
        ("homogeneous er_eff identity", homogeneous_er_eff_is_exact),
        ("microstrip er_eff bracketed", microstrip_er_eff_is_bracketed),
        ("wider trace lowers Z0", wider_trace_lowers_impedance),
        ("odd mode below even mode", odd_mode_is_below_even_mode),
    )
    for name, test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"SELF-TEST FAIL [{name}]: {exc}", file=sys.stderr)
            raise
        else:
            print(f"SELF-TEST PASS [{name}]")

    try:
        assert _prove_test_not_empty(), "fault-injection proof itself failed"
    except AssertionError as exc:
        print(f"SELF-TEST FAIL [fault injection]: {exc}", file=sys.stderr)
        raise


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "geometry", nargs="?",
        choices=sorted({"microstrip", "coupled-microstrip", "stripline",
                        "coupled-stripline", "coax",
                        "flex-microstrip", "coupled-flex-microstrip"}),
        help="cross-section type (required unless --self-test)")

    geometry = parser.add_argument_group("geometry, millimetres")
    geometry.add_argument("--w", type=float, help="trace width")
    geometry.add_argument("--s", type=float, help="edge-to-edge gap (coupled)")
    geometry.add_argument("--t", type=float, default=0.035,
                          help="copper thickness (default 0.035)")
    geometry.add_argument("--h", type=float,
                          help="laminate height to the reference plane "
                               "(microstrip)")
    geometry.add_argument("--b-spacing", type=float, dest="b_spacing",
                          help="TOTAL plane-to-plane spacing (stripline); "
                               "same convention as si_models.z0_stripline")
    geometry.add_argument("--a", type=float, help="coax inner radius")
    geometry.add_argument("--b", type=float, help="coax outer radius")
    geometry.add_argument("--er", type=float, default=4.3,
                          help="laminate relative permittivity (default 4.3)")
    geometry.add_argument("--cover", type=float, default=0.0,
                          help="soldermask thickness, 0 disables (default 0)")
    geometry.add_argument("--cover-er", type=float, default=3.5,
                          dest="cover_er", help="soldermask er (default 3.5)")
    geometry.add_argument(
        "--stackup-region", dest="stackup_region", metavar="FILE:REGION",
        help="read the flex stack from a project stackup.json flex region, "
             "e.g. layout/stackup.json:cable-tail. The materials stay in the "
             "project file; this solver carries none of its own")
    geometry.add_argument(
        "--flex-stackup", dest="flex_stackup",
        help="JSON flex stack for flex-microstrip / coupled-flex-microstrip. "
             "Every dielectric layer must carry thickness_mm, er AND source; "
             "there is deliberately no --pi-er flag, because a permittivity "
             "typed at a prompt arrives without a citation and a flex er is "
             "not a constant of the material class")

    mesh = parser.add_argument_group("mesh")
    mesh.add_argument("--cells-per-feature", type=int, default=4,
                      dest="cells_per_feature",
                      help="upper bound on grid pitch, in cells across the "
                           "smallest feature (default 4). The pitch is then "
                           "fitted to the geometry so every dimension lands "
                           "on whole cells.")
    mesh.add_argument("--max-nodes", type=int, default=300_000,
                      dest="max_nodes",
                      help="node budget; the pitch is relaxed to stay under "
                           "it and the report says so (default 300000)")
    mesh.add_argument("--box-margin-factor", type=float, default=8.0,
                      dest="box_margin_factor",
                      help="open-structure box wall distance, in multiples of "
                           "(h+t) (default 8)")
    mesh.add_argument("--refine", action="store_true",
                      help="also solve at half the pitch and report a "
                           "Richardson error bound")

    gate = parser.add_argument_group("gate")
    gate.add_argument("--target", type=float,
                      help="target impedance in ohms; turns this into a "
                           "PASS/FAIL gate")
    gate.add_argument("--tolerance-percent", type=float, default=10.0,
                      dest="tolerance_percent",
                      help="allowed deviation from --target (default 10)")
    gate.add_argument("--max-grid-error-percent", type=float, default=2.0,
                      dest="max_grid_error_percent",
                      help="with --refine, refuse to answer above this "
                           "estimated grid error (default 2)")

    parser.add_argument("--json", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run self-tests and exit")
    return parser


REQUIRED_ARGUMENTS = {
    "microstrip": ("w", "h"),
    "coupled-microstrip": ("w", "s", "h"),
    "stripline": ("w", "b_spacing"),
    "coupled-stripline": ("w", "s", "b_spacing"),
    "coax": ("a", "b"),
    # h and er are absent on purpose: on flex they come from the stackup file
    # with their sources, not from flags. flex_stackup is listed so a missing
    # file is refused at argument time rather than deep in the builder.
    "flex-microstrip": ("w",),
    "coupled-flex-microstrip": ("w", "s"),
}


def _check_required(parser: argparse.ArgumentParser,
                    args: argparse.Namespace) -> None:
    missing = [name for name in REQUIRED_ARGUMENTS[args.geometry]
               if getattr(args, name) is None]
    if missing:
        flags = ", ".join("--" + name.replace("_", "-") for name in missing)
        parser.error(f"{args.geometry} requires {flags}")


def _geometry_echo(args: argparse.Namespace) -> dict[str, object]:
    keys = ("w", "s", "t", "h", "b_spacing", "a", "b", "er", "cover",
            "cover_er")
    return {key: getattr(args, key) for key in keys
            if getattr(args, key) is not None}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            run_self_tests()
        except (AssertionError, ValueError, FieldSolverError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    if args.geometry is None:
        parser.error("geometry is required; pass it positionally "
                     "(e.g. `field_solver.py microstrip --w 0.2 --h 0.1`) "
                     "or use --self-test")
    _check_required(parser, args)

    try:
        section = _build_section(args, args.cells_per_feature)
        result = solve_line(section)
    except FieldSolverError as exc:
        parser.error(str(exc))

    metric_name, impedance = _primary_impedance(args, result)

    grid: dict[str, object] = {
        "method": "sparse_direct" if HAVE_SCIPY else "red_black_sor",
        "pitch_um": round(section.dx_mm * 1000.0, 5),
        "nodes": section.node_count,
        "converged": result.converged,
        "box_margin_factor": args.box_margin_factor,
        "notes": list(section.notes),
    }

    reason_codes: list[str] = []
    if args.refine:
        try:
            fine_section = _build_section(args, args.cells_per_feature * 2)
            fine_result = solve_line(fine_section)
            _, fine_impedance = _primary_impedance(args, fine_result)
            ratio = section.dx_mm / fine_section.dx_mm
            if ratio <= 1.0:
                grid["refinement"] = {
                    "skipped": "node budget prevented a finer grid; "
                               "raise --max-nodes"}
                reason_codes.append("REFINEMENT_NOT_POSSIBLE")
            else:
                estimate = richardson_estimate(impedance, fine_impedance,
                                               refinement_ratio=ratio)
                grid["refinement"] = {
                    "fine_pitch_um": round(fine_section.dx_mm * 1000.0, 5),
                    "fine_nodes": fine_section.node_count,
                    "refinement_ratio": round(ratio, 4),
                    **{key: round(value, 6) for key, value in estimate.items()},
                }
                impedance = fine_impedance
        except FieldSolverError as exc:
            grid["refinement"] = {"skipped": str(exc)}
            reason_codes.append("REFINEMENT_FAILED")

    results: dict[str, object] = {metric_name: round(impedance, 4)}
    if args.geometry in COUPLED:
        results.update({
            "z_odd_ohm": round(result.z_odd_ohm(), 4),
            "z_even_ohm": round(result.z_even_ohm(), 4),
            "z_common_ohm": round(result.z_common_ohm(), 4),
            "er_eff_odd": round(result.er_eff_odd(), 5),
            "er_eff_even": round(result.er_eff_even(), 5),
        })
    else:
        results["er_eff"] = round(result.er_eff(), 5)
    results["capacitance_f_per_m"] = [
        [float(value) for value in row]
        for row in result.capacitance_matrix_f_per_m
    ]

    closed_form = _closed_form(args)
    if closed_form.get("available"):
        reference = float(closed_form["value_ohm"])       # type: ignore[arg-type]
        closed_form["numerical_minus_closed_form_percent"] = round(
            (impedance - reference) / reference * 100.0, 3)

    status = "GREEN"
    if not result.converged:
        status = "NO_COVERAGE"
        reason_codes.append("SOLVE_NOT_CONVERGED")

    gate: dict[str, object] = {"applied": args.target is not None}
    if args.target is not None:
        deviation = abs(impedance - args.target) / args.target * 100.0
        within = deviation <= args.tolerance_percent
        gate.update({
            "target_ohm": args.target,
            "tolerance_percent": args.tolerance_percent,
            "deviation_percent": round(deviation, 3),
            "within_tolerance": within,
        })
        if not within and status == "GREEN":
            status = "RED"
            reason_codes.append("TARGET_NOT_MET")

    refinement = grid.get("refinement")
    if isinstance(refinement, dict) and "grid_error_percent" in refinement:
        grid_error = float(refinement["grid_error_percent"])
        if grid_error > args.max_grid_error_percent:
            status = "NO_COVERAGE"
            reason_codes.append("GRID_ERROR_TOO_LARGE")

    # ``status`` answers "did the computation complete?"; it does NOT answer
    # "was an acceptance criterion satisfied?", and the two were riding on the
    # same word. With no --target the solver still reported GREEN, so a
    # consumer reading the top-level status could not tell a met specification
    # from a number nobody had a specification for. ``gate.verdict`` is that
    # second axis, in the repo's usual vocabulary: with no criterion there is
    # nothing to have passed, and NO_COVERAGE is what "not measured against
    # anything" is called everywhere else here.
    gate.update(acceptance_verdict(
        applied=args.target is not None,
        met=bool(gate.get("within_tolerance")),
        computed=status != "NO_COVERAGE",
        missing_reason="no --target was supplied, so no acceptance criterion "
                       "was applied to this impedance"))

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": "field_solver",
        "model_class": MODEL_CLASS,
        "signoff": SIGNOFF,
        "signoff_blockers": SIGNOFF_BLOCKERS,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "geometry": args.geometry,
        "inputs": _geometry_echo(args),
        "results": results,
        "closed_form_comparison": closed_form,
        "grid": grid,
        "gate": gate,
        "limits": LIMITS,
    }

    diag("")
    diag(f"{args.geometry}: {metric_name} = {impedance:.3f} ohm "
         f"({grid['method']}, {section.node_count} nodes, "
         f"pitch {section.dx_mm * 1000:.3f} um)")
    if closed_form.get("available"):
        diag(f"  closed form {closed_form['value_ohm']} ohm -> numerical is "
             f"{closed_form['numerical_minus_closed_form_percent']:+.2f}%")
    for note in section.notes:
        diag(f"  note: {note}")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
