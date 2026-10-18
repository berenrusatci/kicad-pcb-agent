"""Can a bench tell WHICH rail failed to come up?

THE QUESTION NOTHING IN THIS BUNDLE ASKS
--------------------------------------------------------------------------
``railbudget.py`` knows the enable graph - which rail turns on which, and
what tiers that forces. ``testpoint_plan.py`` knows which nets get a test
point. Neither one asks the question a bench asks at 9am on bring-up day:

    the board does not boot. Which rail did not come up?

If two rails are enabled by the same signal and neither carries an
observation point, their failures produce the SAME symptom. No amount of
probing separates them, because there is nothing to probe. The board comes
back for a second fab cycle to add a test point that cost nothing before
the Gerbers went out.

WHAT IS COUNTED, AND WHY IT IS NOT "RAILS WITH TEST POINTS"
--------------------------------------------------------------------------
Counting rails that have a test point is the wrong metric and it reads
well, which is what makes it dangerous. A plan can cover five of five rails
and still leave three failures indistinguishable if the observation points
sit in the wrong places relative to the sequence.

The unit here is therefore the DISTINGUISHABLE CLASS: a set of rails whose
failures produce the same observable state. Two rails in one class are two
faults a technician cannot tell apart. The metric is how many classes the
plan resolves the rails into, and the failure is a class with more than one
member.

A RAIL TEST POINT AND A POWER-GOOD PIN ARE NOT THE SAME OBSERVATION
--------------------------------------------------------------------------
PG says the regulator BELIEVES it is up. A rail test point says the voltage
is actually there. A regulator can assert PG into a rail that is shorted
downstream, current-limiting at a fraction of its nominal - PG high, rail
low, and a plan that counted PG as coverage would report the rail
observable while the bench sees nothing wrong.

So observation kinds are tracked separately and never merged. A rail whose
only observation is PG is reported as ``PG_ONLY``: real information, and
not a substitute for the rail itself.

RESET IS PART OF THE SEQUENCE, NOT A SEPARATE TOPIC
--------------------------------------------------------------------------
A reset that deasserts before its rails are up produces a boot hang whose
cause is invisible unless BOTH edges can be seen. Declaring a reset without
an observation point on it is ``RESET_UNOBSERVABLE``, and it is called out
separately from the rails because the symptom (a board that is powered and
dead) looks nothing like a missing rail.

WHAT THIS DOES NOT DO
--------------------------------------------------------------------------
It does not decide whether a test point is physically probeable. There is
no placement here, and ``testpoint_plan.py`` already draws that line: its
output is ``phase: early_schematic`` with ``physical: null``. This
inherits the boundary rather than quietly crossing it - the scope of the
PLAN is checked, never the reachability of a pad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .railbudget import ALWAYS_ON, Rail, enable_order

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

# What an observation actually establishes. Kept apart on purpose - see the
# module docstring on PG.
OBS_RAIL = "rail_voltage"        # the voltage is measurably present
OBS_PG = "power_good"            # the regulator says it is up
OBS_ENABLE = "enable_signal"     # the EN input, not the output it gates
OBS_RESET = "reset_signal"
OBS_KINDS = (OBS_RAIL, OBS_PG, OBS_ENABLE, OBS_RESET)

# Only a rail observation proves the rail. The others are real information
# about something else.
PROVES_RAIL = (OBS_RAIL,)

# Reason codes.
INDISTINGUISHABLE = "INDISTINGUISHABLE_FAILURES"
UNOBSERVED_RAIL = "RAIL_UNOBSERVED"
PG_ONLY = "PG_ONLY"
RESET_UNOBSERVABLE = "RESET_UNOBSERVABLE"
RESET_RAIL_UNKNOWN = "RESET_RAIL_NOT_A_DECLARED_RAIL"
ENABLE_UNOBSERVED = "ENABLE_EDGE_UNOBSERVED"
NO_RAILS = "NO_RAILS_DECLARED"

GREEN = "GREEN"
RED = "RED"


class ObservabilityError(ValueError):
    """Bad input, raised at load time rather than producing a plausible
    coverage number from a malformed plan."""


# --------------------------------------------------------------------------- #
# Observation points
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Observation:
    """One thing a bench can look at.

    ``net`` is the net the probe touches; ``kind`` is what seeing it
    establishes. The two are separate because the same net can be observed
    for different reasons and only one of them proves the rail.
    """

    net: str
    kind: str
    source: str = ""            # where it came from (a testpoint plan id, say)

    def to_dict(self) -> dict:
        payload = {"net": self.net, "kind": self.kind}
        if self.source:
            payload["source"] = self.source
        return payload


def load_observation(entry: Any) -> Observation:
    if not isinstance(entry, Mapping):
        raise ObservabilityError("each observation must be an object")
    net = entry.get("net")
    if not isinstance(net, str) or not net.strip():
        raise ObservabilityError("an observation needs a non-empty 'net'")
    kind = entry.get("kind")
    if kind not in OBS_KINDS:
        raise ObservabilityError(
            f"observation on {net!r} declares kind {kind!r}; it must be one "
            f"of {list(OBS_KINDS)}. There is no default: a power-good pin "
            "counted as a rail measurement is the error this distinction "
            "exists to prevent")
    return Observation(net=net.strip(), kind=kind,
                       source=str(entry.get("source", "") or ""))


def observations_from_testpoint_plan(plan: Mapping[str, Any]) -> List[Observation]:
    """Read ``testpoint_plan.py``'s artifact into observations.

    The mapping from a test point's own category to what it ESTABLISHES is
    made here rather than in the plan, because the plan is about DFT scope
    and this is about diagnosability - the same test point means different
    things to the two questions.
    """
    category_kind = {
        "power_rail": OBS_RAIL,
        "pmic_enable_pg": OBS_PG,
        "boot_strap": OBS_ENABLE,
        "debug": OBS_ENABLE,
    }
    out: List[Observation] = []
    for point in plan.get("testpoints", []) or []:
        if not isinstance(point, Mapping):
            continue
        net = point.get("net") or point.get("name")
        if not isinstance(net, str) or not net.strip():
            continue
        category = str(point.get("category", "")).strip()
        kind = category_kind.get(category)
        if kind is None:
            # An uncategorised test point is NOT assumed to prove a rail.
            # Guessing here would inflate coverage with points whose meaning
            # nobody recorded.
            continue
        out.append(Observation(net=net.strip(), kind=kind,
                               source=str(point.get("id", "") or "testpoint_plan")))
    return out


# --------------------------------------------------------------------------- #
# Rail observability
# --------------------------------------------------------------------------- #

@dataclass
class RailObservability:
    rail: str
    tier: int
    kinds: Tuple[str, ...]
    status: str
    reason: Optional[str]
    detail: str

    @property
    def proven(self) -> bool:
        return any(kind in PROVES_RAIL for kind in self.kinds)

    def to_dict(self) -> dict:
        payload = {"rail": self.rail, "tier": self.tier,
                   "observations": list(self.kinds), "status": self.status,
                   "proves_rail": self.proven, "detail": self.detail}
        if self.reason:
            payload["issue"] = self.reason
        return payload


def rail_observability(rails: Sequence[Rail],
                       observations: Sequence[Observation],
                       tiers: Sequence[Sequence[str]]) -> List[RailObservability]:
    from .findings import FAIL, NO_COVERAGE, PASS

    tier_of = {name: index for index, group in enumerate(tiers)
               for name in group}
    by_net: Dict[str, List[str]] = {}
    for observation in observations:
        by_net.setdefault(observation.net, []).append(observation.kind)

    out: List[RailObservability] = []
    for rail in rails:
        kinds = tuple(sorted(set(by_net.get(rail.name, []))))
        tier = tier_of.get(rail.name, -1)
        if not kinds:
            out.append(RailObservability(
                rail.name, tier, kinds, FAIL, UNOBSERVED_RAIL,
                "nothing on this rail can be looked at; a failure here is "
                "invisible and indistinguishable from every other invisible "
                "one"))
        elif not any(kind in PROVES_RAIL for kind in kinds):
            out.append(RailObservability(
                rail.name, tier, kinds, NO_COVERAGE, PG_ONLY,
                "the only observation is the regulator's own opinion. PG can "
                "be asserted into a rail that is shorted downstream and "
                "current-limiting: PG high, rail low, bench sees nothing "
                "wrong"))
        else:
            out.append(RailObservability(
                rail.name, tier, kinds, PASS, None,
                "the rail voltage itself can be measured"))
    return out


# --------------------------------------------------------------------------- #
# Distinguishable classes - the metric that matters
# --------------------------------------------------------------------------- #

@dataclass
class FailureClass:
    """Rails whose failures a bench cannot tell apart."""

    tier: int
    rails: Tuple[str, ...]
    detail: str

    @property
    def ambiguous(self) -> bool:
        return len(self.rails) > 1

    def to_dict(self) -> dict:
        return {"tier": self.tier, "rails": list(self.rails),
                "ambiguous": self.ambiguous, "detail": self.detail}


def failure_classes(rails: Sequence[Rail],
                    states: Sequence[RailObservability],
                    tiers: Sequence[Sequence[str]]) -> List[FailureClass]:
    """Group the UNPROVEN rails by the tier they come up in.

    Why tier and not the whole board: rails in different tiers fail at
    different points in the sequence, so an observation anywhere downstream
    already separates them in time - the later tier never starts. Rails
    INSIDE one tier come up together and the graph does not order them, so
    without an observation on each, their failures are the same event seen
    from outside.

    A proven rail forms its own class: it is directly distinguishable.
    """
    proven = {s.rail for s in states if s.proven}
    classes: List[FailureClass] = []
    for index, group in enumerate(tiers):
        blind = tuple(name for name in group if name not in proven)
        for name in group:
            if name in proven:
                classes.append(FailureClass(
                    index, (name,),
                    "directly observable, so its failure stands alone"))
        if not blind:
            continue
        if len(blind) == 1:
            classes.append(FailureClass(
                index, blind,
                "not directly observable, but it is the only unobserved rail "
                "in its tier - the failure is separable by elimination"))
        else:
            classes.append(FailureClass(
                index, blind,
                f"{len(blind)} rails come up together in tier {index} and "
                "none of them can be measured; their failures are one "
                "symptom, and no amount of probing separates them"))
    return classes


# --------------------------------------------------------------------------- #
# Reset
# --------------------------------------------------------------------------- #

@dataclass
class ResetCheck:
    net: str
    gated_by: Tuple[str, ...]
    status: str
    reason: Optional[str]
    detail: str

    def to_dict(self) -> dict:
        payload = {"net": self.net, "gated_by": list(self.gated_by),
                   "status": self.status, "detail": self.detail}
        if self.reason:
            payload["issue"] = self.reason
        return payload


def check_resets(resets: Sequence[Mapping[str, Any]],
                 rails: Sequence[Rail],
                 observations: Sequence[Observation]) -> List[ResetCheck]:
    """A reset released too early is a powered, dead board.

    The symptom looks nothing like a missing rail, which is why this is
    reported apart from the rail table rather than folded into it. Seeing
    the fault needs BOTH edges: the reset deassertion and the rail it was
    supposed to wait for.
    """
    from .findings import FAIL, NO_COVERAGE, PASS

    rail_names = {rail.name for rail in rails}
    observed_reset = {o.net for o in observations if o.kind == OBS_RESET}
    proven_rails = {o.net for o in observations if o.kind in PROVES_RAIL}

    out: List[ResetCheck] = []
    for entry in resets:
        if not isinstance(entry, Mapping):
            raise ObservabilityError("each reset must be an object")
        net = entry.get("net")
        if not isinstance(net, str) or not net.strip():
            raise ObservabilityError("a reset needs a non-empty 'net'")
        net = net.strip()
        gated = tuple(str(r) for r in (entry.get("waits_for") or []))

        unknown = [r for r in gated if r not in rail_names]
        if unknown:
            out.append(ResetCheck(
                net, gated, FAIL, RESET_RAIL_UNKNOWN,
                f"this reset declares it waits for {unknown}, which are not "
                "declared rails. The sequence it claims to respect cannot be "
                "checked against anything"))
            continue

        if net not in observed_reset:
            out.append(ResetCheck(
                net, gated, FAIL, RESET_UNOBSERVABLE,
                "the reset edge cannot be seen. A reset that deasserts before "
                "its rails are up gives a powered, dead board, and without "
                "this edge the cause is indistinguishable from a dead SoC"))
            continue

        blind_rails = [r for r in gated if r not in proven_rails]
        if blind_rails:
            out.append(ResetCheck(
                net, gated, NO_COVERAGE, UNOBSERVED_RAIL,
                f"the reset edge is observable but {blind_rails} are not, so "
                "the ORDER between them cannot be established - only half of "
                "the comparison exists"))
            continue

        out.append(ResetCheck(
            net, gated, PASS, None,
            "both the reset edge and every rail it waits for can be measured, "
            "so their order is checkable on a scope"))
    return out


# --------------------------------------------------------------------------- #
# Enable edges
# --------------------------------------------------------------------------- #

def unobserved_enable_edges(rails: Sequence[Rail],
                            observations: Sequence[Observation]) -> List[dict]:
    """An enable that is neither observable nor driven by an observable rail.

    Reported as information rather than as a failure: the enable is usually
    the OUTPUT of an observable rail, in which case measuring the rail
    already tells you whether the gate opened. It only becomes a gap when
    the source itself is invisible.
    """
    visible = {o.net for o in observations
               if o.kind in (OBS_RAIL, OBS_ENABLE, OBS_PG)}
    out: List[dict] = []
    for rail in rails:
        if rail.enable_source == ALWAYS_ON:
            continue
        if rail.enable_source in visible:
            continue
        out.append({
            "rail": rail.name, "enable_source": rail.enable_source,
            "issue": ENABLE_UNOBSERVED,
            "detail": f"{rail.name} is gated by {rail.enable_source!r}, and "
                      "neither that signal nor the rail driving it can be "
                      "looked at - a gate that never opened looks exactly "
                      "like a regulator that failed"})
    return out


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

def diagnosability(states: Sequence[RailObservability],
                   classes: Sequence[FailureClass]) -> Dict[str, int]:
    """Rails, and the number of DISTINCT symptoms they can produce.

    Reporting both is the point: "5 rails, 5 classes" and "5 rails, 2
    classes" are very different boards and a single coverage percentage
    makes them look similar.
    """
    return {
        "rails": len(states),
        "distinguishable_classes": len(classes),
        "ambiguous_classes": sum(1 for c in classes if c.ambiguous),
        "rails_proven": sum(1 for s in states if s.proven),
        "rails_pg_only": sum(1 for s in states if s.reason == PG_ONLY),
        "rails_unobserved": sum(1 for s in states if s.reason == UNOBSERVED_RAIL),
    }


def rollup(states: Sequence[RailObservability],
           classes: Sequence[FailureClass],
           resets: Sequence[ResetCheck]) -> Tuple[str, List[str]]:
    """RED when a failure would be invisible or ambiguous.

    This IS allowed to be RED, unlike the proposal layer: an unobservable
    rail is a defect in the design's testability, present in the artifacts
    being read, not a shortfall somebody else measured.
    """
    from .findings import FAIL, NO_COVERAGE

    reasons: List[str] = []
    if any(s.status == FAIL for s in states):
        reasons.append(UNOBSERVED_RAIL)
    if any(c.ambiguous for c in classes):
        reasons.append(INDISTINGUISHABLE)
    if any(r.status == FAIL for r in resets):
        reasons.append(RESET_UNOBSERVABLE)
    if reasons:
        return RED, sorted(set(reasons))

    soft = []
    if any(s.status == NO_COVERAGE for s in states):
        soft.append(PG_ONLY)
    if any(r.status == NO_COVERAGE for r in resets):
        soft.append(UNOBSERVED_RAIL)
    if soft:
        return NO_COVERAGE, sorted(set(soft))
    if not states:
        return NO_COVERAGE, [NO_RAILS]
    return GREEN, []
