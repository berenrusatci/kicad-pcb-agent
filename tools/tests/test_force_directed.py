#!/usr/bin/env python3
"""Tests for the force-directed placement engine.

Ported from the source pytest suite (``test_kuvvet_yonelimli_yerlesim.py``)
to unittest. The source module was library-only: its ``__main__`` ran the
self-tests and printed prose, there was no report, no schema and no CLI
contract. Those are pinned here alongside the physics regressions.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCHEMAS, SCRIPTS  # noqa: E402

from scripts.place import force_directed as fd  # noqa: E402

SCRIPT = SCRIPTS / "place" / "force_directed.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


def write_problem(directory: str, problem: dict) -> Path:
    path = Path(directory) / "problem.json"
    path.write_text(json.dumps(problem, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #
class GraphConstruction(unittest.TestCase):
    def test_a_two_pin_net_yields_one_edge(self):
        self.assertEqual({("U1", "U2"): 1.0},
                         fd.build_graph([fd.Net("SDA", ["U1", "U2"])]))

    def test_a_many_pin_net_shares_out_its_weight(self):
        """A 4-pin net: every edge gets 1/(4-1), so it cannot steamroll a
        2-pin critical net."""
        edges = fd.build_graph([fd.Net("BUS", ["U1", "U2", "U3", "U4"])])
        self.assertEqual(6, len(edges))  # C(4,2)
        for weight in edges.values():
            self.assertAlmostEqual(1 / 3, weight)

    def test_ground_and_power_nets_stay_out_of_the_graph(self):
        self.assertTrue(fd.is_plane_net("GND"))
        self.assertTrue(fd.is_plane_net("+3V3"))
        self.assertFalse(fd.is_plane_net("GND_SENSE"))  # a real routed sense line
        self.assertEqual({}, fd.build_graph([fd.Net("GND", ["U1", "U2", "U3"])]))

    def test_plane_skipping_can_be_turned_off(self):
        self.assertEqual(
            {("U1", "U2"): 1.0},
            fd.build_graph([fd.Net("GND", ["U1", "U2"])], skip_plane_nets=False))

    def test_a_single_pin_net_yields_no_edge(self):
        self.assertEqual({}, fd.build_graph([fd.Net("NC", ["U1"])]))

    def test_the_same_pair_accumulates_weight_across_nets(self):
        edges = fd.build_graph([fd.Net("SDA", ["U1", "U2"]),
                                fd.Net("SCL", ["U1", "U2"])])
        self.assertAlmostEqual(2.0, edges[("U1", "U2")])


class Clustering(unittest.TestCase):
    def test_two_independent_groups_are_separated(self):
        edges = fd.build_graph([fd.Net("A", ["U1", "C1"]),
                                fd.Net("B", ["U2", "C2"])])
        clusters = fd.find_clusters(edges, ["U1", "C1", "U2", "C2"])
        self.assertEqual([["C1", "U1"], ["C2", "U2"]], sorted(clusters))

    def test_an_unconnected_component_is_a_single_member_cluster(self):
        """No coverage loss — an unconnected part is never silently dropped."""
        edges = fd.build_graph([fd.Net("A", ["U1", "C1"])])
        self.assertIn(["TP1"], fd.find_clusters(edges, ["U1", "C1", "TP1"]))

    def test_a_weak_edge_does_not_draw_a_cluster_boundary(self):
        """The 0.111-weight links of a 10-pin bus (threshold 0.5) must not
        merge clusters, or everything lands in one giant cluster."""
        edges = fd.build_graph([fd.Net("BUS", [f"U{i}" for i in range(10)])])
        clusters = fd.find_clusters(edges, [f"U{i}" for i in range(10)],
                                    weight_threshold=0.5)
        self.assertEqual(10, len(clusters))


class InitialPlacementAndDeterminism(unittest.TestCase):
    def test_initial_placement_is_deterministic(self):
        components = [fd.Component(f"U{i}") for i in range(10)]
        self.assertEqual(fd.initial_placement(components, 30.0, 30.0),
                         fd.initial_placement(components, 30.0, 30.0))

    def test_initial_placement_leaves_fixed_components_alone(self):
        components = [fd.Component("J1", x=1.0, y=2.0, fixed=True),
                      fd.Component("U1")]
        self.assertEqual((1.0, 2.0),
                         fd.initial_placement(components, 30.0, 30.0)["J1"])

    def test_invalid_board_dimensions_are_rejected(self):
        with self.assertRaises(ValueError):
            fd.initial_placement([fd.Component("U1")], 0.0, 10.0)

    def test_solve_placement_is_deterministic(self):
        netlist = [fd.Net("A", ["U0", "U1"]), fd.Net("B", ["U2", "U3", "U4"])]
        first = fd.solve_placement([fd.Component(f"U{i}") for i in range(8)],
                                   netlist, 40.0, 40.0)
        second = fd.solve_placement([fd.Component(f"U{i}") for i in range(8)],
                                    netlist, 40.0, 40.0)
        self.assertEqual(first.coordinates, second.coordinates)


# --------------------------------------------------------------------------- #
# The physics engine
# --------------------------------------------------------------------------- #
class PhysicsEngine(unittest.TestCase):
    def test_the_engine_shortens_the_ratsnest(self):
        components = [fd.Component(f"U{i}", 2.0, 2.0) for i in range(10)]
        netlist = [fd.Net("A", ["U0", "U9"]), fd.Net("B", ["U1", "U8"]),
                   fd.Net("C", ["U2", "U7"])]
        placement = fd.solve_placement(components, netlist, 60.0, 60.0)
        self.assertLess(placement.final_ratsnest_mm,
                        placement.initial_ratsnest_mm)
        self.assertGreater(placement.improvement_ratio, 0.2)

    def test_connected_parts_end_closer_than_unconnected_ones(self):
        components = [fd.Component(ref, 1.0, 1.0) for ref in ("U1", "C1", "U2")]
        netlist = [fd.Net("VDD_SENSE", ["U1", "C1"])]  # U2 is connected to nothing
        coordinates = fd.solve_placement(components, netlist, 40.0, 40.0).coordinates
        self.assertLess(math.dist(coordinates["U1"], coordinates["C1"]),
                        math.dist(coordinates["U1"], coordinates["U2"]))

    def test_a_fixed_component_never_moves(self):
        components = [fd.Component("J1", 5.0, 5.0, x=2.5, y=2.5, fixed=True),
                      fd.Component("U1", 2.0, 2.0)]
        placement = fd.solve_placement(components, [fd.Net("D+", ["J1", "U1"])],
                                       40.0, 40.0)
        self.assertEqual((2.5, 2.5), placement.coordinates["J1"])

    def test_coordinates_stay_inside_the_board(self):
        components = [fd.Component(f"U{i}", 4.0, 4.0) for i in range(12)]
        netlist = [fd.Net("A", [f"U{i}" for i in range(12)], weight=5.0)]
        placement = fd.solve_placement(components, netlist, 30.0, 20.0)
        for x, y in placement.coordinates.values():
            self.assertTrue(2.0 - 1e-6 <= x <= 28.0 + 1e-6)
            self.assertTrue(2.0 - 1e-6 <= y <= 18.0 + 1e-6)

    def test_no_overlap_remains_even_under_a_heavy_net(self):
        """REGRESSION (this caught a real bug): a 6-pin net of weight 10 made
        attraction CRUSH repulsion in the pure force balance and all 6 pairs
        overlapped. The separation pass was added for exactly this; with a
        big enough board the overlap count must be ZERO."""
        components = [fd.Component(f"U{i}", 3.0, 3.0) for i in range(6)]
        netlist = [fd.Net("CLK", [f"U{i}" for i in range(6)], weight=10.0)]
        placement = fd.solve_placement(components, netlist, 50.0, 50.0)
        finding = fd.check_courtyard_overlap(components, placement.coordinates)
        self.assertEqual("PASS", finding["status"], finding["violations"])

    def test_a_board_that_is_too_small_is_not_silently_called_a_success(self):
        components = [fd.Component(f"U{i}", 5.0, 5.0) for i in range(6)]
        netlist = [fd.Net("CLK", [f"U{i}" for i in range(6)])]
        placement = fd.solve_placement(components, netlist, 8.0, 8.0)
        self.assertEqual(
            "FAIL",
            fd.check_courtyard_overlap(components, placement.coordinates)["status"])

    def test_an_empty_netlist_does_not_raise(self):
        placement = fd.solve_placement([], [], 10.0, 10.0)
        self.assertEqual({}, placement.coordinates)
        self.assertEqual(0.0, placement.improvement_ratio)

    def test_a_max_constraint_shortens_the_distance(self):
        """Decoupling (<= 1.5 mm): nothing PULLS U1 and C1 together (C1's rail
        is a plane and stays out of the graph), so the constraint force is the
        only thing that can. The constraint has to bind or the test measures
        nothing."""
        components = [fd.Component("U1", 1.0, 1.0), fd.Component("C1", 1.0, 1.0),
                      fd.Component("U2", 1.0, 1.0)]
        netlist = [fd.Net("GND", ["U1", "C1"]),
                   fd.Net("SIG", ["U2", "C1"], weight=8.0)]
        free = fd.solve_placement(components, netlist, 40.0, 40.0)
        bound = fd.solve_placement(
            components, netlist, 40.0, 40.0,
            constraints=[fd.DistanceConstraint("U1", "C1", max_mm=1.5,
                                               note="decoupling")])
        self.assertLess(
            math.dist(bound.coordinates["U1"], bound.coordinates["C1"]),
            math.dist(free.coordinates["U1"], free.coordinates["C1"]))

    def test_a_min_constraint_lengthens_the_distance(self):
        """LDO <-> I2C >= 10 mm isolation."""
        components = [fd.Component("U5", 2.0, 2.0), fd.Component("U1", 2.0, 2.0)]
        netlist = [fd.Net("EN", ["U5", "U1"])]
        free = fd.solve_placement(components, netlist, 60.0, 60.0)
        bound = fd.solve_placement(
            components, netlist, 60.0, 60.0,
            constraints=[fd.DistanceConstraint("U5", "U1", min_mm=10.0,
                                               note="LDO isolation")])
        self.assertGreater(
            math.dist(bound.coordinates["U5"], bound.coordinates["U1"]),
            math.dist(free.coordinates["U5"], free.coordinates["U1"]))


# --------------------------------------------------------------------------- #
# Hard acceptance gates
# --------------------------------------------------------------------------- #
class HardGates(unittest.TestCase):
    def test_courtyard_gate_passes_on_a_clean_placement(self):
        components = [fd.Component("U1", 2.0, 2.0), fd.Component("U2", 2.0, 2.0)]
        finding = fd.check_courtyard_overlap(
            components, {"U1": (0.0, 0.0), "U2": (10.0, 0.0)})
        self.assertEqual("PASS", finding["status"])

    def test_courtyard_gate_fails_on_overlap(self):
        components = [fd.Component("U1", 2.0, 2.0), fd.Component("U2", 2.0, 2.0)]
        finding = fd.check_courtyard_overlap(
            components, {"U1": (0.0, 0.0), "U2": (0.5, 0.0)})
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual("U1", finding["violations"][0]["a"])

    def test_courtyard_gate_is_no_coverage_when_empty(self):
        """'No components at all' can never silently be a PASS."""
        self.assertEqual("NO_COVERAGE",
                         fd.check_courtyard_overlap([], {})["status"])

    def test_constraint_gate_measures_a_violation(self):
        finding = fd.check_distance_constraints(
            [fd.DistanceConstraint("U1", "C1", max_mm=1.5, note="decoupling")],
            {"U1": (0.0, 0.0), "C1": (5.0, 0.0)})
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual(5.0, finding["violations"][0]["measured_mm"])
        self.assertEqual("max", finding["violations"][0]["kind"])

    def test_constraint_gate_passes_when_met(self):
        finding = fd.check_distance_constraints(
            [fd.DistanceConstraint("U1", "C1", max_mm=1.5)],
            {"U1": (0.0, 0.0), "C1": (1.0, 0.0)})
        self.assertEqual("PASS", finding["status"])

    def test_an_empty_constraint_list_is_no_coverage(self):
        self.assertEqual(
            "NO_COVERAGE",
            fd.check_distance_constraints([], {"U1": (0.0, 0.0)})["status"])

    def test_a_min_violation_is_reported(self):
        finding = fd.check_distance_constraints(
            [fd.DistanceConstraint("U5", "U1", min_mm=10.0)],
            {"U5": (0.0, 0.0), "U1": (3.0, 0.0)})
        self.assertEqual("min", finding["violations"][0]["kind"])


class ReportAndSelfTest(unittest.TestCase):
    def test_the_markdown_report_carries_the_seed_warning(self):
        components = [fd.Component("U1"), fd.Component("C1")]
        netlist = [fd.Net("VDD_SENSE", ["U1", "C1"])]
        placement = fd.solve_placement(components, netlist, 20.0, 20.0)
        edges = fd.build_graph(netlist)
        text = fd.placement_markdown_report(
            placement,
            fd.find_clusters(edges, ["U1", "C1"]),
            [fd.check_courtyard_overlap(components, placement.coordinates)])
        self.assertIn("SEED placement", text)
        self.assertIn("enclosure keepout", text)
        self.assertIn("courtyard_overlap", text)

    def test_ratsnest_length_known_value(self):
        self.assertEqual(10.0, fd.total_ratsnest_mm(
            {"U1": (0.0, 0.0), "U2": (3.0, 4.0)}, {("U1", "U2"): 2.0}))

    def test_ratsnest_skips_a_missing_coordinate(self):
        self.assertEqual(0.0, fd.total_ratsnest_mm({"U1": (0.0, 0.0)},
                                                   {("U1", "U2"): 1.0}))

    def test_fault_injection_really_breaks(self):
        """With attraction 0 the engine must NOT improve anything — the proof
        that the improvement test is not vacuous."""
        self.assertTrue(fd.prove_tests_not_empty())

    def test_builtin_self_tests_pass(self):
        self.assertEqual([], fd.run_self_tests())


# --------------------------------------------------------------------------- #
# Hierarchical placement
# --------------------------------------------------------------------------- #
class HierarchicalPlacement(unittest.TestCase):
    CATEGORIES = {
        "U0": fd.PlacementCategory.POWER_DECOUPLING,
        "U1": fd.PlacementCategory.POWER_DECOUPLING,
        "U2": fd.PlacementCategory.CRITICAL_HS,
        "U3": fd.PlacementCategory.CRITICAL_HS,
        "U4": fd.PlacementCategory.LOW_SPEED_IO,
        "U5": fd.PlacementCategory.LOW_SPEED_IO,
    }

    def test_every_category_is_placed(self):
        components = [fd.Component(f"U{i}") for i in range(6)]
        netlist = [fd.Net("A", ["U0", "U1"]), fd.Net("B", ["U2", "U3"]),
                   fd.Net("C", ["U4", "U5"])]
        placement = fd.solve_hierarchical_placement(
            components, self.CATEGORIES, netlist, 40.0, 40.0)
        self.assertEqual({"U0", "U1", "U2", "U3", "U4", "U5"},
                         set(placement.coordinates))

    def test_an_earlier_stage_is_locked(self):
        """After the power/decoupling stage, U0/U1 must NOT move during the HS
        stage — later stages lock earlier ones, they never drag them back."""
        components = [fd.Component(f"U{i}") for i in range(4)]
        categories = {ref: self.CATEGORIES[ref] for ref in
                      ("U0", "U1", "U2", "U3")}
        netlist = [fd.Net("A", ["U0", "U1"]), fd.Net("B", ["U1", "U2"]),
                   fd.Net("C", ["U2", "U3"])]
        power_only = fd.solve_hierarchical_placement(
            components[:2],
            {"U0": fd.PlacementCategory.POWER_DECOUPLING,
             "U1": fd.PlacementCategory.POWER_DECOUPLING},
            [fd.Net("A", ["U0", "U1"])], 40.0, 40.0)
        full = fd.solve_hierarchical_placement(
            components, categories, netlist, 40.0, 40.0)
        self.assertEqual(power_only.coordinates["U0"], full.coordinates["U0"])
        self.assertEqual(power_only.coordinates["U1"], full.coordinates["U1"])

    def test_a_fixed_component_without_a_category_is_preserved(self):
        connector = fd.Component("J1", 10.0, 5.0, x=35.0, y=35.0, fixed=True)
        placement = fd.solve_hierarchical_placement(
            [connector, fd.Component("U0")],
            {"U0": fd.PlacementCategory.POWER_DECOUPLING},
            [fd.Net("A", ["U0", "J1"])], 40.0, 40.0)
        self.assertEqual((35.0, 35.0), placement.coordinates["J1"])

    def test_an_empty_component_list(self):
        placement = fd.solve_hierarchical_placement([], {}, [], 40.0, 40.0)
        self.assertEqual({}, placement.coordinates)
        self.assertEqual(0, placement.iterations)

    def test_the_ratsnest_improves(self):
        components = [fd.Component(f"U{i}") for i in range(6)]
        netlist = [fd.Net("A", ["U0", "U1"]), fd.Net("B", ["U2", "U3"]),
                   fd.Net("C", ["U4", "U5"])]
        placement = fd.solve_hierarchical_placement(
            components, self.CATEGORIES, netlist, 40.0, 40.0)
        self.assertLess(placement.final_ratsnest_mm,
                        placement.initial_ratsnest_mm)


# --------------------------------------------------------------------------- #
# Thermal anchors
# --------------------------------------------------------------------------- #
SQUARE_ZONE = fd.ThermalContactZone(
    name="heatsink_boss_1",
    polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
    z_clearance_mm=0.3,
)


class ThermalConstraints(unittest.TestCase):
    def test_a_component_outside_the_zone_produces_nothing(self):
        components, constraints = fd.thermal_constraints(
            [fd.ComponentThermalState("U1", 50.0, 50.0)], [SQUARE_ZONE])
        self.assertEqual([], components)
        self.assertEqual([], constraints)

    def test_a_component_inside_the_zone_produces_an_anchor(self):
        components, constraints = fd.thermal_constraints(
            [fd.ComponentThermalState("U1", 5.0, 5.0)], [SQUARE_ZONE])
        self.assertEqual(1, len(components))
        self.assertTrue(components[0].fixed)
        self.assertEqual((5.0, 5.0), (components[0].x, components[0].y))
        self.assertEqual(1, len(constraints))
        self.assertEqual("U1", constraints[0].ref_a)
        self.assertEqual(components[0].ref, constraints[0].ref_b)
        self.assertEqual(3.0, constraints[0].max_mm)

    def test_no_data_means_no_output(self):
        self.assertEqual(([], []), fd.thermal_constraints([], []))

    def test_the_anchor_really_acts_on_the_engine(self):
        """Proof that this is more than data generation: the produced anchor +
        constraint actually pull the heat source towards the contact zone."""
        extra, constraints = fd.thermal_constraints(
            [fd.ComponentThermalState("U1", 35.0, 35.0)],
            [fd.ThermalContactZone("boss", [(5, 5), (9, 5), (9, 9), (5, 9)], 0.3)],
            max_distance_mm=1.0)
        movable = fd.Component("U1", x=35.0, y=35.0)
        placement = fd.solve_placement([movable] + extra, [], 40.0, 40.0,
                                       constraints=constraints)
        x, y = placement.coordinates["U1"]
        self.assertGreater(math.hypot(x - 35.0, y - 35.0), 5.0)


# --------------------------------------------------------------------------- #
# HighSpeedRuleManager
# --------------------------------------------------------------------------- #
class HighSpeedDetection(unittest.TestCase):
    def test_a_differential_net_class(self):
        self.assertTrue(fd.is_high_speed_net("ANY_NAME", net_class="DIFF_90OHM"))

    def test_csi_and_mipi_names(self):
        self.assertTrue(fd.is_high_speed_net("CAM0_CSI_D0"))
        self.assertTrue(fd.is_high_speed_net("MIPI_CLK"))

    def test_p_n_suffixes(self):
        self.assertTrue(fd.is_high_speed_net("HDMI0_TX0_P"))
        self.assertTrue(fd.is_high_speed_net("ETH_TRD0_N"))

    def test_a_one_character_body_is_not_a_false_positive(self):
        self.assertFalse(fd.is_high_speed_net("A_P"))
        self.assertFalse(fd.is_high_speed_net("N"))

    def test_ordinary_nets_are_not_high_speed(self):
        self.assertFalse(fd.is_high_speed_net("GPIO5"))
        self.assertFalse(fd.is_high_speed_net("I2C_SCL"))

    def test_a_high_speed_net_gets_its_weight_raised_automatically(self):
        """The caller must not have to write `weight=` as well."""
        edges = fd.build_graph([fd.Net("HDMI0_TX0_P", ["U1", "U2"]),
                                fd.Net("GPIO5", ["U3", "U4"])])
        self.assertGreater(edges[("U1", "U2")], edges[("U3", "U4")])
        self.assertAlmostEqual(fd.HIGH_SPEED_DEFAULT_WEIGHT, edges[("U1", "U2")])

    def test_an_explicit_weight_is_never_overwritten(self):
        edges = fd.build_graph(
            [fd.Net("HDMI0_TX0_P", ["U1", "U2"], weight=0.5)])
        self.assertAlmostEqual(0.5, edges[("U1", "U2")])


class HighSpeedKeepouts(unittest.TestCase):
    def test_an_ordinary_net_produces_no_keepout(self):
        self.assertEqual([], fd.high_speed_keepouts(
            fd.Net("GPIO5", ["U1", "U2"]),
            {"U1": (0.0, 0.0), "U2": (10.0, 0.0)}, trace_width_mm=0.2))

    def test_centre_and_radius(self):
        keepouts = fd.high_speed_keepouts(
            fd.Net("HDMI0_TX0_P", ["U1", "U2"]),
            {"U1": (0.0, 0.0), "U2": (10.0, 0.0)}, trace_width_mm=0.2)
        self.assertEqual(1, len(keepouts))
        keepout = keepouts[0]
        self.assertAlmostEqual(5.0, keepout.center_x_mm)
        self.assertAlmostEqual(0.0, keepout.center_y_mm)
        self.assertAlmostEqual(0.2 * 3.0 + 0.15, keepout.radius_mm)
        self.assertEqual(("U1", "U2"), (keepout.source_ref, keepout.target_ref))

    def test_a_third_component_in_the_corridor_violates(self):
        keepouts = [fd.HighSpeedKeepout("TRD0", 5.0, 0.0, 1.0,
                                        source_ref="D4", target_ref="D6")]
        components = {ref: fd.Component(ref, 2.0, 2.0)
                      for ref in ("D4", "D6", "R1")}
        coordinates = {"D4": (0.0, 0.0), "D6": (10.0, 0.0), "R1": (5.0, 0.0)}
        self.assertEqual(["R1"], fd.keepout_violations(
            coordinates, components, keepouts))

    def test_a_keepouts_own_endpoints_never_violate_it(self):
        """Otherwise EVERY keepout would pointlessly FAIL against its own two
        ends — the corridor starts at those very pins."""
        keepouts = [fd.HighSpeedKeepout("TRD0", 5.0, 0.0, 100.0,
                                        source_ref="D4", target_ref="D6")]
        components = {ref: fd.Component(ref, 2.0, 2.0) for ref in ("D4", "D6")}
        self.assertEqual([], fd.keepout_violations(
            {"D4": (0.0, 0.0), "D6": (10.0, 0.0)}, components, keepouts))

    def test_the_finding_wrapper(self):
        keepouts = [fd.HighSpeedKeepout("TRD0", 5.0, 0.0, 1.0,
                                        source_ref="D4", target_ref="D6")]
        components = {ref: fd.Component(ref, 2.0, 2.0) for ref in ("D4", "R1")}
        finding = fd.check_keepout_overlap(
            {"D4": (0.0, 0.0), "R1": (5.0, 0.0)}, components, keepouts)
        self.assertEqual("high_speed_keepout_violation", finding["check"])
        self.assertEqual("FAIL", finding["status"])
        self.assertEqual([{"ref": "R1"}], finding["violations"])

    def test_no_components_is_no_coverage(self):
        self.assertEqual("NO_COVERAGE",
                         fd.check_keepout_overlap({}, {}, [])["status"])

    def test_the_engine_rejects_a_step_into_a_keepout(self):
        """FAULT-INJECTION-LIKE PROOF: without a keepout a strong attraction
        pulls R1 into the D4-D6 corridor (the courtyards never touch, so
        repulsion never engages); WITH the keepout R1 must stay outside — a
        concrete demonstration that the infinite repulsion cannot be crushed
        by a soft force."""
        components = [
            fd.Component("D4", 2.0, 2.0, x=5.0, y=20.0, fixed=True),
            fd.Component("D6", 2.0, 2.0, x=45.0, y=20.0, fixed=True),
            fd.Component("R1", 1.0, 1.0, x=25.0, y=5.0),
        ]
        # R1 is tied to BOTH D4 and D6 -> without a keepout the equilibrium
        # sits exactly in the middle of the corridor.
        netlist = [fd.Net("SIG_A", ["D4", "R1"], weight=20.0),
                   fd.Net("SIG_B", ["D6", "R1"], weight=20.0)]
        keepouts = [fd.HighSpeedKeepout("TRD0", 25.0, 20.0, 8.0,
                                        source_ref="D4", target_ref="D6")]

        without = fd.solve_placement(components, netlist, 50.0, 50.0,
                                     max_iterations=200)
        with_keepout = fd.solve_placement(components, netlist, 50.0, 50.0,
                                          max_iterations=200, keepouts=keepouts)

        x, y = with_keepout.coordinates["R1"]
        r1_radius = math.hypot(1.0, 1.0) / 2.0
        self.assertGreaterEqual(
            math.hypot(x - 25.0, y - 20.0), 8.0 + r1_radius - 1e-6,
            "R1 entered the keepout circle - the infinite repulsion did not fire")

        # Without the keepout R1 must really be in the corridor, otherwise
        # this test measures nothing.
        xf, yf = without.coordinates["R1"]
        self.assertLess(math.hypot(xf - 25.0, yf - 20.0), 8.0 + r1_radius)


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #
PROBLEM = {
    "board": {"width_mm": 40.0, "height_mm": 40.0},
    "components": [
        {"ref": "U1", "width_mm": 2.0, "height_mm": 2.0,
         "category": "power_decoupling"},
        {"ref": "C1", "width_mm": 1.0, "height_mm": 1.0,
         "category": "power_decoupling"},
        {"ref": "U2", "width_mm": 2.0, "height_mm": 2.0,
         "category": "critical_hs"},
    ],
    "nets": [
        {"name": "SIG", "connections": ["U1", "U2"]},
        {"name": "VDD_SENSE", "connections": ["U1", "C1"]},
    ],
    "constraints": [
        {"ref_a": "U1", "ref_b": "C1", "max_mm": 15.0, "note": "decoupling"},
    ],
}


# --------------------------------------------------------------------------- #
# Multi-attempt placement: score, calibrate, pick the best
# --------------------------------------------------------------------------- #
try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    HAVE_CALIBRATION_TIER = True
except ImportError:
    HAVE_CALIBRATION_TIER = False


def _simple_problem():
    components = [fd.Component("U1", 2.0, 2.0), fd.Component("U2", 2.0, 2.0),
                 fd.Component("U3", 2.0, 2.0)]
    netlist = [fd.Net("SIG1", ["U1", "U2"]), fd.Net("SIG2", ["U2", "U3"])]
    return components, netlist


class AngleOffsetTests(unittest.TestCase):
    def test_zero_offset_matches_the_original_spiral(self):
        components = [fd.Component("U1"), fd.Component("U2")]
        default = fd.initial_placement(components, 50.0, 50.0)
        explicit_zero = fd.initial_placement(components, 50.0, 50.0, angle_offset_rad=0.0)
        self.assertEqual(default, explicit_zero)

    def test_nonzero_offset_rotates_the_spiral(self):
        components = [fd.Component("U1"), fd.Component("U2"), fd.Component("U3")]
        base = fd.initial_placement(components, 50.0, 50.0)
        rotated = fd.initial_placement(components, 50.0, 50.0, angle_offset_rad=math.pi / 3.0)
        self.assertNotEqual(base, rotated)

    def test_offset_is_still_fully_deterministic(self):
        components = [fd.Component("U1"), fd.Component("U2")]
        a = fd.initial_placement(components, 50.0, 50.0, angle_offset_rad=1.0)
        b = fd.initial_placement(components, 50.0, 50.0, angle_offset_rad=1.0)
        self.assertEqual(a, b)

    def test_solve_placement_accepts_and_uses_the_offset(self):
        components, netlist = _simple_problem()
        default = fd.solve_placement(components, netlist, 50.0, 50.0)
        offset = fd.solve_placement(components, netlist, 50.0, 50.0,
                                    angle_offset_rad=math.pi / 3.0)
        self.assertNotEqual(default.coordinates, offset.coordinates)


class PlacementScoring(unittest.TestCase):
    def test_score_components_with_no_extras_is_all_zero(self):
        components, netlist = _simple_problem()
        placement = fd.solve_placement(components, netlist, 50.0, 50.0)
        violations, thermal, hs = fd._score_components(placement, netlist)
        self.assertEqual(0, violations)
        self.assertEqual(0.0, thermal)
        self.assertEqual(0.0, hs)

    def test_lower_ratsnest_yields_a_lower_total_score(self):
        """The whole point of the score: a tighter (lower-ratsnest)
        placement must score BETTER (lower)."""
        good = fd.PlacementResult({"U1": (0, 0), "U2": (1, 0)}, 1, True, 0.0, 10.0, 1.0)
        bad = fd.PlacementResult({"U1": (0, 0), "U2": (1, 0)}, 1, True, 0.0, 10.0, 9.0)
        good_score = fd.placement_score(good, [])
        bad_score = fd.placement_score(bad, [])
        self.assertLess(good_score.total_score, bad_score.total_score)

    def test_keepout_violations_dominate_the_score(self):
        """The default keepout weight (50x) must make even ONE violation
        outweigh a large ratsnest improvement - it is documented as a HARD
        gate, the score must reflect that."""
        keepouts = [fd.HighSpeedKeepout("TRD0", 5.0, 0.0, 100.0,
                                        source_ref="D4", target_ref="D6")]
        components = [fd.Component("D4", 2.0, 2.0), fd.Component("D6", 2.0, 2.0),
                     fd.Component("R1", 2.0, 2.0)]
        clean = fd.PlacementResult({"D4": (0, 0), "D6": (10, 0)}, 1, True, 0.0, 10.0, 0.5)
        violating = fd.PlacementResult({"D4": (0, 0), "D6": (10, 0), "R1": (5, 0)},
                                       1, True, 0.0, 10.0, 0.5)
        clean_score = fd.placement_score(clean, [], components, keepouts=keepouts)
        violating_score = fd.placement_score(violating, [], components, keepouts=keepouts)
        self.assertGreater(violating_score.total_score, clean_score.total_score)
        self.assertEqual(1, violating_score.keepout_violation_count)

    def test_thermal_and_hs_score_components_are_populated(self):
        placement = fd.PlacementResult({"U1": (0, 0), "U2": (5, 0)}, 1, True, 0.0, 5.0, 5.0)
        thermal_constraint = fd.DistanceConstraint("U1", "U2", max_mm=3.0)
        net = fd.Net("HDMI0_TX0_P", ["U1", "U2"])
        score = fd.placement_score(placement, [net], thermal_constraints_=[thermal_constraint])
        self.assertGreater(score.thermal_spread_score, 0.0)
        self.assertGreater(score.hs_compactness_score, 0.0)


class ExampleFromPlacementTests(unittest.TestCase):
    def test_matches_score_components_exactly(self):
        """example_from_placement must feed from the SAME _score_components
        placement_score uses - proves the two cannot silently diverge."""
        components, netlist = _simple_problem()
        placement = fd.solve_placement(components, netlist, 50.0, 50.0)
        example = fd.example_from_placement("e1", placement, netlist, real_quality_score=2.0,
                                            components=components)
        violations, thermal, hs = fd._score_components(placement, netlist, components)
        self.assertEqual(violations, example.keepout_violation_count)
        self.assertEqual(round(thermal, 4), example.thermal_spread_score)
        self.assertEqual(round(hs, 4), example.hs_compactness_score)
        self.assertEqual(2.0, example.real_quality_score)


@unittest.skipUnless(HAVE_CALIBRATION_TIER, "numpy/scipy not installed")
class CalibrateScoreWeights(unittest.TestCase):
    def _examples(self, n=8):
        return [
            fd.HistoricalPlacementExample(f"ex{i}", ratsnest_mm=float(i),
                                          keepout_violation_count=0,
                                          thermal_spread_score=0.0,
                                          hs_compactness_score=0.0,
                                          real_quality_score=float(i))
            for i in range(1, n + 1)
        ]

    def test_too_few_examples_raises(self):
        with self.assertRaises(ValueError):
            fd.calibrate_score_weights(self._examples(3))

    def test_minimum_example_count_succeeds(self):
        weights = fd.calibrate_score_weights(self._examples(fd.MIN_CALIBRATION_EXAMPLES))
        self.assertEqual(fd.MIN_CALIBRATION_EXAMPLES, weights.example_count)

    def test_weights_are_never_negative(self):
        """The whole reason NNLS was chosen over plain OLS."""
        weights = fd.calibrate_score_weights(self._examples(10))
        self.assertGreaterEqual(weights.weight_ratsnest, 0.0)
        self.assertGreaterEqual(weights.weight_keepout, 0.0)
        self.assertGreaterEqual(weights.weight_thermal, 0.0)
        self.assertGreaterEqual(weights.weight_hs_compactness, 0.0)

    def test_same_examples_always_produce_the_same_weights(self):
        examples = self._examples(10)
        first = fd.calibrate_score_weights(examples)
        second = fd.calibrate_score_weights(examples)
        self.assertEqual(first, second)

    def test_ratsnest_only_signal_is_learned_as_the_dominant_weight(self):
        """When only ratsnest varies and quality tracks it 1:1, the
        calibrated ratsnest weight must dominate the other three."""
        weights = fd.calibrate_score_weights(self._examples(10))
        self.assertGreater(weights.weight_ratsnest, weights.weight_keepout)
        self.assertGreater(weights.weight_ratsnest, weights.weight_thermal)
        self.assertGreater(weights.weight_ratsnest, weights.weight_hs_compactness)


@unittest.skipIf(HAVE_CALIBRATION_TIER, "numpy/scipy IS installed here")
class CalibrateScoreWeightsMissingTier(unittest.TestCase):
    def test_missing_dependency_raises_an_actionable_error(self):
        examples = [fd.HistoricalPlacementExample(f"ex{i}", float(i), 0, 0.0, 0.0, float(i))
                   for i in range(1, 9)]
        with self.assertRaises(fd.CalibrationDependencyMissing) as ctx:
            fd.calibrate_score_weights(examples)
        self.assertIn("pip install numpy scipy", str(ctx.exception))


class TryMultiplePlacements(unittest.TestCase):
    def test_default_parameter_sets_all_run(self):
        components, netlist = _simple_problem()
        multi = fd.try_multiple_placements(components, netlist, 50.0, 50.0)
        self.assertEqual(len(fd.DEFAULT_PARAMETER_SETS), len(multi.all_results))
        self.assertEqual(len(fd.DEFAULT_PARAMETER_SETS), len(multi.all_scores))

    def test_best_name_is_the_lowest_scoring_one(self):
        components, netlist = _simple_problem()
        multi = fd.try_multiple_placements(components, netlist, 50.0, 50.0)
        best_score = multi.all_scores[multi.best_name].total_score
        self.assertTrue(all(best_score <= s.total_score for s in multi.all_scores.values()))
        self.assertEqual(multi.best_result, multi.all_results[multi.best_name])

    def test_empty_parameter_sets_raises(self):
        components, netlist = _simple_problem()
        with self.assertRaises(ValueError):
            fd.try_multiple_placements(components, netlist, 50.0, 50.0, parameter_sets=[])

    def test_a_custom_parameter_set_is_actually_used(self):
        components, netlist = _simple_problem()
        custom = (fd.PlacementParameterSet("only_one", attraction=0.5, repulsion=0.5),)
        multi = fd.try_multiple_placements(components, netlist, 50.0, 50.0, parameter_sets=custom)
        self.assertEqual(["only_one"], list(multi.all_results.keys()))

    def test_different_start_preset_actually_differs_from_standard(self):
        components, netlist = _simple_problem()
        multi = fd.try_multiple_placements(components, netlist, 50.0, 50.0)
        self.assertNotEqual(multi.all_results["standard"].coordinates,
                            multi.all_results["different_start"].coordinates)


class CliContract(unittest.TestCase):
    def test_missing_positional_is_exit_2(self):
        proc = run()
        self.assertEqual(2, proc.returncode)
        self.assertEqual("", proc.stdout.strip())

    def test_self_test_flag_exits_zero_without_a_problem_file(self):
        self.assertEqual(0, run("--self-test").returncode)

    def test_a_clean_problem_exits_zero_with_json_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, PROBLEM)))
            self.assertEqual(0, proc.returncode, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual("placement-result/v1", report["schema_version"])
            self.assertEqual({"U1", "C1", "U2"},
                             set(report["placement"]["coordinates"]))
            self.assertIs(False, report["signoff"])

    def test_a_violated_constraint_exits_one(self):
        problem = json.loads(json.dumps(PROBLEM))
        problem["constraints"][0]["max_mm"] = 0.01
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, problem)))
            self.assertEqual(1, proc.returncode)
            statuses = {c["check"]: c["status"]
                        for c in json.loads(proc.stdout)["checks"]}
            self.assertEqual("FAIL", statuses["distance_constraints"])

    def test_no_constraints_is_no_coverage_and_exits_one(self):
        problem = json.loads(json.dumps(PROBLEM))
        problem.pop("constraints")
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, problem)))
            self.assertEqual(1, proc.returncode)
            statuses = {c["check"]: c["status"]
                        for c in json.loads(proc.stdout)["checks"]}
            self.assertEqual("NO_COVERAGE", statuses["distance_constraints"])

    def test_the_keepout_gate_only_runs_when_a_trace_width_is_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_problem(tmp, PROBLEM)
            without = json.loads(run(str(path)).stdout)
            self.assertNotIn("high_speed_keepout_violation",
                             [c["check"] for c in without["checks"]])
            with_width = json.loads(
                run(str(path), "--trace-width-mm", "0.2").stdout)
            self.assertIn("high_speed_keepout_violation",
                          [c["check"] for c in with_width["checks"]])

    def test_hierarchical_flag_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = json.loads(
                run(str(write_problem(tmp, PROBLEM)), "--hierarchical").stdout)
            self.assertIs(True, report["params"]["hierarchical"])
            self.assertEqual({"U1", "C1", "U2"},
                             set(report["placement"]["coordinates"]))

    def test_an_unknown_category_is_exit_2(self):
        problem = json.loads(json.dumps(PROBLEM))
        problem["components"][0]["category"] = "nonsense"
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, run(str(write_problem(tmp, problem))).returncode)

    def test_a_missing_board_block_is_exit_2(self):
        problem = json.loads(json.dumps(PROBLEM))
        problem.pop("board")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, run(str(write_problem(tmp, problem))).returncode)

    def test_invalid_json_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{oops", encoding="utf-8")
            self.assertEqual(2, run(str(bad)).returncode)

    def test_json_flag_writes_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "placement.json"
            proc = run(str(write_problem(tmp, PROBLEM)), "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_markdown_flag_writes_the_human_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "placement.md"
            proc = run(str(write_problem(tmp, PROBLEM)), "--markdown", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertIn("SEED placement", out.read_text(encoding="utf-8"))
            json.loads(proc.stdout)     # stdout stays pure JSON

    def test_stdout_is_pure_json_and_diagnostics_go_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, PROBLEM)))
            json.loads(proc.stdout)
            self.assertIn("iterations=", proc.stderr)

    def test_the_cli_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_problem(tmp, PROBLEM)
            self.assertEqual(json.loads(run(str(path)).stdout),
                             json.loads(run(str(path)).stdout))

    def test_self_test_does_not_run_during_a_normal_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(str(write_problem(tmp, PROBLEM)))
            self.assertNotIn("SELF-TEST", proc.stdout)
            self.assertNotIn("SELF-TEST", proc.stderr)


class SchemaFile(unittest.TestCase):
    def test_schema_exists_and_pins_the_version(self):
        schema = json.loads(
            (SCHEMAS / "placement_result.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(fd.SCHEMA_VERSION,
                         schema["properties"]["schema_version"]["const"])


if __name__ == "__main__":
    unittest.main()
