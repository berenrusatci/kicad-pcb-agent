#!/usr/bin/env python3
"""Deterministic validator for PCB quality-gate receipts.

The validator checks the receipt contract, binds evidence to current files by
SHA-256, validates metric meaning/coverage, reduces mandatory child gates, and
checks promotion/retention evidence.  It never turns an absent field into
evidence.  The legacy top-level ``schema`` spelling is accepted read-only as an
alias for ``schema_version``; canonical output always uses ``schema_version``.

CLI:
    python3 gate_receipt.py RECEIPT [--root DIR] [--json REPORT]
                                    [--require-fault-injection]

Exit codes:
    0  receipt is current and the effective verdict is GREEN
    1  receipt is RED/FAIL, NO_COVERAGE, INVALID_METRIC, or STALE
    2  malformed input, unsupported schema, or validator execution error
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (  # noqa: E402
    PARTS_DO_NOT_SUM,
    SCANNED_EXCEEDS_ELIGIBLE,
    coverage_rule_violations,
)
from common.hashing import sha256_file, valid_digest  # noqa: E402
from common.report_io import emit, run_cli  # noqa: E402


SCHEMA_VERSION = "gate-receipt/v1"
REPORT_SCHEMA_VERSION = "gate-receipt-validator-report/v1"
SUPPORTED_SCHEMA_VERSIONS = {
    SCHEMA_VERSION,
}
LEGACY_SCHEMA_MAP = {
    "usb-hs-pair-metrics/v1": SCHEMA_VERSION,
    "usb-hs-pair-verifier-receipt/v1": SCHEMA_VERSION,
}
GREEN_WORDS = {"GREEN", "PASS"}
RED_WORDS = {
    "RED", "FAIL", "NO_COVERAGE", "INVALID_METRIC", "STALE", "ERROR"
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMANTIC_FIELDS = (
    "definition", "units", "distance_semantics", "region",
    "sampling_step", "geometry_model",
)
RED_RETENTION = {"receipt", "child_receipts", "logs", "scratch"}
GREEN_RETENTION = {
    "receipt", "hashes", "metrics", "reproducibility_command"
}


@dataclass
class Issue:
    code: str
    status: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "status": self.status,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class Context:
    receipt_path: Path
    root: Path
    require_fault: bool = False
    seen: set[Path] = field(default_factory=set)
    issues: list[Issue] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def issue(self, code: str, status: str, path: str, message: str) -> None:
        self.issues.append(Issue(code, status, path, message))

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append({"code": code, "path": path, "message": message})


def resolve_path(raw: Any, ctx: Context, issue_path: str) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = ctx.receipt_path.parent / path
    path = path.resolve()
    try:
        path.relative_to(ctx.root)
    except ValueError:
        ctx.issue("PATH_OUTSIDE_ROOT", "ERROR", issue_path,
                  f"path escapes validation root {ctx.root}: {path}")
        return None
    return path


def is_utc(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    offset = parsed.utcoffset()
    return parsed.tzinfo is not None and offset is not None \
        and offset.total_seconds() == 0


def require_mapping(obj: dict[str, Any], key: str, ctx: Context,
                    base: str = "$") -> dict[str, Any] | None:
    value = obj.get(key)
    if not isinstance(value, dict):
        ctx.issue("MISSING_FIELD", "ERROR", f"{base}.{key}",
                  "object is required")
        return None
    return value


def require_list(obj: dict[str, Any], key: str, ctx: Context,
                 base: str = "$") -> list[Any] | None:
    value = obj.get(key)
    if not isinstance(value, list):
        ctx.issue("MISSING_FIELD", "ERROR", f"{base}.{key}",
                  "array is required")
        return None
    return value


def require_text(obj: dict[str, Any], key: str, ctx: Context,
                 base: str = "$") -> str | None:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        ctx.issue("MISSING_FIELD", "ERROR", f"{base}.{key}",
                  "non-empty string is required")
        return None
    return value


def validate_hash_artifact(item: Any, ctx: Context, base: str,
                           *, require_role: bool = False) -> Path | None:
    if not isinstance(item, dict):
        ctx.issue("INVALID_HASH_RECORD", "ERROR", base,
                  "artifact identity must be an object")
        return None
    if require_role:
        require_text(item, "role", ctx, base)
    raw_path = require_text(item, "path", ctx, base)
    expected = item.get("sha256", item.get("hash"))
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        ctx.issue("INVALID_SHA256", "ERROR", f"{base}.sha256",
                  "64 lowercase hexadecimal characters are required")
        return None
    path = resolve_path(raw_path, ctx, f"{base}.path")
    if path is None:
        return None
    if not path.is_file():
        ctx.issue("MISSING_ARTIFACT", "STALE", f"{base}.path",
                  f"file does not exist: {path}")
        return None
    actual = sha256_file(path)
    if actual != expected:
        ctx.issue("HASH_MISMATCH", "STALE", f"{base}.sha256",
                  f"expected {expected}, current {actual}")
    return path


def validate_tool(receipt: dict[str, Any], ctx: Context) -> None:
    tool = require_mapping(receipt, "tool", ctx)
    if tool is None:
        return
    require_text(tool, "name", ctx, "$.tool")
    require_text(tool, "version", ctx, "$.tool")
    validate_hash_artifact(tool, ctx, "$.tool")


def validate_inputs(receipt: dict[str, Any], ctx: Context) -> None:
    inputs = require_list(receipt, "inputs", ctx)
    if inputs is None:
        return
    if not inputs:
        ctx.issue("NO_INPUTS", "ERROR", "$.inputs",
                  "at least one hash-bound input is required")
    for index, item in enumerate(inputs):
        validate_hash_artifact(item, ctx, f"$.inputs[{index}]", require_role=True)


def validate_config(receipt: dict[str, Any], ctx: Context) -> None:
    records: list[tuple[Any, str]] = []
    for name in ("config", "rules"):
        if name not in receipt:
            continue
        value = receipt[name]
        if isinstance(value, list):
            records.extend((item, f"$.{name}[{index}]")
                           for index, item in enumerate(value))
        else:
            records.append((value, f"$.{name}"))
    if not records:
        ctx.issue("MISSING_CONFIG_HASH", "ERROR", "$.config",
                  "config or rules path+sha256 identity is required")
        return
    for item, base in records:
        validate_hash_artifact(item, ctx, base)


def metric_leaf_items(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Return deterministic dotted paths and values for non-object leaves."""
    if isinstance(value, dict):
        found: list[tuple[str, Any]] = []
        for key in sorted(value):
            child_path = f"{prefix}.{key}" if prefix else key
            found.extend(metric_leaf_items(value[key], child_path))
        return found
    return [(prefix, value)] if prefix else []


