#!/usr/bin/env python3
"""Tests for scripts/production/cad_asset_qualification.py.

The gate under test exists because the gate BELOW it is a cardinality
check. ``cad_assets.check_pin_count`` compares two counts; two counts being
equal is not two pinouts being the same part. The central test in this file
is therefore the one that keeps the pin COUNT fixed and changes a pin's
FUNCTION: the count gate stays PASS and this gate must go RED. If that test
ever inverts, the skill has no reason to exist.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "production"))

import cad_assets as base  # noqa: E402
import cad_asset_qualification as mod  # noqa: E402

SYMBOL = mod.SAMPLE_SYMBOL
FOOTPRINT = mod.SAMPLE_FOOTPRINT
SOURCE = mod.SAMPLE_SOURCE
LIFECYCLE = mod.SAMPLE_LIFECYCLE


def run(source=None, symbol=None, footprint=None, **kwargs) -> dict:
    """A qualification over the in-module fixtures, with overrides."""
    return mod.qualify(
        source=SOURCE if source is None else source,
        source_error=kwargs.pop("source_error", None),
        symbol_text=SYMBOL if symbol is None else symbol,
        symbol_error=kwargs.pop("symbol_error", None),
        footprint_text=FOOTPRINT if footprint is None else footprint,
        footprint_error=kwargs.pop("footprint_error", None),
        lifecycle_data=kwargs.pop("lifecycle_data", LIFECYCLE),
        lifecycle_supplied=kwargs.pop("lifecycle_supplied", True),
        **kwargs)


def coverage_of(report: dict) -> dict:
    return {k: report["coverage"][k]
            for k in ("eligible", "scanned", "excluded", "violations")}


class StructuralReadingTests(unittest.TestCase):
    def test_pins_carry_name_and_electrical_role_not_just_a_number(self):
        """``cad_assets`` reads the NUMBER. The whole point here is that the
        number is the least interesting third of a pin."""
        pins, inconsistent = mod.symbol_pins(SYMBOL)
        self.assertEqual([], inconsistent)
        self.assertEqual({"number": "1", "name": "VIN",
                          "electrical_role": "power_in"}, pins["1"])
        self.assertEqual("output", pins["3"]["electrical_role"])

    def test_a_pin_repeated_across_units_is_collapsed(self):
        """The fixture defines pin 1 in two unit bodies, exactly as
        ``cad_assets`` does, and both tools must agree on the pin set."""
        pins, _ = mod.symbol_pins(SYMBOL)
        self.assertEqual(sorted(pins), base.symbol_pin_numbers(SYMBOL))

    def test_a_repeat_that_disagrees_is_an_inconsistency_not_a_duplicate(self):
        """CONSTRUCTED fault injection. Collapsing by number alone would
        silently keep whichever body was parsed first."""
        broken = ('(kicad_symbol_lib (symbol "U" '
                  '(symbol "U_1_1" (pin power_in line (name "VIN") (number "1"))) '
                  '(symbol "U_1_2" (pin output line (name "SW") (number "1")))))')
        pins, inconsistent = mod.symbol_pins(broken)
        self.assertEqual(["1"], sorted(pins))
        self.assertEqual(1, len(inconsistent))
        self.assertIn("çelişkili", inconsistent[0])

    def test_a_pin_instance_form_in_a_schematic_is_not_a_symbol_pin(self):
        """``(pin "1" (uuid ...))`` appears in a .kicad_sch. Its second
        element is a quoted string and its third is a list, so the symbol
        pin shape must not match it."""
        instance = '(symbol_instance (pin "1" (uuid "abc")) (pin "2" (uuid "def")))'
        pins, _ = mod.symbol_pins(instance)
        self.assertEqual({}, pins)

    def test_numberless_mechanical_pads_are_separated_not_dropped(self):
        pads, mechanical = mod.footprint_pads(FOOTPRINT)
        self.assertEqual(["1", "2", "3", "4", "EP"], sorted(pads))
        self.assertEqual(1, len(mechanical))
        self.assertEqual("np_thru_hole", mechanical[0]["type"])

    def test_pad_span_is_centreline_and_excludes_the_exposed_pad(self):
        pads, _ = mod.footprint_pads(FOOTPRINT)
        span_x, span_y, sampled = mod.pad_centre_span(pads, "EP")
        self.assertEqual((1.5, 1.0), (span_x, span_y))
        self.assertEqual(4, sampled)

    def test_footprint_name_comes_from_the_root_node(self):
        self.assertEqual("FixtureLib:DFN-4-1EP_1.5x1.5mm_P1mm",
                         mod.footprint_name(FOOTPRINT))


class MatchingPairTests(unittest.TestCase):
    def test_a_matching_pair_is_green(self):
        report = run()
        self.assertEqual("GREEN", report["gate"]["verdict"])
        self.assertEqual([], report["caps"])
        self.assertEqual(0, report["summary"]["FAIL"])
        self.assertEqual(0, report["summary"]["NO_COVERAGE"])

    def test_green_records_every_pin_as_matched(self):
        report = run()
        self.assertEqual({"matched"},
                         {p["status"] for p in report["pin_universe"]})
        self.assertEqual({"matched"},
                         {p["status"] for p in report["pad_universe"]})

    def test_green_is_impossible_without_an_authoritative_land_pattern(self):
        """The GREEN path is narrow ON PURPOSE. Removing the only
        authoritative ingredient must close it."""
        source = copy.deepcopy(SOURCE)
        source["land_pattern"] = {"source_type": "none"}
        self.assertNotEqual("GREEN", run(source)["gate"]["verdict"])


class SemanticVsCountTests(unittest.TestCase):
    """The reason the skill exists, in four tests."""

    def test_equal_pin_count_with_swapped_functions_is_red(self):
        """Pin 1 is VIN and pin 2 is GND in the fixture. The authoritative
        record is edited to say the opposite - same numbers, same COUNT,
        swapped functions. On a regulator this is a short across the input
        on first power-up, and it passes ERC and DRC."""
        source = copy.deepcopy(SOURCE)
        source["pins"][0]["name"], source["pins"][1]["name"] = "GND", "VIN"
        source["pins"][0]["function"] = "ground return"
        source["pins"][1]["function"] = "input supply"

        report = run(source)
        self.assertEqual(len(SOURCE["pins"]), len(source["pins"]),
                         "the fixture must keep the pin COUNT identical")
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertEqual("FAIL", report["by_check"][mod.CHECK_PIN]["status"])
        joined = " ".join(report["checks"][0]["violations"])
        self.assertIn("ad/işlev", joined)

    def test_the_count_gate_underneath_stays_pass_on_that_same_run(self):
        """NOT a defect in ``cad_assets`` - the measured statement of what a
        cardinality check can and cannot see. If this assertion ever fails,
        the count gate grew semantics and this skill's premise changed."""
        source = copy.deepcopy(SOURCE)
        source["pins"][0]["name"], source["pins"][1]["name"] = "GND", "VIN"
        report = run(source)
        self.assertEqual("PASS", report["by_check"][mod.CHECK_BASE]["status"])
        direct = base.check_pin_count(SYMBOL, len(source["pins"]), FOOTPRINT)
        self.assertEqual([], [v for v in direct["violations"]
                              if v.startswith("sembol_vs_datasheet")])

    def test_a_wrong_electrical_role_is_red(self):
        """Pin 4 is ``EN``/``input`` here and ``NC`` on the other variant.
        Name and number identical, role different."""
        source = copy.deepcopy(SOURCE)
        source["pins"][3]["electrical_role"] = "no_connect"
        report = run(source)
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertIn("elektriksel rol",
                      " ".join(report["checks"][0]["violations"]))

    def test_a_pin_the_authoritative_table_never_defines_is_red(self):
        """Both directions. Only comparing the source side would let a
        symbol carry pins the datasheet does not have."""
        source = copy.deepcopy(SOURCE)
        source["pins"] = [p for p in source["pins"] if p["number"] != "4"]
        source["pads"] = [p for p in source["pads"] if p["number"] != "4"]
        report = run(source)
        self.assertEqual("RED", report["gate"]["verdict"])
        statuses = {p["number"]: p["status"] for p in report["pin_universe"]}
        self.assertEqual("missing_in_source", statuses["4"])


