"""Closed-form signal-integrity models (IPC-2141 / IPC-2221 / Wadell).

SINGLE SOURCE OF TRUTH. Before consolidation the IPC-2141 microstrip and
stripline expressions existed twice:

  - crosstalk_thinning.impedance_z0_ohm  (no domain guard: happily returned a
    NEGATIVE impedance when 5.98*h/(0.8w+t) <= 1)
  - zdiff_solver.z0_microstrip / zdiff_stripline (raised ValueError instead)

zdiff_solver's guard is the correct behaviour and is kept here, so
crosstalk_thinning now also refuses out-of-domain geometry rather than
reporting nonsense.

NOT A SIGN-OFF TOOL. The published Polar comparison for this same closed form
spans -29.94% .. +2.41% depending on geometry. Every consumer must emit
``model_class: exploratory_closed_form`` and ``signoff: false``.
"""

from __future__ import annotations

import math

from .units import C_MM_PS, MM_TO_MIL, OZ_TO_MM

MODEL_CLASS = "exploratory_closed_form"
SIGNOFF = False

# IPC-2141 stated validity window for W/H.
W_OVER_H_MIN = 0.1
W_OVER_H_MAX = 3.0


class ModelDomainError(ValueError):
    """Geometry falls outside the approximation's valid domain."""


def require_positive(**values: float) -> None:
    for name, value in values.items():
        if value is None or not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"{name} must be a finite positive number, got {value!r}")


# --------------------------------------------------------------------------- #
# Effective permittivity and propagation
# --------------------------------------------------------------------------- #
def er_eff(er: float, h_mm: float, w_mm: float, layer_type: str) -> float:
    """Effective dielectric constant.

    Stripline is a homogeneous medium (er_eff == er); microstrip is mixed
    (air above, laminate below) and uses the Hammerstad correction.
    """
    require_positive(er=er, h_mm=h_mm, w_mm=w_mm)
    if layer_type == "stripline":
        return er
    return (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 12.0 * h_mm / w_mm) ** -0.5


def prop_velocity_mm_ps(er_effective: float) -> float:
    """Propagation velocity in mm/ps for a given effective permittivity."""
    require_positive(er_effective=er_effective)
    return C_MM_PS / math.sqrt(er_effective)


# --------------------------------------------------------------------------- #
# Characteristic impedance - IPC-2141 closed form
# --------------------------------------------------------------------------- #
def z0_microstrip(w: float, t: float, h: float, er: float) -> float:
    """Single-ended microstrip Z0, ohms.

    Z0 = 87/sqrt(er+1.41) * ln(5.98*h/(0.8*w+t))
    """
    require_positive(w=w, t=t, h=h, er=er)
    log_argument = 5.98 * h / (0.8 * w + t)
    if log_argument <= 1.0:
        raise ModelDomainError(
            "geometry is outside this approximation's positive-impedance domain")
    return 87.0 / math.sqrt(er + 1.41) * math.log(log_argument)


def z0_stripline(w: float, t: float, b: float, er: float) -> float:
    """Symmetric stripline Z0, ohms. ``b`` is the TOTAL plane-to-plane spacing.

    Z0 = 60/sqrt(er) * ln(4*b/(0.67*pi*(0.8*w+t)))

    Callers that track ``h`` as the height to the NEAREST plane must pass
    ``b = 2*h`` and record that the structure was assumed symmetric.
    """
    require_positive(w=w, t=t, b=b, er=er)
    log_argument = 4.0 * b / (0.67 * math.pi * (0.8 * w + t))
    if log_argument <= 1.0:
        raise ModelDomainError(
            "geometry is outside this approximation's positive-impedance domain")
    return 60.0 / math.sqrt(er) * math.log(log_argument)


def zdiff_microstrip(w: float, s: float, t: float, h: float, er: float) -> float:
    """Edge-coupled microstrip differential impedance, ohms."""
    require_positive(s=s)
    z0 = z0_microstrip(w, t, h, er)
    z_odd = z0 * (1.0 - 0.347 * math.exp(-2.9 * s / h))
    return 2.0 * z_odd


def zdiff_stripline(w: float, s: float, t: float, h: float, er: float) -> float:
    """Edge-coupled symmetric stripline Zdiff, ohms.

    ``h`` is the total separation between the two reference planes and the
    trace is assumed centred between them.
    """
    require_positive(s=s)
    z0 = z0_stripline(w, t, h, er)
    return 2.0 * z0 * (1.0 - 0.374 * math.exp(-2.9 * s / h))