def metric_leaf_paths(value: Any) -> list[str]:
    return [path for path, _ in metric_leaf_items(value)]


def validate_semantic_spec(metric_id: str, spec: Any, ctx: Context,
                           base: str) -> bool:
    if not isinstance(spec, dict):
        ctx.issue("INVALID_METRIC_SEMANTICS", "INVALID_METRIC", base,
                  "semantic entry must be an object")
        return False
    complete = True
    for field_name in SEMANTIC_FIELDS:
        value = spec.get(field_name)
        if value is None or value == "" or value == {}:
            complete = False
            ctx.issue("INCOMPLETE_METRIC_SEMANTICS", "INVALID_METRIC",
                      f"{base}.{field_name}", "field is required")
    name = metric_id.lower()
    distance = str(spec.get("distance_semantics", "")).lower()
    geometry = str(spec.get("geometry_model", "")).lower()
    if "edge_gap" in name and "center" in distance:
        complete = False
        ctx.issue("CENTERLINE_AS_EDGE_GAP", "INVALID_METRIC", base,
                  "an edge-gap metric cannot use centerline distance")
    if "arc_aware" in name and "chord" in geometry:
        complete = False
        ctx.issue("CHORD_AS_ARC", "INVALID_METRIC", base,
                  "an arc-aware metric cannot use a chord-only geometry model")
    if "impedance" in name and any(word in geometry for word in
                                   ("parallelism", "coupling", "spice")):
        complete = False
        ctx.issue("PROXY_AS_SIGNOFF", "INVALID_METRIC", base,
                  "parallelism/coupling/SPICE is not impedance or MIPI SI proof")
    return complete


def load_semantic_refs(value: Any, ctx: Context) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, list):
        ctx.issue("INVALID_SEMANTIC_REFS", "INVALID_METRIC",
                  "$.metric_semantic_refs",
                  "semantic refs must be an array of metric_id/path/sha256 objects")
        return {}
    loaded: dict[str, Any] = {}
    seen_ids: set[str] = set()
    for index, ref in enumerate(value):
        base = f"$.metric_semantic_refs[{index}]"
        if not isinstance(ref, dict):
            ctx.issue("INVALID_SEMANTIC_REF", "INVALID_METRIC", base,
                      "ref must be an object with metric_id, path and sha256")
            continue
        metric_id = ref.get("metric_id")
        raw_path = ref.get("path")
        expected = ref.get("sha256")
        shape_ok = True
        if not isinstance(metric_id, str) or not metric_id.strip():
            shape_ok = False
            ctx.issue("INVALID_SEMANTIC_REF", "INVALID_METRIC",
                      f"{base}.metric_id", "non-empty metric_id is required")
        if not isinstance(raw_path, str) or not raw_path.strip():
            shape_ok = False
            ctx.issue("INVALID_SEMANTIC_REF", "INVALID_METRIC",
                      f"{base}.path", "non-empty path is required")
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            shape_ok = False
            ctx.issue("INVALID_SEMANTIC_REF", "INVALID_METRIC",
                      f"{base}.sha256", "exact SHA-256 is required")
        if not shape_ok:
            continue
        if metric_id in seen_ids:
            ctx.issue("DUPLICATE_SEMANTIC_MAPPING", "INVALID_METRIC",
                      f"{base}.metric_id",
                      f"metric {metric_id} is referenced more than once")
            continue
        seen_ids.add(metric_id)
        path = validate_hash_artifact(ref, ctx, base)
        if path is None:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ctx.issue("INVALID_SEMANTIC_REF_JSON", "INVALID_METRIC", base,
                      str(exc))
            continue
        if not isinstance(document, dict) or document.get("metric_id") != metric_id:
            ctx.issue("SEMANTIC_REF_ID_MISMATCH", "INVALID_METRIC", base,
                      "referenced JSON must bind the same metric_id")
            continue
        spec = document.get("semantics")
        if validate_semantic_spec(metric_id, spec, ctx,
                                  f"{base}.document.semantics"):
            loaded[metric_id] = spec
    return loaded


