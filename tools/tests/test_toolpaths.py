#!/usr/bin/env python3
"""Tests for common/toolpaths.py (ported from test_arac_yollari.py).

The kicad-cli resolver tests stayed with common/kicad_cli.py, which owns that
logic; what is tested here is the KiCad embedded-python resolver, the poppler
resolver and the health matrix — including the property the source cared most
about: one missing tool must never stop the others from being checked.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401

from common import toolpaths  # noqa: E402
from common.toolpaths import (  # noqa: E402
    KICAD_PYTHON_ENV,
    POPPLER_BIN_ENV,
    ToolStatus,
    _command_status,
    _python_import_status,
    check_all_tools,
    find_kicad_python,
    find_pdftocairo,
)


class KicadPythonResolver(unittest.TestCase):
    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            interpreter = Path(tmp) / "python.exe"
            interpreter.touch()
            with patch.dict(os.environ, {KICAD_PYTHON_ENV: "/env/python"}):
                self.assertEqual(str(interpreter),
                                 find_kicad_python(str(interpreter)))

    def test_env_var_is_used_when_no_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            interpreter = Path(tmp) / "python.exe"
            interpreter.touch()
            with patch.dict(os.environ, {KICAD_PYTHON_ENV: str(interpreter)}):
                self.assertEqual(str(interpreter), find_kicad_python())

    def test_no_candidate_raises_with_actionable_message(self):
        env = {k: v for k, v in os.environ.items() if k != KICAD_PYTHON_ENV}
        # _kicad_python_candidates, cunku find_kicad_python ARTIK onu cagiriyor:
        # Windows-only yardimci POSIX adaylariyla birlestirildi. Yalniz Windows
        # tarafini bosaltmak Linux'ta gercek bir /usr/... adayini geride birakir,
        # fonksiyon onu dondurur ve test "hic aday yok" halini hic olcmez.
        with patch.dict(os.environ, env, clear=True), \
             patch.object(toolpaths, "_kicad_python_candidates",
                          return_value=[]):
            with self.assertRaises(FileNotFoundError) as caught:
                find_kicad_python()
        self.assertIn(KICAD_PYTHON_ENV, str(caught.exception))

    def test_requested_but_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_kicad_python("/kesinlikle/yok/python.exe")


class PdftocairoResolver(unittest.TestCase):
    def test_explicit_path_beats_path_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "pdftocairo"
            exe.touch()
            with patch.object(toolpaths.shutil, "which",
                              return_value="PATH-PDFTOCAIRO"):
                self.assertEqual(str(exe), find_pdftocairo(str(exe)))

    def test_env_var_beats_path_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "pdftocairo"
            exe.touch()
            with patch.dict(os.environ, {POPPLER_BIN_ENV: str(exe)}), \
                 patch.object(toolpaths.shutil, "which",
                              return_value="PATH-PDFTOCAIRO"):
                self.assertEqual(str(exe), find_pdftocairo())

    def test_path_lookup_is_used_as_a_fallback(self):
        env = {k: v for k, v in os.environ.items() if k != POPPLER_BIN_ENV}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(toolpaths.shutil, "which", return_value="/usr/bin/x"):
            self.assertEqual("/usr/bin/x", find_pdftocairo())

    def test_no_candidate_raises_with_install_instructions(self):
        env = {k: v for k, v in os.environ.items() if k != POPPLER_BIN_ENV}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(toolpaths.shutil, "which", return_value=None), \
             patch.object(toolpaths, "_windows_poppler_candidates",
                          return_value=[]):
            with self.assertRaises(FileNotFoundError) as caught:
                find_pdftocairo()
        self.assertIn("poppler", str(caught.exception))


class SingleToolStatus(unittest.TestCase):
    def test_missing_tool_returns_fail_instead_of_raising(self):
        """One missing tool must not abort the whole preflight."""
        with patch.object(toolpaths.shutil, "which", return_value=None):
            status = _command_status("Sahte Araç", ["olmayan-arac", "--version"],
                                     "madde X")
        self.assertIs(False, status.ok)
        self.assertIn("bulunamadı", status.detail)
        self.assertEqual("madde X", status.install_hint)

    def test_successful_command_passes(self):
        with patch.object(toolpaths.shutil, "which", return_value="/usr/bin/git"), \
             patch.object(toolpaths.subprocess, "run") as fake_run:
            fake_run.return_value = subprocess.CompletedProcess(
                ["git", "--version"], 0, stdout="git version 2.54.0\n", stderr="")
            status = _command_status("git", ["git", "--version"], "madde 9")
        self.assertIs(True, status.ok)
        self.assertIn("git version", status.detail)

    def test_timeout_fails_without_crashing(self):
        with patch.object(toolpaths.shutil, "which", return_value="/usr/bin/x"), \
             patch.object(toolpaths.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("x", 5)):
            status = _command_status("X", ["x", "--version"], "madde X")
        self.assertIs(False, status.ok)
        self.assertIn("çalıştırılamadı", status.detail)

    def test_real_import_passes(self):
        """``os`` exists in every Python install — real subprocess run."""
        self.assertIs(True, _python_import_status("os modülü", "os",
                                                  "madde X").ok)

    def test_missing_import_fails(self):
        status = _python_import_status("Sahte",
                                       "kesinlikle_var_olmayan_modul_xyz",
                                       "madde X")
        self.assertIs(False, status.ok)
        self.assertTrue("ModuleNotFoundError" in status.detail
                        or "No module named" in status.detail, status.detail)

    def test_fail_line_shows_the_install_hint(self):
        line = ToolStatus("X", False, "bulunamadı", "poppler-utils kurulumu").line()
        self.assertIn("poppler-utils kurulumu", line)
        self.assertIn("FAIL", line)

    def test_pass_line_hides_the_install_hint(self):
        line = ToolStatus("X", True, "1.0", "poppler-utils kurulumu").line()
        self.assertIn("PASS", line)
        self.assertNotIn("poppler-utils kurulumu", line)


class HealthMatrix(unittest.TestCase):
    def test_one_missing_tool_does_not_block_the_others(self):
        """The OPPOSITE of a bash ``A && B && C`` chain: even with the first
        tool missing, every later tool is still checked."""
        with patch.object(toolpaths, "kicad_cli_version",
                          side_effect=FileNotFoundError("yok")):
            statuses = check_all_tools()
        names = {s.name for s in statuses}
        self.assertIn("KiCad CLI", names)
        self.assertIn("git", names)
        self.assertIn("uv", names)
        self.assertGreaterEqual(len(statuses), 8)
        by_name = {s.name: s for s in statuses}
        self.assertIs(False, by_name["KiCad CLI"].ok)

    def test_kicad_cli_argument_is_forwarded(self):
        with patch.object(toolpaths, "kicad_cli_version") as fake:
            fake.return_value = ("/yol/kicad-cli", "10.0.4")
            check_all_tools(kicad_cli="/ozel/yol")
        fake.assert_called_once_with("/ozel/yol")

    def test_kicad_cli_resolution_is_not_duplicated(self):
        """The resolver lives in common/kicad_cli.py; toolpaths must CALL it,
        never re-implement it."""
        source = Path(toolpaths.__file__).read_text(encoding="utf-8")
        self.assertNotIn('which("kicad-cli")', source)
        self.assertIn("from .kicad_cli import", source)

    def test_version_check_uses_the_shared_resolver(self):
        with patch.object(toolpaths, "_require_cli",
                          return_value="/bin/kicad-cli") as resolver, \
             patch.object(toolpaths.subprocess, "run") as fake_run:
            fake_run.return_value = subprocess.CompletedProcess(
                ["kicad-cli", "--version"], 0, stdout="10.0.4\n", stderr="")
            path, version = toolpaths.kicad_cli_version()
        resolver.assert_called_once_with(None)
        self.assertEqual("/bin/kicad-cli", path)
        self.assertEqual("10.0.4", version)

    def test_version_check_fails_loudly_on_a_bad_exit(self):
        with patch.object(toolpaths, "_require_cli",
                          return_value="/bin/kicad-cli"), \
             patch.object(toolpaths.subprocess, "run") as fake_run:
            fake_run.return_value = subprocess.CompletedProcess(
                ["kicad-cli", "--version"], 3, stdout="", stderr="boom")
            with self.assertRaises(RuntimeError):
                toolpaths.kicad_cli_version()


if __name__ == "__main__":
    unittest.main()
