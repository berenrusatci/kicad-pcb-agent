#!/usr/bin/env python3
"""Cross-check what a connector can CARRY against what a rail DEMANDS.

CAPACITY IS NOT DEMAND
----------------------
A connector datasheet states a CAPACITY: how much current one contact may
carry before it overheats. A rail tree states a DEMAND: how much current the
load actually draws in the worst case. They are different quantities from
different documents, and the row this tool serves - "Konnektör akımı
(derating sonrası)" in the verification matrix - is the comparison between
them. Reading a per-contact rating as though it were the rail's draw inverts
the question and always answers yes.

WHY connector_spec's OWN rail_cross_check BLOCK IS NOT THE ANSWER
-----------------------------------------------------------------
``connector_spec.json`` has a ``current.rail_cross_check`` object with a
``rail_worst_case_a`` field. Trusting it would be a cross-check of a document
against itself: whoever wrote the connector record also wrote the number it
is checked against. So this tool NEVER reads demand from there. It reads
demand from ``rail_tree.json`` and capacity from ``connector_spec.json``, and
where the spec pre-declared a worst case it COMPARES the two: a disagreement
between two documents about one quantity is a FAIL, because after that
neither can be believed.

WHY N x I_rated IS NOT OFFERED
------------------------------
Contacts in parallel do not carry N times one contact's current. The centre
contact runs hottest, its resistance rises with temperature, and it sheds
current to the outer ones; and the derating curve is a function of BOTH
ambient temperature and how many adjacent contacts are carrying. A group
capacity therefore has to be READ off the curve for that grouping, not
multiplied. ``capacity.policy: n_times_rated`` does not load.

WHY A ROOM-TEMPERATURE RATING IS NEVER SUBSTITUTED
--------------------------------------------------
``skills/konnektor/SKILL.md`` §1 and §3b: the datasheet number is a single
contact on a bare test board at 20-25 C ambient with a 30 C rise. Without the
derating curve read at the ambient the thermal report actually predicts,
there is no capacity figure - and the room-temperature number is not a
conservative stand-in, it is the optimistic one. Absent curve, absent
ambient, or a basis that is a local screening policy rather than the curve:
``NO_COVERAGE``, never a number.

THE SOURCE TRAVELS WITH THE NUMBER
----------------------------------
Whoever states a value states where it came from:

  * single-contact capacity -> ``connector_spec.datasheet_locator`` plus the
    ``derating_basis`` / ``ambient_c`` pair that says how it was derated;
  * group capacity -> the map, because no field in connector-spec/v1 can
    hold a per-GROUP rating, and the source block beside it;
  * rail demand -> the map, because rail-tree/v1 pins exactly seven fields
    per rail and forbids others, so there is nowhere in it to record where
    ``i_max_a`` came from. That asymmetry is stated here rather than papered
    over by treating the file's existence as its own provenance.

WHAT IS NOT HERE, ON PURPOSE
----------------------------
Which rail passes through which contact group is a PROJECT fact and lives in
``system-planning/``, not in this file. It arrives as ``--map``. This tool
ships no connector, no rail and no grouping of its own.

Usage:
    connector_current.py --connector-spec connector_spec.json \\
                         --rail-tree rail_tree.json \\
                         --map connector_rail_map.json [--json report.json]
    connector_current.py --self-test

Exit codes: 0 = GREEN, 1 = RED or NO_COVERAGE, 2 = the tool could not run.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (FAIL, Finding, NO_COVERAGE, PASS,  # noqa: E402
                             coverage_rule_violations, result, summarise)
from common.hashing import sha256_file  # noqa: E402
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "connector-current-report/v1"
TOOL_NAME = "connector_current"

CONNECTOR_SPEC_VERSION = "connector-spec/v1"
RAIL_TREE_VERSION = "rail-tree/v1"
MAP_VERSION = "connector-rail-map/v1"

# Provenance. ``revision`` is in the list for the reason vendormodel.py gives:
# a document without one names a FAMILY of documents, and the one that was
# read is not recoverable later. ``locator`` is in it because "the datasheet"
# is not an answer to "which curve, at which ambient".
SOURCE_FIELDS = ("document", "revision", "locator", "accessed")

# How a group's capacity may be established. There is no third option and no
# default: an unlabelled policy does not load.
POLICY_SINGLE = "single_contact"
POLICY_GROUP = "sourced_group_rating"
POLICIES = (POLICY_SINGLE, POLICY_GROUP)

# Named so the refusal can be specific about what was attempted.
REFUSED_POLICIES = ("n_times_rated", "n_times_i", "parallel_sum",
                    "sum_of_contacts", "contacts_times_rated")

BASIS_DATASHEET_CURVE = "datasheet_curve"
BASIS_LOCAL_SCREEN = "adjacent_contact_screen"

# From connector_spec.schema.json's ``role`` enum. These are the roles that
# carry current and therefore need a rail behind them.
POWER_ROLES = ("power_in", "power_out", "bidirectional")
ROLE_UNKNOWN = "unknown"

GREEN = "GREEN"
RED = "RED"

# Reason codes. Every one of these is a thing a permissive version of this
# tool would have called PASS.
RAIL_NOT_IN_TREE = "RAIL_NOT_IN_RAIL_TREE"
REFDES_NOT_IN_SPEC = "REFDES_NOT_IN_CONNECTOR_SPEC"
DEMAND_ABSENT = "RAIL_WORST_CASE_DEMAND_ABSENT"
DEMAND_UNSOURCED = "RAIL_WORST_CASE_DEMAND_UNSOURCED"
DERATING_BASIS_ABSENT = "DERATING_BASIS_ABSENT"
DERATING_BASIS_IS_LOCAL_SCREEN = "DERATING_BASIS_IS_LOCAL_SCREEN"
DERATED_CAPACITY_ABSENT = "DERATED_CAPACITY_ABSENT"
ROOM_TEMPERATURE_NOT_SUBSTITUTED = "ROOM_TEMPERATURE_RATING_NOT_SUBSTITUTED"
AMBIENT_ABSENT = "DERATING_AMBIENT_ABSENT"
CAPACITY_UNSOURCED = "CONNECTOR_CAPACITY_UNSOURCED"
GROUP_RATING_ABSENT = "GROUP_RATING_ABSENT"
GROUP_RATING_UNSOURCED = "GROUP_RATING_UNSOURCED"
SINGLE_CONTACT_CONTRADICTS_GROUP = "SINGLE_CONTACT_POLICY_CONTRADICTS_GROUP"
PARALLEL_ASSUMPTION_UNDECLARED = "PARALLEL_PIN_ASSUMPTION_UNDECLARED"
DERATED_EXCEEDS_RATED = "DERATED_EXCEEDS_RATED"
DEMAND_EXCEEDS_CAPACITY = "DEMAND_EXCEEDS_CONNECTOR_CAPACITY"
MARGIN_THIN = "CONNECTOR_MARGIN_THIN"
SPEC_BLOCK_CONTRADICTS = "SPEC_DECLARED_DEMAND_CONTRADICTS_RAIL_TREE"
MAPPING_INCOMPLETE = "MAPPING_INCOMPLETE"
POWER_CONNECTOR_UNMAPPED = "POWER_CONNECTOR_UNMAPPED"
CONNECTOR_ROLE_UNKNOWN = "CONNECTOR_ROLE_UNKNOWN"

LIMITS = [
    "compares two DOCUMENTS; it measures no contact, no rise and no board. "
    "Contact current sharing is an electro-thermal measurement (matrix "
    "level C->D) and nothing here promotes it",
    "capacity for more than one contact is READ from a source, never "
    "computed as N x I_rated - parallel contacts do not add linearly",
    "the room-temperature rating is never substituted for a missing "
    "derating curve; that substitution is optimistic, not conservative",
    "the rail->contact-group mapping is an INPUT. This tool knows no "
    "project and invents no grouping",
    "demand is read only from rail_tree; connector_spec's own "
    "rail_cross_check block is treated as a claim to be checked, never as "
    "the source",
    "inrush does not appear in a worst-case average: a bulk capacitor's "
    "charging current erodes the contact and is a separate question "
    "(konnektor SKILL §3f)",
    "no margin is assumed. Without --min-margin-a only the SIGN of "
    "capacity - demand is judged",
]


class ConnectorCurrentError(ValueError):
    """The run cannot produce an answer. Never downgraded to a verdict."""


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def read_json(path: Path, label: str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConnectorCurrentError(f"{label}: cannot read {path}: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConnectorCurrentError(
            f"{label}: {path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConnectorCurrentError(f"{label}: {path} must be a JSON object")
    return doc


def _pin_version(doc: dict, expected: str, label: str, path: Path) -> None:
    """A version bump may redefine a field; consuming it blind is how a
    renamed quantity becomes a silently wrong comparison."""
    found = doc.get("schema_version")
    if found != expected:
        raise ConnectorCurrentError(
            f"{label}: {path} declares schema_version {found!r}, this tool "
            f"reads {expected!r}. The version is not ignored: a later "
            "revision may redefine a field this comparison depends on")


def read_source(block: Any) -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Validate one declared source. Returns (source, missing field names)."""
    if not isinstance(block, dict):
        return None, list(SOURCE_FIELDS)
    missing = [f for f in SOURCE_FIELDS if not str(block.get(f, "") or "").strip()]
    if missing:
        return None, missing
    source = {f: str(block[f]).strip() for f in SOURCE_FIELDS}
    for key, value in block.items():
        if key not in source:
            source[str(key)] = value
    return source, []


