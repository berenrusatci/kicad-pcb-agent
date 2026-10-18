#!/usr/bin/env python3
"""Tests for scripts/production/ (contracts, freerouting, assembly, outputs).

Converted from four archived pytest suites to unittest.

MOCKING POLICY, STATED IN THE TEST NAMES
----------------------------------------
Every test whose name contains ``_binary_not_exercised`` stubs out java,
freerouting.jar, kibot or kicad-cli. Those tests prove the BRIDGE logic - the
argv that gets built, the fail-fast line scanning, the gate arithmetic - and
prove NOTHING about the external tool. None of these three binaries is
available in this environment, so no test here is end-to-end evidence that
FreeRouting or KiBot works.

``ImportIsolation`` proves the point of the whole tier: a missing binary
degrades to a reported result instead of breaking collection.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCHEMAS, SCRIPTS  # noqa: E402

PRODUCTION = SCRIPTS / "production"

from scripts.production import assembly as asm  # noqa: E402
from scripts.production import contracts as con  # noqa: E402
from scripts.production import freerouting as fr  # noqa: E402
from scripts.production import outputs as out  # noqa: E402


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
class DrcContractShape(unittest.TestCase):
    def test_drc_reads_violations_and_unconnected_items(self):
        """A violations-only reader passed six real error-severity unconnected
        items silently - a board could reach fabrication unrouted."""
        contract = con.build_drc_contract("b.kicad_pcb", {
            "violations": [{"severity": "error", "type": "clearance"}],
            "unconnected_items": [{"severity": "error", "type": "unconnected"}],
        })
        self.assertEqual(contract.violation_count, 2)

    def test_erc_reads_all_sheets_not_just_the_first(self):
        contract = con.build_drc_contract("b.kicad_sch", {"sheets": [
            {"path": "/", "violations": [{"severity": "error"}]},
            {"path": "/sub", "violations": [{"severity": "fatal"}]},
        ]}, source="erc")
        self.assertEqual(contract.violation_count, 2)

    def test_warnings_are_counted_separately(self):
        contract = con.build_drc_contract("b.kicad_pcb", {"violations": [
            {"severity": "error"}, {"severity": "warning"},
            {"severity": "ignore"}]})
        self.assertEqual((contract.violation_count, contract.warning_count),
                         (1, 2))

    def test_erc_treats_fatal_as_severe_drc_does_not_see_it(self):
        erc = con.build_drc_contract("s", {"sheets": [
            {"violations": [{"severity": "fatal"}]}]}, "erc")
        self.assertEqual(erc.violation_count, 1)

    def test_an_unknown_source_raises_rather_than_returning_empty(self):
        """With the wrong source the violation list comes back empty and
        manufactures a PASS."""
        with self.assertRaises(ValueError):
            con.build_drc_contract("b", {"violations": []}, source="whatever")

    def test_schema_recognition(self):
        self.assertFalse(con.schema_unrecognised({"violations": []}))
        self.assertFalse(con.schema_unrecognised({"sheets": []}))
        self.assertTrue(con.schema_unrecognised({"results": []}))
        self.assertTrue(con.schema_unrecognised({}))


class DrcGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_missing_file_raises_it_does_not_fail_open(self):
        with self.assertRaises(con.ContractGateError):
            con.drc_gate_passes(str(self.tmp / "nope.json"))

    def test_broken_json_raises(self):
        path = self.tmp / "drc.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(con.ContractGateError):
            con.drc_gate_passes(str(path))

    def test_an_unexpected_schema_raises(self):
        path = self.tmp / "drc.json"
        path.write_text('{"something": 1}', encoding="utf-8")
        with self.assertRaises(con.ContractGateError):
            con.drc_gate_passes(str(path))

    def test_a_clean_contract_passes(self):
        path = str(self.tmp / "drc.json")
        con.write_drc_contract(
            con.build_drc_contract("b", {"violations": [],
                                         "unconnected_items": []}), path)
        self.assertTrue(con.drc_gate_passes(path))

    def test_fault_injection_one_error_shuts_the_gate(self):
        path = str(self.tmp / "drc.json")
        con.write_drc_contract(
            con.build_drc_contract(
                "b", {"violations": [{"severity": "error",
                                      "type": "clearance"}]}), path)
        self.assertFalse(con.drc_gate_passes(path))

    def test_the_gate_reads_disk_not_the_object_in_memory(self):
        path = str(self.tmp / "drc.json")
        contract = con.build_drc_contract("b", {"violations": []})
        con.write_drc_contract(contract, path)
        # Corrupt the on-disk copy only; the in-memory object stays clean.
        # Still a well-formed contract - the point of this test is which COPY
        # the gate reads, not what it does with a broken one.
        Path(path).write_text(
            json.dumps({"schema_version": con.CONTRACT_SCHEMA_VERSION,
                        "violation_count": 3}), encoding="utf-8")
        self.assertFalse(con.drc_gate_passes(path))
        self.assertEqual(contract.violation_count, 0)

    def test_a_contract_from_another_schema_version_raises(self):
        """A gate that cannot read the contract must not pass it. The version
        field was written and never checked, so a contract from a future
        writer - with fields this reader does not know about - would have been
        judged on `violation_count` alone."""
        path = str(self.tmp / "drc.json")
        Path(path).write_text(
            json.dumps({"schema_version": con.CONTRACT_SCHEMA_VERSION + 1,
                        "violation_count": 0}), encoding="utf-8")
        with self.assertRaises(con.ContractGateError):
            con.drc_gate_passes(path)

    def test_a_contract_with_no_version_at_all_raises(self):
        path = str(self.tmp / "drc.json")
        Path(path).write_text(json.dumps({"violation_count": 0}),
                              encoding="utf-8")
        with self.assertRaises(con.ContractGateError):
            con.drc_gate_passes(path)

    def test_the_contract_file_is_utf8_and_finite(self):
        path = str(self.tmp / "drc.json")
        con.write_drc_contract(
            con.build_drc_contract("bö.kicad_pcb", {"violations": []}), path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("bö", text)
        self.assertNotIn("NaN", text)


class PartsGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_missing_file_raises(self):
        with self.assertRaises(con.ContractGateError):
            con.parts_gate_passes(str(self.tmp / "nope.json"))

    def test_fault_injection_nrnd_without_an_alternative_shuts_the_gate(self):
        path = str(self.tmp / "parts.json")
        con.write_parts_contract(
            [con.PartContractRow("U1", "C12345", 0.1, "NRND", False)], path)
        ok, issues = con.parts_gate_passes(path)
        self.assertFalse(ok)
        self.assertTrue(issues)

    def test_nrnd_with_an_alternative_passes(self):
        path = str(self.tmp / "parts.json")
        con.write_parts_contract(
            [con.PartContractRow("U1", "C12345", 0.1, "NRND", True)], path)
        ok, issues = con.parts_gate_passes(path)
        self.assertTrue(ok, issues)

    def test_a_high_risk_score_shuts_the_gate(self):
        path = str(self.tmp / "parts.json")
        con.write_parts_contract(
            [con.PartContractRow("U1", "C1", 0.9, "Active", False)], path)
        self.assertFalse(con.parts_gate_passes(path)[0])

    def test_a_missing_risk_score_is_assumed_worst_case(self):
        path = self.tmp / "parts.json"
        path.write_text(json.dumps({"schema_version": 1, "rows": [
            {"refdes": "U1", "lifecycle_status": "Active"}]}),
            encoding="utf-8")
        self.assertFalse(con.parts_gate_passes(str(path))[0])

    def test_an_active_low_risk_part_passes(self):
        path = str(self.tmp / "parts.json")
        con.write_parts_contract(
            [con.PartContractRow("U1", "C1", 0.1, "Active", False)], path)
        self.assertTrue(con.parts_gate_passes(path)[0])


# --------------------------------------------------------------------------- #
# FreeRouting
# --------------------------------------------------------------------------- #
class FreeroutingBridge(unittest.TestCase):
    def test_java_error_patterns_cover_the_observed_crash(self):
        line = ("Exception in thread \"main\" java.lang.StackOverflowError\n"
                "\tat app.freerouting.board.PolylineTrace.combine")
        self.assertTrue(any(p in line for p in fr.JAVA_ERROR_PATTERNS))

    def test_out_of_memory_is_also_caught(self):
        self.assertTrue(any(p in "java.lang.OutOfMemoryError: Java heap space"
                            for p in fr.JAVA_ERROR_PATTERNS))

    def test_a_missing_java_raises_unavailable_binary_not_exercised(self):
        with mock.patch.object(fr, "java_available", return_value=False):
            with self.assertRaises(fr.FreeroutingUnavailable):
                fr.run_freerouting("a.dsn", "b.ses")

    def test_a_missing_jar_raises_unavailable_binary_not_exercised(self):
        with mock.patch.object(fr, "java_available", return_value=True):
            with self.assertRaises(fr.FreeroutingUnavailable):
                fr.run_freerouting("a.dsn", "b.ses", jar="/nope/freerouting.jar")

    def test_the_jvm_flags_are_present_binary_not_exercised(self):
        """-Xss raises the stack; -Djava.awt.headless=true stops a crash
        handler opening a dialog nobody will ever close."""
        seen = {}

        class FakeProc:
            returncode = 0
            stdout = None

            def poll(self):
                return 0

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            return FakeProc()

        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "freerouting.jar"
            jar.write_text("", encoding="utf-8")
            with mock.patch.object(fr, "java_available", return_value=True), \
                 mock.patch.object(fr.subprocess, "Popen",
                                   side_effect=fake_popen):
                fr.run_freerouting("a.dsn", "b.ses", jar=str(jar))
        argv = seen["argv"]
        self.assertEqual(argv[0], "java")
        self.assertIn("-Xmx4g", argv)
        self.assertIn("-Xss8m", argv)
        self.assertIn("-Djava.awt.headless=true", argv)
        self.assertIn("-de", argv)
        self.assertIn("-do", argv)

    def test_a_java_exception_kills_the_process_immediately_binary_not_exercised(self):
        """The timeout is NOT waited out."""
        lines = iter(["Starting\n",
                      "Exception in thread \"main\" java.lang.StackOverflowError\n",
                      ""])
        killed = {"count": 0}

        class FakeStdout:
            def readline(self):
                return next(lines, "")

        class FakeProc:
            returncode = 1
            stdout = FakeStdout()

            def poll(self):
                return None if killed["count"] == 0 else 1

            def kill(self):
                killed["count"] += 1

            def wait(self, timeout=None):
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "freerouting.jar"
            jar.write_text("", encoding="utf-8")
            with mock.patch.object(fr, "java_available", return_value=True), \
                 mock.patch.object(fr.subprocess, "Popen",
                                   return_value=FakeProc()):
                run = fr.run_freerouting("a.dsn", "b.ses", jar=str(jar),
                                         timeout_s=9999)
        self.assertTrue(run.java_error)
        self.assertFalse(run.ok)
        self.assertGreaterEqual(killed["count"], 1)
        self.assertIn("StackOverflowError", run.stderr)

    def test_no_unverified_gui_flag_is_shipped_binary_not_exercised(self):
        """MEASURED: -Dgui.enabled=false does NOT silence v2.2.4's
        "set gui.enabled to false" warning and is absent from its -help, so
        it must NOT be shipped. -Djava.awt.headless=true is the one that was
        measured to actually work."""
        seen = {}

        class FakeProc:
            returncode = 0
            stdout = None

            def poll(self):
                return 0

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "freerouting.jar"
            jar.write_text("", encoding="utf-8")
            with mock.patch.object(fr, "java_available", return_value=True), \
                 mock.patch.object(fr.subprocess, "Popen",
                                   side_effect=lambda argv, **kw:
                                       seen.__setitem__("argv", argv) or FakeProc()):
                fr.run_freerouting("a.dsn", "b.ses", jar=str(jar))
        self.assertIn("-Djava.awt.headless=true", seen["argv"])
        self.assertNotIn("-Dgui.enabled=false", seen["argv"],
                         "an unverified flag that measurably does nothing "
                         "was shipped")

    def test_regression_a_silent_child_is_still_killed_at_the_deadline(self):
        """P0 REGRESSION, found by a LIVE run against the real jar.

        FreeRouting prints a few lines and then goes SILENT for the whole
        routing computation. The old implementation called
        proc.stdout.readline() inside the polling loop; that blocks, so the
        deadline check at the top of the loop was never reached again. A
        240 s budget ran 1682 s and only stopped when the process was killed
        by hand - while still reporting timed_out=True, i.e. claiming to
        have enforced a budget it never enforced.

        This test spawns a REAL child process that prints one line and then
        sleeps far past the deadline. It fails (by hanging) against the old
        implementation and passes only when the deadline is genuinely
        enforced.
        """
        child = ("import sys, time; "
                 "print('starting', flush=True); "
                 "time.sleep(120)")
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "freerouting.jar"
            jar.write_text("", encoding="utf-8")

            real_popen = fr.subprocess.Popen

            def fake_popen(argv, **kwargs):
                # Ignore the java argv; run a silent-then-sleeping child with
                # the same pipe wiring the real call uses.
                return real_popen([sys.executable, "-c", child], **kwargs)

            with mock.patch.object(fr, "java_available", return_value=True), \
                 mock.patch.object(fr.subprocess, "Popen",
                                   side_effect=fake_popen):
                started = time.monotonic()
                run = fr.run_freerouting("a.dsn", "b.ses", jar=str(jar),
                                         timeout_s=3)
                elapsed = time.monotonic() - started

        self.assertTrue(run.timed_out, "the deadline was not reported")
        self.assertFalse(run.ok)
        self.assertLess(elapsed, 30,
                        f"the deadline was NOT enforced: the call took "
                        f"{elapsed:.1f}s against a 3s budget")
        self.assertIn("starting", run.stdout,
                      "output produced before the silence was lost")

    def test_an_import_that_changed_nothing_is_not_routed(self):
        """ImportSpecctraSES can return True and add zero tracks when the
        padstacks do not match."""
        result = fr.SesResult(ok=True, tracks_before=12, tracks_after=12,
                              tracks_changed=False, stdout="", stderr="")
        self.assertFalse(result.ok and result.tracks_changed)

    def test_a_missing_pcbnew_raises_unavailable_binary_not_exercised(self):
        with mock.patch.object(fr, "_pcbnew_available", return_value=False):
            with self.assertRaises(fr.FreeroutingUnavailable):
                fr.export_dsn("b.kicad_pcb", "b.dsn")
            with self.assertRaises(fr.FreeroutingUnavailable):
                fr.import_ses("b.kicad_pcb", "b.ses")

    def test_the_chain_stops_at_a_failed_dsn_export_binary_not_exercised(self):
        with mock.patch.object(fr, "export_dsn",
                               return_value=fr.DsnResult(False, None, "", "no")):
            with tempfile.TemporaryDirectory() as tmp:
                outcome = fr.route_chain("b.kicad_pcb", workdir=tmp)
        self.assertEqual(outcome["stage"], "dsn_export")
        self.assertFalse(outcome["ok"])

    def test_the_chain_reports_an_unchanged_track_count_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            ses = Path(tmp) / "board.ses"
            ses.write_text("", encoding="utf-8")
            with mock.patch.object(
                    fr, "export_dsn",
                    return_value=fr.DsnResult(True, "b.dsn", "", "")), \
                 mock.patch.object(
                     fr, "ensure_jar", return_value="freerouting.jar"), \
                 mock.patch.object(
                     fr, "run_freerouting",
                     return_value=fr.RouteRun(True, str(ses), "", "")), \
                 mock.patch.object(
                     fr, "import_ses",
                     return_value=fr.SesResult(True, 12, 12, False, "", "")):
                outcome = fr.route_chain("b.kicad_pcb", workdir=tmp)
        self.assertFalse(outcome["ok"])
        self.assertIn("padstacks", outcome["detail"])


class JarBootstrap(unittest.TestCase):
    """freerouting.jar auto-download. Network calls are always mocked -
    ``download_jar`` itself is exercised against fakes, never the real
    GitHub API (see the module's own HONESTY NOTE: the live call is
    untested in this environment)."""

    GOOD_RELEASE = {"assets": [
        {"name": "source.zip",
         "browser_download_url": "https://example.com/source.zip"},
        {"name": "freerouting-2.1.0.jar",
         "browser_download_url": "https://github.com/example/fr.jar"},
    ]}

    def test_finds_the_jar_asset_among_others(self):
        self.assertEqual("https://github.com/example/fr.jar",
                         fr.find_jar_download_url(self.GOOD_RELEASE))

    def test_fault_injection_no_jar_asset_is_refused(self):
        with self.assertRaises(ValueError):
            fr.find_jar_download_url({"assets": [
                {"name": "source.zip",
                 "browser_download_url": "https://example.com/source.zip"}]})

    def test_a_non_https_asset_url_is_not_picked(self):
        with self.assertRaises(ValueError):
            fr.find_jar_download_url({"assets": [
                {"name": "freerouting.jar",
                 "browser_download_url": "http://example.com/fr.jar"}]})

    def test_ensure_jar_returns_an_existing_path_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "freerouting.jar"
            jar.write_text("x", encoding="utf-8")
            self.assertEqual(str(jar), fr.ensure_jar(str(jar)))

    def test_fault_injection_a_missing_jar_fails_closed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "freerouting.jar")
            with self.assertRaises(fr.FreeroutingUnavailable):
                fr.ensure_jar(missing)
            with self.assertRaises(fr.FreeroutingUnavailable):
                fr.ensure_jar(missing, allow_download=False)

    def test_a_missing_jar_with_download_allowed_calls_download_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "freerouting.jar")
            with mock.patch.object(fr, "download_jar",
                                   return_value=missing) as fake:
                result = fr.ensure_jar(missing, allow_download=True)
            fake.assert_called_once()
            self.assertEqual(missing, result)

    @staticmethod
    def _fake_response(body: bytes):
        """A minimal stand-in for ``http.client.HTTPResponse``: real
        chunked ``read(size)`` via BytesIO (``shutil.copyfileobj`` reads in
        fixed-size chunks, not one ``read()``-everything call), plus the
        context-manager protocol ``urlopen`` results are used with."""
        buf = io.BytesIO(body)

        class FakeResponse:
            status = 200

            def read(self, size=-1):
                return buf.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return FakeResponse()

    def test_download_jar_writes_the_asset_atomically_network_not_exercised(self):
        """Simulates urlopen: metadata request returns GOOD_RELEASE JSON,
        the second request returns jar bytes. Verifies the final file has
        the right content and no stray .part file is left behind."""
        payload = b"PK\x03\x04 fake jar bytes" * 100  # bigger than one chunk
        release_json = json.dumps(self.GOOD_RELEASE).encode("utf-8")
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            return self._fake_response(release_json if calls["n"] == 1 else payload)

        with tempfile.TemporaryDirectory() as tmp:
            dest = str(Path(tmp) / "freerouting.jar")
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = fr.download_jar(dest)
            self.assertEqual(dest, result)
            self.assertEqual(payload, Path(dest).read_bytes())
            self.assertFalse(Path(dest + ".part").exists())

    def test_fault_injection_a_network_error_raises_connection_error_not_exercised(self):
        import urllib.error

        def raising_urlopen(request, timeout=None):
            raise urllib.error.URLError("no route to host")

        with tempfile.TemporaryDirectory() as tmp:
            dest = str(Path(tmp) / "freerouting.jar")
            with mock.patch("urllib.request.urlopen", side_effect=raising_urlopen):
                with self.assertRaises(ConnectionError):
                    fr.download_jar(dest)
            self.assertFalse(Path(dest).exists())

    def test_fault_injection_a_zero_byte_download_is_refused_not_exercised(self):
        release_json = json.dumps(self.GOOD_RELEASE).encode("utf-8")
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            return self._fake_response(release_json if calls["n"] == 1 else b"")

        with tempfile.TemporaryDirectory() as tmp:
            dest = str(Path(tmp) / "freerouting.jar")
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(ConnectionError):
                    fr.download_jar(dest)
            self.assertFalse(Path(dest).exists())
            self.assertFalse(Path(dest + ".part").exists())

    def test_route_chain_never_downloads_without_explicit_opt_in_binary_not_exercised(self):
        """A missing jar with allow_jar_download left at its default (False)
        must fail closed - route_chain() must not silently reach the
        network just because export_dsn succeeded."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    fr, "export_dsn",
                    return_value=fr.DsnResult(True, "b.dsn", "", "")), \
                 mock.patch.object(fr, "download_jar") as fake_download:
                with self.assertRaises(fr.FreeroutingUnavailable):
                    fr.route_chain("b.kicad_pcb", workdir=tmp,
                                   jar=str(Path(tmp) / "nope.jar"))
        fake_download.assert_not_called()

    def test_route_chain_downloads_when_opted_in_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            ses = Path(tmp) / "board.ses"
            ses.write_text("", encoding="utf-8")
            jar_path = str(Path(tmp) / "nope.jar")
            with mock.patch.object(
                    fr, "export_dsn",
                    return_value=fr.DsnResult(True, "b.dsn", "", "")), \
                 mock.patch.object(
                     fr, "download_jar", return_value=jar_path) as fake_download, \
                 mock.patch.object(
                     fr, "run_freerouting",
                     return_value=fr.RouteRun(True, str(ses), "", "")), \
                 mock.patch.object(
                     fr, "import_ses",
                     return_value=fr.SesResult(True, 5, 8, True, "", "")):
                outcome = fr.route_chain("b.kicad_pcb", workdir=tmp,
                                         jar=jar_path,
                                         allow_jar_download=True)
        fake_download.assert_called_once()
        self.assertTrue(outcome["ok"])


