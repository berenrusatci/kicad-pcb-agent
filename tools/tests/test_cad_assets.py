#!/usr/bin/env python3
"""Tests for scripts/production/cad_assets.py.

The gate this file guards is the one that costs a board when it is skipped:
an internet symbol whose pin count does not match the datasheet, or a symbol
from one package variant paired with a footprint from another. Both pass ERC
and DRC.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "production"))

import cad_assets as mod  # noqa: E402

SYMBOL_3_PINS = mod.SAMPLE_SYMBOL
FOOTPRINT_3_PADS = mod.SAMPLE_FOOTPRINT


class PinReadingTests(unittest.TestCase):
    def test_a_pin_repeated_across_units_is_counted_once(self):
        """The sample symbol defines pin "1" in two unit bodies. Counting raw
        ``(pin`` forms would report 4 pins for a 3-pin part and fail the
        datasheet comparison for the wrong reason."""
        self.assertEqual(["1", "2", "EP"], mod.symbol_pin_numbers(SYMBOL_3_PINS))

    def test_the_exposed_pad_is_counted_not_filtered(self):
        """MASTER_RULEBOOK Faz 1 requires the thermal pad to be counted and
        tied to GND. Filtering it hides exactly the mismatch this checks."""
        self.assertIn("EP", mod.footprint_pad_numbers(FOOTPRINT_3_PADS))

    def test_a_number_token_outside_a_pin_is_not_a_pin(self):
        """CONSTRUCTED fault injection, not an observed file: the archive read
        these with ``\\(number\\s+"..."`` over the raw text, which cannot tell
        where the token sits. Reading the nesting makes the claim the check
        actually needs."""
        symbol = ('(kicad_symbol_lib (symbol "U1" (pin_names (number "99")) '
                  '(symbol "U1_1_1" (pin passive line (name "A") (number "1")))))')
        self.assertEqual(["1"], mod.symbol_pin_numbers(symbol))


class PinCountGateTests(unittest.TestCase):
    def test_a_matching_symbol_and_footprint_pass(self):
        finding = mod.check_pin_count(SYMBOL_3_PINS, 3, FOOTPRINT_3_PADS)
        self.assertEqual("PASS", finding["status"])

    def test_a_datasheet_mismatch_fails(self):
        finding = mod.check_pin_count(SYMBOL_3_PINS, 8)
        self.assertEqual("FAIL", finding["status"])
        self.assertIn("sembol_vs_datasheet", " ".join(finding["violations"]))

    def test_a_footprint_missing_the_exposed_pad_fails(self):
        """The variant mismatch: symbol from the exposed-pad part, footprint
        from the one without it. ERC and DRC both stay clean."""
        no_ep = '(footprint "PKG" (pad "1" smd rect (at 0 0)) (pad "2" smd rect (at 1 0)))'
        finding = mod.check_pin_count(SYMBOL_3_PINS, 3, no_ep)
        self.assertEqual("FAIL", finding["status"])
        joined = " ".join(finding["violations"])
        self.assertIn("footprint_vs_sembol", joined)
        self.assertIn("EP", joined)

    def test_a_symbol_with_no_pins_is_no_coverage_not_pass(self):
        finding = mod.check_pin_count("(kicad_symbol_lib)", 0)
        self.assertEqual("NO_COVERAGE", finding["status"])


class LifecycleGateTests(unittest.TestCase):
    def test_blocked_states_refuse_the_download(self):
        for state in ("NRND", "EOL", "Obsolete"):
            with self.subTest(state=state):
                allowed, reason = mod.lifecycle_gate("PART", state)
                self.assertFalse(allowed)
                self.assertIn("İstisnasız", reason)

    def test_an_unknown_lifecycle_is_allowed_but_says_confirm(self):
        for state in (None, "Unknown", "yeni bir şey"):
            with self.subTest(state=state):
                allowed, reason = mod.lifecycle_gate("PART", state)
                self.assertTrue(allowed)
                self.assertIn("CONFIRM", reason)

    def test_active_is_allowed_without_a_caveat(self):
        allowed, reason = mod.lifecycle_gate("PART", "Active")
        self.assertTrue(allowed)
        self.assertNotIn("CONFIRM", reason)


class DownloadAllowlistTests(unittest.TestCase):
    def test_https_on_a_listed_host_is_allowed(self):
        self.assertTrue(mod.download_allowed("https://api.snapeda.com/a.kicad_sym")[0])

    def test_plain_http_is_refused(self):
        ok, reason = mod.download_allowed("http://api.snapeda.com/a.kicad_sym")
        self.assertFalse(ok)
        self.assertIn("https", reason)

    def test_an_unlisted_host_is_refused(self):
        ok, reason = mod.download_allowed("https://cdn.example.com/a.kicad_sym")
        self.assertFalse(ok)
        self.assertIn("beyaz liste", reason)

    def test_the_tool_makes_no_network_call(self):
        """The rejection this port overturned was "network dependent". The
        replacement must actually not be: no HTTP client is imported."""
        source = (SCRIPTS / "production" / "cad_assets.py").read_text(encoding="utf-8")
        for banned in ("import urllib.request", "import requests", "urlopen(", "httpx"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)


class RegistrationTests(unittest.TestCase):
    def project(self, tmp):
        root = Path(tmp)
        symbol = root / "src" / "part.kicad_sym"
        symbol.parent.mkdir(parents=True)
        symbol.write_text(SYMBOL_3_PINS, encoding="utf-8")
        footprint = root / "src" / "part.kicad_mod"
        footprint.write_text(FOOTPRINT_3_PADS, encoding="utf-8")
        return root, symbol, footprint

    def test_an_unproven_asset_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, symbol, _ = self.project(tmp)
            with self.assertRaises(PermissionError):
                mod.register_assets(str(root / "proj"), "lib", str(symbol),
                                    pin_check_passed=False)

    def test_registration_creates_both_lib_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, symbol, footprint = self.project(tmp)
            proj = root / "proj"
            out = mod.register_assets(str(proj), "mylib", str(symbol), str(footprint),
                                      pin_check_passed=True)
            self.assertEqual(["sym-lib-table", "fp-lib-table"], out["registered"])
            self.assertIn('(name "mylib")', (proj / "sym-lib-table").read_text(encoding="utf-8"))
            self.assertIn("mylib.pretty", (proj / "fp-lib-table").read_text(encoding="utf-8"))
            self.assertEqual(["step"], out["missing"])

    def test_registering_twice_does_not_duplicate_the_nickname(self):
        """A duplicate nickname makes KiCad refuse to open the project."""
        with tempfile.TemporaryDirectory() as tmp:
            root, symbol, _ = self.project(tmp)
            proj = root / "proj"
            mod.register_assets(str(proj), "mylib", str(symbol), pin_check_passed=True)
            second = mod.register_assets(str(proj), "mylib", str(symbol), pin_check_passed=True)
            self.assertEqual([], second["registered"])
            table = (proj / "sym-lib-table").read_text(encoding="utf-8")
            self.assertEqual(1, table.count('(name "mylib")'))

    def test_a_corrupt_lib_table_raises_instead_of_appending_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "sym-lib-table"
            table.write_text("not an s-expression at all", encoding="utf-8")
            with self.assertRaises(ValueError):
                mod.add_lib_table_entry(str(table), "x", "uri")


class CliTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())

    def test_a_mismatch_exits_one_and_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            symbol = Path(tmp) / "part.kicad_sym"
            symbol.write_text(SYMBOL_3_PINS, encoding="utf-8")
            out = Path(tmp) / "report.json"
            code = mod.main([str(symbol), "--datasheet-pins", "8", "--json", str(out)])
            self.assertEqual(1, code)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("FAIL", report["findings"][0]["status"])

    def test_the_datasheet_pin_count_has_no_default(self):
        """A default would compare the symbol against itself and always pass."""
        with tempfile.TemporaryDirectory() as tmp:
            symbol = Path(tmp) / "part.kicad_sym"
            symbol.write_text(SYMBOL_3_PINS, encoding="utf-8")
            with self.assertRaises(SystemExit):
                mod.main([str(symbol)])

    def test_register_refuses_when_the_gate_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            symbol = Path(tmp) / "part.kicad_sym"
            symbol.write_text(SYMBOL_3_PINS, encoding="utf-8")
            with self.assertRaises(PermissionError):
                mod.main([str(symbol), "--datasheet-pins", "8", "--register",
                          "--project", str(Path(tmp) / "proj")])


if __name__ == "__main__":
    unittest.main()
