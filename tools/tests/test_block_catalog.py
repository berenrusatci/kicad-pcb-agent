"""Tests for common/blocks.py, common/netlist.py and scripts/block_catalog.py.

The catalogue's job is to make a decision checkable, so most of what
follows asserts that it says NO in the cases where a permissive reader
would say yes: a parameter nobody supplied, a block nobody can verify, a
net that exists but carries a name no rule can bind to.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.blocks import (ABSENT, CATALOG, FSYNC_ROUTES, PRESENT,  # noqa: E402
                           UNVERIFIABLE, BlockRefusal, instantiate)
from common.findings import NO_COVERAGE  # noqa: E402
from common.netlist import (autonamed_nets, generated_nets,  # noqa: E402
                            is_autonamed, no_connect_nets, parse_nets,
                            read_nets, refs_on_net, refs_with_prefix)
from scripts.block_catalog import (build_instances, load_decision,  # noqa: E402
                                   main, parse_params, rollup,
                                   verification_finding)

SCRIPT = TOOLS / "scripts" / "block_catalog.py"

NETLIST = """(export (version "E")
  (nets
    (net (code "1") (name "VDD") (class "Default")
      (node (ref "U1") (pin "5") (pinfunction "OUT") (pintype "power_out"))
      (node (ref "C1") (pin "1") (pintype "passive"))
      (node (ref "C2") (pin "1") (pintype "passive"))
      (node (ref "C3") (pin "1") (pintype "passive")))
    (net (code "2") (name "FSYNC") (class "Default")
      (node (ref "J1") (pin "17") (pintype "passive"))
      (node (ref "J2") (pin "19") (pintype "passive")))
    (net (code "3") (name "Net-(D1-IO1)") (class "Default")
      (node (ref "D1") (pin "1") (pintype "passive"))
      (node (ref "J1") (pin "2") (pintype "passive")))
    (net (code "4") (name "Net-(D1-IO2)") (class "Default")
      (node (ref "D1") (pin "2") (pintype "passive"))
      (node (ref "J1") (pin "3") (pintype "passive")))
    (net (code "5") (name "unconnected-(U1-NC-Pad4)") (class "Default")
      (node (ref "U1") (pin "4") (pintype "no_connect")))))
