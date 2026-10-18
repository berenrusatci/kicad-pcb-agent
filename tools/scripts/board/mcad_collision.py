#!/usr/bin/env python3
"""ECAD/MCAD 3-D collision: component bodies against enclosure obstructions.

A single "max allowed height in this zone" number throws away information: a
real enclosure has obstructions at specific places with specific volumes (a
screw boss 3 mm across and 5 mm tall inside a lid that otherwise leaves 8 mm).
This tool tests each component's real 3-D box - placement, rotation, layer -
against each obstruction volume and names the collision.

backend: file  (regex .kicad_pcb reader; kicad-cli only for the optional STEP export)

WHAT IS NOT DONE, EXPLICITLY
----------------------------
The STEP file's B-rep is NOT parsed. STEP AP214 is not something to regex, and
no CAD kernel is a dependency here. Component bodies (W x D x H) are supplied
by the caller in the enclosure JSON, read from the datasheet or a 3-D model.
Automatic body extraction belongs behind a real CAD kernel and must not be
claimed until it exists. A refdes with no body data is NOT scanned - never
reported as "no collision".

``--step-export`` runs ``kicad-cli pcb export step`` purely to surface which
3-D models were missing: every substituted model is a component this check is
blind to, and those are reported as violations of a separate coverage check
rather than being swallowed.

Usage:
    mcad_collision.py <board.kicad_pcb> --enclosure enclosure.json
                      [--json out.json] [--board-thickness-mm 1.6]
                      [--step-export board.step]
    mcad_collision.py --self-test

Enclosure JSON:
    {"bodies": {"J1": {"width_mm": 5, "depth_mm": 5, "height_mm": 8}},
     "obstructions": [{"name": "lid_inner_face", "x_min_mm": -10, ...}]}

Exit: 0 = no FAIL, 1 = at least one FAIL, 2 = run error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.collision import (  # noqa: E402
    Body3D,
    ObstructionVolume,
    Placement,
    boxes_collide_3d,
    component_box_3d,
    mcad_collision_finding,
    rotated_aabb,
)
from common.findings import Finding, board_exit_code, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "mcad-collision-report/v1"
TOOL_NAME = "mcad_collision"


# --------------------------------------------------------------------------- #
# .kicad_pcb placement extraction (no pcbnew needed)
# --------------------------------------------------------------------------- #
_FOOTPRINT_HEAD = re.compile(r'\(footprint\s+"([^"]+)"')
_AT_LINE = re.compile(r'^\s*\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?\)\s*$',
                      re.MULTILINE)
_LAYER_LINE = re.compile(r'^\s*\(layer\s+"([^"]+)"\)\s*$', re.MULTILINE)
_REFERENCE = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"')


def footprint_blocks(pcb_text: str) -> list[str]:
    """Split the board text into balanced ``(footprint ...)`` blocks."""
    blocks: list[str] = []
    cursor = 0
    while True:
        match = _FOOTPRINT_HEAD.search(pcb_text, cursor)
        if not match:
            break
        start = pcb_text.rfind("(", 0, match.start())
        if start == -1 or pcb_text[start:match.start()].strip() != "":
            start = match.start()
        depth = 0
        i = start
        while i < len(pcb_text):
            if pcb_text[i] == "(":
                depth += 1
            elif pcb_text[i] == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        blocks.append(pcb_text[start:i])
        cursor = i
    return blocks


def parse_placements(pcb_text: str) -> list[Placement]:
    """Refdes / position / rotation / layer for every footprint.

    A footprint with no ``Reference`` property (a truncated or malformed
    block) is SKIPPED: a partial placement record is more dangerous than no
    record, because it silently shifts a body somewhere it is not.
    """
    placements: list[Placement] = []
    for block in footprint_blocks(pcb_text):
        library = _FOOTPRINT_HEAD.search(block)
        reference = _REFERENCE.search(block)
        at = _AT_LINE.search(block)
        layer = _LAYER_LINE.search(block)
        if not (library and reference and at):
            continue
        placements.append(Placement(
            ref=reference.group(1),
            footprint=library.group(1),
            x_mm=float(at.group(1)),
            y_mm=float(at.group(2)),
            angle_deg=float(at.group(3)) if at.group(3) else 0.0,
            layer=layer.group(1) if layer else "F.Cu",
        ))
    return placements


def load_enclosure(path: str) -> tuple[dict[str, Body3D], list[ObstructionVolume]]:
    """Read the caller-supplied bodies and obstruction volumes."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    bodies = {
        ref: Body3D(float(b["width_mm"]), float(b["depth_mm"]), float(b["height_mm"]))
        for ref, b in data.get("bodies", {}).items()
    }
    volumes = [
        ObstructionVolume(
            name=v["name"],
            x_min_mm=float(v["x_min_mm"]), y_min_mm=float(v["y_min_mm"]),
            x_max_mm=float(v["x_max_mm"]), y_max_mm=float(v["y_max_mm"]),
            z_min_mm=float(v["z_min_mm"]), z_max_mm=float(v["z_max_mm"]))
        for v in data.get("obstructions", [])
    ]
    return bodies, volumes


