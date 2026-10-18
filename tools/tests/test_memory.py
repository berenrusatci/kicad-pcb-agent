#!/usr/bin/env python3
"""scripts/memory/: error memory, recovery ladder, and the vault rule.

Converted from the archive's pytest modules ``test_hata_hafizasi.py`` and
``test_otonom_kurtarma_motoru.py``. The vault tests are new: the archive had
no such rule, and re-pointing the storage is the main change in the port.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SCRIPTS, TOOLS  # noqa: E402

MEMORY = SCRIPTS / "memory"
ERROR_MEMORY = MEMORY / "error_memory.py"
RECOVERY = MEMORY / "recovery.py"

sys.path.insert(0, str(TOOLS))
from scripts.memory import error_memory, recovery, store  # noqa: E402

CLEARANCE_A = 'Clearance violation (net "GND" and net "+3V3") at (12.34, 56.78)'
CLEARANCE_B = 'Clearance violation (net "GND" and net "+3V3") at (98.76, 54.32)'
PIN_ERROR = "Symbol pin not connected to any net"

DRC_REPORT = {
    "violations": [
        {"severity": "error", "type": "clearance", "description": CLEARANCE_A},
        {"severity": "error", "type": "clearance", "description": CLEARANCE_B},
        {"severity": "warning", "type": "silk", "description": "Silk over pad"},
    ]
}


def run(*args: str, env: dict | None = None) -> tuple[int, dict | None, str]:
    proc = subprocess.run([sys.executable, *args], text=True, encoding="utf-8",
                          capture_output=True, check=False,
                          env={**os.environ, **(env or {})})
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


# --------------------------------------------------------------------------- #
class VaultRuleTests(unittest.TestCase):
    """The standing project rule: the vault holds no design or tool state."""

    def test_the_default_store_is_under_pcb_agent_var(self):
        default = store.default_store()
        self.assertEqual(("var", "memory"), default.parts[-2:])
        self.assertEqual(store.repo_root(), default.parents[1])

    def test_the_default_store_is_outside_tools(self):
        """Memory churns every session; inside tools/ it would make the
        hash-bound tool registry drift for no engineering reason."""
        self.assertNotIn("tools", store.default_store().parts)

    def test_a_path_inside_a_vault_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "MyVault"
            (vault / ".obsidian").mkdir(parents=True)
            with self.assertRaises(store.VaultWriteRefused):
                store.assert_not_vault(vault / "notes" / "error_memory.md")

    def test_a_path_naming_the_vault_marker_directly_is_refused(self):
        with self.assertRaises(store.VaultWriteRefused):
            store.assert_not_vault(Path("/tmp/x/.obsidian/notes.md"))

    def test_the_refusal_happens_before_anything_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "MyVault"
            (vault / ".obsidian").mkdir(parents=True)
            target = vault / "memory"
            with self.assertRaises(store.VaultWriteRefused):
                store.resolve_store(target, create=True)
            self.assertFalse(target.exists(), "a directory was created anyway")

    def test_a_normal_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Path(tmp).resolve(),
                             store.resolve_store(tmp).resolve())

    def test_the_environment_variable_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[store.ENV_VAR] = tmp
            try:
                self.assertEqual(Path(tmp).resolve(),
                                 store.resolve_store().resolve())
            finally:
                del os.environ[store.ENV_VAR]

    def test_an_explicit_store_beats_the_environment(self):
        with tempfile.TemporaryDirectory() as env_dir, \
                tempfile.TemporaryDirectory() as explicit:
            os.environ[store.ENV_VAR] = env_dir
            try:
                self.assertEqual(Path(explicit).resolve(),
                                 store.resolve_store(explicit).resolve())
            finally:
                del os.environ[store.ENV_VAR]

    def test_jlc_parts_cache_dir_sits_under_the_same_var_memory_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = store.jlc_parts_cache_dir(tmp)
            self.assertEqual(Path(tmp).resolve() / "jlc_parts_cache",
                             cache.resolve())

    def test_jlc_parts_cache_dir_inside_a_vault_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "MyVault"
            (vault / ".obsidian").mkdir(parents=True)
            with self.assertRaises(store.VaultWriteRefused):
                store.jlc_parts_cache_dir(vault / "memory", create=True)


# --------------------------------------------------------------------------- #
class SignatureTests(unittest.TestCase):
    def test_the_same_error_class_at_different_coordinates_shares_a_signature(self):
        self.assertEqual(error_memory.signature(CLEARANCE_A),
                         error_memory.signature(CLEARANCE_B))

    def test_different_error_classes_get_different_signatures(self):
        self.assertNotEqual(error_memory.signature(CLEARANCE_A),
                            error_memory.signature("Track width too small"))

    def test_refdes_numbers_are_normalised_but_the_prefix_is_kept(self):
        """C7 and C8 are the same class of problem; C7 and R11 are not.
        Normalising the letter away too would merge unrelated classes."""
        self.assertEqual(
            error_memory.signature("Courtyard overlap C7 and C8"),
            error_memory.signature("Courtyard overlap C11 and C12"))
        self.assertNotEqual(
            error_memory.signature("Courtyard overlap C7 and C8"),
            error_memory.signature("Courtyard overlap R11 and R12"))

    def test_coordinates_are_normalised_before_bare_numbers(self):
        """Order matters: numbers first would erase the fact that it WAS a
        coordinate, and two unrelated errors would collide."""
        self.assertIn("<coord>", error_memory.normalise(CLEARANCE_A))

    def test_unrelated_messages_do_not_score_as_similar(self):
        """FAULT INJECTION: retrieval that matches everything is worse than
        no retrieval, because it sends the agent to the wrong fix."""
        self.assertLess(error_memory.similarity(CLEARANCE_A, PIN_ERROR), 0.55)

    def test_similarity_of_an_empty_message_is_zero(self):
        self.assertEqual(0.0, error_memory.similarity("", CLEARANCE_A))


class ErrorMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="error-memory-")
        self.memory = error_memory.ErrorMemory(
            Path(self.temp.name) / "error_memory.md")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, fix: str, outcome: str = "solved",
               message: str = CLEARANCE_A) -> error_memory.Record:
        return error_memory.Record("DRC", message, "placement too tight", fix,
                                   outcome, "TestProject", tags=["clearance"])

    def test_a_missing_file_is_an_empty_memory_not_an_error(self):
        self.assertEqual([], self.memory.records())

    def test_storing_is_idempotent(self):
        record = self.record("moved C7 by 2 mm")
        self.assertTrue(self.memory.remember(record))
        self.assertFalse(self.memory.remember(record))
        self.assertEqual(1, len(self.memory.records()))

    def test_a_stored_record_survives_the_markdown_round_trip(self):
        self.memory.remember(self.record("moved C7 by 2 mm"))
        stored = self.memory.records()[0]
        self.assertEqual("DRC", stored.kind)
        self.assertEqual("moved C7 by 2 mm", stored.fix)
        self.assertEqual("TestProject", stored.project)
        self.assertEqual(["clearance"], stored.tags)

    def test_a_signature_match_retrieves_the_prior_fix(self):
        self.memory.remember(self.record("moved C7 by 2 mm"))
        found = self.memory.suggest(CLEARANCE_B)
        self.assertEqual(1.0, found["suggested"][0]["score"])
        self.assertEqual("moved C7 by 2 mm", found["suggested"][0]["fix"])

    def test_a_known_failed_fix_is_never_suggested(self):
        self.memory.remember(self.record("moved C7 by 2 mm"))
        self.memory.remember(self.record("reduced width to 0.15 mm", "failed"))
        found = self.memory.suggest(CLEARANCE_A)
        self.assertNotIn("0.15", json.dumps(found["suggested"]))
        self.assertIn("0.15", json.dumps(found["already_failed"]))

    def test_needs_human_history_is_its_own_bucket(self):
        self.memory.remember(self.record("asked the user", "needs_human"))
        found = self.memory.suggest(CLEARANCE_A)
        self.assertEqual([], found["suggested"])
        self.assertEqual(1, len(found["needs_human"]))

    def test_a_half_written_record_is_skipped_not_guessed(self):
        self.memory.path.write_text(
            error_memory.HEADER + "## DRC - deadbeef\n- **kind:** DRC\n"
            "- **fix:** something\n\n", encoding="utf-8")
        self.assertEqual([], self.memory.records())

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(error_memory.MemoryError_):
            error_memory.Record("DRC", CLEARANCE_A, "x", "y", "probably")

    def test_report_violations_deduplicate_by_class(self):
        classes = error_memory.unique_messages(
            error_memory.report_violations(DRC_REPORT))
        self.assertEqual(2, len(classes),
                         "two coordinates of one class must collapse to one")

    def test_errors_only_drops_warnings(self):
        self.assertEqual(
            2, len(error_memory.report_violations(DRC_REPORT, errors_only=True)))

    def test_drc_gate_reports_are_readable_too(self):
        gate_report = {"schema_version": "drc-gate-report/v1", "mode": "drc",
                       "violations": DRC_REPORT["violations"]}
        self.assertEqual("DRC", error_memory.detect_kind(gate_report))
        self.assertEqual(3, len(error_memory.report_violations(gate_report)))

    def test_erc_sheets_are_flattened(self):
        erc = {"sheets": [{"violations": [
            {"severity": "error", "description": PIN_ERROR}]}]}
        self.assertEqual("ERC", error_memory.detect_kind(erc))
        self.assertEqual(1, len(error_memory.report_violations(erc)))


class ErrorMemoryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="error-memory-cli-")
        self.store = Path(self.temp.name) / "memory"
        self.report = Path(self.temp.name) / "drc.json"
        self.report.write_text(json.dumps(DRC_REPORT), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_positional_exits_2(self):
        exit_code, _report, stderr = run(str(ERROR_MEMORY))
        self.assertEqual(2, exit_code)
        self.assertIn("REPORT is required", stderr)

    def test_learn_without_its_required_flags_exits_2(self):
        exit_code, _report, stderr = run(str(ERROR_MEMORY), str(self.report),
                                         "--learn")
        self.assertEqual(2, exit_code)
        self.assertIn("--learn requires", stderr)

    def test_self_test_is_opt_in_and_clean(self):
        exit_code, report, _stderr = run(str(ERROR_MEMORY), "--self-test")
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", report["self_test"], report)

    def test_learn_then_suggest_across_two_invocations(self):
        exit_code, learned, _stderr = run(
            str(ERROR_MEMORY), str(self.report), "--store", str(self.store),
            "--learn", "--root-cause", "tight placement",
            "--fix", "moved C7 by 2 mm", "--outcome", "solved",
            "--project", "TestProject")
        self.assertEqual(0, exit_code)
        self.assertEqual("STORED", learned["status"])
        self.assertEqual(2, learned["stored"])

        exit_code, suggested, _stderr = run(
            str(ERROR_MEMORY), str(self.report), "--store", str(self.store))
        self.assertEqual(0, exit_code)
        self.assertEqual("MATCHED", suggested["status"])
        self.assertEqual(2, suggested["matched_classes"])
        self.assertIn("moved C7 by 2 mm", json.dumps(suggested["entries"]))

    def test_suggesting_from_an_empty_memory_exits_1(self):
        exit_code, report, _stderr = run(
            str(ERROR_MEMORY), str(self.report), "--store", str(self.store))
        self.assertEqual(1, exit_code)
        self.assertEqual("NO_MATCH", report["status"])

    def test_a_vault_store_is_refused_at_the_cli(self):
        vault = Path(self.temp.name) / "MyVault"
        (vault / ".obsidian").mkdir(parents=True)
        exit_code, report, _stderr = run(
            str(ERROR_MEMORY), str(self.report), "--store", str(vault),
            "--learn", "--root-cause", "x", "--fix", "y",
            "--outcome", "solved")
        self.assertEqual(2, exit_code)
        self.assertEqual("ERROR", report["status"])
        self.assertIn("vault", json.dumps(report).lower())


# --------------------------------------------------------------------------- #
class IsolationTests(unittest.TestCase):
    TARGET = "scripts.memory.recovery"

    def path(self):
        return [str(TOOLS)]

    def test_a_normal_call_returns_its_result(self):
        outcome = recovery.run_isolated(f"{self.TARGET}:_probe_add",
                                        {"a": 3.0, "b": 4.0},
                                        extra_path=self.path())
        self.assertTrue(outcome.ok, outcome.to_dict())
        self.assertEqual(7.0, outcome.result)

    def test_a_crash_is_caught_not_propagated(self):
        outcome = recovery.run_isolated(f"{self.TARGET}:_probe_crash", {},
                                        extra_path=self.path())
        self.assertFalse(outcome.ok)
        self.assertEqual(recovery.CRASHED, outcome.failure)
        self.assertIn("fault injection", outcome.stderr)

    def test_a_hang_times_out_without_taking_the_caller_down(self):
        outcome = recovery.run_isolated(f"{self.TARGET}:_probe_sleep",
                                        {"seconds": 5}, timeout_s=1,
                                        extra_path=self.path())
        self.assertFalse(outcome.ok)
        self.assertEqual(recovery.TIMEOUT, outcome.failure)

    def test_unexpected_stdout_is_a_failure_not_a_success(self):
        """exit 0 with non-JSON stdout must never read as a clean run."""
        outcome = recovery.run_isolated(f"{self.TARGET}:_probe_noise", {},
                                        extra_path=self.path())
        self.assertFalse(outcome.ok)
        self.assertEqual(recovery.BAD_OUTPUT, outcome.failure)

    def test_a_missing_module_is_unavailable_not_crashed(self):
        outcome = recovery.run_isolated("no_such_module_xyz:go")
        self.assertEqual(recovery.UNAVAILABLE, outcome.failure)

    def test_a_missing_function_is_unavailable(self):
        outcome = recovery.run_isolated(f"{self.TARGET}:no_such_function",
                                        extra_path=self.path())
        self.assertEqual(recovery.UNAVAILABLE, outcome.failure)

    def test_a_malformed_target_is_rejected_without_a_subprocess(self):
        outcome = recovery.run_isolated("not_a_target")
        self.assertEqual(recovery.UNAVAILABLE, outcome.failure)


class LadderTests(unittest.TestCase):
    TARGET = "scripts.memory.recovery"

    def rung(self, name, function, kwargs=None):
        return recovery.Rung(name, function, kwargs or {})

    def test_the_ladder_stops_at_the_first_working_rung(self):
        report = recovery.climb("goal", [
            self.rung("crashing", f"{self.TARGET}:_probe_crash"),
            self.rung("working", f"{self.TARGET}:_probe_add",
                      {"a": 1.0, "b": 1.0}),
            self.rung("never", f"{self.TARGET}:_probe_crash"),
        ], 30, extra_path=[str(TOOLS)])
        self.assertEqual("RECOVERED", report["status"])
        self.assertEqual("working", report["succeeded_rung"])
        self.assertEqual(2, len(report["attempts"]),
                         "the third rung must not have been tried")
        self.assertFalse(report["needs_human"])

    def test_an_exhausted_ladder_escalates(self):
        report = recovery.climb("goal", [
            self.rung("crashing", f"{self.TARGET}:_probe_crash"),
        ], 30, extra_path=[str(TOOLS)])
        self.assertEqual("EXHAUSTED", report["status"])
        self.assertTrue(report["needs_human"])

    def test_a_ladder_that_was_never_installed_does_not_escalate(self):
        """The archive conflated 'every rung failed' with 'no rung could be
        imported'. Only the first is an engineering question."""
        report = recovery.climb("goal", [
            self.rung("missing", "no_such_module_xyz:go")], 30)
        self.assertEqual("NO_LADDER", report["status"])
        self.assertFalse(report["needs_human"])
        self.assertEqual(1, report["rungs_unavailable"])
        self.assertEqual(0, report["rungs_attempted"])

    def test_unavailable_rungs_do_not_stop_the_ladder(self):
        report = recovery.climb("goal", [
            self.rung("missing", "no_such_module_xyz:go"),
            self.rung("working", f"{self.TARGET}:_probe_add",
                      {"a": 2.0, "b": 2.0}),
        ], 30, extra_path=[str(TOOLS)])
        self.assertEqual("RECOVERED", report["status"])
        self.assertEqual(4.0, report["result"])
        self.assertEqual(1, report["rungs_unavailable"])

    def test_an_empty_plan_is_rejected(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.parse_plan({"rungs": []})

    def test_a_rung_without_a_function_is_rejected(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.parse_plan({"rungs": [{"name": "x"}]})

    def test_a_non_positive_timeout_is_rejected(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery.parse_plan({"rungs": [{"name": "x", "function": "a:b"}],
                                 "timeout_s": 0})


class RecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-cli-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, rungs) -> Path:
        path = self.root / "plan.json"
        path.write_text(json.dumps({"goal": "test", "timeout_s": 30,
                                    "rungs": rungs}), encoding="utf-8")
        return path

    def test_missing_positional_exits_2(self):
        exit_code, _report, stderr = run(str(RECOVERY))
        self.assertEqual(2, exit_code)
        self.assertIn("PLAN is required", stderr)

    def test_self_test_is_opt_in_and_clean(self):
        exit_code, report, _stderr = run(str(RECOVERY), "--self-test")
        self.assertEqual(0, exit_code)
        self.assertEqual("PASS", report["self_test"], report)

    def test_a_successful_plan_exits_0_and_archives_the_report(self):
        plan = self.plan([{"name": "working",
                           "function": "scripts.memory.recovery:_probe_add",
                           "kwargs": {"a": 1.0, "b": 2.0}}])
        exit_code, report, _stderr = run(
            str(RECOVERY), str(plan), "--store", str(self.root / "memory"),
            env={"PYTHONPATH": str(TOOLS)})
        self.assertEqual(0, exit_code)
        self.assertEqual("RECOVERED", report["status"])
        self.assertTrue(Path(report["archived"]).is_file())

    def test_an_exhausted_plan_exits_1(self):
        plan = self.plan([{"name": "crashing",
                           "function": "scripts.memory.recovery:_probe_crash"}])
        exit_code, report, _stderr = run(
            str(RECOVERY), str(plan), "--no-store",
            env={"PYTHONPATH": str(TOOLS)})
        self.assertEqual(1, exit_code)
        self.assertEqual("EXHAUSTED", report["status"])

    def test_a_malformed_plan_exits_2(self):
        path = self.root / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        exit_code, report, _stderr = run(str(RECOVERY), str(path), "--no-store")
        self.assertEqual(2, exit_code)
        self.assertEqual("ERROR", report["status"])


if __name__ == "__main__":
    unittest.main()
