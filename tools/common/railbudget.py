"""Power-up sequencing and per-rail noise budgets.

TWO CHECKS THAT SHARE A SUBJECT
--------------------------------------------------------------------------
Sequencing asks whether the rails come up in the order the design says they
do. Noise budgeting asks whether what arrives on a rail fits inside what
the load will tolerate. They live together because both are properties of a
rail that no ERC and no DRC can see: the netlist proves an enable pin is
CONNECTED, never that the thing connected to it can turn it on.

THE SEQUENCING FAILURE THIS EXISTS FOR
--------------------------------------------------------------------------
An enable pin driven by a rail whose voltage sits near the regulator's own
V_EN(HI) threshold. The connection is present, ERC is silent, and the
regulator turns on - at room temperature, on the first board. The margin is
what decides whether it still does at -40 C on the hundredth. So the check
is not "is EN connected" but "how many millivolts of margin does the
driving rail have over the threshold", and the threshold is a datasheet
input that is never defaulted.

The second sequencing failure is quieter: a written order that the wiring
does not implement. A document says 1.8 V comes up first; the enable graph
says something else. Nothing compares the two, so they drift apart, and the
document is what the next engineer reads.

WHY NOISE CONTRIBUTORS CANNOT ALL BE ADDED THE SAME WAY
--------------------------------------------------------------------------
Deterministic contributors - switching ripple, a load step - are correlated
and add LINEARLY in the worst case. Random contributors - regulator noise,
thermal noise - are uncorrelated and add in QUADRATURE. Summing a random
term linearly overstates it, and summing a deterministic term in quadrature
UNDERSTATES it, which is the dangerous direction. So every contributor
declares its kind, each family is summed by its own rule, and the two
totals are then combined linearly - the pessimistic choice, because nothing
here knows their relative phase.

PSRR IS A CURVE, NOT A NUMBER
--------------------------------------------------------------------------
Attenuating an input ripple by a regulator's PSRR requires the PSRR AT THE
RIPPLE'S FREQUENCY. A PSRR quoted at 1 kHz says nothing about a 2 MHz
switching residue, and regulators routinely lose 40 dB between the two.
This module refuses to apply a PSRR figure quoted at a different frequency
rather than interpolating one it cannot see.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

# Reason codes.
EN_BELOW_THRESHOLD = "ENABLE_BELOW_THRESHOLD"
EN_MARGIN_THIN = "ENABLE_MARGIN_THIN"
EN_THRESHOLD_UNDECLARED = "ENABLE_THRESHOLD_NOT_DECLARED"
ORDER_CONTRADICTS_WIRING = "DECLARED_ORDER_CONTRADICTS_ENABLE_GRAPH"
ENABLE_CYCLE = "ENABLE_CYCLE"
UNKNOWN_ENABLE_SOURCE = "ENABLE_SOURCE_NOT_A_KNOWN_RAIL"
BUDGET_EXCEEDED = "NOISE_BUDGET_EXCEEDED"
PSRR_FREQUENCY_MISMATCH = "PSRR_QUOTED_AT_A_DIFFERENT_FREQUENCY"
NO_BUDGET = "NOISE_BUDGET_NOT_DECLARED"

ALWAYS_ON = "always_on"

DETERMINISTIC = "deterministic"
RANDOM = "random"
KINDS = (DETERMINISTIC, RANDOM)


# ------------------------------------------------------------- sequencing


@dataclass(frozen=True)
class Rail:
    """One rail and what turns it on.

    ``enable_source`` is the rail feeding this regulator's EN pin, or
    ``ALWAYS_ON`` when the regulator has no gate of its own.
    ``en_threshold_v`` is the datasheet V_EN(HI); ``None`` means it was not
    declared, which is reported rather than assumed.
    """

    name: str
    voltage_v: float
    enable_source: str = ALWAYS_ON
    en_threshold_v: Optional[float] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "voltage_v": self.voltage_v,
                "enable_source": self.enable_source,
                "en_threshold_v": self.en_threshold_v}


@dataclass
class EnableCheck:
    rail: str
    source: str
    source_voltage_v: Optional[float]
    threshold_v: Optional[float]
    margin_v: Optional[float]
    codes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"rail": self.rail, "source": self.source,
                "source_voltage_v": self.source_voltage_v,
                "threshold_v": self.threshold_v,
                "margin_v": self.margin_v, "reason_codes": list(self.codes)}


def check_enable_margins(rails: Sequence[Rail],
                         min_margin_v: Optional[float] = None) -> List[EnableCheck]:
    """Can each enable source actually reach its regulator's threshold?"""
    by_name = {rail.name: rail for rail in rails}
    checks: List[EnableCheck] = []
    for rail in rails:
        if rail.enable_source == ALWAYS_ON:
            continue
        source = by_name.get(rail.enable_source)
        codes: List[str] = []
        source_v = source.voltage_v if source else None
        if source is None:
            codes.append(UNKNOWN_ENABLE_SOURCE)
        if rail.en_threshold_v is None:
            codes.append(EN_THRESHOLD_UNDECLARED)
        margin = None
        if source is not None and rail.en_threshold_v is not None:
            margin = source.voltage_v - rail.en_threshold_v
            if margin < 0:
                codes.append(EN_BELOW_THRESHOLD)
            elif min_margin_v is not None and margin < min_margin_v:
                codes.append(EN_MARGIN_THIN)
        checks.append(EnableCheck(rail.name, rail.enable_source, source_v,
                                  rail.en_threshold_v, margin, codes))
    return checks


