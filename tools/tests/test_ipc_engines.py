"""Tests for the IPC rule engines: fab_class (IPC-6012) and dfa (IPC-A-610).

Ported from pcb-tool-v2's test_ipc6012_dfm_motoru.py and
test_ipc_a_610_dfa_motoru.py, converted from pytest to unittest.

The property these tests exist to defend: a check that examined nothing is
NO_COVERAGE and the run's verdict is NEEDS_HUMAN, never a silent PASS. The
archive already got this right through its Bulgu contract; the port must not
lose it while switching to common.findings.Finding.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._paths import SCHEMAS, TOOLS  # noqa: F401  (also fixes sys.path)

from common.escape_geom import FAB_MIN_MASK_DAM_MM
from scripts.ipc import dfa
from scripts.ipc import fab_class as fc
from scripts.ipc.report import CHECK_SCHEMA


def run_tool(module_relpath: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOLS / module_relpath), *args],
        capture_output=True, text=True, encoding="utf-8")


def write_json(directory: Path, name: str, payload) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# fab_class / IPC-6012
# --------------------------------------------------------------------------- #
class FabClassLimitsTest(unittest.TestCase):
    def test_class_3_is_strictly_tighter_than_class_2(self):
        c2 = fc.CLASS_LIMITS[fc.FabClass.CLASS_2]
        c3 = fc.CLASS_LIMITS[fc.FabClass.CLASS_3]
        self.assertGreater(c3.min_annular_ring_mm, c2.min_annular_ring_mm)
        self.assertLess(c3.max_aspect_ratio, c2.max_aspect_ratio)

    def test_mask_dam_floor_comes_from_escape_geom(self):
        """Single source: the dam floor is this bundle's fab minimum, and
        IPC-6012 itself gives no hard number."""
        for limits in fc.CLASS_LIMITS.values():
            self.assertEqual(limits.min_mask_dam_mm, FAB_MIN_MASK_DAM_MM)

    def test_every_limit_set_carries_a_source_note(self):
        for limits in fc.CLASS_LIMITS.values():
            self.assertTrue(limits.source_note.strip())

    def test_limits_can_be_overridden(self):
        custom = fc.ClassLimits(fc.FabClass.CLASS_2, 0.5, 0.5, 4.0, "verified")
        engine = fc.FabClassEngine(fc.FabClass.CLASS_2, limits=custom)
        self.assertEqual(engine.limits.min_annular_ring_mm, 0.5)


class FabClassMeasurementTest(unittest.TestCase):
    def test_annular_ring_is_single_sided(self):
        via = fc.ViaMeasurement("V1", 0.30, 0.60)
        self.assertAlmostEqual(via.annular_ring_mm, 0.15, places=9)

    def test_aspect_ratio(self):
        hole = fc.HoleMeasurement("H1", 1.6, 0.2)
        self.assertAlmostEqual(hole.aspect_ratio, 8.0, places=9)

    def test_zero_hole_is_rejected(self):
        with self.assertRaises(ValueError):
            _ = fc.HoleMeasurement("H1", 1.6, 0.0).aspect_ratio


class FabClassEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = fc.FabClassEngine(fc.FabClass.CLASS_2)

    def test_empty_sets_are_no_coverage_never_pass(self):
        for finding in self.engine.run_all():
            with self.subTest(check=finding["check"]):
                self.assertEqual(finding["status"], "NO_COVERAGE")
                self.assertEqual(finding["scanned"], 0)

    def test_compliant_via_passes(self):
        finding = self.engine.check_annular_ring(
            [fc.ViaMeasurement("V1", 0.30, 0.70)])
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 1)

    def test_thin_ring_fails_with_a_short_by_figure(self):
        finding = self.engine.check_annular_ring(
            [fc.ViaMeasurement("V1", 0.30, 0.32)])
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["ref"], "V1")
        self.assertGreater(finding["violations"][0]["short_by_mm"], 0)

    def test_narrow_mask_dam_fails(self):
        finding = self.engine.check_mask_dam(
            [fc.DamMeasurement("U1.1-U1.2", FAB_MIN_MASK_DAM_MM - 0.05)])
        self.assertEqual(finding["status"], "FAIL")

    def test_excessive_aspect_ratio_fails(self):
        finding = self.engine.check_aspect_ratio(
            [fc.HoleMeasurement("H1", 2.4, 0.2)])   # 12:1 vs a 10:1 limit
        self.assertEqual(finding["status"], "FAIL")

    def test_class_3_rejects_a_via_class_2_accepts(self):
        via = [fc.ViaMeasurement("V1", 0.30, 0.30 + 2 * 0.06)]
        self.assertEqual(
            fc.FabClassEngine(fc.FabClass.CLASS_2).check_annular_ring(via)["status"],
            "PASS")
        self.assertEqual(
            fc.FabClassEngine(fc.FabClass.CLASS_3).check_annular_ring(via)["status"],
            "FAIL")

    def test_measurements_from_json_tolerates_missing_keys(self):
        vias, dams, holes = fc.measurements_from_json({})
        self.assertEqual((vias, dams, holes), ([], [], []))

    def test_measurements_from_json_rejects_a_non_object(self):
        with self.assertRaises(ValueError):
            fc.measurements_from_json([1, 2, 3])

    def test_fault_injection_really_fires(self):
        self.assertTrue(fc._prove_test_not_empty())

    def test_self_tests_are_clean(self):
        self.assertEqual(fc.run_self_tests(), [])


# --------------------------------------------------------------------------- #
# dfa / IPC-A-610
# --------------------------------------------------------------------------- #
class DfaClearanceTest(unittest.TestCase):
    def setUp(self):
        self.size = dfa.PackageSize(2.0, 1.25, 0.6)

    def test_package_size_rejects_non_positive_dimensions(self):
        for bad in ((0.0, 1.0, 1.0), (1.0, -1.0, 1.0), (1.0, 1.0, 0.0)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dfa.PackageSize(*bad)

    def test_class_multipliers_are_ordered(self):
        values = [
            dfa.minimum_clearance(dfa.ComponentType.SMD_PASSIVE,
                                  dfa.ComponentType.SMD_PASSIVE,
                                  self.size, self.size, cls).minimum_clearance_mm
            for cls in (dfa.AssemblyClass.CLASS_1, dfa.AssemblyClass.CLASS_2,
                        dfa.AssemblyClass.CLASS_3)
        ]
        self.assertLess(values[0], values[1])
        self.assertLess(values[1], values[2])

    def test_pair_lookup_is_unordered(self):
        forward = dfa.minimum_clearance(
            dfa.ComponentType.SMD_PASSIVE, dfa.ComponentType.SMD_IC,
            self.size, self.size)
        reverse = dfa.minimum_clearance(
            dfa.ComponentType.SMD_IC, dfa.ComponentType.SMD_PASSIVE,
            self.size, self.size)
        self.assertEqual(forward.minimum_clearance_mm,
                         reverse.minimum_clearance_mm)

    def test_connector_dominates_even_next_to_a_passive(self):
        result = dfa.minimum_clearance(
            dfa.ComponentType.CONNECTOR, dfa.ComponentType.SMD_PASSIVE,
            self.size, self.size)
        self.assertGreaterEqual(result.minimum_clearance_mm,
                                dfa._CONNECTOR_BASE_MM)

    def test_tht_demands_more_than_smd(self):
        smd = dfa.minimum_clearance(
            dfa.ComponentType.SMD_PASSIVE, dfa.ComponentType.SMD_PASSIVE,
            self.size, self.size)
        tht = dfa.minimum_clearance(
            dfa.ComponentType.THT, dfa.ComponentType.THT,
            self.size, self.size)
        self.assertGreater(tht.minimum_clearance_mm, smd.minimum_clearance_mm)

    def test_shadowing_triggers_only_above_the_threshold(self):
        tall = dfa.PackageSize(
            2.0, 1.25, 0.6 + dfa.SHADOWING_HEIGHT_THRESHOLD_MM + 0.5)
        self.assertTrue(dfa.minimum_clearance(
            dfa.ComponentType.SMD_PASSIVE, dfa.ComponentType.SMD_IC,
            self.size, tall).shadowing_risk)
        self.assertFalse(dfa.minimum_clearance(
            dfa.ComponentType.SMD_PASSIVE, dfa.ComponentType.SMD_IC,
            self.size, self.size).shadowing_risk)

    def test_shadowing_does_not_apply_to_tht_pairs(self):
        tall = dfa.PackageSize(2.0, 1.25, 10.0)
        result = dfa.minimum_clearance(
            dfa.ComponentType.THT, dfa.ComponentType.SMD_PASSIVE,
            tall, self.size)
        self.assertFalse(result.shadowing_risk)

    def test_edge_gap_of_overlapping_boxes_is_zero_not_negative(self):
        self.assertEqual(
            dfa.edge_gap_mm(0, 0, self.size, 0, 0.1, 0, self.size, 0), 0.0)

    def test_edge_gap_is_edge_to_edge_not_centre_to_centre(self):
        gap = dfa.edge_gap_mm(0, 0, self.size, 0, 3.0, 0, self.size, 0)
        self.assertAlmostEqual(gap, 3.0 - self.size.length_mm, places=4)

    def test_90_degree_rotation_swaps_length_and_width(self):
        upright = dfa.edge_gap_mm(0, 0, self.size, 0, 3.0, 0, self.size, 0)
        rotated = dfa.edge_gap_mm(0, 0, self.size, 90, 3.0, 0, self.size, 90)
        self.assertGreater(rotated, upright)

    def test_edge_keepout_orders_connector_highest(self):
        self.assertGreater(
            dfa.minimum_edge_clearance_mm(dfa.ComponentType.CONNECTOR),
            dfa.minimum_edge_clearance_mm(dfa.ComponentType.SMD_PASSIVE))


class DfaEngineTest(unittest.TestCase):
    def setUp(self):
        self.size = dfa.PackageSize(2.0, 1.25, 0.6)
        self.engine = dfa.DfaEngine(dfa.AssemblyClass.CLASS_2)

    def test_empty_placement_is_no_coverage(self):
        for finding in self.engine.run_all():
            with self.subTest(check=finding["check"]):
                self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_a_single_component_yields_zero_pairs(self):
        placement = [dfa.PlacedComponent(
            "R1", dfa.ComponentType.SMD_PASSIVE, self.size, 0.0, 0.0)]
        finding = self.engine.check_component_clearance(placement)
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertEqual(finding["scanned"], 0)

    def test_well_spaced_components_pass(self):
        placement = [
            dfa.PlacedComponent("R1", dfa.ComponentType.SMD_PASSIVE,
                                self.size, 0.0, 0.0),
            dfa.PlacedComponent("R2", dfa.ComponentType.SMD_PASSIVE,
                                self.size, 20.0, 0.0),
        ]
        finding = self.engine.check_component_clearance(placement)
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 1)

    def test_crowded_components_fail(self):
        placement = [
            dfa.PlacedComponent("R1", dfa.ComponentType.SMD_PASSIVE,
                                self.size, 0.0, 0.0),
            dfa.PlacedComponent("R2", dfa.ComponentType.SMD_PASSIVE,
                                self.size, 2.05, 0.0),
        ]
        finding = self.engine.check_component_clearance(placement)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["a"], "R1")

    def test_pair_count_is_n_choose_2(self):
        placement = [
            dfa.PlacedComponent(f"R{i}", dfa.ComponentType.SMD_PASSIVE,
                                self.size, i * 20.0, 0.0)
            for i in range(5)
        ]
        finding = self.engine.check_component_clearance(placement)
        self.assertEqual(finding["scanned"], 10)

    def test_edge_check_without_outline_is_no_coverage(self):
        placement = [dfa.PlacedComponent(
            "R1", dfa.ComponentType.SMD_PASSIVE, self.size, 0.0, 0.0)]
        finding = self.engine.check_edge_clearance(placement, [])
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_component_too_close_to_the_edge_fails(self):
        placement = [dfa.PlacedComponent(
            "R1", dfa.ComponentType.SMD_PASSIVE, self.size, 0.5, 0.5)]
        finding = self.engine.check_edge_clearance(placement, [[0.0, 0.0]])
        self.assertEqual(finding["status"], "FAIL")

    def test_component_far_from_the_edge_passes(self):
        placement = [dfa.PlacedComponent(
            "R1", dfa.ComponentType.SMD_PASSIVE, self.size, 25.0, 25.0)]
        finding = self.engine.check_edge_clearance(placement, [[0.0, 0.0]])
        self.assertEqual(finding["status"], "PASS")

    def test_placement_from_json_rejects_a_non_object(self):
        with self.assertRaises(ValueError):
            dfa.placement_from_json([])

    def test_placement_from_json_rejects_an_unknown_type(self):
        with self.assertRaises(ValueError):
            dfa.placement_from_json({"components": [{
                "ref": "R1", "type": "not_a_type", "length_mm": 1.0,
                "width_mm": 1.0, "height_mm": 1.0, "x_mm": 0.0, "y_mm": 0.0}]})

    def test_fault_injection_really_fires(self):
        self.assertTrue(dfa._prove_test_not_empty())

    def test_self_tests_are_clean(self):
        self.assertEqual(dfa.run_self_tests(), [])


# --------------------------------------------------------------------------- #
# The CLI contract
# --------------------------------------------------------------------------- #
class EngineCliContractTest(unittest.TestCase):
    CLEAN_MEASUREMENTS = {
        "vias": [{"ref": "V1", "hole_mm": 0.30, "pad_mm": 0.70}],
        "dams": [{"id": "U1.1-U1.2", "width_mm": 0.40}],
        "holes": [{"ref": "H1", "board_thickness_mm": 1.6, "hole_mm": 0.30}],
    }
    DIRTY_MEASUREMENTS = {
        "vias": [{"ref": "V1", "hole_mm": 0.30, "pad_mm": 0.32}],
        "dams": [{"id": "U1.1-U1.2", "width_mm": 0.40}],
        "holes": [{"ref": "H1", "board_thickness_mm": 1.6, "hole_mm": 0.30}],
    }
    CLEAN_PLACEMENT = {
        "components": [
            {"ref": "R1", "type": "smd_passive", "length_mm": 2.0,
             "width_mm": 1.25, "height_mm": 0.6, "x_mm": 25.0, "y_mm": 25.0},
            {"ref": "R2", "type": "smd_passive", "length_mm": 2.0,
             "width_mm": 1.25, "height_mm": 0.6, "x_mm": 45.0, "y_mm": 25.0},
        ],
        "board_edge_points_mm": [[0, 0], [80, 0], [80, 60], [0, 60]],
    }

    def test_missing_positional_input_exits_2(self):
        for relpath in ("scripts/ipc/fab_class.py", "scripts/ipc/dfa.py"):
            with self.subTest(tool=relpath):
                proc = run_tool(relpath)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_self_test_is_opt_in(self):
        for relpath in ("scripts/ipc/fab_class.py", "scripts/ipc/dfa.py"):
            with self.subTest(tool=relpath):
                proc = run_tool(relpath, "--self-test")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("SELF-TEST PASS", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_clean_input_passes_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cases = (
                ("scripts/ipc/fab_class.py",
                 write_json(tmpdir, "m.json", self.CLEAN_MEASUREMENTS)),
                ("scripts/ipc/dfa.py",
                 write_json(tmpdir, "p.json", self.CLEAN_PLACEMENT)),
            )
            for relpath, path in cases:
                with self.subTest(tool=relpath):
                    proc = run_tool(relpath, str(path))
                    report = json.loads(proc.stdout)
                    self.assertEqual(report["schema_version"], CHECK_SCHEMA)
                    self.assertEqual(report["verdict"], "PASS", proc.stdout)
                    self.assertEqual(proc.returncode, 0)
                    self.assertTrue(report["limits"])

    def test_a_violation_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp), "m.json", self.DIRTY_MEASUREMENTS)
            proc = run_tool("scripts/ipc/fab_class.py", str(path))
            report = json.loads(proc.stdout)
            self.assertEqual(report["verdict"], "FAIL")
            self.assertEqual(proc.returncode, 1)

    def test_no_coverage_is_needs_human_and_exits_1(self):
        """The strict reading: an empty check is not a success.

        This is the one place these new tools deliberately differ from
        common.findings.board_exit_code, which returns 0 for NO_COVERAGE to
        preserve via_stub's published contract.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp), "empty.json", {})
            proc = run_tool("scripts/ipc/fab_class.py", str(path))
            report = json.loads(proc.stdout)
            self.assertEqual(report["verdict"], "NEEDS_HUMAN")
            self.assertEqual(report["summary"]["NO_COVERAGE"], 3)
            self.assertEqual(proc.returncode, 1)

    def test_json_flag_writes_and_matches_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            path = write_json(tmpdir, "m.json", self.CLEAN_MEASUREMENTS)
            out = tmpdir / "report.json"
            proc = run_tool("scripts/ipc/fab_class.py", str(path),
                            "--json", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.is_file())
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_malformed_input_exits_2_not_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            proc = run_tool("scripts/ipc/fab_class.py", str(path))
            self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_missing_file_exits_2(self):
        proc = run_tool("scripts/ipc/dfa.py", "/nonexistent/placement.json")
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_schema_forbids_a_scanned_zero_pass(self):
        schema = json.loads(
            (SCHEMAS / "ipc_check_report.schema.json").read_text(encoding="utf-8"))
        rule = schema["$defs"]["finding"]["allOf"][0]
        self.assertEqual(rule["if"]["properties"]["scanned"]["const"], 0)
        self.assertEqual(rule["then"]["properties"]["status"]["const"],
                         "NO_COVERAGE")
        self.assertEqual(
            set(schema["properties"]["verdict"]["enum"]),
            {"PASS", "FAIL", "NEEDS_HUMAN"})


if __name__ == "__main__":
    unittest.main()
