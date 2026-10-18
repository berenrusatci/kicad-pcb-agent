"""Decision-unit state machine, dependency graph and cascade invalidation.

Ported from the archived ``karar_birimleri.py``. Two things changed in the
port, both deliberate:

1. THE ON-DISK SHAPE IS THE ONE THIS REPO ALREADY HAS.
   ``scripts/context_pack.py`` already validates a ``pcb-decision-units/v1``
   document with English field names (``decision_id``, ``question``,
   ``status``, ``depends_on``, ...) and the status vocabulary
   ``blocked/ready/proposed/accepted/rejected``. The archive wrote a second,
   incompatible Turkish shape (``karar_id``/``durum`` with
   ``ACIK/KANIT_BEKLIYOR/KABUL_EDILDI/GECERSIZ_KILINDI``). Growing a third
   dialect would have been the worst option, so this module reads BOTH and
   writes only the canonical one. ``KABUL_EDILDI`` and ``accepted`` are the
   same state; the promotion gate asks one question of one vocabulary.

2. INVALIDATION IS STILL EXPLICIT, NOT MAGIC.
   ``invalidation_triggers`` remains documentation: nothing here watches the
   filesystem. ``invalidate()`` is called by a human or a checker, and it is
   the call - not the trigger text - that cascades. The archive's docstring
   claimed the same thing; it is repeated here because the field name invites
   the opposite assumption. The closure predicate below checks only that an
   accepted decision DECLARED its triggers, never that they still hold.

3. ``accepted`` IS NOT ``closed``.
   ``accepted`` means a human made the engineering decision. ``closed`` means
   that decision is verifiably realised in the repository. ``closed`` is not a
   status anybody may write by hand - it is ``closure_state()``, computed from
   evidence, the evidence catalogue and the declared output artifact. The
   in-between state, ``accepted_but_not_closed``, is reported explicitly with
   reason codes, because that state is exactly where decision debt hides.

Cascade rule, unchanged from the archive: invalidating a decision reopens
every decision that depends on it, directly or transitively, EXCEPT those
already invalidated (overwriting those would erase their own history).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "pcb-decision-units/v1"

BLOCKED = "blocked"
READY = "ready"
PROPOSED = "proposed"
ACCEPTED = "accepted"
REJECTED = "rejected"

#: The vocabulary ``scripts/context_pack.py`` enforces. Kept identical on
#: purpose; if the two ever drift, a pack that validates would name a status
#: the promotion gate cannot read.
STATUSES = frozenset({BLOCKED, READY, PROPOSED, ACCEPTED, REJECTED})

#: Archive (Turkish) status tokens -> canonical status.
#: ``GECERSIZ_KILINDI`` ("invalidated") maps to ``blocked``: an invalidated
#: decision is not merely open again, it cannot legally proceed until its
#: premise is redecided.
LEGACY_STATUSES = {
    "ACIK": READY,
    "KANIT_BEKLIYOR": PROPOSED,
    "KABUL_EDILDI": ACCEPTED,
    "GECERSIZ_KILINDI": BLOCKED,
    "REDDEDILDI": REJECTED,
}

LEGACY_FIELDS = {
    "karar_id": "decision_id",
    "soru": "question",
    "sahip_skill": "owner_skill",
    "bagimliliklar": "depends_on",
    "kisitlar": "constraints",
    "secenekler": "options",
    "gereken_kanit": "required_evidence",
    "durum": "status",
    "gecersizlik_tetikleyicileri": "invalidation_triggers",
    "secilen_secenek": "chosen_option",
    "gecersiz_kilinma_sebebi": "invalidated_reason",
}

DEFAULT_FILENAME = "decision_units.json"
#: Filenames the archive used, tried in order when a directory is given.
CANDIDATE_FILENAMES = (
    DEFAULT_FILENAME,
    "karar_birimleri.json",
)

# --------------------------------------------------------------------------- #
# The THIRD dialect, which nothing loads
# --------------------------------------------------------------------------- #
#: ``skills/decompose-pcb-decisions`` documents its artifacts against a JSON
#: Schema that is a third decision dialect: ``decision-units/v1``, not
#: ``pcb-decision-units/v1``. No loader in ``tools/`` reads it, so a skill can
#: emit a document that satisfies its own contract and that this module then
#: refuses or silently misreads.
#:
#: It is recorded here rather than deleted, because deleting it would destroy
#: design intent this module does not yet implement (evidence LEVEL and LOOP
#: are genuinely richer than a flat id list). ``test_decisions.py`` recomputes
#: this divergence from the schema file and fails if either side moves, so the
#: gap stays visible until somebody reconciles it on purpose.
SKILL_SCHEMA_RELPATH = (
    "skills/decompose-pcb-decisions/references/decision_units.schema.json")

SKILL_SCHEMA_VERSION = "decision-units/v1"

#: Document-level statuses that schema allows and this module cannot parse.
#: A ``TBD`` or ``CONFIRM`` document would raise ``DecisionError`` here.
SKILL_DIALECT_EXTRA_STATUSES = frozenset({"TBD", "CONFIRM"})

#: Skill-dialect field -> nearest canonical field. "Nearest" is doing real
#: work: none of these is a rename, each loses or gains information.
SKILL_DIALECT_FIELD_MAP = {
    # array of units, at the document root
    "decision_units": "decisions",
    # {artifact, schema} object -> a bare path string (schema binding lost)
    "output_contract": "output_artifact",
    # SINGULAR string -> plural list (arity mismatch, silently truncating)
    "invalidation_trigger": "invalidation_triggers",
    # {evidence_ids, level, loop, measurement_id} -> a flat id list
    # (assurance level and verification loop lost)
    "accepted_evidence": "evidence_ids",
}

#: The only evidence status that can close a decision. ``scripts/
#: context_pack.py`` validates the full vocabulary
#: (canonical/stale/rejected); this module only needs to know which one is
#: good enough to close on.
EVIDENCE_CANONICAL = "canonical"

# --------------------------------------------------------------------------- #
# Closure reason codes
# --------------------------------------------------------------------------- #
#: A CLOSED vocabulary. ``decision_gate()``, ``scripts/promote.py`` and
#: ``scripts/context_pack.py`` all map from these codes, so a new closure
#: condition must be added here rather than invented at a call site - that
#: divergence is precisely what this module exists to prevent.
CLOSURE_NOT_ACCEPTED = "NOT_ACCEPTED"
CLOSURE_REQUIRED_EVIDENCE_UNDECLARED = "REQUIRED_EVIDENCE_UNDECLARED"
CLOSURE_EVIDENCE_IDS_EMPTY = "EVIDENCE_IDS_EMPTY"
CLOSURE_EVIDENCE_NOT_IN_CATALOGUE = "EVIDENCE_NOT_IN_CATALOGUE"
CLOSURE_EVIDENCE_NOT_CANONICAL = "EVIDENCE_NOT_CANONICAL"
CLOSURE_INVALIDATION_TRIGGER_UNDECLARED = "INVALIDATION_TRIGGER_UNDECLARED"
CLOSURE_OUTPUT_ARTIFACT_UNDECLARED = "OUTPUT_ARTIFACT_UNDECLARED"

#: Emission order, which is also the order a caller should read them in:
#: status first, then what the decision promised, then what it delivered.
CLOSURE_REASONS = (
    CLOSURE_NOT_ACCEPTED,
    CLOSURE_REQUIRED_EVIDENCE_UNDECLARED,
    CLOSURE_EVIDENCE_IDS_EMPTY,
    CLOSURE_EVIDENCE_NOT_IN_CATALOGUE,
    CLOSURE_EVIDENCE_NOT_CANONICAL,
    CLOSURE_INVALIDATION_TRIGGER_UNDECLARED,
    CLOSURE_OUTPUT_ARTIFACT_UNDECLARED,
)

#: Reason codes that mean "this decision's evidence does not hold up", as
#: opposed to "the decision never got as far as being accepted".
CLOSURE_EVIDENCE_REASONS = frozenset({
    CLOSURE_REQUIRED_EVIDENCE_UNDECLARED,
    CLOSURE_EVIDENCE_IDS_EMPTY,
    CLOSURE_EVIDENCE_NOT_IN_CATALOGUE,
    CLOSURE_EVIDENCE_NOT_CANONICAL,
})

#: Conditions the decision report's section 6.6 lists for ``closed`` that this
#: predicate DOES NOT evaluate. They are named rather than omitted: a caller
#: that reads ``closed is True`` as full 6.6 closure would be wrong, and
#: silence is how that mistake gets made. Each value says why the condition is
#: not computable from a decision document alone.
UNCHECKED_CLOSURE_CONDITIONS = {
    "required_evidence_matches_evidence_ids":
        "required_evidence is free prose and evidence_ids are catalogue keys; "
        "nothing in the schema links a given sentence to a given id, so this "
        "predicate can only check that BOTH were supplied",
    "implementation_check_pass":
        "no implementation check is declared per decision; the promotion "
        "gates (DRC/ERC/contract) are board-wide, not decision-scoped",
    "output_artifact_exists_on_disk":
        "output_artifact is repository-relative and this module is given no "
        "root; scripts/context_pack.py performs the confinement check that "
        "genuinely belongs to a manifest",
    "generated_docs_current":
        "no generated-view registry exists yet; nothing records which "
        "document was derived from which decision",
    "invalidation_premises_still_true":
        "invalidation_triggers are documentation - nothing monitors them "
        "(see this module's header, point 2)",
}


class DecisionError(ValueError):
    """The decision document is not usable as a graph."""


class DependencyCycleError(DecisionError):
    """The decision dependency graph is not a DAG."""


class UnknownDependencyError(DecisionError):
    """A decision depends on an id that does not exist."""


def _as_list(value: Any) -> list[str]:
    """Normalise a legacy free-text field into the canonical string list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise DecisionError(f"expected string or list, got {type(value).__name__}")