class FaultInjectionTests(unittest.TestCase):
    def test_flipping_one_pin_function_turns_green_into_red(self):
        """The fault-injection the SKILL.md demands: baseline GREEN, ONE
        field changed, RED. The pin count is asserted unchanged so the
        result cannot be attributed to cardinality."""
        baseline = run()
        self.assertEqual("GREEN", baseline["gate"]["verdict"])

        injected = copy.deepcopy(SOURCE)
        injected["pins"][2]["name"] = "FB"          # was SW
        injected["pins"][2]["function"] = "feedback"
        after = run(injected)

        self.assertEqual(len(SOURCE["pins"]), len(injected["pins"]))
        self.assertEqual("RED", after["gate"]["verdict"])
        self.assertEqual(1, after["coverage"]["per_metric"][mod.CHECK_PIN]["violations"])
        self.assertEqual(baseline["coverage"]["per_metric"][mod.CHECK_PIN]["scanned"],
                         after["coverage"]["per_metric"][mod.CHECK_PIN]["scanned"],
                         "the same population was scanned; only the verdict moved")

    def test_a_cap_never_softens_a_measured_red(self):
        """RED outranks every cap. A missing provenance field cannot make a
        wrong pin function less wrong."""
        source = copy.deepcopy(SOURCE)
        source["pins"][2]["name"] = "FB"
        source["provenance"]["revision"] = ""
        source["land_pattern"] = {"source_type": "unknown"}
        report = run(source)
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertTrue(report["caps"], "the caps are still reported, just not binding")


class ExposedPadTests(unittest.TestCase):
    def test_a_footprint_missing_the_exposed_pad_is_red(self):
        """The variant substitution ``cad_assets.py`` names in its header:
        symbol from the exposed-pad part, footprint from the one without."""
        stripped = FOOTPRINT.replace(
            '  (pad "EP" smd rect (at 0 0) (size 0.8 0.8))\n', "")
        report = run(footprint=stripped)
        self.assertEqual("RED", report["gate"]["verdict"])
        joined = " ".join(report["checks"][1]["violations"])
        self.assertIn("EP", joined)
        self.assertIn("varyant", joined)

    def test_an_exposed_pad_with_no_symbol_pin_is_red(self):
        """It cannot be tied to GND, and ERC does not complain because
        there is nothing there to complain about."""
        symbol = SYMBOL.replace(
            '      (pin passive line (at 0 0 0) (name "EP") (number "EP")))', "))")
        report = run(symbol=symbol)
        self.assertEqual("RED", report["gate"]["verdict"])
        joined = " ".join(report["checks"][1]["violations"]
                          + report["checks"][0]["violations"])
        self.assertIn("EP", joined)


