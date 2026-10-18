#!/usr/bin/env python3
"""Tests for scripts/board/thermal_bridge.py.

Converted from the archived pytest suite to unittest.

Special attention to the write path, because this is the only ported tool
that mutates a ``.kicad_pcb``: the .bak, the verification, and the rollback
are all exercised, including a deliberately corrupted result.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS, REPO, SCHEMAS  # noqa: E402

import thermal_bridge as mod  # noqa: E402

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]

MINIMAL_BOARD = """(kicad_pcb
\t(version 20241229)
\t(gr_line (start 0 0) (end 20 0) (layer "Edge.Cuts") (uuid "a"))
)
"""


class Containment(unittest.TestCase):
    def test_inside_and_outside(self):
        self.assertTrue(mod.point_in_polygon(5, 5, SQUARE))
        self.assertFalse(mod.point_in_polygon(15, 5, SQUARE))
        self.assertFalse(mod.point_in_polygon(5, -1, SQUARE))

    def test_a_concave_polygon_is_handled(self):
        ell = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
        self.assertTrue(mod.point_in_polygon(2, 8, ell))
        self.assertFalse(mod.point_in_polygon(8, 8, ell))

    def test_no_zones_returns_none_rather_than_raising(self):
        """No enclosure model shared is a coverage gap, not an error."""
        self.assertIsNone(mod.contact_zone_under(5, 5, []))

    def test_the_matching_zone_is_returned(self):
        zone = mod.ContactZone("boss", SQUARE, 0.4)
        self.assertIs(mod.contact_zone_under(5, 5, [zone]), zone)


class ThermalContactCheck(unittest.TestCase):
    ZONES = [mod.ContactZone("boss", SQUARE, 0.4)]

    def test_missing_mask_opening_fails(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 1.2, 5, 5, False)], self.ZONES)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["problem"],
                         "NO_B_MASK_OPENING")

    def test_opening_plus_enig_passes(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 1.2, 5, 5, True, "ENIG")], self.ZONES)
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 1)

    def test_hasl_is_a_violation_even_with_an_opening(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 1.2, 5, 5, True, "HASL")], self.ZONES)
        self.assertEqual(finding["violations"][0]["problem"], "FINISH_NOT_ENIG")

    def test_tbd_finish_is_also_a_violation(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 1.2, 5, 5, True, "TBD")], self.ZONES)
        self.assertEqual(finding["status"], "FAIL")

    def test_enig_is_matched_case_insensitively(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 1.2, 5, 5, True, "enig")], self.ZONES)
        self.assertEqual(finding["status"], "PASS")

    def test_a_cool_part_is_not_scanned(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("R1", 0.05, 5, 5, False)], self.ZONES)
        self.assertEqual(finding["scanned"], 0)
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_a_hot_part_not_over_a_zone_is_not_scanned(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 5.0, 50, 50, False)], self.ZONES)
        self.assertEqual(finding["scanned"], 0)

    def test_no_enclosure_data_is_no_coverage_never_pass(self):
        finding = mod.check_thermal_contact(
            [mod.HotComponent("U1", 1.2, 5, 5, False)], [])
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_the_power_threshold_is_configurable(self):
        component = mod.HotComponent("U1", 0.3, 5, 5, False)
        self.assertEqual(
            mod.check_thermal_contact([component], self.ZONES,
                                      critical_power_w=0.1)["status"], "FAIL")


class ThermalPad(unittest.TestCase):
    def test_thickness_accounts_for_compression(self):
        self.assertAlmostEqual(mod.thermal_pad_thickness_mm(0.3), 0.4, places=9)
        self.assertAlmostEqual(mod.thermal_pad_thickness_mm(0.5, 0.5), 1.0,
                               places=9)

    def test_a_non_positive_gap_is_refused(self):
        with self.assertRaises(ValueError):
            mod.thermal_pad_thickness_mm(0.0)
        with self.assertRaises(ValueError):
            mod.thermal_pad_thickness_mm(-1.0)

    def test_compression_must_be_a_fraction(self):
        for bad in (0.0, 1.0, 1.5, -0.2):
            with self.assertRaises(ValueError):
                mod.thermal_pad_thickness_mm(0.5, bad)

    def test_bom_note_carries_the_name_and_thickness(self):
        note = mod.thermal_pad_bom_note("U3_PMIC", 0.4)
        self.assertIn("U3_PMIC", note)
        self.assertIn("0.4mm", note)


class JunctionTemperature(unittest.TestCase):
    def test_no_rtheta_data_is_no_coverage(self):
        data = mod.JunctionTempInput(name="U1", power_w=1.0)
        tj, reason = mod.junction_temperature_c(data)
        self.assertIsNone(tj)
        finding = mod.check_junction_temperature(data)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_rtheta_ja_path_uses_ambient(self):
        data = mod.JunctionTempInput(name="U1", power_w=2.0,
                                     r_theta_ja_c_per_w=20.0,
                                     ambient_temp_c=25.0)
        tj, method = mod.junction_temperature_c(data)
        self.assertAlmostEqual(65.0, tj)  # 25 + 2*20
        self.assertIn("RthetaJA", method)

    def test_case_contact_path_is_preferred_over_rtheta_ja(self):
        """A component thermally bonded to the enclosure has the CASE as its
        real reference point, not ambient air - even if both paths are
        available, the case path must win."""
        data = mod.JunctionTempInput(
            name="U1", power_w=2.0, r_theta_ja_c_per_w=20.0,
            r_theta_jc_c_per_w=5.0, ambient_temp_c=25.0,
            case_contact_temp_c=40.0)
        tj, method = mod.junction_temperature_c(data)
        self.assertAlmostEqual(50.0, tj)  # 40 + 2*5, NOT 25 + 2*20 = 65
        self.assertIn("RthetaJC", method)

    def test_no_max_tj_is_no_coverage_even_though_tj_was_computed(self):
        """An unbounded 'it passed' claim must never be invented."""
        data = mod.JunctionTempInput(name="U1", power_w=2.0,
                                     r_theta_ja_c_per_w=20.0)
        finding = mod.check_junction_temperature(data)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertIn("Tj", finding["detail"])

    def test_within_limit_passes(self):
        data = mod.JunctionTempInput(name="U1", power_w=1.0,
                                     r_theta_ja_c_per_w=20.0,
                                     ambient_temp_c=25.0,
                                     max_allowed_tj_c=125.0)
        finding = mod.check_junction_temperature(data)
        self.assertEqual("PASS", finding["status"])
        self.assertEqual(1, finding["scanned"])

    def test_fault_injection_exceeding_the_limit_fails(self):
        data = mod.JunctionTempInput(name="U1", power_w=5.0,
                                     r_theta_ja_c_per_w=20.0,
                                     ambient_temp_c=25.0,
                                     max_allowed_tj_c=100.0)
        finding = mod.check_junction_temperature(data)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, len(finding["violations"]))
        self.assertAlmostEqual(125.0, finding["violations"][0]["tj_c"])


class ThermalBarrier(unittest.TestCase):
    def test_no_shared_copper_means_no_barrier(self):
        pair = mod.SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, False, 20.0)
        self.assertFalse(mod.thermal_barrier_needed(pair))
        self.assertIsNone(mod.suggest_slot(pair))

    def test_shared_copper_with_room_gets_a_slot(self):
        pair = mod.SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, True, 6.0)
        slot = mod.suggest_slot(pair)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.start[0], 5.0)
        self.assertAlmostEqual(slot.web_mm, 5.0, places=6)

    def test_a_slot_that_would_thin_the_web_is_withheld_not_shrunk(self):
        """Trading mechanical strength for thermal isolation is never done
        silently; the caller must see NEEDS_HUMAN."""
        pair = mod.SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, True, 2.0)
        self.assertIsNone(mod.suggest_slot(pair))

    def test_the_boundary_web_is_accepted(self):
        pair = mod.SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, True,
                                       mod.MIN_WEB_MM + 1.0)
        self.assertIsNotNone(mod.suggest_slot(pair, slot_width_mm=1.0))

    def test_the_check_reports_an_unresolvable_pair(self):
        finding, slots = mod.check_thermal_barriers(
            [mod.SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, True, 2.0)])
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["problem"], "NO_SLOT_FITS")
        self.assertEqual(slots, [])

    def test_pairs_needing_no_barrier_are_not_scanned(self):
        finding, slots = mod.check_thermal_barriers(
            [mod.SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, False, 2.0)])
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertEqual(slots, [])

    def test_there_is_no_distance_based_exemption(self):
        """An invented 'far enough is safe' multiplier was deliberately
        removed; shared thick copper needs a barrier at ANY distance."""
        far = mod.SourceSensitivePair("U1", 0, 0, "Y1", 10_000, 0, True, 20.0)
        self.assertTrue(mod.thermal_barrier_needed(far))


class BlockGeneration(unittest.TestCase):
    def test_a_mask_polygon_needs_three_points(self):
        with self.assertRaises(ValueError):
            mod.mask_opening_block("b.kicad_pcb", "x", [(0, 0), (1, 1)])

    def test_the_mask_block_is_a_filled_b_mask_polygon(self):
        block = mod.mask_opening_block("b.kicad_pcb", "x",
                                       [(0, 0), (1, 0), (1, 1)])
        self.assertIn('(layer "B.Mask")', block)
        self.assertIn("(fill yes)", block)
        self.assertEqual(block.count("(xy "), 3)

    def test_uuids_are_deterministic(self):
        first = mod.mask_opening_block("b.kicad_pcb", "x",
                                       [(0, 0), (1, 0), (1, 1)])
        second = mod.mask_opening_block("b.kicad_pcb", "x",
                                        [(0, 0), (1, 0), (1, 1)])
        self.assertEqual(first, second)

    def test_a_slot_becomes_two_parallel_edge_cuts_lines(self):
        slot = mod.MillingSlot("s", (5.0, 0.0), (5.0, 10.0), 1.0, 4.0)
        block = mod.slot_block("b.kicad_pcb", slot)
        self.assertEqual(block.count("gr_line"), 2)
        self.assertIn('(layer "Edge.Cuts")', block)
        self.assertIn("(start 4.5 0.0)", block)
        self.assertIn("(start 5.5 0.0)", block)

    def test_balance_checker_ignores_parens_inside_strings(self):
        self.assertTrue(mod._balanced('(a "b)c" )'))
        self.assertFalse(mod._balanced("(a"))
        self.assertFalse(mod._balanced("a)"))


class WritePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.board = self.tmp / "b.kicad_pcb"
        self.board.write_text(MINIMAL_BOARD, encoding="utf-8")
        self.original = self.board.read_text(encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def block(self):
        return mod.mask_opening_block(str(self.board), "u1",
                                      [(4, 4), (6, 4), (6, 6)])

    def test_a_successful_write_leaves_a_bak_and_inserts_the_block(self):
        state = mod.write_blocks(str(self.board), [self.block()], force=True)
        self.assertTrue(state["wrote"])
        self.assertFalse(state["rolled_back"])
        self.assertTrue(Path(state["backup"]).is_file())
        self.assertEqual(Path(state["backup"]).read_text(encoding="utf-8"),
                         self.original)
        self.assertIn('(layer "B.Mask")',
                      self.board.read_text(encoding="utf-8"))

    def test_the_written_board_still_balances(self):
        mod.write_blocks(str(self.board), [self.block()], force=True)
        self.assertTrue(mod._balanced(self.board.read_text(encoding="utf-8")))

    def test_a_failed_verification_rolls_back_to_the_bak(self):
        """The single most important behaviour of this tool."""
        def corrupting_balanced(text):
            return False
        with mock.patch.object(mod, "_balanced",
                               side_effect=[True, False]):
            state = mod.write_blocks(str(self.board), [self.block()],
                                     force=True)
        self.assertFalse(state["wrote"])
        self.assertTrue(state["rolled_back"])
        self.assertTrue(state["problems"])
        self.assertEqual(self.board.read_text(encoding="utf-8"), self.original)

    def test_an_unbalanced_source_board_is_refused_before_any_write(self):
        bad = self.tmp / "bad.kicad_pcb"
        bad.write_text("(kicad_pcb (foo)", encoding="utf-8")
        with self.assertRaises(ValueError):
            mod.write_blocks(str(bad), [self.block()], force=True)
        self.assertFalse((self.tmp / "bad.kicad_pcb.bak").exists())

    def test_a_missing_board_raises(self):
        with self.assertRaises(FileNotFoundError):
            mod.write_blocks(str(self.tmp / "nope.kicad_pcb"),
                             [self.block()], force=True)

    def test_nothing_to_write_is_not_reported_as_a_write(self):
        state = mod.write_blocks(str(self.board), [], force=True)
        self.assertFalse(state["wrote"])
        self.assertFalse(state["rolled_back"])

    def test_the_kicad_open_guard_is_consulted(self):
        """Without force, assert_kicad_closed must run before any write."""
        with mock.patch.object(mod, "assert_kicad_closed",
                               side_effect=RuntimeError("KiCad is running")):
            with self.assertRaises(RuntimeError):
                mod.write_blocks(str(self.board), [self.block()], force=False)
        self.assertEqual(self.board.read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.tmp / "b.kicad_pcb.bak").exists())


class CliContract(unittest.TestCase):
    SCRIPT = BOARD_SCRIPTS / "thermal_bridge.py"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.board = self.tmp / "b.kicad_pcb"
        self.board.write_text(MINIMAL_BOARD, encoding="utf-8")
        self.thermal = self.tmp / "t.json"
        self.thermal.write_text(json.dumps({
            "contact_zones": [{"name": "boss",
                               "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                               "z_gap_mm": 0.4}],
            "components": [{"ref": "U1", "power_w": 1.2, "x_mm": 5, "y_mm": 5,
                            "b_mask_opening_defined": False}],
            "pairs": [{"source": "U1", "source_x": 0, "source_y": 0,
                       "sensitive": "Y1", "sensitive_x": 10, "sensitive_y": 0,
                       "shared_thick_copper": True,
                       "distance_to_edge_mm": 6.0}],
            "mask_openings": [{"name": "u1_pad",
                               "polygon": [[4, 4], [6, 4], [6, 6], [4, 6]]}],
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def test_missing_board_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_thermal_json_exits_2(self):
        proc = self.run_cli(str(self.board))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--thermal", proc.stderr)

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_dry_run_does_not_touch_the_board(self):
        before = self.board.read_text(encoding="utf-8")
        proc = self.run_cli(str(self.board), "--thermal", str(self.thermal))
        self.assertEqual(proc.returncode, 1)      # missing mask opening
        report = json.loads(proc.stdout)
        self.assertFalse(report["wrote"])
        self.assertEqual(self.board.read_text(encoding="utf-8"), before)
        # Proposals are still shown, so the caller can review before writing.
        kinds = {p["kind"] for p in report["proposals"]}
        self.assertEqual(kinds, {"b_mask_opening", "edge_cuts_slot"})

    def test_write_produces_a_backup_and_the_geometry(self):
        proc = self.run_cli(str(self.board), "--thermal", str(self.thermal),
                            "--write", "--force")
        report = json.loads(proc.stdout)
        self.assertTrue(report["wrote"])
        self.assertFalse(report["rolled_back"])
        self.assertTrue(Path(str(self.board) + ".bak").is_file())
        text = self.board.read_text(encoding="utf-8")
        self.assertIn('(layer "B.Mask")', text)
        self.assertIn('(layer "Edge.Cuts")', text)

    def test_the_report_carries_the_pad_thickness_note(self):
        proc = self.run_cli(str(self.board), "--thermal", str(self.thermal))
        pads = json.loads(proc.stdout)["params"]["thermal_pads"]
        self.assertEqual(pads[0]["zone"], "boss")
        self.assertAlmostEqual(pads[0]["pad_thickness_mm"], 0.5333, places=4)

    def test_the_report_matches_its_schema(self):
        schema = json.loads((SCHEMAS / "thermal_bridge.schema.json")
                            .read_text(encoding="utf-8"))
        proc = self.run_cli(str(self.board), "--thermal", str(self.thermal))
        report = json.loads(proc.stdout)
        for field in schema["required"]:
            self.assertIn(field, report)
        self.assertEqual(report["schema_version"],
                         schema["properties"]["schema_version"]["const"])
        self.assertEqual(report["tool"],
                         schema["properties"]["tool"]["const"])
        allowed = set(schema["properties"])
        self.assertTrue(set(report) <= allowed, set(report) - allowed)


class NotPublishedOverMcp(unittest.TestCase):
    def test_no_mcp_server_exposes_thermal_bridge(self):
        """A board-mutating tool must not be reachable as an MCP tool."""
        from _paths import SERVERS
        for server in SERVERS.glob("*.py"):
            self.assertNotIn("thermal_bridge",
                             server.read_text(encoding="utf-8"),
                             f"{server.name} references thermal_bridge")


if __name__ == "__main__":
    unittest.main()
