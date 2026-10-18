#!/usr/bin/env python3
"""Topological route planner: detour, layer change, single-step shove.

Implements the "do not give up at the first obstacle" ladder: try the direct
path, then a single bend, then a waypoint detour around the obstacle, and only
then a via down to another layer. Each net's report records WHICH rung solved
it, so "why is there a via here" is answerable afterwards.

backend: file  (pure stdlib; the optional board writer imports pcbnew lazily)

WHY pcbnew IS IMPORTED LAZILY HERE, OUTSIDE scripts/board/
----------------------------------------------------------
The bundle confines pcbnew to ``scripts/board/`` so an import failure cannot
break collection. This module lives in ``scripts/route/`` next to the other
routers because that is what it is - a solver - and 100 % of its engineering
logic is pure geometry with zero pcbnew involvement. Only ``write_tracks()``,
one function at the very bottom, touches a board, and it imports pcbnew INSIDE
the function body. The module therefore imports, self-tests and runs its whole
CLI on a machine with no KiCad; the confinement rule's actual goal (a missing
pcbnew never breaks anything but the one thing that needs it) is met without
filing a router under "board checks".

WHAT "PUSH & SHOVE" MEANS HERE, HONESTLY
----------------------------------------
KiCad's real interactive PNS router lives in C++ and is exported to NEITHER
the SWIG ``pcbnew`` module nor the IPC API. Automating KiCad's own push-and-
shove from Python is not possible today, and claiming otherwise would be
false. What exists here is ``suggest_shove()``: displacing ONE blocking track
perpendicular to itself. No cascade, no chained shove. When the displaced
track would hit something else, the suggestion is withdrawn rather than
guessed at.

Usage:
    topological.py <requests.json> [--json out.json] [--chamfer-mm 0.25]
    topological.py --self-test

requests.json:
    {"requests": [{"net": "SDA", "start": [0,0], "end": [10,0],
                   "width_mm": 0.2, "clearance_mm": 0.2, "high_speed": false}],
     "obstacles": [{"name": "U1", "x_min": 4, "y_min": -1,
                    "x_max": 6, "y_max": 1, "clearance_mm": 0.2}],
     "lower_layer_obstacles": [...]}

Exit: 0 = every request routed, 1 = at least one FAIL or NO_COVERAGE (an empty
request set is NOT a green run), 2 = run error.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (FAIL, NO_COVERAGE, Finding, board_report,  # noqa: E402
                             result)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "topological-route-report/v1"
TOOL_NAME = "topological"

Point = tuple[float, float]


class Strategy(str, Enum):
    """Which rung of the ladder solved this net - written into the report."""

    DIRECT = "DIRECT"
    L_BEND = "L_BEND"
    U_DETOUR = "U_DETOUR"
    LAYER_CHANGE = "LAYER_CHANGE"
    NOT_FOUND = "NOT_FOUND"


@dataclass
class Obstacle:
    """A rectangle no route may cross.

    ``clearance_mm`` inflates the rectangle on every side - the simplest and
    most conservative way to fold the DRC clearance into the geometry.
    """

    name: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    clearance_mm: float = 0.2

    def inflated(self, extra_mm: float = 0.0):
        pad = self.clearance_mm + extra_mm
        return (self.x_min - pad, self.y_min - pad,
                self.x_max + pad, self.y_max + pad)

    def contains(self, point: Point, extra_mm: float = 0.0) -> bool:
        x_min, y_min, x_max, y_max = self.inflated(extra_mm)
        return x_min <= point[0] <= x_max and y_min <= point[1] <= y_max


@dataclass
class Track:
    """A track already on the board - a candidate to shove."""

    name: str
    start: Point
    end: Point
    width_mm: float
    net: str
    locked: bool = False    # high-speed / hand-drawn: NEVER moved

    def as_obstacle(self, clearance_mm: float = 0.2) -> Obstacle:
        half = self.width_mm / 2.0
        return Obstacle(
            name=f"track:{self.name}",
            x_min=min(self.start[0], self.end[0]) - half,
            y_min=min(self.start[1], self.end[1]) - half,
            x_max=max(self.start[0], self.end[0]) + half,
            y_max=max(self.start[1], self.end[1]) + half,
            clearance_mm=clearance_mm)


@dataclass
class RouteRequest:
    start: Point
    end: Point
    net: str
    width_mm: float = 0.2
    clearance_mm: float = 0.2
    layer: str = "F.Cu"
    high_speed: bool = False   # True forbids vias entirely


@dataclass
class RouteResult:
    segments: list[tuple[Point, Point]]
    strategy: Strategy
    via_count: int = 0
    layers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def length_mm(self) -> float:
        return round(sum(math.dist(a, b) for a, b in self.segments), 4)

    @property
    def found(self) -> bool:
        return self.strategy != Strategy.NOT_FOUND and bool(self.segments)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def manhattan_mm(a: Point, b: Point) -> float:
    """|dx| + |dy| - the realistic lower bound for orthogonal routing."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _segment_hits_box(a: Point, b: Point, box) -> bool:
    """Liang-Barsky slab test for segment vs AABB.

    Written out rather than pulled from a library: it is closed-form, twenty
    lines, and fully testable, and the bundle avoids dependencies.
    """
    x_min, y_min, x_max, y_max = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for delta, origin, low, high in ((dx, a[0], x_min, x_max),
                                     (dy, a[1], y_min, y_max)):
        if abs(delta) < 1e-12:
            if origin < low or origin > high:
                return False
            continue
        ta, tb = (low - origin) / delta, (high - origin) / delta
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return False
    return True