def enable_order(rails: Sequence[Rail]) -> List[List[str]]:
    """Rails grouped into the tiers the enable graph forces.

    Tier 0 is everything always-on; tier N is everything enabled by tier
    N-1. Rails inside a tier come up together - the graph does not order
    them, and pretending it does would invent a sequence the hardware never
    promised.

    Raises on a cycle: a cycle is not a sequence, and a topological sort
    that silently drops the cycle's members reports a shorter, plausible
    order instead.
    """
    by_name = {rail.name: rail for rail in rails}
    tier: Dict[str, int] = {}
    remaining = {rail.name for rail in rails}
    level = 0
    while remaining:
        ready = []
        for name in sorted(remaining):
            source = by_name[name].enable_source
            if source == ALWAYS_ON or source not in remaining:
                ready.append(name)
        if not ready:
            raise ValueError(
                f"{ENABLE_CYCLE}: {sorted(remaining)} enable each other; a "
                "cycle is not a sequence and no order exists")
        for name in ready:
            tier[name] = level
            remaining.discard(name)
        level += 1
    tiers: List[List[str]] = [[] for _ in range(level)]
    for name, index in tier.items():
        tiers[index].append(name)
    return [sorted(group) for group in tiers]


def compare_declared_order(rails: Sequence[Rail],
                           declared: Sequence[Sequence[str]]) -> List[dict]:
    """Does the written power-up order match what the enable graph forces?

    ``declared`` is a list of groups: ``[["V1V8"], ["V1V2", "V2V8"]]`` reads
    as "1.8 V first, then the other two together".
    """
    actual = enable_order(rails)
    actual_tier = {name: index for index, group in enumerate(actual)
                   for name in group}
    declared_tier = {name: index for index, group in enumerate(declared)
                     for name in group}
    violations: List[dict] = []

    unknown = sorted(set(declared_tier) - set(actual_tier))
    if unknown:
        violations.append({"issue": ORDER_CONTRADICTS_WIRING,
                           "detail": "the declared order names rails that are "
                                     "not in the rail set",
                           "rails": unknown})
    missing = sorted(set(actual_tier) - set(declared_tier))
    if missing:
        violations.append({"issue": ORDER_CONTRADICTS_WIRING,
                           "detail": "these rails exist but the declared order "
                                     "does not mention them",
                           "rails": missing})

    shared = sorted(set(actual_tier) & set(declared_tier))
    for first in shared:
        for second in shared:
            if first >= second:
                continue
            # Only an inversion is a contradiction. The graph putting two
            # rails in one tier while the document orders them is NOT one:
            # the document may be stating an intent the hardware permits.
            if (declared_tier[first] < declared_tier[second]
                    and actual_tier[first] > actual_tier[second]):
                violations.append({
                    "issue": ORDER_CONTRADICTS_WIRING,
                    "detail": f"the document brings {first} up before "
                              f"{second}, the enable wiring does the reverse",
                    "rails": [first, second],
                    "declared_tiers": [declared_tier[first], declared_tier[second]],
                    "actual_tiers": [actual_tier[first], actual_tier[second]]})
    return violations