def validate_metric_semantics(receipt: dict[str, Any], ctx: Context) -> None:
    metrics = receipt.get("parsed_metrics")
    if not isinstance(metrics, dict) or not metrics:
        ctx.issue("MISSING_METRICS", "ERROR", "$.parsed_metrics",
                  "non-empty typed metric object is required")
        metric_paths: list[str] = []
    else:
        metric_items = metric_leaf_items(metrics)
        metric_paths = [path for path, _ in metric_items]
        if not metric_paths:
            ctx.issue("MISSING_METRIC_LEAVES", "INVALID_METRIC",
                      "$.parsed_metrics", "at least one typed leaf is required")
        for metric_id, value in metric_items:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ctx.issue("INVALID_METRIC_TYPE", "INVALID_METRIC",
                          f"$.parsed_metrics.{metric_id}",
                          "metric leaf must be a JSON number; bool/null/string/list are invalid")
            elif isinstance(value, float) and not math.isfinite(value):
                ctx.issue("INVALID_METRIC_NUMBER", "INVALID_METRIC",
                          f"$.parsed_metrics.{metric_id}",
                          "metric number must be finite I-JSON")
    if len(metric_paths) != len(set(metric_paths)):
        ctx.issue("AMBIGUOUS_METRIC_PATH", "INVALID_METRIC",
                  "$.parsed_metrics",
                  "nested and dotted keys collapse to the same metric path")
    leaf_ids = set(metric_paths)

    semantics = receipt.get("metric_semantics")
    inline: dict[str, Any] = {}
    if semantics is not None and not isinstance(semantics, dict):
        ctx.issue("INVALID_METRIC_SEMANTICS", "INVALID_METRIC",
                  "$.metric_semantics", "semantic mapping must be an object")
    elif isinstance(semantics, dict):
        for metric_id, spec in semantics.items():
            base = f"$.metric_semantics.{metric_id}"
            if not isinstance(metric_id, str) or not metric_id.strip():
                ctx.issue("INVALID_SEMANTIC_METRIC_ID", "INVALID_METRIC", base,
                          "non-empty string metric id is required")
                continue
            if validate_semantic_spec(metric_id, spec, ctx, base):
                inline[metric_id] = spec

    refs = load_semantic_refs(receipt.get("metric_semantic_refs"), ctx)
    if not inline and not refs:
        ctx.issue("MISSING_METRIC_SEMANTICS", "INVALID_METRIC",
                  "$.metric_semantics",
                  "inline semantics or hash-bound semantic refs are required")
    conflicts = sorted(set(inline) & set(refs))
    for metric_id in conflicts:
        ctx.issue("DUPLICATE_SEMANTIC_MAPPING", "INVALID_METRIC",
                  "$.metric_semantic_refs",
                  f"metric {metric_id} has both inline and referenced semantics")
    mapped_ids = set(inline) | set(refs)
    missing = sorted(leaf_ids - mapped_ids)
    extra = sorted(mapped_ids - leaf_ids)
    for metric_id in missing:
        ctx.issue("MISSING_METRIC_SEMANTIC_MAPPING", "INVALID_METRIC",
                  f"$.parsed_metrics.{metric_id}",
                  "every parsed metric leaf needs exactly one semantic mapping")
    for metric_id in extra:
        ctx.issue("EXTRA_METRIC_SEMANTIC_MAPPING", "INVALID_METRIC",
                  f"$.metric_semantics.{metric_id}",
                  "semantic mapping has no parsed metric leaf")


def validate_coverage(receipt: dict[str, Any], ctx: Context) -> None:
    coverage = require_mapping(receipt, "coverage", ctx)
    if coverage is None:
        return
    values: dict[str, int] = {}
    for key in ("scanned", "eligible", "excluded"):
        value = coverage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            ctx.issue("INVALID_COVERAGE", "ERROR", f"$.coverage.{key}",
                      "non-negative integer is required")
        else:
            values[key] = value
    if values.get("scanned") == 0:
        if not isinstance(coverage.get("reason"), str) or not coverage["reason"].strip():
            ctx.issue("MISSING_NO_COVERAGE_REASON", "NO_COVERAGE",
                      "$.coverage.reason", "zero scope requires an explicit reason")
        ctx.issue("NO_COVERAGE", "NO_COVERAGE", "$.coverage.scanned",
                  "zero scanned items can never be PASS")
    if all(key in values for key in ("scanned", "eligible", "excluded")):
        scanned = values["scanned"]
        eligible = values["eligible"]
        excluded = values["excluded"]
        # The rule itself lives in common.findings (sourced from
        # schemas/metric_coverage.schema.json); this only maps rule names
        # onto gate_receipt's richer issue records.
        for rule in coverage_rule_violations(values):
            if rule == SCANNED_EXCEEDS_ELIGIBLE:
                ctx.issue("INVALID_COVERAGE", "RED", "$.coverage",
                          f"scanned ({scanned}) cannot exceed eligible ({eligible})")
            elif rule == PARTS_DO_NOT_SUM:
                ctx.issue("INVALID_COVERAGE", "RED", "$.coverage",
                          "scanned + excluded must equal eligible")
    per_metric = coverage.get("per_metric")
    if per_metric is not None:
        if not isinstance(per_metric, dict) or not per_metric:
            ctx.issue("INVALID_COVERAGE", "RED", "$.coverage.per_metric",
                      "per_metric coverage must be a non-empty metric-id mapping")
            return
        metric_ids = set(metric_leaf_paths(receipt.get("parsed_metrics", {})))
        coverage_ids = set(per_metric)
        if coverage_ids != metric_ids:
            ctx.issue("INVALID_COVERAGE", "RED", "$.coverage.per_metric",
                      "per_metric coverage ids must exactly match parsed metric leaves")
        for metric_id, counts in per_metric.items():
            base = f"$.coverage.per_metric.{metric_id}"
            if not isinstance(counts, dict):
                ctx.issue("INVALID_COVERAGE", "RED", base,
                          "coverage counts must be an object")
                continue
            row: dict[str, int] = {}
            for key in ("scanned", "eligible", "excluded"):
                value = counts.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    ctx.issue("INVALID_COVERAGE", "RED", f"{base}.{key}",
                              "non-negative integer is required")
                else:
                    row[key] = value
            if len(row) != 3:
                continue
            if row["scanned"] == 0:
                ctx.issue("NO_COVERAGE", "NO_COVERAGE", f"{base}.scanned",
                          "zero per-metric scope can never be PASS")
            if coverage_rule_violations(row):
                ctx.issue("INVALID_COVERAGE", "RED", base,
                          "require scanned <= eligible and scanned + excluded == eligible")