class PadMapTests(unittest.TestCase):
    def test_equal_counts_with_disjoint_numbering_is_red(self):
        """Five pads numbered A1..A5 against five pins numbered 1..5 is two
        counts of five and zero matches."""
        renumbered = FOOTPRINT
        for old in ("1", "2", "3", "4"):
            renumbered = renumbered.replace(f'(pad "{old}" smd', f'(pad "A{old}" smd')
        source = copy.deepcopy(SOURCE)
        report = run(source, footprint=renumbered)
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertEqual("FAIL", report["by_check"][mod.CHECK_PADMAP]["status"])

    def test_the_mechanical_pad_never_reaches_the_pad_map(self):
        report = run()
        self.assertNotIn("", {e["number"] for e in report["padmap"]})
        self.assertEqual(1, report["assets"]["footprint"]["mechanical_pad_count"])


class UpstreamCountGateArtifactTests(unittest.TestCase):
    """The numberless-pad artifact WAS real; it was fixed at the source.

    Measured 2026-08-10 while this tool was written: ``footprint_pad_numbers``
    collected every ``(pad "N" ...)`` including KiCad's mounting-hole form
    ``(pad "" np_thru_hole ...)``, so a CORRECT footprint carrying one
    mounting hole made the cardinality gate report
    ``footprint_vs_sembol: 6 pad / 5 pin; sembolde olmayan pad=['']``.

    ``cad_assets.footprint_pad_numbers`` now drops numberless pads (the
    exposed pad, which HAS a number, is still counted - see its docstring for
    why the two cases are not symmetric). These tests pin the fix, and the
    narrowing logic below stays because it is what keeps the exclusion from
    swallowing a genuine mismatch.
    """

    def test_the_numberless_pad_no_longer_reaches_the_count_gate(self):
        self.assertNotIn("", base.footprint_pad_numbers(FOOTPRINT))
        direct = base.check_pin_count(SYMBOL, 5, FOOTPRINT)
        self.assertEqual("PASS", direct["status"], direct)

    def test_this_tool_is_green_on_a_footprint_with_a_mounting_hole(self):
        report = run()
        self.assertEqual("GREEN", report["gate"]["verdict"])
        row = report["coverage"]["per_metric"][mod.CHECK_BASE]
        self.assertEqual(row["scanned"] + row["excluded"], row["eligible"])
        # Both upstream comparisons now run: nothing has to be excluded to
        # keep the mounting hole from failing a correct footprint.
        self.assertEqual(2, row["scanned"])
        self.assertEqual(0, row["excluded"])

    def test_a_genuine_cardinality_mismatch_is_still_inherited(self):
        """The exclusion must be narrow: it applies only when dropping the
        numberless pads makes the counts agree."""
        extra = FOOTPRINT.replace(
            '  (pad "" np_thru_hole circle (at 2 2) (size 1 1))\n',
            '  (pad "9" smd rect (at 3 3) (size 1 1))\n')
        report = run(footprint=extra)
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertEqual("FAIL", report["by_check"][mod.CHECK_BASE]["status"])


class PackageIdentityTests(unittest.TestCase):
    def test_a_pad_span_outside_tolerance_is_red(self):
        source = copy.deepcopy(SOURCE)
        source["land_pattern"]["pad_span_x_mm"] = 2.4   # measured 1.5
        report = run(source)
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertIn("pad_span_x_mm",
                      " ".join(report["checks"][3]["violations"]))

    def test_a_wrong_footprint_name_token_is_red(self):
        source = copy.deepcopy(SOURCE)
        source["expected_footprint_name"] = "QFN-40_5x5mm"
        report = run(source)
        self.assertEqual("RED", report["gate"]["verdict"])

    def test_an_undeclared_dimension_is_excluded_never_defaulted(self):
        source = copy.deepcopy(SOURCE)
        source["land_pattern"].pop("pad_span_y_mm")
        report = run(source)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        attribute = next(a for a in report["package"]["attributes"]
                         if a["attribute"] == "pad_span_y_mm")
        self.assertEqual("excluded", attribute["status"])
        self.assertIn("ÜRETİLMEZ", attribute["exclusion_reason"])

    def test_a_span_without_a_tolerance_is_excluded_never_defaulted(self):
        """No fallback tolerance. A comparison against an invented tolerance
        is not a measurement."""
        source = copy.deepcopy(SOURCE)
        source["land_pattern"].pop("tolerance_mm")
        report = run(source)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        excluded = [a["attribute"] for a in report["package"]["attributes"]
                    if a["status"] == "excluded"]
        self.assertEqual(["pad_span_x_mm", "pad_span_y_mm"], excluded)

    def test_a_source_with_no_package_attributes_at_all_is_confirm_not_no_coverage(self):
        source = copy.deepcopy(SOURCE)
        source.pop("expected_footprint_name")
        source["land_pattern"] = {"source_type": "manufacturer",
                                  "document_id": "X", "revision": "1"}
        report = run(source)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        self.assertIn(mod.CHECK_PACKAGE,
                      [e["check"] for e in report["excluded_checks"]])