@dataclass(frozen=True)
class Link:
    """One rail carried by one contact group of one connector."""

    rail: str
    refdes: str
    group_name: str
    contacts: Tuple[str, ...]
    policy: str
    group_rated_a: Optional[float]
    capacity_source: Optional[Dict[str, str]]
    capacity_source_missing: Tuple[str, ...]
    demand_source: Optional[Dict[str, str]]
    demand_source_missing: Tuple[str, ...]

    @property
    def contact_count(self) -> int:
        return len(self.contacts)


@dataclass
class Exclusion:
    rail: str
    reason: str

    def to_dict(self) -> dict:
        return {"rail": self.rail, "reason": self.reason}


def _contacts(entry: dict, where: str) -> Tuple[str, ...]:
    group = entry.get("contact_group")
    if not isinstance(group, dict):
        raise ConnectorCurrentError(f"{where}: 'contact_group' must be an object")
    contacts = group.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        raise ConnectorCurrentError(
            f"{where}: contact_group.contacts must be a non-empty array. The "
            "count is not defaulted to 1: a group whose size is guessed sets "
            "the capacity question this tool exists to ask")
    names = [str(c).strip() for c in contacts]
    if any(not n for n in names):
        raise ConnectorCurrentError(f"{where}: a contact designator is empty")
    if len(set(names)) != len(names):
        raise ConnectorCurrentError(
            f"{where}: the same contact is listed twice; a duplicated "
            "designator inflates the group size without adding a contact")
    return tuple(names)