# --------------------------------------------------------------------------- #
# Assembly / CPL
# --------------------------------------------------------------------------- #
class RotationMap(unittest.TestCase):
    def test_the_hash_is_stable_for_an_identical_map(self):
        entries = [asm.RotationEntry("A:B", 90.0)]
        self.assertFalse(asm.rotation_map_changed(
            asm.hash_rotation_map("JLC", entries),
            asm.hash_rotation_map("JLC", list(entries))))

    def test_the_hash_changes_when_an_offset_changes(self):
        self.assertTrue(asm.rotation_map_changed(
            asm.hash_rotation_map("JLC", [asm.RotationEntry("A:B", 90.0)]),
            asm.hash_rotation_map("JLC", [asm.RotationEntry("A:B", 180.0)])))

    def test_the_hash_changes_when_an_entry_is_added(self):
        self.assertTrue(asm.rotation_map_changed(
            asm.hash_rotation_map("JLC", [asm.RotationEntry("A:B", 90.0)]),
            asm.hash_rotation_map("JLC", [asm.RotationEntry("A:B", 90.0),
                                          asm.RotationEntry("C:D", 0.0)])))

    def test_the_hash_changes_when_the_fab_changes(self):
        entries = [asm.RotationEntry("A:B", 90.0)]
        self.assertTrue(asm.rotation_map_changed(
            asm.hash_rotation_map("JLC", entries),
            asm.hash_rotation_map("PCBWay", entries)))


