#!/usr/bin/env python3
"""Tests for common/drc_summary.py (ported from test_drc_ozetleyici.py).

Project discipline: fault-injection proof for every A-level check — first the
correct clustering (PASS), then ``cell_size_mm`` is deliberately shrunk and
the clusters must really split apart (i.e. the test MEASURES something).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401

from common import drc_summary, toolpaths  # noqa: E402
from common.drc_summary import (  # noqa: E402
    Cluster,
    all_violations,
    cluster_summary,
    cluster_violations,
    drc_cluster_finding,
    nearest_footprint,
    representative_position,
    unconnected_net_positions,
)


def _violation(x: float, y: float, severity: str = "error",
               description: str = "kısa devre") -> dict:
    return {
        "severity": severity,
        "description": description,
        "items": [{"pos": {"x": x, "y": y}, "description": description}],
    }


class RepresentativePosition(unittest.TestCase):
    def test_single_item(self):
        self.assertEqual((12.5, 34.0),
                         representative_position(_violation(12.5, 34.0)))

    def test_multiple_items_are_averaged(self):
        violation = {
            "severity": "error",
            "items": [{"pos": {"x": 0.0, "y": 0.0}},
                      {"pos": {"x": 10.0, "y": 0.0}}],
        }
        self.assertEqual((5.0, 0.0), representative_position(violation))

    def test_missing_pos_returns_none(self):
        """No coordinate is invented: empty items or missing pos -> None."""
        for violation in ({"severity": "error", "items": []},
                          {"severity": "error"},
                          {"severity": "error", "items": [{"description": "x"}]}):
            with self.subTest(violation=violation):
                self.assertIsNone(representative_position(violation))


class UnconnectedNetPositions(unittest.TestCase):
    def test_extracts_the_bracketed_net_name_from_the_item_description(self):
        # Observed real kicad-cli shape: the net name is NOT a top-level
        # field, it is bracketed inside each item's own description.
        report = {"unconnected_items": [
            {"description": "Missing connection",
             "items": [
                 {"pos": {"x": 1.0, "y": 2.0}},
                 {"pos": {"x": 3.0, "y": 4.0}}]},
        ]}
        report["unconnected_items"][0]["items"][0]["description"] = \
            "5 icin [IMU_INT1] U1 pad F.Cu"
        report["unconnected_items"][0]["items"][1]["description"] = \
            "3 icin [IMU_INT1] U2 pad F.Cu"
        groups = unconnected_net_positions(report)
        self.assertEqual([(1.0, 2.0), (3.0, 4.0)], groups["IMU_INT1"])

    def test_groups_by_violation_level_net_name_when_no_bracket_is_present(self):
        report = {"unconnected_items": [
            {"net_name": "SPI_MOSI",
             "items": [{"pos": {"x": 1.0, "y": 2.0}},
                       {"pos": {"x": 3.0, "y": 4.0}}]},
            {"net_name": "SPI_MISO",
             "items": [{"pos": {"x": 5.0, "y": 6.0}}]},
        ]}
        groups = unconnected_net_positions(report)
        self.assertEqual([(1.0, 2.0), (3.0, 4.0)], groups["SPI_MOSI"])
        self.assertEqual([(5.0, 6.0)], groups["SPI_MISO"])

    def test_falls_back_to_description_when_net_name_is_absent(self):
        report = {"unconnected_items": [
            {"description": "Unconnected I2C_SDA",
             "items": [{"pos": {"x": 1.0, "y": 1.0}}]},
        ]}
        groups = unconnected_net_positions(report)
        self.assertIn("Unconnected I2C_SDA", groups)

    def test_missing_net_name_and_description_uses_a_placeholder_not_a_crash(self):
        report = {"unconnected_items": [
            {"items": [{"pos": {"x": 1.0, "y": 1.0}}]},
        ]}
        groups = unconnected_net_positions(report)
        self.assertIn("UNKNOWN_NET", groups)

    def test_items_without_a_position_are_not_invented(self):
        report = {"unconnected_items": [
            {"net_name": "N1", "items": [{"description": "no pos here"}]},
        ]}
        groups = unconnected_net_positions(report)
        self.assertEqual([], groups.get("N1", []))

    def test_no_unconnected_items_is_an_empty_dict(self):
        self.assertEqual({}, unconnected_net_positions({"violations": []}))


class Clustering(unittest.TestCase):
    def test_close_violations_form_one_cluster(self):
        near = [_violation(10.0 + (i % 4) * 0.1, 10.0 + (i % 3) * 0.1)
                for i in range(12)]
        far = [_violation(100.0, 100.0), _violation(200.0, 200.0)]

        clusters = cluster_violations(near + far, cell_size_mm=2.0)

        counts = sorted((c.count for c in clusters), reverse=True)
        self.assertEqual([12, 1, 1], counts)
        biggest = clusters[0]
        self.assertEqual(12, biggest.count)
        self.assertIsNotNone(biggest.center)
        self.assertLessEqual(10.0, biggest.center[0])
        self.assertLessEqual(biggest.center[0], 10.4)
        self.assertLessEqual(10.0, biggest.center[1])
        self.assertLessEqual(biggest.center[1], 10.3)

    def test_unlocated_violation_is_kept_not_dropped(self):
        located = [_violation(5.0, 5.0), _violation(5.1, 5.0)]
        unlocated = [{"severity": "warning", "description": "items yok",
                      "items": []}]

        clusters = cluster_violations(located + unlocated, cell_size_mm=2.0)

        unlocated_clusters = [c for c in clusters if c.unlocated]
        self.assertEqual(1, len(unlocated_clusters))
        self.assertEqual(1, unlocated_clusters[0].count)
        self.assertEqual(3, sum(c.count for c in clusters))

    def test_fault_injection_small_cell_splits_clusters(self):
        """FAULT INJECTION: the same 12 near violations must NOT stay in one
        cluster once ``cell_size_mm`` is made tiny — proof that the first test
        actually measures the parameter instead of always returning one
        cluster."""
        near = [_violation(10.0 + (i % 4) * 0.1, 10.0 + (i % 3) * 0.1)
                for i in range(12)]

        coarse = cluster_violations(near, cell_size_mm=2.0)
        fine = cluster_violations(near, cell_size_mm=0.01)

        self.assertEqual(1, len(coarse))
        self.assertGreater(len(fine), 1,
                           "clustering appears independent of cell_size_mm")
        self.assertEqual(12, sum(c.count for c in fine))

    def test_severity_counts(self):
        # 4.0/4.1 chosen on purpose: on a 2.0 mm grid both land in the same
        # cell. Values near a cell BOUNDARY can split by rounding direction;
        # that is a separate, known grid-boundary behaviour.
        violations = [_violation(4.0, 4.0, severity="error"),
                      _violation(4.1, 4.0, severity="warning")]
        clusters = cluster_violations(violations, cell_size_mm=2.0)
        self.assertEqual(1, len(clusters))
        self.assertEqual({"error": 1, "warning": 1},
                         clusters[0].severity_counts)

    def test_invalid_cell_size_rejected(self):
        with self.assertRaises(ValueError):
            cluster_violations([_violation(1.0, 1.0)], cell_size_mm=0)

    def test_empty_input(self):
        self.assertEqual([], cluster_violations([], cell_size_mm=2.0))

    def test_all_violations_reads_both_keys(self):
        report = {"violations": [_violation(1, 1)],
                  "unconnected_items": [_violation(2, 2)]}
        self.assertEqual(2, len(all_violations(report)))


class Summary(unittest.TestCase):
    def test_refdes_format(self):
        cluster = Cluster(center=(12.0, 34.0), count=12,
                          severity_counts={"error": 12}, refdes="U1")
        self.assertEqual(
            "Özet: U1 etrafında 12 adet error ihlali kümelendi. "
            "Öneri: U1 bölgesindeki yerleşimi genişletin.",
            cluster_summary(cluster))

    def test_position_used_when_no_refdes(self):
        cluster = Cluster(center=(12.345, 34.0), count=3,
                          severity_counts={"warning": 3})
        summary = cluster_summary(cluster)
        self.assertIn("(12.3", summary)
        self.assertIn("Öneri:", summary)
        self.assertIn("bu bölgedeki yerleşimi genişletin", summary)

    def test_unlocated_cluster_gets_a_different_message(self):
        cluster = Cluster(center=None, count=2,
                          severity_counts={"warning": 2})
        summary = cluster_summary(cluster)
        self.assertIn("konum bilgisi taşımayan", summary)
        self.assertIn("2 adet warning", summary)


class NearestFootprint(unittest.TestCase):
    def test_no_board_path_returns_none(self):
        self.assertIsNone(nearest_footprint((0.0, 0.0), board_path=None))

    def test_missing_pcbnew_returns_none(self):
        def raise_not_found(requested=None):
            raise FileNotFoundError("test: pcbnew yok")

        original = toolpaths.find_kicad_python
        toolpaths.find_kicad_python = raise_not_found
        try:
            self.assertIsNone(
                nearest_footprint((0.0, 0.0), board_path="board.kicad_pcb"))
        finally:
            toolpaths.find_kicad_python = original

    def test_picks_the_nearest_box(self):
        """The runner is faked, so no real pcbnew is needed; this exercises
        only the nearest-box selection."""
        boxes = {
            "U1": {"x_min": 9.0, "y_min": 9.0, "x_max": 11.0, "y_max": 11.0},
            "C3": {"x_min": 99.0, "y_min": 99.0,
                   "x_max": 101.0, "y_max": 101.0},
        }

        def fake_run(script_path, argv, kicad_python=None, timeout_s=60):
            return subprocess.CompletedProcess(
                ["python"], returncode=0,
                stdout=json.dumps(boxes, ensure_ascii=False), stderr="")

        original_find = toolpaths.find_kicad_python
        original_run = toolpaths.run_pcbnew_script
        toolpaths.find_kicad_python = lambda requested=None: "python"
        toolpaths.run_pcbnew_script = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                board = str(Path(tmp) / "board.kicad_pcb")
                self.assertEqual("U1", nearest_footprint((10.5, 10.5), board))
        finally:
            toolpaths.find_kicad_python = original_find
            toolpaths.run_pcbnew_script = original_run

    def test_module_does_not_import_toolpaths_eagerly(self):
        """drc_summary is a clustering library; the subprocess dependency is
        imported only inside nearest_footprint."""
        source = Path(drc_summary.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\nfrom .toolpaths import", source)


class ClusterFinding(unittest.TestCase):
    def test_unrecognised_schema_is_no_coverage(self):
        """Neither violations nor unconnected_items: whether DRC really ran
        is UNKNOWN, so this is NO_COVERAGE, never a silent PASS."""
        finding = drc_cluster_finding({"unknown_field": []})
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_clean_board_is_a_real_pass(self):
        """A recognised schema with zero violations is a real PASS, not
        NO_COVERAGE: zero violations means 'checked and clean' here."""
        finding = drc_cluster_finding({"violations": [],
                                       "unconnected_items": []})
        self.assertEqual("PASS", finding["status"])
        self.assertEqual(1, finding["scanned"])
        self.assertEqual([], finding["violations"])

    def test_violating_board_fails_with_cluster_summaries(self):
        report = {
            "violations": [_violation(10.0 + i * 0.1, 10.0,
                                      description="VCC/GND kısa devresi")
                           for i in range(5)],
            "unconnected_items": [],
        }
        finding = drc_cluster_finding(report, cell_size_mm=2.0)

        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, finding["scanned"])
        self.assertEqual(1, len(finding["violations"]))
        self.assertEqual(5, finding["violations"][0]["count"])
        self.assertIn("Özet:", finding["violations"][0]["summary"])
        self.assertIn("Öneri:", finding["violations"][0]["summary"])

    def test_unconnected_items_are_included(self):
        report = {
            "violations": [],
            "unconnected_items": [_violation(1.0, 1.0, severity="error",
                                             description="bağlanmamış pad")],
        }
        finding = drc_cluster_finding(report)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, finding["violations"][0]["count"])

    def test_finding_is_json_serialisable_with_bundle_encoder(self):
        from common.report_io import dumps
        report = {"violations": [_violation(1.0, 2.0)], "unconnected_items": []}
        text = dumps(drc_cluster_finding(report))
        self.assertIn("drc_clustering", text)


if __name__ == "__main__":
    unittest.main()