# ------------------------------------------------------------ noise budget


@dataclass(frozen=True)
class NoiseContributor:
    """One term in a rail's noise budget.

    ``kind`` decides how it is summed and is therefore not cosmetic.
    """

    name: str
    volts: float
    kind: str = DETERMINISTIC

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.volts < 0:
            raise ValueError(f"{self.name}: contribution cannot be negative")

    def to_dict(self) -> dict:
        return {"name": self.name, "volts": self.volts, "kind": self.kind}


def attenuate_by_psrr(input_ripple_v: float, psrr_db: float,
                      ripple_hz: float, psrr_at_hz: float,
                      tolerance_ratio: float = 1.0) -> float:
    """Input ripple as it appears on the output, given PSRR at ITS frequency.

    Refuses when the PSRR figure was quoted at a different frequency. A
    regulator can lose tens of dB between 1 kHz and a switching residue in
    the megahertz, so reusing the low-frequency figure produces an output
    ripple estimate that is optimistic by orders of magnitude - and
    optimistic is the direction that ships.
    """
    if input_ripple_v < 0:
        raise ValueError("input ripple cannot be negative")
    if ripple_hz <= 0 or psrr_at_hz <= 0:
        raise ValueError("frequencies must be > 0")
    ratio = ripple_hz / psrr_at_hz
    if not (1.0 / (1.0 + tolerance_ratio) <= ratio <= (1.0 + tolerance_ratio)):
        raise ValueError(
            f"{PSRR_FREQUENCY_MISMATCH}: PSRR is quoted at {psrr_at_hz:g} Hz "
            f"but the ripple is at {ripple_hz:g} Hz. PSRR is a curve; this "
            "module will not carry a figure across the gap.")
    return input_ripple_v * (10.0 ** (-psrr_db / 20.0))


def sum_contributors(contributors: Iterable[NoiseContributor]) -> dict:
    """Sum each family by its own rule, then combine the two linearly."""
    items = list(contributors)
    deterministic = sum(c.volts for c in items if c.kind == DETERMINISTIC)
    random_rss = math.sqrt(sum(c.volts ** 2 for c in items if c.kind == RANDOM))
    return {
        "deterministic_v": deterministic,
        "random_rss_v": random_rss,
        "total_v": deterministic + random_rss,
        "contributors": [c.to_dict() for c in items],
        "rule": "deterministic terms add linearly (correlated, worst case); "
                "random terms add in quadrature; the two totals are then "
                "added linearly because their relative phase is unknown",
    }


@dataclass
class RailNoiseResult:
    rail: str
    budget_v: Optional[float]
    summary: dict
    codes: List[str] = field(default_factory=list)

    @property
    def headroom_v(self) -> Optional[float]:
        if self.budget_v is None:
            return None
        return self.budget_v - self.summary["total_v"]

    def to_dict(self) -> dict:
        return {"rail": self.rail, "budget_v": self.budget_v,
                "headroom_v": self.headroom_v, "reason_codes": list(self.codes),
                **self.summary}


def check_noise_budget(rail: str, contributors: Iterable[NoiseContributor],
                       budget_v: Optional[float]) -> RailNoiseResult:
    """Total the contributors and judge them against the budget.

    An absent budget is reported, not assumed. The sum is still produced -
    it is useful on its own - but nothing calls it acceptable, because
    nothing said what acceptable was.
    """
    summary = sum_contributors(contributors)
    codes: List[str] = []
    if budget_v is None:
        codes.append(NO_BUDGET)
    elif summary["total_v"] > budget_v:
        codes.append(BUDGET_EXCEEDED)
    return RailNoiseResult(rail=rail, budget_v=budget_v, summary=summary,
                           codes=codes)
