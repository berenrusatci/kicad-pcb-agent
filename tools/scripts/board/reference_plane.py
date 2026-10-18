#!/usr/bin/env python3
"""Checks that every high-speed trace segment has an unbroken reference
plane directly beneath it - a split/void under a high-speed trace breaks
signal integrity even when length matching is otherwise clean.

Checks BOTH endpoints of each segment AND its midpoint against the
same-layer reference-plane polygon: any of the three points landing outside
the polygon means that segment crosses a split or void.

This is geometry-only - it takes already-extracted trace segments and plane
polygons (``TraceSegment``/``PlanePolygon``), it does not itself read a
``.kicad_pcb`` or a zone's real fill shape. A caller that has a real board
should extract these from ``common.boardgeom``/pcbnew zone outlines first;
this checker's job is the geometric test, not the extraction.

Usage:
    reference_plane.py <segments.json> <planes.json> [--reference-net GND]
                       [--json out.json]
    reference_plane.py --self-test

``segments.json``: ``[{"net_name", "layer", "x1", "y1", "x2", "y2"}, ...]``
``planes.json``: ``[{"layer", "net_name", "points_mm": [[x, y], ...]}, ...]``

Exit codes: 0 = every segment has an unbroken reference plane beneath it,
1 = at least one segment crosses a split/void or is missing a plane
definition (or NO_COVERAGE), 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.boardgeom import point_in_polygon  # noqa: E402
from common.findings import result  # noqa: E402
from common.report_io import emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "reference_plane"


@dataclass
class PlanePolygon:
    layer: str
    net_name: str
    points_mm: List[Tuple[float, float]]  # (x, y) mm, closed polygon


@dataclass
class TraceSegment:
    net_name: str
    layer: str
    x1: float
    y1: float
    x2: float
    y2: float


def check_reference_plane_continuity(high_speed_segments: List[TraceSegment],
                                     planes: List[PlanePolygon],
                                     reference_net: str = "GND") -> List[dict]:
    """Checks BOTH endpoints and the MIDPOINT of every high-speed segment
    against the same-layer ``reference_net`` plane polygon. A point outside
    the polygon means that segment's copper has no continuous reference
    plane beneath it there (crossing a split/void).

    Acceptance criterion: the returned list should be EMPTY - a violation
    here breaks signal integrity even when length matching is clean
    (skills/highspeed-routing's own coverage-gate discipline).
    """
    findings: List[dict] = []
    for segment in high_speed_segments:
        matching_plane = next(
            (p for p in planes if p.layer == segment.layer and p.net_name == reference_net),
            None)
        if matching_plane is None:
            findings.append({
                "net": segment.net_name, "layer": segment.layer,
                "reason": f"no '{reference_net}' reference-plane definition found "
                         f"on layer {segment.layer}.",
            })
            continue

        mid_x, mid_y = (segment.x1 + segment.x2) / 2, (segment.y1 + segment.y2) / 2
        points = [(segment.x1, segment.y1), (mid_x, mid_y), (segment.x2, segment.y2)]
        for px, py in points:
            if not point_in_polygon(px, py, matching_plane.points_mm):
                findings.append({
                    "net": segment.net_name, "layer": segment.layer,
                    "point_mm": [round(px, 4), round(py, 4)],
                    "reason": f"point is OUTSIDE the '{reference_net}' plane - crosses a "
                             "split/void. Add a GND->GND stitching via at the layer "
                             "transition, or an adjacent GND->PWR 100nF capacitor.",
                })
                break  # report once per segment, do not pile on the same segment
    return findings


def check_reference_plane_continuity_finding(high_speed_segments: List[TraceSegment],
                                              planes: List[PlanePolygon],
                                              reference_net: str = "GND"):
    """``check_reference_plane_continuity()`` wrapped as a Finding -
    ``scanned=0`` (no high-speed segments given) is NO_COVERAGE, never a
    silent PASS."""
    violations = check_reference_plane_continuity(high_speed_segments, planes, reference_net)
    return result("reference_plane_continuity", len(high_speed_segments), violations,
                 f"reference_net={reference_net}, {len(high_speed_segments)} high-speed "
                 f"segment(s) checked against {len(planes)} plane polygon(s).")


def _self_test() -> int:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    plane = PlanePolygon(layer="In1.Cu", net_name="GND", points_mm=square)

    clean_segment = TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 9.0, 9.0)
    clean = check_reference_plane_continuity([clean_segment], [plane])
    assert clean == [], f"a segment fully inside the plane should have no findings: {clean}"

    crossing_segment = TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 15.0, 1.0)  # exits at x=15
    crossing = check_reference_plane_continuity([crossing_segment], [plane])
    assert len(crossing) == 1 and "OUTSIDE" in crossing[0]["reason"], \
        f"a segment crossing the plane boundary must be caught: {crossing}"

    missing_plane_segment = TraceSegment("USB_DP", "F.Cu", 1.0, 1.0, 5.0, 5.0)  # no F.Cu plane
    missing = check_reference_plane_continuity([missing_plane_segment], [plane])
    assert len(missing) == 1 and "no" in missing[0]["reason"], \
        f"a layer with no reference plane at all must be caught: {missing}"

    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("segments", nargs="?")
    ap.add_argument("planes", nargs="?")
    ap.add_argument("--reference-net", default="GND")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.segments or not a.planes:
        ap.error("segments.json and planes.json are required unless --self-test is given")

    raw_segments = json.loads(Path(a.segments).read_text(encoding="utf-8"))
    raw_planes = json.loads(Path(a.planes).read_text(encoding="utf-8"))
    segments = [TraceSegment(s["net_name"], s["layer"], s["x1"], s["y1"], s["x2"], s["y2"])
               for s in raw_segments]
    planes = [PlanePolygon(p["layer"], p["net_name"],
                           [tuple(pt) for pt in p["points_mm"]]) for p in raw_planes]

    finding = check_reference_plane_continuity_finding(segments, planes, a.reference_net)
    report = {
        "schema_version": SCHEMA_VERSION, "tool": TOOL_NAME, "checks": [finding],
        "params": {"reference_net": a.reference_net},
        "summary": {s: sum(1 for f in [finding] if f["status"] == s)
                   for s in ("PASS", "FAIL", "NO_COVERAGE")},
    }
    write_report(report, a.json)
    emit(report)
    # The docstring above already promised "(or NO_COVERAGE)" exits 1; the
    # code branched on FAIL alone and contradicted it. An empty segments.json
    # makes check_reference_plane_continuity_finding return NO_COVERAGE
    # (scanned=0), and exit 0 there reads as "every segment has an unbroken
    # plane beneath it" when no segment was examined at all.
    summary = report["summary"]
    return 1 if summary["FAIL"] or summary["NO_COVERAGE"] else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
