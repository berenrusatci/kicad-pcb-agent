#!/usr/bin/env python3
"""Tests for the last-resort grid A* router.

Ported from the source pytest suite (``test_otonom_python_router.py``) to
unittest, including the two-phase (broad + narrow phase) collision
regression tests, which are the newest and most important behaviour in the
file. The source module had no CLI report at all: its ``__main__`` ran the
self-tests and printed prose. The CLI contract below is new and is pinned
here.

``grid_astar_search`` touches no CAD library, so everything is exercised for
real without mocks.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

from scripts.route import grid_astar as ga  # noqa: E402

SCRIPT = SCRIPTS / "route" / "grid_astar.py"


@dataclass
class PlainBox:
    """A caller-supplied obstacle that is NOT one of this module's dataclasses
    — proves the BoxLike protocol really is duck-typed."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def write_problem(directory: str, problem: dict) -> Path:
    path = Path(directory) / "problem.json"
    path.write_text(json.dumps(problem, ensure_ascii=False), encoding="utf-8")
    return path


class GridAStarSearch(unittest.TestCase):
    def test_obstacle_free_straight_path_is_found(self):
        outcome = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), cell_mm=0.5)
        self.assertTrue(outcome.found)
        self.assertAlmostEqual(5.0, ga.path_length_mm(outcome.path), delta=0.6)

    def test_search_goes_around_a_wall(self):
        wall = PlainBox(2.0, -0.3, 3.0, 5.0)
        outcome = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), [wall],
                                       cell_mm=0.25, clearance_mm=0.2)
        self.assertTrue(outcome.found)
        self.assertTrue(
            any(y > 4.5 for _, y in outcome.path)
            or any(y < -0.5 for _, y in outcome.path)
            or len(outcome.path) > 2)

    def test_blocked_start_is_a_clean_fail(self):
        closed = PlainBox(-1.0, -1.0, 1.0, 1.0)
        outcome = ga.grid_astar_search((0.0, 0.0), (10.0, 0.0), [closed],
                                       cell_mm=0.5, clearance_mm=0.1)
        self.assertFalse(outcome.found)
        self.assertIn("blocked region", outcome.reason)

    def test_node_budget_stops_an_unbounded_search(self):
        outcome = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0),
                                       cell_mm=0.01, max_nodes=50)
        self.assertFalse(outcome.found)
        self.assertIn("budget", outcome.reason)

    def test_same_cell_shortcut(self):
        outcome = ga.grid_astar_search((0.0, 0.0), (0.02, 0.02), cell_mm=0.5)
        self.assertTrue(outcome.found)
        self.assertEqual(0, outcome.nodes_visited)

    def test_a_full_wall_leaves_no_path(self):
        full_wall = PlainBox(2.0, -1000.0, 3.0, 1000.0)
        outcome = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), [full_wall],
                                       cell_mm=0.5, clearance_mm=0.1)
        self.assertFalse(outcome.found)

    def test_fault_injection_obstacle_on_the_start_point(self):
        obstacle = ga.BoxObstacle(-0.5, -0.5, 0.5, 0.5)
        outcome = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), [obstacle],
                                       cell_mm=0.5, clearance_mm=0.1)
        self.assertFalse(outcome.found)

    def test_invalid_search_parameters_are_rejected(self):
        for kwargs in ({"cell_mm": 0.0}, {"clearance_mm": -1.0},
                       {"max_nodes": 0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    ga.grid_astar_search((0.0, 0.0), (1.0, 0.0), **kwargs)


class SimplifyPath(unittest.TestCase):
    def test_collinear_points_are_merged(self):
        path = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (2.0, 1.0), (2.0, 2.0)]
        self.assertEqual([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0)],
                         ga.simplify_path(path))

    def test_a_short_path_is_unchanged(self):
        self.assertEqual([(0.0, 0.0), (1.0, 1.0)],
                         ga.simplify_path([(0.0, 0.0), (1.0, 1.0)]))