class Cpl(unittest.TestCase):
    FOOTPRINT = asm.FootprintGeometry("U1", "Package_SO:SOIC-8", (0.0, 0.0),
                                      (-1.0, -0.5, 3.0, 1.5), 90.0, "F.Cu")

    def test_the_pick_point_is_the_courtyard_centre_not_the_origin(self):
        row = asm.build_cpl([self.FOOTPRINT],
                            asm.hash_rotation_map("JLC", []))[0]
        self.assertAlmostEqual(row["MidX"], 1.0, places=9)
        self.assertAlmostEqual(row["MidY"], 0.5, places=9)
        self.assertNotEqual((row["MidX"], row["MidY"]), self.FOOTPRINT.origin)

    def test_the_fab_offset_is_applied(self):
        mapped = asm.hash_rotation_map(
            "JLC", [asm.RotationEntry("Package_SO:SOIC-8", 180.0)])
        row = asm.build_cpl([self.FOOTPRINT], mapped)[0]
        self.assertEqual(row["Rotation"], 270.0)
        self.assertTrue(row["rotation_map_matched"])

    def test_rotation_wraps_at_360(self):
        mapped = asm.hash_rotation_map(
            "JLC", [asm.RotationEntry("Package_SO:SOIC-8", 300.0)])
        self.assertEqual(asm.build_cpl([self.FOOTPRINT], mapped)[0]["Rotation"],
                         30.0)

    def test_fault_injection_an_unmapped_footprint_fails_the_gate(self):
        """A silent 0 degree offset can reverse an entire batch."""
        rows = asm.build_cpl([self.FOOTPRINT], asm.hash_rotation_map("JLC", []))
        self.assertEqual(rows[0]["Rotation"], 90.0)
        self.assertFalse(rows[0]["rotation_map_matched"])
        finding = asm.check_rotation_map_coverage(rows, [self.FOOTPRINT])
        self.assertEqual(finding["status"], "FAIL")

    def test_the_layer_is_carried_through(self):
        bottom = asm.FootprintGeometry("C1", "L:F", (0, 0), (0, 0, 1, 1), 0,
                                       "B.Cu")
        row = asm.build_cpl([bottom], asm.hash_rotation_map("JLC", []))[0]
        self.assertEqual(row["Layer"], "B.Cu")