@dataclass
class DecisionUnit:
    """One design decision as a queryable state-machine record."""

    decision_id: str
    question: str
    owner_skill: str = ""
    status: str = READY
    depends_on: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    invalidation_triggers: list[str] = field(default_factory=list)
    output_artifact: str = ""
    chosen_option: str | None = None
    invalidated_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, str) or not self.decision_id.strip():
            raise DecisionError("decision_id must be non-empty text")
        if self.status not in STATUSES:
            raise DecisionError(
                f"{self.decision_id}: unknown status {self.status!r}; "
                f"expected one of {sorted(STATUSES)}")

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    @staticmethod
    def from_dict(raw: dict) -> "DecisionUnit":
        if not isinstance(raw, dict):
            raise DecisionError("decision record must be an object")
        data: dict[str, Any] = {}
        for key, value in raw.items():
            data[LEGACY_FIELDS.get(key, key)] = value
        status = data.get("status", READY)
        if isinstance(status, str):
            status = LEGACY_STATUSES.get(status, status)
        data["status"] = status
        for key in ("depends_on", "constraints", "options",
                    "required_evidence", "evidence_ids",
                    "invalidation_triggers"):
            if key in data:
                data[key] = _as_list(data[key])
        known = set(DecisionUnit.__dataclass_fields__)
        return DecisionUnit(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        out = {
            "decision_id": self.decision_id,
            "question": self.question,
            "owner_skill": self.owner_skill,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "constraints": list(self.constraints),
            "options": list(self.options),
            "required_evidence": list(self.required_evidence),
            "evidence_ids": list(self.evidence_ids),
            "invalidation_triggers": list(self.invalidation_triggers),
            "output_artifact": self.output_artifact,
        }
        if self.chosen_option is not None:
            out["chosen_option"] = self.chosen_option
        if self.invalidated_reason is not None:
            out["invalidated_reason"] = self.invalidated_reason
        return out


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def decisions_path(project_dir: Path | str) -> Path | None:
    """First existing decision document under ``project_dir``.

    Both the canonical name and the archive's ``DOCS/karar_birimleri.json``
    location are probed. Returns None when the project has never recorded a
    decision - which is a legitimate state, not an error.
    """
    base = Path(project_dir)
    for parent in (base, base / "DOCS"):
        for name in CANDIDATE_FILENAMES:
            candidate = parent / name
            if candidate.is_file():
                return candidate
    return None


def parse_decisions(document: Any) -> list[DecisionUnit]:
    """Accept ``{"decisions": [...]}``, ``{"kararlar": [...]}`` or a bare list."""
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict):
        records = document.get("decisions")
        if records is None:
            records = document.get("kararlar")
        if records is None:
            raise DecisionError(
                "decision document has neither 'decisions' nor 'kararlar'")
    else:
        raise DecisionError("decision document root must be an object or list")
    if not isinstance(records, list):
        raise DecisionError("'decisions' must be a list")
    units = [DecisionUnit.from_dict(item) for item in records]
    seen: set[str] = set()
    for unit in units:
        if unit.decision_id in seen:
            raise DecisionError(f"duplicate decision_id: {unit.decision_id}")
        seen.add(unit.decision_id)
    return units