class TwoPhaseCollisionTest(unittest.TestCase):
    """REGRESSION: the AABB of a 45-degree segment obstacle is far bigger
    than the real line, and the narrow phase must close that gap with a true
    point-to-segment distance."""

    def test_empty_corner_of_a_diagonal_aabb_is_no_longer_blocked(self):
        """Line (0,10)->(20,0), i.e. x+2y=20. AABB X:[0,20] Y:[0,10]. The
        point (0.5,0.5) near the (0,0) corner is |0.5+1-20|/sqrt(5)=8.27 mm
        from the line: INSIDE the AABB but nowhere near the copper. The old
        AABB-only model called that blocked."""
        diagonal = ga.SegmentObstacle(0.0, 10.0, 20.0, 0.0,
                                      width_mm=0.29, ref="trk_TEST")
        self.assertFalse(ga.cell_blocked((2, 2), [diagonal],
                                         cell_mm=0.25, clearance_mm=0.15))

    def test_a_cell_on_the_diagonal_is_still_blocked(self):
        """The narrow phase only cancels false positives; a cell truly ON the
        line must stay blocked. This is a correction, not a relaxation."""
        diagonal = ga.SegmentObstacle(0.0, 0.0, 20.0, 20.0,
                                      width_mm=0.29, ref="trk_TEST")
        self.assertTrue(ga.cell_blocked((40, 40), [diagonal],
                                        cell_mm=0.25, clearance_mm=0.15))

    def test_box_obstacles_never_enter_the_narrow_phase(self):
        """Box obstacles have no x1/width_mm, so the pure-AABB behaviour must
        be preserved bit for bit."""
        closed = PlainBox(-1.0, -1.0, 1.0, 1.0)
        outcome = ga.grid_astar_search((0.0, 0.0), (10.0, 0.0), [closed],
                                       cell_mm=0.5, clearance_mm=0.1)
        self.assertFalse(outcome.found)

    def test_a_via_is_a_zero_length_segment(self):
        via = ga.SegmentObstacle(5.0, 5.0, 5.0, 5.0, width_mm=0.6, ref="via1")
        self.assertTrue(ga.cell_blocked((20, 20), [via],
                                        cell_mm=0.25, clearance_mm=0.1))
        self.assertFalse(ga.cell_blocked((40, 40), [via],
                                         cell_mm=0.25, clearance_mm=0.1))

    def test_segment_obstacle_aabb_includes_half_the_width(self):
        segment = ga.SegmentObstacle(0.0, 0.0, 10.0, 0.0, width_mm=0.4)
        self.assertAlmostEqual(-0.2, segment.x_min)
        self.assertAlmostEqual(10.2, segment.x_max)
        self.assertAlmostEqual(-0.2, segment.y_min)
        self.assertAlmostEqual(0.2, segment.y_max)


class ExtraCostFn(unittest.TestCase):
    def test_a_positive_extra_cost_makes_the_router_prefer_a_longer_path(self):
        def avoid_middle(a, b):
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            return 100.0 if abs(mid[1]) < 0.1 else 0.0

        cheap = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), cell_mm=0.5)
        penalised = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), cell_mm=0.5,
                                         extra_cost_fn=avoid_middle)
        self.assertTrue(cheap.found)
        self.assertTrue(penalised.found)
        self.assertTrue(any(abs(y) > 0.1 for _, y in penalised.path))

    def test_no_extra_cost_fn_behaves_exactly_as_before(self):
        outcome = ga.grid_astar_search((0.0, 0.0), (5.0, 0.0), cell_mm=0.5,
                                       extra_cost_fn=None)
        self.assertTrue(outcome.found)


