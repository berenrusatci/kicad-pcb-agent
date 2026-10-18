"""Measurement versus simulation: how wrong the model actually was.

WHY THIS CAN BE BUILT AND TESTED TODAY
--------------------------------------
Queue item #67 was blocked on "no board has been fabricated, so there is no
measurement". That is true of the DATA and false of the TOOL, and the two were
being confused. A VNA exports Touchstone; this bundle already reads and writes
Touchstone; so the comparison machinery can be built and fully exercised now
against a network generated from a known model, and the day a real ``.s4p``
arrives from the instrument nothing changes.

What genuinely cannot be done without a board is CORRELATE THIS BOARD. That is
a missing measurement, and it is reported as NO_COVERAGE rather than papered
over - which is exactly what a tool with no data should say.

THE LOOP THIS CLOSES
--------------------
Every model in this bundle is currently unfalsified. ``field_solver`` reports
a bounded grid error, and that bound says the NUMERICS converged, not that the
answer matches copper. The residual here is the first number in the whole
chain that could show the model is wrong:

    coupon plan  ->  fabricate  ->  measure  ->  correlate  ->  adjust er
       (4a)                          (VNA)        (here)      (feedback)

BAND-LIMITED ON PURPOSE
-----------------------
A residual quoted over a band the instrument never swept, or over frequencies
where the fixture dominates, is not a statement about the model. The comparison
is restricted to an explicitly declared band, the overlap is reported, and a
comparison with too few shared points refuses rather than averaging three
samples into a headline.

WHAT A RESIDUAL DOES NOT PROVE
------------------------------
A small residual says the model and the measurement agree, not that either is
right: a launch de-embedded with the wrong reference plane can move both. The
report carries the de-embedding status as an input the caller declares, never
as something inferred from the data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

METRICS = ("magnitude_db", "phase_deg", "group_delay_ps")

# Fewer shared points than this and a mean residual is describing noise.
MIN_SHARED_POINTS = 8


class CorrelationError(ValueError):
    """The two networks cannot be meaningfully compared."""


@dataclass
class Band:
    """The frequency window the comparison is allowed to speak about."""

    start_hz: float
    stop_hz: float

    def __post_init__(self) -> None:
        if not (self.start_hz > 0 and self.stop_hz > self.start_hz):
            raise CorrelationError(
                f"need 0 < start < stop, got {self.start_hz}, {self.stop_hz}")

    def mask(self, frequency: np.ndarray) -> np.ndarray:
        return (frequency >= self.start_hz) & (frequency <= self.stop_hz)


def _to_db(values: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(values), 1e-30))


def common_grid(measured_hz: np.ndarray, simulated_hz: np.ndarray,
                band: Band, tolerance_hz: float = 1.0
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Frequencies present in BOTH sweeps and inside the band.

    Interpolating one onto the other is not offered. A VNA sweep and a solver
    sweep disagree about their grids for real reasons, and silently resampling
    the measurement invents data points that were never taken - which then
    appear in the residual as if they had been.
    """
    measured = np.asarray(measured_hz, dtype=float)
    simulated = np.asarray(simulated_hz, dtype=float)
    measured_index: list[int] = []
    simulated_index: list[int] = []
    shared: list[float] = []
    for position, value in enumerate(measured):
        if not band.mask(np.array([value]))[0]:
            continue
        distance = np.abs(simulated - value)
        nearest = int(np.argmin(distance))
        if distance[nearest] <= tolerance_hz:
            measured_index.append(position)
            simulated_index.append(nearest)
            shared.append(value)
    return (np.array(shared, dtype=float),
            np.array(measured_index, dtype=int),
            np.array(simulated_index, dtype=int))


@dataclass
class Residual:
    """The disagreement on one metric, over one band."""

    metric: str
    points: int
    mean: float
    mean_absolute: float
    rms: float
    worst: float
    worst_frequency_hz: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "points": self.points,
            # The signed mean is a BIAS: a model that is uniformly 0.4 dB
            # optimistic reads very differently from one that is 0.4 dB out
            # in random directions, and only the signed figure separates them.
            "mean": self.mean,
            "mean_absolute": self.mean_absolute,
            "rms": self.rms,
            "worst": self.worst,
            "worst_frequency_hz": self.worst_frequency_hz,
        }


