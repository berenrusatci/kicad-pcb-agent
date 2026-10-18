#!/usr/bin/env python3
"""Tests for the interconnect mating gate (scripts/interconnect_mating.py).

The gate answers one question nobody in this repo owned: **A'nın fiziksel çıkışı
B'nin fiziksel girişine takılır mı, ve pinler örtüşür mü?** Netlist is
electrical, footprint is single-part, keepout is volume - none of them is
MATING. So almost every test below is a refusal, and each is a case where a
permissive version of the tool returns a confident PASS.

WHY F1 IS NOT ENOUGH, AND F2 IS THE ONLY TEST THAT MATTERS FOR THE DENOMINATOR
-----------------------------------------------------------------------------
``docs/BULGU-bos-kontrol-temiz-gorunur.md:79-85`` measured the failure this gate
is built against: two links declared, a third forgotten, and
``eligible=2, scanned=2, violations=0 -> GREEN`` - the missing link never
entered the universe at all. F1 (delete a link, expect RED) passes even for a
gate whose denominator comes from the declaration list, because deleting a link
leaves a hole IN THE DECLARATION. F2 deletes the link AND tries to shrink the
denominator, three different ways, and is the only test that proves the
denominator comes from ``interface_contract.json``.

WHY F5 AND F6 CARRY A **POSITIVE** EXPECTATION
---------------------------------------------
In F5 (pitch broken, position count untouched) R4 must STAY ``PASS``. In F6
(pin order permuted, pitch and count untouched) R3 **and** R4 must stay
``PASS``. Those PASSes are not defects - they are the PROOF that the gate exists
for a reason: 20 = 20 passes while pin 1 lands on pin 20. Precedent:
``ecad-varlik-kalifikasyonu:278-282``.

AND THE NEGATIVE SIDE
--------------------
An undamaged fixture must produce ``GREEN``. Without that, a gate that answers
RED to everything passes the whole suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

import interconnect_mating as mod  # noqa: E402

SCRIPT = SCRIPTS / "interconnect_mating.py"
REPORT_SCHEMA = SCHEMAS / "interconnect_mating_report.schema.json"
CHAIN_SCHEMA = (SCRIPTS.parents[1] / "skills" / "arayuz-eslesme-kapisi"
                / "references" / "interconnect_chain.schema.json")

# The real CAM-W chain. Not a fixture: the seed data derived from
# cam-w-og05b10/docs/interface_contract.json, its two pin files, bom/parts.json
# and proc-r-rk3588/docs/gaps.md B2.
REPO = SCRIPTS.parents[2]
CAM_W_CONTRACT = REPO / "cam-w-og05b10" / "docs" / "interface_contract.json"
CAM_W_CHAIN = REPO / "system-planning" / "interconnect_chain.json"


def run(chain: dict, contract=None, **kwargs) -> dict:
    return mod._run_fixture(chain, contract, **kwargs)


def codes(report: dict) -> set:
    return set(report["reason_codes"])


def rule_results(report: dict, rule: str, prefix: str = "mate:") -> list:
    return [r for r in report["rules"]
            if r["rule"] == rule and str(r["edge_id"]).startswith(prefix)]


# --------------------------------------------------------------------------- #
# Negative side first: a gate that can only say RED is not a gate
# --------------------------------------------------------------------------- #

class UndamagedFixtureTest(unittest.TestCase):

    def setUp(self):
        self.report = run(mod._fixture_chain())

    def test_an_undamaged_chain_is_green(self):
        self.assertEqual(self.report["status"], mod.GREEN,
                         (self.report["gate"]["reasons"],
                          [c["reason"] for c in self.report["caps"]]))

    def test_green_carries_the_domain_state_in_its_own_field(self):
        self.assertEqual(self.report["domain_state"], mod.CHAIN_COMPLETE)
        # The closed vocabulary is for gate.verdict ONLY. A domain word leaking
        # into it means a reader cannot tell a gate decision from a domain
        # answer (coverage-gate.md:90-112).
        self.assertNotIn(self.report["domain_state"], VERDICTS)

    def test_the_coverage_arithmetic_holds(self):
        self.assertEqual(mod.coverage_rule_violations(self.report["coverage"]),
                         [])
        cov = self.report["coverage"]
        self.assertEqual(cov["scanned"] + cov["excluded"], cov["eligible"])
        self.assertEqual(cov["violations"], 0)

    def test_exit_code_is_zero_only_on_green(self):
        self.assertEqual(mod.status_exit_code(self.report["status"]), 0)


# --------------------------------------------------------------------------- #
# F1 + F2 - the pair that is not negotiable
# --------------------------------------------------------------------------- #

class F1MissingLinkTest(unittest.TestCase):
    """F1: zincirden bir halkayı sil (``medium`` -> null) -> RED +
    ``NO_MATED_PART``, halka ADIYLA raporda."""

    def setUp(self):
        self.report = run(mod._fixture_chain(medium=None))

    def test_a_link_with_no_medium_is_red(self):
        self.assertEqual(self.report["status"], mod.RED)
        self.assertIn(mod.NO_MATED_PART, codes(self.report))

    def test_the_missing_link_is_named_in_the_report(self):
        named = [e for e in self.report["edges"]
                 if mod.NO_MATED_PART in e["reason_codes"]]
        self.assertEqual([e["edge_id"] for e in named], ["mate:ic_1"])
        # And it is named inside the FINDING too, not only in the edge list:
        # a reader binding an assertion to the finding must see the name.
        blob = json.dumps([f["violations"] for f in self.report["findings"]],
                          ensure_ascii=False)
        self.assertIn("ic_1", blob)

    def test_the_missing_link_stays_in_the_denominator(self):
        # The edge is SCANNED and counted as a violation. Dropping it from the
        # population is exactly the move this gate exists to prevent.
        self.assertEqual(self.report["coverage"]["excluded"], 0)
        self.assertGreaterEqual(self.report["coverage"]["violations"], 1)


class F2DenominatorProvenanceTest(unittest.TestCase):
    """F2: halkayı sil VE ``eligible``'ı da azaltmayı dene -> hâlâ RED.

    Bu, ``eligible`` paydasının doğru populasyondan geldiğini sınayan TEK
    testtir. F1 tek başına geçen bir kapı hâlâ evren-küçültmeyle kandırılabilir.
    """

    def test_the_chain_file_may_not_state_the_denominator(self):
        shrunk = mod._fixture_chain(medium=None)
        shrunk["chains"][0]["eligible"] = 2
        with self.assertRaises(mod.InterconnectMatingError) as ctx:
            run(shrunk)
        self.assertIn(mod.ELIGIBLE_FROM_CHAIN_REFUSED, str(ctx.exception))

    def test_every_denominator_alias_is_refused(self):
        for key in mod.FORBIDDEN_CHAIN_KEYS:
            chain = mod._fixture_chain()
            chain["chains"][0][key] = 1
            with self.assertRaises(mod.InterconnectMatingError,
                                   msg=f"{key} kabul edildi"):
                run(chain)

    def test_deleting_the_link_does_not_shrink_the_universe(self):
        empty = mod._fixture_chain()
        empty["chains"][0]["interconnects"] = []
        report = run(empty)
        self.assertGreaterEqual(report["coverage"]["eligible"], 1)
        self.assertEqual(report["status"], mod.RED, report["gate"]["reasons"])
        self.assertNotIn(report["status"], (mod.GREEN, mod.NO_COV))

    def test_deleting_the_whole_chain_does_not_shrink_the_universe(self):
        report = run({"schema_version": mod.CHAIN_SCHEMA_VERSION, "chains": []},
                     contract=mod._fixture_contract(2))
        # Two required interfaces in the contract -> two edges in the universe,
        # both violating. Not NO_COVERAGE: the contract says they must exist.
        self.assertEqual(report["coverage"]["eligible"], 2)
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.MISSING_TRANSITION, codes(report))

    def test_the_derivation_of_the_denominator_is_recorded(self):
        report = run(mod._fixture_chain())
        derivation = report["eligible_derivation"]
        self.assertIn("interface_contract", derivation["source"])
        self.assertTrue(derivation["required_links"])
        # The chain's own edge count is published BESIDE the denominator, so
        # the moment one stands in for the other is visible.
        self.assertIn("chain_declared_edge_count", derivation)

    def test_a_chain_bound_to_no_contract_link_is_red(self):
        stray = mod._fixture_chain()
        stray["chains"][0]["contract_ref"] = "fixture.json#/olmayan_arayuz"
        report = run(stray)
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.CONTRACT_REF_UNRESOLVED, codes(report))
        self.assertTrue(report["unbound_chains"])


# --------------------------------------------------------------------------- #
# F3, F4, F10 - the coverage guards
# --------------------------------------------------------------------------- #

class F3NoCoverageTest(unittest.TestCase):
    """F3: hiç zincir + hiç zorunlu bağ -> ``NO_COVERAGE`` +
    ``coverage.reason`` dolu."""

    def setUp(self):
        self.report = run(
            {"schema_version": mod.CHAIN_SCHEMA_VERSION, "chains": []},
            contract={"schema_version": "interface-contract/v1",
                      "interfaces": [], "coverage": {}, "status": "TBD"})

    def test_scanned_zero_is_no_coverage_never_green(self):
        self.assertEqual(self.report["coverage"]["scanned"], 0)
        self.assertEqual(self.report["status"], mod.NO_COV)

    def test_the_reason_is_filled_in(self):
        self.assertTrue(self.report["coverage"]["reason"])

    def test_no_coverage_exits_one_not_zero(self):
        self.assertEqual(mod.status_exit_code(self.report["status"]), 1)


class F4UnjustifiedExclusionTest(unittest.TestCase):
    """F4: gerekçesiz ``excluded`` -> RED + ``UNJUSTIFIED_EXCLUSION``."""

    def setUp(self):
        self.report = run(mod._fixture_chain(),
                          exclusions=["mate:ic_1:a"])

    def test_an_exclusion_without_a_reason_is_red(self):
        self.assertEqual(self.report["status"], mod.RED)
        self.assertIn(mod.UNJUSTIFIED_EXCLUSION, codes(self.report))

    def test_an_unjustified_exclusion_does_not_leave_the_population(self):
        # ``excluded`` is not the polite form of "we did not look"
        # (sistem-arayuz-sozlesmesi:150-158).
        self.assertEqual(self.report["coverage"]["excluded"], 0)
        self.assertTrue(self.report["unjustified_exclusions"])

    def test_a_justified_exclusion_does_leave_the_population(self):
        report = run(mod._fixture_chain(),
                     exclusions=["int:ic_1:medium:kablo ayrı zincirde ölçüldü"])
        self.assertEqual(report["coverage"]["excluded"], 1)
        self.assertEqual(mod.coverage_rule_violations(report["coverage"]), [])
        self.assertEqual(report["excluded_edges"][0]["edge_id"],
                         "int:ic_1:medium")

    def test_an_edge_id_containing_colons_is_split_correctly(self):
        # ``mate:ic_1:a`` is an id, not "edge 'mate' with reason 'ic_1:a'".
        # Splitting on the first colon turned an UNJUSTIFIED exclusion into a
        # justified one and disabled this whole guard.
        justified, unjustified = mod.parse_exclusions(
            ["mate:ic_1:a", "mate:ic_1:b:kayıt başka zincirde"],
            ["mate:ic_1:a", "mate:ic_1:b"])
        self.assertEqual([u["edge_id"] for u in unjustified], ["mate:ic_1:a"])
        self.assertEqual(justified,
                         [{"edge_id": "mate:ic_1:b",
                           "reason": "kayıt başka zincirde"}])


class F10CoverageArithmeticTest(unittest.TestCase):
    """F10: ``excluded``'ı elle 1 artır -> ``INVALID_COVERAGE`` (exit 2)."""

    def setUp(self):
        self.report = run(mod._fixture_chain(),
                          exclusions=["mate:hayali_kenar:bahane"])

    def test_excluding_an_edge_outside_the_universe_breaks_the_arithmetic(self):
        self.assertEqual(self.report["status"], mod.INVALID_COVERAGE)
        self.assertIn(mod.UNKNOWN_EXCLUSION, codes(self.report))

    def test_invalid_coverage_exits_two(self):
        self.assertEqual(mod.status_exit_code(self.report["status"]), 2)

    def test_scanned_is_counted_not_derived(self):
        # If ``scanned`` were ``eligible - excluded`` the sum would always
        # hold and a phantom exclusion would silently shrink the numerator.
        cov = self.report["coverage"]
        self.assertNotEqual(cov["scanned"] + cov["excluded"], cov["eligible"])
        self.assertTrue(mod.coverage_rule_violations(cov))


