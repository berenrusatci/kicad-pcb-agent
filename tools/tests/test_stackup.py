#!/usr/bin/env python3
"""Tests for common/stackup.py.

Ported from ``pcb-tool-v2/test_pcb_stackup_planner.py`` (pytest) to unittest:
``pytest.raises`` -> ``assertRaises``, ``@parametrize`` -> ``subTest``,
``pytest.approx`` -> ``assertAlmostEqual``. No pytest import anywhere.

Two behaviours that were NOT in the archive suite are pinned here because the
port changed them deliberately:
  * the fab profile table is data on disk, so a missing table must RAISE rather
    than fall back to hardcoded limits;
  * layer roles are matched by token, not by substring-searching Turkish prose.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401  (puts tools/ and scripts/ on sys.path)

from common.stackup import (  # noqa: E402
    AntennaPlacement,
    Component,
    CrystalPlacement,
    DEFAULT_MAX_ASPECT_RATIO,
    DiffPair,
    FabProfile,
    FrequencyBand,
    HighCurrentTrace,
    InterfaceKind,
    IsolationRequirement,
    LAYER_THICKNESS_ASSUMPTION_MM,
    MAX_ASPECT_RATIO_BY_VIA_TYPE,
    MechanicalThermalLimits,
    Net,
    PackageKind,
    PhysicalVia,
    RfStitching,
    SignalKind,
    StackupDataError,
    StackupPlanError,
    ThermalManagement,
    ViaKind,
    ViaTypeDetail,
    ViaUsage,
    antenna_keepout_check,
    antenna_keepout_violations,
    build_stackup,
    copper_balance_delta_pct,
    crystal_placement_check,
    crystal_placement_violations,
    decoupling_check,
    decoupling_violations,
    electrical_drc,
    fab_dfm_check,
    fab_dfm_violations,
    fab_profiles,
    group_signals,
    impedance_target_ohm,
    isolation_violations,
    layer_roles,
    length_matching_check,
    length_matching_violations,
    physical_drc,
    plan_stackup,
    power_copper_violations,
    power_trace_widths_mm,
    required_copper_oz,
    required_isolation_mm,
    required_layer_count,
    rf_stitching_pitch_mm,
    rf_stitching_violations,
    blind_buried_region_valid_for_stackup,
    microvia_span_valid_for_stackup,
    resolve_stackup_layer_index,
    select_via_type_for_bga,
    solve_impedance_geometry,
    stacked_microvia_sequence_valid,
    stackup_as_list,
    thermal_management_violations,
    thermal_via_count,
    trace_width_mm,
    via_layer_span_valid,
    via_stub_check,
    via_stub_violations,
    via_type_aspect_ratio_check,
    via_type_aspect_ratio_violations,
)
from common.units import MM_TO_MIL, OZ_TO_MM  # noqa: E402


# --------------------------------------------------------------------------- #
# group_signals
# --------------------------------------------------------------------------- #
class GroupSignals(unittest.TestCase):
    def test_basic_distribution(self):
        nets = [
            Net("3V3", SignalKind.POWER),
            Net("GND", SignalKind.GND),
            Net("CLK", SignalKind.FAST_DIGITAL),
            Net("WIFI_ANT", SignalKind.RF),
            Net("ADC_IN", SignalKind.ANALOG),
            Net("LED", SignalKind.SLOW_DIGITAL),
        ]
        groups = group_signals(nets)

        self.assertEqual(1, len(groups["power"]))
        self.assertEqual(1, len(groups["ground"]))
        self.assertEqual(1, len(groups["analog"]))
        self.assertEqual(1, len(groups["standard_io"]))
        # RF counts in its own group AND among the critical signals.
        self.assertEqual(1, len(groups["rf"]))
        self.assertEqual(2, len(groups["critical"]))   # CLK + WIFI_ANT

    def test_empty_list(self):
        for group in group_signals([]).values():
            self.assertEqual([], group)


# --------------------------------------------------------------------------- #
# required_layer_count
# --------------------------------------------------------------------------- #
class LayerCount(unittest.TestCase):
    def test_simple_board_is_two_layers(self):
        groups = group_signals([Net("3V3", SignalKind.POWER),
                                Net("GND", SignalKind.GND),
                                Net("LED", SignalKind.SLOW_DIGITAL)])
        self.assertEqual(2, required_layer_count(groups, []))

    def test_fast_signal_needs_four(self):
        groups = group_signals([Net("GND", SignalKind.GND),
                                Net("CLK", SignalKind.FAST_DIGITAL)])
        self.assertEqual(4, required_layer_count(groups, []))

    def test_fine_pitch_bga_needs_eight(self):
        groups = group_signals([Net("GND", SignalKind.GND)])
        parts = [Component("U1", PackageKind.FINE_PITCH_BGA, pin_count=200)]
        self.assertEqual(8, required_layer_count(groups, parts))

    def test_two_fine_pitch_bgas_need_ten(self):
        groups = group_signals([Net("GND", SignalKind.GND)])
        parts = [Component("U1", PackageKind.FINE_PITCH_BGA, pin_count=300),
                 Component("U2", PackageKind.FINE_PITCH_BGA, pin_count=300)]
        self.assertEqual(10, required_layer_count(groups, parts))

    def test_high_pin_count_single_bga_needs_ten(self):
        groups = group_signals([Net("GND", SignalKind.GND)])
        parts = [Component("U1", PackageKind.FINE_PITCH_BGA, pin_count=800)]
        self.assertEqual(10, required_layer_count(groups, parts))

    def test_rf_needs_at_least_six(self):
        groups = group_signals([Net("GND", SignalKind.GND),
                                Net("ANT", SignalKind.RF)])
        self.assertGreaterEqual(required_layer_count(groups, []), 6)

    def test_high_current_rail_needs_at_least_six(self):
        groups = group_signals([
            Net("GND", SignalKind.GND),
            Net("12V_MOTOR", SignalKind.POWER, max_current_a=5.0),
            Net("3V3", SignalKind.POWER, max_current_a=0.5),
        ])
        self.assertGreaterEqual(required_layer_count(groups, []), 6)

    def test_many_rails_and_many_critical_signals_need_six(self):
        nets = ([Net(f"CLK{i}", SignalKind.FAST_DIGITAL) for i in range(12)]
                + [Net("3V3", SignalKind.POWER), Net("5V0", SignalKind.POWER),
                   Net("1V8", SignalKind.POWER)])
        groups = group_signals(nets)
        self.assertGreaterEqual(required_layer_count(groups, []), 6)


# --------------------------------------------------------------------------- #
# build_stackup
# --------------------------------------------------------------------------- #
class BuildStackup(unittest.TestCase):
    def test_produces_exactly_the_requested_layer_count(self):
        groups = group_signals([Net("GND", SignalKind.GND)])
        for layer_count in (2, 4, 6, 8, 10):
            with self.subTest(layer_count=layer_count):
                stackup = build_stackup(layer_count, groups)
                self.assertEqual(layer_count, len(stackup))
                for index in range(1, layer_count + 1):
                    self.assertIn(f"layer_{index}", stackup)

    def test_four_layer_l2_is_gnd(self):
        groups = group_signals([Net("CLK", SignalKind.FAST_DIGITAL),
                                Net("GND", SignalKind.GND)])
        stackup = build_stackup(4, groups)
        self.assertIn("GND", layer_roles(stackup["layer_2"]))

    def test_roles_are_tokens_not_localised_prose(self):
        """The port's fix: rules must not depend on Turkish display text."""
        groups = group_signals([Net("GND", SignalKind.GND)])
        stackup = build_stackup(2, groups)
        self.assertEqual({"SIGNAL", "POWER"}, layer_roles(stackup["layer_1"]))
        self.assertEqual({"GND"}, layer_roles(stackup["layer_2"]))

    def test_stackup_as_list_is_ordered_and_report_shaped(self):
        groups = group_signals([Net("GND", SignalKind.GND)])
        rows = stackup_as_list(build_stackup(6, groups))
        self.assertEqual([1, 2, 3, 4, 5, 6], [row["index"] for row in rows])
        for row in rows:
            self.assertTrue(row["roles"])
            self.assertIsInstance(row["note"], str)


