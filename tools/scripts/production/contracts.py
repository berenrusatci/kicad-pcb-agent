"""Artifact contracts: drc.json and parts.json, read from DISK at every gate.

WHY THESE EXIST
---------------
Steps used to hand each other Python objects: one function ran the DRC and the
caller checked the returned dict. An in-memory dict and the JSON on disk drift
apart silently - two agents running DRC at different times, or one forgetting
to write its report. These gates therefore ALWAYS re-read the file, never a
value passed in.

FAIL-CLOSED, ALWAYS
-------------------
A missing, unparsable or wrong-shaped contract raises ``ContractGateError``. It
does NOT return False and it certainly does not return True. "The check never
ran" and "the check ran and found nothing" are different answers, and the
second must never be inferred from the first.

Library module: no CLI of its own. ``outputs.py`` is the entry point.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import FAIL, NO_COVERAGE, PASS  # noqa: E402
from common.report_io import write_report  # noqa: E402

CONTRACT_SCHEMA_VERSION = 1   # bumped when a field is added; never changed silently


class ContractGateError(RuntimeError):
    """A contract gate could not be evaluated, or it failed.

    A distinct type so an orchestrator can tell "the production gate is shut"
    apart from unrelated runtime errors.
    """


# --------------------------------------------------------------------------- #
# drc.json
# --------------------------------------------------------------------------- #
@dataclass
class DrcContract:
    """A small, stable projection of the raw kicad-cli DRC/ERC JSON.

    The raw shapes differ (DRC puts violations at the top level plus a separate
    ``unconnected_items`` list; ERC nests them under ``sheets[].violations``).
    Reducing both to one flat record keeps that difference from leaking into
    every caller.
    """

    schema_version: int
    source: str                      # "drc" | "erc"
    subject_path: str
    violation_count: int             # error/fatal severity only
    warning_count: int
    violations: list[dict] = field(default_factory=list)
    created_utc: str = ""


def build_drc_contract(subject_path: str, raw: dict,
                       source: str = "drc") -> DrcContract:
    """Reduce a raw kicad-cli report to a contract.

    ``source`` selects which violation path to read. An unrecognised value
    raises: with the wrong source the violation list would come back empty and
    manufacture a PASS.

    ``unconnected_items`` is included for DRC. A real board produced six
    error-severity unconnected items that a violations-only reader passed
    silently - a board could have reached fabrication with unrouted nets.
    """
    if source == "drc":
        all_violations = list(raw.get("violations", []))
        all_violations += list(raw.get("unconnected_items", []))
    elif source == "erc":
        all_violations = [v for s in raw.get("sheets", [])
                          for v in s.get("violations", [])]
    else:
        raise ValueError(f"unknown source={source!r} (only 'drc' or 'erc')")

    severe = {"error", "fatal"} if source == "erc" else {"error"}
    errors = [v for v in all_violations if v.get("severity") in severe]
    warnings = [v for v in all_violations if v.get("severity") not in severe]
    return DrcContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        source=source,
        subject_path=subject_path,
        violation_count=len(errors),
        warning_count=len(warnings),
        violations=errors,
        created_utc=datetime.now(timezone.utc).isoformat(),
    )


def schema_unrecognised(raw: dict) -> bool:
    """True when a report matches NEITHER the DRC nor the ERC shape.

    Fail-closed guard: a "clean" verdict from an unrecognised schema means
    "nothing could be read", not "nothing is wrong". Callers must check this
    BEFORE trusting a clean result.
    """
    return "violations" not in raw and "sheets" not in raw


def write_drc_contract(contract: DrcContract, path: str = "drc.json") -> str:
    # Written through common/report_io: UTF-8, ensure_ascii=False,
    # allow_nan=False and an atomic temp-file + fsync + replace, so a reader
    # can never see a half-written gate contract.
    write_report(asdict(contract), Path(path))
    return path


def drc_gate_passes(path: str = "drc.json") -> bool:
    """``violation_count == 0``, read from disk. Raises when unreadable."""
    file_path = Path(path)
    if not file_path.exists():
        raise ContractGateError(
            f"{path} does not exist - the DRC/ERC never ran, or its contract "
            "was never written. This gate cannot fail open.")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractGateError(f"{path} is not valid JSON: {exc}") from exc
    if "violation_count" not in data:
        raise ContractGateError(
            f"{path} has no 'violation_count' field - unexpected schema "
            f"(version mismatch? current is {CONTRACT_SCHEMA_VERSION})")
    if data.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ContractGateError(
            f"{path} is schema_version {data.get('schema_version')!r}, this "
            f"gate reads {CONTRACT_SCHEMA_VERSION}. A contract it cannot read "
            "is not a contract it may pass.")
    return data["violation_count"] == 0


# --------------------------------------------------------------------------- #
# parts.json
# --------------------------------------------------------------------------- #
@dataclass
class PartContractRow:
    refdes: str
    supplier_code: str
    risk_score: float
    lifecycle_status: str        # Active | NRND | EOL | Obsolete | ...
    alternative_found: bool = False


@dataclass
class PartsContract:
    schema_version: int
    rows: list[PartContractRow]
    created_utc: str = ""


def write_parts_contract(rows, path: str = "parts.json") -> str:
    contract = PartsContract(
        schema_version=CONTRACT_SCHEMA_VERSION, rows=list(rows),
        created_utc=datetime.now(timezone.utc).isoformat())
    # Written through common/report_io: UTF-8, ensure_ascii=False,
    # allow_nan=False and an atomic temp-file + fsync + replace, so a reader
    # can never see a half-written gate contract.
    write_report(asdict(contract), Path(path))
    return path


RISKY_LIFECYCLE = {"NRND", "EOL", "Obsolete"}


def parts_gate_status(path: str = "parts.json",
                      max_risk_score: float = 0.5) -> tuple[str, list[str], int]:
    """Evaluate every part row from disk. Returns (status, issues, scanned).

    Status is PASS / FAIL / NO_COVERAGE, never a bare bool, because the empty
    contract has to be sayable. The old bool version looped over
    ``data.get("rows", [])`` and returned ``not issues``: with no ``rows`` key
    at all, or with ``rows: []``, the loop ran zero times, no issue was
    appended, and the gate reported a clean pass on a contract that had
    examined nothing. That is the "empty set is clean" vacuity - the same
    shape ``Finding`` already forbids for a single check.

    So:
      * no ``rows`` key / no ``schema_version`` / wrong version -> raises,
        per this module's fail-closed rule for a wrong-shaped contract;
      * ``rows: []`` -> NO_COVERAGE, scanned 0. A parts contract that lists
        no parts is not a parts contract that passed;
      * otherwise every row is evaluated and a row with no ``risk_score`` is
        assumed maximally risky, not safe.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ContractGateError(
            f"{path} does not exist - write_parts_contract() was never called. "
            "This gate cannot fail open.")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractGateError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractGateError(f"{path} is not a JSON object - unexpected schema")
    if "schema_version" not in data:
        raise ContractGateError(
            f"{path} has no 'schema_version' field - unexpected schema "
            f"(current is {CONTRACT_SCHEMA_VERSION})")
    if data["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise ContractGateError(
            f"{path} is schema_version {data['schema_version']}, this gate "
            f"reads {CONTRACT_SCHEMA_VERSION}. A contract it cannot read is "
            "not a contract it may pass.")
    if "rows" not in data:
        raise ContractGateError(
            f"{path} has no 'rows' field - unexpected schema. An absent row "
            "list is not an empty one, and neither one passes a gate.")
    rows = data["rows"]
    if not isinstance(rows, list):
        raise ContractGateError(f"{path}: 'rows' is not a list")
    if not rows:
        return (NO_COVERAGE, [], 0)

    issues: list[str] = []
    for row in rows:
        refdes = row.get("refdes", "?")
        risk = row.get("risk_score", 1.0)     # missing field = worst case
        status = row.get("lifecycle_status", "?")
        alternative = row.get("alternative_found", False)
        if risk > max_risk_score:
            issues.append(f"{refdes}: risk_score {risk} > {max_risk_score}")
        if status in RISKY_LIFECYCLE and not alternative:
            issues.append(
                f"{refdes}: lifecycle {status} with no pin-compatible alternative")
    return (FAIL if issues else PASS, issues, len(rows))


def parts_gate_passes(path: str = "parts.json",
                      max_risk_score: float = 0.5) -> tuple[bool, list[str]]:
    """Boolean view of ``parts_gate_status`` for callers that want one.

    NO_COVERAGE has no truthful bool, so it raises rather than picking one.
    Returning False would read as "the parts are bad" and True would be the
    original bug; a gate that cannot be expressed in the caller's vocabulary
    fails closed, exactly like a missing file does above.
    """
    status, issues, _ = parts_gate_status(path, max_risk_score)
    if status == NO_COVERAGE:
        raise ContractGateError(
            f"{path} lists no parts - nothing was examined, so this gate has "
            "no boolean answer. Use parts_gate_status() and treat "
            "NO_COVERAGE as NO_COVERAGE.")
    return (status == PASS, issues)
