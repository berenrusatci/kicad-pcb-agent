#!/usr/bin/env python3
"""Tests for scripts/report/capability_inventory.py.

Focuses on the four AST-based columns added in this port (security status,
internal/external dependencies, AI contract) plus the pre-existing behaviour
the port task required NOT to break (category matching, status detection,
git log reading, test discovery) - each gets at least a smoke test so a
future edit cannot silently drop one.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS  # noqa: E402

REPORT_SCRIPTS = SCRIPTS / "report"
sys.path.insert(0, str(REPORT_SCRIPTS))

import capability_inventory as mod  # noqa: E402


# --------------------------------------------------------------------------- #
# NEW: security status
# --------------------------------------------------------------------------- #
class SecurityStatusTests(unittest.TestCase):
    def test_no_pattern_is_na(self):
        self.assertEqual("N/A", mod.determine_security_status("def f():\n    return 1\n"))

    def test_raise_notimplementederror_is_fail_closed(self):
        source = "def f():\n    raise NotImplementedError('todo')\n"
        self.assertEqual("Güvenli İskelet (Fail-Closed)", mod.determine_security_status(source))

    def test_raise_exception_is_fail_closed(self):
        source = "def f():\n    raise Exception('not done')\n"
        self.assertEqual("Güvenli İskelet (Fail-Closed)", mod.determine_security_status(source))

    def test_trivial_body_plus_mock_keyword_is_risky(self):
        source = '"""this is a mock module"""\ndef f():\n    pass\n'
        self.assertEqual("Riskli Mock (Sessiz Hata)", mod.determine_security_status(source))

    def test_trivial_body_without_mock_keyword_is_not_flagged_risky(self):
        """A trivial body alone (e.g. a legitimate constant getter) must
        not be flagged risky without the mock/fake keyword signal too."""
        source = "def f():\n    return True\n"
        self.assertEqual("N/A", mod.determine_security_status(source))

    def test_fail_closed_takes_priority_over_risky_mock(self):
        source = ('"""mock module"""\n'
                 'def a():\n    pass\n'
                 'def b():\n    raise NotImplementedError\n')
        self.assertEqual("Güvenli İskelet (Fail-Closed)", mod.determine_security_status(source))

    def test_syntax_error_is_reported_not_raised(self):
        self.assertIn("N/A", mod.determine_security_status("def f(:\n"))


# --------------------------------------------------------------------------- #
# NEW: internal dependency radar
# --------------------------------------------------------------------------- #
class InternalDependenciesTests(unittest.TestCase):
    PROJECT_MODULES = {"kicad_koprusu", "hata_hafizasi", "common_helpers"}

    def test_project_modules_are_found(self):
        source = "import os\nimport kicad_koprusu\nfrom hata_hafizasi import X\n"
        self.assertEqual(["hata_hafizasi", "kicad_koprusu"],
                         mod.find_internal_dependencies(source, self.PROJECT_MODULES))

    def test_stdlib_is_excluded(self):
        source = "import os\nimport sys\nimport json\n"
        self.assertEqual([], mod.find_internal_dependencies(source, self.PROJECT_MODULES))

    def test_third_party_not_in_project_is_excluded(self):
        source = "import numpy\nimport scipy\n"
        self.assertEqual([], mod.find_internal_dependencies(source, self.PROJECT_MODULES))

    def test_relative_import_without_module_name_does_not_crash(self):
        source = "from . import x\n"
        self.assertEqual([], mod.find_internal_dependencies(source, self.PROJECT_MODULES))

    def test_syntax_error_returns_empty_not_raises(self):
        self.assertEqual([], mod.find_internal_dependencies("def f(:\n", self.PROJECT_MODULES))


# --------------------------------------------------------------------------- #
# NEW: external tool/network dependency scan
# --------------------------------------------------------------------------- #
class ExternalDependenciesTests(unittest.TestCase):
    def test_known_binary_is_named(self):
        source = 'import subprocess\nsubprocess.run(["ngspice", "-b", "x.cir"])\n'
        self.assertEqual("subprocess(ngspice)", mod.find_external_dependencies(source))

    def test_unrecognised_binary_falls_back_to_generic_subprocess(self):
        source = 'import subprocess\nsubprocess.run(["some_random_tool"])\n'
        self.assertEqual("subprocess", mod.find_external_dependencies(source))

    def test_http_request_is_detected(self):
        source = "import requests\nrequests.get('http://example.com')\n"
        self.assertEqual("http_request", mod.find_external_dependencies(source))

    def test_both_kinds_combine(self):
        source = ('import subprocess, requests\n'
                 'subprocess.run(["kicad-cli", "pcb", "drc"])\n'
                 'requests.post("http://x")\n')
        result = mod.find_external_dependencies(source)
        self.assertIn("subprocess(kicad-cli)", result)
        self.assertIn("http_request", result)

    def test_nothing_found_is_yok(self):
        self.assertEqual("Yok", mod.find_external_dependencies("def f():\n    return 1\n"))

    def test_syntax_error_is_reported_not_raised(self):
        self.assertIn("N/A", mod.find_external_dependencies("def f(:\n"))


# --------------------------------------------------------------------------- #
# NEW: AI contract detection
# --------------------------------------------------------------------------- #
class AiContractTests(unittest.TestCase):
    def test_pydantic_basemodel_is_detected(self):
        source = "from pydantic import BaseModel\nclass X(BaseModel):\n    y: int\n"
        self.assertEqual("Evet", mod.has_ai_contract(source))

    def test_typed_dict_is_detected(self):
        source = "from typing import TypedDict\nclass X(TypedDict):\n    y: int\n"
        self.assertEqual("Evet", mod.has_ai_contract(source))

    def test_dataclass_decorator_is_detected(self):
        source = "from dataclasses import dataclass\n@dataclass\nclass X:\n    y: int\n"
        self.assertEqual("Evet", mod.has_ai_contract(source))

    def test_plain_class_is_not_a_contract(self):
        self.assertEqual("Hayır", mod.has_ai_contract("class X:\n    pass\n"))

    def test_syntax_error_is_hayir_not_raised(self):
        self.assertEqual("Hayır", mod.has_ai_contract("def f(:\n"))


# --------------------------------------------------------------------------- #
# Pre-existing behaviour that must NOT be broken by this port
# --------------------------------------------------------------------------- #
class PreExistingBehaviourTests(unittest.TestCase):
    def test_category_matches_by_filename_keyword(self):
        self.assertEqual("routing", mod.determine_category("grid_astar.py", ""))

    def test_category_falls_back_to_other(self):
        self.assertEqual("diğer", mod.determine_category("completely_unrelated_name.py", ""))

    def test_skeleton_marker_is_detected(self):
        # Dotted capital İ (not plain ASCII "I") - matches how the real
        # source docstrings spell it, and is exactly the turkish_lower()
        # case this module exists to get right (see its own test below).
        status, source = mod.determine_status('"""Bu dosya bir İSKELET."""\n')
        self.assertEqual("İskelet", status)
        self.assertEqual("marker", source)

    def test_status_without_marker_is_an_automatic_estimate(self):
        status, source = mod.determine_status("def f():\n    pass\n")
        self.assertEqual("otomatik_tahmin", source)


class StatusMarkerScopeTests(unittest.TestCase):
    """REGRESSION: the marker scan used to read the WHOLE source, so this
    very module flagged ITSELF as a skeleton (it defines the marker word),
    and 12 empty package markers were reported as unfinished work."""

    def test_regression_a_marker_word_in_the_BODY_is_not_a_marker(self):
        source = ('"""A completely finished module."""\n'
                  '_SKELETON_MARKERS = ("iskelet",)\n'
                  'def a():\n    return 1\n'
                  'def b():\n    return 2\n'
                  'def c():\n    return 3\n')
        status, status_source = mod.determine_status(source)
        self.assertNotEqual("marker", status_source,
                            "a marker word in the body was read as a declaration")
        self.assertNotEqual("İskelet", status)

    def test_regression_this_module_does_not_flag_itself_as_a_skeleton(self):
        """The self-referential case, run against the REAL file."""
        real = Path(mod.__file__)
        status, status_source = mod.determine_status(
            real.read_text(encoding="utf-8"), Path("tools/scripts/report/capability_inventory.py"))
        self.assertNotEqual("İskelet", status,
                            "capability_inventory.py still flags itself")

    def test_a_marker_in_the_DOCSTRING_is_still_honoured(self):
        status, status_source = mod.determine_status(
            '"""Bu dosya bir İSKELET."""\n' + "def f():\n    return 1\n" * 1)
        self.assertEqual("İskelet", status)
        self.assertEqual("marker", status_source)

    def test_an_empty_package_marker_is_not_called_a_skeleton(self):
        status, status_source = mod.determine_status(
            '"""scripts/board package marker."""\n', Path("tools/scripts/board/__init__.py"))
        self.assertEqual("Paket işaretçisi", status)
        self.assertEqual("dosya_turu", status_source)

    def test_a_test_fixture_is_not_called_a_skeleton(self):
        status, status_source = mod.determine_status(
            '"""Identity-only fixture."""\n', Path("tools/fixtures/gate_receipt/x.py"))
        self.assertEqual("Test altyapısı", status)
        self.assertEqual("dosya_turu", status_source)

    def test_a_real_module_path_still_gets_a_real_status(self):
        """The file-type shortcut must not swallow ordinary modules."""
        status, status_source = mod.determine_status(
            '"""A real tool."""\n' + "def f():\n    return 1\n" * 100,
            Path("tools/scripts/board/real_tool.py"))
        self.assertEqual("Var", status)
        self.assertEqual("otomatik_tahmin", status_source)

    def test_regression_naming_the_status_vocabulary_is_not_a_declaration(self):
        """This module's own docstring must list its status values without
        that being read as a self-declaration."""
        status, status_source = mod.determine_status(
            '"""``status`` (Var/Kısmen var/İskelet) is a ROUGH ESTIMATE."""\n'
            + "def f():\n    return 1\n" * 100)
        self.assertNotEqual("İskelet", status)
        self.assertNotEqual("marker", status_source)

    def test_turkish_lower_handles_the_dotted_i_trap(self):
        # Plain str.lower() fails this exact case (İ -> i + combining dot).
        self.assertIn("değil", mod.turkish_lower("DEĞİL"))

    def test_summary_extraction_skips_separator_lines(self):
        source = '"""\nTitle\n=====\nReal summary sentence here.\n"""\n'
        summary = mod.extract_summary(source)
        self.assertIn("Real summary sentence", summary)

    def test_real_or_mock_flags_mock_keyword(self):
        status, findings = mod.scan_real_or_mock("# this is a mock implementation\n")
        self.assertEqual("KONTROL EDİLMELİ", status)
        self.assertTrue(findings)

    def test_real_or_mock_clean_source_is_unflagged(self):
        status, findings = mod.scan_real_or_mock("def f():\n    return compute(x)\n")
        self.assertEqual("işaret yok (otomatik tarama)", status)
        self.assertEqual([], findings)


class TestMatchingTests(unittest.TestCase):
    """REGRESSION: matching only ``test_<stem>.py`` reported 55 of 97 modules
    as untested when only ONE genuinely was - because this suite GROUPS its
    tests (test_board_checks.py covers four board modules, test_common.py
    eleven common/ modules). A 95 % false-positive rate makes the column
    worse than useless: a real gap cannot be seen in the noise."""

    def _tree(self, tmp):
        root = Path(tmp)
        (root / "tools" / "scripts").mkdir(parents=True)
        (root / "tools" / "tests").mkdir(parents=True)
        return root

    def test_a_grouped_suite_counts_as_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            module = root / "tools" / "scripts" / "widget.py"
            module.write_text("def f():\n    return 1\n", encoding="utf-8")
            # No test_widget.py - covered by a differently-named suite.
            (root / "tools" / "tests" / "test_group.py").write_text(
                "from scripts.widget import f\n", encoding="utf-8")
            mod._IMPORT_REFERENCE_CACHE.clear()
            self.assertIsNotNone(mod._find_test_file(module, root))

    def test_a_cli_run_as_a_subprocess_counts_as_coverage(self):
        """test_tool_registry.py runs SCRIPTS / 'sync_tool_registry.py' and
        never imports it, yet genuinely tests it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            module = root / "tools" / "scripts" / "cli_tool.py"
            module.write_text("def main():\n    return 0\n", encoding="utf-8")
            (root / "tools" / "tests" / "test_runner.py").write_text(
                'SCRIPT = SCRIPTS / "cli_tool.py"\n', encoding="utf-8")
            mod._IMPORT_REFERENCE_CACHE.clear()
            self.assertIsNotNone(mod._find_test_file(module, root))

    def test_fault_injection_a_bare_word_mention_is_NOT_coverage(self):
        """det_uuid("rewire", ...) passes a tool NAME as an argument without
        testing rewire.py - a loose text search called that 'tested'."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            module = root / "tools" / "scripts" / "rewire.py"
            module.write_text("def f():\n    return 1\n", encoding="utf-8")
            (root / "tools" / "tests" / "test_other.py").write_text(
                'det_uuid("rewire", "/tmp/b.kicad_sch", "w1")\n', encoding="utf-8")
            mod._IMPORT_REFERENCE_CACHE.clear()
            self.assertIsNone(mod._find_test_file(module, root),
                              "a bare word mention was counted as a test")

    def test_a_genuinely_untested_module_is_still_reported(self):
        """The fix must not turn every module green."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            module = root / "tools" / "scripts" / "lonely.py"
            module.write_text("def f():\n    return 1\n", encoding="utf-8")
            (root / "tools" / "tests" / "test_group.py").write_text(
                "from scripts.widget import f\n", encoding="utf-8")
            mod._IMPORT_REFERENCE_CACHE.clear()
            self.assertIsNone(mod._find_test_file(module, root))

    def test_package_markers_are_excluded_from_the_lookup(self):
        """Every __init__.py shares the stem '__init__', so one test naming
        it would otherwise mark all of them tested at once."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            marker = root / "tools" / "scripts" / "__init__.py"
            marker.write_text('"""marker."""\n', encoding="utf-8")
            (root / "tools" / "tests" / "test_any.py").write_text(
                'EXCLUDE = "__init__.py"\n', encoding="utf-8")
            mod._IMPORT_REFERENCE_CACHE.clear()
            self.assertIsNone(mod._find_test_file(marker, root))

    def test_the_direct_name_convention_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            module = root / "tools" / "scripts" / "widget.py"
            module.write_text("def f():\n    return 1\n", encoding="utf-8")
            direct = root / "tools" / "tests" / "test_widget.py"
            direct.write_text("import widget\n", encoding="utf-8")
            mod._IMPORT_REFERENCE_CACHE.clear()
            self.assertEqual(direct, mod._find_test_file(module, root))


class FindModuleFilesTests(unittest.TestCase):
    def test_recursive_scan_excludes_test_files_and_junk_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "real_module.py").write_text("x = 1\n", encoding="utf-8")
            (root / "pkg" / "test_real_module.py").write_text("x = 1\n", encoding="utf-8")
            (root / ".venv").mkdir()
            (root / ".venv" / "site_module.py").write_text("x = 1\n", encoding="utf-8")

            files = mod.find_module_files(root)
            names = {f.name for f in files}
            self.assertIn("real_module.py", names)
            self.assertNotIn("test_real_module.py", names)
            self.assertNotIn("site_module.py", names)


class FullScanIntegrationTests(unittest.TestCase):
    """Builds a tiny two-file fixture repo and scans it end to end - proves
    the new columns and the pre-existing ones show up together in one
    record without the old fields being dropped."""

    def test_scan_produces_a_complete_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widget_router.py").write_text(
                '"""Routes widgets. Kısmen var - only handles the simple case."""\n'
                "from dataclasses import dataclass\n"
                "import subprocess\n\n"
                "@dataclass\n"
                "class WidgetResult:\n"
                "    ok: bool\n\n"
                "def route(x):\n"
                "    subprocess.run(['kicad-cli', 'pcb', 'drc'])\n"
                "    return WidgetResult(True)\n",
                encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_widget_router.py").write_text(
                "def test_route():\n    assert True\n"
                "def test_route_again():\n    assert True\n",
                encoding="utf-8")

            records = mod.scan_repo(root, test_results=None)
            self.assertEqual(1, len(records))
            r = records[0]
            self.assertEqual("widget_router.py", r.module_name)
            self.assertEqual("routing", r.category)
            self.assertTrue(r.has_test_file)
            self.assertEqual(2, r.test_function_count)
            self.assertEqual("olculemedi", r.test_status)  # test_results=None
            self.assertEqual("Evet", r.has_ai_contract)
            self.assertIn("subprocess(kicad-cli)", r.external_dependencies)


class MockScreenTests(unittest.TestCase):
    """REGRESSION: the bare-keyword scan flagged 38 of 98 modules. MEASURED
    against the tree, one was a real scaffold - a 97 % false-positive rate,
    which is the same as flagging nothing. Every case below is a real string
    from this repo, not an invented one."""

    def flags(self, source, module_name=""):
        return mod.scan_real_or_mock_detailed(source, module_name).findings

    def suppressed(self, source, module_name=""):
        return mod.scan_real_or_mock_detailed(source, module_name).suppressed

    # -- domain vocabulary -------------------------------------------------- #
    def test_via_stub_is_a_physical_object_not_a_software_stub(self):
        source = ('"""Via stub resonance check. A blind via leaves no stub: it\n'
                  'spans only the used layers, so the stub effect is zero."""\n'
                  "STUB_LENGTH_THRESHOLD_MM = 0.5\n")
        self.assertEqual([], self.flags(source))

    def test_stubborn_is_not_the_word_stub(self):
        """A substring match: ``grid_astar``'s "far more stubborn"."""
        source = '"""Explored with A*. Slower, but far more stubborn."""\n'
        self.assertEqual([], self.flags(source))

    def test_an_identifier_is_a_name_not_a_claim(self):
        source = ("class _FakeBoard:\n    pass\n\n"
                  "def f(stub_length_mm=0.5):\n    return stub_length_mm\n")
        self.assertEqual([], self.flags(source))

    def test_placeholder_as_a_data_operation_is_not_a_confession(self):
        """``error_memory`` redacts per-run values before hashing."""
        source = ('"""Replace everything that changes per run with a placeholder,\n'
                  'so the same CLASS of error hashes the same way."""\n')
        self.assertEqual([], self.flags(source))

    # -- negation ----------------------------------------------------------- #
    def test_a_denial_is_not_a_confession(self):
        for sentence in (
            "there is deliberately NO hardcoded fallback, because a silently "
            "substituted fab limit is an invisible wrong answer",
            "it never falls back to hardcoded limits",
            "the endpoint and key are read from the environment - NEITHER is hardcoded",
            "what makes it a genuine registry-binding test rather than a stub",
            "this is the one piece of this bridge that is NOT a placeholder",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual([], self.flags(f'"""Module.\n\n{sentence}."""\n'))

    def test_a_denial_that_matched_is_screened_not_lost(self):
        """Two different things produce an empty finding list: a phrase that
        never looked like a confession, and one that did and was screened.
        Only the second leaves an audit trail, and it must."""
        for sentence in ("there is deliberately NO hardcoded fallback",
                         "it never falls back to hardcoded limits",
                         "NEITHER is hardcoded"):
            with self.subTest(sentence=sentence):
                scan = mod.scan_real_or_mock_detailed(f'"""Module.\n\n{sentence}."""\n')
                self.assertEqual([], scan.findings)
                self.assertTrue(scan.suppressed, "screened, not lost")

    def test_negation_does_not_reach_across_a_clause(self):
        """``jlcpcb_dfm``'s REAL confession carries an unrelated "not"
        earlier in the same line. A clause-wide negation test loses it - the
        one finding this whole screen exists to keep."""
        source = ('"""Build the request body.\n\n'
                  'GUESSED SHAPE, not verified: a filename plus a placeholder for\n'
                  'however the real API wants the file transferred."""\n')
        self.assertTrue(self.flags(source))

    # -- another artefact --------------------------------------------------- #
    def test_a_sentence_about_the_archive_is_about_the_archive(self):
        source = ('"""Adapters.\n\n'
                  "The phases the archive's ``main.py`` hardcoded into one\n"
                  'monolithic ``cmd_run`` are separate here."""\n')
        self.assertEqual([], self.flags(source))

    def test_a_bullet_list_inherits_the_paragraph_that_introduces_it(self):
        """``drc_gate``: the vendored script is named in the lead-in, the
        defect is numbered underneath it."""
        source = ('"""DRC gate.\n\n'
                  "VENDORED FROM ``krt/drc.sh``. That script had defects:\n\n"
                  "  1. no --exit-code-violations -> a false PASS;\n"
                  "  2. the companion .kicad_pro -> hardcoded to ONE board's\n"
                  '     absolute path."""\n')
        self.assertEqual([], self.flags(source))

    def test_naming_this_module_itself_does_not_count_as_another_artefact(self):
        source = ('"""Bridge.\n\n'
                  'bridge.py returns a hardcoded price table."""\n')
        self.assertTrue(self.flags(source, module_name="bridge.py"))

    # -- citation ----------------------------------------------------------- #
    def test_quoting_a_phrase_is_citing_it_not_asserting_it(self):
        source = '# Adjacent on purpose: "is a stub" confesses, "stub focused" does not.\n'
        self.assertEqual([], self.flags(source))

    def test_a_multiline_comment_block_keeps_its_quotation_together(self):
        source = ('# The real confession reads\n'
                  '# "a placeholder for however the real API wants the file\n'
                  '# transferred" and nothing else here does.\n')
        self.assertEqual([], self.flags(source))

    # -- self-test blocks --------------------------------------------------- #
    def test_a_fake_inside_a_self_test_is_the_test_double(self):
        source = ("def _self_test():\n"
                  "    # A fake, deterministic simulator that improves every call.\n"
                  "    return 0\n")
        self.assertEqual([], self.flags(source))

    def test_the_same_sentence_outside_a_self_test_still_counts(self):
        """FAULT INJECTION: proves the exemption is scoped to the block, not
        to the sentence."""
        source = ("def run():\n"
                  "    # The simulator here is a fake, deterministic one.\n"
                  "    return 0\n")
        self.assertTrue(self.flags(source))

    # -- canned constants --------------------------------------------------- #
    def test_a_dispatch_table_is_not_a_canned_answer(self):
        """All five modules the unscoped rule flagged were classifiers whose
        return really does depend on the input."""
        source = ("def board_exit_code(status):\n"
                  '    if status == "GREEN":\n        return 0\n'
                  '    if status == "ERROR":\n        return 2\n'
                  "    return 1\n")
        self.assertEqual([], self.flags(source))

    def test_guard_clauses_returning_none_are_refusals(self):
        source = ("def fetch_response(blocker, request):\n"
                  "    if blocker.locked:\n        return None\n"
                  "    if request.width == 0:\n        return None\n"
                  "    return compute(blocker)\n")
        self.assertEqual([], self.flags(source))

    def test_an_api_function_answering_without_asking_is_flagged(self):
        source = ("def fetch_api_price(part):\n"
                  '    if part == "R1":\n        return 0.01\n'
                  '    if part == "C1":\n        return 0.02\n'
                  "    return 0.0\n")
        self.assertTrue(self.flags(source))

    def test_a_function_that_really_reaches_out_is_not_flagged(self):
        source = ("import subprocess\n\n"
                  "def fetch_api_price(part):\n"
                  '    if part == "CACHED":\n        return 0.01\n'
                  '    if part == "ALSO_CACHED":\n        return 0.02\n'
                  "    return subprocess.run(['curl', part])\n")
        self.assertEqual([], self.flags(source))

    # -- the screen is not blind -------------------------------------------- #
    def test_a_real_confession_still_flags(self):
        for sentence in ("this module is a mock",
                         "the return value is just a stub",
                         "everything below is a placeholder for the real API",
                         "returns fake data until the endpoint is known"):
            with self.subTest(sentence=sentence):
                self.assertTrue(self.flags(f'"""{sentence}."""\n'))

    def test_suppressed_hits_are_kept_for_audit(self):
        """A filter that silently drops evidence is indistinguishable from a
        broken one - the failure mode that hid the junit key bug."""
        scan = mod.scan_real_or_mock_detailed('"""There is NO hardcoded fallback."""\n')
        self.assertEqual([], scan.findings)
        self.assertEqual(1, len(scan.suppressed))
        self.assertIn("olumsuzlama", scan.suppressed[0])


class MockScreenRealFileTests(unittest.TestCase):
    """Run against the REAL files, like the self-reference test above: a
    screen tuned on quoted excerpts can still be wrong about the tree."""

    def scan(self, path):
        return mod.scan_real_or_mock_detailed(
            Path(path).read_text(encoding="utf-8"), Path(path).name)

    def test_the_one_real_scaffold_is_still_flagged(self):
        path = SCRIPTS / "production" / "jlcpcb_dfm.py"
        self.assertTrue(self.scan(path).findings,
                        "jlcpcb_dfm.py documents a GUESSED response shape")

    def test_the_via_stub_checker_is_not_flagged(self):
        """117 of the 199 keyword hits were the word "stub" - this file is
        where most of them live, and it MEASURES a physical via barrel."""
        self.assertEqual([], self.scan(SCRIPTS / "board" / "via_stub.py").findings)

    def test_this_module_does_not_flag_itself(self):
        """Self-reference, third time: the file that must NAME the phrases it
        screens for cannot be condemned by naming them."""
        self.assertEqual([], self.scan(REPORT_SCRIPTS / "capability_inventory.py").findings)


class JunitKeyTests(unittest.TestCase):
    """``test_status`` is the one column this module promises is MEASURED,
    not estimated. It silently degraded to "olculemedi" for every row
    because the junit ``classname`` was reduced with ``split(".")[-1]``,
    which yields the test CLASS for unittest suites, never the test FILE.
    """

    def test_unittest_shape_returns_the_test_file_not_the_class(self):
        self.assertEqual("test_ids",
                         mod.junit_test_module("tests.test_ids.DetUuidTests"))

    def test_function_shape_has_no_class_part(self):
        self.assertEqual("test_ids", mod.junit_test_module("tests.test_ids"))

    def test_nested_class_still_resolves_to_the_file(self):
        self.assertEqual(
            "test_common",
            mod.junit_test_module("tests.test_common.Outer.Inner"))

    def test_unprefixed_classname_falls_back_instead_of_inventing_a_key(self):
        self.assertEqual("suite", mod.junit_test_module("pkg.suite"))
        self.assertEqual("", mod.junit_test_module(""))


class TestStatusMeasuredEndToEndTests(unittest.TestCase):
    """The unit test above encodes what pytest's classname LOOKS like; this
    one asks the installed pytest directly. Without it, a future pytest
    that changes the format would put the column straight back to a silent
    "olculemedi" with every unit test still green."""

    def test_a_real_pytest_run_reaches_the_module_row(self):
        try:
            import pytest  # noqa: F401
        except ImportError:  # pragma: no cover - environment without pytest
            self.skipTest("pytest not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "widget.py").write_text("def go():\n    return 1\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            # A unittest.TestCase - the exact shape whose junit classname
            # carries a trailing class name.
            (tests_dir / "test_widget.py").write_text(
                "import unittest\n\n"
                "class WidgetTests(unittest.TestCase):\n"
                "    def test_go(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8")

            results = mod.run_all_tests_and_collect_results(root, timeout_s=300)
            self.assertIn("test_widget", results,
                          f"junit keys did not include the test file: {sorted(results)}")

            records = mod.scan_repo(root, test_results=results)
            widget = next(r for r in records if r.module_name == "widget.py")
            self.assertEqual("gecti", widget.test_status)


class MeasurementStatusTests(unittest.TestCase):
    def _record(self, test_status, has_test_file=True):
        return mod.ModuleRecord(
            module_name="x.py", relative_path="x.py", category="diğer", status="Var",
            status_source="marker", summary="s", has_test_file=has_test_file,
            test_function_count=1, test_status=test_status,
            real_or_mock="işaret yok (otomatik tarama)",
        )

    def test_skipped_run_is_not_a_warning(self):
        status = mod.measurement_status([self._record("olculemedi")], None)
        self.assertEqual("atlandi (--skip-tests)", status["state"])
        self.assertEqual("", status["warning"])

    def test_results_that_match_nothing_raise_a_warning(self):
        """The exact shape of the junit key bug: a suite really ran, and
        not one result reached a module row."""
        status = mod.measurement_status([self._record("olculemedi")],
                                        {"WidgetTests": (5, 5)})
        self.assertEqual("eslesmedi", status["state"])
        self.assertIn("eslesmedi", status["warning"])

    def test_empty_junit_after_a_real_run_raises_a_warning(self):
        status = mod.measurement_status([self._record("olculemedi")], {})
        self.assertEqual("kosulamadi", status["state"])
        self.assertTrue(status["warning"])

    def test_a_matching_run_is_clean(self):
        status = mod.measurement_status([self._record("gecti")], {"test_x": (5, 5)})
        self.assertEqual("olculdu", status["state"])
        self.assertEqual("", status["warning"])
        self.assertEqual(1, status["modules_measured"])


class OutputWritingTests(unittest.TestCase):
    def test_csv_is_written_with_all_headers(self):
        record = mod.ModuleRecord(
            module_name="x.py", relative_path="x.py", category="diğer", status="Var",
            status_source="marker", summary="s", has_test_file=False,
            test_function_count=0, test_status="test_yok", real_or_mock="işaret yok (otomatik tarama)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            mod.write_csv([record], path)
            content = path.read_text(encoding="utf-8")
            self.assertIn("Güvenlik Statüsü", content)
            self.assertIn("İç Bağımlılıklar", content)
            self.assertIn("Dış Bağımlılıklar", content)
            self.assertIn("AI Sözleşmesi Var Mı?", content)
            self.assertIn("x.py", content)


class SelfTestTests(unittest.TestCase):
    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())


class CliContract(unittest.TestCase):
    SCRIPT = REPORT_SCRIPTS / "capability_inventory.py"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.SCRIPT), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              check=False)

    def test_missing_args_exits_2(self):
        proc = self.run_cli()
        self.assertEqual(2, proc.returncode)

    def test_self_test_exits_zero(self):
        proc = self.run_cli("--self-test")
        self.assertEqual(0, proc.returncode)

    def test_scan_on_a_tiny_repo_exits_zero_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            proc = self.run_cli(str(root), "--skip-tests", "--out", "inv.xlsx")
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            self.assertTrue((root / "inv.csv").exists())


if __name__ == "__main__":
    unittest.main()
