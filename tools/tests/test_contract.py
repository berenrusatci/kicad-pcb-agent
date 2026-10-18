#!/usr/bin/env python3
"""Tests for common/contract.py - the validation primitives.

WHY THIS FILE EXISTS: the capability inventory's test-matching was fixed to
count grouped suites, and after that fix exactly ONE real module remained
genuinely untested across the whole tree - this one. It was easy to miss
because ``scripts/production/contracts.py`` (plural) IS tested, and a
loose name search conflates the two.

WHY IT MATTERS: every other tool leans on these helpers to REJECT malformed
input. If ``require_int`` accepted a bool (bool IS an int in Python), or
``require_number`` accepted NaN/inf (which ``json.loads`` will happily
produce from ``NaN``/``Infinity``), a contract check would pass on data it
should have refused - and the failure would surface far away from here, in
whichever gate trusted the value. Each primitive is therefore tested for
what it ACCEPTS and, more importantly, for what it must REFUSE.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401

from common.contract import (  # noqa: E402
    ContractError,
    Issue,
    is_finite_number,
    is_text,
    require_dict,
    require_int,
    require_list,
    require_number,
    require_string_list,
    require_text,
)


class IssueRecord(unittest.TestCase):
    def test_as_dict_carries_the_four_mandatory_fields(self):
        issue = Issue(code="X", status="ERROR", path="$.a", message="bad")
        self.assertEqual({"code": "X", "status": "ERROR",
                          "path": "$.a", "message": "bad"}, issue.as_dict())

    def test_empty_context_is_omitted_not_serialised_as_empty(self):
        self.assertNotIn("context", Issue("X", "ERROR", "$", "m").as_dict())

    def test_a_populated_context_is_kept(self):
        issue = Issue("X", "ERROR", "$", "m", context={"expected": 3})
        self.assertEqual({"expected": 3}, issue.as_dict()["context"])

    def test_the_record_is_json_serialisable(self):
        json.dumps(Issue("X", "ERROR", "$", "m", {"n": 1}).as_dict())


class RequireDict(unittest.TestCase):
    def test_a_dict_passes_through(self):
        value = {"a": 1}
        self.assertIs(value, require_dict(value, "x"))

    def test_fault_injection_non_dicts_are_refused(self):
        for bad in ([], "", 0, None, [("a", 1)]):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    require_dict(bad, "x")

    def test_an_empty_dict_is_still_a_dict(self):
        self.assertEqual({}, require_dict({}, "x"))


class RequireList(unittest.TestCase):
    def test_a_nonempty_list_passes(self):
        self.assertEqual([1], require_list([1], "x"))

    def test_an_empty_list_is_refused_by_default(self):
        with self.assertRaises(ContractError):
            require_list([], "x")

    def test_an_empty_list_is_allowed_when_asked(self):
        self.assertEqual([], require_list([], "x", nonempty=False))

    def test_fault_injection_a_string_is_not_a_list(self):
        """A string is iterable, so a naive check would let 'abc' through as
        a three-item list."""
        with self.assertRaises(ContractError):
            require_list("abc", "x")

    def test_a_tuple_is_not_a_list(self):
        with self.assertRaises(ContractError):
            require_list((1, 2), "x")


class RequireText(unittest.TestCase):
    def test_real_text_passes(self):
        self.assertEqual("a", require_text("a", "x"))

    def test_fault_injection_blank_and_whitespace_are_refused(self):
        for bad in ("", "   ", "\t\n"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ContractError):
                    require_text(bad, "x")

    def test_non_strings_are_refused(self):
        for bad in (0, None, [], {}, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    require_text(bad, "x")


class RequireStringList(unittest.TestCase):
    def test_a_clean_unique_list_passes(self):
        self.assertEqual(["a", "b"], require_string_list(["a", "b"], "x"))

    def test_fault_injection_duplicates_are_refused(self):
        with self.assertRaises(ContractError):
            require_string_list(["a", "a"], "x")

    def test_duplicates_are_allowed_when_uniqueness_is_waived(self):
        self.assertEqual(["a", "a"],
                         require_string_list(["a", "a"], "x", unique=False))

    def test_a_blank_item_is_refused_and_named_by_index(self):
        with self.assertRaises(ContractError) as caught:
            require_string_list(["a", "  "], "x")
        self.assertIn("x[1]", str(caught.exception))

    def test_a_non_string_item_is_refused(self):
        with self.assertRaises(ContractError):
            require_string_list(["a", 2], "x")


class RequireInt(unittest.TestCase):
    def test_an_integer_passes(self):
        self.assertEqual(3, require_int(3, "x"))

    def test_fault_injection_a_bool_is_not_an_integer(self):
        """bool IS an int in Python, so a plain isinstance check lets True
        through as 1 - which would silently satisfy a count field."""
        for bad in (True, False):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    require_int(bad, "x")

    def test_a_float_is_refused_even_when_whole(self):
        with self.assertRaises(ContractError):
            require_int(3.0, "x")

    def test_the_minimum_is_enforced(self):
        self.assertEqual(0, require_int(0, "x", minimum=0))
        with self.assertRaises(ContractError):
            require_int(-1, "x", minimum=0)

    def test_the_error_names_the_minimum(self):
        with self.assertRaises(ContractError) as caught:
            require_int(-1, "x", minimum=0)
        self.assertIn(">= 0", str(caught.exception))


class RequireNumber(unittest.TestCase):
    def test_ints_and_floats_pass_as_float(self):
        self.assertEqual(3.0, require_number(3, "x"))
        self.assertEqual(1.5, require_number(1.5, "x"))

    def test_fault_injection_a_bool_is_not_a_number(self):
        with self.assertRaises(ContractError):
            require_number(True, "x")

    def test_fault_injection_nan_and_infinity_are_refused(self):
        """json.loads accepts NaN/Infinity by default, so these really do
        arrive from real input rather than only from hand-written code."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractError):
                    require_number(bad, "x")

    def test_the_json_route_that_produces_them_is_real(self):
        parsed = json.loads('{"v": NaN}')
        with self.assertRaises(ContractError):
            require_number(parsed["v"], "v")

    def test_strings_are_refused_even_when_numeric_looking(self):
        with self.assertRaises(ContractError):
            require_number("3", "x")


