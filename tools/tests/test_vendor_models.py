"""Tests for common/vendormodel.py and scripts/sim/model_registry.py.

WHAT THESE PROVE
--------------------------------------------------------------------------
The registry exists to refuse. So every test here puts it in front of
something a permissive registry would wave through:

  * a model with no declared range (every reader in this bundle would
    extrapolate off the end of its data without raising);
  * a query outside the declared range (must yield NO NUMBER, not a clamped
    one, and not a FAIL either - the design is not wrong, the model does not
    reach);
  * a declaration that overclaims its own file (the one check nothing else in
    the bundle can make);
  * a file whose bytes moved under a registered id (keeps the authority of
    the model it replaced);
  * a derived or synthetic model asked to close a sign-off gate.

The Touchstone tests need ``skrf``. Where it is absent they skip rather than
pass, because a cross-check that did not run is not a cross-check that
succeeded - which is the same distinction the tool itself draws with
``ENVELOPE_UNCHECKED``.
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

from common.findings import FAIL, NO_COVERAGE, PASS  # noqa: E402
from common.hashing import sha256_file  # noqa: E402
from common.vendormodel import (AUTHORITY_INSUFFICIENT,  # noqa: E402
                                AXIS_UNDECLARED, DIGEST_UNVERIFIED,
                                ENVELOPE_OVERCLAIMS, ENVELOPE_UNCHECKED,
                                GRANT_SCREEN_ONLY, GRANT_SIGNOFF, GREEN,
                                MODEL_ABSENT, OUT_OF_ENVELOPE, RED,
                                SCREEN_ONLY, STALE_MODEL, SUPERSEDED,
                                UNKNOWN_MODEL, ModelSyntaxError, Query,
                                answer, check_envelope, check_integrity,
                                grant_summary, load_model, load_query,
                                load_registry, rollup)
from scripts.sim.model_registry import main, read_registry  # noqa: E402

SCRIPT = TOOLS / "scripts" / "sim" / "model_registry.py"
FIXTURE = TOOLS / "fixtures" / "vendor_models" / "registry_example.json"

BASE = {
    "id": "part-s2p",
    "kind": "touchstone",
    "path": "models/part.s2p",
    "authority": "vendor_published",
    "provenance": {"vendor": "ExampleCo", "document": "DOC-1",
                   "revision": "C", "accessed": "2026-08-08"},
    "valid_range": [{"quantity": "frequency", "unit": "GHz",
                     "min": 0.01, "max": 10.0}],
}


def build(**overrides):
    entry = {k: v for k, v in {**BASE, **overrides}.items() if v is not None}
    return load_model(entry)


def has_skrf() -> bool:
    try:
        import skrf  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Load-time refusals
# --------------------------------------------------------------------------- #

class LoadRefusalTest(unittest.TestCase):
    def assertRefuses(self, code, **overrides):
        with self.assertRaises(ModelSyntaxError) as caught:
            build(**overrides)
        self.assertEqual(caught.exception.code, code)

    def test_a_model_with_no_declared_range_does_not_load(self):
        self.assertRefuses("ENVELOPE_UNDECLARED", valid_range=None)

    def test_an_empty_range_array_does_not_load(self):
        self.assertRefuses("ENVELOPE_UNDECLARED", valid_range=[])

    def test_an_axis_without_a_unit_does_not_load(self):
        # 0..10 is not a range until somebody says Hz, GHz or degrees.
        self.assertRefuses("UNIT_UNDECLARED",
                           valid_range=[{"quantity": "frequency",
                                         "min": 0.0, "max": 10.0}])

    def test_an_inverted_range_does_not_load(self):
        self.assertRefuses("BAD_ENVELOPE",
                           valid_range=[{"quantity": "frequency",
                                         "unit": "GHz", "min": 10.0,
                                         "max": 0.01}])

    def test_a_duplicated_axis_does_not_load(self):
        # Which copy bounds a query would depend on iteration order.
        self.assertRefuses("BAD_ENVELOPE", valid_range=[
            {"quantity": "frequency", "unit": "GHz", "min": 0.0, "max": 10.0},
            {"quantity": "frequency", "unit": "GHz", "min": 0.0, "max": 20.0}])

    def test_a_non_numeric_bound_does_not_load(self):
        self.assertRefuses("BAD_ENVELOPE",
                           valid_range=[{"quantity": "frequency",
                                         "unit": "GHz", "min": "DC",
                                         "max": 10.0}])

    def test_an_infinite_bound_does_not_load(self):
        self.assertRefuses("BAD_ENVELOPE",
                           valid_range=[{"quantity": "frequency",
                                         "unit": "GHz", "min": 0.0,
                                         "max": float("inf")}])

    def test_an_unlabelled_authority_does_not_load(self):
        # No default in either direction: demoting silently hides the
        # demotion, promoting silently is obviously wrong.
        self.assertRefuses("AUTHORITY_UNDECLARED", authority=None)

    def test_an_invented_authority_tier_does_not_load(self):
        self.assertRefuses("AUTHORITY_UNDECLARED", authority="probably_fine")

    def test_provenance_without_a_revision_does_not_load(self):
        self.assertRefuses("PROVENANCE_INCOMPLETE",
                           provenance={"vendor": "ExampleCo",
                                       "document": "DOC-1",
                                       "accessed": "2026-08-08"})

    def test_provenance_without_an_access_date_does_not_load(self):
        # A vendor document without a date names a family of documents.
        self.assertRefuses("PROVENANCE_INCOMPLETE",
                           provenance={"vendor": "ExampleCo",
                                       "document": "DOC-1", "revision": "C"})

    def test_a_blank_provenance_field_is_not_a_filled_one(self):
        self.assertRefuses("PROVENANCE_INCOMPLETE",
                           provenance={"vendor": "ExampleCo",
                                       "document": "   ", "revision": "C",
                                       "accessed": "2026-08-08"})

    def test_an_unknown_kind_does_not_load(self):
        self.assertRefuses("BAD_KIND", kind="spreadsheet")

    def test_a_short_digest_does_not_load(self):
        self.assertRefuses("BAD_DIGEST", sha256="abc123")

    def test_extra_provenance_keys_are_kept_not_dropped(self):
        model = build(provenance={**BASE["provenance"],
                                  "url": "https://example.invalid/doc"})
        self.assertEqual(model.provenance["url"],
                         "https://example.invalid/doc")


class RegistryLoadTest(unittest.TestCase):
    def test_one_bad_entry_does_not_hide_the_others(self):
        broken = {k: v for k, v in BASE.items() if k != "authority"}
        broken["id"] = "broken"
        registry = load_registry([BASE, broken])
        self.assertEqual(list(registry.models), ["part-s2p"])
        self.assertEqual(registry.errors[0]["issue"], "AUTHORITY_UNDECLARED")

    def test_a_duplicate_id_is_reported_rather_than_silently_dropped(self):
        registry = load_registry([BASE, dict(BASE)])
        self.assertEqual(len(registry.models), 1)
        self.assertEqual(registry.errors[0]["issue"], "DUPLICATE_ID")


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #

class EnvelopeQueryTest(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry([BASE])

    def test_inside_the_envelope_clears(self):
        out = answer(Query("part-s2p", "signoff", {"frequency": 5.0}),
                     self.registry)
        self.assertEqual(out.status, PASS)
        self.assertEqual(out.grant, GRANT_SIGNOFF)

    def test_exactly_on_the_upper_bound_clears(self):
        # The bound is inclusive; a model characterised TO 10 GHz was
        # characterised AT 10 GHz.
        out = answer(Query("part-s2p", "signoff", {"frequency": 10.0}),
                     self.registry)
        self.assertEqual(out.status, PASS)

    def test_above_the_envelope_returns_no_number(self):
        out = answer(Query("part-s2p", "signoff", {"frequency": 12.0}),
                     self.registry)
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, OUT_OF_ENVELOPE)

    def test_above_the_envelope_is_not_a_failure_of_the_design(self):
        # The distinction the whole tool turns on: the board is not wrong,
        # the model does not reach.
        out = answer(Query("part-s2p", "signoff", {"frequency": 12.0}),
                     self.registry)
        self.assertNotEqual(out.status, FAIL)

    def test_below_the_envelope_is_also_refused(self):
        # DC is the direction people forget. An s2p starting at 10 MHz says
        # nothing about a rail.
        out = answer(Query("part-s2p", "screening", {"frequency": 0.0}),
                     self.registry)
        self.assertEqual(out.reason, OUT_OF_ENVELOPE)

    def test_an_undeclared_axis_is_not_an_implicit_pass(self):
        out = answer(Query("part-s2p", "screening", {"temperature": 85.0}),
                     self.registry)
        self.assertEqual(out.status, NO_COVERAGE)
        self.assertEqual(out.reason, AXIS_UNDECLARED)

    def test_every_axis_of_a_multi_axis_query_must_hold(self):
        two_axis = load_registry([dict(BASE, valid_range=[
            {"quantity": "frequency", "unit": "GHz", "min": 0.01, "max": 10.0},
            {"quantity": "temperature", "unit": "degC", "min": -40, "max": 85}])])
        good = answer(Query("part-s2p", "screening",
                            {"frequency": 5.0, "temperature": 25.0}), two_axis)
        self.assertEqual(good.status, PASS)
        hot = answer(Query("part-s2p", "screening",
                           {"frequency": 5.0, "temperature": 125.0}), two_axis)
        self.assertEqual(hot.reason, OUT_OF_ENVELOPE)

    def test_a_query_with_no_axes_still_checks_the_other_gates(self):
        out = answer(Query("part-s2p", "signoff", {}), self.registry)
        self.assertEqual(out.status, PASS)

    def test_an_unregistered_model_is_a_failure(self):
        out = answer(Query("nobody", "screening", {}), self.registry)
        self.assertEqual(out.status, FAIL)
        self.assertEqual(out.reason, UNKNOWN_MODEL)


# --------------------------------------------------------------------------- #
# Authority
# --------------------------------------------------------------------------- #

class AuthorityTest(unittest.TestCase):
    def test_a_published_model_may_sign_off(self):
        self.assertTrue(build().may_signoff())

    def test_a_derived_model_may_not(self):
        self.assertFalse(build(authority="vendor_derived").may_signoff())

    def test_a_synthetic_model_may_not(self):
        self.assertFalse(build(authority="synthetic").may_signoff())

    def test_a_derived_model_still_clears_screening(self):
        registry = load_registry([dict(BASE, authority="vendor_derived")])
        out = answer(Query("part-s2p", "screening", {"frequency": 5.0}),
                     registry)
        self.assertEqual(out.status, PASS)
        self.assertEqual(out.grant, GRANT_SCREEN_ONLY)

    def test_asking_a_derived_model_to_sign_off_is_a_refusal(self):
        registry = load_registry([dict(BASE, authority="vendor_derived")])
        out = answer(Query("part-s2p", "signoff", {"frequency": 5.0}),
                     registry)
        self.assertEqual(out.status, FAIL)
        self.assertEqual(out.reason, AUTHORITY_INSUFFICIENT)

    def test_authority_is_checked_after_the_envelope(self):
        # Order matters: an out-of-range question about a weak model is a
        # coverage gap, and reporting it as an authority failure would send
        # somebody hunting for a better model that also would not reach.
        registry = load_registry([dict(BASE, authority="synthetic")])
        out = answer(Query("part-s2p", "signoff", {"frequency": 99.0}),
                     registry)
        self.assertEqual(out.reason, OUT_OF_ENVELOPE)

    def test_grants_are_counted_separately_from_passes(self):
        registry = load_registry([BASE, dict(BASE, id="fit",
                                             authority="vendor_derived")])
        answers = [answer(Query("part-s2p", "signoff", {"frequency": 1.0}),
                          registry),
                   answer(Query("fit", "screening", {"frequency": 1.0}),
                          registry)]
        self.assertEqual(grant_summary(answers),
                         {GRANT_SIGNOFF: 1, GRANT_SCREEN_ONLY: 1})


# --------------------------------------------------------------------------- #
# Supersession
# --------------------------------------------------------------------------- #

class SupersessionTest(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry([
            BASE, dict(BASE, id="part-s2p-revD", supersedes=["part-s2p"])])

    def test_a_superseded_model_is_refused_not_annotated(self):
        # It still produces numbers, which is exactly why this has to be a
        # refusal rather than a note somebody scrolls past.
        out = answer(Query("part-s2p", "screening", {"frequency": 5.0}),
                     self.registry)
        self.assertEqual(out.status, FAIL)
        self.assertEqual(out.reason, SUPERSEDED)

    def test_the_replacement_itself_clears(self):
        out = answer(Query("part-s2p-revD", "signoff", {"frequency": 5.0}),
                     self.registry)
        self.assertEqual(out.status, PASS)

    def test_the_refusal_names_the_replacement(self):
        out = answer(Query("part-s2p", "screening", {}), self.registry)
        self.assertIn("part-s2p-revD", out.detail)

    def test_a_supersedes_string_is_accepted_as_a_single_entry(self):
        model = build(id="x", supersedes="part-s2p")
        self.assertEqual(model.supersedes, ("part-s2p",))


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #

class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root,
                                                            ignore_errors=True))
        self.target = self.root / "models" / "part.s2p"
        self.target.parent.mkdir(parents=True)
        self.target.write_text("! placeholder\n", encoding="utf-8")

    def test_a_matching_digest_passes(self):
        model = build(sha256=sha256_file(self.target))
        self.assertEqual(check_integrity(model, self.root).status, PASS)

    def test_a_moved_digest_is_a_failure(self):
        model = build(sha256=sha256_file(self.target))
        self.target.write_text("! edited\n", encoding="utf-8")
        state = check_integrity(model, self.root)
        self.assertEqual(state.status, FAIL)
        self.assertEqual(state.reason, STALE_MODEL)

    def test_a_missing_file_is_a_failure(self):
        self.target.unlink()
        state = check_integrity(build(), self.root)
        self.assertEqual(state.status, FAIL)
        self.assertEqual(state.reason, MODEL_ABSENT)

    def test_no_registered_digest_is_unverified_not_verified(self):
        state = check_integrity(build(), self.root)
        self.assertEqual(state.status, NO_COVERAGE)
        self.assertEqual(state.reason, DIGEST_UNVERIFIED)

    def test_the_unverified_report_prints_the_digest_to_register(self):
        state = check_integrity(build(), self.root)
        self.assertEqual(state.digest_on_disk, sha256_file(self.target))

    def test_a_stale_model_revokes_the_query_even_inside_the_envelope(self):
        model = build(sha256=sha256_file(self.target))
        self.target.write_text("! edited\n", encoding="utf-8")
        registry = load_registry([dict(BASE, sha256=model.digest)])
        integrity = {"part-s2p": check_integrity(registry.get("part-s2p"),
                                                 self.root)}
        out = answer(Query("part-s2p", "screening", {"frequency": 5.0}),
                     registry, integrity)
        self.assertEqual(out.status, FAIL)
        self.assertEqual(out.reason, STALE_MODEL)


# --------------------------------------------------------------------------- #
# Envelope cross-check against the file itself
# --------------------------------------------------------------------------- #

class EnvelopeCrossCheckTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root,
                                                            ignore_errors=True))

    def write_s2p(self, points_ghz):
        path = self.root / "models" / "part.s2p"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# GHz S RI R 50"]
        for ghz in points_ghz:
            lines.append(f"{ghz} 0.0 0.0 0.9 0.0 0.9 0.0 0.0 0.0")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_a_declaration_inside_the_file_passes(self):
        if not has_skrf():
            self.skipTest("skrf is not installed; the cross-check cannot run")
        self.write_s2p([0.1, 1.0, 5.0, 10.0])
        model = build(valid_range=[{"quantity": "frequency", "unit": "GHz",
                                    "min": 0.1, "max": 10.0}])
        checks = check_envelope(model, self.root)
        self.assertEqual(checks[0].status, PASS)

    def test_a_declaration_that_overclaims_its_own_file_fails(self):
        # The one check nothing else in this bundle can make: the model was
        # characterised to 10 GHz and somebody wrote 20 because that is what
        # the channel needs.
        if not has_skrf():
            self.skipTest("skrf is not installed; the cross-check cannot run")
        self.write_s2p([0.1, 1.0, 5.0, 10.0])
        model = build(valid_range=[{"quantity": "frequency", "unit": "GHz",
                                    "min": 0.1, "max": 20.0}])
        checks = check_envelope(model, self.root)
        self.assertEqual(checks[0].status, FAIL)
        self.assertEqual(checks[0].reason, ENVELOPE_OVERCLAIMS)

    def test_overclaiming_the_low_end_also_fails(self):
        if not has_skrf():
            self.skipTest("skrf is not installed; the cross-check cannot run")
        self.write_s2p([1.0, 5.0, 10.0])
        model = build(valid_range=[{"quantity": "frequency", "unit": "GHz",
                                    "min": 0.0, "max": 10.0}])
        self.assertEqual(check_envelope(model, self.root)[0].reason,
                         ENVELOPE_OVERCLAIMS)

    def test_the_failure_reports_both_ranges_in_hz(self):
        if not has_skrf():
            self.skipTest("skrf is not installed; the cross-check cannot run")
        self.write_s2p([0.1, 10.0])
        model = build(valid_range=[{"quantity": "frequency", "unit": "GHz",
                                    "min": 0.1, "max": 20.0}])
        check = check_envelope(model, self.root)[0]
        self.assertAlmostEqual(check.declared[1], 20e9)
        self.assertAlmostEqual(check.measured[1], 10e9, delta=1.0)

    def test_a_non_touchstone_model_is_unchecked_not_passed(self):
        # An unverifiable claim reported as PASS is how a registry becomes
        # decoration.
        model = build(kind="curve", path="models/psrr.json")
        check = check_envelope(model, self.root)[0]
        self.assertEqual(check.status, NO_COVERAGE)
        self.assertEqual(check.reason, ENVELOPE_UNCHECKED)

    def test_a_missing_file_is_reported_as_such(self):
        check = check_envelope(build(), self.root)[0]
        self.assertEqual(check.reason, MODEL_ABSENT)

    def test_an_unconvertible_unit_is_unchecked_rather_than_guessed(self):
        # Guessing the factor is how a 1000x error becomes invisible.
        self.write_s2p([0.1, 10.0])
        model = build(valid_range=[{"quantity": "frequency", "unit": "rad/s",
                                    "min": 0.1, "max": 10.0}])
        check = check_envelope(model, self.root)[0]
        self.assertEqual(check.status, NO_COVERAGE)
        self.assertEqual(check.reason, ENVELOPE_UNCHECKED)


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

class RollupTest(unittest.TestCase):
    def registry(self, **overrides):
        return load_registry([dict(BASE, **overrides)])

    def test_a_published_clearance_is_green(self):
        out = answer(Query("part-s2p", "signoff", {"frequency": 1.0}),
                     self.registry())
        self.assertEqual(rollup([out])[0], GREEN)

    def test_a_screening_only_run_cannot_be_green(self):
        out = answer(Query("part-s2p", "screening", {"frequency": 1.0}),
                     self.registry(authority="synthetic"))
        status, reasons = rollup([out])
        self.assertEqual(status, SCREEN_ONLY)
        self.assertEqual(reasons, ["NO_SIGNOFF_GRADE_MODEL"])

    def test_no_coverage_outranks_screen_only(self):
        registry = self.registry(authority="synthetic")
        cleared = answer(Query("part-s2p", "screening", {"frequency": 1.0}),
                         registry)
        blind = answer(Query("part-s2p", "screening", {"frequency": 99.0}),
                       registry)
        self.assertEqual(rollup([cleared, blind])[0], NO_COVERAGE)

    def test_red_outranks_no_coverage(self):
        registry = self.registry()
        refused = answer(Query("nobody", "screening", {}), registry)
        blind = answer(Query("part-s2p", "screening", {"frequency": 99.0}),
                       registry)
        self.assertEqual(rollup([refused, blind])[0], RED)

    def test_an_empty_run_is_no_coverage_not_green(self):
        status, reasons = rollup([])
        self.assertEqual(status, NO_COVERAGE)
        self.assertEqual(reasons, ["NO_QUERIES"])


# --------------------------------------------------------------------------- #
# Query loading
# --------------------------------------------------------------------------- #

class QueryLoadTest(unittest.TestCase):
    def test_a_query_without_a_model_does_not_load(self):
        with self.assertRaises(ModelSyntaxError):
            load_query({"use": "signoff"})

    def test_an_unknown_use_does_not_load(self):
        with self.assertRaises(ModelSyntaxError):
            load_query({"model": "x", "use": "probably"})

    def test_a_boolean_coordinate_does_not_load(self):
        # bool is an int in Python; True would silently become 1.0 Hz.
        with self.assertRaises(ModelSyntaxError):
            load_query({"model": "x", "at": {"frequency": True}})

    def test_screening_is_the_default_use(self):
        self.assertEqual(load_query({"model": "x"}).use, "screening")


# --------------------------------------------------------------------------- #
# CLI
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

    def registry_file(self, *models):
        return self.write("registry.json",
                          {"schema_version": "vendor-model-registry/v1",
                           "models": list(models) or [BASE]})

    def query_file(self, *queries):
        return self.write("queries.json",
                          {"schema_version": "vendor-model-query/v1",
                           "queries": list(queries)})

    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def report_for(self, argv):
        out = self.dir / "out.json"
        code = self.run_main(argv + ["--json", str(out)])
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_self_test_passes(self):
        self.assertEqual(self.run_main(["--self-test"]), 0)

    def test_a_wrong_registry_schema_version_is_refused(self):
        path = self.write("bad.json", {"schema_version": "nope",
                                       "models": [BASE]})
        with self.assertRaises(ValueError):
            read_registry(path)

    def test_an_empty_registry_is_refused(self):
        path = self.write("empty.json",
                          {"schema_version": "vendor-model-registry/v1",
                           "models": []})
        with self.assertRaises(ValueError):
            read_registry(path)

    def test_registry_is_required(self):
        with self.assertRaises(ValueError):
            self.run_main([])

    def test_a_clean_signoff_run_is_green_and_exits_zero(self):
        target = self.dir / "models" / "part.s2p"
        target.parent.mkdir(parents=True)
        target.write_text("! placeholder\n", encoding="utf-8")
        registry = self.registry_file(dict(BASE, sha256=sha256_file(target)))
        queries = self.query_file({"model": "part-s2p", "use": "signoff",
                                   "at": {"frequency": 5.0}})
        code, report = self.report_for(["--registry", str(registry),
                                        "--query", str(queries),
                                        "--root", str(self.dir)])
        self.assertEqual(report["status"], GREEN)
        self.assertEqual(code, 0)
        self.assertEqual(report["grants"], {"signoff": 1, "screen_only": 0})

    def test_a_synthetic_only_run_is_screen_only(self):
        target = self.dir / "models" / "part.s2p"
        target.parent.mkdir(parents=True)
        target.write_text("! placeholder\n", encoding="utf-8")
        registry = self.registry_file(dict(BASE, authority="synthetic",
                                           sha256=sha256_file(target)))
        queries = self.query_file({"model": "part-s2p", "use": "screening",
                                   "at": {"frequency": 5.0}})
        code, report = self.report_for(["--registry", str(registry),
                                        "--query", str(queries),
                                        "--root", str(self.dir)])
        self.assertEqual(report["status"], SCREEN_ONLY)
        self.assertNotEqual(code, 0)

    def test_a_missing_model_file_makes_the_run_red(self):
        registry = self.registry_file()
        code, report = self.report_for(["--registry", str(registry),
                                        "--root", str(self.dir)])
        self.assertEqual(report["status"], RED)
        integrity = [f for f in report["findings"]
                     if f["check"] == "models_intact"][0]
        self.assertEqual(integrity["status"], FAIL)
        self.assertNotEqual(code, 0)

    def test_skip_integrity_does_not_invent_a_pass(self):
        # It removes the check, and the report has to show that the check is
        # simply not there rather than that it succeeded.
        registry = self.registry_file()
        _, report = self.report_for(["--registry", str(registry),
                                     "--skip-integrity"])
        self.assertEqual(report["integrity"], [])
        self.assertNotIn("models_intact",
                         [f["check"] for f in report["findings"]])

    def test_an_out_of_envelope_query_makes_the_run_no_coverage(self):
        target = self.dir / "models" / "part.s2p"
        target.parent.mkdir(parents=True)
        target.write_text("! placeholder\n", encoding="utf-8")
        registry = self.registry_file(dict(BASE, sha256=sha256_file(target)))
        queries = self.query_file({"model": "part-s2p", "use": "signoff",
                                   "at": {"frequency": 40.0}})
        _, report = self.report_for(["--registry", str(registry),
                                     "--query", str(queries),
                                     "--root", str(self.dir)])
        self.assertEqual(report["status"], NO_COVERAGE)
        self.assertEqual(report["answers"][0]["issue"], OUT_OF_ENVELOPE)

    def test_the_report_says_it_does_not_judge_accuracy(self):
        registry = self.registry_file()
        _, report = self.report_for(["--registry", str(registry),
                                     "--skip-integrity"])
        self.assertFalse(report["judges_accuracy"])
        self.assertEqual(report["schema_version"], "vendor-model-report/v1")

    def test_the_report_validates_against_its_schema(self):
        try:
            import jsonschema
        except ImportError:                       # pragma: no cover
            self.skipTest("jsonschema is not installed")
        schema = json.loads((TOOLS / "schemas" /
                             "vendor_model_report.schema.json")
                            .read_text(encoding="utf-8"))
        registry = self.registry_file()
        queries = self.query_file({"model": "part-s2p",
                                   "at": {"frequency": 1.0}})
        _, report = self.report_for(["--registry", str(registry),
                                     "--query", str(queries),
                                     "--root", str(self.dir)])
        jsonschema.validate(report, schema)


class FixtureTest(unittest.TestCase):
    def test_the_shipped_fixture_loads_clean(self):
        registry = read_registry(FIXTURE)
        self.assertEqual(registry.errors, [])
        self.assertTrue(registry.models)

    def test_no_fixture_entry_claims_vendor_authority(self):
        # Constraint #3. This repo ships no vendor models, and an invented
        # frequency range next to a real part number would be a fabricated
        # engineering claim.
        registry = read_registry(FIXTURE)
        for model in registry.models.values():
            with self.subTest(model=model.id):
                self.assertNotEqual(model.authority, "vendor_published")

    def test_every_fixture_entry_declares_an_envelope_with_units(self):
        registry = read_registry(FIXTURE)
        for model in registry.models.values():
            with self.subTest(model=model.id):
                self.assertTrue(model.envelope)
                for axis in model.envelope:
                    self.assertTrue(axis.unit.strip())

    def test_the_fixture_exercises_the_supersession_gate(self):
        registry = read_registry(FIXTURE)
        self.assertTrue(registry.superseded_by())


class SubprocessTest(unittest.TestCase):
    def test_self_test_passes_as_a_subprocess(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--self-test"],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("SELF-TEST PASS", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
