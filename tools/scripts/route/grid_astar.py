#!/usr/bin/env python3
"""LAST-RESORT pure-Python grid A* router.

Port of ``otonom_python_router.py``. This does NOT use KiCad's own
``route_trace``/PNS interactive router core. It is the search engine that
takes over when a heuristic routing ladder (straight / L-turn / U-turn /
layer change) is exhausted — i.e. when a corridor is so cluttered that none
of the few candidate shapes comes out clean.

WHY A SEPARATE ENGINE (behind the heuristic ladder, not instead of it)
----------------------------------------------------------------------
A heuristic ladder tries a small number of candidate paths. That is FAST and
usually enough, but in a genuinely maze-like region (many overlapping
obstacles) no candidate may come out clean — that is the ladder's DESIGN
LIMIT, not a bug. This module offers an EXHAUSTIVE search: obstacles are
rasterised onto a grid and every possible 8-direction path between start and
end is explored with A*. Slower, but far more stubborn. It is the last
AUTOMATIC step; only when it also fails may a caller report NEEDS_HUMAN.

ARCHITECTURE — the search is pure Python, any board writing is elsewhere
-----------------------------------------------------------------------
``grid_astar_search`` imports no CAD library at all. It works on plain
``(x, y)`` tuples and on any obstacle object exposing
``x_min/y_min/x_max/y_max``, so it is fully testable without KiCad
installed.

NOT PORTED (said plainly rather than shipped half-working): the source's
``duz_izleri_pcbnew_ile_yaz()`` board-writing layer is absent here. It
delegated to a topological-router bridge module that is not part of this
bundle, and reimplementing that write path from memory would be exactly the
kind of unverified code this codebase refuses to carry. This module produces
polylines; writing them to a board is a separate, still-missing tool.

LIMIT (honesty note): an 8-direction grid A* ROUNDS real continuous geometry
to ``cell_mm``. A corridor narrower than ``cell_mm`` can be missed, so
``cell_mm`` must be chosen SMALLER THAN HALF of (trace width + clearance)
— the default 0.1 mm is safe for a typical 0.2-0.25 mm trace. A caller in a
denser region should shrink it further; search time then grows
quadratically, which is why ``max_nodes`` is a MANDATORY upper bound: an
unbounded search is never silently allowed.

TUR 3 ADDITION — layer-aware, coupled-pair and SI-aware search:

The source's basic ``izgara_a_yildiz_ara`` was ported at Tur 1/2 as
``grid_astar_search`` above. Three genuinely new search capabilities were
added later in the archive and are ported here as library functions (none of
them are wired into this file's own JSON-driven CLI, which still covers only
the single-layer/single-net case; a caller with a multi-layer or diff-pair
problem calls these directly, the same way ``common/stackup.py`` carries
library functions its own CLI does not all exercise):

  * ``via_placement_valid`` / ``ViaPlacementCheck`` — annular-ring and
    hole-to-hole validity for a via CANDIDATE, added after a real incident (a
    0.5 mm via at a 0.4 mm pin pitch produced 221 DRC violations because nothing
    checked its distance to neighbouring pad holes).
  * ``si_cost_function`` / ``via_impedance_discontinuity_cost`` — optional
    ``extra_cost_fn`` closures (``grid_astar_search`` gained an
    ``extra_cost_fn`` parameter to carry them) that penalise a candidate step
    for violating the 3W crosstalk rule, or scale a via's cost by how far the
    stackup solver's geometry already deviates from its target impedance.
    Both CONSUME numbers computed elsewhere (``common.stackup``) rather than
    recomputing them — single source of truth stays with the stackup solver.
  * ``layered_astar_search`` — the ``(cell, layer)`` extension: search across
    multiple PCB layers, pricing a via transition and gating it through
    ``via_placement_valid`` so an unmanufacturable via is never searched
    through, not filtered out afterwards.
  * ``coupled_astar_search`` — routes a differential pair as ONE centreline
    search at a fixed ``gap_mm``, then offsets the found path into P/N
    traces (``_pair_from_centerline``), rather than routing two independent
    nets that could drift apart. Complements (not replaces) ``route/
    diff_pair.py``, which works the other way round: it locks/verifies an
    ALREADY-drawn pair via KiCad's own interactive router.

Rendered board-writing for the layer/coupled paths is out of scope here for
the same reason as the base search: see NOT PORTED above.

Usage:
    python grid_astar.py <problem.json> [--json out.json]
                         [--cell-mm 0.1] [--clearance-mm 0.2]
                         [--max-nodes 200000] [--self-test]

Exit codes: 0 = every net routed, 1 = a net failed to route or there were no
nets at all (NO_COVERAGE), 2 = the tool could not produce an answer.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.boardgeom import seg_point_dist  # noqa: E402
from common.findings import NO_COVERAGE, result, summarise  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_FAIL,
    EXIT_OK,
    diag,
    emit,
    run_cli,
    write_report,
)
from common.tuning import routing_costs  # noqa: E402

SCHEMA_VERSION = "route-result/v1"
TOOL_NAME = "grid_astar"
ROUTING_CHECK = "grid_astar_routing"

Point = Tuple[float, float]
GridCell = Tuple[int, int]

DEFAULT_CELL_MM = 0.1
DEFAULT_CLEARANCE_MM = 0.2
DEFAULT_MAX_NODES = 200_000


class BoxLike(Protocol):
    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass
class SearchResult:
    path: List[Point]
    nodes_visited: int
    found: bool
    reason: str = ""


@dataclass
class BoxObstacle:
    """A rigid axis-aligned obstacle (a component courtyard, a keepout).

    It has no segment fields, so it never enters the narrow phase of the
    collision test and keeps the original pure-AABB behaviour exactly.
    """

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    ref: str = ""


@dataclass
class SegmentObstacle:
    """A copper track (or a via, as a zero-length segment) as a real segment.

    ``x_min/y_min/x_max/y_max`` give the broad-phase AABB, so this satisfies
    the same ``BoxLike`` protocol as ``BoxObstacle`` (duck typing — no new
    type check is needed anywhere). The narrow-phase test is triggered by the
    PRESENCE of the ``x1``/``width_mm`` attributes.

    A via (a point obstacle) is represented with ``x1 == x2, y1 == y2``; the
    point-to-segment distance reduces to a point-to-point distance on its
    own, so no special case is needed.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    width_mm: float
    ref: str = ""

    @property
    def x_min(self) -> float:
        return min(self.x1, self.x2) - self.width_mm / 2.0

    @property
    def x_max(self) -> float:
        return max(self.x1, self.x2) + self.width_mm / 2.0

    @property
    def y_min(self) -> float:
        return min(self.y1, self.y2) - self.width_mm / 2.0

    @property
    def y_max(self) -> float:
        return max(self.y1, self.y2) + self.width_mm / 2.0