def z0_with_notes(w_mm: float, h_mm: float, er: float, t_mm: float,
                  layer_type: str) -> dict:
    """Z0 plus the validity caveats, for tools that report rather than solve.

    ``h_mm`` is the height to the NEAREST reference plane for both layer
    types; the stripline branch converts to b = 2*h and says so.
    Returns ``{"z0_ohm": float|None, "notes": [str], "model_class", "signoff"}``.
    ``z0_ohm`` is None when the geometry is out of domain - never a negative
    impedance, which is what the old crosstalk_thinning copy produced.
    """
    notes: list[str] = []
    w_over_h = w_mm / h_mm
    if not (W_OVER_H_MIN <= w_over_h <= W_OVER_H_MAX):
        notes.append(
            f"W/H={w_over_h:.2f} IPC-2141 geçerlilik aralığı "
            f"({W_OVER_H_MIN}..{W_OVER_H_MAX}) dışında")
    try:
        if layer_type == "stripline":
            z0 = z0_stripline(w_mm, t_mm, 2.0 * h_mm, er)
            notes.append(
                "stripline b=2H (simetrik) varsayıldı — asimetrik yapı CONFIRM")
        else:
            z0 = z0_microstrip(w_mm, t_mm, h_mm, er)
    except ModelDomainError as exc:
        notes.append(f"NO_COVERAGE: {exc}")
        return {"z0_ohm": None, "notes": notes,
                "model_class": MODEL_CLASS, "signoff": SIGNOFF}
    return {"z0_ohm": round(z0, 2), "notes": notes,
            "model_class": MODEL_CLASS, "signoff": SIGNOFF}


# --------------------------------------------------------------------------- #
# Current carrying - IPC-2221 screening only
# --------------------------------------------------------------------------- #
def ipc2221_min_width_mm(current_a: float, delta_t_c: float, copper_oz: float,
                         layer_type: str) -> float:
    """IPC-2221 current-capacity screen. NOT IPC-2152 - SCREEN_ONLY."""
    require_positive(current_a=current_a, delta_t_c=delta_t_c,
                     copper_oz=copper_oz)
    k = 0.024 if layer_type == "stripline" else 0.048
    area_mils2 = (current_a / (k * delta_t_c ** 0.44)) ** (1.0 / 0.725)
    thickness_mils = copper_oz * OZ_TO_MM * MM_TO_MIL
    width_mils = area_mils2 / thickness_mils
    return width_mils / MM_TO_MIL


# --------------------------------------------------------------------------- #
# Crosstalk geometry
# --------------------------------------------------------------------------- #
def coupling_ratio(d_mm: float, h_mm: float) -> float:
    """kx = 1 / (1 + (D/H)^2) - geometric coupling ratio, 0..1."""
    require_positive(d_mm=d_mm, h_mm=h_mm)
    return 1.0 / (1.0 + (d_mm / h_mm) ** 2)


def knee_wavelength_spacing_mm(f_knee_ghz: float, er_effective: float,
                               divisor: float = 20.0) -> float:
    """lambda/N stitching target at the knee frequency, in millimetres."""
    require_positive(f_knee_ghz=f_knee_ghz, er_effective=er_effective,
                     divisor=divisor)
    from .units import C_MPS
    velocity = C_MPS / math.sqrt(er_effective)
    lam_mm = (velocity / (f_knee_ghz * 1e9)) * 1000.0
    return lam_mm / divisor


# --------------------------------------------------------------------------- #
# Differential geometry solver
#
# This lived in scripts/zdiff_solver.py, which made common/stackup.py import
# UPWARDS out of common/ into scripts/ - a layering inversion that only worked
# when tools/ happened to be on sys.path. The solver is a pure function of the
# closed-form models above, so it belongs here; zdiff_solver.py is now just a
# CLI over it.
#
# SIGN-OFF BOUNDARY UNCHANGED: everything here is MODEL_CLASS /
# SIGNOFF = False. "Reachable" means the target lies inside the impedance span
# of the fab-clamped W/S bounds - it is not a manufacturing guarantee.
# --------------------------------------------------------------------------- #
DEFAULT_H_CANDIDATES = (0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.6)


