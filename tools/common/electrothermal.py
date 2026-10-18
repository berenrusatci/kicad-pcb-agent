"""Electrothermal coupling: resistance heats the copper, heat raises resistance.

THE GAP THIS CLOSES
-------------------
``board/current_density.py`` solves the current distribution at a FIXED
resistivity. ``board/thermal_bridge.py`` estimates a junction temperature.
Neither tells the other anything, so a trace that heats up is still solved at
its 20 C resistance, and the temperature that results is computed from a
power that is therefore too low.

The physical loop is short and closes on itself:

    R(T) = R20 * (1 + alpha * (T - 20))     copper resistivity rises with T
    P    = I^2 * R(T)                        the copper dissipates it
    T    = T_ambient + P * Rth               the dissipation raises T

WHY THIS IS WORTH MORE THAN A SLIGHTLY BETTER NUMBER
----------------------------------------------------
Because it can DIVERGE, and the divergence is the finding. Substituting the
first equation into the third gives a fixed-point iteration whose gain is

    G = I^2 * R20 * alpha * Rth

For G >= 1 there is no steady state: every degree of heating buys more than a
degree of further heating and the trace runs away. That is a real failure mode
of a thin trace on a thermally isolated layer, and it is EXACTLY the case a
single-pass calculation reports as a comfortable temperature - it simply
evaluates the first step and stops.

So this module computes G in closed form, refuses to report a temperature when
G >= 1, and reports the margin when G < 1. A tool that iterated silently and
returned whatever the loop happened to reach after N steps would give its most
confident answer precisely where the physics has no answer at all.

THE CLOSED FORM, AND WHY THE ITERATION IS STILL RUN
---------------------------------------------------
The loop is linear, so its fixed point can be written directly:

    T = T_ambient + I^2*R20*Rth*(1 - alpha*20) / (1 - G)

The iteration is still run, and the two are required to agree. The closed form
is only valid while R(T) is linear in T; running the iteration means the day
someone adds a non-linear term - a thermal resistance that varies with
temperature, say - the disagreement shows up rather than the closed form
quietly continuing to be reported.

WHAT MUST BE SUPPLIED
---------------------
``Rth``, the thermal resistance from the copper to ambient, in K/W. It depends
on the stackup, the plane coupling, the airflow and the neighbours, and there
is no way to derive it here. It is a required input; guessing it would set the
answer, because the whole loop gain is proportional to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Copper resistivity at 20 C, ohm*m. Matches board/current_density.py's
# constant deliberately: two different values for the same metal in one
# bundle is a bug waiting for someone to compare two reports.
COPPER_RESISTIVITY_20C_OHM_M = 1.68e-8

# Temperature coefficient of resistance for annealed copper, per kelvin.
# A material property with a settled value, in the same class as the speed of
# light used elsewhere here - not an engineering threshold. Overridable so an
# alloy or a plated finish can be modelled without editing this file.
COPPER_ALPHA_PER_K = 3.93e-3

REFERENCE_TEMPERATURE_C = 20.0

OZ_THICKNESS_M = 35e-6


class ElectrothermalError(ValueError):
    """The coupled problem is not well posed."""


def resistivity_at(temperature_c: float,
                   rho_20: float = COPPER_RESISTIVITY_20C_OHM_M,
                   alpha: float = COPPER_ALPHA_PER_K) -> float:
    """rho(T) = rho20 * (1 + alpha*(T - 20)), ohm*m.

    Refuses below the temperature at which the linear model would predict
    negative resistivity. That point is far outside any PCB's range, but a
    model that returns a negative resistance for an out-of-range input will
    eventually be handed one.
    """
    value = rho_20 * (1.0 + alpha * (temperature_c - REFERENCE_TEMPERATURE_C))
    if value <= 0:
        raise ElectrothermalError(
            f"the linear resistivity model gives {value:.3e} ohm*m at "
            f"{temperature_c} C, which is outside its domain")
    return value


def trace_resistance_ohm(length_mm: float, width_mm: float, copper_oz: float,
                         temperature_c: float,
                         rho_20: float = COPPER_RESISTIVITY_20C_OHM_M,
                         alpha: float = COPPER_ALPHA_PER_K) -> float:
    """R = rho(T) * L / (w * t). DC only: no skin effect, no proximity."""
    for name, value in (("length_mm", length_mm), ("width_mm", width_mm),
                        ("copper_oz", copper_oz)):
        if value <= 0 or not math.isfinite(value):
            raise ElectrothermalError(f"{name} must be positive, got {value}")
    area_m2 = (width_mm * 1e-3) * (copper_oz * OZ_THICKNESS_M)
    return resistivity_at(temperature_c, rho_20, alpha) * (length_mm * 1e-3) \
        / area_m2


def loop_gain(current_a: float, resistance_20c_ohm: float,
              thermal_resistance_k_per_w: float,
              alpha: float = COPPER_ALPHA_PER_K) -> float:
    """G = I^2 * R20 * alpha * Rth, the derivative of the fixed-point map.

    G < 1 converges, G >= 1 runs away. This is the whole stability question in
    one number, and it is available BEFORE any iteration - which is why the
    iteration never has to be trusted to notice.
    """
    if current_a < 0:
        raise ElectrothermalError(f"current must be >= 0, got {current_a}")
    for name, value in (("resistance_20c_ohm", resistance_20c_ohm),
                        ("thermal_resistance_k_per_w",
                         thermal_resistance_k_per_w)):
        if value <= 0:
            raise ElectrothermalError(f"{name} must be positive, got {value}")
    return current_a ** 2 * resistance_20c_ohm * alpha * \
        thermal_resistance_k_per_w


@dataclass
class Equilibrium:
    """The converged operating point, or the refusal to name one."""

    converged: bool
    loop_gain: float
    iterations: int
    temperature_c: float | None
    resistance_ohm: float | None
    power_w: float | None
    rise_k: float | None
    closed_form_temperature_c: float | None
    iteration_vs_closed_form_k: float | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "converged": self.converged,
            "loop_gain": self.loop_gain,
            "stable": self.loop_gain < 1.0,
            "iterations": self.iterations,
            "temperature_c": self.temperature_c,
            "resistance_ohm": self.resistance_ohm,
            "power_w": self.power_w,
            "rise_k": self.rise_k,
            "closed_form_temperature_c": self.closed_form_temperature_c,
            "iteration_vs_closed_form_k": self.iteration_vs_closed_form_k,
            "reason": self.reason,
        }


def closed_form_temperature_c(current_a: float, resistance_20c_ohm: float,
                              thermal_resistance_k_per_w: float,
                              ambient_c: float,
                              alpha: float = COPPER_ALPHA_PER_K
                              ) -> float | None:
    """The fixed point, written directly. None when the loop does not have one.

    T = Ta + I^2*R20*Rth*(1 + alpha*(Ta - 20)) / (1 - G)
    """
    gain = loop_gain(current_a, resistance_20c_ohm,
                     thermal_resistance_k_per_w, alpha)
    if gain >= 1.0:
        return None
    numerator = (current_a ** 2 * resistance_20c_ohm
                 * thermal_resistance_k_per_w
                 * (1.0 + alpha * (ambient_c - REFERENCE_TEMPERATURE_C)))
    return ambient_c + numerator / (1.0 - gain)


def solve_equilibrium(current_a: float, resistance_20c_ohm: float,
                      thermal_resistance_k_per_w: float,
                      ambient_c: float = 25.0,
                      alpha: float = COPPER_ALPHA_PER_K,
                      tolerance_k: float = 1e-9,
                      max_iterations: int = 500) -> Equilibrium:
    """Iterate the coupled loop, and refuse when it has no fixed point.

    The refusal comes from the closed-form gain, not from the iteration
    failing to settle: an unstable loop looks exactly like a slowly
    converging one for the first several steps, and a max-iterations cutoff
    would report the last value it happened to reach as if it meant something.
    """
    gain = loop_gain(current_a, resistance_20c_ohm,
                     thermal_resistance_k_per_w, alpha)
    if gain >= 1.0:
        return Equilibrium(
            converged=False, loop_gain=gain, iterations=0,
            temperature_c=None, resistance_ohm=None, power_w=None,
            rise_k=None, closed_form_temperature_c=None,
            iteration_vs_closed_form_k=None,
            reason=(
                f"loop gain {gain:.4f} >= 1: every degree of heating buys "
                "more than a degree of further heating, so there is no "
                "steady state. A single-pass calculation reports a "
                "comfortable temperature here because it evaluates the first "
                "step and stops"))

    temperature = ambient_c
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        resistance = resistance_20c_ohm * (
            1.0 + alpha * (temperature - REFERENCE_TEMPERATURE_C))
        power = current_a ** 2 * resistance
        updated = ambient_c + power * thermal_resistance_k_per_w
        if abs(updated - temperature) < tolerance_k:
            temperature = updated
            break
        temperature = updated

    resistance = resistance_20c_ohm * (
        1.0 + alpha * (temperature - REFERENCE_TEMPERATURE_C))
    power = current_a ** 2 * resistance
    closed = closed_form_temperature_c(current_a, resistance_20c_ohm,
                                       thermal_resistance_k_per_w,
                                       ambient_c, alpha)
    difference = (abs(temperature - closed) if closed is not None else None)
    return Equilibrium(
        converged=True, loop_gain=gain, iterations=iterations,
        temperature_c=temperature, resistance_ohm=resistance,
        power_w=power, rise_k=temperature - ambient_c,
        closed_form_temperature_c=closed,
        iteration_vs_closed_form_k=difference,
        reason="")


def single_pass_temperature_c(current_a: float, resistance_20c_ohm: float,
                              thermal_resistance_k_per_w: float,
                              ambient_c: float = 25.0) -> float:
    """What the UNCOUPLED calculation reports: R at 20 C, one step, done.

    Kept so the report can show the two side by side. The difference between
    this and the coupled answer is the size of the error the toolchain has
    been carrying, and quoting it is more useful than quietly fixing it.
    """
    return ambient_c + current_a ** 2 * resistance_20c_ohm \
        * thermal_resistance_k_per_w


def maximum_stable_current_a(resistance_20c_ohm: float,
                             thermal_resistance_k_per_w: float,
                             alpha: float = COPPER_ALPHA_PER_K) -> float:
    """The current at which the loop gain reaches 1: G = 1 solved for I.

    Not a rating. It is the point beyond which the coupled model has no
    solution at all, which is a harder limit than any derating curve and sits
    well above the current at which a sane design would already have failed
    on temperature.
    """
    for name, value in (("resistance_20c_ohm", resistance_20c_ohm),
                        ("thermal_resistance_k_per_w",
                         thermal_resistance_k_per_w), ("alpha", alpha)):
        if value <= 0:
            raise ElectrothermalError(f"{name} must be positive, got {value}")
    return math.sqrt(1.0 / (resistance_20c_ohm * alpha
                            * thermal_resistance_k_per_w))
