#!/usr/bin/env python3
"""Post-layout parasitic extraction and back-annotation (#64).

The load-bearing tests here compare against CLOSED ARITHMETIC (rho*L/(w*t),
the TEM identity L' = 1/(c^2 C0')) rather than against a recorded output,
and against the pre-layout/post-layout DIFFERENCE rather than an absolute
number nobody can check.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

from common import parasitics as par  # noqa: E402
from common.units import C_MPS  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "sim"))
import ngspice  # noqa: E402

FIXTURE = "TEST FIXTURE - not a fab stackup"

BOARD = """(kicad_pcb (version 20240108)
(segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net "SIG"))
(segment (start 10 0) (end 20 0) (width 0.2) (layer "F.Cu") (net "SIG"))
(segment (start 0 5) (end 4 5) (width 0.3) (layer "B.Cu") (net "VDD"))
(segment (start 0 9) (end 1 9) (width 0.2) (layer "In1.Cu") (net "SIG"))
(via (at 5 5) (size 0.6) (drill 0.3))
)"""


def _layers(**over) -> dict:
    spec = {"er": 4.3, "height_mm": 0.1, "copper_thickness_mm": 0.035,
            "source": FIXTURE}
    spec.update(over)
    return {name: par.LayerSpec(name=name, **spec) for name in ("F.Cu", "B.Cu")}


SOLVER = {"cells_per_feature": 2, "max_nodes": 30_000}


class GeometryReading(unittest.TestCase):
    """The board is read WITHOUT pcbnew - that was measured, not assumed."""

    def test_segments_come_out_of_the_board_text(self):
        segments = par.read_segments(BOARD)
        self.assertEqual(4, len(segments))
        self.assertEqual({"SIG", "VDD"}, {s.net for s in segments})

    def test_lengths_are_euclidean_not_manhattan(self):
        board = ('(kicad_pcb (segment (start 0 0) (end 3 4) (width 0.2) '
                 '(layer "F.Cu") (net "D")))')
        self.assertAlmostEqual(5.0, par.read_segments(board)[0].length_mm)

    def test_vias_are_counted_even_though_they_are_not_modelled(self):
        self.assertEqual(1, par.count_vias(BOARD))

    def test_a_malformed_segment_is_dropped_not_guessed(self):
        board = ('(kicad_pcb (segment (start 0 0) (width 0.2) '
                 '(layer "F.Cu") (net "D")))')
        self.assertEqual([], par.read_segments(board))


class ExtractionArithmetic(unittest.TestCase):
    def setUp(self):
        self.segments = par.read_segments(BOARD)
        self.result = par.extract(self.segments, _layers(), **SOLVER)

    def test_resistance_matches_rho_l_over_area(self):
        """Closed arithmetic, no fitted coefficient: 20 mm of 0.2 x 0.035 mm."""
        entry = self.result["nets"]["SIG"]
        expected = par.COPPER_RHO_OHM_M * 0.020 / (0.2e-3 * 0.035e-3)
        self.assertAlmostEqual(expected, entry.r_ohm, places=9)

    def test_inductance_obeys_the_tem_identity(self):
        """L' = 1/(c^2 C0'). Checked here against the solver's own vacuum
        capacitance, so a change to either side breaks this."""
        unit = par.per_unit_length(0.2, _layers()["F.Cu"], **SOLVER)
        section = None  # the identity is what matters, not the geometry
        self.assertAlmostEqual(
            unit["l_h_per_m"] * (C_MPS ** 2),
            1.0 / (unit["c_f_per_m"] / unit["er_eff"]), places=6)
        self.assertIsNone(section)

    def test_er_eff_is_between_air_and_laminate(self):
        unit = par.per_unit_length(0.2, _layers()["F.Cu"], **SOLVER)
        self.assertGreater(unit["er_eff"], 1.0)
        self.assertLess(unit["er_eff"], 4.3)

    def test_parasitics_scale_with_length(self):
        short = par.extract(
            [par.Segment("N", "F.Cu", 0.2, 10.0)], _layers(), **SOLVER)
        long = par.extract(
            [par.Segment("N", "F.Cu", 0.2, 20.0)], _layers(), **SOLVER)
        for attr in ("r_ohm", "l_nh", "c_pf"):
            with self.subTest(quantity=attr):
                self.assertAlmostEqual(
                    2.0, getattr(long["nets"]["N"], attr)
                    / getattr(short["nets"]["N"], attr), places=6)

    def test_a_wider_trace_has_less_resistance_and_more_capacitance(self):
        narrow = par.extract([par.Segment("N", "F.Cu", 0.2, 10.0)],
                             _layers(), **SOLVER)["nets"]["N"]
        wide = par.extract([par.Segment("N", "F.Cu", 0.4, 10.0)],
                           _layers(), **SOLVER)["nets"]["N"]
        self.assertLess(wide.r_ohm, narrow.r_ohm)
        self.assertGreater(wide.c_pf, narrow.c_pf)

    def test_the_lumped_validity_frequency_is_reported(self):
        entry = self.result["nets"]["SIG"]
        self.assertLess(entry.lumped_valid_to_hz(3.0), 1e12)
        self.assertGreater(entry.lumped_valid_to_hz(3.0), 0.0)


class ExtractionRefusals(unittest.TestCase):
    """Fault injection. Each is a way the extraction could have produced a
    confident number from nothing."""

    def test_a_stackup_layer_without_a_source_is_refused(self):
        with self.assertRaises(par.ParasiticError) as caught:
            par.LayerSpec("F.Cu", 4.3, 0.1, 0.035, "  ")
        self.assertIn("source", str(caught.exception))

    def test_non_physical_stackup_values_are_refused(self):
        for bad in ({"er": 0.5}, {"height_mm": 0.0},
                    {"copper_thickness_mm": -1.0}):
            with self.subTest(**bad), self.assertRaises(par.ParasiticError):
                par.LayerSpec("F.Cu", **{"er": 4.3, "height_mm": 0.1,
                                         "copper_thickness_mm": 0.035,
                                         "source": FIXTURE, **bad})

    def test_an_unrouted_board_is_refused_not_extracted_as_zero(self):
        with self.assertRaises(par.ParasiticError) as caught:
            par.extract([], _layers())
        self.assertIn("not the same as small ones", str(caught.exception))

    def test_an_undescribed_layer_is_skipped_and_reported(self):
        """In1.Cu is routed but absent from the stackup. Guessing a height
        for it would put a fabricated number in every net that touches it."""
        out = par.extract(par.read_segments(BOARD), _layers(), **SOLVER)
        self.assertEqual({"In1.Cu": 1}, out["skipped_layers"])
        self.assertEqual(2, out["nets"]["SIG"].segments)


class SpiceEmission(unittest.TestCase):
    def setUp(self):
        self.result = par.extract(par.read_segments(BOARD), _layers(), **SOLVER)

    def test_the_subckt_declares_its_model_class(self):
        text = par.to_subckt(self.result["nets"]["SIG"])
        self.assertIn(".subckt PAR_SIG in out", text)
        self.assertIn("SCREEN", text)
        self.assertIn("vias not modelled", text)

    def test_the_capacitance_is_split_across_both_ends(self):
        entry = self.result["nets"]["SIG"]
        text = par.to_subckt(entry)
        halves = [line for line in text.splitlines()
                  if line.startswith(("C1 ", "C2 "))]
        self.assertEqual(2, len(halves))
        for line in halves:
            value = float(line.split()[-1].rstrip("p"))
            self.assertAlmostEqual(entry.c_pf / 2.0, value, places=4)

    def test_the_library_refuses_to_be_empty(self):
        with self.assertRaises(par.ParasiticError):
            par.to_spice_library({"nets": {}})


class BackAnnotation(unittest.TestCase):
    """Pre-layout vs post-layout: the difference is the whole point."""

    NETLIST = "* circuit\nR1 in out 1k\n.end\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        result = par.extract(par.read_segments(BOARD), _layers(), **SOLVER)
        self.library = Path(self.tmp.name) / "par.cir"
        self.library.write_text(par.to_spice_library(result), encoding="utf-8")

    def test_no_library_is_no_coverage_and_leaves_the_netlist_alone(self):
        out, finding = ngspice.back_annotate(self.NETLIST, None)
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(self.NETLIST, out)
        self.assertIn("PRE-layout", finding["detail"])

    def test_a_missing_library_does_not_fall_back_silently(self):
        """A missing file and a clean layout must not produce the same
        report - that is the failure this whole bundle is built against."""
        out, finding = ngspice.back_annotate(
            self.NETLIST, str(Path(self.tmp.name) / "absent.cir"))
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertEqual(self.NETLIST, out)

    def test_an_empty_library_annotates_nothing_and_says_so(self):
        empty = Path(self.tmp.name) / "empty.cir"
        empty.write_text("* nothing here\n", encoding="utf-8")
        _out, finding = ngspice.back_annotate(self.NETLIST, str(empty))
        self.assertEqual("NO_COVERAGE", finding["status"])
        self.assertIn("nothing to annotate", finding["detail"])

    def test_a_real_library_is_spliced_in_before_the_end_card(self):
        out, finding = ngspice.back_annotate(self.NETLIST, str(self.library))
        self.assertEqual("PASS", finding["status"])
        self.assertIn(".subckt PAR_SIG", out)
        self.assertTrue(out.rstrip().endswith(".end"))
        self.assertEqual(1, len([line for line in out.splitlines()
                                 if line.strip().lower() == ".end"]))

    def test_the_splice_does_not_eat_an_existing_subckt(self):
        """``.ends`` starts with ``.end``. A prefix match would delete every
        subckt the schematic already had and still simulate."""
        netlist = ("* circuit\n.subckt OPAMP a b\nR9 a b 1meg\n.ends\n"
                   "X1 in out OPAMP\n.end\n")
        out, _finding = ngspice.back_annotate(netlist, str(self.library))
        self.assertIn(".subckt OPAMP", out)
        self.assertIn(".ends", out)

    def test_post_layout_adds_impedance_the_pre_layout_netlist_did_not_have(self):
        pre, _ = ngspice.back_annotate(self.NETLIST, None)
        post, _ = ngspice.back_annotate(self.NETLIST, str(self.library))
        self.assertNotIn("L1", pre)
        self.assertIn("L1", post)
        self.assertGreater(len(post), len(pre))


class ParasiticsCli(unittest.TestCase):
    SCRIPT = SCRIPTS / "sim" / "parasitics.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False)

    def test_the_self_test_passes(self):
        self.assertEqual(0, self.run_cli("--self-test").returncode)

    def test_a_stackup_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text(BOARD, encoding="utf-8")
            proc = self.run_cli(str(board))
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("stackup", proc.stdout + proc.stderr)

    def test_a_run_reports_vias_as_uncovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text(BOARD, encoding="utf-8")
            stack = Path(tmp) / "s.json"
            stack.write_text(json.dumps({"layers": {
                "F.Cu": {"er": 4.3, "height_mm": 0.1,
                         "copper_thickness_mm": 0.035, "source": FIXTURE}}}),
                encoding="utf-8")
            out = Path(tmp) / "r.json"
            self.run_cli(str(board), "--stackup", str(stack),
                         "--json", str(out), "--cells-per-feature", "2",
                         "--max-nodes", "30000")
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertFalse(report["signoff"])
            self.assertEqual("SCREEN", report["model_class"])
            vias = [c for c in report["checks"] if c["check"] == "vias_modelled"]
            self.assertEqual("NO_COVERAGE", vias[0]["status"])


if __name__ == "__main__":
    unittest.main()