def parse_evidence(document: Any) -> dict[str, dict]:
    """The document's top-level ``evidence[]`` catalogue, keyed by id.

    A decision's ``evidence_ids`` are keys INTO this catalogue; without it the
    ids are unresolvable strings and nothing can be closed on them. A bare
    list of decisions therefore has no catalogue at all, which is reported as
    ``EVIDENCE_NOT_IN_CATALOGUE`` rather than waved through.
    """
    if not isinstance(document, dict):
        return {}
    records = document.get("evidence")
    if records is None:
        return {}
    if not isinstance(records, list):
        raise DecisionError("'evidence' must be a list")
    catalogue: dict[str, dict] = {}
    for item in records:
        if not isinstance(item, dict):
            raise DecisionError("evidence record must be an object")
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise DecisionError("evidence record needs a non-empty evidence_id")
        if evidence_id in catalogue:
            raise DecisionError(f"duplicate evidence_id: {evidence_id}")
        catalogue[evidence_id] = item
    return catalogue


def parse_decision_document(document: Any) -> tuple[list[DecisionUnit],
                                                    dict[str, dict]]:
    """Decisions AND their evidence catalogue, which travel together.

    ``parse_decisions`` alone drops the catalogue, and a decision whose
    catalogue was dropped can never be closed. Prefer this entry point
    anywhere the closure predicate will be consulted.
    """
    return parse_decisions(document), parse_evidence(document)