def _policy(entry: dict, where: str) -> Tuple[str, Optional[float]]:
    capacity = entry.get("capacity")
    if not isinstance(capacity, dict):
        raise ConnectorCurrentError(f"{where}: 'capacity' must be an object")
    policy = capacity.get("policy")
    if isinstance(policy, str) and policy.strip().lower() in REFUSED_POLICIES:
        raise ConnectorCurrentError(
            f"{where}: capacity.policy {policy!r} is not offered. N contacts "
            "do not carry N times one contact's current: the centre contact "
            "runs hottest, its resistance rises, and it sheds current to the "
            "outer ones. The derating curve is a function of ambient AND of "
            "how many adjacent contacts are carrying, so a group's capacity "
            f"has to be READ for that grouping. Use {POLICY_GROUP!r} with the "
            f"figure and its source, or {POLICY_SINGLE!r} if one contact "
            "carries the rail")
    if policy not in POLICIES:
        raise ConnectorCurrentError(
            f"{where}: capacity.policy must be one of {list(POLICIES)}, got "
            f"{policy!r}. There is no default: defaulting to the single-contact "
            "figure would silently treat every multi-contact group as though "
            "one contact carried the whole rail")
    rated = capacity.get("group_rated_a")
    if rated is not None:
        if isinstance(rated, bool) or not isinstance(rated, (int, float)):
            raise ConnectorCurrentError(
                f"{where}: capacity.group_rated_a must be a number or null")
        if rated <= 0:
            raise ConnectorCurrentError(
                f"{where}: capacity.group_rated_a must be > 0")
        rated = float(rated)
    return policy, rated


def parse_map(doc: dict, path: Path) -> Tuple[List[Link], List[Exclusion]]:
    """Read the project's rail -> contact-group mapping.

    Refuses rather than repairs. Every refusal here is a case where the
    alternative is inventing the project fact the map exists to supply.
    """
    _pin_version(doc, MAP_VERSION, "map", path)
    entries = doc.get("links")
    if not isinstance(entries, list):
        raise ConnectorCurrentError(f"map: {path} needs a 'links' array")
    links: List[Link] = []
    seen: set = set()
    for index, entry in enumerate(entries):
        where = f"map: links[{index}]"
        if not isinstance(entry, dict):
            raise ConnectorCurrentError(f"{where} must be an object")
        rail = str(entry.get("rail", "") or "").strip()
        refdes = str(entry.get("refdes", "") or "").strip()
        if not rail or not refdes:
            raise ConnectorCurrentError(
                f"{where}: 'rail' and 'refdes' are both required; a link "
                "missing either end connects nothing")
        contacts = _contacts(entry, where)
        group_name = str(entry.get("contact_group", {}).get("name", "")
                         or "").strip() or f"{refdes}:{','.join(contacts)}"
        key = (rail, refdes, group_name)
        if key in seen:
            raise ConnectorCurrentError(
                f"{where}: {rail} through {refdes}/{group_name} is declared "
                "twice; which entry judged the link would depend on order")
        seen.add(key)
        policy, group_rated_a = _policy(entry, where)
        capacity_source, capacity_missing = read_source(
            entry.get("capacity", {}).get("source"))
        demand_block = entry.get("demand")
        demand_source, demand_missing = read_source(
            demand_block.get("source") if isinstance(demand_block, dict) else None)
        links.append(Link(
            rail=rail, refdes=refdes, group_name=group_name, contacts=contacts,
            policy=policy, group_rated_a=group_rated_a,
            capacity_source=capacity_source,
            capacity_source_missing=tuple(capacity_missing),
            demand_source=demand_source,
            demand_source_missing=tuple(demand_missing)))

    excluded: List[Exclusion] = []
    raw_excluded = doc.get("excluded_rails", []) or []
    if not isinstance(raw_excluded, list):
        raise ConnectorCurrentError(f"map: {path}: 'excluded_rails' must be an array")
    for index, entry in enumerate(raw_excluded):
        where = f"map: excluded_rails[{index}]"
        if not isinstance(entry, dict):
            raise ConnectorCurrentError(f"{where} must be an object")
        rail = str(entry.get("rail", "") or "").strip()
        reason = str(entry.get("reason", "") or "").strip()
        if not rail:
            raise ConnectorCurrentError(f"{where}: 'rail' is required")
        if not reason:
            raise ConnectorCurrentError(
                f"{where}: excluding {rail!r} needs a stated reason. An "
                "exclusion without one is how a rail leaves the numerator and "
                "the denominator at the same time")
        excluded.append(Exclusion(rail=rail, reason=reason))
    return links, excluded


def parse_connector_spec(doc: dict, path: Path) -> Dict[str, dict]:
    _pin_version(doc, CONNECTOR_SPEC_VERSION, "connector_spec", path)
    connectors = doc.get("connectors")
    if not isinstance(connectors, list) or not connectors:
        raise ConnectorCurrentError(
            f"connector_spec: {path} needs a non-empty 'connectors' array")
    by_refdes: Dict[str, dict] = {}
    for entry in connectors:
        if not isinstance(entry, dict):
            raise ConnectorCurrentError("connector_spec: each connector must "
                                        "be an object")
        refdes = str(entry.get("refdes", "") or "").strip()
        if not refdes:
            raise ConnectorCurrentError("connector_spec: a connector has no refdes")
        if refdes in by_refdes:
            raise ConnectorCurrentError(
                f"connector_spec: {refdes} appears twice")
        by_refdes[refdes] = entry
    return by_refdes


def parse_rail_tree(doc: dict, path: Path) -> Dict[str, dict]:
    _pin_version(doc, RAIL_TREE_VERSION, "rail_tree", path)
    rails = doc.get("rails")
    if not isinstance(rails, dict) or not rails:
        raise ConnectorCurrentError(
            f"rail_tree: {path} needs a non-empty 'rails' object")
    for name, entry in rails.items():
        if not isinstance(entry, dict):
            raise ConnectorCurrentError(f"rail_tree: rail {name!r} must be an object")
    return rails


# --------------------------------------------------------------------------- #
# Capacity - what the connector can carry
# --------------------------------------------------------------------------- #