# --------------------------------------------------------------------------- #
# 1. Grid + obstacle mapping (no CAD dependency)
# --------------------------------------------------------------------------- #
def _to_cell(point: Point, cell_mm: float) -> GridCell:
    return (round(point[0] / cell_mm), round(point[1] / cell_mm))


def _to_point(cell: GridCell, cell_mm: float) -> Point:
    return (cell[0] * cell_mm, cell[1] * cell_mm)


def cell_blocked(cell: GridCell, obstacles: Sequence[BoxLike],
                 cell_mm: float, clearance_mm: float) -> bool:
    """TWO-PHASE collision test.

    1. Broad phase (the original behaviour, PRESERVED EXACTLY): if the point
       is outside the obstacle's AABB, move on to the next obstacle — a
       cheap, fast pre-filter.
    2. Narrow phase: if the point IS inside the AABB and the obstacle is a
       SEGMENT obstacle (detected by the PRESENCE of the ``x1``/``width_mm``
       attributes via ``hasattr`` — no new type import and no isinstance
       check), the true perpendicular distance from the point to the segment
       is measured. If that distance exceeds (width/2 + clearance) the point
       is SAFE. This stops the AABB of a 45-degree diagonal track from
       falsely marking the large empty triangles near its corners as
       occupied.

    Box obstacles do NOT have those fields and therefore never enter the
    narrow phase — they stay rigid bodies and the OLD pure-AABB behaviour is
    preserved for them bit for bit.
    """
    x, y = _to_point(cell, cell_mm)
    for obstacle in obstacles:
        if not ((obstacle.x_min - clearance_mm) <= x <= (obstacle.x_max + clearance_mm)
                and (obstacle.y_min - clearance_mm) <= y <= (obstacle.y_max + clearance_mm)):
            continue  # broad phase: outside the AABB -> this obstacle is clear
        if hasattr(obstacle, "x1") and hasattr(obstacle, "width_mm"):
            distance = seg_point_dist((x, y),
                                      (obstacle.x1, obstacle.y1),
                                      (obstacle.x2, obstacle.y2))
            if distance > obstacle.width_mm / 2.0 + clearance_mm:
                # narrow phase: inside the AABB but far enough from the real line
                continue
        return True
    return False


# 8 directions: 4 axial (cost 1) + 4 diagonal (cost sqrt(2)). Consistent with
# a 45-degree routing preference and it cuts the number of right-angle turns
# the grid would otherwise force.
_DIRECTIONS: Tuple[Tuple[int, int, float], ...] = (
    (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
    (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2)),
)


def _heuristic(a: GridCell, b: GridCell) -> float:
    """Octile distance — the ADMISSIBLE (never over-estimating) heuristic for
    an 8-direction grid. Manhattan would be WRONG here: it ignores diagonal
    movement and can make A* return a non-optimal path."""
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    return (dx + dy) + (math.sqrt(2) - 2) * min(dx, dy)


