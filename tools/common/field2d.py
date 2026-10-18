"""2-D electrostatic cross-section field solver (finite difference).

WHY THIS EXISTS
---------------
``common/si_models.py`` carries the IPC-2141/Wadell closed forms and says so
in its own docstring: *"the published Polar comparison for this same closed
form spans -29.94% .. +2.41% depending on geometry"*. Every consumer of it is
pinned to ``model_class=exploratory_closed_form`` / ``signoff=False``, and
``zdiff_solver``'s registry entry states that controlled impedance "requires a
2D field solver against the real fab stackup". That solver did not exist, so
the whole impedance chain rested on a systematically biased approximation.

This module is that solver. It integrates Laplace's equation for a piecewise
inhomogeneous dielectric

    div( eps * grad V ) = 0

on a uniform grid, and recovers the per-unit-length capacitance from the field
energy rather than from a fitted formula.

WHY NOT ``atlc``
----------------
The queue item (#55) originally proposed bridging to ``atlc``. ``atlc`` is not
on PATH, has no official Windows distribution, and item #49 already records the
precedent: the openEMS FDTD bridge was REJECTED because it had never been run
against the real engine. Writing a parser for output we cannot produce would
repeat exactly that mistake and violate constraint #3 (no invented syntax).
An in-house solver has the property that makes it acceptable instead: it can
be checked against geometries whose answer is known in closed form EXACTLY,
so no number in its validation is invented. See ``coax_analytic_z0_ohm``.

METHOD
------
Nodes carry the potential; cells carry the permittivity. The five-point
stencil for a variable-coefficient Laplacian uses face permittivities averaged
from the two cells sharing each edge, which keeps the normal component of D
continuous across a dielectric interface without special-casing the interface.

Capacitance comes from the energy, W = 1/2 * integral( eps * |grad V|^2 ):

    W(V) = 1/2 * V^T P V          (P = Maxwell short-circuit capacitance matrix)
    P_ii = 2 * W(e_i)
    P_ij = W(e_i + e_j) - W(e_i) - W(e_j)

Solving the same geometry twice - once with the real dielectrics and once with
every cell set to vacuum - gives C and C0, and for a TEM line

    Z0     = 1 / (c * sqrt(C * C0))
    er_eff = C / C0

both of which are exact identities for a TEM mode, not fits.

KNOWN LIMITS (these are why ``SIGNOFF`` is still False here)
------------------------------------------------------------
- Uniform grid: curved and angled boundaries are staircased.
- Quasi-static / lossless: no dispersion, no conductor loss, no dielectric
  loss tangent, no skin effect, no surface roughness.
- The outer box is a perfect electric wall at a finite distance; for open
  structures (microstrip) that perturbs the answer until the box is far
  enough away. ``box_margin_factor`` controls it and the CLI reports it.
- No fibre-weave / local er variation, no plating thickness variation.
- Single dielectric constant per cell at a single frequency.

A number from this module is a NUMERICAL result, not a measured one. It
replaces a systematically biased closed form with a discretisation-limited
solve whose error can be BOUNDED by refinement (see ``richardson_estimate``),
which the closed form could not do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

from .units import C_MPS

try:                                    # pragma: no cover - import guard
    import scipy.sparse as _sparse
    import scipy.sparse.linalg as _sparse_linalg
    HAVE_SCIPY = True
except ImportError:                     # pragma: no cover - import guard
    _sparse = None                      # type: ignore[assignment]
    _sparse_linalg = None               # type: ignore[assignment]
    HAVE_SCIPY = False

MODEL_CLASS = "numerical_2d_fdm"
SIGNOFF = False

# Vacuum permittivity, CODATA. Used only inside the energy integral; it
# cancels out of er_eff and appears in Z0 solely through c.
EPS0_F_PER_M = 8.8541878128e-12

# Free-space wave impedance, CODATA (mu0 * c). Used by the analytic coax
# reference that validates this solver.
ETA0_OHM = 376.730313668

# Conductor id reserved for the reference/ground conductor. It is held at 0 V
# in every drive vector; a "conductor" array entry below this means "free".
GROUND_ID = 0
FREE = -1


class FieldSolverError(ValueError):
    """The cross-section or the solve request is not well posed."""


# --------------------------------------------------------------------------- #
# Analytic references
#
# These are the reason an in-house solver is defensible: they are EXACT
# solutions of the same equation, so validating against them invents nothing.
# --------------------------------------------------------------------------- #
def coax_analytic_z0_ohm(a_mm: float, b_mm: float, er: float) -> float:
    """Exact Z0 of an ideal coaxial line, ohms.

    Z0 = eta0 / (2*pi*sqrt(er)) * ln(b/a)

    Exact because the coaxial field is purely radial and the boundary is a
    coordinate surface - there is no approximation anywhere in it.
    """
    if not (a_mm > 0 and b_mm > a_mm and er > 0):
        raise FieldSolverError(
            f"need 0 < a < b and er > 0, got a={a_mm}, b={b_mm}, er={er}")
    return ETA0_OHM / (2.0 * math.pi * math.sqrt(er)) * math.log(b_mm / a_mm)


def coax_analytic_capacitance_f_per_m(a_mm: float, b_mm: float,
                                      er: float) -> float:
    """Exact per-unit-length capacitance of an ideal coaxial line, F/m."""
    if not (a_mm > 0 and b_mm > a_mm and er > 0):
        raise FieldSolverError(
            f"need 0 < a < b and er > 0, got a={a_mm}, b={b_mm}, er={er}")
    return 2.0 * math.pi * EPS0_F_PER_M * er / math.log(b_mm / a_mm)


# --------------------------------------------------------------------------- #
# Cross-section
# --------------------------------------------------------------------------- #
@dataclass
class CrossSection:
    """A rasterised 2-D cross-section on a uniform grid.

    ``eps_r`` is per CELL, shape (ny, nx). ``conductor`` is per NODE, shape
    (ny+1, nx+1): ``FREE`` where the potential is solved for, ``GROUND_ID``
    for the reference conductor, and 1..N for driven conductors.

    Node (j, i) sits at (x0 + i*dx, y0 + j*dx). Cell (j, i) spans the square
    between nodes (j, i) and (j+1, i+1).
    """

    dx_mm: float
    eps_r: np.ndarray
    conductor: np.ndarray
    x0_mm: float = 0.0
    y0_mm: float = 0.0
    notes: list[str] = field(default_factory=list)
    # Cached sparse factorisation. Every drive vector for one cross-section
    # shares the SAME matrix - only the right-hand side changes - so factoring
    # once and reusing it turns an n-conductor extraction from n full solves
    # into one factorisation plus n cheap back-substitutions.
    _factorisation: object | None = field(
        default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.dx_mm <= 0 or not math.isfinite(self.dx_mm):
            raise FieldSolverError(f"dx_mm must be positive, got {self.dx_mm}")
        ny, nx = self.eps_r.shape
        if self.conductor.shape != (ny + 1, nx + 1):
            raise FieldSolverError(
                f"conductor must be (ny+1, nx+1) = {(ny + 1, nx + 1)}, "
                f"got {self.conductor.shape}")
        if np.any(self.eps_r <= 0):
            raise FieldSolverError("eps_r must be positive everywhere")

    # -- geometry queries -------------------------------------------------- #
    @property
    def cell_shape(self) -> tuple[int, int]:
        return tuple(self.eps_r.shape)          # type: ignore[return-value]

    @property
    def node_shape(self) -> tuple[int, int]:
        return tuple(self.conductor.shape)      # type: ignore[return-value]

    @property
    def node_count(self) -> int:
        ny, nx = self.node_shape
        return ny * nx

    def signal_ids(self) -> list[int]:
        """Driven conductor ids, ascending. GROUND_ID is excluded."""
        ids = np.unique(self.conductor)
        return sorted(int(v) for v in ids if v > GROUND_ID)

    def vacuum(self) -> "CrossSection":
        """The same geometry with every dielectric replaced by vacuum."""
        return CrossSection(
            dx_mm=self.dx_mm,
            eps_r=np.ones_like(self.eps_r),
            conductor=self.conductor,
            x0_mm=self.x0_mm,
            y0_mm=self.y0_mm,
            notes=list(self.notes) + ["vacuum fill (C0 solve)"],
        )

    # -- solve ------------------------------------------------------------- #
    def _face_coefficients(self) -> tuple[np.ndarray, ...]:
        """Averaged face permittivities for every INTERIOR node.

        For node (j, i) the edge to its east neighbour is shared by cells
        (j-1, i) and (j, i); averaging those two keeps the flux balance
        correct across a horizontal dielectric boundary.
        """
        eps = self.eps_r
        ny, nx = eps.shape
        if ny < 2 or nx < 2:
            raise FieldSolverError("grid too small to have interior nodes")
        a_e = 0.5 * (eps[0:ny - 1, 1:nx] + eps[1:ny, 1:nx])
        a_w = 0.5 * (eps[0:ny - 1, 0:nx - 1] + eps[1:ny, 0:nx - 1])
        a_n = 0.5 * (eps[0:ny - 1, 0:nx - 1] + eps[0:ny - 1, 1:nx])
        a_s = 0.5 * (eps[1:ny, 0:nx - 1] + eps[1:ny, 1:nx])
        return a_e, a_w, a_n, a_s

    def _free_mask(self) -> np.ndarray:
        """Nodes whose potential is unknown. They must all be interior."""
        free = self.conductor == FREE
        if (free[0, :].any() or free[-1, :].any()
                or free[:, 0].any() or free[:, -1].any()):
            raise FieldSolverError(
                "the outer boundary must be fully constrained; a free node on "
                "the edge leaves the potential undetermined")
        return free

    def _build_system(self):
        """Assemble and factorise the sparse operator once per cross-section."""
        if self._factorisation is not None:
            return self._factorisation

        free = self._free_mask()
        unknowns = int(np.count_nonzero(free))
        if unknowns == 0:
            raise FieldSolverError("no free nodes: the geometry is all metal")

        index = np.full(free.shape, -1, dtype=np.int64)
        index[free] = np.arange(unknowns)

        a_e, a_w, a_n, a_s = self._face_coefficients()
        rows_j, cols_i = np.nonzero(free)
        coefficient_j, coefficient_i = rows_j - 1, cols_i - 1

        east = a_e[coefficient_j, coefficient_i]
        west = a_w[coefficient_j, coefficient_i]
        north = a_n[coefficient_j, coefficient_i]
        south = a_s[coefficient_j, coefficient_i]

        centre = index[rows_j, cols_i]
        rows = [centre]
        cols = [centre]
        data = [east + west + north + south]

        # Where a neighbour is a conductor its known potential moves to the
        # right-hand side. ``boundary_weight`` records, per unknown and per
        # neighbour, which conductor supplies it and with what coefficient.
        boundary: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        neighbours = (
            (0, +1, east), (0, -1, west), (-1, 0, north), (+1, 0, south),
        )
        for delta_j, delta_i, weight in neighbours:
            neighbour_j, neighbour_i = rows_j + delta_j, cols_i + delta_i
            is_free = free[neighbour_j, neighbour_i]
            rows.append(centre[is_free])
            cols.append(index[neighbour_j[is_free], neighbour_i[is_free]])
            data.append(-weight[is_free])
            fixed_here = ~is_free
            if fixed_here.any():
                boundary.append((
                    centre[fixed_here],
                    self.conductor[neighbour_j[fixed_here],
                                   neighbour_i[fixed_here]],
                    weight[fixed_here],
                ))

        matrix = _sparse.coo_matrix(
            (np.concatenate(data), (np.concatenate(rows), np.concatenate(cols))),
            shape=(unknowns, unknowns),
        ).tocsc()

        self._factorisation = {
            "solve": _sparse_linalg.factorized(matrix),
            "index": index,
            "free": free,
            "unknowns": unknowns,
            "boundary": boundary,
        }
        return self._factorisation

    def _solve_sparse(self, drive: Mapping[int, float]
                      ) -> tuple[np.ndarray, dict[str, object]]:
        system = self._build_system()
        rhs = np.zeros(system["unknowns"], dtype=float)
        for centre, conductor_ids, weight in system["boundary"]:
            volts = np.zeros(conductor_ids.shape, dtype=float)
            for cid, value in drive.items():
                volts[conductor_ids == cid] = float(value)
            np.add.at(rhs, centre, weight * volts)

        solution = system["solve"](rhs)

        potential = np.zeros(self.node_shape, dtype=float)
        for cid, value in drive.items():
            potential[self.conductor == cid] = float(value)
        potential[system["free"]] = solution
        return potential, {"method": "sparse_direct", "sweeps": 1,
                           "converged": True, "unknowns": system["unknowns"]}

    def solve_potential(
        self,
        drive: Mapping[int, float],
        *,
        tol: float = 1e-9,
        max_sweeps: int = 20000,
        check_every: int = 20,
        omega: float | None = None,
        initial: np.ndarray | None = None,
        method: str = "auto",
    ) -> tuple[np.ndarray, dict[str, object]]:
        """Solve for the potential. Returns (potential, convergence info).

        ``drive`` maps conductor id -> volts. Ids not listed (including
        GROUND_ID) are held at 0 V.

        ``method='auto'`` uses the sparse direct factorisation when SciPy is
        importable and falls back to red-black SOR otherwise. The fallback is
        kept rather than making SciPy mandatory because it is what proves the
        two paths agree - ``test_field2d`` runs the same geometry through both
        and requires the answers to match.
        """
        for cid in drive:
            if cid == FREE:
                raise FieldSolverError("cannot drive the FREE marker")

        if method not in {"auto", "sparse", "sor"}:
            raise FieldSolverError(f"unknown method {method!r}")
        if method == "sparse" and not HAVE_SCIPY:
            raise FieldSolverError("method='sparse' requires SciPy")
        if method == "sparse" or (method == "auto" and HAVE_SCIPY):
            return self._solve_sparse(drive)
        return self._solve_sor(
            drive, tol=tol, max_sweeps=max_sweeps, check_every=check_every,
            omega=omega, initial=initial)

    def _solve_sor(
        self,
        drive: Mapping[int, float],
        *,
        tol: float = 1e-9,
        max_sweeps: int = 20000,
        check_every: int = 20,
        omega: float | None = None,
        initial: np.ndarray | None = None,
    ) -> tuple[np.ndarray, dict[str, object]]:
        """Red-black SOR. The SciPy-free reference path."""
        ny_n, nx_n = self.node_shape
        fixed = self.conductor != FREE

        potential = np.zeros((ny_n, nx_n), dtype=float)
        if initial is not None:
            if initial.shape != (ny_n, nx_n):
                raise FieldSolverError(
                    f"initial guess shape {initial.shape} != {(ny_n, nx_n)}")
            potential = np.array(initial, dtype=float)
        # Fixed nodes are re-imposed AFTER any initial guess, never before,
        # so a seeded guess can never silently override a boundary condition.
        potential[fixed] = 0.0
        for cid, volts in drive.items():
            if cid == FREE:
                raise FieldSolverError("cannot drive the FREE marker")
            potential[self.conductor == cid] = float(volts)

        a_e, a_w, a_n, a_s = self._face_coefficients()
        denominator = a_e + a_w + a_n + a_s
        if np.any(denominator <= 0):
            raise FieldSolverError("degenerate stencil: zero face permittivity")

        free_interior = ~fixed[1:ny_n - 1, 1:nx_n - 1]
        rows, cols = np.indices(free_interior.shape)
        parity = (rows + cols) % 2
        red = free_interior & (parity == 0)
        black = free_interior & (parity == 1)

        if omega is None:
            # Optimal SOR factor for a Dirichlet Laplacian of this size. Using
            # the larger dimension is the conservative choice: it under-relaxes
            # slightly rather than overshooting into divergence.
            largest = max(ny_n, nx_n)
            omega = 2.0 / (1.0 + math.sin(math.pi / largest))
        omega = float(np.clip(omega, 1.0, 1.99))

        interior = (slice(1, ny_n - 1), slice(1, nx_n - 1))
        previous_energy = None
        sweeps = 0
        converged = False

        for sweeps in range(1, max_sweeps + 1):
            for colour in (red, black):
                gauss_seidel = (
                    a_e * potential[1:ny_n - 1, 2:nx_n]
                    + a_w * potential[1:ny_n - 1, 0:nx_n - 2]
                    + a_n * potential[0:ny_n - 2, 1:nx_n - 1]
                    + a_s * potential[2:ny_n, 1:nx_n - 1]
                ) / denominator
                block = potential[interior]
                potential[interior] = np.where(
                    colour, block + omega * (gauss_seidel - block), block)

            if sweeps % check_every == 0 or sweeps == max_sweeps:
                # Energy is the quantity we actually report, and it converges
                # monotonically and faster than the pointwise field, so it is
                # the honest thing to test. A pointwise residual test would
                # declare victory later for no gain in the reported number.
                energy = self.energy_j_per_m(potential)
                if previous_energy is not None:
                    scale = max(abs(energy), 1e-30)
                    if abs(energy - previous_energy) / scale < tol:
                        converged = True
                        break
                previous_energy = energy

        info = {
            "method": "sor",
            "sweeps": sweeps,
            "omega": omega,
            "converged": converged,
            "tol": tol,
        }
        return potential, info

    # -- energy ------------------------------------------------------------ #
    def energy_j_per_m(self, potential: np.ndarray) -> float:
        """Electrostatic energy per unit length, J/m.

        W = 1/2 * integral( eps0 * eps_r * |grad V|^2 ) dA

        The field in a cell is the average of the two finite differences along
        each axis, which is the standard bilinear-element energy and is second
        order accurate.
        """
        ny, nx = self.cell_shape
        dx_m = self.dx_mm * 1e-3

        e_x = -((potential[0:ny, 1:nx + 1] - potential[0:ny, 0:nx])
                + (potential[1:ny + 1, 1:nx + 1] - potential[1:ny + 1, 0:nx])
                ) / (2.0 * dx_m)
        e_y = -((potential[1:ny + 1, 0:nx] - potential[0:ny, 0:nx])
                + (potential[1:ny + 1, 1:nx + 1] - potential[0:ny, 1:nx + 1])
                ) / (2.0 * dx_m)

        density = self.eps_r * (e_x ** 2 + e_y ** 2)
        return 0.5 * EPS0_F_PER_M * float(np.sum(density)) * dx_m * dx_m


# --------------------------------------------------------------------------- #
# Capacitance extraction
# --------------------------------------------------------------------------- #
def maxwell_capacitance_matrix(
    section: CrossSection,
    *,
    tol: float = 1e-9,
    max_sweeps: int = 20000,
) -> tuple[np.ndarray, dict[str, object]]:
    """Maxwell short-circuit capacitance matrix P, F/m, for the signal ids.

    Uses the energy identity W = 1/2 V^T P V:
        P_ii = 2 W(e_i)
        P_ij = W(e_i + e_j) - W(e_i) - W(e_j)
    which needs n + n(n-1)/2 solves and no charge-contour integration - the
    latter is where a naive FDM extraction usually loses its accuracy, because
    the contour has to be placed in the staircased region next to a conductor.
    """
    ids = section.signal_ids()
    if not ids:
        raise FieldSolverError(
            "cross-section has no driven conductor (ids > GROUND_ID)")

    self_energy: dict[int, float] = {}
    total_sweeps = 0
    all_converged = True

    for cid in ids:
        potential, info = section.solve_potential(
            {cid: 1.0}, tol=tol, max_sweeps=max_sweeps)
        self_energy[cid] = section.energy_j_per_m(potential)
        total_sweeps += int(info["sweeps"])
        all_converged &= bool(info["converged"])

    size = len(ids)
    matrix = np.zeros((size, size), dtype=float)
    for index, cid in enumerate(ids):
        matrix[index, index] = 2.0 * self_energy[cid]

    for a_index in range(size):
        for b_index in range(a_index + 1, size):
            id_a, id_b = ids[a_index], ids[b_index]
            potential, info = section.solve_potential(
                {id_a: 1.0, id_b: 1.0}, tol=tol, max_sweeps=max_sweeps)
            pair_energy = section.energy_j_per_m(potential)
            mutual = pair_energy - self_energy[id_a] - self_energy[id_b]
            matrix[a_index, b_index] = mutual
            matrix[b_index, a_index] = mutual
            total_sweeps += int(info["sweeps"])
            all_converged &= bool(info["converged"])

    info = {
        "conductor_ids": ids,
        "total_sweeps": total_sweeps,
        "converged": all_converged,
    }
    return matrix, info


@dataclass
class LineResult:
    """Per-unit-length parameters of one cross-section."""

    capacitance_matrix_f_per_m: np.ndarray
    vacuum_capacitance_matrix_f_per_m: np.ndarray
    conductor_ids: list[int]
    converged: bool
    total_sweeps: int
    node_count: int
    dx_mm: float

    # -- single-ended ------------------------------------------------------ #
    def z0_ohm(self, index: int = 0) -> float:
        capacitance = self.capacitance_matrix_f_per_m[index, index]
        vacuum = self.vacuum_capacitance_matrix_f_per_m[index, index]
        return _z0_from_capacitance(capacitance, vacuum)

    def er_eff(self, index: int = 0) -> float:
        capacitance = self.capacitance_matrix_f_per_m[index, index]
        vacuum = self.vacuum_capacitance_matrix_f_per_m[index, index]
        return capacitance / vacuum

    # -- coupled ----------------------------------------------------------- #
    def _mode_capacitances(self) -> tuple[float, float, float, float]:
        if len(self.conductor_ids) < 2:
            raise FieldSolverError(
                "even/odd modes need exactly two driven conductors")
        p11 = self.capacitance_matrix_f_per_m[0, 0]
        p12 = self.capacitance_matrix_f_per_m[0, 1]
        q11 = self.vacuum_capacitance_matrix_f_per_m[0, 0]
        q12 = self.vacuum_capacitance_matrix_f_per_m[0, 1]
        # Both conductors at +1 V: the mutual term carries no charge.
        # One at +1 V and the other at -1 V: the mutual term doubles up.
        return (p11 + p12, p11 - p12, q11 + q12, q11 - q12)

    def z_even_ohm(self) -> float:
        c_even, _, c0_even, _ = self._mode_capacitances()
        return _z0_from_capacitance(c_even, c0_even)

    def z_odd_ohm(self) -> float:
        _, c_odd, _, c0_odd = self._mode_capacitances()
        return _z0_from_capacitance(c_odd, c0_odd)

    def z_diff_ohm(self) -> float:
        return 2.0 * self.z_odd_ohm()

    def z_common_ohm(self) -> float:
        return 0.5 * self.z_even_ohm()

    def er_eff_odd(self) -> float:
        _, c_odd, _, c0_odd = self._mode_capacitances()
        return c_odd / c0_odd

    def er_eff_even(self) -> float:
        c_even, _, c0_even, _ = self._mode_capacitances()
        return c_even / c0_even


def _z0_from_capacitance(capacitance: float, vacuum: float) -> float:
    """Z0 = 1 / (c * sqrt(C * C0)); exact for a TEM mode."""
    if capacitance <= 0 or vacuum <= 0:
        raise FieldSolverError(
            f"non-physical capacitance: C={capacitance}, C0={vacuum}")
    return 1.0 / (C_MPS * math.sqrt(capacitance * vacuum))


def solve_line(
    section: CrossSection,
    *,
    tol: float = 1e-9,
    max_sweeps: int = 20000,
) -> LineResult:
    """Full extraction: dielectric solve + vacuum solve -> LineResult."""
    filled, filled_info = maxwell_capacitance_matrix(
        section, tol=tol, max_sweeps=max_sweeps)
    vacuum, vacuum_info = maxwell_capacitance_matrix(
        section.vacuum(), tol=tol, max_sweeps=max_sweeps)
    return LineResult(
        capacitance_matrix_f_per_m=filled,
        vacuum_capacitance_matrix_f_per_m=vacuum,
        conductor_ids=list(filled_info["conductor_ids"]),   # type: ignore[arg-type]
        converged=bool(filled_info["converged"]) and bool(vacuum_info["converged"]),
        total_sweeps=int(filled_info["total_sweeps"])       # type: ignore[arg-type]
        + int(vacuum_info["total_sweeps"]),                 # type: ignore[arg-type]
        node_count=section.node_count,
        dx_mm=section.dx_mm,
    )


def richardson_estimate(coarse: float, fine: float,
                        refinement_ratio: float = 2.0,
                        order: float = 2.0) -> dict[str, float]:
    """Extrapolate two grid resolutions and BOUND the discretisation error.

    This is the property the closed form never had: its -29.94%..+2.41% bias
    is systematic and invisible from a single run, whereas a discretisation
    error shrinks predictably with the grid and can therefore be reported.
    """
    denominator = refinement_ratio ** order - 1.0
    extrapolated = fine + (fine - coarse) / denominator
    scale = max(abs(extrapolated), 1e-30)
    return {
        "coarse": coarse,
        "fine": fine,
        "extrapolated": extrapolated,
        "grid_error_percent": abs(fine - extrapolated) / scale * 100.0,
        "coarse_to_fine_delta_percent":
            abs(fine - coarse) / max(abs(fine), 1e-30) * 100.0,
    }


# --------------------------------------------------------------------------- #
# Geometry builders
#
# Every builder rasterises onto a uniform grid and records what it had to
# round, because a silently rounded trace width is a silently wrong impedance.
# --------------------------------------------------------------------------- #
def _grid_span(length_mm: float, dx_mm: float) -> int:
    """Cells spanning ``length_mm``, at least one."""
    return max(1, int(round(length_mm / dx_mm)))


def _rasterisation_error(dimensions: Sequence[float], dx: float) -> float:
    """Worst relative error when every dimension snaps to a whole cell."""
    worst = 0.0
    for value in dimensions:
        cells = max(1, round(value / dx))
        worst = max(worst, abs(cells * dx - value) / value)
    return worst


def _fit_grid_pitch(dimensions: Sequence[float], dx_max: float, *,
                    dx_min: float = 0.0,
                    max_error: float = 0.002, samples: int = 3000
                    ) -> tuple[float, float]:
    """Largest pitch <= ``dx_max`` on which every dimension lands near a cell.

    WHY THIS IS NOT OPTIONAL. Rounding each dimension independently makes the
    RASTERISED geometry a function of the pitch: at one pitch a 0.100 mm
    substrate becomes 11 cells of 8.75 um = 0.09625 mm, a -3.75% error. Refine
    the grid and it snaps somewhere else. The measured impedance then wanders
    with the mesh instead of converging, and a refinement study - the only
    thing that lets this module BOUND its own error - reports noise. Fitting
    the pitch to the geometry first makes successive refinements solve the
    same structure, which is what makes ``richardson_estimate`` meaningful.
    """
    positive = [float(value) for value in dimensions if value and value > 0]
    if not positive:
        raise FieldSolverError("no positive dimension to fit the grid to")

    # ``dx_min`` is the floor imposed by the node budget. Searching below it
    # would return a pitch the caller cannot afford, which is how the budget
    # used to be advertised and then quietly exceeded.
    floor = max(dx_min, dx_max * 0.2)
    if floor >= dx_max:
        return dx_max, _rasterisation_error(positive, dx_max)

    best_pitch, best_error = dx_max, _rasterisation_error(positive, dx_max)
    for step in range(samples):
        # Sweep downwards from dx_max; the first pitch inside tolerance is the
        # coarsest one that works, i.e. the cheapest acceptable grid.
        pitch = dx_max - (dx_max - floor) * step / (samples - 1)
        if pitch <= 0:
            break
        error = _rasterisation_error(positive, pitch)
        if error <= max_error:
            return pitch, error
        if error < best_error:
            best_pitch, best_error = pitch, error
    return best_pitch, best_error


def _auto_dx(features_mm: Iterable[float], cells_per_feature: int,
             extent_mm: tuple[float, float], max_nodes: int,
             notes: list[str]) -> float:
    """Grid pitch: resolve the smallest feature, fit the geometry, then clamp."""
    positive = [f for f in features_mm if f and f > 0]
    if not positive:
        raise FieldSolverError("no positive feature size to resolve")
    smallest = min(positive)
    dx_max = smallest / cells_per_feature

    width_mm, height_mm = extent_mm
    # The pitch at which the node count exactly reaches the budget. Both the
    # requested pitch and anything the geometry fitter might prefer are held
    # at or above it, so ``max_nodes`` is a real ceiling rather than a hint.
    dx_budget = math.sqrt(width_mm * height_mm / max(max_nodes, 1))
    if dx_budget > dx_max:
        notes.append(
            f"grid pitch raised {dx_budget / dx_max:.2f}x to stay under "
            f"max_nodes={max_nodes}; smallest feature now spans "
            f"{smallest / dx_budget:.1f} cells")
        dx_max = dx_budget

    dx, error = _fit_grid_pitch(positive, dx_max, dx_min=dx_budget)
    notes.append(
        f"grid pitch {dx * 1000:.4f} um; worst dimension rasterisation error "
        f"{error * 100:.3f}%")
    if error > 0.01:
        notes.append(
            "RASTERISATION WARNING: a dimension is more than 1% off its "
            "requested value; the solved geometry is not the asked-for one")
    return dx


def coax(a_mm: float, b_mm: float, er: float, *,
         cells_across_inner: int = 24,
         max_nodes: int = 400_000) -> CrossSection:
    """Coaxial line: THE validation geometry.

    The outer conductor is the boundary itself, so unlike an open structure
    there is no box-truncation error to argue about - the only error left is
    the staircasing of the two circles. That makes the analytic comparison a
    clean measurement of the solver's discretisation error.
    """
    if not (a_mm > 0 and b_mm > a_mm and er > 0):
        raise FieldSolverError(
            f"need 0 < a < b and er > 0, got a={a_mm}, b={b_mm}, er={er}")

    notes: list[str] = []
    dx = _auto_dx([a_mm], cells_across_inner, (2 * b_mm, 2 * b_mm),
                  max_nodes, notes)

    half = _grid_span(b_mm, dx)
    nx = ny = 2 * half
    eps_r = np.full((ny, nx), float(er))
    conductor = np.full((ny + 1, nx + 1), FREE, dtype=int)

    node_axis = (np.arange(nx + 1) - half) * dx
    x_grid, y_grid = np.meshgrid(node_axis, node_axis)
    radius = np.hypot(x_grid, y_grid)

    conductor[radius >= b_mm] = GROUND_ID
    conductor[radius <= a_mm] = 1

    if not np.any(conductor == 1):
        raise FieldSolverError(
            "inner conductor vanished at this grid pitch; raise "
            "cells_across_inner or max_nodes")

    notes.append(f"coax a={a_mm} mm, b={b_mm} mm, er={er}")
    notes.append("circles are staircased onto the uniform grid")
    return CrossSection(dx_mm=dx, eps_r=eps_r, conductor=conductor,
                        x0_mm=-half * dx, y0_mm=-half * dx, notes=notes)


def _planar_section(
    *,
    trace_widths_mm: Sequence[float],
    gap_mm: float,
    thickness_mm: float,
    substrate_mm: float,
    er: float,
    top_ground: bool,
    trace_offset_mm: float,
    box_margin_factor: float,
    cells_per_feature: int,
    max_nodes: int,
    cover_mm: float,
    cover_er: float,
    label: str,
    cover_stack: Sequence[tuple[float, float]] | None = None,
    base_stack: Sequence[tuple[float, float]] | None = None,
) -> CrossSection:
    """Shared rasteriser for microstrip / stripline, 1 or 2 traces.

    ``substrate_mm`` is the laminate thickness below the trace; ``top_ground``
    adds a second reference plane above (stripline). ``trace_offset_mm`` is the
    distance from the trace top to that upper plane.

    ``cover_stack`` is ``[(thickness_mm, er), ...]`` bottom-up, for stacks
    whose cover is NOT one material. A flex coverlay is exactly that: an
    acrylic or epoxy adhesive against the copper and a polyimide film on top
    of it, with different permittivities. Collapsing the two into one
    ``cover_er`` would be an averaged material nobody supplied and nobody
    could cite. When ``cover_stack`` is given, ``cover_mm``/``cover_er`` are
    ignored; the single-layer form stays for soldermask, which really is one
    material.
    """
    layers: list[tuple[float, float]] = (
        [(float(t), float(e)) for t, e in cover_stack] if cover_stack
        else ([(cover_mm, cover_er)] if cover_mm > 0 else []))
    for thick, permittivity in layers:
        if thick <= 0 or not math.isfinite(thick):
            raise FieldSolverError(
                f"cover layer thickness must be positive, got {thick}")
        if permittivity < 1.0 or not math.isfinite(permittivity):
            raise FieldSolverError(
                f"cover layer er must be >= 1 (vacuum), got {permittivity}")
    below: list[tuple[float, float]] = (
        [(float(t), float(e)) for t, e in base_stack] if base_stack else [])
    for thick, permittivity in below:
        if thick <= 0 or not math.isfinite(thick):
            raise FieldSolverError(
                f"base layer thickness must be positive, got {thick}")
        if permittivity < 1.0 or not math.isfinite(permittivity):
            raise FieldSolverError(
                f"base layer er must be >= 1 (vacuum), got {permittivity}")
    if below and abs(sum(t for t, _ in below) - substrate_mm) > 1e-9:
        # substrate_mm is what the box geometry was sized from; a base stack
        # that does not add up to it would put the trace at a height the
        # rasteriser did not reserve, and the answer would be for a board
        # nobody described.
        raise FieldSolverError(
            f"base_stack sums to {sum(t for t, _ in below)} mm but "
            f"substrate_mm is {substrate_mm}; they must agree")

    cover_total = sum(thick for thick, _ in layers)

    total_trace = sum(trace_widths_mm) + (gap_mm if len(trace_widths_mm) > 1 else 0.0)
    features = [thickness_mm, substrate_mm, *trace_widths_mm]
    if len(trace_widths_mm) > 1:
        features.append(gap_mm)
    features.extend(thick for thick, _ in layers)
    features.extend(float(t) for t, _ in (base_stack or ()))

    notes: list[str] = []
    if top_ground:
        height_mm = substrate_mm + thickness_mm + trace_offset_mm
        width_mm = total_trace + 2.0 * box_margin_factor * substrate_mm
    else:
        margin = box_margin_factor * (substrate_mm + thickness_mm)
        # The cover must fit UNDER the lid with air left above it, otherwise
        # the top electric wall lands inside the dielectric and the fringing
        # field is clamped by the box instead of by the geometry.
        height_mm = substrate_mm + thickness_mm + cover_total + margin
        width_mm = total_trace + 2.0 * margin

    dx = _auto_dx(features, cells_per_feature, (width_mm, height_mm),
                  max_nodes, notes)

    nx = _grid_span(width_mm, dx)
    ny = _grid_span(height_mm, dx)
    sub_cells = _grid_span(substrate_mm, dx)
    trace_cells = _grid_span(thickness_mm, dx)
    if sub_cells + trace_cells >= ny:
        raise FieldSolverError(
            "substrate plus trace fills the whole box; raise box_margin_factor")

    eps_r = np.ones((ny, nx), dtype=float)
    if top_ground:
        # Stripline is buried: the laminate fills the WHOLE space between the
        # two planes, above the trace as well as below it. Filling only up to
        # the trace left air above it, which turned a homogeneous line into a
        # mixed-dielectric one and dropped er_eff from 4.3 to 2.45 - caught by
        # the er_eff == er identity test, which is exactly what it is for.
        eps_r[:, :] = float(er)
    elif below:
        # Asymmetric / multi-layer base: painted bottom-up from the reference
        # plane. A flex stack is asymmetric by construction - polyimide below,
        # adhesive and film above - and collapsing the base to one er would
        # average materials nobody supplied, the same refusal the cover stack
        # already makes.
        cursor = 0
        for index, (thick, permittivity) in enumerate(below):
            exact = thick / dx
            if round(exact) < 1:
                raise FieldSolverError(
                    f"base layer {index + 1} is {thick} mm but the grid pitch "
                    f"is {dx:.6f} mm, so it cannot be represented: the "
                    "rasteriser would round it up to a full cell and the "
                    "answer would move without saying why")
            top = min(sub_cells, cursor + _grid_span(thick, dx))
            eps_r[cursor:top, :] = float(permittivity)
            cursor = top
        if cursor < sub_cells:
            # Rounding left a sliver under the trace; give it to the layer
            # nearest the conductor rather than to air, and say so.
            eps_r[cursor:sub_cells, :] = float(below[-1][1])
            notes.append(
                f"base stack rasterisation left {sub_cells - cursor} cell(s); "
                f"assigned to the top base layer (er={below[-1][1]})")
        notes.append("base stack (bottom-up): " + " + ".join(
            f"{t} mm er={e}" for t, e in below))
    else:
        eps_r[0:sub_cells, :] = float(er)
    if layers and not top_ground:
        # A cover sits on the laminate BESIDE the trace and over the top of
        # it. Modelling only the strip above the trace would understate its
        # effect on the fringing field, which is where a cover actually bites.
        #
        # The stack is painted bottom-up from the top of the laminate, so the
        # first layer wraps the trace and the ones above it sit on that. For a
        # flex coverlay this is the physical order: adhesive against the
        # copper, film on the adhesive.
        cursor = sub_cells
        for index, (thick, permittivity) in enumerate(layers):
            # _grid_span floors at ONE cell, so a layer thinner than the pitch
            # is not dropped - it is INFLATED to a whole cell. That is the
            # worse failure: a 0.1 um adhesive silently modelled as a 4 um one
            # moves er_eff and nothing in the report says why. Detect the
            # clamp before it happens rather than trusting the span.
            exact_cells = thick / dx
            if round(exact_cells) < 1:
                raise FieldSolverError(
                    f"cover layer {index + 1} is {thick} mm but the grid pitch "
                    f"is {dx:.6f} mm, so it cannot be represented: the "
                    "rasteriser would round it up to a full cell "
                    f"({dx:.6f} mm, {dx / thick:.0f}x too thick) and the "
                    "answer would move without saying why")
            top = min(ny, cursor + _grid_span(thick, dx))
            realised = (top - cursor) * dx
            if abs(realised - thick) / thick > 0.02:
                notes.append(
                    f"cover layer {index + 1} rasterised {thick:.4f} -> "
                    f"{realised:.4f} mm ({(realised - thick) / thick * 100:+.1f}%)")
            eps_r[cursor:top, :] = float(permittivity)
            cursor = top
        if cursor >= ny:
            raise FieldSolverError(
                "the cover stack reaches the box lid; there is no air above "
                "it, so the top wall would clamp the fringing field")
        notes.append("cover stack (bottom-up): " + " + ".join(
            f"{thick} mm er={permittivity}" for thick, permittivity in layers))

    conductor = np.full((ny + 1, nx + 1), FREE, dtype=int)
    conductor[0, :] = GROUND_ID                       # bottom reference plane
    if top_ground:
        conductor[ny, :] = GROUND_ID
    else:
        conductor[ny, :] = GROUND_ID                  # box lid (electric wall)
    conductor[:, 0] = GROUND_ID
    conductor[:, nx] = GROUND_ID

    trace_bottom_node = sub_cells
    trace_top_node = sub_cells + trace_cells
    start_mm = (width_mm - total_trace) / 2.0
    for index, width in enumerate(trace_widths_mm):
        left_node = _grid_span(start_mm, dx)
        right_node = left_node + _grid_span(width, dx)
        if right_node <= left_node:
            raise FieldSolverError(
                f"trace {index + 1} vanished at this grid pitch "
                f"(w={width} mm, dx={dx:.6f} mm)")
        conductor[trace_bottom_node:trace_top_node + 1,
                  left_node:right_node + 1] = index + 1
        start_mm += width + gap_mm

    realised_widths = [
        float(np.count_nonzero(np.any(conductor == index + 1, axis=0)) - 1) * dx
        for index in range(len(trace_widths_mm))
    ]
    for index, (asked, got) in enumerate(zip(trace_widths_mm, realised_widths)):
        if abs(got - asked) / asked > 0.02:
            notes.append(
                f"trace {index + 1} width rasterised {asked:.4f} -> "
                f"{got:.4f} mm ({(got - asked) / asked * 100:+.1f}%)")

    notes.append(label)
    notes.append(
        "outer box is a perfect electric wall; "
        f"box_margin_factor={box_margin_factor}")
    return CrossSection(dx_mm=dx, eps_r=eps_r, conductor=conductor, notes=notes)


def microstrip(w_mm: float, t_mm: float, h_mm: float, er: float, *,
               box_margin_factor: float = 8.0,
               cells_per_feature: int = 4,
               max_nodes: int = 400_000,
               cover_mm: float = 0.0,
               cover_er: float = 3.5) -> CrossSection:
    """Single-ended microstrip over one reference plane."""
    return _planar_section(
        trace_widths_mm=[w_mm], gap_mm=0.0, thickness_mm=t_mm,
        substrate_mm=h_mm, er=er, top_ground=False, trace_offset_mm=0.0,
        box_margin_factor=box_margin_factor,
        cells_per_feature=cells_per_feature, max_nodes=max_nodes,
        cover_mm=cover_mm, cover_er=cover_er,
        label=f"microstrip w={w_mm} t={t_mm} h={h_mm} er={er}")


def coupled_microstrip(w_mm: float, s_mm: float, t_mm: float, h_mm: float,
                       er: float, *, box_margin_factor: float = 8.0,
                       cells_per_feature: int = 4,
                       max_nodes: int = 400_000,
                       cover_mm: float = 0.0,
                       cover_er: float = 3.5) -> CrossSection:
    """Edge-coupled microstrip pair."""
    return _planar_section(
        trace_widths_mm=[w_mm, w_mm], gap_mm=s_mm, thickness_mm=t_mm,
        substrate_mm=h_mm, er=er, top_ground=False, trace_offset_mm=0.0,
        box_margin_factor=box_margin_factor,
        cells_per_feature=cells_per_feature, max_nodes=max_nodes,
        cover_mm=cover_mm, cover_er=cover_er,
        label=f"coupled microstrip w={w_mm} s={s_mm} t={t_mm} h={h_mm} er={er}")


def stripline(w_mm: float, t_mm: float, b_mm: float, er: float, *,
              box_margin_factor: float = 8.0,
              cells_per_feature: int = 4,
              max_nodes: int = 400_000) -> CrossSection:
    """Symmetric stripline. ``b_mm`` is the TOTAL plane-to-plane spacing.

    Matches ``si_models.z0_stripline``'s convention exactly, so the two are
    directly comparable without a factor-of-two argument at the call site.
    """
    if b_mm <= t_mm:
        raise FieldSolverError(
            f"plane spacing b={b_mm} must exceed trace thickness t={t_mm}")
    below = (b_mm - t_mm) / 2.0
    return _planar_section(
        trace_widths_mm=[w_mm], gap_mm=0.0, thickness_mm=t_mm,
        substrate_mm=below, er=er, top_ground=True, trace_offset_mm=below,
        box_margin_factor=box_margin_factor,
        cells_per_feature=cells_per_feature, max_nodes=max_nodes,
        cover_mm=0.0, cover_er=1.0,
        label=f"symmetric stripline w={w_mm} t={t_mm} b={b_mm} er={er}")


def coupled_stripline(w_mm: float, s_mm: float, t_mm: float, b_mm: float,
                      er: float, *, box_margin_factor: float = 8.0,
                      cells_per_feature: int = 4,
                      max_nodes: int = 400_000) -> CrossSection:
    """Edge-coupled symmetric stripline pair."""
    if b_mm <= t_mm:
        raise FieldSolverError(
            f"plane spacing b={b_mm} must exceed trace thickness t={t_mm}")
    below = (b_mm - t_mm) / 2.0
    return _planar_section(
        trace_widths_mm=[w_mm, w_mm], gap_mm=s_mm, thickness_mm=t_mm,
        substrate_mm=below, er=er, top_ground=True, trace_offset_mm=below,
        box_margin_factor=box_margin_factor,
        cells_per_feature=cells_per_feature, max_nodes=max_nodes,
        cover_mm=0.0, cover_er=1.0,
        label=f"coupled stripline w={w_mm} s={s_mm} t={t_mm} b={b_mm} er={er}")


# --------------------------------------------------------------------------- #
# Flexible and rigid-flex stacks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FlexLayer:
    """One dielectric layer of a flexible stack, with where its numbers came from.

    ``source`` is REQUIRED and is not decoration. A flex stack's permittivity
    is not a property of "polyimide": it depends on the film grade, the
    adhesive system, whether the laminate is adhesiveless, and the frequency
    the value was measured at. Kapton HN and Kapton EN do not share an er, and
    an acrylic adhesive is not an epoxy one. This module therefore ships NO
    default permittivity for any flex material - a number without a citation
    would be indistinguishable from one that was measured, and the whole
    bundle exists to keep those apart (see the promotion rule in
    docs/tool-inventory.md section 5).
    """

    name: str
    thickness_mm: float
    er: float
    source: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise FieldSolverError("flex layer needs a name")
        if self.thickness_mm <= 0 or not math.isfinite(self.thickness_mm):
            raise FieldSolverError(
                f"{self.name}: thickness must be positive, got {self.thickness_mm}")
        if self.er < 1.0 or not math.isfinite(self.er):
            raise FieldSolverError(
                f"{self.name}: er must be >= 1 (vacuum), got {self.er}")
        if not self.source.strip():
            raise FieldSolverError(
                f"{self.name}: er={self.er} carries no source. A permittivity "
                "without a citation is a guess wearing a number; give the "
                "datasheet or measurement it came from.")


@dataclass(frozen=True)
class FlexStackup:
    """Base film + copper + coverlay, as a flex fabricator would describe it.

    Bottom-up: reference plane, base film, copper, coverlay adhesive,
    coverlay film. The adhesive and the film are kept SEPARATE because they
    are different materials with different permittivities; merging them into
    one averaged layer would invent a material.

    ``adhesiveless`` records whether the base laminate has an adhesive layer
    between film and copper. It is carried rather than modelled: on an
    adhesiveless laminate there is nothing to model, and on an adhesive one
    the layer belongs in ``base`` as its own entry once somebody supplies its
    thickness and er. Claiming to model it while ignoring it would be worse
    than saying it is out of scope.
    """

    base: FlexLayer
    copper_thickness_mm: float
    coverlay_adhesive: FlexLayer | None = None
    coverlay_film: FlexLayer | None = None
    adhesiveless: bool = True
    # Extra dielectric BELOW the copper, nearest the reference plane first.
    # ``base`` stays the layer touching the conductor, so a single-layer stack
    # keeps working unchanged. A real flex build is asymmetric by
    # construction; this is what lets the base be more than one material
    # without averaging them into one er nobody supplied.
    base_underlayers: tuple = ()

    def __post_init__(self) -> None:
        if self.copper_thickness_mm <= 0 or not math.isfinite(self.copper_thickness_mm):
            raise FieldSolverError(
                f"copper thickness must be positive, got {self.copper_thickness_mm}")
        if self.coverlay_film is not None and self.coverlay_adhesive is None:
            raise FieldSolverError(
                "a coverlay film with no adhesive layer: the film does not "
                "touch the copper by itself. Supply the adhesive, or model "
                "the pair as one layer and say so in its source.")
        if not self.adhesiveless:
            raise FieldSolverError(
                "adhesive-based base laminates are NOT modelled: the adhesive "
                "between film and copper is a third dielectric and this class "
                "has nowhere to put its thickness and er. Out of scope is an "
                "honest answer; silently treating it as adhesiveless is not.")

    def base_stack(self) -> list[tuple[float, float]]:
        """``[(thickness_mm, er), ...]`` bottom-up: reference plane first,
        the layer touching the copper last."""
        stack = [(layer.thickness_mm, layer.er)
                 for layer in self.base_underlayers]
        stack.append((self.base.thickness_mm, self.base.er))
        return stack

    def base_thickness_mm(self) -> float:
        return sum(thick for thick, _ in self.base_stack())

    def cover_stack(self) -> list[tuple[float, float]]:
        """``[(thickness_mm, er), ...]`` bottom-up, for ``_planar_section``."""
        return [(layer.thickness_mm, layer.er)
                for layer in (self.coverlay_adhesive, self.coverlay_film)
                if layer is not None]

    def sources(self) -> list[str]:
        return [f"{layer.name}: er={layer.er} <- {layer.source}"
                for layer in (*self.base_underlayers, self.base,
                              self.coverlay_adhesive,
                              self.coverlay_film) if layer is not None]

    def describe(self) -> str:
        parts = [f"base {self.base.name} {self.base.thickness_mm} mm "
                 f"er={self.base.er}",
                 f"copper {self.copper_thickness_mm} mm"]
        parts += [f"{layer.name} {layer.thickness_mm} mm er={layer.er}"
                  for layer in (self.coverlay_adhesive, self.coverlay_film)
                  if layer is not None]
        return " | ".join(parts)


def flex_microstrip(w_mm: float, stackup: FlexStackup, *,
                    box_margin_factor: float = 8.0,
                    cells_per_feature: int = 4,
                    max_nodes: int = 400_000) -> CrossSection:
    """Coverlay-covered microstrip on a flexible base film.

    Geometrically this is a microstrip whose cover is a STACK rather than one
    material. Nothing about the solver changes - ``eps_r`` was always per
    cell - which is why this is a builder and not a second solver.
    """
    section = _planar_section(
        trace_widths_mm=[w_mm], gap_mm=0.0,
        thickness_mm=stackup.copper_thickness_mm,
        substrate_mm=stackup.base_thickness_mm(), er=stackup.base.er,
        top_ground=False, trace_offset_mm=0.0,
        box_margin_factor=box_margin_factor,
        cells_per_feature=cells_per_feature, max_nodes=max_nodes,
        cover_mm=0.0, cover_er=1.0,
        cover_stack=stackup.cover_stack(),
        base_stack=stackup.base_stack(),
        label=f"flex microstrip w={w_mm} | {stackup.describe()}")
    section.notes.extend(stackup.sources())
    if not stackup.cover_stack():
        section.notes.append(
            "NO COVERLAY: this is a bare flex trace. Real flex is covered "
            "almost everywhere, so a bare result is the exposed-window case "
            "and not the line's normal impedance.")
    return section


def coupled_flex_microstrip(w_mm: float, s_mm: float, stackup: FlexStackup, *,
                            box_margin_factor: float = 8.0,
                            cells_per_feature: int = 4,
                            max_nodes: int = 400_000) -> CrossSection:
    """Edge-coupled differential pair on flex, under a coverlay.

    The coverlay matters more here than single-ended: it fills the GAP
    between the two traces, which is where the coupling lives. A pair solved
    without it reads a gap full of air and reports a differential impedance
    the built board will not have.
    """
    section = _planar_section(
        trace_widths_mm=[w_mm, w_mm], gap_mm=s_mm,
        thickness_mm=stackup.copper_thickness_mm,
        substrate_mm=stackup.base_thickness_mm(), er=stackup.base.er,
        top_ground=False, trace_offset_mm=0.0,
        box_margin_factor=box_margin_factor,
        cells_per_feature=cells_per_feature, max_nodes=max_nodes,
        cover_mm=0.0, cover_er=1.0,
        cover_stack=stackup.cover_stack(),
        base_stack=stackup.base_stack(),
        label=f"coupled flex microstrip w={w_mm} s={s_mm} | {stackup.describe()}")
    section.notes.extend(stackup.sources())
    return section


BUILDERS = {
    "coax": coax,
    "microstrip": microstrip,
    "coupled-microstrip": coupled_microstrip,
    "stripline": stripline,
    "coupled-stripline": coupled_stripline,
    "flex-microstrip": flex_microstrip,
    "coupled-flex-microstrip": coupled_flex_microstrip,
}