# --------------------------------------------------------------------------- #
# electrical_drc
# --------------------------------------------------------------------------- #
class ElectricalDrc(unittest.TestCase):
    def test_missing_l2_gnd_is_an_error(self):
        groups = group_signals([Net("CLK", SignalKind.FAST_DIGITAL),
                                Net("GND", SignalKind.GND)])
        dirty = {"layer_1": "SIGNAL — a", "layer_2": "SIGNAL — b",
                 "layer_3": "POWER — c", "layer_4": "SIGNAL — d"}
        errors, _ = electrical_drc(dirty, groups, 4)
        self.assertTrue(any("dönüş yolu" in e for e in errors))

    def test_valid_stackup_has_no_errors(self):
        groups = group_signals([Net("CLK", SignalKind.FAST_DIGITAL),
                                Net("GND", SignalKind.GND)])
        stackup = build_stackup(4, groups)
        errors, _ = electrical_drc(stackup, groups, 4)
        self.assertEqual([], errors)

    def test_adjacent_signal_layers_warn(self):
        groups = group_signals([Net("GND", SignalKind.GND)])
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "SIGNAL — b",
                   "layer_3": "GND — c"}
        _, warnings = electrical_drc(stackup, groups, 3)
        self.assertTrue(any("DİK" in w for w in warnings))

    def test_rf_in_too_few_layers_is_an_error(self):
        groups = group_signals([Net("ANT", SignalKind.RF),
                                Net("GND", SignalKind.GND)])
        stackup = build_stackup(4, groups)
        errors, _ = electrical_drc(stackup, groups, 4)
        self.assertTrue(any("RF" in e for e in errors))

    def test_four_rails_on_one_power_layer_is_an_error(self):
        groups = group_signals([
            Net("3V3", SignalKind.POWER), Net("5V0", SignalKind.POWER),
            Net("1V8", SignalKind.POWER), Net("12V", SignalKind.POWER),
        ])
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "GND — b",
                   "layer_3": "POWER — c", "layer_4": "SIGNAL — d"}
        errors, _ = electrical_drc(stackup, groups, 4)
        self.assertTrue(any("bölünemez" in e for e in errors))


# --------------------------------------------------------------------------- #
# physical_drc
# --------------------------------------------------------------------------- #
class PhysicalDrc(unittest.TestCase):
    def test_too_thin_a_board_with_many_layers_errors(self):
        limits = MechanicalThermalLimits(target_thickness_mm=0.8)
        stackup = {f"layer_{i}": "GND — x" for i in range(1, 9)}
        errors = physical_drc(stackup, limits, [], 8)
        self.assertTrue(any("MEKANİK HATA" in e for e in errors))

    def test_enough_thickness_has_no_mechanical_error(self):
        limits = MechanicalThermalLimits(target_thickness_mm=2.0)
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "GND — b",
                   "layer_3": "POWER — c", "layer_4": "SIGNAL — d"}
        errors = physical_drc(stackup, limits, [], 4)
        self.assertFalse(any("MEKANİK HATA" in e for e in errors))

    def test_asymmetric_copper_errors(self):
        limits = MechanicalThermalLimits(target_thickness_mm=3.0,
                                         copper_balance_tolerance_pct=5.0)
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "SIGNAL — b",
                   "layer_3": "GND — c", "layer_4": "GND — d"}
        errors = physical_drc(stackup, limits, [], 4)
        self.assertTrue(any("bükülecektir" in e for e in errors))

    def test_through_via_aspect_ratio_errors(self):
        limits = MechanicalThermalLimits(
            target_thickness_mm=3.0,        # 3.0 / 0.2 = 15:1, over the 8:1 cap
            allowed_via_kind=ViaKind.THROUGH_ONLY)
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "GND — b"}
        errors = physical_drc(stackup, limits, [], 2)
        self.assertTrue(any("delme oranı" in e for e in errors))

    def test_blind_buried_skips_the_aspect_ratio_check(self):
        limits = MechanicalThermalLimits(
            target_thickness_mm=3.0,
            allowed_via_kind=ViaKind.BLIND_BURIED_ALLOWED)
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "GND — b"}
        errors = physical_drc(stackup, limits, [], 2)
        self.assertFalse(any("delme oranı" in e for e in errors))


class CopperBalance(unittest.TestCase):
    def test_symmetric_stackup_is_near_zero(self):
        stackup = {"layer_1": "SIGNAL — a", "layer_2": "GND — b",
                   "layer_3": "GND — c", "layer_4": "SIGNAL — d"}
        self.assertAlmostEqual(0.0, copper_balance_delta_pct(stackup, 4),
                               places=6)


