#!/usr/bin/env python3
"""Tests for scripts/route/safe_write.py.

The pcbnew/kicad-cli-dependent functions (check_netclass_widths_from_board,
write_verify_or_rollback) are skipped without pcbnew, same as every other
board/route module in this suite - what's always testable is the pure
netclass-width logic.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

ROUTE_SCRIPTS = SCRIPTS / "route"
sys.path.insert(0, str(ROUTE_SCRIPTS))

import safe_write as mod  # noqa: E402

try:
    import pcbnew  # noqa: F401
    HAVE_PCBNEW = True
except Exception:                                    # pragma: no cover
    HAVE_PCBNEW = False


class NetclassWidthViolationsTests(unittest.TestCase):
    def test_widths_above_minimum_are_clean(self):
        violations = mod.netclass_width_violations(
            {"Default": 0.2, "HighSpeed": 0.15}, 0.1)
        self.assertEqual([], violations)

    def test_fault_injection_narrow_netclass_is_caught(self):
        """Clean scenario passes first, then a deliberately narrow
        netclass is injected and must flip the result - proves the check
        is not empty."""
        clean = mod.netclass_width_violations({"Default": 0.2}, 0.1)
        self.assertEqual([], clean, "clean scenario should pass before fault injection")

        broken = mod.netclass_width_violations({"Default": 0.2, "TooNarrow": 0.05}, 0.1)
        self.assertEqual(1, len(broken))
        self.assertEqual("TooNarrow", broken[0]["netclass"])
        self.assertEqual(0.05, broken[0]["defined_track_width_mm"])
        self.assertEqual(0.1, broken[0]["board_min_track_width_mm"])

    def test_width_exactly_at_minimum_is_not_a_violation(self):
        self.assertEqual([], mod.netclass_width_violations({"X": 0.1}, 0.1))


class CheckNetclassWidthsTests(unittest.TestCase):
    def test_no_coverage_when_no_netclasses(self):
        finding = mod.check_netclass_widths({}, 0.1)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_pass_when_all_widths_are_fine(self):
        finding = mod.check_netclass_widths({"Default": 0.2}, 0.1)
        self.assertEqual("PASS", finding["status"])
        self.assertEqual(1, finding["scanned"])

    def test_fail_when_a_width_violates(self):
        finding = mod.check_netclass_widths({"Default": 0.2, "Bad": 0.05}, 0.1)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, len(finding["violations"]))


@unittest.skipUnless(HAVE_PCBNEW, "pcbnew (KiCad SWIG) is not importable")
class PcbnewBridgedTests(unittest.TestCase):
    def test_placeholder_requires_pcbnew(self):
        pass  # real-board coverage lives in the pcbnew-gated suite elsewhere


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = ROUTE_SCRIPTS / "safe_write.py"

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


if __name__ == "__main__":
    unittest.main()