class LandPatternAuthorityTests(unittest.TestCase):
    def test_a_missing_land_pattern_authority_is_confirm_not_green(self):
        source = copy.deepcopy(SOURCE)
        source["land_pattern"] = {"source_type": "none",
                                  "pad_span_x_mm": 1.5, "pad_span_y_mm": 1.0,
                                  "tolerance_mm": 0.05}
        report = run(source)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        self.assertFalse(report["land_pattern"]["authoritative"])

    def test_a_manufacturer_pattern_without_a_revision_is_confirm(self):
        source = copy.deepcopy(SOURCE)
        source["land_pattern"].pop("revision")
        self.assertEqual("CONFIRM", run(source)["gate"]["verdict"])

    def test_an_ipc_derived_pattern_with_its_derivation_is_local_policy(self):
        source = copy.deepcopy(SOURCE)
        source["land_pattern"]["source_type"] = "ipc_derived"
        source["land_pattern"]["derivation"] = "IPC-7351B nominal, density level M"
        report = run(source)
        self.assertEqual("LOCAL_POLICY", report["gate"]["verdict"])
        self.assertIn("imza değildir", " ".join(report["gate"]["verdict_reasons"]))

    def test_a_fallback_with_no_derivation_recorded_is_screen_only(self):
        source = copy.deepcopy(SOURCE)
        source["land_pattern"]["source_type"] = "local_library"
        report = run(source)
        self.assertEqual("SCREEN_ONLY", report["gate"]["verdict"])

    def test_neither_fallback_verdict_is_a_signoff(self):
        for source_type, derivation in (("ipc_derived", "IPC-7351B density M"),
                                        ("local_library", None)):
            with self.subTest(source_type=source_type):
                source = copy.deepcopy(SOURCE)
                source["land_pattern"]["source_type"] = source_type
                if derivation:
                    source["land_pattern"]["derivation"] = derivation
                report = run(source)
                self.assertNotEqual("GREEN", report["gate"]["verdict"])
                self.assertFalse(report["gate"]["signoff"])


class ProvenanceTests(unittest.TestCase):
    def test_any_missing_provenance_field_caps_the_run_at_confirm(self):
        for field in mod.PROVENANCE_FIELDS:
            with self.subTest(field=field):
                source = copy.deepcopy(SOURCE)
                source["provenance"].pop(field)
                report = run(source)
                self.assertEqual("CONFIRM", report["gate"]["verdict"])
                self.assertIn(field, report["source"]["provenance_missing"])

    def test_a_sourceless_record_never_produces_green(self):
        source = copy.deepcopy(SOURCE)
        source.pop("provenance")
        report = run(source)
        self.assertNotEqual("GREEN", report["gate"]["verdict"])
        self.assertFalse(report["source"]["provenance_complete"])


class NoCoverageTests(unittest.TestCase):
    def test_an_unreadable_source_is_no_coverage_with_scanned_zero(self):
        report = run(source={}, source_error="source.json: dosya yok")
        self.assertEqual("NO_COVERAGE", report["gate"]["verdict"])
        self.assertEqual(0, report["coverage"]["scanned"])
        self.assertGreater(report["coverage"]["eligible"], 0,
                           "the candidate is still eligible; it just was not scanned")

    def test_an_empty_source_table_is_no_coverage_not_a_mass_red(self):
        """Comparing 5 candidate pins against an empty table would emit five
        violations and speak with certainty about something never measured.

        The rest of the record (package attributes, lifecycle) is still
        readable here, so those rows keep scanning - the report does not
        pretend to have looked at less than it did. What must hold is that
        the pin/pad universes are scanned=0 and the aggregate verdict is
        NO_COVERAGE."""
        source = copy.deepcopy(SOURCE)
        source["pins"] = []
        source["pads"] = []
        report = run(source)
        self.assertEqual("NO_COVERAGE", report["gate"]["verdict"])
        for metric in (mod.CHECK_PIN, mod.CHECK_PAD, mod.CHECK_PADMAP,
                       mod.CHECK_BASE):
            with self.subTest(metric=metric):
                row = report["coverage"]["per_metric"][metric]
                self.assertEqual(0, row["scanned"])
                self.assertEqual(0, row["violations"])
                self.assertEqual(row["eligible"], row["excluded"])

    def test_an_unreadable_symbol_is_no_coverage(self):
        report = run(symbol="(kicad_symbol_lib)")
        self.assertEqual("NO_COVERAGE", report["gate"]["verdict"])
        self.assertEqual(0, report["coverage"]["per_metric"][mod.CHECK_PIN]["scanned"])

    def test_symbol_footprint_agreement_alone_is_not_coverage(self):
        """Without an authoritative source the pad map is computable and is
        NOT evidence: two files agreeing says nothing about either being the
        right part."""
        report = run(source={}, source_error="yok")
        row = report["coverage"]["per_metric"][mod.CHECK_PADMAP]
        self.assertEqual(0, row["scanned"])
        self.assertEqual(row["eligible"], row["excluded"])


class LifecycleTests(unittest.TestCase):
    def test_a_blocking_lifecycle_refuses_the_release(self):
        for state in ("NRND", "EOL", "Obsolete"):
            with self.subTest(state=state):
                record = copy.deepcopy(LIFECYCLE)
                record["lines"][0]["lifecycle"] = state
                report = run(lifecycle_data=record)
                self.assertEqual("RED", report["gate"]["verdict"])
                self.assertIn("İstisnasız",
                              " ".join(report["checks"][5]["violations"]))

    def test_a_missing_lifecycle_record_is_confirm_not_a_silent_active(self):
        report = run(lifecycle_data=None, lifecycle_supplied=False)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        self.assertIn(mod.CHECK_LIFECYCLE,
                      [e["check"] for e in report["excluded_checks"]])

    def test_an_mpn_absent_from_the_record_is_confirm(self):
        record = {"schema_version": "lifecycle-report/v1",
                  "lines": [{"mpn": "SOMETHING-ELSE", "lifecycle": "Active"}]}
        report = run(lifecycle_data=record)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        self.assertFalse(report["lifecycle"]["found_in_record"])

    def test_the_lifecycle_state_is_read_never_written(self):
        report = run()
        self.assertEqual("Active", report["lifecycle"]["state"])
        self.assertEqual(LIFECYCLE["lines"][0]["lifecycle"], "Active",
                         "the input record must be untouched")


