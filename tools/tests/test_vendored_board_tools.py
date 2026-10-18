#!/usr/bin/env python3
"""scripts/board/measure_board.py and scripts/board/pair_coupling.py.

Both were vendored from unversioned scripts with no tests. The geometry is
exercised WITHOUT pcbnew (the maths is pure and importable); only the
end-to-end runs need KiCad.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import BOARD_SCRIPTS, SCHEMAS  # noqa: E402

MEASURE = BOARD_SCRIPTS / "measure_board.py"
COUPLING = BOARD_SCRIPTS / "pair_coupling.py"
COUPLING_SCHEMA = json.loads(
    (SCHEMAS / "pair_coupling_report.schema.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(BOARD_SCRIPTS))
import measure_board  # noqa: E402
import pair_coupling  # noqa: E402

from common.findings import by_check_map  # noqa: E402
from common.hashing import sha256_file  # noqa: E402

HAVE_PCBNEW = importlib.util.find_spec("pcbnew") is not None


def missing_required(document: dict, schema: dict) -> list[str]:
    """Fields the schema marks required that the document does not carry."""
    return [key for key in schema.get("required", ()) if key not in document]


def run(*args: str) -> tuple[int, dict | None, str]:
    proc = subprocess.run([sys.executable, *args], text=True, encoding="utf-8",
                          capture_output=True, check=False)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


class MeasureGeometryTests(unittest.TestCase):
    def test_perpendicular_distance(self):
        self.assertAlmostEqual(
            1.0, measure_board.seg_dist((0.0, 1.0), (-5.0, 0.0), (5.0, 0.0)))

    def test_distance_clamps_to_the_endpoint(self):
        self.assertAlmostEqual(
            5.0, measure_board.seg_dist((10.0, 0.0), (-5.0, 0.0), (5.0, 0.0)))

    def test_a_degenerate_segment_is_a_point(self):
        self.assertAlmostEqual(
            1.0, measure_board.seg_dist((0.0, 1.0), (0.0, 0.0), (0.0, 0.0)))

    def test_a_parallel_pair_is_fully_coupled(self):
        coupled, total, percent = measure_board.coupled_fraction(
            [((0.0, 0.0), (10.0, 0.0))], [((0.0, 0.2), (10.0, 0.2))], 0.3)
        self.assertAlmostEqual(10.0, total, places=6)
        self.assertGreater(percent, 99.9)
        self.assertGreater(coupled, 9.9)

    def test_a_pair_beyond_tolerance_is_not_coupled(self):
        _c, _t, percent = measure_board.coupled_fraction(
            [((0.0, 0.0), (10.0, 0.0))], [((0.0, 0.2), (10.0, 0.2))], 0.1)
        self.assertEqual(0.0, percent)

    def test_an_empty_partner_is_never_coupled(self):
        """FAULT INJECTION: the failure that would make every board look
        perfectly coupled."""
        _c, _t, percent = measure_board.coupled_fraction(
            [((0.0, 0.0), (10.0, 0.0))], [], 1000.0)
        self.assertEqual(0.0, percent)

    def test_a_right_angle_corner_is_detected(self):
        corners = measure_board.corner_angles({"N": [
            ((0.0, 0.0), (5.0, 0.0), "F.Cu"),
            ((5.0, 0.0), (5.0, 5.0), "F.Cu"),
        ]})
        self.assertEqual(1, len(corners))
        self.assertAlmostEqual(90.0, corners[0]["turn_deg"], places=1)

    def test_a_45_degree_corner_is_not_a_right_angle(self):
        corners = measure_board.corner_angles({"N": [
            ((0.0, 0.0), (5.0, 0.0), "F.Cu"),
            ((5.0, 0.0), (10.0, 5.0), "F.Cu"),
        ]})
        self.assertAlmostEqual(45.0, corners[0]["turn_deg"], places=1)

    def test_segments_on_different_layers_do_not_form_a_corner(self):
        self.assertEqual([], measure_board.corner_angles({"N": [
            ((0.0, 0.0), (5.0, 0.0), "F.Cu"),
            ((5.0, 0.0), (5.0, 5.0), "B.Cu"),
        ]}))

    def test_the_coupling_note_states_what_the_number_is_not(self):
        self.assertIn("not a field solution", measure_board.COUPLING_NOTE)


class CouplingGeometryTests(unittest.TestCase):
    def test_a_close_parallel_pair_couples(self):
        coupled, total = pair_coupling.coupled_length(
            [(0, 0.0, 0.0, 10.0, 0.0)], [(0, 0.0, 0.35, 10.0, 0.35)], 0.60)
        self.assertAlmostEqual(10.0, total, places=6)
        self.assertGreater(coupled / total, 0.99)

    def test_a_distant_pair_does_not_couple(self):
        coupled, _total = pair_coupling.coupled_length(
            [(0, 0.0, 0.0, 10.0, 0.0)], [(0, 0.0, 2.0, 10.0, 2.0)], 0.60)
        self.assertAlmostEqual(0.0, coupled)

    def test_the_layer_rule_holds(self):
        """Same X/Y, different layer: not coupled. The original honoured this
        and it is the difference between a pair and two unrelated tracks."""
        coupled, _total = pair_coupling.coupled_length(
            [(0, 0.0, 0.0, 10.0, 0.0)], [(31, 0.0, 0.35, 10.0, 0.35)], 0.60)
        self.assertAlmostEqual(0.0, coupled)

    def test_an_empty_partner_is_never_coupled(self):
        coupled, _total = pair_coupling.coupled_length(
            [(0, 0.0, 0.0, 10.0, 0.0)], [], 1000.0)
        self.assertAlmostEqual(0.0, coupled)

    def test_pair_discovery_needs_both_members(self):
        self.assertEqual(
            [("/CAM0_P", "/CAM0_N")],
            pair_coupling.discover_pairs(["/CAM0_P", "/CAM0_N", "/LONE_P",
                                          "GND"]))

    def test_the_net_prefix_filter_is_optional_not_hardcoded(self):
        """The original welded 'CAM'/'CSI' into the tool."""
        names = ["/CAM0_P", "/CAM0_N", "/USB_P", "/USB_N"]
        self.assertEqual(2, len(pair_coupling.discover_pairs(names)))
        self.assertEqual(1, len(pair_coupling.discover_pairs(names, ("CAM",))))

    def test_the_weaker_direction_decides(self):
        """A long P hugging a short N is not a coupled pair; averaging the two
        directions would hide exactly that."""
        long_p = [(0, 0.0, 0.0, 20.0, 0.0)]
        short_n = [(0, 0.0, 0.2, 5.0, 0.2)]
        coupled_p, total_p = pair_coupling.coupled_length(long_p, short_n, 0.6)
        coupled_n, total_n = pair_coupling.coupled_length(short_n, long_p, 0.6)
        self.assertLess(coupled_p / total_p, 0.5)
        self.assertGreater(coupled_n / total_n, 0.9)
        self.assertLess(min(coupled_p / total_p, coupled_n / total_n), 0.5)


# --------------------------------------------------------------------------- #
# A board without pcbnew: pair_coupling only ever asks a board for its tracks.
# --------------------------------------------------------------------------- #
class _Point:
    def __init__(self, x_mm: float, y_mm: float):
        self.x = int(x_mm * 1e6)                       # nm, as pcbnew reports
        self.y = int(y_mm * 1e6)


class _StubTrack:
    def __init__(self, net, layer, start, end):
        self._net, self._layer = net, layer
        self._start, self._end = _Point(*start), _Point(*end)

    def GetClass(self):                                # noqa: N802
        return "PCB_TRACK"

    def GetNetname(self):                              # noqa: N802
        return self._net

    def GetLayer(self):                                # noqa: N802
        return self._layer

    def GetStart(self):                                # noqa: N802
        return self._start

    def GetEnd(self):                                  # noqa: N802
        return self._end


# D0 runs 0.35 mm apart on one layer (coupled); D1 runs 2 mm apart (not).
STUB_TRACKS = [
    _StubTrack("/D0_P", 0, (0.0, 0.0), (10.0, 0.0)),
    _StubTrack("/D0_N", 0, (0.0, 0.35), (10.0, 0.35)),
    _StubTrack("/D1_P", 0, (0.0, 20.0), (10.0, 20.0)),
    _StubTrack("/D1_N", 0, (0.0, 22.0), (10.0, 22.0)),
]


class _StubBoard:
    def GetTracks(self):                               # noqa: N802
        return STUB_TRACKS


def screen_stub_board(board_path) -> dict:
    """Run ``screen`` against ``_StubBoard`` so the envelope is testable.

    The geometry is already covered above; what is under test here is the
    report ENVELOPE, and pinning it to a real KiCad install would have meant
    the check only ran where pcbnew happens to be importable.
    """
    original = pair_coupling.load_board
    pair_coupling.load_board = lambda _path: _StubBoard()
    try:
        return pair_coupling.screen(Path(board_path), prefixes=[],
                                    explicit_pairs=[], spacing_max_mm=0.60,
                                    threshold=0.50, step_mm=0.05)
    finally:
        pair_coupling.load_board = original


class CouplingReportSchemaTests(unittest.TestCase):
    """The produced report must satisfy the schema the report names.

    Nothing validated it, and that is exactly why ``design`` and ``metrics``
    were absent from every report this tool ever wrote while
    ``pair_coupling_report.schema.json`` listed both as REQUIRED: the report
    carried no board digest, so it could not be tied to the board it measured
    and any assertion resting on it was SCREEN_ONLY forever.

    These checks are targeted rather than routed through the hand-rolled
    ``validate()`` in ``test_skew_timing.py``: that walker implements exactly
    the keyword subset ``length_report.schema.json`` uses and reports every
    other keyword as an error, and this schema uses ``oneOf``, ``maximum`` and
    ``description``, which it does not. Growing it into a general validator
    would be re-implementing ``jsonschema``, which this repo deliberately does
    not depend on. Everything below therefore READS THE SCHEMA FILE instead of
    restating it, so the test cannot drift away from the contract.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="coupling-schema-")
        self.board = Path(self._tmp.name) / "b.kicad_pcb"
        self.board.write_bytes(b"(kicad_pcb (version 20260206))\n")
        self.report = screen_stub_board(self.board)

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_required_top_level_field_is_emitted(self):
        self.assertEqual([], missing_required(self.report, COUPLING_SCHEMA),
                         sorted(self.report))

    def test_the_required_field_check_has_teeth(self):
        """FAULT INJECTION: a check that never says no proves nothing.

        These are the two fields the producer actually omitted.
        """
        for field in ("design", "metrics"):
            with self.subTest(field=field):
                stripped = {k: v for k, v in self.report.items() if k != field}
                self.assertEqual([field],
                                 missing_required(stripped, COUPLING_SCHEMA))

    def test_design_ties_the_report_to_the_board_it_measured(self):
        design = self.report["design"]
        schema = COUPLING_SCHEMA["properties"]["design"]
        self.assertEqual([], missing_required(design, schema))
        self.assertEqual(str(self.board), design["path"])
        self.assertEqual(sha256_file(self.board), design["sha256"])
        # The digest shape the schema demands, read from the schema.
        pattern = schema["properties"]["sha256"]["oneOf"][0]["pattern"]
        self.assertRegex(design["sha256"], pattern)

    def test_an_unhashable_board_says_null_rather_than_omitting_design(self):
        """LOUD, not absent: a missing binding must be visible in the report."""
        report = screen_stub_board(Path(self._tmp.name) / "gone.kicad_pcb")
        self.assertEqual([], missing_required(report, COUPLING_SCHEMA))
        self.assertIsNone(report["design"]["sha256"])
        self.assertTrue(report["design"]["path"])

    def test_metrics_is_the_violation_count_keyed_by_check_name(self):
        metrics = self.report["metrics"]
        checks = {c["check"]: c for c in self.report["checks"]}
        self.assertEqual(set(checks), set(metrics))
        for name, value in metrics.items():
            with self.subTest(check=name):
                # additionalProperties: integer, minimum 0
                self.assertIsInstance(value, int)
                self.assertNotIsInstance(value, bool)
                self.assertGreaterEqual(value, 0)
                self.assertEqual(len(checks[name]["violations"]), value)
        # An uncoupled pair is a real violation, so the map is not all zeros
        # by construction - a metrics map that can only ever say 0 is the
        # vacuity this field is supposed to end.
        self.assertEqual(1, metrics["pair_coupling"])

    def test_by_check_carries_the_coverage_metrics_cannot(self):
        self.assertEqual(by_check_map(self.report["checks"]),
                         self.report["by_check"])
        self.assertEqual(2, self.report["by_check"]["pair_coupling"]["scanned"])

    def test_the_nested_required_blocks_are_complete(self):
        for field in ("params", "coverage", "summary"):
            with self.subTest(field=field):
                self.assertEqual([], missing_required(
                    self.report[field], COUPLING_SCHEMA["properties"][field]))
        item_schema = COUPLING_SCHEMA["properties"]["pairs"]["items"]
        self.assertEqual(2, len(self.report["pairs"]))
        for row in self.report["pairs"]:
            with self.subTest(pair=row["pair"]):
                self.assertEqual([], missing_required(row, item_schema))

    def test_the_declared_constants_match_the_schema(self):
        for field in ("schema_version", "tool", "model_class", "signoff"):
            with self.subTest(field=field):
                self.assertEqual(COUPLING_SCHEMA["properties"][field]["const"],
                                 self.report[field])
        self.assertIn(self.report["status"],
                      COUPLING_SCHEMA["properties"]["status"]["enum"])