def flatten_numeric_metrics(value: Any, prefix: str = "") -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            found.extend(flatten_numeric_metrics(child, name))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        found.append((prefix.lower(), float(value)))
    return found


def validate_command_semantics(receipt: dict[str, Any], ctx: Context) -> None:
    argv = receipt.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        ctx.issue("INVALID_ARGV", "ERROR", "$.argv",
                  "exact non-empty argv array is required")
        argv = []
    exit_code = receipt.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        ctx.issue("INVALID_EXIT_CODE", "ERROR", "$.exit_code",
                  "integer exit code is required")
    gate_kind = str(receipt.get("gate_kind", receipt.get("profile", ""))).lower()
    if "drc" in gate_kind or "erc" in gate_kind:
        if "--exit-code-violations" not in argv:
            ctx.issue("MISSING_EXIT_CODE_VIOLATIONS", "RED", "$.argv",
                      "DRC/ERC gate must use --exit-code-violations")
        if "--severity-all" not in argv:
            ctx.issue("MISSING_SEVERITY_ALL", "RED", "$.argv",
                      "audit DRC/ERC gate must not hide severity categories")
        metrics = flatten_numeric_metrics(receipt.get("parsed_metrics", {}))
        for name, value in metrics:
            leaf = name.rsplit(".", 1)[-1]
            if value > 0 and leaf in {
                "violations", "violation_count", "errors", "error_count"
            }:
                ctx.issue("PARSED_VIOLATIONS", "RED",
                          f"$.parsed_metrics.{name}",
                          f"parsed {leaf}={value:g}; exit code alone cannot pass the gate")