class BooleanChecks(unittest.TestCase):
    """The accumulating validators' half: return False, never raise."""

    def test_is_text(self):
        self.assertTrue(is_text("a"))
        for bad in ("", "  ", None, 0, [], True):
            with self.subTest(bad=bad):
                self.assertFalse(is_text(bad))

    def test_is_finite_number(self):
        for good in (0, 1, -1, 1.5):
            with self.subTest(good=good):
                self.assertTrue(is_finite_number(good))
        for bad in (True, False, "1", None, [],
                    float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertFalse(is_finite_number(bad))

    def test_the_two_families_agree_on_what_is_acceptable(self):
        """is_finite_number must accept exactly what require_number accepts -
        otherwise the two validation styles would disagree on the same
        input and a value rejected by one gate would pass another."""
        samples = [0, 1, -1, 1.5, True, False, "1", None, [],
                   float("nan"), float("inf"), float("-inf")]
        for value in samples:
            with self.subTest(value=value):
                try:
                    require_number(value, "x")
                    raised = False
                except ContractError:
                    raised = True
                self.assertEqual(not raised, is_finite_number(value))


class ErrorType(unittest.TestCase):
    def test_contract_error_is_a_value_error(self):
        """Callers that catch ValueError must keep catching these."""
        self.assertTrue(issubclass(ContractError, ValueError))

    def test_the_error_names_the_offending_field(self):
        with self.assertRaises(ContractError) as caught:
            require_dict([], "manifest.inputs")
        self.assertIn("manifest.inputs", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
