#!/usr/bin/env python3
"""Tests for common/collision.py, scripts/board/collision_radar.py and
scripts/board/mcad_collision.py.

Converted from two archived pytest suites to unittest.

The geometry tests use plain dataclasses and DO NOT need pcbnew. The loader
tests use duck-typed fake board objects, which is precisely why the loaders
were split away from the maths - only the CLI end-to-end test needs real
KiCad, and it skips without it.

``REAL_KICAD10_FOOTPRINT`` is a verbatim ``(footprint ...)`` block from a real
``.kicad_pcb``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS, REPO, SCHEMAS, TOOLS  # noqa: E402

from common.collision import (  # noqa: E402
    BBox,
    Body3D,
    ObstructionVolume,
    Placement,
    TrackObstacle,
    boxes_collide,
    boxes_collide_3d,
    colliding_pairs,
    collision_finding,
    component_box_3d,
    mcad_collision_finding,
    nm_to_mm,
    outside_board,
    point_to_segment_mm,
    rotated_aabb,
)

import collision_radar  # noqa: E402
import mcad_collision  # noqa: E402

try:
    import pcbnew  # noqa: F401
    HAVE_PCBNEW = True
except Exception:                                    # pragma: no cover
    HAVE_PCBNEW = False


# --------------------------------------------------------------------------- #
# Fake board objects - duck typing, no pcbnew
# --------------------------------------------------------------------------- #
class FakeBBox:
    def __init__(self, left, top, right, bottom):
        self._v = (left, top, right, bottom)

    def GetLeft(self):
        return self._v[0]

    def GetTop(self):
        return self._v[1]

    def GetRight(self):
        return self._v[2]

    def GetBottom(self):
        return self._v[3]


class FakeFootprint:
    def __init__(self, ref, left_mm, top_mm, right_mm, bottom_mm):
        self.ref = ref
        self._box = FakeBBox(*[int(v * 1_000_000)
                               for v in (left_mm, top_mm, right_mm, bottom_mm)])
        self.bbox_calls = []

    def GetReference(self):
        return self.ref

    def GetBoundingBox(self, aggregate, include_text):
        self.bbox_calls.append((aggregate, include_text))
        return self._box


class FakeDrawing:
    def __init__(self, layer, left_mm, top_mm, right_mm, bottom_mm):
        self.layer = layer
        self._box = FakeBBox(*[int(v * 1_000_000)
                               for v in (left_mm, top_mm, right_mm, bottom_mm)])

    def GetLayer(self):
        return self.layer

    def GetBoundingBox(self):
        return self._box


class FakePoint:
    def __init__(self, x_mm, y_mm):
        self.x = int(x_mm * 1_000_000)
        self.y = int(y_mm * 1_000_000)


class FakeTrack:
    """Duck type for a track or via. Records how GetWidth was called."""

    def __init__(self, net, kind, start_mm, end_mm, width_mm, top_layer=0):
        self.net = net
        self.kind = kind
        self._start = FakePoint(*start_mm)
        self._end = FakePoint(*end_mm)
        self._width = int(width_mm * 1_000_000)
        self._top = top_layer
        self.width_calls = []

    def GetNetname(self):
        return self.net

    def Type(self):
        return self.kind

    def GetPosition(self):
        return self._start

    def GetStart(self):
        return self._start

    def GetEnd(self):
        return self._end

    def TopLayer(self):
        return self._top

    def GetWidth(self, layer=None):
        self.width_calls.append(layer)
        return self._width


class FakeBoard:
    def __init__(self, footprints=(), drawings=(), tracks=()):
        self._f, self._d, self._t = list(footprints), list(drawings), list(tracks)

    def GetFootprints(self):
        return self._f

    def GetDrawings(self):
        return self._d

    def GetTracks(self):
        return self._t


# --------------------------------------------------------------------------- #
class BoxGeometry(unittest.TestCase):
    def test_separated_boxes_do_not_collide(self):
        self.assertIsNone(boxes_collide(BBox("U1", 0, 0, 2, 2),
                                        BBox("C3", 5, 5, 6, 6)))

    def test_edge_to_edge_contact_is_not_a_collision(self):
        """Two parts exactly touching is legal placement, not an error."""
        self.assertIsNone(boxes_collide(BBox("U2", 0, 0, 2, 2),
                                        BBox("U3", 2, 0, 4, 2)))

    def test_overlap_amounts_are_exact(self):
        overlap = boxes_collide(BBox("U1", 0, 0, 2, 2),
                                BBox("C3", 1.2, 0.5, 3.2, 2.5))
        self.assertIsNotNone(overlap)
        self.assertAlmostEqual(overlap[0], 0.8, places=6)
        self.assertAlmostEqual(overlap[1], 1.5, places=6)

    def test_overlap_in_one_axis_only_is_not_a_collision(self):
        self.assertIsNone(boxes_collide(BBox("A", 0, 0, 2, 2),
                                        BBox("B", 1, 5, 3, 7)))

    def test_escape_vector_clears_the_overlap_with_margin(self):
        """part_b is the one treated as movable; the suggestion must exceed
        the overlap (so the parts actually separate) and point away from
        part_a's centre."""
        violations = colliding_pairs({
            "U1": BBox("U1", 0, 0, 2, 2),
            "C3": BBox("C3", 1.2, 0.5, 3.2, 2.5)})
        self.assertEqual(len(violations), 1)
        row = violations[0]
        self.assertEqual(row["kind"], "COLLISION")
        self.assertEqual((row["part_a"], row["part_b"]), ("C3", "U1"))
        self.assertGreater(abs(row["suggested_escape_x_mm"]), row["overlap_x_mm"])
        self.assertGreater(abs(row["suggested_escape_y_mm"]), row["overlap_y_mm"])
        # U1's centre is left of and above C3's, so it escapes down-left.
        self.assertLess(row["suggested_escape_x_mm"], 0)
        self.assertLess(row["suggested_escape_y_mm"], 0)

    def test_pairs_are_reported_in_a_stable_order(self):
        boxes = {"Z9": BBox("Z9", 0, 0, 2, 2), "A1": BBox("A1", 1, 1, 3, 3)}
        self.assertEqual(colliding_pairs(boxes)[0]["part_a"], "A1")


