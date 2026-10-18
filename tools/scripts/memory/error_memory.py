#!/usr/bin/env python3
"""Persistent ERC/DRC/DFM error memory: what we tried, and what it cost.

PORTED FROM ``pcb-tool-v2/hata_hafizasi.py``.

WHY IT EXISTS
-------------
Without it, every session rediscovers the same clearance violation from
scratch and re-tries the fix that failed last time. The memory keeps the
class of an error (not its coordinates) and both the fixes that WORKED and
the fixes that DID NOT, because re-trying a known-bad fix for the third time
is worse than having no memory at all.

RETRIEVAL, HONESTLY
-------------------
This is NOT embedding-based RAG and is not presented as one. Two local,
testable layers:
  1. SIGNATURE match. Coordinates, refdes, quoted net names and bare numbers
     are replaced by placeholders before hashing, so the same CLASS of error
     at different coordinates gets the same signature. Plain string equality
     cannot do this.
  2. Lexical Jaccard overlap on the normalised tokens, and only above
     ``--similarity``. A weak match offered as a suggestion sends the agent
     back to the wrong fix.

CHANGED IN THE PORT
-------------------
  * STORAGE MOVED OUT OF THE VAULT. The archive's default was a Markdown file
    intended for an Obsidian vault; this project's standing rule is that the
    vault holds no design or tool state. See ``scripts/memory/store.py`` -
    the default is ``pcb-agent/var/memory/error_memory.md`` and a vault path
    is refused fail-closed rather than written to.
  * A CLI, a schema and the 0/1/2 exit contract. The archive exposed the
    class only to other Python code and a ``__main__`` self-test.
  * Reads the repo's ``drc-gate-report/v1`` as well as raw kicad-cli JSON,
    so the memory can be fed straight from the promotion gate.

CLI:
    error_memory.py REPORT [--store DIR] [--similarity F] [--json OUT]
        --learn --root-cause TEXT --fix TEXT
               --outcome {solved,failed,needs_human} [--project NAME]
        (default action, without --learn, is to SUGGEST from memory)

Exit codes: 0 done, 1 nothing retained/no match to act on, 2 could not run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import (  # noqa: E402
    EXIT_FAIL, EXIT_OK, diag, emit, run_cli, write_report,
)
from scripts.memory.store import (  # noqa: E402
    VaultWriteRefused, error_memory_path,
)

SCHEMA_VERSION = "error-memory-report/v1"

CHECK_KINDS = ("DRC", "ERC", "DFM", "SIMULATION", "OTHER")
OUTCOMES = ("solved", "failed", "needs_human")

HEADER = """---
tags: [pcb, error-memory, drc, erc, dfm]
generated_by: scripts/memory/error_memory.py
---

# Error memory

Managed automatically. Hand edits are fine, but every record's `signature`
line MUST survive: retrieval runs on it.

Storage lives under pcb-agent/var/, NOT in an Obsidian vault.

