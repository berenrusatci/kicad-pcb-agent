#!/usr/bin/env python3
"""Tests for the hoisted shared helpers in common/.

These functions previously existed as 2-4 divergent copies each. The point of
these tests is not just that they work, but that the SPECIFIC divergences
found during consolidation stay fixed.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, TOOLS  # noqa: E402

from common import findings, hashing, paths, sexpr, si_models, units  # noqa: E402
from common.paths import PathConfinementError  # noqa: E402
from common.report_io import ReportWriteError, dumps, write_report  # noqa: E402


class Hashing(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.bin"
            target.write_bytes(b"merhaba d\xc3\xbcnya" * 1000)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                             hashing.sha256_file(target))

    def test_valid_digest(self):
        self.assertTrue(hashing.valid_digest("a" * 64))
        self.assertTrue(hashing.valid_digest("A" * 64))
        for bad in ("a" * 63, "a" * 65, "g" * 64, None, 42, ""):
            self.assertFalse(hashing.valid_digest(bad), bad)

    def test_canonical_digest_is_key_order_independent(self):
        self.assertEqual(hashing.canonical_digest({"a": 1, "b": 2}),
                         hashing.canonical_digest({"b": 2, "a": 1}))

    def test_canonical_digest_rejects_nan(self):
        with self.assertRaises(ValueError):
            hashing.canonical_digest({"x": float("nan")})


class Units(unittest.TestCase):
    def test_mm_round_trips_internal_units(self):
        self.assertAlmostEqual(12.34, units.mm(units.to_iu(12.34)), places=9)

    def test_canonical_xy_is_unrounded(self):
        """The via_stub fork: rounding must now be an explicit call."""
        class P:
            x = 1234567
            y = 7654321

        raw = units.xy(P())
        self.assertNotEqual(raw, units.round_xy(raw, 4))
        self.assertEqual(units.round_xy(raw, 4), units.xy_rounded(P(), 4))


class Geometry(unittest.TestCase):
    def test_dist_point_to_points_is_inf_when_empty(self):
        """Both old copies had to agree on this; only one actually did."""
        from common.boardgeom import dist_point_to_points
        self.assertEqual(float("inf"), dist_point_to_points((0, 0), []))

    def test_seg_point_dist_handles_degenerate_segment(self):
        from common.boardgeom import seg_point_dist
        self.assertAlmostEqual(5.0, seg_point_dist((3, 4), (0, 0), (0, 0)))

    def test_seg_seg_min_dist_is_zero_when_crossing(self):
        from common.boardgeom import seg_seg_min_dist
        self.assertEqual(0.0, seg_seg_min_dist((-1, 0), (1, 0),
                                               (0, -1), (0, 1)))


class SiModels(unittest.TestCase):
    def test_single_ipc2141_microstrip_implementation(self):
        z0 = si_models.z0_microstrip(0.4, 0.035, 1.6, 4.5)
        self.assertAlmostEqual(
            si_models.zdiff_microstrip(0.4, 0.4, 0.035, 1.6, 4.5),
            2.0 * z0 * (1.0 - 0.347 * 2.718281828459045 ** (-2.9 * 0.4 / 1.6)),
            places=6)

    def test_domain_guard_is_kept(self):
        with self.assertRaises(si_models.ModelDomainError):
            si_models.z0_microstrip(5.0, 0.035, 0.05, 4.5)

    def test_z0_with_notes_never_returns_a_negative_impedance(self):
        out = si_models.z0_with_notes(5.0, 0.05, 4.3, 0.035, "microstrip")
        self.assertIsNone(out["z0_ohm"])
        self.assertIs(False, out["signoff"])

    def test_stripline_b_equals_two_h_is_declared(self):
        out = si_models.z0_with_notes(0.2, 0.2, 4.3, 0.035, "stripline")
        self.assertTrue(any("b=2H" in n for n in out["notes"]))

    def test_er_eff_is_er_for_stripline_and_lower_for_microstrip(self):
        self.assertEqual(4.3, si_models.er_eff(4.3, 0.2, 0.2, "stripline"))
        self.assertLess(si_models.er_eff(4.3, 0.2, 0.2, "microstrip"), 4.3)

    def test_ipc2221_is_screen_only_and_monotonic_in_current(self):
        low = si_models.ipc2221_min_width_mm(0.5, 10, 1.0, "microstrip")
        high = si_models.ipc2221_min_width_mm(2.0, 10, 1.0, "microstrip")
        self.assertGreater(high, low)


class SExpr(unittest.TestCase):
    STACKUP = '''(kicad_pcb
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2) (epsilon_r 4.5))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
)'''

    def test_block_respects_strings_containing_parens(self):
        text = '(a "b)c" (d e))'
        self.assertEqual(text, sexpr.block(text, 0))

    def test_num_kills_float_residue_and_negative_zero(self):
        self.assertEqual("195.58", sexpr.num(195.57999999999998))
        self.assertEqual("0", sexpr.num(-0.0))

    def test_quote_round_trip(self):
        for raw in ('plain', 'has "quote"', 'back\\slash'):
            self.assertEqual(raw, sexpr.unquote(sexpr.q(raw)))

    def test_direct_children_ignores_nested_forms(self):
        """The stackup parser must not pick up a nested (layer ...)."""
        nested = '(stackup (layer "A" (type "copper") (layer "INNER")) (layer "B"))'
        names = [sexpr.prop(f, "layer") for f in
                 sexpr.direct_children(nested, "layer")]
        self.assertEqual(2, len(names))

    def test_prop_extracts_quoted_and_bare_values(self):
        form = '(layer "F.Cu" (type "copper") (thickness 0.035))'
        self.assertEqual("copper", sexpr.prop(form, "type"))
        self.assertEqual("0.035", sexpr.prop(form, "thickness"))
        self.assertIsNone(sexpr.prop(form, "epsilon_r"))

    def test_stackup_parses_to_the_same_values_as_the_old_parser(self):
        """Regression pin for the via_stub parser deletion.

        These numbers were captured from the ORIGINAL via_stub parser
        (balanced_form/direct_layer_forms) on this exact input before it was
        replaced by common.sexpr.
        """
        sys.path.insert(0, str(TOOLS / "scripts" / "board"))
        try:
            import via_stub
        except Exception as exc:                       # pragma: no cover
            self.skipTest(f"pcbnew unavailable: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "s.kicad_pcb"
            board.write_text(self.STACKUP, encoding="utf-8")
            stackup, status = via_stub.parse_stackup(str(board))
        self.assertEqual("stackup", status)
        self.assertEqual(
            [("F.Cu", "copper", 0.035, None, 0.0, 0.035),
             ("dielectric 1", "prepreg", 0.2, 4.5, 0.035, 0.235),
             ("B.Cu", "copper", 0.035, None, 0.235, 0.27)],
            [(i.name, i.kind, i.thickness_mm, i.dk, round(i.z0_mm, 6),
              round(i.z1_mm, 6)) for i in stackup.items],
        )
        self.assertEqual({"F.Cu": 0.0175, "B.Cu": 0.2525},
                         {k: round(v, 6) for k, v in stackup.copper_z_mm.items()})


class Paths(unittest.TestCase):
    def test_absolute_and_dotdot_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in ("/etc/passwd", "../outside", "a/../../b"):
                with self.subTest(bad=bad):
                    with self.assertRaises(PathConfinementError):
                        paths.safe_path(root, bad)

    def test_symlink_escape_is_rejected(self):
        """The lexical+resolved pair check; the old copies caught only one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("x", encoding="utf-8")
            link = root / "link"
            try:
                link.symlink_to(outside)
            except OSError:                            # pragma: no cover
                self.skipTest("symlinks unavailable")
            with self.assertRaises(PathConfinementError):
                paths.safe_path(root, "link/secret.txt")

    def test_good_path_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "f.json").write_text("{}", encoding="utf-8")
            self.assertEqual((root / "sub" / "f.json").resolve(),
                             paths.safe_path(root, "sub/f.json"))

    def test_confine_to_dir_enforces_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(PathConfinementError):
                paths.confine_to_dir(root / "a.txt", root, suffix=".json")
            self.assertEqual((root / "a.json").resolve(),
                             paths.confine_to_dir(root / "a.json", root,
                                                  suffix=".json"))