# --------------------------------------------------------------------------- #
# plan_stackup (end to end)
# --------------------------------------------------------------------------- #
class PlanStackup(unittest.TestCase):
    def test_simple_board_lands_on_four_layers(self):
        # A 2-layer stackup has one mixed SIGNAL+POWER face and one plain GND
        # face, so the copper-balance screen finds asymmetry and the algorithm
        # steps up to 4. That is the intended DFM behaviour, not a bug.
        nets = [Net("3V3", SignalKind.POWER), Net("GND", SignalKind.GND),
                Net("LED", SignalKind.SLOW_DIGITAL)]
        plan = plan_stackup(nets, [], MechanicalThermalLimits())
        self.assertEqual(4, plan["layer_count"])
        self.assertEqual(4, len(plan["layers"]))

    def test_fine_pitch_bga_reaches_eight_layers(self):
        nets = [Net("GND", SignalKind.GND),
                Net("CLK", SignalKind.FAST_DIGITAL)]
        parts = [Component("U1", PackageKind.FINE_PITCH_BGA, pin_count=300)]
        plan = plan_stackup(nets, parts,
                            MechanicalThermalLimits(target_thickness_mm=1.6))
        self.assertEqual(8, plan["layer_count"])

    def test_contradictory_constraints_raise(self):
        # Wants 8 layers but must fit 0.8 mm (needs ~1.2 mm): unsatisfiable, so
        # the planner must refuse rather than return a stackup that cannot be
        # built.
        nets = [Net("GND", SignalKind.GND)]
        parts = [Component("U1", PackageKind.FINE_PITCH_BGA, pin_count=200)]
        limits = MechanicalThermalLimits(target_thickness_mm=0.8)
        with self.assertRaises(RuntimeError):
            plan_stackup(nets, parts, limits, max_attempts=3)

    def test_the_refusal_is_a_stackup_plan_error(self):
        nets = [Net("GND", SignalKind.GND)]
        parts = [Component("U1", PackageKind.FINE_PITCH_BGA, pin_count=200)]
        limits = MechanicalThermalLimits(target_thickness_mm=0.8)
        with self.assertRaises(StackupPlanError):
            plan_stackup(nets, parts, limits, max_attempts=3)

    def test_the_attempt_log_is_returned_not_printed(self):
        """The archive printed each attempt; the caller could not report it."""
        nets = [Net("3V3", SignalKind.POWER), Net("GND", SignalKind.GND)]
        plan = plan_stackup(nets, [], MechanicalThermalLimits())
        self.assertGreaterEqual(len(plan["attempts"]), 1)
        self.assertEqual(plan["layer_count"],
                         plan["attempts"][-1]["layer_count"])
        self.assertEqual([], plan["attempts"][-1]["errors"])


# --------------------------------------------------------------------------- #
# trace_width_mm (IPC-2221)
# --------------------------------------------------------------------------- #
class TraceWidth(unittest.TestCase):
    def test_more_current_needs_a_wider_trace(self):
        self.assertGreater(trace_width_mm(5.0), trace_width_mm(1.0))

    def test_internal_layer_needs_a_wider_trace_than_external(self):
        # Same current: an inner layer sheds heat less easily (lower k), so it
        # needs more copper.
        self.assertGreater(trace_width_mm(2.0, external=False),
                           trace_width_mm(2.0, external=True))

    def test_thicker_copper_allows_a_narrower_trace(self):
        self.assertLess(trace_width_mm(3.0, copper_oz=2.0),
                        trace_width_mm(3.0, copper_oz=1.0))

    def test_zero_current_is_zero_width(self):
        self.assertEqual(0.0, trace_width_mm(0.0))

    def test_negative_current_is_zero_width(self):
        self.assertEqual(0.0, trace_width_mm(-1.0))

    def test_matches_the_archive_value_within_the_known_constant_drift(self):
        """~1 A / 10 °C / 1 oz external. The archive used 1 oz = 1.378 mil.

        The bundle derives it from common.units (OZ_TO_MM * MM_TO_MIL =
        1.3701 mil), so widths come out ~0.6% WIDER. Asserting with a tolerance
        instead of the archive literal keeps si_models as the single source of
        the formula; the drift itself is pinned by
        ``ConstantDrift.test_copper_thickness_constant``.
        """
        archive_mm = 0.30105        # 1 A, dT=10, 1 oz, external, 1.378 mil
        self.assertAlmostEqual(archive_mm, trace_width_mm(1.0), delta=0.01)

    def test_only_rails_with_a_declared_current_get_a_recommendation(self):
        groups = group_signals([
            Net("3V3", SignalKind.POWER, max_current_a=1.5),
            Net("VBAT", SignalKind.POWER, max_current_a=0.0),  # skipped
            Net("GND", SignalKind.GND),
        ])
        widths = power_trace_widths_mm(groups)
        self.assertIn("3V3", widths)
        self.assertNotIn("VBAT", widths)
        self.assertGreater(widths["3V3"], 0)


class ConstantDrift(unittest.TestCase):
    def test_copper_thickness_constant(self):
        """Pin the one numeric divergence between the archive and the bundle.

        Archive: 1 oz == 1.378 mil, hardcoded. Bundle: OZ_TO_MM * MM_TO_MIL.
        If either constant moves, this fails instead of silently shifting every
        current-capacity answer.
        """
        derived_mil = OZ_TO_MM * MM_TO_MIL
        self.assertAlmostEqual(1.378, derived_mil, delta=1.378 * 0.01)