class StalenessTests(unittest.TestCase):
    def test_a_changed_asset_hash_after_qualification_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _write_fixture(Path(tmp))
            first = mod.qualify(source=SOURCE, source_error=None,
                                symbol_text=SYMBOL, symbol_error=None,
                                footprint_text=FOOTPRINT, footprint_error=None,
                                lifecycle_data=LIFECYCLE, lifecycle_supplied=True,
                                **paths)
            self.assertEqual("GREEN", first["gate"]["verdict"])
            prior = json.loads(json.dumps(first))
            prior["assets"]["symbol"]["sha256"] = "0" * 64
            second = mod.qualify(source=SOURCE, source_error=None,
                                 symbol_text=SYMBOL, symbol_error=None,
                                 footprint_text=FOOTPRINT, footprint_error=None,
                                 lifecycle_data=LIFECYCLE, lifecycle_supplied=True,
                                 prior=prior, **paths)
            self.assertEqual("STALE", second["gate"]["verdict"])
            self.assertIn("symbol", " ".join(second["stale_reasons"]))

    def test_a_changed_source_file_hash_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _write_fixture(Path(tmp))
            first = mod.qualify(source=SOURCE, source_error=None,
                                symbol_text=SYMBOL, symbol_error=None,
                                footprint_text=FOOTPRINT, footprint_error=None,
                                lifecycle_data=LIFECYCLE, lifecycle_supplied=True,
                                **paths)
            prior = json.loads(json.dumps(first))
            prior["source"]["file_sha256"] = "1" * 64
            second = mod.qualify(source=SOURCE, source_error=None,
                                 symbol_text=SYMBOL, symbol_error=None,
                                 footprint_text=FOOTPRINT, footprint_error=None,
                                 lifecycle_data=LIFECYCLE, lifecycle_supplied=True,
                                 prior=prior, **paths)
            self.assertEqual("STALE", second["gate"]["verdict"])
            self.assertIn("kaynak dosyası", " ".join(second["stale_reasons"]))

    def test_a_snapshot_digest_that_does_not_match_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _write_fixture(root)
            snapshot = root / "snapshot.txt"
            snapshot.write_text("a different document\n", encoding="utf-8")
            report = mod.qualify(source=SOURCE, source_error=None,
                                 symbol_text=SYMBOL, symbol_error=None,
                                 footprint_text=FOOTPRINT, footprint_error=None,
                                 lifecycle_data=LIFECYCLE, lifecycle_supplied=True,
                                 snapshot_path=str(snapshot), **paths)
            self.assertEqual("STALE", report["gate"]["verdict"])
            self.assertFalse(report["source"]["snapshot"]["verified"])

    def test_a_matching_snapshot_verifies_and_stays_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _write_fixture(root)
            snapshot = root / "snapshot.txt"
            snapshot.write_text(mod.SAMPLE_SNAPSHOT, encoding="utf-8")
            report = mod.qualify(source=SOURCE, source_error=None,
                                 symbol_text=SYMBOL, symbol_error=None,
                                 footprint_text=FOOTPRINT, footprint_error=None,
                                 lifecycle_data=LIFECYCLE, lifecycle_supplied=True,
                                 snapshot_path=str(snapshot), **paths)
            self.assertEqual("GREEN", report["gate"]["verdict"])
            self.assertTrue(report["source"]["snapshot"]["verified"])

    def test_without_a_snapshot_the_digest_is_declared_but_unverified(self):
        report = run()
        self.assertIsNone(report["source"]["snapshot"]["verified"])
        self.assertEqual(SOURCE["provenance"]["snapshot_sha256"],
                         report["source"]["snapshot"]["declared_sha256"])


class ExceptionTests(unittest.TestCase):
    EVIDENCE = {"document_id": "APP-NOTE-7", "revision": "A",
                "locator": "Section 3.2", "accessed": "2026-08-10",
                "snapshot_sha256": "b" * 64}

    def test_an_evidenced_exception_excludes_its_item(self):
        source = copy.deepcopy(SOURCE)
        source["pins"][3]["name"] = "NOT_IN_SYMBOL"     # would be a mismatch
        source["exceptions"] = [{"scope": "pin", "item": "4",
                                 "reason": "üretici app-note farklı ad kullanıyor",
                                 "evidence": self.EVIDENCE}]
        report = run(source)
        entry = next(p for p in report["pin_universe"] if p["number"] == "4")
        self.assertEqual("excluded", entry["status"])
        self.assertEqual(1, report["coverage"]["per_metric"][mod.CHECK_PIN]["excluded"])
        self.assertNotEqual("RED", report["gate"]["verdict"])

    def test_an_exception_without_evidence_is_refused_and_the_item_stays_scanned(self):
        source = copy.deepcopy(SOURCE)
        source["pins"][3]["name"] = "NOT_IN_SYMBOL"
        source["exceptions"] = [{"scope": "pin", "item": "4",
                                 "reason": "çünkü öyle", "evidence": {}}]
        report = run(source)
        entry = next(p for p in report["pin_universe"] if p["number"] == "4")
        self.assertEqual("mismatch", entry["status"])
        self.assertEqual("RED", report["gate"]["verdict"])
        self.assertEqual(1, len(report["refused_exceptions"]))

    def test_an_exception_without_a_reason_is_refused(self):
        source = copy.deepcopy(SOURCE)
        source["exceptions"] = [{"scope": "pin", "item": "4", "reason": "",
                                 "evidence": self.EVIDENCE}]
        report = run(source)
        self.assertEqual(1, len(report["refused_exceptions"]))
        self.assertIn("reason boş", report["refused_exceptions"][0]["refusal_reason"])

    def test_a_refusal_makes_the_report_louder_not_quieter(self):
        source = copy.deepcopy(SOURCE)
        source["exceptions"] = [{"scope": "pin", "item": "4",
                                 "reason": "gerekçe var", "evidence": {}}]
        report = run(source)
        self.assertEqual("CONFIRM", report["gate"]["verdict"])
        self.assertIn("REDDEDİLDİ", " ".join(c["reason"] for c in report["caps"]))