"""

_COORDINATE = re.compile(
    r"\(?\s*-?\d+[.,]\d+\s*(mm|mils?)?\s*[,;]\s*-?\d+[.,]\d+\s*(mm|mils?)?\s*\)?")
_REFDES = re.compile(r"\b([A-Z]{1,3})(\d+)\b")
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")
_SPACE = re.compile(r"\s+")
_FIELD = re.compile(r"^-\s*\*\*(?P<name>[^:*]+):\*\*\s*(?P<value>.*)$")


class MemoryError_(RuntimeError):
    """The memory could not be read or written."""


# --------------------------------------------------------------------------- #
# Normalisation and signature
# --------------------------------------------------------------------------- #
def normalise(message: str) -> str:
    """Replace everything that changes per run with a placeholder.

    ORDER MATTERS: coordinates before bare numbers, or "(12.34, 56.78)"
    collapses into two numbers and the fact that it WAS a coordinate is gone.
    """
    text = str(message).strip()
    text = _COORDINATE.sub(" <coord> ", text)
    text = _QUOTED.sub(" <net> ", text)
    text = _REFDES.sub(lambda m: f"<{m.group(1).lower()}ref>", text)
    text = _NUMBER.sub(" <num> ", text)
    return _SPACE.sub(" ", text).strip().lower()


def signature(message: str) -> str:
    """Stable 8-hex-character id for an error CLASS."""
    return hashlib.sha256(normalise(message).encode("utf-8")).hexdigest()[:8]


def _tokens(message: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9<>_]+", normalise(message))
            if len(t) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of the normalised token sets."""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    kind: str
    message: str
    root_cause: str
    fix: str
    outcome: str
    project: str = ""
    recorded: str = field(default_factory=lambda: date.today().isoformat())
    tags: list[str] = field(default_factory=list)
    signature: str = ""
    # How many times this exact (signature, fix, outcome) has been seen.
    #
    # Deduplication is deliberate and stays - forty identical records break
    # retrieval, as this module's docstring says. But collapsing them also
    # threw the COUNT away, and a fix that has worked forty times was stored
    # indistinguishably from one that worked once. Nothing could then rank
    # them, which is what scripts/memory/priors.py needs. Keeping one record
    # and a counter preserves retrieval and recovers the frequency.
    #
    # Absent in older files, where it reads as 1 - so existing memories stay
    # loadable and simply carry no frequency information.
    occurrences: int = 1

    def __post_init__(self) -> None:
        if self.kind not in CHECK_KINDS:
            raise MemoryError_(f"unknown check kind: {self.kind}")
        if self.outcome not in OUTCOMES:
            raise MemoryError_(f"unknown outcome: {self.outcome}")
        if not self.signature:
            self.signature = signature(self.message)

    def to_markdown(self) -> str:
        tags = " ".join(f"#{t.lstrip('#')}" for t in self.tags)
        return (
            f"## {self.kind} - {self.signature}\n"
            f"- **kind:** {self.kind}\n"
            f"- **signature:** {self.signature}\n"
            f"- **message:** {self.message}\n"
            f"- **root_cause:** {self.root_cause}\n"
            f"- **fix:** {self.fix}\n"
            f"- **outcome:** {self.outcome}\n"
            f"- **project:** {self.project}\n"
            f"- **recorded:** {self.recorded}\n"
            f"- **occurrences:** {self.occurrences}\n"
            f"- **tags:** {tags}\n\n"
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "signature": self.signature,
            "message": self.message, "root_cause": self.root_cause,
            "fix": self.fix, "outcome": self.outcome,
            "project": self.project, "recorded": self.recorded,
            "occurrences": self.occurrences,
            "tags": list(self.tags),
        }


def parse_markdown(text: str) -> list[Record]:
    """Half-written records are SKIPPED, not guessed at.

    A record missing its message or signature cannot be retrieved correctly,
    and a partial record turns into a wrong suggestion.
    """
    records: list[Record] = []
    for section in text.split("\n## ")[1:]:
        fields: dict[str, str] = {}
        for line in section.splitlines():
            match = _FIELD.match(line.strip())
            if match:
                fields[match.group("name").strip().lower()] = \
                    match.group("value").strip()
        if not fields.get("message") or not fields.get("signature"):
            continue
        kind = fields.get("kind", "OTHER")
        outcome = fields.get("outcome", "needs_human")
        records.append(Record(
            kind=kind if kind in CHECK_KINDS else "OTHER",
            message=fields["message"],
            root_cause=fields.get("root_cause", ""),
            fix=fields.get("fix", ""),
            outcome=outcome if outcome in OUTCOMES else "needs_human",
            project=fields.get("project", ""),
            recorded=fields.get("recorded", ""),
            tags=[t.lstrip("#") for t in fields.get("tags", "").split()
                  if t.strip()],
            signature=fields["signature"],
            # Older files have no counter; one record means one observation.
            occurrences=_positive_int(fields.get("occurrences"), default=1),
        ))
    return records


def _positive_int(value: str | None, default: int = 1) -> int:
    """A malformed counter falls back to the default rather than raising.

    A hand-edited memory file is explicitly allowed (see HEADER); a typo in a
    counter must not make the whole memory unreadable, because losing every
    record is far worse than losing one frequency.
    """
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


