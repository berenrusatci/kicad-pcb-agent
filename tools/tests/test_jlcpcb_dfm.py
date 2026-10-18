#!/usr/bin/env python3
"""Tests for scripts/production/jlcpcb_dfm.py.

The real endpoint is NEVER exercised here (see the module's own STATUS
note: it has not been verified against a live JLCPCB account in this
environment). Every network-shaped test mocks ``urllib.request.urlopen``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS, TOOLS  # noqa: E402,F401

from scripts.production import jlcpcb_dfm as jd  # noqa: E402

SCRIPT = SCRIPTS / "production" / "jlcpcb_dfm.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class _NoEnv:
    """Context manager: neither JLCPCB_API_KEY nor JLCPCB_DFM_ENDPOINT set."""

    def __enter__(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in (jd.API_KEY_ENV, jd.ENDPOINT_ENV)}
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
        return False


class ResponseParsing(unittest.TestCase):
    def test_a_clean_response_is_ok(self):
        result = jd.parse_dfm_response({"pass": True, "issues": []})
        self.assertTrue(result.ok)
        self.assertEqual([], result.violations)

    def test_a_dirty_response_carries_its_issues(self):
        result = jd.parse_dfm_response({"pass": False, "issues": [
            {"code": "SILK_OVER_PAD", "message": "silk over pad",
             "severity": "warning"}]})
        self.assertFalse(result.ok)
        self.assertEqual(1, len(result.violations))
        self.assertEqual("SILK_OVER_PAD", result.violations[0].code)

    def test_a_response_with_no_verdict_carries_no_verdict(self):
        """This test used to assert the OPPOSITE - that an absent "pass" key
        with an empty issue list means ok=True - and pinned the bug in place.

        ``ok = bool(raw.get("pass", not violations))`` inferred acceptance
        from the absence of complaints, which is the "unmeasured read as
        zero" rule this bundle refuses everywhere else. A response that does
        not say whether the board passed says nothing about whether the board
        passed, and the module's own docstring already admits the shape is
        UNVERIFIED against a real JLCPCB account.
        """
        result = jd.parse_dfm_response({"issues": []})
        self.assertIsNone(result.ok)

    def test_an_explicit_refusal_survives_an_empty_issue_list(self):
        """check_dfm() read only the violation list, so a vendor saying
        pass=false with no itemised reason became a PASS finding."""
        result = jd.parse_dfm_response({"pass": False, "issues": []})
        self.assertIs(result.ok, False)

    def test_fault_injection_an_unrecognised_shape_is_refused(self):
        with self.assertRaises(jd.JlcpcbDfmUnavailable):
            jd.parse_dfm_response({"totally": "unexpected"})


class ConfigInjection(unittest.TestCase):
    def test_neither_key_nor_endpoint_is_hardcoded(self):
        with _NoEnv():
            self.assertIsNone(jd.api_key())
            self.assertIsNone(jd.api_endpoint())

    def test_the_environment_variables_are_honoured(self):
        with _NoEnv():
            os.environ[jd.API_KEY_ENV] = "k123"
            os.environ[jd.ENDPOINT_ENV] = "https://example.com/dfm"
            try:
                self.assertEqual("k123", jd.api_key())
                self.assertEqual("https://example.com/dfm", jd.api_endpoint())
            finally:
                del os.environ[jd.API_KEY_ENV]
                del os.environ[jd.ENDPOINT_ENV]

    def test_fault_injection_missing_config_never_sends_a_request_not_exercised(self):
        with _NoEnv():
            with mock.patch("urllib.request.urlopen") as fake_urlopen:
                with self.assertRaises(jd.JlcpcbDfmUnavailable):
                    jd.submit_for_dfm(__file__)
            fake_urlopen.assert_not_called()

    def test_fault_injection_a_missing_file_is_refused_before_any_config_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(jd.JlcpcbDfmUnavailable):
                jd.submit_for_dfm(str(Path(tmp) / "nope.zip"))


class NetworkOptIn(unittest.TestCase):
    def test_allow_network_false_is_no_coverage_and_never_calls_submit(self):
        with mock.patch.object(jd, "submit_for_dfm") as fake_submit:
            finding = jd.check_dfm("anything.zip", allow_network=False)
        self.assertEqual("NO_COVERAGE", finding["status"])
        fake_submit.assert_not_called()

    def test_allow_network_true_with_no_config_is_no_coverage_not_exercised(self):
        with _NoEnv():
            finding = jd.check_dfm(__file__, allow_network=True)
        self.assertEqual("NO_COVERAGE", finding["status"])

    def test_a_clean_submission_reports_pass_binary_not_exercised(self):
        with mock.patch.object(
                jd, "submit_for_dfm",
                return_value=jd.DfmResult(ok=True, violations=[])):
            finding = jd.check_dfm(__file__, allow_network=True)
        self.assertEqual("PASS", finding["status"])

    def test_a_dirty_submission_reports_fail_binary_not_exercised(self):
        with mock.patch.object(
                jd, "submit_for_dfm",
                return_value=jd.DfmResult(
                    ok=False,
                    violations=[jd.DfmViolation("X", "bad", "error")])):
            finding = jd.check_dfm(__file__, allow_network=True)
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(1, len(finding["violations"]))


class LiveCallShapeNotExercised(unittest.TestCase):
    """The HTTP plumbing itself, entirely mocked - proves the request is
    built and the response is parsed, proves NOTHING about the real API."""

    def test_a_successful_round_trip(self):
        class FakeResponse:
            status = 200

            def read(self):
                return json.dumps({"pass": True, "issues": []}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with _NoEnv():
            os.environ[jd.API_KEY_ENV] = "k123"
            os.environ[jd.ENDPOINT_ENV] = "https://example.com/dfm"
            try:
                with mock.patch("urllib.request.urlopen",
                               return_value=FakeResponse()):
                    result = jd.submit_for_dfm(__file__)
            finally:
                del os.environ[jd.API_KEY_ENV]
                del os.environ[jd.ENDPOINT_ENV]
        self.assertTrue(result.ok)

    def test_fault_injection_a_non_200_status_raises(self):
        class FakeResponse:
            status = 500

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with _NoEnv():
            os.environ[jd.API_KEY_ENV] = "k123"
            os.environ[jd.ENDPOINT_ENV] = "https://example.com/dfm"
            try:
                with mock.patch("urllib.request.urlopen",
                               return_value=FakeResponse()):
                    with self.assertRaises(jd.JlcpcbDfmUnavailable):
                        jd.submit_for_dfm(__file__)
            finally:
                del os.environ[jd.API_KEY_ENV]
                del os.environ[jd.ENDPOINT_ENV]


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        self.assertEqual([], jd.self_test())

    def test_self_test_flag_exits_zero(self):
        self.assertEqual(0, run("--self-test").returncode)

    def test_self_test_writes_nothing_to_stdout(self):
        proc = run("--self-test")
        self.assertEqual("", proc.stdout)


class CliContract(unittest.TestCase):
    def test_missing_positional_input_is_exit_2(self):
        proc = run()
        self.assertEqual(2, proc.returncode)

    def test_without_allow_network_the_report_is_no_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "fab.zip"
            zip_path.write_bytes(b"PK\x03\x04")
            proc = run(str(zip_path))
        self.assertEqual(1, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("NO_COVERAGE", report["status"])

    def test_stdout_is_pure_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "fab.zip"
            zip_path.write_bytes(b"PK\x03\x04")
            proc = run(str(zip_path))
        json.loads(proc.stdout)   # must not raise


if __name__ == "__main__":
    unittest.main()
