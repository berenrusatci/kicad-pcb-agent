#!/usr/bin/env python3
"""Tests for scripts/board/mechanical_dxf.py.

``ezdxf`` is not installed in this environment, so the NO_COVERAGE path is
exercised for real; the rest of the DXF-reading logic (INSUNITS parsing, the
inch->mil->mm scale-correction regression, LWPOLYLINE/LINE/ARC/CIRCLE scan)
is exercised against a fake module placed in ``sys.modules``, mirroring the
source project's own test strategy for the same code.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS  # noqa: E402,F401

import mechanical_dxf as mdxf  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake ezdxf
# --------------------------------------------------------------------------- #
class _FakeLWPolyline:
    def __init__(self, points):
        self._points = points

    def get_points(self):
        return self._points


class _FakeLine:
    def __init__(self, start, end):
        self.dxf = SimpleNamespace(
            start=SimpleNamespace(x=start[0], y=start[1]),
            end=SimpleNamespace(x=end[0], y=end[1]))


class _FakeArc:
    def __init__(self, center, radius, start_deg, end_deg):
        self.dxf = SimpleNamespace(center=SimpleNamespace(x=center[0], y=center[1]),
                                   radius=radius, start_angle=start_deg, end_angle=end_deg)


class _FakeCircle:
    def __init__(self, center, radius):
        self.dxf = SimpleNamespace(center=SimpleNamespace(x=center[0], y=center[1]), radius=radius)


class _FakeModelSpace:
    def __init__(self, entities):
        self._entities = entities  # list[(kind, layer, obj)]

    def query(self, query_str):
        import re
        m = re.match(r'^(\w+)\[layer=="([^"]*)"\]$', query_str)
        assert m, f"unexpected query shape: {query_str}"
        kind, layer = m.group(1), m.group(2)
        return [obj for (k, l, obj) in self._entities if k == kind and l == layer]


class _FakeDoc:
    def __init__(self, insunits, entities):
        self.header = {"$INSUNITS": insunits}
        self._msp = _FakeModelSpace(entities)

    def modelspace(self):
        return self._msp


class _FakeEzdxfModule:
    class DXFStructureError(Exception):
        pass

    def __init__(self, doc=None, open_error=None):
        self._doc = doc
        self._open_error = open_error

    def readfile(self, path):
        if self._open_error is not None:
            raise self._open_error
        return self._doc


def _square_polyline(side, layer="Board_Outline"):
    return _FakeLWPolyline([(0.0, 0.0), (side, 0.0), (side, side), (0.0, side), (0.0, 0.0)])


def _install_fake_ezdxf(doc=None, open_error=None):
    sys.modules["ezdxf"] = _FakeEzdxfModule(doc, open_error)


class _EzdxfIsolation(unittest.TestCase):
    def setUp(self):
        self._orig = sys.modules.get("ezdxf")

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("ezdxf", None)
        else:
            sys.modules["ezdxf"] = self._orig


# --------------------------------------------------------------------------- #
# ezdxf not installed -> real NO_COVERAGE in this environment
# --------------------------------------------------------------------------- #
class NoEzdxfTests(_EzdxfIsolation):
    def test_missing_ezdxf_is_no_coverage_not_a_crash(self):
        sys.modules.pop("ezdxf", None)
        points, holes, note = mdxf.read_dxf_outline("whatever.dxf")
        self.assertEqual([], points)
        self.assertEqual([], holes)
        self.assertEqual("EZDXF_NOT_INSTALLED", note["reason_code"])

    def test_build_report_no_coverage_when_ezdxf_missing(self):
        sys.modules.pop("ezdxf", None)
        report = mdxf.build_report(
            "x.dxf", "Board_Outline", None, None, None, 3.0, 0.3,
            "unknown", [], None, "unknown")
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(0, report["coverage"]["scanned"])


# --------------------------------------------------------------------------- #
# Fake-ezdxf-backed reading
# --------------------------------------------------------------------------- #
class ReadDxfOutlineTests(_EzdxfIsolation):
    def test_mm_file_reads_directly(self):
        _install_fake_ezdxf(_FakeDoc(4, [("LWPOLYLINE", "Board_Outline", _square_polyline(50.0))]))
        points, holes, note = mdxf.read_dxf_outline("card.dxf")
        self.assertIsNone(note)
        self.assertAlmostEqual(50.0, points[1][0])

    def test_inch_file_does_not_regress_to_1000x_scale_error(self):
        """The regression this file exists to prevent: a 1x1 inch square
        (INSUNITS=1) must become exactly 25.4x25.4mm, not 0.0254mm."""
        _install_fake_ezdxf(_FakeDoc(1, [("LWPOLYLINE", "Board_Outline", _square_polyline(1.0))]))
        points, _holes, note = mdxf.read_dxf_outline("inch_card.dxf")
        self.assertIsNone(note)
        self.assertAlmostEqual(25.4, points[1][0], places=6)

    def test_unrecognized_insunits_raises_confirm_not_silent_mm(self):
        _install_fake_ezdxf(_FakeDoc(0, [("LWPOLYLINE", "Board_Outline", _square_polyline(10.0))]))
        with self.assertRaises(mdxf.OutlineImportError) as ctx:
            mdxf.read_dxf_outline("ambiguous.dxf")
        self.assertEqual("UNKNOWN_INSUNITS", ctx.exception.reason_code)

    def test_line_and_arc_are_scanned_too(self):
        _install_fake_ezdxf(_FakeDoc(4, [
            ("LINE", "Board_Outline", _FakeLine((0.0, 0.0), (10.0, 0.0))),
            ("ARC", "Board_Outline", _FakeArc((10.0, 5.0), 5.0, 270.0, 90.0)),
        ]))
        points, _holes, note = mdxf.read_dxf_outline("mixed.dxf")
        self.assertIsNone(note)
        self.assertGreater(len(points), 2)

    def test_other_layer_geometry_is_ignored(self):
        _install_fake_ezdxf(_FakeDoc(4, [("LWPOLYLINE", "Other_Layer", _square_polyline(10.0))]))
        points, holes, note = mdxf.read_dxf_outline("card.dxf", outline_layer="Board_Outline")
        self.assertEqual("OUTLINE_LAYER_EMPTY", note["reason_code"])

    def test_holes_read_from_paired_layer(self):
        _install_fake_ezdxf(_FakeDoc(4, [
            ("LWPOLYLINE", "Board_Outline", _square_polyline(20.0)),
            ("CIRCLE", "Board_Outline_Deliği", _FakeCircle((2.0, 2.0), 1.0)),
        ]))
        _points, holes, note = mdxf.read_dxf_outline("card.dxf")
        self.assertIsNone(note)
        self.assertEqual([(2.0, 2.0, 2.0)], holes)


class AnchorCheckTests(unittest.TestCase):
    def test_matching_anchor_does_not_raise(self):
        mdxf.verify_anchor([(5.0, 5.0, 2.0)], (5.1, 5.0))

    def test_far_anchor_raises_confirm(self):
        with self.assertRaises(mdxf.OutlineImportError) as ctx:
            mdxf.verify_anchor([(5.0, 5.0, 2.0)], (500.0, 500.0))
        self.assertEqual("ANCHOR_CHECK_FAILED", ctx.exception.reason_code)

    def test_no_known_coordinate_skips_check(self):
        mdxf.verify_anchor([(5.0, 5.0, 2.0)], None)  # must not raise


# --------------------------------------------------------------------------- #
# Zone derivation + height check (pure geometry, no ezdxf/pcbnew needed)
# --------------------------------------------------------------------------- #
class ZoneDerivationTests(unittest.TestCase):
    def test_ceiling_map_wins_over_global_height(self):
        ceiling = [{"polygon_mm": [[0, 0], [50, 0], [50, 30], [0, 30]], "ceiling_mm": 4.0}]
        zones = mdxf.derive_mounting_hole_zones(
            [(5.0, 5.0, 2.0)], ring_mm=3.0, global_max_height_mm=99.0,
            ceiling_map=ceiling, clearance_mm=0.3)
        self.assertEqual("step", zones[0]["source"])
        self.assertAlmostEqual(3.7, zones[0]["max_allowed_height_mm"])

    def test_global_height_used_as_local_policy_fallback(self):
        zones = mdxf.derive_mounting_hole_zones(
            [(5.0, 5.0, 2.0)], global_max_height_mm=5.0, clearance_mm=0.5)
        self.assertEqual("local_policy", zones[0]["source"])
        self.assertAlmostEqual(4.5, zones[0]["max_allowed_height_mm"])

    def test_unknown_height_is_null_not_assumed_unlimited(self):
        zones = mdxf.derive_mounting_hole_zones([(5.0, 5.0, 2.0)])
        self.assertIsNone(zones[0]["max_allowed_height_mm"])

    def test_height_violation_is_caught_by_fault_injection(self):
        zones = mdxf.derive_mounting_hole_zones(
            [(5.0, 5.0, 2.0)], global_max_height_mm=4.0, clearance_mm=0.3)
        clean = mdxf.check_component_heights(
            [{"refdes": "U1", "x_mm": 5, "y_mm": 5, "body_height_mm": 1.0}], zones)
        self.assertEqual([], clean, "clean scenario should pass before fault injection")
        broken = mdxf.check_component_heights(
            [{"refdes": "U1", "x_mm": 5, "y_mm": 5, "body_height_mm": 99.0}], zones)
        self.assertEqual(1, len(broken))
        self.assertEqual("U1", broken[0]["refdes"])


class IpdToleranceTests(unittest.TestCase):
    def test_worst_case_is_direct_sum(self):
        result = mdxf.ipd_tolerance_chain(60.0, [0.1, 0.2], rss=False)
        self.assertAlmostEqual(0.3, result["total_tolerance_mm"])
        self.assertEqual("worst_case", result["method"])

    def test_rss_is_smaller_than_worst_case(self):
        rss = mdxf.ipd_tolerance_chain(60.0, [0.1, 0.2], rss=True)
        worst = mdxf.ipd_tolerance_chain(60.0, [0.1, 0.2], rss=False)
        self.assertLess(rss["total_tolerance_mm"], worst["total_tolerance_mm"])

    def test_optical_center_offset_applies(self):
        self.assertEqual((10.5, 9.7), mdxf.apply_optical_center_offset((10.0, 10.0), (0.5, -0.3)))


# --------------------------------------------------------------------------- #
# build_report status derivation end-to-end (fake ezdxf)
# --------------------------------------------------------------------------- #
class BuildReportStatusTests(_EzdxfIsolation):
    def test_open_polygon_is_confirm(self):
        open_poly = _FakeLWPolyline([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
        _install_fake_ezdxf(_FakeDoc(4, [("LWPOLYLINE", "Board_Outline", open_poly)]))
        report = mdxf.build_report("x.dxf", "Board_Outline", None, None, None,
                                   3.0, 0.3, "unknown", [], None, "unknown")
        self.assertEqual("CONFIRM", report["status"])

    def test_closed_polygon_with_global_height_is_screen_only(self):
        _install_fake_ezdxf(_FakeDoc(4, [
            ("LWPOLYLINE", "Board_Outline", _square_polyline(50.0)),
            ("CIRCLE", "Board_Outline_Deliği", _FakeCircle((5.0, 5.0), 2.0)),
        ]))
        report = mdxf.build_report("x.dxf", "Board_Outline", None, 5.0, None,
                                   3.0, 0.3, "unknown", [], None, "unknown")
        self.assertEqual("SCREEN_ONLY", report["status"])
        self.assertEqual(1, len(report["zones"]))

    def test_closed_polygon_with_ceiling_map_is_green(self):
        _install_fake_ezdxf(_FakeDoc(4, [
            ("LWPOLYLINE", "Board_Outline", _square_polyline(50.0)),
            ("CIRCLE", "Board_Outline_Deliği", _FakeCircle((5.0, 5.0), 2.0)),
        ]))
        ceiling = [{"polygon_mm": [[0, 0], [50, 0], [50, 50], [0, 50]], "ceiling_mm": 4.0}]
        report = mdxf.build_report("x.dxf", "Board_Outline", None, None, ceiling,
                                   3.0, 0.3, "unknown", [], None, "unknown")
        self.assertEqual("GREEN", report["status"])

    def test_no_holes_is_no_coverage(self):
        _install_fake_ezdxf(_FakeDoc(4, [("LWPOLYLINE", "Board_Outline", _square_polyline(50.0))]))
        report = mdxf.build_report("x.dxf", "Board_Outline", None, 5.0, None,
                                   3.0, 0.3, "unknown", [], None, "unknown")
        self.assertEqual("NO_COVERAGE", report["status"])

    def test_anchor_mismatch_is_confirm(self):
        _install_fake_ezdxf(_FakeDoc(4, [
            ("LWPOLYLINE", "Board_Outline", _square_polyline(50.0)),
            ("CIRCLE", "Board_Outline_Deliği", _FakeCircle((5.0, 5.0), 2.0)),
        ]))
        report = mdxf.build_report("x.dxf", "Board_Outline", (500.0, 500.0), None, None,
                                   3.0, 0.3, "unknown", [], None, "unknown")
        self.assertEqual("CONFIRM", report["status"])

    def test_exit_code_matches_status(self):
        self.assertEqual(0, mdxf.exit_code_for({"status": "GREEN"}))
        for status in ("CONFIRM", "SCREEN_ONLY", "NO_COVERAGE", "RED"):
            self.assertEqual(1, mdxf.exit_code_for({"status": status}))


# --------------------------------------------------------------------------- #
# write_edge_cuts - fake pcbnew, same write pattern as the source project
# --------------------------------------------------------------------------- #
class _FakeVector2I:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _FakePcbShape:
    def __init__(self, board):
        self.board = board
        self.shape = self.start = self.end = self.layer = self.width = None

    def SetShape(self, s):
        self.shape = s

    def SetStart(self, v):
        self.start = v

    def SetEnd(self, v):
        self.end = v

    def SetLayer(self, l):
        self.layer = l

    def SetWidth(self, w):
        self.width = w


class _FakeBoard:
    def __init__(self):
        self.added = []
        self.saved_to = None

    def Add(self, obj):
        self.added.append(obj)

    def Save(self, path):
        self.saved_to = path


class _FakePcbnewModule:
    Edge_Cuts = "Edge.Cuts"
    SHAPE_T_SEGMENT = "SEGMENT"

    def __init__(self, board):
        self._board = board

    def LoadBoard(self, path):
        return self._board

    def PCB_SHAPE(self, board):
        return _FakePcbShape(board)

    def VECTOR2I(self, x, y):
        return _FakeVector2I(x, y)

    def FromMM(self, value):
        return int(round(value * 1_000_000))


class WriteEdgeCutsTests(unittest.TestCase):
    def setUp(self):
        self._orig = sys.modules.get("pcbnew")

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("pcbnew", None)
        else:
            sys.modules["pcbnew"] = self._orig

    def test_not_closed_is_rejected_without_touching_pcbnew(self):
        sys.modules.pop("pcbnew", None)
        result = mdxf.write_edge_cuts("board.kicad_pcb", [(0, 0), (10, 0), (10, 10)])
        self.assertFalse(result["written"])
        self.assertEqual("OUTLINE_NOT_CLOSED", result["reason_code"])

    def test_missing_pcbnew_is_reported_not_raised(self):
        sys.modules.pop("pcbnew", None)
        closed = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        result = mdxf.write_edge_cuts("board.kicad_pcb", closed)
        self.assertFalse(result["written"])
        self.assertEqual("PCBNEW_NOT_AVAILABLE", result["reason_code"])

    def test_closed_polygon_writes_four_segments(self):
        board = _FakeBoard()
        sys.modules["pcbnew"] = _FakePcbnewModule(board)
        closed = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        result = mdxf.write_edge_cuts("board.kicad_pcb", closed)
        self.assertTrue(result["written"])
        self.assertEqual(4, len(board.added))
        self.assertEqual("board.kicad_pcb", board.saved_to)
        for shape in board.added:
            self.assertEqual("Edge.Cuts", shape.layer)


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mdxf._self_test())


if __name__ == "__main__":
    unittest.main()