class CliTests(unittest.TestCase):
    def test_measure_requires_its_positional(self):
        exit_code, _report, stderr = run(str(MEASURE))
        self.assertEqual(2, exit_code)
        self.assertIn("BOARD is required", stderr)

    def test_coupling_requires_its_positional(self):
        exit_code, _report, stderr = run(str(COUPLING))
        self.assertEqual(2, exit_code)
        self.assertIn("BOARD is required", stderr)

    def test_measure_self_test(self):
        exit_code, report, _stderr = run(str(MEASURE), "--self-test")
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", report["self_test"], report)

    def test_coupling_self_test(self):
        exit_code, report, _stderr = run(str(COUPLING), "--self-test")
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", report["self_test"], report)

    def test_an_out_of_range_threshold_exits_2(self):
        exit_code, _report, stderr = run(str(COUPLING), "b.kicad_pcb",
                                         "--coupled-threshold", "1.5")
        self.assertEqual(2, exit_code)
        self.assertIn("--coupled-threshold", stderr)

    def test_a_missing_board_exits_2(self):
        for tool in (MEASURE, COUPLING):
            with self.subTest(tool=tool.name):
                exit_code, report, _stderr = run(str(tool), "/nope.kicad_pcb")
                self.assertEqual(2, exit_code)
                self.assertEqual("ERROR", report["status"])


