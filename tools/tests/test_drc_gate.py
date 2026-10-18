#!/usr/bin/env python3
"""scripts/drc_gate.py - the vendored replacement for tools/krt/drc.sh.

The shell original had no tests because it had no output contract. These
tests pin the four defects that made it unusable as gate evidence, plus the
census that stops an empty design reading as a clean one.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402
from _paths import SCRIPTS  # noqa: E402

DRC_GATE = SCRIPTS / "drc_gate.py"

sys.path.insert(0, str(SCRIPTS))
import drc_gate  # noqa: E402

PCB = """(kicad_pcb (version 20240108)
	(footprint
		"Resistor_SMD:R_0402"
		(at 10 10)
		(pad "1" smd rect (at 0 0))
		(pad "2" smd rect (at 1 0))
	)
	(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu"))
	(arc (start 0 0) (mid 1 1) (end 2 0) (width 0.2))
	(via (at 1 0) (size 0.6) (drill 0.3))
	(zone (net 0) (layer "F.Cu"))
	(setup (via_size 0.6) (via_drill 0.3))
)"""

SCH = """(kicad_sch (version 20240108)
	(lib_symbols
		(symbol "Device:R"
			(symbol "Device:R_0_1" (rectangle (start 0 0)))
		)
	)
	(symbol
		(lib_id "Device:R")
		(at 10 10 0)
	)
	(wire (pts (xy 0 0) (xy 1 0)))
	(junction (at 1 0))
	(label "NET1" (at 1 0 0))
	(no_connect (at 2 0))
)"""


def run(*args: str) -> tuple[int, dict | None, str]:
    proc = subprocess.run([sys.executable, *args], text=True, encoding="utf-8",
                          capture_output=True, check=False)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr



class _ReportBase(unittest.TestCase):
    """build_report hashes the design file, so the fixture must be real."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drc-gate-test-")
        self.board = Path(self.temp.name) / "b.kicad_pcb"
        self.board.write_text(PCB, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def report(self, raw, census, argv=None):
        return drc_gate.build_report(
            self.board, "drc",
            {"name": "kicad-cli", "path": "", "version": "", "sha256": ""},
            argv or drc_gate.build_argv("kicad-cli", "drc", "b", "o"), 0,
            raw, census,
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", None)


FULL_CENSUS = {"footprint": 1, "pad": 2, "segment": 1, "arc": 0, "via": 0,
               "zone": 0}


class GateContractTests(_ReportBase):
    """The flags whose absence made the shell script report false PASSes."""

    def test_drc_argv_always_carries_both_gate_flags(self):
        argv = drc_gate.build_argv("kicad-cli", "drc", "b.kicad_pcb", "o.json")
        self.assertIn("--exit-code-violations", argv)
        self.assertIn("--severity-all", argv)

    def test_erc_argv_always_carries_both_gate_flags(self):
        argv = drc_gate.build_argv("kicad-cli", "erc", "b.kicad_sch", "o.json")
        self.assertIn("--exit-code-violations", argv)
        self.assertIn("--severity-all", argv)

    def test_drc_argv_carries_all_track_errors(self):
        """D7's root cause, pinned.

        Without this flag kicad-cli reports only the FIRST error on each
        track and the winner is a race between DRC workers, so the same board
        with the same digest returned 182..205 violations run to run. The
        threshold this gate enforces is zero, and zero cannot sit on a number
        that is redrawn every run.
        """
        argv = drc_gate.build_argv("kicad-cli", "drc", "b.kicad_pcb", "o.json")
        self.assertIn("--all-track-errors", argv)

    def test_erc_argv_does_not_carry_all_track_errors(self):
        """``kicad-cli sch erc`` has no such option; passing it would turn
        every ERC run into an argument error - a fix for one mode must not
        break the other."""
        argv = drc_gate.build_argv("kicad-cli", "erc", "b.kicad_sch", "o.json")
        self.assertNotIn("--all-track-errors", argv)

    def test_schematic_parity_is_opt_in(self):
        without = drc_gate.build_argv("kicad-cli", "drc", "b", "o")
        with_flag = drc_gate.build_argv("kicad-cli", "drc", "b", "o",
                                        schematic_parity=True)
        self.assertNotIn("--schematic-parity", without)
        self.assertIn("--schematic-parity", with_flag)

    def test_parity_without_the_flag_is_no_coverage_not_pass(self):
        """kicad-cli returns an EMPTY parity list unless asked; an empty list
        is not evidence of parity."""
        report = self.report(
            {"violations": []},
            FULL_CENSUS)
        parity = next(c for c in report["checks"]
                      if c["check"] == "schematic_parity")
        self.assertEqual("NO_COVERAGE", parity["status"])

    def test_violations_with_exit_zero_are_still_a_fail(self):
        """The exact false PASS the original shell script produced."""
        report = self.report(
            {"violations": [{"severity": "error", "type": "clearance",
                             "description": "too close"}]},
            FULL_CENSUS)
        self.assertEqual("FAIL", report["status"])
        self.assertEqual(1, report["metrics"]["violation_count"])


class CensusTests(_ReportBase):
    def test_board_objects_are_counted_across_newlines(self):
        """KiCad 10 writes '(footprint\\n\\t\\t"lib:fp"'. A matcher that
        requires a space after the token counts zero and turns every board
        into a silent NO_COVERAGE."""
        census = drc_gate.pcb_census(PCB)
        self.assertEqual(1, census["footprint"])
        self.assertEqual(2, census["pad"])
        self.assertEqual(1, census["segment"])
        self.assertEqual(1, census["arc"])
        self.assertEqual(1, census["via"])
        self.assertEqual(1, census["zone"])

    def test_setup_via_size_is_not_counted_as_a_via(self):
        self.assertEqual(1, drc_gate.pcb_census(PCB)["via"])

    def test_library_symbols_are_not_counted_as_instances(self):
        """(symbol ...) inside (lib_symbols ...) is a definition. Counting it
        would inflate the ERC denominator with things ERC never checks."""
        census = drc_gate.sch_census(SCH)
        self.assertEqual(1, census["symbol"])
        self.assertEqual(1, census["wire"])
        self.assertEqual(1, census["junction"])
        self.assertEqual(1, census["label"])
        self.assertEqual(1, census["no_connect"])

    def test_an_empty_design_is_no_coverage(self):
        report = self.report(
            {"violations": []},
            {name: 0 for name in (*drc_gate.PCB_OBJECTS, "pad")})
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(0, report["coverage"]["scanned"])

    def test_coverage_arithmetic_holds(self):
        report = self.report(
            {"violations": []},
            FULL_CENSUS)
        coverage = report["coverage"]
        self.assertEqual(coverage["eligible"],
                         coverage["scanned"] + coverage["excluded"])
        self.assertLessEqual(coverage["violations"], coverage["scanned"])


GOOD_ARGV = drc_gate.build_argv(
    "/opt/kicad/bin/kicad-cli", "drc", "/tmp/x/design.kicad_pcb",
    "/tmp/x/report.json")


def stub(version="10.0.4", argv=None, digest="a" * 64, violations=191,
         unconnected=21, mode="drc"):
    """A minimal drc-gate-report/v1 with only the fields the guard reads."""
    return {
        "schema_version": drc_gate.SCHEMA_VERSION,
        "mode": mode,
        "engine": {"name": "kicad-cli", "path": "/opt/kicad/bin/kicad-cli",
                   "version": version, "sha256": ""},
        "argv": GOOD_ARGV if argv is None else argv,
        "design": {"path": "b.kicad_pcb", "sha256": digest},
        "metrics": {"violation_count": violations, "error_count": 0,
                    "warning_count": 0, "unconnected_count": unconnected},
    }


class ComparabilityGuardTests(unittest.TestCase):
    """D-#44: two DRC numbers are a measurement only when the engine and the
    flags behind them are known AND equal.

    The case: a committed drc.json recorded 191 violations / 21 unconnected
    under kicad-cli 10.0.5; the same unchanged board under 10.0.4 returned a
    different number. Nothing refused to subtract the two.
    """

    def test_matching_engine_and_flags_are_comparable(self):
        """The guard has to let the legitimate case through, or the tests
        below only prove that it refuses everything."""
        result = drc_gate.compare_runs(stub(), stub())
        self.assertTrue(result["comparable"], result["blockers"])
        self.assertEqual("COMPARED", result["verdict"])
        self.assertEqual(0, result["delta"]["violation_count"])

    def test_different_engine_versions_are_refused(self):
        result = drc_gate.compare_runs(stub(version="10.0.5", violations=191),
                                       stub(version="10.0.4", violations=222))
        self.assertFalse(result["comparable"])
        self.assertEqual("REFUSED", result["verdict"])
        self.assertIn("ENGINE_VERSION_MISMATCH",
                      {b["code"] for b in result["blockers"]})
        self.assertIsNone(
            result["delta"],
            "a refused comparison must publish NO delta; a '31' here is the "
            "very number that cannot be attributed to anything")

    def test_an_unmeasured_version_is_refused_not_assumed_equal(self):
        """Two blanks are two unanswered questions, not one matching answer."""
        result = drc_gate.compare_runs(stub(version=""), stub(version=""))
        self.assertFalse(result["comparable"])
        self.assertEqual(
            2, sum(1 for b in result["blockers"]
                   if b["code"] == "ENGINE_VERSION_UNKNOWN"))

    def test_the_literal_string_unknown_is_not_a_version(self):
        """engine_version() returns 'unknown' when kicad-cli says nothing."""
        result = drc_gate.compare_runs(stub(version="unknown"),
                                       stub(version="unknown"))
        self.assertFalse(result["comparable"])

    def test_different_flags_are_refused(self):
        """--all-track-errors alone moved this repo's board ~184 -> 292."""
        thin = [t for t in GOOD_ARGV if t != "--all-track-errors"]
        result = drc_gate.compare_runs(stub(violations=292),
                                       stub(argv=thin, violations=184))
        self.assertFalse(result["comparable"])
        codes = {b["code"] for b in result["blockers"]}
        self.assertIn("INVOCATION_MISMATCH", codes)

    def test_raw_kicad_cli_json_carries_no_argv_and_is_refused(self):
        """The shape a project's build/drc.json actually has: it records
        kicad_version but never the command line, so whether it was produced
        with --all-track-errors is unknowable from the file."""
        raw = {"kicad_version": "10.0.4", "violations": [], "unconnected_items": []}
        result = drc_gate.compare_runs(raw, raw)
        self.assertFalse(result["comparable"])
        self.assertIn("INVOCATION_UNKNOWN",
                      {b["code"] for b in result["blockers"]})

    def test_drc_and_erc_are_not_compared(self):
        erc = stub(mode="erc", argv=drc_gate.build_argv(
            "/opt/kicad/bin/kicad-cli", "erc", "/tmp/x/design.kicad_sch",
            "/tmp/x/report.json"))
        result = drc_gate.compare_runs(stub(), erc)
        self.assertIn("MODE_MISMATCH", {b["code"] for b in result["blockers"]})

    def test_an_unrecognised_document_is_refused(self):
        result = drc_gate.compare_runs({"hello": "world"}, stub())
        self.assertFalse(result["comparable"])
        self.assertIn("UNRECOGNISED_REPORT",
                      {b["code"] for b in result["blockers"]})

    def test_same_board_disagreeing_is_flagged_not_read_as_progress(self):
        result = drc_gate.compare_runs(stub(violations=292),
                                       stub(violations=205))
        self.assertTrue(result["comparable"])
        self.assertEqual("none_expected", result["attribution"])
        self.assertFalse(result["agreement"])

    def test_a_changed_board_attributes_the_delta_to_the_design(self):
        result = drc_gate.compare_runs(stub(digest="a" * 64, violations=292),
                                       stub(digest="b" * 64, violations=0))
        self.assertTrue(result["comparable"])
        self.assertEqual("design", result["attribution"])
        self.assertEqual(-292, result["delta"]["violation_count"])

    def test_argv_signature_ignores_paths_but_keeps_flags(self):
        """Every run stages the design in a fresh temp dir, so the paths
        differ between two runs of the SAME command; comparing raw argv would
        refuse every honest comparison."""
        other = drc_gate.build_argv("C:/KiCad/bin/kicad-cli.exe", "drc",
                                    "D:/other/board.kicad_pcb", "D:/o.json")
        self.assertEqual(drc_gate.argv_signature(GOOD_ARGV),
                         drc_gate.argv_signature(other))
        self.assertIn("--all-track-errors", drc_gate.argv_signature(GOOD_ARGV))

    def test_a_gate_report_publishes_its_own_provenance(self):
        report = drc_gate.build_report(
            Path(__file__), "drc",
            {"name": "kicad-cli", "path": "", "version": "10.0.4", "sha256": ""},
            GOOD_ARGV, 0, {"violations": [], "kicad_version": "10.0.4"},
            FULL_CENSUS, "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:01+00:00", None)
        self.assertEqual("10.0.4", report["provenance"]["engine_version"])
        self.assertEqual("kicad-cli version",
                         report["provenance"]["engine_version_source"])
        self.assertIn("--all-track-errors",
                      report["provenance"]["argv_signature"])


class CompareCliTests(unittest.TestCase):
    """The refusal must be visible at the process boundary, not only in a
    library return value: exit 2 (no answer), never 0."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drc-compare-")
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name: str, payload: dict) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_version_mismatch_exits_2(self):
        left = self.write("a.json", stub(version="10.0.5", violations=191))
        right = self.write("b.json", stub(version="10.0.4", violations=222))
        code, report, _stderr = run(str(DRC_GATE), "--compare",
                                    str(left), str(right))
        self.assertEqual(2, code)
        self.assertEqual("REFUSED", report["verdict"])

    def test_the_same_pair_with_the_version_corrected_is_accepted(self):
        left = self.write("a.json", stub(version="10.0.4", violations=191))
        right = self.write("b.json", stub(version="10.0.4", violations=191))
        code, report, _stderr = run(str(DRC_GATE), "--compare",
                                    str(left), str(right))
        self.assertEqual(0, code)
        self.assertEqual("COMPARED", report["verdict"])

    def test_a_real_disagreement_exits_1_not_2(self):
        """1 = the two runs disagree; 2 = no comparison was possible. A gate
        that maps both onto one code cannot tell a regression from a refusal."""
        left = self.write("a.json", stub(violations=292))
        right = self.write("b.json", stub(violations=205))
        code, report, _stderr = run(str(DRC_GATE), "--compare",
                                    str(left), str(right))
        self.assertEqual(1, code)
        self.assertFalse(report["agreement"])

    def test_a_missing_report_exits_2(self):
        left = self.write("a.json", stub())
        code, report, _stderr = run(str(DRC_GATE), "--compare", str(left),
                                    str(self.dir / "nope.json"))
        self.assertEqual(2, code)
        self.assertEqual("ERROR", report["status"])

    def test_compare_and_design_together_are_refused(self):
        left = self.write("a.json", stub())
        code, _report, stderr = run(str(DRC_GATE), "b.kicad_pcb", "--compare",
                                    str(left), str(left))
        self.assertEqual(2, code)
        self.assertIn("does not take", stderr)


class CliTests(unittest.TestCase):
    def test_missing_positional_exits_2(self):
        exit_code, _report, stderr = run(str(DRC_GATE))
        self.assertEqual(2, exit_code)
        self.assertIn("DESIGN is required", stderr)

    def test_self_test_is_opt_in_and_clean(self):
        exit_code, report, _stderr = run(str(DRC_GATE), "--self-test")
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", report["self_test"], report)

    def test_an_unknown_suffix_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thing.txt"
            path.write_text("not a design", encoding="utf-8")
            exit_code, report, _stderr = run(str(DRC_GATE), str(path))
            self.assertEqual(2, exit_code)
            self.assertEqual("ERROR", report["status"])

    def test_a_missing_file_exits_2(self):
        exit_code, report, _stderr = run(str(DRC_GATE), "/nope.kicad_pcb")
        self.assertEqual(2, exit_code)
        self.assertEqual("ERROR", report["status"])

    def test_a_missing_kicad_cli_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            path.write_text(PCB, encoding="utf-8")
            exit_code, report, _stderr = run(
                str(DRC_GATE), str(path), "--kicad-cli",
                "/definitely/not/kicad-cli")
            self.assertEqual(2, exit_code)
            self.assertEqual("ERROR", report["status"])


def _kicad_cli() -> str | None:
    """``shutil.which`` is not the gate's own resolver; use the resolver so
    these tests run wherever the tool itself would run.

    This is load-bearing on Windows: the KiCad installer does not put
    kicad-cli on PATH, so a ``shutil.which`` skip guard silently skipped every
    live-engine test on the machine KiCad was actually installed on.
    """
    try:
        return drc_gate.resolve_kicad_cli(None)
    except Exception:
        return None


@unittest.skipUnless(_kicad_cli(), "kicad-cli not installed")
class LiveEngineTests(unittest.TestCase):
    def test_a_real_run_records_the_engine_and_writes_a_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text(PCB, encoding="utf-8")
            out = Path(tmp) / "report.json"
            raw = Path(tmp) / "raw.json"
            exit_code, report, _stderr = run(
                str(DRC_GATE), str(board), "--json", str(out),
                "--keep-raw", str(raw))
            self.assertIn(exit_code, (0, 1))
            self.assertTrue(out.is_file())
            self.assertTrue(raw.is_file())
            self.assertEqual("kicad-cli", report["engine"]["name"])
            self.assertTrue(report["engine"]["version"])
            self.assertEqual(64, len(report["engine"]["sha256"]))
            self.assertEqual(report["design"]["path"], str(board.resolve()))

    def test_the_report_never_leaks_the_temp_workdir_as_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text(PCB, encoding="utf-8")
            _exit, report, _stderr = run(str(DRC_GATE), str(board))
            self.assertNotIn("drc-gate-", json.dumps(report["design"]))


# A REAL board, deliberately. A synthetic fixture was tried first and was
# measured to be NON-DISCRIMINATING: 40 packed tracks returned 32 violations
# with the flag and 32 without it, five runs each, so a determinism test built
# on it would have passed before the fix as loudly as after - the vacuous
# green this repo keeps finding. This board is the one the defect was measured
# on: without --all-track-errors it varied on 5 runs out of 5.
_CAMW = _paths.REPO.parent / "cam-w-og05b10" / "cam-w-og05b10.kicad_pcb"


def _fingerprint(report: dict) -> str:
    """Everything the gate would act on, and nothing that legitimately moves.

    Timestamps and the engine block are excluded because they SHOULD differ
    between runs; the violation list is compared item by item rather than by
    count, because two runs agreeing on 292 while disagreeing on WHICH 292 is
    the same defect wearing a matching total.
    """
    return json.dumps({
        "metrics": report["metrics"],
        "severities": report["severities"],
        "census": report["census"],
        "coverage": report["coverage"],
        "status": report["status"],
        "violations": sorted(drc_gate.describe(v) for v in report["violations"]),
    }, sort_keys=True, ensure_ascii=False)


@unittest.skipUnless(_kicad_cli() and _CAMW.is_file(),
                     "needs kicad-cli and the cam-w-og05b10 board")
class DeterminismTests(unittest.TestCase):
    """D7: the same board must produce the same report, run after run."""

    RUNS = 5

    def test_five_consecutive_runs_agree_bit_for_bit(self):
        prints, counts = [], []
        for _ in range(self.RUNS):
            report = drc_gate.gate(_CAMW)
            prints.append(_fingerprint(report))
            counts.append(report["metrics"]["violation_count"])
        self.assertEqual(
            1, len(set(prints)),
            f"{self.RUNS} runs on one unchanged board disagreed; violation "
            f"counts were {counts}. A gate whose measurement is redrawn each "
            f"run cannot carry a threshold of zero.")

    def test_two_live_runs_pass_the_comparability_guard(self):
        """The determinism test above compares a fingerprint that DELIBERATELY
        excludes the engine block, so it would stay green across a kicad-cli
        swap that changed every count. This is the missing half: the guard
        must accept two runs of the same engine, and it must have a version
        to accept - a report whose engine version came back empty is refused,
        which is what would have caught #44's baseline."""
        left, right = drc_gate.gate(_CAMW), drc_gate.gate(_CAMW)
        self.assertTrue(left["provenance"]["engine_version"],
                        "the gate recorded no measured kicad-cli version")
        result = drc_gate.compare_runs(left, right)
        self.assertTrue(result["comparable"], result["blockers"])
        self.assertTrue(
            result["agreement"],
            f"same board, same engine, different answer: {result['delta']}")

    def test_the_digest_is_the_same_board_across_those_runs(self):
        """Guards the guard: identical reports prove nothing if the runs were
        not actually looking at the same file."""
        digests = {drc_gate.gate(_CAMW)["design"]["sha256"] for _ in range(2)}
        self.assertEqual(1, len(digests))


if __name__ == "__main__":
    unittest.main()
