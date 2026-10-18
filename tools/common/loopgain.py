"""Loop-gain stability margins read from a measured or simulated response.

WHAT IS AND IS NOT VENDOR-SPECIFIC
--------------------------------------------------------------------------
A regulator vendor's compensation model is theirs and is not reproduced
here. Reading margins off a loop-gain response is not: it is arithmetic on
a sweep that came from an AC simulation or a network analyser. So this
module never models a loop - it reads one someone else produced, and every
acceptance threshold is an INPUT rather than a number invented here.

FOUR WAYS A SINGLE "PHASE MARGIN" NUMBER LIES
--------------------------------------------------------------------------
1. **More than one crossover.** A loop gain can cross 0 dB several times.
   Each crossing has its own phase margin, and the loop is only as good as
   the WORST of them. A tool that finds "the" crossover reports the first
   or the last and silently discards the rest.
2. **Conditional stability.** Phase can dip past -180 degrees while gain is
   still ABOVE 0 dB. Such a loop is stable as built and goes unstable if
   the gain drops - which is what happens as components age, as the load
   falls, or when a capacitor loses value to DC bias. A phase margin
   measured at crossover says nothing about this and looks fine.
3. **Wrapped phase.** Measured and simulated phase arrives wrapped into
   +/-180 degrees. Computing a margin from wrapped phase produces a number
   that is confidently wrong rather than obviously wrong, because it is
   still in a plausible range. Phase is unwrapped here, and the report says
   how much unwrapping was applied so the caller can see it happened.
4. **A sweep that does not contain the crossing.** If the response never
   reaches 0 dB inside the swept band, there is no margin to report. That
   is NOT a pass, and it is not an extrapolation problem to solve - it says
   the band was too narrow or the loop is not the one you think it is.

RESOLUTION IS A REPORTED QUANTITY
--------------------------------------------------------------------------
A crossing is located by interpolating between two samples. How far apart
those samples are bounds how well it can be located, so the bracketing
points and their spacing travel with every crossing. A crossover reported
to four digits from decade-spaced samples is a false precision this module
declines to produce.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Reason codes.
NO_CROSSOVER = "NO_GAIN_CROSSOVER_IN_BAND"
MULTIPLE_CROSSOVERS = "MULTIPLE_GAIN_CROSSOVERS"
CONDITIONALLY_STABLE = "CONDITIONALLY_STABLE"
PHASE_MARGIN_LOW = "PHASE_MARGIN_BELOW_BUDGET"
GAIN_MARGIN_LOW = "GAIN_MARGIN_BELOW_BUDGET"
COARSE_SAMPLING = "CROSSING_POORLY_BRACKETED"
NO_PHASE_CROSSOVER = "NO_PHASE_CROSSOVER_IN_BAND"


@dataclass(frozen=True)
class Sample:
    freq_hz: float
    mag_db: float
    phase_deg: float


@dataclass(frozen=True)
class Crossing:
    """One 0 dB gain crossing, with everything needed to judge its quality."""

    freq_hz: float
    phase_deg: float                # unwrapped phase at the crossing
    phase_margin_deg: float
    direction: str                  # "falling" (normal) or "rising"
    bracket_hz: Tuple[float, float]
    bracket_decades: float

    def to_dict(self) -> dict:
        return {
            "freq_hz": self.freq_hz,
            "phase_deg": self.phase_deg,
            "phase_margin_deg": self.phase_margin_deg,
            "direction": self.direction,
            "bracket_hz": list(self.bracket_hz),
            "bracket_decades": self.bracket_decades,
        }


@dataclass(frozen=True)
class PhaseCrossing:
    """One -180 degree phase crossing and the gain margin there."""

    freq_hz: float
    mag_db: float
    gain_margin_db: float
    above_unity: bool               # gain still above 0 dB at this crossing
    bracket_hz: Tuple[float, float]

    def to_dict(self) -> dict:
        return {
            "freq_hz": self.freq_hz,
            "mag_db": self.mag_db,
            "gain_margin_db": self.gain_margin_db,
            "above_unity": self.above_unity,
            "bracket_hz": list(self.bracket_hz),
        }


def load_samples(rows: Sequence[Sequence[float]]) -> List[Sample]:
    """Build a sweep from ``[(f, mag_db, phase_deg), ...]``.

    Rejects an unsorted or duplicated frequency axis rather than sorting
    silently: a sweep whose points are out of order usually means two
    sweeps were concatenated, and interpolating across that seam invents a
    crossing where the data has a discontinuity.
    """
    if len(rows) < 2:
        raise ValueError("a sweep needs at least two points")
    samples = []
    for row in rows:
        if len(row) < 3:
            raise ValueError(f"each row needs (freq_hz, mag_db, phase_deg), got {row!r}")
        freq, mag, phase = float(row[0]), float(row[1]), float(row[2])
        if freq <= 0:
            raise ValueError(f"frequency must be > 0 (log axis), got {freq}")
        samples.append(Sample(freq, mag, phase))
    for earlier, later in zip(samples, samples[1:]):
        if later.freq_hz <= earlier.freq_hz:
            raise ValueError(
                f"frequency axis must strictly increase; {earlier.freq_hz} is "
                f"followed by {later.freq_hz}. An unsorted axis usually means "
                "two sweeps were concatenated, and interpolating across the "
                "seam invents a crossing.")
    return samples


def unwrap_phase(samples: Sequence[Sample]) -> Tuple[List[float], int]:
    """Unwrap phase, returning the unwrapped series and the wrap count.

    The count is returned rather than hidden because it is the evidence
    that unwrapping happened at all. A caller looking at a -170 degree
    margin has no way to tell a genuine value from a wrapped one; the count
    tells them.
    """
    unwrapped: List[float] = []
    offset = 0.0
    wraps = 0
    previous: Optional[float] = None
    for sample in samples:
        phase = sample.phase_deg
        if previous is not None:
            delta = phase - previous
            if delta > 180.0:
                offset -= 360.0
                wraps += 1
            elif delta < -180.0:
                offset += 360.0
                wraps += 1
        unwrapped.append(phase + offset)
        previous = phase
    return unwrapped, wraps


def _log_interp(x0: float, y0: float, x1: float, y1: float, target: float) -> float:
    """Frequency of ``target`` between two points, interpolating linearly in
    log-frequency because that is the axis a Bode response is linear on."""
    if y1 == y0:
        return x0
    fraction = (target - y0) / (y1 - y0)
    log_f = math.log10(x0) + fraction * (math.log10(x1) - math.log10(x0))
    return 10.0 ** log_f


def _interp_at(x0: float, y0: float, x1: float, y1: float, freq: float) -> float:
    if x1 == x0:
        return y0
    fraction = (math.log10(freq) - math.log10(x0)) / (math.log10(x1) - math.log10(x0))
    return y0 + fraction * (y1 - y0)


def find_gain_crossings(samples: Sequence[Sample],
                        unwrapped: Sequence[float]) -> List[Crossing]:
    """Every 0 dB crossing in the band, not just the first."""
    crossings: List[Crossing] = []
    for i in range(len(samples) - 1):
        a, b = samples[i], samples[i + 1]
        if a.mag_db == 0.0 and b.mag_db == 0.0:
            continue
        if (a.mag_db > 0.0) == (b.mag_db > 0.0):
            continue
        freq = _log_interp(a.freq_hz, a.mag_db, b.freq_hz, b.mag_db, 0.0)
        phase = _interp_at(a.freq_hz, unwrapped[i], b.freq_hz, unwrapped[i + 1], freq)
        # Margin is measured from -180 degrees, which is where the negative
        # feedback has become positive.
        margin = phase + 180.0
        decades = math.log10(b.freq_hz / a.freq_hz)
        crossings.append(Crossing(
            freq_hz=freq, phase_deg=phase, phase_margin_deg=margin,
            direction="falling" if a.mag_db > b.mag_db else "rising",
            bracket_hz=(a.freq_hz, b.freq_hz), bracket_decades=decades))
    return crossings


def find_phase_crossings(samples: Sequence[Sample],
                         unwrapped: Sequence[float]) -> List[PhaseCrossing]:
    """Every -180 degree crossing, with the gain margin at each."""
    crossings: List[PhaseCrossing] = []
    for i in range(len(samples) - 1):
        p0, p1 = unwrapped[i], unwrapped[i + 1]
        if (p0 > -180.0) == (p1 > -180.0):
            continue
        a, b = samples[i], samples[i + 1]
        freq = _log_interp(a.freq_hz, p0, b.freq_hz, p1, -180.0)
        mag = _interp_at(a.freq_hz, a.mag_db, b.freq_hz, b.mag_db, freq)
        crossings.append(PhaseCrossing(
            freq_hz=freq, mag_db=mag, gain_margin_db=-mag,
            above_unity=mag > 0.0, bracket_hz=(a.freq_hz, b.freq_hz)))
    return crossings


def is_conditionally_stable(phase_crossings: Sequence[PhaseCrossing]) -> bool:
    """True when phase passes -180 degrees while gain is still above 0 dB.

    Such a loop is stable at its nominal gain and unstable if the gain
    FALLS - the direction gain moves as parts age, as a ceramic capacitor
    loses value to DC bias, or as the load lightens. A phase margin taken
    at crossover cannot see this, which is why it is reported separately.
    """
    return any(crossing.above_unity for crossing in phase_crossings)


@dataclass
class StabilityReport:
    crossings: List[Crossing]
    phase_crossings: List[PhaseCrossing]
    wraps_removed: int
    band_hz: Tuple[float, float]

    @property
    def worst_phase_margin_deg(self) -> Optional[float]:
        """The smallest margin among all crossings - a loop is only as good
        as its worst one."""
        if not self.crossings:
            return None
        return min(c.phase_margin_deg for c in self.crossings)

    @property
    def worst_gain_margin_db(self) -> Optional[float]:
        if not self.phase_crossings:
            return None
        return min(c.gain_margin_db for c in self.phase_crossings)

    def reason_codes(self, min_phase_margin_deg: Optional[float] = None,
                     min_gain_margin_db: Optional[float] = None,
                     max_bracket_decades: float = 0.25) -> List[str]:
        codes: List[str] = []
        if not self.crossings:
            codes.append(NO_CROSSOVER)
        if len(self.crossings) > 1:
            codes.append(MULTIPLE_CROSSOVERS)
        if not self.phase_crossings:
            codes.append(NO_PHASE_CROSSOVER)
        if is_conditionally_stable(self.phase_crossings):
            codes.append(CONDITIONALLY_STABLE)
        if any(c.bracket_decades > max_bracket_decades for c in self.crossings):
            codes.append(COARSE_SAMPLING)
        worst_pm = self.worst_phase_margin_deg
        if (min_phase_margin_deg is not None and worst_pm is not None
                and worst_pm < min_phase_margin_deg):
            codes.append(PHASE_MARGIN_LOW)
        worst_gm = self.worst_gain_margin_db
        if (min_gain_margin_db is not None and worst_gm is not None
                and worst_gm < min_gain_margin_db):
            codes.append(GAIN_MARGIN_LOW)
        return codes

    def to_dict(self) -> dict:
        return {
            "band_hz": list(self.band_hz),
            "phase_wraps_removed": self.wraps_removed,
            "gain_crossings": [c.to_dict() for c in self.crossings],
            "phase_crossings": [c.to_dict() for c in self.phase_crossings],
            "worst_phase_margin_deg": self.worst_phase_margin_deg,
            "worst_gain_margin_db": self.worst_gain_margin_db,
            "conditionally_stable": is_conditionally_stable(self.phase_crossings),
        }


def analyse(rows: Sequence[Sequence[float]]) -> StabilityReport:
    """Read a sweep and locate every margin in it."""
    samples = load_samples(rows)
    unwrapped, wraps = unwrap_phase(samples)
    return StabilityReport(
        crossings=find_gain_crossings(samples, unwrapped),
        phase_crossings=find_phase_crossings(samples, unwrapped),
        wraps_removed=wraps,
        band_hz=(samples[0].freq_hz, samples[-1].freq_hz))
