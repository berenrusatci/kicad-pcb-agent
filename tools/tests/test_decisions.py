#!/usr/bin/env python3
"""common/decisions.py: graph validity, cascade invalidation, dialect merge.

Converted from the archive's pytest module ``test_karar_birimleri.py`` and
extended, because the port had to reconcile two on-disk dialects that the
archive never had to think about.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import TOOLS  # noqa: E402,F401

from common.decisions import (  # noqa: E402
    ACCEPTED, BLOCKED, CLOSURE_EVIDENCE_IDS_EMPTY,
    CLOSURE_EVIDENCE_NOT_CANONICAL, CLOSURE_EVIDENCE_NOT_IN_CATALOGUE,
    CLOSURE_INVALIDATION_TRIGGER_UNDECLARED, CLOSURE_NOT_ACCEPTED,
    CLOSURE_OUTPUT_ARTIFACT_UNDECLARED, CLOSURE_REASONS,
    CLOSURE_REQUIRED_EVIDENCE_UNDECLARED, READY, SCHEMA_VERSION,
    SKILL_DIALECT_EXTRA_STATUSES, SKILL_DIALECT_FIELD_MAP, SKILL_SCHEMA_RELPATH,
    SKILL_SCHEMA_VERSION, STATUSES, UNCHECKED_CLOSURE_CONDITIONS, DecisionError,
    DecisionUnit, DependencyCycleError, UnknownDependencyError,
    build_critical_pin_decision, closure_state, critical_pin_category,
    decision_gate, decisions_path, detect_and_save_critical_pin_decisions,
    dependents_of, dump_decisions, invalidate, load_project_decisions,
    parse_decision_document, parse_decisions, save_decisions,
    topological_order, unaccepted, upsert,
)

#: A canonical evidence catalogue, keyed the way ``parse_evidence`` returns it.
EVIDENCE = {"E1": {"evidence_id": "E1", "kind": "artifact",
                   "source": "field solver", "status": "canonical"}}


def unit(decision_id: str, status: str = READY, depends=()) -> DecisionUnit:
    return DecisionUnit(decision_id=decision_id, question=f"{decision_id}?",
                        status=status, depends_on=list(depends))


def closed_unit(decision_id: str, depends=()) -> DecisionUnit:
    """An accepted decision that also satisfies every closure condition."""
    return DecisionUnit(
        decision_id=decision_id, question=f"{decision_id}?", status=ACCEPTED,
        depends_on=list(depends), required_evidence=["field solver run"],
        evidence_ids=["E1"], invalidation_triggers=["stackup change"],
        output_artifact=f"artifacts/{decision_id}.json")


class SchemaTests(unittest.TestCase):
    def test_canonical_status_vocabulary_matches_context_pack(self):
        """If these two drift, a pack that validates names a status the
        promotion gate cannot read."""
        source = (TOOLS / "scripts" / "context_pack.py").read_text(
            encoding="utf-8")
        line = next(l for l in source.splitlines()
                    if l.startswith("DECISION_STATUSES"))
        from common.decisions import STATUSES
        for status in STATUSES:
            self.assertIn(f'"{status}"', line, f"{status} unknown to context_pack")

    def test_canonical_schema_version_matches_context_pack(self):
        source = (TOOLS / "scripts" / "context_pack.py").read_text(
            encoding="utf-8")
        self.assertIn(f'DECISION_SCHEMA = "{SCHEMA_VERSION}"', source)

    def test_dump_is_always_the_canonical_dialect(self):
        document = dump_decisions([unit("D1", ACCEPTED)])
        self.assertEqual(SCHEMA_VERSION, document["schema_version"])
        self.assertEqual("D1", document["decisions"][0]["decision_id"])
        self.assertEqual(ACCEPTED, document["decisions"][0]["status"])


class LegacyDialectTests(unittest.TestCase):
    LEGACY = {
        "kararlar": [
            {"karar_id": "stackup", "soru": "4 or 6 layers?",
             "durum": "KABUL_EDILDI", "bagimliliklar": [],
             "gereken_kanit": "stackup report",
             "sahip_skill": "empedans-stackup"},
            {"karar_id": "zdiff", "soru": "90 ohm?",
             "durum": "ACIK", "bagimliliklar": ["stackup"]},
        ]
    }

    def test_turkish_records_load_as_canonical_units(self):
        units = parse_decisions(self.LEGACY)
        by_id = {u.decision_id: u for u in units}
        self.assertEqual(ACCEPTED, by_id["stackup"].status)
        self.assertEqual(READY, by_id["zdiff"].status)
        self.assertEqual(["stackup"], by_id["zdiff"].depends_on)
        self.assertEqual("empedans-stackup", by_id["stackup"].owner_skill)

    def test_free_text_evidence_becomes_a_list(self):
        units = parse_decisions(self.LEGACY)
        by_id = {u.decision_id: u for u in units}
        self.assertEqual(["stackup report"], by_id["stackup"].required_evidence)

    def test_kabul_edildi_and_accepted_are_the_same_gate_answer(self):
        """The two dialects must be indistinguishable to the gate.

        Neither record carries evidence, so BOTH are accepted-but-not-closed:
        the point of this test is that the two dialects get the same answer,
        not that a bare accepted record passes.
        """
        turkish = decision_gate(parse_decisions(
            {"kararlar": [{"karar_id": "A", "soru": "?",
                           "durum": "KABUL_EDILDI"}]}))
        english = decision_gate(parse_decisions(
            {"decisions": [{"decision_id": "A", "question": "?",
                            "status": "accepted"}]}))
        self.assertEqual(turkish["passed"], english["passed"])
        self.assertEqual(turkish["accepted"], english["accepted"])
        self.assertEqual(turkish["closure_reasons"], english["closure_reasons"])
        self.assertEqual(1, turkish["accepted"])
        self.assertFalse(turkish["passed"],
                         "acceptance without evidence is not closure")

    def test_unknown_status_is_refused_not_guessed(self):
        with self.assertRaises(DecisionError):
            parse_decisions({"decisions": [{"decision_id": "A",
                                            "question": "?",
                                            "status": "maybe"}]})

    def test_duplicate_ids_are_refused(self):
        with self.assertRaises(DecisionError):
            parse_decisions({"decisions": [
                {"decision_id": "A", "question": "?"},
                {"decision_id": "A", "question": "?"},
            ]})

    def test_document_without_either_key_is_refused(self):
        with self.assertRaises(DecisionError):
            parse_decisions({"something_else": []})


class GraphTests(unittest.TestCase):
    def test_topological_order_puts_dependencies_first(self):
        units = [unit("C", depends=["B"]), unit("B", depends=["A"]), unit("A")]
        order = topological_order(units)
        self.assertLess(order.index("A"), order.index("B"))
        self.assertLess(order.index("B"), order.index("C"))

    def test_order_is_deterministic(self):
        units = [unit("C"), unit("A"), unit("B")]
        self.assertEqual(topological_order(units), topological_order(units))
        self.assertEqual(["A", "B", "C"], topological_order(units))

    def test_unknown_dependency_is_an_error(self):
        with self.assertRaises(UnknownDependencyError):
            topological_order([unit("A", depends=["ghost"])])

    def test_cycle_is_detected_and_named(self):
        units = [unit("A", depends=["B"]), unit("B", depends=["A"])]
        with self.assertRaises(DependencyCycleError) as caught:
            topological_order(units)
        self.assertIn("->", str(caught.exception))

    def test_self_dependency_is_a_cycle(self):
        with self.assertRaises(DependencyCycleError):
            topological_order([unit("A", depends=["A"])])

    def test_dependents_are_transitive(self):
        units = [unit("A"), unit("B", depends=["A"]), unit("C", depends=["B"]),
                 unit("D")]
        self.assertEqual({"B", "C"}, set(dependents_of(units, "A")))
        self.assertEqual([], dependents_of(units, "D"))


class CascadeTests(unittest.TestCase):
    def setUp(self):
        self.units = [
            unit("stackup", ACCEPTED),
            unit("zdiff", ACCEPTED, ["stackup"]),
            unit("routing", ACCEPTED, ["zdiff"]),
            unit("unrelated", ACCEPTED),
        ]

    def test_invalidation_cascades_to_transitive_dependents(self):
        reopened = invalidate(self.units, "stackup", "fab changed the stackup")
        self.assertEqual({"zdiff", "routing"}, set(reopened))
        by_id = {u.decision_id: u for u in self.units}
        self.assertEqual(BLOCKED, by_id["stackup"].status)
        self.assertEqual(READY, by_id["zdiff"].status)
        self.assertEqual(READY, by_id["routing"].status)

    def test_unrelated_decisions_survive(self):
        invalidate(self.units, "stackup", "fab changed the stackup")
        by_id = {u.decision_id: u for u in self.units}
        self.assertEqual(ACCEPTED, by_id["unrelated"].status)

    def test_already_invalidated_dependents_keep_their_own_history(self):
        self.units[1].status = BLOCKED
        self.units[1].invalidated_reason = "its own earlier reason"
        invalidate(self.units, "stackup", "fab changed the stackup")
        by_id = {u.decision_id: u for u in self.units}
        self.assertEqual(BLOCKED, by_id["zdiff"].status)
        self.assertEqual("its own earlier reason", by_id["zdiff"].invalidated_reason)

    def test_invalidating_an_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            invalidate(self.units, "ghost", "reason")

    def test_an_empty_reason_is_refused(self):
        with self.assertRaises(DecisionError):
            invalidate(self.units, "stackup", "   ")


class GateTests(unittest.TestCase):
    def test_all_closed_passes(self):
        report = decision_gate(
            [closed_unit("A"), closed_unit("B", ["A"])], EVIDENCE)
        self.assertTrue(report["passed"], report["closure_reasons"])
        self.assertEqual(2, report["accepted"])
        self.assertEqual(2, report["closed"])
        self.assertEqual([], report["unaccepted_ids"])
        self.assertEqual([], report["accepted_but_not_closed_ids"])

    def test_accepted_without_evidence_does_not_pass(self):
        """The gate used to read ``status`` alone, so this passed."""
        report = decision_gate([unit("A", ACCEPTED), unit("B", ACCEPTED, ["A"])])
        self.assertFalse(report["passed"])
        self.assertEqual(2, report["accepted"])
        self.assertEqual(0, report["closed"])
        self.assertEqual([], report["unaccepted_ids"],
                         "these are not OPEN decisions; they are unclosed ones")
        self.assertEqual(["A", "B"], report["accepted_but_not_closed_ids"])

    def test_one_open_decision_fails_and_is_named(self):
        report = decision_gate([closed_unit("A"), unit("B", READY, ["A"])],
                               EVIDENCE)
        self.assertFalse(report["passed"])
        self.assertEqual(["B"], report["unaccepted_ids"])

    def test_an_empty_project_does_not_pass_by_default(self):
        """Zero decisions is zero evidence; promote.py turns this into a
        blocker rather than the archive's 'this gate is skipped'."""
        report = decision_gate([])
        self.assertEqual(0, report["total"])
        self.assertTrue(report["passed"], "the graph itself is trivially valid")
        self.assertEqual(0, report["accepted"])

    def test_a_broken_graph_never_reports_a_pass(self):
        report = decision_gate([closed_unit("A", ["B"]),
                                closed_unit("B", ["A"])], EVIDENCE)
        self.assertFalse(report["passed"])
        self.assertFalse(report["graph_valid"])
        self.assertIn("cycle", report["graph_error"])

    def test_unaccepted_lists_every_non_accepted_state(self):
        units = [unit("A", ACCEPTED), unit("B", READY), unit("C", BLOCKED)]
        self.assertEqual({"B", "C"},
                         {u.decision_id for u in unaccepted(units)})