# --------------------------------------------------------------------------- #
# 2. A* search (pure Python)
# --------------------------------------------------------------------------- #
def grid_astar_search(
    start: Point,
    end: Point,
    obstacles: Sequence[BoxLike] = (),
    cell_mm: float = DEFAULT_CELL_MM,
    clearance_mm: float = DEFAULT_CLEARANCE_MM,
    max_nodes: int = DEFAULT_MAX_NODES,
    extra_cost_fn: Optional[Callable[[Point, Point], float]] = None,
) -> SearchResult:
    """Shortest 8-direction grid path between ``start`` and ``end``.

    Obstacles may be any object exposing ``x_min/y_min/x_max/y_max``; no
    conversion step is required.

    If ``max_nodes`` is exceeded the search STOPS and returns
    ``found=False`` with an explanatory ``reason`` — it never keeps searching
    silently forever (see the LIMIT note in the module docstring).

    ``extra_cost_fn``, when given, is called with the (x, y) millimetre
    coordinates of each candidate step's two endpoints and its return value is
    ADDED to that step's base movement cost — e.g. ``si_cost_function`` below,
    for penalising a candidate step that would run too close to another
    high-speed trace. It never changes which cells are blocked, only how
    expensive an open one is.
    """
    if cell_mm <= 0:
        raise ValueError(f"cell_mm must be positive, got {cell_mm!r}")
    if clearance_mm < 0:
        raise ValueError(f"clearance_mm must not be negative, got {clearance_mm!r}")
    if max_nodes <= 0:
        raise ValueError(f"max_nodes must be positive, got {max_nodes!r}")

    start_cell = _to_cell(start, cell_mm)
    end_cell = _to_cell(end, cell_mm)

    if cell_blocked(start_cell, obstacles, cell_mm, clearance_mm):
        return SearchResult([], 0, False, "start point lies in a blocked region")
    if cell_blocked(end_cell, obstacles, cell_mm, clearance_mm):
        return SearchResult([], 0, False, "end point lies in a blocked region")
    if start_cell == end_cell:
        return SearchResult([start, end], 0, True, "start and end share one cell")

    open_heap: List[Tuple[float, float, GridCell]] = [
        (_heuristic(start_cell, end_cell), 0.0, start_cell)]
    came_from: Dict[GridCell, Optional[GridCell]] = {start_cell: None}
    g_score: Dict[GridCell, float] = {start_cell: 0.0}
    visited: set = set()

    while open_heap:
        if len(visited) > max_nodes:
            return SearchResult([], len(visited), False,
                                f"node budget ({max_nodes}) exceeded")

        _, current_g, current = heapq.heappop(open_heap)
        if current in visited:
            continue
        visited.add(current)

        if current == end_cell:
            cells: List[GridCell] = []
            node: Optional[GridCell] = current
            while node is not None:
                cells.append(node)
                node = came_from[node]
            cells.reverse()
            path = [_to_point(cell, cell_mm) for cell in cells]
            path[0] = start
            path[-1] = end
            return SearchResult(simplify_path(path), len(visited), True, "routed")

        for dx, dy, cost in _DIRECTIONS:
            neighbour = (current[0] + dx, current[1] + dy)
            if neighbour in visited:
                continue
            if cell_blocked(neighbour, obstacles, cell_mm, clearance_mm):
                continue
            step_cost = cost
            if extra_cost_fn is not None:
                step_cost += extra_cost_fn(_to_point(current, cell_mm),
                                           _to_point(neighbour, cell_mm))
            candidate_g = current_g + step_cost
            if candidate_g < g_score.get(neighbour, float("inf")):
                g_score[neighbour] = candidate_g
                came_from[neighbour] = current
                heapq.heappush(
                    open_heap,
                    (candidate_g + _heuristic(neighbour, end_cell),
                     candidate_g, neighbour))

    return SearchResult([], len(visited), False,
                        "no obstacle-free path found (search exhausted)")


def simplify_path(path: List[Point]) -> List[Point]:
    """Merge consecutive COLLINEAR points — reduces the grid's staircase
    polyline to straight runs so the real trace count drops. No extra vias or
    corners are introduced; this is point reduction only."""
    if len(path) < 3:
        return path
    out = [path[0]]
    for index in range(1, len(path) - 1):
        a, b, c = out[-1], path[index], path[index + 1]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        if abs(cross) < 1e-9:
            continue  # collinear - drop b
        out.append(b)
    out.append(path[-1])
    return out


def path_length_mm(path: Sequence[Point]) -> float:
    return sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))


# --------------------------------------------------------------------------- #
# 3. Via placement validity (annular ring + hole-to-hole)
# --------------------------------------------------------------------------- #
# Added after a real incident: a 0.5 mm via at a 0.4 mm pin pitch produced 221
# DRC violations because nothing checked a via candidate's distance to
# NEIGHBOURING holes before it was placed. This gates via candidates BEFORE
# the search considers them (see layered_astar_search below), not after.
@dataclass(frozen=True)
class ViaPlacementCheck:
    via_diameter_mm: float
    hole_diameter_mm: float
    min_annular_ring_mm: float = 0.15
    min_hole_to_hole_mm: float = 0.2

    @property
    def annular_ring_mm(self) -> float:
        return (self.via_diameter_mm - self.hole_diameter_mm) / 2.0

    @property
    def annular_ring_sufficient(self) -> bool:
        return self.annular_ring_mm >= self.min_annular_ring_mm


