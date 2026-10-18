#!/usr/bin/env python3
"""Tests for scripts/sim/eye.py.

An eye number is easy to produce and hard to trust, so the load-bearing
tests here are ones whose answer exists before the code runs:

  * an ideal matched lossless channel has a rectangular pulse response, so
    the cursor is exactly 1, the ISI is exactly 0, and the eye is the full
    swing;
  * a PURE DELAY cannot change an eye, so two channels differing only in
    length must agree - a pipeline that drifts with delay is broken even
    when one case looks right;
  * loss can only close an eye, never open one.

The remaining tests are refusals: the cases where a number could be
produced but would mean nothing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "sim"))

import eye as mod  # noqa: E402
from common import tline as tl  # noqa: E402

UI_PS = 100.0          # 10 Gb/s
requires_skrf = unittest.skipUnless(tl.HAVE_SKRF, "scikit-rf is not installed")


@requires_skrf
class ClosedFormControls(unittest.TestCase):
    def eye_of(self, **kwargs):
        channel = mod.ideal_channel(**kwargs)
        t, pulse, dt = mod.pulse_response(channel, UI_PS)
        return mod.peak_distortion_eye(t, pulse, UI_PS, dt)

    def test_an_ideal_channel_is_a_full_clean_eye(self):
        eye = self.eye_of(delay_ps=20.0)
        self.assertAlmostEqual(1.0, eye.cursor, places=3)
        self.assertLess(eye.isi_sum, 1e-3)
        self.assertAlmostEqual(2.0, eye.height, places=2)

    def test_a_pure_delay_does_not_change_the_eye(self):
        """Agreement between these two is as strong a check as either alone."""
        short = self.eye_of(delay_ps=20.0)
        long = self.eye_of(delay_ps=250.0)
        self.assertAlmostEqual(short.height, long.height, places=3)
        self.assertAlmostEqual(short.cursor, long.cursor, places=3)

    def test_loss_closes_the_eye_monotonically(self):
        heights = [self.eye_of(delay_ps=20.0, loss_db_at_fmax=loss).height
                   for loss in (0.0, -3.0, -6.0, -12.0, -20.0)]
        self.assertEqual(heights, sorted(heights, reverse=True), heights)

    def test_isi_grows_with_loss(self):
        low = self.eye_of(delay_ps=20.0, loss_db_at_fmax=-3.0)
        high = self.eye_of(delay_ps=20.0, loss_db_at_fmax=-20.0)
        self.assertGreater(high.isi_sum, low.isi_sum)

    def test_the_open_width_never_exceeds_one_ui(self):
        for loss in (0.0, -6.0, -20.0):
            with self.subTest(loss=loss):
                self.assertLessEqual(
                    self.eye_of(delay_ps=20.0, loss_db_at_fmax=loss).width_ui, 1.0001)


@requires_skrf
class RefusalTests(unittest.TestCase):
    def test_a_ui_the_frequency_axis_cannot_resolve_is_refused(self):
        """FAULT INJECTION: with too few samples per bit the eye becomes an
        artefact of the sampling rather than a property of the channel, and
        it would look plausible."""
        narrow = mod.ideal_channel(20.0, f_max_ghz=1.0, points=51)
        with self.assertRaises(ValueError) as caught:
            mod.pulse_response(narrow, 1.0)
        self.assertIn("per UI", str(caught.exception))

    def test_a_four_port_is_refused_by_pulse_response(self):
        pair = tl.coupled_line_network(
            tl.frequency_grid(1e8, 2e10, 201),
            tl.LineSpec(45.0, 3.0, 50.0), tl.LineSpec(60.0, 3.0, 50.0))
        with self.assertRaises(ValueError):
            mod.pulse_response(pair, UI_PS)

    def test_a_non_positive_ui_is_refused(self):
        for bad in (0.0, -10.0):
            with self.subTest(ui=bad):
                with self.assertRaises(ValueError):
                    mod.pulse_response(mod.ideal_channel(20.0), bad)


@requires_skrf
class DifferentialPathTests(unittest.TestCase):
    def pair(self):
        # Starts at 100 MHz, not DC: common.tline refuses a DC grid (a line
        # is degenerate there), and pulse_response extrapolates to DC itself
        # - so this also exercises that path rather than side-stepping it.
        return tl.coupled_line_network(
            tl.frequency_grid(1e8, 2e10, 401),
            tl.LineSpec(45.0, 3.0, 50.0), tl.LineSpec(60.0, 3.0, 50.0))

    def test_a_four_port_reduces_to_its_differential_two_port(self):
        two_port = mod.differential_2port(self.pair())
        self.assertEqual(2, two_port.nports)

    def test_declaring_the_wrong_port_order_destroys_the_eye(self):
        """The failure mode common/tline.py measured: read a per_line file as
        per_end and |Sdd21| goes from 1 to 0 - a perfect channel reported
        dead. It must not silently become a plausible small number."""
        per_line = tl.reorder_ports(self.pair(), "per_end", "per_line")
        right = mod.check_eye(per_line, UI_PS, port_order="per_line")
        wrong = mod.check_eye(per_line, UI_PS, port_order="per_end")
        self.assertEqual("PASS", right["status"])
        self.assertEqual("FAIL", wrong["status"])
        self.assertIn("eye_closed", {v["issue"] for v in wrong["violations"]})


@requires_skrf
class GateTests(unittest.TestCase):
    def test_a_clean_channel_passes_a_demanding_limit(self):
        finding = mod.check_eye(mod.ideal_channel(20.0), UI_PS, min_height=1.0)
        self.assertEqual("PASS", finding["status"])

    def test_a_lossy_channel_fails_the_same_limit(self):
        finding = mod.check_eye(mod.ideal_channel(20.0, loss_db_at_fmax=-20.0),
                                UI_PS, min_height=1.0)
        self.assertEqual("FAIL", finding["status"])
        self.assertIn("eye_height_below_limit", {v["issue"] for v in finding["violations"]})

    def test_the_detail_states_what_the_number_is_not(self):
        """A worst-case bound reported without its caveats is the kind of
        number that ends up in a sign-off table."""
        detail = mod.check_eye(mod.ideal_channel(20.0), UI_PS)["detail"]
        self.assertIn("BOUND", detail)
        self.assertIn("BER", detail)
        self.assertIn("equaliser", detail)

    def test_volts_appear_only_when_a_swing_is_supplied(self):
        without = mod.check_eye(mod.ideal_channel(20.0), UI_PS)["detail"]
        with_swing = mod.check_eye(mod.ideal_channel(20.0), UI_PS, swing_v=0.8)["detail"]
        self.assertNotIn(" V of a ", without)
        self.assertIn("0.8 V swing", with_swing)


class CliTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())

    @requires_skrf
    def test_the_ui_has_no_default(self):
        """An eye is defined per bit; a default would silently analyse a bit
        rate nobody chose."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.s2p"
            mod.ideal_channel(20.0).write_touchstone(str(path.with_suffix("")))
            with self.assertRaises(SystemExit):
                mod.main([str(path)])

    @requires_skrf
    def test_a_failing_channel_exits_one_and_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.s2p"
            mod.ideal_channel(20.0, loss_db_at_fmax=-20.0).write_touchstone(
                str(path.with_suffix("")))
            out = Path(tmp) / "eye.json"
            code = mod.main([str(path), "--ui-ps", str(UI_PS),
                             "--min-height", "1.0", "--json", str(out)])
            self.assertEqual(1, code)
            self.assertEqual("FAIL", json.loads(out.read_text(encoding="utf-8"))
                             ["findings"][0]["status"])


if __name__ == "__main__":
    unittest.main()