# --------------------------------------------------------------------------- #
# F5 + F6 - and their POSITIVE expectations
# --------------------------------------------------------------------------- #

class F5PitchTest(unittest.TestCase):
    """F5: pitch 0.5 -> 0.4, POZİSYON SABİT. R4 aynı koşuda ``PASS`` kalmalı."""

    def setUp(self):
        self.report = run(mod._fixture_chain(pitch_b=0.4))

    def test_a_pitch_mismatch_is_red(self):
        self.assertEqual(self.report["status"], mod.RED)
        self.assertIn(mod.PITCH_MISMATCH, codes(self.report))

    def test_r4_stays_pass_and_that_is_the_point(self):
        results = rule_results(self.report, "R4")
        self.assertTrue(results)
        for entry in results:
            self.assertEqual(entry["status"], mod.PASS, entry)
        # PASS kalması bir kusur değil, bu kapının varlık sebebinin KANITIDIR:
        # sayı eşitliği kimlik eşitliği değildir.

    def test_r4_never_carries_signoff_authority(self):
        for entry in rule_results(self.report, "R4"):
            self.assertEqual(entry["authority"], mod.SCREEN)

    def test_a_screen_only_fail_can_still_close_the_gate(self):
        # ``screen_only`` means "a PASS does not sign off", NOT "a FAIL says
        # nothing": a 30-contact cable does not fit a 22-contact socket.
        report = run(mod._fixture_chain())
        report = run(_with_positions(mod._fixture_chain(), 4))
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.POSITION_COUNT_MISMATCH, codes(report))