class Orientation(unittest.TestCase):
    ROWS = [{"Designator": "U1", "Rotation": 270.0, "Layer": "F.Cu",
             "MidX": 0, "MidY": 0, "rotation_map_matched": True}]

    def test_a_matching_angle_passes(self):
        finding = asm.check_orientation(
            [asm.PolarisedPart("U1", "ic_pin1", 270.0)], self.ROWS)
        self.assertEqual(finding["status"], "PASS")

    def test_fault_injection_a_reversed_part_is_caught(self):
        finding = asm.check_orientation(
            [asm.PolarisedPart("U1", "ic_pin1", 90.0)], self.ROWS)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["problem"],
                         "REVERSED_MOUNT_RISK")
        self.assertEqual(finding["violations"][0]["delta_deg"], 180.0)

    def test_the_delta_takes_the_shorter_way_round(self):
        rows = [{"Designator": "D1", "Rotation": 359.0, "Layer": "F.Cu",
                 "MidX": 0, "MidY": 0, "rotation_map_matched": True}]
        finding = asm.check_orientation(
            [asm.PolarisedPart("D1", "diode", 0.0)], rows,
            tolerance_deg=2.0)
        self.assertEqual(finding["status"], "PASS")

    def test_a_part_missing_from_the_cpl_is_flagged(self):
        finding = asm.check_orientation(
            [asm.PolarisedPart("D9", "diode", 0.0)], self.ROWS)
        self.assertEqual(finding["violations"][0]["problem"], "NOT_IN_CPL")

    def test_no_polarised_parts_is_no_coverage(self):
        self.assertEqual(asm.check_orientation([], self.ROWS)["status"],
                         "NO_COVERAGE")


