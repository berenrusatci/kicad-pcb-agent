#!/usr/bin/env python3
"""Force-directed component placement driven by the netlist, not by taste.

Port of ``kuvvet_yonelimli_yerlesim.py``. Components are placed by a physics
graph that pulls electrically connected parts together and pushes
overlapping courtyards apart, instead of by coordinates a language model
invented. Language models have weak spatial reasoning; coordinates are never
assigned freely here, they fall out of the electrical connectivity.

Section 3.5 (``try_multiple_placements``/``placement_score``/
``calibrate_score_weights``) was added during the "Tur 3" drift re-sync: the
source solves the same netlist with several parameter bundles, scores each
result (ratsnest + keepout violations + thermal spread + HS compactness,
weighted), and recommends the lowest-scoring one while returning every
attempt. ``calibrate_score_weights`` needs the optional numpy/scipy tier
(lazy-imported only there); every other function in this module stays
stdlib-only.

WHAT THIS IS NOT (deliberate limits — this engine does NOT finish a layout)
--------------------------------------------------------------------------
The output is a **SEED placement**, not a production-ready one. The engine
knows nothing about:
  - 3-D / Z-axis and enclosure keepouts (a mechanical check must follow),
  - fixed millimetre rules (decoupling <= 1.5 mm, oscillator <= 5 mm,
    LDO-to-I2C >= 10 mm). Those CAN be fed in as ``DistanceConstraint``
    inputs, but convergence does NOT guarantee them; they must afterwards be
    MEASURED with ``check_distance_constraints``,
  - thermal separation beyond the anchors built by ``thermal_constraints``,
    EMI zones, or high-speed region clearance beyond the keepouts in the
    HighSpeedRuleManager section.

DETERMINISM (a deliberate design decision)
------------------------------------------
Classic force-directed algorithms start from a random layout. Randomness is
banned here, so the initial placement is generated DETERMINISTICALLY with a
golden-angle (Vogel) spiral. The same netlist always yields the same
coordinates; otherwise two runs produce two different boards and no report
is reproducible.

Usage:
    python force_directed.py <problem.json> [--json out.json]
                             [--max-iterations 400] [--hierarchical]
                             [--trace-width-mm 0.2] [--self-test]

Exit codes: 0 = every gate PASSed, 1 = a gate FAILed or had NO_COVERAGE,
2 = the tool could not produce an answer.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, result, summarise  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_FAIL,
    EXIT_OK,
    diag,
    emit,
    run_cli,
    write_report,
)

SCHEMA_VERSION = "placement-result/v1"
TOOL_NAME = "force_directed"

COURTYARD_CHECK = "courtyard_overlap"
CONSTRAINT_CHECK = "distance_constraints"
KEEPOUT_CHECK = "high_speed_keepout_violation"

# Power/ground nets are EXCLUDED from the force-directed graph (weight 0):
# a ground net is poured as a PLANE, it is not a point-to-point trace. Left
# in the graph, a GND net with hundreds of pins collapses every component
# onto a single point (physically meaningless) and the real signal clusters
# disappear.
PLANE_NET_NAMES: Tuple[str, ...] = (
    "GND", "GNDA", "AGND", "DGND", "VSS",
    "+3V3", "+3.3V", "+5V", "+1V8", "VCC", "VDD", "VBAT", "VBUS",
)


def is_plane_net(net_name: str,
                 names: Sequence[str] = PLANE_NET_NAMES) -> bool:
    """Is this net a power/ground PLANE (and therefore out of the graph)?

    A plain exact-name match, deliberately NOT a regex: a regex would risk a
    surprise match such as ``GND_SENSE`` — a sense line that really is routed
    as a trace — being written off as a plane.
    """
    name = net_name.strip().upper()
    return any(name == candidate.upper() for candidate in names)


@dataclass
class Component:
    """A component to be placed.

    ``fixed=True`` parts (connectors, mounting holes, antennas) NEVER move —
    the engine's counterpart of "coordinates are locked" for mechanically
    constrained parts.
    """

    ref: str
    width_mm: float = 2.0    # courtyard width (radius of the repulsion force)
    height_mm: float = 2.0
    x: float = 0.0
    y: float = 0.0
    fixed: bool = False

    @property
    def radius_mm(self) -> float:
        """Radius of the circle enclosing the courtyard — the reach of the
        repulsion force. Approximating a rectangular courtyard with a circle
        is SEPARATE from, and more conservative than, the exact overlap test
        in ``check_courtyard_overlap``."""
        return math.hypot(self.width_mm, self.height_mm) / 2.0


@dataclass
class Net:
    """A net and the component references attached to it."""

    name: str
    connections: List[str]
    weight: float = 1.0  # > 1 for critical nets (differential pairs, clocks)


@dataclass
class DistanceConstraint:
    """A HARD millimetre constraint coming from the layout rules.

    ``max_mm`` caps how far apart two components may sit (decoupling
    <= 1.5 mm, oscillator <= 5 mm); ``min_mm`` sets how close they may get
    (LDO <-> I2C >= 10 mm). The engine uses them as SOFT forces;
    ``check_distance_constraints`` measures them as a HARD acceptance gate.
    """

    ref_a: str
    ref_b: str
    max_mm: Optional[float] = None
    min_mm: Optional[float] = None
    note: str = ""


class PlacementCategory(str, Enum):
    """The mandatory 3-stage hierarchy: power/decoupling first, then the
    critical high-speed/differential group, and low-speed I/O last. Blind,
    unordered placement and routing produce density walls, deadlock and
    short-circuit pathologies."""

    POWER_DECOUPLING = "power_decoupling"
    CRITICAL_HS = "critical_hs"
    LOW_SPEED_IO = "low_speed_io"


_HIERARCHY_ORDER: Tuple[PlacementCategory, ...] = (
    PlacementCategory.POWER_DECOUPLING,
    PlacementCategory.CRITICAL_HS,
    PlacementCategory.LOW_SPEED_IO,
)


@dataclass
class PlacementResult:
    coordinates: Dict[str, Tuple[float, float]]
    iterations: int
    converged: bool
    last_move_mm: float
    initial_ratsnest_mm: float
    final_ratsnest_mm: float

    @property
    def improvement_ratio(self) -> float:
        """Fraction of the total ratsnest length removed (0-1).

        This is the ONLY measurable proof that the engine actually did
        something; the tests assert on it.
        """
        if self.initial_ratsnest_mm <= 0:
            return 0.0
        return max(
            0.0,
            (self.initial_ratsnest_mm - self.final_ratsnest_mm)
            / self.initial_ratsnest_mm,
        )


# --------------------------------------------------------------------------- #
# 1. Graph construction (netlist -> weighted edge list)
# --------------------------------------------------------------------------- #
def build_graph(netlist: Sequence[Net],
                skip_plane_nets: bool = True) -> Dict[Tuple[str, str], float]:
    """Turn the netlist into a weighted, undirected adjacency graph.

    STAR MODEL: an N-pin net contributes edges of weight ``weight / (N - 1)``
    rather than a full clique of N*(N-1)/2 unit edges. Rationale: the clique
    of an 8-pin bus is 28 edges and would steamroll a 2-pin critical
    differential pair (1 edge) — a physically wrong priority order. Dividing
    keeps the total attraction budget of a many-pin net comparable to that of
    a 2-pin net.

    Power/ground nets (``is_plane_net``) are SKIPPED by default; see
    ``PLANE_NET_NAMES``.
    """
    edges: Dict[Tuple[str, str], float] = {}
    for net in netlist:
        if skip_plane_nets and is_plane_net(net.name):
            continue
        unique = sorted(set(net.connections))
        if len(unique) < 2:
            continue  # a single-pin/unconnected net produces no attraction
        # HighSpeedRuleManager (see section 6): if the weight was NOT set by
        # hand (still the 1.0 default) AND the net is detected as high-speed
        # or differential, the weight is raised automatically - the caller
        # does not have to write `Net(weight=...)` as well. A deliberately
        # chosen non-default value (say 0.5) is left untouched.
        effective_weight = net.weight
        if effective_weight == 1.0 and is_high_speed_net(net.name):
            effective_weight = HIGH_SPEED_DEFAULT_WEIGHT
        share = effective_weight / (len(unique) - 1)
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                key = (unique[i], unique[j])
                edges[key] = edges.get(key, 0.0) + share
    return edges


def find_clusters(edges: Dict[Tuple[str, str], float],
                  all_refs: Sequence[str],
                  weight_threshold: float = 0.5) -> List[List[str]]:
    """Connected components over the edges ABOVE ``weight_threshold``, i.e.
    "everything sharing a net is one logical cluster".

    Weak edges below the threshold (for example the 0.11-weight links coming
    out of a 10-pin bus) do not draw a cluster boundary; otherwise everything
    lands in one giant cluster and the clustering carries no information.
    Components with no links at all are RETURNED as single-member clusters —
    they are never silently dropped, because losing coverage is exactly what
    the finding contract forbids.
    """
    parent: Dict[str, str] = {ref: ref for ref in all_refs}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (a, b), weight in edges.items():
        if weight >= weight_threshold and a in parent and b in parent:
            union(a, b)

    groups: Dict[str, List[str]] = {}
    for ref in all_refs:
        groups.setdefault(find(ref), []).append(ref)
    return [sorted(g) for g in sorted(groups.values(), key=lambda g: (-len(g), g[0]))]


def total_ratsnest_mm(coordinates: Dict[str, Tuple[float, float]],
                      edges: Dict[Tuple[str, str], float]) -> float:
    """Weighted total ratsnest (air-wire) length, mm.

    The single-number measure of placement quality. It is multiplied by the
    weight: 1 mm of a critical net is worth more than 1 mm of an ordinary
    GPIO.
    """
    total = 0.0
    for (a, b), weight in edges.items():
        if a not in coordinates or b not in coordinates:
            continue
        ax, ay = coordinates[a]
        bx, by = coordinates[b]
        total += weight * math.hypot(ax - bx, ay - by)
    return total


# --------------------------------------------------------------------------- #
# 2. Deterministic initial placement
# --------------------------------------------------------------------------- #
GOLDEN_ANGLE_RAD = math.pi * (3.0 - math.sqrt(5.0))  # ~2.39996 rad


def initial_placement(components: Sequence[Component],
                      board_width_mm: float,
                      board_height_mm: float,
                      angle_offset_rad: float = 0.0) -> Dict[str, Tuple[float, float]]:
    """DETERMINISTIC initial placement on a golden-angle (Vogel) spiral.

    ``random`` is NOT used (see the determinism note in the module
    docstring). The Vogel spiral spreads points evenly over the board area,
    so the first iterations do not suffer an artificial "everything on top of
    everything" repulsion explosion.

    The existing (x, y) of ``fixed=True`` components is PRESERVED — those
    come from a mechanical constraint and the spiral must not overwrite them.

    ``angle_offset_rad`` rotates the whole spiral (still fully deterministic
    for a given offset) - it exists so ``try_multiple_placements`` can seed
    a genuinely different starting configuration without touching
    ``random``: the same netlist and the same offset always produce the
    same seed, but a different offset can walk the solver into a different
    local minimum.
    """
    if board_width_mm <= 0 or board_height_mm <= 0:
        raise ValueError("board dimensions must be positive")

    center_x, center_y = board_width_mm / 2.0, board_height_mm / 2.0
    max_r = min(board_width_mm, board_height_mm) * 0.45
    movable = [c for c in components if not c.fixed]

    coordinates: Dict[str, Tuple[float, float]] = {
        c.ref: (c.x, c.y) for c in components if c.fixed
    }
    count = max(1, len(movable))
    for index, component in enumerate(movable):
        radius = max_r * math.sqrt((index + 0.5) / count)
        angle = index * GOLDEN_ANGLE_RAD + angle_offset_rad
        coordinates[component.ref] = (
            center_x + radius * math.cos(angle),
            center_y + radius * math.sin(angle),
        )
    return coordinates


# --------------------------------------------------------------------------- #
# 3. Physics engine
# --------------------------------------------------------------------------- #
def _separate_overlaps(coordinates: Dict[str, Tuple[float, float]],
                       by_ref: Dict[str, Component],
                       board_width_mm: float,
                       board_height_mm: float,
                       passes: int = 4) -> None:
    """Separation pass — pushes overlapping courtyards apart. Mutates the
    coordinate dict IN PLACE.

    WHY A SEPARATE PASS WAS NEEDED (a measured design fault):
    In the pure force-directed model, attraction ``k*w*d`` grows WITHOUT
    BOUND (with distance and net weight) while repulsion ``k*overlap`` is
    BOUNDED (at most ``k*(r_a+r_b)``). Give it a heavily weighted net (say a
    6-pin clock/bus with weight=10) and attraction CRUSHES repulsion, so the
    components pile up — this was actually observed in a test (all 6 pairs
    overlapped). Non-overlap is therefore no longer left to a force balance;
    it is ENFORCED geometrically at the end of every iteration.

    It still is NOT a guarantee: if the board is too small the separation
    runs into the board edge and an overlap remains — in that case
    ``check_courtyard_overlap`` FAILs and the placement is rejected. There is
    no silent "close enough, call it separated".
    """
    movable = [ref for ref in coordinates if not by_ref[ref].fixed]
    if not movable:
        return
    refs = list(coordinates.keys())
    for _ in range(passes):
        moved = False
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                ra, rb = refs[i], refs[j]
                ca, cb = by_ref[ra], by_ref[rb]
                if ca.fixed and cb.fixed:
                    continue
                ax, ay = coordinates[ra]
                bx, by = coordinates[rb]
                # AABB overlap - the SAME criterion check_courtyard_overlap
                # uses, so the engine solves exactly what the gate measures.
                overlap_x = (ca.width_mm + cb.width_mm) / 2.0 - abs(ax - bx)
                overlap_y = (ca.height_mm + cb.height_mm) / 2.0 - abs(ay - by)
                if overlap_x <= 0 or overlap_y <= 0:
                    continue
                moved = True
                # Separate along the axis needing the least push (the
                # shortest escape route).
                if overlap_x < overlap_y:
                    direction = 1.0 if bx >= ax else -1.0
                    push = (overlap_x + 1e-4) * direction
                    dx_a, dy_a, dx_b, dy_b = -push, 0.0, push, 0.0
                else:
                    direction = 1.0 if by >= ay else -1.0
                    push = (overlap_y + 1e-4) * direction
                    dx_a, dy_a, dx_b, dy_b = 0.0, -push, 0.0, push

                # A fixed part does not move -> the other one absorbs it all.
                if ca.fixed:
                    dx_b, dy_b = dx_b * 2.0, dy_b * 2.0
                    dx_a = dy_a = 0.0
                elif cb.fixed:
                    dx_a, dy_a = dx_a * 2.0, dy_a * 2.0
                    dx_b = dy_b = 0.0
                else:
                    dx_a, dy_a, dx_b, dy_b = dx_a / 2, dy_a / 2, dx_b / 2, dy_b / 2

                for ref, dx, dy in ((ra, dx_a, dy_a), (rb, dx_b, dy_b)):
                    if by_ref[ref].fixed:
                        continue
                    component = by_ref[ref]
                    x, y = coordinates[ref]
                    coordinates[ref] = (
                        min(max(x + dx, component.width_mm / 2.0),
                            board_width_mm - component.width_mm / 2.0),
                        min(max(y + dy, component.height_mm / 2.0),
                            board_height_mm - component.height_mm / 2.0),
                    )
        if not moved:
            break


def solve_placement(
    components: Sequence[Component],
    netlist: Sequence[Net],
    board_width_mm: float,
    board_height_mm: float,
    constraints: Sequence[DistanceConstraint] = (),
    max_iterations: int = 400,
    attraction: float = 0.08,
    repulsion: float = 1.0,
    convergence_mm: float = 0.01,
    initial_step_mm: float = 2.0,
    keepouts: Sequence["HighSpeedKeepout"] = (),
    angle_offset_rad: float = 0.0,
) -> PlacementResult:
    """Force-directed placement: PULL connected components together, PUSH
    overlapping ones apart.

    Force model (a Fruchterman-Reingold derivative adapted to PCBs):
      - **Attraction (Hooke):** for every weighted edge ``F = k_att * w * d``.
        Directly proportional to distance (a spring) — distant links pull
        harder, which is what minimises the ratsnest total.
      - **Repulsion (courtyard based):** engages ONLY while two courtyard
        circles overlap (``overlap > 0``), as ``F = k_rep * overlap``. The
        classic 1/d^2 Coulomb term was rejected because on a PCB the goal is
        not "push components apart" but merely "stop them overlapping";
        pushing a distant component only lengthens the ratsnest.
      - **Constraint force:** a violated ``DistanceConstraint`` adds a pull or
        push proportional to the violation (a SOFT constraint; the hard
        acceptance is measured by ``check_distance_constraints``).
      - **Keepout (HighSpeedRuleManager, section 6):** when ``keepouts`` are
        given, a force SEPARATE from courtyard repulsion applies with
        INFINITE stiffness — if a component's normal force step would move it
        inside a keepout circle, that step is REJECTED ENTIRELY for that
        component (it stays where it was); it is not a soft force that lets
        it "come a bit closer". Rationale: a soft force can be crushed by a
        strong attraction (e.g. a heavily weighted HS net) — exactly the
        problem ``_separate_overlaps`` already solves for courtyard overlap.

    Cooling: the step size decreases linearly with the iteration, which
    converges instead of oscillating. ``fixed=True`` components never move
    but do keep applying force to the others.
    """
    if not components:
        return PlacementResult({}, 0, False, 0.0, 0.0, 0.0)

    edges = build_graph(netlist)
    coordinates = initial_placement(components, board_width_mm, board_height_mm,
                                    angle_offset_rad=angle_offset_rad)
    initial_ratsnest = total_ratsnest_mm(coordinates, edges)

    by_ref = {c.ref: c for c in components}
    movable_refs = [c.ref for c in components if not c.fixed]

    last_move = float("inf")
    iteration = 0
    for iteration in range(1, max_iterations + 1):
        step = initial_step_mm * (1.0 - (iteration - 1) / max_iterations)
        forces: Dict[str, List[float]] = {ref: [0.0, 0.0] for ref in coordinates}

        # --- Attraction (ratsnest edges) ---
        for (a, b), weight in edges.items():
            if a not in coordinates or b not in coordinates:
                continue
            ax, ay = coordinates[a]
            bx, by = coordinates[b]
            dx, dy = bx - ax, by - ay
            distance = math.hypot(dx, dy)
            if distance < 1e-9:
                continue
            magnitude = attraction * weight * distance
            ux, uy = dx / distance, dy / distance
            forces[a][0] += magnitude * ux
            forces[a][1] += magnitude * uy
            forces[b][0] -= magnitude * ux
            forces[b][1] -= magnitude * uy

        # --- Repulsion (courtyard overlap) ---
        refs = list(coordinates.keys())
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                ra, rb = refs[i], refs[j]
                ca, cb = by_ref[ra], by_ref[rb]
                ax, ay = coordinates[ra]
                bx, by = coordinates[rb]
                dx, dy = bx - ax, by - ay
                distance = math.hypot(dx, dy)
                min_distance = ca.radius_mm + cb.radius_mm
                if distance >= min_distance:
                    continue
                if distance < 1e-9:
                    # Exactly coincident: separate along a deterministic
                    # direction (no `random` anywhere in this file).
                    dx, dy, distance = 1.0, 0.0, 1.0
                overlap = min_distance - distance
                magnitude = repulsion * overlap
                ux, uy = dx / distance, dy / distance
                forces[ra][0] -= magnitude * ux
                forces[ra][1] -= magnitude * uy
                forces[rb][0] += magnitude * ux
                forces[rb][1] += magnitude * uy

        # --- Soft millimetre constraints ---
        for constraint in constraints:
            if (constraint.ref_a not in coordinates
                    or constraint.ref_b not in coordinates):
                continue
            ax, ay = coordinates[constraint.ref_a]
            bx, by = coordinates[constraint.ref_b]
            dx, dy = bx - ax, by - ay
            distance = math.hypot(dx, dy)
            if distance < 1e-9:
                continue
            ux, uy = dx / distance, dy / distance
            magnitude = 0.0
            if constraint.max_mm is not None and distance > constraint.max_mm:
                magnitude = +(distance - constraint.max_mm)     # pull together
            elif constraint.min_mm is not None and distance < constraint.min_mm:
                magnitude = -(constraint.min_mm - distance)     # push apart
            if magnitude != 0.0:
                forces[constraint.ref_a][0] += magnitude * ux
                forces[constraint.ref_a][1] += magnitude * uy
                forces[constraint.ref_b][0] -= magnitude * ux
                forces[constraint.ref_b][1] -= magnitude * uy

        # --- Position update (movable only) + clamp inside the board ---
        last_move = 0.0
        for ref in movable_refs:
            fx, fy = forces[ref]
            magnitude = math.hypot(fx, fy)
            if magnitude < 1e-12:
                continue
            scale = min(step, magnitude) / magnitude
            x, y = coordinates[ref]
            new_x = x + fx * scale
            new_y = y + fy * scale
            component = by_ref[ref]
            # Keep the courtyard inside the board outline.
            new_x = min(max(new_x, component.width_mm / 2.0),
                        board_width_mm - component.width_mm / 2.0)
            new_y = min(max(new_y, component.height_mm / 2.0),
                        board_height_mm - component.height_mm / 2.0)
            if keepouts and violates_keepout(ref, new_x, new_y, component, keepouts):
                # INFINITE repulsion: instead of a soft force the WHOLE step
                # is rejected and the component stays put this iteration.
                continue
            last_move = max(last_move, math.hypot(new_x - x, new_y - y))
            coordinates[ref] = (new_x, new_y)

        # Non-overlap is not left to a force balance; it is enforced.
        _separate_overlaps(coordinates, by_ref, board_width_mm, board_height_mm)

        if last_move < convergence_mm:
            break

    return PlacementResult(
        coordinates={ref: (round(x, 4), round(y, 4))
                     for ref, (x, y) in coordinates.items()},
        iterations=iteration,
        converged=last_move < convergence_mm,
        last_move_mm=round(last_move, 6),
        initial_ratsnest_mm=round(initial_ratsnest, 4),
        final_ratsnest_mm=round(total_ratsnest_mm(coordinates, edges), 4),
    )


# --------------------------------------------------------------------------- #
# 3b. Hierarchical (staged) placement
# --------------------------------------------------------------------------- #
def solve_hierarchical_placement(
    components: Sequence[Component],
    categories: Dict[str, PlacementCategory],
    netlist: Sequence[Net],
    board_width_mm: float,
    board_height_mm: float,
    constraints: Sequence[DistanceConstraint] = (),
    **kwargs,
) -> PlacementResult:
    """Run ``solve_placement`` through the mandatory 3-stage hierarchy
    (power/decoupling -> critical HS/differential -> low-speed I/O).

    Each stage LOCKS (``fixed=True``) the components placed by the previous
    stage: they no longer move, but they keep applying force to the new
    stage's components. Placement therefore follows the flow-first ordering
    rather than an arbitrary one.

    Components with no entry in ``categories`` (e.g. fixed connectors) count
    as already-fixed in EVERY stage; components already carrying
    ``fixed=True`` never move in any stage (the engine's own rule, unbroken
    here).
    """
    locked: Dict[str, Tuple[float, float]] = {}
    last_result: Optional[PlacementResult] = None
    included: List[PlacementCategory] = []

    for category in _HIERARCHY_ORDER:
        included.append(category)
        stage_components: List[Component] = []
        for component in components:
            if component.fixed:
                stage_components.append(component)
            elif component.ref in locked:
                x, y = locked[component.ref]
                stage_components.append(Component(
                    component.ref, component.width_mm, component.height_mm,
                    x, y, fixed=True))
            elif categories.get(component.ref) in included:
                stage_components.append(component)
            # else: this component's turn has not come -> excluded from stage

        if not stage_components:
            continue
        stage_result = solve_placement(
            stage_components, netlist, board_width_mm, board_height_mm,
            constraints, **kwargs)
        for ref, position in stage_result.coordinates.items():
            if categories.get(ref) == category:
                locked[ref] = position
        last_result = stage_result

    if last_result is None:
        return PlacementResult({}, 0, False, 0.0, 0.0, 0.0)

    all_coordinates: Dict[str, Tuple[float, float]] = dict(locked)
    for component in components:
        if component.ref not in all_coordinates:
            # fixed, or never entered any stage
            all_coordinates[component.ref] = (component.x, component.y)

    edges = build_graph(netlist)
    return PlacementResult(
        coordinates={ref: (round(x, 4), round(y, 4))
                     for ref, (x, y) in all_coordinates.items()},
        iterations=last_result.iterations,
        converged=last_result.converged,
        last_move_mm=last_result.last_move_mm,
        initial_ratsnest_mm=last_result.initial_ratsnest_mm,
        final_ratsnest_mm=round(total_ratsnest_mm(all_coordinates, edges), 4),
    )


# --------------------------------------------------------------------------- #
# 3c. Thermal anchors - a thermal keepout turned into a placement INPUT
# --------------------------------------------------------------------------- #
@dataclass
class ThermalContactZone:
    """A metal contact surface (heatsink boss) derived from the enclosure.

    PORTING NOTE: the source took this type from a dedicated ECAD/MCAD
    thermal bridge module that is NOT part of this bundle. Rather than
    importing something that does not exist, the two fields this function
    actually needs are declared here. The geometry (point-in-polygon,
    polygon centroid) is a faithful port; the enclosure-model plumbing that
    produced the zones is not, and must be supplied by the caller.
    """

    name: str
    polygon: List[Tuple[float, float]]  # mm, 2-D projection of the surface
    z_clearance_mm: float = 0.0


@dataclass
class ComponentThermalState:
    """A heat-dissipating component and where it currently sits."""

    ref: str
    x: float
    y: float
    power_w: float = 0.0


def _point_in_polygon(x: float, y: float,
                      polygon: Sequence[Tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test."""
    inside = False
    count = len(polygon)
    for i in range(count):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % count]
        if ((y1 > y) != (y2 > y)) and (
                x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def find_contact_zone(x: float, y: float,
                      zones: Sequence[ThermalContactZone]
                      ) -> Optional[ThermalContactZone]:
    """The zone whose polygon contains (x, y), or None.

    An empty ``zones`` list (no enclosure model shared) returns None and does
    NOT raise — "no enclosure data, so this step is skipped" is the rule,
    and it must not be confused with "thermally fine".
    """
    if not zones:
        return None
    for zone in zones:
        if _point_in_polygon(x, y, zone.polygon):
            return zone
    return None


def thermal_constraints(
    thermal_states: Sequence[ComponentThermalState],
    zones: Sequence[ThermalContactZone],
    max_distance_mm: float = 3.0,
) -> Tuple[List[Component], List[DistanceConstraint]]:
    """Turn a thermal keepout finding into a REAL INPUT to the placement
    engine, rather than a separate later step the engine knows nothing about.

    For every component sitting INSIDE its enclosure thermal contact surface,
    a FIXED synthetic "thermal anchor" component is created at the polygon
    centroid, plus a ``DistanceConstraint(max_mm=...)`` forcing the real
    component to stay within ``max_distance_mm`` of it — the placement engine
    can no longer drag the heat source away from the enclosure contact.

    For components with no shared enclosure contact data, NO anchor and NO
    constraint are produced — they are not silently assumed "thermally safe";
    only the extra input this function would have produced disappears.
    """
    extra_components: List[Component] = []
    constraints: List[DistanceConstraint] = []
    for state in thermal_states:
        zone = find_contact_zone(state.x, state.y, zones)
        if zone is None:
            continue
        center_x = sum(p[0] for p in zone.polygon) / len(zone.polygon)
        center_y = sum(p[1] for p in zone.polygon) / len(zone.polygon)
        anchor_ref = f"_THERMAL_ANCHOR_{state.ref}"
        extra_components.append(
            Component(anchor_ref, 0.1, 0.1, center_x, center_y, fixed=True))
        constraints.append(DistanceConstraint(
            ref_a=state.ref, ref_b=anchor_ref, max_mm=max_distance_mm,
            note=f"thermal keepout: {state.ref} must stay within "
                 f"{max_distance_mm}mm of enclosure contact surface "
                 f"'{zone.name}'",
        ))
    return extra_components, constraints


# --------------------------------------------------------------------------- #
# 6. HighSpeedRuleManager - high-speed / differential-pair protection
# --------------------------------------------------------------------------- #
#
# WHY THIS SECTION EXISTS (a measured diagnostic finding): running the engine
# on an ESD cluster whose parts all hang off the same two fixed anchors
# showed that, while minimising the ratsnest, the engine pulls those parts
# together until the courtyard overlap is resolved and then does NOT open the
# corridor between them that the real routing needs. The engine knows the
# goal "no overlap"; it does NOT know the goal "leave room between these two
# for a differential pair's via-pair to pass". This section turns that
# missing goal into a concrete input: high-speed/differential nets are
# recognised automatically (A), an exclusion (keepout) zone is computed along
# their route using the 3W rule (B), and any component entering that zone is
# rejected both during soft placement (the INFINITE repulsion in
# solve_placement) and at the hard acceptance gate (C).

HIGH_SPEED_DEFAULT_WEIGHT = 3.0  # consistent with the "> 1" in Net.weight


def is_high_speed_net(net_name: str, net_class: str = "") -> bool:
    """True when the net class is a differential-impedance class such as
    ``DIFF_90OHM``, OR the net name ends in _P/_N (a differential-pair
    suffix), OR the name contains CSI/MIPI. To limit false positives the
    _P/_N test only fires when the remaining body is at least 2 characters
    long (so a stray "P"/"N" cannot match by accident)."""
    name = net_name.strip().upper()
    net_class_name = net_class.strip().upper()
    if "DIFF" in net_class_name:
        return True
    if "CSI" in name or "MIPI" in name:
        return True
    if name.endswith("_P") or name.endswith("_N"):
        if len(name[:-2]) >= 2:
            return True
    return False


@dataclass
class HighSpeedKeepout:
    """A computed exclusion zone for one high-speed net.

    ``source_ref``/``target_ref``: the pin pair that PRODUCED this keepout.
    A keepout's OWN two endpoints must NOT count as violators of it — it
    would be meaningless to reject the very component the corridor starts at.
    Without these two fields every keepout would immediately (and wrongly)
    find its own endpoints in violation.
    """

    net_name: str
    center_x_mm: float
    center_y_mm: float
    radius_mm: float   # per the 3W rule: trace width * 3 + pad/track margin
    source_ref: str = ""
    target_ref: str = ""


_KEEPOUT_PAD_TRACK_MARGIN_MM = 0.15  # fixed pad/track allowance on top of 3W


def high_speed_keepouts(net: Net,
                        coordinates: Dict[str, Tuple[float, float]],
                        trace_width_mm: float) -> List[HighSpeedKeepout]:
    """Keepouts along the pin pairs of a high-speed net, per the 3W rule.

    Because this runs during placement (before routing), the "follow the real
    routed segments" path does not exist here: with no segment data, the
    MIDPOINT of the straight line between two pins is used (a simplified
    representation of the end-to-end run). When real routed-segment data is
    available a segment-based variant can be added later WITHOUT breaking
    this signature.

    The 3W formula is NOT reinvented: the width is already an input here, so
    the radius is simply ``trace_width_mm * 3`` plus a fixed pad/track margin
    (0.15 mm) of the same order as typical escape/trim tolerances.
    """
    if not is_high_speed_net(net.name):
        return []
    radius = trace_width_mm * 3.0 + _KEEPOUT_PAD_TRACK_MARGIN_MM
    unique = sorted(set(net.connections))
    keepouts: List[HighSpeedKeepout] = []
    for i in range(len(unique)):
        for j in range(i + 1, len(unique)):
            a, b = unique[i], unique[j]
            if a not in coordinates or b not in coordinates:
                continue
            ax, ay = coordinates[a]
            bx, by = coordinates[b]
            keepouts.append(HighSpeedKeepout(
                net_name=net.name,
                center_x_mm=(ax + bx) / 2.0,
                center_y_mm=(ay + by) / 2.0,
                radius_mm=radius,
                source_ref=a,
                target_ref=b,
            ))
    return keepouts


def violates_keepout(ref: str, x: float, y: float, component: Component,
                     keepouts: Sequence[HighSpeedKeepout]) -> bool:
    """Does one (ref, x, y) position violate ANY keepout?

    Extracted as a shared helper so that the hard gate
    (``keepout_violations``) and the INFINITE repulsion inside
    ``solve_placement`` use the SAME logic and cannot drift apart.
    """
    for keepout in keepouts:
        if ref in (keepout.source_ref, keepout.target_ref):
            continue  # the net's own endpoint - see HighSpeedKeepout docstring
        distance = math.hypot(x - keepout.center_x_mm, y - keepout.center_y_mm)
        if distance < component.radius_mm + keepout.radius_mm:
            return True
    return False


def keepout_violations(coordinates: Dict[str, Tuple[float, float]],
                       components: Dict[str, Component],
                       keepouts: Sequence[HighSpeedKeepout]) -> List[str]:
    """Refs whose courtyard circle intersects any keepout circle.

    Returns the raw ``List[str]`` contract; ``check_keepout_overlap`` wraps
    it into a ``Finding``. Keeping the two separate is what allowed the new
    violation type to be added ALONGSIDE the existing courtyard/constraint
    gates rather than changing them.
    """
    violations: List[str] = []
    for ref, component in components.items():
        if ref not in coordinates:
            continue
        x, y = coordinates[ref]
        if violates_keepout(ref, x, y, component, keepouts):
            violations.append(ref)
    return violations


def check_keepout_overlap(coordinates: Dict[str, Tuple[float, float]],
                          components: Dict[str, Component],
                          keepouts: Sequence[HighSpeedKeepout]) -> Finding:
    """The hard acceptance gate for the high-speed keepouts."""
    refs = keepout_violations(coordinates, components, list(keepouts))
    return result(
        KEEPOUT_CHECK,
        len(components),
        [{"ref": ref} for ref in refs],
        f"{len(components)} component(s) tested against "
        f"{len(list(keepouts))} high-speed keepout zone(s) (3W rule)",
    )


# --------------------------------------------------------------------------- #
# 4. Hard acceptance gates
# --------------------------------------------------------------------------- #
def check_courtyard_overlap(components: Sequence[Component],
                            coordinates: Dict[str, Tuple[float, float]]
                            ) -> Finding:
    """Do the (rectangular, AABB) courtyards overlap — the HARD gate.

    The engine's repulsion REDUCES overlap but does not GUARANTEE its absence
    (convergence can stop in a local minimum). A separate, exact geometric
    test is therefore MANDATORY — same purpose as a CAD courtyard-overlap
    check, but before the placement is ever written to the board.
    """
    by_ref = {c.ref: c for c in components}
    refs = [ref for ref in coordinates if ref in by_ref]
    violations: List[Dict[str, object]] = []
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            ra, rb = refs[i], refs[j]
            ca, cb = by_ref[ra], by_ref[rb]
            ax, ay = coordinates[ra]
            bx, by = coordinates[rb]
            overlap_x = (ca.width_mm + cb.width_mm) / 2.0 - abs(ax - bx)
            overlap_y = (ca.height_mm + cb.height_mm) / 2.0 - abs(ay - by)
            if overlap_x > 0 and overlap_y > 0:
                violations.append({
                    "a": ra, "b": rb,
                    "overlap_x_mm": round(overlap_x, 4),
                    "overlap_y_mm": round(overlap_y, 4),
                })
    return result(
        COURTYARD_CHECK,
        len(refs),
        violations,
        f"pairwise AABB overlap test over {len(refs)} component(s)",
    )


def check_distance_constraints(constraints: Sequence[DistanceConstraint],
                               coordinates: Dict[str, Tuple[float, float]]
                               ) -> Finding:
    """MEASURE the millimetre constraints (a hard criterion, not a soft
    force).

    The engine can converge and still leave a constraint violated; this
    reports that as a number. An EMPTY constraint list is NO_COVERAGE — "I
    supplied no constraints" can never silently become "the constraints are
    met".
    """
    violations: List[Dict[str, object]] = []
    scanned = 0
    for constraint in constraints:
        if (constraint.ref_a not in coordinates
                or constraint.ref_b not in coordinates):
            continue
        scanned += 1
        ax, ay = coordinates[constraint.ref_a]
        bx, by = coordinates[constraint.ref_b]
        distance = math.hypot(ax - bx, ay - by)
        if constraint.max_mm is not None and distance > constraint.max_mm:
            violations.append({
                "constraint": f"{constraint.ref_a}-{constraint.ref_b}",
                "kind": "max", "target_mm": constraint.max_mm,
                "measured_mm": round(distance, 4), "note": constraint.note,
            })
        if constraint.min_mm is not None and distance < constraint.min_mm:
            violations.append({
                "constraint": f"{constraint.ref_a}-{constraint.ref_b}",
                "kind": "min", "target_mm": constraint.min_mm,
                "measured_mm": round(distance, 4), "note": constraint.note,
            })
    return result(
        CONSTRAINT_CHECK,
        scanned,
        violations,
        "measurement of the layout millimetre constraints",
    )


# --------------------------------------------------------------------------- #
# 3.5 Multi-attempt placement: score, calibrate, pick the best (Tur 3 drift)
# --------------------------------------------------------------------------- #
@dataclass
class PlacementParameterSet:
    """A parameter bundle for one placement ATTEMPT -
    ``try_multiple_placements`` solves the same netlist with EACH bundle
    separately."""

    name: str
    attraction: float = 0.08
    repulsion: float = 1.0
    initial_step_mm: float = 2.0
    angle_offset_rad: float = 0.0


# 4 default attempts: standard, strong attraction (tighter clustering),
# strong repulsion (more breathing room/lower overlap risk), a different
# start angle (spiral offset - can walk the engine into a different local
# minimum). A caller can supply its own set too; these are just a
# reasonable default for a caller who wants "try N without thinking about it".
DEFAULT_PARAMETER_SETS: Tuple[PlacementParameterSet, ...] = (
    PlacementParameterSet("standard", attraction=0.08, repulsion=1.0),
    PlacementParameterSet("strong_attraction", attraction=0.16, repulsion=1.0),
    PlacementParameterSet("strong_repulsion", attraction=0.08, repulsion=2.0),
    PlacementParameterSet("different_start", attraction=0.08, repulsion=1.0,
                          angle_offset_rad=math.pi / 3.0),
)


@dataclass
class PlacementScore:
    parameter_set_name: str
    total_score: float
    ratsnest_mm: float
    keepout_violation_count: int
    thermal_spread_score: float
    hs_compactness_score: float


def placement_score(
    placement: PlacementResult,
    netlist: Sequence[Net],
    components: Sequence[Component] = (),
    keepouts: Sequence["HighSpeedKeepout"] = (),
    thermal_constraints_: Sequence[DistanceConstraint] = (),
    parameter_set_name: str = "",
    weight_ratsnest: float = 1.0,
    weight_keepout: float = 50.0,
    weight_thermal: float = 1.0,
    weight_hs_compactness: float = 1.0,
) -> PlacementScore:
    """Reduces one placement RESULT to a single number - LOWER = BETTER.
    ``try_multiple_placements``'s "best" pick is made from this score.

    Components:
      - **ratsnest_mm**: ``placement.final_ratsnest_mm`` (already the
        weighted-sum airline total) - used directly.
      - **keepout_violation_count**: ``keepout_violations()`` count - the
        default weight (50x) is DELIBERATELY heavy: this is a HARD
        acceptance criterion (section 6), there is no "a little bit
        violated" category, the score must reflect that.
      - **thermal_spread_score**: if ``thermal_constraints_`` are given
        (e.g. from ``thermal_constraints()``), the ratio of each
        constraint's REAL distance to its ``max_mm`` is summed (near/over
        1.0 = risky). BOUNDARY: this is NOT a real thermal simulation -
        it only measures how close to the edge of Phase 4b's own mm
        constraint the placement stayed.
      - **hs_compactness_score**: the bounding-box diagonal of each
        high-speed net group is summed - a small diagonal means
        "compact/easy to route".

    The total score is a WEIGHTED SUM (lower = better).
    """
    violation_count, thermal_score, hs_compactness = _score_components(
        placement, netlist, components, keepouts, thermal_constraints_)
    total = (
        weight_ratsnest * placement.final_ratsnest_mm
        + weight_keepout * violation_count
        + weight_thermal * thermal_score
        + weight_hs_compactness * hs_compactness
    )
    return PlacementScore(
        parameter_set_name=parameter_set_name,
        total_score=round(total, 4),
        ratsnest_mm=placement.final_ratsnest_mm,
        keepout_violation_count=violation_count,
        thermal_spread_score=round(thermal_score, 4),
        hs_compactness_score=round(hs_compactness, 4),
    )


def _score_components(
    placement: PlacementResult,
    netlist: Sequence[Net],
    components: Sequence[Component] = (),
    keepouts: Sequence["HighSpeedKeepout"] = (),
    thermal_constraints_: Sequence[DistanceConstraint] = (),
) -> Tuple[int, float, float]:
    """The UNWEIGHTED raw components of ``placement_score()`` (keepout
    violation count, thermal spread score, hs compactness score) - both
    ``placement_score()`` and ``calibrate_score_weights()``'s feature
    extraction feed from this ONE place, so the same logic cannot silently
    diverge between the two.
    """
    violation_count = 0
    if keepouts and components:
        component_map = {c.ref: c for c in components}
        violation_count = len(keepout_violations(placement.coordinates, component_map, list(keepouts)))

    thermal_score = 0.0
    for constraint in thermal_constraints_:
        if constraint.max_mm is None:
            continue
        if constraint.ref_a not in placement.coordinates or constraint.ref_b not in placement.coordinates:
            continue
        ax, ay = placement.coordinates[constraint.ref_a]
        bx, by = placement.coordinates[constraint.ref_b]
        d = math.hypot(ax - bx, ay - by)
        thermal_score += d / constraint.max_mm

    hs_compactness = 0.0
    for net in netlist:
        if not is_high_speed_net(net.name):
            continue
        pins = [p for p in sorted(set(net.connections)) if p in placement.coordinates]
        if len(pins) < 2:
            continue
        xs = [placement.coordinates[p][0] for p in pins]
        ys = [placement.coordinates[p][1] for p in pins]
        hs_compactness += math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    return violation_count, thermal_score, hs_compactness


@dataclass
class HistoricalPlacementExample:
    """A calibration input: a PAST placement's raw score components + that
    placement's REAL (human/post-DRC-verified) quality score.

    ``real_quality_score``: LOWER = BETTER (same direction as
    ``placement_score()``) - e.g. should be a concrete, later-measured
    value such as the DRC violations remaining after real routing plus the
    number of components needing rework, NOT a number ``placement_score()``
    produced itself (otherwise calibration is circular/meaningless).
    """

    name: str
    ratsnest_mm: float
    keepout_violation_count: int
    thermal_spread_score: float
    hs_compactness_score: float
    real_quality_score: float


@dataclass
class CalibratedWeightSet:
    weight_ratsnest: float
    weight_keepout: float
    weight_thermal: float
    weight_hs_compactness: float
    residual_norm: float
    example_count: int


MIN_CALIBRATION_EXAMPLES = 8
RECOMMENDED_CALIBRATION_EXAMPLES = 10


class CalibrationDependencyMissing(RuntimeError):
    """numpy/scipy are not installed. Actionable by design."""


def calibrate_score_weights(examples: Sequence[HistoricalPlacementExample]) -> CalibratedWeightSet:
    """Calibrates ``placement_score()``'s four weights (ratsnest/keepout/
    thermal/hs-compactness) from historical ``examples`` with a SMALL-SCALE,
    FULLY DETERMINISTIC linear regression.

    NOT a "large ML model": uses ``scipy.optimize.nnls`` (non-negative
    least squares) to find the WEIGHTS that best fit the raw score
    components (X) to the real quality score (y) - 4 unknowns, closed-form,
    does NOT use ``random``; the same ``examples`` ALWAYS produce the same
    weights.

    The non-negative constraint is DELIBERATE: ``placement_score()``'s own
    semantics ("the more of each component, the worse") CONTRADICT a
    negative weight - e.g. "more keepout violations IMPROVES the score"
    would be physically meaningless, and NNLS makes that structurally
    IMPOSSIBLE (plain OLS does not guarantee this).

    At least ``MIN_CALIBRATION_EXAMPLES`` (8) examples are REQUIRED - 4
    equations for 4 unknowns is theoretically enough but risks overfitting
    in practice; fewer raises ``ValueError``. Fewer than
    ``RECOMMENDED_CALIBRATION_EXAMPLES`` (10) is ACCEPTED but the resulting
    ``residual_norm`` should be read with particular care (a low residual
    on few examples can be MISLEADING).

    BOUNDARY: this function does not verify how REPRESENTATIVE ``examples``
    itself is - 10-20 examples do not always mean "diverse enough", that is
    the caller's responsibility.

    Needs ``numpy``/``scipy`` (imported lazily here only, module import
    stays stdlib-only): raises ``CalibrationDependencyMissing`` with an
    actionable message if they are not installed.
    """
    if len(examples) < MIN_CALIBRATION_EXAMPLES:
        raise ValueError(
            f"calibration needs at least {MIN_CALIBRATION_EXAMPLES} historical examples, "
            f"{len(examples)} given.")

    try:
        import numpy as np
        from scipy.optimize import nnls
    except ImportError as exc:
        raise CalibrationDependencyMissing(
            "calibrate_score_weights needs the optional numpy/scipy tier, which is not "
            f"installed ({exc}). Install with: pip install numpy scipy. Every other "
            "function in this module is stdlib-only and is unaffected.") from exc

    feature_matrix = np.array([
        [e.ratsnest_mm, float(e.keepout_violation_count), e.thermal_spread_score,
         e.hs_compactness_score]
        for e in examples
    ])
    target_vector = np.array([e.real_quality_score for e in examples])

    weights, residual_norm = nnls(feature_matrix, target_vector)

    return CalibratedWeightSet(
        weight_ratsnest=round(float(weights[0]), 6),
        weight_keepout=round(float(weights[1]), 6),
        weight_thermal=round(float(weights[2]), 6),
        weight_hs_compactness=round(float(weights[3]), 6),
        residual_norm=round(float(residual_norm), 6),
        example_count=len(examples),
    )


def example_from_placement(
    name: str,
    placement: PlacementResult,
    netlist: Sequence[Net],
    real_quality_score: float,
    components: Sequence[Component] = (),
    keepouts: Sequence["HighSpeedKeepout"] = (),
    thermal_constraints_: Sequence[DistanceConstraint] = (),
) -> HistoricalPlacementExample:
    """Produces a ``HistoricalPlacementExample`` from a real ``PlacementResult``
    (+ an externally supplied, REAL/verified ``real_quality_score``) - calls
    ``_score_components()`` (the SAME function ``placement_score()`` uses) to
    extract the raw components, no manual recomputation/copying needed.
    """
    violation_count, thermal_score, hs_compactness = _score_components(
        placement, netlist, components, keepouts, thermal_constraints_)
    return HistoricalPlacementExample(
        name=name,
        ratsnest_mm=placement.final_ratsnest_mm,
        keepout_violation_count=violation_count,
        thermal_spread_score=round(thermal_score, 4),
        hs_compactness_score=round(hs_compactness, 4),
        real_quality_score=real_quality_score,
    )


@dataclass
class MultiPlacementResult:
    best_name: str
    best_result: PlacementResult
    all_results: Dict[str, PlacementResult]
    all_scores: Dict[str, PlacementScore]


def try_multiple_placements(
    components: Sequence[Component],
    netlist: Sequence[Net],
    board_width_mm: float,
    board_height_mm: float,
    constraints: Sequence[DistanceConstraint] = (),
    keepouts: Sequence["HighSpeedKeepout"] = (),
    thermal_constraints_: Sequence[DistanceConstraint] = (),
    parameter_sets: Sequence[PlacementParameterSet] = DEFAULT_PARAMETER_SETS,
    **common_kwargs,
) -> MultiPlacementResult:
    """Solves the SAME netlist SEPARATELY with EVERY parameter bundle in
    ``parameter_sets`` - each is FULLY DETERMINISTIC on its own (same
    parameter set + same netlist -> ALWAYS the same result, ``random`` is
    never used anywhere); there IS diversity ACROSS ``parameter_sets`` (this
    steers the engine into different local minima), there is NO randomness.

    Every result is scored with ``placement_score()``, the LOWEST score
    (best) is recommended - but ALL results (with their scores) are returned
    in ``all_results``/``all_scores``: a "single answer" is never forced on
    the caller, a human/agent can see why B was recommended over A.
    """
    if not parameter_sets:
        raise ValueError("at least one parameter set must be given")

    all_results: Dict[str, PlacementResult] = {}
    all_scores: Dict[str, PlacementScore] = {}
    for pset in parameter_sets:
        placement = solve_placement(
            components, netlist, board_width_mm, board_height_mm, constraints,
            attraction=pset.attraction,
            repulsion=pset.repulsion,
            initial_step_mm=pset.initial_step_mm,
            angle_offset_rad=pset.angle_offset_rad,
            keepouts=keepouts,
            **common_kwargs,
        )
        score = placement_score(
            placement, netlist, components, keepouts=keepouts,
            thermal_constraints_=thermal_constraints_, parameter_set_name=pset.name,
        )
        all_results[pset.name] = placement
        all_scores[pset.name] = score

    best_name = min(all_scores, key=lambda name: all_scores[name].total_score)
    return MultiPlacementResult(
        best_name=best_name,
        best_result=all_results[best_name],
        all_results=all_results,
        all_scores=all_scores,
    )


def placement_markdown_report(placement: PlacementResult,
                              clusters: Sequence[Sequence[str]],
                              findings: Sequence[Finding]) -> str:
    """Human-readable Markdown summary. Goes to stderr or a file, never to
    stdout, which stays machine-readable JSON."""
    lines = [
        "# Force-Directed Placement Report",
        "",
        f"- Iterations: {placement.iterations}",
        f"- Converged: {'YES' if placement.converged else 'NO (hit max iterations)'}",
        f"- Ratsnest (weighted): {placement.initial_ratsnest_mm}mm -> "
        f"{placement.final_ratsnest_mm}mm "
        f"(improvement {placement.improvement_ratio * 100:.1f}%)",
        "",
        "## Clusters (ratsnest based)",
    ]
    for index, cluster in enumerate(clusters, 1):
        lines.append(f"{index}. {', '.join(cluster)}")
    lines += ["", "## Gates", ""]
    for finding in findings:
        lines.append(
            f"- **{finding['check']}**: {finding['status']} "
            f"(scanned={finding['scanned']}, "
            f"violations={len(finding['violations'])})")
    lines += [
        "",
        "> WARNING: this is a SEED placement. It is not approved until the",
        "> 3-D/enclosure keepout check and the layout millimetre rules have",
        "> been run separately.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def _number(container: dict, key: str, where: str,
            default: Optional[float] = None) -> float:
    if key not in container:
        if default is not None:
            return default
        raise ValueError(f"{where}: missing required field '{key}'")
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: '{key}' must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{where}: '{key}' must be finite, got {value!r}")
    return float(value)


def load_problem(path: Path) -> dict:
    """Read the positional JSON placement problem. UTF-8, object at the root."""
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


def parse_components(problem: dict) -> Tuple[List[Component],
                                             Dict[str, PlacementCategory]]:
    components: List[Component] = []
    categories: Dict[str, PlacementCategory] = {}
    for index, item in enumerate(problem.get("components", []) or []):
        where = f"components[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"{where}: 'ref' must be a non-empty string")
        components.append(Component(
            ref=ref,
            width_mm=_number(item, "width_mm", where, 2.0),
            height_mm=_number(item, "height_mm", where, 2.0),
            x=_number(item, "x", where, 0.0),
            y=_number(item, "y", where, 0.0),
            fixed=bool(item.get("fixed", False)),
        ))
        category = item.get("category")
        if category is not None:
            try:
                categories[ref] = PlacementCategory(category)
            except ValueError as exc:
                raise ValueError(
                    f"{where}: unknown category {category!r}; expected one of "
                    f"{[c.value for c in PlacementCategory]}") from exc
    return components, categories


def parse_nets(problem: dict) -> List[Net]:
    nets: List[Net] = []
    for index, item in enumerate(problem.get("nets", []) or []):
        where = f"nets[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        name = item.get("name", item.get("net"))
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: 'name' must be a non-empty string")
        connections = item.get("connections")
        if (not isinstance(connections, list)
                or any(not isinstance(c, str) for c in connections)):
            raise ValueError(f"{where}: 'connections' must be a list of strings")
        nets.append(Net(name=name, connections=list(connections),
                        weight=_number(item, "weight", where, 1.0)))
    return nets


def parse_constraints(problem: dict) -> List[DistanceConstraint]:
    constraints: List[DistanceConstraint] = []
    for index, item in enumerate(problem.get("constraints", []) or []):
        where = f"constraints[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        for key in ("ref_a", "ref_b"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"{where}: '{key}' must be a non-empty string")
        if item.get("max_mm") is None and item.get("min_mm") is None:
            raise ValueError(f"{where}: at least one of 'max_mm'/'min_mm' is required")
        constraints.append(DistanceConstraint(
            ref_a=item["ref_a"],
            ref_b=item["ref_b"],
            max_mm=(None if item.get("max_mm") is None
                    else _number(item, "max_mm", where)),
            min_mm=(None if item.get("min_mm") is None
                    else _number(item, "min_mm", where)),
            note=str(item.get("note", "")),
        ))
    return constraints


def solve_problem(problem: dict, args: argparse.Namespace, source: str) -> dict:
    """Run the engine over a parsed problem file and build the report."""
    board = problem.get("board")
    if not isinstance(board, dict):
        raise ValueError("problem: 'board' must be an object with width_mm/height_mm")
    board_width = _number(board, "width_mm", "board")
    board_height = _number(board, "height_mm", "board")

    components, categories = parse_components(problem)
    netlist = parse_nets(problem)
    constraints = parse_constraints(problem)

    solver = problem.get("solver") or {}
    if not isinstance(solver, dict):
        raise ValueError("problem: 'solver' must be an object")
    kwargs = {
        "max_iterations": int(args.max_iterations
                              if args.max_iterations is not None
                              else solver.get("max_iterations", 400)),
        "attraction": float(solver.get("attraction", 0.08)),
        "repulsion": float(solver.get("repulsion", 1.0)),
        "convergence_mm": float(solver.get("convergence_mm", 0.01)),
        "initial_step_mm": float(solver.get("initial_step_mm", 2.0)),
    }

    hierarchical = bool(args.hierarchical or problem.get("hierarchical", False))
    if hierarchical:
        placement = solve_hierarchical_placement(
            components, categories, netlist, board_width, board_height,
            constraints, **kwargs)
    else:
        placement = solve_placement(
            components, netlist, board_width, board_height, constraints,
            **kwargs)

    trace_width_mm = args.trace_width_mm
    if trace_width_mm is None:
        high_speed = problem.get("high_speed") or {}
        if isinstance(high_speed, dict) and "trace_width_mm" in high_speed:
            trace_width_mm = _number(high_speed, "trace_width_mm", "high_speed")

    keepouts: List[HighSpeedKeepout] = []
    if trace_width_mm is not None:
        for net in netlist:
            keepouts.extend(high_speed_keepouts(
                net, placement.coordinates, float(trace_width_mm)))

    by_ref = {c.ref: c for c in components}
    edges = build_graph(netlist)
    clusters = find_clusters(edges, [c.ref for c in components])

    findings = [
        check_courtyard_overlap(components, placement.coordinates),
        check_distance_constraints(constraints, placement.coordinates),
    ]
    if trace_width_mm is not None:
        findings.append(check_keepout_overlap(
            placement.coordinates, by_ref, keepouts))

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "input": source,
        "model_class": "seed_placement",
        "signoff": False,
        "params": {
            "board_width_mm": board_width,
            "board_height_mm": board_height,
            "hierarchical": hierarchical,
            "trace_width_mm": (None if trace_width_mm is None
                               else float(trace_width_mm)),
            **kwargs,
        },
        "placement": {
            "coordinates": {ref: [x, y]
                            for ref, (x, y) in sorted(placement.coordinates.items())},
            "iterations": placement.iterations,
            "converged": placement.converged,
            "last_move_mm": placement.last_move_mm,
            "initial_ratsnest_mm": placement.initial_ratsnest_mm,
            "final_ratsnest_mm": placement.final_ratsnest_mm,
            "improvement_ratio": round(placement.improvement_ratio, 6),
        },
        "clusters": clusters,
        "keepouts": [
            {"net": k.net_name,
             "center_mm": [round(k.center_x_mm, 4), round(k.center_y_mm, 4)],
             "radius_mm": round(k.radius_mm, 4),
             "source_ref": k.source_ref, "target_ref": k.target_ref}
            for k in keepouts
        ],
        "checks": findings,
        "summary": summarise(findings),
    }


