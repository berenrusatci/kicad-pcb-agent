#!/usr/bin/env python3
"""Tests for common/escape_geom.py (ported from test_pcb_highspeed_escape.py).

Includes the source's fault-injection pairs: each check is first shown CLEAN,
then a defect is injected and the check must flip. A test that can only ever
pass proves nothing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401

from common.escape_geom import (  # noqa: E402
    FAB_MIN_MASK_DAM_MM,
    BoardPathSegment,
    DiffPairFanout,
    EscapeAssessment,
    HighDensityConnectorEscape,
    PinChannel,
    RouteSegment,
    build_escape_plan,
    channel_width_mm,
    check_mask_dam,
    check_multi_board_skew_budget,
    check_right_angle_corners,
    check_skew_budget,
    coplanar_gnd_clearance_mm,
    escape_findings,
    escape_messages,
    fanout_distance_mm,
    fanout_required,
    mask_dam_messages,
    mask_dam_width_mm,
    max_trace_width_for_dam_mm,
    meander_length_mm,
    meander_required,
    multi_board_skew_budget,
    pair_to_pair_min_distance_mm,
    right_angle_corners,
    skew_mm_to_ps,
    turn_angle_deg,
    via_pair_required,
)

# SOT-23-6 reference channel from the source doc.
SOT23_6 = PinChannel(pad_column_pitch_mm=2.274, pad_length_mm=1.32,
                     mask_expansion_mm=0.05)


class MaskDam(unittest.TestCase):
    def test_channel_width_sot23_6(self):
        self.assertAlmostEqual(0.954, channel_width_mm(SOT23_6), places=6)

    def test_reference_table(self):
        for track_mm, expected_dam_mm in ((0.40, 0.177), (0.30, 0.227),
                                          (0.25, 0.252)):
            with self.subTest(track_mm=track_mm):
                self.assertAlmostEqual(expected_dam_mm,
                                       mask_dam_width_mm(SOT23_6, track_mm),
                                       delta=1e-3)

    def test_violation_is_caught(self):
        messages = mask_dam_messages(SOT23_6, 0.40, fab_min_dam_mm=0.20)
        self.assertEqual(1, len(messages))
        self.assertIn("KRİTİK", messages[0])

    def test_valid_track_gives_empty_list(self):
        self.assertEqual([], mask_dam_messages(SOT23_6, 0.25,
                                               fab_min_dam_mm=0.20))

    def test_no_channel_is_critical(self):
        narrow = PinChannel(pad_column_pitch_mm=1.0, pad_length_mm=1.2)
        self.assertIn("short", mask_dam_messages(narrow, 0.2)[0])

    def test_max_trace_width_inverts_consistently(self):
        widest = max_trace_width_for_dam_mm(SOT23_6, FAB_MIN_MASK_DAM_MM)
        self.assertAlmostEqual(FAB_MIN_MASK_DAM_MM,
                               mask_dam_width_mm(SOT23_6, widest), places=6)

    def test_fault_injection_clean_then_violating(self):
        clean = mask_dam_messages(SOT23_6, 0.25, fab_min_dam_mm=0.20)
        self.assertEqual([], clean, "already FAIL before injection — scenario "
                                    "is invalid")
        broken = mask_dam_messages(SOT23_6, 0.90, fab_min_dam_mm=0.20)
        self.assertEqual(1, len(broken), "injected mask-dam violation missed")
        self.assertIn("KRİTİK", broken[0])

    def test_finding_wrapper_scans_one_channel(self):
        clean = check_mask_dam(SOT23_6, 0.25)
        self.assertEqual("PASS", clean["status"])
        self.assertEqual(1, clean["scanned"])
        broken = check_mask_dam(SOT23_6, 0.90)
        self.assertEqual("FAIL", broken["status"])
        self.assertEqual(1, len(broken["violations"]))


class Fanout(unittest.TestCase):
    def test_required_at_or_above_pair_pitch(self):
        self.assertIs(False, fanout_required(
            DiffPairFanout(pad_length_mm=0.60, pair_pitch_mm=0.62)))
        self.assertIs(True, fanout_required(
            DiffPairFanout(pad_length_mm=0.62, pair_pitch_mm=0.62)))

    def test_distance_is_none_when_not_required(self):
        self.assertIsNone(fanout_distance_mm(
            DiffPairFanout(pad_length_mm=0.5, pair_pitch_mm=0.62)))

    def test_distance_when_required(self):
        self.assertEqual(1.42, fanout_distance_mm(
            DiffPairFanout(pad_length_mm=0.62, pair_pitch_mm=0.62)))


class Corners(unittest.TestCase):
    def test_right_angle_is_caught(self):
        horizontal = RouteSegment(0, 0, 10, 0)
        vertical = RouteSegment(10, 0, 10, 10)
        self.assertAlmostEqual(90.0, turn_angle_deg(horizontal, vertical))
        self.assertEqual([0], right_angle_corners([horizontal, vertical]))

    def test_45_degrees_is_clean(self):
        horizontal = RouteSegment(0, 0, 10, 0)
        diagonal = RouteSegment(10, 0, 17.07, 7.07)
        self.assertEqual([], right_angle_corners([horizontal, diagonal]))

    def test_straight_line_has_no_corner(self):
        a = RouteSegment(0, 0, 10, 0)
        b = RouteSegment(10, 0, 20, 0)
        self.assertEqual([], right_angle_corners([a, b]))

    def test_fault_injection_clean_then_violating(self):
        a = RouteSegment(0, 0, 10, 0)
        straight = RouteSegment(10, 0, 20, 0)
        self.assertEqual([], right_angle_corners([a, straight]),
                         "scenario already FAIL — invalid")
        bent = RouteSegment(10, 0, 10, 10)
        self.assertEqual([0], right_angle_corners([a, bent]),
                         "injected 90 degree corner missed")

    def test_single_segment_is_no_coverage_not_pass(self):
        """A route with no corner to measure must not look clean."""
        finding = check_right_angle_corners([RouteSegment(0, 0, 1, 0)])
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_corner_finding_counts_pairs(self):
        finding = check_right_angle_corners([
            RouteSegment(0, 0, 10, 0),
            RouteSegment(10, 0, 10, 10),
            RouteSegment(10, 10, 20, 20),
        ])
        self.assertEqual(2, finding["scanned"])
        self.assertEqual("FAIL", finding["status"])


class Skew(unittest.TestCase):
    def test_mm_to_ps_real_example(self):
        # 1.47 mm = 8.8 ps (USB 2.0 HS example)
        self.assertAlmostEqual(8.8, skew_mm_to_ps(1.47), delta=0.1)

    def test_small_skew_needs_no_meander(self):
        self.assertIs(False, meander_required(1.47, budget_ps=100))
        self.assertEqual(0.0, meander_length_mm(1.47, budget_ps=100))

    def test_large_skew_needs_meander(self):
        self.assertIs(True, meander_required(30.0, budget_ps=100))
        self.assertGreater(meander_length_mm(30.0, budget_ps=100), 0)

    def test_skew_finding(self):
        self.assertEqual("PASS", check_skew_budget(1.47, 100)["status"])
        self.assertEqual("FAIL", check_skew_budget(30.0, 100)["status"])


class MultiBoardSkew(unittest.TestCase):
    def test_identical_paths_have_zero_skew(self):
        paths = [BoardPathSegment("cam0", host_pcb_length_mm=50.0,
                                  cable_length_mm=100.0,
                                  board_pcb_length_mm=10.0,
                                  layer_transition_count=1),
                 BoardPathSegment("cam1", host_pcb_length_mm=50.0,
                                  cable_length_mm=100.0,
                                  board_pcb_length_mm=10.0,
                                  layer_transition_count=1)]
        outcome = multi_board_skew_budget(paths, budget_ps=50.0)
        self.assertEqual(0.0, outcome.max_skew_ps)
        self.assertFalse(outcome.budget_exceeded)

    def test_a_longer_cable_increases_that_boards_delay(self):
        paths = [BoardPathSegment("near", cable_length_mm=100.0),
                 BoardPathSegment("far", cable_length_mm=500.0)]
        outcome = multi_board_skew_budget(paths, budget_ps=1e9)
        self.assertEqual("far", outcome.slowest_board)
        self.assertEqual("near", outcome.fastest_board)
        self.assertGreater(outcome.max_skew_ps, 0.0)

    def test_via_transitions_add_delay(self):
        base = BoardPathSegment("a", host_pcb_length_mm=10.0)
        with_vias = BoardPathSegment("b", host_pcb_length_mm=10.0,
                                     layer_transition_count=4)
        outcome = multi_board_skew_budget([base, with_vias], budget_ps=1e9)
        self.assertEqual("b", outcome.slowest_board)
        self.assertAlmostEqual(
            4 * 5.0, outcome.board_results["b"].via_delay_ps)

    def test_fault_injection_exceeding_the_budget_fails(self):
        paths = [BoardPathSegment("near", cable_length_mm=10.0),
                 BoardPathSegment("far", cable_length_mm=10000.0)]
        finding = check_multi_board_skew_budget(paths, budget_ps=10.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, len(finding["violations"]))
        self.assertEqual("far", finding["violations"][0]["slowest_board"])

    def test_within_budget_passes(self):
        paths = [BoardPathSegment("near", cable_length_mm=10.0),
                 BoardPathSegment("far", cable_length_mm=10.1)]
        finding = check_multi_board_skew_budget(paths, budget_ps=1e6)
        self.assertEqual("PASS", finding["status"])

    def test_empty_paths_raises(self):
        with self.assertRaises(ValueError):
            multi_board_skew_budget([], budget_ps=100.0)

    def test_empty_paths_finding_is_no_coverage_not_a_crash(self):
        finding = check_multi_board_skew_budget([], budget_ps=100.0)
        self.assertEqual("NO_COVERAGE", finding["status"])

    def test_a_single_board_reads_as_zero_skew_against_itself(self):
        outcome = multi_board_skew_budget(
            [BoardPathSegment("only", cable_length_mm=100.0)], budget_ps=1.0)
        self.assertEqual(0.0, outcome.max_skew_ps)
        self.assertEqual("only", outcome.fastest_board)
        self.assertEqual("only", outcome.slowest_board)

    def test_custom_velocities_change_the_delay(self):
        slow_cable = multi_board_skew_budget(
            [BoardPathSegment("a", cable_length_mm=100.0)], budget_ps=1e9,
            cable_velocity_mm_ns=100.0)
        fast_cable = multi_board_skew_budget(
            [BoardPathSegment("a", cable_length_mm=100.0)], budget_ps=1e9,
            cable_velocity_mm_ns=200.0)
        self.assertGreater(slow_cable.board_results["a"].cable_delay_ps,
                           fast_cable.board_results["a"].cable_delay_ps)


class GndPour(unittest.TestCase):
    def test_clearance_scales_with_width(self):
        self.assertAlmostEqual(0.5, coplanar_gnd_clearance_mm(0.4), delta=1e-6)


class CombinedReport(unittest.TestCase):
    def test_messages_mention_every_supplied_criterion(self):
        assessment = EscapeAssessment(
            net="USB_DP",
            channel=SOT23_6,
            track_width_mm=0.90,
            fanout=DiffPairFanout(pad_length_mm=0.62, pair_pitch_mm=0.62),
            route_segments=[RouteSegment(0, 0, 10, 0),
                            RouteSegment(10, 0, 10, 10)],
            skew_mm=30.0,
            budget_ps=100,
        )
        messages = escape_messages(assessment)
        joined = "\n".join(messages)
        self.assertIn("maske barajı", joined)
        self.assertIn("BİLGİ", joined)
        self.assertIn("85-95°", joined)
        self.assertIn("UYARI", joined)

    def test_empty_assessment_yields_no_coverage_findings_not_pass(self):
        """The message list is empty for an empty assessment — which is why
        the Finding form exists: nothing supplied must be NO_COVERAGE."""
        assessment = EscapeAssessment(net="X")
        self.assertEqual([], escape_messages(assessment))
        statuses = {f["check"]: f["status"] for f in escape_findings(assessment)}
        self.assertEqual(4, len(statuses))
        self.assertTrue(all(s == "NO_COVERAGE" for s in statuses.values()),
                        statuses)

    def test_findings_flip_on_a_bad_escape(self):
        assessment = EscapeAssessment(
            net="USB_DP", channel=SOT23_6, track_width_mm=0.90,
            route_segments=[RouteSegment(0, 0, 10, 0),
                            RouteSegment(10, 0, 10, 10)],
            skew_mm=30.0, budget_ps=100,
        )
        statuses = {f["check"]: f["status"] for f in escape_findings(assessment)}
        self.assertEqual("FAIL", statuses["mask_dam_channel"])
        self.assertEqual("FAIL", statuses["escape_right_angle_corners"])
        self.assertEqual("FAIL", statuses["escape_skew_budget"])
        self.assertEqual("NO_COVERAGE", statuses["escape_pair_fanout"])


class Df40Escape(unittest.TestCase):
    DF40 = HighDensityConnectorEscape(pad_pitch_mm=0.4, track_width_mm=0.127)

    def test_pair_to_pair_distance_matches_the_reference_ratio(self):
        # reference: ~0.47mm at 0.127mm track -> the exact ratio, not rounded
        self.assertAlmostEqual(0.47, pair_to_pair_min_distance_mm(self.DF40),
                               places=4)

    def test_pair_to_pair_distance_scales_with_track_width(self):
        wider = HighDensityConnectorEscape(pad_pitch_mm=0.4, track_width_mm=0.2)
        self.assertGreater(pair_to_pair_min_distance_mm(wider),
                           pair_to_pair_min_distance_mm(self.DF40))

    def test_escape_plan_starts_at_the_pad_and_moves_in_the_escape_direction(self):
        plan = build_escape_plan(self.DF40, pad_position=(10.0, 10.0),
                                 escape_direction=(0.0, 1.0))
        self.assertEqual((10.0, 10.0), plan[0])
        self.assertEqual((10.0, 10.0 + self.DF40.first_straight_segment_mm),
                         plan[1])

    def test_escape_plan_default_reference_values_are_carried_through_unchanged(self):
        self.assertAlmostEqual(0.55, self.DF40.first_straight_segment_mm)
        self.assertAlmostEqual(2.1, self.DF40.commit_distance_mm)
        self.assertAlmostEqual(1.6, self.DF40.max_trim_segment_mm)


class ViaPairRequired(unittest.TestCase):
    def test_a_clear_diagonal_needs_no_via_pair(self):
        self.assertFalse(via_pair_required(
            (0.0, 0.0), (10.0, 10.0), obstacle_bboxes=[]))

    def test_fault_injection_an_obstacle_on_the_diagonal_requires_a_via_pair(self):
        blocking_bbox = (4.0, 4.0, 6.0, 6.0)
        self.assertTrue(via_pair_required(
            (0.0, 0.0), (10.0, 10.0), [blocking_bbox]))

    def test_an_obstacle_off_the_diagonal_does_not_block(self):
        far_bbox = (100.0, 100.0, 110.0, 110.0)
        self.assertFalse(via_pair_required(
            (0.0, 0.0), (10.0, 10.0), [far_bbox]))

    def test_only_one_of_several_obstacles_needs_to_block(self):
        clear_bbox = (100.0, 100.0, 110.0, 110.0)
        blocking_bbox = (4.0, 4.0, 6.0, 6.0)
        self.assertTrue(via_pair_required(
            (0.0, 0.0), (10.0, 10.0), [clear_bbox, blocking_bbox]))

    def test_a_horizontal_segment_is_handled_without_division_by_zero(self):
        # dy == 0 exercises the Liang-Barsky p == 0 branch for that axis.
        self.assertFalse(via_pair_required(
            (0.0, 5.0), (10.0, 5.0), obstacle_bboxes=[]))
        self.assertTrue(via_pair_required(
            (0.0, 5.0), (10.0, 5.0), [(4.0, 4.0, 6.0, 6.0)]))


if __name__ == "__main__":
    unittest.main()