def validate_fault_injection(receipt: dict[str, Any], ctx: Context) -> None:
    required = ctx.require_fault or str(receipt.get("assurance_level", "")).upper() == "A"
    fault = receipt.get("fault_injection")
    if not required and fault is None:
        return
    if not isinstance(fault, dict):
        ctx.issue("MISSING_FAULT_INJECTION", "RED", "$.fault_injection",
                  "A-level gate requires a fault-injection receipt")
        return
    require_text(fault, "status", ctx, "$.fault_injection")
    identities = (
        ("fixture", False),
        ("baseline_artifact", False),
        ("injected_artifact", False),
    )
    identity_paths: dict[str, Path] = {}
    for name, require_role in identities:
        identity = fault.get(name)
        if not isinstance(identity, dict):
            ctx.issue("MISSING_FAULT_ARTIFACT_BINDING", "RED",
                      f"$.fault_injection.{name}",
                      "path+sha256 artifact identity is required")
            continue
        path = validate_hash_artifact(identity, ctx,
                                      f"$.fault_injection.{name}",
                                      require_role=require_role)
        if path is not None:
            identity_paths[name] = path

    command = fault.get("command")
    if not isinstance(command, dict):
        ctx.issue("MISSING_FAULT_COMMAND", "RED", "$.fault_injection.command",
                  "exact argv and exit_code are required")
    else:
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or not all(
                isinstance(value, str) for value in argv):
            ctx.issue("INVALID_FAULT_COMMAND", "RED",
                      "$.fault_injection.command.argv",
                      "exact non-empty argv array is required")
        exit_code = command.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            ctx.issue("INVALID_FAULT_COMMAND", "RED",
                      "$.fault_injection.command.exit_code",
                      "integer exit code is required")
        elif str(fault.get("status", "")).upper() in GREEN_WORDS and exit_code != 0:
            ctx.issue("FAULT_COMMAND_FAILED", "RED",
                      "$.fault_injection.command.exit_code",
                      "PASS fault harness command must exit zero")
        if isinstance(argv, list):
            tool_path = receipt.get("tool", {}).get("path") \
                if isinstance(receipt.get("tool"), dict) else None
            baseline_path = fault.get("baseline_artifact", {}).get("path") \
                if isinstance(fault.get("baseline_artifact"), dict) else None
            for label, required_path in (("tool", tool_path),
                                         ("baseline", baseline_path)):
                if isinstance(required_path, str) and required_path not in argv:
                    ctx.issue("UNBOUND_FAULT_COMMAND", "RED",
                              "$.fault_injection.command.argv",
                              f"exact command does not reference hash-bound {label} path")

    failures = fault.get("observed_failures")
    expected = fault.get("expected_failed_metrics")
    if not isinstance(failures, list) or not failures or not all(
            isinstance(value, str) and value for value in failures):
        ctx.issue("EMPTY_FAULT_INJECTION", "RED",
                  "$.fault_injection.observed_failures",
                  "the injected fault must cause at least one observed failure")
        failures = []
    if not isinstance(expected, list) or not expected or not all(
            isinstance(value, str) and value for value in expected):
        ctx.issue("MISSING_EXPECTED_FAULT_METRICS", "RED",
                  "$.fault_injection.expected_failed_metrics",
                  "non-empty expected metric-id array is required")
        expected = []
    if len(failures) != len(set(failures)) or len(expected) != len(set(expected)):
        ctx.issue("DUPLICATE_FAULT_METRIC_ID", "RED", "$.fault_injection",
                  "expected and observed metric ids must be unique")
    metric_ids = set(metric_leaf_paths(receipt.get("parsed_metrics", {})))
    for metric_id in sorted(set(failures) | set(expected)):
        if metric_id not in metric_ids:
            ctx.issue("UNKNOWN_FAULT_METRIC_ID", "RED", "$.fault_injection",
                      f"fault metric id is not a parsed metric leaf: {metric_id}")
    missing_failures = sorted(set(expected) - set(failures))
    if missing_failures:
        ctx.issue("EXPECTED_FAULT_NOT_OBSERVED", "RED",
                  "$.fault_injection.observed_failures",
                  f"expected failures not observed: {', '.join(missing_failures)}")

    baseline = fault.get("baseline_artifact")
    injected = fault.get("injected_artifact")
    input_hashes = {
        item.get("sha256") for item in receipt.get("inputs", [])
        if isinstance(item, dict) and isinstance(item.get("sha256"), str)
    }
    if isinstance(baseline, dict) and baseline.get("sha256") not in input_hashes:
        ctx.issue("UNBOUND_FAULT_BASELINE", "RED",
                  "$.fault_injection.baseline_artifact.sha256",
                  "baseline hash must match a receipt input hash")
    if isinstance(baseline, dict) and isinstance(injected, dict) \
            and baseline.get("sha256") == injected.get("sha256"):
        ctx.issue("FAULT_DID_NOT_CHANGE_ARTIFACT", "RED", "$.fault_injection",
                  "baseline and injected artifact hashes must differ")

    referenced_documents: dict[str, Any] = {}
    for name in ("fixture", "injected_artifact"):
        path = identity_paths.get(name)
        if path is None:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ctx.issue("INVALID_FAULT_ARTIFACT_JSON", "RED",
                      f"$.fault_injection.{name}", str(exc))
            continue
        if not isinstance(document, dict):
            ctx.issue("INVALID_FAULT_ARTIFACT_JSON", "RED",
                      f"$.fault_injection.{name}",
                      "fault artifact JSON root must be an object")
            continue
        referenced_documents[name] = document
    fixture_document = referenced_documents.get("fixture")
    if isinstance(fixture_document, dict) and \
            fixture_document.get("expected_failed_metrics") != expected:
        ctx.issue("FAULT_FIXTURE_EXPECTATION_MISMATCH", "RED",
                  "$.fault_injection.expected_failed_metrics",
                  "receipt expectations differ from hash-bound fixture JSON")
    injected_document = referenced_documents.get("injected_artifact")
    if isinstance(injected_document, dict):
        if injected_document.get("observed_failed_metrics") != failures:
            ctx.issue("FAULT_OBSERVATION_MISMATCH", "RED",
                      "$.fault_injection.observed_failures",
                      "receipt failures differ from hash-bound injected observation")
        baseline_sha = baseline.get("sha256") if isinstance(baseline, dict) else None
        if injected_document.get("baseline_sha256") != baseline_sha:
            ctx.issue("FAULT_BASELINE_OBSERVATION_MISMATCH", "RED",
                      "$.fault_injection.injected_artifact",
                      "injected observation is not bound to the baseline hash")
    if str(fault.get("status", "")).upper() not in GREEN_WORDS:
        ctx.issue("FAULT_INJECTION_FAILED", "RED", "$.fault_injection.status",
                  "fault-injection harness did not prove rejection")


def declared_verdict(receipt: dict[str, Any]) -> str | None:
    value = receipt.get("verdict", receipt.get("status"))
    return value.upper() if isinstance(value, str) else None


def validate_blockers(receipt: dict[str, Any], ctx: Context) -> None:
    blockers = require_list(receipt, "blockers", ctx)
    if blockers is None:
        return
    for index, blocker in enumerate(blockers):
        base = f"$.blockers[{index}]"
        if not isinstance(blocker, dict):
            ctx.issue("INVALID_BLOCKER", "ERROR", base, "blocker must be an object")
            continue
        require_text(blocker, "code", ctx, base)
        require_text(blocker, "message", ctx, base)
    if blockers and declared_verdict(receipt) in GREEN_WORDS:
        ctx.issue("OPEN_BLOCKERS", "RED", "$.blockers",
                  "GREEN is impossible while blockers remain open")


