#!/usr/bin/env python3
"""Tests for scripts/sim/ngspice.py.

Converted from the archived pytest suite to unittest.

MOCKING POLICY, STATED IN THE TEST NAMES
----------------------------------------
Every test whose name contains ``_binary_not_exercised`` replaces the ngspice
executable with a stub. Those tests prove the BRIDGE logic - argument
building, parsing, acceptance - and prove NOTHING about ngspice itself.

``RealNgspiceBinary`` runs the actual solver and is skipped when it is not
installed; that class is the only place where a pass is evidence the bridge
works end to end.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCHEMAS, SCRIPTS  # noqa: E402

SIM_SCRIPTS = SCRIPTS / "sim"
sys.path.insert(0, str(SIM_SCRIPTS))
import ngspice as mod  # noqa: E402

HAVE_NGSPICE = mod.find_ngspice() is not None

# Verbatim ngspice batch output shapes.
SWEEP_OUTPUT = mod.SAMPLE_OUTPUT
OP_OUTPUT = """\
Circuit: * t

Doing analysis at TEMP = 27.000000 and TNOM = 27.000000

No. of Data Rows : 1
v(vout) = 4.545455e+00
v(vin) = 5.000000e+00
"""


class Analyses(unittest.TestCase):
    def test_op_line(self):
        self.assertEqual(mod.OpAnalysis().spice_line(), ".op")

    def test_dc_line(self):
        self.assertEqual(mod.DcSweep("V1", 0, 5, 0.5).spice_line(),
                         ".dc V1 0 5 0.5")

    def test_ac_line(self):
        self.assertEqual(mod.AcSweep(10, 1e6, 20, "dec").spice_line(),
                         ".ac dec 20 10 1000000.0")

    def test_an_invalid_ac_kind_is_refused(self):
        with self.assertRaises(ValueError):
            mod.AcSweep(10, 1e6, 20, "quadratic").spice_line()

    def test_tran_line_without_start(self):
        self.assertEqual(mod.TranAnalysis(1e-6, 1e-3).spice_line(),
                         ".tran 1e-06 0.001")

    def test_tran_line_with_start(self):
        self.assertEqual(mod.TranAnalysis(1e-6, 1e-3, 5e-4).spice_line(),
                         ".tran 1e-06 0.001 0.0005")


class OutputParsing(unittest.TestCase):
    def test_the_sweep_table_is_parsed(self):
        sim = mod.parse_output(SWEEP_OUTPUT)
        self.assertEqual(sim.rows, 3)
        self.assertEqual(sorted(sim.data), ["v(vin)", "v(vout)"])

    def test_minimum_and_last_differ(self):
        sim = mod.parse_output(SWEEP_OUTPUT)
        self.assertEqual(sim.minimum("v(vout)"), 3.1)
        self.assertEqual(sim.last("v(vout)"), 3.1)
        self.assertEqual(sim.minimum("v(vin)"), 4.0)

    def test_the_sweep_axis_is_kept(self):
        sim = mod.parse_output(SWEEP_OUTPUT)
        self.assertEqual([f for f, _ in sim.data["v(vout)"]], [0.0, 0.5, 1.0])

    def test_the_op_scalar_form_is_parsed(self):
        """ngspice prints `v(vout) = 4.545455e+00` for .op, not a table. The
        archived parser understood only the table and silently returned zero
        rows for the analysis its own docstring called the most direct rail
        check. Measured against ngspice-46."""
        sim = mod.parse_output(OP_OUTPUT)
        self.assertEqual(sim.rows, 2)
        self.assertEqual(sim.minimum("v(vout)"), 4.545455)
        self.assertEqual(sim.minimum("v(vin)"), 5.0)

    def test_empty_output_yields_no_rows(self):
        self.assertEqual(mod.parse_output("").rows, 0)
        self.assertEqual(mod.parse_output("nothing useful here\n").rows, 0)

    def test_the_raw_output_is_retained_for_the_report(self):
        self.assertIn("v-sweep", mod.parse_output(SWEEP_OUTPUT).raw)

    def test_a_missing_node_returns_none_not_zero(self):
        sim = mod.parse_output(SWEEP_OUTPUT)
        self.assertIsNone(sim.minimum("v(nowhere)"))
        self.assertIsNone(sim.last("v(nowhere)"))


class RailTargets(unittest.TestCase):
    def test_the_tolerance_derives_the_lower_limit(self):
        self.assertAlmostEqual(
            mod.RailTarget("vout", 3.3, tolerance_pct=5.0).lower_limit_v,
            3.135, places=6)

    def test_an_explicit_minimum_overrides_the_tolerance(self):
        target = mod.RailTarget("vout", 3.3, tolerance_pct=50.0,
                                min_accept_v=3.2)
        self.assertEqual(target.lower_limit_v, 3.2)

    def test_cli_rail_spellings(self):
        self.assertEqual(mod._parse_rail("vout=3.3").nominal_v, 3.3)
        self.assertEqual(mod._parse_rail("vout=3.3:2").tolerance_pct, 2.0)
        self.assertEqual(mod._parse_rail("vout=3.3@3.2").min_accept_v, 3.2)
        with self.assertRaises(ValueError):
            mod._parse_rail("vout")


class VoltageAcceptance(unittest.TestCase):
    def setUp(self):
        self.sim = mod.parse_output(SWEEP_OUTPUT)

    def test_a_generous_tolerance_passes(self):
        finding = mod.check_voltage_drop(
            self.sim, [mod.RailTarget("vout", 3.3, tolerance_pct=10.0)])
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 1)

    def test_fault_injection_a_limit_above_the_minimum_must_fail(self):
        finding = mod.check_voltage_drop(
            self.sim, [mod.RailTarget("vout", 3.3, min_accept_v=3.25)])
        self.assertEqual(finding["status"], "FAIL")
        self.assertAlmostEqual(finding["violations"][0]["measured_min_v"], 3.1)
        self.assertAlmostEqual(finding["violations"][0]["drop_mv"], 200.0,
                               places=3)

    def test_the_worst_case_minimum_is_used_not_the_last_value(self):
        """A rail that dips and recovers has still reset the MCU."""
        dipping = mod.parse_output(
            "Index   v-sweep   v(vout)\n"
            "----\n0\t0.0\t3.30\n1\t0.5\t2.90\n2\t1.0\t3.30\n")
        finding = mod.check_voltage_drop(
            dipping, [mod.RailTarget("vout", 3.3, min_accept_v=3.0)])
        self.assertEqual(finding["status"], "FAIL")

    def test_a_missing_ngspice_is_no_coverage_and_invents_nothing(self):
        finding = mod.check_voltage_drop(None, [mod.RailTarget("vout", 3.3)])
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertEqual(finding["violations"], [])
        self.assertIn("DID NOT RUN", finding["detail"])

    def test_empty_output_is_no_coverage(self):
        finding = mod.check_voltage_drop(mod.parse_output(""),
                                         [mod.RailTarget("vout", 3.3)])
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_an_unwatched_node_is_a_violation_not_lost_coverage(self):
        """Otherwise forgetting to watch a node silently hides its result."""
        finding = mod.check_voltage_drop(self.sim,
                                         [mod.RailTarget("v3v3", 3.3)])
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["scanned"], 1)
        self.assertIn("never added", finding["violations"][0]["reason"])

    def test_a_bare_node_name_resolves_to_the_v_wrapped_key(self):
        finding = mod.check_voltage_drop(
            self.sim, [mod.RailTarget("vout", 3.3, tolerance_pct=10.0)])
        self.assertEqual(finding["status"], "PASS")


class DecouplingSuggestions(unittest.TestCase):
    def test_pass_produces_no_suggestions(self):
        sim = mod.parse_output(SWEEP_OUTPUT)
        finding = mod.check_voltage_drop(
            sim, [mod.RailTarget("vout", 3.3, tolerance_pct=10.0)])
        self.assertEqual([], mod.decoupling_suggestion(finding))

    def test_no_coverage_produces_no_suggestions(self):
        finding = mod.check_voltage_drop(None, [mod.RailTarget("vout", 3.3)])
        self.assertEqual([], mod.decoupling_suggestion(finding))

    def test_a_mild_drop_suggests_the_smallest_tier(self):
        # sim's vout minimum is fixed at 3.1; nominal_v=3.15 keeps the drop
        # (~1.6%) inside the mildest <=2% tier.
        sim = mod.parse_output(SWEEP_OUTPUT)
        finding = mod.check_voltage_drop(
            sim, [mod.RailTarget("vout", 3.15, min_accept_v=3.12)])
        suggestions = mod.decoupling_suggestion(finding)
        self.assertEqual(1, len(suggestions))
        self.assertEqual("vout", suggestions[0].node)
        self.assertEqual([100e-9], suggestions[0].capacitance_f)

    def test_a_severe_drop_suggests_bulk_capacitance(self):
        severe = mod.parse_output(
            "Index   v-sweep   v(vout)\n----\n0\t0.0\t3.30\n1\t0.5\t2.50\n")
        finding = mod.check_voltage_drop(
            severe, [mod.RailTarget("vout", 3.3, min_accept_v=3.2)])
        suggestions = mod.decoupling_suggestion(finding)
        self.assertEqual([100e-9, 10e-6], suggestions[0].capacitance_f)

    def test_the_default_distance_is_the_one_project_rule(self):
        """REGRESSION. This module used to hold its own 1.5 mm constant under
        a comment claiming it was "one source rather than a second,
        independently-drifting constant" - while decoupling_coverage.py held
        3.0 mm for the same quantity under a comment admitting the two were
        NOT reconciled. Two tools, opposite verdicts about one capacitor.

        There is now one table, so the claim is checkable instead of stated."""
        from common.tuning import decoupling_limits
        limit_mm, _target_mm = decoupling_limits()

        sim = mod.parse_output(SWEEP_OUTPUT)
        finding = mod.check_voltage_drop(
            sim, [mod.RailTarget("vout", 3.3, min_accept_v=3.15)])
        suggestions = mod.decoupling_suggestion(finding)
        self.assertEqual(limit_mm, suggestions[0].max_distance_mm)

    def test_no_module_reintroduces_its_own_decoupling_distance(self):
        """A static guard, because the drift returns the moment somebody adds
        a convenient module-level default back."""
        import re
        from _paths import SCRIPTS
        offenders = []
        for path in SCRIPTS.rglob("*.py"):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"\s*[A-Z_]*DECOUPLING[A-Z_]*(DISTANCE|MM)[A-Z_]*\s*=\s*[\d.]", line):
                    offenders.append(f"{path.name}:{line_no}: {line.strip()}")
        self.assertEqual([], offenders,
                         "decoupling distance belongs in tuning_defaults.json, not in a module")

    def test_an_unwatched_node_violation_produces_no_suggestion(self):
        """Its violation shape has no nominal_v/drop_mv - must not crash or
        fabricate a suggestion from data that is not there."""
        sim = mod.parse_output(SWEEP_OUTPUT)
        finding = mod.check_voltage_drop(sim, [mod.RailTarget("v3v3", 3.3)])
        self.assertEqual([], mod.decoupling_suggestion(finding))

    def test_explicit_distance_override_is_carried_through(self):
        sim = mod.parse_output(SWEEP_OUTPUT)
        finding = mod.check_voltage_drop(
            sim, [mod.RailTarget("vout", 3.3, min_accept_v=3.15)])
        suggestions = mod.decoupling_suggestion(finding, max_distance_mm=3.0)
        self.assertEqual(3.0, suggestions[0].max_distance_mm)


class AcAcceptance(unittest.TestCase):
    OUTPUT = ("Index   frequency   v(out)\n----\n"
              "0\t1.000000e+01\t1.000000e+00\n"
              "1\t1.000000e+02\t5.000000e-01\n"
              "2\t1.000000e+03\t0.000000e+00\n")

    def test_gain_below_the_limit_fails(self):
        sim = mod.parse_output(self.OUTPUT)
        finding = mod.check_ac_band(sim, "out", -3.0, 1.0, 1e4)
        self.assertEqual(finding["status"], "FAIL")
        self.assertAlmostEqual(finding["violations"][0]["gain_db"], -6.021,
                               places=3)

    def test_zero_and_negative_points_are_skipped_not_turned_into_minus_inf(self):
        sim = mod.parse_output(self.OUTPUT)
        finding = mod.check_ac_band(sim, "out", -100.0, 1.0, 1e4)
        self.assertEqual(finding["scanned"], 2)
        self.assertIn("skipped_invalid_points=1", finding["detail"])

    def test_a_band_with_no_usable_point_is_no_coverage(self):
        sim = mod.parse_output(self.OUTPUT)
        finding = mod.check_ac_band(sim, "out", 0.0, 1e9, 1e10)
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_no_simulation_is_no_coverage(self):
        self.assertEqual(mod.check_ac_band(None, "out", 0, 1, 2)["status"],
                         "NO_COVERAGE")


class ModelCoverage(unittest.TestCase):
    def test_kicads_double_underscore_no_model_marker_is_detected(self):
        """MEASURED against real KiCad 10 output. A first draft only looked at
        `X` subckt lines and caught none of the twelve model-less parts."""
        self.assertEqual(
            mod.components_without_model("U4 __U4\nY1 __Y1\nJ1 __J1\n"),
            ["U4", "Y1", "J1"])

    def test_an_x_instance_without_a_subckt_is_detected(self):
        self.assertEqual(
            mod.components_without_model("X1 a b OPAMP\n"), ["X1"])

    def test_an_x_instance_with_a_subckt_is_fine(self):
        netlist = ".subckt OPAMP a b\n.ends\nX1 a b OPAMP\n"
        self.assertEqual(mod.components_without_model(netlist), [])

    def test_an_include_line_vouches_for_external_models(self):
        netlist = '.include "models.lib"\nX1 a b OPAMP\n'
        self.assertEqual(mod.components_without_model(netlist), [])

    def test_ordinary_components_are_not_flagged(self):
        self.assertEqual(
            mod.components_without_model("R1 a b 1k\nC1 a 0 100n\n"), [])

    def test_the_finding_reports_coverage(self):
        finding = mod.check_model_coverage("R1 a b 1k\nU4 __U4\n")
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["scanned"], 2)


class NetlistPreparation(unittest.TestCase):
    def test_the_analysis_lands_before_dot_end(self):
        prepared = mod.add_analysis("* c\nR1 a 0 1\n.end\n",
                                    mod.OpAnalysis(), ["out"])
        self.assertLess(prepared.index(".op"), prepared.index(".end"))
        self.assertEqual(prepared.count(".end"), 2)   # .endc and .end

    def test_watched_nodes_are_wrapped_in_v(self):
        prepared = mod.add_analysis("* c\n", mod.OpAnalysis(), ["out", "v(in)"])
        self.assertIn("print v(out) v(in)", prepared)

    def test_a_simulation_with_nothing_watched_is_refused(self):
        with self.assertRaises(ValueError):
            mod.add_analysis("* c\n", mod.OpAnalysis(), [])


class BinaryDiscovery(unittest.TestCase):
    def test_the_console_build_is_preferred(self):
        """Windows trap: ngspice.exe is the interactive build and hangs on
        every call, including --version."""
        self.assertEqual(mod.NGSPICE_CANDIDATES[0], "ngspice_con")

    def test_an_explicit_name_is_tried_alone(self):
        with mock.patch.object(mod.shutil, "which",
                               side_effect=lambda n: "/x/" + n
                               if n == "ngspice_con" else None):
            self.assertIsNone(mod.find_ngspice("ngspice"))

    def test_an_absent_binary_reports_absent_not_a_version(self):
        with mock.patch.object(mod, "find_ngspice", return_value=None):
            self.assertEqual(mod.ngspice_version(), "ABSENT")

    def test_run_returns_none_when_absent_binary_not_exercised(self):
        with mock.patch.object(mod, "find_ngspice", return_value=None):
            self.assertIsNone(mod.run_ngspice("nowhere.cir"))

    def test_stdin_is_devnull_so_a_wrong_build_times_out_binary_not_exercised(self):
        """Guards against the interactive build hanging the whole run."""
        with tempfile.TemporaryDirectory() as tmp:
            netlist = Path(tmp) / "c.cir"
            netlist.write_text("* c\n.end\n", encoding="utf-8")
            captured = {}

            def fake_run(argv, **kwargs):
                captured.update(kwargs)
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(mod, "find_ngspice", return_value="/bin/true"), \
                 mock.patch.object(mod, "ngspice_version", return_value="stub"), \
                 mock.patch.object(mod.subprocess, "run", side_effect=fake_run):
                mod.run_ngspice(str(netlist))
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        self.assertIn("timeout", captured)


class SimulationBridgeWithAStub(unittest.TestCase):
    """The BRIDGE is exercised here; the real ngspice binary is NOT."""

    def test_batch_mode_flag_is_used_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            netlist = Path(tmp) / "c.cir"
            netlist.write_text("* c\n.end\n", encoding="utf-8")
            seen = {}

            def fake_run(argv, **kwargs):
                seen["argv"] = argv
                return subprocess.CompletedProcess(argv, 0, SWEEP_OUTPUT, "")

            with mock.patch.object(mod, "find_ngspice", return_value="/x/ngspice"), \
                 mock.patch.object(mod, "ngspice_version", return_value="stub-1"), \
                 mock.patch.object(mod.subprocess, "run", side_effect=fake_run):
                sim = mod.run_ngspice(str(netlist))
        self.assertEqual(seen["argv"][:2], ["/x/ngspice", "-b"])
        self.assertEqual(sim.rows, 3)
        self.assertEqual(sim.version, "stub-1")

    def test_a_nonzero_exit_with_usable_output_is_not_a_failure_binary_not_exercised(self):
        """ngspice returns non-zero on convergence warnings while still
        printing usable data; the row count is what counts."""
        with tempfile.TemporaryDirectory() as tmp:
            netlist = Path(tmp) / "c.cir"
            netlist.write_text("* c\n.end\n", encoding="utf-8")
            with mock.patch.object(mod, "find_ngspice", return_value="/x/ng"), \
                 mock.patch.object(mod, "ngspice_version", return_value="stub"), \
                 mock.patch.object(
                     mod.subprocess, "run",
                     return_value=subprocess.CompletedProcess(
                         [], 1, SWEEP_OUTPUT, "warning: convergence")):
                sim = mod.run_ngspice(str(netlist))
        self.assertEqual(sim.rows, 3)

    def test_a_missing_netlist_raises_binary_not_exercised(self):
        with mock.patch.object(mod, "find_ngspice", return_value="/x/ng"):
            with self.assertRaises(FileNotFoundError):
                mod.run_ngspice("/definitely/not/here.cir")


@unittest.skipUnless(HAVE_NGSPICE, "ngspice is not installed")
class RealNgspiceBinary(unittest.TestCase):
    """The ONLY class here where a pass is evidence the bridge really works."""

    DIVIDER = "* divider\nV1 vin 0 5\nR1 vin vout 1\nR2 vout 0 10\n.end\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.netlist = self.tmp / "c.cir"
        self.netlist.write_text(self.DIVIDER, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_version_string_is_real(self):
        self.assertIn("ngspice", mod.ngspice_version().lower())

    def test_an_op_run_produces_the_expected_divider_voltage(self):
        prepared = self.tmp / "p.cir"
        prepared.write_text(
            mod.add_analysis(self.DIVIDER, mod.OpAnalysis(), ["vout"]),
            encoding="utf-8")
        sim = mod.run_ngspice(str(prepared))
        self.assertIsNotNone(sim)
        self.assertGreater(sim.rows, 0)
        self.assertAlmostEqual(sim.minimum("v(vout)"), 5 * 10 / 11, places=4)

    def test_fault_injection_against_the_real_solver(self):
        prepared = self.tmp / "p.cir"
        prepared.write_text(
            mod.add_analysis(self.DIVIDER, mod.OpAnalysis(), ["vout"]),
            encoding="utf-8")
        sim = mod.run_ngspice(str(prepared))
        ok = mod.check_voltage_drop(sim, [mod.RailTarget("vout", 4.5,
                                                         tolerance_pct=5.0)])
        self.assertEqual(ok["status"], "PASS")
        bad = mod.check_voltage_drop(sim, [mod.RailTarget("vout", 5.0,
                                                          min_accept_v=4.9)])
        self.assertEqual(bad["status"], "FAIL")


class CliContract(unittest.TestCase):
    SCRIPT = SIM_SCRIPTS / "ngspice.py"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.netlist = self.tmp / "c.cir"
        self.netlist.write_text(
            "* divider\nV1 vin 0 5\nR1 vin vout 1\nR2 vout 0 10\n.end\n",
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def test_missing_input_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_watch_exits_2(self):
        proc = self.run_cli(str(self.netlist))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--watch", proc.stderr)

    def test_behavioural_without_a_model_note_exits_2(self):
        """A behavioural PASS and a vendor-model PASS are not the same claim."""
        proc = self.run_cli(str(self.netlist), "--watch", "vout",
                            "--model-class", "behavioural")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--model-note", proc.stderr)

    def test_behavioural_with_a_note_is_accepted(self):
        proc = self.run_cli(str(self.netlist), "--watch", "vout",
                            "--model-class", "behavioural",
                            "--model-note", "ideal divider, no PSRR")
        self.assertIn(proc.returncode, (0, 1))
        report = json.loads(proc.stdout)
        self.assertEqual(report["params"]["model_class"], "behavioural")
        self.assertTrue(report["params"]["model_note"])

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_dc_analysis_requires_its_parameters(self):
        proc = self.run_cli(str(self.netlist), "--watch", "vout",
                            "--analysis", "dc")
        self.assertEqual(proc.returncode, 2)

    def test_the_report_matches_its_schema(self):
        schema = json.loads((SCHEMAS / "ngspice.schema.json")
                            .read_text(encoding="utf-8"))
        proc = self.run_cli(str(self.netlist), "--watch", "vout",
                            "--rail", "vout=3.3")
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertIn("netlist", report)
        self.assertNotIn("board", report)

    @unittest.skipUnless(HAVE_NGSPICE, "ngspice is not installed")
    def test_end_to_end_fault_injection_exits_1_real_binary(self):
        proc = self.run_cli(str(self.netlist), "--watch", "vout",
                            "--rail", "vout=5.0@4.9",
                            "--model-class", "vendor_spice")
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertTrue(report["params"]["simulation_ran"])

    @unittest.skipIf(HAVE_NGSPICE, "ngspice IS installed here")
    def test_without_ngspice_the_report_says_it_did_not_run(self):
        proc = self.run_cli(str(self.netlist), "--watch", "vout",
                            "--rail", "vout=3.3")
        report = json.loads(proc.stdout)
        self.assertFalse(report["params"]["simulation_ran"])
        self.assertEqual(report["params"]["solver"], "ABSENT")
        self.assertGreater(report["summary"]["NO_COVERAGE"], 0)


if __name__ == "__main__":
    unittest.main()