def load_decisions(path: Path | str) -> list[DecisionUnit]:
    return parse_decisions(json.loads(Path(path).read_text(encoding="utf-8")))


def load_decision_document(path: Path | str) -> tuple[list[DecisionUnit],
                                                      dict[str, dict]]:
    return parse_decision_document(
        json.loads(Path(path).read_text(encoding="utf-8")))


def load_project_decisions(project_dir: Path | str) -> list[DecisionUnit]:
    """Decisions for a project, or ``[]`` when none were ever recorded."""
    path = decisions_path(project_dir)
    return load_decisions(path) if path else []


def dump_decisions(units: Iterable[DecisionUnit],
                   evidence: Iterable[dict] | dict[str, dict] | None = None
                   ) -> dict:
    """The canonical document. Always ``pcb-decision-units/v1``.

    ``evidence`` is round-tripped rather than blanked: writing ``[]`` back
    over a real catalogue would silently un-close every decision in the
    document, which is the exact drift this module is meant to stop.
    """
    if evidence is None:
        records: list[dict] = []
    elif isinstance(evidence, dict):
        records = [evidence[key] for key in sorted(evidence)]
    else:
        records = list(evidence)
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence": records,
        "decisions": [u.to_dict() for u in units],
    }


def save_decisions(path: Path | str, units: Iterable[DecisionUnit],
                   evidence: Iterable[dict] | dict[str, dict] | None = None
                   ) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dump_decisions(units, evidence), indent=2,
                   ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8")


