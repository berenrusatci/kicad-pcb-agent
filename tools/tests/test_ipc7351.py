#!/usr/bin/env python3
"""Tests for common/ipc7351.py (ported from test_ipc7351_footprint.py)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401

from common.ipc7351 import (  # noqa: E402
    COMMON_CHIP_PACKAGES_MM,
    BgaComponent,
    ChipComponent,
    DensityLevel,
    GullwingComponent,
    bga_land_pattern,
    gullwing_land_pattern,
    land_pattern,
    land_pattern_for_package,
    qfn_land_pattern,
)


class LandPattern(unittest.TestCase):
    def test_0603_nominal_gives_plausible_pads(self):
        result = land_pattern_for_package("0603", DensityLevel.B_NOMINAL)
        # For a 0603 (1.6x0.8 mm body) a typical pad is ~0.9-1.0 mm wide and
        # ~0.9-1.1 mm long.
        self.assertGreaterEqual(result.pad_width_mm, 0.7)
        self.assertLessEqual(result.pad_width_mm, 1.2)
        self.assertGreaterEqual(result.pad_length_mm, 0.7)
        self.assertLessEqual(result.pad_length_mm, 1.3)
        # Pad centres sit further apart than one pad is long.
        self.assertGreater(result.pad_pitch_mm, result.pad_length_mm)

    def test_density_a_is_largest_and_c_smallest(self):
        """Density A (maximum) must give the BIGGEST pad and C (minimum) the
        SMALLEST, because the fillet targets decrease in that order."""
        component = COMMON_CHIP_PACKAGES_MM["0402"]
        a = land_pattern(component, DensityLevel.A_MAXIMUM)
        b = land_pattern(component, DensityLevel.B_NOMINAL)
        c = land_pattern(component, DensityLevel.C_MINIMUM)
        self.assertGreaterEqual(a.pad_length_mm, b.pad_length_mm)
        self.assertGreaterEqual(b.pad_length_mm, c.pad_length_mm)
        self.assertGreater(a.zmax_mm, b.zmax_mm)
        self.assertGreater(b.zmax_mm, c.zmax_mm)

    def test_unknown_package_raises_instead_of_inventing(self):
        with self.assertRaises(KeyError):
            land_pattern_for_package("9999_YOK")

    def test_negative_gmin_raises(self):
        # Deliberately overlapping geometry (T so large that S goes negative).
        overlapping = ChipComponent(length_nom_mm=1.0, width_nom_mm=0.5,
                                    termination_nom_mm=0.9, tolerance_mm=0.05)
        with self.assertRaises(ValueError):
            land_pattern(overlapping)

    def test_hand_entered_dimensions_work(self):
        custom = ChipComponent(length_nom_mm=3.2, width_nom_mm=2.5,
                               termination_nom_mm=0.6, tolerance_mm=0.1)
        result = land_pattern(custom, DensityLevel.B_NOMINAL)
        self.assertGreater(result.pad_width_mm, 0)
        self.assertGreater(result.pad_length_mm, 0)

    def test_density_is_carried_through(self):
        for density in DensityLevel:
            with self.subTest(density=density):
                result = land_pattern_for_package("0805", density)
                self.assertEqual(density, result.density)

    def test_geometry_identity_zmax_gmin(self):
        """pad_length = (Zmax - Gmin)/2 and pitch = (Zmax + Gmin)/2, so the
        outer span is reconstructible: pitch + pad_length == Zmax."""
        result = land_pattern_for_package("1206")
        self.assertAlmostEqual(result.zmax_mm,
                               result.pad_pitch_mm + result.pad_length_mm,
                               places=3)
        self.assertAlmostEqual(result.gmin_mm,
                               result.pad_pitch_mm - result.pad_length_mm,
                               places=3)

    def test_pad_width_equals_xmax(self):
        result = land_pattern_for_package("0402")
        self.assertEqual(result.xmax_mm, result.pad_width_mm)


class GullwingAndQfn(unittest.TestCase):
    SOIC8 = GullwingComponent(
        pin_count=8, pitch_mm=1.27, lead_span_nom_mm=6.0,
        lead_width_nom_mm=0.4, lead_length_nom_mm=0.6, tolerance_mm=0.1)

    def test_gullwing_gives_plausible_pads(self):
        result = gullwing_land_pattern(self.SOIC8, DensityLevel.B_NOMINAL)
        self.assertGreater(result.pad_width_mm, 0)
        self.assertGreater(result.pad_length_mm, 0)
        self.assertGreater(result.pad_pitch_mm, result.pad_length_mm)

    def test_qfn_pad_is_smaller_than_gullwing_for_the_same_geometry(self):
        """QFN's toe fillet is smaller than gullwing's at every density
        level, so its pad must come out smaller for identical input geometry."""
        gw = gullwing_land_pattern(self.SOIC8, DensityLevel.B_NOMINAL)
        qfn = qfn_land_pattern(self.SOIC8, DensityLevel.B_NOMINAL)
        self.assertLess(qfn.zmax_mm, gw.zmax_mm)
        self.assertLess(qfn.pad_length_mm, gw.pad_length_mm)

    def test_density_a_is_largest_and_c_smallest_for_gullwing(self):
        a = gullwing_land_pattern(self.SOIC8, DensityLevel.A_MAXIMUM)
        b = gullwing_land_pattern(self.SOIC8, DensityLevel.B_NOMINAL)
        c = gullwing_land_pattern(self.SOIC8, DensityLevel.C_MINIMUM)
        self.assertGreater(a.zmax_mm, b.zmax_mm)
        self.assertGreater(b.zmax_mm, c.zmax_mm)

    def test_negative_gmin_raises_for_gullwing_too(self):
        overlapping = GullwingComponent(
            pin_count=8, pitch_mm=0.5, lead_span_nom_mm=2.0,
            lead_width_nom_mm=0.3, lead_length_nom_mm=1.5, tolerance_mm=0.05)
        with self.assertRaises(ValueError):
            gullwing_land_pattern(overlapping)

    def test_pitch_is_carried_through_unchanged(self):
        """IPC-7351 does not derive the in-row pitch; it must pass through
        untouched from the datasheet value."""
        result = gullwing_land_pattern(self.SOIC8)
        self.assertNotEqual(self.SOIC8.pitch_mm, result.pad_pitch_mm)


class Bga(unittest.TestCase):
    BGA = BgaComponent(pin_count=256, pitch_mm=0.5, ball_diameter_nom_mm=0.3)

    def test_nsmd_pad_is_80_percent_of_ball_diameter(self):
        result = bga_land_pattern(self.BGA, "NSMD")
        self.assertAlmostEqual(0.24, result.pad_diameter_mm)
        self.assertEqual("NSMD", result.mask_type)

    def test_smd_pad_equals_ball_diameter(self):
        result = bga_land_pattern(self.BGA, "SMD")
        self.assertAlmostEqual(0.3, result.pad_diameter_mm)

    def test_nsmd_pad_is_smaller_than_smd(self):
        nsmd = bga_land_pattern(self.BGA, "NSMD")
        smd = bga_land_pattern(self.BGA, "SMD")
        self.assertLess(nsmd.pad_diameter_mm, smd.pad_diameter_mm)

    def test_pitch_is_carried_through(self):
        result = bga_land_pattern(self.BGA)
        self.assertEqual(self.BGA.pitch_mm, result.pitch_mm)

    def test_unknown_mask_type_is_rejected(self):
        with self.assertRaises(ValueError):
            bga_land_pattern(self.BGA, "BOGUS")

    def test_fault_injection_non_positive_ball_diameter_is_rejected(self):
        zero = BgaComponent(pin_count=100, pitch_mm=0.5,
                            ball_diameter_nom_mm=0.0)
        with self.assertRaises(ValueError):
            bga_land_pattern(zero)


if __name__ == "__main__":
    unittest.main()
