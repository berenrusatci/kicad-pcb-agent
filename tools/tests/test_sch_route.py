#!/usr/bin/env python3
"""Tests for scripts/sch/sch_route.py — the pin-to-pin orthogonal router.

WHY THIS FILE EXISTS: the capability inventory reported this module as
untested, and a rigorous re-check (AST + real-import scan across every test
file) confirmed it: nothing referenced it. It is one of only three genuinely
untested modules in the tree.

WHAT IS ACTUALLY BEING PROVED: most of ``Router``'s rules are SHORT-CIRCUIT
PREVENTION rules, not routing-quality preferences. KiCad's connectivity
model means a wire that merely TOUCHES a foreign net's wire end, or runs
along it, silently joins two nets — a defect no DRC on the board side can
undo, because it happened in the schematic. Each of those rules is
fault-injected here: first a clean case that routes, then the same case with
the hazard present, which must NOT route.

``Router`` needs no KiCad file: it works on a grid of cells.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCH_SCRIPTS, TOOLS  # noqa: E402,F401

from common.sexpr import GRID  # noqa: E402
from sch_route import CROSS_COST, SHARE_BONUS, Router, mst_order  # noqa: E402


def _router(width_mm: float = 50.0, height_mm: float = 50.0) -> Router:
    """A small empty sheet; keeps the A* search fast and the assertions
    readable in cell coordinates."""
    return Router(origin=(0.0, 0.0), size=(width_mm, height_mm))


def _pt(router: Router, cell: tuple[int, int]) -> tuple[float, float]:
    return router.point(cell)


class CoordinateMapping(unittest.TestCase):
    def test_cell_and_point_round_trip(self):
        router = _router()
        for cell in ((0, 0), (3, 7), (10, 2)):
            with self.subTest(cell=cell):
                self.assertEqual(cell, router.cell(router.point(cell)))

    def test_the_grid_is_kicads_connectivity_grid(self):
        """Off-grid wire ends do not connect in KiCad; the router must work
        on the same grid sch_wire writes on."""
        self.assertEqual(GRID, _router().grid)

    def test_the_sheet_extent_is_derived_from_the_size(self):
        router = _router(width_mm=10 * GRID, height_mm=4 * GRID)
        self.assertEqual(11, router.nx)
        self.assertEqual(5, router.ny)


class BasicRouting(unittest.TestCase):
    def test_a_clear_sheet_routes_a_straight_line(self):
        router = _router()
        path = router.route(_pt(router, (0, 0)), _pt(router, (6, 0)), "N1")
        self.assertIsNotNone(path)
        # A straight run collapses to its two endpoints: no invented corners.
        self.assertEqual(2, len(path))
        self.assertEqual(_pt(router, (0, 0)), path[0])
        self.assertEqual(_pt(router, (6, 0)), path[-1])

    def test_identical_start_and_goal_is_none_not_an_empty_wire(self):
        """A zero-length wire is not a connection; returning [] would let a
        caller write a degenerate segment."""
        router = _router()
        self.assertIsNone(router.route(_pt(router, (2, 2)),
                                       _pt(router, (2, 2)), "N1"))

    def test_a_wall_is_routed_around(self):
        router = _router()
        for j in range(-1, 4):          # vertical wall at x=3, open above
            router.blocked.add((3, j))
        path = router.route(_pt(router, (0, 0)), _pt(router, (6, 0)), "N1")
        self.assertIsNotNone(path)
        self.assertGreater(len(path), 2, "the wall was not detoured around")

    def test_fault_injection_a_fully_enclosed_start_cannot_route(self):
        router = _router()
        for cell in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            router.blocked.add(cell)
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (6, 0)), "N1"),
                          "a path was found out of a sealed cell")

    def test_a_goal_outside_the_sheet_cannot_route(self):
        router = _router(width_mm=5 * GRID, height_mm=5 * GRID)
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (99, 99)), "N1"))

    def test_corners_are_collapsed_to_turn_points_only(self):
        """_unwind must drop collinear cells; a point per grid step would
        make sch_wire emit dozens of one-step wires."""
        router = _router()
        for j in range(-1, 4):
            router.blocked.add((3, j))
        path = router.route(_pt(router, (0, 0)), _pt(router, (6, 0)), "N1")
        for a, b in zip(path, path[1:]):
            self.assertTrue(a[0] == b[0] or a[1] == b[1],
                            f"non-orthogonal segment {a}->{b}")


class ShortCircuitPrevention(unittest.TestCase):
    """The rules that exist so two nets never silently become one."""

    def test_a_clean_route_exists_before_each_hazard_is_injected(self):
        """Baseline: without any foreign wire, this route succeeds. Every
        fault-injection below reuses these same endpoints, so a later None
        is caused by the hazard and not by an impossible geometry."""
        router = _router()
        self.assertIsNotNone(router.route(_pt(router, (0, 0)),
                                          _pt(router, (4, 0)), "N1"))

    def test_fault_injection_a_foreign_wire_end_may_not_be_touched(self):
        """Ending on (or crossing) a foreign net's wire END is a KiCad
        connection — the two nets short."""
        router = _router()
        # A foreign net's wire ends on every cell of the only corridor.
        for i in range(0, 5):
            router.endpoints.setdefault((i, 0), set()).add("FOREIGN")
            router.blocked.add((i, 1))
            router.blocked.add((i, -1))
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (4, 0)), "N1"),
                          "a route was drawn through a foreign wire END")

    def test_fault_injection_running_along_a_foreign_wire_is_forbidden(self):
        """Parallel/overlapping flow on the same axis is a short, even
        though a perpendicular crossing would be legal."""
        router = _router()
        for i in range(0, 5):
            # foreign net occupies the corridor HORIZONTALLY (axis 0)
            router.occupied.setdefault((i, 0), {}).setdefault("FOREIGN", set()).add(0)
            router.blocked.add((i, 1))
            router.blocked.add((i, -1))
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (4, 0)), "N1"),
                          "a route ran along a foreign net's wire")

    def test_a_perpendicular_crossing_of_a_foreign_wire_is_allowed(self):
        """Not a short in KiCad — allowed, but priced at CROSS_COST so the
        router prefers to avoid it when it can."""
        router = _router()
        # foreign net runs VERTICALLY (axis 1) across the row we travel on
        router.occupied.setdefault((2, 0), {}).setdefault("FOREIGN", set()).add(1)
        path = router.route(_pt(router, (0, 0)), _pt(router, (4, 0)), "N1")
        self.assertIsNotNone(path, "a legal perpendicular crossing was refused")

    def test_a_route_may_not_END_on_a_foreign_wire_even_crossing_it(self):
        """Landing ON a foreign wire makes KiCad treat it as a T-junction:
        a short, even though merely crossing that same cell is fine."""
        router = _router()
        router.occupied.setdefault((4, 0), {}).setdefault("FOREIGN", set()).add(1)
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (4, 0)), "N1"),
                          "a route ended on top of a foreign net's wire")

    def test_fault_injection_a_foreign_pin_anchor_may_not_be_stepped_on(self):
        router = _router()
        router.pin_owner = {(i, 0): "FOREIGN" for i in range(1, 5)}
        for i in range(0, 5):
            router.blocked.add((i, 1))
            router.blocked.add((i, -1))
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (4, 0)), "N1"),
                          "a route stepped on another net's pin anchor")

    def test_its_own_pin_anchor_is_walkable(self):
        router = _router()
        router.pin_owner = {(i, 0): "N1" for i in range(0, 5)}
        self.assertIsNotNone(router.route(_pt(router, (0, 0)),
                                          _pt(router, (4, 0)), "N1"),
                             "a route refused to use its OWN pin anchor")


class SameNetSharing(unittest.TestCase):
    def test_sharing_the_same_nets_wire_is_cheaper_than_empty_space(self):
        """SHARE_BONUS is what makes branches of one net meet in a T rather
        than run side by side."""
        router = _router()
        router.occupied.setdefault((1, 0), {}).setdefault("N1", set()).add(0)
        shared = router._passable((1, 0), "N1", 0)
        empty = router._passable((1, 5), "N1", 0)
        self.assertIsNotNone(shared)
        self.assertLess(shared, empty)
        self.assertAlmostEqual(empty - SHARE_BONUS, shared, places=6)

    def test_crossing_a_foreign_wire_is_more_expensive_than_empty_space(self):
        router = _router()
        router.occupied.setdefault((1, 0), {}).setdefault("FOREIGN", set()).add(1)
        crossing = router._passable((1, 0), "N1", 0)
        empty = router._passable((1, 5), "N1", 0)
        self.assertIsNotNone(crossing)
        self.assertGreater(crossing, empty)
        self.assertAlmostEqual(empty + CROSS_COST, crossing, places=6)


class Commit(unittest.TestCase):
    def test_commit_marks_every_cell_along_the_run_not_just_the_ends(self):
        """A half-recorded wire would let the next net route straight
        through the gap and short."""
        router = _router()
        router.commit([_pt(router, (0, 0)), _pt(router, (4, 0))], "N1")
        for i in range(0, 5):
            with self.subTest(cell=(i, 0)):
                self.assertIn("N1", router.occupied.get((i, 0), {}))

    def test_commit_records_both_endpoints(self):
        router = _router()
        router.commit([_pt(router, (0, 0)), _pt(router, (4, 0))], "N1")
        self.assertIn("N1", router.endpoints.get((0, 0), set()))
        self.assertIn("N1", router.endpoints.get((4, 0), set()))

    def test_a_committed_wire_blocks_a_later_foreign_net(self):
        """The end-to-end safety property: route N1, commit it, and N2 must
        not be able to run along it."""
        router = _router()
        path = router.route(_pt(router, (0, 0)), _pt(router, (4, 0)), "N1")
        router.commit(path, "N1")
        for i in range(0, 5):
            router.blocked.add((i, 1))
            router.blocked.add((i, -1))
        self.assertIsNone(router.route(_pt(router, (0, 0)),
                                       _pt(router, (4, 0)), "N2"),
                          "a second net ran along the first net's committed wire")


class MstOrder(unittest.TestCase):
    def test_fewer_than_two_points_has_no_edges(self):
        self.assertEqual([], mst_order([]))
        self.assertEqual([], mst_order([(0.0, 0.0)]))

    def test_n_points_yield_n_minus_one_edges(self):
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        self.assertEqual(len(points) - 1, len(mst_order(points)))

    def test_every_point_is_connected(self):
        points = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (20.0, 20.0)]
        reached = {0}
        for i, j in mst_order(points):
            self.assertIn(i, reached, "an edge started from an unreached point")
            reached.add(j)
        self.assertEqual(set(range(len(points))), reached)

    def test_the_nearest_point_is_attached_first(self):
        """Manhattan-nearest first — a far point must not win over a near one."""
        points = [(0.0, 0.0), (1.0, 0.0), (100.0, 0.0)]
        self.assertEqual((0, 1), mst_order(points)[0])


if __name__ == "__main__":
    unittest.main()
