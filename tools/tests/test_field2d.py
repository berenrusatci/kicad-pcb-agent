#!/usr/bin/env python3
"""Tests for the 2-D field solver (common/field2d.py + scripts/field_solver.py).

The load-bearing tests here are the ones that compare against something
KNOWN rather than against a previously recorded output:

  * coax against its exact analytic Z0 - closed, exact, no fitted coefficient
  * er_eff == er in a homogeneous medium - an identity, mesh independent
  * the sparse and SOR solvers against each other - two independent methods
  * grid refinement reducing the error - the property the closed form lacks

That is what makes an in-house solver defensible where the rejected openEMS
FDTD bridge (#49) was not: nothing in this validation is invented.
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

import numpy as np  # noqa: E402

import field_solver as fs  # noqa: E402
from common import field2d as f2  # noqa: E402
from common import si_models as si  # noqa: E402

SCRIPT = SCRIPTS / "field_solver.py"

# Small budgets throughout: these tests must prove behaviour, not burn minutes.
SMALL = {"max_nodes": 60_000}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)


class AnalyticReference(unittest.TestCase):
    """Against the exact coaxial solution - the anchor for everything else."""

    def test_coax_matches_the_exact_analytic_solution(self):
        section = f2.coax(0.5, 1.5, 4.3, cells_across_inner=48,
                          max_nodes=200_000)
        numerical = f2.solve_line(section).z0_ohm()
        exact = f2.coax_analytic_z0_ohm(0.5, 1.5, 4.3)
        deviation = abs(numerical - exact) / exact * 100.0
        self.assertLess(deviation, 2.0,
                        f"numerical={numerical:.4f} exact={exact:.4f} "
                        f"deviation={deviation:.3f}%")

    def test_analytic_coax_z0_matches_its_own_capacitance(self):
        """Z0 = 1/(c*sqrt(C*C0)) must reproduce the analytic Z0 exactly.

        Pure algebra on the two analytic expressions - it pins the identity
        the solver uses, independently of any discretisation.
        """
        a_mm, b_mm, er = 0.4, 2.0, 3.3
        capacitance = f2.coax_analytic_capacitance_f_per_m(a_mm, b_mm, er)
        vacuum = f2.coax_analytic_capacitance_f_per_m(a_mm, b_mm, 1.0)
        from common.units import C_MPS
        derived = 1.0 / (C_MPS * (capacitance * vacuum) ** 0.5)
        self.assertAlmostEqual(derived, f2.coax_analytic_z0_ohm(a_mm, b_mm, er),
                               places=9)

    def test_refining_the_grid_reduces_the_coax_error(self):
        """The property the closed form does not have: error shrinks."""
        exact = f2.coax_analytic_z0_ohm(0.5, 1.5, 4.3)
        errors = []
        for cells in (12, 24, 48):
            section = f2.coax(0.5, 1.5, 4.3, cells_across_inner=cells,
                              max_nodes=200_000)
            value = f2.solve_line(section).z0_ohm()
            errors.append(abs(value - exact) / exact)
        self.assertLess(errors[1], errors[0], errors)
        self.assertLess(errors[2], errors[1], errors)

    def test_coax_rejects_non_physical_radii(self):
        for a_mm, b_mm in ((1.0, 1.0), (2.0, 1.0), (-1.0, 2.0)):
            with self.subTest(a=a_mm, b=b_mm):
                with self.assertRaises(f2.FieldSolverError):
                    f2.coax_analytic_z0_ohm(a_mm, b_mm, 4.3)


class PhysicalInvariants(unittest.TestCase):
    """Identities that hold regardless of the mesh."""

    def test_homogeneous_medium_gives_er_eff_exactly_er(self):
        section = f2.coax(0.5, 1.5, 4.3, cells_across_inner=16, **SMALL)
        self.assertAlmostEqual(f2.solve_line(section).er_eff(), 4.3, places=6)

    def test_stripline_is_homogeneous_so_er_eff_equals_er(self):
        """Different geometry, same identity - this one is grid-aligned."""
        section = f2.stripline(0.15, 0.035, 0.3, 4.3, cells_per_feature=3,
                               max_nodes=120_000)
        self.assertAlmostEqual(f2.solve_line(section).er_eff(), 4.3, places=5)

    def test_microstrip_er_eff_lies_between_air_and_laminate(self):
        section = f2.microstrip(0.2, 0.035, 0.1, 4.3, cells_per_feature=3,
                                max_nodes=120_000)
        er_eff = f2.solve_line(section).er_eff()
        self.assertGreater(er_eff, 1.0)
        self.assertLess(er_eff, 4.3)

    def test_capacitance_matrix_is_symmetric(self):
        section = f2.coupled_microstrip(0.1, 0.15, 0.035, 0.1, 4.3,
                                        cells_per_feature=3, max_nodes=120_000)
        matrix = f2.solve_line(section).capacitance_matrix_f_per_m
        self.assertTrue(np.allclose(matrix, matrix.T, rtol=1e-9), matrix)

    def test_a_symmetric_pair_has_equal_self_capacitances(self):
        section = f2.coupled_microstrip(0.1, 0.15, 0.035, 0.1, 4.3,
                                        cells_per_feature=3, max_nodes=120_000)
        matrix = f2.solve_line(section).capacitance_matrix_f_per_m
        self.assertAlmostEqual(matrix[0, 0] / matrix[1, 1], 1.0, places=6)

    def test_mutual_capacitance_term_is_negative(self):
        """Maxwell off-diagonal coefficients are negative; a positive one
        would mean odd and even modes had been swapped."""
        section = f2.coupled_microstrip(0.1, 0.15, 0.035, 0.1, 4.3,
                                        cells_per_feature=3, max_nodes=120_000)
        matrix = f2.solve_line(section).capacitance_matrix_f_per_m
        self.assertLess(matrix[0, 1], 0.0)


class SolverAgreement(unittest.TestCase):
    """Two independent methods on the same operator must agree."""

    def test_sparse_and_sor_agree_to_solver_tolerance(self):
        section = f2.coax(0.5, 1.5, 4.3, cells_across_inner=10,
                          max_nodes=40_000)
        sparse, sparse_info = section.solve_potential({1: 1.0}, method="sparse")
        loose, sor_info = section.solve_potential(
            {1: 1.0}, method="sor", tol=1e-13, max_sweeps=60_000)
        self.assertEqual("sparse_direct", sparse_info["method"])
        self.assertEqual("sor", sor_info["method"])
        self.assertTrue(sor_info["converged"])
        energy_sparse = section.energy_j_per_m(sparse)
        energy_sor = section.energy_j_per_m(loose)
        self.assertLess(abs(energy_sparse - energy_sor) / energy_sparse, 1e-8)

    def test_sparse_method_is_rejected_when_scipy_is_absent(self):
        section = f2.coax(0.5, 1.5, 4.3, cells_across_inner=8, max_nodes=20_000)
        original = f2.HAVE_SCIPY
        try:
            f2.HAVE_SCIPY = False
            with self.assertRaises(f2.FieldSolverError):
                section.solve_potential({1: 1.0}, method="sparse")
        finally:
            f2.HAVE_SCIPY = original


class Monotonicity(unittest.TestCase):
    def test_wider_trace_lowers_impedance(self):
        narrow = f2.solve_line(f2.microstrip(0.15, 0.035, 0.1, 4.3,
                                             cells_per_feature=3,
                                             max_nodes=120_000)).z0_ohm()
        wide = f2.solve_line(f2.microstrip(0.30, 0.035, 0.1, 4.3,
                                           cells_per_feature=3,
                                           max_nodes=120_000)).z0_ohm()
        self.assertLess(wide, narrow)

    def test_higher_permittivity_lowers_impedance(self):
        low = f2.solve_line(f2.microstrip(0.2, 0.035, 0.1, 3.0,
                                          cells_per_feature=3,
                                          max_nodes=120_000)).z0_ohm()
        high = f2.solve_line(f2.microstrip(0.2, 0.035, 0.1, 4.3,
                                           cells_per_feature=3,
                                           max_nodes=120_000)).z0_ohm()
        self.assertLess(high, low)

    def test_odd_mode_is_below_even_mode(self):
        result = f2.solve_line(f2.coupled_microstrip(
            0.1, 0.15, 0.035, 0.1, 4.3, cells_per_feature=3,
            max_nodes=120_000))
        self.assertLess(result.z_odd_ohm(), result.z_even_ohm())

    def test_wider_gap_raises_differential_impedance(self):
        close = f2.solve_line(f2.coupled_microstrip(
            0.1, 0.1, 0.035, 0.1, 4.3, cells_per_feature=3,
            max_nodes=120_000)).z_diff_ohm()
        far = f2.solve_line(f2.coupled_microstrip(
            0.1, 0.4, 0.035, 0.1, 4.3, cells_per_feature=3,
            max_nodes=150_000)).z_diff_ohm()
        self.assertGreater(far, close)

    def test_soldermask_lowers_impedance(self):
        """Mask adds dielectric to the fringing field, so Z0 must fall.

        The closed form cannot represent this at all, which is one concrete
        thing the numerical solver buys.
        """
        bare = f2.solve_line(f2.microstrip(0.2, 0.035, 0.1, 4.3,
                                           cells_per_feature=3,
                                           max_nodes=150_000)).z0_ohm()
        masked = f2.solve_line(f2.microstrip(0.2, 0.035, 0.1, 4.3,
                                             cells_per_feature=3,
                                             cover_mm=0.02, cover_er=3.5,
                                             max_nodes=150_000)).z0_ohm()
        self.assertLess(masked, bare)


class GridFitting(unittest.TestCase):
    """The bug that made refinement studies report noise instead of error."""

    def test_pitch_is_fitted_so_dimensions_land_on_whole_cells(self):
        pitch, error = f2._fit_grid_pitch([0.035, 0.1, 0.2], 0.035 / 4)
        self.assertLess(error, 0.002, f"pitch={pitch} error={error}")

    def test_rasterisation_error_is_zero_for_an_exact_divisor(self):
        self.assertAlmostEqual(
            f2._rasterisation_error([0.1, 0.2, 0.05], 0.05), 0.0, places=12)

    def test_successive_refinements_solve_the_same_geometry(self):
        """Before the fitter, h snapped to a different value at each pitch and
        the impedance wandered by ~3% instead of converging."""
        values = [
            f2.solve_line(f2.microstrip(0.2, 0.035, 0.1, 4.3,
                                        cells_per_feature=cells,
                                        max_nodes=200_000)).z0_ohm()
            for cells in (3, 6)
        ]
        spread = abs(values[1] - values[0]) / values[0] * 100.0
        self.assertLess(spread, 1.0, f"values={values} spread={spread:.3f}%")

    def test_node_budget_is_respected(self):
        section = f2.microstrip(0.2, 0.035, 0.1, 4.3, cells_per_feature=12,
                                max_nodes=50_000)
        self.assertLessEqual(section.node_count, 90_000, section.node_count)


class ClosedFormDelta(unittest.TestCase):
    """The whole point of #55: measure the bias instead of assuming it."""

    def test_numerical_and_closed_form_differ_measurably(self):
        section = f2.microstrip(0.2, 0.035, 0.1, 4.3, cells_per_feature=3,
                                max_nodes=150_000)
        numerical = f2.solve_line(section).z0_ohm()
        closed = si.z0_microstrip(0.2, 0.035, 0.1, 4.3)
        delta = (numerical - closed) / closed * 100.0
        # Not asserting a specific value - that would pin today's mesh. The
        # claim is that the gap is real and in the band si_models documents.
        self.assertGreater(abs(delta), 1.0,
                           f"numerical={numerical} closed={closed}")
        self.assertLess(abs(delta), 35.0,
                        f"delta={delta:.2f}% outside the documented band")

    def test_both_models_agree_on_direction_with_width(self):
        """They disagree on magnitude, not on physics."""
        widths = (0.15, 0.30)
        numerical = [f2.solve_line(f2.microstrip(w, 0.035, 0.1, 4.3,
                                                 cells_per_feature=3,
                                                 max_nodes=150_000)).z0_ohm()
                     for w in widths]
        closed = [si.z0_microstrip(w, 0.035, 0.1, 4.3) for w in widths]
        self.assertLess(numerical[1], numerical[0])
        self.assertLess(closed[1], closed[0])