def blocking_obstacles(segments, obstacles, width_mm: float) -> list[str]:
    """Names of the obstacles this path crosses.

    Half the track width is added to the clearance: a track is a ribbon, not a
    line, so a centreline passing 0.05 mm outside a box still puts copper in it.
    """
    extra = width_mm / 2.0
    return [obstacle.name for obstacle in obstacles
            if any(_segment_hits_box(a, b, obstacle.inflated(extra))
                   for a, b in segments)]


def _direct(request: RouteRequest):
    return [(request.start, request.end)]


def _l_paths(request: RouteRequest):
    """Both single-bend paths: horizontal-first and vertical-first."""
    (x1, y1), (x2, y2) = request.start, request.end
    horizontal = [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2))]
    vertical = [((x1, y1), (x1, y2)), ((x1, y2), (x2, y2))]
    return [p for p in (horizontal, vertical) if any(a != b for a, b in p)]


def _u_paths(request: RouteRequest, obstacles):
    """Detour candidates that run along one side of each obstacle."""
    (x1, y1), (x2, y2) = request.start, request.end
    extra = request.width_mm / 2.0 + request.clearance_mm
    candidates = []
    for obstacle in obstacles:
        x_min, y_min, x_max, y_max = obstacle.inflated(extra)
        for corridor_x in (x_min, x_max):
            candidates.append([((x1, y1), (corridor_x, y1)),
                               ((corridor_x, y1), (corridor_x, y2)),
                               ((corridor_x, y2), (x2, y2))])
        for corridor_y in (y_min, y_max):
            candidates.append([((x1, y1), (x1, corridor_y)),
                               ((x1, corridor_y), (x2, corridor_y)),
                               ((x2, corridor_y), (x2, y2))])
    return [[(a, b) for a, b in path if a != b] for path in candidates]