class CoverageArithmeticTests(unittest.TestCase):
    """``scanned + excluded == eligible`` in EVERY emitted report."""

    def _reports(self):
        red = copy.deepcopy(SOURCE)
        red["pins"][0]["name"] = "WRONG"
        no_land = copy.deepcopy(SOURCE)
        no_land["land_pattern"] = {"source_type": "none"}
        no_prov = copy.deepcopy(SOURCE)
        no_prov.pop("provenance")
        empty = copy.deepcopy(SOURCE)
        empty["pins"] = []
        empty["pads"] = []
        stripped = FOOTPRINT.replace(
            '  (pad "EP" smd rect (at 0 0) (size 0.8 0.8))\n', "")
        yield "green", run()
        yield "red", run(red)
        yield "confirm_land", run(no_land)
        yield "confirm_provenance", run(no_prov)
        yield "no_coverage_empty_table", run(empty)
        yield "no_coverage_source", run(source={}, source_error="yok")
        yield "no_coverage_symbol", run(symbol="(kicad_symbol_lib)")
        yield "red_missing_epad", run(footprint=stripped)
        yield "no_lifecycle", run(lifecycle_data=None, lifecycle_supplied=False)

    def test_totals_balance_in_every_report(self):
        for name, report in self._reports():
            with self.subTest(report=name):
                cov = report["coverage"]
                self.assertEqual(cov["eligible"], cov["scanned"] + cov["excluded"])
                self.assertLessEqual(cov["violations"], cov["scanned"])

    def test_every_per_metric_row_balances(self):
        for name, report in self._reports():
            for metric, row in report["coverage"]["per_metric"].items():
                with self.subTest(report=name, metric=metric):
                    self.assertEqual(row["eligible"], row["scanned"] + row["excluded"])
                    self.assertLessEqual(row["scanned"], row["eligible"])
                    self.assertLessEqual(row["violations"], row["scanned"])

    def test_the_shared_rule_checker_agrees(self):
        from common.findings import coverage_rule_violations
        for name, report in self._reports():
            with self.subTest(report=name):
                self.assertEqual([], coverage_rule_violations(coverage_of(report)))

    def test_scanned_zero_is_never_green(self):
        for name, report in self._reports():
            with self.subTest(report=name):
                if report["coverage"]["scanned"] == 0:
                    self.assertEqual("NO_COVERAGE", report["gate"]["verdict"])

    def test_a_broken_coverage_block_is_invalid_coverage(self):
        """The arithmetic is derived, so it cannot break from inside. The
        verdict function is still exercised against a broken block, because
        a rule with no failing case is a rule nobody has tested."""
        verdict, reasons = mod.final_verdict(
            [], [], [], [], {"eligible": 3, "scanned": 3, "excluded": 3,
                             "violations": 0})
        self.assertEqual("INVALID_COVERAGE", verdict)
        self.assertIn("PARTS_DO_NOT_SUM", reasons[0])


class VerdictVocabularyTests(unittest.TestCase):
    def test_no_verdict_outside_the_shared_dictionary_is_produced(self):
        allowed = {"GREEN", "RED", "CONFIRM", "SCREEN_ONLY", "LOCAL_POLICY",
                   "SIM_ONLY", "NO_COVERAGE", "INVALID_METRIC",
                   "INVALID_COVERAGE", "INVALID_SPARAM", "INCONCLUSIVE",
                   "STALE", "GEOMETRY_PRECHECK_PASS", "TBD", "API_YOK"}
        seen = set()
        for source_type in ("manufacturer", "ipc_derived", "local_library",
                            "none", "unknown"):
            source = copy.deepcopy(SOURCE)
            source["land_pattern"]["source_type"] = source_type
            seen.add(run(source)["gate"]["verdict"])
        seen.add(run(source={}, source_error="yok")["gate"]["verdict"])
        self.assertTrue(seen <= allowed, seen - allowed)

    def test_the_verdict_enum_matches_the_output_schema(self):
        schema = json.loads((SCHEMAS / "cad_asset_qualification.schema.json")
                            .read_text(encoding="utf-8"))
        enum = set(schema["$defs"]["gate"]["properties"]["verdict"]["enum"])
        receipt = json.loads(
            (SCHEMAS.parent.parent / "skills" / "pcb-orchestrator" / "references"
             / "gate_receipt.schema.json").read_text(encoding="utf-8"))
        self.assertTrue(enum <= set(receipt["properties"]["verdict"]["enum"]),
                        "a verdict the gate receipt cannot carry is unusable")