class Richardson(unittest.TestCase):
    def test_extrapolation_bounds_a_converging_sequence(self):
        estimate = f2.richardson_estimate(coarse=104.42, fine=104.61)
        self.assertGreater(estimate["extrapolated"], 104.61)
        self.assertLess(estimate["grid_error_percent"], 0.2)

    def test_identical_values_report_zero_grid_error(self):
        estimate = f2.richardson_estimate(coarse=50.0, fine=50.0)
        self.assertAlmostEqual(estimate["grid_error_percent"], 0.0, places=12)


class Guards(unittest.TestCase):
    def test_free_node_on_the_boundary_is_refused(self):
        eps = np.ones((4, 4))
        conductor = np.full((5, 5), f2.FREE, dtype=int)
        conductor[2, 2] = 1
        section = f2.CrossSection(dx_mm=0.01, eps_r=eps, conductor=conductor)
        with self.assertRaises(f2.FieldSolverError):
            section.solve_potential({1: 1.0})

    def test_mismatched_conductor_shape_is_refused(self):
        with self.assertRaises(f2.FieldSolverError):
            f2.CrossSection(dx_mm=0.01, eps_r=np.ones((4, 4)),
                            conductor=np.zeros((4, 4), dtype=int))

    def test_non_positive_permittivity_is_refused(self):
        with self.assertRaises(f2.FieldSolverError):
            f2.CrossSection(dx_mm=0.01, eps_r=np.zeros((4, 4)),
                            conductor=np.zeros((5, 5), dtype=int))

    def test_non_positive_pitch_is_refused(self):
        with self.assertRaises(f2.FieldSolverError):
            f2.CrossSection(dx_mm=0.0, eps_r=np.ones((4, 4)),
                            conductor=np.zeros((5, 5), dtype=int))

    def test_stripline_spacing_must_exceed_thickness(self):
        with self.assertRaises(f2.FieldSolverError):
            f2.stripline(0.15, 0.2, 0.1, 4.3)

    def test_even_odd_modes_need_two_conductors(self):
        section = f2.microstrip(0.2, 0.035, 0.1, 4.3, cells_per_feature=3,
                                **SMALL)
        with self.assertRaises(f2.FieldSolverError):
            f2.solve_line(section).z_diff_ohm()