def via_placement_valid(
        candidate: Point, via_diameter_mm: float, hole_diameter_mm: float,
        neighbour_holes: Sequence[Tuple[float, float, float]] = (),
        min_annular_ring_mm: float = 0.15,
        min_hole_to_hole_mm: float = 0.2) -> Tuple[bool, str]:
    """Is a via candidate manufacturable at this point? Returns ``(ok, why)``;
    ``why`` is populated only when ``ok`` is False.

    ``neighbour_holes`` is every OTHER hole/via centre on the board as
    ``(x_mm, y_mm, hole_diameter_mm)`` - pad holes MUST be included. This is
    exactly the check that was missing in the incident this function
    documents: the via's distance to neighbouring PAD holes was never
    measured at all.

    PARAMETER NOTE: ``via_diameter_mm``/``hole_diameter_mm`` and
    ``min_annular_ring_mm`` are three independent numbers with no built-in
    consistency guarantee - e.g. the archive's own 0.5 mm/0.3 mm via/hole
    default pairing yields exactly a 0.1 mm ring, which is BELOW its own
    0.15 mm default minimum. There is deliberately no invented "safe" default
    triple here; a caller must pass geometry that is actually consistent with
    its fab's verified minimums (``common.stackup.fab_profiles()``), not rely
    on unexamined defaults.
    """
    check = ViaPlacementCheck(via_diameter_mm, hole_diameter_mm,
                              min_annular_ring_mm, min_hole_to_hole_mm)
    if not check.annular_ring_sufficient:
        return False, (
            f"insufficient annular ring: {check.annular_ring_mm:.4f}mm < "
            f"{min_annular_ring_mm}mm (via={via_diameter_mm}mm, "
            f"hole={hole_diameter_mm}mm)")
    for hx, hy, neighbour_diameter in neighbour_holes:
        distance = math.hypot(candidate[0] - hx, candidate[1] - hy)
        required = (via_diameter_mm / 2.0 + neighbour_diameter / 2.0
                   + min_hole_to_hole_mm)
        if distance < required:
            return False, (
                f"hole-to-hole clearance violation: {distance:.4f}mm from the "
                f"neighbour at ({hx:.3f},{hy:.3f}), required >= "
                f"{required:.4f}mm (via_radius={via_diameter_mm / 2.0}mm + "
                f"neighbour_radius={neighbour_diameter / 2.0}mm + "
                f"min_hole_to_hole={min_hole_to_hole_mm}mm)")
    return True, ""


# --------------------------------------------------------------------------- #
# 4. SI-aware step costs (fed INTO the search as extra_cost_fn, never checked
#    only after routing)
# --------------------------------------------------------------------------- #
def si_cost_function(
        other_traces: Sequence[BoxLike], net_width_mm: float,
        rule_multiplier: float | None = None,
        violation_weight: float | None = None) -> Callable[[Point, Point], float]:
    """Build an ``extra_cost_fn`` for ``grid_astar_search``/
    ``layered_astar_search`` enforcing the 3W rule: parallel high-speed
    traces should stay >= ``rule_multiplier`` * ``net_width_mm`` apart
    (default 3x - the standard crosstalk/EMI figure).

    The returned closure measures each candidate step's MIDPOINT distance to
    the nearest of ``other_traces`` (anything ``SegmentObstacle``-shaped) and
    returns a penalty proportional to the shortfall below the threshold, or
    exactly 0.0 once the threshold is met - it never subtracts from the base
    A* cost.

    ``net_width_mm`` must be the REAL width that meets the target impedance
    (i.e. ``common.stackup.solve_impedance_geometry``'s output) - this
    function does not compute or re-derive that geometry, only consumes it.

    ``other_traces`` empty always returns 0.0 (a fast path; a minimum over an
    empty sequence would otherwise raise).
    """
    # READ, not written here: these were literal defaults in this
    # signature. See common/tuning.py for why they moved into
    # fixtures/references/tuning_defaults.json.
    costs = routing_costs()
    if rule_multiplier is None:
        rule_multiplier = costs["rule_multiplier"]
    if violation_weight is None:
        violation_weight = costs["violation_weight"]

    threshold_mm = rule_multiplier * net_width_mm

    def _cost(a: Point, b: Point) -> float:
        if not other_traces:
            return 0.0
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        nearest = min(
            seg_point_dist(mid, (trace.x1, trace.y1), (trace.x2, trace.y2))
            for trace in other_traces)
        if nearest >= threshold_mm:
            return 0.0
        shortfall = threshold_mm - nearest
        return violation_weight * shortfall

    return _cost


def via_impedance_discontinuity_cost(base_via_cost: float,
                                     impedance_deviation_pct: float = 0.0) -> float:
    """Scale a via's search cost by how far the stackup GEOMETRY already
    deviates from its target impedance (``common.stackup.
    solve_impedance_geometry``'s error percentage).

    Rationale: every via is itself a small impedance discontinuity (a
    reference-plane transition, extra capacitance). If the geometry has
    already missed its target, adding another via is riskier - this scales
    ``base_via_cost`` linearly with that risk. ``impedance_deviation_pct=0.0``
    (the default) returns ``base_via_cost`` unchanged.
    """
    return base_via_cost * (1.0 + impedance_deviation_pct / 100.0)


# --------------------------------------------------------------------------- #
# 5. Layer/via-aware A* - search state is (cell, layer)
# --------------------------------------------------------------------------- #
GridNode3D = Tuple[int, int, int]


def _heuristic_3d(a: GridNode3D, b: GridNode3D) -> float:
    """The 2D octile heuristic, with ZERO contribution from the layer
    difference. Admissibility (a heuristic must never OVER-estimate the true
    cost) requires this: adding a layer-change estimate would mean guessing
    ``via_cost``, and guessing too high could make A* return a non-optimal
    path. This makes the search behave a little more like Dijkstra
    (potentially more nodes visited) but never returns a WRONG path."""
    return _heuristic((a[0], a[1]), (b[0], b[1]))


@dataclass
class LayeredSearchResult:
    path: List[Tuple[float, float, int]]     # (x_mm, y_mm, layer_index)
    via_positions: List[Point]
    nodes_visited: int
    found: bool
    reason: str = ""