class ReceiptTests(unittest.TestCase):
    def test_parsed_metrics_and_per_metric_carry_the_same_keys(self):
        report = run()
        receipt = mod.build_receipt(
            report, ["cad_asset_qualification.py"],
            start_utc="2026-08-10T00:00:00Z", end_utc="2026-08-10T00:00:01Z",
            exit_code=0, inputs=[("symbol", {"path": "a", "sha256": "0" * 64})])
        self.assertEqual(set(receipt["parsed_metrics"]),
                         set(receipt["coverage"]["per_metric"]))
        self.assertEqual(set(receipt["parsed_metrics"]),
                         set(receipt["metric_semantics"]))

    def test_every_metric_leaf_is_a_finite_integer(self):
        """A null leaf would be an INVALID_METRIC under gate-receipt/v1, and
        a 0 leaf on an unmeasured metric would compare equal to a threshold
        of 0 and report a vacuous PASS."""
        report = run(source={}, source_error="yok")
        receipt = mod.build_receipt(
            report, ["x"], start_utc="2026-08-10T00:00:00Z",
            end_utc="2026-08-10T00:00:01Z", exit_code=1,
            inputs=[("symbol", {"path": "a", "sha256": "0" * 64})])
        for name, value in receipt["parsed_metrics"].items():
            with self.subTest(metric=name):
                self.assertIsInstance(value, int)
                self.assertNotIsInstance(value, bool)

    def test_a_receipt_always_has_at_least_one_input(self):
        report = run()
        receipt = mod.build_receipt(
            report, ["x"], start_utc="2026-08-10T00:00:00Z",
            end_utc="2026-08-10T00:00:01Z", exit_code=0, inputs=[])
        self.assertEqual(1, len(receipt["inputs"]))
        self.assertEqual("gate-receipt/v1", receipt["schema_version"])
        self.assertEqual("L1", receipt["gate_kind"])

    def test_semantics_carry_the_fields_the_VALIDATOR_requires(self):
        """The two gate-receipt contracts disagree on the field names (see
        ``build_receipt``); the executable one wins, and this pins it."""
        import gate_receipt as gr
        report = run()
        receipt = mod.build_receipt(
            report, ["x"], start_utc="2026-08-10T00:00:00Z",
            end_utc="2026-08-10T00:00:01Z", exit_code=0,
            inputs=[("symbol", {"path": "a", "sha256": "0" * 64})])
        for name, spec in receipt["metric_semantics"].items():
            with self.subTest(metric=name):
                for field in gr.SEMANTIC_FIELDS:
                    self.assertTrue(str(spec.get(field) or "").strip(),
                                    f"{name}.{field} is required by gate_receipt.py")

    def test_blockers_are_objects_and_a_green_receipt_has_none(self):
        """``validate_blockers`` requires ``{code, message}`` and turns a
        GREEN receipt with any open blocker RED."""
        green = mod.build_receipt(
            run(), ["x"], start_utc="2026-08-10T00:00:00Z",
            end_utc="2026-08-10T00:00:01Z", exit_code=0,
            inputs=[("symbol", {"path": "a", "sha256": "0" * 64})])
        self.assertEqual([], green["blockers"])

        source = copy.deepcopy(SOURCE)
        source["pins"][0]["name"] = "WRONG"
        red = mod.build_receipt(
            run(source), ["x"], start_utc="2026-08-10T00:00:00Z",
            end_utc="2026-08-10T00:00:01Z", exit_code=1,
            inputs=[("symbol", {"path": "a", "sha256": "0" * 64})])
        self.assertTrue(red["blockers"])
        for blocker in red["blockers"]:
            self.assertEqual({"code", "message"}, set(blocker))

    def test_a_zero_scanned_receipt_states_a_reason(self):
        receipt = mod.build_receipt(
            run(source={}, source_error="yok"), ["x"],
            start_utc="2026-08-10T00:00:00Z", end_utc="2026-08-10T00:00:01Z",
            exit_code=1, inputs=[("symbol", {"path": "a", "sha256": "0" * 64})])
        self.assertEqual(0, receipt["coverage"]["scanned"])
        self.assertTrue(receipt["coverage"]["reason"].strip())


class ReceiptValidatorTests(unittest.TestCase):
    """The receipt is run through ``gate_receipt.py`` itself.

    Asserting the shape by hand is how a receipt ends up structurally
    plausible and rejected in practice - which is exactly what happened on
    the first draft of this tool (null blockers crashed the validator,
    metric_semantics used the schema's field names instead of the
    validator's, and there was no config/rules identity at all)."""

    def _validate(self, receipt_path: Path) -> dict:
        import gate_receipt as gr
        resolved = receipt_path.resolve()
        ctx = gr.Context(resolved, Path(resolved.anchor).resolve(),
                         seen={resolved})
        return gr.validate_receipt(
            json.loads(resolved.read_text(encoding="utf-8")), ctx)

    def _emit(self, root: Path, source_override=None):
        files = _write_fixture(root)
        if source_override is not None:
            Path(files["source_path"]).write_text(
                json.dumps(source_override), encoding="utf-8")
        lifecycle = root / "lifecycle_report.json"
        lifecycle.write_text(json.dumps(LIFECYCLE), encoding="utf-8")
        receipt = root / "cad_asset_gate_receipt.json"
        code = mod.main(["--source", files["source_path"],
                         "--symbol", files["symbol_path"],
                         "--footprint", files["footprint_path"],
                         "--lifecycle", str(lifecycle),
                         "--receipt", str(receipt),
                         "--now", "2026-08-10T00:00:00Z"])
        return code, receipt

    def test_a_green_receipt_validates_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, receipt = self._emit(Path(tmp))
            self.assertEqual(0, code)
            report = self._validate(receipt)
            self.assertEqual("GREEN", report["effective_status"],
                             json.dumps(report.get("issues"), ensure_ascii=False))

    def test_a_red_receipt_does_not_validate_green(self):
        source = copy.deepcopy(SOURCE)
        source["pins"][0]["name"] = "WRONG"
        with tempfile.TemporaryDirectory() as tmp:
            code, receipt = self._emit(Path(tmp), source)
            self.assertEqual(1, code)
            report = self._validate(receipt)
            self.assertNotEqual("GREEN", report["effective_status"])
            self.assertEqual([], [i for i in report["issues"]
                                  if i["code"] in ("INPUT_ERROR",
                                                   "INVALID_HASH_RECORD",
                                                   "MISSING_CONFIG_HASH",
                                                   "INCOMPLETE_METRIC_SEMANTICS")],
                             "RED must come from the verdict, not a malformed receipt")


