"""Tests for common/assertions.py, common/matrix.py and scripts/assert_matrix.py.

WHAT THESE TESTS ARE FOR
--------------------------------------------------------------------------
The layer under test exists to stop a permissive reader from saying YES.
So the tests are written the same way round: each one drives the language
into a corner where a sloppy implementation would pass, and requires a
refusal. A test that only shows the happy path proves the code runs, not
that the gate is a gate.

Four of these are regressions against defects this repo has actually paid
for, and they are marked as such:

  * the ``(0.7-0.4)/2`` float boundary (measurement error class 1) - bit
    this repo twice;
  * the empty comparison, where ``None`` against a threshold once got
    reported as agreement (class 2);
  * ``_EMPHASIS`` stripping intra-word underscores, so ``PWR_FLAG`` became
    ``PWRFLAG`` and every assertion naming a tool missed its matrix row -
    found in #66's first real run against the matrix and fixed without a
    test, which is what this file closes;
  * ``UNASSERTED_RULE`` counted as FAIL, which reports a violation where
    there is only silence.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.assertions import (ESTABLISHES_MEASUREMENT,  # noqa: E402
                               ESTABLISHES_PHYSICAL, ESTABLISHES_RECORD_ONLY,
                               EVIDENCE_MISSING, EVIDENCE_UNBOUND,
                               FALSIFIER_NO_COVERAGE, GREEN, MISSING,
                               PATH_ABSENT, RECORD_INCOMPLETE, RED,
                               SCREEN_ONLY, STALE_EVIDENCE, TYPE_MISMATCH,
                               UNIT_MISMATCH, UNITS_UNDECLARED,
                               VACUOUS_ASSERTION, AssertionSyntaxError,
                               Evidence, MEASUREMENT_ABSENT,
                               establishment_summary, evaluate,
                               load_assertion, parse_selector, prove, rollup,
                               select)
from common.assertions import (AUTHORITY_SCREEN_ONLY,  # noqa: E402
                               AUTHORITY_SIGNOFF, COVERAGE_PARTIAL,
                               NOTHING_SCANNED, SCREEN_ONLY_AUTHORITY,
                               authority_summary)
from common.findings import FAIL, NO_COVERAGE, PASS  # noqa: E402
from common.findings import board_report, by_check_map, result  # noqa: E402
from common.matrix import (AMBIGUOUS_LEVEL, PARTIAL_LEVEL,  # noqa: E402
                           UNPARSED_LEVEL, classify_level, cross_reference,
                           parse_matrix, partial_requirement, rule_key,
                           strip_markdown)
from scripts.assert_matrix import (load_set, main, matrix_findings,  # noqa: E402
                                   overall)

SCRIPT = TOOLS / "scripts" / "assert_matrix.py"
FIXTURE = TOOLS / "fixtures" / "assertions" / "matrix_core.json"
MATRIX = TOOLS.parent / "skills" / "dogrulama-matrisi" / "SKILL.md"


def sample_interconnect_report() -> dict:
    """A real interconnect_mating.py report, built by interconnect_mating.py.

    Same principle as ``sample_drc_gate_report``: nothing here transcribes the
    shape. This gate compares two DOCUMENTS and imports no pcbnew, so unlike the
    board producers its report CAN be built in this interpreter - which means its
    selectors get the strong check (does the path resolve in a real report?)
    rather than the weaker source-string grep.
    """
    from scripts import interconnect_mating

    return interconnect_mating._run_fixture(
        interconnect_mating._fixture_chain())


def sample_drc_gate_report(mode: str) -> dict:
    """A real drc_gate.py report, built by drc_gate.py.

    The point is that nothing here transcribes the report shape: if a metric
    is renamed or dropped, this returns the new shape and the selectors that
    read the old one fail. A hand-written dict would keep agreeing with an
    assertion long after the tool stopped.
    """
    from scripts import drc_gate

    raw = {"violations": [], "unconnected_items": [], "schematic_parity": [],
           "kicad_version": "test"}
    census = (drc_gate.pcb_census(drc_gate.SELF_TEST_PCB) if mode == "drc"
              else drc_gate.sch_census(drc_gate.SELF_TEST_SCH))
    with tempfile.TemporaryDirectory() as workdir:
        design = Path(workdir) / ("d.kicad_pcb" if mode == "drc"
                                  else "d.kicad_sch")
        design.write_text("(x)", encoding="utf-8")
        return drc_gate.build_report(
            design, mode, {"name": "kicad-cli"},
            drc_gate.build_argv("kicad-cli", mode, str(design), "out.json",
                                schematic_parity=(mode == "drc")),
            0, raw, census, "t0", "t1", None)


# A loadable level-B assertion. Individual tests override single keys so the
# thing being tested is the only difference from a known-good baseline.
BASE = {
    "id": "a1",
    "rule": "DRC 0 ihlal (repo rule set)",
    "level": "B",
    "loop": "L0",
    "on_fail": "duzelt",
    "source": "SKILL.md 2",
    "evidence": "drc",
    "selector": "summary.FAIL",
    "operator": "at_most",
    "expected": 0,
    "tolerance": 0,
    "falsifier": {"operation": "set", "path": "summary.FAIL", "value": 3},
    # Both axes are explicit on the baseline because the loader refuses to
    # default them for a machine level: authority because a screen and a
    # solver both produce numbers and only one may close a rule, and the
    # coverage guard because "no violations" and "nothing examined" are the
    # same number without it. The waiver (rather than a selector) is what this
    # tiny fixture document honestly warrants - it carries no scanned count.
    "authority": "signoff",
    "coverage_waiver": "test baseline: the fixture document carries no scanned count",
}


def build(**overrides):
    """Load BASE with overrides; a value of None drops the key entirely."""
    entry = {k: v for k, v in {**BASE, **overrides}.items() if v is not None}
    return load_assertion(entry)


def evidence_for(document, **kwargs):
    return {"drc": Evidence(name="drc", document=document, **kwargs)}


# --------------------------------------------------------------------------- #
# Load-time refusals
# --------------------------------------------------------------------------- #

class LoadRefusalTest(unittest.TestCase):
    """Every refusal must happen at LOAD time.

    An assertion that only blows up on the day its evidence turns up is a
    rule that is silently unenforced on every run where it does not.
    """

    def assertRefuses(self, code, **overrides):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(**overrides)
        self.assertEqual(caught.exception.code, code)

    def test_a_numeric_operator_without_tolerance_does_not_load(self):
        # The boundary policy has to be chosen by a person, not inherited
        # from float arithmetic. 0 is a fine answer; absent is not.
        self.assertRefuses("MISSING_TOLERANCE", tolerance=None)

    def test_tolerance_zero_is_accepted_because_it_was_written_down(self):
        self.assertEqual(build(tolerance=0).tolerance, 0.0)

    def test_a_negative_tolerance_does_not_load(self):
        self.assertRefuses("BAD_TOLERANCE", tolerance=-1)

    def test_a_boolean_tolerance_does_not_load(self):
        # bool is an int in Python; True would silently become 1.0.
        self.assertRefuses("BAD_TOLERANCE", tolerance=True)

    def test_an_executable_assertion_without_a_falsifier_does_not_load(self):
        self.assertRefuses("MISSING_FALSIFIER", falsifier=None)

    def test_an_unknown_operator_does_not_load(self):
        self.assertRefuses("UNKNOWN_OPERATOR", operator="eval")

    def test_units_without_a_units_selector_does_not_load(self):
        self.assertRefuses("UNITS_UNBOUND", units="mm")

    def test_a_units_selector_without_units_does_not_load(self):
        self.assertRefuses("UNITS_UNBOUND", units_selector="units")

    def test_a_wildcard_selector_does_not_load(self):
        # ``checks[*].status`` reads as one assertion but covers a set whose
        # size depends on the report - and the empty set passes vacuously.
        self.assertRefuses("BAD_SELECTOR", selector="checks[*].status")

    def test_level_a_without_an_operator_is_an_unbacked_claim(self):
        self.assertRefuses("UNBACKED_CLAIM", level="A", operator=None,
                           expected=None, tolerance=None, falsifier=None)

    def test_level_b_without_an_operator_is_also_unbacked(self):
        self.assertRefuses("UNBACKED_CLAIM", operator=None, expected=None,
                           tolerance=None, falsifier=None)

    def test_a_bad_level_does_not_load(self):
        self.assertRefuses("BAD_LEVEL", level="E")

    def test_a_bad_loop_does_not_load(self):
        self.assertRefuses("BAD_LOOP", loop="L9")

    def test_every_required_field_is_required(self):
        for field in ("id", "rule", "level", "loop", "on_fail", "source"):
            with self.subTest(field=field):
                entry = {k: v for k, v in BASE.items() if k != field}
                with self.assertRaises(AssertionSyntaxError):
                    load_assertion(entry)

    def test_on_fail_is_required_because_a_report_nobody_can_act_on_is_noise(self):
        with self.assertRaises(AssertionSyntaxError):
            build(on_fail="   ")

    def test_an_operator_needing_expected_does_not_load_without_it(self):
        entry = {k: v for k, v in BASE.items() if k != "expected"}
        with self.assertRaises(AssertionSyntaxError):
            load_assertion(entry)

    def test_is_true_needs_no_expected(self):
        entry = {k: v for k, v in BASE.items() if k != "expected"}
        entry.update(operator="is_true", tolerance=None)
        entry.pop("tolerance", None)
        self.assertEqual(load_assertion(entry).operator, "is_true")

    def test_a_set_falsifier_without_a_value_does_not_load(self):
        self.assertRefuses("BAD_FALSIFIER",
                           falsifier={"operation": "set", "path": "summary.FAIL"})

    def test_an_unknown_falsifier_operation_does_not_load(self):
        self.assertRefuses("BAD_FALSIFIER",
                           falsifier={"operation": "patch",
                                      "path": "summary.FAIL", "value": 1})

    def test_a_non_object_assertion_does_not_load(self):
        with self.assertRaises(AssertionSyntaxError):
            load_assertion(["not", "an", "object"])


# --------------------------------------------------------------------------- #
# Selector language
# --------------------------------------------------------------------------- #

class SelectorTest(unittest.TestCase):
    def test_a_plain_dotted_path_parses(self):
        self.assertEqual(parse_selector("summary.FAIL"), ("summary", "FAIL"))

    def test_an_index_parses(self):
        self.assertEqual(parse_selector("checks[0].status"),
                         ("checks", 0, "status"))

    def test_repeated_indices_parse(self):
        self.assertEqual(parse_selector("grid[1][2]"), ("grid", 1, 2))

    def test_an_empty_selector_is_refused(self):
        with self.assertRaises(AssertionSyntaxError):
            parse_selector("   ")

    def test_a_trailing_dot_is_refused(self):
        with self.assertRaises(AssertionSyntaxError):
            parse_selector("summary.")

    def test_a_quoted_key_is_refused(self):
        with self.assertRaises(AssertionSyntaxError):
            parse_selector('summary["FAIL"]')

    def test_a_negative_index_is_refused(self):
        with self.assertRaises(AssertionSyntaxError):
            parse_selector("checks[-1]")

    def test_missing_is_distinct_from_a_json_null(self):
        # A report may legitimately carry null; folding that into "absent"
        # would lose the difference between "nothing applies" and "the tool
        # does not have this field".
        document = {"threshold": None}
        self.assertIsNone(select(document, ("threshold",)))
        self.assertIs(select(document, ("nope",)), MISSING)

    def test_indexing_past_the_end_is_missing_not_an_error(self):
        self.assertIs(select({"checks": [1]}, ("checks", 5)), MISSING)

    def test_indexing_a_dict_is_missing_rather_than_a_crash(self):
        self.assertIs(select({"checks": {"a": 1}}, ("checks", 0)), MISSING)


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #

class OperatorTest(unittest.TestCase):
    def test_at_most_holds_at_the_threshold(self):
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 0}}))
        self.assertEqual(out.status, PASS)

    def test_at_most_fails_above_it(self):
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 1}}))
        self.assertEqual(out.status, FAIL)

    def test_the_failure_carries_the_matrix_action(self):
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 1}}))
        self.assertEqual(out.to_dict()["on_fail"], "duzelt")

    def test_a_margin_is_always_reported_for_numeric_operators(self):
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 3}}))
        self.assertEqual(out.margin, 3.0)

    def test_a_boolean_is_not_a_number(self):
        # True >= 0.5 is arithmetically true and semantically nonsense.
        out = evaluate(build(), evidence_for({"summary": {"FAIL": True}}))
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, TYPE_MISMATCH)

    def test_a_string_where_a_number_belongs_is_no_coverage(self):
        out = evaluate(build(), evidence_for({"summary": {"FAIL": "0"}}))
        self.assertEqual(out.reason, TYPE_MISMATCH)

    def test_is_true_refuses_truthiness(self):
        # A non-empty list of violations is truthy; an assertion accepting it
        # would pass on the evidence of its own failure.
        assertion = build(operator="is_true", expected=None, tolerance=None,
                          selector="summary.clean",
                          falsifier={"operation": "set",
                                     "path": "summary.clean", "value": False})
        self.assertEqual(
            evaluate(assertion, evidence_for({"summary": {"clean": ["x"]}})).status,
            FAIL)
        self.assertEqual(
            evaluate(assertion, evidence_for({"summary": {"clean": 1}})).status,
            FAIL)
        self.assertEqual(
            evaluate(assertion, evidence_for({"summary": {"clean": True}})).status,
            PASS)

    def test_count_operators_read_a_length_not_a_value(self):
        assertion = build(operator="count_at_most", expected=0, tolerance=0,
                          selector="violations",
                          falsifier={"operation": "set", "path": "violations",
                                     "value": [1, 2]})
        self.assertEqual(evaluate(assertion, evidence_for({"violations": []})).status,
                         PASS)
        self.assertEqual(
            evaluate(assertion, evidence_for({"violations": [1]})).status, FAIL)

    def test_a_count_operator_on_a_number_is_no_coverage(self):
        assertion = build(operator="count_at_most", expected=0, tolerance=0,
                          selector="violations",
                          falsifier={"operation": "set", "path": "violations",
                                     "value": [1]})
        out = evaluate(assertion, evidence_for({"violations": 0}))
        self.assertEqual(out.reason, TYPE_MISMATCH)

    def test_one_of_checks_membership(self):
        assertion = build(operator="one_of", expected=["GREEN", "SCREEN_ONLY"],
                          tolerance=None, selector="status",
                          falsifier={"operation": "set", "path": "status",
                                     "value": "RED"})
        self.assertEqual(evaluate(assertion, evidence_for({"status": "GREEN"})).status,
                         PASS)
        self.assertEqual(evaluate(assertion, evidence_for({"status": "RED"})).status,
                         FAIL)

    def test_equals_is_exact_for_strings(self):
        assertion = build(operator="equals", expected="mm", tolerance=0,
                          selector="units",
                          falsifier={"operation": "set", "path": "units",
                                     "value": "mil"})
        self.assertEqual(evaluate(assertion, evidence_for({"units": "mm"})).status,
                         PASS)


# --------------------------------------------------------------------------- #
# The three measurement traps
# --------------------------------------------------------------------------- #

class MeasurementTrapTest(unittest.TestCase):
    def test_a_missing_field_is_no_coverage_never_a_pass(self):
        # Error class 2. Comparing None against a threshold is how 0 == 0
        # once got reported as agreement.
        out = evaluate(build(selector="summary.NOPE"),
                       evidence_for({"summary": {"FAIL": 0}}))
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, PATH_ABSENT)

    def test_a_missing_field_is_not_a_failure_either(self):
        out = evaluate(build(selector="summary.NOPE"),
                       evidence_for({"summary": {"FAIL": 0}}))
        self.assertNotEqual(out.status, FAIL)

    def test_the_float_boundary_fails_strict_and_passes_tolerant(self):
        # Error class 1, and the exact arithmetic that bit this repo twice:
        # (0.7 - 0.4) / 2 is 0.1499999... so an annular ring exactly on a
        # 0.15 mm limit reads as a violation.
        ring = (0.7 - 0.4) / 2
        self.assertLess(ring, 0.15)          # the trap is real, not folklore
        strict = build(selector="ring", operator="at_least", expected=0.15,
                       tolerance=0,
                       falsifier={"operation": "set", "path": "ring",
                                  "value": 0.0})
        tolerant = build(selector="ring", operator="at_least", expected=0.15,
                         tolerance=1e-9,
                         falsifier={"operation": "set", "path": "ring",
                                    "value": 0.0})
        self.assertEqual(evaluate(strict, evidence_for({"ring": ring})).status, FAIL)
        self.assertEqual(evaluate(tolerant, evidence_for({"ring": ring})).status, PASS)

    def test_the_boundary_margin_is_visible_rather_than_silent(self):
        ring = (0.7 - 0.4) / 2
        out = evaluate(build(selector="ring", operator="at_least",
                             expected=0.15, tolerance=0,
                             falsifier={"operation": "set", "path": "ring",
                                        "value": 0.0}),
                       evidence_for({"ring": ring}))
        self.assertLess(out.margin, 0.0)
        self.assertGreater(out.margin, -1e-15)

    def test_an_undeclared_unit_is_no_coverage(self):
        # Error class 3. What is catchable is an unlabelled number being
        # compared against a labelled threshold.
        out = evaluate(build(units="mm", units_selector="units"),
                       evidence_for({"summary": {"FAIL": 0}}))
        self.assertEqual(out.reason, UNITS_UNDECLARED)

    def test_a_mismatched_unit_is_no_coverage_not_a_conversion(self):
        out = evaluate(build(units="mm", units_selector="units"),
                       evidence_for({"summary": {"FAIL": 0}, "units": "mil"}))
        self.assertEqual(out.reason, UNIT_MISMATCH)

    def test_a_matching_unit_lets_the_comparison_through(self):
        out = evaluate(build(units="mm", units_selector="units"),
                       evidence_for({"summary": {"FAIL": 0}, "units": "mm"}))
        self.assertEqual(out.status, PASS)


# --------------------------------------------------------------------------- #
# Evidence binding and freshness
# --------------------------------------------------------------------------- #

class EvidenceTest(unittest.TestCase):
    def test_an_absent_evidence_report_is_no_coverage(self):
        out = evaluate(build(), {})
        self.assertEqual(out.reason, EVIDENCE_MISSING)

    def test_unbound_evidence_is_not_stale_it_is_unbound(self):
        source = Evidence(name="drc", document={"summary": {"FAIL": 0}})
        self.assertFalse(source.bound)
        self.assertTrue(source.freshness()[0])

    def test_a_bound_report_whose_digest_matches_is_fresh(self):
        source = Evidence(name="drc",
                          document={"summary": {"FAIL": 0},
                                    "inputs": {"sha": "abc"}},
                          artifact="board.kicad_pcb", artifact_digest="abc",
                          hash_selector="inputs.sha",
                          hash_path=("inputs", "sha"))
        self.assertTrue(source.bound)
        self.assertTrue(source.freshness()[0])
        self.assertEqual(evaluate(build(), {"drc": source}).status, PASS)

    def test_a_digest_that_moved_makes_every_assertion_no_coverage(self):
        # The artifact changed under a report that still reads green. That is
        # worse than an unchecked rule because it reads as evidence.
        source = Evidence(name="drc",
                          document={"summary": {"FAIL": 0},
                                    "inputs": {"sha": "stale"}},
                          artifact="board.kicad_pcb", artifact_digest="fresh",
                          hash_selector="inputs.sha",
                          hash_path=("inputs", "sha"))
        out = evaluate(build(), {"drc": source})
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, STALE_EVIDENCE)

    def test_a_declared_binding_the_report_does_not_carry_is_stale(self):
        source = Evidence(name="drc", document={"summary": {"FAIL": 0}},
                          artifact="board.kicad_pcb", artifact_digest="abc",
                          hash_selector="inputs.sha",
                          hash_path=("inputs", "sha"))
        self.assertEqual(evaluate(build(), {"drc": source}).reason, STALE_EVIDENCE)


# --------------------------------------------------------------------------- #
# Levels C and D
# --------------------------------------------------------------------------- #

class LevelCTest(unittest.TestCase):
    def filing(self, **record):
        entry = {"id": "c1", "rule": "MSL / paketleme", "level": "C",
                 "loop": "L1", "on_fail": "alternatif parca",
                 "source": "SKILL.md 2"}
        if record:
            entry["record"] = record
        return load_assertion(entry)

    def test_a_complete_record_passes_but_only_as_record_only(self):
        out = evaluate(self.filing(decision="MSL3", source="datasheet p4",
                                   date="2026-08-07"), {})
        self.assertEqual(out.status, PASS)
        self.assertEqual(out.establishes, ESTABLISHES_RECORD_ONLY)

    def test_a_record_only_pass_is_not_counted_as_a_measurement(self):
        # Dressing C up as A is the matrix's named most-dangerous mistake:
        # the line reads "checked" and nothing was measured.
        out = evaluate(self.filing(decision="MSL3", source="datasheet p4",
                                   date="2026-08-07"), {})
        counts = establishment_summary([out])
        self.assertEqual(counts[ESTABLISHES_MEASUREMENT], 0)
        self.assertEqual(counts[ESTABLISHES_RECORD_ONLY], 1)

    def test_a_record_missing_its_source_does_not_close(self):
        out = evaluate(self.filing(decision="MSL3", date="2026-08-07"), {})
        self.assertEqual(out.status, FAIL)
        self.assertEqual(out.reason, RECORD_INCOMPLETE)

    def test_a_record_missing_its_date_does_not_close(self):
        out = evaluate(self.filing(decision="MSL3", source="datasheet p4"), {})
        self.assertEqual(out.status, FAIL)

    def test_a_blank_string_does_not_count_as_a_filled_field(self):
        out = evaluate(self.filing(decision="MSL3", source="   ",
                                   date="2026-08-07"), {})
        self.assertEqual(out.status, FAIL)

    def test_no_record_at_all_is_no_coverage(self):
        out = evaluate(self.filing(), {})
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, RECORD_INCOMPLETE)


class LevelDTest(unittest.TestCase):
    def physical(self, **measurement):
        entry = {"id": "d1", "rule": "Isil dogrulama", "level": "D",
                 "loop": "L3", "on_fail": "bakir revizyonu",
                 "source": "SKILL.md 2"}
        if measurement:
            entry["measurement"] = measurement
        return load_assertion(entry)

    def test_without_a_measurement_it_is_no_coverage(self):
        # Not validated. Not invalidated either.
        out = evaluate(self.physical(), {})
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, MEASUREMENT_ABSENT)

    def test_a_reading_without_its_setup_is_not_evidence(self):
        out = evaluate(self.physical(instrument="FLIR E8", date="2026-08-07"), {})
        self.assertEqual(out.reason, MEASUREMENT_ABSENT)

    def test_a_complete_record_without_an_explicit_verdict_is_no_coverage(self):
        # This layer does not judge a physical reading on the lab's behalf.
        out = evaluate(self.physical(instrument="FLIR E8", setup="25C still air",
                                     baseline="rev A", date="2026-08-07"), {})
        self.assertEqual(out.reason, MEASUREMENT_ABSENT)

    def test_an_explicit_pass_establishes_a_physical_measurement(self):
        out = evaluate(self.physical(instrument="FLIR E8", setup="25C still air",
                                     baseline="rev A", date="2026-08-07",
                                     **{"pass": True}), {})
        self.assertEqual(out.status, PASS)
        self.assertEqual(out.establishes, ESTABLISHES_PHYSICAL)
        self.assertEqual(establishment_summary([out])[ESTABLISHES_PHYSICAL], 1)

    def test_an_explicit_fail_is_a_failure(self):
        out = evaluate(self.physical(instrument="FLIR E8", setup="25C still air",
                                     baseline="rev A", date="2026-08-07",
                                     **{"pass": False}), {})
        self.assertEqual(out.status, FAIL)


# --------------------------------------------------------------------------- #
# Falsifiability
# --------------------------------------------------------------------------- #

class ProofTest(unittest.TestCase):
    def setUp(self):
        self.evidence = evidence_for({"summary": {"FAIL": 0}})

    def test_a_real_falsifier_flips_the_assertion(self):
        self.assertTrue(prove(build(), self.evidence).proven)

    def test_a_falsifier_that_changes_nothing_is_vacuous(self):
        vacuous = build(falsifier={"operation": "set", "path": "summary.FAIL",
                                   "value": 0})
        proof = prove(vacuous, self.evidence)
        self.assertFalse(proof.proven)
        self.assertEqual(proof.reason, VACUOUS_ASSERTION)

    def test_deleting_the_field_proves_nothing_and_says_which_kind(self):
        # The check went blind rather than catching anything. Keeping remove
        # in the language and reporting it is how a falsifier author gets
        # shown the difference.
        blinded = build(falsifier={"operation": "remove",
                                   "path": "summary.FAIL"})
        proof = prove(blinded, self.evidence)
        self.assertFalse(proof.proven)
        self.assertEqual(proof.reason, FALSIFIER_NO_COVERAGE)

    def test_a_falsifier_aimed_at_a_field_that_is_not_there_proves_nothing(self):
        proof = prove(build(falsifier={"operation": "set",
                                       "path": "summary.NOPE", "value": 9}),
                      self.evidence)
        self.assertFalse(proof.proven)
        self.assertEqual(proof.reason, PATH_ABSENT)

    def test_proving_without_the_evidence_is_reported_not_skipped(self):
        proof = prove(build(), {})
        self.assertFalse(proof.proven)
        self.assertEqual(proof.reason, EVIDENCE_MISSING)

    def test_the_proof_does_not_mutate_the_real_report(self):
        prove(build(), self.evidence)
        self.assertEqual(self.evidence["drc"].document["summary"]["FAIL"], 0)

    def test_a_stale_binding_does_not_mask_the_falsifiability_question(self):
        # Mutating the document would make a declared hash binding call it
        # stale, which would hide whether the predicate can break at all.
        source = Evidence(name="drc",
                          document={"summary": {"FAIL": 0},
                                    "inputs": {"sha": "abc"}},
                          artifact="board.kicad_pcb", artifact_digest="abc",
                          hash_selector="inputs.sha",
                          hash_path=("inputs", "sha"))
        self.assertTrue(prove(build(), {"drc": source}).proven)

    def test_levels_c_and_d_have_nothing_to_mutate(self):
        filing = load_assertion({"id": "c1", "rule": "MSL", "level": "C",
                                 "loop": "L1", "on_fail": "x",
                                 "source": "SKILL.md"})
        self.assertIsNone(prove(filing, self.evidence))


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

class RollupTest(unittest.TestCase):
    """RED > NO_COVERAGE > SCREEN_ONLY > GREEN, and each step tested against
    a run that also contains the levels below it."""

    def bound_evidence(self):
        return {"drc": Evidence(name="drc",
                                document={"summary": {"FAIL": 0},
                                          "inputs": {"sha": "abc"}},
                                artifact="b.kicad_pcb", artifact_digest="abc",
                                hash_selector="inputs.sha",
                                hash_path=("inputs", "sha"))}

    def test_bound_and_passing_is_green(self):
        self.assertEqual(rollup([evaluate(build(), self.bound_evidence())])[0],
                         GREEN)

    def test_unbound_evidence_can_never_be_green(self):
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 0}}))
        status, reasons = rollup([out])
        self.assertEqual(status, SCREEN_ONLY)
        self.assertEqual(reasons, [EVIDENCE_UNBOUND])

    def test_no_coverage_outranks_screen_only(self):
        unbound = evaluate(build(), evidence_for({"summary": {"FAIL": 0}}))
        blind = evaluate(build(selector="summary.NOPE"),
                         evidence_for({"summary": {"FAIL": 0}}))
        self.assertEqual(rollup([unbound, blind])[0], NO_COVERAGE)

    def test_red_outranks_no_coverage(self):
        failing = evaluate(build(), evidence_for({"summary": {"FAIL": 4}}))
        blind = evaluate(build(selector="summary.NOPE"),
                         evidence_for({"summary": {"FAIL": 4}}))
        self.assertEqual(rollup([failing, blind])[0], RED)

    def test_an_empty_run_is_no_coverage_not_green(self):
        # The empty comparison at run level: nothing asserted is not agreement.
        status, reasons = rollup([])
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(reasons, ["NO_ASSERTIONS"])

    def test_a_record_only_pass_does_not_make_a_run_screen_only(self):
        filing = load_assertion({"id": "c1", "rule": "MSL", "level": "C",
                                 "loop": "L1", "on_fail": "x",
                                 "source": "SKILL.md",
                                 "record": {"decision": "MSL3",
                                            "source": "ds", "date": "2026"}})
        self.assertEqual(rollup([evaluate(filing, {})])[0], GREEN)


# --------------------------------------------------------------------------- #
# matrix.py - parsing the matrix as data
# --------------------------------------------------------------------------- #

class MarkdownNormalisationTest(unittest.TestCase):
    def test_emphasis_and_backticks_are_stripped(self):
        self.assertEqual(strip_markdown("**DRC** `0` *ihlal*"), "DRC 0 ihlal")

    def test_an_intra_word_underscore_survives(self):
        # REGRESSION (#66, first real run): _EMPHASIS stripped underscores,
        # so PWR_FLAG became PWRFLAG and sch_wire.py became schwire.py. GFM
        # does not treat intra-word underscores as emphasis, and every
        # assertion naming a tool would have missed its row.
        self.assertEqual(strip_markdown("PWR_FLAG ile susturma"),
                         "PWR_FLAG ile susturma")
        self.assertEqual(strip_markdown("`sch_wire.py`"), "sch_wire.py")

    def test_whitespace_collapses(self):
        self.assertEqual(strip_markdown("a   b\n c"), "a b c")

    def test_rule_key_folds_case_but_not_underscores(self):
        self.assertEqual(rule_key("**PWR_FLAG**"), "pwr_flag")

    def test_two_spellings_of_one_rule_share_a_key(self):
        self.assertEqual(rule_key("DRC  0 `ihlal`"), rule_key("**DRC** 0 ihlal"))


class LevelClassificationTest(unittest.TestCase):
    def test_a_plain_level_resolves(self):
        self.assertEqual(classify_level("A")[0], "A")
        self.assertEqual(classify_level("`B`")[0], "B")

    def test_a_compound_level_is_ambiguous_not_flattened(self):
        # "document gate today, file-measurable intended" - which one holds
        # right now is an engineering decision, not a regex's call.
        level, issue, note = classify_level("C→A")
        self.assertIsNone(level)
        self.assertEqual(issue, AMBIGUOUS_LEVEL)
        self.assertIn("decision", note)

    def test_an_ascii_arrow_is_the_same_case(self):
        self.assertEqual(classify_level("C->A")[1], AMBIGUOUS_LEVEL)

    def test_a_partial_level_is_partial_not_plain(self):
        # Asserting A(kısmi) as plain A claims exactly the portion nobody
        # automated.
        level, issue, _ = classify_level("A(kısmi)")
        self.assertIsNone(level)
        self.assertEqual(issue, PARTIAL_LEVEL)

    def test_a_screen_qualifier_is_partial_too(self):
        self.assertEqual(classify_level("A(screen)")[1], PARTIAL_LEVEL)

    def test_an_unrecognised_cell_resolves_to_nothing(self):
        self.assertEqual(classify_level("belki")[1], UNPARSED_LEVEL)

    def test_no_unresolved_cell_ever_yields_a_level(self):
        for cell in ("C→A", "C->D", "A(kısmi)", "A(screen)", "", "-", "AB"):
            with self.subTest(cell=cell):
                self.assertIsNone(classify_level(cell)[0])


class MatrixParsingTest(unittest.TestCase):
    TABLE = (
        "## Faz 2\n"
        "\n"
        "| Kural | Sev | Araç | Döngü | Fail'de |\n"
        "|---|---|---|---|---|\n"
        "| ERC error = 0 | B | `kicad-cli` | L0 | düzelt |\n"
        "| MSL / paketleme | C | karar kaydı | L1 | alternatif |\n"
        "| Konnektör akımı | C→A | — | L2 | derating |\n"
    )

    def test_rows_are_read_with_their_columns(self):
        rows, problems = parse_matrix(self.TABLE)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].rule, "ERC error = 0")
        self.assertEqual(rows[0].level, "B")
        self.assertEqual(rows[0].loop, "L0")
        self.assertEqual(rows[0].on_fail, "düzelt")
        self.assertEqual(rows[0].section, "Faz 2")

    def test_columns_are_found_by_heading_not_by_position(self):
        swapped = self.TABLE.replace("| Kural | Sev |", "| Sev | Kural |")
        swapped = swapped.replace("| ERC error = 0 | B |", "| B | ERC error = 0 |")
        rows, _ = parse_matrix(swapped)
        self.assertEqual(rows[0].rule, "ERC error = 0")
        self.assertEqual(rows[0].level, "B")

    def test_an_unresolvable_level_keeps_its_raw_cell(self):
        rows, _ = parse_matrix(self.TABLE)
        compound = rows[2]
        self.assertIsNone(compound.level)
        self.assertEqual(compound.level_raw, "C→A")
        self.assertEqual(compound.level_issue, AMBIGUOUS_LEVEL)

    def test_a_rule_table_with_no_level_column_is_a_reported_problem(self):
        # The matrix's own first sentence: a rule without a level is not a
        # rule. Skipping the table silently would hide that.
        text = ("| Kural | Araç |\n|---|---|\n| bir kural | bir araç |\n")
        rows, problems = parse_matrix(text)
        self.assertEqual(rows, [])
        self.assertEqual(problems[0]["issue"], "NO_LEVEL_COLUMN")

    def test_a_table_that_is_not_a_rule_table_is_left_alone(self):
        text = ("| Araç | Ne yapar |\n|---|---|\n| x.py | bir şey |\n")
        rows, problems = parse_matrix(text)
        self.assertEqual((rows, problems), ([], []))

    def test_a_short_row_is_reported_rather_than_shifted(self):
        text = self.TABLE + "| yarim satir |\n"
        _, problems = parse_matrix(text)
        self.assertEqual(problems[-1]["issue"], "SHORT_ROW")

    def test_the_real_matrix_parses_without_errors(self):
        if not MATRIX.exists():                       # pragma: no cover
            self.skipTest("dogrulama-matrisi/SKILL.md is not in this checkout")
        rows, problems = parse_matrix(MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(problems, [])
        self.assertGreater(len(rows), 0)

    def test_the_real_matrix_still_contains_unresolvable_levels(self):
        # Not a defect: these six rows are a pending engineering decision,
        # and the test exists so a future parser change cannot quietly
        # flatten them into plain levels.
        if not MATRIX.exists():                       # pragma: no cover
            self.skipTest("dogrulama-matrisi/SKILL.md is not in this checkout")
        rows, _ = parse_matrix(MATRIX.read_text(encoding="utf-8"))
        unresolved = [r for r in rows if r.level is None]
        self.assertTrue(unresolved)
        for row in unresolved:
            self.assertIn(row.level_issue, (AMBIGUOUS_LEVEL, PARTIAL_LEVEL,
                                            UNPARSED_LEVEL))


class CrossReferenceTest(unittest.TestCase):
    TABLE = (
        "## Faz 2\n"
        "| Kural | Sev | Araç | Döngü | Fail'de |\n"
        "|---|---|---|---|---|\n"
        "| DRC 0 ihlal (repo rule set) | B | drc | L0 | duzelt |\n"
        "| MSL / paketleme | C | karar | L1 | alternatif |\n"
    )

    def rows(self):
        return parse_matrix(self.TABLE)[0]

    def test_a_matching_assertion_is_matched(self):
        coverage = cross_reference(self.rows(), [build()])
        self.assertEqual(len(coverage.matched), 1)
        self.assertNotIn("issue", coverage.matched[0])

    def test_matching_ignores_markdown_and_case(self):
        assertion = build(rule="**drc 0 ihlal** (repo rule set)")
        coverage = cross_reference(self.rows(), [assertion])
        self.assertEqual(coverage.orphans, [])

    def test_matching_is_exact_never_fuzzy(self):
        # A near-miss must NOT be re-pointed at the neighbouring row. Fuzzy
        # matching is how an assertion silently starts checking the wrong
        # rule the moment somebody rewords one.
        assertion = build(rule="DRC 0 ihlal")
        coverage = cross_reference(self.rows(), [assertion])
        self.assertEqual(len(coverage.orphans), 1)
        self.assertEqual(coverage.orphans[0]["issue"], "ORPHAN_ASSERTION")

    def test_a_rule_nobody_asserted_is_listed(self):
        coverage = cross_reference(self.rows(), [build()])
        self.assertEqual([r.rule for r in coverage.unasserted],
                         ["MSL / paketleme"])

    def test_a_level_the_matrix_disagrees_with_is_a_defect(self):
        # The level decides what a pass may establish, so the two cannot
        # differ - a C asserted as B claims a measurement nobody made.
        assertion = build(level="A")
        coverage = cross_reference(self.rows(), [assertion])
        self.assertEqual(coverage.matched[0]["issue"], "LEVEL_DISAGREEMENT")

    def test_a_loop_disagreement_is_reported(self):
        coverage = cross_reference(self.rows(), [build(loop="L2")])
        self.assertEqual(coverage.matched[0]["issue"], "LOOP_DISAGREEMENT")

    def test_an_assertion_on_an_unresolvable_row_inherits_the_issue(self):
        text = ("| Kural | Sev | Araç | Döngü | Fail'de |\n|---|---|---|---|---|\n"
                "| DRC 0 ihlal (repo rule set) | C→A | drc | L0 | duzelt |\n")
        coverage = cross_reference(parse_matrix(text)[0], [build()])
        self.assertEqual(coverage.matched[0]["issue"], AMBIGUOUS_LEVEL)


# --------------------------------------------------------------------------- #
# Findings and overall verdict
# --------------------------------------------------------------------------- #

class MatrixFindingTest(unittest.TestCase):
    def coverage(self, assertions):
        text = ("| Kural | Sev | Araç | Döngü | Fail'de |\n|---|---|---|---|---|\n"
                "| DRC 0 ihlal (repo rule set) | B | drc | L0 | duzelt |\n"
                "| MSL / paketleme | C | karar | L1 | alternatif |\n")
        return cross_reference(parse_matrix(text)[0], assertions)

    def test_an_unasserted_rule_is_no_coverage_not_a_failure(self):
        # REGRESSION: this was FAIL in the first cut. A rule nobody asserted
        # is silence, not a violation - reporting it as RED means a run can
        # never be green until every prose row is mechanised, which trains
        # readers to ignore the colour.
        gap, _ = matrix_findings(self.coverage([build()]))
        self.assertEqual(gap["check"], "rules_asserted")
        self.assertEqual(gap["status"], NO_COVERAGE)
        self.assertEqual(len(gap["unasserted"]), 1)

    def test_an_orphan_assertion_IS_a_failure(self):
        # It passes forever and reads as coverage.
        _, binding = matrix_findings(self.coverage([build(rule="yok boyle kural")]))
        self.assertEqual(binding["check"], "assertion_rule_binding")
        self.assertEqual(binding["status"], FAIL)

    def test_full_coverage_passes(self):
        filing = load_assertion({"id": "c1", "rule": "MSL / paketleme",
                                 "level": "C", "loop": "L1",
                                 "on_fail": "alternatif", "source": "SKILL.md",
                                 "record": {"decision": "MSL3", "source": "ds",
                                            "date": "2026-08-07"}})
        gap, binding = matrix_findings(self.coverage([build(), filing]))
        self.assertEqual(gap["status"], PASS)
        self.assertEqual(binding["status"], PASS)

    def test_a_matrix_that_parsed_to_nothing_is_no_coverage(self):
        empty = cross_reference([], [])
        self.assertEqual(matrix_findings(empty)[0]["status"], NO_COVERAGE)


class OverallTest(unittest.TestCase):
    def test_a_failing_finding_is_red_whatever_the_assertions_say(self):
        from common.findings import Finding
        bad = Finding("assertion_rule_binding", FAIL, 1, [{"x": 1}], "")
        status, _ = overall([bad], [])
        self.assertEqual(status, RED)

    def test_screen_only_survives_only_when_nothing_else_lowered_it(self):
        from common.findings import result
        ok = result("assertions_hold", 1, [], "")
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 0}}))
        self.assertEqual(overall([ok], [out])[0], SCREEN_ONLY)

    def test_a_no_coverage_finding_beats_a_screen_only_rollup(self):
        from common.findings import Finding
        gap = Finding("rules_asserted", NO_COVERAGE, 0, [], "")
        out = evaluate(build(), evidence_for({"summary": {"FAIL": 0}}))
        self.assertEqual(overall([gap], [out])[0], NO_COVERAGE)


# --------------------------------------------------------------------------- #
# Assertion set loading
# --------------------------------------------------------------------------- #

class LoadSetTest(unittest.TestCase):
    def write(self, payload):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        json.dump(payload, handle)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(path.unlink)
        return path

    def test_a_wrong_schema_version_is_refused(self):
        path = self.write({"schema_version": "nope", "assertions": [BASE]})
        with self.assertRaises(ValueError):
            load_set(path)

    def test_an_empty_assertion_list_is_refused(self):
        # A set that asserts nothing would otherwise roll up as vacuously fine.
        path = self.write({"schema_version": "assertion-set/v1",
                           "assertions": []})
        with self.assertRaises(ValueError):
            load_set(path)

    def test_one_malformed_assertion_does_not_hide_the_others(self):
        broken = {k: v for k, v in BASE.items() if k != "tolerance"}
        broken["id"] = "broken"
        path = self.write({"schema_version": "assertion-set/v1",
                           "assertions": [BASE, broken]})
        assertions, errors, _ = load_set(path)
        self.assertEqual([a.id for a in assertions], ["a1"])
        self.assertEqual(errors[0]["issue"], "MISSING_TOLERANCE")

    def test_a_duplicate_id_is_reported_rather_than_silently_dropped(self):
        path = self.write({"schema_version": "assertion-set/v1",
                           "assertions": [BASE, dict(BASE)]})
        assertions, errors, _ = load_set(path)
        self.assertEqual(len(assertions), 1)
        self.assertEqual(errors[0]["issue"], "DUPLICATE_ID")

    def test_the_shipped_fixture_loads_clean(self):
        assertions, errors, evidence = load_set(FIXTURE)
        self.assertEqual(errors, [])
        self.assertTrue(assertions)
        self.assertEqual(sorted(evidence),
                         ["coupling", "crosstalk", "dfm", "drc", "erc",
                          "interconnect", "measure"])

    def test_every_fixture_selector_names_a_path_its_tool_actually_writes(self):
        """The gap this set shipped with for a year.

        Every selector used to name a ``summary.*`` path that no tool in this
        tree emits, so the first run against a real report returned
        NO_COVERAGE six times out of six - indistinguishable, in a report,
        from assertions nobody had gotten around to running. The engine was
        covered; the BINDING was not, because every engine test builds its
        evidence inline.

        This test closes that by reading the shape from the producing tools
        themselves rather than from a copy: a report shape that drifts away
        from its assertion now fails here instead of on the day someone
        finally points the set at a board.
        """
        # drc/erc: build a REAL report from the producing function, so a
        # renamed metric fails here rather than being copied into this test.
        reports = {mode: sample_drc_gate_report(mode) for mode in ("drc", "erc")}
        # interconnect_mating.py imports no pcbnew, so its report is built for
        # real too: a renamed rule_coverage key fails HERE rather than on the
        # day somebody points the set at a chain.
        reports["interconnect"] = sample_interconnect_report()

        # measure_board.py imports pcbnew, which this interpreter does not
        # have (it lives in KiCad's own 3.11). Its report therefore cannot be
        # built here, so the check drops to the source line that writes the
        # key - weaker than the two above, and enough to catch a rename.
        measure_source = (TOOLS / "scripts" / "board"
                          / "measure_board.py").read_text(encoding="utf-8")

        assertions, _, _ = load_set(FIXTURE)
        for assertion in assertions:
            with self.subTest(assertion=assertion.id):
                if not assertion.executable:
                    # Nothing to bind: level C and D read a filing or a
                    # measurement record, not a path in a tool's report.
                    continue
                path = parse_selector(assertion.selector, assertion.id)
                if assertion.evidence in reports:
                    self.assertIsNot(
                        select(reports[assertion.evidence], path), MISSING,
                        f"{assertion.id} reads {assertion.selector!r}, which "
                        f"drc_gate.py does not write in {assertion.evidence} "
                        "mode")
                else:
                    self._check_against_producer(assertion, measure_source)

    # Which file produces each evidence source. Kept here rather than in the
    # set file because the point is to read the shape from the PRODUCER: a
    # second copy of the shape is the drift this test exists to catch.
    # Evidence sources whose report this interpreter can actually build, so
    # their binding is verified rather than pinned to the board-tool
    # convention. Kept beside PRODUCERS for the same reason: read the shape
    # from the producer, never from a second copy.
    VERIFIABLE_BINDINGS = {"interconnect": sample_interconnect_report}

    PRODUCERS = {
        "measure": ("board", "measure_board.py"),
        "dfm": ("board", "dfm_emc_check.py"),
        "coupling": ("board", "pair_coupling.py"),
        "crosstalk": (None, "crosstalk_thinning.py"),
    }

    def _producer_source(self, evidence_name):
        sub, filename = self.PRODUCERS[evidence_name]
        base = TOOLS / "scripts"
        return (base / sub / filename if sub else base / filename).read_text(
            encoding="utf-8")

    def _check_against_producer(self, assertion, measure_source):
        """Weaker than building the report, and enough to catch a rename.

        These tools import pcbnew, which lives in KiCad's own interpreter, so
        their reports cannot be constructed here. What can be checked is that
        the key the assertion reads is a string the producing file writes.
        """
        source = self._producer_source(assertion.evidence)
        block, _, rest = assertion.selector.partition(".")
        if block == "measurements":
            self.assertIn(f'measurements["{rest}"]', measure_source,
                          f"{assertion.id} reads {assertion.selector!r}, "
                          "which measure_board.py does not write")
        elif block == "by_check":
            # by_check is the shared name-keyed coverage map, so a tool gets it
            # either by building it itself or by routing through board_report()
            # - dfm_emc_check does the latter, and looking for the literal
            # string in its source would only prove which of the two it chose.
            # What varies per tool, and what a rename would break, is the CHECK
            # NAME.
            check_name = rest.partition(".")[0]
            self.assertTrue(
                "by_check" in source or "board_report" in source,
                f"{assertion.evidence} neither emits by_check nor routes "
                "through board_report()")
            self.assertIn(f'"{check_name}"', source,
                          f"{assertion.id} reads check {check_name!r}, which "
                          f"{assertion.evidence} does not produce")
        elif block == "coverage":
            self.assertIn('"coverage"', source,
                          f"{assertion.id} reads {assertion.selector!r}, but "
                          f"{assertion.evidence} emits no coverage block")
            self.assertIn(f'"{rest}"', source,
                          f"{assertion.id} reads coverage.{rest}, which "
                          f"{assertion.evidence} does not write")
        else:
            self.fail(f"{assertion.id} reads {assertion.selector!r}: this test "
                      f"does not know how to verify the {block!r} block "
                      "against its producer. Teach it rather than removing it")

    def test_every_fixture_evidence_source_declares_a_hash_binding(self):
        """Without it the best a run can do is SCREEN_ONLY - and that was the
        state measure_board.py was in: it emitted no digest at all, so no
        assertion resting on it could ever have been GREEN."""
        _, _, evidence = load_set(FIXTURE)
        for name, binding in evidence.items():
            with self.subTest(evidence=name):
                if binding.get("unbound_reason"):
                    # An unbindable source is allowed and must SAY SO. Some
                    # tools genuinely have no artifact to hash - crosstalk
                    # takes CLI geometry numbers, not a design file - and the
                    # run correctly caps at SCREEN_ONLY. What is not allowed is
                    # omitting the binding silently, because then "unbindable"
                    # and "somebody forgot" look identical.
                    self.assertNotIn("artifact", binding)
                    self.assertNotIn("hash_selector", binding)
                    continue
                self.assertTrue(binding.get("artifact"))
                builder = self.VERIFIABLE_BINDINGS.get(name)
                if builder is None:
                    # A board tool: its report needs pcbnew and cannot be built
                    # in this interpreter, so the binding is pinned to the
                    # shared envelope's field name.
                    self.assertEqual(binding.get("hash_selector"),
                                     "design.sha256")
                    continue
                # Not a board tool. `design.sha256` would be the wrong NAME here
                # - this gate reads no design file, it compares two documents -
                # so instead of pinning a convention the binding is CHECKED: the
                # declared selector must resolve to a digest in a real report.
                # Weaker convention, stronger evidence.
                selector = binding.get("hash_selector")
                self.assertTrue(selector, f"{name} declares no hash_selector")
                digest = select(builder(), parse_selector(selector, name))
                self.assertIsNot(digest, MISSING,
                                 f"{name} declares hash_selector {selector!r}, "
                                 "which its producer does not write")
                self.assertIsInstance(digest, str, f"{name}: {selector} is not "
                                                   "a digest string")
                self.assertEqual(len(digest), 64,
                                 f"{name}: {selector} is not a sha256")

    def test_every_fixture_assertion_declares_where_its_rule_came_from(self):
        # Constraint #3: no invented threshold. The source field is what
        # makes that auditable rather than asserted.
        assertions, _, _ = load_set(FIXTURE)
        for assertion in assertions:
            with self.subTest(assertion=assertion.id):
                self.assertIn("dogrulama-matrisi", assertion.source)

    def test_every_fixture_assertion_is_falsifiable(self):
        assertions, _, _ = load_set(FIXTURE)
        for assertion in assertions:
            with self.subTest(assertion=assertion.id):
                if not assertion.executable:
                    # A level C or D assertion has no predicate to mutate: what
                    # falsifies it is an incomplete filing or a missing
                    # measurement record, and the loader already enforces those
                    # contracts (RECORD_FIELDS / MEASUREMENT_FIELDS plus an
                    # explicit pass boolean). Demanding a document falsifier
                    # here would only be satisfiable by giving it a fake
                    # predicate.
                    self.assertIn(assertion.level, ("C", "D"))
                    self.assertIsNone(assertion.operator)
                    continue
                self.assertIsNotNone(assertion.falsifier)


# --------------------------------------------------------------------------- #
# CLI, end to end
# --------------------------------------------------------------------------- #

class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir,
                                                            ignore_errors=True))

    def write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def simple_set(self, **overrides):
        return self.write("set.json", {
            "schema_version": "assertion-set/v1",
            "assertions": [{**BASE, **overrides}]})

    def run_main(self, argv):
        """main() returns the exit code; JSON goes to the --json file.

        Called in process rather than through subprocess because these tests
        assert on the report CONTENT, and the report is written to --json
        either way. stdout and stderr are swallowed so a hundred-line report
        per test does not bury the rest of the suite; ``SubprocessTest``
        below still exercises the real argv path once.
        """
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def test_list_operators_exits_clean(self):
        self.assertEqual(self.run_main(["--list-operators"]), 0)

    def test_self_test_passes(self):
        self.assertEqual(self.run_main(["--self-test"]), 0)

    def test_a_set_with_no_evidence_is_not_green(self):
        report = self.dir / "out.json"
        code = self.run_main(["--set", str(self.simple_set()),
                              "--json", str(report)])
        self.assertNotEqual(code, 0)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], NO_COVERAGE)

    def test_unbound_evidence_gives_a_real_result_but_not_green(self):
        drc = self.write("drc.json", {"summary": {"FAIL": 0}})
        report = self.dir / "out.json"
        code = self.run_main(["--set", str(self.simple_set()),
                              "--evidence", f"drc={drc}",
                              "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], SCREEN_ONLY)
        self.assertNotEqual(code, 0)

    def test_a_violation_is_red_and_exits_non_zero(self):
        drc = self.write("drc.json", {"summary": {"FAIL": 5}})
        report = self.dir / "out.json"
        code = self.run_main(["--set", str(self.simple_set()),
                              "--evidence", f"drc={drc}",
                              "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], RED)
        self.assertNotEqual(code, 0)

    def test_a_bound_and_fresh_run_is_green_and_exits_zero(self):
        board = self.dir / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")
        from common.hashing import sha256_file
        drc = self.write("drc.json", {"summary": {"FAIL": 0},
                                      "inputs": {"board_sha256":
                                                 sha256_file(board)}})
        assertion_set = self.write("set.json", {
            "schema_version": "assertion-set/v1",
            "evidence": {"drc": {"artifact": "board.kicad_pcb",
                                 "hash_selector": "inputs.board_sha256"}},
            "assertions": [BASE]})
        report = self.dir / "out.json"
        code = self.run_main(["--set", str(assertion_set),
                              "--evidence", f"drc={drc}",
                              "--root", str(self.dir), "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], GREEN)
        self.assertEqual(code, 0)

    def test_a_byte_change_in_the_board_drops_the_run_to_no_coverage(self):
        # Staleness caught, and caught as absence of coverage rather than as
        # a design failure: the report is not wrong, it is about another file.
        board = self.dir / "board.kicad_pcb"
        board.write_text("(kicad_pcb)", encoding="utf-8")
        from common.hashing import sha256_file
        drc = self.write("drc.json", {"summary": {"FAIL": 0},
                                      "inputs": {"board_sha256":
                                                 sha256_file(board)}})
        assertion_set = self.write("set.json", {
            "schema_version": "assertion-set/v1",
            "evidence": {"drc": {"artifact": "board.kicad_pcb",
                                 "hash_selector": "inputs.board_sha256"}},
            "assertions": [BASE]})
        board.write_text("(kicad_pcb edited)", encoding="utf-8")
        report = self.dir / "out.json"
        self.run_main(["--set", str(assertion_set), "--evidence", f"drc={drc}",
                       "--root", str(self.dir), "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], NO_COVERAGE)
        self.assertEqual(payload["outcomes"][0]["reason"], STALE_EVIDENCE)

    def test_a_declared_artifact_that_is_missing_fails_rather_than_falling_back(self):
        # The attacker-shaped mistake: if a typo in the artifact path quietly
        # downgraded to "no binding declared", misspelling the path would be
        # the cheapest way to silence a staleness check forever.
        drc = self.write("drc.json", {"summary": {"FAIL": 0}})
        assertion_set = self.write("set.json", {
            "schema_version": "assertion-set/v1",
            "evidence": {"drc": {"artifact": "typo.kicad_pcb",
                                 "hash_selector": "inputs.board_sha256"}},
            "assertions": [BASE]})
        report = self.dir / "out.json"
        self.run_main(["--set", str(assertion_set), "--evidence", f"drc={drc}",
                       "--root", str(self.dir), "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], RED)
        binding = [f for f in payload["findings"]
                   if f["check"] == "evidence_bindable"][0]
        self.assertEqual(binding["status"], FAIL)
        self.assertEqual(binding["violations"][0]["issue"], "ARTIFACT_MISSING")

    def test_prove_reports_a_vacuous_assertion(self):
        drc = self.write("drc.json", {"summary": {"FAIL": 0}})
        vacuous = self.simple_set(falsifier={"operation": "set",
                                             "path": "summary.FAIL",
                                             "value": 0})
        report = self.dir / "out.json"
        self.run_main(["--set", str(vacuous), "--evidence", f"drc={drc}",
                       "--prove", "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        proof = [f for f in payload["findings"]
                 if f["check"] == "assertions_falsifiable"][0]
        self.assertEqual(proof["status"], FAIL)
        self.assertEqual(payload["proofs"][0]["reason"], VACUOUS_ASSERTION)

    def test_prove_passes_when_the_fault_is_caught(self):
        drc = self.write("drc.json", {"summary": {"FAIL": 0}})
        report = self.dir / "out.json"
        self.run_main(["--set", str(self.simple_set()),
                       "--evidence", f"drc={drc}", "--prove",
                       "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        proof = [f for f in payload["findings"]
                 if f["check"] == "assertions_falsifiable"][0]
        self.assertEqual(proof["status"], PASS)

    def test_the_report_counts_passes_by_what_they_establish(self):
        drc = self.write("drc.json", {"summary": {"FAIL": 0}})
        report = self.dir / "out.json"
        self.run_main(["--set", str(self.simple_set()),
                       "--evidence", f"drc={drc}", "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["establishes"],
                         {"measurement": 1, "record_only": 0,
                          "physical_measurement": 0})

    def test_the_report_says_it_measures_nothing(self):
        drc = self.write("drc.json", {"summary": {"FAIL": 0}})
        report = self.dir / "out.json"
        self.run_main(["--set", str(self.simple_set()),
                       "--evidence", f"drc={drc}", "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(payload["measures_nothing"])
        self.assertEqual(payload["schema_version"], "assertion-report/v1")

    def test_a_malformed_evidence_argument_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_main(["--set", str(self.simple_set()),
                           "--evidence", "no-equals-sign"])

    def test_set_is_required(self):
        with self.assertRaises(ValueError):
            self.run_main([])

    def test_the_fixture_runs_against_the_real_matrix(self):
        if not MATRIX.exists():                       # pragma: no cover
            self.skipTest("dogrulama-matrisi/SKILL.md is not in this checkout")
        report = self.dir / "out.json"
        self.run_main(["--set", str(FIXTURE), "--matrix", str(MATRIX),
                       "--json", str(report)])
        payload = json.loads(report.read_text(encoding="utf-8"))
        coverage = payload["matrix_coverage"]
        self.assertGreater(coverage["rules_parsed"], 0)
        self.assertEqual(coverage["orphan_assertions"], [],
                         "every fixture assertion must name a real matrix row")


class SubprocessTest(unittest.TestCase):
    """One real argv run, so the module-level import path and the __main__
    wrapper are exercised the way a caller actually invokes them."""

    def test_self_test_passes_as_a_subprocess(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--self-test"],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SELF-TEST PASS", completed.stdout + completed.stderr)


# --------------------------------------------------------------------------- #
# Coverage and authority - the two axes that used to hide inside the level
# --------------------------------------------------------------------------- #

class CoverageGuardTest(unittest.TestCase):
    """The EPAD vacuity, pinned.

    A check that examined nothing reports zero violations, because its
    violation list is empty. Comparing that count against a threshold is the
    empty comparison, and it reads in every report as a clean pass.
    """

    def _doc(self, scanned, violations=0):
        return {"summary": {"FAIL": 0},
                "metrics": {"epad_paste_coverage": violations},
                "by_check": {"epad_paste_coverage": {
                    "status": NO_COVERAGE if scanned == 0 else PASS,
                    "scanned": scanned, "violations": violations}}}

    def test_the_old_metrics_binding_passes_on_a_board_with_nothing_to_check(self):
        # Kept as a regression witness. NOTE the document here is hand-built:
        # board_report() no longer emits a numeric 0 for an unmeasured check
        # (it emits null), so this exact shape can only come from a tool that
        # assembles its own envelope - which is how drc_gate.py shipped the
        # same bug. The witness is what the selector does when it IS handed a
        # zero, and that is why coverage_selector exists independently of the
        # null fix.
        vacuous = build(selector="metrics.epad_paste_coverage",
                        falsifier={"operation": "set",
                                   "path": "metrics.epad_paste_coverage",
                                   "value": 3})
        outcome = evaluate(vacuous, evidence_for(self._doc(scanned=0)))
        self.assertEqual(outcome.status, PASS)

    def test_the_same_rule_bound_to_coverage_reports_no_coverage(self):
        guarded = build(selector="metrics.epad_paste_coverage",
                        coverage_waiver=None,
                        coverage_selector="by_check.epad_paste_coverage.scanned",
                        falsifier={"operation": "set",
                                   "path": "metrics.epad_paste_coverage",
                                   "value": 3})
        outcome = evaluate(guarded, evidence_for(self._doc(scanned=0)))
        self.assertEqual(outcome.status, NO_COVERAGE)
        self.assertEqual(outcome.reason, NOTHING_SCANNED)

    def test_it_passes_once_something_was_actually_examined(self):
        guarded = build(selector="metrics.epad_paste_coverage",
                        coverage_waiver=None,
                        coverage_selector="by_check.epad_paste_coverage.scanned",
                        falsifier={"operation": "set",
                                   "path": "metrics.epad_paste_coverage",
                                   "value": 3})
        outcome = evaluate(guarded, evidence_for(self._doc(scanned=7)))
        self.assertEqual(outcome.status, PASS)

    def test_an_absent_coverage_field_is_not_silently_forgiven(self):
        guarded = build(coverage_waiver=None,
                        coverage_selector="by_check.nope.scanned")
        outcome = evaluate(guarded, evidence_for(self._doc(scanned=7)))
        self.assertEqual(outcome.status, NO_COVERAGE)

    def test_a_machine_rule_must_bind_or_waive_its_denominator(self):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(coverage_waiver=None)
        self.assertEqual(caught.exception.code, "COVERAGE_UNBOUND")

    def test_a_zero_minimum_is_refused_because_it_is_the_vacuity(self):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(coverage_waiver=None,
                  coverage_selector="by_check.x.scanned", coverage_min=0)
        self.assertEqual(caught.exception.code, "COVERAGE_UNBOUND")


class AuthorityTest(unittest.TestCase):
    """A tool producing a number and a tool entitled to close a rule are not
    the same statement, and the level string was carrying both."""

    def test_a_machine_level_must_state_its_authority(self):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(authority=None)
        self.assertEqual(caught.exception.code, "BAD_AUTHORITY")

    def test_a_file_measurable_rule_cannot_accept_hardware(self):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(authority="physical_acceptance")
        self.assertEqual(caught.exception.code, "BAD_AUTHORITY")

    def test_c_and_d_derive_their_authority_rather_than_declaring_it(self):
        filing = load_assertion({
            "id": "c1", "rule": "MSL", "level": "C", "loop": "L1",
            "on_fail": "alternatif", "source": "s",
            "record": {"decision": "MSL3", "source": "ds", "date": "2026-08-07"}})
        self.assertEqual(filing.authority, "record_only")
        self.assertFalse(filing.signs_off())

    def test_a_screening_pass_cannot_reach_green(self):
        screen = build(authority=AUTHORITY_SCREEN_ONLY)
        verdict, reasons = rollup([evaluate(screen, evidence_for(
            {"summary": {"FAIL": 0}}))])
        self.assertEqual(verdict, SCREEN_ONLY)
        self.assertIn(SCREEN_ONLY_AUTHORITY, reasons)

    def test_the_two_screen_only_reasons_stay_distinguishable(self):
        # An unbound result and an unentitled one are different problems with
        # different fixes; collapsing them would hide which one you have.
        screen = build(authority=AUTHORITY_SCREEN_ONLY)
        bound = Evidence(name="drc", document={"summary": {"FAIL": 0}},
                         artifact="b.kicad_pcb", artifact_digest="a" * 64,
                         hash_selector="design.sha256",
                         hash_path=("design", "sha256"))
        bound.document["design"] = {"sha256": "a" * 64}
        verdict, reasons = rollup([evaluate(screen, {"drc": bound})])
        self.assertEqual(verdict, SCREEN_ONLY)
        self.assertEqual(reasons, [SCREEN_ONLY_AUTHORITY])
        self.assertNotIn(EVIDENCE_UNBOUND, reasons)

    def test_establishes_is_refused_instead_of_silently_dropped(self):
        # The whole reason H2's proposed mechanism did nothing: the loader
        # accepted the key and read nothing, so the set file looked correct.
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(establishes="record_only")
        self.assertEqual(caught.exception.code, "UNKNOWN_FIELD")
        self.assertIn("authority", caught.exception.detail)

    def test_authority_summary_separates_screens_from_signoffs(self):
        signed = evaluate(build(authority=AUTHORITY_SIGNOFF),
                          evidence_for({"summary": {"FAIL": 0}}))
        screened = evaluate(build(authority=AUTHORITY_SCREEN_ONLY),
                            evidence_for({"summary": {"FAIL": 0}}))
        summary = authority_summary([signed, screened])
        self.assertEqual(summary["by_authority"][AUTHORITY_SIGNOFF], 1)
        self.assertEqual(summary["by_authority"][AUTHORITY_SCREEN_ONLY], 1)


class PartialCoverageTest(unittest.TestCase):
    """``partial`` has to name the gap, or it is humility that says nothing."""

    def test_partial_without_a_residual_is_refused(self):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(coverage=COVERAGE_PARTIAL)
        self.assertEqual(caught.exception.code, "RESIDUAL_UNDECLARED")

    def test_full_coverage_cannot_also_leave_something_over(self):
        with self.assertRaises(AssertionSyntaxError) as caught:
            build(residual_evidence=["windowpane"])
        self.assertEqual(caught.exception.code, "BAD_COVERAGE")

    def test_the_residual_travels_into_the_outcome(self):
        partial = build(coverage=COVERAGE_PARTIAL,
                        residual_evidence=["multi-aperture paste"])
        payload = evaluate(partial, evidence_for(
            {"summary": {"FAIL": 0}})).to_dict()
        self.assertEqual(payload["coverage"], COVERAGE_PARTIAL)
        self.assertEqual(payload["residual_evidence"], ["multi-aperture paste"])


class QualifiedMatrixRowTest(unittest.TestCase):
    """``A(kısmi)`` and ``A(screen)`` become requirements on the assertion."""

    def test_the_qualifier_maps_onto_the_axis_it_actually_states(self):
        self.assertEqual(partial_requirement("A(kısmi)"), ("A", "coverage", "partial"))
        self.assertEqual(partial_requirement("A(screen)"),
                         ("A", "authority", "screen_only"))

    def test_an_unknown_qualifier_stays_unresolved(self):
        # Inventing an axis for a word nobody defined would be the regex
        # making the engineering call this module refuses to make.
        self.assertIsNone(partial_requirement("A(bir şey)"))
        self.assertIsNone(partial_requirement("C→A"))
        self.assertIsNone(partial_requirement("A"))

    def _rows(self, cell):
        rows, problems = parse_matrix(
            "## S\n\n| Kural | Sev | Araç | Döngü | Fail'de |\n"
            "|---|---|---|---|---|\n"
            f"| EPAD pasta kapsaması | {cell} | t.py | L0 | elle |\n")
        self.assertEqual(problems, [])
        return rows

    def test_declaring_the_axis_claims_the_row(self):
        rows = self._rows("A(kısmi)")
        claimed = build(rule="EPAD pasta kapsaması", level="A",
                        coverage=COVERAGE_PARTIAL,
                        residual_evidence=["windowpane patterns"])
        matched = cross_reference(rows, [claimed]).matched
        self.assertEqual(len(matched), 1)
        self.assertNotIn("issue", matched[0])

    def test_plain_a_still_overclaims_and_is_still_refused(self):
        rows = self._rows("A(kısmi)")
        overclaiming = build(rule="EPAD pasta kapsaması", level="A")
        matched = cross_reference(rows, [overclaiming]).matched
        self.assertEqual(matched[0]["issue"], PARTIAL_LEVEL)

    def test_a_screen_row_needs_the_authority_not_the_coverage(self):
        rows = self._rows("A(screen)")
        wrong_axis = build(rule="EPAD pasta kapsaması", level="A",
                           coverage=COVERAGE_PARTIAL,
                           residual_evidence=["FEXT"])
        self.assertEqual(cross_reference(rows, [wrong_axis]).matched[0]["issue"],
                         PARTIAL_LEVEL)
        right_axis = build(rule="EPAD pasta kapsaması", level="A",
                           authority=AUTHORITY_SCREEN_ONLY)
        self.assertNotIn("issue", cross_reference(rows, [right_axis]).matched[0])


class ByCheckMapTest(unittest.TestCase):
    """The name-keyed door that carries coverage, which ``metrics`` cannot."""

    def test_an_empty_check_is_visible_as_no_coverage_not_as_zero(self):
        findings = [result("epad_paste_coverage", 0, []),
                    result("via_in_pad", 4, [])]
        mapped = by_check_map(findings)
        self.assertEqual(mapped["epad_paste_coverage"]["status"], NO_COVERAGE)
        self.assertEqual(mapped["epad_paste_coverage"]["scanned"], 0)
        self.assertEqual(mapped["via_in_pad"]["status"], PASS)

    def test_both_halves_separate_the_empty_check_from_the_clean_one(self):
        # Two situations that must never be confused: a check that examined
        # nothing, and one that examined four things and found nothing wrong.
        # The report used to give both the same answer - 0 - through the only
        # name-keyed map it had.
        #
        # Two independent fixes landed on this, from opposite ends, and both
        # are asserted here because they do different jobs:
        #   metrics  -> null for the empty check, so a threshold comparison
        #               cannot falsely succeed (the NEGATIVE half)
        #   by_check -> status/scanned, so a rule can affirmatively say the
        #               check ran (the POSITIVE half)
        report = board_report("dfm_emc_check", __file__,
                              [result("epad_paste_coverage", 0, []),
                               result("via_in_pad", 4, [])])

        self.assertIsNone(report["metrics"]["epad_paste_coverage"])
        self.assertEqual(report["metrics"]["via_in_pad"], 0)

        empty = report["by_check"]["epad_paste_coverage"]
        clean = report["by_check"]["via_in_pad"]
        self.assertEqual(empty["status"], NO_COVERAGE)
        self.assertEqual(empty["scanned"], 0)
        self.assertEqual(clean["status"], PASS)
        self.assertEqual(clean["scanned"], 4)

    def test_a_metrics_threshold_can_no_longer_pass_on_an_empty_check(self):
        # The negative half, driven through the real assertion engine rather
        # than asserted about in prose: null is not a number, so at_most is a
        # type error - NO_COVERAGE, never PASS.
        report = board_report("dfm_emc_check", __file__,
                              [result("epad_paste_coverage", 0, [])])
        vacuous = build(selector="metrics.epad_paste_coverage",
                        falsifier={"operation": "set",
                                   "path": "metrics.epad_paste_coverage",
                                   "value": 3})
        outcome = evaluate(vacuous, evidence_for(report))
        self.assertEqual(outcome.status, NO_COVERAGE)
        self.assertEqual(outcome.reason, TYPE_MISMATCH)


if __name__ == "__main__":
    unittest.main()