def find_path(request: RouteRequest, obstacles=(), lower_layer: str = "B.Cu",
              lower_layer_obstacles=None) -> RouteResult:
    """The ladder, in order; the first rung that works wins.

    1. DIRECT       - one straight segment.
    2. L_BEND       - one corner, same layer.
    3. U_DETOUR     - waypoints around the obstacle; shortest clean candidate.
    4. LAYER_CHANGE - via down and back.

    ``high_speed`` SKIPS rung 4 entirely and returns NOT_FOUND: a high-speed
    signal must stay on its reference layer, so the answer is a placement
    change, not a via. NOT_FOUND is not "gave up at the first DRC error" -
    every rung was tried, and ``notes`` records what blocked each one.
    """
    notes: list[str] = []

    direct = _direct(request)
    blockers = blocking_obstacles(direct, obstacles, request.width_mm)
    if not blockers:
        return RouteResult(direct, Strategy.DIRECT, 0, [request.layer], notes)
    notes.append(f"direct path blocked by: {', '.join(blockers)}")

    for path in _l_paths(request):
        if not blocking_obstacles(path, obstacles, request.width_mm):
            return RouteResult(path, Strategy.L_BEND, 0, [request.layer], notes)
    notes.append("both L bends blocked")

    clean = [p for p in _u_paths(request, obstacles)
             if p and not blocking_obstacles(p, obstacles, request.width_mm)]
    if clean:
        shortest = min(clean, key=lambda p: sum(math.dist(a, b) for a, b in p))
        return RouteResult(shortest, Strategy.U_DETOUR, 0, [request.layer], notes)
    notes.append("no clean waypoint detour found")

    if request.high_speed:
        notes.append("LAYER CHANGE SKIPPED: high-speed net, vias forbidden. "
                     "The fix belongs in placement, not routing.")
        return RouteResult([], Strategy.NOT_FOUND, 0, [], notes)

    # An unsupplied lower-layer obstacle list is NOT assumed to be empty; that
    # would be the most dangerous assumption available here.
    if lower_layer_obstacles is None:
        notes.append(f"no obstacle list given for {lower_layer}; the lower layer "
                     "was NOT assumed empty, so no layer change was proposed "
                     "(NEEDS_HUMAN).")
        return RouteResult([], Strategy.NOT_FOUND, 0, [], notes)

    lower_request = RouteRequest(request.start, request.end, request.net,
                                 request.width_mm, request.clearance_mm,
                                 lower_layer, False)
    for candidate in [_direct(lower_request), *_l_paths(lower_request)]:
        if not blocking_obstacles(candidate, lower_layer_obstacles,
                                  request.width_mm):
            return RouteResult(
                candidate, Strategy.LAYER_CHANGE, 2,
                [request.layer, lower_layer, request.layer],
                notes + [f"dropped to {lower_layer} (2 vias: down and up)"])

    notes.append(f"no clean path on {lower_layer} either")
    return RouteResult([], Strategy.NOT_FOUND, 0, [], notes)


# --------------------------------------------------------------------------- #
# 45-degree conversion
# --------------------------------------------------------------------------- #
def _unit(a: Point, b: Point):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return None
    return (dx / length, dy / length)


def right_angle_count(segments) -> int:
    """Remaining 90-degree corners - the measurable proof the chamfer worked."""
    count = 0
    for (a1, b1), (a2, b2) in zip(segments, segments[1:]):
        u1, u2 = _unit(a1, b1), _unit(a2, b2)
        if u1 is None or u2 is None:
            continue
        if abs(u1[0] * u2[0] + u1[1] * u2[1]) < 1e-9:
            count += 1
    return count


