#!/usr/bin/env python3
"""Tests for scripts/sim/openems.py.

Nothing in this file requires CSXCAD/openEMS itself (see the module
docstring's "WHAT THIS PORT DELIBERATELY DOES NOT INCLUDE" section) - the
FDTD run chain was never ported, only the parts independently verifiable
without it: geometry extraction, differential-pair matching, the stackup
Z-lookup, and Touchstone evaluation (skrf-gated, optional).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

SIM_SCRIPTS = SCRIPTS / "sim"
sys.path.insert(0, str(SIM_SCRIPTS))

import openems as mod  # noqa: E402

try:
    import pcbnew  # noqa: F401
    HAVE_PCBNEW = True
except Exception:                                    # pragma: no cover
    HAVE_PCBNEW = False

try:
    import skrf  # noqa: F401
    HAVE_SKRF = True
except Exception:                                    # pragma: no cover
    HAVE_SKRF = False


class LayerZLookupTests(unittest.TestCase):
    STACKUP = {"L1": "SIGNAL", "L2": "GND", "L3": "PWR", "L4": "SIGNAL"}

    def test_insertion_order_is_the_z_source(self):
        self.assertEqual((0.0, mod.LAYER_THICKNESS_ASSUMPTION_MM),
                         mod.layer_z_position(self.STACKUP, "L1"))
        self.assertEqual(
            (mod.LAYER_THICKNESS_ASSUMPTION_MM, 2 * mod.LAYER_THICKNESS_ASSUMPTION_MM),
            mod.layer_z_position(self.STACKUP, "L2"))

    def test_unknown_layer_returns_none_not_a_guess(self):
        self.assertIsNone(mod.layer_z_position(self.STACKUP, "NOPE"))

    def test_explicit_thickness_overrides_the_default(self):
        z = mod.layer_z_position(self.STACKUP, "L2", layer_thickness_mm=0.2)
        self.assertEqual((0.2, 0.4), z)


class _FakeBoard:
    def __init__(self, net_names):
        self._names = net_names

    def GetNetsByName(self):
        return {n: None for n in self._names}


class DifferentialPairMatchingTests(unittest.TestCase):
    def test_suffix_pairs_match(self):
        board = _FakeBoard(["MIPI_D0_P", "MIPI_D0_N", "MIPI_D1_P", "MIPI_D1_N"])
        pairs = mod.match_differential_pairs(board)
        self.assertEqual([("MIPI_D0_P", "MIPI_D0_N"), ("MIPI_D1_P", "MIPI_D1_N")], pairs)

    def test_unrelated_nets_are_excluded(self):
        board = _FakeBoard(["LED0", "GPIO_D0_EN", "MIPI_D0_P"])
        self.assertEqual([], mod.match_differential_pairs(board))

    def test_empty_net_name_is_ignored(self):
        board = _FakeBoard(["", "D0_P", "D0_N"])
        self.assertEqual([("D0_P", "D0_N")], mod.match_differential_pairs(board))

    def test_custom_suffixes(self):
        board = _FakeBoard(["CLK_POS", "CLK_NEG"])
        pairs = mod.match_differential_pairs(board, suffix_pos="_POS", suffix_neg="_NEG")
        self.assertEqual([("CLK_POS", "CLK_NEG")], pairs)


class EvaluateTouchstoneTests(unittest.TestCase):
    def test_missing_file_is_fail_not_no_coverage(self):
        finding = mod.evaluate_touchstone("does-not-exist.s4p", 100.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, finding["scanned"])

    @unittest.skipIf(HAVE_SKRF, "skrf IS installed here")
    def test_missing_skrf_is_no_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "x.s4p"
            fake.write_text("! not a real touchstone file\n", encoding="utf-8")
            finding = mod.evaluate_touchstone(str(fake), 100.0)
            self.assertEqual("NO_COVERAGE", finding["status"])

    @unittest.skipUnless(HAVE_SKRF, "skrf not installed")
    def test_a_real_touchstone_file_evaluates(self):
        import numpy as np
        import skrf as rf

        f = np.linspace(1e9, 6e9, 11)
        s = np.zeros((len(f), 2, 2), dtype=complex)
        s[:, 0, 0] = 0.01  # ~-40dB return loss, well under the -10dB limit
        s[:, 1, 1] = 0.01
        s[:, 0, 1] = 0.99
        s[:, 1, 0] = 0.99
        network = rf.Network(f=f, s=s, z0=100.0, f_unit="Hz")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clean.s2p"
            network.write_touchstone(str(path))
            finding = mod.evaluate_touchstone(str(path), 100.0)
            self.assertEqual("PASS", finding["status"])
            self.assertEqual(2, finding["scanned"])

    @unittest.skipUnless(HAVE_SKRF, "skrf not installed")
    def test_excess_return_loss_is_a_violation(self):
        import numpy as np
        import skrf as rf

        f = np.linspace(1e9, 6e9, 11)
        s = np.zeros((len(f), 1, 1), dtype=complex)
        s[:, 0, 0] = 0.5  # ~-6dB, worse than the -10dB limit -> violation
        network = rf.Network(f=f, s=s, z0=100.0, f_unit="Hz")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.s1p"
            network.write_touchstone(str(path))
            finding = mod.evaluate_touchstone(str(path), 100.0, min_return_loss_db=-10.0)
            self.assertEqual("FAIL", finding["status"])
            self.assertEqual(1, len(finding["violations"]))


@unittest.skipUnless(HAVE_PCBNEW, "pcbnew (KiCad SWIG) is not importable")
class PairGeometryExtractionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_nets_with_no_tracks_are_no_coverage(self):
        import board_fixture
        path = board_fixture.clean_board(self.tmp / "b.kicad_pcb")
        finding, geometry = mod.extract_pair_geometry(str(path), "NOPE_P", "NOPE_N")
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertIsNone(geometry)


class OpenemsInstalledProbeTests(unittest.TestCase):
    def test_probe_does_not_raise_either_way(self):
        # Whatever the answer is on this machine, the probe itself must not
        # import CSXCAD/openEMS (nothing else in this file needs them).
        self.assertIsInstance(mod.openems_installed(), bool)


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = SIM_SCRIPTS / "openems.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False)

    def test_no_mode_flag_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(2, proc.returncode)

    def test_self_test_is_opt_in_and_stderr_only(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(0, proc.returncode)
        self.assertEqual("", proc.stdout)

    def test_evaluate_touchstone_missing_file_exits_1(self):
        proc = self.run_cli("--evaluate-touchstone", "no-such-file.s4p")
        self.assertEqual(1, proc.returncode)

    def test_pair_geometry_without_net_flags_exits_2(self):
        proc = self.run_cli("--pair-geometry", "board.kicad_pcb")
        self.assertEqual(2, proc.returncode)


if __name__ == "__main__":
    unittest.main()