class F6PinOrderTest(unittest.TestCase):
    """F6: pin sırasını permüte et, pitch + sayı SABİT -> RED +
    ``PIN_ORDER_MISMATCH``; R3 **ve** R4 ``PASS`` kalmalı."""

    def setUp(self):
        self.report = run(mod._fixture_chain(reverse_b=True))

    def test_a_permutation_is_red(self):
        self.assertEqual(self.report["status"], mod.RED)

    def test_the_same_net_set_in_the_wrong_order_is_named_as_such(self):
        self.assertIn(mod.PIN_ORDER_MISMATCH, codes(self.report))
        self.assertIn(mod.NET_NAME_MISMATCH, codes(self.report))

    def test_the_permutation_lands_a_rail_on_a_data_pin(self):
        # R12 is the KILLER rule: a wrong pin map in MIPI is not "does not
        # work", it is 3V3 riding a D-PHY lane -> permanent damage.
        self.assertIn(mod.POWER_ON_SIGNAL_PIN, codes(self.report))

    def test_geometry_rules_stay_pass(self):
        for rule in ("R3", "R4"):
            results = rule_results(self.report, rule)
            self.assertTrue(results, rule)
            for entry in results:
                self.assertEqual(entry["status"], mod.PASS, (rule, entry))

    def test_a_different_net_name_is_not_a_permutation(self):
        # ``MIPI_D0_P`` != ``CSI_D0_P`` is a MISMATCH, and the fix is not
        # normalisation (ecad-varlik-kalifikasyonu:130-133). It is also not a
        # permutation: the net SETS differ.
        aliased = mod._fixture_chain()
        face = aliased["chains"][0]["nodes"][1]["faces"][0]
        face["pin_map"] = [dict(p) for p in face["pin_map"]]
        face["pin_map"][1]["net"] = "MIPI_D0_P"
        report = run(aliased)
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.NET_NAME_MISMATCH, codes(report))
        self.assertNotIn(mod.PIN_ORDER_MISMATCH, codes(report))


# --------------------------------------------------------------------------- #
# F7, F8, F9 - authority, provenance, freshness
# --------------------------------------------------------------------------- #

class F7DerivedDataTest(unittest.TestCase):
    """F7: pitch'i datasheet yerine türetilmiş değerden ver ->
    ``SCREEN_ONLY`` / ``LOCAL_POLICY``, asla ``GREEN``."""

    def test_derived_data_never_signs_off(self):
        report = run(mod._fixture_chain(basis="derived"))
        self.assertIn(report["status"], (mod.SCREEN_ONLY, mod.LOCAL_POLICY))
        self.assertNotEqual(report["status"], mod.GREEN)

    def test_a_local_policy_basis_is_local_policy(self):
        report = run(mod._fixture_chain(basis="local_policy"))
        self.assertEqual(report["status"], mod.LOCAL_POLICY)

    def test_a_vendor_page_is_not_an_authority(self):
        report = run(mod._fixture_chain(basis="vendor_page"))
        self.assertEqual(report["status"], mod.SCREEN_ONLY)


class F8ProvenanceTest(unittest.TestCase):
    """F8: provenance'tan ``revision`` düşür -> en fazla ``CONFIRM``."""

    def setUp(self):
        self.report = run(mod._fixture_chain(drop_revision=True))

    def test_a_missing_revision_caps_at_confirm(self):
        self.assertEqual(self.report["status"], mod.CONFIRM)

    def test_the_cap_names_the_field_and_the_reason(self):
        reasons = [c["reason"] for c in self.report["caps"]
                   if c["verdict"] == mod.CONFIRM]
        self.assertTrue(any("revision" in r for r in reasons), reasons)

    def test_provenance_gaps_are_assertable_in_their_own_block(self):
        # Deliberately NOT a finding: as a FAIL it would equate "unsourced"
        # with "wrong pin" (RED); as NO_COVERAGE it would claim nobody looked.
        # Both are false. The right answer is CONFIRM, carried by a cap - and
        # the count stays assertable in its own coverage block.
        block = self.report["provenance_coverage"]
        self.assertEqual(mod.coverage_rule_violations(block), [])
        self.assertGreaterEqual(block["violations"], 1)
        self.assertTrue(self.report["provenance_gaps"])
        self.assertNotIn(mod.PROVENANCE_AXIS, self.report["by_check"])


class F9StaleTest(unittest.TestCase):
    """F9: kapı koştuktan sonra segment kaydını değiştir -> ``STALE``."""

    def test_an_input_that_moved_under_the_gate_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "interface_contract.json"
            chain = root / "interconnect_chain.json"
            contract.write_text(json.dumps(mod._fixture_contract()),
                                encoding="utf-8")
            chain.write_text(json.dumps(mod._fixture_chain()), encoding="utf-8")
            spec = root / "connector_spec.json"
            spec.write_text(json.dumps(mod._fixture_connector_spec()),
                            encoding="utf-8")
            first = mod.build_report(contract, chain,
                                     connector_spec_path=spec)
            self.assertEqual(first["status"], mod.GREEN)
            prior = root / "prior.json"
            mod.write_report(first, prior)

            mutated = mod._fixture_chain()
            mutated["chains"][0]["note"] = "kapıdan SONRA değişti"
            chain.write_text(json.dumps(mutated), encoding="utf-8")
            second = mod.build_report(contract, chain, prior_path=prior,
                                      connector_spec_path=spec)

            self.assertEqual(second["status"], mod.STALE)
            self.assertTrue(second["stale"]["changed"])
            self.assertEqual(second["stale"]["changed"][0]["input"], "chain")

    def test_an_unchanged_input_is_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "interface_contract.json"
            chain = root / "interconnect_chain.json"
            contract.write_text(json.dumps(mod._fixture_contract()),
                                encoding="utf-8")
            chain.write_text(json.dumps(mod._fixture_chain()), encoding="utf-8")
            spec = root / "connector_spec.json"
            spec.write_text(json.dumps(mod._fixture_connector_spec()),
                            encoding="utf-8")
            prior = root / "prior.json"
            mod.write_report(mod.build_report(contract, chain,
                                              connector_spec_path=spec), prior)
            again = mod.build_report(contract, chain, prior_path=prior,
                                     connector_spec_path=spec)
            self.assertEqual(again["status"], mod.GREEN)


# --------------------------------------------------------------------------- #
# The verdict namespace and the authority axis
# --------------------------------------------------------------------------- #

VERDICTS = frozenset({
    "GREEN", "RED", "CONFIRM", "SCREEN_ONLY", "LOCAL_POLICY", "SIM_ONLY",
    "NO_COVERAGE", "INVALID_METRIC", "INVALID_COVERAGE", "INVALID_SPARAM",
    "INCONCLUSIVE", "STALE", "GEOMETRY_PRECHECK_PASS", "TBD", "API_YOK"})


