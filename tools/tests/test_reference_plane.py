#!/usr/bin/env python3
"""Tests for scripts/board/reference_plane.py. Pure geometry, no pcbnew
needed - segments/planes are provided as already-extracted data."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS  # noqa: E402,F401

import reference_plane as mod  # noqa: E402

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


class ReferencePlaneContinuityTests(unittest.TestCase):
    def test_segment_fully_inside_the_plane_is_clean(self):
        plane = mod.PlanePolygon("In1.Cu", "GND", SQUARE)
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 9.0, 9.0)
        self.assertEqual([], mod.check_reference_plane_continuity([segment], [plane]))

    def test_segment_crossing_the_boundary_is_caught(self):
        plane = mod.PlanePolygon("In1.Cu", "GND", SQUARE)
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 15.0, 1.0)  # exits at x=15
        findings = mod.check_reference_plane_continuity([segment], [plane])
        self.assertEqual(1, len(findings))
        self.assertIn("OUTSIDE", findings[0]["reason"])

    def test_missing_plane_on_the_segment_layer_is_caught(self):
        plane = mod.PlanePolygon("In1.Cu", "GND", SQUARE)
        segment = mod.TraceSegment("USB_DP", "F.Cu", 1.0, 1.0, 5.0, 5.0)  # no F.Cu plane
        findings = mod.check_reference_plane_continuity([segment], [plane])
        self.assertEqual(1, len(findings))
        self.assertIn("no", findings[0]["reason"])

    def test_only_reported_once_per_segment_even_with_multiple_bad_points(self):
        """A segment whose start, midpoint AND end are all outside the
        plane still yields exactly one finding, not three."""
        plane = mod.PlanePolygon("In1.Cu", "GND", SQUARE)
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 20.0, 20.0, 30.0, 30.0)
        findings = mod.check_reference_plane_continuity([segment], [plane])
        self.assertEqual(1, len(findings))

    def test_wrong_reference_net_on_the_right_layer_is_treated_as_missing(self):
        plane = mod.PlanePolygon("In1.Cu", "PWR", SQUARE)  # not GND
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 5.0, 5.0)
        findings = mod.check_reference_plane_continuity([segment], [plane], reference_net="GND")
        self.assertEqual(1, len(findings))
        self.assertIn("no", findings[0]["reason"])

    def test_midpoint_over_a_notch_is_caught_even_if_endpoints_are_inside(self):
        """An L-shaped plane with a notch: both endpoints can sit inside
        the outer bounds while the segment's MIDPOINT crosses the notch -
        proves the midpoint check pulls its own weight, not just the
        endpoints."""
        l_shape = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]
        plane = mod.PlanePolygon("In1.Cu", "GND", l_shape)
        # Start (3,9) is in the left strip, end (9,3) is in the bottom
        # strip - both INSIDE the L - but their arithmetic midpoint (6,6)
        # falls squarely in the missing notch (x>5 and y>5).
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 3.0, 9.0, 9.0, 3.0)
        findings = mod.check_reference_plane_continuity([segment], [plane])
        self.assertEqual(1, len(findings))


class FindingWrapperTests(unittest.TestCase):
    def test_no_segments_is_no_coverage(self):
        finding = mod.check_reference_plane_continuity_finding([], [])
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_clean_segments_pass(self):
        plane = mod.PlanePolygon("In1.Cu", "GND", SQUARE)
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 9.0, 9.0)
        finding = mod.check_reference_plane_continuity_finding([segment], [plane])
        self.assertEqual("PASS", finding["status"])

    def test_violating_segments_fail(self):
        plane = mod.PlanePolygon("In1.Cu", "GND", SQUARE)
        segment = mod.TraceSegment("USB_DP", "In1.Cu", 1.0, 1.0, 15.0, 1.0)
        finding = mod.check_reference_plane_continuity_finding([segment], [plane])
        self.assertEqual("FAIL", finding["status"])


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = BOARD_SCRIPTS / "reference_plane.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False)

    def test_missing_args_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(2, proc.returncode)

    def test_self_test_exits_zero(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(0, proc.returncode)

    def test_json_files_round_trip_and_exit_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            segments_path = Path(tmp) / "segments.json"
            planes_path = Path(tmp) / "planes.json"
            segments_path.write_text(json.dumps([
                {"net_name": "USB_DP", "layer": "In1.Cu", "x1": 1.0, "y1": 1.0,
                 "x2": 9.0, "y2": 9.0},
            ]), encoding="utf-8")
            planes_path.write_text(json.dumps([
                {"layer": "In1.Cu", "net_name": "GND", "points_mm": SQUARE},
            ]), encoding="utf-8")
            proc = self.run_cli(str(segments_path), str(planes_path))
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual("PASS", report["checks"][0]["status"])


if __name__ == "__main__":
    unittest.main()
