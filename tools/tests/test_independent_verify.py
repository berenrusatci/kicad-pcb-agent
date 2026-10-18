#!/usr/bin/env python3
"""Tests for scripts/independent_verify.py.

Converted from the archived pytest suite to unittest, plus new coverage for
the contract source change: the contract is now a JSON file pinned by
``schemas/independent_verify_contract.schema.json``, not a Markdown table
inside the project docs. The strictness tests below are the whole point of
that change - a malformed contract must be an ERROR, not an empty PASS.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCHEMAS, SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import independent_verify as mod  # noqa: E402

BOARD = """(kicad_pcb
  (segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net "USB_D_P"))
  (segment (start 0 1) (end 10 1) (width 0.2) (layer "F.Cu") (net "USB_D_N"))
  (segment (start 10 1) (end 14 1) (width 0.2) (layer "F.Cu") (net "USB_D_N"))
  (via (at 5 5) (size 0.6) (layers "F.Cu" "B.Cu") (net "USB_D_P"))
  (segment (start 0 3) (end 5 3) (width 0.25) (layer "B.Cu") (net "PWR"))
  (arc (start 0 4) (mid 1 4.5) (end 2 4) (width 0.2) (layer "F.Cu")
       (net "USB_D_P"))
  (segment (start 0 9) (end 1 9) (width 0.2) (layer "F.Cu") (net ""))
)
"""


def contract(**overrides):
    base = {"schema_version": "independent-verify-contract/v1",
            "nets": [{"name": "USB_D", "target_width_mm": 0.2,
                      "allowed_layers": ["F.Cu"]}]}
    base.update(overrides)
    return base


class BoardReading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.board = self.tmp / "b.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")
        self.tracks = mod.read_tracks(str(self.board))

    def tearDown(self):
        self._tmp.cleanup()

    def test_items_are_grouped_by_net(self):
        self.assertEqual(set(self.tracks),
                         {"USB_D_P", "USB_D_N", "PWR"})

    def test_a_net_less_item_is_skipped(self):
        """An item belonging to no net belongs to no contract."""
        self.assertNotIn("", self.tracks)

    def test_vias_arcs_and_segments_are_distinguished(self):
        kinds = sorted(s.kind for s in self.tracks["USB_D_P"])
        self.assertEqual(kinds, ["arc", "segment", "via"])

    def test_segment_length_is_euclidean(self):
        segment = next(s for s in self.tracks["USB_D_P"] if s.kind == "segment")
        self.assertAlmostEqual(segment.length_mm, 10.0, places=9)

    def test_a_via_has_zero_length(self):
        via = next(s for s in self.tracks["USB_D_P"] if s.kind == "via")
        self.assertEqual(via.length_mm, 0.0)


class WidthCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        board = Path(self._tmp.name) / "b.kicad_pcb"
        board.write_text(BOARD, encoding="utf-8")
        self.tracks = mod.read_tracks(str(board))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_uniform_width_passes(self):
        finding = mod.check_width(self.tracks, "USB_D", 0.2)
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 3)

    def test_fault_injection_the_wrong_target_fails_everywhere(self):
        finding = mod.check_width(self.tracks, "USB_D", 0.25)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(len(finding["violations"]), 3)

    def test_vias_are_not_measured_as_track_width(self):
        """The via is 0.6 mm; if it were counted the 0.2 mm check would fail."""
        self.assertEqual(mod.check_width(self.tracks, "USB_D", 0.2)["status"],
                         "PASS")

    def test_a_pattern_matching_nothing_is_no_coverage(self):
        finding = mod.check_width(self.tracks, "NOTHING", 0.2)
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertEqual(finding["scanned"], 0)

    def test_the_tolerance_is_honoured(self):
        self.assertEqual(
            mod.check_width(self.tracks, "PWR", 0.2, tolerance_mm=0.1)["status"],
            "PASS")
        self.assertEqual(
            mod.check_width(self.tracks, "PWR", 0.2, tolerance_mm=0.01)["status"],
            "FAIL")


class SkewCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        board = Path(self._tmp.name) / "b.kicad_pcb"
        board.write_text(BOARD, encoding="utf-8")
        self.tracks = mod.read_tracks(str(board))

    def tearDown(self):
        self._tmp.cleanup()

    def test_skew_is_measured_from_the_real_lengths(self):
        """P is 10 mm of segment plus a ~2 mm arc chord; N is 14 mm."""
        finding = mod.check_skew(self.tracks, "USB_D", tolerance_mm=1.0)
        self.assertEqual(finding["status"], "FAIL")
        row = finding["violations"][0]
        self.assertAlmostEqual(row["length_n_mm"], 14.0, places=3)
        self.assertGreater(row["skew_mm"], 0)

    def test_a_generous_tolerance_passes(self):
        self.assertEqual(
            mod.check_skew(self.tracks, "USB_D", tolerance_mm=10.0)["status"],
            "PASS")

    def test_an_unrouted_pair_is_no_coverage_not_zero_skew(self):
        finding = mod.check_skew(self.tracks, "NOWHERE")
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertIn("not routed", finding["detail"])

    def test_one_half_routed_is_also_no_coverage(self):
        tracks = {"X_P": self.tracks["USB_D_P"]}
        self.assertEqual(mod.check_skew(tracks, "X")["status"], "NO_COVERAGE")

    def test_custom_suffixes(self):
        tracks = {"CLK+": self.tracks["USB_D_P"], "CLK-": self.tracks["USB_D_N"]}
        finding = mod.check_skew(tracks, "CLK", p_suffix="+", n_suffix="-",
                                 tolerance_mm=100.0)
        self.assertEqual(finding["status"], "PASS")


class LayerLeakCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        board = Path(self._tmp.name) / "b.kicad_pcb"
        board.write_text(BOARD, encoding="utf-8")
        self.tracks = mod.read_tracks(str(board))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_via_is_not_a_layer_leak(self):
        """Changing layer is what a via is FOR."""
        finding = mod.check_layer_leak(self.tracks, "USB_D",
                                       frozenset({"F.Cu"}))
        self.assertEqual(finding["status"], "PASS")

    def test_a_real_leak_is_detected(self):
        finding = mod.check_layer_leak(self.tracks, "USB_D",
                                       frozenset({"B.Cu"}))
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(len(finding["violations"]), 4)   # 3 segments + 1 arc

    def test_a_pattern_matching_nothing_is_no_coverage(self):
        self.assertEqual(
            mod.check_layer_leak(self.tracks, "ZZZ",
                                 frozenset({"F.Cu"}))["status"], "NO_COVERAGE")


class ContractLoading(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, payload):
        path = self.tmp / "c.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_a_valid_contract_loads(self):
        rows = mod.load_contract(self.write(contract()))
        self.assertEqual(rows[0].name, "USB_D")
        self.assertEqual(rows[0].target_width_mm, 0.2)
        self.assertEqual(rows[0].allowed_layers, frozenset({"F.Cu"}))

    def test_a_missing_file_raises(self):
        with self.assertRaises(mod.ContractError):
            mod.load_contract(str(self.tmp / "nope.json"))

    def test_broken_json_raises(self):
        with self.assertRaises(mod.ContractError):
            mod.load_contract(self.write("{not json"))

    def test_an_unknown_schema_version_raises(self):
        with self.assertRaises(mod.ContractError):
            mod.load_contract(self.write(
                contract(schema_version="something-else/v9")))

    def test_a_missing_required_field_raises(self):
        with self.assertRaises(mod.ContractError):
            mod.load_contract(self.write({"nets": []}))

    def test_an_empty_net_list_raises_rather_than_silently_covering_nothing(self):
        """The exact failure the Markdown-table source used to hide."""
        with self.assertRaises(mod.ContractError):
            mod.load_contract(self.write(contract(nets=[])))

    def test_a_nameless_net_entry_raises(self):
        with self.assertRaises(mod.ContractError):
            mod.load_contract(self.write(contract(nets=[{"target_width_mm": 0.2}])))

    def test_the_default_skew_tolerance_is_applied(self):
        rows = mod.load_contract(self.write(contract(
            default_skew_tolerance_mm=7.5,
            nets=[{"name": "A"}, {"name": "B", "skew_tolerance_mm": 2.0}])))
        self.assertEqual(rows[0].skew_tolerance_mm, 7.5)
        self.assertEqual(rows[1].skew_tolerance_mm, 2.0)

    def test_an_unstated_skew_tolerance_is_not_invented(self):
        rows = mod.load_contract(self.write(contract(nets=[{"name": "A"}])))
        self.assertIsNone(rows[0].skew_tolerance_mm)

    def test_the_required_field_list_comes_from_the_schema_file(self):
        schema = json.loads(
            (SCHEMAS / "independent_verify_contract.schema.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(mod._required_contract_fields(), schema["required"])


class Orchestration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.board = self.tmp / "b.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_stated_items_are_measured(self):
        rows = [mod.NetContract("USB_D")]     # nothing stated
        findings = mod.verify(str(self.board), rows)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "project_contract")
        self.assertEqual(findings[0]["status"], "NO_COVERAGE")

    def test_each_stated_item_produces_its_own_finding(self):
        rows = [mod.NetContract("USB_D", target_width_mm=0.2,
                                skew_tolerance_mm=15.0,
                                allowed_layers=frozenset({"F.Cu"}))]
        findings = mod.verify(str(self.board), rows)
        self.assertEqual([f["check"] for f in findings],
                         ["track_width[USB_D]", "skew[USB_D]",
                          "layer_leak[USB_D]"])

    def test_the_net_name_is_regex_escaped(self):
        """A net called 'D+' must not be read as a regex quantifier."""
        rows = [mod.NetContract("D+", target_width_mm=0.2)]
        findings = mod.verify(str(self.board), rows)
        self.assertEqual(findings[0]["status"], "NO_COVERAGE")


class CliContract(unittest.TestCase):
    SCRIPT = SCRIPTS / "independent_verify.py"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.board = self.tmp / "b.kicad_pcb"
        self.board.write_text(BOARD, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def write_contract(self, payload=None):
        path = self.tmp / "c.json"
        path.write_text(json.dumps(payload or contract()), encoding="utf-8")
        return str(path)

    def test_missing_board_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_contract_exits_2(self):
        proc = self.run_cli(str(self.board))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--contract", proc.stderr)

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_a_clean_board_exits_0_with_json_on_stdout(self):
        proc = self.run_cli(str(self.board), "--contract", self.write_contract())
        self.assertEqual(proc.returncode, 0)
        report = json.loads(proc.stdout)
        self.assertEqual(report["schema_version"],
                         "independent-verify-report/v1")
        self.assertEqual(report["params"]["contract_nets"], ["USB_D"])

    def test_a_violated_contract_exits_1(self):
        proc = self.run_cli(str(self.board), "--contract", self.write_contract(
            contract(nets=[{"name": "USB_D", "target_width_mm": 0.5}])))
        self.assertEqual(proc.returncode, 1)

    def test_a_broken_contract_exits_2_not_0(self):
        """The whole reason the contract moved out of a Markdown table."""
        path = self.tmp / "bad.json"
        path.write_text("{oops", encoding="utf-8")
        proc = self.run_cli(str(self.board), "--contract", str(path))
        self.assertEqual(proc.returncode, 2)

    def test_the_report_matches_its_schema(self):
        schema = json.loads((SCHEMAS / "independent_verify.schema.json")
                            .read_text(encoding="utf-8"))
        proc = self.run_cli(str(self.board), "--contract", self.write_contract())
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertIn(report["tool"], schema["properties"]["tool"]["enum"])


class IndependenceFromKicadDrc(unittest.TestCase):
    def test_the_module_never_reads_a_kicad_drc_report(self):
        """The point of this tool: it must not consult the thing it doubles."""
        source = (SCRIPTS / "independent_verify.py").read_text(encoding="utf-8")
        for forbidden in ("drc_summary", "pcb drc", "drc.json", "import pcbnew"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