class SelfTest(unittest.TestCase):
    def test_builtin_self_tests_pass(self):
        fs.run_self_tests()

    def test_fault_injection_rejects_a_wrong_constant(self):
        self.assertTrue(fs._prove_test_not_empty())

    def test_self_test_flag_exits_zero(self):
        proc = run("--self-test")
        self.assertEqual(0, proc.returncode, proc.stderr)


class CliContract(unittest.TestCase):
    def test_missing_geometry_is_exit_2(self):
        proc = run()
        self.assertEqual(2, proc.returncode, proc.stdout)

    def test_missing_required_dimension_is_exit_2(self):
        proc = run("microstrip", "--w", "0.2")     # no --h
        self.assertEqual(2, proc.returncode)
        self.assertIn("--h", proc.stderr)

    def test_stdout_is_pure_json_and_diagnostics_go_to_stderr(self):
        proc = run("microstrip", "--w", "0.2", "--h", "0.1",
                   "--max-nodes", "60000")
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual("field-solver-report/v1", report["schema_version"])
        self.assertIn("z0_ohm", proc.stderr)

    def test_report_carries_model_class_and_signoff_labelling(self):
        report = json.loads(run("microstrip", "--w", "0.2", "--h", "0.1",
                                "--max-nodes", "60000").stdout)
        self.assertEqual("numerical_2d_fdm", report["model_class"])
        self.assertIs(False, report["signoff"])
        self.assertTrue(report["signoff_blockers"])
        self.assertTrue(report["limits"])

    def test_closed_form_comparison_is_reported(self):
        report = json.loads(run("microstrip", "--w", "0.2", "--h", "0.1",
                                "--max-nodes", "60000").stdout)
        comparison = report["closed_form_comparison"]
        self.assertTrue(comparison["available"])
        self.assertIn("numerical_minus_closed_form_percent", comparison)

    def test_coax_reports_its_reference_as_exact(self):
        report = json.loads(run("coax", "--a", "0.5", "--b", "1.5",
                                "--er", "4.3", "--max-nodes", "40000").stdout)
        self.assertIs(True, report["closed_form_comparison"]["exact"])

    def test_target_met_is_green_and_exit_zero(self):
        """Target taken from this solver's own answer, so the test pins the
        gate wiring rather than a particular impedance."""
        probe = json.loads(run("microstrip", "--w", "0.2", "--h", "0.1",
                               "--max-nodes", "60000").stdout)
        target = probe["results"]["z0_ohm"]
        proc = run("microstrip", "--w", "0.2", "--h", "0.1",
                   "--max-nodes", "60000", "--target", str(target))
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual("GREEN", report["status"])
        self.assertIs(True, report["gate"]["within_tolerance"])

    def test_target_missed_is_red_and_exit_one(self):
        proc = run("microstrip", "--w", "0.2", "--h", "0.1",
                   "--max-nodes", "60000", "--target", "500",
                   "--tolerance-percent", "1")
        self.assertEqual(1, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("RED", report["status"])
        self.assertIn("TARGET_NOT_MET", report["reason_codes"])

    def test_no_target_means_no_gate(self):
        report = json.loads(run("microstrip", "--w", "0.2", "--h", "0.1",
                                "--max-nodes", "60000").stdout)
        self.assertIs(False, report["gate"]["applied"])
        self.assertEqual("GREEN", report["status"])

    def test_refine_reports_a_grid_error_bound(self):
        report = json.loads(run("microstrip", "--w", "0.2", "--h", "0.1",
                                "--refine", "--max-nodes", "200000").stdout)
        refinement = report["grid"]["refinement"]
        self.assertIn("grid_error_percent", refinement)
        self.assertGreaterEqual(refinement["refinement_ratio"], 1.0)

    def test_grid_error_ceiling_forces_no_coverage(self):
        """An impossible ceiling must make the tool refuse, not round down."""
        proc = run("microstrip", "--w", "0.2", "--h", "0.1", "--refine",
                   "--max-nodes", "200000",
                   "--max-grid-error-percent", "0.0000001")
        self.assertEqual(1, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual("NO_COVERAGE", report["status"])
        self.assertIn("GRID_ERROR_TOO_LARGE", report["reason_codes"])

    def test_json_flag_writes_the_same_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "field.json"
            proc = run("microstrip", "--w", "0.2", "--h", "0.1",
                       "--max-nodes", "60000", "--json", str(out))
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             json.loads(proc.stdout))

    def test_coupled_geometry_reports_all_four_modal_impedances(self):
        report = json.loads(run("coupled-microstrip", "--w", "0.1", "--s",
                                "0.15", "--h", "0.1", "--max-nodes",
                                "80000").stdout)
        for key in ("z_diff_ohm", "z_odd_ohm", "z_even_ohm", "z_common_ohm"):
            self.assertIn(key, report["results"])
        self.assertLess(report["results"]["z_odd_ohm"],
                        report["results"]["z_even_ohm"])


class SchemaFile(unittest.TestCase):
    def setUp(self):
        self.path = SCHEMAS / "field_solver_report.schema.json"
        self.schema = json.loads(self.path.read_text(encoding="utf-8"))

    def test_schema_exists_and_pins_the_version(self):
        self.assertEqual("field-solver-report/v1",
                         self.schema["properties"]["schema_version"]["const"])

    def test_schema_pins_signoff_false(self):
        self.assertIs(False, self.schema["properties"]["signoff"]["const"])

    def test_schema_pins_the_model_class(self):
        self.assertEqual("numerical_2d_fdm",
                         self.schema["properties"]["model_class"]["const"])

    def test_a_real_report_satisfies_the_required_fields(self):
        report = json.loads(run("microstrip", "--w", "0.2", "--h", "0.1",
                                "--max-nodes", "60000").stdout)
        for key in self.schema["required"]:
            self.assertIn(key, report)

    def test_reason_codes_emitted_are_declared_in_the_schema(self):
        declared = set(
            self.schema["properties"]["reason_codes"]["items"]["enum"])
        emitted = {
            "TARGET_NOT_MET", "SOLVE_NOT_CONVERGED", "GRID_ERROR_TOO_LARGE",
            "REFINEMENT_FAILED", "REFINEMENT_NOT_POSSIBLE",
        }
        self.assertEqual(emitted, declared)


# --------------------------------------------------------------------------- #
# Flexible / rigid-flex stacks
# --------------------------------------------------------------------------- #
# FIXTURE VALUES, NOT DATASHEET VALUES. These permittivities exist to make the
# arithmetic checkable and are deliberately labelled as fixtures in their
# `source` strings. A real flex er depends on the film grade, the adhesive
# system and the frequency it was measured at; this module ships no default
# for any of them, and neither do these tests.
FIXTURE = "TEST FIXTURE - not a datasheet value"


def _base(er: float = 3.40, thickness: float = 0.050) -> f2.FlexLayer:
    return f2.FlexLayer("PI base film", thickness, er, FIXTURE)


def _stack(*, covered: bool = True, adhesive_er: float = 3.20,
           film_er: float = 3.40) -> f2.FlexStackup:
    if not covered:
        return f2.FlexStackup(base=_base(), copper_thickness_mm=0.018)
    return f2.FlexStackup(
        base=_base(), copper_thickness_mm=0.018,
        coverlay_adhesive=f2.FlexLayer("coverlay adhesive", 0.025,
                                       adhesive_er, FIXTURE),
        coverlay_film=f2.FlexLayer("coverlay PI film", 0.025, film_er, FIXTURE))


class FlexStackupContract(unittest.TestCase):
    """What the data structure refuses. Each refusal is a way a flex result
    could have looked authoritative while resting on nothing."""

    def test_a_permittivity_without_a_source_is_refused(self):
        with self.assertRaises(f2.FieldSolverError) as caught:
            f2.FlexLayer("PI", 0.05, 3.4, "   ")
        self.assertIn("source", str(caught.exception))

    def test_er_below_vacuum_is_refused(self):
        with self.assertRaises(f2.FieldSolverError):
            f2.FlexLayer("PI", 0.05, 0.9, FIXTURE)

    def test_non_positive_thickness_is_refused(self):
        for bad in (0.0, -0.01, float("nan")):
            with self.subTest(thickness=bad), \
                    self.assertRaises(f2.FieldSolverError):
                f2.FlexLayer("PI", bad, 3.4, FIXTURE)

    def test_a_coverlay_film_without_its_adhesive_is_refused(self):
        """The film does not touch the copper by itself; a stack that skips
        the adhesive is describing a board nobody can build."""
        with self.assertRaises(f2.FieldSolverError) as caught:
            f2.FlexStackup(
                base=_base(), copper_thickness_mm=0.018,
                coverlay_film=f2.FlexLayer("film", 0.025, 3.4, FIXTURE))
        self.assertIn("adhesive", str(caught.exception))

    def test_adhesive_based_laminates_are_refused_not_approximated(self):
        """Out of scope is an honest answer; treating an adhesive laminate as
        adhesiveless would silently drop a third dielectric."""
        with self.assertRaises(f2.FieldSolverError) as caught:
            f2.FlexStackup(base=_base(), copper_thickness_mm=0.018,
                           adhesiveless=False)
        self.assertIn("NOT modelled", str(caught.exception))

    def test_every_layer_reports_where_its_number_came_from(self):
        sources = _stack().sources()
        self.assertEqual(3, len(sources))
        for line in sources:
            self.assertIn(FIXTURE, line)


class FlexGeometry(unittest.TestCase):
    SHARED = {"cells_per_feature": 3, "max_nodes": 120_000}

    def test_the_coverlay_slows_the_line_down(self):
        """Replacing air above the trace with dielectric must raise er_eff and
        lower Z0. If this ever inverts, the cover is being painted somewhere
        other than over the trace."""
        bare = f2.solve_line(f2.flex_microstrip(0.15, _stack(covered=False),
                                                **self.SHARED))
        covered = f2.solve_line(f2.flex_microstrip(0.15, _stack(),
                                                   **self.SHARED))
        self.assertGreater(covered.er_eff(), bare.er_eff())
        self.assertLess(covered.z0_ohm(), bare.z0_ohm())

    def test_er_eff_stays_between_air_and_the_densest_material(self):
        result = f2.solve_line(f2.flex_microstrip(0.15, _stack(), **self.SHARED))
        self.assertGreater(result.er_eff(), 1.0)
        self.assertLess(result.er_eff(), 3.40)

    def test_the_two_cover_layers_are_not_one_averaged_layer(self):
        """The reason the adhesive and the film are modelled separately.

        Two stacks with the same TOTAL cover thickness but different
        permittivity ordering must not land on the same answer; if they do,
        the solver is averaging them and the separate layers are decoration.
        """
        low_first = f2.solve_line(f2.flex_microstrip(
            0.15, _stack(adhesive_er=2.8, film_er=3.8), **self.SHARED))
        high_first = f2.solve_line(f2.flex_microstrip(
            0.15, _stack(adhesive_er=3.8, film_er=2.8), **self.SHARED))
        self.assertNotAlmostEqual(low_first.er_eff(), high_first.er_eff(),
                                  places=3)
        # The layer nearer the copper carries more of the field, so the stack
        # with the denser material against the trace must be the slower one.
        self.assertGreater(high_first.er_eff(), low_first.er_eff())

    def test_a_cover_layer_too_thin_for_the_grid_is_refused(self):
        """Fault injection, and it found a real defect while being written.

        ``_grid_span`` floors at one cell, so a sub-pitch layer is not
        dropped - it is INFLATED to a whole cell. A 0.1 nm adhesive modelled
        as a 4 um one moves er_eff and nothing in the report says why. The
        first version of this test asserted "vanished" and passed nothing;
        the guard now catches the clamp instead of trusting the span.
        """
        thin = f2.FlexStackup(
            base=_base(), copper_thickness_mm=0.018,
            coverlay_adhesive=f2.FlexLayer("adhesive", 1e-7, 3.2, FIXTURE),
            coverlay_film=f2.FlexLayer("film", 0.025, 3.4, FIXTURE))
        with self.assertRaises(f2.FieldSolverError) as caught:
            f2.flex_microstrip(0.15, thin, cells_per_feature=2,
                               max_nodes=40_000)
        self.assertIn("cannot be represented", str(caught.exception))

    def test_a_bare_flex_trace_says_it_is_the_exposed_window_case(self):
        section = f2.flex_microstrip(0.15, _stack(covered=False), **self.SHARED)
        self.assertTrue(any("NO COVERLAY" in note for note in section.notes))

    def test_the_pair_is_coupled_through_the_coverlay_not_through_air(self):
        """On a differential pair the cover fills the GAP, which is where the
        coupling lives. Solving without it reports an impedance the built
        board will not have."""
        bare = f2.solve_line(f2.coupled_flex_microstrip(
            0.10, 0.15, _stack(covered=False), **self.SHARED))
        covered = f2.solve_line(f2.coupled_flex_microstrip(
            0.10, 0.15, _stack(), **self.SHARED))
        self.assertLess(covered.z_diff_ohm(), bare.z_diff_ohm())

    def test_the_builders_are_registered_so_the_cli_can_reach_them(self):
        for name in ("flex-microstrip", "coupled-flex-microstrip"):
            self.assertIn(name, f2.BUILDERS)


class FlexCliContract(unittest.TestCase):
    """The stackup arrives as a sourced file, never as loose flags."""

    def _run(self, stackup: dict | None, *extra: str):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [sys.executable, str(SCRIPTS / "field_solver.py"),
                    "flex-microstrip", "--w", "0.15", *extra]
            if stackup is not None:
                path = Path(tmp) / "flex.json"
                path.write_text(json.dumps(stackup), encoding="utf-8")
                argv += ["--flex-stackup", str(path)]
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", check=False)
            return proc

    def _good(self) -> dict:
        return {
            "base": {"thickness_mm": 0.05, "er": 3.4, "source": FIXTURE},
            "copper_thickness_mm": 0.018,
            "coverlay_adhesive": {"thickness_mm": 0.025, "er": 3.2,
                                  "source": FIXTURE},
            "coverlay_film": {"thickness_mm": 0.025, "er": 3.4,
                              "source": FIXTURE},
        }

    def test_a_sourced_stackup_solves_and_reports_its_sources(self):
        proc = self._run(self._good())
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        report = json.loads(proc.stdout[proc.stdout.find("{"):])
        notes = " ".join(report["grid"]["notes"])
        self.assertIn("cover stack", notes)
        self.assertIn(FIXTURE, notes)
        self.assertFalse(report["signoff"],
                         "a flex solve is not a sign-off any more than a "
                         "rigid one is")

    def test_a_layer_without_a_source_is_refused_at_the_cli(self):
        payload = self._good()
        del payload["coverlay_adhesive"]["source"]
        proc = self._run(payload)
        self.assertEqual(2, proc.returncode)
        self.assertIn("source", proc.stdout + proc.stderr)

    def test_the_geometry_cannot_run_without_a_stackup_file(self):
        proc = self._run(None)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("flex-stackup", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
