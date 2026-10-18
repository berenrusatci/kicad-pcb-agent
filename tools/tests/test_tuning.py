#!/usr/bin/env python3
"""Tests for common/tuning.py and the tuning_defaults.json table.

The table exists because one physical rule was written in three modules
with two different values, and the module that claimed to be the single
source was the one that had drifted. So these tests check two things: the
loader refuses rather than defaults, and the real shipped table is
internally coherent.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCRIPTS, TOOLS  # noqa: E402

from common import tuning  # noqa: E402

GOOD = {
    "schema_version": tuning.TUNING_SCHEMA,
    "decoupling": {"max_distance_mm": {"value": 3.0}, "target_distance_mm": {"value": 1.5}},
    "routing": {"rule_multiplier": {"value": 3.0}, "violation_weight": {"value": 5.0},
                "via_cost": {"value": 5.0}},
    "thermal": {"ambient_temp_c": {"value": 25.0}},
    "pdn": {"drop_pct_tiers": {"value": [
        {"max_drop_pct": 2.0, "capacitors_f": [1e-7]},
        {"max_drop_pct": None, "capacitors_f": [1e-7, 1e-5]},
    ]}},
}


class LoaderTests(unittest.TestCase):
    def setUp(self):
        tuning.clear_cache()
        self.addCleanup(tuning.clear_cache)

    def write(self, payload):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "tuning.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_missing_table_raises_instead_of_defaulting(self):
        """The whole point: a silently substituted screening threshold turns
        a gate into a guess."""
        with self.assertRaises(tuning.TuningDataError) as caught:
            tuning.load_tuning(Path("nope") / "missing.json")
        self.assertIn("no built-in", str(caught.exception))

    def test_a_wrong_schema_version_raises(self):
        path = self.write(dict(GOOD, schema_version="tuning-defaults/v99"))
        with self.assertRaises(tuning.TuningDataError):
            tuning.load_tuning(path)

    def test_a_missing_key_raises_rather_than_returning_none(self):
        """``None`` would be coalesced away by a caller and become 0."""
        payload = json.loads(json.dumps(GOOD))
        del payload["routing"]["via_cost"]
        path = self.write(payload)
        with self.assertRaises(tuning.TuningDataError):
            tuning.tuning_float("routing", "via_cost", path)

    def test_a_bare_value_without_its_rationale_is_rejected(self):
        """Every entry is an object carrying value + rationale; a bare number
        is how a table stops explaining itself."""
        payload = json.loads(json.dumps(GOOD))
        payload["routing"]["via_cost"] = 5.0
        path = self.write(payload)
        with self.assertRaises(tuning.TuningDataError):
            tuning.tuning_float("routing", "via_cost", path)

    def test_a_target_looser_than_the_limit_is_rejected(self):
        payload = json.loads(json.dumps(GOOD))
        payload["decoupling"]["target_distance_mm"]["value"] = 9.0
        path = self.write(payload)
        with self.assertRaises(tuning.TuningDataError):
            tuning.decoupling_limits(path)

    def test_out_of_order_tiers_are_rejected(self):
        """The lookup takes the FIRST tier that covers the drop, so an
        out-of-order table silently returns the wrong capacitors."""
        payload = json.loads(json.dumps(GOOD))
        payload["pdn"]["drop_pct_tiers"]["value"] = [
            {"max_drop_pct": 5.0, "capacitors_f": [1e-7]},
            {"max_drop_pct": 2.0, "capacitors_f": [1e-6]},
        ]
        path = self.write(payload)
        with self.assertRaises(tuning.TuningDataError):
            tuning.drop_pct_tiers(path)

    def test_null_becomes_the_open_ended_top_tier(self):
        """JSON has no infinity literal; encoding it as null keeps the file
        valid without a magic large number."""
        path = self.write(GOOD)
        tiers = tuning.drop_pct_tiers(path)
        self.assertEqual(float("inf"), tiers[-1][0])


class ShippedTableTests(unittest.TestCase):
    def setUp(self):
        tuning.clear_cache()
        self.addCleanup(tuning.clear_cache)

    def test_the_real_table_loads_and_is_coherent(self):
        limit, target = tuning.decoupling_limits()
        self.assertLessEqual(target, limit)
        costs = tuning.routing_costs()
        self.assertEqual({"rule_multiplier", "violation_weight", "via_cost"}, set(costs))
        self.assertTrue(tuning.ambient_temp_c() > 0)
        self.assertTrue(tuning.drop_pct_tiers())

    def test_every_entry_carries_a_rationale(self):
        """A number without a stated reason is the thing this table replaced.
        Entries whose reason was never recorded say THAT, explicitly."""
        table = tuning.load_tuning()
        for section, entries in table.items():
            if not isinstance(entries, dict):
                continue
            for key, entry in entries.items():
                with self.subTest(entry=f"{section}.{key}"):
                    self.assertIn("rationale", entry)
                    self.assertIn("source", entry)
                    self.assertTrue(entry["rationale"].strip())

    def test_the_consumers_agree_on_the_decoupling_limit(self):
        """Three modules, one number - checkable now, not just asserted in a
        comment the way it was before."""
        limit, _target = tuning.decoupling_limits()

        sys.path.insert(0, str(SCRIPTS / "board"))
        sys.path.insert(0, str(SCRIPTS / "sim"))
        import ngspice  # noqa: E402
        finding = {"status": "FAIL", "violations": [
            {"node": "vout", "nominal_v": 3.3, "drop_mv": 200.0}]}
        suggestion = ngspice.decoupling_suggestion(finding)[0]
        self.assertEqual(limit, suggestion.max_distance_mm)

    def test_no_module_reintroduces_a_literal_routing_default(self):
        """Static guard: the drift returns the moment a convenient default
        goes back into a signature."""
        import re
        offenders = []
        pattern = re.compile(r"(rule_multiplier|violation_weight|via_cost)"
                             r"\s*:\s*float\s*=\s*[\d.]")
        for path in (SCRIPTS.rglob("*.py")):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(TOOLS)}:{line_no}: {line.strip()}")
        self.assertEqual([], offenders,
                         "these live in fixtures/references/tuning_defaults.json")


if __name__ == "__main__":
    unittest.main()
