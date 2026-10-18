#!/usr/bin/env python3
"""Power-up sequencing and per-rail noise budgets.

WHAT NO OTHER GATE IN THIS BUNDLE CAN SEE
--------------------------------------------------------------------------
ERC proves an enable pin is CONNECTED. It cannot prove the thing connected
to it can turn the regulator on, because that needs a datasheet threshold
and a rail voltage, and a netlist carries neither. DRC has even less to say.
So a board can be ERC-clean, DRC-clean, and still have a regulator whose
enable sits a hundred millivolts above its own V_EN(HI) at room temperature
and below it at -40 C.

The second thing nothing checks: a written power-up order that the enable
wiring does not implement. The document is what the next engineer reads,
and it drifts from the hardware silently.

Three inputs are REQUIRED and never defaulted: each regulator's V_EN(HI),
the frequency a PSRR figure was quoted at, and the noise budget itself.
Each is a number this tool could invent and an engineer would later believe.

Usage:
    rail_budget.py --rails rails.json [--declared-order "V1V8;V1V2,V2V8"]
                   [--min-enable-margin-v 0.3] [--json rail_budget.json]
    rail_budget.py --self-test

``rails.json``:
    {"rails": [
       {"name": "VIN_3V3", "voltage_v": 3.3},
       {"name": "V1V8", "voltage_v": 1.8, "enable_source": "VIN_3V3",
        "en_threshold_v": 1.1},
       {"name": "V1V2", "voltage_v": 1.2, "enable_source": "V1V8",
        "en_threshold_v": 1.1}],
     "noise": [
       {"rail": "V1V2", "budget_v": 0.012,
        "contributors": [
          {"name": "regulator output noise", "volts": 7e-6, "kind": "random"},
          {"name": "input ripple", "volts": 0.05, "kind": "deterministic",
           "psrr_db": 60, "at_hz": 1000, "ripple_hz": 1000}]}]}

A contributor carrying ``psrr_db`` is attenuated by it; ``at_hz`` and
``ripple_hz`` must agree, because PSRR is a curve and carrying a figure
across a frequency gap is optimistic in the direction that ships.

Exit codes: 0 = GREEN, 1 = RED or NO_COVERAGE, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, NO_COVERAGE, result, summarise  # noqa: E402
from common.railbudget import (ALWAYS_ON, EN_THRESHOLD_UNDECLARED,  # noqa: E402
                               NO_BUDGET, NoiseContributor, Rail,
                               attenuate_by_psrr, check_enable_margins,
                               check_noise_budget, compare_declared_order,
                               enable_order)
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "rail-budget/v1"
TOOL_NAME = "rail_budget"


def load_rails(doc: dict) -> List[Rail]:
    entries = doc.get("rails")
    if not isinstance(entries, list) or not entries:
        raise ValueError("'rails' must be a non-empty array")
    rails = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValueError("every rail needs a 'name'")
        name = str(entry["name"])
        if name in seen:
            raise ValueError(f"duplicate rail {name!r}")
        seen.add(name)
        if "voltage_v" not in entry:
            raise ValueError(f"{name}: 'voltage_v' is required")
        rails.append(Rail(
            name=name, voltage_v=float(entry["voltage_v"]),
            enable_source=str(entry.get("enable_source", ALWAYS_ON)),
            en_threshold_v=(None if entry.get("en_threshold_v") is None
                            else float(entry["en_threshold_v"]))))
    return rails


def build_contributors(entries: List[dict]) -> List[NoiseContributor]:
    """Turn declared contributors into terms, applying PSRR where given."""
    out: List[NoiseContributor] = []
    for entry in entries:
        volts = float(entry["volts"])
        if entry.get("psrr_db") is not None:
            volts = attenuate_by_psrr(
                volts, float(entry["psrr_db"]),
                ripple_hz=float(entry["ripple_hz"]),
                psrr_at_hz=float(entry["at_hz"]))
        out.append(NoiseContributor(name=str(entry["name"]), volts=volts,
                                    kind=str(entry.get("kind", "deterministic"))))
    return out


def parse_declared_order(text: Optional[str]) -> Optional[List[List[str]]]:
    """``"V1V8;V1V2,V2V8"`` -> ``[["V1V8"], ["V1V2", "V2V8"]]``.

    Semicolons order, commas group. Rails inside one group come up together
    and the notation deliberately cannot say otherwise - a written order
    finer than the hardware enforces is a claim nothing can check.
    """
    if not text:
        return None
    groups = []
    for chunk in text.split(";"):
        names = [n.strip() for n in chunk.split(",") if n.strip()]
        if not names:
            raise ValueError(f"empty group in --declared-order: {text!r}")
        groups.append(names)
    if not groups:
        raise ValueError("--declared-order is empty")
    return groups


def enable_finding(checks, min_margin_v: Optional[float]) -> Finding:
    check = "enable_threshold_margin"
    if not checks:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no rail declares an enable source; every rail is "
                       "always-on and nothing sequences")
    undeclared = [c for c in checks if EN_THRESHOLD_UNDECLARED in c.codes]
    violations = [c.to_dict() for c in checks
                  if c.codes and EN_THRESHOLD_UNDECLARED not in c.codes]
    if violations:
        return result(check, len(checks), violations,
                      f"{len(checks)} enable path(s) against declared thresholds")
    if undeclared:
        return Finding(check, NO_COVERAGE, len(checks) - len(undeclared), [],
                       f"{len(undeclared)} regulator(s) declare no V_EN(HI); "
                       "the threshold is a datasheet number and is not "
                       "defaulted, so those paths are unjudged")
    detail = f"{len(checks)} enable path(s) clear their thresholds"
    if min_margin_v is None:
        detail += " (no --min-enable-margin-v given, so only sign was checked)"
    return result(check, len(checks), [], detail)


def order_finding(rails, declared) -> Finding:
    check = "declared_order_matches_wiring"
    if declared is None:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no --declared-order given; the enable graph was "
                       "computed but nothing was compared against it")
    violations = compare_declared_order(rails, declared)
    return result(check, len(rails), violations,
                  f"{len(rails)} rail(s): document against enable graph")


def noise_finding(results) -> Finding:
    check = "noise_within_budget"
    if not results:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no rail declares a noise budget")
    unbudgeted = [r for r in results if NO_BUDGET in r.codes]
    violations = [r.to_dict() for r in results
                  if r.codes and NO_BUDGET not in r.codes]
    if violations:
        return result(check, len(results), violations,
                      f"{len(results)} rail(s) against declared budgets")
    if unbudgeted:
        return Finding(check, NO_COVERAGE, len(results) - len(unbudgeted), [],
                       f"{len(unbudgeted)} rail(s) have a computed total but no "
                       "declared budget; the sum is reported, not judged")
    return result(check, len(results), [],
                  f"all {len(results)} rail(s) fit inside their budgets")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check power-up sequencing and per-rail noise budgets.")
    parser.add_argument("--rails", type=Path, help="rail description document")
    parser.add_argument("--declared-order",
                        help='written power-up order, e.g. "V1V8;V1V2,V2V8" '
                             "(semicolons order, commas group)")
    parser.add_argument("--min-enable-margin-v", type=float,
                        help="required headroom over V_EN(HI); without it only "
                             "the SIGN of the margin is checked")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.rails:
        raise ValueError("--rails is required")

    doc = json.loads(args.rails.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{args.rails}: document must be an object")
    rails = load_rails(doc)
    declared = parse_declared_order(args.declared_order)

    checks = check_enable_margins(rails, args.min_enable_margin_v)
    tiers = enable_order(rails)

    noise_results = []
    for entry in doc.get("noise", []) or []:
        contributors = build_contributors(entry.get("contributors", []))
        budget = entry.get("budget_v")
        noise_results.append(check_noise_budget(
            str(entry["rail"]), contributors,
            None if budget is None else float(budget)))

    findings = [enable_finding(checks, args.min_enable_margin_v),
                order_finding(rails, declared),
                noise_finding(noise_results)]
    counts = summarise(findings)
    if counts.get("FAIL"):
        status = "RED"
        reason_codes = [f["check"].upper() for f in findings
                        if f["status"] == "FAIL"]
    elif counts.get(NO_COVERAGE):
        status = "NO_COVERAGE"
        reason_codes = [f["check"].upper() for f in findings
                        if f["status"] == NO_COVERAGE]
    else:
        status = "GREEN"
        reason_codes = []

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "early_schematic",
        "rails": [r.to_dict() for r in rails],
        "enable_tiers": tiers,
        "declared_order": declared,
        "enable_checks": [c.to_dict() for c in checks],
        "noise": [r.to_dict() for r in noise_results],
        "findings": findings,
        "coverage": summarise(findings),
    }

    diag("")
    diag(f"rail budget: {len(rails)} rail(s), {len(tiers)} enable tier(s)")
    for index, group in enumerate(tiers):
        diag(f"  tier {index}: {', '.join(group)}")
    for check in checks:
        if check.margin_v is None:
            diag(f"  ?    {check.rail:12s} enable margin unjudged "
                 f"({', '.join(check.codes)})")
        else:
            mark = "RED " if check.codes else "ok  "
            diag(f"  {mark} {check.rail:12s} EN from {check.source} -> "
                 f"margin {check.margin_v * 1000:+.0f} mV")
    for entry in noise_results:
        head = entry.headroom_v
        head_text = "no budget" if head is None else f"headroom {head * 1e3:+.3f} mV"
        diag(f"  {'RED ' if entry.codes and NO_BUDGET not in entry.codes else 'ok  '}"
             f" {entry.rail:12s} total {entry.summary['total_v'] * 1e3:.3f} mV, "
             f"{head_text}")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


def _self_test() -> int:
    """Drives each check into the case a permissive version would pass."""
    rails = [Rail("VIN", 3.3),
             Rail("V1V8", 1.8, "VIN", 1.1),
             Rail("V1V2", 1.2, "V1V8", 1.1)]
    tiers = enable_order(rails)
    assert tiers == [["VIN"], ["V1V8"], ["V1V2"]], tiers

    # An enable driven by a rail below its own threshold: connected, ERC
    # silent, and the regulator never turns on.
    bad = [Rail("VIN", 3.3),
           Rail("V1V2", 1.2, "VIN", 1.1),      # 2.2 V of margin: comfortable
           Rail("V2V8", 2.8, "V1V2", 1.4)]     # 1.2 V driving a 1.4 V gate
    codes = [c for c in check_enable_margins(bad) if c.rail == "V2V8"][0].codes
    assert "ENABLE_BELOW_THRESHOLD" in codes, codes

    # A margin that EXISTS but is thin is a different finding. This is the
    # case that passes at room temperature and fails over the corner.
    marginal = [Rail("V1V8", 1.8), Rail("V1V2", 1.2, "V1V8", 1.6)]
    thin = check_enable_margins(marginal, min_margin_v=0.5)[0]
    assert "ENABLE_MARGIN_THIN" in thin.codes, thin.codes
    assert abs(thin.margin_v - 0.2) < 1e-12, thin.margin_v
    # ...and with no budget declared, the same path reports nothing.
    quiet = check_enable_margins(marginal)[0]
    assert quiet.codes == [], quiet.codes

    # An undeclared threshold is reported, never assumed.
    unknown = check_enable_margins([Rail("VIN", 3.3), Rail("V", 1.0, "VIN")])
    assert EN_THRESHOLD_UNDECLARED in unknown[0].codes

    # A cycle is refused; a topological sort that dropped it would report a
    # shorter, plausible order.
    try:
        enable_order([Rail("A", 1.0, "B", 0.5), Rail("B", 1.0, "A", 0.5)])
    except ValueError as exc:
        assert "ENABLE_CYCLE" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an enable cycle produced an order")

    # A document that inverts the wiring is caught.
    violations = compare_declared_order(rails, [["V1V2"], ["V1V8"], ["VIN"]])
    assert violations, "an inverted declared order passed"

    # Deterministic and random terms are not summed the same way.
    det = [NoiseContributor("a", 0.003), NoiseContributor("b", 0.004)]
    rnd = [NoiseContributor("a", 0.003, "random"),
           NoiseContributor("b", 0.004, "random")]
    total_det = check_noise_budget("R", det, None).summary["total_v"]
    total_rnd = check_noise_budget("R", rnd, None).summary["total_v"]
    assert abs(total_det - 0.007) < 1e-12, total_det
    assert abs(total_rnd - 0.005) < 1e-12, total_rnd

    # PSRR quoted at another frequency is refused rather than carried across.
    try:
        attenuate_by_psrr(0.05, 60.0, ripple_hz=2e6, psrr_at_hz=1e3)
    except ValueError as exc:
        assert "PSRR" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a PSRR figure was carried across a frequency gap")

    # No budget is NO_BUDGET, and the sum is still produced.
    unjudged = check_noise_budget("R", det, None)
    assert NO_BUDGET in unjudged.codes and unjudged.summary["total_v"] > 0

    diag("SELF-TEST PASS: enable below threshold caught, thin margin "
         "distinguished, undeclared threshold reported, enable cycle refused, "
         "inverted document caught, deterministic and random summed by "
         "different rules, PSRR frequency gap refused")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