class ViaPlacementValidity(unittest.TestCase):
    def test_insufficient_annular_ring_is_rejected(self):
        # via 0.3mm, hole 0.29mm -> ring 0.005mm, far under the 0.15mm default.
        ok, why = ga.via_placement_valid((0.0, 0.0), 0.3, 0.29)
        self.assertFalse(ok)
        self.assertIn("annular ring", why)

    def test_sufficient_annular_ring_with_no_neighbours_passes(self):
        # 0.65mm via / 0.3mm hole -> 0.175mm ring, above the 0.15mm default.
        ok, why = ga.via_placement_valid((0.0, 0.0), 0.65, 0.3)
        self.assertTrue(ok, why)

    def test_fault_injection_the_reported_incident_scenario(self):
        """REGRESSION: a 0.5mm via at a 0.4mm pin pitch produced 221 real DRC
        violations because nothing measured its distance to neighbouring pad
        holes. A neighbour 0.4mm away with a 0.3mm hole must now be rejected
        even though the via's OWN annular ring is fine."""
        ok, why = ga.via_placement_valid(
            (0.0, 0.0), 0.65, 0.3, neighbour_holes=[(0.4, 0.0, 0.3)])
        self.assertFalse(ok)
        self.assertIn("hole-to-hole", why)

    def test_a_neighbour_far_enough_away_passes(self):
        ok, why = ga.via_placement_valid(
            (0.0, 0.0), 0.65, 0.3, neighbour_holes=[(5.0, 0.0, 0.3)])
        self.assertTrue(ok, why)

    def test_annular_ring_math(self):
        check = ga.ViaPlacementCheck(0.5, 0.3)
        self.assertAlmostEqual(0.1, check.annular_ring_mm)
        self.assertFalse(check.annular_ring_sufficient)


class SiCostFunction(unittest.TestCase):
    def test_empty_other_traces_is_always_free(self):
        cost_fn = ga.si_cost_function([], net_width_mm=0.2)
        self.assertEqual(0.0, cost_fn((0.0, 0.0), (1.0, 0.0)))

    def test_a_step_far_from_other_traces_is_free(self):
        trace = ga.SegmentObstacle(0.0, 10.0, 10.0, 10.0, width_mm=0.2)
        cost_fn = ga.si_cost_function([trace], net_width_mm=0.2)
        self.assertEqual(0.0, cost_fn((0.0, 0.0), (1.0, 0.0)))

    def test_fault_injection_a_step_inside_the_3w_threshold_is_penalised(self):
        # Threshold = 3 * 0.2 = 0.6mm; a step 0.1mm from another HS trace
        # must cost more than zero.
        trace = ga.SegmentObstacle(0.0, 0.1, 10.0, 0.1, width_mm=0.01)
        cost_fn = ga.si_cost_function([trace], net_width_mm=0.2)
        cost = cost_fn((0.0, 0.0), (1.0, 0.0))
        self.assertGreater(cost, 0.0)

    def test_penalty_grows_with_the_shortfall(self):
        close = ga.SegmentObstacle(0.0, 0.05, 10.0, 0.05, width_mm=0.01)
        far = ga.SegmentObstacle(0.0, 0.4, 10.0, 0.4, width_mm=0.01)
        cost_fn = ga.si_cost_function([close, far], net_width_mm=0.2)
        closer_cost = cost_fn((0.0, 0.0), (1.0, 0.0))
        cost_fn2 = ga.si_cost_function([far], net_width_mm=0.2)
        farther_cost = cost_fn2((0.0, 0.0), (1.0, 0.0))
        self.assertGreater(closer_cost, farther_cost)


class ViaImpedanceDiscontinuityCost(unittest.TestCase):
    def test_zero_deviation_leaves_the_cost_unchanged(self):
        self.assertEqual(5.0, ga.via_impedance_discontinuity_cost(5.0, 0.0))

    def test_positive_deviation_scales_the_cost_up(self):
        self.assertAlmostEqual(5.5, ga.via_impedance_discontinuity_cost(5.0, 10.0))


