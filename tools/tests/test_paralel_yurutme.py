#!/usr/bin/env python3
"""Regression for the fan-out gate: paralel-yurutme.

The skill has no tool of its own - it decides whether work may be split
before any tool runs. So the thing under test is the DECISION, and the only
way a decision gets a regression is to write it down as a function and inject
faults into it. That is what this file does; the skill's section 6 cites it.

The coverage arithmetic is NOT reimplemented here. ``common.findings`` owns it
(one implementation, rule sourced from ``metric_coverage.schema.json``) and a
second copy in a test is exactly the drift this repo keeps finding.
"""

from __future__ import annotations

import copy
import itertools
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _paths import REPO  # noqa: E402

from common.findings import coverage_status  # noqa: E402

SKILL_DIR = REPO / "skills" / "paralel-yurutme"
SKILL = SKILL_DIR / "SKILL.md"
SCHEMA = SKILL_DIR / "references" / "fanout_plan.schema.json"
EXAMPLE = SKILL_DIR / "references" / "fanout_plan.example.json"
ENVELOPE = SKILL_DIR / "references" / "execution-envelope.md"
REGISTRY = REPO / "config" / "tool_registry.json"

BOARD_SUFFIXES = (".kicad_pcb", ".kicad_sch")


# --------------------------------------------------------------------------- #
# The gate, as a function. Unit-level tests 2(board)/3 and plan-level 1/2.
# --------------------------------------------------------------------------- #
def scanned_units(units: list[dict]) -> list[dict]:
    """Units the gate actually evaluated. A unit that never declared what it
    writes cannot be proven disjoint, so it is excluded rather than trusted."""
    return [u for u in units if u.get("writes")]


def writer_intersections(units: list[dict]) -> list[dict]:
    """Test 2: pairwise write-path intersections over every SCANNED unit.

    Deliberately not limited to FANOUT_APPROVED units: a unit kept inline
    still writes from the main agent, and a branch writing the same path is
    the same lost update.
    """
    found = []
    for left, right in itertools.combinations(scanned_units(units), 2):
        shared = sorted(set(left["writes"]) & set(right["writes"]))
        if shared:
            found.append({"left": left["id"], "right": right["id"],
                          "paths": shared})
    return found


def dependency_violations(units: list[dict]) -> list[dict]:
    """Test 1: unit M reads something unit N writes in the same run."""
    found = []
    for reader, writer in itertools.permutations(scanned_units(units), 2):
        shared = sorted(set(reader.get("reads", [])) & set(writer["writes"]))
        if shared:
            found.append({"reader": reader["id"], "writer": writer["id"],
                          "paths": shared})
    return found


def touches_board(unit: dict) -> bool:
    return any(str(p).endswith(BOARD_SUFFIXES) for p in unit.get("writes", []))


def profitable(prof: dict) -> bool:
    """Test 3. A reason to isolate AND the isolation being viable.

    reason  = the branch keeps bulky output out of main context, or needs
              context main does not already hold
    viable  = the result comes back as a summary, and the unit is its own
              deliverable

    No numeric threshold: none has been published or measured. See
    ``references/execution-envelope.md`` section 3.
    """
    reason = prof["bulky_output"] or prof["context_not_loaded"]
    viable = prof["summarisable"] and prof["own_deliverable"]
    return bool(reason and viable)


def classify(unit: dict) -> str:
    if not unit.get("writes"):
        return "EXCLUDED"
    if touches_board(unit):
        return "FANOUT_UNSAFE"
    prof = unit.get("profitability")
    if prof is not None and not profitable(prof):
        return "INLINE_CHEAPER"
    return "FANOUT_APPROVED"


def recount(units: list[dict]) -> dict:
    scanned = scanned_units(units)
    return {
        "eligible": len(units),
        "scanned": len(scanned),
        "excluded": len(units) - len(scanned),
        "violations": len(writer_intersections(units)),
    }


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter(text: str) -> str:
    """The YAML block only.

    Reading ``writes_board`` out of the whole file is a real trap: a skill that
    DEFERS to board-write-lock quotes ``writes_board: true`` in its prose, and a
    body-wide search then files it as a board writer.
    """
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise AssertionError("no frontmatter block")
    return parts[1]


def declared_writes_board(text: str) -> bool | None:
    match = re.search(r"^writes_board:\s*(true|false)\s*$",
                      frontmatter(text), re.M)
    return None if match is None else match.group(1) == "true"