def chamfer_45(segments, chamfer_mm: float = 0.25):
    """Replace each 90-degree corner with a 45-degree chamfer.

    The path search works orthogonally on purpose - obstacle geometry is AABB
    based and intersection tests are exact on axis-aligned paths - so the
    45-degree conversion is a SEPARATE, final step.

    A chamfer larger than half a neighbouring segment would reverse it, so at
    such a corner the chamfer SHRINKS to fit rather than silently producing
    broken geometry.
    """
    if chamfer_mm <= 0:
        raise ValueError("chamfer_mm must be positive")
    if len(segments) < 2:
        return list(segments)

    out: list[tuple[Point, Point]] = []
    remaining = [(tuple(a), tuple(b)) for a, b in segments]
    previous_end: Point | None = None

    for i, (a, b) in enumerate(remaining):
        start = previous_end if previous_end is not None else a
        end = b
        if i + 1 < len(remaining):
            next_a, next_b = remaining[i + 1]
            u1, u2 = _unit(start, end), _unit(next_a, next_b)
            if (u1 is not None and u2 is not None
                    and abs(u1[0] * u2[0] + u1[1] * u2[1]) < 1e-9):
                cut = min(chamfer_mm, math.dist(start, end) / 2.0,
                          math.dist(next_a, next_b) / 2.0)
                if cut > 1e-9:
                    corner = end
                    chamfer_start = (corner[0] - u1[0] * cut, corner[1] - u1[1] * cut)
                    chamfer_end = (corner[0] + u2[0] * cut, corner[1] + u2[1] * cut)
                    out.append((start, chamfer_start))
                    out.append((chamfer_start, chamfer_end))
                    previous_end = chamfer_end
                    continue
        out.append((start, end))
        previous_end = end

    return [(a, b) for a, b in out if a != b]


# --------------------------------------------------------------------------- #
# Single-step shove
# --------------------------------------------------------------------------- #
def suggest_shove(blocker: Track, request: RouteRequest, other_obstacles=(),
                  extra_mm: float = 0.0) -> Track | None:
    """Displace ONE blocking track perpendicular to itself. No cascade.

    Returns None (caller reports NEEDS_HUMAN) when:
      * the blocker is locked - a hand-drawn or high-speed track is never
        moved by this tool,
      * the displaced track would hit another obstacle - chained shoves are
        not attempted,
      * the blocker is diagonal - "perpendicular" would be an arbitrary
        choice there.
    """
    if blocker.locked:
        return None
    direction = _unit(blocker.start, blocker.end)
    if direction is None:
        return None
    horizontal = abs(direction[1]) < 1e-9
    vertical = abs(direction[0]) < 1e-9
    if not (horizontal or vertical):
        return None

    needed = (blocker.width_mm / 2.0 + request.width_mm / 2.0
              + request.clearance_mm + extra_mm)

    if horizontal:
        track_y = blocker.start[1]
        target_y = (request.start[1] + request.end[1]) / 2.0
        current = abs(target_y - track_y)
        if current >= needed:
            return None                      # not actually blocking
        shift = (needed - current) * (-1.0 if target_y >= track_y else 1.0)
        moved = Track(blocker.name, (blocker.start[0], track_y + shift),
                      (blocker.end[0], blocker.end[1] + shift),
                      blocker.width_mm, blocker.net)
    else:
        track_x = blocker.start[0]
        target_x = (request.start[0] + request.end[0]) / 2.0
        current = abs(target_x - track_x)
        if current >= needed:
            return None
        shift = (needed - current) * (-1.0 if target_x >= track_x else 1.0)
        moved = Track(blocker.name, (track_x + shift, blocker.start[1]),
                      (blocker.end[0] + shift, blocker.end[1]),
                      blocker.width_mm, blocker.net)

    if blocking_obstacles([(moved.start, moved.end)],
                          [o for o in other_obstacles
                           if o.name != f"track:{blocker.name}"],
                          moved.width_mm):
        return None
    return moved


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def check_via_ban(results: dict[str, RouteResult], high_speed_nets) -> Finding:
    """No via on a high-speed net.

    ``find_path`` already refuses, but paths can arrive from a hand edit or
    another router, so the gate stands on its own.
    """
    violations = []
    scanned = 0
    for net in high_speed_nets:
        if net not in results:
            continue
        scanned += 1
        if results[net].via_count > 0:
            violations.append({"net": net, "via_count": results[net].via_count})
    return result("high_speed_via_ban", scanned, violations,
                  "high-speed signals route without changing layer")