class LayeredAStar(unittest.TestCase):
    def test_a_clean_single_layer_route_needs_no_vias(self):
        outcome = ga.layered_astar_search((0.0, 0.0), (5.0, 0.0),
                                          layer_count=2, cell_mm=0.5)
        self.assertTrue(outcome.found)
        self.assertEqual([], outcome.via_positions)
        self.assertTrue(all(p[2] == 0 for p in outcome.path))

    def test_an_obstacle_on_the_only_open_layer_forces_a_via(self):
        # via/hole geometry chosen to actually satisfy the default 0.15mm
        # annular-ring minimum (see the PARAMETER NOTE on via_placement_valid
        # - the 0.5/0.3 defaults alone do NOT).
        wall = ga.BoxObstacle(2.0, -1000.0, 3.0, 1000.0)
        outcome = ga.layered_astar_search(
            (0.0, 0.0), (5.0, 0.0), layer_count=2,
            layer_obstacles={0: [wall]}, cell_mm=0.5, clearance_mm=0.1,
            via_diameter_mm=0.65, via_hole_diameter_mm=0.3)
        self.assertTrue(outcome.found)
        self.assertGreater(len(outcome.via_positions), 0)
        self.assertTrue(any(p[2] == 1 for p in outcome.path))

    def test_fault_injection_a_via_violating_placement_rules_is_never_used(self):
        """The candidate via point sits 0.3mm from a neighbour hole, which
        fails hole-to-hole clearance at the default 0.5mm via diameter. With
        no other layer-change point available in a fully blocked layer, the
        search must report NOT FOUND rather than silently placing a bad via."""
        wall = ga.BoxObstacle(-1000.0, -1000.0, 1000.0, 1000.0)
        outcome = ga.layered_astar_search(
            (0.0, 0.0), (5.0, 0.0), layer_count=2,
            layer_obstacles={0: [wall]}, cell_mm=0.5, clearance_mm=0.0,
            neighbour_holes=[(0.0, 0.0, 0.3), (5.0, 0.0, 0.3)])
        # every point on layer 0 is blocked, so no via candidate is ever
        # reachable in the first place -- this proves the search does not
        # silently succeed through the fully-blocked layer either way.
        self.assertFalse(outcome.found)

    def test_invalid_layer_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            ga.layered_astar_search((0.0, 0.0), (1.0, 0.0), layer_count=0)
        with self.assertRaises(ValueError):
            ga.layered_astar_search((0.0, 0.0), (1.0, 0.0), layer_count=2,
                                    start_layer=5)

    def test_same_node_shortcut(self):
        outcome = ga.layered_astar_search((0.0, 0.0), (0.02, 0.02),
                                          layer_count=2, cell_mm=0.5)
        self.assertTrue(outcome.found)
        self.assertEqual(0, outcome.nodes_visited)


class CoupledAStar(unittest.TestCase):
    def test_a_clean_pair_routes_with_a_constant_gap(self):
        outcome = ga.coupled_astar_search(
            (0.0, 0.1), (0.0, -0.1), (5.0, 0.1), (5.0, -0.1),
            width_mm=0.15, gap_mm=0.2, cell_mm=0.5, stub_length_mm=0.0)
        self.assertTrue(outcome.found)
        self.assertGreaterEqual(len(outcome.p_path), 2)
        self.assertGreaterEqual(len(outcome.n_path), 2)

    def test_an_obstacle_blocks_the_whole_corridor(self):
        wall = ga.BoxObstacle(2.0, -1000.0, 3.0, 1000.0)
        outcome = ga.coupled_astar_search(
            (0.0, 0.1), (0.0, -0.1), (5.0, 0.1), (5.0, -0.1),
            width_mm=0.15, gap_mm=0.2, obstacles=[wall],
            cell_mm=0.5, clearance_mm=0.1, stub_length_mm=0.0)
        self.assertFalse(outcome.found)
        self.assertEqual([], outcome.p_path)
        self.assertEqual([], outcome.n_path)

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            ga.coupled_astar_search((0, 0), (0, 0), (1, 0), (1, 0),
                                    width_mm=0.0, gap_mm=0.2)
        with self.assertRaises(ValueError):
            ga.coupled_astar_search((0, 0), (0, 0), (1, 0), (1, 0),
                                    width_mm=0.15, gap_mm=0.0)
        with self.assertRaises(ValueError):
            ga.coupled_astar_search((0, 0), (0, 0), (1, 0), (1, 0),
                                    width_mm=0.15, gap_mm=0.2,
                                    stub_length_mm=-1.0)


