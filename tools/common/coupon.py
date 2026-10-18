"""Impedance test-coupon planning: making the board measurable before it ships.

WHY THIS RUNS BEFORE GERBER OUTPUT
----------------------------------
Correlating a simulation against a measurement happens after fabrication. The
decision that makes that correlation POSSIBLE happens before it: a coupon has
to be on the panel. Miss it and the board comes back unmeasurable, and the
only remedy is another fab cycle.

So this is a gate on the way out, not a report on the way back. It answers one
question - is every controlled-impedance class represented by a coupon that is
actually long enough to measure - and refuses when the answer is no.

WHAT IS DERIVED AND WHAT MUST BE SUPPLIED
-----------------------------------------
Derived, because it follows from the physics:

    v_p   = c / sqrt(er_eff)              propagation velocity
    dx    = v_p * t_r / 2                 TDR spatial resolution; the factor 2
                                          is there because the step travels
                                          out to the discontinuity and back
    L_min = resolution_multiple * dx      the plateau has to be longer than
                                          the resolution or the reading is a
                                          transition, not an impedance

Supplied, because it belongs to the instrument or the fab and guessing it
would silently set the answer:

    t_r                   the TDR step rise time. REQUIRED, no default: a
                          20 ps head and a 200 ps head differ by 10x in the
                          coupon length they need, and a coupon that is too
                          short does not fail loudly - it reports a plausible
                          impedance measured on a transition.
    resolution_multiple   how many resolution lengths of settled line to
                          require. Defaulted to 2.0 and reported, because it
                          is a geometric argument (one resolution length is
                          the minimum for a plateau to exist at all, two
                          gives a readable one), not a standard's number.
    probe pitch, pad and launch dimensions
                          instrument- and fab-specific.

NOT CLAIMED: this does not lay out the coupon, place it on the panel, or emit
Gerber. It produces the plan and the coverage verdict; a human or the CAD step
places it. Nor does it certify that a measured coupon represents the
production trace - the coupon shares the stackup, but it does not share the
routing, and a class whose production geometry differs from its coupon is
reported as a MISMATCH rather than quietly accepted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .units import C_MPS

STRUCTURES = ("microstrip", "coupled-microstrip", "stripline",
              "coupled-stripline")

# One resolution length is the minimum for a plateau to exist; two gives one
# that can be read. Reported in every plan so the choice is visible.
DEFAULT_RESOLUTION_MULTIPLE = 2.0


class CouponError(ValueError):
    """The coupon plan cannot be produced from what was supplied."""


# --------------------------------------------------------------------------- #
# Requirements
# --------------------------------------------------------------------------- #
@dataclass
class ImpedanceClass:
    """One controlled-impedance requirement that must be measurable."""

    name: str
    structure: str
    target_ohm: float
    tolerance_percent: float
    layer: str
    w_mm: float
    er: float
    h_mm: float = 0.0
    s_mm: float = 0.0
    t_mm: float = 0.035
    nets: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise CouponError("an impedance class needs a name")
        if self.structure not in STRUCTURES:
            raise CouponError(
                f"{self.name}: structure must be one of {STRUCTURES}, "
                f"got {self.structure!r}")
        for label, value in (("target_ohm", self.target_ohm),
                             ("tolerance_percent", self.tolerance_percent),
                             ("w_mm", self.w_mm), ("er", self.er),
                             ("t_mm", self.t_mm)):
            if value <= 0 or not math.isfinite(value):
                raise CouponError(f"{self.name}: {label} must be positive")
        if self.is_coupled and self.s_mm <= 0:
            raise CouponError(
                f"{self.name}: a coupled structure needs a positive s_mm")
        if self.h_mm <= 0:
            raise CouponError(
                f"{self.name}: h_mm must be positive - the coupon has to "
                "reproduce the production stackup, so its height is not "
                "optional")

    @property
    def is_coupled(self) -> bool:
        return self.structure.startswith("coupled")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name, "structure": self.structure,
            "target_ohm": self.target_ohm,
            "tolerance_percent": self.tolerance_percent,
            "layer": self.layer, "w_mm": self.w_mm, "er": self.er,
            "h_mm": self.h_mm, "t_mm": self.t_mm, "nets": list(self.nets),
        }
        if self.is_coupled:
            payload["s_mm"] = self.s_mm
        return payload


def parse_impedance_class(raw: Mapping[str, Any]) -> ImpedanceClass:
    known = {"name", "structure", "target_ohm", "tolerance_percent", "layer",
             "w_mm", "s_mm", "t_mm", "er", "h_mm", "nets"}
    unknown = set(raw) - known
    if unknown:
        raise CouponError(
            f"unknown impedance-class keys {sorted(unknown)}; a typo would "
            "silently drop the requirement it was meant to describe")
    for required in ("name", "structure", "target_ohm", "w_mm", "er", "h_mm"):
        if required not in raw:
            raise CouponError(f"impedance class is missing {required!r}")
    return ImpedanceClass(
        name=str(raw["name"]),
        structure=str(raw["structure"]),
        target_ohm=float(raw["target_ohm"]),                # type: ignore[arg-type]
        tolerance_percent=float(raw.get("tolerance_percent", 10.0)),  # type: ignore[arg-type]
        layer=str(raw.get("layer", "unspecified")),
        w_mm=float(raw["w_mm"]),                            # type: ignore[arg-type]
        s_mm=float(raw.get("s_mm", 0.0)),                   # type: ignore[arg-type]
        t_mm=float(raw.get("t_mm", 0.035)),                 # type: ignore[arg-type]
        er=float(raw["er"]),                                # type: ignore[arg-type]
        h_mm=float(raw["h_mm"]),                            # type: ignore[arg-type]
        nets=[str(net) for net in raw.get("nets", [])],     # type: ignore[union-attr]
    )


def parse_requirements(raw: Mapping[str, Any]) -> list[ImpedanceClass]:
    classes = raw.get("impedance_classes")
    if not isinstance(classes, Sequence) or not classes:
        raise CouponError(
            "requirements need a non-empty 'impedance_classes' list; a coupon "
            "plan for no controlled-impedance class is an empty plan that "
            "would still report success")
    parsed = [parse_impedance_class(item) for item in classes]
    seen: set[str] = set()
    for item in parsed:
        if item.name in seen:
            raise CouponError(f"duplicate impedance class: {item.name}")
        seen.add(item.name)
    return parsed


# --------------------------------------------------------------------------- #
# Derivation
# --------------------------------------------------------------------------- #
def propagation_velocity_mm_per_ps(er_eff: float) -> float:
    """v_p in mm/ps. Exact for a TEM mode."""
    if er_eff <= 0:
        raise CouponError(f"er_eff must be positive, got {er_eff}")
    return (C_MPS / math.sqrt(er_eff)) * 1e3 / 1e12


def tdr_resolution_mm(er_eff: float, rise_time_ps: float) -> float:
    """Spatial resolution of a TDR step, in millimetres.

    dx = v_p * t_r / 2. The halving is not a safety factor: the step travels
    to the discontinuity and the reflection travels back, so a feature shorter
    than half the rise-time length cannot be separated from its neighbours.
    """
    if rise_time_ps <= 0:
        raise CouponError(f"rise_time_ps must be positive, got {rise_time_ps}")
    return propagation_velocity_mm_per_ps(er_eff) * rise_time_ps / 2.0


def minimum_coupon_length_mm(er_eff: float, rise_time_ps: float,
                             resolution_multiple: float
                             = DEFAULT_RESOLUTION_MULTIPLE) -> float:
    """How much settled line the measurement needs.

    Shorter than this and the instrument reads a transition rather than an
    impedance - and it does so without complaining, which is why this is a
    gate and not a warning.
    """
    if resolution_multiple <= 0:
        raise CouponError(
            f"resolution_multiple must be positive, got {resolution_multiple}")
    return tdr_resolution_mm(er_eff, rise_time_ps) * resolution_multiple


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
@dataclass
class Coupon:
    """One planned coupon, with the numbers that make it measurable."""

    impedance_class: str
    structure: str
    layer: str
    target_ohm: float
    tolerance_percent: float
    w_mm: float
    s_mm: float
    t_mm: float
    h_mm: float
    er: float
    er_eff: float
    er_eff_source: str
    rise_time_ps: float
    resolution_multiple: float
    resolution_mm: float
    minimum_length_mm: float
    planned_length_mm: float
    launch_note: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def long_enough(self) -> bool:
        return self.planned_length_mm >= self.minimum_length_mm

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "impedance_class": self.impedance_class,
            "structure": self.structure,
            "layer": self.layer,
            "target_ohm": self.target_ohm,
            "tolerance_percent": self.tolerance_percent,
            "geometry_mm": {"w": self.w_mm, "t": self.t_mm, "h": self.h_mm},
            "er": self.er,
            "er_eff": self.er_eff,
            "er_eff_source": self.er_eff_source,
            "tdr": {
                "rise_time_ps": self.rise_time_ps,
                "resolution_mm": self.resolution_mm,
                "resolution_multiple": self.resolution_multiple,
            },
            "minimum_length_mm": self.minimum_length_mm,
            "planned_length_mm": self.planned_length_mm,
            "long_enough": self.long_enough,
            "launch_note": self.launch_note,
            "warnings": list(self.warnings),
        }
        if self.structure.startswith("coupled"):
            payload["geometry_mm"]["s"] = self.s_mm
        return payload


def plan_coupon(requirement: ImpedanceClass, er_eff: float,
                er_eff_source: str, rise_time_ps: float,
                resolution_multiple: float = DEFAULT_RESOLUTION_MULTIPLE,
                planned_length_mm: float | None = None,
                length_margin: float = 1.0) -> Coupon:
    """Build one coupon from one requirement.

    ``planned_length_mm`` is what the caller intends to lay out. Left None it
    is set to the minimum times ``length_margin``, and the report says the
    length was derived rather than chosen.
    """
    resolution = tdr_resolution_mm(er_eff, rise_time_ps)
    minimum = minimum_coupon_length_mm(er_eff, rise_time_ps,
                                       resolution_multiple)
    if length_margin < 1.0:
        raise CouponError(
            f"length_margin below 1.0 would plan a coupon shorter than the "
            f"minimum it just computed, got {length_margin}")
    planned = (planned_length_mm if planned_length_mm is not None
               else minimum * length_margin)

    warnings: list[str] = []
    if planned < minimum:
        warnings.append(
            f"planned {planned:.2f} mm is below the {minimum:.2f} mm needed "
            f"to resolve a {rise_time_ps:g} ps step: the instrument would "
            "read a transition, not an impedance")
    if requirement.is_coupled and requirement.s_mm <= 0:
        warnings.append("coupled structure with no gap declared")

    return Coupon(
        impedance_class=requirement.name,
        structure=requirement.structure,
        layer=requirement.layer,
        target_ohm=requirement.target_ohm,
        tolerance_percent=requirement.tolerance_percent,
        w_mm=requirement.w_mm, s_mm=requirement.s_mm, t_mm=requirement.t_mm,
        h_mm=requirement.h_mm, er=requirement.er,
        er_eff=er_eff, er_eff_source=er_eff_source,
        rise_time_ps=rise_time_ps,
        resolution_multiple=resolution_multiple,
        resolution_mm=resolution,
        minimum_length_mm=minimum,
        planned_length_mm=planned,
        launch_note=("probe launch and reference-via placement are "
                     "instrument-specific and are not derived here"),
        warnings=warnings,
    )


def coverage(requirements: Sequence[ImpedanceClass],
             coupons: Iterable[Coupon]) -> dict[str, Any]:
    """Which controlled-impedance classes have a usable coupon.

    An uncovered class is the failure this whole step exists to prevent: the
    board is fabricated, and the one measurement that would have validated the
    stackup cannot be taken.
    """
    by_class = {coupon.impedance_class: coupon for coupon in coupons}
    missing = [item.name for item in requirements if item.name not in by_class]
    too_short = [coupon.impedance_class for coupon in by_class.values()
                 if not coupon.long_enough]
    return {
        "required_classes": [item.name for item in requirements],
        "planned_classes": sorted(by_class),
        "missing_classes": missing,
        "too_short_classes": sorted(too_short),
        "covered": not missing and not too_short,
        "coverage_fraction": (
            (len(requirements) - len(missing) - len(too_short))
            / len(requirements)) if requirements else 0.0,
    }


def geometry_matches(requirement: ImpedanceClass, coupon: Coupon,
                     tolerance_mm: float = 1e-6) -> bool:
    """A coupon that does not share the production cross-section measures
    something else, however good the measurement is."""
    same = (abs(requirement.w_mm - coupon.w_mm) <= tolerance_mm
            and abs(requirement.h_mm - coupon.h_mm) <= tolerance_mm
            and abs(requirement.t_mm - coupon.t_mm) <= tolerance_mm
            and requirement.structure == coupon.structure)
    if requirement.is_coupled:
        same = same and abs(requirement.s_mm - coupon.s_mm) <= tolerance_mm
    return same
