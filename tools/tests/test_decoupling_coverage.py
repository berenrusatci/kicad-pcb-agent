#!/usr/bin/env python3
"""Tests for scripts/board/decoupling_coverage.py.

The heuristics (value parsing, IC detection, power-net matching) are pure
functions and are tested directly. The discovery loop is tested against a
FAKE board object that exposes only the pcbnew methods the module actually
calls - so the real coverage behaviour is proved without a KiCad install.

WHAT THIS FILE IS REALLY FOR: this module exists because
``measure_board.py --decoupling REF.PAD:REF.PAD`` can only check pairs the
operator NAMES, so a forgotten IC power pin silently stays outside coverage.
The tests below therefore focus on the discovery half - that a pin nobody
named still gets found, and that an unrecognised footprint is reported as
UNSCANNED rather than as clean.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS, TOOLS  # noqa: E402,F401

from scripts.board import decoupling_coverage as dc  # noqa: E402

SCRIPT = SCRIPTS / "board" / "decoupling_coverage.py"


# --------------------------------------------------------------------------- #
# Minimal pcbnew stand-ins: only the methods the module calls.
# --------------------------------------------------------------------------- #
class FakePoint:
    def __init__(self, x_mm: float, y_mm: float):
        # common.units.xy() divides by the KiCad internal unit (nm).
        self.x = int(x_mm * 1_000_000)
        self.y = int(y_mm * 1_000_000)


class FakePad:
    def __init__(self, number: str, net: str, x_mm: float, y_mm: float):
        self._number, self._net = number, net
        self._pos = FakePoint(x_mm, y_mm)

    def GetNumber(self):
        return self._number

    def GetNetname(self):
        return self._net

    def GetPosition(self):
        return self._pos


class FakeFPID:
    def __init__(self, name: str):
        self._name = name

    def GetLibItemName(self):
        return self._name


class FakeFootprint:
    def __init__(self, ref: str, footprint_id: str, value: str = "",
                 x_mm: float = 0.0, y_mm: float = 0.0, pads=()):
        self._ref, self._id, self._value = ref, footprint_id, value
        self._pos = FakePoint(x_mm, y_mm)
        self._pads = list(pads)

    def GetReference(self):
        return self._ref

    def GetFPID(self):
        return FakeFPID(self._id)

    def GetValue(self):
        return self._value

    def GetPosition(self):
        return self._pos

    def Pads(self):
        return self._pads


class FakeBoard:
    def __init__(self, footprints):
        self._footprints = list(footprints)

    def GetFootprints(self):
        return self._footprints


def _ic(ref="U1", fid="QFN-48-1EP_7x7mm", x=0.0, y=0.0, nets=("VDD",)):
    pads = [FakePad(str(i + 1), net, x, y) for i, net in enumerate(nets)]
    return FakeFootprint(ref, fid, "", x, y, pads)


def _cap(ref="C1", value="100nF", x=0.0, y=0.0):
    return FakeFootprint(ref, "C_0402_1005Metric", value, x, y, [])


class CapacitanceParsing(unittest.TestCase):
    def test_common_spellings(self):
        for text, expected in (("100nF", 100e-9), ("0.1uF", 0.1e-6),
                               ("100n", 100e-9), ("22pF", 22e-12),
                               ("4.7uF", 4.7e-6), ("1m", 1e-3)):
            with self.subTest(text=text):
                self.assertAlmostEqual(expected, dc.parse_capacitance_f(text),
                                       delta=expected * 1e-9)

    def test_fault_injection_an_unparsable_value_is_never_guessed(self):
        for bad in ("DNP", "", "100 ohm", "abc", "10k"):
            with self.subTest(bad=bad):
                self.assertIsNone(dc.parse_capacitance_f(bad))


class IcDetection(unittest.TestCase):
    def test_common_ic_packages_are_recognised(self):
        for fid in ("QFN-48-1EP_7x7mm", "BGA-256", "SOIC-8", "TSSOP-20",
                    "LQFP-64", "DFN-6", "SON-8"):
            with self.subTest(fid=fid):
                self.assertTrue(dc.is_ic_footprint(fid, 8))

    def test_passives_are_not_ics(self):
        for fid in ("R_0402_1005Metric", "C_0603_1608Metric", "L_0805"):
            with self.subTest(fid=fid):
                self.assertFalse(dc.is_ic_footprint(fid, 2))

    def test_sot_is_split_by_pad_count_not_by_name(self):
        """SOT-23-3 is usually a transistor; SOT-23-5/6 an LDO/ESD array."""
        self.assertFalse(dc.is_ic_footprint("SOT-23", 3))
        self.assertTrue(dc.is_ic_footprint("SOT-23-5", 5))


class PowerNetDetection(unittest.TestCase):
    def test_plain_rails_match(self):
        for net in ("VDD", "VCC", "+3V3", "3V3", "5V", "AVDD", "VBUS"):
            with self.subTest(net=net):
                self.assertTrue(dc.is_power_net(net))

    def test_project_specific_rails_match_on_their_token(self):
        self.assertTrue(dc.is_power_net("VDD_CAM_A"))
        self.assertTrue(dc.is_power_net("AVDD_SENSOR"))

    def test_signals_do_not_match(self):
        for net in ("SPI_MOSI", "GND", "MIPI_D0_P", ""):
            with self.subTest(net=net):
                self.assertFalse(dc.is_power_net(net))

    def test_a_rail_named_without_a_known_token_is_missed(self):
        """Documented boundary, pinned so it cannot silently change: this is
        a heuristic and CAMERA_SUPPLY is genuinely not matched."""
        self.assertFalse(dc.is_power_net("CAMERA_SUPPLY"))


class CandidateSelection(unittest.TestCase):
    def test_only_caps_in_the_decoupling_band_are_candidates(self):
        board = FakeBoard([
            _cap("C1", "100nF"),      # in band
            _cap("C2", "10uF"),       # bulk - out of band
            _cap("C3", "22pF"),       # RF - out of band
            _cap("C4", "47nF"),       # lower edge, in band
        ])
        refs = {c["ref"] for c in dc.find_capacitor_candidates(board)}
        self.assertEqual({"C1", "C4"}, refs)

    def test_a_cap_with_an_unparsable_value_is_dropped_not_guessed(self):
        board = FakeBoard([_cap("C1", "DNP"), _cap("C2", "100nF")])
        refs = {c["ref"] for c in dc.find_capacitor_candidates(board)}
        self.assertEqual({"C2"}, refs)


class CoverageDiscovery(unittest.TestCase):
    def test_a_close_capacitor_passes(self):
        board = FakeBoard([_ic("U1", x=0.0, y=0.0, nets=("VDD",)),
                           _cap("C1", "100nF", x=1.0, y=0.0)])
        finding = dc.check_decoupling_coverage(board, max_distance_mm=3.0)
        self.assertEqual("PASS", finding["status"])
        self.assertEqual(1, finding["scanned"])

    def test_fault_injection_a_distant_capacitor_fails(self):
        board = FakeBoard([_ic("U1", x=0.0, y=0.0, nets=("VDD",)),
                           _cap("C1", "100nF", x=20.0, y=0.0)])
        finding = dc.check_decoupling_coverage(board, max_distance_mm=3.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual("capacitor_too_far", finding["violations"][0]["problem"])
        self.assertAlmostEqual(20.0, finding["violations"][0]["distance_mm"])

    def test_fault_injection_no_capacitor_at_all_is_a_distinct_violation(self):
        """A missing part and a misplaced part need different fixes, so they
        must not collapse into one 'decoupling failed' count."""
        board = FakeBoard([_ic("U1", x=0.0, y=0.0, nets=("VDD",))])
        finding = dc.check_decoupling_coverage(board)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual("no_capacitor", finding["violations"][0]["problem"])

    def test_a_pin_nobody_named_is_still_discovered(self):
        """The whole point of this module: measure_board.py would only check
        the pairs the operator listed; here BOTH power pins are found."""
        board = FakeBoard([_ic("U1", x=0.0, y=0.0, nets=("VDD", "AVDD", "GND")),
                           _cap("C1", "100nF", x=1.0, y=0.0)])
        finding = dc.check_decoupling_coverage(board)
        self.assertEqual(2, finding["scanned"], "a power pin was missed")

    def test_the_nearest_capacitor_is_the_one_measured(self):
        board = FakeBoard([_ic("U1", x=0.0, y=0.0, nets=("VDD",)),
                           _cap("C_FAR", "100nF", x=50.0, y=0.0),
                           _cap("C_NEAR", "100nF", x=8.0, y=0.0)])
        finding = dc.check_decoupling_coverage(board, max_distance_mm=3.0)
        self.assertEqual("C_NEAR", finding["violations"][0]["nearest_capacitor"])

    def test_a_board_with_no_recognised_ic_is_no_coverage_not_a_pass(self):
        """'We looked and found nothing to check' must not read as clean."""
        board = FakeBoard([_cap("C1", "100nF"),
                           FakeFootprint("R1", "R_0402_1005Metric", "10k")])
        finding = dc.check_decoupling_coverage(board)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(0, finding["scanned"])

    def test_an_unrecognised_footprint_is_unscanned_not_clean(self):
        """Documented boundary: a custom-named footprint drops out of
        coverage entirely - it never becomes a violation."""
        board = FakeBoard([FakeFootprint("U9", "MY_CUSTOM_MODULE", "",
                                         0.0, 0.0, [FakePad("1", "VDD", 0.0, 0.0)])])
        finding = dc.check_decoupling_coverage(board)
        self.assertEqual(0, finding["scanned"])
        self.assertEqual("NO_COVERAGE", finding["status"])

    def test_ground_pins_are_not_counted_as_power_pins(self):
        board = FakeBoard([_ic("U1", x=0.0, y=0.0, nets=("GND", "GND")),
                           _cap("C1", "100nF", x=1.0, y=0.0)])
        finding = dc.check_decoupling_coverage(board)
        self.assertEqual(0, finding["scanned"])


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        self.assertEqual([], dc.self_test())

    def test_self_test_flag_exits_zero(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, proc.returncode)

    def test_missing_positional_input_is_exit_2(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, proc.returncode)


if __name__ == "__main__":
    unittest.main()
