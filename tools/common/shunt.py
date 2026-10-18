"""Series shunt sizing for non-destructive per-rail current measurement.

WHY THIS IS NOT "PUT 0.1 OHM ON EVERY RAIL"
--------------------------------------------------------------------------
A shunt is a trade between two quantities that pull in opposite directions:

  * The bigger R is, the finer the current you can resolve, because the
    meter reads ``V = I*R`` and its own resolution floor is fixed.
  * The bigger R is, the more the rail droops at full load, because the
    shunt sits OUTSIDE a fixed-output regulator's feedback loop and its
    drop is therefore not corrected.

One fixed value cannot serve three rails whose full-scale currents differ
by 4x. So this module does not choose a value; it computes the WINDOW
``[r_min, r_max]`` the two constraints leave open, and reports that the
window is EMPTY when they cross. An empty window is a real and useful
answer - it says the measurement cannot be made this way with this meter,
and names which of the two bounds has to move.

WHY THE FULL-SCALE CURRENT IS NOT A GUESS
--------------------------------------------------------------------------
This exists to close a gap of the form "we do not know what this rail
draws". Sizing the shunt from the unknown draw would be circular. It is
sized from the REGULATOR'S RATED OUTPUT CURRENT instead - a datasheet
number - so the instrument covers the whole range the rail could possibly
be in. The measurement then produces the draw; the draw was never an input.

WHAT THIS MODULE REFUSES TO DO
--------------------------------------------------------------------------
1. It never invents a package power rating. Ratings for one nominal size
   span more than 3x across vendors, so ``package_power_w`` is an input;
   absent, the dissipation check reports NO_COVERAGE rather than passing.
2. It never claims an ACCURACY from a RESOLUTION. A meter that displays
   10 uV steps is not accurate to 10 uV, and a 2-terminal shunt carries
   its pad and trace resistance in series with the element. Accuracy is
   computed only when the uncertainty terms are supplied, and the
   parasitic term is required whenever sensing is not Kelvin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

# IEC 60063 preferred values, one decade. Availability in any given
# package is a SOURCING question this module does not answer.
E24: tuple[float, ...] = (
    1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
    3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1,
)
E96: tuple[float, ...] = (
    1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30,
    1.33, 1.37, 1.40, 1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74,
    1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32,
    2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09,
    3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
    4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49,
    5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32,
    7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53, 9.76,
)
E_SERIES = {"E24": E24, "E96": E96}

# Where the shunt sits relative to the rail's bypass bank.
UPSTREAM_OF_BYPASS = "upstream_of_bypass"
DOWNSTREAM_OF_BYPASS = "downstream_of_bypass"
POSITIONS = (UPSTREAM_OF_BYPASS, DOWNSTREAM_OF_BYPASS)

# Reason codes. Every one of these is a refusal to report a number that
# would look authoritative and be wrong.
EMPTY_WINDOW = "SHUNT_WINDOW_EMPTY"
NO_PREFERRED_VALUE = "NO_PREFERRED_VALUE_IN_WINDOW"
DOWNSTREAM_OF_BYPASS_RISK = "SHUNT_DOWNSTREAM_OF_BYPASS"
POWER_UNRATED = "PACKAGE_POWER_NOT_DECLARED"
POWER_EXCEEDED = "PACKAGE_POWER_EXCEEDED"
ACCURACY_UNKNOWN = "ACCURACY_TERMS_NOT_DECLARED"
PARASITIC_UNDECLARED = "NON_KELVIN_PARASITIC_NOT_DECLARED"


def preferred_values(decade_low: float, decade_high: float,
                     series: str = "E24") -> list[float]:
    """Every preferred value in ``[decade_low, decade_high]``.

    Walks whole decades rather than dividing, so a value sitting exactly on
    a bound is included instead of being lost to float error.
    """
    if series not in E_SERIES:
        raise ValueError(f"series must be one of {sorted(E_SERIES)}, got {series!r}")
    if decade_low <= 0 or decade_high <= 0:
        raise ValueError("resistance bounds must be positive")
    if decade_high < decade_low:
        return []
    values: list[float] = []
    exp_low = math.floor(math.log10(decade_low))
    exp_high = math.ceil(math.log10(decade_high))
    for exponent in range(exp_low, exp_high + 1):
        scale = 10.0 ** exponent
        for mantissa in E_SERIES[series]:
            value = mantissa * scale
            # Round to the series' own precision before comparing, so that
            # 0.1 does not miss a bound of 0.1 by one ulp.
            value = float(f"{value:.6g}")
            if decade_low - 1e-15 <= value <= decade_high + 1e-15:
                values.append(value)
    return values


@dataclass(frozen=True)
class ShuntWindow:
    """The open interval of resistances that satisfies both constraints."""

    r_min_ohm: float          # from the resolution requirement
    r_max_ohm: float          # from the droop budget
    binding: str              # which bound closed the window, when empty

    @property
    def is_empty(self) -> bool:
        return self.r_min_ohm > self.r_max_ohm

    def to_dict(self) -> dict:
        return {
            "r_min_ohm": self.r_min_ohm,
            "r_max_ohm": self.r_max_ohm,
            "empty": self.is_empty,
            "binding": self.binding,
        }


def shunt_window(full_scale_current_a: float, max_droop_v: float,
                 meter_resolution_v: float,
                 current_resolution_a: float) -> ShuntWindow:
    """Compute the resistance window from the two opposing constraints.

    ``r_max`` = max_droop_v / full_scale_current_a
        At the regulator's rated output the shunt must not drop more than
        the rail's droop budget. The drop is NOT corrected by a fixed-output
        LDO: its feedback is taken at its own output pin, upstream of the
        shunt.

    ``r_min`` = meter_resolution_v / current_resolution_a
        To distinguish two currents that differ by ``current_resolution_a``
        the meter must see at least one of its own steps between them.
        This is a RESOLUTION bound, not an accuracy bound.
    """
    for name, value in (("full_scale_current_a", full_scale_current_a),
                        ("max_droop_v", max_droop_v),
                        ("meter_resolution_v", meter_resolution_v),
                        ("current_resolution_a", current_resolution_a)):
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value!r}")

    r_max = max_droop_v / full_scale_current_a
    r_min = meter_resolution_v / current_resolution_a
    binding = "none"
    if r_min > r_max:
        # Name the bound that is cheaper to move, so the report can say
        # what would have to change rather than only that it failed.
        binding = "droop_budget" if r_max < r_min / 2 else "meter_resolution"
    return ShuntWindow(r_min_ohm=r_min, r_max_ohm=r_max, binding=binding)


def select_shunt(window: ShuntWindow, series: str = "E24",
                 prefer: str = "lowest") -> Optional[float]:
    """Pick a preferred value inside the window, or None when none exists.

    ``prefer="lowest"`` biases towards the smallest droop; the resolution
    requirement is already met by every value in the window, so there is
    nothing to gain by spending more of the droop budget than necessary.
    """
    if window.is_empty:
        return None
    candidates = preferred_values(window.r_min_ohm, window.r_max_ohm, series)
    if not candidates:
        return None
    return candidates[0] if prefer == "lowest" else candidates[-1]


def current_uncertainty_pct(resistor_tolerance_pct: float,
                            meter_accuracy_pct: float,
                            shunt_ohm: float,
                            parasitic_ohm: Optional[float] = None,
                            kelvin: bool = False) -> Optional[float]:
    """Relative uncertainty of the measured current, in percent.

    ``I = V / R``, so the relative uncertainties of V and R combine in
    quadrature (independent terms).

    The parasitic term is what makes a 2-terminal shunt different from a
    4-terminal one. Without Kelvin connections the pads and the trace
    between the testpoint and the element are IN SERIES with the element,
    and a few milliohms of copper is a double-digit percentage of a
    100 milliohm shunt. Returns None when that term is required and has
    not been declared - an unknown cannot be reported as a small one.
    """
    if shunt_ohm <= 0:
        raise ValueError("shunt_ohm must be > 0")
    if not kelvin and parasitic_ohm is None:
        return None
    terms = [resistor_tolerance_pct, meter_accuracy_pct]
    if not kelvin:
        # Treated as a one-sided systematic bound expressed as a percentage
        # of the element; it does not cancel, so it is carried at full size.
        terms.append(100.0 * float(parasitic_ohm) / shunt_ohm)
    return math.sqrt(sum(term * term for term in terms))


@dataclass
class RailShunt:
    """One rail's sizing outcome."""

    rail: str
    regulator_ref: str
    regulator_pin: str
    full_scale_current_a: float
    window: ShuntWindow
    position: str
    series: str = "E24"
    shunt_ohm: Optional[float] = None
    package_power_w: Optional[float] = None
    resistor_tolerance_pct: float = 1.0
    meter_accuracy_pct: float = 0.5
    meter_resolution_v: float = 1e-5
    parasitic_ohm: Optional[float] = None
    kelvin: bool = False
    allow_downstream: bool = False
    reason_codes: list[str] = field(default_factory=list)

    # -- derived ---------------------------------------------------------
    @property
    def droop_at_full_scale_v(self) -> Optional[float]:
        if self.shunt_ohm is None:
            return None
        return self.shunt_ohm * self.full_scale_current_a

    @property
    def power_at_full_scale_w(self) -> Optional[float]:
        if self.shunt_ohm is None:
            return None
        return self.full_scale_current_a ** 2 * self.shunt_ohm

    @property
    def achieved_current_resolution_a(self) -> Optional[float]:
        if self.shunt_ohm is None:
            return None
        return self.meter_resolution_v / self.shunt_ohm

    @property
    def uncertainty_pct(self) -> Optional[float]:
        if self.shunt_ohm is None:
            return None
        return current_uncertainty_pct(
            self.resistor_tolerance_pct, self.meter_accuracy_pct,
            self.shunt_ohm, self.parasitic_ohm, self.kelvin)

    def evaluate(self) -> list[str]:
        """Fill ``reason_codes`` and return them. Idempotent."""
        codes: list[str] = []
        if self.window.is_empty:
            codes.append(EMPTY_WINDOW)
        elif self.shunt_ohm is None:
            codes.append(NO_PREFERRED_VALUE)

        if self.position == DOWNSTREAM_OF_BYPASS and not self.allow_downstream:
            codes.append(DOWNSTREAM_OF_BYPASS_RISK)

        power = self.power_at_full_scale_w
        if power is not None:
            if self.package_power_w is None:
                codes.append(POWER_UNRATED)
            elif power > self.package_power_w:
                codes.append(POWER_EXCEEDED)

        if self.shunt_ohm is not None:
            if not self.kelvin and self.parasitic_ohm is None:
                codes.append(PARASITIC_UNDECLARED)
            elif self.uncertainty_pct is None:
                codes.append(ACCURACY_UNKNOWN)

        self.reason_codes = codes
        return codes

    def to_dict(self) -> dict:
        return {
            "rail": self.rail,
            "regulator": {"ref": self.regulator_ref, "pin": self.regulator_pin},
            "full_scale_current_a": self.full_scale_current_a,
            "position": self.position,
            "window": self.window.to_dict(),
            "series": self.series,
            "shunt_ohm": self.shunt_ohm,
            "droop_at_full_scale_v": self.droop_at_full_scale_v,
            "power_at_full_scale_w": self.power_at_full_scale_w,
            "package_power_w": self.package_power_w,
            "achieved_current_resolution_a": self.achieved_current_resolution_a,
            "kelvin": self.kelvin,
            "parasitic_ohm": self.parasitic_ohm,
            "current_uncertainty_pct": self.uncertainty_pct,
            "reason_codes": list(self.reason_codes),
        }