def validate_children(receipt: dict[str, Any], ctx: Context) -> None:
    children = receipt.get("child_receipts", [])
    if not isinstance(children, list):
        ctx.issue("INVALID_CHILD_RECEIPTS", "ERROR", "$.child_receipts",
                  "array is required")
        return
    for index, item in enumerate(children):
        base = f"$.child_receipts[{index}]"
        if not isinstance(item, dict):
            ctx.issue("INVALID_CHILD_RECEIPT", "ERROR", base,
                      "child identity must be an object")
            continue
        mandatory = item.get("mandatory", True)
        if not isinstance(mandatory, bool):
            ctx.issue("INVALID_CHILD_MODE", "ERROR", f"{base}.mandatory",
                      "boolean is required")
            mandatory = True
        path = validate_hash_artifact(item, ctx, base)
        if path is None:
            continue
        if path in ctx.seen:
            ctx.issue("CHILD_CYCLE", "ERROR", base,
                      f"receipt cycle includes {path}")
            continue
        try:
            child_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ctx.issue("INVALID_CHILD_JSON", "ERROR", base, str(exc))
            continue
        if not isinstance(child_data, dict):
            ctx.issue("INVALID_CHILD_JSON", "ERROR", base,
                      "child receipt root must be an object")
            continue
        declared = declared_verdict(child_data)
        identity_declared = item.get("verdict")
        if isinstance(identity_declared, str) and declared != identity_declared.upper():
            ctx.issue("CHILD_VERDICT_MISMATCH", "STALE", f"{base}.verdict",
                      f"identity says {identity_declared}, child says {declared}")
        child_ctx = Context(path, ctx.root, ctx.require_fault,
                            seen=set(ctx.seen) | {path})
        child_report = validate_receipt(child_data, child_ctx)
        if mandatory and child_report["effective_status"] != "GREEN":
            ctx.issue("MANDATORY_CHILD_RED", "RED", base,
                      f"mandatory child effective status is "
                      f"{child_report['effective_status']}")
        elif not mandatory and child_report["effective_status"] != "GREEN":
            ctx.warn("ADVISORY_CHILD_RED", base,
                     f"advisory child is {child_report['effective_status']}")


def validate_retention(receipt: dict[str, Any], ctx: Context) -> None:
    retention = require_mapping(receipt, "retention", ctx)
    if retention is None:
        return
    policy = require_text(retention, "policy", ctx, "$.retention")
    expires = retention.get("expires_utc")
    if policy != "indefinite" and not is_utc(expires):
        ctx.issue("INVALID_RETENTION_EXPIRY", "ERROR", "$.retention.expires_utc",
                  "UTC expiry is required unless policy is 'indefinite'")
    elif policy != "indefinite" and is_utc(receipt.get("end_utc")):
        expiry_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(receipt["end_utc"]).replace("Z", "+00:00"))
        if expiry_dt <= end_dt:
            ctx.issue("RETENTION_EXPIRES_BEFORE_AUDIT", "RED",
                      "$.retention.expires_utc",
                      "evidence expiry must be later than receipt completion")
    retain = retention.get("retain")
    if not isinstance(retain, list) or not all(isinstance(x, str) for x in retain):
        ctx.issue("INVALID_RETENTION_SET", "ERROR", "$.retention.retain",
                  "string array is required")
        return
    needed = GREEN_RETENTION if declared_verdict(receipt) in GREEN_WORDS else RED_RETENTION
    missing = sorted(needed - set(retain))
    if missing:
        ctx.issue("INCOMPLETE_RETENTION", "RED", "$.retention.retain",
                  f"missing required retained evidence: {', '.join(missing)}")