def _write_fixture(root: Path) -> dict:
    symbol = root / "part.kicad_sym"
    symbol.write_text(SYMBOL, encoding="utf-8")
    footprint = root / "part.kicad_mod"
    footprint.write_text(FOOTPRINT, encoding="utf-8")
    source = root / "pin_package_source.json"
    source.write_text(json.dumps(SOURCE), encoding="utf-8")
    return {"symbol_path": str(symbol), "footprint_path": str(footprint),
            "source_path": str(source)}


class CliTests(unittest.TestCase):
    def paths(self, root: Path):
        files = _write_fixture(root)
        lifecycle = root / "lifecycle_report.json"
        lifecycle.write_text(json.dumps(LIFECYCLE), encoding="utf-8")
        return files, str(lifecycle)

    def test_self_test_passes(self):
        self.assertEqual(0, mod._self_test())

    def test_a_green_run_exits_zero_and_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, lifecycle = self.paths(root)
            out = root / "cad_asset_qualification.json"
            receipt = root / "cad_asset_gate_receipt.json"
            code = mod.main(["--source", files["source_path"],
                             "--symbol", files["symbol_path"],
                             "--footprint", files["footprint_path"],
                             "--lifecycle", lifecycle,
                             "--json", str(out), "--receipt", str(receipt),
                             "--now", "2026-08-10T00:00:00Z"])
            self.assertEqual(0, code)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("GREEN", report["gate"]["verdict"])
            self.assertEqual("cad-asset-qualification-report/v1",
                             report["schema_version"])
            self.assertEqual("GREEN",
                             json.loads(receipt.read_text(encoding="utf-8"))["verdict"])

    def test_a_red_run_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, lifecycle = self.paths(root)
            broken = copy.deepcopy(SOURCE)
            broken["pins"][0]["name"] = "WRONG"
            Path(files["source_path"]).write_text(json.dumps(broken), encoding="utf-8")
            code = mod.main(["--source", files["source_path"],
                             "--symbol", files["symbol_path"],
                             "--footprint", files["footprint_path"],
                             "--lifecycle", lifecycle])
            self.assertEqual(1, code)

    def test_confirm_also_exits_one_because_this_is_a_release_gate(self):
        """Deliberately unlike ``bom_lifecycle``, which exits 0 on CONFIRM.
        An exit code a caller reads as success would open the gate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, _ = self.paths(root)
            code = mod.main(["--source", files["source_path"],
                             "--symbol", files["symbol_path"],
                             "--footprint", files["footprint_path"]])
            self.assertEqual(1, code)

    def test_a_missing_source_file_reports_no_coverage_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, lifecycle = self.paths(root)
            out = root / "report.json"
            code = mod.main(["--source", str(root / "does_not_exist.json"),
                             "--symbol", files["symbol_path"],
                             "--footprint", files["footprint_path"],
                             "--lifecycle", lifecycle, "--json", str(out)])
            self.assertEqual(1, code)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual("NO_COVERAGE", report["gate"]["verdict"])
            self.assertEqual(0, report["coverage"]["scanned"])

    def test_the_three_inputs_have_no_defaults(self):
        """A default source would compare the asset against itself."""
        with tempfile.TemporaryDirectory() as tmp:
            files, _ = self.paths(Path(tmp))
            with self.assertRaises(SystemExit):
                mod.main(["--symbol", files["symbol_path"],
                          "--footprint", files["footprint_path"]])

    def test_registration_is_refused_unless_the_verdict_is_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, lifecycle = self.paths(root)
            broken = copy.deepcopy(SOURCE)
            broken["pins"][0]["name"] = "WRONG"
            Path(files["source_path"]).write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(PermissionError):
                mod.main(["--source", files["source_path"],
                          "--symbol", files["symbol_path"],
                          "--footprint", files["footprint_path"],
                          "--lifecycle", lifecycle,
                          "--register", "--project", str(root / "proj")])

    def test_registration_on_green_goes_through_the_tool_not_by_hand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files, lifecycle = self.paths(root)
            project = root / "proj"
            code = mod.main(["--source", files["source_path"],
                             "--symbol", files["symbol_path"],
                             "--footprint", files["footprint_path"],
                             "--lifecycle", lifecycle,
                             "--register", "--project", str(project),
                             "--lib-name", "qualified"])
            self.assertEqual(0, code)
            self.assertTrue((project / "sym-lib-table").exists())
            self.assertTrue((project / "fp-lib-table").exists())

    def test_the_tool_makes_no_network_call(self):
        source = (SCRIPTS / "production" / "cad_asset_qualification.py") \
            .read_text(encoding="utf-8")
        for banned in ("import urllib.request", "import requests", "urlopen(",
                       "httpx", "import socket"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_the_tool_reads_kicad_files_structurally_not_with_regex(self):
        """``cad_assets`` explains why. A second parser that regexes the raw
        text would reintroduce the bug the first one removed."""
        source = (SCRIPTS / "production" / "cad_asset_qualification.py") \
            .read_text(encoding="utf-8")
        self.assertIn("from common.sexpr import", source)
        self.assertNotIn("import re", source)


if __name__ == "__main__":
    unittest.main()