class OutlineOverhang(unittest.TestCase):
    BOARD = BBox("EDGE_CUTS", 0, 0, 10, 10)

    def test_overhang_amount_and_direction(self):
        over = outside_board({"J1": BBox("J1", -1.0, 2, 1.0, 4)}, self.BOARD)
        self.assertEqual(len(over), 1)
        self.assertAlmostEqual(over[0]["overlap_x_mm"], 1.0, places=6)
        self.assertLess(over[0]["suggested_escape_x_mm"], 0)
        self.assertEqual(over[0]["part_b"], None)

    def test_a_part_inside_the_board_is_clean(self):
        self.assertEqual(outside_board({"R1": BBox("R1", 1, 1, 2, 2)},
                                       self.BOARD), [])

    def test_a_part_flush_with_the_edge_is_not_an_overhang(self):
        self.assertEqual(outside_board({"R1": BBox("R1", 0, 0, 2, 2)},
                                       self.BOARD), [])


class CollisionFinding(unittest.TestCase):
    def test_empty_board_is_no_coverage_never_pass(self):
        finding = collision_finding({})
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertEqual(finding["scanned"], 0)

    def test_one_part_alone_has_no_pairs_but_is_outline_checked(self):
        boxes = {"U1": BBox("U1", 1, 1, 2, 2)}
        self.assertEqual(collision_finding(boxes)["status"], "NO_COVERAGE")
        with_board = collision_finding(boxes, BBox("EDGE_CUTS", 0, 0, 10, 10))
        self.assertEqual(with_board["status"], "PASS")
        self.assertEqual(with_board["scanned"], 1)

    def test_scanned_counts_pairs_plus_outline_checks(self):
        boxes = {f"U{i}": BBox(f"U{i}", i * 5, 0, i * 5 + 1, 1) for i in range(4)}
        self.assertEqual(collision_finding(boxes)["scanned"], 6)
        self.assertEqual(
            collision_finding(boxes, BBox("E", -1, -1, 99, 99))["scanned"], 10)

    def test_fault_injection_stacked_parts_must_fail(self):
        boxes = {"U1": BBox("U1", 0, 0, 2, 2), "C3": BBox("C3", 0, 0, 2, 2)}
        self.assertEqual(collision_finding(boxes)["status"], "FAIL")