def check_geometry(results: dict[str, RouteResult]) -> Finding:
    """No 90-degree corner left in any path."""
    violations = []
    scanned = 0
    for net, route in results.items():
        if not route.segments:
            continue
        scanned += 1
        count = right_angle_count(route.segments)
        if count:
            violations.append({"net": net, "right_angles": count})
    return result("45_degree_geometry", scanned, violations,
                  "every turn must be 45 degrees or an arc")


def check_routability(results: dict[str, RouteResult]) -> Finding:
    """Nets the ladder could not solve at all."""
    violations = [
        {"net": net, "notes": route.notes}
        for net, route in results.items() if not route.found
    ]
    return result("route_found", len(results), violations,
                  "every requested net must reach one of DIRECT / L_BEND / "
                  "U_DETOUR / LAYER_CHANGE")


def plan_row(net: str, route: RouteResult) -> dict:
    """One routing-plan record, ready to serialise."""
    return {
        "net": net,
        "strategy": route.strategy.value,
        "layers": route.layers,
        "via_count": route.via_count,
        "length_mm": route.length_mm,
        "segments": [[list(a), list(b)] for a, b in route.segments],
        "segment_count": len(route.segments),
        "notes": route.notes,
    }


# --------------------------------------------------------------------------- #
# Board writing - the ONLY pcbnew user, imported lazily inside the function
# --------------------------------------------------------------------------- #
def write_tracks(board_path: str, net_name: str, width_mm: float,
                 segments, layer: str = "F.Cu") -> int:
    """Append the path to a board as PCB_TRACKs. Returns the count added.

    Imports pcbnew inside the body on purpose: the rest of this module - the
    entire solver and CLI - runs with no KiCad installed.
    """
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            "pcbnew could not be imported; only write_tracks() needs it. Run "
            "this under a Python that can import the KiCad SWIG module.") from exc

    board = pcbnew.LoadBoard(board_path)
    net = board.FindNet(net_name)
    if net is None:
        raise ValueError(f"net not found: {net_name}")
    layer_id = board.GetLayerID(layer)
    added = 0
    for a, b in segments:
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
        track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
        track.SetWidth(pcbnew.FromMM(width_mm))
        track.SetLayer(layer_id)
        track.SetNet(net)
        board.Add(track)
        added += 1
    board.Save(board_path)
    return added