def layered_astar_search(
        start: Point, end: Point, layer_count: int = 2,
        start_layer: int = 0, end_layer: int = 0,
        layer_obstacles: Optional[Dict[int, Sequence[BoxLike]]] = None,
        cell_mm: float = DEFAULT_CELL_MM, clearance_mm: float = DEFAULT_CLEARANCE_MM,
        max_nodes: int = DEFAULT_MAX_NODES,
        via_cost: float | None = None, via_diameter_mm: float = 0.5,
        via_hole_diameter_mm: float = 0.3,
        neighbour_holes: Sequence[Tuple[float, float, float]] = (),
        min_annular_ring_mm: float = 0.15, min_hole_to_hole_mm: float = 0.2,
        extra_cost_fn: Optional[Callable[[Point, Point], float]] = None,
        impedance_deviation_pct: float = 0.0) -> LayeredSearchResult:
    """The layer-aware extension of ``grid_astar_search``.

    Search state is the triple ``(cell_x, cell_y, layer)``. Moving within a
    layer costs the normal 8-direction cost; CHANGING layer (opening a via)
    costs ``via_cost`` extra AND that (x, y) point is never even OFFERED as a
    neighbour unless it passes ``via_placement_valid`` - a via that would
    violate annular-ring/hole-to-hole clearance is never searched through, not
    filtered out afterwards (see the via-placement-validity section above:
    this check did not exist in the project before a real 221-violation
    incident).

    ``layer_obstacles`` is ``{layer_index: [obstacle, ...]}``; a layer absent
    from the mapping is treated as obstacle-free.

    ``extra_cost_fn`` uses the SAME contract as ``grid_astar_search`` and
    applies ONLY to same-layer moves (a via transition is priced separately,
    via ``via_cost``).

    ``impedance_deviation_pct`` feeds ``via_impedance_discontinuity_cost`` to
    scale ``via_cost`` (0.0, the default, leaves it unchanged - see that
    function for where this number should come from).
    """
    if layer_count < 1:
        raise ValueError(f"layer_count must be >= 1, got {layer_count!r}")
    if not (0 <= start_layer < layer_count and 0 <= end_layer < layer_count):
        raise ValueError("start_layer/end_layer must be in [0, layer_count)")

    layer_obstacles = layer_obstacles or {}

    start_cell = _to_cell(start, cell_mm)
    end_cell = _to_cell(end, cell_mm)
    start_node: GridNode3D = (start_cell[0], start_cell[1], start_layer)
    end_node: GridNode3D = (end_cell[0], end_cell[1], end_layer)

    def _layer_blocked(cell: GridCell, layer: int) -> bool:
        return cell_blocked(cell, layer_obstacles.get(layer, ()), cell_mm, clearance_mm)

    if _layer_blocked(start_cell, start_layer):
        return LayeredSearchResult([], [], 0, False, "start point lies in a blocked region")
    if _layer_blocked(end_cell, end_layer):
        return LayeredSearchResult([], [], 0, False, "end point lies in a blocked region")
    if start_node == end_node:
        return LayeredSearchResult(
            [(start[0], start[1], start_layer), (end[0], end[1], end_layer)],
            [], 0, True)

    via_validity_cache: Dict[GridCell, Tuple[bool, str]] = {}

    def _via_valid(cell: GridCell) -> Tuple[bool, str]:
        if cell not in via_validity_cache:
            point = _to_point(cell, cell_mm)
            via_validity_cache[cell] = via_placement_valid(
                point, via_diameter_mm, via_hole_diameter_mm, neighbour_holes,
                min_annular_ring_mm, min_hole_to_hole_mm)
        return via_validity_cache[cell]

    # Resolved ONCE, before the loop: a per-step table read would turn a
    # configuration lookup into a hot-path cost.
    resolved_via_cost = routing_costs()["via_cost"] if via_cost is None else via_cost

    open_heap: List[Tuple[float, float, GridNode3D]] = [
        (_heuristic_3d(start_node, end_node), 0.0, start_node)]
    came_from: Dict[GridNode3D, Optional[GridNode3D]] = {start_node: None}
    g_score: Dict[GridNode3D, float] = {start_node: 0.0}
    visited: set = set()

    while open_heap:
        if len(visited) > max_nodes:
            return LayeredSearchResult([], [], len(visited), False,
                                       f"node budget ({max_nodes}) exceeded")

        _, current_g, current = heapq.heappop(open_heap)
        if current in visited:
            continue
        visited.add(current)

        if current == end_node:
            node_path: List[GridNode3D] = []
            n: Optional[GridNode3D] = current
            while n is not None:
                node_path.append(n)
                n = came_from[n]
            node_path.reverse()

            path_mm: List[Tuple[float, float, int]] = [
                (*_to_point((n[0], n[1]), cell_mm), n[2]) for n in node_path]
            path_mm[0] = (start[0], start[1], start_layer)
            path_mm[-1] = (end[0], end[1], end_layer)
            via_positions = [
                _to_point((node_path[i][0], node_path[i][1]), cell_mm)
                for i in range(1, len(node_path))
                if node_path[i][2] != node_path[i - 1][2]]
            return LayeredSearchResult(path_mm, via_positions, len(visited), True)

        cx, cy, clayer = current

        for dx, dy, cost in _DIRECTIONS:
            neighbour_cell = (cx + dx, cy + dy)
            neighbour: GridNode3D = (neighbour_cell[0], neighbour_cell[1], clayer)
            if neighbour in visited:
                continue
            if _layer_blocked(neighbour_cell, clayer):
                continue
            step_cost = cost
            if extra_cost_fn is not None:
                step_cost += extra_cost_fn(_to_point((cx, cy), cell_mm),
                                           _to_point(neighbour_cell, cell_mm))
            candidate_g = current_g + step_cost
            if candidate_g < g_score.get(neighbour, float("inf")):
                g_score[neighbour] = candidate_g
                came_from[neighbour] = current
                f = candidate_g + _heuristic_3d(neighbour, end_node)
                heapq.heappush(open_heap, (f, candidate_g, neighbour))

        valid, _why = _via_valid((cx, cy))
        if valid:
            effective_via_cost = via_impedance_discontinuity_cost(
                resolved_via_cost, impedance_deviation_pct)
            for target_layer in range(layer_count):
                if target_layer == clayer:
                    continue
                neighbour = (cx, cy, target_layer)
                if neighbour in visited:
                    continue
                if _layer_blocked((cx, cy), target_layer):
                    continue
                candidate_g = current_g + effective_via_cost
                if candidate_g < g_score.get(neighbour, float("inf")):
                    g_score[neighbour] = candidate_g
                    came_from[neighbour] = current
                    f = candidate_g + _heuristic_3d(neighbour, end_node)
                    heapq.heappush(open_heap, (f, candidate_g, neighbour))

    return LayeredSearchResult([], [], len(visited), False,
                               "no obstacle-free path found (search exhausted)")


