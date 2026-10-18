#!/usr/bin/env python3
"""Tests for scripts/sim/sparam.py and its library, common/tline.py.

These assert against CLOSED FORM, not against whatever scikit-rf returned
when the module was written. That is not a style preference: the first
reading of the port-pairing probe was wrong, and the closed-form assertion
is what caught it within seconds. A test that recorded the library's output
would have frozen the mistake instead.

MERGED SUITE. #54 was attempted twice independently, once as scripts/sim/
sparam.py and once as scripts/sparam.py + common/tline.py. The two AGREED on
the measured se2gmm convention - stated differently ("ports 2k-1 and 2k form
pair k" and "port order [A_in, B_in, A_out, B_out]" are the same claim), which
is a stronger result than either probe alone. They disagreed on everything
else, so the implementations were merged into one and so were the tests: the
analysis half below is the three-layout probe and its closed-form skew check,
the synthesis half is the exact line identities.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "sim"))

import numpy as np  # noqa: E402

import sparam as mod  # noqa: E402
from common import tline as tl  # noqa: E402

SCRIPT = SCRIPTS / "sim" / "sparam.py"

FREQS = [1.0, 5.0, 10.0]


def requires_skrf(test):
    try:
        mod._skrf()
    except mod.SparamUnavailable:  # pragma: no cover - environment without skrf
        return unittest.skip("scikit-rf not installed")(test)
    return test


@requires_skrf
class MixedModeConventionTests(unittest.TestCase):
    """The measured fact the whole module rests on."""

    def test_a_zero_skew_pair_is_a_perfect_differential_through(self):
        mixed = mod.to_mixed_mode(mod.ideal_pair_network(FREQS))
        self.assertAlmostEqual(0.0, mod.mixed_mode_db(mixed, mod.DD, 1, 0)[0], places=6)
        self.assertLess(mod.mixed_mode_db(mixed, mod.DC, 1, 0)[0], -100.0)

    def test_skew_matches_the_closed_form(self):
        """|Sdd21| = |cos(d/2)| and |Sdc21| = |sin(d/2)|. A wrong pairing is
        blind to skew, so agreeing with this is a discriminator."""
        for skew in (5.0, 20.0, 60.0):
            with self.subTest(skew=skew):
                mixed = mod.to_mixed_mode(mod.ideal_pair_network(FREQS, skew_deg=skew))
                half = math.radians(skew / 2)
                self.assertAlmostEqual(20 * math.log10(abs(math.cos(half))),
                                       mod.mixed_mode_db(mixed, mod.DD, 1, 0)[0], places=3)
                self.assertAlmostEqual(20 * math.log10(abs(math.sin(half))),
                                       mod.mixed_mode_db(mixed, mod.DC, 1, 0)[0], places=3)

    def test_conductor_numbered_ports_collapse_when_fed_unpermuted(self):
        """FAULT INJECTION: the failure mode that made the old code refuse to
        report mixed-mode at all. Feeding a conductor-numbered s4p straight
        into se2gmm reports a dead channel for a perfect one."""
        by_conductor = mod.ideal_pair_network(FREQS, by_conductor=True)
        collapsed = mod.to_mixed_mode(by_conductor)
        self.assertLess(mod.mixed_mode_db(collapsed, mod.DD, 1, 0)[0], -100.0)

    def test_stating_the_pairing_fixes_it(self):
        by_conductor = mod.ideal_pair_network(FREQS, by_conductor=True)
        fixed = mod.to_mixed_mode(by_conductor, positives=[1, 2], negatives=[3, 4])
        self.assertAlmostEqual(0.0, mod.mixed_mode_db(fixed, mod.DD, 1, 0)[0], places=6)

    def test_an_odd_port_count_is_refused(self):
        rf = mod._skrf()
        import numpy as np
        freq = rf.Frequency.from_f(FREQS, unit="ghz")
        three = rf.Network(frequency=freq, s=np.zeros((len(FREQS), 3, 3), dtype=complex), z0=50)
        with self.assertRaises(ValueError):
            mod.to_mixed_mode(three)

    def test_a_half_specified_pairing_is_refused(self):
        """Half a pairing is how the mode indices get mirrored silently."""
        with self.assertRaises(ValueError):
            mod.to_mixed_mode(mod.ideal_pair_network(FREQS), positives=[1, 2])

    def test_a_pairing_that_drops_or_repeats_a_port_is_refused(self):
        for positives, negatives in (([1, 1], [3, 4]), ([1, 2], [3, 3]), ([1, 2], [3, 9])):
            with self.subTest(positives=positives, negatives=negatives):
                with self.assertRaises(ValueError):
                    mod.to_mixed_mode(mod.ideal_pair_network(FREQS), positives, negatives)


@requires_skrf
class TouchstoneIoTests(unittest.TestCase):
    def test_a_written_file_reads_back_identically(self):
        net = mod.ideal_pair_network(FREQS, skew_deg=13.0)
        with tempfile.TemporaryDirectory() as tmp:
            written = mod.write_touchstone(net, Path(tmp) / "channel.s4p")
            self.assertTrue(written.exists())
            back = mod.read_touchstone(written)
            self.assertEqual(net.nports, back.nports)
            self.assertAlmostEqual(
                mod.mixed_mode_db(mod.to_mixed_mode(net), mod.DC, 1, 0)[0],
                mod.mixed_mode_db(mod.to_mixed_mode(back), mod.DC, 1, 0)[0], places=4)

    def test_the_extension_follows_the_port_count(self):
        """skrf picks its format from the file name, so a 4-port written to
        .s2p produces a file that silently misparses on the next read."""
        net = mod.ideal_pair_network(FREQS)
        with tempfile.TemporaryDirectory() as tmp:
            written = mod.write_touchstone(net, Path(tmp) / "wrong.s2p")
            self.assertEqual(".s4p", written.suffix)

    def test_a_missing_file_raises_rather_than_returning_empty(self):
        with self.assertRaises(FileNotFoundError):
            mod.read_touchstone(Path("no") / "such.s2p")


@requires_skrf
class CascadeTests(unittest.TestCase):
    def line(self, loss_db):
        rf = mod._skrf()
        import numpy as np
        freq = rf.Frequency.from_f(FREQS, unit="ghz")
        amp = 10.0 ** (loss_db / 20.0)
        s = np.zeros((len(FREQS), 2, 2), dtype=complex)
        s[:, 0, 1] = s[:, 1, 0] = amp
        return rf.Network(frequency=freq, s=s, z0=50)

    def test_losses_add_in_db(self):
        """Two matched -1 dB sections cascade to -2 dB. Closed form again."""
        chained = mod.cascade([self.line(-1.0), self.line(-1.0)])
        self.assertAlmostEqual(-2.0, 20 * math.log10(abs(chained.s[0, 1, 0])), places=6)

    def test_a_non_two_port_is_refused(self):
        with self.assertRaises(ValueError):
            mod.cascade([self.line(-1.0), mod.ideal_pair_network(FREQS)])

    def test_mismatched_frequency_axes_are_refused(self):
        """skrf would interpolate silently, changing the answer."""
        rf = mod._skrf()
        import numpy as np
        other = rf.Network(frequency=rf.Frequency.from_f([2.0, 3.0], unit="ghz"),
                           s=np.zeros((2, 2, 2), dtype=complex), z0=50)
        with self.assertRaises(ValueError):
            mod.cascade([self.line(-1.0), other])

    def test_an_empty_cascade_is_refused(self):
        with self.assertRaises(ValueError):
            mod.cascade([])


@requires_skrf
class ChannelGateTests(unittest.TestCase):
    def test_a_clean_channel_passes(self):
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS), il_limit_db=-6.0)
        self.assertEqual("PASS", finding["status"])

    def test_excess_insertion_loss_fails(self):
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS, loss_db=-12.0), il_limit_db=-6.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertIn("insertion_loss_exceeded", {v["issue"] for v in finding["violations"]})

    def test_skew_is_caught_as_mode_conversion(self):
        """60 deg skew -> |Sdc21| = sin(30 deg) = -6 dB, well above a -20 dB
        limit. This is the term the single-ended checker could not see."""
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS, skew_deg=60.0),
            mode_conv_limit_db=-20.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertIn("mode_conversion_exceeded", {v["issue"] for v in finding["violations"]})

    def test_the_detail_records_the_measured_convention(self):
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS), il_limit_db=-6.0)
        self.assertIn("pair", finding["detail"])
        self.assertIn("2k", mod.SE2GMM_CONVENTION)


@requires_skrf
class TwoPortIsNotAPairTests(unittest.TestCase):
    """A 2-port has an EVEN port count but no differential pair.

    The dispatch used to be ``nports % 2 == 0``, which sent a plain 2-port
    into the differential gate: se2gmm then paired the two ENDS of one
    conductor as if they were P and N, and the gate reported a mode-conversion
    figure for a structure with nothing to convert into.
    """

    def line(self, loss_db=0.0):
        frequency = tl.frequency_grid(1e9, 1e10, 5)
        return tl.single_line_network(
            frequency, tl.LineSpec(50.0, 3.1, 100.0), z0_port=50.0)

    def test_a_two_port_uses_the_single_ended_gate(self):
        finding = mod.check_single_ended_channel(self.line(), il_limit_db=-6.0)
        self.assertEqual("sparam_single_ended_channel", finding["check"])
        self.assertEqual("PASS", finding["status"])

    def test_the_single_ended_detail_says_no_mixed_mode_was_reported(self):
        finding = mod.check_single_ended_channel(self.line(), il_limit_db=-6.0)
        self.assertIn("No pair", finding["detail"])

    def test_a_lossy_two_port_still_fails_its_limit(self):
        lossy = tl.single_line_network(
            tl.frequency_grid(1e9, 2e10, 11),
            tl.LineSpec(50.0, 3.1, 300.0, tan_delta=0.02), z0_port=50.0)
        finding = mod.check_single_ended_channel(lossy, il_limit_db=-1.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertIn("insertion_loss_exceeded",
                      {v["issue"] for v in finding["violations"]})


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


# --------------------------------------------------------------------------- #
# Synthesis half - common/tline.py
#
# The analysis tests above check what a network MEANS. These check that the
# networks this bundle BUILDS are right, and they do it against identities
# that hold exactly, so a regression cannot hide inside a tolerance.
# --------------------------------------------------------------------------- #
@requires_skrf
class LineIdentityTests(unittest.TestCase):
    def setUp(self):
        self.frequency = tl.frequency_grid(1e8, 1e10, 21)

    def test_a_matched_lossless_line_has_no_reflection(self):
        network = tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 100.0), z0_port=50.0)
        self.assertLess(float(np.abs(network.s[:, 0, 0]).max()), 1e-12)

    def test_a_matched_lossless_line_passes_everything(self):
        network = tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 100.0), z0_port=50.0)
        self.assertAlmostEqual(float(np.abs(network.s[:, 1, 0]).min()), 1.0,
                               places=12)

    def test_a_mismatched_lossless_line_conserves_energy(self):
        network = tl.single_line_network(
            self.frequency, tl.LineSpec(75.0, 3.1, 100.0), z0_port=50.0)
        total = (np.abs(network.s[:, 0, 0]) ** 2
                 + np.abs(network.s[:, 1, 0]) ** 2)
        self.assertLess(float(np.abs(total - 1.0).max()), 1e-10)

    def test_cascading_halves_reproduces_the_whole_line(self):
        """Exact: the cascade of two 50 mm sections IS the 100 mm line."""
        whole = tl.single_line_network(self.frequency,
                                       tl.LineSpec(50.0, 3.1, 100.0))
        half = tl.single_line_network(self.frequency,
                                      tl.LineSpec(50.0, 3.1, 50.0))
        self.assertLess(
            float(np.abs(tl.cascade([half, half]).s - whole.s).max()), 1e-12)

    def test_group_delay_equals_the_analytic_delay(self):
        frequency = tl.frequency_grid(1e9, 1e10, 201)
        spec = tl.LineSpec(50.0, 3.1, 100.0)
        network = tl.single_line_network(frequency, spec)
        phase = np.unwrap(np.angle(network.s[:, 1, 0]))
        delay_ps = -np.gradient(phase, 2 * np.pi * frequency.f).mean() * 1e12
        self.assertAlmostEqual(delay_ps, spec.delay_ps(), delta=0.2)

    def test_delay_scales_with_the_square_root_of_permittivity(self):
        slow = tl.LineSpec(50.0, 4.0, 100.0).delay_ps()
        fast = tl.LineSpec(50.0, 1.0, 100.0).delay_ps()
        self.assertAlmostEqual(slow / fast, 2.0, places=9)

    def test_a_synthesised_pair_converts_no_energy_between_modes(self):
        pair = tl.coupled_line_network(self.frequency,
                                       tl.LineSpec(45.0, 3.0, 100.0),
                                       tl.LineSpec(60.0, 3.2, 100.0))
        converted = tl.to_mixed_mode(pair)
        self.assertLess(float(np.abs(converted.s[:, 1, 2]).max()), 1e-9)

    def test_the_differential_delay_follows_the_odd_mode(self):
        pair = tl.coupled_line_network(self.frequency,
                                       tl.LineSpec(45.0, 3.0, 100.0),
                                       tl.LineSpec(60.0, 3.2, 100.0))
        converted = tl.to_mixed_mode(pair)
        phase = np.unwrap(np.angle(converted.s[:, 1, 0]))
        delay_ps = -np.gradient(phase,
                                2 * np.pi * self.frequency.f).mean() * 1e12
        self.assertAlmostEqual(delay_ps,
                               tl.LineSpec(45.0, 3.0, 100.0).delay_ps(),
                               delta=1.0)

    def test_mismatched_mode_lengths_are_refused(self):
        with self.assertRaises(ValueError):
            tl.coupled_line_network(self.frequency,
                                    tl.LineSpec(45.0, 3.0, 100.0),
                                    tl.LineSpec(60.0, 3.2, 90.0))


@requires_skrf
class LossModelTests(unittest.TestCase):
    def setUp(self):
        self.frequency = tl.frequency_grid(1e8, 2e10, 21)

    def test_dielectric_loss_grows_with_frequency(self):
        lossy = tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 100.0, tan_delta=0.02))
        loss = tl.insertion_loss_db(lossy)
        self.assertLess(loss[-1], loss[0])

    def test_a_larger_loss_tangent_costs_more(self):
        low = tl.insertion_loss_db(tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 100.0, tan_delta=0.005)))
        high = tl.insertion_loss_db(tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 100.0, tan_delta=0.02)))
        self.assertLess(high[-1], low[-1])

    def test_loss_scales_with_length(self):
        short = tl.insertion_loss_db(tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 50.0, tan_delta=0.02)))
        long = tl.insertion_loss_db(tl.single_line_network(
            self.frequency, tl.LineSpec(50.0, 3.1, 100.0, tan_delta=0.02)))
        self.assertAlmostEqual(long[-1] / short[-1], 2.0, delta=0.02)

    def test_conductor_loss_is_absent_until_a_reference_is_given(self):
        """Never derived from geometry: that would need a roughness model."""
        self.assertEqual(0.0, float(
            tl.conductor_attenuation(np.array([1e9]), 50.0, None)[0]))

    def test_conductor_loss_scales_as_root_f(self):
        alpha = tl.conductor_attenuation(np.array([1e9, 4e9]), 50.0, (10.0, 1e9))
        self.assertAlmostEqual(float(alpha[1] / alpha[0]), 2.0, places=9)

    def test_a_negative_loss_tangent_is_refused(self):
        with self.assertRaises(ValueError):
            tl.dielectric_attenuation(np.array([1e9]), 3.1, -0.01)

    def test_losslessness_is_reported_not_inferred(self):
        self.assertTrue(tl.LineSpec(50.0, 3.1, 10.0).is_lossless())
        self.assertFalse(
            tl.LineSpec(50.0, 3.1, 10.0, tan_delta=0.01).is_lossless())


class LineSpecGuardTests(unittest.TestCase):
    def test_non_positive_impedance_is_refused(self):
        with self.assertRaises(ValueError):
            tl.LineSpec(0.0, 3.1, 10.0)

    def test_non_positive_permittivity_is_refused(self):
        with self.assertRaises(ValueError):
            tl.LineSpec(50.0, 0.0, 10.0)

    def test_non_positive_length_is_refused(self):
        with self.assertRaises(ValueError):
            tl.LineSpec(50.0, 3.1, 0.0)

    def test_zero_magnitude_maps_to_a_finite_floor(self):
        """-inf is not valid JSON and report_io dumps with allow_nan=False."""
        value = tl.to_db(np.array([0.0]))
        self.assertTrue(bool(np.isfinite(value).all()))
        self.assertLess(float(value[0]), -500.0)

    @requires_skrf
    def test_a_reversed_sweep_is_refused(self):
        with self.assertRaises(ValueError):
            tl.frequency_grid(1e10, 1e8, 11)


@requires_skrf
class SynthesisCliTests(unittest.TestCase):
    """The --line mode: geometry in, Touchstone out."""

    def run_cli(self, *args):
        import subprocess
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True,
                              encoding="utf-8", check=False)

    def test_line_without_a_length_is_refused(self):
        self.assertEqual(2, self.run_cli("--line", "--z0", "50",
                                         "--er-eff", "3.1").returncode)

    def test_line_without_z0_or_geometry_is_refused(self):
        proc = self.run_cli("--line", "--length-mm", "50")
        self.assertNotEqual(0, proc.returncode)

    def test_a_synthesised_line_records_its_provenance(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            proc = self.run_cli("--line", "--z0", "50", "--er-eff", "3.1",
                                "--length-mm", "100", "--points", "21",
                                "--il-limit-db", "-30",
                                "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("no field solve", report["provenance"]["source"])
            self.assertIs(True, report["lossless"])

    def test_a_geometry_run_records_the_field_solve(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "g.json"
            proc = self.run_cli("--line", "--geometry", "microstrip",
                                "--w", "0.2", "--h", "0.1", "--er", "4.3",
                                "--length-mm", "50", "--points", "11",
                                "--max-nodes", "40000", "--il-limit-db", "-30",
                                "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("field_solver", report["provenance"]["source"])
            self.assertGreater(report["provenance"]["z0_ohm"], 0.0)

    def test_a_lossy_run_is_not_reported_as_lossless(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "l.json"
            self.run_cli("--line", "--z0", "50", "--er-eff", "3.1",
                         "--length-mm", "100", "--points", "21",
                                "--il-limit-db", "-30",
                         "--tan-delta", "0.02", "--json", str(out))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertIs(False, report["lossless"])

    def test_a_synthesised_pair_can_be_written_and_re_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pair.s4p"
            proc = self.run_cli("--line", "--z0", "45", "--z-even", "60",
                                "--er-eff", "3.0", "--length-mm", "100",
                                "--points", "11", "--write", str(path))
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            self.assertTrue(path.is_file())
            self.assertEqual(4, mod.read_touchstone(path).nports)


@requires_skrf
class ComplianceMaskTests(unittest.TestCase):
    """#53. The masks themselves are NOT in this repository yet; what is here
    is the machinery, and these tests pin the behaviour that keeps a missing
    mask from reading as a pass."""

    def setUp(self):
        self.masks = mod.load_masks()
        self.fixture = self.masks["fixture-symmetric-3ghz"]

    # -- the honest-absence half ------------------------------------------- #
    def test_no_mask_and_no_limits_is_no_coverage_not_a_pass(self):
        """The defect this closes: the signature used to default to
        -6/-10/-20 dB, so a caller who said nothing got a verdict against
        numbers naming no standard - and that PASS looked exactly like a
        PASS against a real specification."""
        finding = mod.check_differential_channel(mod.ideal_pair_network(FREQS))
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertIn("nothing said what passing means", finding["detail"])

    def test_an_unsourced_interface_mask_grades_nothing(self):
        for name in ("mipi-csi2-dphy", "usb3-gen1", "pcie-gen3"):
            with self.subTest(mask=name):
                mask = self.masks[name]
                self.assertEqual("CONFIRM", mask.status)
                self.assertEqual((), mask.points)
                finding = mod.check_differential_channel(
                    mod.ideal_pair_network(FREQS), mask=mask)
                self.assertEqual("NO_COVERAGE", finding["status"])
                self.assertIn("not the same as compliant", finding["detail"])

    def test_a_sweep_that_misses_the_mask_band_is_no_coverage(self):
        """Overlap is not optional. A sweep entirely outside the mask grades
        zero points, and reporting PASS for zero graded points is the empty
        comparison this bundle exists to refuse."""
        # FREQS are GHz: ideal_pair_network multiplies by 1e9.
        far = mod.ideal_pair_network(np.linspace(20.0, 30.0, 5))
        finding = mod.check_differential_channel(far, mask=self.fixture)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertIn("do not overlap", finding["detail"])

    def test_points_outside_the_mask_are_reported_not_extrapolated(self):
        wide = mod.ideal_pair_network(np.linspace(0.5, 5.0, 21))
        finding = mod.check_differential_channel(wide, mask=self.fixture)
        self.assertIn("were NOT", finding["detail"])
        self.assertLess(finding["scanned"], 21)

    # -- the grading half -------------------------------------------------- #
    def test_a_clean_channel_passes_the_fixture_mask(self):
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS), mask=self.fixture)
        self.assertEqual("PASS", finding["status"])
        self.assertIn("fixture-symmetric-3ghz", finding["detail"])

    def test_the_violation_names_the_frequency_and_the_mask(self):
        """A compliance failure that does not say WHERE it failed cannot be
        acted on; that is the whole reason for a frequency-dependent mask."""
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS, loss_db=-12.0), mask=self.fixture)
        self.assertEqual("FAIL", finding["status"])
        worst = finding["violations"][0]
        self.assertEqual("insertion_loss_exceeded", worst["issue"])
        self.assertIn("hz", worst)
        self.assertEqual("fixture-symmetric-3ghz", worst["mask"])

    def test_skew_trips_the_mode_conversion_term_of_the_mask(self):
        finding = mod.check_differential_channel(
            mod.ideal_pair_network(FREQS, skew_deg=60.0), mask=self.fixture)
        self.assertEqual("FAIL", finding["status"])
        self.assertIn("mode_conversion_exceeded",
                      {v["issue"] for v in finding["violations"]})

    def test_the_limit_is_interpolated_between_breakpoints(self):
        """Linear in dB, checked against hand arithmetic rather than against
        a recorded output: -1.0 dB at 0.1 GHz and -3.0 dB at 1.5 GHz put the
        midpoint at -2.0 dB."""
        halfway = (1.0e8 + 1.5e9) / 2.0
        self.assertAlmostEqual(-2.0, self.fixture.limit_at(halfway, "il_db"),
                               places=6)

    def test_the_mask_says_nothing_outside_its_own_breakpoints(self):
        self.assertIsNone(self.fixture.limit_at(1.0e7, "il_db"))
        self.assertIsNone(self.fixture.limit_at(1.0e10, "il_db"))

    def test_every_mask_carries_a_citation(self):
        for name, mask in self.masks.items():
            with self.subTest(mask=name):
                self.assertTrue(mask.provenance.get("standard"))
                if mask.status == "ACTIVE":
                    for key in ("revision", "section", "locator", "accessed"):
                        self.assertTrue(mask.provenance.get(key),
                                        f"{name} is ACTIVE without {key}")


class MaskRegistryFaultInjection(unittest.TestCase):
    """Each case is a way a mask could have looked authoritative while
    resting on nothing. The loader must refuse, not repair."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, masks: dict) -> Path:
        path = Path(self.tmp.name) / "masks.json"
        path.write_text(json.dumps(
            {"schema_version": "signal-compliance-masks/v1", "masks": masks}),
            encoding="utf-8")
        return path

    def _sourced(self, **over) -> dict:
        base = {"interface": "X", "status": "ACTIVE", "standard": "S",
                "revision": "1.0", "section": "4.2", "locator": "https://x",
                "accessed": "2026-08-09",
                "points": [{"hz": 1e8, "il_db": -1.0},
                           {"hz": 1e9, "il_db": -3.0}]}
        base.update(over)
        return {"m": base}

    def test_the_good_case_loads(self):
        self.assertIn("m", mod.load_masks(self._write(self._sourced())))

    def test_active_without_provenance_is_refused(self):
        for missing in ("revision", "section", "locator", "accessed"):
            with self.subTest(missing=missing):
                payload = self._sourced(**{missing: None})
                with self.assertRaises(mod.MaskError) as caught:
                    mod.load_masks(self._write(payload))
                self.assertIn(missing, str(caught.exception))

    def test_confirm_with_points_is_refused(self):
        """Either it is sourced or it is not; carrying points while claiming
        to be unknown is the contradiction that lets a placeholder grade a
        board."""
        payload = self._sourced(status="CONFIRM")
        with self.assertRaises(mod.MaskError) as caught:
            mod.load_masks(self._write(payload))
        self.assertIn("not both", str(caught.exception))

    def test_a_single_breakpoint_cannot_define_a_band(self):
        payload = self._sourced(points=[{"hz": 1e8, "il_db": -1.0}])
        with self.assertRaises(mod.MaskError):
            mod.load_masks(self._write(payload))

    def test_descending_breakpoints_are_refused(self):
        payload = self._sourced(points=[{"hz": 1e9, "il_db": -3.0},
                                        {"hz": 1e8, "il_db": -1.0}])
        with self.assertRaises(mod.MaskError) as caught:
            mod.load_masks(self._write(payload))
        self.assertIn("ascend", str(caught.exception))

    def test_an_unknown_status_is_refused(self):
        payload = self._sourced(status="PROBABLY_FINE")
        with self.assertRaises(mod.MaskError):
            mod.load_masks(self._write(payload))

    def test_a_missing_registry_is_an_error_not_an_empty_mask_set(self):
        with self.assertRaises(mod.MaskError):
            mod.load_masks(Path(self.tmp.name) / "nope.json")


if __name__ == "__main__":
    unittest.main()