def exit_code_for(report: dict) -> int:
    """0 only when no gate FAILed and no gate was empty.

    NO_COVERAGE exits 1 on purpose: a run whose gates measured nothing must
    not be reported as a clean placement.
    """
    summary = report["summary"]
    if summary["FAIL"] or summary["NO_COVERAGE"]:
        return EXIT_FAIL
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 5. Self-test + fault injection (OPT-IN, never on a normal run)
# --------------------------------------------------------------------------- #
def prove_tests_not_empty() -> bool:
    """FAULT INJECTION: with the attraction coefficient set to 0 the engine
    MUST NOT be able to improve the ratsnest. That is the proof that the
    ``improvement_ratio`` test measures something (otherwise it would pass
    under every condition)."""
    components = [Component(f"U{i}", 2.0, 2.0) for i in range(6)]
    netlist = [Net("SIG", ["U0", "U5"]), Net("SIG2", ["U1", "U4"])]
    broken = solve_placement(components, netlist, 50.0, 50.0, attraction=0.0)
    return broken.improvement_ratio < 0.01


def run_self_tests() -> List[str]:
    """Return a list of failure messages; an empty list means PASS. Never
    prints — the CLI routes the messages to stderr via ``diag``."""
    errors: List[str] = []

    # 1. Determinism: same input -> same output.
    components = [Component(f"U{i}") for i in range(8)]
    netlist = [Net("A", ["U0", "U1"]), Net("B", ["U2", "U3", "U4"])]
    first = solve_placement(components, netlist, 40.0, 40.0)
    second = solve_placement([Component(f"U{i}") for i in range(8)],
                             netlist, 40.0, 40.0)
    if first.coordinates != second.coordinates:
        errors.append("determinism violated: the same netlist produced two "
                      "different placements")

    # 2. Does the engine actually shorten the ratsnest?
    if first.improvement_ratio <= 0.0:
        errors.append("the engine did not improve the ratsnest length")

    # 3. Does the fault injection really break?
    if not prove_tests_not_empty():
        errors.append("fault injection did not break: the improvement test may "
                      "be vacuous")

    # 4. Power nets must stay out of the graph.
    if build_graph([Net("GND", ["U0", "U1", "U2"])]):
        errors.append("the GND net entered the graph (it must be a plane)")

    # 5. An empty gate must be NO_COVERAGE, never a silent PASS.
    if check_distance_constraints([], {"U1": (0.0, 0.0)})["status"] != "NO_COVERAGE":
        errors.append("an empty constraint list did not read as NO_COVERAGE")

    return errors


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("problem", type=Path, nargs="?",
                        help="JSON placement problem (components, nets, board "
                             "outline); required unless --self-test")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="override the solver iteration cap")
    parser.add_argument("--hierarchical", action="store_true",
                        help="force the 3-stage hierarchical placement")
    parser.add_argument("--trace-width-mm", type=float, default=None,
                        help="high-speed trace width; enables the 3W keepout gate")
    parser.add_argument("--markdown", type=Path,
                        help="also WRITE the human-readable Markdown summary here")
    parser.add_argument("--json", type=Path,
                        help="also WRITE the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the built-in self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --self-test is OPT-IN; the source ran it as the only thing __main__ did
    # and printed the outcome to stdout.
    if args.self_test:
        errors = run_self_tests()
        for message in errors:
            diag(f"SELF-TEST FAIL: {message}")
        return EXIT_FAIL if errors else EXIT_OK

    if args.problem is None:
        parser.error("a JSON placement problem is required; pass it "
                     "positionally (e.g. `force_directed.py problem.json`) "
                     "or use --self-test")

    problem = load_problem(args.problem)
    report = solve_problem(problem, args, str(args.problem))

    if args.markdown is not None:
        placement = report["placement"]
        summary_text = placement_markdown_report(
            PlacementResult(
                coordinates={ref: (xy[0], xy[1])
                             for ref, xy in placement["coordinates"].items()},
                iterations=placement["iterations"],
                converged=placement["converged"],
                last_move_mm=placement["last_move_mm"],
                initial_ratsnest_mm=placement["initial_ratsnest_mm"],
                final_ratsnest_mm=placement["final_ratsnest_mm"]),
            report["clusters"], report["checks"])
        Path(args.markdown).write_text(summary_text, encoding="utf-8")

    diag(f"iterations={report['placement']['iterations']} "
         f"summary={report['summary']}")
    write_report(report, args.json)
    emit(report)
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