# --------------------------------------------------------------------------- #
# 6. Coupled (differential-pair) A* - one centreline search, offset into P/N
# --------------------------------------------------------------------------- #
def _perpendicular_unit_vector(a: Point, b: Point) -> Point:
    """Unit vector perpendicular to segment a->b (90 degrees counterclockwise)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return (0.0, 0.0)
    return (-dy / length, dx / length)


def _pair_from_centerline(centerline: List[Point],
                          radius_mm: float) -> Tuple[List[Point], List[Point]]:
    """Offset every point of a centreline polyline perpendicular to its LOCAL
    direction, producing two parallel paths (P: +radius, N: -radius).

    LIMIT (an accepted approximation, not a bug): at a corner, the two
    adjacent segments' offset directions are AVERAGED - a simple miter/bevel
    blend, not a true miter-join computation. Sharp corners can therefore
    introduce a geometric error on the order of ``radius_mm`` between the P
    and N paths. Acceptable for screening; review by hand for
    tight-tolerance sharp-corner scenarios.
    """
    if len(centerline) < 2:
        return list(centerline), list(centerline)

    p_path: List[Point] = []
    n_path: List[Point] = []
    for i, point in enumerate(centerline):
        if i == 0:
            nx, ny = _perpendicular_unit_vector(centerline[0], centerline[1])
        elif i == len(centerline) - 1:
            nx, ny = _perpendicular_unit_vector(centerline[-2], centerline[-1])
        else:
            n1x, n1y = _perpendicular_unit_vector(centerline[i - 1], point)
            n2x, n2y = _perpendicular_unit_vector(point, centerline[i + 1])
            nx, ny = (n1x + n2x) / 2.0, (n1y + n2y) / 2.0
            norm = math.hypot(nx, ny)
            if norm > 1e-9:
                nx, ny = nx / norm, ny / norm
        p_path.append((point[0] + nx * radius_mm, point[1] + ny * radius_mm))
        n_path.append((point[0] - nx * radius_mm, point[1] - ny * radius_mm))
    return p_path, n_path


@dataclass
class CoupledSearchResult:
    p_path: List[Point]
    n_path: List[Point]
    centerline_result: SearchResult
    found: bool
    reason: str = ""


def coupled_astar_search(
        p_start: Point, n_start: Point, p_end: Point, n_end: Point,
        width_mm: float, gap_mm: float, obstacles: Sequence[BoxLike] = (),
        cell_mm: float = DEFAULT_CELL_MM, clearance_mm: float = DEFAULT_CLEARANCE_MM,
        max_nodes: int = DEFAULT_MAX_NODES, stub_length_mm: float = 0.5,
        extra_cost_fn: Optional[Callable[[Point, Point], float]] = None) -> CoupledSearchResult:
    """Route a differential pair as ONE search with a fixed ``gap_mm``, rather
    than two independent nets that might drift apart or clear obstacles
    differently.

    1. Each net opens with a straight stub of ``stub_length_mm``, AWAY from
       the other net (spreads a tight pad pitch out into the coupled
       corridor) - ``stub_length_mm=0`` skips this when the caller has
       already computed a merge point.
    2. A single ``grid_astar_search`` runs between the stub midpoints. The
       obstacle clearance used for THAT search is widened to
       ``clearance_mm + gap_mm / 2 + width_mm`` so it accounts for the full
       coupled corridor (P + gap + N) - a conservative approximation; at
       sharp corners the corridor width is not exactly preserved (see
       ``_pair_from_centerline``).
    3. The found centreline is offset into P/N paths via
       ``_pair_from_centerline``, stitched back onto the original pad points
       (through the stubs) and simplified.
    """
    if stub_length_mm < 0:
        raise ValueError("stub_length_mm must not be negative")
    if width_mm <= 0 or gap_mm <= 0:
        raise ValueError("width_mm and gap_mm must be positive")

    def _apply_stub(start: Point, other_start: Point, length: float) -> Point:
        if length <= 1e-9:
            return start
        dx = start[0] - other_start[0]
        dy = start[1] - other_start[1]
        d = math.hypot(dx, dy)
        if d < 1e-9:
            return start
        return (start[0] + dx / d * length, start[1] + dy / d * length)

    p_merge_start = _apply_stub(p_start, n_start, stub_length_mm)
    n_merge_start = _apply_stub(n_start, p_start, stub_length_mm)
    p_merge_end = _apply_stub(p_end, n_end, stub_length_mm)
    n_merge_end = _apply_stub(n_end, p_end, stub_length_mm)

    center_start = ((p_merge_start[0] + n_merge_start[0]) / 2.0,
                    (p_merge_start[1] + n_merge_start[1]) / 2.0)
    center_end = ((p_merge_end[0] + n_merge_end[0]) / 2.0,
                  (p_merge_end[1] + n_merge_end[1]) / 2.0)

    radius_mm = gap_mm / 2.0 + width_mm / 2.0
    corridor_clearance_mm = clearance_mm + radius_mm

    center_result = grid_astar_search(
        center_start, center_end, obstacles, cell_mm=cell_mm,
        clearance_mm=corridor_clearance_mm, max_nodes=max_nodes,
        extra_cost_fn=extra_cost_fn)
    if not center_result.found:
        return CoupledSearchResult([], [], center_result, False, center_result.reason)

    p_inner, n_inner = _pair_from_centerline(list(center_result.path), radius_mm)
    p_path = simplify_path([p_start] + p_inner + [p_end])
    n_path = simplify_path([n_start] + n_inner + [n_end])

    return CoupledSearchResult(p_path, n_path, center_result, True)


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def _number(container: dict, key: str, where: str) -> float:
    if key not in container:
        raise ValueError(f"{where}: missing required field '{key}'")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: '{key}' must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{where}: '{key}' must be finite, got {value!r}")
    return float(value)


def _point(container: dict, key: str, where: str) -> Point:
    value = container.get(key)
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   for v in value)):
        raise ValueError(f"{where}: '{key}' must be a [x, y] pair of numbers")
    if not all(math.isfinite(float(v)) for v in value):
        raise ValueError(f"{where}: '{key}' coordinates must be finite")
    return (float(value[0]), float(value[1]))


def load_problem(path: Path) -> dict:
    """Read the positional JSON routing problem. UTF-8, object at the root."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        problem = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(problem, dict):
        raise ValueError(f"{path}: the root of the problem file must be an object")
    return problem