def capacity_of(link: Link, connector: Optional[dict]
                ) -> Tuple[Optional[float], List[str], dict]:
    """The group's derated capacity, or None with the reason it is unknown.

    Returns ``(amps, reason_codes, provenance)``. ``amps`` is None whenever
    the capacity would have to be guessed - and the room-temperature rating
    is a guess, not a fallback.
    """
    codes: List[str] = []
    provenance: dict = {
        "refdes": link.refdes,
        "contact_group": link.group_name,
        "contact_count": link.contact_count,
        "contacts": list(link.contacts),
        "policy": link.policy,
    }
    if connector is None:
        return None, [REFDES_NOT_IN_SPEC], provenance

    current = connector.get("current")
    if not isinstance(current, dict):
        return None, [DERATED_CAPACITY_ABSENT], provenance

    rated = current.get("rated_a_per_contact")
    derated = current.get("derated_a_per_contact")
    basis = current.get("derating_basis")
    ambient = current.get("ambient_c")
    locator = str(connector.get("datasheet_locator", "") or "").strip()

    provenance.update({
        "connector_mpn": connector.get("mpn"),
        "datasheet_locator": locator or None,
        "derating_basis": basis,
        "ambient_c": ambient,
        "rated_a_per_contact": rated,
        "derated_a_per_contact": derated,
        "adjacent_carrying_contacts": current.get("adjacent_carrying_contacts"),
        "parallel_pin_assumption": current.get("parallel_pin_assumption"),
    })

    # ---- the derating basis, before any number is believed ----------------
    if basis is None or basis == "unknown":
        codes.append(DERATING_BASIS_ABSENT)
    elif basis == BASIS_LOCAL_SCREEN:
        # connector_spec.schema.json marks this LOCAL_POLICY. A local screen
        # is a real screening signal and not a datasheet curve, so it can
        # rule a connector OUT but never sign one off.
        codes.append(DERATING_BASIS_IS_LOCAL_SCREEN)
    elif basis != BASIS_DATASHEET_CURVE:
        codes.append(DERATING_BASIS_ABSENT)

    if ambient is None:
        codes.append(AMBIENT_ABSENT)

    if derated is None:
        codes.append(DERATED_CAPACITY_ABSENT)
        if rated is not None:
            # Stated as its own code so a reader sees the substitution was
            # AVAILABLE and refused, rather than wondering why a number that
            # is sitting right there was not used.
            codes.append(ROOM_TEMPERATURE_NOT_SUBSTITUTED)
    elif rated is not None and float(derated) > float(rated):
        # Not a missing input: a stated one that cannot be true. Derating
        # removes capacity.
        codes.append(DERATED_EXCEEDS_RATED)

    if not locator:
        codes.append(CAPACITY_UNSOURCED)

    # ---- the grouping ------------------------------------------------------
    amps: Optional[float] = None
    if link.policy == POLICY_SINGLE:
        if link.contact_count != 1:
            codes.append(SINGLE_CONTACT_CONTRADICTS_GROUP)
        elif derated is not None:
            amps = float(derated)
            provenance["capacity_from"] = (
                "connector_spec.connectors[].current.derated_a_per_contact")
    else:
        if link.contact_count > 1:
            if current.get("parallel_pin_assumption") is None:
                codes.append(PARALLEL_ASSUMPTION_UNDECLARED)
        if link.group_rated_a is None:
            codes.append(GROUP_RATING_ABSENT)
        if link.capacity_source is None:
            codes.append(GROUP_RATING_UNSOURCED)
            provenance["source_missing_fields"] = list(link.capacity_source_missing)
        if link.group_rated_a is not None and link.capacity_source is not None:
            amps = float(link.group_rated_a)
            provenance["capacity_from"] = "map.links[].capacity.group_rated_a"
            provenance["source"] = dict(link.capacity_source)

    if codes:
        # One blocking code is enough to withhold the number. A capacity that
        # survives half its checks is not a capacity.
        amps = None
    provenance["capacity_a"] = amps
    return amps, codes, provenance


# --------------------------------------------------------------------------- #
# Demand - what the rail draws
# --------------------------------------------------------------------------- #

def demand_of(link: Link, rail: Optional[dict]
              ) -> Tuple[Optional[float], List[str], dict]:
    codes: List[str] = []
    provenance: dict = {
        "rail": link.rail,
        "demand_from": f"rail_tree.rails.{link.rail}.i_max_a",
    }
    if rail is None:
        return None, [RAIL_NOT_IN_TREE], provenance

    i_max = rail.get("i_max_a")
    provenance["i_max_a"] = i_max
    provenance["standard"] = rail.get("standard")
    if i_max is None:
        codes.append(DEMAND_ABSENT)
    if link.demand_source is None:
        codes.append(DEMAND_UNSOURCED)
        provenance["source_missing_fields"] = list(link.demand_source_missing)
    else:
        provenance["source"] = dict(link.demand_source)

    amps = None if codes else float(i_max)
    provenance["demand_a"] = amps
    return amps, codes, provenance


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #

@dataclass
class LinkResult:
    link: Link
    status: str
    codes: List[str] = field(default_factory=list)
    capacity_a: Optional[float] = None
    demand_a: Optional[float] = None
    capacity_provenance: dict = field(default_factory=dict)
    demand_provenance: dict = field(default_factory=dict)
    spec_declared_demand_a: Optional[float] = None
    detail: str = ""

    @property
    def margin_a(self) -> Optional[float]:
        if self.capacity_a is None or self.demand_a is None:
            return None
        return self.capacity_a - self.demand_a

    def to_dict(self) -> dict:
        return {
            "rail": self.link.rail,
            "refdes": self.link.refdes,
            "contact_group": self.link.group_name,
            "contact_count": self.link.contact_count,
            "status": self.status,
            "reason_codes": list(self.codes),
            "capacity_a": self.capacity_a,
            "demand_a": self.demand_a,
            "margin_a": self.margin_a,
            "spec_declared_demand_a": self.spec_declared_demand_a,
            "detail": self.detail,
            "provenance": {
                "capacity": self.capacity_provenance,
                "demand": self.demand_provenance,
            },
        }