class VerdictNamespaceTest(unittest.TestCase):

    def test_the_vocabulary_is_the_closed_fifteen(self):
        self.assertEqual(len(VERDICTS), 15)

    def test_no_run_produces_a_verdict_outside_the_vocabulary(self):
        runs = [
            run(mod._fixture_chain()),
            run(mod._fixture_chain(medium=None)),
            run(mod._fixture_chain(pitch_b=0.4)),
            run(mod._fixture_chain(basis="derived")),
            run(mod._fixture_chain(drop_revision=True)),
            run(mod._fixture_chain(), exclusions=["mate:hayali:x"]),
        ]
        for report in runs:
            self.assertIn(report["status"], VERDICTS, report["status"])
            self.assertEqual(report["status"], report["effective_status"])
            self.assertEqual(report["status"], report["gate"]["verdict"])

    def test_the_domain_state_never_leaks_into_the_verdict_field(self):
        for report in (run(mod._fixture_chain()),
                       run(mod._fixture_chain(medium=None))):
            self.assertIn(report["domain_state"], mod.DOMAIN_ORDER)
            self.assertNotIn(report["domain_state"], VERDICTS)

    def test_a_side_that_declared_itself_wrong_is_red_with_its_own_state(self):
        # Real case: proc-a-cv72s/pins/som_cv72s_if.json:9 - the side LOOKED,
        # the face EXISTS, and it declared explicitly that it does not match.
        # Not NO_COVERAGE (somebody looked), not a geometry mismatch (the type
        # is unknown), never PASS. No new verdict is added to the vocabulary.
        chain = mod._fixture_chain()
        face = chain["chains"][0]["nodes"][1]["faces"][0]
        face["declared_nonmatching"] = True
        face["declared_nonmatching_note"] = \
            "gercek SoM pinlerine KARSILIK GELMEZ"
        report = run(chain)
        self.assertEqual(report["status"], mod.RED)
        self.assertEqual(report["domain_state"], mod.DECLARED_NONMATCHING)

    def test_every_rule_declares_an_authority(self):
        report = run(mod._fixture_chain())
        self.assertTrue(report["rules"])
        for entry in report["rules"]:
            self.assertIn(entry["authority"],
                          (mod.SIGNOFF, mod.SCREEN, mod.RECORD), entry)

    def test_a_record_only_fail_caps_but_does_not_close_the_gate(self):
        # R19 (plating) is record_only: a mismatch is recorded and adds a
        # CONFIRM cap. It must not read as a measured incompatibility.
        chain = mod._fixture_chain()
        chain["chains"][0]["nodes"][1]["faces"][0]["contact_finish"] = "Sn"
        # The second document has to agree with the chain about J2's finish, or
        # SPEC_CONTRADICTS_CHAIN fires too and the run is RED for a different
        # (and correct) reason - which would hide what this test measures.
        spec = mod._fixture_connector_spec()
        for entry in spec["connectors"]:
            if entry["refdes"] == "J2":
                entry["contact_finish"] = "Sn"
        report = run(chain, spec=spec)
        self.assertEqual(report["status"], mod.CONFIRM, report["gate"])
        self.assertIn(mod.PLATING_MISMATCH, codes(report))


# --------------------------------------------------------------------------- #
# Authority boundaries and the L3 handoff
# --------------------------------------------------------------------------- #

class AuthorityBoundaryTest(unittest.TestCase):

    def setUp(self):
        self.report = run(mod._fixture_chain())

    def test_the_gate_never_claims_a_signoff(self):
        self.assertIs(self.report["signoff"], False)
        self.assertIs(self.report["writes_board"], False)

    def test_l3_is_not_listed_because_no_physical_measurement_is_published(self):
        # coverage-gate.md:75-77: a skill that does not publish
        # PHYSICAL_MEASUREMENT cannot close an L3 rule even if it lists L3.
        self.assertEqual(self.report["assurance_loop"], ["L1", "L2"])
        self.assertNotIn("L3", self.report["assurance_loop"])

    def test_the_limit_owner_is_this_gate_not_the_l3_producer(self):
        self.assertTrue(self.report["l3_handoff"])
        for row in self.report["l3_handoff"]:
            self.assertEqual(row["owner"], "arayuz-eslesme-kapisi")
            authority = row["limit"]["authority"]
            self.assertNotEqual(authority["owner_skill"],
                                "l3-bringup-karakterizasyon",
                                "SELF_AUTHORED_LIMIT: whoever takes the "
                                "measurement may not write the limit")

    def test_every_l3_row_carries_a_limit(self):
        # A row without a limit is NO_COVERAGE: physical_measurement.py's
        # rule_limit has no fallback branch.
        for row in self.report["l3_handoff"]:
            self.assertIsNotNone(row["limit"])
            self.assertTrue(row["limit"]["units"])
            self.assertTrue({"min", "max"} & set(row["limit"]))

    def test_fixture_field_names_are_reused_not_invented(self):
        # konnektor/SKILL.md:200-203. A quantity living under two names carries
        # two different values.
        allowed = {"actual_connector_mpn", "actual_fpc_thickness",
                   "datum_fixture_used", "insert_remove_without_force",
                   "actuator_full_lock_visible", "post_mate_clearance_margin"}
        for row in self.report["l3_handoff"]:
            self.assertTrue(row["fixture_fields"])
            self.assertLessEqual(set(row["fixture_fields"]), allowed)

    def test_a_missing_bom_line_is_confirm_not_red(self):
        # bom-lifecycle owns MPN selection; this gate only records the gap and
        # opens an L2 edge.
        chain = mod._fixture_chain()
        chain["chains"][0]["interconnects"][0]["medium"]["bom_line"] = None
        report = run(chain)
        self.assertEqual(report["status"], mod.CONFIRM)
        self.assertIn(mod.BOM_LINE_ABSENT, codes(report))


# --------------------------------------------------------------------------- #
# Both edge classes are in the universe
# --------------------------------------------------------------------------- #

class EdgeUniverseTest(unittest.TestCase):

    def test_internal_and_mating_edges_are_both_eligible(self):
        report = run(mod._fixture_chain())
        classes = {e["edge_class"] for e in report["edges"]}
        self.assertEqual(classes, {"mating", "internal"})
        self.assertEqual(report["coverage"]["eligible"], len(report["edges"]))

    def test_a_cable_creates_two_matings_and_one_internal_edge(self):
        report = run(mod._fixture_chain())
        ids = [e["edge_id"] for e in report["edges"]]
        self.assertEqual(sorted(ids),
                         ["int:ic_1:medium", "mate:ic_1:a", "mate:ic_1:b"])

    def test_a_direct_mate_creates_one_edge_and_no_internal_edge(self):
        chain = mod._fixture_chain()
        chain["chains"][0]["interconnects"][0]["medium"] = {
            "kind": "DIRECT_MATE", "mpn": "X", "manufacturer": "Y",
            "revision": "A"}
        report = run(chain)
        self.assertEqual([e["edge_id"] for e in report["edges"]],
                         ["mate:ic_1"])

    def test_an_undeclared_cable_mapping_is_no_coverage(self):
        # Kablonun iki ucundaki sıranın düz mü ters mi olduğu tek yüzlü bir
        # modelde GÖRÜNMEZ. Beyan yoksa NO_COVERAGE - asla GREEN.
        chain = mod._fixture_chain(mapping=None)
        chain["chains"][0]["interconnects"][0]["medium"]["pin_to_pin"] = []
        report = run(chain)
        self.assertIn(mod.MAPPING_UNDECLARED, codes(report))
        self.assertNotEqual(report["status"], mod.GREEN)

    def test_a_custom_mapping_with_an_empty_table_is_red(self):
        chain = mod._fixture_chain(mapping="custom")
        chain["chains"][0]["interconnects"][0]["medium"]["pin_to_pin"] = []
        report = run(chain)
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.PINMAP_UNDECLARED, codes(report))

    def test_a_board_internal_edge_is_measured(self):
        # CAM-W's own measured case: J101 pin 17 = XVCLK, J201 pin 17 = FSYNC.
        # Counting the board as ONE face would never have measured it.
        chain = mod._fixture_chain()
        node = chain["chains"][0]["nodes"][0]
        second = json.loads(json.dumps(node["faces"][0]))
        second["face_id"] = "J1B"
        second["refdes"] = "J1B"
        second["pin_map"] = [dict(p) for p in second["pin_map"]]
        second["pin_map"][1]["net"] = "BASKA_NET"
        node["faces"].append(second)
        node["internal_mappings"] = [
            {"from_face": "J1", "to_face": "J1B", "mapping": "straight",
             "pin_to_pin": []}]
        report = run(chain)
        internal = [e for e in report["edges"]
                    if e["edge_id"] == "int:board_a:J1>J1B"]
        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["status"], mod.FAIL)
        self.assertIn(mod.NET_NAME_MISMATCH, codes(report))

    def test_a_node_shared_by_two_chains_counts_its_internal_edge_once(self):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        report = mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN)
        shared = [e for e in report["edges"]
                  if e["edge_id"] == "int:cam-w-og05b10:J101>J201"]
        self.assertEqual(len(shared), 1,
                         "J101 <-> J201 sürekliliği TEK BİR FAKTTIR")


