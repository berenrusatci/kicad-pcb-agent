#!/usr/bin/env python3
"""Tests for scripts/route/diff_pair.py.

Same discipline as test_mechanical_dxf.py: a fake ``pcbnew`` module in
``sys.modules`` (real pcbnew is not importable in this environment), plus a
class that removes it entirely to prove the fail-closed NO_COVERAGE path is
real, not just documented.

MOST CRITICAL TEST: test_successful_pair_is_locked - directly verifies every
drawn track carries SetLocked(True) (the module's own "most critical rule").
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

ROUTE_SCRIPTS = SCRIPTS / "route"
sys.path.insert(0, str(ROUTE_SCRIPTS))

import diff_pair as mod  # noqa: E402

NM_PER_MM = 1_000_000


# --------------------------------------------------------------------------- #
# Fake pcbnew
# --------------------------------------------------------------------------- #
class _FakePoint:
    def __init__(self, x_mm: float, y_mm: float):
        self.x = int(round(x_mm * NM_PER_MM))
        self.y = int(round(y_mm * NM_PER_MM))


class _FakePad:
    def __init__(self, number: str, x_mm: float, y_mm: float, net_name: str):
        self._number = number
        self._pos = _FakePoint(x_mm, y_mm)
        self._net_name = net_name

    def GetNumber(self):
        return self._number

    def GetPosition(self):
        return self._pos

    def GetNetname(self):
        return self._net_name


class _FakeFootprint:
    def __init__(self, ref: str, pads: List[_FakePad]):
        self._ref = ref
        self._pads = pads

    def GetReference(self):
        return self._ref

    def Pads(self):
        return self._pads


class _FakeNet:
    def __init__(self, name: str):
        self._name = name

    def GetNetname(self):
        return self._name


class _FakeTrack:
    def __init__(self, board):
        self.board = board
        self.start = self.end = self.width = self.layer = self.net = None
        self.locked = False

    def SetStart(self, v):
        self.start = v

    def SetEnd(self, v):
        self.end = v

    def SetWidth(self, w):
        self.width = w

    def SetLayer(self, layer):
        self.layer = layer

    def SetNet(self, net):
        self.net = net

    def SetLocked(self, value: bool):
        self.locked = value


class _FakeBoard:
    def __init__(self, footprints: List[_FakeFootprint], nets: Optional[List[str]] = None):
        self._footprints = footprints
        self.nets: Dict[str, _FakeNet] = {n: _FakeNet(n) for n in (nets or [])}
        self.added_tracks: List[_FakeTrack] = []
        self.saved = False
        self.saved_path: Optional[str] = None

    def GetFootprints(self):
        return self._footprints

    def FindNet(self, name):
        return self.nets.get(name)

    def GetLayerID(self, name):
        return {"F.Cu": 0, "In2.Cu": 1, "B.Cu": 2}.get(name, 0)

    def Add(self, item):
        self.added_tracks.append(item)

    def Save(self, path):
        self.saved = True
        self.saved_path = path


class _FakePcbnewModule:
    def __init__(self, board: _FakeBoard):
        self._board = board

    def LoadBoard(self, path):
        return self._board

    def FromMM(self, value):
        return int(round(value * NM_PER_MM))

    def VECTOR2I(self, x, y):
        return (x, y)

    def PCB_TRACK(self, board):
        return _FakeTrack(board)


def _install_fake_pcbnew(board: _FakeBoard):
    sys.modules["pcbnew"] = _FakePcbnewModule(board)


def _parallel_board() -> _FakeBoard:
    """P: (0,0)->(10,0), N: (0,0.3)->(10,0.3) - fully parallel (0 deg),
    must route successfully."""
    fp1 = _FakeFootprint("J1", [_FakePad("1", 0.0, 0.0, "ETH_TRD0_P"),
                                _FakePad("2", 0.0, 0.3, "ETH_TRD0_N")])
    fp2 = _FakeFootprint("D1", [_FakePad("1", 10.0, 0.0, "ETH_TRD0_P"),
                                _FakePad("2", 10.0, 0.3, "ETH_TRD0_N")])
    return _FakeBoard([fp1, fp2], nets=["ETH_TRD0_P", "ETH_TRD0_N"])


class _PcbnewIsolation(unittest.TestCase):
    def setUp(self):
        self._orig = sys.modules.get("pcbnew")

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("pcbnew", None)
        else:
            sys.modules["pcbnew"] = self._orig


# --------------------------------------------------------------------------- #
# Pure geometry - no pcbnew needed
# --------------------------------------------------------------------------- #
class NetPadPositionsTests(unittest.TestCase):
    def test_finds_the_right_pads(self):
        board = _parallel_board()
        pads = mod.net_pad_positions(board, "ETH_TRD0_P")
        self.assertEqual(2, len(pads))
        self.assertEqual({(0.0, 0.0), (10.0, 0.0)}, {(p[2], p[3]) for p in pads})

    def test_unmatched_net_returns_empty_list(self):
        board = _parallel_board()
        self.assertEqual([], mod.net_pad_positions(board, "NO_SUCH_NET"))


class ClosestPairingTests(unittest.TestCase):
    def test_same_order_case_is_matched_correctly(self):
        p1, p2 = (0.0, 0.0), (10.0, 0.0)
        n1, n2 = (0.0, 0.3), (10.0, 0.3)
        p_start, n_start, p_end, n_end = mod.closest_pairing(p1, p2, n1, n2)
        self.assertEqual((p1, n1), (p_start, n_start))
        self.assertEqual((p2, n2), (p_end, n_end))

    def test_crossed_order_is_matched_by_physical_proximity(self):
        """N pads given in REVERSE order (n1 is actually close to p2) -
        the function must find the physically-correct pairing anyway."""
        p1, p2 = (0.0, 0.0), (10.0, 0.0)
        n1, n2 = (10.0, 0.3), (0.0, 0.3)  # reversed
        p_start, n_start, p_end, n_end = mod.closest_pairing(p1, p2, n1, n2)
        self.assertEqual(p1, p_start)
        self.assertEqual((0.0, 0.3), n_start)
        self.assertEqual(p2, p_end)
        self.assertEqual((10.0, 0.3), n_end)


class ParallelismAngleTests(unittest.TestCase):
    def test_fully_parallel_is_zero(self):
        angle = mod.parallelism_angle_deg((0.0, 0.0), (10.0, 0.0), (0.0, 0.3), (10.0, 0.3))
        self.assertAlmostEqual(0.0, angle, places=6)

    def test_perpendicular_is_ninety(self):
        angle = mod.parallelism_angle_deg((0.0, 0.0), (10.0, 0.0), (0.0, 0.0), (0.0, 10.0))
        self.assertAlmostEqual(90.0, angle, places=6)

    def test_zero_length_vector_returns_180_not_0(self):
        """Start==End (a zero-length vector) is an UNDEFINED angle - the
        function must not silently assume 0, the least-safe value (180) is
        returned instead."""
        angle = mod.parallelism_angle_deg((5.0, 5.0), (5.0, 5.0), (0.0, 0.0), (10.0, 0.0))
        self.assertEqual(180.0, angle)


# --------------------------------------------------------------------------- #
# lock_diff_pairs - main orchestration
# --------------------------------------------------------------------------- #
class LockDiffPairsTests(_PcbnewIsolation):
    def _board_path(self, tmp_path: Path, content: str = "( original-board-content )") -> str:
        """A REAL temp file path: at least one successful pair triggers a
        real (unmocked) ``shutil.copy`` for the .bak - a bare "board.kicad_pcb"
        string resolves against cwd and usually does not exist there."""
        path = tmp_path / "board.kicad_pcb"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def setUp(self):
        super().setUp()
        self._tmpdir = self._make_tmp_dir()

    def _make_tmp_dir(self):
        import tempfile
        self._tmp_obj = tempfile.TemporaryDirectory()
        return Path(self._tmp_obj.name)

    def tearDown(self):
        self._tmp_obj.cleanup()
        super().tearDown()

    def test_simple_parallel_pair_locks_successfully(self):
        board = _parallel_board()
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir),
            [mod.DiffPairRequest("ETH_TRD0_P", "ETH_TRD0_N")])

        self.assertEqual("PASS", finding["status"])
        self.assertEqual(1, finding["scanned"])
        self.assertEqual(1, len(outcomes))
        self.assertTrue(outcomes[0].locked)
        self.assertEqual(2, len(board.added_tracks))
        for track in board.added_tracks:
            self.assertTrue(track.locked, "MOST CRITICAL RULE: every drawn track must be locked")

    def test_large_angle_mismatch_refuses_and_draws_nothing(self):
        """P: (0,0)->(10,0) [0deg], N: (0,0)->(0,10) [90deg] - exceeds the
        15deg tolerance, must be REFUSED, nothing written to the board."""
        fp1 = _FakeFootprint("J1", [_FakePad("1", 0.0, 0.0, "ETH_TRD0_P"),
                                    _FakePad("2", 0.0, 0.0, "ETH_TRD0_N")])
        fp2 = _FakeFootprint("D1", [_FakePad("1", 10.0, 0.0, "ETH_TRD0_P"),
                                    _FakePad("2", 0.0, 10.0, "ETH_TRD0_N")])
        board = _FakeBoard([fp1, fp2], nets=["ETH_TRD0_P", "ETH_TRD0_N"])
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir),
            [mod.DiffPairRequest("ETH_TRD0_P", "ETH_TRD0_N")])

        self.assertEqual("FAIL", finding["status"])
        self.assertFalse(outcomes[0].locked)
        self.assertIn("pair-twist", outcomes[0].detail.lower().replace(" ", "-"))
        self.assertEqual([], board.added_tracks)

    def test_three_pad_net_refuses_and_names_the_upstream_tool(self):
        fp1 = _FakeFootprint("J1", [
            _FakePad("1", 0.0, 0.0, "SIG_P"), _FakePad("2", 0.0, 0.3, "SIG_N"),
            _FakePad("3", 5.0, 0.0, "SIG_P"),  # 3rd pad -> P net now has 3
        ])
        board = _FakeBoard([fp1], nets=["SIG_P", "SIG_N"])
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir), [mod.DiffPairRequest("SIG_P", "SIG_N")])

        self.assertFalse(outcomes[0].locked)
        self.assertIn("2-pad segments upstream", outcomes[0].detail)
        self.assertEqual([], board.added_tracks)

    def test_net_missing_from_board_level_registry_fails_closed(self):
        """Pads may carry a net name even when board.FindNet() cannot find
        it (inconsistent/corrupt board data) - must fail closed."""
        board = _parallel_board()
        board.nets = {}
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir),
            [mod.DiffPairRequest("ETH_TRD0_P", "ETH_TRD0_N")])

        self.assertFalse(outcomes[0].locked)
        self.assertIn("FindNet", outcomes[0].detail)
        self.assertEqual([], board.added_tracks)

    def test_empty_pair_list_is_no_coverage(self):
        board = _parallel_board()
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(self._board_path(self._tmpdir), [])

        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])
        self.assertEqual([], outcomes)

    def test_mixed_pass_and_fail_pairs_report_correctly(self):
        fp1 = _FakeFootprint("J1", [
            _FakePad("1", 0.0, 0.0, "ETH_TRD0_P"), _FakePad("2", 0.0, 0.3, "ETH_TRD0_N"),
            _FakePad("3", 0.0, 1.0, "USB_P"), _FakePad("4", 0.0, 1.0, "USB_N"),
        ])
        fp2 = _FakeFootprint("D1", [
            _FakePad("1", 10.0, 0.0, "ETH_TRD0_P"), _FakePad("2", 0.0, 10.0, "ETH_TRD0_N"),  # 90deg -> FAIL
            _FakePad("3", 10.0, 1.0, "USB_P"), _FakePad("4", 10.0, 1.3, "USB_N"),  # parallel -> PASS
        ])
        board = _FakeBoard([fp1, fp2], nets=["ETH_TRD0_P", "ETH_TRD0_N", "USB_P", "USB_N"])
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir),
            [mod.DiffPairRequest("ETH_TRD0_P", "ETH_TRD0_N"), mod.DiffPairRequest("USB_P", "USB_N")])

        self.assertEqual(2, finding["scanned"])
        self.assertEqual(1, len(finding["violations"]))
        locked_by_p = {o.p_net: o.locked for o in outcomes}
        self.assertFalse(locked_by_p["ETH_TRD0_P"])
        self.assertTrue(locked_by_p["USB_P"])
        self.assertEqual(2, len(board.added_tracks))  # only the USB pair (2 tracks)
        self.assertTrue(board.saved)  # at least one success -> saved

    def test_skew_is_computed_correctly(self):
        fp1 = _FakeFootprint("J1", [_FakePad("1", 0.0, 0.0, "SIG_P"), _FakePad("2", 0.0, 0.3, "SIG_N")])
        fp2 = _FakeFootprint("D1", [_FakePad("1", 10.0, 0.0, "SIG_P"), _FakePad("2", 10.05, 0.3, "SIG_N")])
        board = _FakeBoard([fp1, fp2], nets=["SIG_P", "SIG_N"])
        _install_fake_pcbnew(board)

        finding, outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir), [mod.DiffPairRequest("SIG_P", "SIG_N")])

        self.assertTrue(outcomes[0].locked)
        self.assertAlmostEqual(10.0, outcomes[0].p_length_mm, places=3)
        self.assertAlmostEqual(10.05, outcomes[0].n_length_mm, places=3)
        self.assertAlmostEqual(0.05, outcomes[0].skew_mm, places=3)

    def test_custom_width_and_layer_are_used(self):
        board = _parallel_board()
        _install_fake_pcbnew(board)

        pair = mod.DiffPairRequest("ETH_TRD0_P", "ETH_TRD0_N", width_mm=0.15, layer="In2.Cu")
        mod.lock_diff_pairs(self._board_path(self._tmpdir), [pair])

        for track in board.added_tracks:
            self.assertAlmostEqual(0.15 * NM_PER_MM, track.width)
            self.assertEqual(1, track.layer)  # In2.Cu -> _FakeBoard.GetLayerID mapping

    def test_custom_tolerance_is_applied(self):
        """A 20deg mismatch is REFUSED at the default 15deg tolerance but
        ACCEPTED once tolerance is raised to 25deg."""
        fp1 = _FakeFootprint("J1", [_FakePad("1", 0.0, 0.0, "SIG_P"), _FakePad("2", 0.0, 0.3, "SIG_N")])
        angle_rad = math.radians(20)
        n_end = (10.0 * math.cos(angle_rad), 10.0 * math.sin(angle_rad) + 0.3)
        fp2 = _FakeFootprint("D1", [_FakePad("1", 10.0, 0.0, "SIG_P"),
                                    _FakePad("2", n_end[0], n_end[1], "SIG_N")])

        board = _FakeBoard([fp1, fp2], nets=["SIG_P", "SIG_N"])
        _install_fake_pcbnew(board)
        _, default_outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir), [mod.DiffPairRequest("SIG_P", "SIG_N")])
        self.assertFalse(default_outcomes[0].locked)

        board2 = _FakeBoard([fp1, fp2], nets=["SIG_P", "SIG_N"])
        _install_fake_pcbnew(board2)
        _, wide_outcomes = mod.lock_diff_pairs(
            self._board_path(self._tmpdir), [mod.DiffPairRequest("SIG_P", "SIG_N")],
            tolerance_deg=25.0)
        self.assertTrue(wide_outcomes[0].locked)


class PcbnewMissingFallbackTests(unittest.TestCase):
    def test_missing_pcbnew_is_no_coverage(self):
        orig = sys.modules.pop("pcbnew", None)
        try:
            finding, outcomes = mod.lock_diff_pairs(
                "board.kicad_pcb", [mod.DiffPairRequest("A_P", "A_N")])
            self.assertEqual("NO_COVERAGE", finding["status"])
            self.assertEqual(0, finding["scanned"])
            self.assertEqual([], outcomes)
            self.assertIn("pcbnew module not found", finding["detail"])
        finally:
            if orig is not None:
                sys.modules["pcbnew"] = orig


class BackupDisciplineTests(_PcbnewIsolation):
    """Real filesystem, mocked pcbnew - the .bak write itself is real."""

    def setUp(self):
        super().setUp()
        import tempfile
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self):
        self._tmp_obj.cleanup()
        super().tearDown()

    def test_successful_route_creates_a_real_backup_file(self):
        board_file = self.tmp / "board.kicad_pcb"
        original_content = "( original-board-content )"
        board_file.write_text(original_content, encoding="utf-8")

        board = _parallel_board()
        _install_fake_pcbnew(board)

        mod.lock_diff_pairs(str(board_file), [mod.DiffPairRequest("ETH_TRD0_P", "ETH_TRD0_N")])

        backup = self.tmp / "board.kicad_pcb.bak"
        self.assertTrue(backup.is_file())
        self.assertEqual(original_content, backup.read_text(encoding="utf-8"))
        self.assertTrue(board.saved)
        self.assertEqual(str(board_file), board.saved_path)

    def test_no_backup_when_every_pair_fails(self):
        board_file = self.tmp / "board.kicad_pcb"
        board_file.write_text("( original )", encoding="utf-8")

        fp1 = _FakeFootprint("J1", [_FakePad("1", 0.0, 0.0, "P"), _FakePad("2", 0.0, 0.0, "N")])
        fp2 = _FakeFootprint("D1", [_FakePad("1", 10.0, 0.0, "P"), _FakePad("2", 0.0, 10.0, "N")])
        board = _FakeBoard([fp1, fp2], nets=["P", "N"])
        _install_fake_pcbnew(board)

        mod.lock_diff_pairs(str(board_file), [mod.DiffPairRequest("P", "N")])

        self.assertFalse((self.tmp / "board.kicad_pcb.bak").is_file())
        self.assertFalse(board.saved)


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


if __name__ == "__main__":
    unittest.main()
