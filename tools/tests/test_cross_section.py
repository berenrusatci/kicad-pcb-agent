#!/usr/bin/env python3
"""Tests for scripts/report/cross_section.py - the OPTIONAL DEPENDENCY tier.

Converted from the archived pytest suite to unittest.

The most important tests here are in ``OptionalTierIsolation``: importing this
module, running its self-test and collecting the suite must all work with
svglib, reportlab and Pillow ABSENT, and when they are absent the tool must
fail with one clear message naming them - not an ImportError from the middle
of a render.

The render path itself is skipped unless the whole tier plus kicad-cli is
present; a mocked render would prove nothing about the actual pixels.
"""

from __future__ import annotations

import ast
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

REPORT_SCRIPTS = SCRIPTS / "report"
sys.path.insert(0, str(REPORT_SCRIPTS))
import cross_section as mod  # noqa: E402

TIER = mod.optional_tier_status()
HAVE_TIER = TIER["complete"]
HAVE_KICAD_CLI = shutil.which("kicad-cli") is not None

RECT_BOARD = mod._RECT_BOARD
ROUND_BOARD = mod._ROUND_BOARD


class BoundsParsing(unittest.TestCase):
    def test_a_rectangular_outline(self):
        bounds = mod.bounds_from_text(RECT_BOARD)
        self.assertEqual((bounds.x_min, bounds.y_min, bounds.x_max,
                          bounds.y_max), (-10, -5, 10, 5))
        self.assertEqual((bounds.width, bounds.height), (20, 10))

    def test_fault_injection_an_f_cu_line_must_not_widen_the_outline(self):
        """Proves the layer filter is actually applied."""
        widened = RECT_BOARD.replace(
            '(gr_line (start 0 0) (end 1 1) (layer "F.Cu") (uuid "e"))',
            '(gr_line (start -99 -99) (end 99 99) (layer "F.Cu") (uuid "e"))')
        bounds = mod.bounds_from_text(widened)
        self.assertEqual(bounds.x_max, 10)
        self.assertEqual(bounds.x_min, -10)

    def test_a_circular_outline_uses_centre_plus_radius(self):
        """A gr_circle's `end` is ONE point on the circumference; taking it
        literally collapses the bbox onto one edge."""
        bounds = mod.bounds_from_text(ROUND_BOARD)
        self.assertEqual((bounds.x_min, bounds.y_min, bounds.x_max,
                          bounds.y_max), (-21, -21, 21, 21))

    def test_nested_blocks_do_not_terminate_the_scan_early(self):
        """gr_circle contains (stroke (width ..) (type ..)); a naive regex
        stops at stroke's closing paren and never reaches gr_circle's. This
        was a real regression on a circular board outline."""
        self.assertIn("(stroke", ROUND_BOARD)
        self.assertEqual(mod.bounds_from_text(ROUND_BOARD).width, 42)

    def test_parens_inside_strings_are_skipped(self):
        text = ('(kicad_pcb (gr_line (start 0 0) (end 5 5) '
                '(layer "Edge.Cuts") (uuid "a)b")))')
        self.assertEqual(mod.bounds_from_text(text).x_max, 5)

    def test_a_board_with_no_edge_cuts_raises(self):
        with self.assertRaises(ValueError):
            mod.bounds_from_text('(kicad_pcb (gr_line (start 0 0) (end 1 1) '
                                 '(layer "F.Cu")))')

    def test_an_unterminated_block_raises(self):
        with self.assertRaises(ValueError):
            mod.bounds_from_text('(kicad_pcb (gr_line (start 0 0) '
                                 '(layer "Edge.Cuts")')

    def test_a_polygon_outline_uses_its_xy_points(self):
        text = ('(kicad_pcb (gr_poly (pts (xy 0 0) (xy 8 0) (xy 8 4) (xy 0 4)) '
                '(layer "Edge.Cuts")))')
        bounds = mod.bounds_from_text(text)
        self.assertEqual((bounds.x_max, bounds.y_max), (8, 4))

    def test_degenerate_bounds_are_refused(self):
        for bad in ((0, 0, 0, 5), (0, 0, 5, 0), (5, 0, 0, 5)):
            with self.assertRaises(ValueError):
                mod.Bounds(*bad)

    def test_bounds_from_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            path.write_text(RECT_BOARD, encoding="utf-8")
            self.assertEqual(mod.board_bounds(str(path)).width, 20)