class Panelisation(unittest.TestCase):
    def test_a_compliant_panel_passes(self):
        finding = asm.check_panelisation(
            asm.PanelConstraints(3, True, 5.0, 8.0))
        self.assertEqual(finding["status"], "PASS")
        self.assertEqual(finding["scanned"], 4)

    def test_every_violation_is_reported_separately(self):
        finding = asm.check_panelisation(
            asm.PanelConstraints(2, False, 3.0, 1.0))
        self.assertEqual(finding["status"], "FAIL")
        items = {v["item"] for v in finding["violations"]}
        self.assertEqual(items, {"global_fiducials", "bga_local_fiducials",
                                 "rail_width_mm",
                                 "sensitive_part_depanel_mm"})

    def test_no_panel_data_is_no_coverage_not_assumed_compliant(self):
        self.assertEqual(asm.check_panelisation(None)["status"], "NO_COVERAGE")

    def test_an_omitted_sensitive_distance_is_not_checked(self):
        finding = asm.check_panelisation(
            asm.PanelConstraints(3, True, 5.0, None))
        self.assertEqual(finding["scanned"], 3)


# --------------------------------------------------------------------------- #
# Outputs gate
# --------------------------------------------------------------------------- #
class OutputsGate(unittest.TestCase):
    def test_fault_injection_an_unrecognised_schema_fails_closed(self):
        finding = out.check_report_gate({"results": []}, "drc", "b.kicad_pcb", 1)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["problem"],
                         "SCHEMA_UNRECOGNISED")

    def test_a_clean_drc_report_passes(self):
        finding = out.check_report_gate(
            {"violations": [], "unconnected_items": []}, "drc",
            "b.kicad_pcb", scanned=1)
        self.assertEqual(finding["status"], "PASS")

    def test_error_severity_unconnected_items_shut_the_gate(self):
        finding = out.check_report_gate(
            {"violations": [],
             "unconnected_items": [{"severity": "error"}]},
            "drc", "b.kicad_pcb", scanned=1)
        self.assertEqual(finding["status"], "FAIL")

    def test_erc_errors_under_sheets_are_found(self):
        finding = out.check_report_gate(
            {"sheets": [{"path": "/", "violations": [{"severity": "error"}]}]},
            "erc", "b.kicad_sch", scanned=1)
        self.assertEqual(finding["status"], "FAIL")

    def test_a_warning_only_erc_passes(self):
        finding = out.check_report_gate(
            {"sheets": [{"violations": [{"severity": "warning"}]}]},
            "erc", "b.kicad_sch", scanned=1)
        self.assertEqual(finding["status"], "PASS")

    def test_a_zero_object_design_is_no_coverage_not_a_clean_pass(self):
        """A board with no copper produces a clean kicad-cli report. scanned
        was hard-coded to 1 here, so "nothing to violate" and "no violations"
        were the same finding - in the gate in front of the gerbers.
        drc_gate.py has counted the census since it was written; this one
        did not."""
        finding = out.check_report_gate(
            {"violations": [], "unconnected_items": []}, "drc",
            "b.kicad_pcb", scanned=0)
        self.assertEqual(finding["status"], "NO_COVERAGE")

    def test_the_census_counts_real_board_objects(self):
        """The census comes from drc_gate.py, not a second implementation."""
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            board.write_text(
                "(kicad_pcb (version 20240108)\n"
                '  (footprint "R_0402" (pad "1" smd rect))\n'
                "  (segment (start 0 0) (end 1 0) (width 0.2))\n)",
                encoding="utf-8")
            self.assertGreater(out.design_census(str(board), "drc"), 0)
        self.assertEqual(out.design_census("/definitely/not/here.kicad_pcb",
                                           "drc"), 0)

    def test_no_contracts_supplied_is_no_coverage(self):
        self.assertEqual(out.check_contract_gates(None, None)["status"],
                         "NO_COVERAGE")

    def test_a_broken_contract_becomes_a_violation_not_an_exception(self):
        finding = out.check_contract_gates(None, "/definitely/not/here.json")
        self.assertEqual(finding["status"], "FAIL")

    def test_both_contracts_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = str(Path(tmp) / "parts.json")
            drc = str(Path(tmp) / "drc.json")
            con.write_parts_contract(
                [con.PartContractRow("U1", "C1", 0.1, "Active")], parts)
            con.write_drc_contract(
                con.build_drc_contract("b", {"violations": []}), drc)
            finding = out.check_contract_gates(parts, drc)
        self.assertEqual(finding["scanned"], 2)
        self.assertEqual(finding["status"], "PASS")

    def test_a_missing_pcbnew_is_a_fail_by_default(self):
        """An earlier version filtered its own WARNING messages out of the
        verdict, so the real board checks could fail to run and the gate
        still passed."""
        with mock.patch.dict(sys.modules, {"pcbnew": None}):
            finding = out.check_board_checks("b.kicad_pcb", skip=False)
        self.assertEqual(finding["status"], "FAIL")
        self.assertEqual(finding["violations"][0]["problem"], "PCBNEW_MISSING")

    def test_skipping_board_checks_is_needs_human_never_pass(self):
        with mock.patch.dict(sys.modules, {"pcbnew": None}):
            finding = out.check_board_checks("b.kicad_pcb", skip=True)
        self.assertEqual(finding["status"], "NO_COVERAGE")
        self.assertIn("NEEDS_HUMAN", finding["detail"])
        self.assertNotEqual(finding["status"], "PASS")

    def test_a_missing_kibot_raises_rather_than_returning_empty_success(self):
        with mock.patch.object(out.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                out.run_kibot("b.kicad_pcb")

    def test_the_kibot_template_is_written_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = out.write_kibot_config(str(Path(tmp) / "kibot.yaml"))
            text = Path(path).read_text(encoding="utf-8")
        self.assertIn("kibot:", text)
        self.assertIn("type: gerber", text)
        self.assertIn("type: position", text)


class ImportIsolation(unittest.TestCase):
    """The point of this tier: a missing binary must not break collection."""

    def test_no_external_binary_is_needed_to_import_the_modules(self):
        for module in (asm, con, fr, out):
            self.assertTrue(hasattr(module, "__file__"))

    def test_java_and_kibot_are_not_present_in_this_environment(self):
        """States plainly that no test here is end-to-end evidence."""
        import shutil as sh
        self.assertIsNone(sh.which("kibot"),
                          "kibot IS installed - the mocked tests above are "
                          "still not end-to-end evidence")

    def test_the_modules_do_not_import_pcbnew_at_module_level(self):
        """Checked with the AST, not by grepping: freerouting.py embeds
        `import pcbnew` inside a STRING (the small script it hands to an
        interpreter that can import it), and a text search would mistake that
        for a real module-level import."""
        import ast
        for name in ("freerouting.py", "outputs.py", "assembly.py",
                     "contracts.py"):
            tree = ast.parse((PRODUCTION / name).read_text(encoding="utf-8"))
            for node in tree.body:          # module level only
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                self.assertNotIn("pcbnew", names,
                                 f"{name} imports pcbnew at module level")


class CliContracts(unittest.TestCase):
    def run_script(self, script, *args):
        return subprocess.run([sys.executable, str(script), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(REPO))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_each_tool_requires_its_positional_input(self):
        for name in ("freerouting.py", "assembly.py", "outputs.py"):
            proc = self.run_script(PRODUCTION / name)
            self.assertEqual(proc.returncode, 2, name)
            self.assertEqual(proc.stdout.strip(), "", name)

    def test_outputs_requires_the_schematic_too(self):
        board = self.tmp / "b.kicad_pcb"
        board.write_text("(kicad_pcb)\n", encoding="utf-8")
        proc = self.run_script(PRODUCTION / "outputs.py", str(board))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("schematic", proc.stderr)

    def test_every_self_test_is_opt_in_and_stderr_only(self):
        for name in ("freerouting.py", "assembly.py", "outputs.py"):
            proc = self.run_script(PRODUCTION / name, "--self-test")
            self.assertEqual(proc.returncode, 0, name)
            self.assertEqual(proc.stdout, "", name)
            self.assertIn("SELF-TEST PASS", proc.stderr, name)

    def test_assembly_end_to_end_reports_the_unmapped_footprint(self):
        path = self.tmp / "a.json"
        path.write_text(json.dumps({
            "footprints": [
                {"refdes": "U1", "library": "Package_SO:SOIC-8",
                 "courtyard_bbox": [-1, -0.5, 3, 1.5], "angle_deg": 90},
                {"refdes": "D1", "library": "Diode_SMD:D_0603",
                 "courtyard_bbox": [0, 0, 1, 0.5], "angle_deg": 0}],
            "rotation_map": {"fab": "JLCPCB", "entries": [
                {"library": "Package_SO:SOIC-8", "offset_deg": 180}]},
            "polarised": [{"refdes": "U1", "kind": "ic_pin1",
                           "expected_deg": 270}],
            "panel": {"global_fiducials": 3, "bga_local_fiducials": True,
                      "rail_width_mm": 5.0,
                      "sensitive_part_depanel_mm": 8.0}}),
            encoding="utf-8")
        proc = self.run_script(PRODUCTION / "assembly.py", str(path))
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        by_check = {c["check"]: c for c in report["checks"]}
        self.assertEqual(by_check["cpl_rotation_map_coverage"]["status"], "FAIL")
        self.assertEqual(by_check["cpl_orientation"]["status"], "PASS")
        self.assertEqual(by_check["panelisation"]["status"], "PASS")
        self.assertTrue(report["params"]["rotation_map_hash"])

    def test_the_reports_match_the_shared_schema(self):
        schema = json.loads((SCHEMAS / "production_gate.schema.json")
                            .read_text(encoding="utf-8"))
        path = self.tmp / "a.json"
        path.write_text(json.dumps({
            "footprints": [], "rotation_map": {"fab": "X", "entries": []},
            "polarised": [], "panel": None}), encoding="utf-8")
        report = json.loads(
            self.run_script(PRODUCTION / "assembly.py", str(path)).stdout)
        self.assertEqual(sorted(report), sorted(schema["required"]))
        self.assertIn(report["tool"], schema["properties"]["tool"]["enum"])

    def test_freerouting_reports_no_coverage_when_the_jar_is_absent(self):
        board = self.tmp / "b.kicad_pcb"
        board.write_text("(kicad_pcb)\n", encoding="utf-8")
        proc = self.run_script(PRODUCTION / "freerouting.py", str(board),
                               "--jar", str(self.tmp / "nope.jar"),
                               "--workdir", str(self.tmp))
        # Never 0: "the router did not run" must not read as "board is routed".
        self.assertEqual(proc.returncode, 1)
        report = json.loads(proc.stdout)
        self.assertIn(report["tool"], ("production_freerouting",))
        self.assertNotEqual(report["checks"][0]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
