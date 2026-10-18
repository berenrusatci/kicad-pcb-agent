#!/usr/bin/env python3
"""Tests for scripts/testpoint_plan.py. Pure logic, no pcbnew needed."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS))

import testpoint_plan as mod  # noqa: E402

try:
    import jsonschema
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


def _rail_tree():
    return {"3V3": {"vout": 3.3}, "1V2_CORE": {"vout": 1.2}}


class DeriveMandatoryTestpointsTests(unittest.TestCase):
    def test_one_testpoint_per_rail_plus_default_debug_nets(self):
        tps = mod.derive_mandatory_testpoints(_rail_tree())
        power = [t for t in tps if t["net_class"] == "POWER"]
        debug = [t for t in tps if t["net_class"] == "DEBUG"]
        self.assertEqual(2, len(power))
        self.assertEqual(len(mod.DEFAULT_DEBUG_NETS), len(debug))

    def test_custom_debug_nets_override_the_default_list(self):
        tps = mod.derive_mandatory_testpoints(_rail_tree(), debug_nets=["CUSTOM_NET"])
        debug_names = {t["net"] for t in tps if t["net_class"] == "DEBUG"}
        self.assertEqual({"CUSTOM_NET"}, debug_names)

    def test_invalid_method_raises(self):
        with self.assertRaises(ValueError):
            mod.derive_mandatory_testpoints(_rail_tree(), default_method="not_a_method")

    def test_every_testpoint_carries_a_valid_method(self):
        tps = mod.derive_mandatory_testpoints(_rail_tree(), default_method="flying_probe")
        self.assertTrue(all(t["method"] == "flying_probe" for t in tps))


class CoverageGapTests(unittest.TestCase):
    def test_full_coverage_has_no_findings(self):
        tps = mod.derive_mandatory_testpoints(_rail_tree())
        gaps = mod.check_mandatory_scope_coverage(tps, 2, list(mod.DEFAULT_DEBUG_NETS))
        self.assertEqual([], gaps)

    def test_undercounted_rails_are_flagged(self):
        tps = mod.derive_mandatory_testpoints(_rail_tree())  # only 2 rails
        gaps = mod.check_mandatory_scope_coverage(tps, 5, list(mod.DEFAULT_DEBUG_NETS))
        self.assertTrue(any("power rail" in g.lower() or "MISSING" in g for g in gaps))

    def test_missing_debug_net_is_flagged(self):
        tps = mod.derive_mandatory_testpoints(_rail_tree(), debug_nets=["SWDIO"])
        gaps = mod.check_mandatory_scope_coverage(tps, 2, ["SWDIO", "SWCLK"])
        self.assertTrue(any("SWCLK" in g for g in gaps))


class BuildTestpointPlanTests(unittest.TestCase):
    def test_phase_is_always_early_schematic(self):
        plan = mod.build_testpoint_plan(_rail_tree())
        self.assertEqual("early_schematic", plan["phase"])

    def test_full_coverage_is_screen_only_not_green(self):
        """Even fully-covered mandatory scope must not claim GREEN at this
        phase - nothing here is placement/accessibility verified."""
        plan = mod.build_testpoint_plan(_rail_tree())
        self.assertEqual("SCREEN_ONLY", plan["status"])

    def test_coverage_block_arithmetic_is_self_consistent(self):
        plan = mod.build_testpoint_plan(_rail_tree())
        cov = plan["coverage"]
        self.assertEqual(cov["eligible"], cov["scanned"] + cov["excluded"])

    def test_mandatory_scope_has_all_five_required_keys(self):
        plan = mod.build_testpoint_plan(_rail_tree())
        scope = plan["mandatory_scope"]
        for key in ("power_rails", "swd", "uart", "boot_strap", "pmic_enable_pg"):
            self.assertIn(key, scope)
            self.assertIn("required", scope[key])
            self.assertIn("covered", scope[key])

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema not installed")
    def test_output_validates_against_the_real_schema(self):
        schema = json.loads(
            (SCHEMAS.parent.parent / "skills" / "dft-testpoints" / "references" /
             "testpoint_plan.schema.json").read_text(encoding="utf-8"))
        plan = mod.build_testpoint_plan(_rail_tree())
        jsonschema.validate(plan, schema)  # raises on failure


class BringupChecklistTests(unittest.TestCase):
    def test_rail_order_is_enforced_in_output(self):
        plan = mod.build_testpoint_plan(_rail_tree())
        checklist = mod.generate_bringup_checklist(plan["testpoints"], ["1V2_CORE", "3V3"])
        self.assertLess(checklist.index("1V2_CORE"), checklist.index("3V3"))

    def test_debug_nets_appear_in_their_own_section(self):
        plan = mod.build_testpoint_plan(_rail_tree())
        checklist = mod.generate_bringup_checklist(plan["testpoints"], ["3V3", "1V2_CORE"])
        self.assertIn("SWDIO", checklist)
        self.assertIn("Debug/Boot Access", checklist)

    def test_tolerance_value_is_reflected_in_the_note(self):
        plan = mod.build_testpoint_plan(_rail_tree())
        checklist = mod.generate_bringup_checklist(plan["testpoints"], ["3V3"], tolerance_pct=10.0)
        self.assertIn("10%", checklist)


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = SCRIPTS / "testpoint_plan.py"

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

    def test_rail_tree_json_produces_a_screen_only_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rail_tree.json"
            path.write_text(json.dumps({"3V3": {"vout": 3.3}}), encoding="utf-8")
            proc = self.run_cli(str(path))
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            plan = json.loads(proc.stdout)
            self.assertEqual("SCREEN_ONLY", plan["status"])

    def test_bringup_checklist_mode_writes_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            out_path = Path(tmp) / "checklist.md"
            plan_path.write_text(json.dumps(mod.build_testpoint_plan(_rail_tree())),
                                 encoding="utf-8")
            proc = self.run_cli("--bringup-checklist", str(plan_path),
                                "--rail-order", "3V3,1V2_CORE", "--out", str(out_path))
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            self.assertTrue(out_path.exists())


if __name__ == "__main__":
    unittest.main()