class TrackObstacles(unittest.TestCase):
    def test_point_to_segment_clamps_to_the_nearest_end(self):
        self.assertAlmostEqual(point_to_segment_mm(-5, 0, 0, 0, 10, 0), 5.0,
                               places=9)
        self.assertAlmostEqual(point_to_segment_mm(5, 3, 0, 0, 10, 0), 3.0,
                               places=9)

    def test_a_zero_length_segment_is_a_point_obstacle(self):
        self.assertAlmostEqual(point_to_segment_mm(3, 4, 0, 0, 0, 0), 5.0,
                               places=9)

    def test_the_aabb_of_a_diagonal_track_is_much_larger_than_the_track(self):
        """The narrow phase exists for exactly this case."""
        track = TrackObstacle("t", 48, 16.46, 31.86, 27.275, 0.2)
        corner = (track.x_min + 0.2, track.y_min + 0.2)
        self.assertTrue(track.x_min <= corner[0] <= track.x_max)
        self.assertTrue(track.y_min <= corner[1] <= track.y_max)
        # Inside the AABB, but the real copper is far away: NOT blocked.
        self.assertFalse(track.blocks_point(*corner))

    def test_a_point_on_the_track_is_blocked(self):
        track = TrackObstacle("t", 0, 0, 10, 0, 0.4)
        self.assertTrue(track.blocks_point(5, 0.1))
        self.assertFalse(track.blocks_point(5, 0.5))

    def test_clearance_widens_the_narrow_phase(self):
        track = TrackObstacle("t", 0, 0, 10, 0, 0.4)
        self.assertTrue(track.blocks_point(5, 0.35, clearance_mm=0.2))

    def test_a_point_outside_the_aabb_short_circuits(self):
        track = TrackObstacle("t", 0, 0, 10, 0, 0.2)
        self.assertFalse(track.blocks_point(50, 50))


class LoadersWithFakeBoards(unittest.TestCase):
    def test_footprint_boxes_convert_nm_to_mm(self):
        board = FakeBoard(footprints=[FakeFootprint("U1", 0, 0, 2, 2)])
        boxes = collision_radar.footprint_boxes(board)
        self.assertAlmostEqual(boxes["U1"].x_max, 2.0, places=6)
        self.assertAlmostEqual(boxes["U1"].width, 2.0, places=6)

    def test_bounding_box_excludes_silkscreen_text(self):
        """GetBoundingBox(False, False) - both flags off, or 'REF**' inflates
        the box and manufactures phantom collisions."""
        footprint = FakeFootprint("U1", 0, 0, 2, 2)
        collision_radar.footprint_boxes(FakeBoard(footprints=[footprint]))
        self.assertEqual(footprint.bbox_calls, [(False, False)])

    def test_outline_box_only_uses_edge_cuts(self):
        board = FakeBoard(drawings=[
            FakeDrawing(44, 0, 0, 10, 10),      # Edge.Cuts
            FakeDrawing(0, -50, -50, 50, 50),   # F.Cu, must be ignored
        ])
        box = collision_radar.board_outline_box(board, edge_layer=44)
        self.assertEqual((box.x_min, box.y_min, box.x_max, box.y_max),
                         (0.0, 0.0, 10.0, 10.0))

    def test_no_edge_cuts_returns_none_not_an_empty_box(self):
        board = FakeBoard(drawings=[FakeDrawing(0, 0, 0, 10, 10)])
        self.assertIsNone(collision_radar.board_outline_box(board, edge_layer=44))

    def test_via_width_is_requested_WITH_a_layer_argument(self):
        """KiCad 10 trap: the no-arg GetWidth() overload trips a debug assert
        that opens a modal dialog and hangs a headless run."""
        via = FakeTrack("GND", "VIA", (5, 5), (5, 5), 0.6, top_layer=31)
        collision_radar.track_obstacles(FakeBoard(tracks=[via]), via_type="VIA")
        self.assertEqual(via.width_calls, [31])
        self.assertNotIn(None, via.width_calls)

    def test_a_via_becomes_a_zero_length_point_obstacle(self):
        via = FakeTrack("GND", "VIA", (5, 5), (5, 5), 0.6)
        obstacles = collision_radar.track_obstacles(FakeBoard(tracks=[via]),
                                                    via_type="VIA")
        self.assertEqual(obstacles[0].x1, obstacles[0].x2)
        self.assertEqual(obstacles[0].ref, "via_GND")

    def test_excluded_net_is_dropped(self):
        tracks = [FakeTrack("SDA", "TRK", (0, 0), (10, 0), 0.2),
                  FakeTrack("SCL", "TRK", (0, 1), (10, 1), 0.2)]
        obstacles = collision_radar.track_obstacles(
            FakeBoard(tracks=tracks), exclude_net="SDA", via_type="VIA")
        self.assertEqual([o.ref for o in obstacles], ["trk_SCL"])

    def test_units_helper(self):
        self.assertAlmostEqual(nm_to_mm(1_500_000), 1.5, places=9)