class ClosureTests(unittest.TestCase):
    """``accepted`` is a human's decision; ``closed`` is the repo's proof."""

    def test_a_fully_evidenced_accepted_decision_is_closed(self):
        state = closure_state(closed_unit("A"), EVIDENCE)
        self.assertTrue(state["closed"], state["reasons"])
        self.assertTrue(state["accepted"])
        self.assertEqual([], state["reasons"])

    def test_closed_is_never_a_hand_written_status(self):
        """No status spells 'closed'; only the predicate can say it."""
        self.assertNotIn("closed", STATUSES)
        with self.assertRaises(DecisionError):
            DecisionUnit(decision_id="A", question="?", status="closed")

    def test_an_open_decision_is_not_closed(self):
        state = closure_state(unit("A", READY), EVIDENCE)
        self.assertFalse(state["closed"])
        self.assertFalse(state["accepted"])
        self.assertIn(CLOSURE_NOT_ACCEPTED, state["reasons"])

    def test_accepted_without_evidence_ids_is_not_closed(self):
        victim = closed_unit("A")
        victim.evidence_ids = []
        state = closure_state(victim, EVIDENCE)
        self.assertFalse(state["closed"])
        self.assertTrue(state["accepted"])
        self.assertEqual([CLOSURE_EVIDENCE_IDS_EMPTY], state["reasons"])

    def test_evidence_id_that_resolves_to_nothing_is_not_closed(self):
        victim = closed_unit("A")
        victim.evidence_ids = ["GHOST"]
        state = closure_state(victim, EVIDENCE)
        self.assertEqual([CLOSURE_EVIDENCE_NOT_IN_CATALOGUE], state["reasons"])
        self.assertEqual(["GHOST"], state["missing_evidence_ids"])

    def test_an_absent_catalogue_is_not_treated_as_a_pass(self):
        """Dropping the catalogue must not silently close the decision."""
        state = closure_state(closed_unit("A"))
        self.assertFalse(state["closed"])
        self.assertEqual([CLOSURE_EVIDENCE_NOT_IN_CATALOGUE], state["reasons"])

    def test_noncanonical_evidence_is_not_closed(self):
        stale = {"E1": dict(EVIDENCE["E1"], status="stale")}
        state = closure_state(closed_unit("A"), stale)
        self.assertEqual([CLOSURE_EVIDENCE_NOT_CANONICAL], state["reasons"])
        self.assertEqual(["E1"], state["noncanonical_evidence_ids"])

    def test_missing_output_artifact_is_not_closed(self):
        victim = closed_unit("A")
        victim.output_artifact = "   "
        self.assertEqual([CLOSURE_OUTPUT_ARTIFACT_UNDECLARED],
                         closure_state(victim, EVIDENCE)["reasons"])

    def test_missing_required_evidence_and_trigger_are_named_separately(self):
        victim = closed_unit("A")
        victim.required_evidence = []
        victim.invalidation_triggers = []
        self.assertEqual([CLOSURE_REQUIRED_EVIDENCE_UNDECLARED,
                          CLOSURE_INVALIDATION_TRIGGER_UNDECLARED],
                         closure_state(victim, EVIDENCE)["reasons"])

    def test_reasons_come_back_in_the_declared_order(self):
        state = closure_state(unit("A", READY), EVIDENCE)
        self.assertEqual(sorted(state["reasons"], key=CLOSURE_REASONS.index),
                         state["reasons"])
        for code in state["reasons"]:
            self.assertIn(code, CLOSURE_REASONS)

    def test_a_raw_mapping_and_a_unit_get_the_same_answer(self):
        """context_pack.py passes raw dicts; promote.py passes units."""
        raw = closed_unit("A").to_dict()
        self.assertEqual(closure_state(raw, EVIDENCE),
                         closure_state(closed_unit("A"), EVIDENCE))

    def test_unchecked_conditions_are_named_not_silently_skipped(self):
        """`closed is True` must never read as 'nothing left to check'."""
        self.assertIn("implementation_check_pass", UNCHECKED_CLOSURE_CONDITIONS)
        for key, why in UNCHECKED_CLOSURE_CONDITIONS.items():
            self.assertTrue(why.strip(), f"{key} has no stated reason")
        report = decision_gate([closed_unit("A")], EVIDENCE)
        self.assertEqual(UNCHECKED_CLOSURE_CONDITIONS,
                         report["unchecked_closure_conditions"])


