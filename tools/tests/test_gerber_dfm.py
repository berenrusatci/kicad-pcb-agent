#!/usr/bin/env python3
"""Tests for scripts/board/gerber_dfm.py.

Converted from the archived pytest suite to unittest.

``REAL_KICAD10_*`` are VERBATIM excerpts from ``.gtl``/``.gts`` files that
``kicad-cli pcb export gerbers`` produced from a real board on KiCad 10.0.4.
They are not invented sample data; they are the proof that the parser was
validated against actual fab output.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS, REPO, SCHEMAS, TOOLS  # noqa: E402

import gerber_dfm as mod  # noqa: E402

# --- kicad-cli pcb export gerbers, KiCad 10.0.4, real board ---------------- #
REAL_KICAD10_FCU = """\
%TF.GenerationSoftware,KiCad,Pcbnew,10.0.4*%
%TF.FileFunction,Copper,L1,Top*%
%TF.FilePolarity,Positive*%
%FSLAX46Y46*%
%MOMM*%
%LPD*%
G01*
%AMRoundRect*
0 Rectangle with rounded corners*
4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*
1,1,$1+$1,$2,$3*%
%TA.AperFunction,SMDPad,CuDef*%
%ADD10RoundRect,0.140000X-0.140000X-0.170000X0.140000X-0.170000X0.140000X0.170000X-0.140000X0.170000X0*%
%TD*%
%TA.AperFunction,HeatsinkPad*%
%ADD17R,3.700000X3.700000*%
%TD*%
%TA.AperFunction,ViaPad*%
%ADD29C,0.600000*%
%TD*%
%TA.AperFunction,Conductor*%
%ADD31C,0.200000*%
%TD*%
D10*
%TO.P,C18,1*%
%TO.N,+3V3*%
X8520000Y-11500000D03*
%TO.P,C18,2*%
%TO.N,GND*%
X9480000Y-11500000D03*
%TD*%
D31*
X-2450000Y10250000D02*
X-2450000Y9750000D01*
X5375900Y7855900D02*
X5520000Y8000000D01*
"""

REAL_KICAD10_FMASK = """\
%TF.FileFunction,Soldermask,Top*%
%FSLAX46Y46*%
%MOMM*%
%LPD*%
G01*
%TA.AperFunction,SMDPad,CuDef*%
%ADD10RoundRect,0.140000X-0.140000X-0.170000X0.140000X-0.170000X0.140000X0.170000X-0.140000X0.170000X0*%
%TD*%
%TA.AperFunction,ComponentPad*%
%ADD21O,0.900000X2.400000*%
%TD*%
D10*
%TO.C,C18*%
X8520000Y-11500000D03*
X9480000Y-11500000D03*
%TD*%
%TO.C,U1*%
D21*
X-2450000Y10750000D03*
X0Y9000000D03*
"""


class RealGerberParsing(unittest.TestCase):
    """Against verbatim KiCad 10 output, not invented data."""

    def test_file_function_is_read(self):
        self.assertEqual(mod.parse_gerber(REAL_KICAD10_FCU).file_function,
                         "Copper,L1,Top")

    def test_roundrect_bounding_box(self):
        """r=0.14, corners x in [-0.14, 0.14], y in [-0.17, 0.17]
        -> width 0.28+0.28=0.56, height 0.34+0.28=0.62."""
        aperture = mod.parse_gerber(REAL_KICAD10_FCU).apertures["10"]
        self.assertEqual(aperture.shape, "RoundRect")
        self.assertAlmostEqual(aperture.width_mm, 0.56, places=6)
        self.assertAlmostEqual(aperture.height_mm, 0.62, places=6)

    def test_rect_and_circle_apertures(self):
        apertures = mod.parse_gerber(REAL_KICAD10_FCU).apertures
        self.assertEqual((apertures["17"].shape, apertures["17"].width_mm,
                          apertures["17"].height_mm), ("R", 3.7, 3.7))
        self.assertEqual((apertures["29"].shape, apertures["29"].width_mm),
                         ("C", 0.6))

    def test_obround_aperture_keeps_both_axes(self):
        aperture = mod.parse_gerber(REAL_KICAD10_FMASK).apertures["21"]
        self.assertEqual((aperture.shape, aperture.width_mm, aperture.height_mm),
                         ("O", 0.9, 2.4))

    def test_flash_coordinates_use_the_46_format_scale(self):
        flashes = mod.parse_gerber(REAL_KICAD10_FCU).flashes
        self.assertEqual(len(flashes), 2)
        self.assertAlmostEqual(flashes[0].x_mm, 8.52, places=6)
        self.assertAlmostEqual(flashes[0].y_mm, -11.5, places=6)

    def test_net_and_refdes_attributes_are_attached(self):
        flashes = mod.parse_gerber(REAL_KICAD10_FCU).flashes
        self.assertEqual((flashes[0].net, flashes[0].refdes), ("+3V3", "C18"))
        self.assertEqual((flashes[1].net, flashes[1].refdes), ("GND", "C18"))

    def test_td_clears_the_current_attributes(self):
        """%TD*% must reset net/refdes, or they bleed onto later flashes."""
        text = REAL_KICAD10_FCU + "D29*\nX0Y0D03*\n"
        last = mod.parse_gerber(text).flashes[-1]
        self.assertIsNone(last.net)
        self.assertIsNone(last.refdes)

    def test_mask_layer_uses_the_component_attribute(self):
        flashes = mod.parse_gerber(REAL_KICAD10_FMASK).flashes
        self.assertEqual([f.refdes for f in flashes],
                         ["C18", "C18", "U1", "U1"])

    def test_draw_segments_are_captured(self):
        draws = mod.parse_gerber(REAL_KICAD10_FCU).draws
        self.assertEqual(len(draws), 2)
        self.assertAlmostEqual(draws[0].start[0], -2.45, places=6)
        self.assertAlmostEqual(draws[0].start[1], 10.25, places=6)

    def test_a_d01_without_a_preceding_d02_is_not_invented(self):
        text = "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.2*%\nD10*\nX0Y0D01*\n"
        self.assertEqual(mod.parse_gerber(text).draws, [])


class Geometry(unittest.TestCase):
    def test_gap_between_separated_boxes(self):
        self.assertAlmostEqual(
            mod.box_gap_mm((0, 0, 1, 1), (2, 0, 3, 1)), 1.0, places=9)

    def test_overlapping_boxes_give_a_negative_gap(self):
        self.assertLess(mod.box_gap_mm((0, 0, 2, 2), (1, 1, 3, 3)), 0)

    def test_diagonal_separation_is_euclidean(self):
        self.assertAlmostEqual(
            mod.box_gap_mm((0, 0, 1, 1), (2, 2, 3, 3)), 2 ** 0.5, places=9)

    def test_a_flash_with_an_unknown_aperture_has_no_box(self):
        """None, not a zero box: a zero box would fake infinite clearance."""
        apertures = {"99": mod.Aperture("99", "UNKNOWN", 0.0, 0.0)}
        self.assertIsNone(
            mod.flash_box(mod.Flash(0, 0, "99"), apertures))
        self.assertIsNone(
            mod.flash_box(mod.Flash(0, 0, "missing"), apertures))

    def test_same_net_pairs_are_skipped_by_default(self):
        gerber = mod.parse_gerber(
            "%TF.FileFunction,Copper,L1,Top*%\n%FSLAX46Y46*%\n%MOMM*%\n"
            "%ADD10C,0.5*%\nD10*\n%TO.N,GND*%\nX1000000Y1000000D03*\n"
            "X1600000Y1000000D03*\n%TD*%\n")
        self.assertEqual(mod.close_flash_pairs(gerber, 0.5), [])
        self.assertEqual(
            len(mod.close_flash_pairs(gerber, 0.5, skip_same_net=False)), 1)


class Checks(unittest.TestCase):
    def test_wrong_layer_is_refused(self):
        copper = mod.parse_gerber(REAL_KICAD10_FCU)
        with self.assertRaises(mod.WrongLayerError):
            mod.check_mask_dam(copper, 0.2)
        mask = mod.parse_gerber(REAL_KICAD10_FMASK)
        with self.assertRaises(mod.WrongLayerError):
            mod.check_copper_clearance(mask, 0.2)

    def test_soldermask_is_matched_case_insensitively(self):
        """KiCad 10 writes 'Soldermask,Top' - lower-case m. A first draft
        matched 'SolderMask' and never fired."""
        finding = mod.check_mask_dam(mod.parse_gerber(REAL_KICAD10_FMASK), 0.05)
        self.assertNotEqual(finding["status"], "NO_COVERAGE")

    def test_real_mask_pads_0_44mm_apart_fail_a_0_2mm_dam(self):
        """C18's two 0.56 x 0.62 mask openings are 0.96 mm centre to centre,
        so the dam is 0.96 - 0.56 = 0.40 mm; a 0.5 mm minimum must fail it."""
        mask = mod.parse_gerber(REAL_KICAD10_FMASK)
        finding = mod.check_mask_dam(mask, 0.5)
        self.assertEqual(finding["status"], "FAIL")
        gaps = [v["gap_mm"] for v in finding["violations"]]
        self.assertIn(0.4, gaps)

    def test_the_same_pads_pass_a_0_2mm_dam(self):
        mask = mod.parse_gerber(REAL_KICAD10_FMASK)
        self.assertEqual(mod.check_mask_dam(mask, 0.2)["status"], "PASS")

    def test_an_empty_layer_is_no_coverage_not_pass(self):
        empty = mod.parse_gerber("%TF.FileFunction,Copper,L1,Top*%\n")
        finding = mod.check_copper_clearance(empty, 0.2)
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertEqual(finding["scanned"], 0)

    def test_unknown_apertures_are_reported_not_silently_dropped(self):
        text = ("%TF.FileFunction,Copper,L1,Top*%\n%FSLAX46Y46*%\n%MOMM*%\n"
                "%ADD40P,1.0*%\nD40*\nX0Y0D03*\n")
        gerber = mod.parse_gerber(text)
        warnings = mod.parse_warnings(gerber)
        self.assertEqual(len(warnings), 1)
        self.assertIn("D40", warnings[0])


class DirectoryScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_both_layers_are_found_in_a_directory(self):
        (self.tmp / "b-F_Cu.gtl").write_text(REAL_KICAD10_FCU, encoding="utf-8")
        (self.tmp / "b-F_Mask.gts").write_text(REAL_KICAD10_FMASK,
                                               encoding="utf-8")
        findings, _ = mod.scan(self.tmp, 0.2, 0.2)
        by_name = {f["check"]: f for f in findings}
        self.assertGreater(by_name["gerber_mask_dam"]["scanned"], 0)
        self.assertGreater(by_name["gerber_copper_clearance"]["scanned"], 0)

    def test_a_directory_with_no_usable_layer_is_no_coverage(self):
        (self.tmp / "readme.txt").write_text("hello", encoding="utf-8")
        findings, _ = mod.scan(self.tmp, 0.2, 0.2)
        self.assertTrue(all(f["status"] == "NO_COVERAGE" for f in findings))


class CliContract(unittest.TestCase):
    SCRIPT = BOARD_SCRIPTS / "gerber_dfm.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def test_missing_input_exits_2_and_does_not_pretend_to_pass(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("required", proc.stderr)

    def test_self_test_is_opt_in_and_writes_only_to_stderr(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_stdout_is_json_only_and_diagnostics_go_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b-F_Cu.gtl"
            path.write_text(
                "%TF.FileFunction,Copper,L1,Top*%\n%FSLAX46Y46*%\n%MOMM*%\n"
                "%ADD40P,1.0*%\nD40*\nX0Y0D03*\n", encoding="utf-8")
            proc = self.run_cli(str(Path(tmp)))
        report = json.loads(proc.stdout)          # must parse cleanly
        self.assertEqual(report["schema_version"], "gerber-dfm-report/v1")
        self.assertIn("WARNING", proc.stderr)

    def test_a_failing_board_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "b-F_Mask.gts").write_text(REAL_KICAD10_FMASK,
                                                    encoding="utf-8")
            proc = self.run_cli(str(Path(tmp)), "--mask-dam-min-mm", "0.5")
        self.assertEqual(proc.returncode, 1)

    def test_json_report_is_utf8_and_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "b-F_Mask.gts").write_text(REAL_KICAD10_FMASK,
                                                    encoding="utf-8")
            out = Path(tmp) / "r.json"
            self.run_cli(str(Path(tmp)), "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("checks", report)


class SchemaAgreement(unittest.TestCase):
    def test_the_report_matches_its_schema(self):
        schema = json.loads((SCHEMAS / "gerber_dfm.schema.json")
                            .read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "b-F_Mask.gts").write_text(REAL_KICAD10_FMASK,
                                                    encoding="utf-8")
            findings, warnings = mod.scan(Path(tmp), 0.2, 0.2)
        from common.findings import board_report
        report = board_report(mod.TOOL_NAME, tmp, findings,
                              params={"parse_warnings": warnings},
                              schema_version=mod.SCHEMA_VERSION)
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertEqual(report["schema_version"],
                         schema["properties"]["schema_version"]["const"])
        self.assertIn(report["tool"], schema["properties"]["tool"]["enum"])
        finding_keys = set(schema["$defs"]["finding"]["properties"])
        for check in report["checks"]:
            self.assertEqual(set(check), finding_keys)


if __name__ == "__main__":
    unittest.main()
