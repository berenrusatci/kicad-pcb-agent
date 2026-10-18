#!/usr/bin/env python3
"""Tests for scripts/sim/ai_feedback_loop.py.

The loop is driven entirely by injected callbacks in these tests - no real
openEMS/PathWave is called anywhere (see the module's own docstring for
why that integration is deliberately not built). ``apply_trace_geometry``'s
pcbnew-dependent path uses a fake pcbnew module, same discipline as
test_diff_pair.py / test_mechanical_dxf.py.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

SIM_SCRIPTS = SCRIPTS / "sim"
sys.path.insert(0, str(SIM_SCRIPTS))

import ai_feedback_loop as mod  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Log parsing
# --------------------------------------------------------------------------- #
class ParseSimulationLogTests(unittest.TestCase):
    def test_canonical_dict_passes_through(self):
        log = mod.parse_simulation_log(
            {"metric": "differential_impedance", "current_value": 115.4,
             "target_value": 100.0, "status": "FAIL"})
        self.assertEqual(115.4, log["current_value"])
        self.assertEqual("FAIL", log["status"])

    def test_canonical_json_string_is_parsed(self):
        text = json.dumps({"metric": "differential_impedance", "current_value": 100.0,
                          "target_value": 100.0, "status": "PASS"})
        log = mod.parse_simulation_log(text)
        self.assertEqual("PASS", log["status"])

    def test_invalid_json_string_raises(self):
        with self.assertRaises(mod.SimulationLogError):
            mod.parse_simulation_log("{not json")

    def test_unrecognised_shape_raises_not_guessed(self):
        with self.assertRaises(mod.SimulationLogError):
            mod.parse_simulation_log({"some_other_key": 1})

    def test_wrong_type_raises(self):
        with self.assertRaises(mod.SimulationLogError):
            mod.parse_simulation_log(12345)

    def test_invalid_status_value_raises(self):
        with self.assertRaises(mod.SimulationLogError):
            mod.parse_simulation_log({"metric": "x", "current_value": 1.0,
                                      "target_value": 1.0, "status": "MAYBE"})

    def test_non_numeric_value_raises(self):
        with self.assertRaises(mod.SimulationLogError):
            mod.parse_simulation_log({"metric": "x", "current_value": "high",
                                      "target_value": 100.0, "status": "FAIL"})

    def test_touchstone_finding_shape_is_adapted(self):
        finding = {
            "check": "openems_touchstone_evaluation", "status": "FAIL", "scanned": 1,
            "violations": [{"port": 1, "max_return_loss_db": -6.0, "limit_db": -10.0}],
        }
        log = mod.parse_simulation_log(finding)
        self.assertEqual("return_loss_db", log["metric"])
        self.assertEqual(-6.0, log["current_value"])
        self.assertEqual(-10.0, log["target_value"])
        self.assertEqual("FAIL", log["status"])

    def test_touchstone_finding_picks_the_worst_of_several_violations(self):
        finding = {
            "check": "x", "status": "FAIL", "scanned": 2,
            "violations": [
                {"port": 1, "max_return_loss_db": -8.0, "limit_db": -10.0},
                {"port": 2, "max_return_loss_db": -4.0, "limit_db": -10.0},
            ],
        }
        log = mod.parse_simulation_log(finding)
        self.assertEqual(-4.0, log["current_value"])

    def test_touchstone_finding_with_no_violations_raises(self):
        finding = {"check": "x", "status": "PASS", "scanned": 1, "violations": []}
        with self.assertRaises(mod.SimulationLogError):
            mod.parse_simulation_log(finding)


# --------------------------------------------------------------------------- #
# 2. Correction engine - uses the real common.stackup solver
# --------------------------------------------------------------------------- #
class ComputeCorrectedGeometryTests(unittest.TestCase):
    def test_reachable_target_returns_a_geometry_close_to_it(self):
        corrected = mod.compute_corrected_geometry(target_ohm=100.0, h_mm=0.2, er=4.3)
        self.assertLess(abs(corrected.predicted_zdiff_ohm - 100.0), 5.0)
        self.assertGreater(corrected.w_mm, 0.0)
        self.assertGreater(corrected.s_mm, 0.0)

    def test_unreachable_target_raises_convergence_error(self):
        """An absurd target (1000 ohm differential) cannot be met by any
        W/S within the swept range on a normal stackup - must raise, not
        return a garbage-fit geometry. (Verified against the real solver:
        1000/500 ohm are unreachable at h=0.2mm/er=4.3, unlike a very LOW
        target which a wide/close pair can actually satisfy.)"""
        with self.assertRaises(mod.ConvergenceError):
            mod.compute_corrected_geometry(target_ohm=1000.0, h_mm=0.2, er=4.3)

    def test_uses_the_same_solver_common_stackup_exposes(self):
        """Proves this module did not reimplement a second impedance
        model - it calls common.stackup's own function."""
        from common.stackup import DiffPair, InterfaceKind, solve_impedance_geometry

        pair = DiffPair(name="x", interface=InterfaceKind.USB2_0, length_p_mm=0, length_n_mm=0,
                        target_impedance_ohm_override=90.0)
        direct = solve_impedance_geometry(pair, 0.2, 4.3, 0.035)
        corrected = mod.compute_corrected_geometry(target_ohm=90.0, h_mm=0.2, er=4.3, t_mm=0.035)
        self.assertAlmostEqual(direct["best"]["w_mm"], corrected.w_mm)
        self.assertAlmostEqual(direct["best"]["s_mm"], corrected.s_mm)