# --------------------------------------------------------------------------- #
class ErrorMemory:
    """Markdown-backed memory. A missing file is an empty memory, not an error."""

    def __init__(self, path: Path, threshold: float = 0.55) -> None:
        self.path = Path(path)
        self.threshold = threshold

    def records(self) -> list[Record]:
        if not self.path.exists():
            return []
        return parse_markdown(self.path.read_text(encoding="utf-8"))

    def remember(self, record: Record) -> bool:
        """True when a NEW record was stored, False when one already existed.

        Idempotency is mandatory: one DRC run can raise the same violation
        forty times, and forty identical records break retrieval.

        A repeat is no longer discarded silently, though - it increments the
        existing record's ``occurrences``. Collapsing repeats kept retrieval
        working but destroyed the frequency, so a fix that had worked forty
        times was stored exactly like one that worked once and nothing could
        rank them. The return value is unchanged: callers that only ask "was
        this new" still get the same answer.
        """
        existing_records = self.records()
        key = (record.signature, record.fix.strip(), record.outcome)
        for index, existing in enumerate(existing_records):
            if (existing.signature, existing.fix.strip(),
                    existing.outcome) == key:
                existing_records[index].occurrences = existing.occurrences + 1
                self._rewrite(existing_records)
                return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(HEADER, encoding="utf-8")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_markdown())
        return True

    def _rewrite(self, records: list[Record]) -> None:
        """Rewrite the whole file atomically.

        Incrementing a counter cannot be an append, and a half-written memory
        is worse than a stale one: ``atomic_write_text`` means a reader never
        sees a truncated file.
        """
        from common.report_io import atomic_write_text
        body = HEADER + "".join(item.to_markdown() for item in records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, body)

    def similar(self, message: str,
                kind: str | None = None) -> list[tuple[float, Record]]:
        target = signature(message)
        found: list[tuple[float, Record]] = []
        for record in self.records():
            if kind is not None and record.kind != kind:
                continue
            if record.signature == target:
                found.append((1.0, record))
                continue
            score = similarity(message, record.message)
            if score >= self.threshold:
                found.append((round(score, 4), record))
        return sorted(found, key=lambda pair: -pair[0])

    def suggest(self, message: str, kind: str | None = None) -> dict:
        """Three buckets. ``failed`` fixes are returned but NEVER as advice."""
        buckets: dict[str, list[dict]] = {
            "suggested": [], "already_failed": [], "needs_human": []}
        for score, record in self.similar(message, kind):
            item = {"score": score, **record.to_dict()}
            if record.outcome == "solved":
                buckets["suggested"].append(item)
            elif record.outcome == "failed":
                buckets["already_failed"].append(item)
            else:
                buckets["needs_human"].append(item)
        return buckets


# --------------------------------------------------------------------------- #
# Report ingestion
# --------------------------------------------------------------------------- #
TAG_KEYWORDS = (
    ("clearance", "clearance"), ("track width", "track-width"),
    ("annular", "annular-ring"), ("silk", "silkscreen"),
    ("courtyard", "courtyard"), ("unconnected", "unconnected"),
    ("hole", "hole"), ("via", "via"), ("power", "power"),
    ("pin not", "pin"), ("solder_mask", "solder-mask"),
)


def guess_tag(message: str) -> str:
    lowered = message.lower()
    for needle, tag in TAG_KEYWORDS:
        if needle in lowered:
            return tag
    return "general"


def report_violations(document: dict, errors_only: bool = False) -> list[dict]:
    """Violations from raw kicad-cli JSON OR from drc-gate-report/v1."""
    violations = list(document.get("violations", []))
    for sheet in document.get("sheets", []):
        violations.extend(sheet.get("violations", []))
    for item in document.get("unconnected_items", []):
        violations.append({**item, "type": item.get("type", "unconnected")})
    if errors_only:
        violations = [v for v in violations
                      if str(v.get("severity", "")).lower() == "error"]
    return violations


def unique_messages(violations) -> list[tuple[str, str, dict]]:
    """``(signature, message, violation)`` deduplicated by error CLASS."""
    seen: set[str] = set()
    out: list[tuple[str, str, dict]] = []
    for violation in violations:
        message = str(violation.get("description", "")).strip()
        if not message:
            continue
        sig = signature(message)
        if sig in seen:
            continue
        seen.add(sig)
        out.append((sig, message, violation))
    return out


def detect_kind(document: dict) -> str:
    mode = str(document.get("mode", "")).upper()
    if mode in CHECK_KINDS:
        return mode
    return "ERC" if document.get("sheets") else "DRC"