@unittest.skipUnless(HAVE_PCBNEW, "pcbnew not installed")
class LiveBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from clean_board_fixture import clean_routed_board
        cls.temp = tempfile.TemporaryDirectory(prefix="vendored-board-")
        cls.board = Path(cls.temp.name) / "b.kicad_pcb"
        clean_routed_board(cls.board)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_measure_emits_the_envelope_and_writes_the_report(self):
        """This run measures ONE net and leaves five other checks unmeasured
        (no --pair, no --decoupling, no --gnd-via-of, ...), so the report is
        NO_COVERAGE and the exit code follows it.

        It used to assert exit 0, which was true for the wrong reason: the
        rollup demoted to NO_COVERAGE only when EVERY check was unmeasured, so
        1 PASS beside 5 NO_COVERAGE reported PASS, and the exit code read the
        FAIL count alone. What this test is actually for - the envelope is
        emitted and the report is written - is unchanged.
        """
        out = Path(self.temp.name) / "measure.json"
        exit_code, report, _stderr = run(
            str(MEASURE), str(self.board), "--net", "NET1",
            "--json", str(out))
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(1, exit_code)
        self.assertTrue(out.is_file())
        self.assertEqual("measure-board-report/v1", report["schema_version"])
        self.assertEqual({"NET1"}, set(report["measurements"]["net_length_mm"]))
        self.assertGreater(report["measurements"]["net_length_mm"]["NET1"], 0)
        self.assertIn("coupling_note", report["measurements"])

    def test_measuring_an_absent_net_is_no_coverage_not_a_pass(self):
        _exit, report, _stderr = run(str(MEASURE), str(self.board),
                                     "--net", "/DOES_NOT_EXIST")
        routed = next(c for c in report["checks"] if c["check"] == "nets_routed")
        self.assertEqual("FAIL", routed["status"])

    def test_measuring_with_no_nets_at_all_is_no_coverage(self):
        _exit, report, _stderr = run(str(MEASURE), str(self.board))
        routed = next(c for c in report["checks"] if c["check"] == "nets_routed")
        self.assertEqual("NO_COVERAGE", routed["status"])

    def test_coupling_on_a_board_with_no_pairs_is_no_coverage(self):
        """The original printed 'COUPLED 0/0' and exited 0."""
        exit_code, report, _stderr = run(str(COUPLING), str(self.board))
        self.assertEqual(1, exit_code, "an unmeasured board must not exit 0")
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(0, report["coverage"]["scanned"])

    def test_a_real_coupling_run_is_bound_to_the_board_file(self):
        """End to end, through the CLI, on a board pcbnew actually loaded."""
        _exit, report, _stderr = run(str(COUPLING), str(self.board))
        self.assertEqual([], missing_required(report, COUPLING_SCHEMA), report)
        self.assertEqual(sha256_file(self.board), report["design"]["sha256"])
        self.assertEqual(self.board.resolve(),
                         Path(report["design"]["path"]).resolve())
        self.assertEqual({"pair_coupling", "pairs_discovered"},
                         set(report["metrics"]))

    def test_coupling_always_declares_it_is_not_a_signoff(self):
        _exit, report, _stderr = run(str(COUPLING), str(self.board))
        self.assertFalse(report["signoff"])
        self.assertEqual("sampled_centreline_proximity_screen",
                         report["model_class"])


if __name__ == "__main__":
    unittest.main()
