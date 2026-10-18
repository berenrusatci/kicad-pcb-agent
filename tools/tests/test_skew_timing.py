#!/usr/bin/env python3
"""Tests for board/skew_timing.py - the mm -> ps derived-metric adapter.

What is actually at stake here, and therefore what these tests check:

  * the CONVERSION INPUT. ``escape_geom.FR4_MICROSTRIP_VELOCITY_MM_NS = 167.6``
    exists in this repo and its own docstring calls it "a single typical FR4
    microstrip number". If it were reachable as a default, every skew verdict
    on every board would come from it and every one would look like an answer.
    ``NoDefaultVelocity`` proves it is not reachable: not by import, not by a
    signature default, and not by arithmetic coincidence.
  * the UNITS. ``units`` must be a real field of the artifact holding "ps",
    with ``measured`` holding the picosecond number - not a millimetre number
    with a picosecond label pasted on by an assertion downstream.
  * the ZERO. ``measure_board`` reports ``skew_mm = |0 - 0| = 0.0`` for a pair
    with no copper, which reads as a perfectly matched pair. That is the
    loudest silent pass in the chain and it must come out NO_COVERAGE.
  * the BINDING. ``board_sha256`` must be the parent report's ``design.sha256``
    verbatim, or the picoseconds float free of the board they describe.
  * the SCHEMA. ``length-report/v1`` had zero producers in this repo. If this
    tool is going to be the first, the emitted document has to validate
    against it, so ``SchemaAgreement`` validates it - by hand, because the
    repo has no ``jsonschema`` dependency.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS, REPO, TOOLS  # noqa: E402

import skew_timing  # noqa: E402
from common.coupon import CouponError, propagation_velocity_mm_per_ps  # noqa: E402

SCRIPT = BOARD_SCRIPTS / "skew_timing.py"
SCHEMA = (REPO / "skills" / "highspeed-routing" / "references"
          / "length_report.schema.json")

BOARD_SHA = "b3" + "a" * 62
SOURCE = "stackup_plan.json 2026-08-10 rev C, solved against the fab stackup"
BUDGET_SOURCE = "MIPI D-PHY v2.1 table 16 intra-pair skew"

# 1.2 mm of copper on a pair, and one pair with no copper at all.
PARENT = {
    "schema_version": "measure-board-report/v1",
    "tool": "measure_board.py",
    "board": "b.kicad_pcb",
    "design": {"path": "b.kicad_pcb", "sha256": BOARD_SHA},
    "measurements": {"pair_skew_mm": {
        "D0_P:D0_N": {"p_mm": 25.1, "n_mm": 23.9, "skew_mm": 1.2},
        "UNROUTED_P:UNROUTED_N": {"p_mm": 0.0, "n_mm": 0.0, "skew_mm": 0.0},
    }},
    "status": "PASS",
}

STATED = ("--er-eff", "3.08", "--er-eff-source", SOURCE)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class ParentFile:
    """A measure_board report on disk, with optional surgery applied."""

    def __init__(self, payload=None):
        self.payload = json.loads(json.dumps(payload or PARENT))

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "measure_board.json"
        self.path.write_text(json.dumps(self.payload), encoding="utf-8")
        self.out = self.dir / "skew.json"
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False

    def report(self):
        return json.loads(self.out.read_text(encoding="utf-8"))


def metric_of(report, pair):
    return next(m for m in report["metrics"] if m["net_or_pair"] == pair)


# --------------------------------------------------------------------------- #
class Conversion(unittest.TestCase):
    """The arithmetic, checked against physics rather than recorded output."""

    def test_skew_ps_is_length_over_the_tem_velocity(self):
        expected = 1.2 / propagation_velocity_mm_per_ps(3.08)
        self.assertAlmostEqual(expected, skew_timing.skew_ps(1.2, 3.08),
                               places=12)

    def test_a_vacuum_line_converts_at_the_speed_of_light(self):
        """er_eff = 1: 1 mm takes 1/0.299792458 ps, and nothing else."""
        self.assertAlmostEqual(1.0 / 0.299792458,
                               skew_timing.skew_ps(1.0, 1.0), places=9)

    def test_quadrupling_er_eff_doubles_the_delay(self):
        self.assertAlmostEqual(
            2.0, skew_timing.skew_ps(1.0, 4.0) / skew_timing.skew_ps(1.0, 1.0),
            places=12)

    def test_a_non_positive_er_eff_is_refused_not_clamped(self):
        with self.assertRaises(CouponError):
            skew_timing.skew_ps(1.0, 0.0)


# --------------------------------------------------------------------------- #
class NoDefaultVelocity(unittest.TestCase):
    """The FR4 typical constant must not be reachable as a default."""

    def test_the_hazard_is_real(self):
        """Guard the premise: escape_geom really does default the velocity.

        If this ever stops being true the rest of this class is checking
        nothing, and the reader deserves to be told which half changed.
        """
        from common.escape_geom import (
            FR4_MICROSTRIP_VELOCITY_MM_NS, skew_mm_to_ps,
        )
        self.assertEqual(167.6, FR4_MICROSTRIP_VELOCITY_MM_NS)
        default = inspect.signature(skew_mm_to_ps).parameters["velocity_mm_ns"]
        self.assertEqual(167.6, default.default,
                         "escape_geom.skew_mm_to_ps no longer defaults its "
                         "velocity; update this test, not the tool")

    def test_importing_the_tool_does_not_pull_in_escape_geom(self):
        """A fresh interpreter, so another test's import cannot mask it."""
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(TOOLS)!r});"
            f"sys.path.insert(0, {str(BOARD_SCRIPTS)!r});"
            "import skew_timing;"
            "print(any('escape_geom' in name for name in sys.modules))"
        )
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              encoding="utf-8", check=False)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("False", proc.stdout.strip(),
                         "skew_timing reached escape_geom; the typical FR4 "
                         "velocity is one import away from being a default")

    def test_the_conversion_has_no_default_permittivity(self):
        for name, parameter in inspect.signature(
                skew_timing.skew_ps).parameters.items():
            with self.subTest(parameter=name):
                self.assertIs(inspect.Parameter.empty, parameter.default)

    def test_the_derived_value_is_not_the_fr4_constant_answer(self):
        """Arithmetic proof, not a text search: 167.6 mm/ns was not used."""
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED, "--json", str(fixture.out))
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            measured = metric_of(fixture.report(), "D0_P:D0_N")["measured"]
        fr4_answer = 1.2 / 167.6 * 1000.0
        self.assertAlmostEqual(1.2 / propagation_velocity_mm_per_ps(3.08),
                               measured, places=3)
        self.assertGreater(abs(measured - fr4_answer), 0.05,
                           "the derived ps equals the typical-FR4 answer")

    def test_no_velocity_source_is_refused(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path))
            self.assertEqual(2, proc.returncode)
            self.assertIn("er_eff", proc.stderr)
            self.assertEqual("", proc.stdout.strip(),
                             "a refusal must not emit a report")
            self.assertFalse(fixture.out.exists())

    def test_a_stated_er_eff_without_its_provenance_is_refused(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--er-eff", "3.08")
            self.assertEqual(2, proc.returncode)
            self.assertIn("--er-eff-source", proc.stderr)

    def test_an_empty_provenance_string_is_refused(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--er-eff", "3.08",
                       "--er-eff-source", "   ")
            self.assertEqual(2, proc.returncode)
            self.assertIn("--er-eff-source", proc.stderr)

    def test_solve_without_a_cross_section_is_refused(self):
        """The solver would answer for a geometry that is not the board's."""
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--solve", "--structure",
                       "microstrip")
            self.assertEqual(2, proc.returncode)
            self.assertIn("--w-mm", proc.stderr)