def upsert(units: list[DecisionUnit], unit: DecisionUnit) -> list[DecisionUnit]:
    """Replace by ``decision_id`` or append. Mutates and returns ``units``."""
    for index, existing in enumerate(units):
        if existing.decision_id == unit.decision_id:
            units[index] = unit
            return units
    units.append(unit)
    return units


# --------------------------------------------------------------------------- #
# Dependency graph
# --------------------------------------------------------------------------- #
def topological_order(units: Iterable[DecisionUnit]) -> list[str]:
    """Dependencies first. Raises on unknown ids or cycles.

    The walk starts from sorted ids so the output is deterministic; an
    unstable order would make any receipt built from it unreproducible.
    """
    units = list(units)
    ids = {u.decision_id for u in units}
    edges = {u.decision_id: list(u.depends_on) for u in units}

    for node, parents in edges.items():
        for parent in parents:
            if parent not in ids:
                raise UnknownDependencyError(
                    f"{node}: unknown dependency {parent!r}")

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(ids, WHITE)
    order: list[str] = []
    stack: list[str] = []

    def visit(node: str) -> None:
        colour[node] = GREY
        stack.append(node)
        for parent in edges[node]:
            if colour[parent] == GREY:
                start = stack.index(parent)
                cycle = " -> ".join(stack[start:] + [parent])
                raise DependencyCycleError(f"decision dependency cycle: {cycle}")
            if colour[parent] == WHITE:
                visit(parent)
        stack.pop()
        colour[node] = BLACK
        order.append(node)

    for node in sorted(ids):
        if colour[node] == WHITE:
            visit(node)
    return order


def dependents_of(units: Iterable[DecisionUnit], decision_id: str) -> list[str]:
    """Every decision that depends on ``decision_id``, directly or not."""
    units = list(units)
    reverse: dict[str, list[str]] = {}
    for unit in units:
        for parent in unit.depends_on:
            reverse.setdefault(parent, []).append(unit.decision_id)

    seen: list[str] = []
    queue = list(reverse.get(decision_id, []))
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.append(node)
        queue.extend(reverse.get(node, []))
    return seen


def invalidate(units: list[DecisionUnit], decision_id: str,
               reason: str) -> list[str]:
    """Invalidate a decision and reopen everything that rests on it.

    Mutates ``units`` in place and returns the ids that were reopened. An
    already-invalidated dependent keeps its own recorded reason.
    """
    by_id = {u.decision_id: u for u in units}
    if decision_id not in by_id:
        raise KeyError(f"unknown decision_id: {decision_id}")
    if not isinstance(reason, str) or not reason.strip():
        raise DecisionError("invalidation reason must be non-empty text")

    target = by_id[decision_id]
    target.status = BLOCKED
    target.invalidated_reason = reason

    reopened: list[str] = []
    for node in dependents_of(units, decision_id):
        unit = by_id[node]
        if unit.status == BLOCKED:
            continue
        unit.status = READY
        unit.invalidated_reason = (
            f"premise {decision_id!r} was invalidated: {reason}")
        reopened.append(node)
    return reopened