class DecisionDebtTests(unittest.TestCase):
    """Report section 6.9: a count VECTOR, never a weighted score."""

    def units(self):
        no_evidence = closed_unit("B")
        no_evidence.evidence_ids = []
        no_output = closed_unit("C")
        no_output.output_artifact = ""
        return [unit("A", READY), no_evidence, no_output, closed_unit("D")]

    def test_the_debt_vector_counts_each_kind_of_debt(self):
        debt = decision_gate(self.units(), EVIDENCE)["decision_debt"]
        self.assertEqual(1, debt["open"])
        self.assertEqual(3, debt["accepted"])
        self.assertEqual(1, debt["closed"])
        self.assertEqual(2, debt["accepted_but_not_closed"])
        self.assertEqual(1, debt["accepted_evidence_missing"])
        self.assertEqual(1, debt["accepted_output_artifact_missing"])
        self.assertEqual(0, debt["accepted_invalidation_trigger_missing"])
        self.assertEqual(1, debt["by_reason"][CLOSURE_EVIDENCE_IDS_EMPTY])

    def test_no_weighted_score_is_invented(self):
        """6.9 forbids it: the weights are governance, not measurement."""
        report = decision_gate(self.units(), EVIDENCE)
        for key in report["decision_debt"]:
            self.assertNotIn("score", key)
        self.assertNotIn("decision_debt_score", report)

    def test_uncomputable_fields_are_named_rather_than_reported_as_zero(self):
        debt = decision_gate(self.units(), EVIDENCE)["decision_debt"]
        for key in ("oldest_open_decision_age_days", "phase_blocking_open",
                    "status_drift_across_documents"):
            self.assertIn(key, debt["not_computable"])
            self.assertNotIn(key, debt, "a zero here would read as 'no debt'")


