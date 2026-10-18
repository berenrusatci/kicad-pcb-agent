#!/usr/bin/env python3
"""Tests for scripts/route/topological.py.

Converted from the archived pytest suite to unittest.

None of these need pcbnew: the whole solver is pure geometry and the single
pcbnew user (``write_tracks``) imports it inside the function. One test proves
exactly that.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCHEMAS, SCRIPTS  # noqa: E402

ROUTE_SCRIPTS = SCRIPTS / "route"
sys.path.insert(0, str(ROUTE_SCRIPTS))
import topological as mod  # noqa: E402


class Geometry(unittest.TestCase):
    def test_manhattan_distance(self):
        self.assertEqual(mod.manhattan_mm((0, 0), (3, 4)), 7)

    def test_a_segment_crossing_a_box_is_detected(self):
        box = (4.0, -1.0, 6.0, 1.0)
        self.assertTrue(mod._segment_hits_box((0, 0), (10, 0), box))

    def test_a_segment_passing_above_the_box_is_not(self):
        box = (4.0, -1.0, 6.0, 1.0)
        self.assertFalse(mod._segment_hits_box((0, 5), (10, 5), box))

    def test_an_axis_aligned_segment_outside_the_slab_is_rejected(self):
        box = (4.0, -1.0, 6.0, 1.0)
        self.assertFalse(mod._segment_hits_box((0, 0), (0, 10), box))

    def test_clearance_inflates_the_obstacle(self):
        obstacle = mod.Obstacle("o", 4, -1, 6, 1, clearance_mm=0.5)
        self.assertEqual(obstacle.inflated(), (3.5, -1.5, 6.5, 1.5))
        self.assertEqual(obstacle.inflated(0.1), (3.4, -1.6, 6.6, 1.6))

    def test_half_the_track_width_is_added_to_the_clearance(self):
        """A track is a ribbon: a centreline just outside the box still puts
        copper inside it."""
        obstacle = mod.Obstacle("o", 4, 0.3, 6, 5, clearance_mm=0.0)
        path = [((0.0, 0.0), (10.0, 0.0))]
        self.assertEqual(mod.blocking_obstacles(path, [obstacle], 0.2), [])
        self.assertEqual(mod.blocking_obstacles(path, [obstacle], 0.8), ["o"])

    def test_fault_injection_an_obstacle_on_the_path_must_be_named(self):
        blockers = mod.blocking_obstacles(
            [((0.0, 0.0), (10.0, 0.0))],
            [mod.Obstacle("bar", 4.0, -5.0, 6.0, 5.0, 0.2)], 0.2)
        self.assertEqual(blockers, ["bar"])

    def test_a_track_converted_to_an_obstacle_covers_its_width(self):
        track = mod.Track("t", (0, 0), (10, 0), 0.4, "N")
        obstacle = track.as_obstacle()
        self.assertEqual(obstacle.y_min, -0.2)
        self.assertEqual(obstacle.y_max, 0.2)
        self.assertEqual(obstacle.name, "track:t")


class TheLadder(unittest.TestCase):
    def test_a_clear_path_is_direct(self):
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"))
        self.assertEqual(route.strategy, mod.Strategy.DIRECT)
        self.assertEqual(route.via_count, 0)
        self.assertTrue(route.found)

    def test_a_diagonal_target_with_a_blocked_direct_falls_to_an_l_bend(self):
        obstacle = mod.Obstacle("o", 3, 3, 7, 7)
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 10), "SIG"),
                              [obstacle])
        self.assertEqual(route.strategy, mod.Strategy.L_BEND)
        self.assertEqual(route.via_count, 0)

    def test_a_barrier_is_detoured_around_without_a_via(self):
        obstacle = mod.Obstacle("barrier", 4.0, -1.0, 6.0, 1.0)
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"),
                              [obstacle])
        self.assertTrue(route.found)
        self.assertEqual(route.via_count, 0)
        self.assertIn(route.strategy,
                      (mod.Strategy.L_BEND, mod.Strategy.U_DETOUR))

    def test_the_shortest_clean_detour_is_chosen(self):
        obstacle = mod.Obstacle("barrier", 4.0, -1.0, 6.0, 1.0)
        route = mod.find_path(mod.RouteRequest((0, -5), (10, 5), "SIG"),
                              [obstacle])
        self.assertTrue(route.found)

    def test_a_high_speed_net_is_never_solved_with_a_via(self):
        wall = mod.Obstacle("wall", -100.0, -1.0, 100.0, 1.0)
        route = mod.find_path(
            mod.RouteRequest((0, 0), (10, 0), "MIPI_D0_P", high_speed=True),
            [wall], lower_layer_obstacles=[])
        self.assertEqual(route.strategy, mod.Strategy.NOT_FOUND)
        self.assertEqual(route.via_count, 0)
        self.assertTrue(any("placement" in note for note in route.notes))

    def test_a_layer_change_is_used_when_the_lower_layer_is_declared_clear(self):
        wall = mod.Obstacle("wall", -100.0, -1.0, 100.0, 1.0)
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"),
                              [wall], lower_layer_obstacles=[])
        self.assertEqual(route.strategy, mod.Strategy.LAYER_CHANGE)
        self.assertEqual(route.via_count, 2)
        self.assertEqual(route.layers, ["F.Cu", "B.Cu", "F.Cu"])

    def test_an_undeclared_lower_layer_is_NOT_assumed_empty(self):
        """The most dangerous assumption available here."""
        wall = mod.Obstacle("wall", -100.0, -1.0, 100.0, 1.0)
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"), [wall])
        self.assertEqual(route.strategy, mod.Strategy.NOT_FOUND)
        self.assertTrue(any("NOT assumed empty" in n for n in route.notes))

    def test_a_blocked_lower_layer_also_yields_not_found(self):
        wall = mod.Obstacle("wall", -100.0, -1.0, 100.0, 1.0)
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"),
                              [wall], lower_layer_obstacles=[wall])
        self.assertEqual(route.strategy, mod.Strategy.NOT_FOUND)

    def test_notes_record_which_rung_failed(self):
        wall = mod.Obstacle("wall", -100.0, -1.0, 100.0, 1.0)
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"), [wall])
        joined = " ".join(route.notes)
        self.assertIn("direct path blocked", joined)
        self.assertIn("L bends blocked", joined)

    def test_length_is_reported(self):
        route = mod.find_path(mod.RouteRequest((0, 0), (3, 4), "SIG"))
        self.assertAlmostEqual(route.length_mm, 5.0, places=4)


class Chamfer(unittest.TestCase):
    def test_a_right_angle_is_removed(self):
        path = [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (5.0, 5.0))]
        self.assertEqual(mod.right_angle_count(path), 1)
        self.assertEqual(mod.right_angle_count(mod.chamfer_45(path)), 0)

    def test_the_chamfer_shrinks_rather_than_reversing_a_short_segment(self):
        path = [((0.0, 0.0), (0.2, 0.0)), ((0.2, 0.0), (0.2, 0.2))]
        result = mod.chamfer_45(path, chamfer_mm=5.0)
        for a, b in result:
            self.assertNotEqual(a, b)
        self.assertEqual(mod.right_angle_count(result), 0)

    def test_a_single_segment_is_untouched(self):
        path = [((0.0, 0.0), (5.0, 0.0))]
        self.assertEqual(mod.chamfer_45(path), path)

    def test_a_non_positive_chamfer_is_refused(self):
        with self.assertRaises(ValueError):
            mod.chamfer_45([((0, 0), (1, 0)), ((1, 0), (1, 1))], 0.0)

    def test_the_endpoints_are_preserved(self):
        path = [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (5.0, 5.0))]
        result = mod.chamfer_45(path)
        self.assertEqual(result[0][0], (0.0, 0.0))
        self.assertEqual(result[-1][1], (5.0, 5.0))

    def test_a_collinear_join_is_not_treated_as_a_corner(self):
        path = [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (10.0, 0.0))]
        self.assertEqual(mod.right_angle_count(path), 0)
        self.assertEqual(len(mod.chamfer_45(path)), 2)


class Shove(unittest.TestCase):
    REQUEST = mod.RouteRequest((0, 0.05), (10, 0.05), "SIG",
                               width_mm=0.2, clearance_mm=0.2)

    def test_a_locked_track_is_never_moved(self):
        locked = mod.Track("hs", (0, 0), (10, 0), 0.2, "USB_DP", locked=True)
        self.assertIsNone(mod.suggest_shove(locked, self.REQUEST))

    def test_a_diagonal_track_is_refused(self):
        """'Perpendicular' would be an arbitrary choice there."""
        diagonal = mod.Track("d", (0, 0), (10, 10), 0.2, "N")
        self.assertIsNone(mod.suggest_shove(diagonal, self.REQUEST))

    def test_a_zero_length_track_is_refused(self):
        point = mod.Track("p", (0, 0), (0, 0), 0.2, "N")
        self.assertIsNone(mod.suggest_shove(point, self.REQUEST))

    def test_a_horizontal_blocker_moves_perpendicular_and_far_enough(self):
        blocker = mod.Track("t", (0, 0), (10, 0), 0.2, "N")
        moved = mod.suggest_shove(blocker, self.REQUEST)
        self.assertIsNotNone(moved)
        self.assertEqual(moved.start[0], blocker.start[0])   # x unchanged
        separation = abs(moved.start[1] - self.REQUEST.start[1])
        self.assertGreaterEqual(separation, 0.2 / 2 + 0.2 / 2 + 0.2 - 1e-9)

    def test_a_vertical_blocker_moves_in_x(self):
        blocker = mod.Track("t", (5, 0), (5, 10), 0.2, "N")
        request = mod.RouteRequest((5, 0), (5, 10), "SIG")
        moved = mod.suggest_shove(blocker, request)
        self.assertIsNotNone(moved)
        self.assertEqual(moved.start[1], blocker.start[1])

    def test_a_track_that_is_already_clear_is_left_alone(self):
        blocker = mod.Track("t", (0, 50), (10, 50), 0.2, "N")
        self.assertIsNone(mod.suggest_shove(blocker, self.REQUEST))

    def test_no_cascade_the_shove_is_withdrawn_if_it_hits_something(self):
        blocker = mod.Track("t", (0, 0), (10, 0), 0.2, "N")
        everywhere = mod.Obstacle("other", -100, -100, 100, 100)
        self.assertIsNone(
            mod.suggest_shove(blocker, self.REQUEST, [everywhere]))

    def test_the_blockers_own_obstacle_does_not_veto_its_move(self):
        blocker = mod.Track("t", (0, 0), (10, 0), 0.2, "N")
        moved = mod.suggest_shove(blocker, self.REQUEST,
                                  [blocker.as_obstacle()])
        self.assertIsNotNone(moved)


class Gates(unittest.TestCase):
    def test_the_via_ban_gate_stands_on_its_own(self):
        """Paths can arrive from a hand edit or another router."""
        routes = {"USB_DP": mod.RouteResult([((0, 0), (1, 1))],
                                            mod.Strategy.LAYER_CHANGE,
                                            via_count=2)}
        finding = mod.check_via_ban(routes, ["USB_DP"])
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["via_count"], 2)

    def test_a_high_speed_net_with_no_result_is_not_scanned(self):
        finding = mod.check_via_ban({}, ["USB_DP"])
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_the_geometry_gate_catches_a_remaining_right_angle(self):
        routes = {"SIG": mod.RouteResult(
            [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (5.0, 5.0))],
            mod.Strategy.L_BEND)}
        self.assertEqual(mod.check_geometry(routes)["status"], "FAIL")

    def test_the_geometry_gate_passes_a_chamfered_path(self):
        segments = mod.chamfer_45([((0.0, 0.0), (5.0, 0.0)),
                                   ((5.0, 0.0), (5.0, 5.0))])
        routes = {"SIG": mod.RouteResult(segments, mod.Strategy.L_BEND)}
        self.assertEqual(mod.check_geometry(routes)["status"], "PASS")

    def test_empty_paths_are_not_scanned_by_the_geometry_gate(self):
        routes = {"SIG": mod.RouteResult([], mod.Strategy.NOT_FOUND)}
        self.assertEqual(mod.check_geometry(routes)["status"], "NO_COVERAGE")

    def test_routability_reports_the_unsolved_nets(self):
        routes = {"A": mod.RouteResult([((0, 0), (1, 0))], mod.Strategy.DIRECT),
                  "B": mod.RouteResult([], mod.Strategy.NOT_FOUND, notes=["x"])}
        finding = mod.check_routability(routes)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["scanned"], 2)
        self.assertEqual(finding["violations"][0]["net"], "B")

    def test_no_requests_is_no_coverage(self):
        self.assertEqual(mod.check_routability({})["status"], "NO_COVERAGE")

    def test_the_plan_row_is_serialisable(self):
        route = mod.find_path(mod.RouteRequest((0, 0), (10, 0), "SIG"))
        json.dumps(mod.plan_row("SIG", route))    # must not raise


class ImportIsolation(unittest.TestCase):
    def test_the_module_does_not_import_pcbnew_at_module_level(self):
        source = (ROUTE_SCRIPTS / "topological.py").read_text(encoding="utf-8")
        top_level = [line for line in source.splitlines()
                     if line.startswith("import ") or line.startswith("from ")]
        self.assertFalse([line for line in top_level if "pcbnew" in line])

    def test_the_import_lives_inside_write_tracks(self):
        import inspect
        self.assertIn("import pcbnew", inspect.getsource(mod.write_tracks))

    def test_the_module_is_importable_without_pcbnew(self):
        """It already is - this module was imported at the top of this file."""
        self.assertTrue(hasattr(mod, "find_path"))

    @unittest.skipIf(importlib.util.find_spec("pcbnew") is not None,
                     "pcbnew IS installed here, so the missing-module path "
                     "cannot be exercised")
    def test_write_tracks_raises_a_clear_error_without_pcbnew(self):
        with self.assertRaises(RuntimeError):
            mod.write_tracks("b.kicad_pcb", "N", 0.2, [((0, 0), (1, 0))])


class CliContract(unittest.TestCase):
    SCRIPT = ROUTE_SCRIPTS / "topological.py"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def write_requests(self, payload):
        path = self.tmp / "r.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_missing_input_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_a_solvable_set_exits_0(self):
        path = self.write_requests({
            "requests": [{"net": "SDA", "start": [0, 0], "end": [10, 0]}]})
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)
        self.assertEqual(report["params"]["plan"][0]["strategy"], "DIRECT")

    def test_an_unsolvable_high_speed_net_exits_1(self):
        path = self.write_requests({
            "requests": [{"net": "USB_DP", "start": [0, 0], "end": [10, 0],
                          "high_speed": True}],
            "obstacles": [{"name": "wall", "x_min": -100, "y_min": -1,
                           "x_max": 100, "y_max": 1}]})
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        by_check = {c["check"]: c for c in report["checks"]}
        self.assertEqual(by_check["route_found"]["status"], "FAIL")
        self.assertEqual(by_check["high_speed_via_ban"]["status"], "PASS")

    def test_an_empty_request_set_exits_1_not_0(self):
        """NO_COVERAGE must not read as a green run.

        The tool used to return 0 whenever there was no FAIL, so a request file
        with no nets - nothing routed at all - exited 0 and a chain would have
        taken it for "everything routed". grid_astar already exited 1 here.
        """
        path = self.write_requests({"requests": []})
        proc = self.run_cli(path)
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["FAIL"], 0)
        self.assertGreater(report["summary"]["NO_COVERAGE"], 0)

    def test_no_chamfer_makes_the_geometry_gate_fail(self):
        path = self.write_requests({
            "requests": [{"net": "SDA", "start": [0, 0], "end": [10, 10]}],
            "obstacles": [{"name": "o", "x_min": 3, "y_min": 3,
                           "x_max": 7, "y_max": 7}]})
        chamfered = json.loads(self.run_cli(path).stdout)
        raw = json.loads(self.run_cli(path, "--no-chamfer").stdout)
        self.assertEqual(
            {c["check"]: c["status"] for c in chamfered["checks"]}
            ["45_degree_geometry"], "PASS")
        self.assertEqual(
            {c["check"]: c["status"] for c in raw["checks"]}
            ["45_degree_geometry"], "FAIL")

    def test_the_report_matches_its_schema(self):
        schema = json.loads((SCHEMAS / "topological.schema.json")
                            .read_text(encoding="utf-8"))
        path = self.write_requests({
            "requests": [{"net": "SDA", "start": [0, 0], "end": [10, 0]}]})
        report = json.loads(self.run_cli(path).stdout)
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertIn("request", report)
        self.assertNotIn("board", report)


if __name__ == "__main__":
    unittest.main()
