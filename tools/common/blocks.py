"""Parametric circuit-block catalogue: the side that READS an architecture
decision.

THE LOOP THIS CLOSES
--------------------------------------------------------------------------
Architecture decisions in this project are already produced as records -
which connector family, whether a serialiser exists, how the frame-sync
signal is routed, what differential impedance applies. Nothing consumed
them. A decision that nothing reads is a comment: it can drift away from
the design for months and no check will notice, because no check was ever
pointed at it.

This module is the consumer. It turns a decision into BLOCK INSTANCES - a
concrete, machine-readable statement of what must exist - and gives each
block a way to be checked against a netlist. The value is not the emission;
it is that the emission and the verification come from the SAME record, so
a design that quietly stopped matching its decision produces a finding
instead of silence.

THE CATALOGUE IS A TABLE, NOT AN IF-CHAIN
--------------------------------------------------------------------------
Same reason the profile decision tree is a table: a rule written as data can
be read, diffed and tested, and a caller cannot half-apply it. An if-chain
invites exactly the failure these blocks exist to prevent - picking the
connector and forgetting the impedance rule that came with it.

THREE THINGS THIS REFUSES TO DO
--------------------------------------------------------------------------
1. **No defaulted parameters.** A required parameter that is absent
   produces a refusal naming it. Every value this module could invent is
   one an engineer would later find in a report and believe.
2. **No part numbers.** A block emits component ROLES with constraints
   ("bulk capacitor, >= 10 uF, X7R or better"). Which MPN satisfies that is
   a sourcing decision with its own inputs - stock, lifecycle, package -
   and none of them are visible here.
3. **No confident zero.** Verification leans on reference-designator
   prefixes, which are a convention rather than a guarantee. A template
   with no verification method reports NO_COVERAGE; it never reports that a
   block is present because nothing contradicted it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# Verification outcome vocabulary used inside this module. The report layer
# maps these onto the repo-wide PASS / FAIL / NO_COVERAGE.
PRESENT = "present"
ABSENT = "absent"
UNVERIFIABLE = "unverifiable"


class BlockRefusal(ValueError):
    """A block could not be instantiated. Carries the reason structurally so
    a report can say WHICH parameter was missing rather than 'bad input'."""

    def __init__(self, block: str, missing: Sequence[str], detail: str = ""):
        self.block = block
        self.missing = list(missing)
        self.detail = detail
        super().__init__(
            f"{block}: cannot instantiate, missing required parameter(s) "
            f"{self.missing}" + (f" - {detail}" if detail else ""))

    def to_dict(self) -> dict:
        return {"block": self.block, "issue": "MISSING_REQUIRED_PARAMETER",
                "missing": self.missing, "detail": self.detail}


@dataclass(frozen=True)
class Parameter:
    name: str
    description: str
    required: bool = True
    default: Any = None

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "required": self.required, "default": self.default}


@dataclass(frozen=True)
class BlockTemplate:
    """One reusable block. ``emit`` and ``verify`` are the two halves that
    must stay pointed at the same decision."""

    key: str
    title: str
    rationale: str
    parameters: Sequence[Parameter]
    emit: Callable[[Dict[str, Any]], dict]
    verify: Optional[Callable[[Dict[str, Any], dict], dict]] = None

    def required_names(self) -> List[str]:
        return [p.name for p in self.parameters if p.required]

    def resolve(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fill declared defaults; refuse on a missing required parameter."""
        missing = [p.name for p in self.parameters
                   if p.required and params.get(p.name) is None]
        if missing:
            raise BlockRefusal(self.key, missing,
                               "no parameter here is defaulted; a value this "
                               "module invented would be read later as one an "
                               "engineer chose")
        resolved = {p.name: params.get(p.name, p.default) for p in self.parameters}
        unknown = sorted(set(params) - set(resolved))
        if unknown:
            raise BlockRefusal(self.key, [], f"unknown parameter(s): {unknown}")
        return resolved

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title,
                "rationale": self.rationale,
                "parameters": [p.to_dict() for p in self.parameters],
                "verifiable": self.verify is not None}


@dataclass
class BlockInstance:
    key: str
    params: Dict[str, Any]
    emitted: dict
    verification: dict = field(default_factory=lambda: {"outcome": UNVERIFIABLE,
                                                        "detail": "not run"})

    def to_dict(self) -> dict:
        return {"block": self.key, "params": self.params,
                "emitted": self.emitted, "verification": self.verification}


# ---------------------------------------------------------------- blocks