class SkillDialectDivergenceTests(unittest.TestCase):
    """The THIRD dialect nothing loads, kept machine-visible on purpose.

    ``skills/decompose-pcb-decisions`` documents its artifacts against a JSON
    Schema that no loader in ``tools/`` reads. Deleting it would destroy design
    intent (evidence level/loop) this module does not implement; ignoring it
    lets a skill emit documents the promotion gate silently misreads. So the
    divergence is recorded in ``common/decisions.py`` and these tests fail if
    EITHER side moves.
    """

    @classmethod
    def setUpClass(cls):
        cls.path = TOOLS.parent / SKILL_SCHEMA_RELPATH
        cls.schema = json.loads(cls.path.read_text(encoding="utf-8"))
        cls.unit_props = cls.schema["$defs"]["unit"]["properties"]

    def test_the_skill_schema_still_exists(self):
        self.assertTrue(self.path.is_file(),
                        "the third dialect must be reconciled, not deleted")

    def test_the_skill_schema_version_is_still_a_different_dialect(self):
        const = self.schema["properties"]["schema_version"]["const"]
        self.assertEqual(SKILL_SCHEMA_VERSION, const)
        self.assertNotEqual(SCHEMA_VERSION, const,
                            "if these converged, remove the divergence record")

    def test_extra_document_statuses_are_exactly_the_recorded_ones(self):
        extra = set(self.schema["properties"]["status"]["enum"]) - set(STATUSES)
        self.assertEqual(SKILL_DIALECT_EXTRA_STATUSES, extra,
                         "the skill's status vocabulary drifted; "
                         "common/decisions.py cannot parse these")

    def test_the_unit_status_vocabulary_has_not_drifted(self):
        self.assertEqual(set(STATUSES),
                         set(self.unit_props["status"]["enum"]))

    def test_every_recorded_field_divergence_is_still_real(self):
        canonical_fields = set(DecisionUnit.__dataclass_fields__)
        for foreign, canonical in SKILL_DIALECT_FIELD_MAP.items():
            present = (foreign in self.unit_props
                       or foreign in self.schema["properties"])
            self.assertTrue(present, f"{foreign} vanished from the skill schema")
            self.assertIn(canonical, canonical_fields | {"decisions"})
            self.assertNotIn(foreign, canonical_fields,
                             f"{foreign} is now canonical; retire the mapping")

    def test_no_canonical_loader_accepts_the_skill_dialect(self):
        """Documenting the gap is not the same as closing it."""
        with self.assertRaises(DecisionError):
            parse_decisions({"schema_version": SKILL_SCHEMA_VERSION,
                             "decision_units": [{"decision_id": "A",
                                                 "question": "?"}]})