# --------------------------------------------------------------------------- #
def self_test() -> list[str]:
    import tempfile

    problems: list[str] = []
    a = 'Clearance violation (net "GND" and net "+3V3") at (12.34, 56.78)'
    b = 'Clearance violation (net "GND" and net "+3V3") at (98.76, 54.32)'
    if signature(a) != signature(b):
        problems.append("coordinates changed the signature; normalisation broke")
    if signature(a) == signature("Track width too small"):
        problems.append("different error classes collapsed to one signature")

    # FAULT INJECTION: unrelated messages must NOT match, or every suggestion
    # is noise and the memory is worse than useless.
    if similarity(a, "Symbol pin not connected to any net") >= 0.55:
        problems.append("unrelated messages scored as similar")

    with tempfile.TemporaryDirectory(prefix="error-memory-selftest-") as tmp:
        memory = ErrorMemory(Path(tmp) / "error_memory.md")
        record = Record("DRC", a, "placement too tight",
                        "moved C7 by 2 mm; track width NOT reduced",
                        "solved", "TestProject", tags=["clearance"])
        if not memory.remember(record):
            problems.append("first record was not stored")
        if memory.remember(record):
            problems.append("identical record stored twice (not idempotent)")
        if len(memory.records()) != 1:
            problems.append("round trip did not return exactly one record")

        found = memory.suggest(b)
        if not found["suggested"] or found["suggested"][0]["score"] != 1.0:
            problems.append("signature match did not retrieve the prior fix")

        memory.remember(Record("DRC", a, "same root cause",
                               "reduced track width to 0.15 mm", "failed",
                               "TestProject"))
        again = memory.suggest(a)
        if any("0.15" in item["fix"] for item in again["suggested"]):
            problems.append("a KNOWN-FAILED fix was offered as a suggestion")
        if not again["already_failed"]:
            problems.append("known-failed fix missing from the do-not-retry list")

    try:
        from scripts.memory.store import assert_not_vault  # noqa: PLC0415
        vault = Path(tempfile.mkdtemp(prefix="fake-vault-"))
        (vault / ".obsidian").mkdir()
        try:
            assert_not_vault(vault / "notes" / "error_memory.md")
            problems.append("the vault guard did not refuse a vault path")
        except VaultWriteRefused:
            pass
    except OSError as exc:                        # pragma: no cover
        problems.append(f"vault guard test could not run: {exc}")
    return problems


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?",
                        help="kicad-cli DRC/ERC JSON or drc-gate-report/v1")
    parser.add_argument("--store", help="memory directory "
                                        "(default: pcb-agent/var/memory)")
    parser.add_argument("--similarity", type=float, default=0.55,
                        help="minimum Jaccard score for a lexical match")
    parser.add_argument("--kind", choices=CHECK_KINDS,
                        help="override the check kind (default: from report)")
    parser.add_argument("--errors-only", action="store_true",
                        help="ignore warnings when reading the report")
    parser.add_argument("--learn", action="store_true",
                        help="store the report's error classes with a fix")
    parser.add_argument("--root-cause", default="",
                        help="required with --learn")
    parser.add_argument("--fix", default="", help="required with --learn")
    parser.add_argument("--outcome", choices=OUTCOMES,
                        help="required with --learn")
    parser.add_argument("--project", default="")
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return args
    if not args.report:
        parser.error("REPORT is required (a DRC/ERC JSON path)")
    if args.learn and not (args.root_cause and args.fix and args.outcome):
        parser.error("--learn requires --root-cause, --fix and --outcome")
    if not 0.0 < args.similarity <= 1.0:
        parser.error("--similarity must be in (0, 1]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        emit({"schema_version": SCHEMA_VERSION,
              "self_test": "PASS" if not problems else "FAIL",
              "problems": problems})
        return EXIT_OK if not problems else EXIT_FAIL

    path = Path(args.report).expanduser().resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    kind = args.kind or detect_kind(document)
    violations = report_violations(document, args.errors_only)
    classes = unique_messages(violations)

    store = error_memory_path(args.store, create=args.learn)
    memory = ErrorMemory(store, args.similarity)

    report = {
        "schema_version": SCHEMA_VERSION,
        "action": "learn" if args.learn else "suggest",
        "report": str(path),
        "store": str(store),
        "kind": kind,
        "violations_read": len(violations),
        "error_classes": len(classes),
        "records_in_memory": len(memory.records()),
        "entries": [],
    }

    if args.learn:
        stored = 0
        for sig, message, _violation in classes:
            record = Record(kind=kind, message=message,
                            root_cause=args.root_cause, fix=args.fix,
                            outcome=args.outcome, project=args.project,
                            tags=[kind.lower(), guess_tag(message)])
            added = memory.remember(record)
            stored += int(added)
            report["entries"].append({"signature": sig, "message": message,
                                      "stored": added})
        report["stored"] = stored
        report["status"] = "STORED" if stored else "NOTHING_NEW"
        diag(f"error memory: {stored} new record(s) in {store}")
        result_code = EXIT_OK if stored else EXIT_FAIL
    else:
        matched = 0
        for sig, message, _violation in classes:
            buckets = memory.suggest(message, kind)
            hit = any(buckets.values())
            matched += int(hit)
            report["entries"].append({"signature": sig, "message": message,
                                      "matched": hit, **buckets})
        report["matched_classes"] = matched
        report["status"] = "MATCHED" if matched else "NO_MATCH"
        diag(f"error memory: {matched}/{len(classes)} error class(es) "
             f"had prior records in {store}")
        result_code = EXIT_OK if matched else EXIT_FAIL

    write_report(report, args.json_path)
    emit(report)
    return result_code


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