def validate_promotion(receipt: dict[str, Any], ctx: Context) -> None:
    gate_kind = str(receipt.get("gate_kind", "")).lower()
    if gate_kind != "promotion" and "promotion" not in receipt:
        return
    promotion = require_mapping(receipt, "promotion", ctx)
    if promotion is None:
        return
    decision = require_text(promotion, "decision", ctx, "$.promotion")
    require_text(promotion, "scratch_path", ctx, "$.promotion")
    reasons = require_list(promotion, "reason_codes", ctx, "$.promotion")
    commands = require_list(promotion, "commands", ctx, "$.promotion")
    sources = require_list(promotion, "source_artifacts", ctx, "$.promotion")
    targets = require_list(promotion, "target_artifacts", ctx, "$.promotion")
    for name, records in (("source_artifacts", sources), ("target_artifacts", targets)):
        if records is not None:
            if not records:
                ctx.issue("MISSING_PROMOTION_ARTIFACT", "ERROR",
                          f"$.promotion.{name}", "at least one artifact is required")
            for index, item in enumerate(records):
                validate_hash_artifact(item, ctx,
                                       f"$.promotion.{name}[{index}]",
                                       require_role=True)
    if commands is not None:
        if not commands:
            ctx.issue("MISSING_PROMOTION_COMMAND", "ERROR",
                      "$.promotion.commands", "exact commands are required")
        for index, command in enumerate(commands):
            base = f"$.promotion.commands[{index}]"
            if not isinstance(command, dict):
                ctx.issue("INVALID_PROMOTION_COMMAND", "ERROR", base,
                          "command must be an object")
                continue
            argv = command.get("argv")
            if not isinstance(argv, list) or not argv or not all(
                    isinstance(x, str) for x in argv):
                ctx.issue("INVALID_PROMOTION_COMMAND", "ERROR", f"{base}.argv",
                          "exact non-empty argv array is required")
            if not isinstance(command.get("exit_code"), int):
                ctx.issue("INVALID_PROMOTION_COMMAND", "ERROR",
                          f"{base}.exit_code", "integer is required")
    if reasons is not None and not all(isinstance(reason, str) and reason.strip()
                                       for reason in reasons):
        ctx.issue("INVALID_RED_REASONS", "ERROR", "$.promotion.reason_codes",
                  "reason codes must be non-empty strings")
    upper = decision.upper() if isinstance(decision, str) else ""
    if upper == "RED":
        if isinstance(reasons, list) and not reasons:
            ctx.issue("MISSING_RED_REASONS", "RED", "$.promotion.reason_codes",
                      "RED promotion requires reason codes")
        for index, target in enumerate(targets or []):
            if not isinstance(target, dict):
                continue
            base = f"$.promotion.target_artifacts[{index}]"
            before = target.get("before_sha256")
            after = target.get("after_sha256")
            current = target.get("sha256")
            if not isinstance(before, str) or not SHA256_RE.fullmatch(before):
                ctx.issue("MISSING_RED_CANONICAL_HASH_PROOF", "RED",
                          f"{base}.before_sha256",
                          "RED promotion requires canonical before_sha256")
            if not isinstance(after, str) or not SHA256_RE.fullmatch(after):
                ctx.issue("MISSING_RED_CANONICAL_HASH_PROOF", "RED",
                          f"{base}.after_sha256",
                          "RED promotion requires canonical after_sha256")
            if isinstance(after, str) and SHA256_RE.fullmatch(after) \
                    and isinstance(current, str) and after != current:
                ctx.issue("RED_PROMOTION_CURRENT_HASH_MISMATCH", "STALE",
                          f"{base}.after_sha256",
                          "after_sha256 must equal the hash-bound current artifact")
            if isinstance(before, str) and SHA256_RE.fullmatch(before) \
                    and isinstance(after, str) and SHA256_RE.fullmatch(after) \
                    and before != after:
                ctx.issue("RED_PROMOTION_MODIFIED_CANONICAL", "RED", base,
                          "RED promotion changed the canonical artifact")
    if upper == "GREEN":
        if isinstance(reasons, list) and reasons:
            ctx.issue("GREEN_WITH_RED_REASONS", "RED",
                      "$.promotion.reason_codes",
                      "GREEN promotion cannot contain RED reasons")
        if declared_verdict(receipt) not in GREEN_WORDS:
            ctx.issue("PROMOTION_VERDICT_MISMATCH", "RED", "$.promotion.decision",
                      "promotion GREEN requires receipt GREEN")
        if any(isinstance(cmd, dict) and cmd.get("exit_code") != 0
               for cmd in (commands or [])):
            ctx.issue("PROMOTION_COMMAND_FAILED", "RED", "$.promotion.commands",
                      "all promotion commands must exit zero")
        atomic = promotion.get("atomic") is True or any(
            isinstance(cmd, dict) and "--atomic" in cmd.get("argv", [])
            for cmd in (commands or [])
        )
        if not atomic:
            ctx.issue("MISSING_ATOMIC_PROMOTION_EVIDENCE", "RED", "$.promotion",
                      "GREEN promotion requires atomic=true or an exact --atomic command")
    elif upper != "RED":
        ctx.issue("INVALID_PROMOTION_DECISION", "ERROR", "$.promotion.decision",
                  "decision must be GREEN or RED")


def normalize_schema(receipt: dict[str, Any], ctx: Context) -> None:
    raw_version = receipt.get("schema_version")
    raw_alias = receipt.get("schema")
    normalized_version = LEGACY_SCHEMA_MAP.get(raw_version, raw_version)
    normalized_alias = LEGACY_SCHEMA_MAP.get(raw_alias, raw_alias)
    if raw_version is not None and raw_alias is not None \
            and normalized_version != normalized_alias:
        ctx.issue("CONFLICTING_SCHEMA_VERSION", "ERROR", "$.schema_version",
                  "schema and schema_version disagree")
    if "schema_version" not in receipt and "schema" in receipt:
        receipt["schema_version"] = receipt["schema"]
        ctx.warn("LEGACY_SCHEMA_ALIAS", "$.schema",
                 "legacy schema spelling normalized to schema_version")
    version = receipt.get("schema_version")
    if version in LEGACY_SCHEMA_MAP:
        receipt["source_schema_version"] = version
        receipt["schema_version"] = LEGACY_SCHEMA_MAP[version]
        ctx.warn("LEGACY_SCHEMA_VERSION", "$.schema_version",
                 f"legacy {version} normalized to {SCHEMA_VERSION}")
    version = receipt.get("schema_version")
    if not isinstance(version, str):
        ctx.issue("MISSING_SCHEMA_VERSION", "ERROR", "$.schema_version",
                  "schema version is required")
    elif version not in SUPPORTED_SCHEMA_VERSIONS:
        ctx.issue("UNSUPPORTED_SCHEMA_VERSION", "ERROR", "$.schema_version",
                  f"unsupported schema: {version}")


def effective_status(receipt: dict[str, Any], issues: list[Issue]) -> str:
    statuses = {issue.status for issue in issues}
    for status in ("ERROR", "STALE", "INVALID_METRIC", "NO_COVERAGE", "RED"):
        if status in statuses:
            return status
    declared = declared_verdict(receipt)
    if declared in GREEN_WORDS:
        return "GREEN"
    if declared in {"STALE", "INVALID_METRIC", "NO_COVERAGE", "ERROR"}:
        return declared
    return "RED"


