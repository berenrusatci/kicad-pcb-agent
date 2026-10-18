"""Tests for common/i2c.py and scripts/sch/i2c_pullup.py.

THE TEST THAT JUSTIFIES THE TOOL
--------------------------------------------------------------------------
``test_two_correct_pullups_are_one_wrong_network``: CAM_I2C carries two
1.5k pull-ups on each line. Either one alone sits inside the window. In
parallel they are 750 ohm, below Rmin, and the driver is being asked for
more current than its datasheet promises. Every schematic reviewer who
looks at one resistor at a time passes this board, and so does ERC, which
has no idea what a pull-up is for.

Everything else here defends the refusals:

  * a number with no source is a GAP, not a number - and a gap is
    NO_COVERAGE, never a PASS with a plausible default;
  * a source document with no revision names a moving target, so it is
    the same gap;
  * a net that is not in the netlist is NO_COVERAGE, not "no pull-ups
    found, therefore fail" and certainly not silence;
  * a two-pin part whose value will not read (``DNP``) makes the parallel
    equivalent UNKNOWN, because dropping it biases the answer high, which
    is the direction that passes;
  * a slave IC touching SDA and VDD is not a pull-up. That one is a
    regression test: the first cut of the resolver used "bridges both
    nets" and reported NO_COVERAGE on a perfectly ordinary bus.

Every electrical number below comes from tools/fixtures/i2c_pullup/, whose
sources are all clearly labelled FIXTURE. None of them is this project's
value, and the tool ships no I2C constant of its own.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.findings import NO_COVERAGE  # noqa: E402
from common.i2c import (ABOVE_RMAX, BELOW_RMIN, IN_WINDOW,  # noqa: E402
                        MISSING_INPUT, NO_PULLUP, SOURCE_INCOMPLETE,
                        UNPARSEABLE_RESISTOR_VALUE, UNSOURCED_INPUT,
                        WINDOW_EMPTY, classify, parallel_ohms,
                        parse_resistance, r_max_ohms, r_min_ohms,
                        read_rise_time_band, read_sourced, resolve_pullups,
                        rise_time_coefficient)
from common.netlist import read_components, read_nets  # noqa: E402
from scripts.sch.i2c_pullup import (build_report, evaluate_segment,  # noqa: E402
                                    load_segments, main, read_window_inputs)

FIXTURES = TOOLS / "fixtures" / "i2c_pullup"
NETLIST = FIXTURES / "netlist.net"
SENSOR_OK = FIXTURES / "segments_sensor_ok.json"
DEFECTS = FIXTURES / "segments_defects.json"
SCRIPT = TOOLS / "scripts" / "sch" / "i2c_pullup.py"

# The fixture window, recomputed here from the fixture's own declared
# numbers rather than pasted, so a fixture edit fails loudly instead of
# quietly disagreeing with the assertions.
FIXTURE_R_MIN = (3.3 - 0.4) / 0.003                      # 966.67 ohm
FIXTURE_R_MAX = 3e-7 / (rise_time_coefficient(0.3, 0.7) * 1e-10)   # 3540.7 ohm

ELECTRICAL_INPUTS = ("vdd_v", "vol_v", "iol_a", "c_bus_f", "t_rise_s",
                     "rise_time_band")


def fixture_nets():
    return read_nets(NETLIST), read_components(NETLIST)


def sensor_segment() -> dict:
    """A fresh, complete, fully sourced segment from the fixture."""
    doc = json.loads(SENSOR_OK.read_text(encoding="utf-8"))
    return copy.deepcopy(doc["segments"][0])


def segment_on(bus: str, sda: str, scl: str) -> dict:
    segment = sensor_segment()
    segment["bus"] = bus
    segment["lines"] = {"SDA": sda, "SCL": scl}
    return segment


def evaluate(segment: dict) -> dict:
    nets, components = fixture_nets()
    return evaluate_segment(nets, components, segment)


def line(segment_report: dict, name: str) -> dict:
    return next(entry for entry in segment_report["lines"]
                if entry["line"] == name)


class ResistanceParsingTest(unittest.TestCase):
    def test_reads_the_forms_a_netlist_actually_contains(self):
        self.assertEqual(parse_resistance("2.2k"), 2200.0)
        self.assertEqual(parse_resistance("2k2"), 2200.0)   # infix notation
        self.assertEqual(parse_resistance("470"), 470.0)
        self.assertEqual(parse_resistance("470R"), 470.0)
        self.assertEqual(parse_resistance("10k"), 10000.0)
        self.assertEqual(parse_resistance("1M"), 1e6)
        self.assertEqual(parse_resistance("4.7 kOhm"), 4700.0)

    def test_refuses_what_it_cannot_read_instead_of_guessing(self):
        # Each of these is a real value seen in a real BOM field. Reading
        # any of them as a number would put a fabricated resistance into a
        # parallel equivalent.
        for text in ("DNP", "", "   ", "4.7k 1%", "R_PULLUP", None, "?"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_resistance(text)


class WindowArithmeticTest(unittest.TestCase):
    def test_rmin_is_the_drivers_limit(self):
        self.assertAlmostEqual(r_min_ohms(3.3, 0.4, 0.003), 966.6666666, 5)

    def test_the_085_in_the_matrix_note_is_the_declared_band(self):
        # 0.8473, not a stored constant: ln((1-0.3)/(1-0.7)).
        self.assertAlmostEqual(rise_time_coefficient(0.3, 0.7), 0.84729786, 7)
        # A different band gives a different coefficient, which is the whole
        # point of deriving it.
        self.assertNotAlmostEqual(rise_time_coefficient(0.2, 0.8), 0.84729786, 3)

    def test_rmax_is_the_rise_time_limit(self):
        value = r_max_ohms(3e-7, 1e-10, rise_time_coefficient(0.3, 0.7))
        self.assertAlmostEqual(value, FIXTURE_R_MAX, 6)

    def test_parallel_equivalent_halves_for_two_equal_pullups(self):
        self.assertEqual(parallel_ohms([1500.0, 1500.0]), 750.0)
        self.assertAlmostEqual(parallel_ohms([4700.0, 4700.0]), 2350.0)
        self.assertAlmostEqual(parallel_ohms([2200.0, 4700.0]), 1498.5507, 4)

    def test_no_pullup_is_not_infinite_ohms_near_enough(self):
        self.assertIsNone(parallel_ohms([]))

    def test_classify_names_which_side_of_the_window(self):
        self.assertEqual(classify(2200.0, 966.7, 3540.7), IN_WINDOW)
        self.assertEqual(classify(750.0, 966.7, 3540.7), BELOW_RMIN)
        self.assertEqual(classify(10000.0, 966.7, 3540.7), ABOVE_RMAX)
        self.assertEqual(classify(None, 966.7, 3540.7), NO_PULLUP)
        # An empty window blames the window, not the fitted resistor: no
        # value of R satisfies it, so pointing at R sends the reader to the
        # wrong component.
        self.assertEqual(classify(2200.0, 4000.0, 1000.0), WINDOW_EMPTY)

    def test_contradictory_inputs_raise_rather_than_return_a_number(self):
        with self.assertRaises(ValueError):
            r_min_ohms(3.3, 3.6, 0.003)      # VOL above VDD
        with self.assertRaises(ValueError):
            r_min_ohms(3.3, 0.4, 0.0)        # IOL of zero
        with self.assertRaises(ValueError):
            r_max_ohms(3e-7, 0.0, 0.85)      # no capacitance
        with self.assertRaises(ValueError):
            rise_time_coefficient(0.7, 0.3)  # band upside down


class SourcedInputTest(unittest.TestCase):
    def test_a_complete_input_carries_its_citation(self):
        value, gap = read_sourced(
            {"vdd_v": {"value": 3.3,
                       "source": {"document": "FIXTURE doc", "revision": "1",
                                  "clause": "Table 2"}}}, "vdd_v", "V")
        self.assertIsNone(gap)
        self.assertEqual(value.value, 3.3)
        self.assertEqual(value.source.cite(), "FIXTURE doc rev 1 §Table 2")

    def test_a_bare_number_is_a_gap_not_a_value(self):
        value, gap = read_sourced({"vdd_v": 3.3}, "vdd_v", "V")
        self.assertIsNone(value)
        self.assertEqual(gap.code, UNSOURCED_INPUT)

    def test_an_absent_input_is_a_gap(self):
        value, gap = read_sourced({}, "vdd_v", "V")
        self.assertIsNone(value)
        self.assertEqual(gap.code, MISSING_INPUT)

    def test_a_document_without_a_revision_names_a_moving_target(self):
        value, gap = read_sourced(
            {"t_rise_s": {"value": 3e-7,
                          "source": {"document": "some I2C spec"}}},
            "t_rise_s", "s")
        self.assertIsNone(value)
        self.assertEqual(gap.code, SOURCE_INCOMPLETE)
        self.assertIn("revision", gap.detail)

    def test_a_non_numeric_value_is_a_hard_error_not_a_gap(self):
        # "3.3 volts" is a malformed document, not an absent input; it must
        # not be reported as something a source would fix.
        with self.assertRaises(ValueError):
            read_sourced({"vdd_v": {"value": "3.3 volts",
                                    "source": {"document": "d",
                                               "revision": "1"}}},
                         "vdd_v", "V")

    def test_the_rise_time_band_is_sourced_too(self):
        band, gap = read_rise_time_band(
            {"rise_time_band": {"low_fraction": 0.3, "high_fraction": 0.7,
                                "source": {"document": "FIXTURE spec",
                                           "revision": "1"}}})
        self.assertIsNone(gap)
        self.assertEqual((band.low_fraction, band.high_fraction), (0.3, 0.7))
        _, gap = read_rise_time_band(
            {"rise_time_band": {"low_fraction": 0.3, "high_fraction": 0.7}})
        self.assertEqual(gap.code, UNSOURCED_INPUT)


class NetlistResolutionTest(unittest.TestCase):
    """The network is read from the netlist, or it is not read at all."""

    def setUp(self):
        self.nets, self.components = fixture_nets()

    def resolve(self, net, rail="+3V3"):
        return resolve_pullups(self.nets, self.components, "SDA", net, rail)

    def test_a_single_pullup_resolves_to_its_value(self):
        resolution = self.resolve("SENSOR_SDA")
        self.assertEqual(resolution.gaps, [])
        self.assertEqual([p.ref for p in resolution.pullups], ["R101"])
        self.assertEqual(resolution.parallel_ohms, 2200.0)

    def test_two_pullups_on_one_segment_halve_the_equivalent(self):
        resolution = self.resolve("CAM_SDA")
        self.assertEqual([p.ref for p in resolution.pullups], ["R201", "R203"])
        self.assertEqual([p.ohms for p in resolution.pullups], [1500.0, 1500.0])
        self.assertEqual(resolution.parallel_ohms, 750.0)

    def test_a_slave_ic_touching_sda_and_vdd_is_not_a_pullup(self):
        # Regression: every powered device on the bus has a pin on the line
        # and a pin on the rail. A "bridges both nets" rule reports the whole
        # ordinary bus as unmodelled and the tool never says anything again.
        resolution = self.resolve("SENSOR_SDA")
        self.assertIn("U1", resolution.refs_on_net)
        self.assertNotIn("U1", [p.ref for p in resolution.pullups])
        self.assertEqual([entry[0] for entry in resolution.other_bridging],
                         ["U1"])

    def test_a_two_pin_part_with_an_unreadable_value_is_no_coverage(self):
        resolution = self.resolve("DNP_SDA")
        self.assertEqual([g.code for g in resolution.gaps],
                         [UNPARSEABLE_RESISTOR_VALUE])
        # And the equivalent is withheld, not computed from what was legible.
        self.assertIsNone(resolution.parallel_ohms)
        self.assertEqual(resolution.pullups, [])

    def test_a_net_absent_from_the_netlist_is_no_coverage(self):
        resolution = self.resolve("GHOST_SDA")
        self.assertFalse(resolution.resolved)
        self.assertEqual([g.code for g in resolution.gaps],
                         ["SEGMENT_NET_NOT_IN_NETLIST"])

    def test_a_rail_absent_from_the_netlist_is_no_coverage(self):
        resolution = self.resolve("SENSOR_SDA", rail="+9V9")
        self.assertFalse(resolution.resolved)
        self.assertEqual([g.code for g in resolution.gaps],
                         ["RAIL_NET_NOT_IN_NETLIST"])

    def test_a_segment_with_no_pullup_at_all_resolves_and_reports_none(self):
        resolution = self.resolve("DNP_SCL")
        self.assertEqual(resolution.gaps, [])          # nothing was ambiguous
        self.assertIsNone(resolution.parallel_ohms)    # there is just nothing


class SegmentVerdictTest(unittest.TestCase):
    def test_a_correct_pullup_passes_and_shows_its_window(self):
        report = evaluate(sensor_segment())
        self.assertEqual(report["status"], "PASS")
        self.assertAlmostEqual(report["window"]["r_min_ohms"], FIXTURE_R_MIN, 6)
        self.assertAlmostEqual(report["window"]["r_max_ohms"], FIXTURE_R_MAX, 6)
        for name in ("SDA", "SCL"):
            entry = line(report, name)
            self.assertEqual(entry["verdict"], IN_WINDOW)
            self.assertEqual(entry["parallel_ohms"], 2200.0)

    def test_two_correct_pullups_are_one_wrong_network(self):
        """The defect the row exists for.

        1.5k alone is inside [966.7, 3540.7]. Two of them are not, and no
        single-resistor review catches it.
        """
        self.assertEqual(classify(1500.0, FIXTURE_R_MIN, FIXTURE_R_MAX),
                         IN_WINDOW)
        report = evaluate(segment_on("CAM_I2C", "CAM_SDA", "CAM_SCL"))
        self.assertEqual(report["status"], "FAIL")
        entry = line(report, "SDA")
        self.assertEqual(entry["parallel_ohms"], 750.0)
        self.assertEqual(entry["verdict"], BELOW_RMIN)
        self.assertEqual([p["ref"] for p in entry["pullups"]],
                         ["R201", "R203"])

    def test_a_single_pullup_below_rmin_fails(self):
        report = evaluate(segment_on("PMIC_I2C", "PMIC_SDA", "PMIC_SCL"))
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(line(report, "SDA")["verdict"], BELOW_RMIN)
        self.assertEqual(line(report, "SDA")["parallel_ohms"], 470.0)

    def test_a_pullup_above_rmax_fails(self):
        report = evaluate(segment_on("EEPROM_I2C", "EEPROM_SDA", "EEPROM_SCL"))
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(line(report, "SDA")["verdict"], ABOVE_RMAX)
        self.assertEqual(line(report, "SDA")["parallel_ohms"], 10000.0)

    def test_no_pullup_names_the_parts_that_are_not_modelled(self):
        report = evaluate(segment_on("LS_I2C", "LS_SDA", "DNP_SCL"))
        entry = line(report, "SDA")
        self.assertEqual(entry["verdict"], NO_PULLUP)
        self.assertEqual(entry["status"], "FAIL")
        self.assertIn("Q1", entry["detail"])
        self.assertEqual(entry["other_bridging_refs"],
                         [{"ref": "Q1", "pins": 3}, {"ref": "U5", "pins": 4}])

    def test_a_fully_sourced_but_contradictory_input_set_is_not_a_pass(self):
        segment = sensor_segment()
        segment["inputs"]["vol_v"]["value"] = 4.0     # VOL above VDD
        report = evaluate(segment)
        self.assertEqual(report["status"], "FAIL")
        self.assertIsNone(report["window"])
        self.assertIn("VOL", report["window_error"])


class MissingSourcedInputTest(unittest.TestCase):
    """Every electrical input, removed one at a time, must give NO_COVERAGE.

    The netlist resolves perfectly in all of these. That is the point: the
    design half of the check is fine and the answer is still "not
    established", because the window was never built.
    """

    def assert_no_coverage(self, segment, expected_code=None):
        report = evaluate(segment)
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertNotIn("PASS", {e["status"] for e in report["lines"]})
        self.assertTrue(report["gaps"])
        self.assertIsNone(report["window"])
        if expected_code:
            self.assertIn(expected_code, [g["code"] for g in report["gaps"]])
        return report

    def test_each_missing_input_is_no_coverage(self):
        for field in ELECTRICAL_INPUTS:
            with self.subTest(missing=field):
                segment = sensor_segment()
                del segment["inputs"][field]
                report = self.assert_no_coverage(segment, MISSING_INPUT)
                # The report must say WHICH input is absent, by name.
                self.assertEqual(report["inputs"][field]["absent"],
                                 MISSING_INPUT)

    def test_each_unsourced_input_is_no_coverage(self):
        for field in ("vdd_v", "vol_v", "iol_a", "c_bus_f", "t_rise_s"):
            with self.subTest(unsourced=field):
                segment = sensor_segment()
                segment["inputs"][field] = segment["inputs"][field]["value"]
                self.assert_no_coverage(segment, UNSOURCED_INPUT)

    def test_a_missing_spec_revision_is_no_coverage(self):
        # tr without a revision is "300 ns according to something".
        segment = sensor_segment()
        del segment["inputs"]["t_rise_s"]["source"]["revision"]
        report = self.assert_no_coverage(segment, SOURCE_INCOMPLETE)
        self.assertIn("t_rise_s", [g["field"] for g in report["gaps"]])

    def test_a_missing_mode_is_no_coverage(self):
        segment = sensor_segment()
        del segment["mode"]
        self.assert_no_coverage(segment, "MISSING_MODE")

    def test_declaring_the_band_and_the_coefficient_is_a_contradiction(self):
        segment = sensor_segment()
        segment["inputs"]["rise_time_coefficient"] = {
            "value": 0.85, "source": {"document": "FIXTURE", "revision": "1"}}
        with self.assertRaises(ValueError):
            read_window_inputs(segment)

    def test_a_sourced_coefficient_may_replace_the_band(self):
        segment = sensor_segment()
        del segment["inputs"]["rise_time_band"]
        segment["inputs"]["rise_time_coefficient"] = {
            "value": 0.8472978603872034,
            "source": {"document": "FIXTURE - synthetic spec", "revision": "1",
                       "clause": "stated multiplier"}}
        report = evaluate(segment)
        self.assertEqual(report["status"], "PASS")
        self.assertIn("declared directly",
                      report["window"]["rise_time_coefficient_basis"])


class UnresolvableSegmentTest(unittest.TestCase):
    def test_a_segment_whose_nets_are_not_in_the_netlist_is_no_coverage(self):
        report = evaluate(segment_on("GHOST_I2C", "GHOST_SDA", "GHOST_SCL"))
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertEqual({e["status"] for e in report["lines"]}, {NO_COVERAGE})
        self.assertEqual({g["code"] for g in report["gaps"]},
                         {"SEGMENT_NET_NOT_IN_NETLIST"})
        # The window was computable; the design was not. Both facts survive.
        self.assertIsNotNone(report["window"])

    def test_a_segment_on_an_absent_rail_is_no_coverage(self):
        segment = sensor_segment()
        segment["rail_net"] = "+1V8"
        report = evaluate(segment)
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertEqual({g["code"] for g in report["gaps"]},
                         {"RAIL_NET_NOT_IN_NETLIST"})


class ReportTest(unittest.TestCase):
    def build(self, path):
        nets, components = fixture_nets()
        segments = [evaluate_segment(nets, components, entry)
                    for entry in load_segments(path)]
        return build_report(NETLIST, path, segments)

    def test_a_clean_run_is_green(self):
        report = self.build(SENSOR_OK)
        self.assertEqual(report["status"], "GREEN")
        self.assertEqual(report["reason_codes"], [])
        self.assertEqual(report["coverage"][NO_COVERAGE], 0)

    def test_every_electrical_input_carries_where_it_came_from(self):
        report = self.build(SENSOR_OK)
        inputs = report["segments"][0]["inputs"]
        for field in ("vdd_v", "vol_v", "iol_a", "c_bus_f", "t_rise_s"):
            with self.subTest(field=field):
                source = inputs[field]["source"]
                self.assertTrue(source["document"].strip())
                self.assertTrue(source["revision"].strip())
                self.assertIn(source["document"], inputs[field]["cite"])
        self.assertIn("source", inputs["rise_time_band"])
        # The coefficient is traceable to the clause that defines the band.
        self.assertIn("ln((1-0.3)/(1-0.7))",
                      report["segments"][0]["window"]
                      ["rise_time_coefficient_basis"])

    def test_the_report_is_bound_to_the_bytes_it_read(self):
        report = self.build(SENSOR_OK)
        self.assertEqual(len(report["netlist"]["sha256"]), 64)
        self.assertEqual(len(report["segment_document"]["sha256"]), 64)

    def test_defects_roll_up_to_red_and_name_their_codes(self):
        report = self.build(DEFECTS)
        self.assertEqual(report["status"], "RED")
        self.assertEqual(set(report["reason_codes"]),
                         {BELOW_RMIN, ABOVE_RMAX})
        self.assertEqual(report["coverage"]["FAIL"], 1)

    def test_a_missing_net_does_not_blame_the_sourced_inputs(self):
        # Two different failures with two different owners: an input gap is
        # answered by citing a document, a missing net by fixing the
        # schematic. A report that folds them together sends the reader to
        # the wrong desk.
        nets, components = fixture_nets()
        segments = [evaluate_segment(nets, components,
                                     segment_on("G", "GHOST_SDA", "GHOST_SCL"))]
        report = build_report(NETLIST, None, segments)
        findings = {f["check"]: f for f in report["findings"]}
        self.assertEqual(findings["i2c_electrical_inputs_sourced"]["status"],
                         "PASS")
        self.assertEqual(findings["i2c_pullup_network_resolved"]["status"],
                         NO_COVERAGE)
        self.assertEqual(report["status"], NO_COVERAGE)

    def test_a_defect_beats_a_gap_in_the_rollup(self):
        # A run with one measured failure and one unmeasurable segment is
        # RED: the defect that WAS found stays found.
        nets, components = fixture_nets()
        segments = [evaluate_segment(nets, components,
                                     segment_on("CAM_I2C", "CAM_SDA", "CAM_SCL")),
                    evaluate_segment(nets, components,
                                     segment_on("GHOST", "GHOST_SDA", "GHOST_SCL"))]
        report = build_report(NETLIST, None, segments)
        self.assertEqual(report["status"], "RED")


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        """Alt surec + DETERMINISTIK metin kodlamasi.

        `text=True` tek basina `locale.getpreferredencoding()` kullanir; bu
        makinede cp1254. Aracin JSON raporu ise UTF-8 yazilir ve icinde
        '§' (SBVS/JEDEC bolum isareti) gecer. Sonuc olculdu 2026-08-11:
        `test_report_is_written_where_asked` DOSYA ile STDOUT'u
        karsilastirirken dosyada '§', stdout'ta mojibake goruyordu ve test
        araci degil KONSOLU olcuyordu.

        `PYTHONIOENCODING` cocuga da verilir: yalniz ebeveynde cozmek yetmez,
        cunku cocugun kendi stdout'u da yerel kod sayfasiyla kodlar.
        Ayni desen bugun yazilan uc muhafiz testinde de kullanildi
        (`tests/test_verify_zones.py::_run`).
        """
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)

    def test_a_clean_bus_exits_zero(self):
        done = self.run_cli("--netlist", str(NETLIST),
                            "--segments", str(SENSOR_OK))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "GREEN")

    def test_defects_exit_one(self):
        done = self.run_cli("--netlist", str(NETLIST),
                            "--segments", str(DEFECTS))
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["status"], "RED")

    def test_no_coverage_never_exits_zero(self):
        with_gap = json.loads(SENSOR_OK.read_text(encoding="utf-8"))
        del with_gap["segments"][0]["inputs"]["c_bus_f"]
        path = TOOLS / "tests" / "_i2c_no_coverage.tmp.json"
        path.write_text(json.dumps(with_gap), encoding="utf-8")
        try:
            done = self.run_cli("--netlist", str(NETLIST),
                                "--segments", str(path))
        finally:
            path.unlink()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["status"], NO_COVERAGE)

    def test_a_missing_argument_is_an_error_not_a_default(self):
        done = self.run_cli("--netlist", str(NETLIST))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stdout)["status"], "ERROR")

    def test_self_test_passes(self):
        self.assertEqual(self.run_cli("--self-test").returncode, 0)

    def test_report_is_written_where_asked(self):
        path = TOOLS / "tests" / "_i2c_report.tmp.json"
        try:
            done = self.run_cli("--netlist", str(NETLIST),
                                "--segments", str(SENSOR_OK),
                                "--json", str(path))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                             json.loads(done.stdout))
        finally:
            path.unlink(missing_ok=True)


class InputDocumentTest(unittest.TestCase):
    def test_a_segment_without_an_identity_is_refused(self):
        path = TOOLS / "tests" / "_i2c_bad_doc.tmp.json"
        for doc in ({"segments": []},
                    {"segments": [{"rail_net": "+3V3", "lines": {"SDA": "A"}}]},
                    {"segments": [{"bus": "B", "lines": {"SDA": "A"}}]},
                    {"segments": [{"bus": "B", "rail_net": "+3V3"}]}):
            with self.subTest(doc=doc):
                path.write_text(json.dumps(doc), encoding="utf-8")
                try:
                    with self.assertRaises(ValueError):
                        load_segments(path)
                finally:
                    path.unlink()

    def test_main_refuses_to_run_without_a_netlist(self):
        with self.assertRaises(ValueError):
            main(["--segments", str(SENSOR_OK)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
