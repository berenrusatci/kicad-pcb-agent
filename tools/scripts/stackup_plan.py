#!/usr/bin/env python3
"""Plan a PCB layer count / stackup from a design-intent JSON file.

Reads a design intent (nets, components, mechanical limits and any optional
placement/DFM items), finds the smallest layer count that satisfies the
electrical and manufacturing rules in ``common.stackup``, runs the screening
checks over whatever the intent declared, and emits ONE JSON report on stdout.

    stackup_plan.py DESIGN_INTENT.json [--json REPORT.json]
                    [--fab-profile JLCPCB_STANDARD] [--copper-oz 1.0]
                    [--delta-t-c 10.0] [--er-eff 3.0]
                    [--max-attempts 10]
    stackup_plan.py --self-test

Exit codes: 0 = plan found and EVERY check was measured and clean, 1 = a check
FAILed, a check was never measured (NO_COVERAGE), or no layer count satisfied
the constraints, 2 = bad input or an unexpected error.

GREEN needs full coverage, not just an absence of failures: a check that had
nothing to examine (no antennas declared, so the antenna keep-out rule never
ran) is NO_COVERAGE and drags the report's status down with it. Declare the
section in the intent, or read the report as "this was not checked".

REPLACES the archive's ``if __name__ == "__main__":`` demo block, which
hardcoded one example board and printed 47 lines to stdout. Nothing here
prints to stdout except the report; every diagnostic goes to stderr.

SCREENING ONLY. Every number in this report comes from a closed-form or
rule-of-thumb model — ``model_class: exploratory_closed_form``,
``signoff: false``. Controlled impedance, creepage on a mains design and
thermal sizing all require their own sign-off path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    PASS,
    result,
    rollup_status,
    summarise,
)
from common.report_io import (  # noqa: E402
    EXIT_ERROR,
    EXIT_FAIL,
    EXIT_OK,
    diag,
    emit,
    error_report,
    run_cli,
    write_report,
)
from common.stackup import (  # noqa: E402
    MODEL_CLASS,
    SIGNOFF,
    AntennaPlacement,
    Component,
    CrystalPlacement,
    DiffPair,
    FrequencyBand,
    HighCurrentTrace,
    InterfaceKind,
    IsolationRequirement,
    MechanicalThermalLimits,
    Net,
    PackageKind,
    PhysicalVia,
    RfStitching,
    SignalKind,
    StackupDataError,
    StackupPlanError,
    ThermalManagement,
    ViaKind,
    ViaTypeDetail,
    ViaUsage,
    antenna_keepout_check,
    crystal_placement_check,
    decoupling_check,
    fab_dfm_check,
    fab_profiles,
    group_signals,
    isolation_check,
    length_matching_check,
    plan_stackup,
    power_copper_check,
    power_trace_widths_mm,
    rf_stitching_check,
    stackup_as_list,
    thermal_management_check,
    via_stub_check,
    via_type_aspect_ratio_check,
)

SCHEMA_VERSION = "stackup-plan-report/v1"
TOOL_NAME = "stackup_plan"


class IntentError(ValueError):
    """The design-intent file is not usable. Always exit 2, never a verdict."""


# --------------------------------------------------------------------------- #
# Design-intent parsing. Unknown enum members and unknown keys are REJECTED:
# a typo'd signal kind used to be indistinguishable from a slow digital net.
# --------------------------------------------------------------------------- #
def _enum(enum_cls, value, where: str):
    try:
        return enum_cls[str(value).upper()]
    except KeyError:
        raise IntentError(
            f"{where}: {value!r} is not a valid {enum_cls.__name__}; "
            f"expected one of {[m.name for m in enum_cls]}") from None


def _obj(entry, where: str) -> dict:
    if not isinstance(entry, dict):
        raise IntentError(f"{where}: expected an object, got {type(entry).__name__}")
    return entry


def _fields(entry: dict, where: str, required: Sequence[str],
            optional: Sequence[str]) -> None:
    allowed = set(required) | set(optional)
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise IntentError(f"{where}: unknown field(s) {unknown}; "
                          f"allowed: {sorted(allowed)}")
    missing = [name for name in required if name not in entry]
    if missing:
        raise IntentError(f"{where}: missing required field(s) {missing}")


def _list(intent: dict, key: str) -> list:
    value = intent.get(key, [])
    if not isinstance(value, list):
        raise IntentError(f"{key}: expected a list")
    return value


def parse_intent(intent: dict) -> dict:
    """Design-intent dict -> the dataclasses ``common.stackup`` operates on."""
    if not isinstance(intent, dict):
        raise IntentError("design intent must be a JSON object")

    known = {"schema_version", "board", "nets", "components", "constraints",
             "diff_pairs", "vias", "physical_vias", "antennas", "crystals",
             "rf_traces", "isolation", "high_current_traces", "thermal",
             "design_rules"}
    unknown = sorted(set(intent) - known)
    if unknown:
        raise IntentError(f"unknown top-level field(s) {unknown}; "
                          f"allowed: {sorted(known)}")

    nets = []
    for index, entry in enumerate(_list(intent, "nets")):
        where = f"nets[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where, ("name", "kind"),
                ("controlled_impedance", "max_current_a"))
        nets.append(Net(
            name=str(entry["name"]),
            kind=_enum(SignalKind, entry["kind"], where),
            controlled_impedance=bool(entry.get("controlled_impedance", False)),
            max_current_a=float(entry.get("max_current_a", 0.0)),
        ))
    if not nets:
        raise IntentError("nets: at least one net is required; planning a "
                          "stackup for an empty netlist would be a fabricated "
                          "answer")

    components = []
    for index, entry in enumerate(_list(intent, "components")):
        where = f"components[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where, ("name", "package"),
                ("pin_count", "power_pin_count", "decoupling_cap_count"))
        components.append(Component(
            name=str(entry["name"]),
            package=_enum(PackageKind, entry["package"], where),
            pin_count=int(entry.get("pin_count", 0)),
            power_pin_count=int(entry.get("power_pin_count", 0)),
            decoupling_cap_count=int(entry.get("decoupling_cap_count", 0)),
        ))

    raw_limits = _obj(intent.get("constraints", {}), "constraints")
    _fields(raw_limits, "constraints", (),
            ("target_thickness_mm", "max_dissipation_w", "allowed_via_kind",
             "copper_balance_tolerance_pct"))
    defaults = MechanicalThermalLimits()
    limits = MechanicalThermalLimits(
        target_thickness_mm=float(raw_limits.get(
            "target_thickness_mm", defaults.target_thickness_mm)),
        max_dissipation_w=float(raw_limits.get(
            "max_dissipation_w", defaults.max_dissipation_w)),
        allowed_via_kind=(
            _enum(ViaKind, raw_limits["allowed_via_kind"],
                  "constraints.allowed_via_kind")
            if "allowed_via_kind" in raw_limits else defaults.allowed_via_kind),
        copper_balance_tolerance_pct=float(raw_limits.get(
            "copper_balance_tolerance_pct",
            defaults.copper_balance_tolerance_pct)),
    )

    diff_pairs = []
    for index, entry in enumerate(_list(intent, "diff_pairs")):
        where = f"diff_pairs[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where, ("name", "interface", "length_p_mm", "length_n_mm"),
                ("data_rate_gbps", "target_impedance_ohm_override"))
        diff_pairs.append(DiffPair(
            name=str(entry["name"]),
            interface=_enum(InterfaceKind, entry["interface"], where),
            length_p_mm=float(entry["length_p_mm"]),
            length_n_mm=float(entry["length_n_mm"]),
            data_rate_gbps=float(entry.get("data_rate_gbps", 0.0)),
            target_impedance_ohm_override=float(
                entry.get("target_impedance_ohm_override", 0.0)),
        ))

    vias = []
    for index, entry in enumerate(_list(intent, "vias")):
        where = f"vias[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where,
                ("net_name", "start_layer", "end_layer", "data_rate_gbps"), ())
        vias.append(ViaUsage(
            net_name=str(entry["net_name"]),
            start_layer=int(entry["start_layer"]),
            end_layer=int(entry["end_layer"]),
            data_rate_gbps=float(entry["data_rate_gbps"]),
        ))

    physical_vias = []
    for index, entry in enumerate(_list(intent, "physical_vias")):
        where = f"physical_vias[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where,
                ("net_name", "start_layer", "end_layer", "kind",
                 "drill_diameter_mm", "pad_diameter_mm"),
                ("in_pad", "filled_and_capped"))
        physical_vias.append(PhysicalVia(
            net_name=str(entry["net_name"]),
            start_layer=int(entry["start_layer"]),
            end_layer=int(entry["end_layer"]),
            kind=_enum(ViaTypeDetail, entry["kind"], where),
            drill_diameter_mm=float(entry["drill_diameter_mm"]),
            pad_diameter_mm=float(entry["pad_diameter_mm"]),
            in_pad=bool(entry.get("in_pad", False)),
            filled_and_capped=bool(entry.get("filled_and_capped", False)),
        ))

    antennas = []
    for index, entry in enumerate(_list(intent, "antennas")):
        where = f"antennas[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where,
                ("name", "band", "edge_clearance_mm", "nearest_copper_mm"), ())
        antennas.append(AntennaPlacement(
            name=str(entry["name"]),
            band=_enum(FrequencyBand, entry["band"], where),
            edge_clearance_mm=float(entry["edge_clearance_mm"]),
            nearest_copper_mm=float(entry["nearest_copper_mm"]),
        ))

    crystals = []
    for index, entry in enumerate(_list(intent, "crystals")):
        where = f"crystals[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where,
                ("name", "ic_pin_distance_mm", "noise_source_distance_mm"),
                ("guard_ring",))
        crystals.append(CrystalPlacement(
            name=str(entry["name"]),
            ic_pin_distance_mm=float(entry["ic_pin_distance_mm"]),
            noise_source_distance_mm=float(entry["noise_source_distance_mm"]),
            guard_ring=bool(entry.get("guard_ring", False)),
        ))

    rf_traces = []
    for index, entry in enumerate(_list(intent, "rf_traces")):
        where = f"rf_traces[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where, ("name", "frequency_ghz", "via_pitch_mm"), ())
        rf_traces.append(RfStitching(
            name=str(entry["name"]),
            frequency_ghz=float(entry["frequency_ghz"]),
            via_pitch_mm=float(entry["via_pitch_mm"]),
        ))

    isolation = []
    for index, entry in enumerate(_list(intent, "isolation")):
        where = f"isolation[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where,
                ("name", "voltage_delta_v", "design_clearance_mm"), ())
        isolation.append(IsolationRequirement(
            name=str(entry["name"]),
            voltage_delta_v=float(entry["voltage_delta_v"]),
            design_clearance_mm=float(entry["design_clearance_mm"]),
        ))

    high_current = []
    for index, entry in enumerate(_list(intent, "high_current_traces")):
        where = f"high_current_traces[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where, ("name", "current_a", "width_mm"), ("external",))
        high_current.append(HighCurrentTrace(
            name=str(entry["name"]),
            current_a=float(entry["current_a"]),
            width_mm=float(entry["width_mm"]),
            external=bool(entry.get("external", True)),
        ))

    thermal = []
    for index, entry in enumerate(_list(intent, "thermal")):
        where = f"thermal[{index}]"
        entry = _obj(entry, where)
        _fields(entry, where, ("name", "dissipation_w", "thermal_via_count"), ())
        thermal.append(ThermalManagement(
            name=str(entry["name"]),
            dissipation_w=float(entry["dissipation_w"]),
            thermal_via_count=int(entry["thermal_via_count"]),
        ))

    design_rules = _obj(intent.get("design_rules", {}), "design_rules")
    _fields(design_rules, "design_rules", (),
            ("track_mm", "clearance_mm", "hole_mm", "fab_profile"))

    return {
        "board": intent.get("board"),
        "nets": nets,
        "components": components,
        "limits": limits,
        "diff_pairs": diff_pairs,
        "vias": vias,
        "physical_vias": physical_vias,
        "antennas": antennas,
        "crystals": crystals,
        "rf_traces": rf_traces,
        "isolation": isolation,
        "high_current_traces": high_current,
        "thermal": thermal,
        "design_rules": design_rules,
    }


def load_intent(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise IntentError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise IntentError(f"{path} is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report(intent_path: str, parsed: dict, *, fab_profile_key: str | None,
                 copper_oz: float, delta_t_c: float, er_eff: float,
                 max_attempts: int) -> dict:
    limits = parsed["limits"]
    groups = group_signals(parsed["nets"])

    plan: dict | None = None
    plan_error: str | None = None
    try:
        plan = plan_stackup(parsed["nets"], parsed["components"], limits,
                            max_attempts=max_attempts)
    except StackupPlanError as exc:
        plan_error = str(exc)

    layer_count = plan["layer_count"] if plan else None
    checks = [
        decoupling_check(parsed["components"]),
        length_matching_check(parsed["diff_pairs"]),
        antenna_keepout_check(parsed["antennas"]),
        crystal_placement_check(parsed["crystals"]),
        rf_stitching_check(parsed["rf_traces"], er_eff),
        isolation_check(parsed["isolation"]),
        power_copper_check(parsed["high_current_traces"], delta_t_c),
        thermal_management_check(parsed["thermal"]),
        via_type_aspect_ratio_check(parsed["physical_vias"]),
    ]
    if layer_count is not None:
        checks.append(via_stub_check(parsed["vias"], layer_count,
                                     limits.allowed_via_kind))

    rules = parsed["design_rules"]
    profile_key = fab_profile_key or rules.get("fab_profile")
    fab_used = None
    if profile_key:
        profiles = fab_profiles()
        if profile_key not in profiles:
            raise IntentError(
                f"unknown fab profile {profile_key!r}; "
                f"available: {sorted(profiles)}")
        profile = profiles[profile_key]
        fab_used = profile.key
        if all(key in rules for key in ("track_mm", "clearance_mm", "hole_mm")):
            checks.append(fab_dfm_check(
                float(rules["track_mm"]), float(rules["clearance_mm"]),
                float(rules["hole_mm"]), profile))
        else:
            # A skipped check used to leave NO TRACE in ``checks``: the branch
            # emitted a stderr diag() and nothing else, so a run with a fab
            # profile but incomplete design_rules produced a report in which
            # fab_dfm_limits simply did not exist. An absent check cannot even
            # be COUNTED as NO_COVERAGE, so the summary looked identical to a
            # run that never asked for a fab profile at all. The finding is now
            # explicit (scanned=0 => NO_COVERAGE, enforced by Finding itself);
            # the diag stays, because stderr is where a human notices it.
            checks.append(result(
                "fab_dfm_limits", 0, [],
                f"design_rules eksik (track_mm, clearance_mm ve hole_mm "
                f"gerekli); {profile.name} profiline karşı DFM kontrolü "
                f"çalıştırılamadı"))
            diag("design_rules incomplete; fab DFM check skipped "
                 "(need track_mm, clearance_mm and hole_mm)")

    summary = summarise(checks)
    # WAS: `RED if summary["FAIL"] else GREEN` — it branched on FAIL only and
    # never read summary["NO_COVERAGE"]. Most of the 9-11 checks here come from
    # common/stackup.py as `result(name, len(collection), ...)`, so every
    # section the intent does not declare (no diff pairs, no antennas, no
    # crystals, no RF, no thermal) arrives as scanned=0 => NO_COVERAGE. A
    # typical intent therefore reported GREEN with 5-8 of its checks never
    # measured: exactly the "an unmeasured check is not a clean check"
    # violation that Finding already forbids for a SINGLE check. The aggregate
    # rule now comes from common.findings.rollup_status (see the COVERAGE
    # MONOTONICITY note there) instead of being re-derived here; its
    # PASS/FAIL/NO_COVERAGE vocabulary is mapped onto this report's
    # GREEN/RED/NO_COVERAGE one. UNRESOLVED still wins over everything: if no
    # layer count satisfied the constraints there is no stackup for the checks
    # to be about, and it already exits non-zero.
    if plan is None:
        status = "UNRESOLVED"
    else:
        status = {PASS: "GREEN", FAIL: "RED",
                  NO_COVERAGE: "NO_COVERAGE"}[rollup_status(checks)]

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "model_class": MODEL_CLASS,
        "signoff": SIGNOFF,
        "input": str(intent_path),
        "board": parsed["board"],
        "status": status,
        "params": {
            "copper_oz": copper_oz,
            "delta_t_c": delta_t_c,
            "er_eff": er_eff,
            "max_attempts": max_attempts,
            "fab_profile": fab_used,
        },
        "plan": None if plan is None else {
            "layer_count": plan["layer_count"],
            "layers": stackup_as_list(plan["layers"]),
            "attempts": plan["attempts"],
            "warnings": plan["warnings"],
            "groups": plan["groups"],
        },
        "plan_error": plan_error,
        "power_trace_widths_mm": power_trace_widths_mm(groups, copper_oz,
                                                       delta_t_c),
        "checks": checks,
        "summary": summary,
    }


def report_exit_code(report: dict) -> int:
    if report["status"] == "GREEN":
        return EXIT_OK
    return EXIT_FAIL


# --------------------------------------------------------------------------- #
# Self-tests (OPT-IN; stderr only, never stdout)
# --------------------------------------------------------------------------- #
def run_self_tests() -> None:
    """A handful of invariants the CLI would otherwise only prove in tests."""
    from common.stackup import (SignalKind as SK, required_layer_count,
                                required_isolation_mm, trace_width_mm)

    def more_current_needs_more_copper() -> None:
        assert trace_width_mm(5.0) > trace_width_mm(1.0), \
            "trace width did not grow with current"

    def zero_current_is_zero_width() -> None:
        assert trace_width_mm(0.0) == 0.0, "zero current must give zero width"

    def rf_forces_six_layers() -> None:
        groups = group_signals([Net("GND", SK.GND), Net("ANT", SK.RF)])
        count = required_layer_count(groups, [])
        assert count >= 6, f"RF needs >=6 layers, got {count}"

    def isolation_grows_with_voltage() -> None:
        assert required_isolation_mm(500) > required_isolation_mm(50), \
            "clearance did not grow with voltage"

    def fab_table_loads() -> None:
        profiles = fab_profiles()
        for key in ("JLCPCB_STANDARD", "JLCPCB_ADVANCED", "PCBWAY_STANDARD"):
            assert key in profiles, f"missing fab profile {key}"

    tests = (
        ("current -> width", more_current_needs_more_copper),
        ("zero current", zero_current_is_zero_width),
        ("RF layer floor", rf_forces_six_layers),
        ("isolation table", isolation_grows_with_voltage),
        ("fab profiles load", fab_table_loads),
    )
    for name, test in tests:
        test()
        diag(f"SELF-TEST PASS [{name}]")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("intent", nargs="?",
                        help="design-intent JSON file (required unless "
                             "--self-test)")
    parser.add_argument("--json", type=Path,
                        help="also WRITE the JSON report to this path "
                             "(never reads it)")
    parser.add_argument("--fab-profile",
                        help="fab profile key, e.g. JLCPCB_STANDARD; "
                             "overrides design_rules.fab_profile")
    parser.add_argument("--copper-oz", type=float, default=1.0,
                        help="copper weight for the trace-width screen")
    parser.add_argument("--delta-t-c", type=float, default=10.0,
                        help="allowed temperature rise, °C")
    parser.add_argument("--er-eff", type=float, default=3.0,
                        help="effective permittivity for the RF stitching rule")
    parser.add_argument("--max-attempts", type=int, default=10,
                        help="how many layer counts to try before giving up")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the built-in self-tests and exit "
                             "(opt-in; never runs during a normal plan)")
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --self-test is OPT-IN and writes to stderr only.
    if args.self_test:
        try:
            run_self_tests()
        except (AssertionError, StackupDataError) as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return EXIT_FAIL
        return EXIT_OK

    if args.intent is None:
        # Missing required input is an ERROR (exit 2 via argparse), never a
        # hint printed with a success exit code.
        parser.error("a design-intent JSON file is required; pass it "
                     "positionally (e.g. `stackup_plan.py intent.json`) "
                     "or use --self-test")

    if args.copper_oz <= 0 or args.delta_t_c <= 0 or args.er_eff <= 0:
        parser.error("--copper-oz, --delta-t-c and --er-eff must be positive")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")

    try:
        parsed = parse_intent(load_intent(Path(args.intent)))
        report = build_report(
            str(args.intent), parsed,
            fab_profile_key=args.fab_profile,
            copper_oz=args.copper_oz, delta_t_c=args.delta_t_c,
            er_eff=args.er_eff, max_attempts=args.max_attempts)
    except (IntentError, StackupDataError) as exc:
        code = ("INPUT_ERROR" if isinstance(exc, IntentError)
                else "REFERENCE_DATA_ERROR")
        emit(error_report(SCHEMA_VERSION, code, str(exc), tool=TOOL_NAME))
        diag(f"ERROR: {exc}")
        return EXIT_ERROR

    if report["plan_error"]:
        diag(f"UNRESOLVED: {report['plan_error']}")
    if report["status"] == "NO_COVERAGE":
        # Name them. "NO_COVERAGE" alone tells a reader that something was not
        # measured but not WHICH intent section to declare to fix it.
        unmeasured = sorted(check["check"] for check in report["checks"]
                            if check["status"] == NO_COVERAGE)
        diag(f"NO_COVERAGE: {len(unmeasured)} check(s) were never measured: "
             f"{', '.join(unmeasured)}")
    for warning in (report["plan"] or {}).get("warnings", []):
        diag(warning)

    write_report(report, args.json)
    emit(report)
    return report_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