# --------------------------------------------------------------------------- #
# 3. pcbnew write-back
# --------------------------------------------------------------------------- #
class _FakeTrack:
    def __init__(self, net_name, width=0):
        self._net_name = net_name
        self._width = width
        self.cls = "PCB_TRACK"

    def GetClass(self):
        return self.cls

    def GetNetname(self):
        return self._net_name

    def SetWidth(self, w):
        self._width = w

    def GetWidth(self):
        return self._width


class _FakeBoard:
    def __init__(self, tracks):
        self._tracks = tracks
        self.saved_to = None

    def GetTracks(self):
        return self._tracks

    def Save(self, path):
        self.saved_to = path


class _FakePcbnewModule:
    def __init__(self, board):
        self._board = board

    def LoadBoard(self, path):
        return self._board

    def FromMM(self, value):
        return int(round(value * 1_000_000))


class ApplyTraceGeometryTests(unittest.TestCase):
    def setUp(self):
        self._orig = sys.modules.get("pcbnew")

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("pcbnew", None)
        else:
            sys.modules["pcbnew"] = self._orig

    def test_missing_pcbnew_raises_runtime_error(self):
        sys.modules.pop("pcbnew", None)
        with self.assertRaises(RuntimeError):
            mod.apply_trace_geometry("board.kicad_pcb", ("USB_DP", "USB_DM"), 0.2, 0.15)

    def test_only_matching_nets_are_updated(self):
        tracks = [_FakeTrack("USB_DP"), _FakeTrack("USB_DM"), _FakeTrack("GND")]
        board = _FakeBoard(tracks)
        sys.modules["pcbnew"] = _FakePcbnewModule(board)

        updated = mod.apply_trace_geometry("board.kicad_pcb", ("USB_DP", "USB_DM"), 0.25, 0.15)

        self.assertEqual(2, updated)
        self.assertEqual(int(round(0.25 * 1_000_000)), tracks[0].GetWidth())
        self.assertEqual(int(round(0.25 * 1_000_000)), tracks[1].GetWidth())
        self.assertEqual(0, tracks[2].GetWidth())
        self.assertEqual("board.kicad_pcb", board.saved_to)

    def test_no_matching_tracks_does_not_save(self):
        board = _FakeBoard([_FakeTrack("GND")])
        sys.modules["pcbnew"] = _FakePcbnewModule(board)
        updated = mod.apply_trace_geometry("board.kicad_pcb", ("USB_DP", "USB_DM"), 0.2, 0.15)
        self.assertEqual(0, updated)
        self.assertIsNone(board.saved_to)