# --------------------------------------------------------------------------- #
class FanoutGate(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = load(EXAMPLE)
        self.units = self.plan["units"]

    def test_the_example_is_internally_consistent(self):
        """The shipped example may not lie about its own arithmetic."""
        self.assertEqual(recount(self.units), self.plan["coverage"])
        proof = self.plan["disjointness_proof"]
        scanned = scanned_units(self.units)
        self.assertEqual(proof["intersections"], writer_intersections(self.units))
        self.assertEqual(proof["checked_paths"],
                         sum(len(u["writes"]) for u in scanned))
        self.assertEqual(proof["pairs_compared"],
                         len(list(itertools.combinations(scanned, 2))))

    def test_the_example_decisions_match_the_gate(self):
        for unit in self.units:
            with self.subTest(unit=unit["id"]):
                self.assertEqual(unit["decision"], classify(unit))

    def test_a_clean_plan_is_green(self):
        self.assertEqual("GREEN", coverage_status(self.plan["coverage"]))
        self.assertEqual("GREEN", self.plan["gate"]["verdict"])

    def test_an_unmeasured_speedup_is_not_claimed(self):
        """Section 5: wall-clock gain was never measured in this repo, so the
        honest field value is null. An estimate here would be the exact
        vacuous-evidence class this bundle exists to catch."""
        self.assertIsNone(self.plan["speedup_measured_s"])

    # ------------------------------------------------------- fault injection
    def test_fault_two_branches_writing_the_same_path(self):
        broken = copy.deepcopy(self.units)
        broken[1]["writes"].append("rail_tree.json")
        hits = writer_intersections(broken)
        self.assertEqual(1, len(hits))
        self.assertEqual(["rail_tree.json"], hits[0]["paths"])
        coverage = recount(broken)
        self.assertEqual("RED", coverage_status(coverage))

    def test_fault_a_branch_writing_the_board(self):
        broken = copy.deepcopy(self.units[0])
        broken["writes"].append("cam-w-og05b10-wired.kicad_sch")
        self.assertTrue(touches_board(broken))
        self.assertEqual("FANOUT_UNSAFE", classify(broken))

    def test_fault_a_branch_reading_another_branch_output(self):
        broken = copy.deepcopy(self.units)
        broken[1]["reads"].append("rail_tree.json")
        hits = dependency_violations(broken)
        self.assertEqual(1, len(hits))
        self.assertEqual("mekanik-dxf", hits[0]["reader"])
        self.assertEqual("guc-isil", hits[0]["writer"])

    def test_fault_a_unit_that_never_declared_what_it_writes(self):
        """Silence is not disjointness. The unit is excluded and does NOT
        raise scanned - otherwise an undeclared writer would be counted as
        proven."""
        before = recount(self.units)
        broken = copy.deepcopy(self.units)
        broken.append({"id": "sessiz", "decision": "EXCLUDED",
                       "reason": "writes bildirilmedi", "writes": [],
                       "reads": []})
        after = recount(broken)
        self.assertEqual(before["scanned"], after["scanned"])
        self.assertEqual(before["excluded"] + 1, after["excluded"])
        self.assertEqual("EXCLUDED", classify(broken[-1]))

    def test_fault_inflated_eligible_is_invalid_coverage(self):
        broken = dict(self.plan["coverage"], eligible=9)
        self.assertEqual("INVALID_COVERAGE", coverage_status(broken))

    def test_fault_an_empty_candidate_list_is_no_coverage(self):
        self.assertEqual("NO_COVERAGE", coverage_status(recount([])))

    def test_fault_all_four_questions_no_is_inline_not_failure(self):
        unit = copy.deepcopy(self.units[0])
        unit["profitability"] = {k: False for k in unit["profitability"]}
        self.assertEqual("INLINE_CHEAPER", classify(unit))

    def test_a_reason_without_viability_still_stays_inline(self):
        """Bulky output alone does not justify a branch whose result cannot
        come back as a summary."""
        unit = copy.deepcopy(self.units[0])
        unit["profitability"].update(summarisable=False)
        self.assertEqual("INLINE_CHEAPER", classify(unit))


# --------------------------------------------------------------------------- #
class SkillContract(unittest.TestCase):
    """The same contract class newer skills ship, applied to this one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_the_skill_and_its_reference_files_exist(self):
        for path in (SKILL, SCHEMA, EXAMPLE, ENVELOPE):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"missing {path}")

    def test_the_frontmatter_key_order_follows_the_repo_law(self):
        expected = ["name", "description", "version", "last-reviewed", "phase",
                    "assurance_loop", "evidence_origin", "signoff", "inputs",
                    "outputs", "writes_board", "tools"]
        keys = [m.group(1) for m in
                re.finditer(r"^([a-z_-]+):", frontmatter(self.text), re.M)]
        self.assertEqual(expected, keys)

    def test_the_skill_names_tools_the_registry_carries(self):
        """Written as an inline list on purpose: the registry test's regex
        only checks ``tools: [...]``, and a block list would be skipped in
        silence."""
        match = re.search(r"^tools:\s*\[(.*?)\]", self.text, re.M | re.S)
        self.assertIsNotNone(match, "tools: must be the inline [a, b] form")
        names = [x.strip() for x in match.group(1).split(",") if x.strip()]
        self.assertTrue(names, "an empty tools list is unchecked, not clean")
        registry = {t["name"] for t in load(REGISTRY)["tools"]}
        for name in names:
            with self.subTest(tool=name):
                self.assertIn(name, registry)

    def test_the_gate_writes_no_board(self):
        self.assertIs(False, declared_writes_board(self.text))

    def test_the_skill_declares_the_open_question_rather_than_deciding_it(self):
        self.assertIn("AÇIK MADDE", self.text)

    def test_the_skill_does_not_claim_another_skill_s_rule(self):
        """It owns the fan-out EVIDENCE. The lock itself and the DAG belong to
        pcb-orchestrator; the text must defer to both by name."""
        for owner in ("board-write-lock", "phase-dag",
                      "decompose-pcb-decisions"):
            with self.subTest(owner=owner):
                self.assertIn(owner, self.text)

    def test_the_metric_name_avoids_the_c2_collision(self):
        """``parallelism_coverage`` is trace geometry owned by
        highspeed-routing. This gate's metric is ``fanout_coverage`` and the
        schema pins it, so the two can never be read as one number."""
        self.assertEqual("fanout_coverage",
                         load(SCHEMA)["properties"]["metric_name"]["const"])
        self.assertIn("fanout_coverage", self.text)
        self.assertIn("glossary.md]] §8", self.text)

    def test_the_gate_never_signs_off(self):
        self.assertIn("signoff: false", self.text)
        self.assertFalse(load(SCHEMA)["properties"]["signoff"]["const"])

    def test_the_invariant_tail_sections_are_present(self):
        for heading in ("## 5. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ",
                        "## 6. Doğrulama",
                        "## 7. Kabul kriterleri",
                        "## İlgili"):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.text)

    def test_the_acceptance_list_closes_on_coverage_arithmetic(self):
        self.assertIn(
            "- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.",
            self.text)

    def test_the_verdict_enum_stays_inside_the_closed_dictionary(self):
        closed = {"GREEN", "RED", "CONFIRM", "SCREEN_ONLY", "LOCAL_POLICY",
                  "SIM_ONLY", "NO_COVERAGE", "INVALID_METRIC",
                  "INVALID_COVERAGE", "INVALID_SPARAM", "INCONCLUSIVE",
                  "STALE", "GEOMETRY_PRECHECK_PASS", "TBD", "API_YOK"}
        schema = load(SCHEMA)
        emitted = set(schema["$defs"]["gate"]["properties"]["verdict"]["enum"])
        self.assertTrue(emitted <= closed, emitted - closed)
        domain = set(schema["properties"]["domain_state"]["enum"])
        self.assertFalse(domain & closed,
                         "domain state must not leak into gate.verdict")


# --------------------------------------------------------------------------- #
class ReadOnlySkillCountIsMeasured(unittest.TestCase):
    """The skill's open item, as a live measurement rather than a sentence.

    board-write-lock section 5 lists the read-only skills that may run in
    parallel. A hand-kept list drifts, so the count is recomputed here; the
    test asserts the MEASUREMENT, not the list, because the list belongs to
    board-write-lock and a second owner is the drift this repo keeps finding.
    """

    def test_the_writes_board_split_is_recomputable(self):
        skills = sorted((REPO / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        writers, readers = [], []
        for skill in skills:
            declared = declared_writes_board(skill.read_text(encoding="utf-8"))
            self.assertIsNotNone(declared,
                                 f"{skill.parent.name} declares no writes_board")
            (writers if declared else readers).append(skill.parent.name)
        self.assertEqual(len(skills), len(writers) + len(readers))
        self.assertIn("paralel-yurutme", readers)
        listed = (REPO / "skills" / "pcb-orchestrator" / "references"
                  / "board-write-lock.md").read_text(encoding="utf-8")
        for writer in writers:
            with self.subTest(writer=writer):
                self.assertIn(writer, listed,
                              "a board writer missing from the serial list is "
                              "the dangerous direction of this drift")


if __name__ == "__main__":
    unittest.main()