def _declared_demand(connector: Optional[dict], rail_name: str) -> Optional[float]:
    """The worst case the connector_spec pre-declared for this rail, if any.

    Read to be CHECKED, never to be used as demand.
    """
    if not isinstance(connector, dict):
        return None
    current = connector.get("current")
    if not isinstance(current, dict):
        return None
    block = current.get("rail_cross_check")
    if not isinstance(block, dict):
        return None
    if str(block.get("rail", "")).strip() != rail_name:
        return None
    value = block.get("rail_worst_case_a")
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def evaluate(link: Link, connector: Optional[dict], rail: Optional[dict],
             min_margin_a: Optional[float]) -> LinkResult:
    capacity_a, capacity_codes, capacity_prov = capacity_of(link, connector)
    demand_a, demand_codes, demand_prov = demand_of(link, rail)
    codes = list(capacity_codes) + list(demand_codes)

    declared = _declared_demand(connector, link.rail)
    if declared is not None and demand_a is not None and not math.isclose(
            declared, demand_a, rel_tol=1e-9, abs_tol=0.0):
        # The tolerance is float representation, not engineering margin: two
        # documents claiming the same quantity must claim the same number.
        codes.append(SPEC_BLOCK_CONTRADICTS)

    if capacity_a is None or demand_a is None:
        status = NO_COVERAGE
        detail = ("nothing was compared: "
                  + ", ".join(codes[:4] or ["unknown"]))
        return LinkResult(link=link, status=status, codes=codes,
                          capacity_a=capacity_a, demand_a=demand_a,
                          capacity_provenance=capacity_prov,
                          demand_provenance=demand_prov,
                          spec_declared_demand_a=declared, detail=detail)

    margin = capacity_a - demand_a
    if margin < 0:
        codes.append(DEMAND_EXCEEDS_CAPACITY)
    elif min_margin_a is not None and margin < min_margin_a:
        codes.append(MARGIN_THIN)

    status = FAIL if codes else PASS
    if status == FAIL:
        detail = (f"{link.rail} draws {demand_a:g} A through {link.refdes} "
                  f"{link.group_name}, which carries {capacity_a:g} A derated")
    else:
        detail = (f"{demand_a:g} A of {capacity_a:g} A derated "
                  f"({margin:g} A spare)")
    return LinkResult(link=link, status=status, codes=codes,
                      capacity_a=capacity_a, demand_a=demand_a,
                      capacity_provenance=capacity_prov,
                      demand_provenance=demand_prov,
                      spec_declared_demand_a=declared, detail=detail)


# --------------------------------------------------------------------------- #
# Coverage of the mapping itself
# --------------------------------------------------------------------------- #

def rail_coverage(rails: Dict[str, dict], links: Sequence[Link],
                  excluded: Sequence[Exclusion]) -> Tuple[dict, List[dict]]:
    """eligible / scanned / excluded over the rail tree, plus what broke it.

    The triple obeys ``common.findings.coverage_rule_violations``: ``scanned``
    is the rails a link actually reached, ``excluded`` is every other rail in
    the tree - whether the map excluded it on purpose or forgot it. Which of
    those two it was is NOT hidden by the sum coming out right: a forgotten
    rail is listed in the returned violations, and the finding built from them
    is NO_COVERAGE.
    """
    tree = set(rails)
    linked = {link.rail for link in links}
    excluded_names = {entry.rail for entry in excluded}
    violations: List[dict] = []

    contradicted = sorted(linked & excluded_names & tree)
    for name in contradicted:
        violations.append({"issue": MAPPING_INCOMPLETE, "rail": name,
                           "detail": "declared both as carried by a connector "
                                     "and as excluded"})
    for name in sorted(linked - tree):
        violations.append({"issue": RAIL_NOT_IN_TREE, "rail": name,
                           "detail": "the map carries a rail the rail tree "
                                     "does not contain"})
    for name in sorted(excluded_names - tree):
        violations.append({"issue": RAIL_NOT_IN_TREE, "rail": name,
                           "detail": "the map excludes a rail the rail tree "
                                     "does not contain"})
    for name in sorted(tree - linked - excluded_names):
        violations.append({"issue": MAPPING_INCOMPLETE, "rail": name,
                           "detail": "neither carried by a declared contact "
                                     "group nor excluded with a reason"})

    scanned = linked & tree
    coverage = {
        "eligible": len(tree),
        "scanned": len(scanned),
        "excluded": len(tree - scanned),
        # Only breakage among the rails that WERE scanned; an unaccounted rail
        # is a coverage hole, not a violation found in something examined.
        "violations": len(contradicted),
    }
    return coverage, violations