# --------------------------------------------------------------------------- #
# Closure predicate
# --------------------------------------------------------------------------- #
def closure_state(decision: "DecisionUnit | dict",
                  evidence: dict[str, dict] | None = None) -> dict:
    """Is this decision CLOSED, and if not, exactly why?

    THE single implementation of closure semantics. ``scripts/promote.py``
    (through ``decision_gate``) and ``scripts/context_pack.py`` both call this
    function; neither is allowed a private copy, because the two previously
    disagreed - the gate accepted evidence-free decisions while the pack
    rejected them, so the same document passed one tool and failed the other.

    ``decision`` may be a :class:`DecisionUnit` or the raw canonical mapping,
    so a validator that has not built units yet can still ask the question.

    ``evidence`` is the document's ``evidence[]`` catalogue keyed by id (see
    :func:`parse_evidence`). Omitting it is NOT neutral: ids that cannot be
    resolved are reported as missing, never assumed good.

    Closure is deliberately NARROWER than the decision report's section 6.6
    wish list - see :data:`UNCHECKED_CLOSURE_CONDITIONS` for what is left
    unevaluated and why. ``closed`` therefore means "closed as far as a
    decision document can prove", never "nothing more to check".
    """
    unit = (decision if isinstance(decision, DecisionUnit)
            else DecisionUnit.from_dict(decision))
    catalogue = evidence or {}
    reasons: list[str] = []

    if unit.status != ACCEPTED:
        reasons.append(CLOSURE_NOT_ACCEPTED)
    if not unit.required_evidence:
        reasons.append(CLOSURE_REQUIRED_EVIDENCE_UNDECLARED)
    if not unit.evidence_ids:
        reasons.append(CLOSURE_EVIDENCE_IDS_EMPTY)

    missing = sorted(set(unit.evidence_ids) - set(catalogue))
    if missing:
        reasons.append(CLOSURE_EVIDENCE_NOT_IN_CATALOGUE)
    noncanonical = sorted(
        eid for eid in set(unit.evidence_ids) & set(catalogue)
        if catalogue[eid].get("status") != EVIDENCE_CANONICAL)
    if noncanonical:
        reasons.append(CLOSURE_EVIDENCE_NOT_CANONICAL)

    if not unit.invalidation_triggers:
        reasons.append(CLOSURE_INVALIDATION_TRIGGER_UNDECLARED)
    if not unit.output_artifact.strip():
        reasons.append(CLOSURE_OUTPUT_ARTIFACT_UNDECLARED)

    reasons.sort(key=CLOSURE_REASONS.index)
    return {
        "decision_id": unit.decision_id,
        "accepted": unit.status == ACCEPTED,
        "closed": not reasons,
        "reasons": reasons,
        "missing_evidence_ids": missing,
        "noncanonical_evidence_ids": noncanonical,
    }


def closure_states(units: Iterable[DecisionUnit],
                   evidence: dict[str, dict] | None = None) -> list[dict]:
    """:func:`closure_state` for every unit, in the order given."""
    return [closure_state(u, evidence) for u in units]


def decision_debt(closures: Iterable[dict]) -> dict:
    """The decision-debt COUNT VECTOR of report section 6.9.

    Deliberately a vector and not a score. Section 6.9 forbids collapsing
    these into a weighted ``decision_debt_score``: the weights are governance
    policy and require an organisational decision, so inventing them here
    would launder a policy choice as a measurement.

    Fields section 6.9 asks for that are NOT computable from a decision
    document are listed in ``not_computable`` with the reason, rather than
    reported as zero - a zero would read as "no debt".
    """
    closures = list(closures)
    accepted_open = [c for c in closures if c["accepted"] and not c["closed"]]
    return {
        "open": sum(1 for c in closures if not c["accepted"]),
        "accepted": sum(1 for c in closures if c["accepted"]),
        "closed": sum(1 for c in closures if c["closed"]),
        "accepted_but_not_closed": len(accepted_open),
        "accepted_evidence_missing": sum(
            1 for c in accepted_open
            if CLOSURE_EVIDENCE_REASONS.intersection(c["reasons"])),
        "accepted_output_artifact_missing": sum(
            1 for c in accepted_open
            if CLOSURE_OUTPUT_ARTIFACT_UNDECLARED in c["reasons"]),
        "accepted_invalidation_trigger_missing": sum(
            1 for c in accepted_open
            if CLOSURE_INVALIDATION_TRIGGER_UNDECLARED in c["reasons"]),
        "by_reason": {
            code: sum(1 for c in accepted_open if code in c["reasons"])
            for code in CLOSURE_REASONS if code != CLOSURE_NOT_ACCEPTED
        },
        "not_computable": {
            "oldest_open_decision_age_days":
                "pcb-decision-units/v1 records no timestamp on any decision",
            "phase_blocking_open":
                "no decision field names the phase or milestone it blocks",
            "unasserted_rule_rows":
                "verification-matrix rows live outside the decision document; "
                "the coverage layer owns that count",
            "invalidation_trigger_fired":
                "invalidation_triggers are never monitored (module header, "
                "point 2), so a changed premise is undetectable here",
            "status_drift_across_documents":
                "a gate sees one document; cross-document drift needs a "
                "registry of every place a decision status is written",
            "reused_decision_ids":
                "ids are unique WITHIN a document by construction and no "
                "retirement registry exists to detect reuse ACROSS time",
            "thresholds_blocked_on_provenance":
                "source-bound thresholds are not decision units; that count "
                "belongs to the threshold promotion gate",
        },
    }


