#!/usr/bin/env python3
"""CPL / assembly: pick points, fab rotation map, orientation and panelisation.

KiBot already produces the CPL/BOM/Gerber package. This tool manages KiBot's
INPUT: the fab-specific rotation offsets, and the checks that catch the two
failure modes a DRC cannot see - a whole batch soldered backwards, and a
de-panel line running past a stress-sensitive part.

backend: file  (pure stdlib)

WHY THE ROTATION MAP IS HASHED
------------------------------
If the fab rotation map changes silently - a new footprint added and the old
entries forgotten - the next build can have EVERY polarised part reversed. The
content hash makes "did this map change between runs" answerable. An
unmatched footprint is never quietly given a 0 degree offset; it is a
violation.

WHY THE PICK POINT IS THE COURTYARD CENTRE
------------------------------------------
Not the footprint origin. The two are usually different (the origin tends to
sit near pin 1) and a placement machine that picks at the origin drops parts.

Usage:
    assembly.py <assembly.json> [--json out.json] [--max-rotation-delta-deg 1.0]
    assembly.py --self-test

assembly.json:
    {"footprints": [{"refdes": "U1", "library": "Package_SO:SOIC-8",
                     "origin": [0,0], "courtyard_bbox": [-1,-1,1,1],
                     "angle_deg": 90, "layer": "F.Cu"}],
     "rotation_map": {"fab": "JLCPCB",
                      "entries": [{"library": "Package_SO:SOIC-8",
                                   "offset_deg": 180}]},
     "polarised": [{"refdes": "U1", "kind": "ic_pin1", "expected_deg": 270}],
     "panel": {"global_fiducials": 3, "bga_local_fiducials": true,
               "rail_width_mm": 5.0, "sensitive_part_depanel_mm": 8.0}}

Exit: 0 = every check measured and clean, 1 = at least one FAIL *or*
NO_COVERAGE, 2 = run error. NO_COVERAGE is non-zero here for the same reason
its two siblings say so (``freerouting.py``, ``outputs.py``): an unfilled
panel block or an empty footprint list is "nobody checked", not "compliant".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    Finding,
    board_report,
    result,
)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "production-gate-report/v1"
TOOL_NAME = "production_assembly"

MIN_GLOBAL_FIDUCIALS = 3
MIN_RAIL_MM = 5.0
MIN_SENSITIVE_DEPANEL_MM = 5.0


class PolarisedKind(str, Enum):
    DIODE = "diode"
    IC_PIN1 = "ic_pin1"
    LED = "led"
    ELECTROLYTIC = "electrolytic"


@dataclass
class FootprintGeometry:
    refdes: str
    library: str                       # the rotation-map join key
    origin: tuple[float, float]
    courtyard_bbox: tuple[float, float, float, float]
    angle_deg: float
    layer: str                         # F.Cu | B.Cu


@dataclass
class RotationEntry:
    library: str
    offset_deg: float


@dataclass
class RotationMap:
    fab: str
    entries: list[RotationEntry]
    content_hash: str = ""
    note: str = ""


@dataclass
class PolarisedPart:
    refdes: str
    kind: str
    expected_deg: float


@dataclass
class PanelConstraints:
    global_fiducials: int
    bga_local_fiducials: bool
    rail_width_mm: float
    sensitive_part_depanel_mm: float | None = None


def hash_rotation_map(fab: str, entries, note: str = "") -> RotationMap:
    """Stamp the map with a content hash so silent drift is detectable."""
    payload = json.dumps([(fab, e.library, e.offset_deg) for e in entries],
                         sort_keys=True, ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return RotationMap(fab=fab, entries=list(entries), content_hash=digest,
                       note=note)


def rotation_map_changed(old: RotationMap, new: RotationMap) -> bool:
    return old.content_hash != new.content_hash


def build_cpl(footprints, rotation_map: RotationMap) -> list[dict]:
    """Designator / MidX / MidY / Layer / Rotation for every footprint.

    The pick point is the courtyard bbox centre. The rotation is the footprint
    angle plus the fab offset; a footprint with no rotation-map entry keeps
    ``rotation_map_matched: false`` so the caller can refuse rather than ship a
    silently-unoffset part.
    """
    offsets = {e.library: e.offset_deg for e in rotation_map.entries}
    rows: list[dict] = []
    for footprint in footprints:
        x_min, y_min, x_max, y_max = footprint.courtyard_bbox
        offset = offsets.get(footprint.library)
        rotation = footprint.angle_deg % 360
        if offset is not None:
            rotation = (rotation + offset) % 360
        rows.append({
            "Designator": footprint.refdes,
            "MidX": round((x_min + x_max) / 2, 4),
            "MidY": round((y_min + y_max) / 2, 4),
            "Layer": footprint.layer,
            "Rotation": rotation,
            "rotation_map_matched": offset is not None,
        })
    return rows


def check_rotation_map_coverage(cpl_rows, footprints) -> Finding:
    """Every footprint must be covered by the fab rotation map."""
    by_refdes = {f.refdes: f for f in footprints}
    violations = [
        {"refdes": row["Designator"],
         "library": by_refdes[row["Designator"]].library,
         "message": "no rotation-map entry; a 0 degree offset was NOT assumed. "
                    "Shipping without updating the fab rotation map can reverse "
                    "the entire batch."}
        for row in cpl_rows if not row["rotation_map_matched"]
    ]
    return result("cpl_rotation_map_coverage", len(cpl_rows), violations,
                  "every footprint needs an explicit fab rotation offset")


def check_orientation(polarised, cpl_rows, tolerance_deg: float = 1.0) -> Finding:
    """Polarised part orientation against the produced CPL angle.

    Skipping this lets a board pass DRC and still have every diode, LED,
    electrolytic and IC mounted backwards: the DRC knows clearance and
    connectivity, not direction.
    """
    polarised = list(polarised)
    by_refdes = {row["Designator"]: row for row in cpl_rows}
    violations: list[dict] = []
    for part in polarised:
        row = by_refdes.get(part.refdes)
        if row is None:
            violations.append({"refdes": part.refdes, "kind": part.kind,
                               "problem": "NOT_IN_CPL"})
            continue
        delta = abs(row["Rotation"] - part.expected_deg) % 360
        delta = min(delta, 360 - delta)
        if delta > tolerance_deg:
            violations.append({
                "refdes": part.refdes, "kind": part.kind,
                "cpl_deg": row["Rotation"], "expected_deg": part.expected_deg,
                "delta_deg": round(delta, 3), "tolerance_deg": tolerance_deg,
                "problem": "REVERSED_MOUNT_RISK",
            })
    return result("cpl_orientation", len(polarised), violations,
                  f"polarised part angles within +/-{tolerance_deg} degrees "
                  f"of the schematic/datasheet expectation")


def check_panelisation(panel: PanelConstraints | None,
                       min_sensitive_mm: float = MIN_SENSITIVE_DEPANEL_MM,
                       min_rail_mm: float = MIN_RAIL_MM,
                       min_global_fiducials: int = MIN_GLOBAL_FIDUCIALS) -> Finding:
    """Fiducials, rail width and de-panel stress distance.

    No panel data at all is NO_COVERAGE: an unpanelised board is a legitimate
    answer, but this tool cannot tell that apart from "nobody filled it in".
    """
    if panel is None:
        return result("panelisation", 0, [],
                      "no panel constraints supplied - not checked, and NOT "
                      "assumed compliant")
    checks = 0
    violations: list[dict] = []

    checks += 1
    if panel.global_fiducials < min_global_fiducials:
        violations.append({"item": "global_fiducials",
                           "value": panel.global_fiducials,
                           "minimum": min_global_fiducials})
    checks += 1
    if not panel.bga_local_fiducials:
        violations.append({"item": "bga_local_fiducials", "value": False,
                           "message": "mandatory when the panel carries a BGA"})
    checks += 1
    if panel.rail_width_mm < min_rail_mm:
        violations.append({"item": "rail_width_mm",
                           "value": panel.rail_width_mm, "minimum": min_rail_mm})
    if panel.sensitive_part_depanel_mm is not None:
        checks += 1
        if panel.sensitive_part_depanel_mm < min_sensitive_mm:
            violations.append({
                "item": "sensitive_part_depanel_mm",
                "value": panel.sensitive_part_depanel_mm,
                "minimum": min_sensitive_mm,
                "message": "de-panel stress can reach a crystal, sensitive "
                           "analog part or BGA corner"})
    return result("panelisation", checks, violations,
                  f">= {min_global_fiducials} global fiducials, >= {min_rail_mm} "
                  f"mm rail, sensitive parts >= {min_sensitive_mm} mm from the "
                  f"de-panel line")


def load_assembly(path: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    footprints = [FootprintGeometry(
        refdes=f["refdes"], library=f["library"],
        origin=tuple(f.get("origin", (0.0, 0.0))),
        courtyard_bbox=tuple(f["courtyard_bbox"]),
        angle_deg=float(f.get("angle_deg", 0.0)),
        layer=f.get("layer", "F.Cu")) for f in data.get("footprints", [])]

    raw_map = data.get("rotation_map", {})
    rotation_map = hash_rotation_map(
        raw_map.get("fab", "UNKNOWN"),
        [RotationEntry(e["library"], float(e["offset_deg"]))
         for e in raw_map.get("entries", [])],
        raw_map.get("note", ""))

    polarised = [PolarisedPart(p["refdes"], p["kind"], float(p["expected_deg"]))
                 for p in data.get("polarised", [])]

    raw_panel = data.get("panel")
    panel = None if raw_panel is None else PanelConstraints(
        global_fiducials=int(raw_panel.get("global_fiducials", 0)),
        bga_local_fiducials=bool(raw_panel.get("bga_local_fiducials", False)),
        rail_width_mm=float(raw_panel.get("rail_width_mm", 0.0)),
        sensitive_part_depanel_mm=raw_panel.get("sensitive_part_depanel_mm"))

    return footprints, rotation_map, polarised, panel


def self_test() -> list[str]:
    problems: list[str] = []

    footprint = FootprintGeometry("U1", "Package_SO:SOIC-8", (0.0, 0.0),
                                  (-1.0, -0.5, 3.0, 1.5), 90.0, "F.Cu")
    mapped = hash_rotation_map("JLCPCB",
                               [RotationEntry("Package_SO:SOIC-8", 180.0)])
    rows = build_cpl([footprint], mapped)

    if abs(rows[0]["MidX"] - 1.0) > 1e-9 or abs(rows[0]["MidY"] - 0.5) > 1e-9:
        problems.append(f"pick point is not the courtyard centre: {rows[0]}")
    if rows[0]["Rotation"] != 270.0:
        problems.append(f"the fab offset was not applied: {rows[0]['Rotation']}")

    # FAULT INJECTION: an unmapped footprint MUST fail, not default to 0.
    unmapped = build_cpl([footprint], hash_rotation_map("JLCPCB", []))
    coverage = check_rotation_map_coverage(unmapped, [footprint])
    if coverage["status"] != FAIL:
        problems.append("fault injection did not hold: unmapped footprint passed")
    if unmapped[0]["Rotation"] != 90.0:
        problems.append("an unmapped footprint was silently given an offset")

    # Hash changes when the map changes.
    changed = hash_rotation_map("JLCPCB",
                                [RotationEntry("Package_SO:SOIC-8", 90.0)])
    if not rotation_map_changed(mapped, changed):
        problems.append("the rotation-map hash did not notice a changed offset")
    if rotation_map_changed(mapped,
                            hash_rotation_map("JLCPCB",
                                              [RotationEntry("Package_SO:SOIC-8",
                                                             180.0)])):
        problems.append("the rotation-map hash changed for an identical map")

    good = check_orientation([PolarisedPart("U1", "ic_pin1", 270.0)], rows)
    if good["status"] != "PASS":
        problems.append(f"a correctly oriented part did not PASS: {good}")
    bad = check_orientation([PolarisedPart("U1", "ic_pin1", 90.0)], rows)
    if bad["status"] != FAIL:
        problems.append("a reversed part was not detected")
    absent = check_orientation([PolarisedPart("D9", "diode", 0.0)], rows)
    if absent["status"] != FAIL:
        problems.append("a polarised part missing from the CPL was not flagged")

    if check_panelisation(None)["status"] != "NO_COVERAGE":
        problems.append("missing panel data did not report NO_COVERAGE")
    thin = check_panelisation(PanelConstraints(2, False, 3.0, 1.0))
    if thin["status"] != FAIL or len(thin["violations"]) != 4:
        problems.append(f"panel violations miscounted: {thin}")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("assembly", nargs="?",
                        help="JSON with footprints, rotation_map, polarised, panel")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--max-rotation-delta-deg", type=float, default=1.0)
    parser.add_argument("--cpl-out", help="also write the CPL rows as JSON here")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: production_assembly")
        return 0

    if not args.assembly:
        parser.error("the assembly JSON is required")

    footprints, rotation_map, polarised, panel = load_assembly(args.assembly)
    cpl_rows = build_cpl(footprints, rotation_map)
    if args.cpl_out:
        write_report({"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
                      "rotation_map_hash": rotation_map.content_hash,
                      "rows": cpl_rows}, args.cpl_out)

    findings = [
        check_rotation_map_coverage(cpl_rows, footprints),
        check_orientation(polarised, cpl_rows, args.max_rotation_delta_deg),
        check_panelisation(panel),
    ]
    report = board_report(TOOL_NAME, args.assembly, findings, params={
        "fab": rotation_map.fab,
        "rotation_map_hash": rotation_map.content_hash,
        "rotation_map_entries": len(rotation_map.entries),
        "footprints": len(footprints),
        "cpl_rows": cpl_rows,
        "max_rotation_delta_deg": args.max_rotation_delta_deg,
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    # This branched on FAIL alone - a hand-rolled copy of
    # common.findings.board_exit_code that never inherited that function's
    # documented (and deliberately frozen) NO_COVERAGE exemption, unlike the
    # two siblings in this directory. check_panelisation() returns
    # NO_COVERAGE when the ``panel`` block is absent, and it says in so many
    # words that this is "not checked, and NOT assumed compliant"; exit 0
    # would have assumed exactly that on the caller's behalf.
    summary = report["summary"]
    return 1 if summary[FAIL] or summary[NO_COVERAGE] else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
