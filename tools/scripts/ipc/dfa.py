#!/usr/bin/env python3
"""IPC-A-610 design-for-assembly clearance engine.

Computes the minimum component-to-component and component-to-board-edge
clearance for an assembly class, then checks a supplied placement against
those targets through the bundle's findings contract (so an empty placement
is NO_COVERAGE, never a silent PASS).

WHAT IT MODELS
--------------
  * SMD-SMD / SMD-THT / THT-THT base clearances (placement-nozzle, wave and
    hand-solder access).
  * A separate, larger connector base, because a connector's mechanical
    mating/actuator keepout dominates.
  * A reflow thermal-SHADOWING surcharge: when two SMD bodies differ in
    height by more than the threshold, the shorter part gets insufficient
    hot-air/IR access (cold-joint risk) and needs extra room.
  * An assembly-class multiplier (Class 1 looser, Class 3 tighter).
  * Component-to-board-edge keepout per component type.

HONESTY NOTE
------------
The base distances and multipliers are REPRESENTATIVE values, not quoted
IPC-A-610 table entries — no purchased copy was available. They are a
screening aid. Also note the deliberate geometric simplification documented
on ``edge_gap_mm``: rotation is handled only for multiples of 90 degrees, so
at arbitrary angles the measured gap is APPROXIMATE and can read LARGER than
reality. Cross-check against real courtyard polygons before sign-off.

INPUT (positional, required) — a JSON file:
    {
      "components": [
        {"ref": "R1", "type": "smd_passive",
         "length_mm": 2.0, "width_mm": 1.25, "height_mm": 0.6,
         "x_mm": 10.0, "y_mm": 10.0, "angle_deg": 0}
      ],
      "board_edge_points_mm": [[0,0], [50,0], [50,40], [0,40]]
    }
``board_edge_points_mm`` may be omitted; the edge check is then NO_COVERAGE.

CLI
---
    dfa.py PLACEMENT_JSON [--class "Class 1"|"Class 2"|"Class 3"]
                          [--json OUT] [--self-test]

    exit 0  every check PASSed
    exit 1  a check FAILed, or a check had no coverage (NEEDS_HUMAN)
    exit 2  bad or missing input, or self-test failure
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from scripts.ipc.report import CHECK_SCHEMA, check_exit_code, check_report  # noqa: E402

SCHEMA_VERSION = CHECK_SCHEMA
TOOL_NAME = "dfa"
STANDARD = "IPC-A-610"

LIMITS = (
    "Base clearances and class multipliers are REPRESENTATIVE values, not "
    "quoted IPC-A-610 table entries.",
    "Bounding-box geometry: rotation is only honoured at multiples of 90 "
    "degrees, so gaps at arbitrary angles are approximate and can read "
    "LARGER than reality. Verify against real courtyard polygons.",
    "Edge clearance is measured from the component CENTRE to the nearest "
    "sampled Edge.Cuts point, not from the body edge — approximate.",
)


class ComponentType(str, Enum):
    SMD_PASSIVE = "smd_passive"    # 2-terminal R/C/L (0201..1210)
    SMD_IC = "smd_ic"              # bodied SMD IC (SOIC/QFN/BGA/SOT)
    THT = "tht"                    # through-hole (DIP, electrolytic)
    CONNECTOR = "connector"        # large mechanical connector (USB, header)


class AssemblyClass(str, Enum):
    """IPC-A-610's Class 1/2/3 taxonomy (the same as IPC-6012). Class 3
    (high reliability) demands more clearance because its tolerance for
    defects and rework is lower."""

    CLASS_1 = "Class 1"  # general consumer electronics
    CLASS_2 = "Class 2"  # dedicated service electronics
    CLASS_3 = "Class 3"  # high reliability (aerospace/medical/military)


@dataclass(frozen=True)
class PackageSize:
    """The body dimensions that matter for placement and reflow."""

    length_mm: float
    width_mm: float
    height_mm: float

    def __post_init__(self) -> None:
        if min(self.length_mm, self.width_mm, self.height_mm) <= 0:
            raise ValueError(f"PackageSize: all dimensions must be positive: {self}")


@dataclass(frozen=True)
class ClearanceTarget:
    type_a: str
    type_b: str
    minimum_clearance_mm: float
    assembly_class: str
    height_delta_mm: float
    shadowing_risk: bool
    source_note: str


# Keys are frozensets: an unordered pair, so A-B and B-A share one rule.
_SMD_SMD_BASE_MM: dict[frozenset, float] = {
    frozenset({ComponentType.SMD_PASSIVE}): 0.20,                       # passive-passive
    frozenset({ComponentType.SMD_PASSIVE, ComponentType.SMD_IC}): 0.25,
    frozenset({ComponentType.SMD_IC}): 0.30,                            # IC-IC
}

_SMD_THT_BASE_MM: dict[frozenset, float] = {
    frozenset({ComponentType.SMD_PASSIVE, ComponentType.THT}): 1.00,
    frozenset({ComponentType.SMD_IC, ComponentType.THT}): 1.50,
    frozenset({ComponentType.THT}): 2.00,                               # THT-THT
}

_CLASS_MULTIPLIER: dict[AssemblyClass, float] = {
    AssemblyClass.CLASS_1: 0.85,
    AssemblyClass.CLASS_2: 1.00,
    AssemblyClass.CLASS_3: 1.30,
}

# A connector's mechanical constraint (mating/actuator keepout) dominates,
# so it gets its own larger base: even next to a tiny passive you want at
# least the connector body + tolerance + hand/tool access.
_CONNECTOR_BASE_MM = 1.50

# Reflow thermal shadowing: when two SMD bodies differ in height by more
# than this, the shorter part may get insufficient hot-air/IR access.
SHADOWING_HEIGHT_THRESHOLD_MM = 2.0
SHADOWING_EXTRA_CLEARANCE_MM = 0.30

# Component-to-board-edge keepout: assembly rail + nozzle/vacuum access +
# depanel stress. Covers the same risk as dfm_emc_check's ceramic edge
# keepout, but applied to ALL component types rather than just ceramics.
EDGE_KEEPOUT_MM: dict[ComponentType, float] = {
    ComponentType.SMD_PASSIVE: 2.0,
    ComponentType.SMD_IC: 3.0,
    ComponentType.THT: 3.0,
    ComponentType.CONNECTOR: 5.0,
}


def edge_gap_mm(x_a: float, y_a: float, size_a: PackageSize, angle_a: float,
                x_b: float, y_b: float, size_b: PackageSize, angle_b: float,
                ) -> float:
    """Shortest edge-to-edge distance between two axis-aligned bounding
    boxes (NOT centre-to-centre).

    DELIBERATE, DOCUMENTED LIMIT: the rotation angle does not truly rotate
    the box; it only swaps length/width at multiples of 90 degrees. At an
    arbitrary angle (e.g. 37 degrees) this is APPROXIMATE and can report a
    LARGER gap than reality. A silent limit manufactures false confidence,
    so it is stated here and repeated in the report's ``limits``. Verify
    against real ``.kicad_pcb`` courtyard polygons for sign-off.
    """
    def box(x, y, size, angle):
        length, width = size.length_mm, size.width_mm
        if round(angle) % 180 == 90:
            length, width = width, length
        return (x - length / 2, y - width / 2,
                x + length / 2, y + width / 2)

    ax0, ay0, ax1, ay1 = box(x_a, y_a, size_a, angle_a)
    bx0, by0, bx1, by1 = box(x_b, y_b, size_b, angle_b)

    dx = max(ax0 - bx1, bx0 - ax1, 0.0)
    dy = max(ay0 - by1, by0 - ay1, 0.0)
    # Rounding is INTENTIONAL: a float subtraction residue such as
    # 0.19999999999999996 would flag an exactly-on-limit pair as a
    # violation. 4 dp matches every other mm value in this module.
    return round(math.hypot(dx, dy), 4)


def minimum_clearance(type_a: ComponentType, type_b: ComponentType,
                      size_a: PackageSize, size_b: PackageSize,
                      assembly_class: AssemblyClass = AssemblyClass.CLASS_2,
                      ) -> ClearanceTarget:
    """(type_a, type_b, package sizes) -> minimum clearance in mm.

    Applies the SMD-SMD / SMD-THT / THT-THT tables, the reflow shadowing
    surcharge and the assembly-class multiplier.
    """
    pair = frozenset({type_a, type_b})
    both_smd = (ComponentType.THT not in pair
                and ComponentType.CONNECTOR not in pair)

    if ComponentType.CONNECTOR in pair:
        base = _CONNECTOR_BASE_MM
        source = "connector mechanical keepout base (REPRESENTATIVE)"
    elif ComponentType.THT in pair:
        base = _SMD_THT_BASE_MM.get(
            pair, _SMD_THT_BASE_MM[frozenset({ComponentType.THT})])
        source = "SMD-THT / THT-THT wave and hand-solder access base (REPRESENTATIVE)"
    else:
        base = _SMD_SMD_BASE_MM.get(
            pair, _SMD_SMD_BASE_MM[frozenset({ComponentType.SMD_IC})])
        source = "SMD-SMD placement-nozzle access base (REPRESENTATIVE)"

    height_delta = abs(size_a.height_mm - size_b.height_mm)
    shadowing = both_smd and height_delta > SHADOWING_HEIGHT_THRESHOLD_MM
    extra = SHADOWING_EXTRA_CLEARANCE_MM if shadowing else 0.0

    multiplier = _CLASS_MULTIPLIER[assembly_class]
    minimum_mm = round((base + extra) * multiplier, 4)

    note = (f"{source}; class multiplier x{multiplier} "
            f"({assembly_class.value})")
    if shadowing:
        note += (f"; +{SHADOWING_EXTRA_CLEARANCE_MM} mm reflow shadowing "
                 f"surcharge (height delta {height_delta:.2f} mm > "
                 f"{SHADOWING_HEIGHT_THRESHOLD_MM} mm)")

    return ClearanceTarget(
        type_a=type_a.value,
        type_b=type_b.value,
        minimum_clearance_mm=minimum_mm,
        assembly_class=assembly_class.value,
        height_delta_mm=round(height_delta, 4),
        shadowing_risk=shadowing,
        source_note=note,
    )


def minimum_edge_clearance_mm(component_type: ComponentType) -> float:
    """Component-to-board-edge minimum, in mm. See ``EDGE_KEEPOUT_MM``."""
    return EDGE_KEEPOUT_MM[component_type]


@dataclass(frozen=True)
class PlacedComponent:
    ref: str
    type: ComponentType
    size: PackageSize
    x_mm: float
    y_mm: float
    angle_deg: float = 0.0


class DfaEngine:
    """Check a placement against the computed clearance targets."""

    def __init__(self,
                 assembly_class: AssemblyClass = AssemblyClass.CLASS_2) -> None:
        self.assembly_class = assembly_class

    def check_component_clearance(
            self, placement: Sequence[PlacedComponent]) -> Finding:
        """Every component PAIR (n*(n-1)/2 of them) measured against its
        computed minimum."""
        pair_count = 0
        violations = []
        for i in range(len(placement)):
            for j in range(i + 1, len(placement)):
                a, b = placement[i], placement[j]
                pair_count += 1
                target = minimum_clearance(a.type, b.type, a.size, b.size,
                                           self.assembly_class)
                measured = edge_gap_mm(a.x_mm, a.y_mm, a.size, a.angle_deg,
                                       b.x_mm, b.y_mm, b.size, b.angle_deg)
                if measured < target.minimum_clearance_mm:
                    violations.append({
                        "a": a.ref,
                        "b": b.ref,
                        "measured_mm": measured,
                        "minimum_mm": target.minimum_clearance_mm,
                        "short_by_mm": round(
                            target.minimum_clearance_mm - measured, 4),
                        "shadowing_risk": target.shadowing_risk,
                    })
        return result(
            "ipc_a_610_component_clearance", pair_count, violations,
            f"{len(placement)} components, {pair_count} pairs checked "
            f"({self.assembly_class.value}). LIMIT: bounding-box "
            "approximation; at arbitrary angles this can differ from the real "
            "courtyard (see edge_gap_mm).")

    def check_edge_clearance(self, placement: Sequence[PlacedComponent],
                             edge_points_mm: Sequence[Sequence[float]],
                             ) -> Finding:
        """Distance from each component to the nearest sampled Edge.Cuts
        point."""
        if not edge_points_mm:
            return result(
                "ipc_a_610_edge_clearance", 0, [],
                "Edge.Cuts point list is empty — no board outline supplied.")
        violations = []
        for component in placement:
            nearest = min(
                math.hypot(component.x_mm - ex, component.y_mm - ey)
                for ex, ey in edge_points_mm)
            minimum = minimum_edge_clearance_mm(component.type)
            if nearest < minimum:
                violations.append({
                    "ref": component.ref,
                    "type": component.type.value,
                    "measured_mm": round(nearest, 4),
                    "minimum_mm": minimum,
                })
        return result(
            "ipc_a_610_edge_clearance", len(placement), violations,
            "Distance from the component CENTRE to the nearest sampled "
            "Edge.Cuts point (not the body edge) — approximate, exactly like "
            "dfm_emc_check.check_edge_keepout_ceramics.")

    def run_all(self, placement: Sequence[PlacedComponent] = (),
                edge_points_mm: Sequence[Sequence[float]] = (),
                ) -> list[Finding]:
        return [
            self.check_component_clearance(placement),
            self.check_edge_clearance(placement, edge_points_mm),
        ]


def placement_from_json(data: dict) -> tuple[
        list[PlacedComponent], list[list[float]]]:
    """Parse the input document."""
    if not isinstance(data, dict):
        raise ValueError("placement file must contain a JSON object")
    components = [
        PlacedComponent(
            ref=str(record.get("ref", "?")),
            type=ComponentType(record["type"]),
            size=PackageSize(float(record["length_mm"]),
                             float(record["width_mm"]),
                             float(record["height_mm"])),
            x_mm=float(record["x_mm"]),
            y_mm=float(record["y_mm"]),
            angle_deg=float(record.get("angle_deg", 0.0)),
        )
        for record in data.get("components", [])
    ]
    edge = [[float(px), float(py)]
            for px, py in data.get("board_edge_points_mm", [])]
    return components, edge


# --------------------------------------------------------------------------- #
# Self-test + fault injection
# --------------------------------------------------------------------------- #
def _prove_test_not_empty() -> bool:
    """FAULT INJECTION: place two SMD passives just below the computed
    minimum and prove the check really reports FAIL."""
    size = PackageSize(2.0, 1.25, 0.6)
    target = minimum_clearance(ComponentType.SMD_PASSIVE,
                               ComponentType.SMD_PASSIVE, size, size)
    # Centre-to-centre so the edge gap lands 0.01 mm under the minimum.
    separation = size.length_mm + target.minimum_clearance_mm - 0.01
    placement = [
        PlacedComponent("R1", ComponentType.SMD_PASSIVE, size, 0.0, 0.0),
        PlacedComponent("R2", ComponentType.SMD_PASSIVE, size, separation, 0.0),
    ]
    finding = DfaEngine().check_component_clearance(placement)
    return finding["status"] == "FAIL"


def run_self_tests() -> list[str]:
    failures: list[str] = []
    size = PackageSize(2.0, 1.25, 0.6)

    # Class 3 must demand more clearance than Class 2, which demands more
    # than Class 1.
    values = [
        minimum_clearance(ComponentType.SMD_PASSIVE, ComponentType.SMD_PASSIVE,
                          size, size, cls).minimum_clearance_mm
        for cls in (AssemblyClass.CLASS_1, AssemblyClass.CLASS_2,
                    AssemblyClass.CLASS_3)
    ]
    if not values[0] < values[1] < values[2]:
        failures.append(f"class multipliers are not ordered: {values}")

    # A connector always demands at least the connector base.
    connector = minimum_clearance(
        ComponentType.CONNECTOR, ComponentType.SMD_PASSIVE, size, size,
        AssemblyClass.CLASS_2)
    if connector.minimum_clearance_mm < _CONNECTOR_BASE_MM:
        failures.append(
            f"connector pair fell below the connector base: "
            f"{connector.minimum_clearance_mm} < {_CONNECTOR_BASE_MM}")

    # Shadowing must trigger only above the threshold, and only SMD-SMD.
    tall = PackageSize(2.0, 1.25, 0.6 + SHADOWING_HEIGHT_THRESHOLD_MM + 0.5)
    shadowed = minimum_clearance(ComponentType.SMD_PASSIVE,
                                 ComponentType.SMD_IC, size, tall)
    if not shadowed.shadowing_risk:
        failures.append("reflow shadowing did not trigger above the threshold")
    flat = minimum_clearance(ComponentType.SMD_PASSIVE,
                             ComponentType.SMD_IC, size, size)
    if flat.shadowing_risk:
        failures.append("reflow shadowing triggered below the threshold")

    # Empty input must be NO_COVERAGE, never PASS.
    for finding in DfaEngine().run_all():
        if finding["status"] != "NO_COVERAGE":
            failures.append(
                f"{finding['check']}: empty input produced "
                f"{finding['status']}, expected NO_COVERAGE")

    # Overlapping boxes must report a zero gap, not a negative one.
    if edge_gap_mm(0, 0, size, 0, 0.1, 0, size, 0) != 0.0:
        failures.append("overlapping boxes did not report a zero gap")

    if not _prove_test_not_empty():
        failures.append(
            "fault injection did not fire: the clearance comparison may be empty")

    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("placement", type=Path, nargs="?",
                        help="JSON file describing the placement "
                             "(required unless --self-test)")
    parser.add_argument("--class", dest="assembly_class",
                        choices=[e.value for e in AssemblyClass],
                        default=AssemblyClass.CLASS_2.value)
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

    if args.placement is None:
        parser.error("a placement JSON file is required; pass it positionally "
                     "or use --self-test")

    data = json.loads(Path(args.placement).read_text(encoding="utf-8"))
    components, edge_points = placement_from_json(data)

    assembly_class = AssemblyClass(args.assembly_class)
    findings = DfaEngine(assembly_class).run_all(components, edge_points)

    report = check_report(
        TOOL_NAME, STANDARD, str(args.placement),
        params={
            "class": assembly_class.value,
            "class_multiplier": _CLASS_MULTIPLIER[assembly_class],
            "component_count": len(components),
            "edge_point_count": len(edge_points),
            "shadowing_threshold_mm": SHADOWING_HEIGHT_THRESHOLD_MM,
        },
        findings=findings,
        limits=LIMITS,
    )
    write_report(report, args.json)
    emit(report)
    return check_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
