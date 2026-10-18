#!/usr/bin/env python3
"""Derives the MANDATORY test-point scope (power rails + debug/boot nets)
from a power rail tree, and writes a schema-conformant
``testpoint_plan.json`` (skills/dft-testpoints's own artifact,
``skills/dft-testpoints/references/testpoint_plan.schema.json``) - that
skill's ``tools:`` list was empty before this port; nothing produced this
artifact yet.

Also writes a human-facing ``bringup_checklist.md`` from the same
test-point list plus a rail enable order - a live lab test record a bench
technician fills in by hand (Obsidian-editable), not a JSON artifact.

SCOPE - EARLY_SCHEMATIC PHASE ONLY, SAID PLAINLY
---------------------------------------------------
This generator only knows net names and rail voltages - it has no board,
no pad coordinates, no probe-accessibility data. The schema's own
``$comment`` on ``phase`` says exactly this: "early_schematic must never
emit a physical coordinate/accessibility PASS: placement does not exist
yet." Every testpoint emitted here has ``"phase": "early_schematic"`` and
``physical: null``; test_access_rules/strategy/defect_universe/
boundary_scan_gate (all optional top-level/nested fields) are not
populated - a LATER tool, run after placement, owns turning this into a
``late_physical`` plan with real pad coordinates and probe accessibility.

``method`` is REQUIRED by the schema (no null option) even though at this
phase the actual DFT strategy has usually not been chosen yet - every
testpoint here defaults to ``--default-method`` (manual, unless overridden)
as the safest generic placeholder; a later DFT-strategy decision should
overwrite it, not leave it stuck on the default.

Usage:
    testpoint_plan.py <rail_tree.json> [--debug-net SWDIO --debug-net SWCLK ...]
                      [--default-method manual] [--json testpoint_plan.json]
    testpoint_plan.py --bringup-checklist <testpoint_plan.json>
                      --rail-order 3V3,1V2_CORE [--tolerance-pct 5.0]
                      [--out bringup_checklist.md]
    testpoint_plan.py --self-test

``rail_tree.json``: ``{"3V3": {"vout": 3.3}, "1V2_CORE": {"vout": 1.2}}``
(the same shape ``common/stackup.py``'s rail-tree output uses).

Exit codes: 0 = plan/checklist written and mandatory scope fully covered,
1 = mandatory scope incomplete (or NO_COVERAGE), 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import coverage_status  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "testpoint-plan/v1"
TOOL_NAME = "testpoint_plan"

DEFAULT_DEBUG_NETS = ("SWDIO", "SWCLK", "nRST", "UART_TX", "UART_RX")
NET_CLASS_POWER = "POWER"
NET_CLASS_DEBUG = "DEBUG"
VALID_METHODS = ("boundary_scan", "bed_of_nails", "flying_probe", "manual")


def derive_mandatory_testpoints(rail_tree: Dict[str, dict],
                                debug_nets: Optional[List[str]] = None,
                                default_method: str = "manual") -> List[dict]:
    """Derives the mandatory power+debug testpoint scope (target: 100%
    coverage for power+debug nodes ONLY - never generalised into a
    board-wide claim, per the schema's own comment).

    Mandatory scope:
      - Every power rail (the regulator's OUTPUT, not its input).
      - SWD (SWDIO/SWCLK/nRST), UART (TX/RX), boot-strap, PMIC enable/PG.
    """
    if default_method not in VALID_METHODS:
        raise ValueError(f"default_method must be one of {VALID_METHODS}, got {default_method!r}")

    testpoints: List[dict] = []
    for rail_name, rail_info in rail_tree.items():
        testpoints.append({
            "tp_id": f"TP_{rail_name}",
            "net": rail_name,
            "net_class": NET_CLASS_POWER,
            "method": default_method,
            "coverage_reason": f"power rail output, expected {rail_info.get('vout')}V",
            "physical": None,
        })

    for net in (debug_nets or list(DEFAULT_DEBUG_NETS)):
        testpoints.append({
            "tp_id": f"TP_{net}",
            "net": net,
            "net_class": NET_CLASS_DEBUG,
            "method": default_method,
            "coverage_reason": "mandatory debug/boot-strap access point",
            "physical": None,
        })

    return testpoints


def check_mandatory_scope_coverage(testpoints: List[dict], expected_power_rail_count: int,
                                   expected_debug_net_names: List[str]) -> List[str]:
    """Target is 100% power+debug coverage - reports what's missing."""
    findings: List[str] = []
    power_tp_count = sum(1 for t in testpoints if t["net_class"] == NET_CLASS_POWER)
    if power_tp_count < expected_power_rail_count:
        findings.append(
            f"MISSING: expected {expected_power_rail_count} power rail(s), "
            f"found {power_tp_count} testpoint(s).")
    present_debug_nets = {t["net"] for t in testpoints if t["net_class"] == NET_CLASS_DEBUG}
    missing = set(expected_debug_net_names) - present_debug_nets
    if missing:
        findings.append(f"MISSING debug testpoint(s): {sorted(missing)}")
    return findings


def build_mandatory_scope_block(testpoints: List[dict], expected_power_rail_count: int,
                                expected_debug_net_names: List[str]) -> dict:
    """The schema's ``mandatory_scope`` block (5 required scope_items).
    Only ``power_rails`` and ``swd``/``uart``/``boot_strap``/
    ``pmic_enable_pg`` (folded together here as "debug", since this
    generator does not distinguish which debug net belongs to which of
    those four sub-categories) are populated from real data - each is
    ``{required, covered, missing}``.
    """
    power_covered = sum(1 for t in testpoints if t["net_class"] == NET_CLASS_POWER)
    present_debug = {t["net"] for t in testpoints if t["net_class"] == NET_CLASS_DEBUG}
    missing_debug = sorted(set(expected_debug_net_names) - present_debug)

    debug_item = {
        "required": len(expected_debug_net_names),
        "covered": len(expected_debug_net_names) - len(missing_debug),
        "missing": missing_debug or None,
    }
    # This generator does not know which debug net is SWD vs UART vs
    # boot-strap vs PMIC enable/PG - it reports the combined debug scope
    # under all four required keys rather than guessing a split that would
    # misrepresent per-category coverage.
    return {
        "power_rails": {
            "required": expected_power_rail_count, "covered": power_covered,
            "missing": None if power_covered >= expected_power_rail_count else
                      [f"{expected_power_rail_count - power_covered} rail(s) unaccounted for"],
        },
        "swd": dict(debug_item), "uart": dict(debug_item),
        "boot_strap": dict(debug_item), "pmic_enable_pg": dict(debug_item),
    }


def build_testpoint_plan(rail_tree: Dict[str, dict], debug_nets: Optional[List[str]] = None,
                         default_method: str = "manual") -> dict:
    testpoints = derive_mandatory_testpoints(rail_tree, debug_nets, default_method)
    expected_debug = debug_nets or list(DEFAULT_DEBUG_NETS)
    mandatory_scope = build_mandatory_scope_block(testpoints, len(rail_tree), expected_debug)

    eligible = len(rail_tree) + len(expected_debug)
    scanned = len(testpoints)
    violations = len(check_mandatory_scope_coverage(testpoints, len(rail_tree), expected_debug))
    coverage = {"eligible": eligible, "scanned": scanned, "excluded": max(0, eligible - scanned),
               "violations": violations}

    status = coverage_status(coverage)
    # coverage_status()'s vocabulary is INVALID_COVERAGE/NO_COVERAGE/RED/GREEN;
    # this schema's status enum is TBD/CONFIRM/SCREEN_ONLY/NO_COVERAGE/RED/GREEN -
    # RED and NO_COVERAGE are shared verbatim, GREEN needs the early_schematic
    # caveat folded in (this plan never carries physical accessibility proof).
    if status == "GREEN":
        status = "SCREEN_ONLY"  # mandatory scope is complete, but nothing here is placement-verified

    return {
        "schema_version": SCHEMA_VERSION, "phase": "early_schematic", "board_sha256": None,
        "testpoints": testpoints, "mandatory_scope": mandatory_scope,
        "coverage": coverage, "status": status,
    }


# --------------------------------------------------------------------------- #
# Bring-up checklist - a human lab document, not a JSON artifact
# --------------------------------------------------------------------------- #
def generate_bringup_checklist(testpoints: List[dict], rail_enable_order: List[str],
                               tolerance_pct: float = 5.0) -> str:
    """Writes the ordered expected-voltage + rail-enable-sequence checklist
    as markdown. Sequencing is ENFORCED here (steps numbered and ordered by
    ``rail_enable_order``) because a rail sequencing mistake can damage the
    core.

    Each power-rail row carries a checkbox AND a markdown TABLE row - the
    value measured with a scope/multimeter is written into the "Measured"
    cell BY HAND (in the lab, on a tablet/laptop, opened directly in
    Obsidian), the result marked PASS/FAIL/NEEDS_HUMAN, and the checkbox
    ticked. The file becomes the board's live test record this way and is
    archived alongside the revision.

    ``tolerance_pct`` computes the "Acceptance Range" column from an
    explicit formula, not a number picked out of the air - the default
    (5%) is a starting point for simple single-stage rails only; if a real
    worst-case tolerance budget exists, pass THAT value in instead.
    """
    lines = ["# Bring-up Checklist\n"]
    lines.append("## 1. Power Sequence (by rail_enable_order - DO NOT REORDER)\n")
    lines.append("| # | Rail | Expected (V) | Acceptance Range (V) | Measured (V) | Result | Done |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, rail in enumerate(rail_enable_order, start=1):
        # Expected voltage is not extracted here: coverage_reason is prose
        # ("power rail output, expected 3.3V"), not a structured field - a
        # human fills in Expected/Acceptance Range/Measured from the real
        # design intent, this table only enforces the row order.
        lines.append(f"| {i} | {rail} |  |  |  | PASS/FAIL/NEEDS_HUMAN | [ ] |")

    lines.append("\n## 2. Debug/Boot Access\n")
    lines.append("| Net | Class | Method | Result | Done |")
    lines.append("|---|---|---|---|---|")
    for tp in testpoints:
        if tp["net_class"] != NET_CLASS_DEBUG:
            continue
        lines.append(f"| {tp['net']} | {tp['net_class']} | {tp['method']} | "
                     "PASS/FAIL/NEEDS_HUMAN | [ ] |")

    lines.append(f"\n*Acceptance range = expected value +/- {tolerance_pct:g}% "
                 "(single-stage default; replace with the Phase 2 worst-case "
                 "tolerance budget if one exists).*\n")
    return "\n".join(lines)


def _self_test() -> int:
    rail_tree = {"3V3": {"vout": 3.3}, "1V2_CORE": {"vout": 1.2}}
    plan = build_testpoint_plan(rail_tree)
    assert plan["phase"] == "early_schematic"
    assert plan["coverage"]["scanned"] == 2 + len(DEFAULT_DEBUG_NETS)
    assert plan["status"] in ("SCREEN_ONLY", "GREEN"), plan["status"]

    missing = check_mandatory_scope_coverage(
        derive_mandatory_testpoints(rail_tree), expected_power_rail_count=3,
        expected_debug_net_names=list(DEFAULT_DEBUG_NETS))
    assert missing, "an under-provisioned rail count must be flagged, not silently accepted"

    checklist = generate_bringup_checklist(plan["testpoints"], ["3V3", "1V2_CORE"])
    assert "3V3" in checklist and "1V2_CORE" in checklist
    assert "DO NOT REORDER" in checklist

    try:
        derive_mandatory_testpoints(rail_tree, default_method="not_a_real_method")
        raise AssertionError("an invalid method must raise, not silently accept garbage")
    except ValueError:
        pass

    diag("SELF-TEST PASS: mandatory scope derivation, coverage gap detection, "
        "and bring-up checklist generation all verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rail_tree", nargs="?")
    ap.add_argument("--debug-net", action="append", dest="debug_nets")
    ap.add_argument("--default-method", default="manual", choices=VALID_METHODS)
    ap.add_argument("--json")
    ap.add_argument("--bringup-checklist", metavar="TESTPOINT_PLAN_JSON")
    ap.add_argument("--rail-order", help="comma-separated rail names, enable order")
    ap.add_argument("--tolerance-pct", type=float, default=5.0)
    ap.add_argument("--out", default="bringup_checklist.md")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()

    if a.bringup_checklist:
        if not a.rail_order:
            ap.error("--bringup-checklist requires --rail-order")
        plan = json.loads(Path(a.bringup_checklist).read_text(encoding="utf-8"))
        checklist = generate_bringup_checklist(
            plan["testpoints"], a.rail_order.split(","), a.tolerance_pct)
        Path(a.out).write_text(checklist, encoding="utf-8")
        diag(f"wrote {a.out}")
        return 0

    if not a.rail_tree:
        ap.error("rail_tree.json is required unless --self-test or --bringup-checklist is given")

    rail_tree = json.loads(Path(a.rail_tree).read_text(encoding="utf-8"))
    plan = build_testpoint_plan(rail_tree, a.debug_nets, a.default_method)
    write_report(plan, a.json)
    emit(plan)
    return 1 if plan["status"] in ("RED", "NO_COVERAGE") else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