"""


class NetlistTest(unittest.TestCase):
    def setUp(self):
        self.nets = parse_nets(NETLIST)

    def test_pairs_ref_and_pin_from_the_same_node(self):
        # The failure this guards: kicad-cli puts ref and pin on separate
        # lines, so a line-oriented reader pairs one net's ref with the
        # next net's pin at any change of formatting.
        self.assertEqual(self.nets["FSYNC"], [("J1", "17"), ("J2", "19")])

    def test_reads_from_a_file(self):
        tmp = Path(__file__).resolve().parent / "_tmp_blocks_netlist.net"
        tmp.write_text(NETLIST, encoding="utf-8")
        try:
            self.assertEqual(read_nets(tmp), self.nets)
        finally:
            tmp.unlink(missing_ok=True)

    def test_rejects_unparseable_input(self):
        with self.assertRaises(ValueError):
            parse_nets("")

    def test_refs_helpers(self):
        self.assertEqual(refs_on_net(self.nets, "VDD"), ["C1", "C2", "C3", "U1"])
        self.assertEqual(refs_with_prefix(self.nets, "VDD", "C"),
                         ["C1", "C2", "C3"])
        self.assertEqual(refs_on_net(self.nets, "NOPE"), [])

    def test_generated_and_no_connect_names_are_kept_apart(self):
        # Lumping them buries the two real cases under the harmless one: a
        # declared no-connect is intentional, a generated name is a
        # conductor no rule can address.
        self.assertEqual(generated_nets(self.nets),
                         ["Net-(D1-IO1)", "Net-(D1-IO2)"])
        self.assertEqual(no_connect_nets(self.nets),
                         ["unconnected-(U1-NC-Pad4)"])
        self.assertEqual(len(autonamed_nets(self.nets)), 3)

    def test_is_autonamed_covers_both_kinds(self):
        self.assertTrue(is_autonamed("Net-(D1-IO1)"))
        self.assertTrue(is_autonamed("unconnected-(U1-NC-Pad4)"))
        self.assertFalse(is_autonamed("VDD"))


class RefusalTest(unittest.TestCase):
    def test_missing_required_parameter_names_itself(self):
        with self.assertRaises(BlockRefusal) as ctx:
            instantiate("decoupling_shell", {"rail": "VDD"})
        self.assertEqual(sorted(ctx.exception.missing),
                         ["bulk_farads", "decades"])
        self.assertEqual(ctx.exception.to_dict()["issue"],
                         "MISSING_REQUIRED_PARAMETER")

    def test_no_required_parameter_has_a_default(self):
        # A default on a required parameter would be a value this module
        # invented, which is the thing it promises not to do.
        for template in CATALOG.values():
            for param in template.parameters:
                if param.required:
                    self.assertIsNone(param.default,
                                      f"{template.key}.{param.name}")

    def test_unknown_block_is_refused(self):
        with self.assertRaises(BlockRefusal):
            instantiate("no_such_block", {})

    def test_unknown_parameter_is_refused_not_ignored(self):
        with self.assertRaises(BlockRefusal):
            instantiate("decoupling_shell",
                        {"rail": "V", "decades": 2, "bulk_farads": 1e-5,
                         "typo_param": 1})

    def test_a_bank_with_no_members_is_refused(self):
        with self.assertRaises(BlockRefusal):
            instantiate("decoupling_shell",
                        {"rail": "V", "decades": 0, "bulk_farads": 1e-5})

    def test_unknown_fsync_route_is_refused(self):
        with self.assertRaises(BlockRefusal):
            instantiate("link_architecture",
                        {"serdes_required": False, "fsync_route": "carrier_pigeon"})


class DecouplingShellTest(unittest.TestCase):
    def setUp(self):
        self.nets = parse_nets(NETLIST)

    def test_bank_is_decade_spaced_and_descending(self):
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 4, "bulk_farads": 1e-5})
        values = [c["nominal_farads"] for c in block.emitted["components"]]
        self.assertEqual(len(values), 4)
        for higher, lower in zip(values, values[1:]):
            self.assertAlmostEqual(higher / lower, 10.0)

    def test_bands_are_labelled_end_to_end(self):
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5})
        bands = [c["band"] for c in block.emitted["components"]]
        self.assertEqual(bands[0], "bulk")
        self.assertEqual(bands[-1], "hf")

    def test_emits_roles_not_part_numbers(self):
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 2, "bulk_farads": 1e-5})
        for component in block.emitted["components"]:
            self.assertIn("role", component)
            self.assertNotIn("mpn", component)

    def test_present_when_the_count_is_met(self):
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5},
                            self.nets)
        self.assertEqual(block.verification["outcome"], PRESENT)

    def test_absent_when_the_count_falls_short(self):
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 6, "bulk_farads": 1e-5},
                            self.nets)
        self.assertEqual(block.verification["outcome"], ABSENT)

    def test_absent_when_the_rail_is_not_in_the_netlist(self):
        block = instantiate("decoupling_shell",
                            {"rail": "GHOST", "decades": 2, "bulk_farads": 1e-5},
                            self.nets)
        self.assertEqual(block.verification["outcome"], ABSENT)

    def test_verification_says_it_did_not_check_values(self):
        # A netlist carries no component values, so a count is all that can
        # honestly be claimed - and the report has to say so.
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5},
                            self.nets)
        self.assertIn("VALUES are not", block.verification["detail"])


class DifferentialPairTest(unittest.TestCase):
    def setUp(self):
        self.nets = parse_nets(NETLIST)
        self.params = {"netclass": "CSI2", "impedance_ohm": 100,
                       "tolerance_pct": 20}

    def test_window_is_computed_from_the_tolerance(self):
        block = instantiate("differential_pair_rule",
                            dict(self.params, positive_net="FSYNC",
                                 negative_net="VDD"))
        rule = block.emitted["rules"][0]
        self.assertEqual(rule["window_ohm"], [80.0, 120.0])

    def test_present_only_when_both_nets_are_explicitly_named(self):
        block = instantiate("differential_pair_rule",
                            dict(self.params, positive_net="FSYNC",
                                 negative_net="VDD"), self.nets)
        self.assertEqual(block.verification["outcome"], PRESENT)

    def test_a_generated_name_is_absent_not_present(self):
        # The important case: the conductor EXISTS. A checker that only asks
        # "is the net there" passes this and leaves the impedance rule bound
        # to nothing.
        block = instantiate("differential_pair_rule",
                            dict(self.params, positive_net="Net-(D1-IO1)",
                                 negative_net="Net-(D1-IO2)"), self.nets)
        self.assertEqual(block.verification["outcome"], ABSENT)
        self.assertEqual(block.verification["unbindable_nets"],
                         ["Net-(D1-IO1)", "Net-(D1-IO2)"])

    def test_missing_nets_report_the_generated_ones_as_a_lead(self):
        block = instantiate("differential_pair_rule",
                            dict(self.params, positive_net="CSI_CLK_P",
                                 negative_net="CSI_CLK_N"), self.nets)
        self.assertEqual(block.verification["outcome"], ABSENT)
        self.assertEqual(block.verification["generated_name_nets"],
                         ["Net-(D1-IO1)", "Net-(D1-IO2)"])
        self.assertNotIn("unconnected-(U1-NC-Pad4)",
                         block.verification["generated_name_nets"])


class LinkArchitectureTest(unittest.TestCase):
    def setUp(self):
        self.nets = parse_nets(NETLIST)

    def test_back_channel_expects_no_frame_sync_net(self):
        block = instantiate("link_architecture",
                            {"serdes_required": True,
                             "fsync_route": "back_channel",
                             "fsync_net": "CAM_FSYNC"}, self.nets)
        self.assertEqual(block.verification["outcome"], PRESENT)

    def test_back_channel_with_a_frame_sync_net_present_is_the_defect(self):
        block = instantiate("link_architecture",
                            {"serdes_required": True,
                             "fsync_route": "back_channel",
                             "fsync_net": "FSYNC"}, self.nets)
        self.assertEqual(block.verification["outcome"], ABSENT)

    def test_connector_pin_route_requires_the_net_to_exist(self):
        good = instantiate("link_architecture",
                           {"serdes_required": False,
                            "fsync_route": "connector_pin",
                            "fsync_net": "FSYNC"}, self.nets)
        self.assertEqual(good.verification["outcome"], PRESENT)
        bad = instantiate("link_architecture",
                          {"serdes_required": False,
                           "fsync_route": "connector_pin",
                           "fsync_net": "MISSING"}, self.nets)
        self.assertEqual(bad.verification["outcome"], ABSENT)

    def test_a_route_with_nothing_to_look_for_is_unverifiable(self):
        block = instantiate("link_architecture",
                            {"serdes_required": False,
                             "fsync_route": "connector_pin"}, self.nets)
        self.assertEqual(block.verification["outcome"], UNVERIFIABLE)

    def test_serdes_plus_a_dedicated_contact_is_flagged_as_redundant(self):
        block = instantiate("link_architecture",
                            {"serdes_required": True,
                             "fsync_route": "connector_pin",
                             "fsync_net": "FSYNC"}, self.nets)
        asserts = [a["assert"] for a in block.emitted["assertions"]]
        self.assertIn("redundant_fsync_contact", asserts)

    def test_every_declared_route_is_documented(self):
        for route in FSYNC_ROUTES:
            self.assertTrue(FSYNC_ROUTES[route].strip(), route)


class NoNetlistTest(unittest.TestCase):
    def test_no_netlist_never_becomes_present(self):
        block = instantiate("decoupling_shell",
                            {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5})
        self.assertEqual(block.verification["outcome"], UNVERIFIABLE)

    def test_verification_finding_without_a_netlist_is_no_coverage(self):
        finding = verification_finding([], None)
        self.assertEqual(finding["status"], NO_COVERAGE)

    def test_a_partial_check_is_not_a_passing_one(self):
        nets = parse_nets(NETLIST)
        instances = [
            instantiate("decoupling_shell",
                        {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5}, nets),
            instantiate("link_architecture",
                        {"serdes_required": False,
                         "fsync_route": "connector_pin"}, nets),
        ]
        finding = verification_finding(instances, nets)
        self.assertEqual(finding["status"], NO_COVERAGE)


class DecisionDocumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_decision.json"

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def _write(self, doc):
        self.tmp.write_text(json.dumps(doc), encoding="utf-8")
        return self.tmp

    def test_loads_a_well_formed_document(self):
        path = self._write({"blocks": [{"block": "decoupling_shell",
                                        "params": {"rail": "VDD"}}]})
        self.assertEqual(len(load_decision(path)), 1)

    def test_rejects_an_empty_or_malformed_document(self):
        for doc in ({}, {"blocks": []}, {"blocks": [{}]},
                    {"blocks": [{"block": "x", "params": []}]}, []):
            with self.assertRaises(ValueError):
                load_decision(self._write(doc))

    def test_one_unbuildable_block_does_not_hide_the_others(self):
        entries = [{"block": "decoupling_shell", "params": {"rail": "VDD"}},
                   {"block": "link_architecture",
                    "params": {"serdes_required": False,
                               "fsync_route": "connector_pin",
                               "fsync_net": "FSYNC"}}]
        instances, refusals = build_instances(entries, parse_nets(NETLIST))
        self.assertEqual(len(instances), 1)
        self.assertEqual(len(refusals), 1)


class ParamParsingTest(unittest.TestCase):
    def test_types_survive(self):
        params = parse_params(["serdes_required=false", "decades=4",
                               "rail=VDD_1V2D", "bulk_farads=1e-5"])
        self.assertIs(params["serdes_required"], False)
        self.assertEqual(params["decades"], 4)
        self.assertEqual(params["rail"], "VDD_1V2D")
        self.assertAlmostEqual(params["bulk_farads"], 1e-5)

    def test_malformed_params_are_rejected(self):
        for bad in (["rail"], ["=VDD"]):
            with self.assertRaises(ValueError):
                parse_params(bad)


class RollupTest(unittest.TestCase):
    def test_fail_beats_no_coverage(self):
        findings = [{"check": "a", "status": "FAIL"},
                    {"check": "b", "status": NO_COVERAGE}]
        self.assertEqual(rollup(findings)[0], "RED")

    def test_no_coverage_beats_pass(self):
        findings = [{"check": "a", "status": "PASS"},
                    {"check": "b", "status": NO_COVERAGE}]
        self.assertEqual(rollup(findings)[0], "NO_COVERAGE")

    def test_all_pass_is_green(self):
        self.assertEqual(rollup([{"check": "a", "status": "PASS"}])[0], "GREEN")


class CliTest(unittest.TestCase):
    def setUp(self):
        here = Path(__file__).resolve().parent
        self.netlist = here / "_tmp_cli_blocks.net"
        self.netlist.write_text(NETLIST, encoding="utf-8")
        self.decision = here / "_tmp_cli_decision.json"
        self.decision.write_text(json.dumps({"blocks": [
            {"block": "decoupling_shell",
             "params": {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5}},
            {"block": "link_architecture",
             "params": {"serdes_required": False,
                        "fsync_route": "connector_pin",
                        "fsync_net": "FSYNC"}},
        ]}), encoding="utf-8")

    def tearDown(self):
        self.netlist.unlink(missing_ok=True)
        self.decision.unlink(missing_ok=True)

    def test_self_test_passes(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--self-test"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_list_exits_zero(self):
        self.assertEqual(main(["--list"]), 0)

    def test_a_matching_design_is_green(self):
        self.assertEqual(main(["--decision", str(self.decision),
                               "--netlist", str(self.netlist)]), 0)

    def test_without_a_netlist_the_run_is_not_green(self):
        self.assertEqual(main(["--decision", str(self.decision)]), 1)

    def test_a_drifted_design_is_red(self):
        self.decision.write_text(json.dumps({"blocks": [
            {"block": "differential_pair_rule",
             "params": {"netclass": "CSI2", "positive_net": "CSI_P",
                        "negative_net": "CSI_N", "impedance_ohm": 100,
                        "tolerance_pct": 20}}]}), encoding="utf-8")
        self.assertEqual(main(["--decision", str(self.decision),
                               "--netlist", str(self.netlist)]), 1)

    def test_decision_and_block_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            main(["--decision", str(self.decision), "--block", "decoupling_shell"])
        with self.assertRaises(ValueError):
            main([])

    def test_report_is_schema_shaped_and_deterministic(self):
        out = Path(__file__).resolve().parent / "_tmp_block_report.json"
        argv = ["--decision", str(self.decision), "--netlist",
                str(self.netlist), "--json", str(out)]
        try:
            main(argv)
            first = out.read_text(encoding="utf-8")
            main(argv)
            self.assertEqual(first, out.read_text(encoding="utf-8"))
            report = json.loads(first)
            self.assertEqual(report["schema_version"], "block-plan/v1")
            self.assertFalse(report["emits_schematic"])
            self.assertEqual(len(report["instances"]), 2)
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