def check_body_coverage(placements, bodies: dict[str, Body3D],
                        missing_models: list[str]) -> Finding:
    """Which placed components have no body data - i.e. were not tested.

    Reported as its own check so "we found no collision" can never be read as
    "every part was examined".
    """
    placements = list(placements)
    untested = [
        {"ref": p.ref, "footprint": p.footprint,
         "reason": "no body data supplied in the enclosure JSON"}
        for p in placements if p.ref not in bodies
    ]
    untested += [
        {"ref": ref, "footprint": None,
         "reason": "3-D model missing at STEP export; this component was substituted"}
        for ref in missing_models
    ]
    return result("mcad_body_coverage", len(placements), untested,
                  "every placed component needs body data before its clearance "
                  "to the enclosure can be claimed")


def self_test() -> list[str]:
    problems: list[str] = []

    box = rotated_aabb(0, 0, 4, 2, 0.0)
    if box != (-2.0, -1.0, 2.0, 1.0):
        problems.append(f"unrotated AABB wrong: {box}")

    box90 = rotated_aabb(0, 0, 4, 2, 90.0)
    if abs(box90[0] - (-1.0)) > 1e-6 or abs(box90[2] - 1.0) > 1e-6:
        problems.append(f"90-degree AABB wrong: {box90}")

    # Separated in Z, overlapping in XY -> not a collision.
    if boxes_collide_3d((0, 0, 0, 5, 5, 2), (1, 1, 10, 4, 4, 15)) is not None:
        problems.append("boxes separated in Z reported as colliding")

    # A component with no body data is not scanned.
    finding = mcad_collision_finding(
        [Placement("U1", "x", 0, 0, 0, "F.Cu")], {}, [])
    if finding["scanned"] != 0 or finding["status"] != "NO_COVERAGE":
        problems.append("a component without body data was mis-reported")

    # FAULT INJECTION: an obstruction placed right on top MUST fail.
    injected = mcad_collision_finding(
        [Placement("J1", "Conn:Header", 0.0, 0.0, 0.0, "F.Cu")],
        {"J1": Body3D(5.0, 5.0, 8.0)},
        [ObstructionVolume("lid_inner_face", -10, -10, 10, 10, 5.0, 20.0)])
    if injected["status"] != "FAIL":
        problems.append("fault injection did not hold: collision test may be empty")

    # A B.Cu part must sit below the board, not overlap the top-side lid.
    bottom = component_box_3d(Placement("C1", "x", 0, 0, 0, "B.Cu"),
                              Body3D(1, 1, 1), board_thickness_mm=1.6)
    if bottom[5] > 0:
        problems.append(f"a bottom-side part was placed above z=0: {bottom}")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--enclosure",
                        help="JSON with component bodies and obstruction volumes")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--board-thickness-mm", type=float, default=1.6)
    parser.add_argument("--step-export", metavar="OUT.step",
                        help="also run kicad-cli pcb export step, to list the "
                             "3-D models that were missing")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: mcad_collision")
        return 0

    if not args.board:
        parser.error("the board path is required")
    if not args.enclosure:
        parser.error("--enclosure is required: component bodies and obstruction "
                     "volumes are caller-supplied, never inferred")

    placements = parse_placements(
        Path(args.board).read_text(encoding="utf-8"))
    bodies, volumes = load_enclosure(args.enclosure)

    missing_models: list[str] = []
    if args.step_export:
        from common.kicad_cli import export_pcb_step, missing_3d_models
        _, stderr = export_pcb_step(args.board, args.step_export)
        missing_models = missing_3d_models(stderr)
        for ref in missing_models:
            diag(f"WARNING: 3-D model missing for {ref}; substituted at export")

    findings = [
        mcad_collision_finding(placements, bodies, volumes,
                               args.board_thickness_mm),
        check_body_coverage(placements, bodies, missing_models),
    ]
    report = board_report(TOOL_NAME, args.board, findings, params={
        "enclosure": args.enclosure,
        "board_thickness_mm": args.board_thickness_mm,
        "placements": len(placements),
        "bodies_supplied": len(bodies),
        "obstruction_volumes": len(volumes),
        "missing_3d_models": missing_models,
        "step_brep_parsed": False,
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