def _emit_decoupling_shell(p: Dict[str, Any]) -> dict:
    """A decade-spaced bypass bank.

    Decade spacing is the rule, not a preference: a capacitor is only low
    impedance near and below its own self-resonance, so covering a band
    means covering it in steps. Putting more of ONE value in parallel
    lowers the impedance at that value's band and does nothing above it,
    which is the mistake this block exists to make hard to repeat.
    """
    decades = int(p["decades"])
    if decades < 1:
        raise BlockRefusal("decoupling_shell", [],
                           "decades must be >= 1; a bank with no members is "
                           "not a shell")
    bulk_f = float(p["bulk_farads"])
    roles = []
    value = bulk_f
    for step in range(decades):
        roles.append({
            "role": "bypass_capacitor",
            "rail": p["rail"],
            "nominal_farads": value,
            "band": "bulk" if step == 0 else ("mid" if step < decades - 1 else "hf"),
            "constraints": [
                "ceramic, X7R or better over the operating temperature range",
                "DC bias derating applies: a capacitor's marked value is not "
                "its value at the rail voltage",
            ],
        })
        value /= 10.0
    return {
        "components": roles,
        "nets": [p["rail"]],
        "rules": [
            f"Every member of the {p['rail']} bank shares one low-inductance "
            "return; a bank on a split return is a bank in name only.",
            "Place ascending by value away from the load: the smallest member "
            "is the one whose loop inductance actually matters.",
        ],
    }


def _verify_decoupling_shell(p: Dict[str, Any], nets: dict) -> dict:
    from .netlist import refs_with_prefix
    rail = p["rail"]
    if rail not in nets:
        return {"outcome": ABSENT, "detail": f"net {rail!r} is not in the netlist"}
    caps = refs_with_prefix(nets, rail, p["capacitor_prefix"])
    wanted = int(p["decades"])
    if len(caps) >= wanted:
        return {"outcome": PRESENT, "found": caps, "expected_at_least": wanted,
                "detail": f"{len(caps)} capacitor(s) on {rail}; the COUNT is "
                          "checked, the VALUES are not - a netlist carries no "
                          "component values"}
    return {"outcome": ABSENT, "found": caps, "expected_at_least": wanted,
            "detail": f"{len(caps)} capacitor(s) on {rail}, fewer than the "
                      f"{wanted} decades the decision declares"}


def _emit_differential_pair_rule(p: Dict[str, Any]) -> dict:
    tol = float(p["tolerance_pct"])
    z = float(p["impedance_ohm"])
    return {
        "components": [],
        "nets": [p["positive_net"], p["negative_net"]],
        "rules": [
            {"kind": "netclass", "name": p["netclass"],
             "diff_pair_impedance_ohm": z,
             "tolerance_pct": tol,
             "window_ohm": [z * (1 - tol / 100.0), z * (1 + tol / 100.0)]},
            {"kind": "note",
             "text": "Impedance is a property of the CROSS-SECTION, so this "
                     "rule is unenforceable until the stackup is fixed. It is "
                     "recorded here so the layout phase inherits it rather "
                     "than rediscovering it."},
        ],
    }


def _verify_differential_pair_rule(p: Dict[str, Any], nets: dict) -> dict:
    """A rule needs something to bind to, and a name is what it binds to.

    The interesting failure is not "the net is missing" but "the conductors
    are there and nameless". A pair joined by wire alone gets a generated
    name derived from whatever component touches it, and an impedance rule
    cannot address it - so the most important electrical constraint on the
    board ends up attached to nothing, with every gate still green.
    """
    from .netlist import generated_nets, is_autonamed

    pair = (p["positive_net"], p["negative_net"])
    missing = [n for n in pair if n not in nets]
    unbindable = [n for n in pair if n in nets and is_autonamed(n)]

    if unbindable:
        return {"outcome": ABSENT, "unbindable_nets": unbindable,
                "detail": "these nets exist but carry generated names; a "
                          "netclass or .kicad_dru rule can only address them "
                          "by that generated string, which is derived from a "
                          "reference designator and changes when the component "
                          "is renumbered - the rule then comes unbound with no "
                          "error"}
    if missing:
        generated = generated_nets(nets)
        detail = "the rule names nets the netlist does not have"
        if generated:
            detail += (f"; the netlist carries {len(generated)} conductor(s) with "
                       "GENERATED names, so the pair may well exist while "
                       "being unaddressable by any rule")
        return {"outcome": ABSENT, "missing_nets": missing,
                "generated_name_nets": generated, "detail": detail}
    return {"outcome": PRESENT,
            "detail": "both nets exist and are explicitly named; this confirms "
                      "the rule can BIND, not that any impedance was achieved "
                      "- no board exists yet"}


FSYNC_ROUTES = {
    "gpio": "a host GPIO drives the sensor directly; only valid at short range",
    "back_channel": "carried inside the serialiser's back channel; there is NO "
                    "separate frame-sync net to look for, and searching for "
                    "one is how this gets misread as missing",
    "connector_pin": "a dedicated contact in the cable; the only option when "
                     "there is neither a serialiser nor a short GPIO run",
    "none": "no frame sync; multi-camera capture is not synchronised in hardware",
}