# --------------------------------------------------------------------------- #
# Promotion gate
# --------------------------------------------------------------------------- #
def unaccepted(units: Iterable[DecisionUnit]) -> list[DecisionUnit]:
    """Every decision that is not ``accepted``."""
    return [u for u in units if u.status != ACCEPTED]


def not_closed(units: Iterable[DecisionUnit],
               evidence: dict[str, dict] | None = None) -> list[DecisionUnit]:
    """Every decision that is not CLOSED (a superset of ``unaccepted``)."""
    return [u for u in units if not closure_state(u, evidence)["closed"]]


def decision_gate(units: Iterable[DecisionUnit],
                  evidence: dict[str, dict] | None = None) -> dict:
    """The promotion gate's view of the decision graph.

    ``graph_valid`` is False when the graph is not a DAG or names an unknown
    dependency. That is a HARDER failure than an open decision: a broken
    graph means the "all decisions accepted" answer cannot be trusted at all,
    so the gate must not report a count as if it were meaningful.

    ``passed`` requires CLOSURE, not merely acceptance. Before this, the gate
    read ``status`` alone, so a decision could be hand-marked ``accepted``
    with no evidence at all and the gate would report a clean promotion -
    exactly the two-way drift between "decided", "evidenced" and "implemented"
    that the decision report's section 6.5 describes. ``accepted_but_not_closed``
    is reported separately from ``unaccepted`` because the two need different
    human responses: one needs a decision, the other needs the receipts for a
    decision already made.
    """
    units = list(units)
    open_units = unaccepted(units)
    closures = closure_states(units, evidence)
    accepted_open = [c for c in closures if c["accepted"] and not c["closed"]]
    report = {
        "total": len(units),
        "accepted": sum(1 for u in units if u.status == ACCEPTED),
        "unaccepted": len(open_units),
        "unaccepted_ids": [u.decision_id for u in open_units],
        "closed": sum(1 for c in closures if c["closed"]),
        "closed_ids": [c["decision_id"] for c in closures if c["closed"]],
        "accepted_but_not_closed": len(accepted_open),
        "accepted_but_not_closed_ids": [c["decision_id"] for c in accepted_open],
        "closure_reasons": {
            c["decision_id"]: list(c["reasons"])
            for c in closures if not c["closed"]
        },
        "unchecked_closure_conditions": dict(UNCHECKED_CLOSURE_CONDITIONS),
        "decision_debt": decision_debt(closures),
        "by_status": {
            status: sum(1 for u in units if u.status == status)
            for status in sorted(STATUSES)
        },
        "graph_valid": True,
        "graph_error": "",
    }
    try:
        report["order"] = topological_order(units)
    except DecisionError as exc:
        report["order"] = []
        report["graph_valid"] = False
        report["graph_error"] = str(exc)
    report["passed"] = (report["graph_valid"] and not open_units
                        and not accepted_open)
    return report


# --------------------------------------------------------------------------- #
# Critical-pin datasheet-confirmation gate (Tur 3 addition, ported from the
# archive's ``karar_birimleri.py`` critical-pin section)
# --------------------------------------------------------------------------- #
# Detects a known critical-pin category (boot-strap, mode-select,
# reserved/NDA) from a NET NAME and auto-generates a decision unit requiring
# datasheet/errata confirmation before it can reach ACCEPTED. This does not
# read a datasheet itself - it only makes sure the accept/dependency
# machinery above cannot be silently bypassed for these pins.

