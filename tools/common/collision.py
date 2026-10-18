"""Duck-typed collision geometry: 2-D boxes, track obstacles, 3-D volumes.

Ported from ``pcb_carpisma_radari.py`` and the geometry half of
``mcad_carpisma_koprusu.py``. The split is the whole point: NOTHING here
imports ``pcbnew``. Every function works on plain dataclasses, so the
collision maths can be tested on a machine without KiCad, and the pcbnew
loaders live in ``scripts/board/collision_radar.py`` where the bundle
confines the import.

TRAPS PRESERVED FROM THE SOURCE
-------------------------------
* Edge-to-edge contact (overlap exactly 0) is NOT a collision. Two parts
  touching at their bounding boxes is legal placement; ``tolerance_mm``
  keeps float noise from turning it into a violation.
* The board outline is a BOUNDING BOX, not a polygon containment test. On a
  round or chamfered outline a corner inside the bbox may still be outside
  the real board, so ``outside_board`` can be a false NEGATIVE there. This
  is documented, not silently assumed away.
* Track obstacles get a two-phase test: the AABB is a cheap broad phase, and
  ``point_to_segment_mm`` is the narrow phase. A 45-degree track's AABB is
  much larger than the track, and treating the AABB as solid produced real
  false positives (a point >1 mm clear of the copper reported as blocked).
* Rotated 3-D bodies are approximated by their axis-aligned bounding box,
  which is always LARGER than the true shape - conservative in the safe
  direction, never narrower.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .findings import Finding, result

NM_PER_MM = 1_000_000


def nm_to_mm(value_nm: float) -> float:
    """pcbnew internal nanometres -> millimetres."""
    return value_nm / NM_PER_MM


# --------------------------------------------------------------------------- #
# 2-D axis-aligned box
# --------------------------------------------------------------------------- #
@dataclass
class BBox:
    """An axis-aligned bounding box in millimetres."""

    ref: str
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x_min + self.x_max) / 2.0, (self.y_min + self.y_max) / 2.0)


def overlap_xy(a: BBox, b: BBox) -> tuple[float, float]:
    """Per-axis overlap of two boxes. Both > 0 means a real 2-D collision."""
    return (
        min(a.x_max, b.x_max) - max(a.x_min, b.x_min),
        min(a.y_max, b.y_max) - max(a.y_min, b.y_min),
    )


def boxes_collide(a: BBox, b: BBox, tolerance_mm: float = 1e-9):
    """``(overlap_x, overlap_y)`` when the boxes really overlap, else None.

    Exactly touching edges are not a collision - see the module docstring.
    """
    ix, iy = overlap_xy(a, b)
    if ix > tolerance_mm and iy > tolerance_mm:
        return (round(ix, 4), round(iy, 4))
    return None


def _escape_vector(fixed: BBox, moving: BBox, ix: float, iy: float,
                   margin_mm: float = 0.1) -> tuple[float, float]:
    """Minimum signed X/Y move that separates ``moving`` from ``fixed``.

    Only a starting suggestion: which axis is the sensible one to move along
    (so the part does not hit a third neighbour) is the caller's decision.
    """
    fx, fy = fixed.center
    mx, my = moving.center
    sign_x = 1.0 if mx >= fx else -1.0
    sign_y = 1.0 if my >= fy else -1.0
    return (round(sign_x * (ix + margin_mm), 4), round(sign_y * (iy + margin_mm), 4))


def colliding_pairs(boxes: dict[str, BBox]) -> list[dict]:
    """Every overlapping footprint pair, as violation records."""
    violations: list[dict] = []
    refs = sorted(boxes)
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            a, b = boxes[refs[i]], boxes[refs[j]]
            overlap = boxes_collide(a, b)
            if overlap is None:
                continue
            ix, iy = overlap
            esc_x, esc_y = _escape_vector(a, b, ix, iy)
            violations.append({
                "kind": "COLLISION",
                "part_a": a.ref,
                "part_b": b.ref,
                "overlap_x_mm": ix,
                "overlap_y_mm": iy,
                "suggested_escape_x_mm": esc_x,
                "suggested_escape_y_mm": esc_y,
            })
    return violations


def outside_board(boxes: dict[str, BBox], board: BBox) -> list[dict]:
    """Footprints sticking out past the board outline bounding box.

    LIMIT: ``board`` is a bbox, so a rounded or chamfered outline can hide a
    real overhang (false negative). Rectangular boards are exact.
    """
    violations: list[dict] = []
    margin = 0.1
    for ref in sorted(boxes):
        fp = boxes[ref]
        over_left = max(0.0, board.x_min - fp.x_min)
        over_right = max(0.0, fp.x_max - board.x_max)
        over_top = max(0.0, board.y_min - fp.y_min)
        over_bottom = max(0.0, fp.y_max - board.y_max)
        over_x = max(over_left, over_right)
        over_y = max(over_top, over_bottom)
        if over_x <= 1e-9 and over_y <= 1e-9:
            continue
        sign_x = -1.0 if over_left >= over_right else 1.0
        sign_y = -1.0 if over_top >= over_bottom else 1.0
        violations.append({
            "kind": "OUTSIDE_BOARD",
            "part_a": ref,
            "part_b": None,
            "overlap_x_mm": round(over_x, 4),
            "overlap_y_mm": round(over_y, 4),
            "suggested_escape_x_mm":
                round(sign_x * (over_x + margin), 4) if over_x > 1e-9 else 0.0,
            "suggested_escape_y_mm":
                round(sign_y * (over_y + margin), 4) if over_y > 1e-9 else 0.0,
        })
    return violations


def collision_finding(boxes: dict[str, BBox], board: BBox | None = None) -> Finding:
    """The PASS/FAIL/NO_COVERAGE finding for a placement scan.

    ``scanned`` counts the pair combinations actually examined plus, when a
    board outline is supplied, one outline check per part - so an empty or
    single-part board can never report a silent PASS.
    """
    violations = colliding_pairs(boxes)
    n = len(boxes)
    scanned = n * (n - 1) // 2
    detail = f"{n} parts, {scanned} pairwise combinations"
    if board is not None:
        violations.extend(outside_board(boxes, board))
        scanned += n
        detail += f" + {n} outline checks"
    return result("collision_radar", scanned, violations, detail + " scanned.")


# --------------------------------------------------------------------------- #
# Track / via obstacles - broad phase AABB, narrow phase real distance
# --------------------------------------------------------------------------- #
def point_to_segment_mm(px: float, py: float,
                        x1: float, y1: float, x2: float, y2: float) -> float:
    """Shortest distance from a point to a line SEGMENT (not the infinite line).

    A zero-length segment is a point obstacle (a via); the formula already
    degenerates to point-to-point, so no special case is needed.
    """
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


@dataclass
class TrackObstacle:
    """A copper track or via as a segment obstacle with an AABB facade.

    Exposes ``x_min``/``y_min``/``x_max``/``y_max`` so it drops into any
    box-shaped broad-phase check unchanged, plus ``x1/y1/x2/y2/width_mm`` so
    a narrow phase can cancel the AABB false positives. A plain ``BBox`` has
    no ``x1``, so it never enters the narrow phase and stays a solid body.
    """

    ref: str
    x1: float
    y1: float
    x2: float
    y2: float
    width_mm: float

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

    def blocks_point(self, px: float, py: float, clearance_mm: float = 0.0) -> bool:
        """Two-phase test: AABB first, then the real distance to the segment.

        The broad phase is inflated by ``clearance_mm`` as well. Without that
        the clearance would be silently capped by the un-inflated AABB and a
        point just outside the copper but inside the clearance ring would be
        reported as free.
        """
        if not (self.x_min - clearance_mm <= px <= self.x_max + clearance_mm
                and self.y_min - clearance_mm <= py <= self.y_max + clearance_mm):
            return False
        return point_to_segment_mm(px, py, self.x1, self.y1, self.x2, self.y2) \
            <= self.width_mm / 2.0 + clearance_mm


# --------------------------------------------------------------------------- #
# 3-D bodies and enclosure obstruction volumes (MCAD)
# --------------------------------------------------------------------------- #
Box3D = tuple[float, float, float, float, float, float]  # xmin ymin zmin xmax ymax zmax


@dataclass
class Placement:
    """Where a footprint sits on the board."""

    ref: str
    footprint: str
    x_mm: float
    y_mm: float
    angle_deg: float
    layer: str  # "F.Cu" | "B.Cu"


@dataclass
class Body3D:
    """A component body, origin-relative and BEFORE rotation is applied."""

    width_mm: float
    depth_mm: float
    height_mm: float


@dataclass
class ObstructionVolume:
    """An enclosure obstruction: screw boss, connector cutout, lid inner face.

    Z reference is the board's top surface = 0.
    """

    name: str
    x_min_mm: float
    y_min_mm: float
    x_max_mm: float
    y_max_mm: float
    z_min_mm: float
    z_max_mm: float


def rotated_aabb(cx: float, cy: float, width: float, depth: float,
                 angle_deg: float) -> tuple[float, float, float, float]:
    """AABB of a rotated rectangle - always >= the true shape."""
    hw, hd = width / 2.0, depth / 2.0
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    xs, ys = [], []
    for kx, ky in ((-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)):
        xs.append(cx + kx * cos_a - ky * sin_a)
        ys.append(cy + kx * sin_a + ky * cos_a)
    return (min(xs), min(ys), max(xs), max(ys))


def component_box_3d(placement: Placement, body: Body3D,
                     board_thickness_mm: float = 1.6) -> Box3D:
    """A component's real 3-D box from placement + rotation + layer.

    Board top surface is z=0. An F.Cu part occupies ``[0, height]``; a B.Cu
    part occupies ``[-thickness - height, -thickness]``, so the two sides
    can never be mistaken for each other.
    """
    x_min, y_min, x_max, y_max = rotated_aabb(
        placement.x_mm, placement.y_mm, body.width_mm, body.depth_mm,
        placement.angle_deg)
    if placement.layer == "F.Cu":
        z_min, z_max = 0.0, body.height_mm
    else:
        z_min, z_max = -board_thickness_mm - body.height_mm, -board_thickness_mm
    return (x_min, y_min, z_min, x_max, y_max, z_max)


def boxes_collide_3d(a: Box3D, b: Box3D):
    """``(ox, oy, oz)`` when all three axes overlap, else None.

    Overlapping in XY but separated in Z (a part under the board versus a
    boss on the lid) is NOT a collision.
    """
    ox = min(a[3], b[3]) - max(a[0], b[0])
    oy = min(a[4], b[4]) - max(a[1], b[1])
    oz = min(a[5], b[5]) - max(a[2], b[2])
    if ox > 0 and oy > 0 and oz > 0:
        return (round(ox, 4), round(oy, 4), round(oz, 4))
    return None


def mcad_collision_finding(placements, bodies: dict[str, Body3D],
                           volumes, board_thickness_mm: float = 1.6) -> Finding:
    """Test every component with known body data against every obstruction.

    A refdes with no body data is SKIPPED and does NOT count towards
    ``scanned``: without a body we cannot claim "no collision", only "not
    tested". No body data at all therefore yields NO_COVERAGE.
    """
    placements = list(placements)
    volumes = list(volumes)
    violations: list[dict] = []
    scanned = 0
    for placement in placements:
        body = bodies.get(placement.ref)
        if body is None:
            continue
        scanned += 1
        box = component_box_3d(placement, body, board_thickness_mm)
        for volume in volumes:
            overlap = boxes_collide_3d(box, (
                volume.x_min_mm, volume.y_min_mm, volume.z_min_mm,
                volume.x_max_mm, volume.y_max_mm, volume.z_max_mm))
            if overlap is None:
                continue
            violations.append({
                "component": placement.ref,
                "obstruction": volume.name,
                "overlap_x_mm": overlap[0],
                "overlap_y_mm": overlap[1],
                "overlap_z_mm": overlap[2],
                "message": f"{placement.ref} collides with '{volume.name}' "
                           f"(X={overlap[0]} Y={overlap[1]} Z={overlap[2]} mm overlap).",
            })
    return result(
        "mcad_3d_collision", scanned, violations,
        f"{len(placements)} placements, {scanned} had body data, "
        f"tested against {len(volumes)} obstruction volumes")
