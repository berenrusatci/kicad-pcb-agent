#!/usr/bin/env python3
"""Tests for scripts/pmic_allocation.py. Pure logic, no pcbnew needed."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS))

import pmic_allocation as mod  # noqa: E402
from common.stackup import MechanicalThermalLimits  # noqa: E402


def _pmic():
    return mod.PmicProfile("TEST_PMIC", [
        mod.PmicOutput("BUCK1", "BUCK", max_current_ma=2000, nominal_voltage_v=3.3),
        mod.PmicOutput("BUCK2", "BUCK", max_current_ma=500, nominal_voltage_v=1.8),
        mod.PmicOutput("LDO1", "LDO", max_current_ma=300, nominal_voltage_v=1.2),
    ])


class VoltageCompatibilityTests(unittest.TestCase):
    def test_within_tolerance_is_compatible(self):
        self.assertTrue(mod._voltage_compatible(3.3, 3.3))
        self.assertTrue(mod._voltage_compatible(3.3, 3.3 * 1.04))

    def test_outside_tolerance_is_not_compatible(self):
        self.assertFalse(mod._voltage_compatible(3.3, 1.8))

    def test_zero_target_is_never_compatible(self):
        self.assertFalse(mod._voltage_compatible(0.0, 0.0))


class RailAllocationTests(unittest.TestCase):
    def test_clean_allocation_passes(self):
        pmic = _pmic()
        needs = [mod.RailNeed("3V3_MAIN", 3.3, 1000.0)]
        finding = mod.check_rail_allocation(pmic, needs)
        self.assertEqual("PASS", finding["status"])
        self.assertEqual("BUCK1", needs[0].source)

    def test_no_needs_is_no_coverage(self):
        finding = mod.check_rail_allocation(_pmic(), [])
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_unsupported_voltage_is_not_force_matched(self):
        """A rail needing 5V must NOT be matched to the closest available
        output (3.3V) just because it's the least-bad option - it must be
        UNALLOCATED."""
        pmic = _pmic()
        needs = [mod.RailNeed("5V_UNSUPPORTED", 5.0, 100.0)]
        finding = mod.check_rail_allocation(pmic, needs)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(mod.UNALLOCATED, needs[0].source)
        self.assertEqual("rail_unallocated", finding["violations"][0]["kind"])

    def test_insufficient_current_is_not_matched_even_at_right_voltage(self):
        pmic = _pmic()
        needs = [mod.RailNeed("1V2_HUNGRY", 1.2, 400.0)]  # LDO1 only has 300mA
        finding = mod.check_rail_allocation(pmic, needs)
        self.assertEqual(mod.UNALLOCATED, needs[0].source)

    def test_closest_voltage_wins_among_candidates(self):
        pmic = mod.PmicProfile("P", [
            mod.PmicOutput("A", "BUCK", 1000, 3.25),
            mod.PmicOutput("B", "BUCK", 1000, 3.35),
        ])
        needs = [mod.RailNeed("R", 3.3, 100.0)]
        mod.check_rail_allocation(pmic, needs)
        # both are within 5% tolerance of 3.3; A (3.25) is closer than B (3.35)
        self.assertEqual("A", needs[0].source)

    def test_output_overload_is_caught_by_second_pass(self):
        """The greedy pass alone can never overbook an output (a candidate
        without enough headroom is filtered out) - the overload check
        exists for a rail whose `source` was pre-filled externally to a
        real output name, which the greedy pass must respect rather than
        silently reassign."""
        pmic = _pmic()  # BUCK1 max 2000mA
        needs = [mod.RailNeed("R1", 3.3, 1500.0, source="BUCK1"),
                mod.RailNeed("R2", 3.3, 1500.0, source="BUCK1")]
        finding = mod.check_rail_allocation(pmic, needs)
        kinds = {v["kind"] for v in finding["violations"]}
        self.assertIn("output_overloaded", kinds)
        self.assertEqual("BUCK1", needs[0].source)
        self.assertEqual("BUCK1", needs[1].source)

    def test_preassigned_source_is_respected_not_overwritten(self):
        pmic = _pmic()
        needs = [mod.RailNeed("R1", 3.3, 100.0, source="BUCK1")]
        mod.check_rail_allocation(pmic, needs)
        self.assertEqual("BUCK1", needs[0].source)

    def test_preassigned_current_is_reserved_for_the_greedy_pass(self):
        """A pre-filled rail's current must reduce the output's remaining
        headroom for OTHER rails processed by the greedy pass."""
        pmic = mod.PmicProfile("P", [mod.PmicOutput("BUCK1", "BUCK", 1000, 3.3)])
        needs = [mod.RailNeed("R1", 3.3, 900.0, source="BUCK1"),
                mod.RailNeed("R2", 3.3, 200.0)]  # only 100mA left on BUCK1
        mod.check_rail_allocation(pmic, needs)
        self.assertEqual("BUCK1", needs[0].source)
        self.assertEqual(mod.UNALLOCATED, needs[1].source)

    def test_greedy_current_accounting_prevents_double_booking(self):
        """The second rail must not be allowed onto BUCK1 once the first
        rail has consumed most of its headroom - it should overflow to
        UNALLOCATED (BUCK1 is the only 3.3V-compatible output here)."""
        pmic = mod.PmicProfile("P", [mod.PmicOutput("BUCK1", "BUCK", 1000, 3.3)])
        needs = [mod.RailNeed("R1", 3.3, 900.0), mod.RailNeed("R2", 3.3, 900.0)]
        mod.check_rail_allocation(pmic, needs)
        self.assertEqual("BUCK1", needs[0].source)
        self.assertEqual(mod.UNALLOCATED, needs[1].source)


class SupplementaryHeatTests(unittest.TestCase):
    def test_heat_estimate_is_positive_for_real_load(self):
        heat = mod.supplementary_dissipation_w(3.3, 500.0)
        self.assertGreater(heat, 0.0)

    def test_thermal_limits_integration_flags_exceeded_budget(self):
        pmic = _pmic()
        needs = [mod.RailNeed("5V_UNSUPPORTED", 5.0, 5000.0)]  # big load -> big heat estimate
        limits = MechanicalThermalLimits(max_dissipation_w=0.01)
        finding = mod.check_rail_allocation(pmic, needs, limits)
        self.assertIn("EXCEEDS", finding["detail"])

    def test_thermal_limits_integration_reports_within_budget(self):
        pmic = _pmic()
        needs = [mod.RailNeed("5V_UNSUPPORTED", 5.0, 1.0)]  # tiny load -> tiny heat estimate
        limits = MechanicalThermalLimits(max_dissipation_w=100.0)
        finding = mod.check_rail_allocation(pmic, needs, limits)
        self.assertIn("Stays within", finding["detail"])


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = SCRIPTS / "pmic_allocation.py"

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

    def test_clean_intent_file_exits_zero(self):
        intent = {
            "pmic": {"name": "T", "outputs": [
                {"name": "BUCK1", "kind": "BUCK", "max_current_ma": 2000, "nominal_voltage_v": 3.3},
            ]},
            "rails": [{"rail_name": "3V3", "voltage_v": 3.3, "estimated_current_ma": 500}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intent.json"
            path.write_text(json.dumps(intent), encoding="utf-8")
            proc = self.run_cli(str(path))
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual("BUCK1", report["allocations"][0]["source"])


if __name__ == "__main__":
    unittest.main()