# Substring match, NOT regex, on purpose: a short/specific pattern close to a
# word boundary reduces surprise matches (e.g. a net that happens to CONTAIN
# "BOOT" as a substring of something unrelated). ORDER MATTERS: a longer,
# more specific pattern (e.g. "SYSBOOT") is tried BEFORE a shorter one it
# also contains ("BOOT") - otherwise a net like "SYSBOOT0" would always fall
# into the general "BOOT" category and the more specific "SYSBOOT" category
# would never be reached.
CRITICAL_PIN_PATTERNS: tuple = ("SYSBOOT", "BOOT", "MODE_SEL", "NC_RESERVED", "NDA")


def critical_pin_category(net_name: str,
                          patterns: tuple = CRITICAL_PIN_PATTERNS) -> str | None:
    """The first matching critical-pin pattern in ``net_name``, or None."""
    name = net_name.strip().upper()
    for pattern in patterns:
        if pattern in name:
            return pattern
    return None


def build_critical_pin_decision(category: str, component: str,
                                reason: str) -> DecisionUnit:
    """Build (does not save) a READY decision requiring the given
    component's ``category`` pin(s) be confirmed against the manufacturer's
    CURRENT datasheet/errata.

    WHAT THE RECORD DOES AND DOES NOT GUARANTEE. This function only produces a
    record in the right shape. Its ``required_evidence`` prose is fixed and
    specific so a human reviewer knows what would settle the question, but
    prose alone stops nothing: ``status`` is writable, so somebody can set this
    decision to ``accepted`` without ever opening the datasheet.

    What DOES stop that is :func:`closure_state`, which the promotion gate
    consults: an accepted decision with no ``evidence_ids``, ids that resolve
    to nothing in the document's ``evidence[]`` catalogue, non-canonical
    evidence, or no ``output_artifact`` is ``accepted_but_not_closed`` and the
    gate does not pass. This record leaves ``evidence_ids`` and
    ``output_artifact`` empty precisely so that acceptance without receipts
    fails loudly.

    (An earlier version of this docstring claimed ``required_evidence`` alone
    made ACCEPTED unreachable "with no extra gate code". That was false: the
    gate read ``status`` and nothing else. The gate code now exists.)
    """
    decision_id = f"critical-pin-confirm-{component}-{category}".lower().replace("_", "-")
    return DecisionUnit(
        decision_id=decision_id,
        question=(
            f"Have the '{category}' category critical pin(s) on {component} "
            "been confirmed against the manufacturer's CURRENT datasheet/"
            "errata?"),
        owner_skill=".claude/skills/schematic-design",
        constraints=[reason],
        required_evidence=[
            "Pin-level / resistor value confirmed from the manufacturer's "
            "CURRENT datasheet/errata (e.g. pull-up/pull-down requirement, "
            "strap level, whether an NC pin is genuinely unconnected or "
            "carries an NDA-gated function)."],
        status=READY,
        invalidation_triggers=[f"if {component}'s part number/revision changes"],
    )


def detect_and_save_critical_pin_decisions(
        project_dir: Path | str, component: str,
        net_names: Iterable[str]) -> list[DecisionUnit]:
    """Scan ``net_names``; for every unique critical-pin category found (that
    the project does not already have a record for), build and save a
    decision unit. Never overwrites an existing id - a decision already
    pulled to ``accepted`` is not silently dropped back to ``ready`` (that is
    ``invalidate()``'s job; this function does not touch it).
    """
    existing_path = decisions_path(project_dir)
    if existing_path:
        units, evidence = load_decision_document(existing_path)
    else:
        units, evidence = [], {}
    existing_ids = {u.decision_id for u in units}
    seen_categories: list[str] = []
    added = False
    for net in net_names:
        category = critical_pin_category(net)
        if category is None or category in seen_categories:
            continue
        seen_categories.append(category)
        unit = build_critical_pin_decision(
            category, component, f"Net {net!r} matched a critical-pin pattern.")
        if unit.decision_id in existing_ids:
            continue
        upsert(units, unit)
        existing_ids.add(unit.decision_id)
        added = True

    if added or existing_path:
        target = existing_path or (Path(project_dir) / DEFAULT_FILENAME)
        save_decisions(target, units, evidence)
    return units
