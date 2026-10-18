"""2-D geometry and pcbnew board queries shared by the board checkers.

Hoisted from stitch_density, dfm_emc_check and mask_dam, which between them
carried two implementations of ``dist_point_to_points``, two identical
``vias`` one-liners, and two independent Edge.Cuts sampling routines.

Importing this module requires ``pcbnew`` only for the board-query helpers at
the bottom; the pure-maths helpers above work without it, so tests can
exercise the geometry without KiCad installed.
"""

from __future__ import annotations

import math

from .units import mm

# --------------------------------------------------------------------------- #
# Pure 2-D geometry - no pcbnew needed
# --------------------------------------------------------------------------- #


def point_in_polygon(x: float, y: float, polygon) -> bool:
    """Standard ray-casting (even-odd rule) point-in-polygon test. No
    external dependency (shapely) needed.

    Hoisted here (2026-08-06) after the third independent copy of this
    exact function turned up during the pcb-tool-v2 merge (mechanical_dxf,
    current_density, reference_plane all needed it) - same duplication this
    module already exists to kill.
    """
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1:
            inside = not inside
    return inside



def dist_point_to_points(point, points) -> float:
    """Minimum distance from ``point`` to any of ``points``.

    Returns ``inf`` for an empty set, so an empty candidate list reads as
    "infinitely far" rather than raising - callers turn that into
    NO_COVERAGE.
    """
    if not points:
        return float("inf")
    x, y = point
    return min(math.hypot(px - x, py - y) for px, py in points)


def ccw(a, b, c) -> float:
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d) -> bool:
    d1, d2 = ccw(c, d, a), ccw(c, d, b)
    d3, d4 = ccw(a, b, c), ccw(a, b, d)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def seg_point_dist(p, a, b) -> float:
    """Distance from point ``p`` to segment ``a``-``b``."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(p[0] - cx, p[1] - cy)


def seg_seg_min_dist(p1, p2, p3, p4) -> float:
    """Minimum distance between two segments (0 if they cross)."""
    if segments_intersect(p1, p2, p3, p4):
        return 0.0
    return min(
        seg_point_dist(p1, p3, p4), seg_point_dist(p2, p3, p4),
        seg_point_dist(p3, p1, p2), seg_point_dist(p4, p1, p2),
    )


# --------------------------------------------------------------------------- #
# pcbnew board queries - imported lazily so the maths above stays importable
# --------------------------------------------------------------------------- #
def vias(board) -> list:
    """Every PCB_VIA on the board."""
    return [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]


def tracks(board) -> list:
    """Every straight PCB_TRACK (arcs excluded - a documented limitation)."""
    return [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]


def nets_matching(board, regex) -> set:
    """Net names matching a compiled-or-string regex."""
    import re
    pattern = re.compile(regex) if isinstance(regex, str) else regex
    out = set()
    for net in board.GetNetsByName().keys():
        name = str(net)
        if pattern.search(name):
            out.add(name)
    return out


def edge_cuts_points(board, step_mm: float = 1.0,
                     approx_mm: float = 0.05) -> list[tuple[float, float]]:
    """Sample the Edge.Cuts outline into a point list, in millimetres.

    Straight ``S_SEGMENT`` edges are sampled at a fixed ``step_mm`` interval;
    arcs, circles and polygons fall back to ``TransformShapeToPolygon``
    corners at ``approx_mm`` accuracy (coarser - a documented limit).

    ACCURACY BUG THIS FIXES
    -----------------------
    dfm_emc_check used to reduce EVERY edge to polygon corners, which on a
    straight segment yields only the four corners of a thin rectangle. Its
    edge-keepout check therefore measured distance to segment ENDPOINTS, so
    a part sitting 0.9 mm from the middle of a 60 mm edge was scored as ~20 mm
    away and passed. stitch_density already had the interval sampler; the two
    now share it, so the keepout check gains the resolution it always claimed.
    """
    import math

    import pcbnew
    points: list[tuple[float, float]] = []
    for drawing in board.GetDrawings():
        if drawing.GetLayer() != pcbnew.Edge_Cuts:
            continue

        is_segment = False
        try:
            is_segment = drawing.GetShape() == pcbnew.SHAPE_T_SEGMENT
        except Exception:
            is_segment = False

        if is_segment and step_mm > 0:
            start = (mm(drawing.GetStart().x), mm(drawing.GetStart().y))
            end = (mm(drawing.GetEnd().x), mm(drawing.GetEnd().y))
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            steps = max(1, int(math.ceil(length / step_mm)))
            for k in range(steps + 1):
                t = k / steps
                points.append((start[0] + (end[0] - start[0]) * t,
                               start[1] + (end[1] - start[1]) * t))
            continue

        poly = pcbnew.SHAPE_POLY_SET()
        try:
            drawing.TransformShapeToPolygon(
                poly, pcbnew.UNDEFINED_LAYER, 0,
                pcbnew.FromMM(approx_mm), pcbnew.ERROR_INSIDE)
        except Exception:
            continue
        for oi in range(poly.OutlineCount()):
            outline = poly.Outline(oi)
            for i in range(outline.PointCount()):
                p = outline.CPoint(i)
                points.append((mm(p.x), mm(p.y)))
    return points