class Coverage(unittest.TestCase):
    def test_rule_matches_the_schema_required_fields(self):
        """The rule and the schema must not drift apart."""
        schema = json.loads(
            (SCHEMAS / "metric_coverage.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["required"]),
                         {"eligible", "scanned", "excluded", "violations"})
        self.assertEqual(["eligible", "scanned", "excluded", "violations"],
                         findings.coverage_required_fields())

    def test_zero_scan_is_no_coverage(self):
        self.assertEqual("NO_COVERAGE", findings.coverage_status(
            {"eligible": 3, "scanned": 0, "excluded": 3, "violations": 0}))

    def test_parts_must_sum(self):
        self.assertEqual("INVALID_COVERAGE", findings.coverage_status(
            {"eligible": 3, "scanned": 2, "excluded": 0, "violations": 0}))

    def test_scanned_cannot_exceed_eligible(self):
        self.assertIn(findings.SCANNED_EXCEEDS_ELIGIBLE,
                      findings.coverage_rule_violations(
                          {"eligible": 1, "scanned": 5, "excluded": 0}))

    def test_violations_cannot_exceed_scanned(self):
        self.assertIn(findings.VIOLATIONS_EXCEED_SCANNED,
                      findings.coverage_rule_violations(
                          {"eligible": 3, "scanned": 3, "excluded": 0,
                           "violations": 9}))

    def test_bool_is_not_a_count(self):
        self.assertIn(findings.NOT_A_COUNT,
                      findings.coverage_rule_violations(
                          {"eligible": True, "scanned": 1, "excluded": 0}))

    def test_green_and_red(self):
        self.assertEqual("GREEN", findings.coverage_status(
            {"eligible": 3, "scanned": 3, "excluded": 0, "violations": 0}))
        self.assertEqual("RED", findings.coverage_status(
            {"eligible": 3, "scanned": 3, "excluded": 0, "violations": 1}))


class FindingsEnvelope(unittest.TestCase):
    def test_zero_scanned_is_never_pass(self):
        self.assertEqual("NO_COVERAGE", findings.result("c", 0, [])["status"])

    def test_violations_make_it_fail(self):
        self.assertEqual("FAIL", findings.result("c", 3, ["v"])["status"])
        self.assertEqual("PASS", findings.result("c", 3, [])["status"])

    def test_board_report_shape(self):
        report = findings.board_report("via_stub", "b.kicad_pcb",
                                       [findings.result("c", 1, [])])
        self.assertEqual("board-check-report/v1", report["schema_version"])
        self.assertEqual({"PASS": 1, "FAIL": 0, "NO_COVERAGE": 0},
                         report["summary"])
        self.assertEqual(0, findings.board_exit_code(report))


class ReportIo(unittest.TestCase):
    def test_dumps_rejects_nan(self):
        """Three board tools used to emit NaN, which is not valid JSON."""
        with self.assertRaises(ValueError):
            dumps({"x": float("nan")})

    def test_dumps_keeps_non_ascii_literal(self):
        self.assertIn("ö", dumps({"k": "öğe"}))

    def test_write_report_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            text = write_report({"a": 1}, out)
            self.assertEqual(json.loads(text),
                             json.loads(out.read_text(encoding="utf-8")))

    def test_write_report_raises_when_a_parent_is_a_file(self):
        """A failed write must raise, so the caller cannot report GREEN with
        no report on disk."""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("i am a file", encoding="utf-8")
            with self.assertRaises(ReportWriteError):
                write_report({"a": 1}, blocker / "sub" / "r.json")

    def test_run_cli_maps_exceptions_to_exit_2(self):
        from common.report_io import run_cli

        def boom():
            raise RuntimeError("kaboom")

        self.assertEqual(2, run_cli(boom, schema_version="x/v1"))

    def test_run_cli_passes_through_normal_codes(self):
        from common.report_io import run_cli
        self.assertEqual(0, run_cli(lambda: 0))
        self.assertEqual(1, run_cli(lambda: 1))
        self.assertEqual(0, run_cli(lambda: None))


class Ids(unittest.TestCase):
    def test_det_uuid_is_deterministic_and_namespaced(self):
        from common.ids import det_uuid
        a = det_uuid("rewire", "/tmp/board.kicad_sch", "w1")
        self.assertEqual(a, det_uuid("rewire", "/tmp/board.kicad_sch", "w1"),
                         "the same inputs must give the same UUID")
        self.assertNotEqual(a, det_uuid("wireify", "/tmp/board.kicad_sch", "w1"),
                            "namespace must separate the tools")
        self.assertNotEqual(a, det_uuid("rewire", "/tmp/board.kicad_sch", "w2"),
                            "tag must separate the objects")

    def test_regression_same_named_files_under_different_boards_differ(self):
        """This assertion used to say the OPPOSITE ("only the basename should
        matter"), which pinned a real defect: this tree carries mipi/power/
        PCIe/hdmi/RTC/PSUs .kicad_sch under several different boards, and a
        shared UUID makes KiCad treat two objects as one."""
        from common.ids import det_uuid
        self.assertNotEqual(
            det_uuid("rewire", "proc-a-cv72s/cv72s/mipi.kicad_sch", "w1"),
            det_uuid("rewire", "proc-r-rk3588/rk3588/mipi.kicad_sch", "w1"))

    def test_a_scratch_copy_keeps_the_canonical_identity(self):
        """Every autonomous run works in .scratch/<id>/ and promotes back;
        a key that changed there would churn every UUID on every run."""
        from common.ids import det_uuid
        canonical = "proj/sheets/mipi.kicad_sch"
        scratch = "proj/.scratch/20260805T052209496105Z/sheets/mipi.kicad_sch"
        self.assertEqual(det_uuid("rewire", canonical, "w1"),
                         det_uuid("rewire", scratch, "w1"))

    def test_two_different_scratch_runs_agree(self):
        from common.ids import det_uuid
        a = det_uuid("rewire", "proj/.scratch/AAA/sheets/mipi.kicad_sch", "w1")
        b = det_uuid("rewire", "proj/.scratch/BBB/sheets/mipi.kicad_sch", "w1")
        self.assertEqual(a, b)

    def test_windows_and_posix_separators_agree(self):
        from common.ids import det_uuid
        self.assertEqual(det_uuid("rewire", r"proj\sheets\mipi.kicad_sch", "w1"),
                         det_uuid("rewire", "proj/sheets/mipi.kicad_sch", "w1"))

    def test_the_scratch_constant_matches_the_scratch_module(self):
        """Duplicated constant; pinned so the two cannot drift apart."""
        from common.ids import SCRATCH_ROOT_NAME
        from common.scratch import SCRATCH_ROOT_NAME as CANONICAL
        self.assertEqual(CANONICAL, SCRATCH_ROOT_NAME)


class HarvestedFromPcbToolV2(unittest.TestCase):
    """Fixes merged back from the projectE-2archive/pcb-tool-v2 ancestor.

    Each of these was a real defect that tree had already solved and this one
    had either lost in the refactor or never had.
    """

    def test_finding_rejects_a_scanned_zero_pass(self):
        # A gate that examined nothing must never be able to say PASS, no
        # matter how the Finding is built.
        for bad in (findings.PASS, findings.FAIL):
            with self.assertRaises(ValueError):
                findings.Finding("empty_check", bad, 0, [], "")
        ok = findings.Finding("empty_check", findings.NO_COVERAGE, 0, [], "")
        self.assertEqual(findings.NO_COVERAGE, ok["status"])

    def test_kicad_cli_honours_the_env_override(self):
        import os
        from common import kicad_cli
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "kicad-cli"
            fake.write_text("#!/bin/sh\n", encoding="utf-8")
            fake.chmod(0o755)
            old = os.environ.get(kicad_cli.KICAD_CLI_ENV)
            os.environ[kicad_cli.KICAD_CLI_ENV] = str(fake)
            try:
                self.assertEqual(str(fake), kicad_cli._require_cli())
            finally:
                if old is None:
                    os.environ.pop(kicad_cli.KICAD_CLI_ENV, None)
                else:
                    os.environ[kicad_cli.KICAD_CLI_ENV] = old

    def test_kicad_cli_refuses_an_unresolvable_request(self):
        from common.kicad_cli import KicadCliError, _require_cli
        with self.assertRaises(KicadCliError):
            _require_cli("/nonexistent/kicad-cli-does-not-exist")

    def test_via_width_is_read_with_a_layer_argument(self):
        # KiCad 10 has per-layer via stacks; PCB_VIA::GetWidth() with no
        # argument trips a debug assert that opens a modal dialog and hangs a
        # headless gate. Source-level check so it fails without pcbnew too.
        src = (TOOLS / "scripts" / "board" / "dfm_emc_check.py").read_text(
            encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "GetWidth()" not in stripped:
                continue
            self.fail(f"no-arg GetWidth() on a via: {stripped}")


if __name__ == "__main__":
    unittest.main()