# --------------------------------------------------------------------------- #
# 4. Loop controller - fully fake simulator, proves the control flow
# --------------------------------------------------------------------------- #
class RunFeedbackLoopTests(unittest.TestCase):
    def _state(self):
        return {"w_mm": 0.10, "s_mm": 0.10, "call": 0}

    def test_converges_on_a_pass_status(self):
        def sim():
            return {"metric": "differential_impedance", "current_value": 100.0,
                   "target_value": 100.0, "status": "PASS"}

        result = mod.run_feedback_loop(sim, lambda: (0.2, 0.15), lambda w, s: None,
                                       h_mm=0.2, er=4.3)
        self.assertTrue(result.converged)
        self.assertEqual(1, result.iterations_used)

    def test_converges_within_tolerance_even_without_pass_status(self):
        def sim():
            return {"metric": "differential_impedance", "current_value": 101.0,
                   "target_value": 100.0, "status": "FAIL"}  # 1% off, tolerance is 2%

        result = mod.run_feedback_loop(sim, lambda: (0.2, 0.15), lambda w, s: None,
                                       h_mm=0.2, er=4.3, tolerance_pct=2.0)
        self.assertTrue(result.converged)

    def test_deterministic_convergence_over_several_iterations(self):
        state = self._state()

        def sim():
            state["call"] += 1
            current = 130.0 - state["call"] * 15.0
            status = "PASS" if abs(current - 100.0) <= 2.0 else "FAIL"
            return {"metric": "differential_impedance", "current_value": current,
                   "target_value": 100.0, "status": status}

        applied = []
        result = mod.run_feedback_loop(
            sim, lambda: (state["w_mm"], state["s_mm"]),
            lambda w, s: applied.append((w, s)) or state.update(w_mm=w, s_mm=s),
            h_mm=0.2, er=4.3, max_iterations=15)
        self.assertTrue(result.converged)
        self.assertGreaterEqual(len(applied), 1)

    def test_exceeding_max_iterations_raises_convergence_error_not_a_fake_pass(self):
        def always_fails():
            return {"metric": "differential_impedance", "current_value": 200.0,
                   "target_value": 100.0, "status": "FAIL"}

        with self.assertRaises(mod.ConvergenceError):
            mod.run_feedback_loop(always_fails, lambda: (0.2, 0.15), lambda w, s: None,
                                  h_mm=0.2, er=4.3, max_iterations=3)

    def test_unparseable_log_raises_runtime_error_fail_closed(self):
        def broken_sim():
            return {"garbage": True}

        with self.assertRaises(RuntimeError):
            mod.run_feedback_loop(broken_sim, lambda: (0.2, 0.15), lambda w, s: None,
                                  h_mm=0.2, er=4.3)

    def test_unreachable_target_propagates_as_convergence_error(self):
        def sim():
            return {"metric": "differential_impedance", "current_value": 200.0,
                   "target_value": 1000.0, "status": "FAIL"}  # 1000 ohm - unreachable

        with self.assertRaises(mod.ConvergenceError):
            mod.run_feedback_loop(sim, lambda: (0.2, 0.15), lambda w, s: None,
                                  h_mm=0.2, er=4.3)

    def test_invalid_max_iterations_raises(self):
        with self.assertRaises(ValueError):
            mod.run_feedback_loop(lambda: {}, lambda: (0.2, 0.15), lambda w, s: None,
                                  h_mm=0.2, er=4.3, max_iterations=0)

    def test_invalid_tolerance_raises(self):
        with self.assertRaises(ValueError):
            mod.run_feedback_loop(lambda: {}, lambda: (0.2, 0.15), lambda w, s: None,
                                  h_mm=0.2, er=4.3, tolerance_pct=0.0)

    def test_history_records_every_iteration(self):
        state = self._state()

        def sim():
            state["call"] += 1
            current = 130.0 - state["call"] * 15.0
            status = "PASS" if abs(current - 100.0) <= 2.0 else "FAIL"
            return {"metric": "differential_impedance", "current_value": current,
                   "target_value": 100.0, "status": status}

        result = mod.run_feedback_loop(sim, lambda: (state["w_mm"], state["s_mm"]),
                                       lambda w, s: state.update(w_mm=w, s_mm=s),
                                       h_mm=0.2, er=4.3)
        self.assertEqual(result.iterations_used, len(result.history))
        self.assertTrue(result.history[-1].within_tolerance)


class SelfTestTests(unittest.TestCase):
    def test_self_test_module_exits_zero(self):
        import subprocess
        proc = subprocess.run([sys.executable, str(SIM_SCRIPTS / "ai_feedback_loop.py")],
                              capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