def connector_coverage(connectors: Dict[str, dict],
                       links: Sequence[Link]) -> Tuple[List[str], List[dict]]:
    """Which power-carrying connectors no link mentions."""
    mapped = {link.refdes for link in links}
    violations: List[dict] = []
    eligible: List[str] = []
    for refdes, entry in sorted(connectors.items()):
        role = entry.get("role")
        if role == ROLE_UNKNOWN or role is None:
            eligible.append(refdes)
            if refdes not in mapped:
                violations.append({
                    "issue": CONNECTOR_ROLE_UNKNOWN, "refdes": refdes,
                    "detail": "role is unknown, so whether it carries a rail "
                              "cannot be decided; it is not silently skipped"})
            continue
        if role not in POWER_ROLES:
            continue
        eligible.append(refdes)
        if refdes not in mapped:
            violations.append({
                "issue": POWER_CONNECTOR_UNMAPPED, "refdes": refdes,
                "role": role,
                "detail": "carries current per its role but no rail is mapped "
                          "through it"})
    return eligible, violations


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def mapping_finding(coverage: dict, violations: List[dict]) -> Finding:
    check = "mapping_accounts_for_every_rail"
    if coverage["eligible"] == 0:
        return Finding(check, NO_COVERAGE, 0, [], "the rail tree has no rails")
    if violations:
        return Finding(check, NO_COVERAGE, coverage["scanned"], violations,
                       f"{len(violations)} rail(s) are unaccounted for; an "
                       "incomplete map means the rails it omits were never "
                       "compared against anything")
    if coverage["scanned"] == 0:
        return Finding(check, NO_COVERAGE, 0, [],
                       "every rail is excluded; no rail reaches a connector "
                       "in this map, so there is nothing to cross-check")
    return result(check, coverage["scanned"], [],
                  f"{coverage['scanned']} rail(s) mapped, "
                  f"{coverage['excluded']} excluded with a reason")


def connector_finding(eligible: List[str], violations: List[dict]) -> Finding:
    check = "mapping_accounts_for_every_power_connector"
    if not eligible:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no connector declares a current-carrying role; with "
                       "every role signal_only or measurement_only there is "
                       "nothing for this check to cover")
    if violations:
        # An unmapped power connector is a HOLE, not a defect: nothing about
        # the connector is known to be wrong, it was simply never compared
        # against anything. Reporting it as FAIL would send somebody hunting
        # for a defect that is not there.
        return Finding(check, NO_COVERAGE, len(eligible) - len(violations),
                       violations,
                       f"{len(violations)} of {len(eligible)} "
                       "current-carrying connector(s) carry no mapped rail, so "
                       "nothing was cross-checked through them")
    return result(check, len(eligible), violations,
                  f"{len(eligible)} current-carrying connector(s)")


CAPACITY_CODES = (DERATING_BASIS_ABSENT, DERATING_BASIS_IS_LOCAL_SCREEN,
                  DERATED_CAPACITY_ABSENT, ROOM_TEMPERATURE_NOT_SUBSTITUTED,
                  AMBIENT_ABSENT, CAPACITY_UNSOURCED, GROUP_RATING_ABSENT,
                  GROUP_RATING_UNSOURCED, SINGLE_CONTACT_CONTRADICTS_GROUP,
                  PARALLEL_ASSUMPTION_UNDECLARED, REFDES_NOT_IN_SPEC)
DEMAND_CODES = (DEMAND_ABSENT, DEMAND_UNSOURCED, RAIL_NOT_IN_TREE)


def capacity_finding(results: Sequence[LinkResult]) -> Finding:
    check = "connector_capacity_is_derated_and_sourced"
    if not results:
        return Finding(check, NO_COVERAGE, 0, [], "no link to judge")
    blocked = [r for r in results
               if any(c in CAPACITY_CODES for c in r.codes)]
    hard = [r.to_dict() for r in results if DERATED_EXCEEDS_RATED in r.codes]
    if hard:
        return result(check, len(results), hard,
                      "a derated figure larger than the rating it came from "
                      "is not a derating")
    if blocked:
        return Finding(check, NO_COVERAGE, len(results) - len(blocked), [],
                       f"{len(blocked)} link(s) have no usable derated "
                       "capacity. The room-temperature rating is not "
                       "substituted: it is the optimistic number, not a "
                       "conservative stand-in")
    return result(check, len(results), [],
                  f"all {len(results)} link(s) carry a derated figure with a "
                  "stated basis, ambient and document")


def demand_finding(results: Sequence[LinkResult]) -> Finding:
    check = "rail_demand_is_present_and_sourced"
    if not results:
        return Finding(check, NO_COVERAGE, 0, [], "no link to judge")
    blocked = [r for r in results if any(c in DEMAND_CODES for c in r.codes)]
    if blocked:
        return Finding(check, NO_COVERAGE, len(results) - len(blocked), [],
                       f"{len(blocked)} link(s) have no sourced worst-case "
                       "demand in the rail tree; rail-tree/v1 has no field "
                       "for provenance, so the map must carry it")
    return result(check, len(results), [],
                  f"all {len(results)} link(s) read a sourced i_max_a")


def comparison_finding(results: Sequence[LinkResult],
                       min_margin_a: Optional[float]) -> Finding:
    check = "demand_within_connector_capacity"
    judged = [r for r in results if r.margin_a is not None]
    if not judged:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no link had both a capacity and a demand, so nothing "
                       "was compared")
    violations = [r.to_dict() for r in judged
                  if DEMAND_EXCEEDS_CAPACITY in r.codes or MARGIN_THIN in r.codes]
    detail = f"{len(judged)} of {len(results)} link(s) compared"
    if min_margin_a is None:
        detail += " (no --min-margin-a given, so only the SIGN was judged)"
    return result(check, len(judged), violations, detail)