class OptionalTierIsolation(unittest.TestCase):
    def test_the_module_imports_without_the_optional_packages(self):
        """It already did - this test module imported it at the top."""
        self.assertTrue(hasattr(mod, "bounds_from_text"))

    def test_no_optional_package_is_imported_at_module_level(self):
        tree = ast.parse((REPORT_SCRIPTS / "cross_section.py")
                         .read_text(encoding="utf-8"))
        forbidden = {"svglib", "reportlab", "PIL"}
        for node in tree.body:
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            self.assertFalse(roots & forbidden,
                             f"module-level import of {roots & forbidden}")

    def test_the_optional_imports_live_inside_the_one_helper(self):
        import inspect
        source = inspect.getsource(mod._require_optional_tier)
        for package in ("svglib", "reportlab", "PIL"):
            self.assertIn(package, source)

    def test_the_self_test_runs_without_the_optional_tier(self):
        self.assertEqual(mod.self_test(), [])

    def test_the_tier_status_probe_does_not_import_anything(self):
        status = mod.optional_tier_status()
        self.assertEqual(set(status["packages"]),
                         {"svglib", "reportlab", "pillow"})
        self.assertIsInstance(status["complete"], bool)

    def test_a_missing_tier_produces_one_actionable_message(self):
        with mock.patch.object(mod, "optional_tier_status", return_value={
                "packages": {"svglib": False, "reportlab": False,
                             "pillow": True},
                "pdf_to_png": None, "complete": False}):
            with self.assertRaises(mod.OptionalDependencyMissing) as caught:
                mod._require_optional_tier()
        message = str(caught.exception)
        self.assertIn("svglib", message)
        self.assertIn("reportlab", message)
        self.assertIn("poppler-utils", message)
        self.assertIn("pip install", message)
        self.assertIn("stdlib-only", message)

    def test_a_complete_tier_does_not_raise(self):
        if not HAVE_TIER:
            self.skipTest("the optional tier is not installed here")
        mod._require_optional_tier()

    def test_render_region_raises_the_actionable_error_when_absent(self):
        if HAVE_TIER:
            self.skipTest("the optional tier IS installed here")
        with self.assertRaises(mod.OptionalDependencyMissing):
            mod.render_region("b.kicad_pcb", "out.png", 0, 0, 1, 1)


class CliContract(unittest.TestCase):
    SCRIPT = REPORT_SCRIPTS / "cross_section.py"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.board = self.tmp / "b.kicad_pcb"
        self.board.write_text(RECT_BOARD, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        # encoding="utf-8" pinned for the same reason as test_stackup_plan.py's
        # run(): report_io's contract is UTF-8 output; text=True without an
        # explicit encoding falls back to the OS locale codepage to decode
        # it, which breaks on any report containing a character outside that
        # codepage.
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def test_missing_board_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_region_exits_2(self):
        proc = self.run_cli(str(self.board))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--region", proc.stderr)

    def test_a_malformed_region_exits_2(self):
        proc = self.run_cli(str(self.board), "--region", "1,2,3")
        self.assertEqual(proc.returncode, 2)

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("SELF-TEST PASS", proc.stderr)

    def test_stdout_is_json_and_the_dependency_message_goes_to_stderr(self):
        proc = self.run_cli(str(self.board), "--region", "-5,-2,5,2",
                            "--out", str(self.tmp / "s.png"))
        report = json.loads(proc.stdout)      # must parse even when it failed
        self.assertEqual(report["schema_version"], "cross-section-report/v1")
        if not HAVE_TIER:
            self.assertEqual(proc.returncode, 1)
            self.assertIn("pip install", proc.stderr)
            self.assertIsNone(report["params"]["output"])

    def test_the_report_declares_itself_inadmissible_as_a_measurement(self):
        proc = self.run_cli(str(self.board), "--region", "-5,-2,5,2",
                            "--out", str(self.tmp / "s.png"))
        params = json.loads(proc.stdout)["params"]
        self.assertTrue(params["visual_guidance_only"])
        self.assertFalse(params["measurement_admissible"])

    def test_the_report_records_which_optional_packages_are_present(self):
        proc = self.run_cli(str(self.board), "--region", "-5,-2,5,2",
                            "--out", str(self.tmp / "s.png"))
        tier = json.loads(proc.stdout)["params"]["optional_tier"]
        self.assertEqual(set(tier["packages"]),
                         {"svglib", "reportlab", "pillow"})

    def test_the_report_matches_its_schema(self):
        schema = json.loads((SCHEMAS / "cross_section.schema.json")
                            .read_text(encoding="utf-8"))
        proc = self.run_cli(str(self.board), "--region", "-5,-2,5,2",
                            "--out", str(self.tmp / "s.png"))
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertIn(report["tool"], schema["properties"]["tool"]["enum"])

    @unittest.skipUnless(HAVE_TIER and HAVE_KICAD_CLI,
                         "needs svglib + reportlab + Pillow + poppler + "
                         "kicad-cli")
    def test_a_real_render_produces_a_png(self):
        proc = self.run_cli(str(self.board), "--region", "-5,-2,5,2",
                            "--out", str(self.tmp / "s.png"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.tmp / "s.png").is_file())


class Documentation(unittest.TestCase):
    def test_the_accuracy_boundary_is_stated_in_the_docstring(self):
        text = mod.__doc__ or ""
        self.assertIn("VISUAL ORIENTATION ONLY", text)
        self.assertIn("0.06", text)

    def test_the_optional_tier_is_documented(self):
        text = mod.__doc__ or ""
        self.assertIn("OPTIONAL DEPENDENCY TIER", text)
        for package in ("svglib", "reportlab", "Pillow"):
            self.assertIn(package, text)


if __name__ == "__main__":
    unittest.main()