def _emit_link_architecture(p: Dict[str, Any]) -> dict:
    route = p["fsync_route"]
    if route not in FSYNC_ROUTES:
        raise BlockRefusal("link_architecture", [],
                           f"fsync_route must be one of {sorted(FSYNC_ROUTES)}, "
                           f"got {route!r}")
    serdes = bool(p["serdes_required"])
    assertions = [
        {"assert": "serialiser_present" if serdes else "serialiser_absent",
         "detail": "a serialiser that is present when the decision says "
                   "absent is dead weight and heat; absent when the decision "
                   "says present means the link does not reach"},
        {"assert": f"fsync_route_is_{route}", "detail": FSYNC_ROUTES[route]},
    ]
    if serdes and route == "connector_pin":
        assertions.append({
            "assert": "redundant_fsync_contact",
            "detail": "a serialiser carries frame sync in its back channel; a "
                      "separate contact for it spends a connector position on "
                      "a signal the link already carries"})
    return {
        "components": ([{"role": "serialiser", "constraints": [
            "must accept the sensor's lane count and line rate"]}] if serdes else []),
        "nets": [],
        "rules": [a["detail"] for a in assertions],
        "assertions": assertions,
    }


def _verify_link_architecture(p: Dict[str, Any], nets: dict) -> dict:
    """The check that gives the decision teeth.

    A missing frame-sync net is only a defect for SOME routes. Under
    ``back_channel`` there is deliberately no such net, so a checker that
    looks for one reports a defect that does not exist - and a checker that
    therefore stops looking misses the case where the net really is absent.
    The route decides which question gets asked.
    """
    route = p["fsync_route"]
    net_name = p.get("fsync_net")
    if route in ("back_channel", "none"):
        if net_name and net_name in nets:
            return {"outcome": ABSENT, "detail":
                    f"route is {route!r}, yet {net_name!r} exists in the "
                    "netlist - the design carries a frame-sync net the "
                    "decision says it does not have"}
        return {"outcome": PRESENT,
                "detail": f"route is {route!r}; no separate frame-sync net is "
                          "expected and none was found"}
    if not net_name:
        return {"outcome": UNVERIFIABLE,
                "detail": f"route {route!r} needs a net to look for; pass "
                          "fsync_net to make this checkable"}
    if net_name in nets:
        return {"outcome": PRESENT, "detail": f"{net_name!r} is in the netlist"}
    return {"outcome": ABSENT,
            "detail": f"route is {route!r} but {net_name!r} is not in the "
                      "netlist; the decision and the design disagree"}


CATALOG: Dict[str, BlockTemplate] = {
    template.key: template for template in (
        BlockTemplate(
            key="decoupling_shell",
            title="Decade-spaced bypass bank on one rail",
            rationale="A capacitor is low impedance only near and below its "
                      "own self-resonance, so a band is covered in decade "
                      "steps. More of one value widens nothing.",
            parameters=(
                Parameter("rail", "net name of the rail being bypassed"),
                Parameter("decades", "how many decade steps the bank spans"),
                Parameter("bulk_farads", "the largest member's nominal value"),
                Parameter("capacitor_prefix",
                          "reference-designator prefix used for capacitors; a "
                          "CONVENTION, which is why verification says so",
                          required=False, default="C"),
            ),
            emit=_emit_decoupling_shell,
            verify=_verify_decoupling_shell,
        ),
        BlockTemplate(
            key="differential_pair_rule",
            title="Differential impedance rule for one pair",
            rationale="The impedance target travels with the connector and "
                      "link choice. Recording it as a block keeps it from "
                      "being rediscovered - or not - during layout.",
            parameters=(
                Parameter("netclass", "netclass name the rule attaches to"),
                Parameter("positive_net", "the pair's P net"),
                Parameter("negative_net", "the pair's N net"),
                Parameter("impedance_ohm", "differential target"),
                Parameter("tolerance_pct", "acceptance window, in percent"),
            ),
            emit=_emit_differential_pair_rule,
            verify=_verify_differential_pair_rule,
        ),
        BlockTemplate(
            key="link_architecture",
            title="Serialiser presence and frame-sync routing",
            rationale="These two travel together: how frame sync reaches the "
                      "sensor depends on whether a serialiser exists, and "
                      "checking for a frame-sync net without knowing the route "
                      "produces a false defect under back-channel.",
            parameters=(
                Parameter("serdes_required", "does the link carry a serialiser"),
                Parameter("fsync_route", f"one of {sorted(FSYNC_ROUTES)}"),
                Parameter("fsync_net",
                          "net to look for; only meaningful for gpio and "
                          "connector_pin routes",
                          required=False, default=None),
            ),
            emit=_emit_link_architecture,
            verify=_verify_link_architecture,
        ),
    )
}


def instantiate(key: str, params: Dict[str, Any],
                nets: Optional[dict] = None) -> BlockInstance:
    """Build one block instance, verifying it when a netlist is supplied."""
    template = CATALOG.get(key)
    if template is None:
        raise BlockRefusal(key, [], f"unknown block; catalogue has "
                                    f"{sorted(CATALOG)}")
    resolved = template.resolve(params)
    instance = BlockInstance(key=key, params=resolved,
                             emitted=template.emit(resolved))
    if template.verify is None:
        instance.verification = {
            "outcome": UNVERIFIABLE,
            "detail": "this template declares no verification method; its "
                      "presence in a design is unchecked, not confirmed"}
    elif nets is None:
        instance.verification = {
            "outcome": UNVERIFIABLE,
            "detail": "no netlist supplied; the block was emitted but nothing "
                      "checked whether the design contains it"}
    else:
        instance.verification = template.verify(resolved, nets)
    return instance
