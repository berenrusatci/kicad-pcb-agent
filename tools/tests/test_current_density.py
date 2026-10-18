#!/usr/bin/env python3
"""Tests for scripts/board/current_density.py - the OPTIONAL DEPENDENCY tier.

Mirrors test_cross_section.py's structure: OptionalTierIsolation proves the
module imports, self-tests and collects with numpy/scipy ABSENT; the solver
correctness tests run only when the tier is actually present (this machine
has it, but the suite must not assume that); the pcbnew bridge tests are
skipped without pcbnew, same as every other board/*.py test module.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS  # noqa: E402,F401

import current_density as mod  # noqa: E402

TIER = mod.optional_tier_status()
HAVE_TIER = TIER["complete"]

try:
    import pcbnew  # noqa: F401
    HAVE_PCBNEW = True
except Exception:                                    # pragma: no cover
    HAVE_PCBNEW = False


class OptionalTierIsolation(unittest.TestCase):
    """These must ALL pass regardless of whether numpy/scipy are installed."""

    def test_the_module_imports_without_the_optional_packages(self):
        # It already did - this test module imported it at the top.
        self.assertIn("current_density", sys.modules)

    def test_no_optional_package_is_imported_at_module_level(self):
        source = (BOARD_SCRIPTS / "current_density.py").read_text(encoding="utf-8")
        import ast
        tree = ast.parse(source)
        top_level_imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports += [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.append(node.module)
        for pkg in ("numpy", "scipy", "matplotlib"):
            self.assertNotIn(pkg, top_level_imports,
                             f"{pkg} must only be imported inside a function")

    def test_the_tier_status_probe_does_not_import_anything(self):
        status = mod.optional_tier_status()
        self.assertIn("packages", status)
        self.assertIn("complete", status)
        self.assertEqual({"numpy", "scipy"}, set(status["packages"]))

    def test_point_in_polygon_needs_no_optional_package(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertTrue(mod._point_in_polygon(5, 5, square))
        self.assertFalse(mod._point_in_polygon(15, 5, square))

    @unittest.skipIf(HAVE_TIER, "the optional tier IS installed here")
    def test_solve_strip_raises_the_actionable_error_when_absent(self):
        with self.assertRaises(mod.OptionalDependencyMissing) as ctx:
            mod.solve_strip(10.0, 5.0, 1.0)
        self.assertIn("pip install numpy scipy", str(ctx.exception))

    def test_self_test_runs_without_crashing_either_way(self):
        proc = subprocess.run(
            [sys.executable, str(BOARD_SCRIPTS / "current_density.py"), "--self-test"],
            capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)


@unittest.skipUnless(HAVE_TIER, "numpy/scipy not installed")
class SolverCorrectness(unittest.TestCase):
    """The analytic case: a uniform copper strip must show a uniform
    current density (current_a / width_mm) away from injection edges."""

    def test_uniform_strip_matches_analytic_density(self):
        mesh = mod.solve_strip(length_mm=20.0, width_mm=10.0, current_a=5.0,
                               copper_oz=1.0, n_cols=40, n_rows=10)
        expected_a_m = 5.0 / (10.0 / 1000.0)
        middle = mesh.current_density_a_m[:, mesh.n_cols // 2 - 1]
        import numpy as np
        observed = float(np.nanmean(middle))
        self.assertAlmostEqual(observed, expected_a_m, delta=expected_a_m * 0.05)

    def test_narrower_strip_has_higher_density_same_current(self):
        wide = mod.solve_strip(20.0, 10.0, 5.0, n_cols=40, n_rows=10)
        narrow = mod.solve_strip(20.0, 2.0, 5.0, n_cols=40, n_rows=10)
        self.assertGreater(narrow.max_current_density_a_m, wide.max_current_density_a_m)

    def test_solve_polygon_uses_real_corners_not_bounding_box(self):
        # An L-shape: the notch corner must NOT be conductive.
        l_shape = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]
        mesh = mod.solve_polygon(l_shape, current_a=1.0, resolution_mm=1.0)
        import numpy as np
        # The notch region (x>5, y>5) should be entirely NaN (non-conductive).
        self.assertTrue(np.isnan(mesh.node_voltage_v[-1, -1]))

    def test_degenerate_polygon_raises(self):
        with self.assertRaises(ValueError):
            mod.solve_polygon([(0, 0), (0, 0), (0, 0)], current_a=1.0)

    def test_save_heatmap_writes_a_file(self):
        mesh = mod.solve_strip(10.0, 5.0, 1.0, n_cols=20, n_rows=5)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "heat.png"
            written = mod.save_heatmap(mesh, out)
            self.assertEqual(out, written)
            self.assertTrue(out.exists())


@unittest.skipUnless(HAVE_PCBNEW, "pcbnew (KiCad SWIG) is not importable")
class PcbnewBridge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_net_with_no_tracks_is_no_coverage(self):
        import board_fixture
        path = board_fixture.clean_board(self.tmp / "b.kicad_pcb")
        finding, mesh = mod.current_density_for_net(
            str(path), "NOPE_NOT_A_REAL_NET", 1.0, str(self.tmp))
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertIsNone(mesh)

    @unittest.skipUnless(HAVE_TIER, "numpy/scipy not installed")
    def test_gnd_net_solves_and_writes_a_png(self):
        import board_fixture
        path = board_fixture.clean_board(self.tmp / "b.kicad_pcb")
        finding, mesh = mod.current_density_for_net(
            str(path), "GND", 1.0, str(self.tmp))
        self.assertIn(finding["status"], ("PASS", "NO_COVERAGE"))
        if finding["status"] == "PASS":
            self.assertIsNotNone(mesh)
            self.assertTrue((self.tmp / "current_density_GND.png").exists())


class CliContract(unittest.TestCase):
    SCRIPT = BOARD_SCRIPTS / "current_density.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False)

    def test_missing_args_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(2, proc.returncode)

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(0, proc.returncode)
        self.assertEqual("", proc.stdout)


if __name__ == "__main__":
    unittest.main()
