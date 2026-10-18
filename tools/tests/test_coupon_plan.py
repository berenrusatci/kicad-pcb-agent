#!/usr/bin/env python3
"""Tests for coupon planning (common/coupon.py + scripts/coupon_plan.py).

The derivations are checked against physics, not against recorded output:

    er_eff = 1   ->  v_p = 0.299792458 mm/ps, which is c
    er_eff x 4   ->  v_p halves
    dx = v_p * t_r / 2, and the 2 is the out-and-back path, not a margin

and the refusals are checked because they are what the gate is for. The
failure this step prevents is silent and expensive: a board that comes back
from fab with no way to measure whether its impedance is right.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

from common.coupon import (  # noqa: E402
    CouponError,
    ImpedanceClass,
    coverage,
    geometry_matches,
    minimum_coupon_length_mm,
    parse_impedance_class,
    parse_requirements,
    plan_coupon,
    propagation_velocity_mm_per_ps,
    tdr_resolution_mm,
)

SCRIPT = SCRIPTS / "coupon_plan.py"

REQUIREMENTS = {"impedance_classes": [
    {"name": "MIPI-90", "structure": "coupled-microstrip", "target_ohm": 90,
     "tolerance_percent": 10, "layer": "F.Cu", "w_mm": 0.09, "s_mm": 0.15,
     "h_mm": 0.10, "er": 4.3, "nets": ["D0_P", "D0_N"]},
    {"name": "SE-50", "structure": "microstrip", "target_ohm": 50,
     "tolerance_percent": 10, "layer": "F.Cu", "w_mm": 0.20, "h_mm": 0.10,
     "er": 4.3},
]}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def a_class(**overrides):
    base = dict(name="c", structure="microstrip", target_ohm=50.0,
                tolerance_percent=10.0, layer="F.Cu", w_mm=0.2, er=4.3,
                h_mm=0.1)
    base.update(overrides)
    return ImpedanceClass(**base)


class Physics(unittest.TestCase):
    def test_velocity_equals_c_in_vacuum(self):
        self.assertAlmostEqual(propagation_velocity_mm_per_ps(1.0),
                               0.299792458, places=9)

    def test_velocity_halves_when_permittivity_quadruples(self):
        self.assertAlmostEqual(
            propagation_velocity_mm_per_ps(1.0)
            / propagation_velocity_mm_per_ps(4.0), 2.0, places=12)

    def test_resolution_is_half_the_rise_time_length(self):
        """The 2 is the out-and-back path, not a safety factor."""
        expected = propagation_velocity_mm_per_ps(4.0) * 100.0 / 2.0
        self.assertAlmostEqual(tdr_resolution_mm(4.0, 100.0), expected,
                               places=12)

    def test_a_faster_edge_needs_a_proportionally_shorter_coupon(self):
        fast = minimum_coupon_length_mm(4.0, 20.0)
        slow = minimum_coupon_length_mm(4.0, 200.0)
        self.assertAlmostEqual(slow / fast, 10.0, places=9)

    def test_a_slower_laminate_needs_a_shorter_coupon(self):
        """Higher er_eff means a slower wave, so the same edge occupies less
        length."""
        self.assertLess(minimum_coupon_length_mm(9.0, 100.0),
                        minimum_coupon_length_mm(2.0, 100.0))

    def test_the_resolution_multiple_scales_linearly(self):
        self.assertAlmostEqual(
            minimum_coupon_length_mm(4.0, 100.0, 2.0)
            / minimum_coupon_length_mm(4.0, 100.0, 1.0), 2.0, places=12)

    def test_a_non_positive_rise_time_is_refused(self):
        with self.assertRaises(CouponError):
            tdr_resolution_mm(4.0, 0.0)

    def test_a_non_positive_permittivity_is_refused(self):
        with self.assertRaises(CouponError):
            propagation_velocity_mm_per_ps(0.0)

    def test_a_non_positive_multiple_is_refused(self):
        with self.assertRaises(CouponError):
            minimum_coupon_length_mm(4.0, 100.0, 0.0)


class RequirementValidation(unittest.TestCase):
    def test_a_coupled_structure_needs_a_gap(self):
        with self.assertRaises(CouponError):
            a_class(structure="coupled-microstrip", s_mm=0.0)

    def test_a_height_is_mandatory(self):
        """The coupon must reproduce the production stackup."""
        with self.assertRaises(CouponError):
            a_class(h_mm=0.0)

    def test_an_unknown_structure_is_refused(self):
        with self.assertRaises(CouponError):
            a_class(structure="coplanar")

    def test_a_typo_in_a_key_is_refused(self):
        with self.assertRaises(CouponError):
            parse_impedance_class({"name": "c", "structure": "microstrip",
                                   "target_ohm": 50, "w_mm": 0.2, "er": 4.3,
                                   "h_mm": 0.1, "widht_mm": 0.2})

    def test_a_missing_required_key_is_refused(self):
        with self.assertRaises(CouponError):
            parse_impedance_class({"name": "c", "structure": "microstrip"})

    def test_an_empty_requirement_set_is_refused(self):
        """A plan for no class would report success having done nothing."""
        with self.assertRaises(CouponError):
            parse_requirements({"impedance_classes": []})

    def test_duplicate_class_names_are_refused(self):
        with self.assertRaises(CouponError):
            parse_requirements({"impedance_classes": [
                {"name": "same", "structure": "microstrip", "target_ohm": 50,
                 "w_mm": 0.2, "er": 4.3, "h_mm": 0.1},
                {"name": "same", "structure": "microstrip", "target_ohm": 90,
                 "w_mm": 0.1, "er": 4.3, "h_mm": 0.1}]})


class Planning(unittest.TestCase):
    def test_a_derived_length_meets_its_own_minimum(self):
        coupon = plan_coupon(a_class(), 3.1, "stated", 50.0)
        self.assertTrue(coupon.long_enough)
        self.assertAlmostEqual(coupon.planned_length_mm,
                               coupon.minimum_length_mm, places=12)

    def test_a_margin_makes_the_coupon_longer(self):
        plain = plan_coupon(a_class(), 3.1, "stated", 50.0)
        padded = plan_coupon(a_class(), 3.1, "stated", 50.0, length_margin=1.5)
        self.assertAlmostEqual(padded.planned_length_mm
                               / plain.planned_length_mm, 1.5, places=9)

    def test_a_short_coupon_is_flagged_and_warned_about(self):
        coupon = plan_coupon(a_class(), 3.1, "stated", 100.0,
                             planned_length_mm=0.5)
        self.assertFalse(coupon.long_enough)
        self.assertTrue(coupon.warnings)
        self.assertIn("transition", coupon.warnings[0])

    def test_a_margin_below_one_is_refused(self):
        """It would plan a coupon shorter than the minimum just computed."""
        with self.assertRaises(CouponError):
            plan_coupon(a_class(), 3.1, "stated", 50.0, length_margin=0.5)

    def test_the_er_eff_source_travels_with_the_coupon(self):
        coupon = plan_coupon(a_class(), 3.1, "field_solver 2-D solve", 50.0)
        self.assertEqual("field_solver 2-D solve", coupon.er_eff_source)

    def test_a_coupled_coupon_reports_its_gap(self):
        coupon = plan_coupon(a_class(structure="coupled-microstrip", s_mm=0.15),
                             2.5, "stated", 50.0)
        self.assertIn("s", coupon.to_dict()["geometry_mm"])


class Coverage(unittest.TestCase):
    def test_a_class_with_no_coupon_is_missing(self):
        report = coverage([a_class(name="only")], [])
        self.assertEqual(["only"], report["missing_classes"])
        self.assertFalse(report["covered"])

    def test_a_short_coupon_breaks_coverage(self):
        requirement = a_class(name="c")
        coupon = plan_coupon(requirement, 3.1, "stated", 100.0,
                             planned_length_mm=0.1)
        report = coverage([requirement], [coupon])
        self.assertEqual(["c"], report["too_short_classes"])
        self.assertFalse(report["covered"])

    def test_a_full_plan_is_covered(self):
        requirement = a_class(name="c")
        coupon = plan_coupon(requirement, 3.1, "stated", 50.0)
        report = coverage([requirement], [coupon])
        self.assertTrue(report["covered"])
        self.assertEqual(1.0, report["coverage_fraction"])

    def test_geometry_mismatch_is_detectable(self):
        """A coupon that does not share the cross-section measures something
        else, however good the measurement is."""
        requirement = a_class(w_mm=0.2)
        matching = plan_coupon(requirement, 3.1, "stated", 50.0)
        self.assertTrue(geometry_matches(requirement, matching))
        self.assertFalse(geometry_matches(a_class(w_mm=0.4), matching))


class CliContract(unittest.TestCase):
    def _write(self, tmp, payload=None):
        path = Path(tmp) / "req.json"
        path.write_text(json.dumps(payload or REQUIREMENTS), encoding="utf-8")
        return path

    def test_self_test_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)

    def test_a_missing_rise_time_is_refused(self):
        """It sets the resolution and therefore the length; never defaulted."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(self._write(tmp)), "--er-eff", "3.1")
            self.assertEqual(2, proc.returncode)
            self.assertIn("--tdr-rise-time-ps", proc.stderr)

    def test_no_er_eff_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(self._write(tmp)), "--tdr-rise-time-ps", "20")
            self.assertEqual(2, proc.returncode)
            self.assertIn("er_eff", proc.stderr)

    def test_a_stated_er_eff_produces_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run(str(self._write(tmp)), "--tdr-rise-time-ps", "20",
                       "--er-eff", "3.1", "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("GREEN", report["status"])
            self.assertEqual(2, len(report["coupons"]))
            self.assertIn("stated", report["coupons"][0]["er_eff_source"])

    def test_solving_records_the_field_solver_as_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run(str(self._write(tmp)), "--tdr-rise-time-ps", "20",
                       "--solve", "--max-nodes", "40000", "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            for coupon in report["coupons"]:
                self.assertIn("field_solver", coupon["er_eff_source"])

    def test_a_stripline_coupon_solves_to_the_laminate_permittivity(self):
        """Homogeneous medium: er_eff must equal er exactly."""
        payload = {"impedance_classes": [
            {"name": "SL", "structure": "stripline", "target_ohm": 50,
             "layer": "In2.Cu", "w_mm": 0.15, "h_mm": 0.30, "er": 4.3}]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            run(str(self._write(tmp, payload)), "--tdr-rise-time-ps", "20",
                "--solve", "--max-nodes", "60000", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertAlmostEqual(4.3, report["coupons"][0]["er_eff"],
                                   places=4)

    def test_an_over_short_forced_length_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = run(str(self._write(tmp)), "--tdr-rise-time-ps", "200",
                       "--er-eff", "3.1", "--length-mm", "0.5",
                       "--json", str(out))
            self.assertEqual(1, proc.returncode)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("RED", report["status"])
            self.assertIn("COUPON_TOO_SHORT", report["reason_codes"])

    def test_a_faster_head_plans_shorter_coupons(self):
        lengths = []
        for rise in ("20", "200"):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "r.json"
                run(str(self._write(tmp)), "--tdr-rise-time-ps", rise,
                    "--er-eff", "3.1", "--json", str(out))
                report = json.loads(out.read_text(encoding="utf-8"))
                lengths.append(report["total_coupon_length_mm"])
        self.assertLess(lengths[0], lengths[1])

    def test_the_phase_is_pinned_to_pre_gerber(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            run(str(self._write(tmp)), "--tdr-rise-time-ps", "20",
                "--er-eff", "3.1", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("pre_gerber", report["phase"])

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(self._write(tmp)), "--tdr-rise-time-ps", "20",
                       "--er-eff", "3.1")
            json.loads(proc.stdout)

    def test_a_missing_requirements_file_is_exit_2(self):
        self.assertEqual(2, run("no-such-file.json", "--tdr-rise-time-ps",
                                "20", "--er-eff", "3.1").returncode)


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(
            (SCHEMAS / "coupon_plan_report.schema.json").read_text(
                encoding="utf-8"))

    def test_schema_pins_the_version_and_phase(self):
        self.assertEqual("coupon-plan-report/v1",
                         self.schema["properties"]["schema_version"]["const"])
        self.assertEqual("pre_gerber",
                         self.schema["properties"]["phase"]["const"])

    def test_reason_codes_emitted_are_declared(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        self.assertEqual({"CLASS_WITHOUT_COUPON", "COUPON_TOO_SHORT"},
                         declared)

    def test_the_er_eff_source_is_required_on_every_coupon(self):
        required = self.schema["properties"]["coupons"]["items"]["required"]
        self.assertIn("er_eff_source", required)

    def test_a_real_report_satisfies_the_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "req.json"
            path.write_text(json.dumps(REQUIREMENTS), encoding="utf-8")
            out = Path(tmp) / "r.json"
            run(str(path), "--tdr-rise-time-ps", "20", "--er-eff", "3.1",
                "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        for key in self.schema["required"]:
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