# --------------------------------------------------------------------------- #
class Units(unittest.TestCase):
    def test_units_is_a_real_field_holding_ps(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            report = fixture.report()
        for metric in report["metrics"]:
            with self.subTest(pair=metric["net_or_pair"]):
                self.assertEqual("ps", metric["units"])

    def test_measured_is_the_picosecond_number_not_the_millimetre_one(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            metric = metric_of(fixture.report(), "D0_P:D0_N")
        self.assertNotAlmostEqual(1.2, metric["measured"], places=3)
        self.assertAlmostEqual(1.2 / propagation_velocity_mm_per_ps(3.08),
                               metric["measured"], places=3)

    def test_the_conversion_input_travels_as_numbers(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            semantics = metric_of(fixture.report(),
                                  "D0_P:D0_N")["semantics"]
        self.assertAlmostEqual(3.08, semantics["epsilon_eff"], places=12)
        self.assertAlmostEqual(
            propagation_velocity_mm_per_ps(3.08) * 1000.0,
            semantics["velocity_mm_per_ns"], places=9)

    def test_a_slower_laminate_yields_a_larger_skew_in_ps(self):
        """Same copper, more permittivity, more picoseconds."""
        values = []
        for er_eff in ("3.08", "9.00"):
            with ParentFile() as fixture:
                run(str(fixture.path), "--er-eff", er_eff, "--er-eff-source",
                    SOURCE, "--json", str(fixture.out))
                values.append(metric_of(fixture.report(),
                                        "D0_P:D0_N")["measured"])
        self.assertLess(values[0], values[1])
        # places=4, not more: ``measured`` is rounded to 4 decimals in ps.
        self.assertAlmostEqual(math.sqrt(9.00 / 3.08), values[1] / values[0],
                               places=4)


# --------------------------------------------------------------------------- #
class Provenance(unittest.TestCase):
    def test_the_parent_board_digest_is_carried_verbatim(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            report = fixture.report()
        self.assertEqual(BOARD_SHA, report["board_sha256"])
        self.assertEqual(PARENT["design"]["sha256"], report["board_sha256"])

    def test_the_parent_millimetre_value_is_carried(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            definition = metric_of(fixture.report(),
                                   "D0_P:D0_N")["semantics"]["metric_definition"]
        self.assertIn("parent_skew_mm=1.2", definition)
        self.assertIn("pair_skew_mm[D0_P:D0_N].skew_mm", definition)
        self.assertIn("measure_board.py", definition)

    def test_a_stated_er_eff_records_that_it_was_stated_and_by_whom(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            definition = metric_of(fixture.report(),
                                   "D0_P:D0_N")["semantics"]["metric_definition"]
        self.assertIn("stated via --er-eff:", definition)
        self.assertIn(SOURCE, definition)
        self.assertNotIn("field_solver", definition)

    def test_a_board_without_a_digest_is_refused_not_bound_to_nothing(self):
        payload = json.loads(json.dumps(PARENT))
        del payload["design"]
        with ParentFile(payload) as fixture:
            proc = run(str(fixture.path), *STATED, "--json", str(fixture.out))
            self.assertEqual(2, proc.returncode)
            report = json.loads(proc.stdout)
            self.assertEqual("ERROR", report["status"])
            self.assertEqual(["PARENT_REPORT_UNUSABLE"], report["reason_codes"])
            self.assertFalse(fixture.out.exists())

    def test_a_truncated_digest_is_refused(self):
        payload = json.loads(json.dumps(PARENT))
        payload["design"]["sha256"] = "deadbeef"
        with ParentFile(payload) as fixture:
            self.assertEqual(2, run(str(fixture.path), *STATED).returncode)

    def test_the_tool_records_its_own_hash_and_argv(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            tool = fixture.report()["tool"]
        from common.hashing import sha256_file
        self.assertEqual(sha256_file(SCRIPT), tool["sha256"])
        self.assertIn("--er-eff", tool["argv"])
        self.assertIn(SOURCE, tool["argv"])


# --------------------------------------------------------------------------- #
class NoCoverage(unittest.TestCase):
    def test_a_pair_with_no_copper_is_no_coverage_not_a_perfect_match(self):
        """skew_mm 0.0 from |0 - 0| is the absence of a route, not a match."""
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED, "--budget-ps", "20",
                       "--budget-source", BUDGET_SOURCE,
                       "--json", str(fixture.out))
            report = fixture.report()
        metric = metric_of(report, "UNROUTED_P:UNROUTED_N")
        self.assertEqual("NO_COVERAGE", metric["verdict"])
        self.assertIsNone(metric["measured"])
        self.assertNotEqual(0.0, metric["measured"])
        self.assertIn("no copper", metric["verdict_reason"])
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(1, proc.returncode,
                         "NO_COVERAGE is never a pass")

    def test_no_coverage_is_counted_as_excluded_with_its_reason(self):
        with ParentFile() as fixture:
            run(str(fixture.path), *STATED, "--json", str(fixture.out))
            coverage = fixture.report()["coverage"]
        self.assertEqual(2, coverage["eligible"])
        self.assertEqual(1, coverage["scanned"])
        self.assertEqual(1, coverage["excluded"])
        self.assertEqual(1, len(coverage["excluded_reasons"]))

    def test_no_coverage_outranks_a_clean_pair(self):
        """One good pair must not carry an unrouted one to GREEN."""
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED, "--budget-ps", "1000",
                       "--budget-source", BUDGET_SOURCE,
                       "--json", str(fixture.out))
            report = fixture.report()
        self.assertEqual("PASS", metric_of(report, "D0_P:D0_N")["verdict"])
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertEqual(1, proc.returncode)

    def test_a_parent_with_no_pairs_measured_is_refused(self):
        payload = json.loads(json.dumps(PARENT))
        payload["measurements"]["pair_skew_mm"] = {}
        with ParentFile(payload) as fixture:
            proc = run(str(fixture.path), *STATED)
            self.assertEqual(2, proc.returncode)
            self.assertIn("PARENT_REPORT_UNUSABLE",
                          json.loads(proc.stdout)["reason_codes"])

    def test_a_pair_absent_from_the_parent_cannot_be_conjured(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED, "--pair", "CK_P:CK_N")
            self.assertEqual(2, proc.returncode)
            self.assertIn("CK_P:CK_N", proc.stdout)


# --------------------------------------------------------------------------- #
class Budget(unittest.TestCase):
    def test_without_a_budget_the_metric_is_recorded_and_unjudged(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--pair", "D0_P:D0_N", *STATED,
                       "--json", str(fixture.out))
            report = fixture.report()
        metric = metric_of(report, "D0_P:D0_N")
        self.assertEqual("INCONCLUSIVE", metric["verdict"])
        self.assertIsNone(metric["limit"])
        self.assertIsNotNone(metric["measured"])
        self.assertEqual("INCONCLUSIVE", report["status"])
        self.assertEqual(1, proc.returncode,
                         "an unjudged metric is not a pass")

    def test_an_unsourced_budget_is_refused(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED, "--budget-ps", "20")
            self.assertEqual(2, proc.returncode)
            self.assertIn("--budget-source", proc.stderr)

    def test_a_source_without_a_number_is_refused(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED,
                       "--budget-source", BUDGET_SOURCE)
            self.assertEqual(2, proc.returncode)
            self.assertIn("--budget-ps", proc.stderr)

    def test_a_sourced_budget_produces_a_pass_and_carries_its_source(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--pair", "D0_P:D0_N", *STATED,
                       "--budget-ps", "20", "--budget-source", BUDGET_SOURCE,
                       "--json", str(fixture.out))
            report = fixture.report()
        metric = metric_of(report, "D0_P:D0_N")
        self.assertEqual("PASS", metric["verdict"])
        self.assertEqual(20.0, metric["limit"])
        self.assertIn(BUDGET_SOURCE, metric["verdict_reason"])
        self.assertEqual("GREEN", report["status"])
        self.assertEqual(0, proc.returncode)

    def test_over_budget_is_red_and_counted_as_a_violation(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--pair", "D0_P:D0_N", *STATED,
                       "--budget-ps", "1.0", "--budget-source", BUDGET_SOURCE,
                       "--json", str(fixture.out))
            report = fixture.report()
        self.assertEqual("FAIL", metric_of(report, "D0_P:D0_N")["verdict"])
        self.assertEqual("RED", report["status"])
        self.assertEqual(1, report["coverage"]["violations"])
        self.assertEqual(1, proc.returncode)


# --------------------------------------------------------------------------- #
class SolvePath(unittest.TestCase):
    """The 2-D field solver as the conversion input."""

    SECTION = ("--solve", "--structure", "coupled-microstrip",
               "--w-mm", "0.09", "--s-mm", "0.15", "--t-mm", "0.035",
               "--h-mm", "0.10", "--er", "4.3", "--max-nodes", "20000")

    def test_solving_records_the_field_solver_as_the_source(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--pair", "D0_P:D0_N",
                       *self.SECTION, "--json", str(fixture.out))
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            semantics = metric_of(fixture.report(),
                                  "D0_P:D0_N")["semantics"]
        self.assertIn("field_solver 2-D solve",
                      semantics["metric_definition"])
        self.assertIn("odd mode", semantics["metric_definition"])
        self.assertNotIn("stated via", semantics["metric_definition"])

    def test_the_solved_permittivity_is_between_air_and_the_laminate(self):
        """er_eff for a microstrip is nowhere near er - the whole reason it
        is never defaulted."""
        with ParentFile() as fixture:
            run(str(fixture.path), "--pair", "D0_P:D0_N", *self.SECTION,
                "--json", str(fixture.out))
            semantics = metric_of(fixture.report(),
                                  "D0_P:D0_N")["semantics"]
        self.assertGreater(semantics["epsilon_eff"], 1.0)
        self.assertLess(semantics["epsilon_eff"], 4.3)

    def test_a_solved_stripline_recovers_the_laminate_permittivity(self):
        """Homogeneous medium: er_eff must be er, which pins the plumbing
        from the solver through to the emitted velocity."""
        with ParentFile() as fixture:
            proc = run(str(fixture.path), "--pair", "D0_P:D0_N", "--solve",
                       "--structure", "stripline", "--w-mm", "0.15",
                       "--t-mm", "0.035", "--h-mm", "0.30", "--er", "4.3",
                       "--max-nodes", "60000", "--json", str(fixture.out))
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            semantics = metric_of(fixture.report(),
                                  "D0_P:D0_N")["semantics"]
        self.assertAlmostEqual(4.3, semantics["epsilon_eff"], places=3)
        self.assertAlmostEqual(299.792458 / math.sqrt(4.3),
                               semantics["velocity_mm_per_ns"], places=1)


# --------------------------------------------------------------------------- #
class CliContract(unittest.TestCase):
    def test_stdout_is_pure_json(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED)
            json.loads(proc.stdout)

    def test_a_missing_parent_report_is_exit_2(self):
        proc = run("no-such-report.json", *STATED)
        self.assertEqual(2, proc.returncode)
        self.assertEqual("ERROR", json.loads(proc.stdout)["status"])

    def test_a_parent_that_is_not_json_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(2, run(str(path), *STATED).returncode)

    def test_the_written_report_matches_stdout(self):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *STATED, "--json", str(fixture.out))
            self.assertEqual(json.loads(proc.stdout), fixture.report())


# --------------------------------------------------------------------------- #
def validate(instance, schema, root=None, path="$"):
    """A hand-rolled validator for the JSON Schema subset this file uses.

    The repo has no ``jsonschema`` dependency and does not want one (see
    ``common/findings.py``), so the schema is honoured by walking it. It
    covers exactly the keywords ``length_report.schema.json`` uses; anything
    else would be a silent pass, so unknown keywords are reported.
    """
    root = schema if root is None else root
    errors: list[str] = []
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return validate(instance, target, root, path)

    checkers = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    known = {"$schema", "$id", "$comment", "$defs", "title", "type", "required",
             "properties", "additionalProperties", "enum", "const", "minItems",
             "minLength", "minimum", "exclusiveMinimum", "pattern", "items"}
    unknown = set(schema) - known
    if unknown:
        errors.append(f"{path}: validator does not implement {sorted(unknown)}")

    if "type" in schema:
        types = schema["type"]
        types = types if isinstance(types, list) else [types]
        if not any(checkers[t](instance) for t in types):
            return errors + [f"{path}: {instance!r} is not {types}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: {instance!r} != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} !~ {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < {schema['minimum']}")
        if "exclusiveMinimum" in schema \
                and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {instance} <= exclusive "
                          f"{schema['exclusiveMinimum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        for index, item in enumerate(instance):
            errors += validate(item, schema.get("items", {}), root,
                               f"{path}[{index}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}: additional property {key!r}")
        for key, value in instance.items():
            if key in properties:
                errors += validate(value, properties[key], root,
                                   f"{path}.{key}")
    return errors


class SchemaAgreement(unittest.TestCase):
    """``length-report/v1`` had zero producers. This is the first one."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    def test_the_validator_agrees_with_itself(self):
        """A validator that never says no proves nothing."""
        self.assertEqual([], validate({"schema_version": "length-report/v1"},
                                      {"properties": {"schema_version": {
                                          "const": "length-report/v1"}}}))
        self.assertTrue(validate({"schema_version": "other/v1"},
                                 {"properties": {"schema_version": {
                                     "const": "length-report/v1"}}}))
        self.assertTrue(validate({}, {"required": ["a"]}))
        self.assertTrue(validate({"b": 1}, {"properties": {},
                                            "additionalProperties": False}))
        self.assertTrue(validate([], {"type": "array", "minItems": 1}))

    def test_the_tool_declares_the_schema_it_emits(self):
        self.assertEqual(self.schema["properties"]["schema_version"]["const"],
                         skew_timing.SCHEMA_VERSION)
        self.assertIn(skew_timing.REPORT_KIND,
                      self.schema["properties"]["report_kind"]["enum"])

    def _validated(self, *args):
        with ParentFile() as fixture:
            proc = run(str(fixture.path), *args, "--json", str(fixture.out))
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            report = fixture.report()
        self.assertEqual([], validate(report, self.schema), report)
        return report

    def test_the_unjudged_report_validates(self):
        self._validated(*STATED)

    def test_the_judged_report_validates(self):
        self._validated(*STATED, "--budget-ps", "20",
                        "--budget-source", BUDGET_SOURCE)

    def test_the_failing_report_validates(self):
        self._validated(*STATED, "--budget-ps", "0.5",
                        "--budget-source", BUDGET_SOURCE)

    def test_the_solved_report_validates(self):
        self._validated("--solve", "--structure", "microstrip", "--w-mm",
                        "0.20", "--t-mm", "0.035", "--h-mm", "0.10",
                        "--er", "4.3", "--max-nodes", "20000")

    def test_every_status_the_tool_can_emit_is_in_the_schema_enum(self):
        allowed = set(self.schema["properties"]["status"]["enum"])
        for verdicts, status in ((["FAIL"], "RED"),
                                 (["NO_COVERAGE"], "NO_COVERAGE"),
                                 (["INCONCLUSIVE"], "INCONCLUSIVE"),
                                 (["PASS"], "GREEN")):
            metrics = [{"verdict": v} for v in verdicts]
            with self.subTest(verdicts=verdicts):
                self.assertEqual(status, skew_timing.overall_status(metrics))
                self.assertIn(status, allowed)


if __name__ == "__main__":
    unittest.main()
