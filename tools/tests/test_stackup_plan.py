#!/usr/bin/env python3
"""CLI contract tests for scripts/stackup_plan.py.

The archive had NO CLI: ``pcb_stackup_planner.py`` ran a hardcoded demo from
``__main__`` and printed 47 lines. These tests pin the bundle contract that
replaced it — positional required input, opt-in self-test, stdout is JSON only,
``--json`` writes.
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

SCRIPT = SCRIPTS / "stackup_plan.py"

CLEAN_INTENT = {
    "board": "demo",
    "nets": [
        {"name": "3V3", "kind": "POWER", "max_current_a": 1.5},
        {"name": "GND", "kind": "GND"},
        {"name": "LED1", "kind": "SLOW_DIGITAL"},
    ],
    "components": [
        {"name": "U2_PMIC", "package": "QFN", "pin_count": 32,
         "power_pin_count": 2, "decoupling_cap_count": 2},
    ],
    "constraints": {"target_thickness_mm": 1.6},
}

FAILING_INTENT = {
    "nets": [
        {"name": "GND", "kind": "GND"},
        {"name": "CLK", "kind": "FAST_DIGITAL"},
    ],
    "components": [
        {"name": "U1", "package": "QFN", "pin_count": 32,
         "power_pin_count": 4, "decoupling_cap_count": 1},
    ],
    "thermal": [
        {"name": "U3", "dissipation_w": 4.0, "thermal_via_count": 2},
    ],
}

# Every optional section declared, every check clean. This is the ONLY shape
# that can reach GREEN: the status rollup demands full coverage, not merely an
# absence of FAILs, so an intent that leaves a section out (CLEAN_INTENT above)
# is NO_COVERAGE by construction. Kept beside CLEAN_INTENT so the difference
# between "nothing failed" and "everything was measured" stays visible.
FULLY_DECLARED_INTENT = {
    "board": "fully-declared",
    "nets": CLEAN_INTENT["nets"],
    "components": CLEAN_INTENT["components"],
    "constraints": {"target_thickness_mm": 1.6},
    "diff_pairs": [
        {"name": "USB_D", "interface": "USB2_0",
         "length_p_mm": 20.0, "length_n_mm": 20.0},
    ],
    "vias": [
        {"net_name": "LED1", "start_layer": 1, "end_layer": 2,
         "data_rate_gbps": 1.0},
    ],
    "physical_vias": [
        {"net_name": "3V3", "start_layer": 1, "end_layer": 2,
         "kind": "THROUGH_HOLE", "drill_diameter_mm": 0.3,
         "pad_diameter_mm": 0.5},
    ],
    "antennas": [
        {"name": "ANT1", "band": "WIFI_BLE_2G4",
         "edge_clearance_mm": 5.0, "nearest_copper_mm": 6.0},
    ],
    "crystals": [
        {"name": "Y1", "ic_pin_distance_mm": 5.0,
         "noise_source_distance_mm": 6.0},
    ],
    "rf_traces": [
        {"name": "ANT_FEED", "frequency_ghz": 2.4, "via_pitch_mm": 1.0},
    ],
    "isolation": [
        {"name": "USB_SHIELD", "voltage_delta_v": 12.0,
         "design_clearance_mm": 5.0},
    ],
    "high_current_traces": [
        {"name": "3V3", "current_a": 1.0, "width_mm": 2.0},
    ],
    "thermal": [
        {"name": "U2_PMIC", "dissipation_w": 0.3, "thermal_via_count": 4},
    ],
    "design_rules": {"track_mm": 0.2, "clearance_mm": 0.2, "hole_mm": 0.4},
}

UNRESOLVABLE_INTENT = {
    "nets": [{"name": "GND", "kind": "GND"}],
    "components": [{"name": "U1", "package": "FINE_PITCH_BGA",
                    "pin_count": 200}],
    "constraints": {"target_thickness_mm": 0.8},
}


def run(*args):
    # encoding="utf-8" pinned explicitly: report_io's contract is "encoding
    # is always UTF-8", and a report can legitimately contain non-ASCII
    # (e.g. the lambda in a lambda/20 stitch-pitch message). Without this,
    # text=True falls back to the OS locale's codepage to decode the child's
    # stdout, which raises UnicodeDecodeError on any codepage that does not
    # happen to include that character (observed: Turkish cp1254).
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class IntentFile:
    """Context manager: an intent JSON in a throwaway directory."""

    def __init__(self, payload):
        self.payload = payload
        self._tmp = None

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "intent.json"
        path.write_text(json.dumps(self.payload, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


class CliContract(unittest.TestCase):
    def test_missing_positional_input_is_exit_2(self):
        """Never 'print a hint and return 0'."""
        proc = run()
        self.assertEqual(2, proc.returncode)
        self.assertEqual("", proc.stdout)

    def test_nonexistent_input_is_exit_2_with_a_structured_error(self):
        proc = run("/nonexistent/intent.json")
        self.assertEqual(2, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("ERROR", report["status"])
        self.assertIn("INPUT_ERROR", report["reason_codes"])

    def test_partly_declared_intent_is_no_coverage_not_green(self):
        """WAS ``test_clean_intent_exits_zero_and_stdout_is_json``, and it
        pinned the wrong semantics: it asserted GREEN + exit 0 for an intent
        that declares no diff pairs, antennas, crystals, RF traces, isolation,
        high-current traces, thermal items or vias. NINE of the ten checks in
        that run are scanned=0 => NO_COVERAGE (the sibling test
        ``test_undeclared_scopes_are_no_coverage_not_pass`` proves it), and the
        old rollup branched on summary["FAIL"] alone, so "nothing failed" was
        printed as a clean board. NO_COVERAGE is never PASS - not for one check
        (``Finding`` has always refused it) and not for the rollup either."""
        with IntentFile(CLEAN_INTENT) as intent:
            proc = run(str(intent))
        report = json.loads(proc.stdout)
        self.assertEqual("stackup-plan-report/v1", report["schema_version"])
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(0, report["summary"]["FAIL"])
        self.assertGreater(report["summary"]["NO_COVERAGE"], 0)
        self.assertEqual(1, proc.returncode, proc.stderr)
        self.assertEqual(4, report["plan"]["layer_count"])

    def test_a_fully_declared_intent_still_reaches_green(self):
        """The rollup tightened, it did not become unsatisfiable: an intent
        that declares every optional section, with no violation anywhere, is
        still GREEN and still exits 0."""
        with IntentFile(FULLY_DECLARED_INTENT) as intent:
            proc = run(str(intent), "--fab-profile", "JLCPCB_STANDARD")
        report = json.loads(proc.stdout)
        self.assertEqual("GREEN", report["status"], report["checks"])
        self.assertEqual(0, report["summary"]["NO_COVERAGE"])
        self.assertEqual(0, report["summary"]["FAIL"])
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_report_is_labelled_screening_only(self):
        with IntentFile(CLEAN_INTENT) as intent:
            report = json.loads(run(str(intent)).stdout)
        self.assertEqual("exploratory_closed_form", report["model_class"])
        self.assertIs(False, report["signoff"])

    def test_a_failing_check_exits_one(self):
        with IntentFile(FAILING_INTENT) as intent:
            proc = run(str(intent))
        self.assertEqual(1, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("RED", report["status"])
        self.assertGreater(report["summary"]["FAIL"], 0)

    def test_unsatisfiable_constraints_exit_one_as_unresolved(self):
        """No stackup is not a pass."""
        with IntentFile(UNRESOLVABLE_INTENT) as intent:
            proc = run(str(intent), "--max-attempts", "3")
        self.assertEqual(1, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("UNRESOLVED", report["status"])
        self.assertIsNone(report["plan"])
        self.assertIsNotNone(report["plan_error"])

    def test_empty_netlist_is_rejected_not_planned(self):
        with IntentFile({"nets": []}) as intent:
            proc = run(str(intent))
        self.assertEqual(2, proc.returncode)

    def test_unknown_enum_member_is_rejected(self):
        payload = {"nets": [{"name": "X", "kind": "FASTT_DIGITAL"}]}
        with IntentFile(payload) as intent:
            proc = run(str(intent))
        self.assertEqual(2, proc.returncode)
        self.assertIn("SignalKind", proc.stdout)

    def test_unknown_field_is_rejected(self):
        payload = {"nets": [{"name": "X", "kind": "GND", "typo": 1}]}
        with IntentFile(payload) as intent:
            self.assertEqual(2, run(str(intent)).returncode)

    def test_json_flag_writes_the_same_report(self):
        with IntentFile(CLEAN_INTENT) as intent:
            out = intent.with_name("report.json")
            proc = run(str(intent), "--json", str(out))
            # Exit 1, not 0: CLEAN_INTENT leaves nine checks unmeasured, so the
            # verdict is NO_COVERAGE. The point of THIS test is that --json
            # writes the identical report whatever the verdict is.
            self.assertEqual(1, proc.returncode, proc.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_json_flag_never_reads_the_input(self):
        """--json is an OUTPUT path in every tool in this bundle."""
        with IntentFile(CLEAN_INTENT) as intent:
            proc = run("--json", str(intent))
        self.assertEqual(2, proc.returncode)   # still no positional input

    def test_stdout_is_pure_json_diagnostics_go_to_stderr(self):
        with IntentFile(UNRESOLVABLE_INTENT) as intent:
            proc = run(str(intent), "--max-attempts", "3")
        json.loads(proc.stdout)               # must not raise
        self.assertIn("UNRESOLVED", proc.stderr)

    def test_fab_profile_adds_a_dfm_check(self):
        payload = dict(CLEAN_INTENT)
        payload["design_rules"] = {"track_mm": 0.05, "clearance_mm": 0.2,
                                   "hole_mm": 0.4}
        with IntentFile(payload) as intent:
            proc = run(str(intent), "--fab-profile", "JLCPCB_STANDARD")
        report = json.loads(proc.stdout)
        checks = {c["check"]: c for c in report["checks"]}
        self.assertIn("fab_dfm_limits", checks)
        self.assertEqual("FAIL", checks["fab_dfm_limits"]["status"])
        self.assertEqual(1, proc.returncode)

    def test_incomplete_design_rules_leave_a_no_coverage_finding(self):
        """A skipped check must still appear in ``checks``. It used to be
        dropped entirely - the branch wrote one diag() to stderr and appended
        nothing - so a run that ASKED for a fab DFM check and did not get one
        was indistinguishable from a run that never asked. An absent finding
        cannot even be counted as NO_COVERAGE."""
        payload = dict(CLEAN_INTENT)
        payload["design_rules"] = {"track_mm": 0.2}   # clearance_mm/hole_mm missing
        with IntentFile(payload) as intent:
            proc = run(str(intent), "--fab-profile", "JLCPCB_STANDARD")
        report = json.loads(proc.stdout)
        checks = {c["check"]: c for c in report["checks"]}
        self.assertIn("fab_dfm_limits", checks)
        self.assertEqual("NO_COVERAGE", checks["fab_dfm_limits"]["status"])
        self.assertEqual(0, checks["fab_dfm_limits"]["scanned"])
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(1, proc.returncode)

    def test_unknown_fab_profile_is_exit_2(self):
        with IntentFile(CLEAN_INTENT) as intent:
            proc = run(str(intent), "--fab-profile", "NO_SUCH_FAB")
        self.assertEqual(2, proc.returncode)

    def test_undeclared_scopes_are_no_coverage_not_pass(self):
        with IntentFile(CLEAN_INTENT) as intent:
            report = json.loads(run(str(intent)).stdout)
        by_check = {c["check"]: c for c in report["checks"]}
        for name in ("antenna_keepout", "crystal_placement",
                     "creepage_clearance", "thermal_vias",
                     "via_type_aspect_ratio"):
            with self.subTest(check=name):
                self.assertEqual("NO_COVERAGE", by_check[name]["status"])
                self.assertEqual(0, by_check[name]["scanned"])

    def test_negative_parameters_are_rejected(self):
        with IntentFile(CLEAN_INTENT) as intent:
            self.assertEqual(2, run(str(intent), "--copper-oz", "0").returncode)
            self.assertEqual(2, run(str(intent), "--delta-t-c", "-5").returncode)

    def test_physical_via_over_its_type_ceiling_fails(self):
        payload = dict(CLEAN_INTENT)
        payload["physical_vias"] = [
            {"net_name": "BGA_D0", "start_layer": 1, "end_layer": 2,
             "kind": "MICROVIA", "drill_diameter_mm": 0.05,
             "pad_diameter_mm": 0.2},
        ]
        with IntentFile(payload) as intent:
            proc = run(str(intent))
        report = json.loads(proc.stdout)
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual("FAIL", checks["via_type_aspect_ratio"]["status"])
        self.assertEqual(1, proc.returncode)

    def test_physical_via_within_its_type_ceiling_passes(self):
        """The CHECK passes; the RUN still exits 1. The old assertion here was
        ``exit 0``, which quietly depended on the broken rollup: this payload
        is CLEAN_INTENT plus one via, so the other undeclared sections remain
        NO_COVERAGE and the report as a whole is NO_COVERAGE. A per-check PASS
        is what this test is about, and that is asserted directly."""
        payload = dict(CLEAN_INTENT)
        payload["physical_vias"] = [
            {"net_name": "PWR", "start_layer": 1, "end_layer": 2,
             "kind": "THROUGH_HOLE", "drill_diameter_mm": 0.3,
             "pad_diameter_mm": 0.5},
        ]
        with IntentFile(payload) as intent:
            proc = run(str(intent))
        report = json.loads(proc.stdout)
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual("PASS", checks["via_type_aspect_ratio"]["status"])
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(1, proc.returncode)

    def test_unknown_via_type_kind_is_rejected(self):
        payload = dict(CLEAN_INTENT)
        payload["physical_vias"] = [
            {"net_name": "X", "start_layer": 1, "end_layer": 2,
             "kind": "NOT_A_VIA_TYPE", "drill_diameter_mm": 0.3,
             "pad_diameter_mm": 0.5},
        ]
        with IntentFile(payload) as intent:
            proc = run(str(intent))
        self.assertEqual(2, proc.returncode)
        self.assertIn("ViaTypeDetail", proc.stdout)


class SelfTest(unittest.TestCase):
    def test_self_test_flag_exits_zero(self):
        proc = run("--self-test")
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_self_test_writes_nothing_to_stdout(self):
        proc = run("--self-test")
        self.assertEqual("", proc.stdout)
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_self_test_does_not_run_during_a_normal_plan(self):
        """Opt-in. zdiff_solver used to run its self-tests on every call."""
        with IntentFile(CLEAN_INTENT) as intent:
            proc = run(str(intent))
        self.assertNotIn("SELF-TEST", proc.stdout)
        self.assertNotIn("SELF-TEST", proc.stderr)


class SchemaAgreement(unittest.TestCase):
    """No jsonschema dependency: check the shape the schema pins by hand."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (SCHEMAS / "stackup_plan.schema.json").read_text(encoding="utf-8"))
        with IntentFile(CLEAN_INTENT) as intent:
            cls.report = json.loads(run(str(intent)).stdout)

    def test_schema_version_constants_agree(self):
        self.assertEqual(self.schema["properties"]["schema_version"]["const"],
                         self.report["schema_version"])

    def test_every_required_key_is_present(self):
        for key in self.schema["required"]:
            with self.subTest(key=key):
                self.assertIn(key, self.report)

    def test_no_key_outside_the_schema(self):
        allowed = set(self.schema["properties"])
        self.assertEqual(set(), set(self.report) - allowed)

    def test_status_is_an_allowed_value(self):
        self.assertIn(self.report["status"],
                      self.schema["properties"]["status"]["enum"])

    def test_layer_roles_are_the_documented_tokens(self):
        allowed = set(self.schema["$defs"]["layer"]["properties"]
                      ["roles"]["items"]["enum"])
        for layer in self.report["plan"]["layers"]:
            with self.subTest(layer=layer["index"]):
                self.assertTrue(set(layer["roles"]) <= allowed)


if __name__ == "__main__":
    unittest.main()