def parse_obstacles(problem: dict) -> List[BoxLike]:
    """Build the obstacle list. ``type`` is ``box`` (default) or ``segment``."""
    obstacles: List[BoxLike] = []
    for index, item in enumerate(problem.get("obstacles", []) or []):
        where = f"obstacles[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        kind = item.get("type", "box")
        ref = str(item.get("ref", ""))
        if kind == "box":
            obstacles.append(BoxObstacle(
                x_min=_number(item, "x_min", where),
                y_min=_number(item, "y_min", where),
                x_max=_number(item, "x_max", where),
                y_max=_number(item, "y_max", where),
                ref=ref,
            ))
        elif kind == "segment":
            obstacles.append(SegmentObstacle(
                x1=_number(item, "x1", where),
                y1=_number(item, "y1", where),
                x2=_number(item, "x2", where),
                y2=_number(item, "y2", where),
                width_mm=_number(item, "width_mm", where),
                ref=ref,
            ))
        else:
            raise ValueError(f"{where}: unknown obstacle type {kind!r} "
                             "(expected 'box' or 'segment')")
    return obstacles


def parse_nets(problem: dict) -> List[dict]:
    nets = []
    for index, item in enumerate(problem.get("nets", []) or []):
        where = f"nets[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        name = item.get("net", item.get("name"))
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: 'net' must be a non-empty string")
        nets.append({
            "net": name,
            "start": _point(item, "start", where),
            "end": _point(item, "end", where),
        })
    return nets


def resolve_params(problem: dict, args: argparse.Namespace) -> dict:
    def pick(flag_value, key: str, fallback):
        if flag_value is not None:
            return flag_value
        if key in problem:
            return problem[key]
        return fallback

    return {
        "cell_mm": float(pick(args.cell_mm, "cell_mm", DEFAULT_CELL_MM)),
        "clearance_mm": float(
            pick(args.clearance_mm, "clearance_mm", DEFAULT_CLEARANCE_MM)),
        "max_nodes": int(pick(args.max_nodes, "max_nodes", DEFAULT_MAX_NODES)),
    }


def route_problem(problem: dict, params: dict, source: str) -> dict:
    """Route every net in the problem and build the report."""
    obstacles = parse_obstacles(problem)
    nets = parse_nets(problem)

    routes = []
    violations = []
    for net in nets:
        outcome = grid_astar_search(
            net["start"], net["end"], obstacles,
            cell_mm=params["cell_mm"],
            clearance_mm=params["clearance_mm"],
            max_nodes=params["max_nodes"],
        )
        routes.append({
            "net": net["net"],
            "found": outcome.found,
            "reason": outcome.reason,
            "nodes_visited": outcome.nodes_visited,
            "path_mm": [[round(x, 4), round(y, 4)] for x, y in outcome.path],
            "length_mm": round(path_length_mm(outcome.path), 4),
        })
        if not outcome.found:
            violations.append({"net": net["net"], "reason": outcome.reason,
                               "nodes_visited": outcome.nodes_visited})

    finding = result(
        ROUTING_CHECK,
        len(nets),
        violations,
        f"{len(nets)} net(s) searched against {len(obstacles)} obstacle(s) "
        f"on a {params['cell_mm']}mm grid with {params['clearance_mm']}mm "
        f"clearance; node budget {params['max_nodes']}.",
    )
    findings = [finding]
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "input": source,
        "params": params,
        "obstacle_count": len(obstacles),
        "routes": routes,
        "checks": findings,
        "summary": summarise(findings),
    }


