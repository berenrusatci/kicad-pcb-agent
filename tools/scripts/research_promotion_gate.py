#!/usr/bin/env python3
"""Audit research-to-skill promotion evidence without mutating the vault.

The gate is intentionally conservative.  It does not decide whether an
electrical claim is true and it never edits a SKILL file.  It checks whether a
claim already marked KABUL has the traceability required by this vault:

* an HTTPS source and a recognizable authoritative source host;
* document identity/revision or publication date and a section/page locator;
* an access date;
* A/B/C/D verification level and L0/L1/L2/L3/stale loop;
* an existing vault-relative validation fixture/validator bound by SHA-256 and
  an existing target artifact;
* an explicit entry in both the decision log and evidence map.

Version 2 also binds the exact invocation, running tool, registry record and
all research/governance inputs into a tamper-evident receipt.  A stored report
can be checked against the current bindings with ``--verify-report``.

Exit codes: 0 = GREEN, 1 = RED/STALE, 2 = malformed input or execution error.

Governance references:
* RFC 8785, JSON Canonicalization Scheme, June 2020, sections 3.1-3.2.
* SLSA v1.2, Source Requirements, ``subject.digest`` requirements.
* ProjectE ``MOCs/MOC-Skills.md`` promotion rule.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.hashing import canonical_digest, sha256_file as sha256_path  # noqa: E402
from common.paths import PathConfinementError, confine_to_dir  # noqa: E402
from common.report_io import (  # noqa: E402
    ReportWriteError,
    atomic_write_text,
    diag,
    emit,
    run_cli,
)


SCHEMA_VERSION = "pcb-research-promotion-report/v2"
ACCEPT_NOTE_STATUSES = {"kabul-edildi", "accepted"}
KNOWN_ROW_DECISIONS = {
    "aday", "candidate", "confirm", "kabul", "accepted", "red", "ret",
    "rejected", "bayat", "stale",
}
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
ACCESS_DATE_RE = re.compile(
    r"\b(?:erişim(?:\s+tarihi)?|accessed(?:\s+at)?)\s*[:=]?\s*"
    r"(20\d{2}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https://[^\s)>\]]+")
MARKDOWN_URL_RE = re.compile(r"\[[^\]]+\]\((https://[^)]+)\)")
LOCATOR_RE = re.compile(
    r"(?:\b(?:section|chapter|appendix|table|figure|page|pages|bölüm|ek|tablo|"
    r"şekil|sayfa|PDF\s+s\.?|s\.)\s*['\"“”‘’]?[A-Z0-9.\-–—]+|"
    r"§\s*['\"“”‘’]?[A-Z0-9.\-–—]+)",
    re.IGNORECASE,
)
DOC_ID_RE = re.compile(
    r"(?:\bRFC\s*\d+\b|\b(?:IPC|IEC|IEEE|JEDEC)[-\s]\d[\w.\-]*\b|"
    r"\b(?:SPR|SL|TIDU|FPGA-(?:TN|DS|AN)|XAPP|UG)\w[\w.\-]*\b|"
    r"\b(?:rev(?:ision)?|version|versiyon|ed(?:ition)?|baskı)\b\.?\s*"
    r"[A-Z0-9][\w.\-]*|\b20\d{2}\b)",
    re.IGNORECASE,
)
LEVEL_RE = re.compile(r"(?:^|[^A-Z])([ABCD])(?:$|[^A-Z])")
LOOP_RE = re.compile(r"\b(L[0-3]|stale|bayat(?:lama)?)\b", re.IGNORECASE)
SKILL_RE = re.compile(r"SKILL-[a-z0-9-]+", re.IGNORECASE)
SHA256_RE = re.compile(r"\b(?:sha256\s*[:=]\s*)?([0-9a-f]{64})\b", re.IGNORECASE)
VALIDATION_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
    r"\.(?:json|md|py|sh|yaml|yml|txt|kicad_sch|kicad_pcb))"
)
VALIDATION_PLACEHOLDERS = {
    "", "-", "?", "tbd", "n/a", "na", "manual", "manuel", "elle",
    "confirm", "no_coverage", "no coverage",
}

# A domain match is a screening aid, not proof of authority.  Unknown hosts are
# rejected for accepted claims until a human adds them to this reviewed list.
AUTHORITATIVE_SUFFIXES = (
    "analog.com", "amd.com", "ansys.com", "hirose.com", "ibis.org",
    "iec.ch", "ieee.org", "intel.com", "ipc.org", "jedec.org",
    "jlcpcb.com", "json-schema.org", "keysight.com", "kicad.org",
    "git-scm.com", "ietf.org", "latticesemi.com", "mipi.org",
    "ncabgroup.com", "nexperia.com",
    "nxp.com", "omnivision-group.com", "pcbway.com", "polarinstruments.com",
    "rfc-editor.org", "rogerscorp.com", "slsa.dev", "ti.com",
    "we-online.com", "w3.org", "xjtag.com",
)


@dataclass(frozen=True)
class Issue:
    code: str
    status: str
    claim_id: str
    note: str
    message: str


def normalize_header(value: str) -> str:
    table = str.maketrans({
        "İ": "i", "I": "i", "Ş": "s", "Ğ": "g", "Ü": "u",
        "Ö": "o", "Ç": "c", "ı": "i", "ş": "s", "ğ": "g",
        "ü": "u", "ö": "o", "ç": "c",
    })
    return re.sub(r"[^a-z0-9]+", "_", value.translate(table).casefold()).strip("_")


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def split_markdown_row(line: str) -> list[str]:
    # Vault claim tables do not place unescaped pipes inside cells.  Keeping the
    # parser deliberately small avoids accepting arbitrary Markdown as schema.
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    cells = split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def claim_rows_with_errors(text: str) -> tuple[list[dict[str, str]], list[int]]:
    lines = text.splitlines()
    rows: list[dict[str, str]] = []
    malformed: list[int] = []
    index = 0
    while index + 1 < len(lines):
        if "|" not in lines[index] or not is_separator(lines[index + 1]):
            index += 1
            continue
        raw_headers = split_markdown_row(lines[index])
        headers = [normalize_header(cell) for cell in raw_headers]
        if "claim_id" not in headers:
            index += 2
            continue
        index += 2
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            cells = split_markdown_row(lines[index])
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
            else:
                malformed.append(index + 1)
            index += 1
    return rows, malformed


def claim_rows(text: str) -> list[dict[str, str]]:
    return claim_rows_with_errors(text)[0]


def all_table_rows(text: str) -> list[dict[str, str]]:
    """Parse every well-formed Markdown table, not only claim tables."""
    lines = text.splitlines()
    rows: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(lines):
        if "|" not in lines[index] or not is_separator(lines[index + 1]):
            index += 1
            continue
        headers = [normalize_header(c) for c in split_markdown_row(lines[index])]
        index += 2
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            cells = split_markdown_row(lines[index])
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
            index += 1
    return rows


def cell(row: dict[str, str], *names: str) -> str:
    for name in names:
        key = normalize_header(name)
        if row.get(key, "").strip():
            return row[key].strip()
    return ""


def extract_urls(value: str) -> list[str]:
    urls = MARKDOWN_URL_RE.findall(value)
    urls.extend(URL_RE.findall(value))
    cleaned = [url.rstrip(".,;:") for url in urls]
    return list(dict.fromkeys(cleaned))


def authoritative(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "gitlab.com" and "/kicad/" in urllib.parse.urlparse(url).path:
        return True
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in AUTHORITATIVE_SUFFIXES)


def find_access_date(source: str) -> str | None:
    # Claim-scoped by design: another row's date or a note-wide frontmatter
    # date must not make this source look fresh.
    match = ACCESS_DATE_RE.search(source)
    return match.group(1) if match else None


def source_without_access_date(value: str) -> str:
    """Remove access timestamps before testing document identity.

    An access date proves when the source was read; it is not a publication
    date or revision.  Treating ``erişim 2026-...`` as the document's 2026
    identity would create a false GREEN for undated live pages.
    """
    return ACCESS_DATE_RE.sub("", value)


def exact_claim_cell(row: dict[str, str]) -> str:
    return cell(row, "claim_id", "claim").strip().strip("`")


def validation_issues(vault: Path, validation: str) -> list[tuple[str, str]]:
    """Return fail-closed validation defects for a claim-table cell.

    A reproducible validation is a vault-relative existing fixture/validator
    path plus the exact SHA-256 of that file.  Prose and manual placeholders
    are visibility states, not executable evidence.
    """
    normalized = normalize_header(validation)
    if validation.strip().casefold() in VALIDATION_PLACEHOLDERS or normalized in {
        normalize_header(item) for item in VALIDATION_PLACEHOLDERS
    }:
        return [("VALIDATION_PLACEHOLDER", "validation is TBD/CONFIRM/NO_COVERAGE")]
    paths = list(dict.fromkeys(VALIDATION_PATH_RE.findall(validation)))
    if not paths:
        return [("VALIDATION_PATH_MISSING", "validation needs a vault-relative fixture/validator path")]
    digests = [match.casefold() for match in SHA256_RE.findall(validation)]
    if not digests:
        return [("VALIDATION_SHA256_MISSING", "validation needs the fixture/validator SHA-256")]
    defects: list[tuple[str, str]] = []
    matched_digest = False
    vault_resolved = vault.resolve()
    for raw_path in paths:
        candidate = (vault / raw_path).resolve(strict=False)
        if not candidate.is_relative_to(vault_resolved) or not candidate.is_file():
            defects.append(("VALIDATION_PATH_NOT_FOUND", f"validation path does not resolve: {raw_path}"))
            continue
        actual = sha256_path(candidate)
        if actual in digests:
            matched_digest = True
        else:
            defects.append(("VALIDATION_SHA256_MISMATCH", f"SHA-256 mismatch: {raw_path}"))
    if not matched_digest and not defects:
        defects.append(("VALIDATION_SHA256_MISMATCH", "no validation path matches the supplied SHA-256"))
    return defects


def target_token_exists(vault: Path, token: str) -> bool:
    token = token.strip().strip("`").strip()
    if not token:
        return False
    if re.fullmatch(r"SKILL-[a-z0-9-]+", token, re.IGNORECASE):
        return (vault / "Skills" / f"{token}.md").is_file()
    if token.endswith(".py") and "/" not in token:
        return (vault / "Skills" / "scripts" / token).is_file()
    if "/" in token or Path(token).suffix:
        return (vault / token).is_file()
    if token.startswith(("02-", "03-")):
        return (vault / f"{token}.md").is_file()
    return False


def existing_target(vault: Path, target: str) -> bool:
    # Every comma-separated target must resolve.  A valid first token cannot
    # conceal a missing second artifact.
    tokens = [part.strip() for part in target.split(",") if part.strip()]
    return bool(tokens) and all(target_token_exists(vault, token) for token in tokens)


def note_is_accepted(frontmatter: dict[str, str]) -> bool:
    return frontmatter.get("durum", "").strip().casefold() in ACCEPT_NOTE_STATUSES


def decision_head(value: str) -> str:
    return value.strip().casefold().split(",", 1)[0].strip()


def row_is_accepted(row: dict[str, str], accepted_note: bool) -> bool:
    decision = cell(row, "karar", "decision")
    if decision:
        return decision_head(decision) in {"kabul", "accepted"}
    return accepted_note


def evidence_has_claim(rows: list[dict[str, str]], claim_id: str) -> bool:
    return any(exact_claim_cell(row) == claim_id for row in rows)


def decision_has_accepted_claim(rows: list[dict[str, str]], claim_id: str) -> bool:
    for row in rows:
        if exact_claim_cell(row) != claim_id:
            continue
        decision = cell(row, "karar", "decision", "status")
        if decision_head(decision) in {"kabul", "accepted"}:
            return True
    return False


def check_claim(
    *, vault: Path, note_path: Path, frontmatter: dict[str, str],
    row: dict[str, str], evidence_rows: list[dict[str, str]],
    decision_rows: list[dict[str, str]],
    max_source_age_days: int | None,
) -> tuple[dict[str, object], list[Issue]]:
    claim_id = cell(row, "claim_id") or "<missing>"
    source = cell(row, "kaynak", "resmi_kaynak", "source")
    validation = cell(row, "dogrulama", "validation")
    target = cell(row, "hedef", "target")
    level_loop = " ".join((
        cell(row, "seviye", "sev", "level"),
        cell(row, "dongu", "loop"),
        cell(row, "seviye_dongu", "level_loop"),
    ))
    accepted = row_is_accepted(row, note_is_accepted(frontmatter))
    urls = extract_urls(source)
    accessed = find_access_date(source)
    issues: list[Issue] = []

    def issue(code: str, status: str, message: str) -> None:
        # Candidate rows are reported but cannot block promotion because they
        # are not yet promoted.  Accepted rows preserve the requested status.
        effective = status if accepted else "ADVISORY"
        issues.append(Issue(code, effective, claim_id,
                            str(note_path.relative_to(vault)), message))

    if claim_id == "<missing>":
        issue("MISSING_CLAIM_ID", "ERROR", "claim_id is required")
    decision = cell(row, "karar", "decision")
    if decision and decision_head(decision) not in KNOWN_ROW_DECISIONS:
        all_note = str(note_path.relative_to(vault))
        issues.append(Issue(
            "UNKNOWN_DECISION_STATUS", "ERROR", claim_id, all_note,
            f"unsupported decision status: {decision}",
        ))
    if not urls:
        issue("MISSING_HTTPS_SOURCE", "RED", "no HTTPS source in source cell")
    for url in urls:
        if not authoritative(url):
            issue("UNCONFIRMED_SOURCE_AUTHORITY", "RED",
                  f"source host is not in reviewed authority list: {url}")
    if not LOCATOR_RE.search(source):
        issue("MISSING_SOURCE_LOCATOR", "RED",
              "source needs section/table/figure/page locator in the claim row")
    if not DOC_ID_RE.search(source_without_access_date(source)):
        issue("MISSING_DOCUMENT_ID_OR_REVISION", "RED",
              "source needs document number/revision/publication date")
    if not accessed:
        issue("MISSING_ACCESS_DATE", "STALE",
              "source access date must be explicit in this claim's source cell")
    else:
        try:
            accessed_date = date.fromisoformat(accessed)
            age = (date.today() - accessed_date).days
            if age < 0:
                issue("FUTURE_ACCESS_DATE", "STALE",
                      f"source access date is in the future: {accessed}")
            elif max_source_age_days is not None and age > max_source_age_days:
                issue("SOURCE_AGE_EXCEEDED", "STALE",
                      f"source is {age} days old; policy limit is "
                      f"{max_source_age_days} days")
        except ValueError:
            issue("INVALID_ACCESS_DATE", "ERROR", f"invalid date: {accessed}")
    if not validation:
        issue("MISSING_REPRODUCIBLE_VALIDATION", "RED",
              "validation/fixture statement is required")
    else:
        for code, message in validation_issues(vault, validation):
            issue(code, "RED", message)
    if not target:
        issue("MISSING_TARGET", "RED", "target skill/artifact is required")
    elif not existing_target(vault, target):
        issue("TARGET_NOT_FOUND", "RED", f"target does not resolve: {target}")
    if not LEVEL_RE.search(level_loop):
        issue("MISSING_VERIFICATION_LEVEL", "RED", "A/B/C/D level is required")
    if not LOOP_RE.search(level_loop):
        issue("MISSING_VERIFICATION_LOOP", "RED", "L0-L3/stale loop is required")
    if claim_id != "<missing>" and not evidence_has_claim(evidence_rows, claim_id):
        issue("MISSING_EVIDENCE_MAP_ENTRY", "RED",
              "claim_id is absent from 03-Kanit-ve-Kaynak-Haritasi.md")
    if claim_id != "<missing>" and not decision_has_accepted_claim(
            decision_rows, claim_id):
        issue("MISSING_DECISION_LOG_ENTRY", "RED",
              "claim_id with accepted decision is absent from 02-Karar-Gunlugu.md")

    return ({
        "claim_id": claim_id,
        "note": str(note_path.relative_to(vault)),
        "accepted": accepted,
        "urls": urls,
        "accessed_at": accessed,
        "target": target,
        "verification": level_loop.strip(),
        "issue_codes": [item.code for item in issues],
    }, issues)


def live_check(url: str, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": "ProjectE-research-promotion-gate/1",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"url": url, "status": response.status, "ok": response.status < 400}
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405):
            # Some authoritative document servers reject HEAD.  A small range
            # GET distinguishes that policy from a dead URL without downloading
            # an entire PDF.
            get = urllib.request.Request(url, headers={
                "User-Agent": "ProjectE-research-promotion-gate/1",
                "Range": "bytes=0-0",
            })
            try:
                with urllib.request.urlopen(get, timeout=timeout) as response:
                    return {"url": url, "status": response.status,
                            "ok": response.status < 400, "fallback": "GET range"}
            except (urllib.error.URLError, TimeoutError, OSError) as inner:
                return {"url": url, "status": getattr(inner, "code", None),
                        "ok": False, "error": str(inner)}
        return {"url": url, "status": exc.code, "ok": False, "error": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"url": url, "status": getattr(exc, "code", None),
                "ok": False, "error": str(exc)}


def build_receipt(
    vault: Path, note_paths: list[Path], invocation_argv: list[str],
    policy: dict[str, object],
) -> tuple[dict[str, object], list[Issue]]:
    tool_path = Path(__file__).resolve()
    registry_path = vault / "Config" / "tool_registry.json"
    inputs = []
    for role, path in (
        ("decision_log", vault / "02-Karar-Gunlugu.md"),
        ("evidence_map", vault / "03-Kanit-ve-Kaynak-Haritasi.md"),
    ):
        inputs.append({
            "role": role,
            "path": str(path.relative_to(vault)),
            "sha256": sha256_path(path),
        })
    for path in note_paths:
        inputs.append({
            "role": "research_note",
            "path": str(path.relative_to(vault)),
            "sha256": sha256_path(path),
        })

    issues: list[Issue] = []
    registry_record: dict[str, object] | None = None
    registry_sha256: str | None = None
    if not registry_path.is_file():
        issues.append(Issue(
            "RECEIPT_REGISTRY_MISSING", "ERROR", "<receipt>",
            "Config/tool_registry.json", "tool registry is required",
        ))
    else:
        registry_sha256 = sha256_path(registry_path)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        matches = [item for item in registry.get("tools", [])
                   if item.get("name") == "research-promotion-gate"]
        if len(matches) != 1:
            issues.append(Issue(
                "RECEIPT_REGISTRY_RECORD_INVALID", "ERROR", "<receipt>",
                "Config/tool_registry.json",
                "research-promotion-gate needs exactly one registry record",
            ))
        else:
            registry_record = matches[0]

    tool = {
        "path": str(tool_path),
        "sha256": sha256_path(tool_path),
        "schema_version": SCHEMA_VERSION,
    }
    if registry_record is not None and registry_record.get("sha256") != tool["sha256"]:
        issues.append(Issue(
            "RECEIPT_TOOL_REGISTRY_HASH_MISMATCH", "ERROR", "<receipt>",
            "Config/tool_registry.json",
            "registry SHA-256 does not match the running tool",
        ))
    if registry_record is not None and registry_record.get("version") != SCHEMA_VERSION:
        issues.append(Issue(
            "RECEIPT_TOOL_REGISTRY_VERSION_MISMATCH", "ERROR", "<receipt>",
            "Config/tool_registry.json",
            "registry version does not match the report schema version",
        ))

    receipt: dict[str, object] = {
        "argv": invocation_argv,
        "tool": tool,
        "registry": {
            "path": "Config/tool_registry.json",
            "sha256": registry_sha256,
            "record": registry_record,
        },
        "inputs": inputs,
    }
    receipt["binding_sha256"] = canonical_digest({
        "tool": receipt["tool"],
        "registry": receipt["registry"],
        "inputs": receipt["inputs"],
        "policy": policy,
    })
    return receipt, issues


def report_payload_digest(report: dict[str, object]) -> str:
    copied = json.loads(json.dumps(report, ensure_ascii=False))
    copied.get("receipt", {}).pop("payload_sha256", None)
    return canonical_digest(copied)


def recompute_report_status(report: dict[str, object]) -> None:
    issues = report.get("issues", [])
    blocking = [item for item in issues
                if item.get("status") in {"RED", "STALE", "ERROR"}]
    trace = [item for item in blocking if item.get("status") in {"RED", "ERROR"}]
    stale = [item for item in blocking if item.get("status") == "STALE"]
    trace_status = (
        "ERROR" if any(item.get("status") == "ERROR" for item in trace)
        else "RED" if trace else "GREEN"
    )
    freshness_status = "STALE" if stale else "GREEN"
    report["trace_status"] = trace_status
    report["freshness_status"] = freshness_status
    report["effective_status"] = (
        "ERROR" if trace_status == "ERROR"
        else "RED" if trace_status == "RED"
        else freshness_status
    )
    report["summary"]["blocking_issues"] = len(blocking)
    report["summary"]["advisories"] = sum(
        item.get("status") == "ADVISORY" for item in issues
    )
    report["receipt"]["payload_sha256"] = report_payload_digest(report)


def stored_report_issues(vault: Path, stored_path: Path,
                         current: dict[str, object]) -> list[Issue]:
    path = validated_report_path(vault, stored_path)
    if not path.is_file():
        return [Issue("STORED_REPORT_MISSING", "STALE", "<receipt>",
                      str(path), "stored report does not exist")]
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [Issue("STORED_REPORT_INVALID", "ERROR", "<receipt>",
                      str(path), str(exc))]
    receipt = stored.get("receipt")
    if not isinstance(receipt, dict):
        return [Issue("STORED_REPORT_RECEIPT_MISSING", "STALE", "<receipt>",
                      str(path), "stored report has no v2 receipt")]
    issues: list[Issue] = []
    expected_payload = receipt.get("payload_sha256")
    if not isinstance(expected_payload, str) or expected_payload != report_payload_digest(stored):
        issues.append(Issue(
            "STORED_REPORT_TAMPERED", "STALE", "<receipt>", str(path),
            "stored report payload SHA-256 does not match its receipt",
        ))
    current_binding = current.get("receipt", {}).get("binding_sha256")
    if receipt.get("binding_sha256") != current_binding:
        issues.append(Issue(
            "STORED_REPORT_INPUT_STALE", "STALE", "<receipt>", str(path),
            "stored report tool/registry/input/policy binding is stale",
        ))
    return issues


def scan(vault: Path, max_source_age_days: int | None,
         check_live: bool, timeout: float,
         invocation_argv: list[str] | None = None) -> dict[str, object]:
    evidence_path = vault / "03-Kanit-ve-Kaynak-Haritasi.md"
    decision_path = vault / "02-Karar-Gunlugu.md"
    research = vault / "_research"
    skills = vault / "Skills"
    for required in (evidence_path, decision_path, research, skills):
        if not required.exists():
            raise ValueError(f"required vault path missing: {required}")
    evidence_rows = all_table_rows(evidence_path.read_text(encoding="utf-8"))
    decision_rows = all_table_rows(decision_path.read_text(encoding="utf-8"))
    all_claims: list[dict[str, object]] = []
    all_issues: list[Issue] = []
    claim_occurrences: dict[str, list[dict[str, object]]] = {}

    note_paths = sorted(research.glob("*.md"))
    for note in note_paths:
        text = note.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)
        rows, malformed_lines = claim_rows_with_errors(text)
        if note_is_accepted(frontmatter) and not rows:
            all_issues.append(Issue(
                "NO_MACHINE_READABLE_CLAIMS", "RED", "<note>",
                str(note.relative_to(vault)),
                "accepted research note has no claim_id table",
            ))
        for line_number in malformed_lines:
            all_issues.append(Issue(
                "MALFORMED_CLAIM_ROW",
                "RED" if note_is_accepted(frontmatter) else "ADVISORY",
                "<row>", str(note.relative_to(vault)),
                f"claim table row at line {line_number} has wrong cell count",
            ))
        for row in rows:
            claim, issues = check_claim(
                vault=vault, note_path=note, frontmatter=frontmatter,
                row=row, evidence_rows=evidence_rows, decision_rows=decision_rows,
                max_source_age_days=max_source_age_days,
            )
            claim_id = str(claim["claim_id"])
            claim_occurrences.setdefault(claim_id, []).append(claim)
            all_claims.append(claim)
            all_issues.extend(issues)

    for claim_id, occurrences in claim_occurrences.items():
        if claim_id == "<missing>" or len(occurrences) < 2:
            continue
        accepted = any(bool(item["accepted"]) for item in occurrences)
        notes = sorted({str(item["note"]) for item in occurrences})
        all_issues.append(Issue(
            "DUPLICATE_CLAIM_ID", "RED" if accepted else "ADVISORY",
            claim_id, ", ".join(notes),
            "claim_id is not globally unique",
        ))

    live_results: list[dict[str, object]] = []
    if check_live:
        url_acceptance: dict[str, bool] = {}
        for claim in all_claims:
            for url in claim["urls"]:
                url_acceptance[url] = (
                    url_acceptance.get(url, False) or bool(claim["accepted"])
                )
        urls = sorted(url_acceptance)
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(urls)))) as pool:
            futures = {pool.submit(live_check, url, timeout): url for url in urls}
            for future in as_completed(futures):
                result = future.result()
                live_results.append(result)
                if not result["ok"]:
                    all_issues.append(Issue(
                        "SOURCE_FETCH_FAILED",
                        "STALE" if url_acceptance[result["url"]] else "ADVISORY",
                        "<source>", "_research",
                        f"{result['url']}: {result.get('error', result.get('status'))}",
                    ))

    accepted_count = sum(bool(claim["accepted"]) for claim in all_claims)
    skill_files = sorted(skills.glob("SKILL-*.md"))
    policy = {
        "max_source_age_days": max_source_age_days,
        "live_check": check_live,
        "authority_screen_is_proof": False,
    }
    receipt, receipt_issues = build_receipt(
        vault, note_paths, invocation_argv or [], policy,
    )
    all_issues.extend(receipt_issues)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "vault": str(vault),
        "effective_status": "GREEN",
        "trace_status": "GREEN",
        "freshness_status": "GREEN",
        "summary": {
            "skill_files": len(skill_files),
            "research_notes": len(note_paths),
            "claims": len(all_claims),
            "accepted_claims": accepted_count,
            "blocking_issues": 0,
            "advisories": 0,
        },
        "claims": all_claims,
        "issues": [asdict(issue) for issue in all_issues],
        "live_sources": sorted(live_results, key=lambda row: str(row["url"])),
        "policy": policy,
        "receipt": receipt,
    }
    recompute_report_status(report)
    return report


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    # No default: the vault root is an INPUT, not a property of where this file
    # happens to live. The old `parents[2]` default silently followed the script
    # around and pointed at the wrong tree after the ProjectE-2 move.
    parser.add_argument("--vault", type=Path, required=True,
                        help="Otonom-PCB-Ajani vault root (required)")
    parser.add_argument("--json", type=Path, dest="json_path",
                        help="write the report to this path")
    parser.add_argument("--verify-report", type=Path,
                        help="verify a stored v2 report against current input bindings")
    parser.add_argument("--max-source-age-days", type=int,
                        help="project policy; omitted means no invented TTL")
    parser.add_argument("--check-live", action="store_true",
                        help="perform read-only HTTPS liveness checks")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="per-source network timeout in seconds")
    return parser.parse_args(argv)


def validated_report_path(vault: Path, requested: Path) -> Path:
    """Confine a vault-internal report target to ``_inbox/*.json``.

    A path entirely outside the vault is allowed (the caller may write to a
    scratch dir); a path that touches the vault either lexically or after
    symlink resolution must land in ``_inbox``. ``confine_to_dir`` from
    common.paths performs that same lexical+resolved pair check.
    """
    if requested.suffix.casefold() != ".json":
        raise ValueError("--json output must use the .json suffix")
    vault = vault.resolve()
    inbox = (vault / "_inbox").resolve()
    lexical = Path(os.path.abspath(requested))
    resolved = requested.resolve(strict=False)
    if lexical.is_relative_to(vault) or resolved.is_relative_to(vault):
        try:
            return confine_to_dir(requested, inbox, suffix=".json")
        except PathConfinementError as exc:
            raise ValueError(
                "vault report output is restricted to _inbox/*.json") from exc
    return resolved


def main(argv: Iterable[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    vault = args.vault.resolve()
    try:
        if args.max_source_age_days is not None and args.max_source_age_days < 0:
            raise ValueError("--max-source-age-days must be non-negative")
        report = scan(vault, args.max_source_age_days,
                      args.check_live, args.timeout,
                      [str(Path(__file__).resolve()), *raw_argv])
        if args.verify_report:
            report["issues"].extend(
                asdict(item) for item in stored_report_issues(
                    vault, args.verify_report, report,
                )
            )
            recompute_report_status(report)
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "effective_status": "ERROR",
            "summary": {"blocking_issues": 1},
            "issues": [{
                "code": "INPUT_ERROR", "status": "ERROR",
                "claim_id": "<input>", "note": str(args.vault),
                "message": str(exc),
            }],
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_path:
        try:
            report_path = validated_report_path(vault, args.json_path)
            atomic_write_text(report_path, rendered + "\n")
        except (OSError, ValueError, ReportWriteError) as exc:
            # The structured error report stays on STDOUT - it is the report
            # for this run, and stdout is the machine-readable stream. The
            # human-readable reason goes to stderr, which is what was
            # missing before.
            diag(f"ERROR: report write refused: {exc}")
            emit({
                "schema_version": SCHEMA_VERSION,
                "effective_status": "ERROR",
                "issues": [{"code": "REPORT_WRITE_ERROR", "message": str(exc)}],
            })
            return 2
    sys.stdout.write(rendered + "\n")
    sys.stdout.flush()
    return 0 if report["effective_status"] == "GREEN" else (
        2 if report["effective_status"] == "ERROR" else 1
    )


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