class VendorLetterTest(unittest.TestCase):
    """Satıcı harfi ASLA anahtar olmaz: Molex A/D, Würth 1/2, piyasa A/B - ve
    MOLEX'TE "B" DİYE BİR TİP YOKTUR."""

    def test_the_vendor_letter_feeds_no_decision(self):
        for letter in ("A", "B", "D", "1", "2"):
            chain = mod._fixture_chain()
            chain["chains"][0]["interconnects"][0]["medium"][
                "vendor_contact_letter"] = letter
            self.assertEqual(run(chain)["status"], mod.GREEN,
                             f"satıcı harfi {letter!r} verdict'i değiştirdi")

    def test_the_normalised_enum_does_feed_a_decision(self):
        chain = mod._fixture_chain()
        # stiffener aynı yüzde -> SAME_SIDE beklenir; OPPOSITE_SIDE beyanı
        # tutarsızdır.
        chain["chains"][0]["interconnects"][0]["medium"][
            "contact_orientation"] = "OPPOSITE_SIDE"
        report = run(chain)
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.CABLE_TYPE_MISMATCH, codes(report))


# --------------------------------------------------------------------------- #
# The real CAM-W seed data
# --------------------------------------------------------------------------- #

class CamWSeedTest(unittest.TestCase):
    """system-planning/interconnect_chain.json against the real board contract.

    The expected answer is RED, and that is the CORRECT answer: the sensor
    head module is unnamed, W1 has no BOM line, and PROC-R's camera connector
    was never drawn (gaps.md B2). Nothing here was prettied up to go green.
    """

    @classmethod
    def setUpClass(cls):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            raise unittest.SkipTest("CAM-W tohum verisi bu checkout'ta yok")
        cls.report = mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN)

    def test_the_real_chain_is_red(self):
        self.assertEqual(self.report["status"], mod.RED)
        self.assertEqual(self.report["domain_state"], mod.CHAIN_BROKEN)

    def test_the_denominator_is_seven_edges_from_two_contract_links(self):
        self.assertEqual(self.report["contract_coverage"]["eligible"], 2)
        self.assertEqual(self.report["coverage"]["eligible"], 7)
        self.assertEqual(mod.coverage_rule_violations(self.report["coverage"]),
                         [])

    def test_the_board_local_contract_shape_is_recorded_not_rejected(self):
        # The canonical schema wants ``interfaces[]``; the real board file uses
        # named sections. Reading one and refusing the other would close the
        # gate to the repo's own data; reading both and staying silent would
        # hide that two contract shapes exist.
        self.assertIn("board-local",
                      self.report["eligible_derivation"]["contract_shape"])

    def test_the_unnamed_sensor_head_module_is_a_missing_medium(self):
        edge = self._edge("mate:ic_head_to_j101")
        self.assertIn(mod.NO_MATED_PART, edge["reason_codes"])

    def test_the_30_pin_ipex_does_not_fit_the_22_pin_hirose(self):
        edge = self._edge("mate:ic_j201_to_procr_cam0:b")
        self.assertIn(mod.POSITION_COUNT_MISMATCH, edge["reason_codes"])
        self.assertIn(mod.INTERFACE_CLASS_MISMATCH, edge["reason_codes"])

    def test_four_lanes_do_not_fit_a_two_lane_port(self):
        # Fiziksel kapı geçilse bile MİMARİ GEÇERSİZ.
        self.assertIn(mod.LANE_COUNT_MISMATCH, codes(self.report))

    def test_w1_has_no_bom_line_and_that_is_recorded_not_fixed(self):
        self.assertIn(mod.BOM_LINE_ABSENT, codes(self.report))

    def test_the_board_internal_transition_is_pinmap_undeclared(self):
        edge = self._edge("int:cam-w-og05b10:J101>J201")
        self.assertIn(mod.PINMAP_UNDECLARED, edge["reason_codes"])

    def test_the_nda_pinout_never_reads_as_passed(self):
        edge = self._edge(
            "int:sensor_head_module_og05b10:sensor_die>module_out")
        self.assertNotEqual(edge["status"], mod.PASS)

    def test_nothing_in_the_seed_data_reads_as_green(self):
        for edge in self.report["edges"]:
            self.assertNotEqual(edge["status"], mod.PASS, edge["edge_id"])

    def _edge(self, edge_id: str) -> dict:
        for edge in self.report["edges"]:
            if edge["edge_id"] == edge_id:
                return edge
        raise AssertionError(f"{edge_id} raporda yok")


# --------------------------------------------------------------------------- #
# CLI contract and schemas
# --------------------------------------------------------------------------- #