# --------------------------------------------------------------------------- #
# MCAD
# --------------------------------------------------------------------------- #
REAL_KICAD10_FOOTPRINT = """\
\t(footprint "Capacitor_SMD:C_0402_1005Metric"
\t\t(layer "F.Cu")
\t\t(uuid "0769dfc2-0e39-4e85-a221-6ac6c5a69373")
\t\t(at -13 14.5)
\t\t(descr "Capacitor SMD 0402")
\t\t(property "Reference" "C16"
\t\t\t(at -0.28 -1.265 0)
\t\t\t(layer "F.SilkS")
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 0.8 0.8)
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(property "Value" "4.7uF"
\t\t\t(at 0 1.16 0)
\t\t\t(layer "F.Fab")
\t\t)
\t)
"""

ROTATED_FOOTPRINT = """\
\t(footprint "Connector_JST:JST_PH"
\t\t(layer "B.Cu")
\t\t(uuid "abc123")
\t\t(at 22.5 -8.0 90)
\t\t(property "Reference" "J1"
\t\t\t(at 0 -2 90)
\t\t\t(layer "B.SilkS")
\t\t)
\t)
"""

REAL_MISSING_MODEL_STDERR = """\
Could not add 3D model for SW1.
File not found: ${KICAD10_3DMODEL_DIR}/Button_Switch_SMD.3dshapes/SW_SPDT.step
"""


class PlacementParsing(unittest.TestCase):
    def test_one_balanced_block_is_extracted(self):
        blocks = mcad_collision.footprint_blocks(REAL_KICAD10_FOOTPRINT)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].count("("), blocks[0].count(")"))

    def test_real_footprint_fields_are_read(self):
        placement = mcad_collision.parse_placements(REAL_KICAD10_FOOTPRINT)[0]
        self.assertEqual(placement.ref, "C16")
        self.assertEqual(placement.footprint,
                         "Capacitor_SMD:C_0402_1005Metric")
        self.assertEqual((placement.x_mm, placement.y_mm), (-13.0, 14.5))
        self.assertEqual(placement.angle_deg, 0.0)
        self.assertEqual(placement.layer, "F.Cu")

    def test_rotation_and_bottom_layer_are_read(self):
        placement = mcad_collision.parse_placements(ROTATED_FOOTPRINT)[0]
        self.assertEqual(placement.angle_deg, 90.0)
        self.assertEqual(placement.layer, "B.Cu")

    def test_a_footprint_with_no_reference_is_skipped(self):
        """A partial placement record is worse than none."""
        broken = '\t(footprint "X:Y"\n\t\t(layer "F.Cu")\n\t\t(at 1 1)\n\t)\n'
        self.assertEqual(mcad_collision.parse_placements(broken), [])

    def test_multiple_footprints_are_separated(self):
        both = REAL_KICAD10_FOOTPRINT + ROTATED_FOOTPRINT
        refs = {p.ref for p in mcad_collision.parse_placements(both)}
        self.assertEqual(refs, {"C16", "J1"})


