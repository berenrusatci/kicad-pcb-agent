#!/usr/bin/env python3
"""Tests for scripts/production/jlc_library.py.

Every ``_binary_not_exercised`` test replaces the JLC2KiCadLib subprocess
call with a stub or an availability check known to fail - none of them run
the real network-touching tool.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS, TOOLS  # noqa: E402,F401

from scripts.production import jlc_library as jl  # noqa: E402

SCRIPT = SCRIPTS / "production" / "jlc_library.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class CacheKey(unittest.TestCase):
    def test_a_write_then_a_read_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(jl.is_cached("C1", "lib", tmp, cache_root=tmp))
            jl._write_marker("C1", "lib", tmp, cache_root=tmp)
            self.assertTrue(jl.is_cached("C1", "lib", tmp, cache_root=tmp))

    def test_a_different_lcsc_code_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl._write_marker("C1", "lib", tmp, cache_root=tmp)
            self.assertFalse(jl.is_cached("C2", "lib", tmp, cache_root=tmp))

    def test_a_different_symbol_lib_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl._write_marker("C1", "lib_a", tmp, cache_root=tmp)
            self.assertFalse(jl.is_cached("C1", "lib_b", tmp, cache_root=tmp))

    def test_a_different_output_dir_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl._write_marker("C1", "lib", tmp, cache_root=tmp)
            self.assertFalse(jl.is_cached("C1", "lib", tmp + "_other",
                                          cache_root=tmp))


class DownloadComponent(unittest.TestCase):
    def test_a_cache_hit_never_checks_availability_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl._write_marker("C1", jl.DEFAULT_SYMBOL_LIB, tmp, cache_root=tmp)
            with mock.patch.object(jl, "jlc2kicadlib_available") as fake_avail:
                outcome = jl.download_component("C1", output_dir=tmp,
                                                 cache_root=tmp)
            fake_avail.assert_not_called()
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.from_cache)

    def test_force_bypasses_a_cache_hit_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl._write_marker("C1", jl.DEFAULT_SYMBOL_LIB, tmp, cache_root=tmp)
            with mock.patch.object(jl, "jlc2kicadlib_available",
                                   return_value=False):
                with self.assertRaises(jl.JlcLibraryUnavailable):
                    jl.download_component("C1", output_dir=tmp,
                                          cache_root=tmp, force=True)

    def test_fault_injection_a_missing_tool_is_refused_not_a_fake_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(jl, "jlc2kicadlib_available",
                                   return_value=False):
                with self.assertRaises(jl.JlcLibraryUnavailable):
                    jl.download_component("C1", output_dir=tmp, cache_root=tmp)

    def test_a_successful_run_writes_the_marker_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr="")
            with mock.patch.object(jl, "jlc2kicadlib_available",
                                   return_value=True), \
                 mock.patch.object(jl.subprocess, "run",
                                   return_value=fake_proc):
                outcome = jl.download_component("C1", output_dir=tmp,
                                                 cache_root=tmp)
            self.assertTrue(outcome.ok)
            self.assertFalse(outcome.from_cache)
            self.assertTrue(jl.is_cached("C1", jl.DEFAULT_SYMBOL_LIB, tmp,
                                         cache_root=tmp))

    def test_fault_injection_a_nonzero_exit_raises_and_does_not_cache_binary_not_exercised(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="LCSC code not found")
            with mock.patch.object(jl, "jlc2kicadlib_available",
                                   return_value=True), \
                 mock.patch.object(jl.subprocess, "run",
                                   return_value=fake_proc):
                with self.assertRaises(jl.JlcLibraryUnavailable):
                    jl.download_component("C1", output_dir=tmp, cache_root=tmp)
            self.assertFalse(jl.is_cached("C1", jl.DEFAULT_SYMBOL_LIB, tmp,
                                          cache_root=tmp))

    def test_the_real_verified_cli_flags_are_used_binary_not_exercised(self):
        """-dir/-symbol_lib/--skip_existing were read from JLC2KiCadLib's
        own published --help output, not guessed - pin them here."""
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            return subprocess.CompletedProcess(args=command, returncode=0,
                                               stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(jl, "jlc2kicadlib_available",
                                   return_value=True), \
                 mock.patch.object(jl.subprocess, "run", side_effect=fake_run):
                jl.download_component("C99887", output_dir=tmp,
                                      symbol_lib="my_lib", cache_root=tmp)
        command = seen["command"]
        self.assertIn("-dir", command)
        self.assertIn(tmp, command)
        self.assertIn("-symbol_lib", command)
        self.assertIn("my_lib", command)
        self.assertIn("--skip_existing", command)
        self.assertIn("C99887", command)


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        self.assertEqual([], jl.self_test())

    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)


class CliContract(unittest.TestCase):
    def test_missing_positional_input_is_exit_2(self):
        proc = run()
        self.assertEqual(2, proc.returncode)

    def test_a_missing_tool_reports_no_coverage_not_a_fake_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run("C12345", "--output-dir", tmp, "--cache-root", tmp)
        self.assertEqual(1, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("NO_COVERAGE", report["status"])

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run("C12345", "--output-dir", tmp, "--cache-root", tmp)
        json.loads(proc.stdout)   # must not raise

    def test_a_cached_part_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl._write_marker("C12345", jl.DEFAULT_SYMBOL_LIB, tmp, cache_root=tmp)
            proc = run("C12345", "--output-dir", tmp, "--cache-root", tmp)
        self.assertEqual(0, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("PASS", report["status"])
        self.assertTrue(report["from_cache"])


if __name__ == "__main__":
    unittest.main()