class CliContractTest(unittest.TestCase):

    def test_the_self_test_passes_as_a_subprocess(self):
        completed = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SELF-TEST PASS", completed.stdout + completed.stderr)

    def test_stdout_is_json_only_and_stderr_carries_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "c.json"
            chain = root / "k.json"
            contract.write_text(json.dumps(mod._fixture_contract()),
                                encoding="utf-8")
            chain.write_text(json.dumps(mod._fixture_chain()), encoding="utf-8")
            spec = root / "connector_spec.json"
            spec.write_text(json.dumps(mod._fixture_connector_spec()),
                            encoding="utf-8")
            out = root / "report.json"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--contract", str(contract),
                 "--chain", str(chain), "--connector-spec", str(spec),
                 "--json", str(out)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)     # stdout: JSON ONLY
            self.assertEqual(payload["schema_version"], mod.SCHEMA_VERSION)
            self.assertIn("interconnect_mating", completed.stderr)
            # ``--json`` ALWAYS writes (canonical CLI contract).
            self.assertTrue(out.is_file())
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))
                             ["status"], payload["status"])

    def test_input_json_is_the_way_to_pass_input_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "c.json"
            chain = root / "k.json"
            contract.write_text(json.dumps(mod._fixture_contract()),
                                encoding="utf-8")
            chain.write_text(json.dumps(mod._fixture_chain()), encoding="utf-8")
            cspec = root / "connector_spec.json"
            cspec.write_text(json.dumps(mod._fixture_connector_spec()),
                             encoding="utf-8")
            spec = root / "in.json"
            spec.write_text(json.dumps({"contract": str(contract),
                                        "chain": str(chain),
                                        "connector_spec": str(cspec)}),
                            encoding="utf-8")
            # stdout is JSON only, and this call goes through emit(); capture
            # it so the suite's own output stays readable.
            import contextlib
            import io
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = mod.run_cli(mod.main, ["--input-json", str(spec),
                                              "--json", str(root / "o.json")])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(captured.getvalue())["status"],
                             mod.GREEN)

    def test_a_missing_input_is_exit_two_not_a_verdict(self):
        code = mod.run_cli(mod.main, ["--chain", "/nonexistent.json"])
        self.assertEqual(code, 2)

    def test_running_with_only_one_document_is_refused(self):
        # Payda sözleşmeden, pay zincirden gelir. Tek dosyayla koşmak, kapının
        # kendi beyanını kendi beyanıyla karşılaştırması olurdu.
        with self.assertRaises(mod.InterconnectMatingError):
            mod.main(["--chain", str(CAM_W_CHAIN)])

    def test_a_chain_schema_version_bump_is_not_ignored(self):
        chain = mod._fixture_chain()
        chain["schema_version"] = "interconnect-chain/v2"
        with self.assertRaises(mod.InterconnectMatingError):
            run(chain)

    def test_both_schema_files_exist_and_are_valid_json(self):
        for path in (REPORT_SCHEMA, CHAIN_SCHEMA):
            self.assertTrue(path.is_file(), path)
            json.loads(path.read_text(encoding="utf-8"))

    def test_the_report_carries_every_field_its_schema_requires(self):
        schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
        report = run(mod._fixture_chain())
        for field in schema["required"]:
            self.assertIn(field, report, field)

    def test_the_report_status_is_inside_the_schema_enum(self):
        schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
        allowed = set(schema["$defs"]["verdict"]["enum"])
        self.assertEqual(allowed, set(VERDICTS))
        states = set(schema["properties"]["domain_state"]["enum"])
        self.assertEqual(states, set(mod.DOMAIN_ORDER))

    def test_inputs_are_bound_by_sha256(self):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        report = mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN)
        for name in ("contract", "chain"):
            self.assertTrue(report["inputs"][name]["sha256"], name)

    def test_the_limits_section_is_not_empty(self):
        report = run(mod._fixture_chain())
        self.assertTrue(report["limits"])
        joined = " ".join(report["limits"])
        for owner in ("bom-lifecycle", "sematik-erc", "mekanik-dxf",
                      "konnektor", "l3-bringup-karakterizasyon"):
            self.assertIn(owner, joined, owner)


class ConnectorSpecCrossCheckTest(unittest.TestCase):
    """`connector_spec.json` OPSİYONELDİR AMA SESSİZ DEĞİLDİR.

    `required: false` kapıyı deponun kendi verisine kapatmamak içindir (CAM-W'de
    o dosya yok) — çapraz doğrulamayı GEÇMİŞ saymak için değil.
    `coverage-gate.md:21`: *"eksik kanıt CONFIRM'dür, PASS değildir."*
    """

    def test_a_missing_connector_spec_caps_at_confirm(self):
        report = run(mod._fixture_chain(), spec=None)
        self.assertEqual(report["status"], mod.CONFIRM, report["gate"])
        self.assertNotEqual(report["status"], mod.GREEN)
        self.assertIn(mod.CONNECTOR_SPEC_ABSENT, codes(report))

    def test_the_absence_is_an_explicit_cap_and_a_recorded_exclusion(self):
        report = run(mod._fixture_chain(), spec=None)
        self.assertTrue([c for c in report["caps"]
                         if c["rule"] == "connector_spec"])
        # rollup_status(excluded=...) is for DELIBERATE, RECORDABLE scope
        # decisions; the reason has to be written down.
        self.assertTrue(report["excluded_checks"])
        self.assertTrue(report["excluded_checks"][0]["reason"])

    def test_supplying_it_reopens_green_so_the_cap_is_not_unconditional(self):
        self.assertEqual(run(mod._fixture_chain())["status"], mod.GREEN)

    def test_a_contradicting_spec_is_red_not_a_cap(self):
        # Two documents that disagree about one quantity is a FAIL, because
        # after that neither can be believed. And an excluded FAIL is never
        # skipped: a measured mismatch cannot be scoped out after the fact.
        report = run(mod._fixture_chain(),
                    spec=mod._fixture_connector_spec(pitch=0.4))
        self.assertEqual(report["status"], mod.RED)
        self.assertIn(mod.SPEC_CONTRADICTS_CHAIN, codes(report))
        self.assertTrue(report["connector_spec_violations"])

    def test_a_refdes_the_spec_does_not_carry_is_reported(self):
        spec = mod._fixture_connector_spec()
        spec["connectors"] = [c for c in spec["connectors"]
                              if c["refdes"] != "J2"]
        report = run(mod._fixture_chain(), spec=spec)
        self.assertIn(mod.REFDES_NOT_IN_CONNECTOR_SPEC, codes(report))

    def test_a_spec_schema_version_bump_is_not_ignored(self):
        spec = mod._fixture_connector_spec()
        spec["schema_version"] = "connector-spec/v2"
        with self.assertRaises(mod.InterconnectMatingError):
            run(mod._fixture_chain(), spec=spec)

    def test_the_declared_input_is_actually_consumed(self):
        # An input that is declared and never read is the 'declared-but-ungated'
        # defect class; the measured precedent is contact_face, `required` in a
        # schema with zero consumers.
        report = run(mod._fixture_chain())
        self.assertIn("connector_spec", report["inputs"])
        self.assertTrue(report["inputs"]["connector_spec"]["sha256"])
        self.assertIn("connector_spec_zincirle_uyusuyor", report["by_check"])
        self.assertEqual(
            report["by_check"]["connector_spec_zincirle_uyusuyor"]["status"],
            mod.PASS)


