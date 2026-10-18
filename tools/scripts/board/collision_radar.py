#!/usr/bin/env python3
"""Placement collision radar - measures overlaps instead of looking at a picture.

Rendering the board and eyeballing it is not measurable. This tool reads each
footprint's real bounding box from ``pcbnew`` and reports part-vs-part overlap
and part-vs-Edge.Cuts overhang as numbers, with the minimum escape distance
needed to separate them. Use this to check placement; use the image tools
(``scripts/report/cross_section.py``) only for visual orientation.

backend: swig  (pcbnew)

All the collision maths lives in ``common/collision.py`` and imports NOTHING
from pcbnew, so it is tested on plain dataclasses. This file is only the
loader: it walks ``board.GetFootprints()``/``GetDrawings()``/``GetTracks()``.
The loaders themselves are duck-typed, so a fake board object exercises them
without KiCad installed - only ``pcbnew`` layer CONSTANTS need the real
module, and those are looked up lazily.

MEASUREMENT TRAPS PRESERVED
---------------------------
* ``GetBoundingBox(False, False)`` - both flags off. The default includes the
  silkscreen reference/value text, which is far larger than the real body and
  manufactures phantom collisions.
* ``PCB_VIA::GetWidth()`` is called WITH a layer argument. KiCad 10 supports
  per-layer via stacks; the no-arg overload trips a debug assert that opens a
  modal dialog and hangs a headless run. Verified against pcbnew 10.0.5.

Usage:
    collision_radar.py <board.kicad_pcb> [--json out.json] [--no-outline]
    collision_radar.py --self-test

Exit: 0 = no FAIL, 1 = at least one FAIL, 2 = run error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.collision import (  # noqa: E402
    BBox,
    TrackObstacle,
    boxes_collide,
    collision_finding,
    colliding_pairs,
    nm_to_mm,
    outside_board,
)
from common.findings import board_exit_code, board_report  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "collision-radar-report/v1"
TOOL_NAME = "collision_radar"


def footprint_boxes(board) -> dict[str, BBox]:
    """Every footprint's bounding box, in millimetres.

    ``board`` is anything with ``GetFootprints()`` - a real ``pcbnew.BOARD``
    or a test double. This function does not import pcbnew.
    """
    boxes: dict[str, BBox] = {}
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()
        bbox = footprint.GetBoundingBox(False, False)   # no silkscreen text
        boxes[ref] = BBox(
            ref=ref,
            x_min=nm_to_mm(bbox.GetLeft()),
            y_min=nm_to_mm(bbox.GetTop()),
            x_max=nm_to_mm(bbox.GetRight()),
            y_max=nm_to_mm(bbox.GetBottom()),
        )
    return boxes


def board_outline_box(board, edge_layer=None) -> BBox | None:
    """Bounding box of the Edge.Cuts drawings, or None when there is none.

    ``edge_layer`` lets a test double supply its own layer id; when omitted
    the real ``pcbnew.Edge_Cuts`` constant is imported lazily so that merely
    importing this module does not require KiCad.
    """
    if edge_layer is None:
        import pcbnew
        edge_layer = pcbnew.Edge_Cuts

    x_min = y_min = float("inf")
    x_max = y_max = float("-inf")
    found = False
    for drawing in board.GetDrawings():
        if drawing.GetLayer() != edge_layer:
            continue
        bbox = drawing.GetBoundingBox()
        found = True
        x_min = min(x_min, nm_to_mm(bbox.GetLeft()))
        y_min = min(y_min, nm_to_mm(bbox.GetTop()))
        x_max = max(x_max, nm_to_mm(bbox.GetRight()))
        y_max = max(y_max, nm_to_mm(bbox.GetBottom()))
    if not found:
        return None
    return BBox("EDGE_CUTS", x_min, y_min, x_max, y_max)


def track_obstacles(board, exclude_net: str | None = None,
                    via_type=None) -> list[TrackObstacle]:
    """Every track and via as a segment obstacle.

    ``exclude_net`` drops one net's own copper, so a net being routed does not
    treat its earlier attempts as obstacles.
    """
    if via_type is None:
        import pcbnew
        via_type = pcbnew.PCB_VIA_T

    obstacles: list[TrackObstacle] = []
    for track in board.GetTracks():
        net = track.GetNetname()
        if exclude_net is not None and net == exclude_net:
            continue
        if track.Type() == via_type:
            position = track.GetPosition()
            x, y = nm_to_mm(position.x), nm_to_mm(position.y)
            # Layer argument is MANDATORY on KiCad 10 - see module docstring.
            width = nm_to_mm(track.GetWidth(track.TopLayer()))
            obstacles.append(TrackObstacle(f"via_{net}", x, y, x, y, width))
        else:
            start, end = track.GetStart(), track.GetEnd()
            obstacles.append(TrackObstacle(
                f"trk_{net}",
                nm_to_mm(start.x), nm_to_mm(start.y),
                nm_to_mm(end.x), nm_to_mm(end.y),
                # A PCB_TRACK has one width; only PCB_VIA::GetWidth needs
                # a layer (KiCad 10 per-layer via stacks). See the module
                # docstring - the no-arg overload is a trap for VIAS only.
                nm_to_mm(track.GetWidth())))
    return obstacles


def self_test() -> list[str]:
    """Pure-geometry self test - no board, no pcbnew."""
    problems: list[str] = []

    if boxes_collide(BBox("U1", 0, 0, 2, 2), BBox("C3", 5, 5, 6, 6)) is not None:
        problems.append("non-overlapping boxes reported as a collision")

    # Edge-to-edge contact is legal placement, not a collision.
    if boxes_collide(BBox("U2", 0, 0, 2, 2), BBox("U3", 2, 0, 4, 2)) is not None:
        problems.append("edge-to-edge contact reported as a collision")

    a = BBox("U1", 0, 0, 2, 2)
    b = BBox("C3", 1.2, 0.5, 3.2, 2.5)
    overlap = boxes_collide(a, b)
    if overlap is None or abs(overlap[0] - 0.8) > 1e-6 or abs(overlap[1] - 1.5) > 1e-6:
        problems.append(f"overlap miscomputed: {overlap} (expected ~(0.8, 1.5))")

    # FAULT INJECTION: deliberately stacked parts MUST fail.
    violations = colliding_pairs({"U1": a, "C3": b})
    if not violations or violations[0]["kind"] != "COLLISION":
        problems.append("fault injection did not hold: stacked parts passed")

    board = BBox("EDGE_CUTS", 0, 0, 10, 10)
    over = outside_board({"J1": BBox("J1", -1.0, 2, 1.0, 4)}, board)
    if not over or abs(over[0]["overlap_x_mm"] - 1.0) > 1e-6:
        problems.append(f"overhang not detected correctly: {over}")
    if outside_board({"R1": BBox("R1", 1, 1, 2, 2)}, board):
        problems.append("a part inside the board reported as overhanging")

    if collision_finding({})["status"] != "NO_COVERAGE":
        problems.append("an empty part list did not report NO_COVERAGE")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--no-outline", action="store_true",
                        help="skip the Edge.Cuts overhang check")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: collision_radar")
        return 0

    if not args.board:
        parser.error("the board path is required")

    import pcbnew

    board = pcbnew.LoadBoard(args.board)
    boxes = footprint_boxes(board)
    outline = None if args.no_outline else board_outline_box(board)
    if not args.no_outline and outline is None:
        diag("WARNING: no Edge.Cuts geometry found; the overhang check is skipped")

    finding = collision_finding(boxes, outline)
    report = board_report(TOOL_NAME, args.board, [finding], params={
        "parts": len(boxes),
        "outline_checked": outline is not None,
        "outline_bbox_mm": None if outline is None else [
            outline.x_min, outline.y_min, outline.x_max, outline.y_max],
        "outline_is_bbox_not_polygon": True,
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