def spec_block_finding(results: Sequence[LinkResult]) -> Finding:
    check = "connector_spec_declared_cross_check_agrees"
    declared = [r for r in results if r.spec_declared_demand_a is not None]
    if not declared:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no connector_spec pre-declared a rail_worst_case_a. "
                       "The comparison in this report stands on its own, but "
                       "the konnektor skill's acceptance criterion asks the "
                       "spec to RECORD the cross-check, and none did")
    violations = [r.to_dict() for r in declared
                  if SPEC_BLOCK_CONTRADICTS in r.codes]
    return result(check, len(declared), violations,
                  f"{len(declared)} pre-declared worst case(s) against the "
                  "rail tree")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--connector-spec", type=Path, dest="connector_spec",
                        help="connector_spec.json (connector-spec/v1): the "
                             "CAPACITY side")
    parser.add_argument("--rail-tree", type=Path, dest="rail_tree",
                        help="rail_tree.json (rail-tree/v1): the DEMAND side")
    parser.add_argument("--map", type=Path, dest="map_path",
                        help="connector-rail-map/v1: which rail passes "
                             "through which contact group. A PROJECT fact; "
                             "this tool ships none")
    parser.add_argument("--min-margin-a", type=float, dest="min_margin_a",
                        help="required headroom in amps between capacity and "
                             "demand. NOT DEFAULTED: without it only the SIGN "
                             "of capacity - demand is judged")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def _artifact(path: Path) -> dict:
    return {"path": str(path), "sha256": sha256_file(path)}


def build_report(connector_spec_path: Path, rail_tree_path: Path,
                 map_path: Path, min_margin_a: Optional[float]) -> dict:
    spec_doc = read_json(connector_spec_path, "connector_spec")
    rail_doc = read_json(rail_tree_path, "rail_tree")
    map_doc = read_json(map_path, "map")

    connectors = parse_connector_spec(spec_doc, connector_spec_path)
    rails = parse_rail_tree(rail_doc, rail_tree_path)
    links, excluded = parse_map(map_doc, map_path)

    results = [evaluate(link, connectors.get(link.refdes),
                        rails.get(link.rail), min_margin_a)
               for link in links]

    coverage, coverage_violations = rail_coverage(rails, links, excluded)
    eligible_connectors, connector_violations = connector_coverage(connectors,
                                                                   links)

    findings = [
        mapping_finding(coverage, coverage_violations),
        connector_finding(eligible_connectors, connector_violations),
        capacity_finding(results),
        demand_finding(results),
        comparison_finding(results, min_margin_a),
        spec_block_finding(results),
    ]
    counts = summarise(findings)
    if counts[FAIL]:
        status = RED
    elif counts[NO_COVERAGE]:
        status = NO_COVERAGE
    else:
        status = GREEN

    reason_codes = sorted({code for r in results for code in r.codes}
                          | {v["issue"] for v in coverage_violations}
                          | {v["issue"] for v in connector_violations})

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "early_schematic",
        "signoff": False,
        "inputs": {
            "connector_spec": _artifact(connector_spec_path),
            "rail_tree": _artifact(rail_tree_path),
            "map": _artifact(map_path),
        },
        "independence": {
            "capacity_from": "connector_spec.connectors[].current",
            "demand_from": "rail_tree.rails[].i_max_a",
            "note": "connector_spec.current.rail_cross_check is never read as "
                    "demand - it was written by whoever wrote the capacity. "
                    "Where it declares a worst case it is compared against "
                    "the rail tree and a disagreement is a FAIL",
        },
        "params": {"min_margin_a": min_margin_a},
        "links": [r.to_dict() for r in results],
        "excluded_rails": [e.to_dict() for e in excluded],
        "rail_coverage": coverage,
        "rail_coverage_violations": coverage_violations,
        "connector_coverage_violations": connector_violations,
        "findings": findings,
        "coverage": summarise(findings),
        "limits": LIMITS,
    }
    invalid = coverage_rule_violations(coverage)
    if invalid:  # pragma: no cover - arithmetic is constructed to hold
        raise ConnectorCurrentError(
            f"rail_coverage does not satisfy the coverage rule: {invalid}")
    return report


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    missing = [name for name, value in (("--connector-spec", args.connector_spec),
                                        ("--rail-tree", args.rail_tree),
                                        ("--map", args.map_path))
               if value is None]
    if missing:
        raise ConnectorCurrentError(
            f"{', '.join(missing)} required. All three are: capacity and "
            "demand must come from SEPARATE documents or the cross-check is a "
            "document checked against itself, and the rail-to-contact-group "
            "mapping is a project fact this tool does not own")

    report = build_report(args.connector_spec, args.rail_tree, args.map_path,
                          args.min_margin_a)
    _diagnostics(report)
    write_report(report, args.json)
    emit(report)
    return status_exit_code(report["status"])


def _diagnostics(report: dict) -> None:
    marks = {PASS: "ok  ", FAIL: "RED ", NO_COVERAGE: "?   "}
    diag("")
    diag(f"connector_current: {len(report['links'])} link(s)")
    for entry in report["links"]:
        diag(f"  {marks[entry['status']]} {entry['rail']:14s} -> "
             f"{entry['refdes']}/{entry['contact_group']}: {entry['detail']}")
        if entry["reason_codes"]:
            diag(f"       {', '.join(entry['reason_codes'])}")
    coverage = report["rail_coverage"]
    diag(f"  rails: {coverage['scanned']} mapped / {coverage['excluded']} "
         f"excluded / {coverage['eligible']} eligible")
    for finding in report["findings"]:
        diag(f"  {marks[finding['status']]} {finding['check']}: "
             f"{finding['detail']}")
    diag(f"  status: {report['status']}")


# --------------------------------------------------------------------------- #
# Self-test - each assertion is a case a permissive version would pass
# --------------------------------------------------------------------------- #

def _source(tag: str) -> dict:
    return {"document": f"FIXTURE {tag}", "revision": "R0",
            "locator": "self-test", "accessed": "2026-08-10"}


def _link(**overrides) -> Link:
    base = dict(rail="V3V3", refdes="J1", group_name="VDD",
                contacts=("1",), policy=POLICY_SINGLE, group_rated_a=None,
                capacity_source=None, capacity_source_missing=(),
                demand_source=_source("demand"), demand_source_missing=())
    base.update(overrides)
    return Link(**base)