def _grid_values(bounds: Sequence[float], minimum: float, name: str) -> list[float]:
    """Expand (start, stop[, step]) into an inclusive, fab-clamped grid."""

    if len(bounds) not in (2, 3):
        raise ValueError(f"{name} must be (start, stop) or (start, stop, step)")
    start, stop = float(bounds[0]), float(bounds[1])
    step = float(bounds[2]) if len(bounds) == 3 else 0.005
    require_positive(start=start, stop=stop, step=step, minimum=minimum)
    start = max(start, minimum)
    if start > stop:
        return []

    count = int(math.floor((stop - start) / step + 1e-12))
    values = [start + index * step for index in range(count + 1)]
    if not math.isclose(values[-1], stop, rel_tol=0.0, abs_tol=1e-12):
        values.append(stop)
    return values


def solve_for_target(
    target_ohm: float,
    h: float,
    er: float,
    t: float,
    w_range: Sequence[float],
    s_range: Sequence[float],
    fab_min_w: float,
    fab_min_s: float,
) -> list[dict[str, float]]:
    """Return the five closest microstrip (w, s) grid points to ``target_ohm``."""

    require_positive(
        target_ohm=target_ohm,
        h=h,
        er=er,
        t=t,
        fab_min_w=fab_min_w,
        fab_min_s=fab_min_s,
    )
    widths = _grid_values(w_range, fab_min_w, "w_range")
    spacings = _grid_values(s_range, fab_min_s, "s_range")
    if not widths or not spacings:
        return []

    candidates: list[dict[str, float]] = []
    for w in widths:
        for s in spacings:
            try:
                impedance = zdiff_microstrip(w, s, t, h, er)
            except ValueError:
                continue
            error_ohm = impedance - target_ohm
            candidates.append(
                {
                    "w_mm": round(w, 9),
                    "s_mm": round(s, 9),
                    "zdiff_ohm": impedance,
                    "error_ohm": error_ohm,
                    "error_percent": abs(error_ohm) / target_ohm * 100.0,
                }
            )

    candidates.sort(
        key=lambda item: (
            item["error_percent"],
            item["w_mm"],
            item["s_mm"],
        )
    )
    return candidates[:5]


def _reachable_impedance_limits(
    h: float,
    er: float,
    t: float,
    w_range: Sequence[float],
    s_range: Sequence[float],
    fab_min_w: float,
    fab_min_s: float,
) -> tuple[float, float] | None:
    widths = _grid_values(w_range, fab_min_w, "w_range")
    spacings = _grid_values(s_range, fab_min_s, "s_range")
    impedances: list[float] = []
    for w in widths:
        for s in spacings:
            try:
                impedances.append(zdiff_microstrip(w, s, t, h, er))
            except ValueError:
                pass
    if not impedances:
        return None
    return min(impedances), max(impedances)


def sweep_stackups(
    target_ohm: float,
    er: float,
    t: float,
    w_range: Sequence[float] = (0.05, 2.0, 0.005),
    s_range: Sequence[float] = (0.05, 2.0, 0.005),
    fab_min_w: float = 0.127,
    fab_min_s: float = 0.127,
    h_candidates: Iterable[float] = DEFAULT_H_CANDIDATES,
) -> dict[str, object]:
    """Sweep dielectric heights and explicitly flag unreachable targets.

    "Reachable" means the target lies within the impedance span of the
    fab-clamped W/S search bounds; it does not imply manufacturing sign-off.
    """

    require_positive(target_ohm=target_ohm, er=er, t=t)
    rows: list[dict[str, object]] = []
    unreachable_h_mm: list[float] = []

    for h_value in h_candidates:
        h = float(h_value)
        require_positive(h=h)
        solutions = solve_for_target(
            target_ohm,
            h,
            er,
            t,
            w_range,
            s_range,
            fab_min_w,
            fab_min_s,
        )
        limits = _reachable_impedance_limits(
            h, er, t, w_range, s_range, fab_min_w, fab_min_s
        )
        reachable = bool(
            limits is not None and limits[0] <= target_ohm <= limits[1]
        )
        if not reachable:
            unreachable_h_mm.append(h)
        rows.append(
            {
                "h_mm": h,
                "reachable": reachable,
                "status": "REACHABLE" if reachable else "UNREACHABLE",
                "reachable_min_ohm": limits[0] if limits else None,
                "reachable_max_ohm": limits[1] if limits else None,
                "best": solutions[0] if solutions else None,
                "top_5": solutions,
            }
        )

    return {
        "model": "IPC-2141/Wadell approximate edge-coupled microstrip",
        "target_ohm": target_ohm,
        "er": er,
        "t_mm": t,
        "fab_min_w_mm": fab_min_w,
        "fab_min_s_mm": fab_min_s,
        "w_range_mm": list(w_range),
        "s_range_mm": list(s_range),
        "stackups": rows,
        "unreachable_h_mm": unreachable_h_mm,
    }
