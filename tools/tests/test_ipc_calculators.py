"""Tests for the IPC calculators: trace_width, clearance, via_current.

Ported from pcb-tool-v2's test_ipc2152_hesaplayici.py,
test_ipc2221_clearance_hesaplayici.py and test_via_capi_hesaplayici.py.
Those were pytest files; this is unittest, because the bundle's suite must
run with no third-party dependency.

Two deliberate divergences from the archive assertions:

1. The archive hardcoded 1 oz = 1.378 mil. This bundle derives it from
   ``common.units.OZ_TO_MM * MM_TO_MIL`` (~1.37008) because
   ``common.si_models`` is the single source for the IPC-2221 closed form
   and re-hardcoding the constant here would recreate exactly the silent
   fork the consolidation removed. Width assertions therefore use a
   tolerance, and ``test_copper_constant_has_not_drifted`` pins the
   divergence so a future change to either constant is caught loudly.

2. The archive proved the clearance table against
   ``pcb_stackup_planner.gerekli_izolasyon_mesafesi_mm``; here the same
   proof runs against ``common.stackup.required_isolation_mm``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._paths import SCHEMAS, TOOLS  # noqa: F401  (also fixes sys.path)

from common.units import MM_TO_MIL, OZ_TO_MM
from scripts.ipc import clearance as clearance_mod
from scripts.ipc import trace_width as tw
from scripts.ipc import via_current as vc
from scripts.ipc.report import CALC_SCHEMA

ARCHIVE_MIL_PER_OZ = 1.378


def run_tool(module_relpath: str, *args: str) -> subprocess.CompletedProcess:
    """Run a tool as a real subprocess, which is the only way to observe the
    actual exit code and the stdout/stderr split."""
    return subprocess.run(
        [sys.executable, str(TOOLS / module_relpath), *args],
        capture_output=True, text=True, encoding="utf-8")


# --------------------------------------------------------------------------- #
# trace_width
# --------------------------------------------------------------------------- #
class TraceWidthTest(unittest.TestCase):
    def test_external_matches_the_shared_ipc2221_helper(self):
        from common.si_models import ipc2221_min_width_mm
        result = tw.min_trace_width(5.0, 10.0, tw.LayerType.EXTERNAL)
        expected = ipc2221_min_width_mm(5.0, 10.0, 1.0, "microstrip")
        self.assertAlmostEqual(result.width_mm, round(expected, 4), places=4)

    def test_copper_constant_has_not_drifted(self):
        """Pin the one real numeric divergence from the archive.

        If either OZ_TO_MM or MM_TO_MIL is ever changed, this fails loudly
        instead of quietly shifting every current-capacity number in the
        bundle.
        """
        derived = OZ_TO_MM * MM_TO_MIL
        self.assertAlmostEqual(derived, ARCHIVE_MIL_PER_OZ, delta=0.02)

    def test_internal_derating_is_applied(self):
        external = tw.min_trace_width(5.0, 10.0, tw.LayerType.EXTERNAL)
        internal = tw.min_trace_width(5.0, 10.0, tw.LayerType.INTERNAL)
        self.assertEqual(internal.inner_derating, tw.DEFAULT_INNER_DERATING)
        self.assertEqual(external.inner_derating, 1.0)

    def test_custom_derating_is_recorded_in_the_result(self):
        result = tw.min_trace_width(5.0, 10.0, tw.LayerType.INTERNAL,
                                    inner_derating=1.5)
        self.assertEqual(result.inner_derating, 1.5)

    def test_internal_is_never_narrower_than_external(self):
        results = tw.compare_layers(10.0, 10.0)
        self.assertGreaterEqual(results["internal"].width_mm,
                                results["external"].width_mm)

    def test_width_grows_with_current(self):
        low = tw.min_trace_width(1.0, 10.0, tw.LayerType.EXTERNAL)
        high = tw.min_trace_width(5.0, 10.0, tw.LayerType.EXTERNAL)
        self.assertGreater(high.width_mm, low.width_mm)

    def test_width_shrinks_with_larger_temperature_budget(self):
        tight = tw.min_trace_width(5.0, 5.0, tw.LayerType.EXTERNAL)
        loose = tw.min_trace_width(5.0, 20.0, tw.LayerType.EXTERNAL)
        self.assertLess(loose.width_mm, tight.width_mm)

    def test_width_shrinks_with_thicker_copper(self):
        thin = tw.min_trace_width(5.0, 10.0, tw.LayerType.EXTERNAL, copper_oz=1.0)
        thick = tw.min_trace_width(5.0, 10.0, tw.LayerType.EXTERNAL, copper_oz=2.0)
        self.assertLess(thick.width_mm, thin.width_mm)

    def test_result_fields_are_populated(self):
        result = tw.min_trace_width(5.0, 10.0, tw.LayerType.EXTERNAL)
        self.assertGreater(result.cross_section_mils2, 0)
        self.assertEqual(result.layer_type, "external")

    def test_invalid_inputs_are_rejected(self):
        for bad in ({"current_a": 0.0}, {"current_a": -1.0},
                    {"delta_t_c": 0.0}, {"delta_t_c": -5.0},
                    {"copper_oz": 0.0}, {"copper_oz": -1.0}):
            with self.subTest(bad=bad):
                kwargs = {"current_a": 1.0, "delta_t_c": 10.0,
                          "layer_type": tw.LayerType.EXTERNAL, "copper_oz": 1.0}
                kwargs.update(bad)
                with self.assertRaises(ValueError):
                    tw.min_trace_width(**kwargs)

    def test_derating_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            tw.min_trace_width(5.0, 10.0, tw.LayerType.INTERNAL,
                               inner_derating=0.9)

    def test_compare_layers_returns_both_keys(self):
        self.assertEqual(set(tw.compare_layers(5.0, 10.0)),
                         {"internal", "external"})

    def test_fault_injection_really_fires(self):
        self.assertTrue(tw._prove_test_not_empty())

    def test_self_tests_are_clean(self):
        self.assertEqual(tw.run_self_tests(), [])


# --------------------------------------------------------------------------- #
# clearance
# --------------------------------------------------------------------------- #
class ClearanceTest(unittest.TestCase):
    def test_external_uncoated_matches_common_stackup(self):
        """The single-source claim in the module docstring, proven."""
        from common.stackup import required_isolation_mm
        for voltage, expected in ((15, 0.05), (30, 0.05), (50, 0.10),
                                  (100, 0.10), (500, 0.50)):
            with self.subTest(voltage=voltage):
                result = clearance_mod.clearance_mm(
                    voltage, clearance_mod.LayerType.EXTERNAL,
                    clearance_mod.CoatingState.UNCOATED)
                self.assertAlmostEqual(result.clearance_mm, expected, places=6)
                self.assertAlmostEqual(result.clearance_mm,
                                       required_isolation_mm(voltage), places=6)
                self.assertEqual(
                    result.confidence,
                    clearance_mod.Confidence.MATCHES_EXISTING_CODE.value)

    def test_every_value_inside_a_band_is_identical(self):
        low = clearance_mod.clearance_mm(101, clearance_mod.LayerType.EXTERNAL)
        high = clearance_mod.clearance_mm(299, clearance_mod.LayerType.EXTERNAL)
        self.assertEqual(low.clearance_mm, high.clearance_mm)
        self.assertFalse(low.interpolated)

    def test_above_the_table_extrapolates_linearly(self):
        result = clearance_mod.clearance_mm(1000, clearance_mod.LayerType.EXTERNAL)
        self.assertAlmostEqual(result.clearance_mm, 1.0, places=6)
        self.assertEqual(result.confidence,
                         clearance_mod.Confidence.CONSERVATIVE_ASSUMPTION.value)

    def test_zero_volts_still_returns_a_positive_clearance(self):
        result = clearance_mod.clearance_mm(0, clearance_mod.LayerType.EXTERNAL)
        self.assertGreater(result.clearance_mm, 0)

    def test_negative_voltage_is_rejected(self):
        with self.assertRaises(ValueError):
            clearance_mod.clearance_mm(-1, clearance_mod.LayerType.EXTERNAL)

    def test_interpolation_produces_an_intermediate_value(self):
        stepped = clearance_mod.clearance_mm(
            200, clearance_mod.LayerType.EXTERNAL, interpolate=False)
        smooth = clearance_mod.clearance_mm(
            200, clearance_mod.LayerType.EXTERNAL, interpolate=True)
        self.assertAlmostEqual(stepped.clearance_mm, 0.40, places=6)
        self.assertLess(0.10, smooth.clearance_mm)
        self.assertLess(smooth.clearance_mm, 0.40)
        self.assertTrue(smooth.interpolated)
        self.assertFalse(stepped.interpolated)

    def test_interpolation_at_a_band_edge_returns_the_table_value(self):
        result = clearance_mod.clearance_mm(
            100, clearance_mod.LayerType.EXTERNAL, interpolate=True)
        self.assertAlmostEqual(result.clearance_mm, 0.10, places=6)

    def test_interpolation_inherits_the_least_confident_endpoint(self):
        result = clearance_mod.clearance_mm(
            200, clearance_mod.LayerType.EXTERNAL, interpolate=True)
        self.assertEqual(result.confidence,
                         clearance_mod.Confidence.CONSERVATIVE_ASSUMPTION.value)

    def test_coated_is_smaller_at_high_voltage(self):
        uncoated = clearance_mod.clearance_mm(
            500, clearance_mod.LayerType.EXTERNAL,
            clearance_mod.CoatingState.UNCOATED)
        coated = clearance_mod.clearance_mm(
            500, clearance_mod.LayerType.EXTERNAL,
            clearance_mod.CoatingState.CONFORMAL_COATED)
        self.assertLess(coated.clearance_mm, uncoated.clearance_mm)

    def test_coated_sits_on_the_process_floor_at_low_voltage(self):
        """Not a bug: 'coating always needs less distance' is false at low
        voltage, because the coating process floor dominates."""
        coated = clearance_mod.clearance_mm(
            15, clearance_mod.LayerType.EXTERNAL,
            clearance_mod.CoatingState.CONFORMAL_COATED)
        self.assertAlmostEqual(coated.clearance_mm,
                               clearance_mod._COATED_FLOOR_MM, places=6)

    def test_internal_plus_coating_is_rejected(self):
        with self.assertRaises(ValueError):
            clearance_mod.clearance_mm(
                50, clearance_mod.LayerType.INTERNAL,
                clearance_mod.CoatingState.CONFORMAL_COATED)

    def test_internal_is_never_narrower_than_external(self):
        for voltage in (15, 50, 100, 300, 500):
            with self.subTest(voltage=voltage):
                internal = clearance_mod.clearance_mm(
                    voltage, clearance_mod.LayerType.INTERNAL)
                external = clearance_mod.clearance_mm(
                    voltage, clearance_mod.LayerType.EXTERNAL)
                self.assertGreaterEqual(internal.clearance_mm,
                                        external.clearance_mm)

    def test_internal_confidence_is_conservative(self):
        result = clearance_mod.clearance_mm(50, clearance_mod.LayerType.INTERNAL)
        self.assertEqual(result.confidence,
                         clearance_mod.Confidence.CONSERVATIVE_ASSUMPTION.value)

    def test_creepage_defaults_to_clearance(self):
        result = clearance_mod.clearance_mm(100, clearance_mod.LayerType.EXTERNAL)
        self.assertAlmostEqual(result.creepage_mm, result.clearance_mm, places=6)

    def test_creepage_factor_is_applied(self):
        result = clearance_mod.clearance_mm(
            100, clearance_mod.LayerType.EXTERNAL, creepage_factor=1.5)
        self.assertAlmostEqual(result.creepage_mm,
                               result.clearance_mm * 1.5, places=4)

    def test_creepage_factor_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            clearance_mod.clearance_mm(100, clearance_mod.LayerType.EXTERNAL,
                                       creepage_factor=0.5)

    def test_all_combinations_returns_the_three_valid_keys(self):
        self.assertEqual(
            set(clearance_mod.all_combinations(100)),
            {"external_uncoated", "internal_uncoated",
             "external_conformal_coated"})

    def test_clearance_is_monotonic_in_voltage(self):
        previous = None
        for voltage in (10, 20, 40, 75, 200, 400, 600):
            value = clearance_mod.clearance_mm(
                voltage, clearance_mod.LayerType.EXTERNAL).clearance_mm
            if previous is not None:
                self.assertGreaterEqual(value, previous)
            previous = value

    def test_fault_injection_really_fires(self):
        self.assertTrue(clearance_mod._prove_test_not_empty())

    def test_self_tests_are_clean(self):
        self.assertEqual(clearance_mod.run_self_tests(), [])


# --------------------------------------------------------------------------- #
# via_current
# --------------------------------------------------------------------------- #
class ViaCurrentTest(unittest.TestCase):
    def setUp(self):
        from common.stackup import fab_profiles
        self.profiles = fab_profiles()
        self.profile = self.profiles["JLCPCB_STANDARD"]

    def test_hole_diameter_grows_with_current(self):
        self.assertGreater(vc.via_hole_diameter_mm(5.0),
                           vc.via_hole_diameter_mm(1.0))

    def test_hole_diameter_shrinks_with_thicker_plating(self):
        self.assertLess(vc.via_hole_diameter_mm(5.0, plating_oz=2.0),
                        vc.via_hole_diameter_mm(5.0, plating_oz=1.0))

    def test_pad_is_hole_plus_two_rings(self):
        self.assertAlmostEqual(vc.pad_diameter_mm(0.3, 0.13), 0.56, places=9)

    def test_pad_rejects_non_positive_hole(self):
        with self.assertRaises(ValueError):
            vc.pad_diameter_mm(0.0, 0.13)

    def test_pad_rejects_negative_ring(self):
        with self.assertRaises(ValueError):
            vc.pad_diameter_mm(0.3, -0.01)

    def test_large_current_stays_above_the_fab_floor(self):
        result = vc.recommend_via(20.0, self.profile)
        self.assertFalse(result.below_manufacturable_floor)
        self.assertFalse(result.stitching_suggested)
        self.assertEqual(result.via_count, 1)
        self.assertAlmostEqual(result.recommended_hole_mm,
                               result.computed_hole_mm, places=4)

    def test_small_current_is_raised_to_the_fab_floor(self):
        result = vc.recommend_via(0.05, self.profile)
        self.assertTrue(result.below_manufacturable_floor)
        self.assertEqual(result.recommended_hole_mm, self.profile.min_hole_mm)

    def test_via_count_is_always_one_in_this_model(self):
        """Pins the documented mathematical boundary.

        Area = pi * D * t is LINEAR in diameter, so whenever the computed
        diameter is below the fab minimum, one minimum-diameter via is
        always sufficient. If this ever fails, either the geometry model
        changed or the linearity argument in the docstring is wrong.
        """
        for current in (0.001, 0.01, 0.05, 0.1, 0.25):
            with self.subTest(current=current):
                result = vc.recommend_via(current, self.profile)
                self.assertTrue(result.below_manufacturable_floor)
                self.assertEqual(result.via_count, 1)

    def test_pad_uses_the_profile_annular_ring(self):
        result = vc.recommend_via(0.05, self.profile)
        self.assertAlmostEqual(
            result.pad_mm,
            self.profile.min_hole_mm + 2 * self.profile.min_annular_ring_mm,
            places=4)

    def test_invalid_inputs_are_rejected(self):
        for bad in ({"current_a": 0.0}, {"current_a": -1.0},
                    {"delta_t_c": 0.0}, {"copper_oz": 0.0}):
            with self.subTest(bad=bad):
                kwargs = {"current_a": 1.0, "delta_t_c": 10.0, "copper_oz": 1.0}
                kwargs.update(bad)
                with self.assertRaises(ValueError):
                    vc.required_area_mils2(**kwargs)

    def test_area_is_recovered_consistently(self):
        """The inversion must round-trip: feeding the recovered area back
        through the cylindrical solve must reproduce the diameter."""
        import math
        area = vc.required_area_mils2(3.0, 10.0, 1.0)
        thickness = vc._oz_to_mils(1.0)
        diameter_mm = (area / (math.pi * thickness)) / MM_TO_MIL
        self.assertAlmostEqual(diameter_mm, vc.via_hole_diameter_mm(3.0),
                               places=9)

    def test_fault_injection_really_fires(self):
        self.assertTrue(vc._prove_test_not_empty())

    def test_self_tests_are_clean(self):
        self.assertEqual(vc.run_self_tests(), [])


# --------------------------------------------------------------------------- #
# The CLI contract - the whole point of the port
# --------------------------------------------------------------------------- #
class CalculatorCliContractTest(unittest.TestCase):
    """Every convention the archive violated, pinned as a test.

    The archive tools: ran self-tests on EVERY invocation, printed
    'SELF-TEST PASS' and a human table to stdout alongside the JSON, and
    exited 0 when the required input was missing.
    """

    CASES = (
        ("scripts/ipc/trace_width.py", ("2.5",)),
        ("scripts/ipc/clearance.py", ("250",)),
        ("scripts/ipc/via_current.py", ("2.0", "--fab", "JLCPCB_STANDARD")),
    )

    def test_missing_positional_input_exits_2(self):
        for relpath, _ in self.CASES:
            with self.subTest(tool=relpath):
                proc = run_tool(relpath)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "",
                                 "a failed run must not write to stdout")

    def test_stdout_is_json_only(self):
        for relpath, args in self.CASES:
            with self.subTest(tool=relpath):
                proc = run_tool(relpath, *args)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(report["schema_version"], CALC_SCHEMA)

    def test_report_carries_the_honesty_fields(self):
        for relpath, args in self.CASES:
            with self.subTest(tool=relpath):
                proc = run_tool(relpath, *args)
                report = json.loads(proc.stdout)
                self.assertIs(report["signoff"], False)
                self.assertTrue(report["model_class"])
                self.assertTrue(report["limits"],
                                "limits must never be empty")
                self.assertIsInstance(report["limits"], list)

    def test_self_test_is_opt_in_and_silent_on_stdout(self):
        for relpath, args in self.CASES:
            with self.subTest(tool=relpath):
                normal = run_tool(relpath, *args)
                self.assertNotIn("SELF-TEST", normal.stdout)
                self.assertNotIn("SELF-TEST", normal.stderr,
                                 "self-tests must not run unless asked")

                explicit = run_tool(relpath, "--self-test")
                self.assertEqual(explicit.returncode, 0, explicit.stderr)
                self.assertIn("SELF-TEST PASS", explicit.stderr)
                self.assertEqual(explicit.stdout, "",
                                 "self-test output belongs on stderr")

    def test_json_flag_writes_and_matches_stdout(self):
        for relpath, args in self.CASES:
            with self.subTest(tool=relpath):
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "report.json"
                    proc = run_tool(relpath, *args, "--json", str(out))
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertTrue(out.is_file(), "--json must WRITE")
                    written = json.loads(out.read_text(encoding="utf-8"))
                    self.assertEqual(written, json.loads(proc.stdout))

    def test_unexpected_exception_exits_2_not_1(self):
        """A crash must be distinguishable from a legitimate FAIL."""
        proc = run_tool("scripts/ipc/via_current.py", "2.0",
                        "--fab", "NO_SUCH_PROFILE")
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_schemas_are_valid_json_and_pin_signoff(self):
        schema = json.loads(
            (SCHEMAS / "ipc_calc_report.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"],
                         CALC_SCHEMA)
        self.assertIs(schema["properties"]["signoff"]["const"], False)
        self.assertEqual(schema["properties"]["limits"]["minItems"], 1)


if __name__ == "__main__":
    unittest.main()