class RuleCoverageTest(unittest.TestCase):
    """Kural BAŞINA payda — `coverage_selector`'ın bağlanabildiği tek yer.

    Bulgular kuralları gruplara topluyor (`pin_ortusmesi` R10+R11+R13'ü birlikte
    sayar), dolayısıyla `metrics.pin_ortusmesi` üzerine yazılmış bir R11
    assertion'ı R10'un ihlalleri üzerinden geçer/kalır — ölçmediği bir şeyi
    iddia eder.
    """

    def test_every_rule_has_its_own_denominator(self):
        report = run(mod._fixture_chain())
        for rule_id in mod.ALL_RULE_IDS:
            self.assertIn(rule_id, report["rule_coverage"], rule_id)

    def test_an_unmeasured_rule_is_no_coverage_not_zero_violations(self):
        report = run(mod._fixture_chain())
        for rule_id, block in report["rule_coverage"].items():
            if block["scanned"] == 0:
                self.assertEqual(block["status"], mod.NO_COVERAGE, rule_id)
            self.assertLessEqual(block["violations"], block["scanned"], rule_id)

    def test_a_record_only_violation_is_counted_separately(self):
        # Folding a record_only FAIL into `violations` would hand it an
        # authority it does not have.
        chain = mod._fixture_chain()
        chain["chains"][0]["nodes"][1]["faces"][0]["contact_finish"] = "Sn"
        spec = mod._fixture_connector_spec()
        for entry in spec["connectors"]:
            if entry["refdes"] == "J2":
                entry["contact_finish"] = "Sn"
        block = run(chain, spec=spec)["rule_coverage"]["R19"]
        self.assertEqual(block["violations"], 0)
        self.assertGreater(block["record_only_violations"], 0)
        self.assertEqual(block["authority"], mod.RECORD)

    def test_the_permutation_shows_up_per_rule(self):
        block = run(mod._fixture_chain(reverse_b=True))["rule_coverage"]
        self.assertGreater(block["R11"]["violations"], 0)
        self.assertGreater(block["R12"]["violations"], 0)
        # F5/F6'nın pozitif tarafı kural başına paydada da görünür.
        self.assertEqual(block["R3"]["status"], mod.PASS)
        self.assertEqual(block["R4"]["status"], mod.PASS)

    def test_the_real_chain_leaves_the_pin_rules_unmeasured(self):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        # PROC-R'nin pin haritası boş (gaps.md B2: blok çizilmedi), dolayısıyla
        # R11/R12/R13 hiçbir kenarda ölçülemiyor. `violations = 0` burada
        # "temiz" DEĞİL, "bakılamadı"dır - ve buna bağlı bir assertion coverage
        # kapısında durur.
        report = mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN)
        for rule_id in ("R11", "R12", "R13"):
            block = report["rule_coverage"][rule_id]
            self.assertEqual(block["scanned"], 0, rule_id)
            self.assertEqual(block["status"], mod.NO_COVERAGE, rule_id)


class MatrixAssertionTest(unittest.TestCase):
    """Matris satırlarımın `coverage_selector`'ı GERÇEKTEN bağlı mı.

    `BULGU-bos-kontrol-temiz-gorunur.md:126-133`: selector bağlanmamış bir A/B
    assertion'ı yüklenmemelidir (`COVERAGE_UNBOUND`), çünkü
    `metrics.<check> at_most 0` yazan bir assertion *"ihlal yok"* ile *"hiçbir
    şeye bakılmadı"*yı aynı sayıya indirger. Bu kapı tam olarak o tuzağı
    kapatmak için var; kendi satırlarının aynı tuzağa düşmesi kabul edilemez.
    """

    SET = SCRIPTS.parents[0] / "fixtures" / "assertions" / "matrix_core.json"
    MATRIX = SCRIPTS.parents[1] / "skills" / "dogrulama-matrisi" / "SKILL.md"

    def assertions(self) -> list:
        data = json.loads(self.SET.read_text(encoding="utf-8"))
        return [a for a in data["assertions"] if a["id"].startswith("ic-")]

    def test_the_set_declares_assertions_for_this_gate(self):
        self.assertTrue(self.assertions())

    def test_every_one_binds_its_coverage_selector_per_rule(self):
        for entry in self.assertions():
            self.assertTrue(entry["coverage_selector"].startswith(
                "rule_coverage."), entry["id"])
            self.assertTrue(entry["coverage_selector"].endswith(".scanned"),
                            entry["id"])
            # Payda ile pay AYNI kuralı adlandırmak zorunda.
            self.assertEqual(
                entry["selector"].rsplit(".", 1)[0],
                entry["coverage_selector"].rsplit(".", 1)[0], entry["id"])

    def test_the_declared_paths_exist_in_a_real_report(self):
        report = run(mod._fixture_chain())
        for entry in self.assertions():
            rule_id = entry["selector"].split(".")[1]
            self.assertIn(rule_id, report["rule_coverage"], entry["id"])
            self.assertIn("violations", report["rule_coverage"][rule_id])
            self.assertIn("scanned", report["rule_coverage"][rule_id])

    def test_the_set_loads_and_names_only_real_matrix_rows(self):
        script = SCRIPTS / "assert_matrix.py"
        if not script.is_file():                         # pragma: no cover
            self.skipTest("assert_matrix.py bu checkout'ta yok")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "assertions.json"
            subprocess.run(
                [sys.executable, str(script), "--set", str(self.SET),
                 "--matrix", str(self.MATRIX), "--json", str(out)],
                capture_output=True, text=True)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["syntax_errors"], [])
            coverage = payload["matrix_coverage"]
            self.assertEqual(coverage["orphan_assertions"], [])
            issues = [(m["assertion"], m["issue"])
                      for m in coverage["matched"] if "issue" in m]
            self.assertEqual(issues, [], issues)

    def test_the_assertions_fire_against_a_broken_report(self):
        """Yüklenen bir assertion ile ATEŞ EDEN bir assertion aynı şey değil."""
        script = SCRIPTS / "assert_matrix.py"
        if not script.is_file():                         # pragma: no cover
            self.skipTest("assert_matrix.py bu checkout'ta yok")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            report = work / "report.json"
            mod.write_report(run(mod._fixture_chain(reverse_b=True)), report)
            out = work / "assertions.json"
            subprocess.run(
                [sys.executable, str(script), "--set", str(self.SET),
                 "--matrix", str(self.MATRIX),
                 "--evidence", f"interconnect={report}", "--json", str(out)],
                capture_output=True, text=True)
            payload = json.loads(out.read_text(encoding="utf-8"))
            outcomes = {o["assertion"]: o for o in payload["outcomes"]}
            self.assertEqual(outcomes["ic-net-identity"]["status"], mod.FAIL)
            self.assertEqual(outcomes["ic-power-on-signal"]["status"], mod.FAIL)

    def test_an_unmeasured_rule_reports_no_coverage_not_a_pass(self):
        """Bu testin tamamı kapının varlık sebebi.

        Gerçek CAM-W verisinde R11/R12/R13 hiçbir kenarda ölçülmüyor
        (PROC-R'nin pin haritası boş). `violations = 0` orada "temiz" değil,
        "bakılamadı"dır — ve bağlı `coverage_selector` sayesinde assertion
        `PASS` değil `NO_COVERAGE` diyor.
        """
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        script = SCRIPTS / "assert_matrix.py"
        if not script.is_file():                         # pragma: no cover
            self.skipTest("assert_matrix.py bu checkout'ta yok")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            report = work / "report.json"
            mod.write_report(mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN),
                             report)
            out = work / "assertions.json"
            subprocess.run(
                [sys.executable, str(script), "--set", str(self.SET),
                 "--matrix", str(self.MATRIX),
                 "--evidence", f"interconnect={report}", "--json", str(out)],
                capture_output=True, text=True)
            payload = json.loads(out.read_text(encoding="utf-8"))
            outcomes = {o["assertion"]: o for o in payload["outcomes"]}
            for name in ("ic-net-identity", "ic-power-on-signal",
                         "ic-diffpair-integrity"):
                self.assertEqual(outcomes[name]["status"], mod.NO_COVERAGE,
                                 (name, outcomes[name].get("detail")))
                self.assertNotEqual(outcomes[name]["status"], mod.PASS)


