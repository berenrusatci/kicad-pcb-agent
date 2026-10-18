"""Proposals that CHANGE the topology, not the numbers inside a fixed one.

THE GAP THIS FILLS
--------------------------------------------------------------------------
Every solver in this bundle optimises WITHIN a topology somebody else chose.
``zdiff_solver`` sweeps W and S. ``field_solver`` solves the cross-section it
is handed. ``pmic_allocation`` assigns rails to channels that exist.
``route/*`` finds a path through a fixed set of obstacles. Not one of them
can say "add a part".

The observation this comes from is concrete: in the antenna case that
prompted the item, the model did not tune parameters - it ADDED a feed line
and the return loss moved. A whole class of fixes lives outside the
parameter space our solvers search, and nothing in the repo proposes them.

WHAT A PROPOSAL IS, AND IS NOT
--------------------------------------------------------------------------
A proposal is a HYPOTHESIS. It is not a fix, not a value, and not a claim
that the design will improve. Four constraints keep it from becoming a
suggestion mill:

  1. It is TRIGGERED by a shortfall another tool already measured. This
     module reads reports; it never measures the board. A second measurement
     here would give the repo two answers to one question, which is what
     #51 and #54 were both about.
  2. It states a DIRECTION, never a magnitude. "Adding a second decoupling
     stage lowers PDN impedance between the two self-resonances" follows
     from the topology. "By 6 dB" does not - that needs a solver, and a
     magnitude printed here would be read as a result. A catalogue entry
     carrying a magnitude does not load.
  3. It NAMES THE TOOL that would verify it, and the OBSERVATION that would
     refute it. A proposal nothing can check is advice; a proposal nothing
     can refute is advertising. Both are refused at load time, for the same
     reason ``assertions.py`` refuses an unfalsifiable assertion.
  4. It STATES ITS COST. Every one of these adds parts, area, or a new
     resonance. A proposal listing only benefits is a sales pitch.

AND IT HAS TO BE ABLE TO NOT APPLY
--------------------------------------------------------------------------
A transformation that fits every shortfall fits none. Each entry therefore
carries at least one ``excluded_when`` condition that can rule it out, and
an entry with an empty exclusion list is ``UNCONDITIONAL_TRANSFORM`` and does
not load. This is the direct analogue of ``VACUOUS_ASSERTION``: the mutation
that must be able to break it.

The exclusions are the most useful part of the catalogue, not the least.
``capacitor_too_far`` and ``no_capacitor`` are both decoupling shortfalls and
only one of them is a topology problem - a capacitor that exists and sits too
far away is fixed by MOVING it, and proposing another part there adds a BOM
line without addressing the distance.

SOME SHORTFALLS ARE ALREADY OWNED
--------------------------------------------------------------------------
A 3W spacing violation is a parameter change and ``crosstalk_thinning``
already solves it. The catalogue says so explicitly rather than staying
silent, because silence here reads as "no tool covers this" and would send
somebody building a second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

DIRECTION_DECREASE = "decrease"
DIRECTION_INCREASE = "increase"
DIRECTIONS = (DIRECTION_DECREASE, DIRECTION_INCREASE)

# What kind of answer an entry is.
KIND_TOPOLOGY = "topology_change"      # adds or removes an element
KIND_OWNED = "already_owned"           # a parameter change an existing tool solves
KINDS = (KIND_TOPOLOGY, KIND_OWNED)

# Reason codes.
UNVERIFIABLE_PROPOSAL = "UNVERIFIABLE_PROPOSAL"
UNFALSIFIABLE_PROPOSAL = "UNFALSIFIABLE_PROPOSAL"
COST_UNDECLARED = "COST_UNDECLARED"
UNCONDITIONAL_TRANSFORM = "UNCONDITIONAL_TRANSFORM"
MAGNITUDE_CLAIMED = "MAGNITUDE_CLAIMED"
NO_TRANSFORM_KNOWN = "NO_TRANSFORM_KNOWN"
EXCLUDED = "EXCLUDED"
ALREADY_OWNED = "ALREADY_OWNED"

GREEN = "GREEN"
RED = "RED"

# Words that turn a direction into a quantity. A catalogue entry containing
# one of these in its predicted effect is claiming a magnitude, and the whole
# discipline of this module is that magnitudes come from solvers.
_MAGNITUDE_TOKENS = ("dB", "%", "ohm", "Ω", "mV", "mm ", "ps", "MHz", "GHz")


class TransformSyntaxError(ValueError):
    """A catalogue entry could not be loaded."""

    def __init__(self, transform_id: str, code: str, detail: str):
        self.transform_id = transform_id
        self.code = code
        self.detail = detail
        super().__init__(f"{transform_id}: {code} - {detail}")

    def to_dict(self) -> dict:
        return {"transform": self.transform_id, "issue": self.code,
                "detail": self.detail}


# --------------------------------------------------------------------------- #
# Trigger and exclusion
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Trigger:
    """The shortfall that makes a transformation worth considering.

    Keyed on things other tools ACTUALLY emit: a finding's ``check`` name, a
    violation ``problem`` string, or a report ``reason_code``. Matching is
    exact, for the reason ``matrix.py`` matches exactly - a fuzzy trigger
    re-points itself at a neighbouring finding the moment somebody renames
    one, and a proposal aimed at the wrong shortfall is worse than none.
    """

    tool: str
    check: Optional[str] = None
    problem: Optional[str] = None
    reason_code: Optional[str] = None

    def matches(self, observation: "Observation") -> bool:
        if observation.tool != self.tool:
            return False
        if self.check is not None and observation.check != self.check:
            return False
        if self.problem is not None and self.problem not in observation.problems:
            return False
        if self.reason_code is not None and \
                self.reason_code not in observation.reason_codes:
            return False
        return self.check is not None or self.problem is not None \
            or self.reason_code is not None

    def to_dict(self) -> dict:
        payload = {"tool": self.tool}
        for key, value in (("check", self.check), ("problem", self.problem),
                           ("reason_code", self.reason_code)):
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True)
class Exclusion:
    """A condition under which the transformation is the wrong answer.

    ``fact`` is a key the caller supplies in ``--context``; ``equals`` is the
    value that rules the transformation out. Facts are supplied, never
    inferred: whether a plane is split under a via row is something
    ``reference_plane.py`` measures, and guessing it here would be a second
    measurement.
    """

    fact: str
    equals: Any
    because: str

    def excludes(self, context: Mapping[str, Any]) -> bool:
        return self.fact in context and context[self.fact] == self.equals

    def to_dict(self) -> dict:
        return {"fact": self.fact, "equals": self.equals,
                "because": self.because}


# --------------------------------------------------------------------------- #
# Transformation
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Transform:
    id: str
    kind: str
    title: str
    triggers: Tuple[Trigger, ...]
    adds: str                       # what element enters the design
    metric: str                     # what it is expected to move
    direction: str                  # which way - never how far
    mechanism: str                  # why, from the topology
    verified_by: str                # the tool that would measure it
    refuted_by: str                 # what would have to be seen for it to be wrong
    cost: Tuple[str, ...]
    excluded_when: Tuple[Exclusion, ...]
    owned_by: Optional[str] = None  # for KIND_OWNED: the tool that already solves it
    notes: str = ""

    def to_dict(self) -> dict:
        payload = {
            "id": self.id, "kind": self.kind, "title": self.title,
            "triggers": [t.to_dict() for t in self.triggers],
            "adds": self.adds, "metric": self.metric,
            "direction": self.direction, "mechanism": self.mechanism,
            "verified_by": self.verified_by, "refuted_by": self.refuted_by,
            "cost": list(self.cost),
            "excluded_when": [e.to_dict() for e in self.excluded_when],
            "magnitude": None,
        }
        if self.owned_by:
            payload["owned_by"] = self.owned_by
        if self.notes:
            payload["notes"] = self.notes
        return payload


def _require_text(entry: Mapping, key: str, transform_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TransformSyntaxError(transform_id, "MISSING_FIELD",
                                   f"{key!r} must be a non-empty string")
    return value.strip()


def load_transform(entry: Any) -> Transform:
    """Build one catalogue entry, refusing anything that could not be checked."""
    if not isinstance(entry, Mapping):
        raise TransformSyntaxError("<unknown>", "NOT_AN_OBJECT",
                                   "each transform must be an object")
    transform_id = entry.get("id") if isinstance(entry.get("id"), str) else "<unnamed>"
    for key in ("id", "title", "adds", "metric", "mechanism"):
        _require_text(entry, key, transform_id)

    kind = entry.get("kind", KIND_TOPOLOGY)
    if kind not in KINDS:
        raise TransformSyntaxError(transform_id, "BAD_KIND",
                                   f"kind must be one of {list(KINDS)}")

    direction = entry.get("direction")
    if direction not in DIRECTIONS:
        raise TransformSyntaxError(
            transform_id, "BAD_DIRECTION",
            f"direction must be one of {list(DIRECTIONS)}. A proposal states "
            "which way a metric moves; how far is the solver's answer")

    raw_triggers = entry.get("triggers")
    if not isinstance(raw_triggers, Sequence) or isinstance(raw_triggers, str) \
            or not raw_triggers:
        raise TransformSyntaxError(transform_id, "MISSING_TRIGGER",
                                   "'triggers' must be a non-empty array; a "
                                   "transformation nothing triggers is never "
                                   "proposed and never reviewed")
    triggers = tuple(_load_trigger(t, transform_id) for t in raw_triggers)

    if kind == KIND_OWNED:
        owned_by = _require_text(entry, "owned_by", transform_id)
        return Transform(
            id=transform_id, kind=kind, title=entry["title"].strip(),
            triggers=triggers, adds=entry["adds"].strip(),
            metric=entry["metric"].strip(), direction=direction,
            mechanism=entry["mechanism"].strip(),
            verified_by=owned_by, refuted_by="-", cost=(),
            excluded_when=(), owned_by=owned_by,
            notes=str(entry.get("notes", "") or ""))

    verified_by = entry.get("verified_by")
    if not isinstance(verified_by, str) or not verified_by.strip():
        raise TransformSyntaxError(
            transform_id, UNVERIFIABLE_PROPOSAL,
            "no tool is named that would measure the predicted effect. A "
            "proposal nobody can check is advice, and this bundle does not "
            "emit advice")

    refuted_by = entry.get("refuted_by")
    if not isinstance(refuted_by, str) or not refuted_by.strip():
        raise TransformSyntaxError(
            transform_id, UNFALSIFIABLE_PROPOSAL,
            "no observation is named that would show the proposal was wrong. "
            "A hypothesis that survives every outcome is advertising")

    cost = entry.get("cost")
    if isinstance(cost, str):
        cost = [cost]
    if not isinstance(cost, Sequence) or not cost:
        raise TransformSyntaxError(
            transform_id, COST_UNDECLARED,
            "every one of these adds parts, area or a new resonance; an entry "
            "listing only benefits is a sales pitch")

    raw_exclusions = entry.get("excluded_when")
    if not isinstance(raw_exclusions, Sequence) or isinstance(raw_exclusions, str) \
            or not raw_exclusions:
        raise TransformSyntaxError(
            transform_id, UNCONDITIONAL_TRANSFORM,
            "no condition can rule this transformation out. One that fits "
            "every shortfall fits none - the same emptiness a falsifier that "
            "cannot break an assertion has")
    exclusions = tuple(_load_exclusion(e, transform_id) for e in raw_exclusions)

    for field_name in ("metric", "mechanism", "adds"):
        text = entry[field_name]
        for token in _MAGNITUDE_TOKENS:
            if token in text:
                raise TransformSyntaxError(
                    transform_id, MAGNITUDE_CLAIMED,
                    f"{field_name!r} contains {token!r}. A number here would "
                    "be read as a prediction, and this module runs no solver. "
                    f"Name the direction and let {verified_by} produce the "
                    "quantity")

    return Transform(
        id=transform_id, kind=kind, title=entry["title"].strip(),
        triggers=triggers, adds=entry["adds"].strip(),
        metric=entry["metric"].strip(), direction=direction,
        mechanism=entry["mechanism"].strip(), verified_by=verified_by.strip(),
        refuted_by=refuted_by.strip(), cost=tuple(str(c) for c in cost),
        excluded_when=exclusions, notes=str(entry.get("notes", "") or ""))


def _load_trigger(raw: Any, transform_id: str) -> Trigger:
    if not isinstance(raw, Mapping):
        raise TransformSyntaxError(transform_id, "BAD_TRIGGER",
                                   "each trigger must be an object")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise TransformSyntaxError(transform_id, "BAD_TRIGGER",
                                   "a trigger needs a 'tool'")
    check = raw.get("check")
    problem = raw.get("problem")
    reason_code = raw.get("reason_code")
    if check is None and problem is None and reason_code is None:
        raise TransformSyntaxError(
            transform_id, "BAD_TRIGGER",
            "a trigger naming only a tool fires on every report that tool "
            "ever produced, including its passing ones")
    return Trigger(tool=tool.strip(), check=check, problem=problem,
                   reason_code=reason_code)


def _load_exclusion(raw: Any, transform_id: str) -> Exclusion:
    if not isinstance(raw, Mapping):
        raise TransformSyntaxError(transform_id, "BAD_EXCLUSION",
                                   "each exclusion must be an object")
    fact = raw.get("fact")
    if not isinstance(fact, str) or not fact.strip():
        raise TransformSyntaxError(transform_id, "BAD_EXCLUSION",
                                   "an exclusion needs a 'fact'")
    if "equals" not in raw:
        raise TransformSyntaxError(transform_id, "BAD_EXCLUSION",
                                   "an exclusion needs an 'equals' value")
    because = raw.get("because")
    if not isinstance(because, str) or not because.strip():
        raise TransformSyntaxError(
            transform_id, "BAD_EXCLUSION",
            "an exclusion without a reason cannot be reviewed, and an "
            "unreviewable exclusion is how a transformation quietly stops "
            "being offered")
    return Exclusion(fact=fact.strip(), equals=raw["equals"],
                     because=because.strip())


# --------------------------------------------------------------------------- #
# Observations - what another tool's report said
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Observation:
    tool: str
    check: Optional[str]
    status: str
    problems: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict:
        payload = {"tool": self.tool, "status": self.status}
        if self.check:
            payload["check"] = self.check
        if self.problems:
            payload["problems"] = list(self.problems)
        if self.reason_codes:
            payload["reason_codes"] = list(self.reason_codes)
        return payload


def observations_from_report(report: Mapping[str, Any]) -> List[Observation]:
    """Read one tool report into the shortfalls it recorded.

    Only FAIL and NO_COVERAGE findings become observations. A PASS is not a
    shortfall, and proposing a topology change against a passing check is how
    a tool starts recommending work for its own sake.
    """
    from .findings import FAIL, NO_COVERAGE

    tool = str(report.get("tool", "")).strip()
    reason_codes = tuple(str(c) for c in report.get("reason_codes", []) or [])
    out: List[Observation] = []
    for finding in report.get("findings", []) or []:
        if not isinstance(finding, Mapping):
            continue
        status = finding.get("status")
        if status not in (FAIL, NO_COVERAGE):
            continue
        problems = []
        for violation in finding.get("violations", []) or []:
            if isinstance(violation, Mapping):
                for key in ("problem", "issue", "code"):
                    value = violation.get(key)
                    if isinstance(value, str):
                        problems.append(value)
        out.append(Observation(
            tool=tool, check=finding.get("check"), status=str(status),
            problems=tuple(dict.fromkeys(problems)),
            reason_codes=reason_codes,
            detail=str(finding.get("detail", ""))))
    return out


# --------------------------------------------------------------------------- #
# Proposal
# --------------------------------------------------------------------------- #

@dataclass
class Proposal:
    transform: Transform
    observation: Observation
    offered: bool
    reason: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> dict:
        payload = {
            "transform": self.transform.id,
            "title": self.transform.title,
            "kind": self.transform.kind,
            "offered": self.offered,
            "triggered_by": self.observation.to_dict(),
            "detail": self.detail,
        }
        if self.reason:
            payload["issue"] = self.reason
        if self.offered:
            payload.update({
                "adds": self.transform.adds,
                "predicted": {"metric": self.transform.metric,
                              "direction": self.transform.direction,
                              "magnitude": None},
                "mechanism": self.transform.mechanism,
                "verify_with": self.transform.verified_by,
                "refuted_if": self.transform.refuted_by,
                "cost": list(self.transform.cost),
            })
        return payload


def propose(observations: Sequence[Observation],
            catalogue: Sequence[Transform],
            context: Mapping[str, Any] | None = None) -> List[Proposal]:
    """Match shortfalls against the catalogue.

    Every match produces a Proposal, INCLUDING the ones ruled out: an
    excluded transformation is the most informative output this module has,
    because it says a plausible-looking fix is the wrong one and why.
    """
    context = dict(context or {})
    proposals: List[Proposal] = []
    for observation in observations:
        for transform in catalogue:
            if not any(t.matches(observation) for t in transform.triggers):
                continue
            if transform.kind == KIND_OWNED:
                proposals.append(Proposal(
                    transform, observation, False, ALREADY_OWNED,
                    f"this is a parameter change, not a topology change, and "
                    f"{transform.owned_by} already solves it. Reported rather "
                    "than skipped: silence here reads as 'no tool covers "
                    "this' and sends somebody building a second one"))
                continue
            blocked = [e for e in transform.excluded_when if e.excludes(context)]
            if blocked:
                proposals.append(Proposal(
                    transform, observation, False, EXCLUDED,
                    "; ".join(e.because for e in blocked)))
                continue
            proposals.append(Proposal(
                transform, observation, True, None,
                f"{transform.mechanism}. Direction only - "
                f"{transform.verified_by} produces the quantity"))
    return proposals


def uncovered(observations: Sequence[Observation],
              proposals: Sequence[Proposal]) -> List[Observation]:
    """Shortfalls the catalogue has nothing to say about.

    Reported explicitly. A proposal list that silently omits what it could
    not address looks like complete coverage of the report it read.
    """
    addressed = {(p.observation.tool, p.observation.check) for p in proposals}
    return [o for o in observations if (o.tool, o.check) not in addressed]


def rollup(proposals: Sequence[Proposal],
           uncovered_observations: Sequence[Observation]) -> Tuple[str, List[str]]:
    """This module never returns RED on its own.

    It measures nothing, so it cannot find a defect. A shortfall it cannot
    address is NO_COVERAGE; a run that offered at least one checkable
    proposal is GREEN. The design's own verdict comes from the tool whose
    report was read.
    """
    from .findings import NO_COVERAGE

    if uncovered_observations:
        return NO_COVERAGE, [NO_TRANSFORM_KNOWN]
    if not proposals:
        return NO_COVERAGE, ["NO_SHORTFALL_OBSERVED"]
    if not any(p.offered for p in proposals):
        return NO_COVERAGE, [EXCLUDED]
    return GREEN, []


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
#
# Every trigger below names a check or reason code some tool in this tree
# ACTUALLY emits; they were read out of the sources, not invented. The
# mechanisms are topological arguments, not measurements: each one says which
# way a quantity moves because of what the added element does, and every
# entry names the tool that turns that direction into a number.

CATALOGUE_ENTRIES: List[dict] = [
    {
        "id": "add-decoupling-stage",
        "title": "Add a second decoupling stage of a different decade",
        "triggers": [{"tool": "decoupling_coverage", "problem": "no_capacitor"}],
        "adds": "one capacitor of a different decade at the same power pin",
        "metric": "power distribution impedance between the two self-resonances",
        "direction": "decrease",
        "mechanism": "a single capacitor is inductive above its own "
                     "self-resonance, so above that point the pin sees the "
                     "package and via inductance alone; a second, smaller "
                     "part resonates higher and carries the band the first "
                     "one has given up",
        "verified_by": "decoupling_coverage",
        "refuted_by": "decoupling_coverage still reports the pin uncovered, "
                      "or the added part sits further from the pin than the "
                      "one it was meant to help",
        "cost": ["one BOM line and one placement site per pin",
                 "an anti-resonance between the two parts, which moves noise "
                 "rather than removing it"],
        "excluded_when": [
            {"fact": "shortfall", "equals": "capacitor_too_far",
             "because": "a capacitor that EXISTS and sits too far away is a "
                        "placement problem. Adding a second part does not "
                        "shorten the first one's loop, and the BOM line buys "
                        "nothing"},
            {"fact": "pin_is_analog_reference", "equals": True,
             "because": "on a reference pin the added loop can inject more "
                        "than it removes; the decision belongs to the analog "
                        "owner, not to a catalogue"},
        ],
    },
    {
        "id": "add-lc-post-filter",
        "title": "Add an LC post-filter after the regulator",
        "triggers": [{"tool": "rail_budget",
                      "reason_code": "NOISE_BUDGET_EXCEEDED"}],
        "adds": "a series inductor or ferrite plus a shunt capacitor on the "
                "rail, after the regulator output",
        "metric": "ripple at the switching fundamental and its harmonics",
        "direction": "decrease",
        "mechanism": "the regulator's own loop cannot reject its own "
                     "switching frequency, so attenuation there has to come "
                     "from outside the loop; a series element after the "
                     "output is outside it",
        "verified_by": "rail_budget",
        "refuted_by": "loop_stability shows the phase margin falling, or the "
                      "filter's resonance lands where the load steps live and "
                      "the transient gets worse",
        "cost": ["DC drop across the series element, which eats headroom the "
                 "enable-threshold check already watches",
                 "a new resonance that peaks if the load is light"],
        "excluded_when": [
            {"fact": "dominant_noise_term", "equals": "random",
             "because": "a broadband random term is not a tone. A post-filter "
                        "attenuates where it is tuned; against a flat "
                        "spectrum it buys a fraction of what its part count "
                        "suggests"},
            {"fact": "rail_feeds_regulator_input", "equals": True,
             "because": "a series element in front of another regulator's "
                        "input meets a negative incremental resistance, and "
                        "the pair can oscillate. That is a stability question "
                        "for loop_stability, not a filter question"},
        ],
    },
    {
        "id": "add-bulk-stage",
        "title": "Add a bulk capacitance stage",
        "triggers": [{"tool": "rail_budget",
                      "reason_code": "NOISE_BUDGET_EXCEEDED"}],
        "adds": "a bulk capacitor near the point of load",
        "metric": "rail sag during a load step",
        "direction": "decrease",
        "mechanism": "the regulator loop needs time to respond to a step, and "
                     "during that time the rail is held up by charge that is "
                     "already local; bulk is the store that supplies it",
        "verified_by": "rail_budget",
        "refuted_by": "the sag is unchanged, which means the limit was the "
                      "path inductance to the load rather than the stored "
                      "charge",
        "cost": ["board area, which competes with the high-frequency parts "
                 "that must sit closer",
                 "inrush at power-up, which the sequencing check then has to "
                 "accommodate"],
        "excluded_when": [
            {"fact": "dominant_noise_term", "equals": "deterministic",
             "because": "a periodic switching tone is not a load step. Bulk "
                        "does little at the switching frequency, and the "
                        "post-filter is the entry that addresses it"},
            {"fact": "inrush_limited", "equals": True,
             "because": "if the supply is already inrush-limited, more bulk "
                        "extends the ramp and can trip the sequencing budget "
                        "rail_budget checks"},
        ],
    },
    {
        "id": "add-stitching-via-row",
        "title": "Add a stitching via row along the discontinuity",
        "triggers": [{"tool": "emc_rules", "check": "emc_via_stitching_pitch"},
                     {"tool": "stitch_density", "check": "edge_stitch_pitch"}],
        "adds": "a row of ground vias tying the reference planes together "
                "along the edge or the plane boundary",
        "metric": "the cavity resonance the plane pair can support",
        "direction": "increase",
        "mechanism": "two planes with an open perimeter form a resonant "
                     "cavity whose lowest mode is set by its largest "
                     "unstitched dimension; shortening that dimension pushes "
                     "the mode up, away from the harmonics that excite it",
        "verified_by": "emc_rules",
        "refuted_by": "reference_plane still reports a discontinuity under "
                      "the signals that cross there - vias between planes do "
                      "nothing for a return path that has no plane to return "
                      "on",
        "cost": ["drill count, which the fab quotes on",
                 "keepouts that fragment the routing channel the vias sit in"],
        "excluded_when": [
            {"fact": "reference_plane_split", "equals": True,
             "because": "stitching a SPLIT plane does not create a return "
                        "path; it ties two references that were never meant "
                        "to be one. reference_plane.py is the tool that says "
                        "whether the plane is continuous there"},
            {"fact": "single_reference_layer", "equals": True,
             "because": "with one reference layer there is no plane pair and "
                        "no cavity, so there is no mode for the row to move"},
        ],
    },
    {
        "id": "add-return-via-per-signal-via",
        "title": "Add a return via beside each high-speed signal via",
        "triggers": [{"tool": "stitch_density", "check": "via_return_stitch"}],
        "adds": "one ground via adjacent to every signal via that changes "
                "reference layer",
        "metric": "the area of the loop the return current must take around "
                  "the transition",
        "direction": "decrease",
        "mechanism": "a signal via that changes reference forces its return "
                     "current to find another path between the planes; "
                     "without a nearby via that path is whatever the cavity "
                     "offers, and its extent is set by geometry rather than "
                     "by design",
        "verified_by": "stitch_density",
        "refuted_by": "the transition still shows the same discontinuity, "
                      "which points at the stub rather than the return - "
                      "via_stub.py is the tool for that",
        "cost": ["a via and its keepout beside every transition, in exactly "
                 "the congested region the transition exists to escape"],
        "excluded_when": [
            {"fact": "same_reference_before_and_after", "equals": True,
             "because": "a via that keeps the same reference plane has no "
                        "return discontinuity to bridge, and the added via "
                        "spends congestion for nothing"},
        ],
    },
    {
        "id": "add-guard-trace",
        "title": "Add a stitched guard trace between aggressor and victim",
        "triggers": [{"tool": "crosstalk_thinning",
                      "reason_code": "CROSSTALK_RISK"}],
        "adds": "a grounded trace between the two, tied to the reference at "
                "intervals short compared with the wavelength of interest",
        "metric": "the fraction of the aggressor's field that terminates on "
                  "the victim",
        "direction": "decrease",
        "mechanism": "coupling is a competition for field lines; a grounded "
                     "conductor in between offers a closer termination than "
                     "the victim does",
        "verified_by": "crosstalk_thinning",
        "refuted_by": "coupling is unchanged or worse - an under-stitched "
                      "guard is a floating conductor coupled to both, which "
                      "is a coupling path rather than a barrier",
        "cost": ["a routing channel, on a board where the spacing could not "
                 "be increased in the first place",
                 "the stitching vias the guard needs to be a guard at all"],
        "excluded_when": [
            {"fact": "guard_can_be_stitched", "equals": False,
             "because": "an unstitched guard couples to both nets and "
                        "radiates; it is the textbook way to make crosstalk "
                        "worse while appearing to address it"},
            {"fact": "spacing_increase_available", "equals": True,
             "because": "if the pair can simply be moved apart, that is a "
                        "parameter change crosstalk_thinning already sizes, "
                        "and it costs no channel"},
        ],
    },
    {
        "id": "add-feedforward-zero",
        "title": "Add a feed-forward capacitor across the upper divider leg",
        "triggers": [{"tool": "loop_stability",
                      "reason_code": "PHASE_MARGIN_BELOW_BUDGET"}],
        "adds": "a capacitor in parallel with the upper feedback divider "
                "resistor",
        "metric": "phase at the gain crossover",
        "direction": "increase",
        "mechanism": "the capacitor puts a zero-pole pair in the feedback "
                     "path; placed below the crossover the zero returns phase "
                     "that the loop's poles had taken",
        "verified_by": "loop_stability",
        "refuted_by": "the margin at the crossing does not improve, or a new "
                      "crossing appears - loop_stability reports every "
                      "crossing rather than the first, which is what makes "
                      "this checkable",
        "cost": ["one part in the feedback path, where a wrong value degrades "
                 "the margin instead of improving it",
                 "faster loop response, which can expose a transient the "
                 "slower loop hid"],
        "excluded_when": [
            {"fact": "conditionally_stable", "equals": True,
             "because": "in a conditionally stable loop the phase already "
                        "passes -180 while the gain is above unity. Moving "
                        "the crossover can land it in that region, so this is "
                        "a compensation decision rather than a catalogue one"},
            {"fact": "feedback_internal", "equals": True,
             "because": "a fixed-output regulator has no external divider to "
                        "put the capacitor across"},
        ],
    },
    {
        "id": "spacing-is-a-parameter",
        "kind": "already_owned",
        "title": "3W spacing shortfall is a parameter change",
        "triggers": [{"tool": "emc_rules", "check": "emc_3w_crosstalk"}],
        "adds": "nothing - the separation is swept, not added to",
        "metric": "centre-to-centre spacing",
        "direction": "increase",
        "mechanism": "the geometry is already in the solver's search space",
        "owned_by": "crosstalk_thinning",
        "notes": "Listed so the report can say a shortfall is covered. An "
                 "absent entry reads as 'no tool addresses this' and is how a "
                 "second implementation gets started - the #54 failure.",
    },
    {
        "id": "plane-setback-is-a-parameter",
        "kind": "already_owned",
        "title": "20H edge setback shortfall is a parameter change",
        "triggers": [{"tool": "emc_rules",
                      "check": "emc_20h_edge_radiation"}],
        "adds": "nothing - the power plane is pulled back, not supplemented",
        "metric": "power plane edge setback",
        "direction": "increase",
        "mechanism": "the setback is a dimension the existing rule engine "
                     "already sizes",
        "owned_by": "emc_rules",
    },
]


def default_catalogue() -> List[Transform]:
    """The built-in catalogue, loaded through the same gate as a user file.

    Loading it here rather than trusting it is deliberate: the refusals are
    only real if the shipped entries have to pass them too.
    """
    return [load_transform(entry) for entry in CATALOGUE_ENTRIES]
