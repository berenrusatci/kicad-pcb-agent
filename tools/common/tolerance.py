"""Component and geometry tolerance models for Monte Carlo analysis.

WHY THIS EXISTS
---------------
Every number this toolchain produces is currently a SINGLE-POINT NOMINAL. The
IPC calculators, the PDN work, the impedance solve and the ngspice bridge all
take one value per component and report one answer. Real boards ship with
resistor tolerance, MLCC DC-bias derating, laminate thickness spread and etch
variation, and the question a fab actually asks - "what fraction of boards
lands inside the window?" - has had no answer here at all.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It does not guess a distribution. A part marked "1%" does not tell you whether
that 1% is a 1-sigma, 3-sigma or hard uniform bound, and the three give very
different yields. So ``normal_from_tolerance`` requires an explicit
``sigma_divisor`` and there is no default: the caller has to state which
convention their vendor uses, and the report records it.

The same applies to MLCC DC-bias. The capacitance of a class-II ceramic falls
with applied voltage, often by more than half, and the curve is specific to the
part number, dielectric and case size. There is no generic formula to reach
for. ``DcBiasDerating`` therefore interpolates a curve the CALLER supplies from
the vendor's data; a capacitor with a DC bias and no curve is reported as
NO_COVERAGE rather than simulated at its printed value, which would silently
overstate the decoupling on every rail.

DETERMINISM
-----------
Every sample is drawn from an explicitly seeded ``numpy`` Generator and the
seed travels in the report, so a Monte Carlo verdict can be reproduced exactly.
An unreproducible statistical claim is not evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

DISTRIBUTIONS = ("normal", "normal_from_tolerance", "uniform", "fixed")


class ToleranceError(ValueError):
    """The tolerance specification is incomplete or self-contradictory."""


# --------------------------------------------------------------------------- #
# DC-bias derating
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DcBiasDerating:
    """Capacitance retention versus applied DC volts, from vendor data.

    ``points`` is [(volts, retention_fraction), ...] with retention in (0, 1].
    Interpolation is linear between supplied points and CLAMPED at the ends -
    never extrapolated, because the curve beyond the vendor's plot is not
    knowledge we have.
    """

    points: tuple[tuple[float, float], ...]
    source: str

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ToleranceError(
                "a DC-bias curve needs at least two points; one point is not "
                "a curve and would be indistinguishable from a guess")
        volts = [v for v, _ in self.points]
        if list(volts) != sorted(volts):
            raise ToleranceError("DC-bias points must be sorted by voltage")
        for volt, retention in self.points:
            if volt < 0:
                raise ToleranceError(f"negative bias voltage: {volt}")
            if not 0.0 < retention <= 1.0:
                raise ToleranceError(
                    f"retention must be in (0, 1], got {retention} at {volt} V")
        if not self.source.strip():
            raise ToleranceError(
                "a DC-bias curve must name its source; an unattributed curve "
                "is indistinguishable from an invented one")

    def retention_at(self, volts: float) -> float:
        """Fraction of nominal capacitance retained at ``volts``, clamped."""
        if volts <= self.points[0][0]:
            return self.points[0][1]
        if volts >= self.points[-1][0]:
            return self.points[-1][1]
        xs = [v for v, _ in self.points]
        ys = [r for _, r in self.points]
        return float(np.interp(volts, xs, ys))


# --------------------------------------------------------------------------- #
# Parameter specification
# --------------------------------------------------------------------------- #
@dataclass
class ParameterSpec:
    """One varying quantity: a component value or a geometric dimension.

    ``distribution``:
      normal                 - ``sigma`` or ``sigma_percent`` given directly
      normal_from_tolerance  - ``tolerance_percent`` PLUS an explicit
                               ``sigma_divisor`` (e.g. 3.0 for "tolerance is
                               3 sigma"). No default: see the module docstring.
      uniform                - flat over nominal * (1 +- tolerance_percent/100)
      fixed                  - held at nominal; present so a swept study can
                               pin one parameter without deleting its entry
    """

    name: str
    nominal: float
    distribution: str = "fixed"
    sigma: float | None = None
    sigma_percent: float | None = None
    tolerance_percent: float | None = None
    sigma_divisor: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    dc_bias_volts: float | None = None
    dc_bias_curve: DcBiasDerating | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.distribution not in DISTRIBUTIONS:
            raise ToleranceError(
                f"{self.name}: unknown distribution {self.distribution!r}; "
                f"expected one of {DISTRIBUTIONS}")
        if not math.isfinite(self.nominal):
            raise ToleranceError(f"{self.name}: nominal must be finite")

        if self.distribution == "normal":
            if self.sigma is None and self.sigma_percent is None:
                raise ToleranceError(
                    f"{self.name}: distribution 'normal' needs sigma or "
                    f"sigma_percent")
        elif self.distribution == "normal_from_tolerance":
            if self.tolerance_percent is None:
                raise ToleranceError(
                    f"{self.name}: needs tolerance_percent")
            if self.sigma_divisor is None:
                raise ToleranceError(
                    f"{self.name}: normal_from_tolerance needs an explicit "
                    f"sigma_divisor. A '1%' part may mean 1 sigma or 3 sigma "
                    f"and the yields differ by orders of magnitude, so this "
                    f"tool will not pick one for you")
            if self.sigma_divisor <= 0:
                raise ToleranceError(
                    f"{self.name}: sigma_divisor must be positive")
        elif self.distribution == "uniform":
            if self.tolerance_percent is None:
                raise ToleranceError(
                    f"{self.name}: distribution 'uniform' needs "
                    f"tolerance_percent")

        if (self.minimum is not None and self.maximum is not None
                and self.minimum > self.maximum):
            raise ToleranceError(f"{self.name}: minimum above maximum")

        if self.dc_bias_volts is not None and self.dc_bias_curve is None:
            self.notes.append(
                "NO_COVERAGE: a DC bias is declared but no derating curve was "
                "supplied; the nominal capacitance is NOT valid under bias")

    # -- derived ----------------------------------------------------------- #
    def effective_nominal(self) -> float:
        """Nominal after any DC-bias derating that the caller could supply."""
        if self.dc_bias_curve is not None and self.dc_bias_volts is not None:
            return self.nominal * self.dc_bias_curve.retention_at(
                self.dc_bias_volts)
        return self.nominal

    def has_coverage_gap(self) -> bool:
        return any(note.startswith("NO_COVERAGE") for note in self.notes)

    def sigma_value(self) -> float:
        """Standard deviation in absolute units, 0 for non-normal specs."""
        centre = abs(self.effective_nominal())
        if self.distribution == "normal":
            if self.sigma is not None:
                return float(self.sigma)
            return centre * float(self.sigma_percent) / 100.0
        if self.distribution == "normal_from_tolerance":
            return (centre * float(self.tolerance_percent) / 100.0
                    / float(self.sigma_divisor))
        return 0.0

    def sample(self, rng: np.random.Generator, count: int) -> np.ndarray:
        """Draw ``count`` values. Bounds are applied by CLIPPING, and clipping
        is reported by ``clipped_fraction`` rather than hidden: a spec whose
        bounds bite hard is no longer the distribution it claims to be."""
        centre = self.effective_nominal()
        if self.distribution == "fixed":
            values = np.full(count, centre, dtype=float)
        elif self.distribution == "uniform":
            half = abs(centre) * float(self.tolerance_percent) / 100.0
            values = rng.uniform(centre - half, centre + half, size=count)
        else:
            values = rng.normal(centre, self.sigma_value(), size=count)

        if self.minimum is not None:
            values = np.maximum(values, self.minimum)
        if self.maximum is not None:
            values = np.minimum(values, self.maximum)
        return values

    def clipped_fraction(self, values: np.ndarray) -> float:
        at_bound = np.zeros(values.shape, dtype=bool)
        if self.minimum is not None:
            at_bound |= np.isclose(values, self.minimum)
        if self.maximum is not None:
            at_bound |= np.isclose(values, self.maximum)
        return float(np.count_nonzero(at_bound)) / max(values.size, 1)

    def describe(self) -> dict[str, object]:
        described: dict[str, object] = {
            "name": self.name,
            "nominal": self.nominal,
            "effective_nominal": self.effective_nominal(),
            "distribution": self.distribution,
        }
        if self.distribution.startswith("normal"):
            described["sigma"] = self.sigma_value()
        if self.tolerance_percent is not None:
            described["tolerance_percent"] = self.tolerance_percent
        if self.sigma_divisor is not None:
            described["sigma_divisor"] = self.sigma_divisor
        if self.minimum is not None:
            described["minimum"] = self.minimum
        if self.maximum is not None:
            described["maximum"] = self.maximum
        if self.dc_bias_volts is not None:
            described["dc_bias_volts"] = self.dc_bias_volts
            described["dc_bias_source"] = (
                self.dc_bias_curve.source if self.dc_bias_curve else None)
        if self.notes:
            described["notes"] = list(self.notes)
        return described


# --------------------------------------------------------------------------- #
# Specification parsing
# --------------------------------------------------------------------------- #
def parse_dc_bias_curve(raw: Mapping[str, object]) -> DcBiasDerating:
    points = raw.get("points")
    if not isinstance(points, (list, tuple)):
        raise ToleranceError("dc_bias_curve.points must be a list of [V, retention]")
    parsed = tuple((float(v), float(r)) for v, r in points)   # type: ignore[misc]
    source = str(raw.get("source", "")).strip()
    return DcBiasDerating(points=parsed, source=source)


def parse_parameter(name: str, raw: Mapping[str, object]) -> ParameterSpec:
    curve = None
    if "dc_bias_curve" in raw and raw["dc_bias_curve"] is not None:
        curve = parse_dc_bias_curve(raw["dc_bias_curve"])   # type: ignore[arg-type]
    known = {
        "nominal", "distribution", "sigma", "sigma_percent",
        "tolerance_percent", "sigma_divisor", "minimum", "maximum",
        "dc_bias_volts",
    }
    unknown = set(raw) - known - {"dc_bias_curve"}
    if unknown:
        raise ToleranceError(
            f"{name}: unknown tolerance keys {sorted(unknown)}; a typo here "
            f"would silently disable the variation it was meant to describe")
    if "nominal" not in raw:
        raise ToleranceError(f"{name}: nominal is required")
    return ParameterSpec(
        name=name,
        nominal=float(raw["nominal"]),                       # type: ignore[arg-type]
        distribution=str(raw.get("distribution", "fixed")),
        sigma=_optional_float(raw.get("sigma")),
        sigma_percent=_optional_float(raw.get("sigma_percent")),
        tolerance_percent=_optional_float(raw.get("tolerance_percent")),
        sigma_divisor=_optional_float(raw.get("sigma_divisor")),
        minimum=_optional_float(raw.get("minimum")),
        maximum=_optional_float(raw.get("maximum")),
        dc_bias_volts=_optional_float(raw.get("dc_bias_volts")),
        dc_bias_curve=curve,
    )


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)      # type: ignore[arg-type]


def parse_specification(raw: Mapping[str, object]) -> list[ParameterSpec]:
    """Parse ``{"parameters": {"R1": {...}, ...}}`` into ParameterSpecs."""
    parameters = raw.get("parameters")
    if not isinstance(parameters, Mapping) or not parameters:
        raise ToleranceError(
            "specification needs a non-empty 'parameters' object; a Monte "
            "Carlo run with nothing varying is a nominal run wearing a hat")
    return [parse_parameter(name, spec)          # type: ignore[arg-type]
            for name, spec in sorted(parameters.items())]


# --------------------------------------------------------------------------- #
# Sampling and statistics
# --------------------------------------------------------------------------- #
def sample_parameters(specs: Sequence[ParameterSpec], trials: int,
                      seed: int) -> dict[str, np.ndarray]:
    """Independent draws per parameter. Correlation is NOT modelled.

    Parts from one reel do track each other, so treating them as independent
    is optimistic for matched pairs and pessimistic for absolute accuracy. The
    report says so rather than leaving the reader to assume either.
    """
    if trials < 1:
        raise ToleranceError(f"trials must be >= 1, got {trials}")
    rng = np.random.default_rng(seed)
    return {spec.name: spec.sample(rng, trials) for spec in specs}


def summarise(values: Iterable[float]) -> dict[str, float]:
    """Distribution summary. Percentiles are reported, not just mean/sigma,
    because a yield question is about the tails."""
    array = np.asarray(list(values), dtype=float)
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        raise ToleranceError("no finite samples to summarise")
    return {
        "count": int(finite.size),
        "discarded_non_finite": int(array.size - finite.size),
        "mean": float(np.mean(finite)),
        "std": float(np.std(finite, ddof=1)) if finite.size > 1 else 0.0,
        "minimum": float(np.min(finite)),
        "maximum": float(np.max(finite)),
        "p01": float(np.percentile(finite, 1)),
        "p05": float(np.percentile(finite, 5)),
        "p50": float(np.percentile(finite, 50)),
        "p95": float(np.percentile(finite, 95)),
        "p99": float(np.percentile(finite, 99)),
    }


def yield_against_limits(values: Iterable[float],
                         lower: float | None = None,
                         upper: float | None = None) -> dict[str, object]:
    """Fraction inside [lower, upper], with a binomial confidence interval.

    The interval matters: 100/100 passing trials is NOT evidence of 100%
    yield, and reporting a bare fraction invites exactly that reading. The
    Wilson score interval is used because the normal approximation is wrong
    at precisely the extremes a yield study cares about.
    """
    array = np.asarray(list(values), dtype=float)
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        raise ToleranceError("no finite samples to evaluate")
    if lower is None and upper is None:
        raise ToleranceError("a yield needs at least one limit")

    inside = np.ones(finite.shape, dtype=bool)
    if lower is not None:
        inside &= finite >= lower
    if upper is not None:
        inside &= finite <= upper

    passes = int(np.count_nonzero(inside))
    total = int(finite.size)
    low, high = _wilson_interval(passes, total)
    return {
        "lower_limit": lower,
        "upper_limit": upper,
        "passes": passes,
        "trials": total,
        "yield_fraction": passes / total,
        "yield_ci95_low": low,
        "yield_ci95_high": high,
        "failures_below": int(np.count_nonzero(finite < lower))
        if lower is not None else 0,
        "failures_above": int(np.count_nonzero(finite > upper))
        if upper is not None else 0,
    }


def _wilson_interval(passes: int, total: int,
                     z_score: float = 1.959963984540054
                     ) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if total <= 0:
        raise ToleranceError("cannot form an interval over zero trials")
    proportion = passes / total
    denominator = 1.0 + z_score ** 2 / total
    centre = proportion + z_score ** 2 / (2 * total)
    spread = z_score * math.sqrt(
        proportion * (1 - proportion) / total + z_score ** 2 / (4 * total ** 2))
    return (max(0.0, (centre - spread) / denominator),
            min(1.0, (centre + spread) / denominator))


def sensitivity(nominal_output: float, perturbed_output: float,
                nominal_input: float, perturbed_input: float) -> float:
    """Finite-difference d(output)/d(input). Used to linearise an expensive
    model (a full field solve) so thousands of trials cost a handful of runs."""
    delta_input = perturbed_input - nominal_input
    if delta_input == 0:
        raise ToleranceError("cannot take a sensitivity over a zero step")
    return (perturbed_output - nominal_output) / delta_input
