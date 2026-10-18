#!/usr/bin/env python3
"""Validate hash-bound PCB context manifests and worker claim outputs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.contract import (  # noqa: E402
    ContractError,
    require_dict,
    require_int,
    require_list,
    require_string_list,
    require_text,
)
from common.decisions import (  # noqa: E402
    CLOSURE_EVIDENCE_IDS_EMPTY,
    CLOSURE_EVIDENCE_NOT_CANONICAL,
    CLOSURE_EVIDENCE_NOT_IN_CATALOGUE,
    CLOSURE_INVALIDATION_TRIGGER_UNDECLARED,
    CLOSURE_OUTPUT_ARTIFACT_UNDECLARED,
    CLOSURE_REQUIRED_EVIDENCE_UNDECLARED,
    closure_state,
)
from common.hashing import sha256_file as sha256  # noqa: E402
from common.hashing import valid_digest  # noqa: E402
from common.paths import PathConfinementError  # noqa: E402
from common.paths import safe_path as _safe_path  # noqa: E402
from common.report_io import diag, emit, write_report  # noqa: E402

SCHEMA = "pcb-context-pack/v1"
INTENT_SCHEMA = "pcb-design-intent/v1"
DECISION_SCHEMA = "pcb-decision-units/v1"
TOOL_SCHEMA = "pcb-tool-registry/v1"
CLAIM_FIELDS = {"claim_id", "statement", "status", "evidence_ids", "confidence", "scope"}
CORE_ROLES = {"design_intent", "decision_unit", "tool_registry"}
DECISION_STATUSES = {"blocked", "ready", "proposed", "accepted", "rejected"}
CLAIM_STATUSES = {"accepted", "rejected", "unverified", "conflicted", "stale"}
EVIDENCE_KINDS = {"artifact", "measurement", "official_source", "human_signoff"}

#: Closure reason code -> the message this validator has always raised for it.
#: The CHECKS now live in ``common/decisions.closure_state`` so the promotion
#: gate and this validator cannot disagree about what "accepted" is worth; only
#: the wording is local. Anything in ``CLOSURE_REASONS`` absent from this map
#: falls back to the raw code, so a new closure condition surfaces as a
#: manifest error rather than being silently dropped.
CLOSURE_MESSAGES = {
    CLOSURE_REQUIRED_EVIDENCE_UNDECLARED:
        "accepted decision declares no required_evidence",
    CLOSURE_EVIDENCE_IDS_EMPTY:
        "accepted decision lacks evidence or invalidation trigger",
    CLOSURE_INVALIDATION_TRIGGER_UNDECLARED:
        "accepted decision lacks evidence or invalidation trigger",
    CLOSURE_EVIDENCE_NOT_IN_CATALOGUE:
        "accepted decision references missing evidence",
    CLOSURE_EVIDENCE_NOT_CANONICAL:
        "accepted decision uses noncanonical evidence",
    CLOSURE_OUTPUT_ARTIFACT_UNDECLARED:
        "accepted decision declares no output artifact",
}


def safe_path(root: Path, relative: str, field: str = "path") -> Path:
    """Confine ``relative`` under ``root``.

    Delegates to ``common.paths.safe_path``, which additionally performs the
    lexical containment check this copy lacked (a symlink under the root
    could previously resolve outside it). ``PathConfinementError`` is
    re-raised as ``ContractError`` so callers and tests keep the same
    exception type and message.
    """
    require_text(relative, field)
    try:
        return _safe_path(root, relative, field)
    except PathConfinementError as exc:
        raise ContractError(str(exc)) from exc


def load_json(path: Path, name: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid {name} JSON: {path}: {exc}") from exc


def validate_evidence_item(item, name: str, root: Path, errors: list[str]):
    item = require_dict(item, name)
    evidence_id = require_text(item.get("evidence_id"), f"{name}.evidence_id")
    kind = require_text(item.get("kind"), f"evidence {evidence_id}.kind")
    if kind not in EVIDENCE_KINDS:
        raise ContractError(f"invalid evidence kind: {evidence_id}")
    source = require_text(item.get("source"), f"evidence {evidence_id}.source")
    if item.get("status") not in {"canonical", "stale", "rejected"}:
        raise ContractError(f"invalid evidence status: {evidence_id}")
    if kind in {"artifact", "measurement"}:
        evidence_path = safe_path(root, require_text(item.get("path"), f"evidence {evidence_id}.path"), f"evidence {evidence_id}.path")
        if not valid_digest(item.get("sha256")):
            raise ContractError(f"evidence {evidence_id}.sha256 is invalid")
        if not evidence_path.is_file() or sha256(evidence_path) != item["sha256"].lower():
            errors.append(f"STALE_EVIDENCE:{evidence_id}")
    elif kind == "official_source":
        if not source.startswith(("https://", "http://")):
            errors.append(f"INVALID_OFFICIAL_SOURCE:{evidence_id}")
        require_text(item.get("locator"), f"evidence {evidence_id}.locator")
        require_text(item.get("retrieved_at"), f"evidence {evidence_id}.retrieved_at")
        if not valid_digest(item.get("content_sha256")):
            raise ContractError(f"evidence {evidence_id}.content_sha256 is invalid")
    elif kind == "human_signoff":
        require_text(item.get("signed_by"), f"evidence {evidence_id}.signed_by")
        require_text(item.get("signed_at"), f"evidence {evidence_id}.signed_at")
        require_text(item.get("scope"), f"evidence {evidence_id}.scope")
    return evidence_id, item


def validate_intent(intent):
    intent = require_dict(intent, "design_intent")
    if intent.get("schema_version") != INTENT_SCHEMA:
        raise ContractError(f"design_intent.schema_version must be {INTENT_SCHEMA}")
    require_string_list(intent.get("goals"), "design_intent.goals")
    require_string_list(intent.get("non_goals"), "design_intent.non_goals")
    constraints = {}
    for i, item in enumerate(require_list(intent.get("constraints"), "design_intent.constraints")):
        item = require_dict(item, f"design_intent.constraints[{i}]")
        for key in ("id", "statement", "source"):
            require_text(item.get(key), f"design_intent.constraints[{i}].{key}")
        if item["id"] in constraints:
            raise ContractError(f"duplicate intent constraint: {item['id']}")
        constraints[item["id"]] = item
    questions = {}
    for i, item in enumerate(require_list(intent.get("open_questions", []), "design_intent.open_questions", nonempty=False)):
        item = require_dict(item, f"design_intent.open_questions[{i}]")
        for key in ("id", "question", "owner"):
            require_text(item.get(key), f"design_intent.open_questions[{i}].{key}")
        if not isinstance(item.get("blocking"), bool):
            raise ContractError(f"design_intent.open_questions[{i}].blocking must be boolean")
        if item["id"] in questions:
            raise ContractError(f"duplicate intent question: {item['id']}")
        questions[item["id"]] = item
    return constraints, questions


def closure_message(closure: dict) -> list[str]:
    """Human phrases for a not-closed decision, in ``CLOSURE_REASONS`` order.

    Deduplicated because two codes (empty evidence ids, undeclared
    invalidation trigger) deliberately share one sentence: from a manifest's
    point of view they are the same failure to supply receipts.
    """
    phrases: list[str] = []
    for code in closure["reasons"]:
        phrase = CLOSURE_MESSAGES.get(code, code)
        if code == CLOSURE_EVIDENCE_NOT_IN_CATALOGUE and closure["missing_evidence_ids"]:
            phrase += ": " + ",".join(closure["missing_evidence_ids"])
        elif code == CLOSURE_EVIDENCE_NOT_CANONICAL and closure["noncanonical_evidence_ids"]:
            phrase += ": " + ",".join(closure["noncanonical_evidence_ids"])
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases


def validate_decisions(document, root: Path):
    document = require_dict(document, "decision_unit")
    if document.get("schema_version") != DECISION_SCHEMA:
        raise ContractError(f"decision_unit.schema_version must be {DECISION_SCHEMA}")
    evidence = {}
    evidence_errors = []
    for i, item in enumerate(require_list(document.get("evidence", []), "decision_unit.evidence", nonempty=False)):
        evidence_id, item = validate_evidence_item(item, f"decision_unit.evidence[{i}]", root, evidence_errors)
        if evidence_id in evidence:
            raise ContractError(f"duplicate decision evidence id: {evidence_id}")
        evidence[evidence_id] = item
    if evidence_errors:
        raise ContractError("invalid decision evidence: " + ",".join(evidence_errors))
    indexed, outputs = {}, set()
    for i, decision in enumerate(require_list(document.get("decisions"), "decision_unit.decisions")):
        decision = require_dict(decision, f"decision_unit.decisions[{i}]")
        decision_id = require_text(decision.get("decision_id"), f"decision_unit.decisions[{i}].decision_id")
        if decision_id in indexed:
            raise ContractError(f"duplicate decision_id: {decision_id}")
        for key in ("question", "owner_skill", "output_artifact"):
            require_text(decision.get(key), f"decision {decision_id}.{key}")
        if decision.get("status") not in DECISION_STATUSES:
            raise ContractError(f"invalid decision status: {decision_id}")
        for key in ("depends_on", "constraints", "options", "required_evidence", "invalidation_triggers"):
            require_string_list(decision.get(key), f"decision {decision_id}.{key}", nonempty=key in {"constraints", "options", "required_evidence"})
        safe_path(root, decision["output_artifact"], f"decision {decision_id}.output_artifact")
        if decision["output_artifact"] in outputs:
            raise ContractError(f"duplicate decision output: {decision['output_artifact']}")
        outputs.add(decision["output_artifact"])
        require_string_list(decision.get("evidence_ids", []), f"decision {decision_id}.evidence_ids", nonempty=False)
        # ONE closure predicate, shared with the promotion gate. This validator
        # used to re-implement the checks inline and drifted stricter than the
        # gate, so the same document passed promotion and failed the pack.
        closure = closure_state(decision, evidence)
        if closure["accepted"] and not closure["closed"]:
            raise ContractError(f"accepted decision is not closed: {decision_id}: "
                                + "; ".join(closure_message(closure)))
        indexed[decision_id] = decision
    for decision_id, decision in indexed.items():
        missing = sorted(set(decision["depends_on"]) - set(indexed))
        if missing:
            raise ContractError(f"decision {decision_id} has missing dependencies: {','.join(missing)}")
    visiting, visited = set(), set()
    def visit(decision_id):
        if decision_id in visiting:
            raise ContractError(f"decision dependency cycle at {decision_id}")
        if decision_id in visited:
            return
        visiting.add(decision_id)
        for parent in indexed[decision_id]["depends_on"]:
            visit(parent)
        visiting.remove(decision_id)
        visited.add(decision_id)
    for decision_id in indexed:
        visit(decision_id)
    return indexed


def validate_tool_registry(document, root: Path, errors):
    document = require_dict(document, "tool_registry")
    if document.get("schema_version") != TOOL_SCHEMA:
        raise ContractError(f"tool_registry.schema_version must be {TOOL_SCHEMA}")
    indexed = {}
    for i, tool in enumerate(require_list(document.get("tools"), "tool_registry.tools")):
        tool = require_dict(tool, f"tool_registry.tools[{i}]")
        for key in ("name", "kind", "locator", "version"):
            require_text(tool.get(key), f"tool_registry.tools[{i}].{key}")
        if tool["kind"] not in {"builtin", "local", "system", "mcp", "human"}:
            raise ContractError(f"invalid tool kind: {tool['name']}")
        require_string_list(tool.get("modes"), f"tool {tool['name']}.modes")
        require_string_list(tool.get("preconditions", []), f"tool {tool['name']}.preconditions", nonempty=False)
        if not isinstance(tool.get("available"), bool):
            raise ContractError(f"tool {tool['name']}.available must be boolean")
        if tool["name"] in indexed:
            raise ContractError(f"duplicate tool registry name: {tool['name']}")
        if tool["kind"] == "local":
            path = safe_path(root, tool["locator"], f"tool {tool['name']}.locator")
            if not valid_digest(tool.get("sha256")):
                raise ContractError(f"tool {tool['name']}.sha256 is invalid")
            if not path.is_file():
                errors.append(f"MISSING_REGISTERED_TOOL:{tool['name']}")
            elif sha256(path) != tool["sha256"].lower():
                errors.append(f"STALE_REGISTERED_TOOL:{tool['name']}")
        indexed[tool["name"]] = tool
    return indexed


def validate_manifest(data: dict, root: Path) -> tuple[dict, dict]:
    errors, warnings = [], []
    if data.get("schema_version") != SCHEMA:
        raise ContractError(f"schema_version must be {SCHEMA}")
    task = require_dict(data.get("task"), "task")
    for key in ("id", "objective", "owner_skill", "mutation_policy"):
        require_text(task.get(key), f"task.{key}")
    if task["mutation_policy"] not in {"read_only", "scratch_only", "canonical_transaction"}:
        raise ContractError("task.mutation_policy is invalid")
    for key in ("scope", "non_goals", "decision_ids", "allowed_actions", "forbidden_actions"):
        require_string_list(task.get(key), f"task.{key}")
    budget = require_dict(data.get("budget"), "budget")
    max_files = require_int(budget.get("max_files"), "budget.max_files", 3)
    max_bytes = require_int(budget.get("max_bytes"), "budget.max_bytes", 1)

    artifacts = require_list(data.get("artifacts"), "artifacts")
    if len(artifacts) > max_files:
        errors.append("FILE_BUDGET_EXCEEDED")
    seen_paths, seen_core, roles, total_bytes, core_documents, checked = set(), set(), set(), 0, {}, []
    # The byte budget exists to bound the DECISION context a worker carries.
    # config/tool_registry.json is neither decision text nor human-authored:
    # it is a generated authority list, it is already bound by SHA-256, and it
    # grows by ~574 bytes with every tool added. Counting it against the same
    # budget meant the limit tracked the size of the toolchain rather than the
    # size of the decision, and it was raised 20000 -> 40000 once for exactly
    # that reason before filling again at 67 tools. Excluding it puts the
    # budget back on the thing it was meant to constrain; both numbers are
    # still reported so nothing is hidden.
    exempt_roles = {"tool_registry"}
    budgeted_bytes, exempt_bytes = 0, 0
    for i, artifact in enumerate(artifacts):
        artifact = require_dict(artifact, f"artifacts[{i}]")
        role = require_text(artifact.get("role"), f"artifacts[{i}].role")
        rel = require_text(artifact.get("path"), f"artifacts[{i}].path")
        require_text(artifact.get("purpose"), f"artifacts[{i}].purpose")
        artifact_decisions = require_string_list(artifact.get("decision_ids"), f"artifacts[{i}].decision_ids")
        unrelated = sorted(set(artifact_decisions) - set(task["decision_ids"]))
        if unrelated:
            errors.append(f"ARTIFACT_UNRELATED_DECISION:{rel}:{','.join(unrelated)}")
        digest = artifact.get("sha256")
        if not valid_digest(digest):
            raise ContractError(f"artifacts[{i}].sha256 is invalid")
        if rel in seen_paths:
            raise ContractError(f"duplicate artifact path: {rel}")
        if role in CORE_ROLES and role in seen_core:
            raise ContractError(f"duplicate core artifact role: {role}")
        seen_paths.add(rel); roles.add(role)
        if role in CORE_ROLES:
            seen_core.add(role)
            if artifact.get("required") is not True:
                errors.append(f"CORE_ARTIFACT_NOT_REQUIRED:{role}")
        elif artifact.get("required") is not True:
            warnings.append(f"OPTIONAL_ARTIFACT:{rel}")
        path = safe_path(root, rel, f"artifacts[{i}].path")
        exists = path.is_file()
        actual = sha256(path) if exists else None
        size = path.stat().st_size if exists else 0
        total_bytes += size
        if role in exempt_roles:
            exempt_bytes += size
        else:
            budgeted_bytes += size
        if not exists:
            errors.append(f"MISSING_ARTIFACT:{rel}")
        elif actual != digest.lower():
            errors.append(f"STALE_ARTIFACT:{rel}")
        elif role in CORE_ROLES:
            try:
                core_documents[role] = load_json(path, role)
            except ContractError as exc:
                errors.append(f"INVALID_CORE_ARTIFACT:{exc}")
        checked.append({"role": role, "path": rel, "purpose": artifact["purpose"], "decision_ids": artifact_decisions, "bytes": size, "sha256": actual})
    missing_roles = sorted(CORE_ROLES - roles)
    if missing_roles:
        errors.append("MISSING_CORE_ARTIFACT_ROLE:" + ",".join(missing_roles))
    if budgeted_bytes > max_bytes:
        errors.append("BYTE_BUDGET_EXCEEDED")

    intent_constraints, intent_questions = ({}, {})
    decisions, registry = {}, {}
    if "design_intent" in core_documents:
        try:
            intent_constraints, intent_questions = validate_intent(core_documents["design_intent"])
        except ContractError as exc:
            errors.append(f"INVALID_DESIGN_INTENT:{exc}")
    if "decision_unit" in core_documents:
        try:
            decisions = validate_decisions(core_documents["decision_unit"], root)
        except ContractError as exc:
            errors.append(f"INVALID_DECISION_UNITS:{exc}")
    if "tool_registry" in core_documents:
        try:
            registry = validate_tool_registry(core_documents["tool_registry"], root, errors)
        except ContractError as exc:
            errors.append(f"INVALID_TOOL_REGISTRY:{exc}")

    missing_decisions = sorted(set(task["decision_ids"]) - set(decisions))
    if missing_decisions:
        errors.append("MISSING_DECISION_ID:" + ",".join(missing_decisions))
    for decision_id in task["decision_ids"]:
        if decision_id in decisions:
            if decisions[decision_id]["status"] == "blocked":
                errors.append(f"BLOCKED_DECISION:{decision_id}")
            if decisions[decision_id]["owner_skill"] != task["owner_skill"]:
                errors.append(f"DECISION_OWNER_MISMATCH:{decision_id}")

    constraints = {}
    for i, item in enumerate(require_list(data.get("constraints"), "constraints")):
        item = require_dict(item, f"constraints[{i}]")
        for key in ("id", "statement", "source"):
            require_text(item.get(key), f"constraints[{i}].{key}")
        if item["id"] in constraints:
            raise ContractError(f"duplicate manifest constraint: {item['id']}")
        constraints[item["id"]] = item
        if item["id"] not in intent_constraints or any(item[k] != intent_constraints[item["id"]][k] for k in ("statement", "source")):
            errors.append(f"INTENT_CONSTRAINT_MISMATCH:{item['id']}")
    for decision_id in task["decision_ids"]:
        if decision_id in decisions:
            missing_constraints = sorted(set(decisions[decision_id]["constraints"]) - set(constraints))
            if missing_constraints:
                errors.append(f"DECISION_CONSTRAINT_MISSING:{decision_id}:{','.join(missing_constraints)}")

    questions, blocking = {}, []
    for i, item in enumerate(require_list(data.get("open_questions", []), "open_questions", nonempty=False)):
        item = require_dict(item, f"open_questions[{i}]")
        for key in ("id", "question", "owner"):
            require_text(item.get(key), f"open_questions[{i}].{key}")
        if not isinstance(item.get("blocking"), bool):
            raise ContractError(f"open_questions[{i}].blocking must be boolean")
        if item["id"] in questions:
            raise ContractError(f"duplicate manifest question: {item['id']}")
        questions[item["id"]] = item
        if item["id"] in intent_questions and any(item[k] != intent_questions[item["id"]][k] for k in ("question", "owner", "blocking")):
            errors.append(f"INTENT_QUESTION_MISMATCH:{item['id']}")
        if item["blocking"]:
            blocking.append(item["id"])
    omitted_blocking = sorted(qid for qid, item in intent_questions.items() if item["blocking"] and qid not in questions)
    if omitted_blocking:
        errors.append("OMITTED_INTENT_BLOCKER:" + ",".join(omitted_blocking))
    if blocking:
        errors.append("BLOCKING_OPEN_QUESTION")

    manifest_tools = set()
    for i, tool in enumerate(require_list(data.get("tools"), "tools")):
        tool = require_dict(tool, f"tools[{i}]")
        for key in ("name", "purpose", "mode"):
            require_text(tool.get(key), f"tools[{i}].{key}")
        if tool["name"] in manifest_tools:
            raise ContractError(f"duplicate manifest tool: {tool['name']}")
        manifest_tools.add(tool["name"])
        if tool["mode"] not in {"read", "write"}:
            raise ContractError(f"tools[{i}].mode must be read or write")
        preconditions = require_string_list(tool.get("preconditions", []), f"tools[{i}].preconditions", nonempty=tool["mode"] == "write")
        registered = registry.get(tool["name"])
        if not registered:
            errors.append(f"UNREGISTERED_TOOL:{tool['name']}")
        elif not registered["available"]:
            errors.append(f"UNAVAILABLE_TOOL:{tool['name']}")
        elif tool["mode"] not in registered["modes"]:
            errors.append(f"TOOL_MODE_NOT_ALLOWED:{tool['name']}:{tool['mode']}")
        elif not set(registered["preconditions"]).issubset(preconditions):
            errors.append(f"TOOL_PRECONDITION_MISSING:{tool['name']}")
        if task["mutation_policy"] == "read_only" and tool["mode"] == "write":
            errors.append(f"READ_ONLY_WRITE_TOOL:{tool['name']}")

    output = require_dict(data.get("output_contract"), "output_contract")
    output_rel = require_text(output.get("artifact_path"), "output_contract.artifact_path")
    safe_path(root, output_rel, "output_contract.artifact_path")
    require_text(output.get("schema_version"), "output_contract.schema_version")
    fields = require_string_list(output.get("required_claim_fields"), "output_contract.required_claim_fields")
    missing_fields = sorted(CLAIM_FIELDS - set(fields))
    if missing_fields:
        errors.append("MISSING_CLAIM_FIELDS:" + ",".join(missing_fields))

    gate_ids = set()
    for i, gate in enumerate(require_list(data.get("gates"), "gates")):
        gate = require_dict(gate, f"gates[{i}]")
        gate_id = require_text(gate.get("id"), f"gates[{i}].id")
        if gate_id in gate_ids:
            raise ContractError(f"duplicate gate id: {gate_id}")
        gate_ids.add(gate_id)
        validator = require_dict(gate.get("validator"), f"gates[{i}].validator")
        validator_rel = require_text(validator.get("path"), f"gates[{i}].validator.path")
        validator_path = safe_path(root, validator_rel, f"gates[{i}].validator.path")
        if not valid_digest(validator.get("sha256")):
            raise ContractError(f"gates[{i}].validator.sha256 is invalid")
        if not validator_path.is_file():
            errors.append(f"MISSING_GATE_VALIDATOR:{validator_rel}")
        elif sha256(validator_path) != validator["sha256"].lower():
            errors.append(f"STALE_GATE_VALIDATOR:{validator_rel}")
        argv = require_string_list(gate.get("argv"), f"gates[{i}].argv", unique=False)
        validator_count = argv.count(validator_rel)
        executable_entrypoint = argv[0] == validator_rel
        python_entrypoint = len(argv) > 1 and Path(argv[0]).name in {"python", "python3", Path(sys.executable).name} and argv[1] == validator_rel
        if validator_count != 1 or not (executable_entrypoint or python_entrypoint):
            errors.append(f"GATE_ARGV_VALIDATOR_NOT_ENTRYPOINT:{gate_id}")
        if argv.count(output_rel) != 1:
            errors.append(f"GATE_ARGV_MISSING_OUTPUT:{gate_id}")
        require_int(gate.get("expected_exit"), f"gates[{i}].expected_exit", 0)

    report = {
        "schema_version": SCHEMA,
        "task_id": task["id"],
        "verdict": "READY" if not errors else "BLOCKED",
        "errors": errors,
        "warnings": warnings,
        "blocking_questions": blocking,
        "artifacts": checked,
        "budget": {"files": len(artifacts), "max_files": max_files, "bytes": total_bytes, "max_bytes": max_bytes,
                   "budgeted_bytes": budgeted_bytes, "exempt_bytes": exempt_bytes,
                   "exempt_roles": sorted(exempt_roles)},
    }
    return report, output


def validate_worker_output(path: Path, contract: dict, root: Path) -> list[str]:
    errors = []
    document = load_json(path, "worker output")
    document = require_dict(document, "worker output")
    if document.get("schema_version") != contract["schema_version"]:
        errors.append("OUTPUT_SCHEMA_MISMATCH")
    evidence = {}
    for i, item in enumerate(require_list(document.get("evidence", []), "worker output.evidence", nonempty=False)):
        evidence_id, item = validate_evidence_item(item, f"worker output.evidence[{i}]", root, errors)
        if evidence_id in evidence:
            raise ContractError(f"duplicate evidence id: {evidence_id}")
        evidence[evidence_id] = item
    claims = require_list(document.get("claims"), "worker output.claims")
    claim_ids = set()
    for i, claim in enumerate(claims):
        claim = require_dict(claim, f"worker output.claims[{i}]")
        missing = sorted(CLAIM_FIELDS - set(claim))
        if missing:
            errors.append(f"CLAIM_FIELDS_MISSING:{i}:{','.join(missing)}")
            continue
        claim_id = require_text(claim.get("claim_id"), f"claims[{i}].claim_id")
        if claim_id in claim_ids:
            raise ContractError(f"duplicate claim id: {claim_id}")
        claim_ids.add(claim_id)
        require_text(claim.get("statement"), f"claim {claim_id}.statement")
        require_text(claim.get("scope"), f"claim {claim_id}.scope")
        if claim.get("confidence") not in {"high", "medium", "low"}:
            raise ContractError(f"invalid claim confidence: {claim_id}")
        if claim.get("status") not in CLAIM_STATUSES:
            raise ContractError(f"invalid claim status: {claim_id}")
        evidence_ids = require_string_list(claim.get("evidence_ids"), f"claim {claim_id}.evidence_ids", nonempty=False)
        missing_evidence = sorted(set(evidence_ids) - set(evidence))
        if missing_evidence:
            errors.append(f"CLAIM_EVIDENCE_MISSING:{claim_id}:{','.join(missing_evidence)}")
        if claim["status"] in {"accepted", "rejected"} and not evidence_ids:
            errors.append(f"CLAIM_WITHOUT_EVIDENCE:{claim_id}")
        if claim["status"] == "accepted" and any(evidence[eid]["status"] != "canonical" for eid in evidence_ids if eid in evidence):
            errors.append(f"CLAIM_NONCANONICAL_EVIDENCE:{claim_id}")
    return errors


def run_gates(data: dict, root: Path) -> tuple[list[str], list[dict]]:
    errors, results = [], []
    for gate in data["gates"]:
        try:
            proc = subprocess.run(gate["argv"], cwd=root, text=True, capture_output=True, timeout=120, shell=False)
            results.append({"id": gate["id"], "exit_code": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]})
            if proc.returncode != gate["expected_exit"]:
                errors.append(f"GATE_EXIT_MISMATCH:{gate['id']}")
        except (OSError, subprocess.SubprocessError) as exc:
            results.append({"id": gate["id"], "error": str(exc)})
            errors.append(f"GATE_EXECUTION_ERROR:{gate['id']}")
    return errors, results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", dest="report", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--postflight", action="store_true", help="validate worker output and execute declared gates")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        data = require_dict(load_json(args.manifest, "manifest"), "manifest")
        report, output = validate_manifest(data, root)
        if args.postflight and report["verdict"] == "READY":
            output_path = safe_path(root, output["artifact_path"], "output_contract.artifact_path")
            if not output_path.is_file():
                report["errors"].append("MISSING_WORKER_OUTPUT")
            else:
                report["errors"].extend(validate_worker_output(output_path, output, root))
                gate_errors, gate_results = run_gates(data, root)
                report["errors"].extend(gate_errors)
                report["gate_results"] = gate_results
            report["verdict"] = "ACCEPTED" if not report["errors"] else "REJECTED"
        code = 0 if report["verdict"] in {"READY", "ACCEPTED"} else 1
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError) as exc:
        report = {"schema_version": SCHEMA, "verdict": "ERROR", "errors": [str(exc)]}
        code = 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        try:
            report_path = safe_path(args.root.resolve(), str(args.report), "--json")
            report_path.write_text(encoded, encoding="utf-8")
        except (OSError, ContractError) as exc:
            diag(f"ERROR: report write failed: {exc}")
            emit({"schema_version": SCHEMA, "verdict": "ERROR",
                  "errors": [f"report write failed: {exc}"]})
            return 2
    sys.stdout.write(encoded)
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