def validate_receipt(receipt: dict[str, Any], ctx: Context) -> dict[str, Any]:
    normalize_schema(receipt, ctx)
    require_text(receipt, "gate_id", ctx)
    if not any(isinstance(receipt.get(key), str) and receipt[key].strip()
               for key in ("gate_kind", "profile")):
        ctx.issue("MISSING_GATE_KIND", "ERROR", "$.gate_kind",
                  "gate_kind or profile is required")
    verdict = declared_verdict(receipt)
    if verdict is None or verdict not in GREEN_WORDS | RED_WORDS:
        ctx.issue("INVALID_VERDICT", "ERROR", "$.verdict",
                  "GREEN/PASS or an explicit RED status is required")
    for key in ("start_utc", "end_utc"):
        if not is_utc(receipt.get(key)):
            ctx.issue("INVALID_TIMESTAMP", "ERROR", f"$.{key}",
                      "zero-offset UTC timestamp is required")
    if is_utc(receipt.get("start_utc")) and is_utc(receipt.get("end_utc")):
        start_dt = datetime.fromisoformat(
            str(receipt["start_utc"]).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(
            str(receipt["end_utc"]).replace("Z", "+00:00"))
        if end_dt < start_dt:
            ctx.issue("INVALID_TIME_RANGE", "ERROR", "$.end_utc",
                      "end_utc must not precede start_utc")
    validate_tool(receipt, ctx)
    validate_inputs(receipt, ctx)
    validate_config(receipt, ctx)
    validate_command_semantics(receipt, ctx)
    validate_metric_semantics(receipt, ctx)
    validate_coverage(receipt, ctx)
    validate_blockers(receipt, ctx)
    validate_fault_injection(receipt, ctx)
    validate_children(receipt, ctx)
    validate_promotion(receipt, ctx)
    validate_retention(receipt, ctx)

    status = effective_status(receipt, ctx.issues)
    if verdict in GREEN_WORDS and isinstance(receipt.get("exit_code"), int) \
            and receipt["exit_code"] != 0:
        ctx.issue("NONZERO_GREEN_EXIT", "RED", "$.exit_code",
                  "GREEN receipt cannot have a nonzero gate exit")
        status = effective_status(receipt, ctx.issues)
    if verdict in GREEN_WORDS and status != "GREEN":
        ctx.issue("FALSE_GREEN", "RED", "$.verdict",
                  f"declared GREEN reduced to {status}")
        status = effective_status(receipt, ctx.issues)

    reason_codes = {issue.code for issue in ctx.issues}
    if verdict not in GREEN_WORDS and verdict is not None:
        reason_codes.add(f"DECLARED_{verdict}")
    # ``receipt.get("blockers", [])`` is NOT enough: a literal ``"blockers":
    # null`` is a present key, so the default never fires and the loop used to
    # raise TypeError. skills/pcb-orchestrator/references/gate_receipt.schema.json
    # explicitly permits ["array","null"], so a receipt written against the
    # published schema crashed the validator with a traceback instead of being
    # reported as the ERROR validate_blockers already recorded for it. A gate
    # that dies is not a gate that said no. Measured 2026-08-10.
    for blocker in receipt.get("blockers") or []:
        if isinstance(blocker, dict) and isinstance(blocker.get("code"), str):
            reason_codes.add(blocker["code"])
    promotion = receipt.get("promotion")
    if isinstance(promotion, dict):
        promotion_reasons = promotion.get("reason_codes")
        if isinstance(promotion_reasons, list):
            for reason in promotion_reasons:
                if isinstance(reason, str) and reason.strip():
                    reason_codes.add(reason)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "receipt_schema_version": receipt.get("schema_version"),
        "source_schema_version": receipt.get("source_schema_version"),
        "receipt": str(ctx.receipt_path),
        "gate_id": receipt.get("gate_id"),
        "declared_verdict": verdict,
        "effective_status": status,
        "reason_codes": sorted(reason_codes),
        "issues": [issue.as_dict() for issue in ctx.issues],
        "warnings": ctx.warnings,
        "summary": {
            "issues": len(ctx.issues),
            "warnings": len(ctx.warnings),
            "green": status == "GREEN",
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path, help="gate_receipt.json")
    parser.add_argument("--root", type=Path,
                        help="confinement root for every receipt/artifact path "
                             "(default: receipt directory)")
    parser.add_argument("--json", type=Path, dest="json_path",
                        help="also write the validator report to this path")
    parser.add_argument("--require-fault-injection", action="store_true",
                        help="require positive fault-injection evidence")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.receipt.resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("receipt root must be a JSON object")
        root = args.root.resolve() if args.root else path.parent
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"receipt path escapes validation root {root}: {path}") from exc
        ctx = Context(path, root, args.require_fault_injection, seen={path})
        report = validate_receipt(data, ctx)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "receipt": str(path),
            "effective_status": "ERROR",
            "reason_codes": ["INPUT_ERROR"],
            "issues": [{
                "code": "INPUT_ERROR", "status": "ERROR", "path": "$",
                "message": str(exc),
            }],
            "warnings": [],
            "summary": {"issues": 1, "warnings": 0, "green": False},
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_path:
        try:
            args.json_path.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            report = {
                "schema_version": REPORT_SCHEMA_VERSION,
                "receipt": str(path),
                "effective_status": "ERROR",
                "reason_codes": ["REPORT_WRITE_ERROR"],
                "issues": [{
                    "code": "REPORT_WRITE_ERROR",
                    "status": "ERROR",
                    "path": "$.validator_output",
                    "message": f"cannot write {args.json_path}: {exc}",
                }],
                "warnings": [],
                "prior_effective_status": report.get("effective_status"),
                "summary": {"issues": 1, "warnings": 0, "green": False},
            }
            rendered = json.dumps(report, ensure_ascii=False, indent=2,
                                  sort_keys=True)
            print(rendered)
            return 2
    print(rendered)
    status = report["effective_status"]
    if status == "GREEN":
        return 0
    if status == "ERROR":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