class PairFromCenterline(unittest.TestCase):
    def test_straight_line_offsets_are_parallel_and_equidistant(self):
        centerline = [(0.0, 0.0), (10.0, 0.0)]
        p, n = ga._pair_from_centerline(centerline, radius_mm=0.5)
        self.assertAlmostEqual(0.5, p[0][1])
        self.assertAlmostEqual(-0.5, n[0][1])
        self.assertAlmostEqual(0.5, p[1][1])
        self.assertAlmostEqual(-0.5, n[1][1])

    def test_short_centerline_is_returned_unchanged(self):
        single = [(1.0, 1.0)]
        p, n = ga._pair_from_centerline(single, radius_mm=0.5)
        self.assertEqual(single, p)
        self.assertEqual(single, n)

    def test_perpendicular_unit_vector_has_unit_length(self):
        nx, ny = ga._perpendicular_unit_vector((0.0, 0.0), (3.0, 4.0))
        self.assertAlmostEqual(1.0, math.hypot(nx, ny))

    def test_degenerate_segment_returns_zero_vector(self):
        self.assertEqual((0.0, 0.0),
                         ga._perpendicular_unit_vector((1.0, 1.0), (1.0, 1.0)))


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        self.assertEqual([], ga.run_self_tests())

    def test_fault_injection_proof(self):
        self.assertTrue(ga.prove_tests_not_empty())

    def test_self_test_flag_exits_zero_without_a_problem_file(self):
        self.assertEqual(0, run("--self-test").returncode)


class CliContract(unittest.TestCase):
    ROUTABLE = {
        "cell_mm": 0.5,
        "clearance_mm": 0.1,
        "nets": [{"net": "SIG", "start": [0.0, 0.0], "end": [5.0, 0.0]}],
    }

    def test_missing_positional_is_exit_2(self):
        proc = run()
        self.assertEqual(2, proc.returncode)
        self.assertEqual("", proc.stdout.strip())

    def test_routable_problem_exits_zero_with_json_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, self.ROUTABLE)))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual("route-result/v1", report["schema_version"])
            self.assertTrue(report["routes"][0]["found"])
            self.assertGreater(report["routes"][0]["length_mm"], 0.0)

    def test_unroutable_net_exits_one(self):
        problem = dict(self.ROUTABLE)
        problem["obstacles"] = [
            {"type": "box", "x_min": 2.0, "y_min": -1000.0,
             "x_max": 3.0, "y_max": 1000.0}]
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, problem)))
            self.assertEqual(1, proc.returncode)
            report = json.loads(proc.stdout)
            self.assertFalse(report["routes"][0]["found"])
            self.assertEqual("FAIL", report["checks"][0]["status"])

    def test_no_nets_is_no_coverage_and_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, {"nets": []})))
            self.assertEqual(1, proc.returncode)
            self.assertEqual("NO_COVERAGE",
                             json.loads(proc.stdout)["checks"][0]["status"])

    def test_segment_obstacles_are_accepted_from_json(self):
        problem = dict(self.ROUTABLE)
        problem["obstacles"] = [
            {"type": "segment", "ref": "trk1", "x1": 0.0, "y1": 10.0,
             "x2": 20.0, "y2": 0.0, "width_mm": 0.29}]
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, problem)))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(1, json.loads(proc.stdout)["obstacle_count"])

    def test_unknown_obstacle_type_is_exit_2(self):
        problem = dict(self.ROUTABLE)
        problem["obstacles"] = [{"type": "blob"}]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, run(str(write_problem(tmp, problem))).returncode)

    def test_invalid_json_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            self.assertEqual(2, run(str(bad)).returncode)

    def test_flags_override_the_problem_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_problem(tmp, self.ROUTABLE)
            report = json.loads(run(str(path), "--cell-mm", "0.25").stdout)
            self.assertEqual(0.25, report["params"]["cell_mm"])

    def test_json_flag_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "route.json"
            proc = run(str(write_problem(tmp, self.ROUTABLE)),
                       "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_stdout_is_pure_json_and_diagnostics_go_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, self.ROUTABLE)))
            json.loads(proc.stdout)
            self.assertIn("routes=", proc.stderr)

    def test_self_test_does_not_run_during_a_normal_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, self.ROUTABLE)))
            self.assertNotIn("SELF-TEST", proc.stdout)
            self.assertNotIn("SELF-TEST", proc.stderr)


class SchemaFile(unittest.TestCase):
    def test_schema_exists_and_pins_the_version(self):
        schema = json.loads(
            (SCHEMAS / "route_result.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(ga.SCHEMA_VERSION,
                         schema["properties"]["schema_version"]["const"])


if __name__ == "__main__":
    unittest.main()