class Geometry3D(unittest.TestCase):
    def test_unrotated_aabb(self):
        self.assertEqual(rotated_aabb(0, 0, 4, 2, 0.0), (-2.0, -1.0, 2.0, 1.0))

    def test_90_degrees_swaps_the_axes(self):
        box = rotated_aabb(0, 0, 4, 2, 90.0)
        self.assertAlmostEqual(box[0], -1.0, places=6)
        self.assertAlmostEqual(box[2], 1.0, places=6)

    def test_45_degrees_is_conservative_never_narrower(self):
        box = rotated_aabb(0, 0, 2, 2, 45.0)
        self.assertGreater(box[2] - box[0], 2.0)

    def test_offset_centre_is_respected(self):
        box = rotated_aabb(10, 20, 2, 2, 0.0)
        self.assertEqual(box, (9.0, 19.0, 11.0, 21.0))

    def test_z_separated_boxes_do_not_collide(self):
        self.assertIsNone(boxes_collide_3d((0, 0, 0, 5, 5, 2),
                                           (1, 1, 10, 4, 4, 15)))

    def test_all_three_axes_must_overlap(self):
        overlap = boxes_collide_3d((0, 0, 0, 5, 5, 5), (1, 1, 1, 4, 4, 4))
        self.assertEqual(overlap, (3.0, 3.0, 3.0))

    def test_bottom_side_parts_sit_below_the_board(self):
        box = component_box_3d(Placement("C1", "x", 0, 0, 0, "B.Cu"),
                               Body3D(1, 1, 1), board_thickness_mm=1.6)
        self.assertLessEqual(box[5], -1.6)
        self.assertAlmostEqual(box[2], -2.6, places=6)

    def test_top_side_parts_start_at_zero(self):
        box = component_box_3d(Placement("C1", "x", 0, 0, 0, "F.Cu"),
                               Body3D(1, 1, 3))
        self.assertEqual((box[2], box[5]), (0.0, 3.0))


class McadFinding(unittest.TestCase):
    LID = ObstructionVolume("lid_inner_face", -10, -10, 10, 10, 5.0, 20.0)

    def test_a_component_without_body_data_is_not_scanned(self):
        finding = mcad_collision_finding(
            [Placement("U1", "x", 0, 0, 0, "F.Cu")], {}, [self.LID])
        self.assertEqual(finding["scanned"], 0)
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_fault_injection_an_obstruction_on_top_must_fail(self):
        finding = mcad_collision_finding(
            [Placement("J1", "Conn:Header", 0.0, 0.0, 0.0, "F.Cu")],
            {"J1": Body3D(5.0, 5.0, 8.0)}, [self.LID])
        self.assertEqual(finding["status"], "FAIL")
        self.assertIn("lid_inner_face", finding["violations"][0]["message"])

    def test_a_short_part_under_the_lid_passes(self):
        finding = mcad_collision_finding(
            [Placement("C1", "x", 0.0, 0.0, 0.0, "F.Cu")],
            {"C1": Body3D(1.0, 1.0, 1.0)}, [self.LID])
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 1)

    def test_a_bottom_side_part_does_not_hit_the_top_lid(self):
        finding = mcad_collision_finding(
            [Placement("J1", "x", 0.0, 0.0, 0.0, "B.Cu")],
            {"J1": Body3D(5.0, 5.0, 8.0)}, [self.LID])
        self.assertEqual(finding["status"], "PASS")

    def test_body_coverage_names_the_untested_parts(self):
        placements = [Placement("U1", "a", 0, 0, 0, "F.Cu"),
                      Placement("C1", "b", 0, 0, 0, "F.Cu")]
        finding = mcad_collision.check_body_coverage(
            placements, {"C1": Body3D(1, 1, 1)}, [])
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["ref"], "U1")

    def test_a_substituted_3d_model_counts_as_untested(self):
        finding = mcad_collision.check_body_coverage(
            [Placement("U1", "a", 0, 0, 0, "F.Cu")],
            {"U1": Body3D(1, 1, 1)}, ["SW1"])
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["ref"], "SW1")