def size_rail(rail: str, regulator_ref: str, regulator_pin: str,
              full_scale_current_a: float, max_droop_v: float,
              meter_resolution_v: float, current_resolution_a: float,
              position: str, *, series: str = "E24",
              package_power_w: Optional[float] = None,
              resistor_tolerance_pct: float = 1.0,
              meter_accuracy_pct: float = 0.5,
              parasitic_ohm: Optional[float] = None,
              kelvin: bool = False,
              allow_downstream: bool = False) -> RailShunt:
    """Size one rail end to end. Never raises on an infeasible rail - an
    infeasible rail is an OUTCOME with reason codes, not an error."""
    if position not in POSITIONS:
        raise ValueError(f"position must be one of {POSITIONS}, got {position!r}")
    window = shunt_window(full_scale_current_a, max_droop_v,
                          meter_resolution_v, current_resolution_a)
    chosen = select_shunt(window, series)
    rail_shunt = RailShunt(
        rail=rail, regulator_ref=regulator_ref, regulator_pin=regulator_pin,
        full_scale_current_a=full_scale_current_a, window=window,
        position=position, series=series, shunt_ohm=chosen,
        package_power_w=package_power_w,
        resistor_tolerance_pct=resistor_tolerance_pct,
        meter_accuracy_pct=meter_accuracy_pct,
        meter_resolution_v=meter_resolution_v,
        parasitic_ohm=parasitic_ohm, kelvin=kelvin,
        allow_downstream=allow_downstream)
    rail_shunt.evaluate()
    return rail_shunt