def exit_code_for(report: dict) -> int:
    """0 only when the routing check PASSed.

    NO_COVERAGE (a problem file with no nets) exits 1: "nothing was routed"
    must not read as "everything routed".
    """
    summary = report["summary"]
    return EXIT_OK if summary["PASS"] and not summary["FAIL"] and not summary[
        NO_COVERAGE] else EXIT_FAIL


# --------------------------------------------------------------------------- #
# Self-test + fault injection (OPT-IN, never on a normal run)
# --------------------------------------------------------------------------- #
def prove_tests_not_empty() -> bool:
    """FAULT INJECTION: put an obstacle right ON TOP of the start point and
    the search MUST fail (found=False). Without this the "a path was found"
    tests could be vacuous."""
    obstacle = BoxObstacle(-0.5, -0.5, 0.5, 0.5)
    outcome = grid_astar_search((0.0, 0.0), (5.0, 0.0), [obstacle],
                                cell_mm=0.5, clearance_mm=0.1)
    return outcome.found is False


def run_self_tests() -> List[str]:
    """Return a list of failure messages; an empty list means PASS. Never
    prints — the CLI routes the messages to stderr via ``diag``."""
    errors: List[str] = []

    # 1. Obstacle-free straight path -> found, length close to the distance.
    s1 = grid_astar_search((0.0, 0.0), (5.0, 0.0), cell_mm=0.5)
    if not s1.found:
        errors.append("obstacle-free straight path not found")
    elif abs(path_length_mm(s1.path) - 5.0) > 0.6:
        errors.append(f"obstacle-free path length far off: {s1.path}")

    # 2. A wall in the middle -> A* must go around it (the scenario a simple
    #    L/U-turn ladder cannot solve).
    wall = BoxObstacle(2.0, -0.3, 3.0, 5.0)
    s2 = grid_astar_search((0.0, 0.0), (5.0, 0.0), [wall],
                           cell_mm=0.25, clearance_mm=0.2)
    if not s2.found:
        errors.append("no path found around the wall")

    # 3. Fully enclosed start point -> a clean FAIL.
    closed = BoxObstacle(-1.0, -1.0, 1.0, 1.0)
    s3 = grid_astar_search((0.0, 0.0), (10.0, 0.0), [closed],
                           cell_mm=0.5, clearance_mm=0.1)
    if s3.found or "blocked region" not in s3.reason:
        errors.append(f"enclosed-start scenario reported wrongly: {s3}")

    # 4. Node budget exceeded -> a clean FAIL (no unbounded search).
    s4 = grid_astar_search((0.0, 0.0), (5.0, 0.0), cell_mm=0.01, max_nodes=50)
    if s4.found or "budget" not in s4.reason:
        errors.append(f"node budget overrun not reported correctly: {s4}")

    # 5. Start and end in the same cell -> single segment, no search needed.
    s5 = grid_astar_search((0.0, 0.0), (0.02, 0.02), cell_mm=0.5)
    if not s5.found or s5.nodes_visited != 0:
        errors.append("same-cell shortcut did not work")

    # 6. Two-phase collision test: a point near the AABB corner of a diagonal
    #    segment but far from the real line must NOT be blocked.
    diagonal = SegmentObstacle(0.0, 10.0, 20.0, 0.0, width_mm=0.29, ref="trk_TEST")
    if cell_blocked((2, 2), [diagonal], cell_mm=0.25, clearance_mm=0.15):
        errors.append("narrow phase missing: the empty corner of a diagonal "
                      "segment AABB is still reported as blocked")
    on_line = SegmentObstacle(0.0, 0.0, 20.0, 20.0, width_mm=0.29, ref="trk_TEST")
    if not cell_blocked((40, 40), [on_line], cell_mm=0.25, clearance_mm=0.15):
        errors.append("narrow phase too permissive: a cell ON the segment is "
                      "no longer blocked")

    # 7. Fault injection.
    if not prove_tests_not_empty():
        errors.append("fault injection did not break: A* may be finding a path "
                      "under every condition")

    return errors


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("problem", type=Path, nargs="?",
                        help="JSON routing problem (grid, obstacles, net "
                             "endpoints); required unless --self-test")
    parser.add_argument("--cell-mm", type=float, default=None,
                        help="override the grid resolution, mm")
    parser.add_argument("--clearance-mm", type=float, default=None,
                        help="override the obstacle clearance, mm")
    parser.add_argument("--max-nodes", type=int, default=None,
                        help="override the node budget")
    parser.add_argument("--json", type=Path,
                        help="also WRITE the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the built-in self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --self-test is OPT-IN. The source ran its self-tests as the ONLY thing
    # __main__ did and printed the outcome to stdout.
    if args.self_test:
        errors = run_self_tests()
        for message in errors:
            diag(f"SELF-TEST FAIL: {message}")
        return EXIT_FAIL if errors else EXIT_OK

    if args.problem is None:
        parser.error("a JSON routing problem is required; pass it positionally "
                     "(e.g. `grid_astar.py problem.json`) or use --self-test")

    problem = load_problem(args.problem)
    params = resolve_params(problem, args)
    report = route_problem(problem, params, str(args.problem))

    diag(f"routes={len(report['routes'])} summary={report['summary']}")
    write_report(report, args.json)
    emit(report)
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