def _connector(**overrides) -> dict:
    current = {"rated_a_per_contact": 2.0, "derated_a_per_contact": 1.2,
               "derating_basis": BASIS_DATASHEET_CURVE, "ambient_c": 85.0,
               "rail_cross_check": None}
    current.update(overrides.pop("current", {}))
    entry = {"refdes": "J1", "mpn": "FIXTURE-CONN", "role": "power_in",
             "datasheet_locator": "FIXTURE datasheet fig. 4",
             "current": current, "status": "CONFIRM"}
    entry.update(overrides)
    return entry


def _self_test() -> int:
    # A sourced, derated, single-contact link under its capacity passes.
    good = evaluate(_link(), _connector(), {"i_max_a": 0.8}, None)
    assert good.status == PASS, good.to_dict()
    assert good.capacity_a == 1.2 and good.demand_a == 0.8

    # Demand above capacity is the finding the row exists for.
    over = evaluate(_link(), _connector(), {"i_max_a": 1.9}, None)
    assert over.status == FAIL and DEMAND_EXCEEDS_CAPACITY in over.codes

    # THE substitution: derating curve absent, room-temperature rating
    # present and NOT used.
    no_curve = evaluate(_link(),
                        _connector(current={"derated_a_per_contact": None,
                                            "derating_basis": None}),
                        {"i_max_a": 0.8}, None)
    assert no_curve.status == NO_COVERAGE, no_curve.to_dict()
    assert ROOM_TEMPERATURE_NOT_SUBSTITUTED in no_curve.codes
    assert no_curve.capacity_a is None
    assert 2.0 not in (no_curve.capacity_a, no_curve.margin_a)

    # A local screening policy is not a datasheet curve.
    screen = evaluate(_link(),
                      _connector(current={"derating_basis": BASIS_LOCAL_SCREEN}),
                      {"i_max_a": 0.8}, None)
    assert screen.status == NO_COVERAGE
    assert DERATING_BASIS_IS_LOCAL_SCREEN in screen.codes

    # A derating read at no stated ambient is not read at 85 C either.
    hot = evaluate(_link(), _connector(current={"ambient_c": None}),
                   {"i_max_a": 0.8}, None)
    assert hot.status == NO_COVERAGE and AMBIENT_ABSENT in hot.codes

    # Demand absent, and demand unsourced, are both NO_COVERAGE.
    blind = evaluate(_link(), _connector(), {"i_max_a": None}, None)
    assert blind.status == NO_COVERAGE and DEMAND_ABSENT in blind.codes
    unsourced = evaluate(_link(demand_source=None,
                               demand_source_missing=SOURCE_FIELDS),
                         _connector(), {"i_max_a": 0.8}, None)
    assert unsourced.status == NO_COVERAGE
    assert DEMAND_UNSOURCED in unsourced.codes

    # Two contacts under a single-contact policy is a contradiction, not a
    # doubling.
    pair = evaluate(_link(contacts=("1", "2")), _connector(),
                    {"i_max_a": 0.8}, None)
    assert pair.status == NO_COVERAGE
    assert SINGLE_CONTACT_CONTRADICTS_GROUP in pair.codes

    # A group rating must be stated AND sourced; stated alone is not enough.
    stated = evaluate(_link(contacts=("1", "2"), policy=POLICY_GROUP,
                            group_rated_a=2.0),
                      _connector(current={"parallel_pin_assumption":
                                          "margin_applied"}),
                      {"i_max_a": 0.8}, None)
    assert stated.status == NO_COVERAGE
    assert GROUP_RATING_UNSOURCED in stated.codes
    sourced = evaluate(_link(contacts=("1", "2"), policy=POLICY_GROUP,
                             group_rated_a=2.0,
                             capacity_source=_source("group")),
                       _connector(current={"parallel_pin_assumption":
                                           "margin_applied"}),
                       {"i_max_a": 0.8}, None)
    assert sourced.status == PASS and sourced.capacity_a == 2.0

    # N x I_rated does not load, whatever it is spelled.
    for policy in REFUSED_POLICIES:
        try:
            _policy({"capacity": {"policy": policy}}, "self-test")
        except ConnectorCurrentError as exc:
            assert "not offered" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"policy {policy!r} was accepted")

    # A pre-written rail_cross_check that disagrees with the rail tree is a
    # FAIL, not a shortcut.
    contradiction = evaluate(
        _link(),
        _connector(current={"rail_cross_check": {"rail": "V3V3",
                                                 "rail_worst_case_a": 0.4,
                                                 "connector_is_binding_constraint": False}}),
        {"i_max_a": 0.8}, None)
    assert contradiction.status == FAIL
    assert SPEC_BLOCK_CONTRADICTS in contradiction.codes

    # A margin that EXISTS but is thin is a separate finding, and only when
    # a margin was asked for.
    thin = evaluate(_link(), _connector(), {"i_max_a": 1.15}, 0.5)
    assert thin.status == FAIL and MARGIN_THIN in thin.codes
    assert evaluate(_link(), _connector(), {"i_max_a": 1.15}, None).status == PASS

    # An incomplete map leaves rails unjudged, and says so.
    coverage, violations = rail_coverage({"A": {}, "B": {}}, [_link(rail="A")], [])
    assert violations and mapping_finding(coverage, violations)["status"] == NO_COVERAGE
    assert not coverage_rule_violations(coverage), coverage

    diag("SELF-TEST PASS: capacity and demand are read from separate "
         "documents, a missing derating curve never falls back to the "
         "room-temperature rating, a local screen is not a curve, N x I_rated "
         "does not load, a group rating must be sourced, a pre-written "
         "cross-check that disagrees with the rail tree fails, and an "
         "incomplete map is NO_COVERAGE rather than a partial pass")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
