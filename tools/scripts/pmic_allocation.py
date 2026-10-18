#!/usr/bin/env python3
"""Checks whether a PMIC's output rails can actually cover the board's real
power needs - closes the gap between "how many outputs does it have"
(BOM/datasheet level) and "do these OUTPUTS actually satisfy THIS PROJECT's
rails" (project-specific allocation). Uses a simple greedy match: each rail
is assigned to the PMIC output with the closest voltage AND enough
remaining current headroom.

Integrates with ``common.stackup.MechanicalThermalLimits``: if a rail
cannot be allocated (a supplementary regulator would be needed), that
regulator's ESTIMATED contribution to the thermal budget
(``max_dissipation_w``) is also reported.

Usage:
    pmic_allocation.py <intent.json> [--json out.json]
    pmic_allocation.py --self-test

``intent.json``: ``{"pmic": {"name": "...", "outputs": [{"name", "kind",
"max_current_ma", "nominal_voltage_v"}, ...]}, "rails": [{"rail_name",
"voltage_v", "estimated_current_ma"}, ...], "thermal_limits":
{"max_dissipation_w": 1.0} (optional)}``

Exit codes: 0 = every rail allocated, 1 = at least one rail unallocated or
an output overloaded (or NO_COVERAGE), 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import result  # noqa: E402
from common.report_io import emit, run_cli, write_report  # noqa: E402
from common.stackup import MechanicalThermalLimits  # noqa: E402

SCHEMA_VERSION = "pmic-allocation-report/v1"
TOOL_NAME = "pmic_allocation"

UNALLOCATED = "UNALLOCATED"

# Relative voltage tolerance for matching - a PMIC output outside this band
# is not even a CANDIDATE (a 1.8V output is never force-matched to a 3.3V
# rail just because it is the "closest" of a bad set).
VOLTAGE_TOLERANCE_FRACTION = 0.05  # 5%

# Default efficiency used for a supplementary regulator's thermal estimate -
# a TYPICAL buck-converter figure (general industry practice, ~85-92%
# range) - must be confirmed against the real part's datasheet once one is
# chosen; this is a FIRST ESTIMATE only, same discipline as
# common/stackup.py's IPC-2221 trace-width first estimate.
DEFAULT_SUPPLEMENTARY_EFFICIENCY = 0.85


@dataclass
class PmicOutput:
    name: str            # "BUCK1", "LDO2"
    kind: str             # "BUCK" or "LDO"
    max_current_ma: float
    nominal_voltage_v: float


@dataclass
class PmicProfile:
    name: str            # "TPS65916"
    outputs: List[PmicOutput]


@dataclass
class RailNeed:
    rail_name: str        # "VD_MPU", "3V3_FPGA"
    voltage_v: float
    estimated_current_ma: float
    source: str = ""       # assigned PMIC output, or UNALLOCATED


def _voltage_compatible(target_v: float, candidate_v: float) -> bool:
    if target_v <= 0:
        return False
    return abs(candidate_v - target_v) / target_v <= VOLTAGE_TOLERANCE_FRACTION


def supplementary_dissipation_w(voltage_v: float, current_ma: float,
                                efficiency: float = DEFAULT_SUPPLEMENTARY_EFFICIENCY) -> float:
    """ESTIMATED power loss (W) of a supplementary regulator:
    ``P_out * (1/efficiency - 1)``. Assumes a buck-type converter; must be
    CONFIRMED against the real chosen part's own efficiency curve once one
    is selected - this is a first estimate only."""
    p_out_w = voltage_v * (current_ma / 1000.0)
    return round(p_out_w * (1.0 / efficiency - 1.0), 4)


def check_rail_allocation(pmic: PmicProfile, needs: List[RailNeed],
                          thermal_limits: Optional[MechanicalThermalLimits] = None):
    """Greedily assigns each ``RailNeed`` to a ``PmicOutput`` (closest
    nominal voltage + enough remaining current headroom).

    VIOLATION KINDS (kept separate):
      - ``rail_unallocated``: no PMIC output covers this rail (voltage
        tolerance + remaining current) - a supplementary regulator is
        needed.
      - ``output_overloaded``: the TOTAL current assigned to one output
        exceeds its ``max_current_ma``. The greedy pass itself can never
        produce this (a candidate is only chosen if it currently has
        enough headroom) - this exists for a ``RailNeed`` whose ``source``
        was pre-filled externally/by hand to a real output name, which the
        greedy pass RESPECTS (does not reassign) rather than silently
        overwriting.

    If ``thermal_limits`` is given, the ESTIMATED heat contribution of the
    supplementary regulator(s) needed for every unallocated rail is
    computed and compared against ``max_dissipation_w`` (reported in the
    detail text, not as a separate violation kind - the thermal-budget
    check itself belongs to a thermal-bridge tool; this is informational).
    """
    scanned = len(needs)
    if scanned == 0:
        return result("pmic_rail_allocation", 0, [], "no rail needs to evaluate")

    remaining_current: Dict[str, float] = {o.name: o.max_current_ma for o in pmic.outputs}
    output_by_name = {o.name: o for o in pmic.outputs}
    assigned: Dict[str, str] = {}  # rail_name -> output_name

    violations: List[Dict[str, Any]] = []
    unallocated_total_heat_w = 0.0

    # Reserve any externally pre-filled assignment's current BEFORE the
    # greedy pass runs, so a rail that was hand-assigned to an output is
    # respected (not silently reassigned) and the greedy pass sees the
    # correct remaining headroom for the rest.
    preassigned_rails = {need.rail_name for need in needs if need.source in output_by_name}
    for need in needs:
        if need.rail_name in preassigned_rails:
            remaining_current[need.source] -= need.estimated_current_ma
            assigned[need.rail_name] = need.source

    for need in needs:
        if need.rail_name in preassigned_rails:
            continue
        candidates = [
            o for o in pmic.outputs
            if _voltage_compatible(need.voltage_v, o.nominal_voltage_v)
            and remaining_current[o.name] >= need.estimated_current_ma
        ]
        if not candidates:
            need.source = UNALLOCATED
            heat_w = supplementary_dissipation_w(need.voltage_v, need.estimated_current_ma)
            unallocated_total_heat_w += heat_w
            violations.append({
                "kind": "rail_unallocated", "rail": need.rail_name,
                "voltage_v": need.voltage_v, "estimated_current_ma": need.estimated_current_ma,
                "detail": f"PMIC {pmic.name} cannot cover this rail, a supplementary "
                         "regulator is needed.",
                "supplementary_regulator_estimated_heat_w": heat_w,
            })
            continue

        # Closest nominal voltage wins; ties go to the candidate with more
        # remaining current headroom (deterministic).
        chosen = min(candidates,
                    key=lambda o: (abs(o.nominal_voltage_v - need.voltage_v),
                                  -remaining_current[o.name]))
        remaining_current[chosen.name] -= need.estimated_current_ma
        need.source = chosen.name
        assigned[need.rail_name] = chosen.name

    # Second-pass overload check: `source` may have been pre-filled
    # externally on the input - here we only sum, per output, the current
    # actually implied by whatever `source` ended up holding (including the
    # greedy assignment above).
    output_total_current: Dict[str, float] = {o.name: 0.0 for o in pmic.outputs}
    for need in needs:
        if need.source in output_by_name:
            output_total_current[need.source] += need.estimated_current_ma
    for output_name, total in output_total_current.items():
        limit = output_by_name[output_name].max_current_ma
        if total > limit:
            violations.append({
                "kind": "output_overloaded", "output": output_name,
                "total_current_ma": round(total, 3), "max_current_ma": limit,
                "detail": f"PMIC {pmic.name} output {output_name} needs "
                         f"{total:.1f}mA total but the limit is {limit:.1f}mA.",
            })

    detail_parts = [f"PMIC {pmic.name}: {len(needs)} rail(s) evaluated, "
                    f"{len(assigned)} allocated."]
    if unallocated_total_heat_w > 0:
        detail_parts.append(
            f"Total ESTIMATED supplementary-regulator heat for unallocated rails: "
            f"{round(unallocated_total_heat_w, 4)}W (efficiency assumption "
            f"{DEFAULT_SUPPLEMENTARY_EFFICIENCY*100:.0f}%, must be confirmed against "
            "the real part's datasheet once chosen).")
        if thermal_limits is not None:
            if unallocated_total_heat_w > thermal_limits.max_dissipation_w:
                detail_parts.append(
                    f"WARNING: this extra heat ALONE ({round(unallocated_total_heat_w, 4)}W) "
                    f"EXCEEDS the current thermal budget ({thermal_limits.max_dissipation_w}W) "
                    "- thermal management (heatsink/enclosure contact area) must be revised.")
            else:
                detail_parts.append(
                    f"Stays within the thermal budget ({thermal_limits.max_dissipation_w}W) "
                    "(for this supplementary regulator ALONE - must be evaluated TOGETHER "
                    "with the board's remaining heat sources).")

    return result("pmic_rail_allocation", scanned, violations, " ".join(detail_parts))


def _self_test() -> int:
    pmic = PmicProfile("TEST_PMIC", [
        PmicOutput("BUCK1", "BUCK", max_current_ma=2000, nominal_voltage_v=3.3),
        PmicOutput("BUCK2", "BUCK", max_current_ma=500, nominal_voltage_v=1.8),
    ])
    clean = [RailNeed("3V3_MAIN", 3.3, 1000.0)]
    finding_clean = check_rail_allocation(pmic, clean)
    assert finding_clean["status"] == "PASS", f"clean allocation should pass: {finding_clean}"
    assert clean[0].source == "BUCK1"

    broken = [RailNeed("5V_UNSUPPORTED", 5.0, 100.0)]
    finding_broken = check_rail_allocation(pmic, broken)
    assert finding_broken["status"] == "FAIL", \
        f"an unsupported voltage must be caught, not silently matched: {finding_broken}"
    assert broken[0].source == UNALLOCATED

    # The greedy pass itself never overbooks an output (a candidate whose
    # remaining current is too low is filtered out). Pre-filling `source`
    # to a real output name exercises the "externally pre-filled, greedy
    # pass respects it" path the second-pass overload check exists for.
    overload = [RailNeed("R1", 3.3, 1500.0, source="BUCK1"),
               RailNeed("R2", 3.3, 1500.0, source="BUCK1")]
    finding_overload = check_rail_allocation(pmic, overload)
    kinds = {v["kind"] for v in finding_overload["violations"]}
    assert "output_overloaded" in kinds, \
        f"exceeding BUCK1's 2000mA with two 1500mA rails must be caught: {finding_overload}"

    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("intent", nargs="?")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()

    if not a.intent:
        ap.error("intent.json is required unless --self-test is given")

    data = json.loads(Path(a.intent).read_text(encoding="utf-8"))
    pmic_data = data["pmic"]
    pmic = PmicProfile(pmic_data["name"], [
        PmicOutput(o["name"], o.get("kind", "BUCK"), o["max_current_ma"], o["nominal_voltage_v"])
        for o in pmic_data["outputs"]
    ])
    needs = [RailNeed(r["rail_name"], r["voltage_v"], r["estimated_current_ma"])
            for r in data["rails"]]
    thermal_limits = None
    if "thermal_limits" in data:
        thermal_limits = MechanicalThermalLimits(
            max_dissipation_w=data["thermal_limits"]["max_dissipation_w"])

    finding = check_rail_allocation(pmic, needs, thermal_limits)
    report = {
        "schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
        "pmic": pmic.name, "checks": [finding],
        "allocations": [{"rail": n.rail_name, "source": n.source} for n in needs],
        "summary": {s: sum(1 for f in [finding] if f["status"] == s)
                   for s in ("PASS", "FAIL", "NO_COVERAGE")},
    }
    write_report(report, a.json)
    emit(report)
    # The docstring above already promised "(or NO_COVERAGE)" exits 1; the
    # code branched on FAIL alone and contradicted it. An intent file with an
    # empty ``rails`` list makes check_rail_allocation return NO_COVERAGE
    # ("no rail needs to evaluate"), and exit 0 there reads as "every rail is
    # allocated" when in fact no rail was ever looked at.
    summary = report["summary"]
    return 1 if summary["FAIL"] or summary["NO_COVERAGE"] else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