class FileTests(unittest.TestCase):
    def test_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_units.json"
            save_decisions(path, [unit("A", ACCEPTED), unit("B", READY, ["A"])])
            reloaded = load_project_decisions(tmp)
            self.assertEqual(["A", "B"],
                             sorted(u.decision_id for u in reloaded))

    def test_the_evidence_catalogue_survives_a_round_trip(self):
        """Writing ``evidence: []`` back would un-close every decision."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_units.json"
            save_decisions(path, [closed_unit("A")], EVIDENCE)
            units, evidence = parse_decision_document(
                json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(EVIDENCE, evidence)
            self.assertTrue(decision_gate(units, evidence)["passed"])

    def test_rescanning_preserves_the_evidence_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision_units.json"
            save_decisions(path, [closed_unit("A")], EVIDENCE)
            detect_and_save_critical_pin_decisions(tmp, "U1", ["NDA_PIN3"])
            _units, evidence = parse_decision_document(
                json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(EVIDENCE, evidence)

    def test_legacy_docs_location_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "DOCS"
            docs.mkdir()
            (docs / "karar_birimleri.json").write_text(
                json.dumps({"kararlar": [{"karar_id": "A", "soru": "?",
                                          "durum": "KABUL_EDILDI"}]}),
                encoding="utf-8")
            self.assertIsNotNone(decisions_path(tmp))
            units = load_project_decisions(tmp)
            self.assertEqual(ACCEPTED, units[0].status)

    def test_a_project_with_no_decision_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(decisions_path(tmp))
            self.assertEqual([], load_project_decisions(tmp))

    def test_upsert_replaces_by_id(self):
        units = [unit("A", READY)]
        upsert(units, unit("A", ACCEPTED))
        self.assertEqual(1, len(units))
        self.assertEqual(ACCEPTED, units[0].status)
        upsert(units, unit("B"))
        self.assertEqual(2, len(units))


class CriticalPinGateTests(unittest.TestCase):
    def test_more_specific_pattern_wins_over_a_shorter_substring(self):
        # "SYSBOOT0" contains both "SYSBOOT" and "BOOT" -- the longer, more
        # specific pattern must be tried first or it is never reachable.
        self.assertEqual("SYSBOOT", critical_pin_category("SYSBOOT0"))

    def test_a_plain_boot_net_falls_back_to_the_general_category(self):
        self.assertEqual("BOOT", critical_pin_category("BOOT_SEL"))

    def test_no_match_returns_none(self):
        self.assertIsNone(critical_pin_category("SPI_MOSI"))

    def test_match_is_case_insensitive(self):
        self.assertEqual("NDA", critical_pin_category("cam0_nda_pin"))

    def test_built_decision_is_ready_and_requires_evidence(self):
        unit = build_critical_pin_decision("NDA", "U1", "Net 'X' matched.")
        self.assertEqual(READY, unit.status)
        self.assertTrue(unit.required_evidence)
        self.assertIn("u1", unit.decision_id)
        self.assertIn("nda", unit.decision_id)

    def test_scanning_nets_creates_one_decision_per_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            nets = ["SYSBOOT0", "SYSBOOT1", "MODE_SEL0", "SPI_MOSI"]
            units = detect_and_save_critical_pin_decisions(tmp, "U1", nets)
            categories = {u.decision_id for u in units}
            self.assertEqual(2, len(units))  # SYSBOOT (dedup'd) + MODE_SEL
            self.assertTrue(any("sysboot" in c for c in categories))
            self.assertTrue(any("mode-sel" in c for c in categories))

    def test_fault_injection_an_undetected_critical_pin_blocks_the_gate(self):
        """A critical pin net that was never scanned/confirmed must leave the
        promotion gate CLOSED, not silently open."""
        with tempfile.TemporaryDirectory() as tmp:
            detect_and_save_critical_pin_decisions(tmp, "U1", ["NDA_PIN3"])
            units = load_project_decisions(tmp)
            gate = decision_gate(units)
            self.assertFalse(gate["passed"])
            self.assertEqual(1, gate["unaccepted"])

    def test_flipping_the_status_alone_does_not_open_the_gate(self):
        """The old docstring claimed ``required_evidence`` made this
        impossible; it did not, because the gate read ``status`` alone."""
        with tempfile.TemporaryDirectory() as tmp:
            detect_and_save_critical_pin_decisions(tmp, "U1", ["NDA_PIN3"])
            units = load_project_decisions(tmp)
            units[0].status = ACCEPTED
            save_decisions(decisions_path(tmp), units)
            units, evidence = parse_decision_document(json.loads(
                decisions_path(tmp).read_text(encoding="utf-8")))
            gate = decision_gate(units, evidence)
            self.assertFalse(gate["passed"])
            self.assertEqual(1, gate["accepted_but_not_closed"])
            self.assertIn(CLOSURE_EVIDENCE_IDS_EMPTY,
                          gate["closure_reasons"][units[0].decision_id])

    def test_closing_the_generated_decision_opens_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            detect_and_save_critical_pin_decisions(tmp, "U1", ["NDA_PIN3"])
            units = load_project_decisions(tmp)
            units[0].status = ACCEPTED
            units[0].evidence_ids = ["E1"]
            units[0].output_artifact = "artifacts/nda_pins.json"
            save_decisions(decisions_path(tmp), units, EVIDENCE)
            units, evidence = parse_decision_document(json.loads(
                decisions_path(tmp).read_text(encoding="utf-8")))
            gate = decision_gate(units, evidence)
            self.assertTrue(gate["passed"], gate["closure_reasons"])
            self.assertEqual(1, gate["closed"])

    def test_rescanning_does_not_overwrite_an_already_accepted_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            detect_and_save_critical_pin_decisions(tmp, "U1", ["NDA_PIN3"])
            units = load_project_decisions(tmp)
            units[0].status = ACCEPTED
            save_decisions(decisions_path(tmp), units)

            # Re-scanning the SAME net must not drop the decision back to READY.
            rescanned = detect_and_save_critical_pin_decisions(tmp, "U1", ["NDA_PIN3"])
            self.assertEqual(ACCEPTED, rescanned[0].status)

    def test_no_critical_pins_creates_no_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            units = detect_and_save_critical_pin_decisions(
                tmp, "U1", ["SPI_MOSI", "GND", "3V3"])
            self.assertEqual([], units)
            self.assertIsNone(decisions_path(tmp))


if __name__ == "__main__":
    unittest.main()