class MissingModelParsing(unittest.TestCase):
    def test_refdes_is_extracted_from_the_real_stderr_shape(self):
        from common.kicad_cli import missing_3d_models
        self.assertEqual(missing_3d_models(REAL_MISSING_MODEL_STDERR), ["SW1"])

    def test_clean_stderr_yields_nothing(self):
        from common.kicad_cli import missing_3d_models
        self.assertEqual(missing_3d_models("Export done.\n"), [])


class CliContracts(unittest.TestCase):
    def run_script(self, script, *args):
        return subprocess.run([sys.executable, str(script), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def test_collision_radar_missing_input_exits_2(self):
        proc = self.run_script(BOARD_SCRIPTS / "collision_radar.py")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")

    def test_collision_radar_self_test_is_opt_in_and_quiet_on_stdout(self):
        proc = self.run_script(BOARD_SCRIPTS / "collision_radar.py", "--self-test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_mcad_missing_enclosure_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text("(kicad_pcb)\n", encoding="utf-8")
            proc = self.run_script(BOARD_SCRIPTS / "mcad_collision.py", str(board))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--enclosure", proc.stderr)

    def test_mcad_end_to_end_reports_the_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text("(kicad_pcb\n" + REAL_KICAD10_FOOTPRINT + ")\n",
                             encoding="utf-8")
            enclosure = Path(tmp) / "e.json"
            enclosure.write_text(json.dumps({
                "bodies": {"C16": {"width_mm": 1.0, "depth_mm": 0.5,
                                   "height_mm": 0.6}},
                "obstructions": [{"name": "lid", "x_min_mm": -20,
                                  "y_min_mm": 10, "x_max_mm": 0,
                                  "y_max_mm": 20, "z_min_mm": 0.3,
                                  "z_max_mm": 9}]}), encoding="utf-8")
            proc = self.run_script(BOARD_SCRIPTS / "mcad_collision.py",
                                   str(board), "--enclosure", str(enclosure))
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertEqual(report["schema_version"], "mcad-collision-report/v1")
        self.assertFalse(report["params"]["step_brep_parsed"])

    @unittest.skipUnless(HAVE_PCBNEW, "pcbnew (KiCad SWIG) is not importable")
    def test_collision_radar_runs_against_a_real_pcbnew_board(self):
        import board_fixture
        with tempfile.TemporaryDirectory() as tmp:
            board = board_fixture.clean_board(Path(tmp) / "b.kicad_pcb")
            proc = self.run_script(BOARD_SCRIPTS / "collision_radar.py",
                                   str(board))
        report = json.loads(proc.stdout)
        self.assertEqual(report["schema_version"], "collision-radar-report/v1")
        self.assertTrue(report["params"]["outline_checked"])
        self.assertGreater(report["checks"][0]["scanned"], 0)
        self.assertEqual(proc.returncode, 0)

    @unittest.skipUnless(HAVE_PCBNEW, "pcbnew (KiCad SWIG) is not importable")
    def test_real_via_widths_are_read_without_the_no_arg_overload(self):
        """Exercises the KiCad 10 modal-dialog trap on a REAL board: a hang
        here would mean the layer argument had been dropped."""
        import board_fixture
        import pcbnew as real_pcbnew
        with tempfile.TemporaryDirectory() as tmp:
            path = board_fixture.clean_board(Path(tmp) / "b.kicad_pcb")
            board = real_pcbnew.LoadBoard(str(path))
            obstacles = collision_radar.track_obstacles(board)
        self.assertTrue(all(o.width_mm > 0 for o in obstacles))


class SchemaAgreement(unittest.TestCase):
    def test_collision_radar_report_matches_its_schema(self):
        from common.findings import board_report
        schema = json.loads((SCHEMAS / "collision_radar.schema.json")
                            .read_text(encoding="utf-8"))
        report = board_report("collision_radar", "b.kicad_pcb",
                              [collision_finding({"U1": BBox("U1", 0, 0, 1, 1),
                                                  "U2": BBox("U2", 5, 5, 6, 6)})],
                              params={}, schema_version=schema["$id"].rsplit(
                                  "/schemas/", 1)[1])
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertIn(report["tool"], schema["properties"]["tool"]["enum"])


if __name__ == "__main__":
    unittest.main()
