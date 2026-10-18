#!/usr/bin/env python3
"""Plans series shunts + probe testpoints for NON-DESTRUCTIVE per-rail
current measurement, and verifies every rail claim against the netlist.

THE GAP THIS CLOSES
--------------------------------------------------------------------------
A regulator's per-rail draw is routinely unknown at schematic time - the
part's total power is public while the split across its rails sits behind
an NDA, or the load is a module nobody has measured. Every number that
hangs off that draw (regulator thermal margin, connector contact count,
bypass sizing) is then an assumption. A series shunt with two probe nodes
turns it into a bench measurement that needs no cut trace and no lifted
pin: read the millivolts, divide by R.

WHAT THIS TOOL IS AND IS NOT
--------------------------------------------------------------------------
It is a PLANNER. It does not edit a schematic, and that is deliberate:
schematics in this repo are GENERATED from pin tables, deterministically,
so an edit made behind the generator's back is erased by the next build
and leaves no trace of having been erased. The plan is the artifact; the
board's own generator consumes it and emits the parts. The same reasoning
that keeps generated files out of git merges applies here.

It also does not accept a shunt value. ``common/shunt.py`` explains why in
full; the short form is that one value cannot serve rails whose full-scale
currents differ several-fold, and that the value follows from the droop
budget and the meter, both of which are inputs.

WHAT IT VERIFIES RATHER THAN ASSUMES
--------------------------------------------------------------------------
Each ``--rail NAME=REF:PIN`` says "this net is that regulator's output".
With ``--netlist`` the claim is CHECKED: the net exists, the ref exists,
and that ref's pin really sits on that net. A mismatch is a FAIL, not a
warning - the whole point is that the shunt lands in the right place, and
a plan that names a net nobody drives would be executed just as happily.
Without ``--netlist`` the check reports NO_COVERAGE; the sizing still runs,
but nothing claims the topology was confirmed.

Usage:
    shunt_plan.py --rail VDD_1V2D=U303:5 --full-scale-current-a 0.3
                  --max-droop-mv 12 --meter-resolution-uv 10
                  --current-resolution-ma 1
                  --position upstream_of_bypass
                  [--netlist build/netlist.net]
                  [--package-power-w 0.0625] [--parasitic-mohm 3]
                  [--kelvin] [--series E24] [--json shunt_plan.json]
    shunt_plan.py --self-test

Per-rail overrides use the same ``NAME=VALUE`` form, e.g.
``--rail-full-scale-current-a VDD_2V8A=0.15``.

Exit codes: 0 = GREEN (every rail sized and verified), 1 = RED or
NO_COVERAGE, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import Finding, NO_COVERAGE, result, summarise  # noqa: E402
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)
from common.netlist import read_nets as read_netlist_nets  # noqa: E402
from common.shunt import (DOWNSTREAM_OF_BYPASS, POSITIONS,  # noqa: E402
                          UPSTREAM_OF_BYPASS, RailShunt, sense_net_name,
                          size_rail, split_preview, testpoint_names)

SCHEMA_VERSION = "shunt-plan/v1"
TOOL_NAME = "shunt_plan"


# ---------------------------------------------------------------- netlist


def verify_rail_claims(claims: Dict[str, Tuple[str, str]],
                       nets: Optional[Dict[str, List[Tuple[str, str]]]]):
    """Check every ``net -> (ref, pin)`` claim against the netlist."""
    check = "rail_claim_matches_netlist"
    if nets is None:
        return result(check, 0, [], "no netlist supplied - topology unverified"), {}

    violations: List[dict] = []
    splits: Dict[str, dict] = {}
    for rail, (ref, pin) in sorted(claims.items()):
        nodes = nets.get(rail)
        if nodes is None:
            violations.append({"rail": rail, "issue": "net_not_in_netlist",
                               "detail": "the plan would place a shunt on a net "
                                         "that nothing drives"})
            continue
        if (ref, pin) not in nodes:
            on_net = sorted({r for r, _ in nodes})
            violations.append({"rail": rail, "issue": "regulator_pin_not_on_net",
                               "claimed": f"{ref}:{pin}", "refs_on_net": on_net})
            continue
        if len(nodes) < 2:
            violations.append({"rail": rail, "issue": "single_node_net",
                               "detail": "nothing to split off; the shunt would "
                                         "leave a dead net on the load side"})
            continue
        splits[rail] = split_preview(rail, nodes, ref, pin)

    return result(check, len(claims), violations,
                  f"{len(claims)} rail claim(s) checked against the netlist"), splits


# ---------------------------------------------------------------- sizing


def check_uncertainty_budget(rails: List[RailShunt],
                             max_uncertainty_pct: Optional[float]) -> Finding:
    """Is the achievable accuracy good enough for the decision it feeds?

    Separated from "were the terms declared" on purpose. Computing 30% and
    reporting PASS because the arithmetic succeeded is the failure this
    whole tool is built against: the number was produced, nobody said what
    would have been acceptable, and a green light was shown anyway. With no
    budget declared this is NO_COVERAGE - the measurement is planned but
    unjudged.
    """
    check = "uncertainty_within_budget"
    sized = [r for r in rails if r.uncertainty_pct is not None]
    if not sized:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no rail has a computable uncertainty")
    if max_uncertainty_pct is None:
        worst = max(r.uncertainty_pct for r in sized)
        return Finding(check, NO_COVERAGE, len(sized), [],
                       f"achievable uncertainty computed (worst {worst:.1f}%) "
                       f"but --max-uncertainty-pct was not declared, so "
                       f"nothing judges whether it is good enough")
    violations = [
        {"rail": r.rail, "issue": "UNCERTAINTY_OVER_BUDGET",
         "achieved_pct": round(r.uncertainty_pct, 3),
         "budget_pct": max_uncertainty_pct,
         "detail": ("dominated by the parasitic term - Kelvin sensing or a "
                    "larger shunt is the lever"
                    if not r.kelvin and r.parasitic_ohm else
                    "dominated by the meter and resistor tolerance")}
        for r in sized if r.uncertainty_pct > max_uncertainty_pct]
    return result(check, len(sized), violations,
                  f"{len(sized)} rail(s) against a {max_uncertainty_pct}% budget")


def check_load_isolation(rails: List[RailShunt], splits: Dict[str, dict],
                         intended: Dict[str, Tuple[str, str]]) -> Finding:
    """Does the shunt measure the load you meant, or that load plus company?

    A shunt reads everything downstream of it. When the rail also feeds
    on-board parts - a cascaded regulator's input, an oscillator, an
    EEPROM - the reading is their sum, and attributing it to the intended
    load overstates that load by however much the others draw. This is the
    difference between a measurement that answers the question and one that
    merely produces a number.
    """
    check = "load_side_isolated"
    if not intended:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no --rail-intended-load declared; the load side is "
                       "reported but not judged")
    violations = []
    scanned = 0
    for rail_name, (ref, pin) in sorted(intended.items()):
        split = splits.get(rail_name)
        if split is None:
            violations.append({"rail": rail_name, "issue": "NO_SPLIT_AVAILABLE",
                               "detail": "netlist did not confirm this rail, so "
                                         "its load side is unknown"})
            continue
        scanned += 1
        moved = split["moves_to_load_side"]
        others = [n for n in moved
                  if not (n["ref"] == ref and n["pin"] == pin)
                  and not n["ref"].startswith(("C", "TP"))]
        if others:
            violations.append({
                "rail": rail_name, "issue": "LOAD_SIDE_NOT_ISOLATED",
                "intended": f"{ref}:{pin}",
                "also_downstream": sorted({n["ref"] for n in others}),
                "detail": "the reading is the SUM of these; it cannot be "
                          "attributed to the intended load alone"})
    if scanned == 0 and violations:
        return Finding(check, "FAIL", len(intended), violations,
                       "no declared rail could be checked")
    return result(check, scanned, violations,
                  f"{scanned} rail(s) checked for load-side isolation "
                  "(bypass caps and testpoints ignored)")


def evaluate_rails(rails: List[RailShunt]):
    """Turn per-rail reason codes into findings."""
    sizing_violations = []
    placement_violations = []
    accuracy_violations = []
    for rail in rails:
        for code in rail.reason_codes:
            entry = {"rail": rail.rail, "issue": code}
            if code in ("SHUNT_WINDOW_EMPTY", "NO_PREFERRED_VALUE_IN_WINDOW"):
                entry["window"] = rail.window.to_dict()
                entry["detail"] = (
                    "the droop budget and the meter resolution leave no "
                    f"resistance open; binding bound: {rail.window.binding}")
                sizing_violations.append(entry)
            elif code == "SHUNT_DOWNSTREAM_OF_BYPASS":
                entry["detail"] = (
                    "a shunt between the bypass bank and the load puts "
                    "resistance in series with the load's charge reservoir: "
                    "transient current flows through it, which both degrades "
                    "the rail under di/dt and makes the reading noisy. Pass "
                    "--allow-downstream-of-bypass with a stated reason to "
                    "override.")
                placement_violations.append(entry)
            elif code == "PACKAGE_POWER_EXCEEDED":
                entry["power_w"] = rail.power_at_full_scale_w
                entry["rating_w"] = rail.package_power_w
                sizing_violations.append(entry)
            elif code == "PACKAGE_POWER_NOT_DECLARED":
                entry["power_w"] = rail.power_at_full_scale_w
                entry["detail"] = ("ratings for one nominal size span >3x "
                                   "across vendors; --package-power-w is not "
                                   "defaulted")
                accuracy_violations.append(entry)
            else:
                entry["detail"] = (
                    "without Kelvin sensing the pad and trace resistance is "
                    "in series with the element; declare --parasitic-mohm or "
                    "--kelvin, otherwise no accuracy can be claimed")
                accuracy_violations.append(entry)

    findings = [
        result("shunt_value_exists", len(rails), sizing_violations,
               f"{len(rails)} rail(s) sized from droop budget and meter floor"),
        result("shunt_placement_safe", len(rails), placement_violations,
               "shunt position relative to the bypass bank"),
        result("measurement_accuracy_declared", len(rails), accuracy_violations,
               "uncertainty terms present for every sized rail"),
    ]
    return findings


def rollup(findings) -> Tuple[str, List[str]]:
    counts = summarise(findings)
    codes = [f["check"].upper() for f in findings if f["status"] == "FAIL"]
    if counts.get("FAIL"):
        return "RED", codes
    if counts.get("NO_COVERAGE"):
        return "NO_COVERAGE", [f["check"].upper() for f in findings
                               if f["status"] == NO_COVERAGE]
    return "GREEN", []


# ---------------------------------------------------------------- CLI


def _pairs(values: Optional[List[str]], what: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"{what} must be NAME=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def parse_rail_specs(values: List[str]) -> Dict[str, Tuple[str, str]]:
    claims: Dict[str, Tuple[str, str]] = {}
    for rail, target in _pairs(values, "--rail").items():
        if ":" not in target:
            raise ValueError(f"--rail must be NAME=REF:PIN, got {rail}={target!r}")
        ref, _, pin = target.partition(":")
        if not rail or not ref.strip() or not pin.strip():
            raise ValueError(f"--rail NAME=REF:PIN has an empty field: {rail}={target!r}")
        claims[rail] = (ref.strip(), pin.strip())
    if not claims:
        raise ValueError("at least one --rail NAME=REF:PIN is required")
    return claims


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan series shunts + testpoints for non-destructive "
                    "per-rail current measurement.")
    parser.add_argument("--rail", action="append", default=[],
                        metavar="NAME=REF:PIN",
                        help="rail net name and the regulator pin that drives it")
    parser.add_argument("--netlist", type=Path,
                        help="KiCad s-expression netlist; without it the "
                             "topology check reports NO_COVERAGE")
    parser.add_argument("--full-scale-current-a", type=float,
                        help="regulator's RATED output current (datasheet). "
                             "Not the expected draw - that is what the "
                             "measurement is for.")
    parser.add_argument("--rail-full-scale-current-a", action="append", default=[],
                        metavar="NAME=AMPS", help="per-rail override")
    parser.add_argument("--max-droop-mv", type=float,
                        help="droop budget across the shunt at full scale")
    parser.add_argument("--rail-max-droop-mv", action="append", default=[],
                        metavar="NAME=MILLIVOLTS", help="per-rail override")
    parser.add_argument("--meter-resolution-uv", type=float,
                        help="smallest step the meter DISPLAYS on the range used")
    parser.add_argument("--current-resolution-ma", type=float,
                        help="how finely the current must be resolved")
    parser.add_argument("--position", choices=POSITIONS,
                        default=UPSTREAM_OF_BYPASS,
                        help="where the shunt sits relative to the bypass bank")
    parser.add_argument("--allow-downstream-of-bypass", action="store_true",
                        help="override the placement rule; requires --reason")
    parser.add_argument("--reason", default="",
                        help="why the placement rule is being overridden")
    parser.add_argument("--series", default="E24", choices=("E24", "E96"))
    parser.add_argument("--package-power-w", type=float,
                        help="the chosen part's power rating; never defaulted")
    parser.add_argument("--resistor-tolerance-pct", type=float, default=1.0)
    parser.add_argument("--meter-accuracy-pct", type=float, default=0.5)
    parser.add_argument("--parasitic-mohm", type=float,
                        help="pad + trace resistance in series with the element")
    parser.add_argument("--kelvin", action="store_true",
                        help="4-terminal sensing; drops the parasitic term")
    parser.add_argument("--max-uncertainty-pct", type=float,
                        help="accuracy budget for the measured current; "
                             "without it the achievable figure is reported "
                             "but NOT judged (NO_COVERAGE)")
    parser.add_argument("--rail-intended-load", action="append", default=[],
                        metavar="NAME=REF:PIN",
                        help="the one load the shunt is meant to measure; "
                             "anything else downstream is flagged")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def _require(args, names: List[str]) -> None:
    missing = [n for n in names if getattr(args, n.replace("-", "_")) is None]
    if missing:
        raise ValueError("missing required argument(s): "
                         + ", ".join("--" + n for n in missing))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    if args.allow_downstream_of_bypass and not args.reason.strip():
        raise ValueError("--allow-downstream-of-bypass requires --reason: an "
                         "override with no stated reason is indistinguishable "
                         "from a mistake six months later")

    claims = parse_rail_specs(args.rail)
    _require(args, ["full-scale-current-a", "max-droop-mv",
                    "meter-resolution-uv", "current-resolution-ma"])

    fs_overrides = {k: float(v) for k, v in
                    _pairs(args.rail_full_scale_current_a,
                           "--rail-full-scale-current-a").items()}
    droop_overrides = {k: float(v) for k, v in
                       _pairs(args.rail_max_droop_mv,
                              "--rail-max-droop-mv").items()}
    unknown = (set(fs_overrides) | set(droop_overrides)) - set(claims)
    if unknown:
        raise ValueError(f"override names no declared rail: {sorted(unknown)}")

    nets = read_netlist_nets(args.netlist) if args.netlist else None
    netlist_finding, splits = verify_rail_claims(claims, nets)

    parasitic_ohm = (args.parasitic_mohm / 1000.0
                     if args.parasitic_mohm is not None else None)

    rails: List[RailShunt] = []
    for rail, (ref, pin) in sorted(claims.items()):
        rails.append(size_rail(
            rail=rail, regulator_ref=ref, regulator_pin=pin,
            full_scale_current_a=fs_overrides.get(rail, args.full_scale_current_a),
            max_droop_v=droop_overrides.get(rail, args.max_droop_mv) / 1000.0,
            meter_resolution_v=args.meter_resolution_uv / 1e6,
            current_resolution_a=args.current_resolution_ma / 1000.0,
            position=args.position, series=args.series,
            package_power_w=args.package_power_w,
            resistor_tolerance_pct=args.resistor_tolerance_pct,
            meter_accuracy_pct=args.meter_accuracy_pct,
            parasitic_ohm=parasitic_ohm, kelvin=args.kelvin,
            allow_downstream=args.allow_downstream_of_bypass))

    intended = parse_rail_specs(args.rail_intended_load) \
        if args.rail_intended_load else {}
    stray = set(intended) - set(claims)
    if stray:
        raise ValueError(f"--rail-intended-load names no declared rail: {sorted(stray)}")

    findings = ([netlist_finding] + evaluate_rails(rails)
                + [check_uncertainty_budget(rails, args.max_uncertainty_pct),
                   check_load_isolation(rails, splits, intended)])
    status, reason_codes = rollup(findings)

    injections = []
    for rail in rails:
        src_tp, load_tp = testpoint_names(rail.rail)
        injections.append({
            "rail": rail.rail,
            "shunt_ref_hint": f"R_SHUNT_{rail.rail}",
            "shunt_ohm": rail.shunt_ohm,
            "series_between": {
                "source": {"ref": rail.regulator_ref, "pin": rail.regulator_pin},
                "load_net": sense_net_name(rail.rail),
            },
            "testpoints": [
                {"name": src_tp, "net": rail.rail,
                 "side": "source", "phase": "early_schematic"},
                {"name": load_tp, "net": sense_net_name(rail.rail),
                 "side": "load", "phase": "early_schematic"},
            ],
            "net_split": splits.get(rail.rail),
            "layout_constraints": [
                "Both testpoints must land on the shunt's own pads. A probe "
                "node placed anywhere else on the net measures the copper "
                "between them as part of the element.",
                "Route the pair as a differential sense pair away from "
                "switching nodes: the reading is millivolts.",
            ],
        })

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "early_schematic",
        "mutates_schematic": False,
        "meter": {
            "resolution_uv": args.meter_resolution_uv,
            "accuracy_pct": args.meter_accuracy_pct,
        },
        "requested_current_resolution_ma": args.current_resolution_ma,
        "max_uncertainty_pct": args.max_uncertainty_pct,
        "position": args.position,
        "downstream_override": {
            "allowed": bool(args.allow_downstream_of_bypass),
            "reason": args.reason,
        },
        "rails": [rail.to_dict() for rail in rails],
        "injections": injections,
        "findings": findings,
        "coverage": summarise(findings),
    }

    diag("")
    diag(f"shunt plan: {len(rails)} rail(s), position {args.position}")
    for rail in rails:
        if rail.shunt_ohm is None:
            diag(f"  RED  {rail.rail:14s} no value in "
                 f"[{rail.window.r_min_ohm:.4g}, {rail.window.r_max_ohm:.4g}] ohm "
                 f"(binding: {rail.window.binding})")
            continue
        unc = rail.uncertainty_pct
        unc_text = f"{unc:.1f}%" if unc is not None else "unknown"
        diag(f"  ok   {rail.rail:14s} {rail.shunt_ohm:.4g} ohm  "
             f"droop {rail.droop_at_full_scale_v * 1000:.1f} mV  "
             f"res {rail.achieved_current_resolution_a * 1000:.3f} mA  "
             f"P {rail.power_at_full_scale_w * 1000:.2f} mW  "
             f"uncertainty {unc_text}")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


# ---------------------------------------------------------------- self-test


def _self_test() -> int:
    """Proves the two bounds really close on each other, and that the
    netlist check really rejects a claim the netlist does not support."""
    from common.shunt import shunt_window, select_shunt

    # A window that exists.
    window = shunt_window(full_scale_current_a=0.3, max_droop_v=0.012,
                          meter_resolution_v=10e-6, current_resolution_a=1e-3)
    assert not window.is_empty, window
    assert abs(window.r_max_ohm - 0.04) < 1e-12, window.r_max_ohm
    assert abs(window.r_min_ohm - 0.01) < 1e-12, window.r_min_ohm
    chosen = select_shunt(window, "E24")
    assert chosen is not None and window.r_min_ohm <= chosen <= window.r_max_ohm

    # Tighten the droop budget until the bounds cross: the answer must be
    # "no value", never the nearest value.
    tight = shunt_window(full_scale_current_a=0.3, max_droop_v=0.001,
                         meter_resolution_v=10e-6, current_resolution_a=1e-3)
    assert tight.is_empty, tight
    assert select_shunt(tight, "E24") is None

    # A window that is non-empty but contains no preferred value.
    narrow = shunt_window(full_scale_current_a=1.0, max_droop_v=0.01021,
                          meter_resolution_v=10.2e-6, current_resolution_a=1e-3)
    assert not narrow.is_empty, narrow
    assert select_shunt(narrow, "E24") is None, "E24 has no value in this window"

    # Netlist verification rejects a pin that is not on the net.
    nets = {"VDD": [("U1", "5"), ("C1", "1")]}
    finding, splits = verify_rail_claims({"VDD": ("U9", "5")}, nets)
    assert finding["status"] == "FAIL", finding
    assert splits == {}
    good, splits = verify_rail_claims({"VDD": ("U1", "5")}, nets)
    assert good["status"] == "PASS", good
    assert splits["VDD"]["moved_node_count"] == 1, splits

    # No netlist is NO_COVERAGE, never a silent PASS.
    absent, _ = verify_rail_claims({"VDD": ("U1", "5")}, None)
    assert absent["status"] == NO_COVERAGE, absent

    # Accuracy is withheld when the parasitic term is undeclared.
    rail = size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                     UPSTREAM_OF_BYPASS)
    assert rail.uncertainty_pct is None
    assert "NON_KELVIN_PARASITIC_NOT_DECLARED" in rail.reason_codes

    # Downstream placement is refused unless overridden.
    down = size_rail("VDD", "U1", "5", 0.3, 0.012, 10e-6, 1e-3,
                     DOWNSTREAM_OF_BYPASS, kelvin=True)
    assert "SHUNT_DOWNSTREAM_OF_BYPASS" in down.reason_codes

    diag("SELF-TEST PASS: empty window refused, no-preferred-value refused, "
         "netlist claim verified and rejected, accuracy withheld without its "
         "terms, downstream placement refused")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