class GateReceiptTest(unittest.TestCase):
    """`interconnect_gate_receipt.json` — bir `gate-receipt/v1` FRAGMENT'i.

    Kapıyı kapatan şey bu dosya DEĞİL, onu doğrulayan
    `tools/scripts/gate_receipt.py`'dir. Depoda iki çelişen gate-receipt
    sözleşmesi var; bu kapı `ecad-varlik-kalifikasyonu` ile aynı seçimi yapar ve
    **DOĞRULAYICIYI** izler — doğrulayıcının reddettiği bir makbuz hiçbir kapı
    açamaz.
    """

    def receipt(self, report: dict, exit_code: int = 0) -> dict:
        return mod.build_receipt(report, ["--test"],
                                 start_utc="2026-08-12T00:00:00Z",
                                 end_utc="2026-08-12T00:00:01Z",
                                 exit_code=exit_code)

    def test_the_receipt_carries_every_required_field(self):
        schema = json.loads((SCHEMAS / "gate_receipt.schema.json")
                            .read_text(encoding="utf-8"))
        receipt = self.receipt(run(mod._fixture_chain()))
        for field in schema["required"]:
            self.assertIn(field, receipt, field)

    def test_parsed_metrics_and_per_metric_share_one_key_set(self):
        receipt = self.receipt(run(mod._fixture_chain()))
        self.assertEqual(set(receipt["parsed_metrics"]),
                         set(receipt["coverage"]["per_metric"]))
        self.assertEqual(set(receipt["parsed_metrics"]),
                         set(receipt["metric_semantics"]))

    def test_a_parsed_metric_leaf_is_an_integer_and_per_metric_is_an_object(self):
        # Measured: writing the count into coverage.per_metric makes the real
        # validator answer INVALID_COVERAGE ("coverage counts must be an
        # object"). The two blocks share KEYS, not value shapes.
        receipt = self.receipt(run(mod._fixture_chain()))
        for name, value in receipt["parsed_metrics"].items():
            self.assertIsInstance(value, int, name)
            self.assertNotIsInstance(value, bool, name)
        for name, block in receipt["coverage"]["per_metric"].items():
            self.assertIsInstance(block, dict, name)
            self.assertEqual(mod.coverage_rule_violations(block), [], name)

    def test_an_unmeasured_finding_never_enters_the_receipt(self):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        # A null leaf is INVALID_METRIC and a 0 leaf on an unmeasured metric
        # compares equal to a threshold of 0 -> a vacuous PASS. Unmeasured
        # findings stay in the report and out of the receipt.
        report = mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN)
        unmeasured = {f["check"] for f in report["findings"]
                      if f["status"] == mod.NO_COVERAGE}
        self.assertTrue(unmeasured, "bu fixture'da ölçülmemiş bulgu yok")
        receipt = self.receipt(report, exit_code=1)
        self.assertEqual(unmeasured & set(receipt["parsed_metrics"]), set())

    def test_blockers_are_objects_and_a_green_receipt_has_none(self):
        green = self.receipt(run(mod._fixture_chain()))
        self.assertEqual(green["blockers"], [])
        red = self.receipt(run(mod._fixture_chain(medium=None)), exit_code=1)
        self.assertTrue(red["blockers"])
        for blocker in red["blockers"]:
            # A literal string (or null list) crashes gate_receipt.py:932.
            self.assertEqual(set(blocker), {"code", "message"})

    def test_a_cap_becomes_a_blocker_so_confirm_cannot_read_as_green(self):
        receipt = self.receipt(run(mod._fixture_chain(drop_revision=True)),
                               exit_code=1)
        self.assertEqual(receipt["verdict"], mod.CONFIRM)
        self.assertTrue([b for b in receipt["blockers"]
                         if b["code"].startswith("CAP_")])

    def test_the_rules_record_points_at_the_contract(self):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        # For this gate the contract IS the rule set being enforced: it defines
        # the denominator. Pointing anywhere else would be decoration.
        report = mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN)
        receipt = self.receipt(report, exit_code=1)
        self.assertTrue(receipt["rules"]["sha256"])
        self.assertTrue(Path(receipt["rules"]["path"]).is_absolute())
        self.assertEqual(Path(receipt["rules"]["path"]).name,
                         "interface_contract.json")

    def test_every_input_path_is_absolute_and_hash_bound(self):
        if not (CAM_W_CONTRACT.is_file() and CAM_W_CHAIN.is_file()):
            self.skipTest("CAM-W tohum verisi bu checkout'ta yok")
        receipt = self.receipt(mod.build_report(CAM_W_CONTRACT, CAM_W_CHAIN),
                               exit_code=1)
        self.assertTrue(receipt["inputs"])
        for item in receipt["inputs"]:
            # resolve_path resolves a relative path against the RECEIPT's
            # directory, not --root, so receipt paths are absolute.
            self.assertTrue(Path(item["path"]).is_absolute(), item)
            self.assertTrue(item["sha256"], item)

    def test_the_real_validator_accepts_a_green_receipt(self):
        """Şeklin doğru olduğunun TEK gerçek kanıtı: doğrulayıcı GREEN diyor."""
        validator = SCRIPTS / "gate_receipt.py"
        if not validator.is_file():                      # pragma: no cover
            self.skipTest("gate_receipt.py bu checkout'ta yok")
        root = REPO
        with tempfile.TemporaryDirectory(dir=str(root)) as tmp:
            work = Path(tmp)
            contract = work / "interface_contract.json"
            chain = work / "interconnect_chain.json"
            contract.write_text(json.dumps(mod._fixture_contract()),
                                encoding="utf-8")
            chain.write_text(json.dumps(mod._fixture_chain()), encoding="utf-8")
            cspec = work / "connector_spec.json"
            cspec.write_text(json.dumps(mod._fixture_connector_spec()),
                             encoding="utf-8")
            report = mod.build_report(contract, chain,
                                      connector_spec_path=cspec)
            self.assertEqual(report["status"], mod.GREEN)
            receipt = work / "receipt.json"
            mod.write_report(self.receipt(report), receipt)
            completed = subprocess.run(
                [sys.executable, str(validator), str(receipt),
                 "--root", str(root)], capture_output=True, text=True)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload.get("effective_status"), "GREEN",
                             payload.get("issues"))
            self.assertEqual(payload.get("reason_codes"), [])
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_the_cli_writes_the_receipt_where_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            contract = work / "c.json"
            chain = work / "k.json"
            contract.write_text(json.dumps(mod._fixture_contract()),
                                encoding="utf-8")
            chain.write_text(json.dumps(mod._fixture_chain()), encoding="utf-8")
            cspec = work / "connector_spec.json"
            cspec.write_text(json.dumps(mod._fixture_connector_spec()),
                             encoding="utf-8")
            out = work / "receipt.json"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--contract", str(contract),
                 "--chain", str(chain), "--connector-spec", str(cspec),
                 "--json", str(work / "r.json"),
                 "--receipt", str(out), "--now", "2026-08-12T00:00:00Z"],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(out.is_file())
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "gate-receipt/v1")
            self.assertEqual(payload["gate_kind"], "L1")
            # --now makes the run reproducible.
            self.assertEqual(payload["started_utc"], "2026-08-12T00:00:00Z")


def _with_positions(chain: dict, positions: int) -> dict:
    """Aynı pitch, FARKLI pozisyon sayısı - R4'ün FAIL tarafı."""
    chain["chains"][0]["interconnects"][0]["medium"]["conductors"] = positions
    return chain


if __name__ == "__main__":
    unittest.main()
