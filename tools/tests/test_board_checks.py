#!/usr/bin/env python3
"""Regression + fault-injection tests for the four pcbnew board checkers.

These tools previously had ZERO tests. Each test here builds a board with
pcbnew (see board_fixture.py), runs a check, and asserts both the clean
result and that an injected defect actually flips the verdict.

Skipped in full when ``pcbnew`` is unavailable, so the rest of the suite
still runs on a machine without KiCad.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS, TOOLS  # noqa: E402,F401

try:
    import pcbnew  # noqa: F401
    HAVE_PCBNEW = True
except Exception:                                    # pragma: no cover
    HAVE_PCBNEW = False

# The only token that makes ZONE::IsTeardropArea() true in a saved board.
TEARDROP_ATTR = "(attr (teardrop (type padvia)))"


def board_with_teardrop_zones(path: Path, teardrops: int, plain: int) -> Path:
    """A board carrying ``teardrops`` teardrop zones and ``plain`` normal ones.

    Built by hand rather than in board_fixture because of an API limit that is
    the whole reason ``check_teardrops`` exists: pcbnew 10.0.5 SWIG does NOT
    wrap ``TEARDROP_TYPE``, so ``ZONE::SetTeardropAreaType`` is uncallable from
    Python and a teardrop zone CANNOT be built through the API (SKILL-dfm §3:
    teardrop generation is a GUI step). The zones are therefore written as
    ordinary copper zones and the teardrop attribute is injected into the saved
    s-expression.

    The result is read back and counted here, so a KiCad file-format change
    fails this fixture loudly instead of quietly producing a board with no
    teardrops on it - which would make the NO_COVERAGE test below pass for
    entirely the wrong reason.
    """
    import pcbnew

    board = pcbnew.NewBoard(str(path))
    for index in range(teardrops + plain):
        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.F_Cu)
        outline = zone.Outline()
        outline.NewOutline()
        for dx, dy in ((0, 0), (5, 0), (5, 5), (0, 5)):
            outline.Append(pcbnew.FromMM(index * 10 + dx), pcbnew.FromMM(dy))
        board.Add(zone)
    pcbnew.SaveBoard(str(path), board)

    if teardrops:                       # count=0 would mean "replace all"
        text = path.read_text(encoding="utf-8")
        patched, count = re.subn(r"\(zone\n(\s*)\(layer",
                                 rf"(zone\n\1{TEARDROP_ATTR}\n\1(layer",
                                 text, count=teardrops)
        if count != teardrops:
            raise AssertionError(f"patched {count} zones, wanted {teardrops}")
        path.write_text(patched, encoding="utf-8")

    marked = sum(1 for z in pcbnew.LoadBoard(str(path)).Zones()
                 if z.IsTeardropArea())
    if marked != teardrops:
        raise AssertionError(
            f"fixture is not measuring what it claims: {marked} teardrop "
            f"zone(s) survived the round trip, expected {teardrops}. The "
            "KiCad zone format may have changed.")
    return path


@unittest.skipUnless(HAVE_PCBNEW, "pcbnew (KiCad SWIG) is not importable")
class BoardCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import board_fixture

        import dfm_emc_check
        import mask_dam
        import stitch_density
        import via_stub

        cls.fx = board_fixture
        cls.dfm = dfm_emc_check
        cls.mask = mask_dam
        cls.stitch = stitch_density
        cls.via = via_stub

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, builder, name):
        import pcbnew
        path = builder(self.tmp / name)
        return pcbnew.LoadBoard(str(path)), str(path)

    # ---------------------------------------------------------------- shape
    def test_report_envelope_has_schema_and_summary(self):
        from common.findings import board_report

        board, path = self.load(self.fx.clean_board, "clean.kicad_pcb")
        report = board_report("dfm_emc_check", path,
                              [self.dfm.check_via_in_pad(board)], params={})
        self.assertEqual("board-check-report/v1", report["schema_version"])
        self.assertEqual("dfm_emc_check", report["tool"])
        self.assertEqual({"PASS", "FAIL", "NO_COVERAGE"}, set(report["summary"]))
        self.assertIn("params", report)

    # ---------------------------------------------------------- teardrops
    def teardrop_board(self, teardrops: int, plain: int):
        import pcbnew

        path = board_with_teardrop_zones(
            self.tmp / f"td{teardrops}_{plain}.kicad_pcb", teardrops, plain)
        return pcbnew.LoadBoard(str(path)), str(path)

    def test_the_teardrop_count_is_a_field_not_a_sentence(self):
        """The measured number must be readable as DATA.

        It used to exist only inside the free-text ``detail``: the Finding was
        hand-built with ``[]`` as its violation list and ``scanned`` set to the
        board's TOTAL zone count, so nothing an assertion could address ever
        held the teardrop count.
        """
        board, _ = self.teardrop_board(teardrops=2, plain=1)
        finding = self.dfm.check_teardrops(board)
        self.assertEqual("PASS", finding["status"], finding["detail"])
        self.assertEqual(2, finding["scanned"])          # not 3 (all zones)
        self.assertIn("2 teardrop zone / 3 zone", finding["detail"])

    def test_zones_that_are_not_teardrops_are_not_coverage(self):
        """FAULT INJECTION: 3 ordinary zones, 0 teardrops.

        The old code reported NO_COVERAGE with ``scanned = 3``, so a coverage
        guard bound to ``by_check.teardrop_count.scanned >= 1`` opened on a
        board with no teardrop on it at all.
        """
        board, _ = self.teardrop_board(teardrops=0, plain=3)
        finding = self.dfm.check_teardrops(board)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])
        self.assertEqual([], finding["violations"])

    def test_the_envelope_exposes_the_count_under_the_check_name(self):
        from common.findings import board_report

        board, path = self.teardrop_board(teardrops=2, plain=1)
        report = board_report("dfm_emc_check", path,
                              [self.dfm.check_teardrops(board)], params={})
        self.assertEqual(2, report["by_check"]["teardrop_count"]["scanned"])
        self.assertEqual("PASS", report["by_check"]["teardrop_count"]["status"])
        # `metrics` is a VIOLATION count and this check has no sourced limit
        # ("how many teardrops must a board have" is not a rule anyone can
        # cite), so it stays 0 by design. A rule that wants the number must
        # bind by_check, which is why the count now lives in `scanned`.
        self.assertEqual(0, report["metrics"]["teardrop_count"])

    # ------------------------------------------------------- annular ring
    def test_clean_board_annular_ring_passes(self):
        board, _ = self.load(self.fx.clean_board, "clean.kicad_pcb")
        finding = self.dfm.check_annular(board, 0.15)
        self.assertEqual("PASS", finding["status"], finding["detail"])
        self.assertGreater(finding["scanned"], 0)

    def test_small_annular_ring_is_detected(self):
        """FAULT INJECTION: a 0.30/0.25 via must flip annular_ring to FAIL."""
        board, _ = self.load(self.fx.board_with_small_annular_ring,
                             "annular.kicad_pcb")
        finding = self.dfm.check_annular(board, 0.15)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, len(finding["violations"]))

    # -------------------------------------------------------- edge keepout
    def test_clean_board_edge_keepout_passes(self):
        board, _ = self.load(self.fx.clean_board, "clean.kicad_pcb")
        finding = self.dfm.check_edge_keepout(board, 2.0)
        self.assertEqual("PASS", finding["status"], finding["detail"])

    def test_ceramic_near_edge_is_detected(self):
        """FAULT INJECTION: C9 at 0.9 mm from the edge must FAIL a 2 mm keepout."""
        board, _ = self.load(self.fx.board_with_edge_ceramic, "edge.kicad_pcb")
        finding = self.dfm.check_edge_keepout(board, 2.0)
        self.assertEqual("FAIL", finding["status"])
        refs = {v.get("ref") or v.get("footprint") for v in finding["violations"]}
        self.assertTrue(any("C9" in str(r) for r in refs), finding["violations"])

    # ------------------------------------------------------------ coverage
    def test_no_vias_reports_no_coverage_not_pass(self):
        """The core discipline: an empty scope is NO_COVERAGE, never PASS."""
        board, _ = self.load(self.fx.board_without_stitching, "bare.kicad_pcb")
        gnd = self.stitch.gnd_vias(board, r"^/?(GND|AGND|DGND|VSS)")
        self.assertEqual([], gnd)
        target = self.stitch.target_spacing_mm(5.0, 4.5)
        finding = self.stitch.check_via_return_stitching(board, gnd, target)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_sparse_stitching_fails_edge_check(self):
        """FAULT INJECTION: 3 widely spaced vias must FAIL the lambda/20 target."""
        board, _ = self.load(self.fx.board_with_sparse_stitching,
                             "sparse.kicad_pcb")
        gnd = self.stitch.gnd_vias(board, r"^/?(GND|AGND|DGND|VSS)")
        self.assertEqual(3, len(gnd))
        target = self.stitch.target_spacing_mm(5.0, 4.5)
        finding = self.stitch.check_edge_stitching(board, gnd, target, 1.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertGreater(len(finding["violations"]), 0)

    def test_target_spacing_is_lambda_over_20(self):
        # 5 GHz, er_eff 4.5 -> v = c/sqrt(4.5); lambda/20 in mm
        import math
        from common.units import C_MPS
        expected = ((C_MPS / math.sqrt(4.5)) / 5e9) * 1000.0 / 20.0
        self.assertAlmostEqual(expected,
                               self.stitch.target_spacing_mm(5.0, 4.5), places=9)

    # ------------------------------------------------------------ mask dam
    def test_mask_dam_runs_and_reports_coverage(self):
        board, _ = self.load(self.fx.clean_board, "clean.kicad_pcb")
        finding = self.mask.check_mask_dam(board, 0.20, 3.0, 0.05)
        self.assertIn(finding["status"], {"PASS", "FAIL", "NO_COVERAGE"})
        self.assertIsInstance(finding["scanned"], int)

    # ------------------------------------------------------------ via stub
    def test_via_stub_without_stackup_is_no_coverage(self):
        """A board with no explicit stackup must not yield a PASS."""
        board, path = self.load(self.fx.clean_board, "clean.kicad_pcb")
        stackup, status = self.via.parse_stackup(path)
        self.assertIsNone(stackup)
        finding = self.via.check_via_stubs(board, stackup, status,
                                           r"(?i)(usb|clk)", 5.0, "test",
                                           2.0, None)
        self.assertEqual("NO_COVERAGE", finding["status"])

    def test_via_stub_xy_rounding_is_explicit(self):
        """via_stub rounds; the shared helper does not. The fork is now named."""
        from common.units import xy, xy_rounded

        class P:
            x = 1234567
            y = 7654321

        self.assertNotEqual(xy(P()), xy_rounded(P(), 4))
        self.assertEqual(self.via.xy(P()), xy_rounded(P(), 4))


if __name__ == "__main__":
    unittest.main()
