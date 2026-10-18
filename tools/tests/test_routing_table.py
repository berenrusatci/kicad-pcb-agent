#!/usr/bin/env python3
"""Tests for scripts/report/routing_table.py.

Focuses on the pure-logic functions (diff-pair matching, interface guess,
category, chain-text building, skew) which need no pcbnew; the real-board
extraction path is exercised only through --self-test and the CLI contract,
consistent with every other report/*.py module in this suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

REPORT_SCRIPTS = SCRIPTS / "report"
sys.path.insert(0, str(REPORT_SCRIPTS))

import routing_table as mod  # noqa: E402
from common.stackup import InterfaceKind, INTERFACE_IMPEDANCE_TARGETS_OHM  # noqa: E402


class DiffPairPartnerTests(unittest.TestCase):
    def test_p_n_suffix_pair_matches(self):
        names = {"MIPI_D0_P", "MIPI_D0_N"}
        self.assertEqual("MIPI_D0_N", mod.find_diff_pair_partner("MIPI_D0_P", names))

    def test_unrelated_net_does_not_match(self):
        names = {"LED0", "GPIO_D0_EN"}
        self.assertIsNone(mod.find_diff_pair_partner("LED0", names))

    def test_candidate_must_actually_exist_on_the_board(self):
        # "SIG_P" looks pairable but "SIG_N" is not in the net set.
        self.assertIsNone(mod.find_diff_pair_partner("SIG_P", {"SIG_P"}))

    def test_dp_dm_suffix_pair_matches(self):
        names = {"USB_DP", "USB_DM"}
        self.assertEqual("USB_DM", mod.find_diff_pair_partner("USB_DP", names))


class InterfaceGuessTests(unittest.TestCase):
    def test_usb3_is_checked_before_usb2(self):
        self.assertEqual(InterfaceKind.USB3_X, mod.guess_interface_kind("USB3_TX_P"))

    def test_plain_usb_falls_back_to_usb2(self):
        self.assertEqual(InterfaceKind.USB2_0, mod.guess_interface_kind("USB_DP"))

    def test_unmatched_name_returns_none(self):
        self.assertIsNone(mod.guess_interface_kind("RANDOM_NET_123"))

    def test_impedance_target_comes_from_common_stackup(self):
        target = mod.impedance_target_from_net_name("PCIE_TX0_P")
        self.assertEqual(INTERFACE_IMPEDANCE_TARGETS_OHM[InterfaceKind.PCIE], target)

    def test_unmatched_impedance_is_none_not_a_guess(self):
        self.assertIsNone(mod.impedance_target_from_net_name("RANDOM_NET"))


class NetCategoryTests(unittest.TestCase):
    def test_gnd_net_is_power_gnd(self):
        category, _ = mod.net_category_and_rationale("GND", None, None)
        self.assertEqual("POWER/GND", category)

    def test_power_rail_name_is_power(self):
        category, _ = mod.net_category_and_rationale("CARRIER_3V3", None, None)
        self.assertEqual("POWER", category)

    def test_substring_false_positive_is_avoided(self):
        """'USBVCC_SENSE' must NOT be flagged as power by a naive substring
        match on 'VCC' - only a whole underscore-delimited token counts."""
        category, _ = mod.net_category_and_rationale("USBVCC_SENSE", None, None)
        self.assertNotEqual("POWER", category)

    def test_diff_pair_with_known_interface_is_high_speed(self):
        category, rationale = mod.net_category_and_rationale(
            "USB_DP", InterfaceKind.USB2_0, "USB_DM")
        self.assertEqual("HIGH SPEED", category)
        self.assertIn("USB_DM", rationale)

    def test_diff_pair_without_known_interface_is_flagged_unclear(self):
        category, rationale = mod.net_category_and_rationale("SIG_P", None, "SIG_N")
        self.assertEqual("HIGH SPEED (interface unclear)", category)

    def test_plain_signal_is_standard_io(self):
        category, _ = mod.net_category_and_rationale("GPIO_1", None, None)
        self.assertEqual("STANDARD I/O", category)

    def test_suggested_layer_matches_the_category(self):
        self.assertEqual("F.Cu", mod.suggested_layer("HIGH SPEED"))
        self.assertEqual("In1.Cu (plane)", mod.suggested_layer("POWER/GND"))
        self.assertEqual("B.Cu", mod.suggested_layer("STANDARD I/O"))
        self.assertEqual("B.Cu", mod.suggested_layer("UNKNOWN CATEGORY"))


class SkewTests(unittest.TestCase):
    def test_small_skew_passes(self):
        skew = mod.evaluate_skew(10.0, 10.05)
        self.assertEqual(0.05, skew["skew_mm"])
        self.assertEqual("PASS", skew["status"])

    def test_large_skew_fails(self):
        skew = mod.evaluate_skew(0.0, 20.0)
        self.assertEqual("FAIL", skew["status"])


class LayerChainTests(unittest.TestCase):
    def test_no_segments_or_vias_reports_unrouted(self):
        data = {"pins": [], "segments": [], "vias": []}
        self.assertIn("unrouted", mod.build_layer_chain_text(data))

    def test_simple_chain_of_two_segments_via_a_via(self):
        data = {
            "pins": [{"pin": "J1.1", "pos_mm": (0.0, 0.0)}],
            "segments": [
                {"layer": "F.Cu", "start_mm": (0.0, 0.0), "end_mm": (5.0, 0.0),
                 "width_mm": 0.2, "length_mm": 5.0},
                {"layer": "In2.Cu", "start_mm": (5.0, 0.0), "end_mm": (10.0, 0.0),
                 "width_mm": 0.2, "length_mm": 5.0},
            ],
            "vias": [{"pos_mm": (5.0, 0.0), "top_layer": "F.Cu", "bottom_layer": "In2.Cu"}],
        }
        chain = mod.build_layer_chain_text(data)
        self.assertIn("F.Cu", chain)
        self.assertIn("VIA", chain)
        self.assertIn("In2.Cu", chain)
        self.assertNotIn("disconnected", chain)

    def test_disconnected_piece_is_appended_not_dropped(self):
        data = {
            "pins": [{"pin": "J1.1", "pos_mm": (0.0, 0.0)}],
            "segments": [
                {"layer": "F.Cu", "start_mm": (0.0, 0.0), "end_mm": (5.0, 0.0),
                 "width_mm": 0.2, "length_mm": 5.0},
                {"layer": "F.Cu", "start_mm": (50.0, 50.0), "end_mm": (60.0, 50.0),
                 "width_mm": 0.2, "length_mm": 10.0},
            ],
            "vias": [],
        }
        chain = mod.build_layer_chain_text(data)
        self.assertIn("disconnected", chain)


class DrcStatusTests(unittest.TestCase):
    def test_no_report_given_is_na(self):
        self.assertIn("N/A", mod.find_drc_status("NET1", None))

    def test_missing_file_is_na(self):
        self.assertIn("N/A", mod.find_drc_status("NET1", "does-not-exist.json"))

    def test_matched_violation_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "drc.json"
            report_path.write_text(
                '{"violations": [{"type": "clearance", "items": [{"net": "NET1"}]}]}',
                encoding="utf-8")
            status = mod.find_drc_status("NET1", str(report_path))
            self.assertIn("clearance", status)

    def test_unmatched_net_reports_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "drc.json"
            report_path.write_text(
                '{"violations": [{"type": "clearance", "items": [{"net": "OTHER"}]}]}',
                encoding="utf-8")
            status = mod.find_drc_status("NET1", str(report_path))
            self.assertIn("no violations", status)


def routed_row(net_name, drc_status="no violations found for this net"):
    """The two columns coverage_findings reads, nothing more."""
    return {"Net Name": net_name, "DRC Status": drc_status}


class CoverageVerdictTests(unittest.TestCase):
    """The report's status used to be ``"NO_COVERAGE" if not
    outcome["success"] else "GREEN"``, and ``success`` is False only when
    pcbnew is missing. Everything else - an empty board, unrouted nets, a DRC
    column that reads "N/A (no DRC report given)" from top to bottom - was
    reported as a clean GREEN table. These pin the axes that verdict now has
    to account for."""

    def test_an_empty_finding_set_is_no_coverage(self):
        """The missing-pcbnew path: generate_and_save returns ``checks: []``
        and needs no special case, because an empty set rolls up to
        NO_COVERAGE ("nothing was checked" is not "everything is fine")."""
        self.assertEqual("NO_COVERAGE", mod.report_status([]))

    def test_a_fully_measured_table_is_green(self):
        data = {"routed": [routed_row("NET1"), routed_row("NET2")],
                "unrouted": []}
        self.assertEqual("GREEN", mod.report_status(mod.coverage_findings(data)))

    def test_a_board_with_no_tracks_is_not_green(self):
        """Zero rows is an unmeasured board, not a clean one."""
        data = {"routed": [], "unrouted": []}
        findings = mod.coverage_findings(data)
        self.assertEqual("NO_COVERAGE", mod.report_status(findings))
        by_check = {f["check"]: f for f in findings}
        self.assertEqual("NO_COVERAGE", by_check["routed_net_rows"]["status"])

    def test_an_unrouted_net_is_not_green(self):
        data = {"routed": [routed_row("NET1")],
                "unrouted": [{"Net Name": "NET2"}]}
        findings = mod.coverage_findings(data)
        self.assertEqual("NO_COVERAGE", mod.report_status(findings))
        by_check = {f["check"]: f for f in findings}
        self.assertEqual("NO_COVERAGE",
                         by_check["unrouted_net_routing"]["status"])
        self.assertEqual("PASS", by_check["routed_net_rows"]["status"])

    def test_a_missing_drc_report_is_not_green(self):
        """``find_drc_status`` returns "N/A (no DRC report given)" for every
        net when --drc-json is absent; the table then makes no DRC claim."""
        data = {"routed": [routed_row("NET1", mod.find_drc_status("NET1", None))],
                "unrouted": []}
        findings = mod.coverage_findings(data)
        self.assertEqual("NO_COVERAGE", mod.report_status(findings))
        by_check = {f["check"]: f for f in findings}
        self.assertEqual("NO_COVERAGE",
                         by_check["drc_status_per_net"]["status"])
        self.assertEqual(0, by_check["drc_status_per_net"]["scanned"])

    def test_no_axis_can_report_a_scanned_zero_pass(self):
        """The single-check invariant, asserted over every axis at once."""
        for data in ({"routed": [], "unrouted": []},
                     {"routed": [routed_row("NET1")], "unrouted": []},
                     {"routed": [], "unrouted": [{"Net Name": "NET2"}]}):
            for finding in mod.coverage_findings(data):
                with self.subTest(data=data, check=finding["check"]):
                    if finding["scanned"] == 0:
                        self.assertEqual("NO_COVERAGE", finding["status"])


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = REPORT_SCRIPTS / "routing_table.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False)

    def test_missing_args_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(2, proc.returncode)

    def test_self_test_is_opt_in_and_exits_zero(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(0, proc.returncode)

    def test_a_board_that_cannot_be_read_never_exits_zero(self):
        """WAS ``test_missing_pcbnew_is_no_coverage_exit_1``, asserting exit
        1 exactly. That pins an assumption about the MACHINE, not about the
        tool, and it fails on any dev box where pcbnew IS importable (it is
        shipped system-wide by some KiCad packages): there the missing board
        file is a load ERROR, exit 2, not a NO_COVERAGE. Both are correct
        refusals and both are what this test exists to protect - what must
        never happen is exit 0 with a GREEN verdict over a board that was
        never read."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli("board.kicad_pcb", "--out-dir", tmp)
            self.assertIn(proc.returncode, (1, 2), proc.stderr)
            report = json.loads(proc.stdout)
            self.assertIn(report["status"], ("NO_COVERAGE", "ERROR"))


if __name__ == "__main__":
    unittest.main()