def testpoint_names(rail: str) -> tuple[str, str]:
    """The two probe nodes. Named by which SIDE of the shunt they touch,
    because a differential reading whose polarity is ambiguous is a
    reading that can be entered into the log with the wrong sign."""
    return f"TP_{rail}_SRC", f"TP_{rail}_LOAD"


def sense_net_name(rail: str) -> str:
    """The net downstream of the shunt.

    Inserting a series element SPLITS the rail into two nets. Every node
    that was on ``rail`` other than the regulator pin moves onto this one.
    Layout, netclass rules and any contract that named the old net have to
    be told; silently reusing the old name would leave two different nets
    answering to it.
    """
    return f"{rail}_SENSE"


def split_preview(rail: str, nodes: Sequence[tuple[str, str]],
                  regulator_ref: str, regulator_pin: str) -> dict:
    """What the net split does to the existing nodes on ``rail``.

    ``nodes`` is ``[(ref, pin), ...]`` as read from the netlist. The
    regulator pin stays on the source side; everything else moves.
    """
    stays: list[dict] = []
    moves: list[dict] = []
    for ref, pin in nodes:
        entry = {"ref": ref, "pin": pin}
        if ref == regulator_ref and pin == regulator_pin:
            stays.append(entry)
        else:
            moves.append(entry)
    return {
        "source_side_net": rail,
        "load_side_net": sense_net_name(rail),
        "stays_on_source_side": stays,
        "moves_to_load_side": moves,
        "moved_node_count": len(moves),
    }