def residual(metric: str, frequency: np.ndarray, measured: np.ndarray,
             simulated: np.ndarray) -> Residual:
    if metric not in METRICS:
        raise CorrelationError(
            f"unknown metric {metric!r}; expected one of {METRICS}")
    difference = np.asarray(measured, dtype=float) - np.asarray(simulated,
                                                                dtype=float)
    if difference.size == 0:
        raise CorrelationError(f"{metric}: nothing to compare")
    worst_index = int(np.argmax(np.abs(difference)))
    return Residual(
        metric=metric,
        points=int(difference.size),
        mean=float(np.mean(difference)),
        mean_absolute=float(np.mean(np.abs(difference))),
        rms=float(np.sqrt(np.mean(difference ** 2))),
        worst=float(difference[worst_index]),
        worst_frequency_hz=float(np.asarray(frequency)[worst_index]),
    )


def extract(network, port_out: int, port_in: int, indices: np.ndarray,
            metric: str) -> np.ndarray:
    """One metric of one S-term, at the given sample indices."""
    term = network.s[indices, port_out, port_in]
    if metric == "magnitude_db":
        return _to_db(term)
    if metric == "phase_deg":
        return np.degrees(np.unwrap(np.angle(term)))
    if metric == "group_delay_ps":
        frequency = np.asarray(network.f, dtype=float)[indices]
        if frequency.size < 2:
            raise CorrelationError(
                "group delay needs at least two shared points")
        phase = np.unwrap(np.angle(term))
        return -np.gradient(phase, 2.0 * math.pi * frequency) * 1e12
    raise CorrelationError(f"unknown metric {metric!r}")


def correlate(measured, simulated, band: Band,
              port_out: int = 1, port_in: int = 0,
              metrics: Sequence[str] = ("magnitude_db",),
              tolerance_hz: float = 1.0) -> dict[str, Any]:
    """Compare two networks over a declared band. Never interpolates."""
    if measured.nports != simulated.nports:
        raise CorrelationError(
            f"port counts differ: measured has {measured.nports}, simulated "
            f"has {simulated.nports}; comparing them would compare different "
            "structures")

    shared, measured_index, simulated_index = common_grid(
        np.asarray(measured.f, dtype=float),
        np.asarray(simulated.f, dtype=float), band, tolerance_hz)

    overlap = {
        "shared_points": int(shared.size),
        "measured_points_in_band": int(
            np.count_nonzero(band.mask(np.asarray(measured.f, dtype=float)))),
        "simulated_points_in_band": int(
            np.count_nonzero(band.mask(np.asarray(simulated.f, dtype=float)))),
        "band_hz": {"start": band.start_hz, "stop": band.stop_hz},
        "start_hz": float(shared[0]) if shared.size else None,
        "stop_hz": float(shared[-1]) if shared.size else None,
    }

    if shared.size < MIN_SHARED_POINTS:
        return {
            "sufficient": False,
            "overlap": overlap,
            "residuals": {},
            "reason": (
                f"only {shared.size} shared frequency point(s) in band; "
                f"at least {MIN_SHARED_POINTS} are needed before a mean "
                "residual describes anything but noise"),
        }

    residuals: dict[str, Any] = {}
    for metric in metrics:
        measured_values = extract(measured, port_out, port_in,
                                  measured_index, metric)
        simulated_values = extract(simulated, port_out, port_in,
                                   simulated_index, metric)
        residuals[metric] = residual(metric, shared, measured_values,
                                     simulated_values).to_dict()

    return {"sufficient": True, "overlap": overlap, "residuals": residuals}


# --------------------------------------------------------------------------- #
# Model feedback
# --------------------------------------------------------------------------- #
def suggest_er_correction(measured_delay_ps: float, simulated_delay_ps: float,
                          assumed_er_eff: float) -> dict[str, Any]:
    """What er_eff would have reproduced the measured delay.

    Delay scales as sqrt(er_eff), so

        er_eff_measured = er_eff_assumed * (t_measured / t_simulated)^2

    This is a POINTER, not an update: the same delay error can come from a
    wrong er, a wrong length, or a fixture that was not de-embedded, and this
    function cannot tell those apart. Nothing applies it automatically.
    """
    if simulated_delay_ps <= 0 or measured_delay_ps <= 0:
        raise CorrelationError(
            f"delays must be positive, got measured={measured_delay_ps}, "
            f"simulated={simulated_delay_ps}")
    if assumed_er_eff <= 0:
        raise CorrelationError(f"er_eff must be positive, got {assumed_er_eff}")
    ratio = measured_delay_ps / simulated_delay_ps
    implied = assumed_er_eff * ratio ** 2
    return {
        "assumed_er_eff": assumed_er_eff,
        "implied_er_eff": implied,
        "delay_ratio": ratio,
        "change_percent": (implied - assumed_er_eff) / assumed_er_eff * 100.0,
        "caveat": (
            "a delay error can equally be a wrong length or an un-de-embedded "
            "fixture; this is a pointer for investigation, not a correction "
            "to apply"),
    }