# --------------------------------------------------------------------------- #
def load_requests(path: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    requests = [RouteRequest(
        start=tuple(r["start"]), end=tuple(r["end"]), net=r["net"],
        width_mm=float(r.get("width_mm", 0.2)),
        clearance_mm=float(r.get("clearance_mm", 0.2)),
        layer=r.get("layer", "F.Cu"),
        high_speed=bool(r.get("high_speed", False)))
        for r in data.get("requests", [])]
    obstacles = [Obstacle(
        name=o["name"], x_min=float(o["x_min"]), y_min=float(o["y_min"]),
        x_max=float(o["x_max"]), y_max=float(o["y_max"]),
        clearance_mm=float(o.get("clearance_mm", 0.2)))
        for o in data.get("obstacles", [])]
    lower = data.get("lower_layer_obstacles")
    lower_obstacles = None if lower is None else [Obstacle(
        name=o["name"], x_min=float(o["x_min"]), y_min=float(o["y_min"]),
        x_max=float(o["x_max"]), y_max=float(o["y_max"]),
        clearance_mm=float(o.get("clearance_mm", 0.2))) for o in lower]
    return requests, obstacles, lower_obstacles, data.get("lower_layer", "B.Cu")


def self_test() -> list[str]:
    problems: list[str] = []

    straight = find_path(RouteRequest((0, 0), (10, 0), "SIG"))
    if straight.strategy != Strategy.DIRECT:
        problems.append(f"expected DIRECT on a clear path, got {straight.strategy}")

    barrier = Obstacle("barrier", 4.0, -1.0, 6.0, 1.0)
    around = find_path(RouteRequest((0, 0), (10, 0), "SIG"), [barrier])
    if not around.found or around.via_count != 0:
        problems.append("no via-free detour found around a barrier")

    wall = Obstacle("wall", -100.0, -1.0, 100.0, 1.0)
    fast = find_path(RouteRequest((0, 0), (10, 0), "MIPI_D0_P", high_speed=True),
                     [wall])
    if fast.via_count != 0 or fast.strategy != Strategy.NOT_FOUND:
        problems.append("the via ban was not applied on a high-speed net")

    path = [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (5.0, 5.0))]
    if right_angle_count(chamfer_45(path)) != 0:
        problems.append("a right angle survived the 45-degree conversion")

    # FAULT INJECTION: an obstacle placed right on the path MUST be reported.
    if not blocking_obstacles([((0.0, 0.0), (10.0, 0.0))],
                              [Obstacle("bar", 4.0, -5.0, 6.0, 5.0, 0.2)], 0.2):
        problems.append("fault injection did not hold: obstacle detection is empty")

    # A locked track is never shoved.
    locked = Track("hs", (0, 0), (10, 0), 0.2, "USB_DP", locked=True)
    if suggest_shove(locked, RouteRequest((0, 0.1), (10, 0.1), "SIG")) is not None:
        problems.append("a locked track was shoved")
    # A diagonal track is never shoved either.
    diagonal = Track("d", (0, 0), (10, 10), 0.2, "N")
    if suggest_shove(diagonal, RouteRequest((0, 0), (10, 0), "SIG")) is not None:
        problems.append("a diagonal track was shoved")

    if check_routability({})["status"] != "NO_COVERAGE":
        problems.append("an empty request set did not report NO_COVERAGE")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requests", nargs="?",
                        help="JSON with requests, obstacles, "
                             "lower_layer_obstacles")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--chamfer-mm", type=float, default=0.25)
    parser.add_argument("--no-chamfer", action="store_true",
                        help="leave the orthogonal path as-is (the 45-degree "
                             "gate will then FAIL - that is the point)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: topological")
        return 0

    if not args.requests:
        parser.error("the routing request JSON is required")

    requests, obstacles, lower_obstacles, lower_layer = load_requests(args.requests)
    results: dict[str, RouteResult] = {}
    for request in requests:
        route = find_path(request, obstacles, lower_layer, lower_obstacles)
        if route.segments and not args.no_chamfer:
            route.segments = chamfer_45(route.segments, args.chamfer_mm)
        results[request.net] = route

    high_speed = [r.net for r in requests if r.high_speed]
    findings = [
        check_routability(results),
        check_via_ban(results, high_speed),
        check_geometry(results),
    ]
    report = board_report(TOOL_NAME, args.requests, findings, params={
        "requests": len(requests),
        "obstacles": len(obstacles),
        "lower_layer": lower_layer,
        "lower_layer_obstacles_supplied": lower_obstacles is not None,
        "chamfer_mm": None if args.no_chamfer else args.chamfer_mm,
        "plan": [plan_row(net, route) for net, route in sorted(results.items())],
        "push_and_shove": "single step only; KiCad's own PNS router is not "
                          "exposed to Python and is NOT used here",
    }, schema_version=SCHEMA_VERSION)
    report["request"] = report.pop("board")
    write_report(report, args.json)
    emit(report)
    # A FAIL anywhere is non-zero, and so is a routability check that never ran:
    # an empty request set means nothing was routed, and a chain reading exit 0
    # would take that for "everything routed" - exactly the reading grid_astar
    # already refuses. The OTHER two gates are allowed to be NO_COVERAGE without
    # forcing exit 1: a board with no high-speed net legitimately leaves the via
    # ban unscanned, and that is not a missing routing result.
    if report["summary"][FAIL]:
        return 1
    routability = next(c for c in report["checks"] if c["check"] == "route_found")
    return 1 if routability["status"] == NO_COVERAGE else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