# --------------------------------------------------------------------------- #
# Fab profiles
# --------------------------------------------------------------------------- #
class FabProfiles(unittest.TestCase):
    def test_the_three_expected_keys_exist(self):
        profiles = fab_profiles()
        for key in ("JLCPCB_STANDARD", "JLCPCB_ADVANCED", "PCBWAY_STANDARD"):
            with self.subTest(profile=key):
                self.assertIn(key, profiles)
                self.assertIsInstance(profiles[key], FabProfile)
                self.assertEqual(key, profiles[key].key)

    def test_values_match_the_archive_table(self):
        standard = fab_profiles()["JLCPCB_STANDARD"]
        self.assertEqual(0.127, standard.min_track_mm)
        self.assertEqual(0.127, standard.min_clearance_mm)
        self.assertEqual(0.3, standard.min_hole_mm)
        self.assertEqual(0.13, standard.min_annular_ring_mm)
        self.assertEqual(8.0, standard.max_aspect_ratio)

    def test_advanced_is_tighter_than_standard(self):
        profiles = fab_profiles()
        self.assertLess(profiles["JLCPCB_ADVANCED"].min_track_mm,
                        profiles["JLCPCB_STANDARD"].min_track_mm)

    def test_a_missing_table_raises_instead_of_falling_back(self):
        """No hardcoded fallback: a substituted fab limit is a silent wrong
        answer, which is exactly what this bundle exists to prevent."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StackupDataError):
                fab_profiles(Path(tmp) / "nope.json")

    def test_a_wrong_schema_version_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fab.json"
            path.write_text('{"schema_version": "other/v9", "profiles": {}}',
                            encoding="utf-8")
            with self.assertRaises(StackupDataError):
                fab_profiles(path)

    def test_an_incomplete_profile_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fab.json"
            path.write_text(
                '{"schema_version": "fab-profiles/v1", "profiles": '
                '{"X": {"name": "X", "limits": {"min_trace_mm": 0.1}}}}',
                encoding="utf-8")
            with self.assertRaises(StackupDataError):
                fab_profiles(path)


class FabDfm(unittest.TestCase):
    def test_conforming_values_produce_no_violation(self):
        profile = fab_profiles()["JLCPCB_ADVANCED"]
        self.assertEqual([], fab_dfm_violations(0.15, 0.15, 0.25, profile))

    def test_narrow_track_violates(self):
        profile = fab_profiles()["JLCPCB_STANDARD"]
        violations = fab_dfm_violations(0.05, 0.2, 0.4, profile)
        self.assertTrue(any("İz genişliği" in v for v in violations))

    def test_small_hole_violates(self):
        profile = fab_profiles()["PCBWAY_STANDARD"]
        violations = fab_dfm_violations(0.2, 0.2, 0.1, profile)
        self.assertTrue(any("Delik çapı" in v for v in violations))

    def test_finding_reports_three_scanned_rules(self):
        finding = fab_dfm_check(0.2, 0.2, 0.4,
                                fab_profiles()["JLCPCB_STANDARD"])
        self.assertEqual("PASS", finding["status"])
        self.assertEqual(3, finding["scanned"])


# --------------------------------------------------------------------------- #
# Decoupling
# --------------------------------------------------------------------------- #
class Decoupling(unittest.TestCase):
    def test_insufficient_decoupling_violates(self):
        part = Component("U1", PackageKind.QFN, pin_count=32,
                         power_pin_count=4, decoupling_cap_count=1)
        self.assertTrue(any("DEKUPLAJ HATASI" in v
                            for v in decoupling_violations(part)))

    def test_sufficient_decoupling_on_a_small_part_is_clean(self):
        part = Component("U1", PackageKind.QFN, pin_count=32,
                         power_pin_count=2, decoupling_cap_count=2)
        self.assertEqual([], decoupling_violations(part))

    def test_high_pin_count_part_wants_a_bulk_cap(self):
        part = Component("U1_SoC", PackageKind.FINE_PITCH_BGA, pin_count=400,
                         power_pin_count=8, decoupling_cap_count=8)
        self.assertTrue(any("BULK KAPASİTÖR" in v
                            for v in decoupling_violations(part)))

    def test_no_power_pins_means_no_violation(self):
        part = Component("R1", PackageKind.STANDARD, pin_count=2)
        self.assertEqual([], decoupling_violations(part))

    def test_no_components_is_no_coverage_not_a_pass(self):
        """The whole point of routing checks through findings.result()."""
        self.assertEqual("NO_COVERAGE", decoupling_check([])["status"])

    def test_finding_counts_every_component_as_scanned(self):
        parts = [Component("R1", PackageKind.STANDARD, pin_count=2),
                 Component("R2", PackageKind.STANDARD, pin_count=2)]
        finding = decoupling_check(parts)
        self.assertEqual("PASS", finding["status"])
        self.assertEqual(2, finding["scanned"])


# --------------------------------------------------------------------------- #
# Impedance target and geometry
# --------------------------------------------------------------------------- #
class ImpedanceTarget(unittest.TestCase):
    def test_usb3_target(self):
        pair = DiffPair("USB", InterfaceKind.USB3_X, 40.0, 40.0,
                        data_rate_gbps=5.0)
        self.assertEqual(90.0, impedance_target_ohm(pair))

    def test_hdmi_target(self):
        pair = DiffPair("HDMI", InterfaceKind.HDMI, 40.0, 40.0)
        self.assertEqual(100.0, impedance_target_ohm(pair))

    def test_override_wins(self):
        pair = DiffPair("OZEL", InterfaceKind.USB3_X, 40.0, 40.0,
                        target_impedance_ohm_override=95.0)
        self.assertEqual(95.0, impedance_target_ohm(pair))


class ImpedanceGeometry(unittest.TestCase):
    """Integration with scripts/zdiff_solver.py (the archive's empedans_cozucu)."""

    def test_reachable_target_returns_a_geometry(self):
        pair = DiffPair("USB_D+/D-", InterfaceKind.USB2_0, 10.0, 10.0)
        out = solve_impedance_geometry(pair, h_mm=0.2, er=4.3,
                                       fab_min_w_mm=0.09, fab_min_s_mm=0.09)
        self.assertEqual(90.0, out["target_ohm"])
        self.assertIs(True, out["reachable"])
        self.assertLess(out["best"]["error_percent"], 5.0)

    def test_unreachable_target_returns_none(self):
        # At h=0.1 mm the fab-clamped (w,s) grid tops out near ~106 ohm, so
        # 1000 ohm is definitively unreachable.
        pair = DiffPair("ASIRI_DAR", InterfaceKind.USB2_0, 10.0, 10.0,
                        target_impedance_ohm_override=1000.0)
        out = solve_impedance_geometry(pair, h_mm=0.1, er=4.5,
                                       fab_min_w_mm=0.127, fab_min_s_mm=0.127)
        self.assertIs(False, out["reachable"])
        self.assertIsNone(out["best"])

    def test_override_target_is_used(self):
        pair = DiffPair("OZEL", InterfaceKind.USB2_0, 5.0, 5.0,
                        target_impedance_ohm_override=100.0)
        out = solve_impedance_geometry(pair, h_mm=0.3, er=4.3,
                                       fab_min_w_mm=0.09, fab_min_s_mm=0.09)
        self.assertEqual(100.0, out["target_ohm"])

    def test_result_is_labelled_as_screening_only(self):
        pair = DiffPair("USB", InterfaceKind.USB2_0, 5.0, 5.0)
        out = solve_impedance_geometry(pair, h_mm=0.2, er=4.3,
                                       fab_min_w_mm=0.09, fab_min_s_mm=0.09)
        self.assertEqual("exploratory_closed_form", out["model_class"])
        self.assertIs(False, out["signoff"])


# --------------------------------------------------------------------------- #
# Length matching
# --------------------------------------------------------------------------- #
class LengthMatching(unittest.TestCase):
    def test_within_tolerance_is_clean(self):
        pair = DiffPair("USB", InterfaceKind.USB3_X, 40.0, 40.1,
                        data_rate_gbps=5.0)
        self.assertEqual([], length_matching_violations(pair))

    def test_outside_tolerance_violates(self):
        # USB3 tolerance is 0.5 mm; this pair is 0.8 mm apart.
        pair = DiffPair("USB", InterfaceKind.USB3_X, 42.0, 41.2,
                        data_rate_gbps=5.0)
        self.assertTrue(any("LENGTH MATCHING HATASI" in v
                            for v in length_matching_violations(pair)))

    def test_pcie_tight_tolerance_catches_a_small_skew(self):
        pair = DiffPair("PCIE", InterfaceKind.PCIE, 30.0, 30.2,
                        data_rate_gbps=16.0)
        self.assertTrue(any("LENGTH MATCHING HATASI" in v
                            for v in length_matching_violations(pair)))

    def test_no_pairs_is_no_coverage(self):
        self.assertEqual("NO_COVERAGE", length_matching_check([])["status"])


# --------------------------------------------------------------------------- #
# Via stub
# --------------------------------------------------------------------------- #
class ViaStub(unittest.TestCase):
    def test_through_via_deep_stub_at_high_speed_warns(self):
        via = ViaUsage("PCIE_TX0", start_layer=1, end_layer=3,
                       data_rate_gbps=16.0)
        self.assertTrue(any("VIA STUB UYARISI" in v for v in
                            via_stub_violations(via, 8, ViaKind.THROUGH_ONLY)))

    def test_blind_buried_via_never_warns(self):
        via = ViaUsage("PCIE_TX0", start_layer=1, end_layer=3,
                       data_rate_gbps=16.0)
        self.assertEqual([], via_stub_violations(
            via, 8, ViaKind.BLIND_BURIED_ALLOWED))

    def test_slow_signal_does_not_warn(self):
        via = ViaUsage("I2C_SDA", start_layer=1, end_layer=3,
                       data_rate_gbps=0.4)
        self.assertEqual([], via_stub_violations(via, 8, ViaKind.THROUGH_ONLY))

    def test_short_stub_does_not_warn_even_at_high_speed(self):
        # Ends on layer 7 of 8 -> a single-layer stub (~0.15 mm), under the
        # 0.5 mm threshold.
        via = ViaUsage("PCIE_TX0", start_layer=1, end_layer=7,
                       data_rate_gbps=16.0)
        self.assertEqual([], via_stub_violations(via, 8, ViaKind.THROUGH_ONLY))

    def test_no_vias_is_no_coverage(self):
        finding = via_stub_check([], 8, ViaKind.THROUGH_ONLY)
        self.assertEqual("NO_COVERAGE", finding["status"])


# --------------------------------------------------------------------------- #
# Per-via-type aspect ratio (Tur 3 addition)
# --------------------------------------------------------------------------- #
class ViaTypeAspectRatio(unittest.TestCase):
    def test_through_hole_ceiling_is_the_shared_constant(self):
        # THROUGH_HOLE must not silently fork from physical_drc's board-level
        # 8:1 limit -- both trace back to the same IPC-2221/2226 number.
        self.assertEqual(DEFAULT_MAX_ASPECT_RATIO,
                         MAX_ASPECT_RATIO_BY_VIA_TYPE[ViaTypeDetail.THROUGH_HOLE])

    def test_aspect_ratio_uses_the_shared_layer_thickness_constant(self):
        via = PhysicalVia("NET1", start_layer=1, end_layer=3,
                          kind=ViaTypeDetail.THROUGH_HOLE,
                          drill_diameter_mm=0.3, pad_diameter_mm=0.5)
        expected = (2 * LAYER_THICKNESS_ASSUMPTION_MM) / 0.3
        self.assertAlmostEqual(expected, via.aspect_ratio)

    def test_zero_drill_diameter_is_infinite_not_a_crash(self):
        via = PhysicalVia("NET1", 1, 3, ViaTypeDetail.THROUGH_HOLE,
                          drill_diameter_mm=0.0, pad_diameter_mm=0.5)
        self.assertEqual(float("inf"), via.aspect_ratio)

    def test_microvia_at_1to1_ceiling_warns_where_through_hole_would_not(self):
        # Same layer span and drill: a MICROVIA violates its 1:1 ceiling, a
        # THROUGH_HOLE of the same geometry stays under its 8:1 ceiling. This
        # is exactly the per-TYPE distinction physical_drc's single board-wide
        # ceiling cannot express.
        drill = LAYER_THICKNESS_ASSUMPTION_MM  # depth/drill = 1.0 for span=1
        micro = PhysicalVia("BGA_D0", 1, 2, ViaTypeDetail.MICROVIA,
                            drill_diameter_mm=drill / 2, pad_diameter_mm=0.2)
        through = PhysicalVia("BGA_D0", 1, 2, ViaTypeDetail.THROUGH_HOLE,
                              drill_diameter_mm=drill / 2, pad_diameter_mm=0.2)
        self.assertTrue(any("VIA TİP/ASPECT ORANI" in v
                            for v in via_type_aspect_ratio_violations(micro)))
        self.assertEqual([], via_type_aspect_ratio_violations(through))

    def test_blind_and_buried_both_use_the_1to1_ceiling(self):
        for kind in (ViaTypeDetail.BLIND, ViaTypeDetail.BURIED):
            with self.subTest(kind=kind):
                self.assertEqual(1.0, MAX_ASPECT_RATIO_BY_VIA_TYPE[kind])

    def test_no_vias_is_no_coverage(self):
        finding = via_type_aspect_ratio_check([])
        self.assertEqual("NO_COVERAGE", finding["status"])

    def test_clean_via_produces_pass(self):
        via = PhysicalVia("NET1", 1, 2, ViaTypeDetail.THROUGH_HOLE,
                          drill_diameter_mm=0.3, pad_diameter_mm=0.5)
        finding = via_type_aspect_ratio_check([via])
        self.assertEqual("PASS", finding["status"])


class SelectViaTypeForBga(unittest.TestCase):
    def test_wide_pitch_selects_through_hole(self):
        via = select_via_type_for_bga(pad_pitch_mm=1.0, routing_layer_gap=4)
        self.assertEqual(ViaTypeDetail.THROUGH_HOLE, via.kind)
        self.assertFalse(via.in_pad)
        self.assertFalse(via.filled_and_capped)

    def test_medium_pitch_with_shallow_gap_selects_blind(self):
        via = select_via_type_for_bga(pad_pitch_mm=0.6, routing_layer_gap=2)
        self.assertEqual(ViaTypeDetail.BLIND, via.kind)
        self.assertFalse(via.in_pad)

    def test_medium_pitch_with_deep_gap_falls_back_to_microvia(self):
        # Documented simplification: 0.5<=pitch<0.8 but NOT shallow still
        # returns MICROVIA (a real via-stack design), not a crash or a wrong
        # BLIND classification.
        via = select_via_type_for_bga(pad_pitch_mm=0.6, routing_layer_gap=3)
        self.assertEqual(ViaTypeDetail.MICROVIA, via.kind)

    def test_fine_pitch_selects_microvia_and_forces_fill_and_cap(self):
        via = select_via_type_for_bga(pad_pitch_mm=0.4, routing_layer_gap=1)
        self.assertEqual(ViaTypeDetail.MICROVIA, via.kind)
        self.assertTrue(via.in_pad)
        self.assertTrue(via.filled_and_capped)  # IPC-4761 Type VII mandatory

    def test_pad_diameter_is_drill_plus_two_annular_rings(self):
        via = select_via_type_for_bga(pad_pitch_mm=1.0, routing_layer_gap=4,
                                      through_hole_drill_mm=0.3,
                                      min_annular_ring_mm=0.1)
        self.assertAlmostEqual(0.5, via.pad_diameter_mm)

    def test_non_positive_pitch_is_rejected(self):
        with self.assertRaises(ValueError):
            select_via_type_for_bga(pad_pitch_mm=0.0, routing_layer_gap=1)

    def test_sub_unity_layer_gap_is_rejected(self):
        with self.assertRaises(ValueError):
            select_via_type_for_bga(pad_pitch_mm=1.0, routing_layer_gap=0)


# --------------------------------------------------------------------------- #
# HDI layer-span feasibility (Tur 4 addition)
# --------------------------------------------------------------------------- #
class ViaLayerSpanValid(unittest.TestCase):
    LAYER_COUNT = 8

    def _via(self, kind, start, end):
        return PhysicalVia("NET1", start, end, kind,
                           drill_diameter_mm=0.1, pad_diameter_mm=0.2)

    def test_through_hole_reaching_both_outer_layers_is_valid(self):
        via = self._via(ViaTypeDetail.THROUGH_HOLE, 1, self.LAYER_COUNT)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_fault_injection_through_hole_not_reaching_both_outer_layers(self):
        via = self._via(ViaTypeDetail.THROUGH_HOLE, 2, 7)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("THROUGH_HOLE", reason)

    def test_buried_between_two_inner_layers_is_valid(self):
        via = self._via(ViaTypeDetail.BURIED, 3, 6)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_fault_injection_buried_touching_an_outer_layer(self):
        via = self._via(ViaTypeDetail.BURIED, 1, 6)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("BURIED", reason)

    def test_blind_from_an_outer_to_an_inner_layer_is_valid(self):
        via = self._via(ViaTypeDetail.BLIND, 1, 3)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_fault_injection_blind_spanning_two_outer_layers_is_really_through_hole(self):
        via = self._via(ViaTypeDetail.BLIND, 1, self.LAYER_COUNT)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("through-hole", reason)

    def test_fault_injection_blind_spanning_two_inner_layers_is_really_buried(self):
        via = self._via(ViaTypeDetail.BLIND, 3, 6)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("buried", reason)

    def test_microvia_spanning_a_single_layer_step_is_valid(self):
        via = self._via(ViaTypeDetail.MICROVIA, 1, 2)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_fault_injection_microvia_spanning_more_than_one_layer(self):
        via = self._via(ViaTypeDetail.MICROVIA, 1, 3)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("stacked_microvia_sequence_valid", reason)

    def test_layers_outside_the_board_range_are_invalid(self):
        via = self._via(ViaTypeDetail.THROUGH_HOLE, 1, 20)
        valid, reason = via_layer_span_valid(via, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("outside", reason)

    def test_layer_count_below_two_is_rejected(self):
        via = self._via(ViaTypeDetail.THROUGH_HOLE, 1, 1)
        with self.assertRaises(ValueError):
            via_layer_span_valid(via, 1)


class StackedMicroviaSequenceValid(unittest.TestCase):
    LAYER_COUNT = 8

    def _microvia(self, start, end):
        return PhysicalVia("NET1", start, end, ViaTypeDetail.MICROVIA,
                           drill_diameter_mm=0.1, pad_diameter_mm=0.2)

    def test_a_two_step_stack_with_no_gap_is_valid(self):
        stack = [self._microvia(1, 2), self._microvia(2, 3)]
        valid, reason = stacked_microvia_sequence_valid(stack, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_fault_injection_a_gap_in_the_stack_is_invalid(self):
        stack = [self._microvia(1, 2), self._microvia(3, 4)]
        valid, reason = stacked_microvia_sequence_valid(stack, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("gap or overlap", reason)

    def test_fault_injection_an_overlapping_stack_is_invalid(self):
        stack = [self._microvia(1, 2), self._microvia(1, 2)]
        valid, reason = stacked_microvia_sequence_valid(stack, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("gap or overlap", reason)

    def test_fault_injection_a_non_microvia_member_is_rejected(self):
        through = PhysicalVia("NET1", 1, 2, ViaTypeDetail.THROUGH_HOLE,
                              drill_diameter_mm=0.1, pad_diameter_mm=0.2)
        stack = [self._microvia(1, 2), through]
        valid, reason = stacked_microvia_sequence_valid(stack, self.LAYER_COUNT)
        self.assertFalse(valid)
        self.assertIn("non-microvia", reason)

    def test_a_three_step_stack_reaches_its_overall_end_layer(self):
        stack = [self._microvia(1, 2), self._microvia(2, 3), self._microvia(3, 4)]
        valid, reason = stacked_microvia_sequence_valid(stack, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_order_of_the_input_list_does_not_matter(self):
        stack = [self._microvia(3, 4), self._microvia(1, 2), self._microvia(2, 3)]
        valid, reason = stacked_microvia_sequence_valid(stack, self.LAYER_COUNT)
        self.assertTrue(valid, reason)

    def test_a_single_via_is_rejected_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            stacked_microvia_sequence_valid([self._microvia(1, 2)], self.LAYER_COUNT)

    def test_an_empty_sequence_is_rejected(self):
        with self.assertRaises(ValueError):
            stacked_microvia_sequence_valid([], self.LAYER_COUNT)


# --------------------------------------------------------------------------- #
# Binding HDI via checks to a real stackup (Tur 4 addition)
# --------------------------------------------------------------------------- #
def _real_8_layer_stackup():
    empty_groups = {"power": [], "ground": [], "critical": [],
                    "rf": [], "analog": [], "standard_io": []}
    return build_stackup(8, empty_groups)


class ResolveStackupLayerIndex(unittest.TestCase):
    def test_a_real_layer_name_resolves_to_its_index(self):
        stackup = _real_8_layer_stackup()
        self.assertEqual(3, resolve_stackup_layer_index("layer_3", stackup))

    def test_fault_injection_a_layer_name_outside_this_stackup_is_refused(self):
        stackup = build_stackup(4, {k: [] for k in
                                    ("power", "ground", "critical", "rf",
                                     "analog", "standard_io")})
        with self.assertRaises(StackupDataError):
            resolve_stackup_layer_index("layer_8", stackup)

    def test_fault_injection_a_non_conforming_name_is_refused(self):
        stackup = _real_8_layer_stackup()
        with self.assertRaises(StackupDataError):
            resolve_stackup_layer_index("not_a_layer_name", stackup)


class HdiViaBoundToARealStackup(unittest.TestCase):
    """Direct continuation of the archive's MikroviaKatmani/
    BlindBuriedViaBolgesi fields (layer NAMES), now checked against a real
    plan_stackup()-shaped dict instead of floating disconnected."""

    def test_a_microvia_between_two_real_adjacent_layers_is_valid(self):
        stackup = _real_8_layer_stackup()
        valid, reason = microvia_span_valid_for_stackup(
            "layer_1", "layer_2", stackup)
        self.assertTrue(valid, reason)

    def test_fault_injection_a_microvia_naming_a_layer_outside_the_stackup(self):
        stackup = _real_8_layer_stackup()
        with self.assertRaises(StackupDataError):
            microvia_span_valid_for_stackup("layer_1", "layer_99", stackup)

    def test_fault_injection_a_microvia_spanning_more_than_one_real_layer(self):
        stackup = _real_8_layer_stackup()
        valid, reason = microvia_span_valid_for_stackup(
            "layer_1", "layer_3", stackup)
        self.assertFalse(valid)

    def test_a_buried_region_between_two_real_inner_layers_is_valid(self):
        stackup = _real_8_layer_stackup()
        valid, reason = blind_buried_region_valid_for_stackup(
            "layer_3", "layer_6", True, stackup)
        self.assertTrue(valid, reason)

    def test_fault_injection_a_buried_region_touching_a_real_outer_layer(self):
        stackup = _real_8_layer_stackup()
        valid, reason = blind_buried_region_valid_for_stackup(
            "layer_1", "layer_6", True, stackup)
        self.assertFalse(valid)

    def test_a_blind_region_from_a_real_outer_to_inner_layer_is_valid(self):
        stackup = _real_8_layer_stackup()
        valid, reason = blind_buried_region_valid_for_stackup(
            "layer_1", "layer_3", False, stackup)
        self.assertTrue(valid, reason)

    def test_a_smaller_real_stackup_correctly_shrinks_the_valid_range(self):
        # A 4-layer stackup has no layer_6 at all - must raise, not silently
        # validate against an assumed 8-layer board.
        stackup = build_stackup(4, {k: [] for k in
                                    ("power", "ground", "critical", "rf",
                                     "analog", "standard_io")})
        with self.assertRaises(StackupDataError):
            blind_buried_region_valid_for_stackup(
                "layer_3", "layer_6", True, stackup)


# --------------------------------------------------------------------------- #
# Antenna keep-out
# --------------------------------------------------------------------------- #
class AntennaKeepout(unittest.TestCase):
    def test_insufficient_copper_clearance_violates(self):
        antenna = AntennaPlacement("WIFI_ANT", FrequencyBand.WIFI_BLE_2G4,
                                   edge_clearance_mm=3.0,
                                   nearest_copper_mm=2.0)   # needs 5 mm
        self.assertTrue(any("bakır/GND dökümüne" in v
                            for v in antenna_keepout_violations(antenna)))

    def test_insufficient_edge_clearance_violates(self):
        antenna = AntennaPlacement("WIFI_ANT", FrequencyBand.WIFI_BLE_2G4,
                                   edge_clearance_mm=1.0,
                                   nearest_copper_mm=6.0)   # edge needs 3 mm
        self.assertTrue(any("Kart kenarına" in v
                            for v in antenna_keepout_violations(antenna)))

    def test_sufficient_keepout_is_clean(self):
        antenna = AntennaPlacement("WIFI_ANT", FrequencyBand.WIFI_BLE_2G4,
                                   edge_clearance_mm=4.0,
                                   nearest_copper_mm=6.0)
        self.assertEqual([], antenna_keepout_violations(antenna))

    def test_subghz_needs_a_larger_keepout(self):
        near = AntennaPlacement("A1", FrequencyBand.WIFI_BLE_2G4, 4.0, 6.0)
        subghz = AntennaPlacement("A2", FrequencyBand.SUBGHZ_433, 4.0, 6.0)
        self.assertEqual([], antenna_keepout_violations(near))
        self.assertNotEqual([], antenna_keepout_violations(subghz))

    def test_no_antennas_is_no_coverage(self):
        self.assertEqual("NO_COVERAGE", antenna_keepout_check([])["status"])


# --------------------------------------------------------------------------- #
# Crystal placement
# --------------------------------------------------------------------------- #
class CrystalPlacementRules(unittest.TestCase):
    def test_crystal_far_from_the_ic_violates(self):
        crystal = CrystalPlacement("XTAL1", ic_pin_distance_mm=12.0,
                                   noise_source_distance_mm=10.0)
        self.assertTrue(any("XTAL pinlerine" in v
                            for v in crystal_placement_violations(crystal)))

    def test_near_noise_without_a_guard_ring_violates(self):
        crystal = CrystalPlacement("XTAL1", ic_pin_distance_mm=5.0,
                                   noise_source_distance_mm=2.0,
                                   guard_ring=False)
        self.assertTrue(any("guard ring" in v
                            for v in crystal_placement_violations(crystal)))

    def test_near_noise_with_a_guard_ring_is_clean(self):
        crystal = CrystalPlacement("XTAL1", ic_pin_distance_mm=5.0,
                                   noise_source_distance_mm=2.0,
                                   guard_ring=True)
        self.assertEqual([], crystal_placement_violations(crystal))

    def test_well_placed_crystal_is_clean(self):
        crystal = CrystalPlacement("XTAL1", ic_pin_distance_mm=4.0,
                                   noise_source_distance_mm=8.0)
        self.assertEqual([], crystal_placement_violations(crystal))

    def test_no_crystals_is_no_coverage(self):
        self.assertEqual("NO_COVERAGE", crystal_placement_check([])["status"])


# --------------------------------------------------------------------------- #
# RF stitching
# --------------------------------------------------------------------------- #
class RfStitchingRules(unittest.TestCase):
    def test_higher_frequency_wants_a_tighter_pitch(self):
        self.assertLess(rf_stitching_pitch_mm(5.0), rf_stitching_pitch_mm(2.4))

    def test_zero_frequency_returns_infinity(self):
        self.assertEqual(float("inf"), rf_stitching_pitch_mm(0.0))

    def test_wide_pitch_violates(self):
        trace = RfStitching("WIFI_RF", frequency_ghz=2.4, via_pitch_mm=100.0)
        self.assertTrue(any("RF STITCHING HATASI" in v
                            for v in rf_stitching_violations(trace)))

    def test_tight_pitch_is_clean(self):
        trace = RfStitching("WIFI_RF", frequency_ghz=2.4, via_pitch_mm=1.0)
        self.assertEqual([], rf_stitching_violations(trace))


# --------------------------------------------------------------------------- #
# Creepage / clearance
# --------------------------------------------------------------------------- #
class Isolation(unittest.TestCase):
    def test_higher_voltage_needs_more_clearance(self):
        self.assertGreater(required_isolation_mm(500),
                           required_isolation_mm(50))

    def test_low_voltage_uses_the_smallest_row(self):
        self.assertEqual(0.05, required_isolation_mm(10))

    def test_insufficient_clearance_violates(self):
        requirement = IsolationRequirement("AC-GND", voltage_delta_v=230.0,
                                           design_clearance_mm=0.2)
        self.assertTrue(any("YALITIM HATASI" in v
                            for v in isolation_violations(requirement)))

    def test_sufficient_clearance_is_clean(self):
        requirement = IsolationRequirement("AC-GND", voltage_delta_v=230.0,
                                           design_clearance_mm=1.0)
        self.assertEqual([], isolation_violations(requirement))

    def test_above_the_table_extrapolates_linearly(self):
        # Highest row is 1000 V / 1.5 mm; 2000 V should scale to ~3.0 mm.
        self.assertAlmostEqual(3.0, required_isolation_mm(2000), delta=0.03)

    def test_table_rows_are_monotonic(self):
        volts = [10, 40, 60, 120, 160, 200, 300, 700, 1000]
        clearances = [required_isolation_mm(v) for v in volts]
        for lower, higher in zip(clearances, clearances[1:]):
            with self.subTest(lower=lower, higher=higher):
                self.assertLessEqual(lower, higher)


# --------------------------------------------------------------------------- #
# High-current copper
# --------------------------------------------------------------------------- #
class CopperWeight(unittest.TestCase):
    def test_more_current_needs_thicker_copper(self):
        self.assertGreater(required_copper_oz(20.0, 2.0),
                           required_copper_oz(2.0, 2.0))

    def test_zero_current_is_zero(self):
        self.assertEqual(0.0, required_copper_oz(0.0, 2.0))

    def test_zero_width_is_zero(self):
        self.assertEqual(0.0, required_copper_oz(5.0, 0.0))

    def test_high_current_in_a_narrow_trace_exceeds_the_economic_limit(self):
        trace = HighCurrentTrace("MOTOR_12V", current_a=15.0, width_mm=2.0)
        self.assertTrue(any("GÜÇ ELEKTRONİĞİ HATASI" in v
                            for v in power_copper_violations(trace)))

    def test_low_current_in_a_wide_trace_is_clean(self):
        trace = HighCurrentTrace("LED_LINE", current_a=0.5, width_mm=2.0)
        self.assertEqual([], power_copper_violations(trace))

    def test_it_is_the_inverse_of_trace_width(self):
        """required_copper_oz must undo trace_width_mm, not re-derive it."""
        width_1oz = trace_width_mm(3.0)
        self.assertAlmostEqual(1.0, required_copper_oz(3.0, width_1oz),
                               places=9)


# --------------------------------------------------------------------------- #
# Thermal vias
# --------------------------------------------------------------------------- #
class ThermalVias(unittest.TestCase):
    def test_via_count_grows_with_power(self):
        self.assertGreater(thermal_via_count(3.0), thermal_via_count(1.0))

    def test_zero_power_needs_no_vias(self):
        self.assertEqual(0, thermal_via_count(0.0))

    def test_insufficient_thermal_vias_violates(self):
        item = ThermalManagement("U3_MOSFET", dissipation_w=4.0,
                                 thermal_via_count=5)
        self.assertTrue(any("TERMAL YÖNETİM HATASI" in v
                            for v in thermal_management_violations(item)))

    def test_sufficient_thermal_vias_is_clean(self):
        item = ThermalManagement("U3_MOSFET", dissipation_w=1.0,
                                 thermal_via_count=10)
        self.assertEqual([], thermal_management_violations(item))


if __name__ == "__main__":
    unittest.main()
