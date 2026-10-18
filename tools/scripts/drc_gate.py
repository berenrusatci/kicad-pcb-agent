#!/usr/bin/env python3
"""Hash-bindable DRC/ERC gate around ``kicad-cli``.

VENDORED FROM ``PRJ-AM5706-6XOV9281-R10/tools/krt/drc.sh``. That script was
nine lines of shell and had four defects that made its output unusable as
gate evidence:

  1. no ``--exit-code-violations``  -> kicad-cli exits 0 WITH violations, so
     any caller reading ``$?`` got a false PASS;
  2. no ``--severity-all``          -> whole severity categories were hidden;
  3. ``>/dev/null 2>&1``            -> a kicad-cli that failed outright was
     indistinguishable from a clean run, and the caller then read a stale or
     absent ``-o`` file as "no violations";
  4. the companion ``.kicad_pro``   -> hardcoded to ONE board's absolute path,
     so on any other board the run silently used KiCad defaults instead of
     the project's design rules.

A fifth defect was inherited rather than fixed at port time and was found on
2026-08-09, the first day this gate was run twice on the same board and the
two answers compared: without ``--all-track-errors`` kicad-cli reports only
the first error per track, and the winner is a race between DRC workers. The
same board hashed identically and returned 182..205 violations across runs.
See ``build_argv`` for the measurement. The lesson is not about a flag: a
gate that is never run twice cannot tell a threshold from a coin toss.

It was ported to Python rather than kept as shell so it can join the
hash-bound tool registry, the unittest suite and ``report_io.run_cli``'s
0/1/2 contract. The temp-directory dance from the original is KEPT: kicad-cli
reads design rules from the ``.kicad_pro`` sitting next to the board, so a
board checked without its project file is checked against the wrong rules.

Coverage is a census of the objects handed to the engine, taken from the
design file itself. A run that examined nothing reports ``NO_COVERAGE``; it
never reports PASS, which is what an empty ``violations`` array would
otherwise look like.

A sixth defect was found on 2026-08-10, and it is the same shape one level
up: a committed ``drc.json`` recorded 191 violations / 21 unconnected, and
the same unchanged board on a second machine returned a different number.
The board digest was identical; the ``kicad-cli`` was 10.0.5 there and
10.0.4 here. Every field needed to see that was already in the report - the
engine version is MEASURED with ``kicad-cli version`` and the exact argv is
stored - but NOTHING refused to compare two reports that disagreed on
either. "191 vs 222" read as a design regression when it was a tool swap.
So there are at least two external variables behind any DRC number: the
ENGINE VERSION and the INVOCATION FLAGS. ``compare_runs`` refuses a
comparison unless both are known and equal on both sides; see its docstring
and ``argv_signature``. A difference that cannot be attributed is not a
measurement.

CLI:
    drc_gate.py DESIGN [--json REPORT] [--kicad-cli PATH] [--project PRO]
                       [--schematic-parity] [--keep-raw PATH] [--self-test]
    drc_gate.py --compare LEFT.json RIGHT.json [--json REPORT]

``DESIGN`` is a ``.kicad_pcb`` (DRC) or a ``.kicad_sch`` (ERC).

Exit codes: 0 clean, 1 violations or NO_COVERAGE, 2 could not run.
For ``--compare``: 0 comparable and in agreement, 1 comparable and the two
runs disagree on an unchanged design, 2 the comparison was REFUSED.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (  # noqa: E402
    FAIL, NO_COVERAGE, PASS, result, rollup_status, summarise,
)
from common.hashing import sha256_file  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_ERROR, EXIT_FAIL, EXIT_OK, diag, emit, run_cli, write_report,
)
from common.sexpr import subforms  # noqa: E402

SCHEMA_VERSION = "drc-gate-report/v1"
COMPARISON_SCHEMA_VERSION = "drc-gate-comparison/v1"

#: Direct children of ``(kicad_pcb ...)`` that the DRC engine evaluates.
PCB_OBJECTS = ("footprint", "segment", "arc", "via", "zone")
#: Direct children of ``(kicad_sch ...)`` that the ERC engine evaluates.
SCH_OBJECTS = ("symbol", "wire", "bus", "junction", "label", "global_label",
               "hierarchical_label", "no_connect", "sheet")

VIOLATION_SEVERITIES = ("error", "warning", "exclusion", "ignore", "info")


class GateError(RuntimeError):
    """The gate could not be run; never reported as a clean design."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_kicad_cli(wanted: str | None) -> str:
    """Explicit path -> $KICAD_CLI -> PATH -> Windows install. Never bare.

    The Windows step is not cosmetic: the KiCad installer does not put
    ``kicad-cli.exe`` on PATH, so on this repo's own development machine the
    gate raised "kicad-cli not found" and every DRC evidence artefact had to
    be produced by hand-passing ``--kicad-cli``. ``common.kicad_cli`` already
    knew where to look; this resolver did not, and the two disagreeing about
    whether KiCad is installed is how a gate ends up unrun rather than red.
    Newest install first, so a machine with 9.x and 10.x beside each other
    does not silently gate on the older engine.
    """
    for candidate in (wanted, os.environ.get("KICAD_CLI")):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path.resolve())
        found = shutil.which(candidate)
        if found:
            return found
        raise GateError(f"requested kicad-cli not found: {candidate}")
    found = shutil.which("kicad-cli")
    if found:
        return found
    if os.name == "nt":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "KiCad"
        for installed in sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True):
            if installed.is_file():
                return str(installed.resolve())
    raise GateError(
        "kicad-cli not found; install KiCad, set $KICAD_CLI or pass --kicad-cli")


def engine_version(exe: str) -> str:
    try:
        proc = subprocess.run([exe, "version"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"kicad-cli version failed: {exc}") from exc
    return (proc.stdout or proc.stderr).strip().splitlines()[0] if (
        proc.stdout or proc.stderr).strip() else "unknown"


# --------------------------------------------------------------------------- #
# Object census - the coverage denominator
# --------------------------------------------------------------------------- #
def root_children(text: str) -> list[tuple[str, str]]:
    """``(name, form)`` for every DIRECT child of the root S-expression.

    ``common.sexpr.direct_children`` cannot be used here: it recognises a form
    only as ``"(name "`` or ``"(name("``, and KiCad 10 writes
    ``"(footprint\\n\\t\\t\\"lib:fp\\""`` - a NEWLINE after the token. That
    made every census come back zero, which would have turned every board
    into a silent NO_COVERAGE. Depth matters too: ``(symbol ...)`` inside
    ``(lib_symbols ...)`` is a library definition, not a schematic instance,
    and counting those would inflate the ERC denominator.
    """
    out: list[tuple[str, str]] = []
    depth = 0
    quoted = escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            index += 1
            continue
        if char == "(":
            depth += 1
            if depth == 2:
                match = re.match(r"\(([^\s()\"]+)", text[index:])
                start = index
                end = _skip_form(text, index)
                if match:
                    out.append((match.group(1), text[start:end]))
                index = end
                depth -= 1
                continue
        elif char == ")":
            depth -= 1
        index += 1
    return out


def _skip_form(text: str, start: int) -> int:
    """Index just past the balanced form beginning at ``text[start]``."""
    depth = 0
    quoted = escaped = False
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return length


def count_objects(text: str, names) -> dict[str, int]:
    counts = {name: 0 for name in names}
    for name, _form in root_children(text):
        if name in counts:
            counts[name] += 1
    return counts


def pcb_census(text: str) -> dict[str, int]:
    """Board objects submitted to DRC.

    Pads are counted separately because they are nested inside footprints,
    and pad clearance/annular-ring rules are a large part of what DRC checks.
    """
    counts = count_objects(text, PCB_OBJECTS)
    counts["pad"] = sum(len(subforms(form, "pad"))
                        for name, form in root_children(text)
                        if name == "footprint")
    return counts


def sch_census(text: str) -> dict[str, int]:
    """Schematic objects submitted to ERC, in ONE sheet."""
    return count_objects(text, SCH_OBJECTS)


def sheet_set(design: Path) -> list[Path]:
    """Every schematic handed to the engine: the root sheet and its siblings.

    Same set for the census and for the run, deliberately - they were computed
    from different inputs before, and the gap showed up as a hierarchical
    design reporting ``wire=0`` while every wire in it sat one level down.

    The glob is an over-approximation: it takes all sibling ``.kicad_sch``
    files rather than only the ones the root sheet references. That is what
    the engine has always been given, so narrowing it here would make the
    census describe something other than the run.
    """
    siblings = sorted(s for s in design.parent.glob("*.kicad_sch")
                      if s.resolve() != design.resolve())
    return [design, *siblings]


def sch_census_set(sheets: list[Path]) -> dict[str, int]:
    """Census across the whole sheet set. A hierarchical root sheet holds
    little but sheet symbols, so a root-only census makes the ERC coverage
    denominator - and every rule that reads it - describe the wrong design."""
    totals = {name: 0 for name in SCH_OBJECTS}
    for sheet in sheets:
        text = sheet.read_text(encoding="utf-8", errors="replace")
        for key, value in sch_census(text).items():
            totals[key] = totals.get(key, 0) + value
    return totals


# --------------------------------------------------------------------------- #
# kicad-cli invocation
# --------------------------------------------------------------------------- #
def build_argv(exe: str, mode: str, design: str, out: str,
               *, schematic_parity: bool = False) -> list[str]:
    """The exact command. ``--exit-code-violations``, ``--severity-all`` and
    ``--all-track-errors`` are NOT optional: without them the gate cannot see
    what it is gating.

    ``--all-track-errors`` joined that list on 2026-08-09, and it was a
    MEASUREMENT that put it there rather than a reading of the docs. Without
    it kicad-cli reports only the FIRST error found on each track, and which
    one is first is decided by whichever DRC worker finishes first - so the
    same board, same file digest, same argv produced a different violation
    count on every run:

        bayraksiz          196 · 205 · 192 · 189 · 203   (clearance 81/90/77/73/87)
        --all-track-errors 292 · 292 · 292 · 292 · 292   (clearance 114 x5)

    Only ``clearance`` moved, because it is the check that fires repeatedly
    on one track; the per-object checks were already stable. The count rises
    (184 -> 292) because the gate now sees every error instead of one per
    track - the old number was not a smaller problem, it was a partial view
    of the same one.

    A threshold of zero cannot sit on a number that changes between runs:
    "0 ihlal" would be the outcome of a draw rather than a measurement. This
    is the root cause, so no N-run union is needed - a union would have
    averaged over the nondeterminism instead of removing it, and would still
    have hidden the under-reporting.
    """
    if mode == "drc":
        argv = [exe, "pcb", "drc", "--format", "json", "--units", "mm",
                "--severity-all", "--exit-code-violations",
                "--all-track-errors"]
        if schematic_parity:
            argv.append("--schematic-parity")
    else:
        argv = [exe, "sch", "erc", "--format", "json", "--units", "mm",
                "--severity-all", "--exit-code-violations"]
    return argv + ["-o", out, design]


def run_engine(exe: str, mode: str, design: Path, project: Path | None,
               *, schematic_parity: bool, keep_raw: Path | None) -> tuple[dict, list[str], int]:
    """Run kicad-cli in a temp directory beside a copy of the project file.

    Returns (parsed report, exact argv, exit code). Raises GateError when the
    engine failed - a failed engine is never a clean design.
    """
    suffix = design.suffix
    with tempfile.TemporaryDirectory(prefix="drc-gate-") as workdir:
        work = Path(workdir)
        staged = work / f"design{suffix}"
        shutil.copy2(design, staged)
        if project is not None:
            shutil.copy2(project, work / "design.kicad_pro")
        # ERC needs the whole sheet set; a hierarchical design has sibling
        # .kicad_sch files that the root sheet references by name.
        if mode == "erc":
            for sibling in sheet_set(design)[1:]:
                shutil.copy2(sibling, work / sibling.name)
        out = work / "report.json"
        argv = build_argv(exe, mode, str(staged), str(out),
                          schematic_parity=schematic_parity)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GateError(f"kicad-cli failed to start: {exc}") from exc
        # 0 = clean, 5 = violations found (documented). Anything else is a
        # tool failure; the original shell script swallowed all of them.
        if proc.returncode not in (0, 5) and not out.is_file():
            raise GateError(
                f"kicad-cli exit {proc.returncode}\n"
                f"{proc.stdout.strip()}\n{proc.stderr.strip()}")
        if not out.is_file():
            raise GateError("kicad-cli produced no report file")
        raw = out.read_text(encoding="utf-8")
        if keep_raw is not None:
            Path(keep_raw).parent.mkdir(parents=True, exist_ok=True)
            Path(keep_raw).write_text(raw, encoding="utf-8")
        try:
            report = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GateError(f"kicad-cli report is not JSON: {exc}") from exc
        # The staged path leaks the temp directory into the evidence; replace
        # it with the real design path so the report is reproducible.
        if isinstance(report, dict) and isinstance(report.get("source"), str):
            report["source"] = str(design)
        return report, argv, proc.returncode


# --------------------------------------------------------------------------- #
# Report parsing
# --------------------------------------------------------------------------- #
def flatten_violations(report: dict, mode: str) -> list[dict]:
    if mode == "erc":
        return [v for sheet in report.get("sheets", [])
                for v in sheet.get("violations", [])]
    return list(report.get("violations", []))


def severity_counts(violations) -> dict[str, int]:
    counts = {name: 0 for name in VIOLATION_SEVERITIES}
    for violation in violations:
        severity = str(violation.get("severity", "")).lower()
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def describe(violation: dict) -> str:
    parts = [str(violation.get("severity", "?")),
             str(violation.get("type", "?")),
             str(violation.get("description", "")).strip()]
    return " | ".join(p for p in parts if p and p != "?")


def build_report(design: Path, mode: str, engine: dict, argv: list[str],
                 exit_code: int, raw: dict, census: dict[str, int],
                 started: str, finished: str,
                 project: Path | None,
                 exclusions: list[dict] | None = None) -> dict:
    exclusions = list(exclusions or [])
    excluded_checks = {e["check"] for e in exclusions}
    violations = flatten_violations(raw, mode)
    severities = severity_counts(violations)
    unconnected = list(raw.get("unconnected_items", []))
    parity = list(raw.get("schematic_parity", []))
    scanned = sum(census.values())

    findings = [
        result(f"{mode}_violations", scanned,
               [describe(v) for v in violations[:200]],
               f"{len(violations)} violation(s) at severity-all"),
        result(f"{mode}_errors", scanned,
               [describe(v) for v in violations
                if str(v.get("severity", "")).lower() == "error"][:200],
               "severity=error violations"),
    ]
    if mode == "drc":
        findings.append(result(
            "unconnected_items", scanned,
            [str(item.get("description", item)) for item in unconnected[:200]],
            f"{len(unconnected)} unconnected item(s)"))
        if "--schematic-parity" in argv:
            findings.append(result(
                "schematic_parity", scanned,
                [str(item.get("description", item)) for item in parity[:200]],
                f"{len(parity)} parity difference(s)"))
        else:
            findings.append(result(
                "schematic_parity", 0, [],
                "not requested: without --schematic-parity kicad-cli always "
                "returns an EMPTY parity list, which is not evidence of parity"))

    # ``parity_difference_count`` is null - NOT zero - when --schematic-parity
    # was not requested. kicad-cli returns an empty parity list either way, so
    # a 0 here would read as "measured, no differences" to anything consuming
    # metrics rather than checks. The findings block already made this
    # distinction; the metrics block did not, and an assertion pointed at the
    # metric duly reported PASS on a design whose parity was never checked -
    # the 0 == 0 agreement this file's own docstring warns about.
    parity_measured = "--schematic-parity" in argv
    metrics = {
        "violation_count": len(violations),
        "error_count": severities.get("error", 0),
        "warning_count": severities.get("warning", 0),
        "exclusion_count": severities.get("exclusion", 0),
        "unconnected_count": len(unconnected),
        "parity_difference_count": len(parity) if parity_measured else None,
    }
    summary = summarise(findings)
    # The metrics block above already refuses to write 0 for an unmeasured
    # parity; the STATUS did not make the same distinction. ``scanned == 0``
    # only catches the case where nothing at all was read, so a run without
    # --schematic-parity produced three PASS checks plus an explicit
    # ``schematic_parity`` NO_COVERAGE and still reported PASS - the verdict
    # promote.py reads to open its DRC gate. rollup_status is monotone: an
    # unmeasured check cannot be outvoted by the measured ones beside it.
    status = rollup_status(findings, excluded_checks)

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "drc_gate.py",
        "mode": mode,
        "status": status,
        "design": {
            "path": str(design),
            "sha256": sha256_file(design),
        },
        "project_file": (
            {"path": str(project), "sha256": sha256_file(project)}
            if project is not None else None),
        "engine": engine,
        "argv": argv,
        # The two external variables that move a DRC number without the design
        # moving, lifted to one place so a consumer does not have to know that
        # the version lives under ``engine`` and the flags under ``argv``.
        # ``engine_version`` is MEASURED (``kicad-cli version``), never a
        # constant in this file; ``argv_signature`` is the path-free part of
        # the invocation, which is the half that changes what gets counted.
        "provenance": {
            "engine_version": engine.get("version") or "",
            "engine_version_source": "kicad-cli version",
            "engine_reported_version": raw.get("kicad_version", ""),
            "argv_signature": argv_signature(argv),
        },
        "exit_code": exit_code,
        "started_utc": started,
        "finished_utc": finished,
        "metrics": metrics,
        "severities": severities,
        "census": census,
        "exclusions": exclusions,
        "coverage": {
            "eligible": scanned,
            "scanned": scanned,
            "excluded": 0,
            "violations": min(len(violations), scanned),
            "reason": (
                f"{mode.upper()}-eligible objects read from the design file: "
                + ", ".join(f"{k}={v}" for k, v in sorted(census.items()))),
        },
        "checks": findings,
        "summary": summary,
        "kicad_version": raw.get("kicad_version", ""),
        "violations": violations,
    }


# --------------------------------------------------------------------------- #
# Comparability guard - two DRC numbers are only a measurement together when
# the engine and the flags behind them are the same
# --------------------------------------------------------------------------- #
#: Argv tokens that name a FILE. They differ between any two runs (the gate
#: stages the design in a fresh temp directory every time) and say nothing
#: about what the engine was asked to check, so they are stripped before two
#: invocations are compared.
PATHISH_SUFFIXES = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".json")
#: Options whose VALUE is an output path; the value is dropped with the flag.
OUTPUT_FLAGS = ("-o", "--output")

#: Values that ``engine_version``/``kicad_version`` use to say "I do not
#: know". They must never compare equal to each other - two unknowns are not
#: the same version, they are two unanswered questions.
UNKNOWN_VERSIONS = ("", "unknown", "none", "null", "n/a")


class ComparisonRefused(RuntimeError):
    """Two DRC results could not be compared. Never a difference of zero."""


def argv_signature(argv) -> list[str] | None:
    """The part of a kicad-cli invocation that decides WHAT gets counted.

    Everything path-shaped is removed: the executable, ``-o <report>`` and
    the design file. What is left is the subcommand plus its flags, e.g.
    ``["pcb", "drc", "--format", "json", "--units", "mm", "--severity-all",
    "--exit-code-violations", "--all-track-errors"]``.

    This exists because ``--all-track-errors`` alone moved the same board
    from ~184 to 292 violations (see ``build_argv``). Two runs whose flag
    sets differ are answering two different questions, and subtracting one
    answer from the other produces a number that describes neither.

    Returns None when there is no argv to read - a raw ``kicad-cli`` JSON
    report records the KiCad version but NOT the command line, so the flags
    behind it are unknowable rather than assumed-default.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    out: list[str] = []
    skip = False
    for token in list(argv)[1:]:            # [0] is the resolved exe path
        token = str(token)
        if skip:
            skip = False
            continue
        if token in OUTPUT_FLAGS:
            skip = True
            continue
        if token.lower().endswith(PATHISH_SUFFIXES):
            continue
        out.append(token)
    return out


def _known_version(value) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in UNKNOWN_VERSIONS else text


def report_provenance(report, *, label: str = "report") -> dict:
    """Everything needed to decide whether ``report`` may be compared.

    Understands both artefact shapes this repo produces:

      * ``drc-gate-report/v1`` - version measured with ``kicad-cli version``,
        full argv, design digest, metrics;
      * a RAW ``kicad-cli`` DRC/ERC JSON (what ``--keep-raw`` writes and what
        a project's ``build/drc.json`` usually is) - carries
        ``kicad_version`` and nothing about the command line.

    An unrecognised document is reported as ``shape="unrecognised"`` with
    everything unknown rather than raising, so the caller can still emit a
    refusal report instead of a stack trace.
    """
    if not isinstance(report, dict):
        return {"label": label, "shape": "unrecognised", "engine_version": None,
                "engine_version_source": None, "argv_signature": None,
                "design_sha256": None, "mode": None, "metrics": {}}

    if report.get("schema_version") == SCHEMA_VERSION:
        engine = report.get("engine") or {}
        provenance = report.get("provenance") or {}
        version = (_known_version(engine.get("version"))
                   or _known_version(provenance.get("engine_version")))
        source = "kicad-cli version" if version else None
        if version is None:
            version = _known_version(report.get("kicad_version"))
            source = "report.kicad_version" if version else None
        signature = argv_signature(report.get("argv"))
        if signature is None and isinstance(provenance.get("argv_signature"), list):
            signature = [str(t) for t in provenance["argv_signature"]]
        metrics = report.get("metrics") or {}
        return {
            "label": label,
            "shape": SCHEMA_VERSION,
            "engine_version": version,
            "engine_version_source": source,
            "argv_signature": signature,
            "design_sha256": (report.get("design") or {}).get("sha256") or None,
            "mode": report.get("mode"),
            "metrics": {
                "violation_count": metrics.get("violation_count"),
                "error_count": metrics.get("error_count"),
                "warning_count": metrics.get("warning_count"),
                "unconnected_count": metrics.get("unconnected_count"),
            },
        }

    if any(key in report for key in ("violations", "sheets", "unconnected_items")):
        mode = "erc" if "sheets" in report else "drc"
        violations = flatten_violations(report, mode)
        return {
            "label": label,
            "shape": "kicad-cli-raw",
            "engine_version": _known_version(report.get("kicad_version")),
            "engine_version_source": (
                "report.kicad_version"
                if _known_version(report.get("kicad_version")) else None),
            # Deliberately None, not the default flags: a raw kicad-cli report
            # does not record how it was invoked, so whether it was produced
            # WITH --all-track-errors is unknowable from the file.
            "argv_signature": None,
            "design_sha256": None,
            "mode": mode,
            "metrics": {
                "violation_count": len(violations),
                "error_count": sum(
                    1 for v in violations
                    if str(v.get("severity", "")).lower() == "error"),
                "warning_count": sum(
                    1 for v in violations
                    if str(v.get("severity", "")).lower() == "warning"),
                "unconnected_count": len(report.get("unconnected_items", [])),
            },
        }

    return {"label": label, "shape": "unrecognised", "engine_version": None,
            "engine_version_source": None, "argv_signature": None,
            "design_sha256": None, "mode": None, "metrics": {}}


def comparability_blockers(left: dict, right: dict) -> list[dict]:
    """Every reason these two runs may not be subtracted from each other."""
    blockers: list[dict] = []
    for side in (left, right):
        if side["shape"] == "unrecognised":
            blockers.append({
                "code": "UNRECOGNISED_REPORT",
                "side": side["label"],
                "detail": ("neither a drc-gate-report/v1 nor a raw kicad-cli "
                           "DRC/ERC JSON; nothing in it can be trusted as a "
                           "violation count"),
            })
    if blockers:
        return blockers

    if left["mode"] != right["mode"]:
        blockers.append({
            "code": "MODE_MISMATCH",
            "side": "both",
            "detail": f"{left['label']}={left['mode']} vs "
                      f"{right['label']}={right['mode']}; DRC and ERC count "
                      "different things",
        })

    for side in (left, right):
        if not side["engine_version"]:
            blockers.append({
                "code": "ENGINE_VERSION_UNKNOWN",
                "side": side["label"],
                "detail": ("no measured kicad-cli version in this artefact; "
                           "an unknown version cannot be shown to match the "
                           "other side"),
            })
    if (left["engine_version"] and right["engine_version"]
            and left["engine_version"] != right["engine_version"]):
        blockers.append({
            "code": "ENGINE_VERSION_MISMATCH",
            "side": "both",
            "detail": f"{left['label']}=kicad-cli {left['engine_version']} vs "
                      f"{right['label']}=kicad-cli {right['engine_version']}; "
                      "a difference in the counts cannot be attributed to the "
                      "design while the tool also changed",
        })

    for side in (left, right):
        if side["argv_signature"] is None:
            blockers.append({
                "code": "INVOCATION_UNKNOWN",
                "side": side["label"],
                "detail": ("this artefact records no argv, so whether it was "
                           "produced with --all-track-errors (worth ~100 "
                           "violations on this repo's board) is unknowable"),
            })
    if (left["argv_signature"] is not None
            and right["argv_signature"] is not None
            and left["argv_signature"] != right["argv_signature"]):
        only_left = [t for t in left["argv_signature"]
                     if t not in right["argv_signature"]]
        only_right = [t for t in right["argv_signature"]
                      if t not in left["argv_signature"]]
        blockers.append({
            "code": "INVOCATION_MISMATCH",
            "side": "both",
            "detail": f"flags only in {left['label']}: {only_left or '-'}; "
                      f"only in {right['label']}: {only_right or '-'}; the two "
                      "runs were asked different questions",
        })
    return blockers


def compare_runs(left, right, *, left_label: str = "left",
                 right_label: str = "right") -> dict:
    """Compare two DRC/ERC results, or REFUSE and say why.

    THE GUARD: the comparison is refused unless both sides carry a known
    kicad-cli version, both versions are equal, both carry a known
    invocation, and the invocations are equal. This is not defensive
    politeness - the case that produced it is in this module's docstring: a
    committed report said 191 violations, the same unchanged board said
    something else here, and the only thing that had moved was kicad-cli
    10.0.5 -> 10.0.4. Subtracting those two numbers yields a figure that
    describes neither the design nor the tool.

    When the comparison IS allowed, the design digests decide what the delta
    means:

      * digests differ  -> ``attribution="design"``, the delta is the design
        change, which is the whole point of comparing;
      * digests equal   -> ``attribution="none_expected"``. A NON-ZERO delta
        here is the nondeterminism defect (``build_argv``), not progress, and
        is flagged as ``agreement=false``;
      * a digest missing -> ``attribution="unknown"``; a raw kicad-cli report
        does not carry one, so the delta cannot be pinned on anything.
    """
    a = report_provenance(left, label=left_label)
    b = report_provenance(right, label=right_label)
    blockers = comparability_blockers(a, b)

    if a["design_sha256"] and b["design_sha256"]:
        design_changed = a["design_sha256"] != b["design_sha256"]
        attribution = "design" if design_changed else "none_expected"
    else:
        design_changed = None
        attribution = "unknown"

    delta = None
    agreement = None
    if not blockers:
        delta = {}
        for key in ("violation_count", "error_count", "warning_count",
                    "unconnected_count"):
            lhs, rhs = a["metrics"].get(key), b["metrics"].get(key)
            delta[key] = (rhs - lhs) if isinstance(lhs, int) and isinstance(
                rhs, int) else None
        if attribution == "none_expected":
            agreement = all(value == 0 for value in delta.values()
                            if value is not None)

    verdict = "REFUSED" if blockers else "COMPARED"
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "tool": "drc_gate.py",
        "verdict": verdict,
        "comparable": not blockers,
        "compared_utc": utc_now(),
        "left": a,
        "right": b,
        "blockers": blockers,
        "design_changed": design_changed,
        "attribution": attribution,
        "delta": delta,
        "agreement": agreement,
        "reason": (
            "; ".join(f"{item['code']} ({item['side']}): {item['detail']}"
                      for item in blockers)
            if blockers else
            f"both runs produced by kicad-cli {a['engine_version']} with an "
            f"identical invocation; delta attributed to: {attribution}"),
    }


def load_report(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError(f"{path} is not JSON: {exc}") from exc


def compare_files(left: Path, right: Path) -> dict:
    for path in (left, right):
        if not Path(path).is_file():
            raise GateError(f"report not found: {path}")
    report = compare_runs(load_report(left), load_report(right),
                          left_label=str(left), right_label=str(right))
    report["inputs"] = [
        {"role": "left", "path": str(left), "sha256": sha256_file(Path(left))},
        {"role": "right", "path": str(right), "sha256": sha256_file(Path(right))},
    ]
    return report


# --------------------------------------------------------------------------- #
def detect_mode(design: Path) -> str:
    suffix = design.suffix.lower()
    if suffix == ".kicad_pcb":
        return "drc"
    if suffix == ".kicad_sch":
        return "erc"
    raise GateError(
        f"unsupported design file {design.name}: expected .kicad_pcb or .kicad_sch")


def find_project(design: Path, explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise GateError(f"project file not found: {path}")
        return path
    sibling = design.with_suffix(".kicad_pro")
    if sibling.is_file():
        return sibling
    candidates = sorted(design.parent.glob("*.kicad_pro"))
    return candidates[0] if len(candidates) == 1 else None


def gate(design: Path, *, kicad_cli: str | None = None,
         project: str | None = None, schematic_parity: bool = False,
         keep_raw: Path | None = None,
         exclusions: list[dict] | None = None) -> dict:
    design = Path(design).expanduser().resolve()
    if not design.is_file():
        raise GateError(f"design file not found: {design}")
    mode = detect_mode(design)
    exe = resolve_kicad_cli(kicad_cli)
    project_path = find_project(design, project)
    if mode == "drc":
        census = pcb_census(design.read_text(encoding="utf-8", errors="replace"))
    else:
        census = sch_census_set(sheet_set(design))

    started = utc_now()
    raw, argv, exit_code = run_engine(
        exe, mode, design, project_path,
        schematic_parity=schematic_parity, keep_raw=keep_raw)
    finished = utc_now()

    engine = {
        "name": "kicad-cli",
        "path": exe,
        "version": engine_version(exe),
        "sha256": sha256_file(exe) if Path(exe).is_file() else "",
    }
    return build_report(design, mode, engine, argv, exit_code, raw, census,
                        started, finished, project_path, exclusions)


# --------------------------------------------------------------------------- #
# Self-test - pure parsing, no KiCad needed
# --------------------------------------------------------------------------- #
SELF_TEST_PCB = """(kicad_pcb (version 20240108)
  (footprint "R_0402" (pad "1" smd rect) (pad "2" smd rect))
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu"))
  (via (at 1 0) (size 0.6) (drill 0.3))
  (zone (net 0) (layer "F.Cu"))
)"""

SELF_TEST_SCH = """(kicad_sch (version 20240108)
  (lib_symbols (symbol "Device:R" (symbol "Device:R_0_1")))
  (symbol (lib_id "Device:R") (at 0 0 0))
  (wire (pts (xy 0 0) (xy 1 0)))
  (junction (at 1 0))
  (label "NET1" (at 1 0 0))
  (no_connect (at 2 0))
)"""


def self_test() -> list[str]:
    problems: list[str] = []

    census = pcb_census(SELF_TEST_PCB)
    if census != {"footprint": 1, "segment": 1, "arc": 0, "via": 1,
                  "zone": 1, "pad": 2}:
        problems.append(f"pcb census wrong: {census}")

    sch = sch_census(SELF_TEST_SCH)
    if sch["symbol"] != 1:
        problems.append(
            f"schematic census counted lib_symbols as instances: {sch}")
    if (sch["wire"], sch["junction"], sch["label"], sch["no_connect"]) != (1, 1, 1, 1):
        problems.append(f"schematic census wrong: {sch}")

    argv = build_argv("kicad-cli", "drc", "b.kicad_pcb", "o.json")
    for flag in ("--exit-code-violations", "--severity-all"):
        if flag not in argv:
            problems.append(f"gate contract lost {flag}")

    # FAULT INJECTION: an empty design must not read as a clean design.
    empty = build_report(
        Path(__file__), "drc",
        {"name": "kicad-cli", "path": "", "version": "", "sha256": ""},
        argv, 0, {"violations": []},
        {name: 0 for name in (*PCB_OBJECTS, "pad")},
        utc_now(), utc_now(), None)
    if empty["status"] != NO_COVERAGE:
        problems.append("empty board reported as PASS instead of NO_COVERAGE")

    dirty = build_report(
        Path(__file__), "drc",
        {"name": "kicad-cli", "path": "", "version": "", "sha256": ""},
        argv, 0,
        {"violations": [{"severity": "error", "type": "clearance",
                         "description": "too close"}]},
        {"footprint": 1, "segment": 1, "arc": 0, "via": 0, "zone": 0, "pad": 2},
        utc_now(), utc_now(), None)
    if dirty["status"] != FAIL:
        problems.append("violations with exit 0 reported as PASS")

    # FAULT INJECTION: the comparability guard. Each case below is built by
    # BREAKING a report that would otherwise compare cleanly, so a guard that
    # silently stopped firing shows up here rather than in a review.
    def _stub(version: str, argv_list: list[str], digest: str,
              violations: int) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "drc",
            "engine": {"name": "kicad-cli", "path": "", "version": version,
                       "sha256": ""},
            "argv": argv_list,
            "design": {"path": "b.kicad_pcb", "sha256": digest},
            "metrics": {"violation_count": violations, "error_count": 0,
                        "warning_count": 0, "unconnected_count": 0},
        }

    good_argv = build_argv("kicad-cli", "drc", "b.kicad_pcb", "o.json")
    baseline = _stub("10.0.4", good_argv, "a" * 64, 191)

    same_tool = compare_runs(baseline, _stub("10.0.4", good_argv, "a" * 64, 191))
    if not same_tool["comparable"]:
        problems.append(
            f"identical engine+argv refused: {same_tool['blockers']}")

    other_version = compare_runs(baseline,
                                 _stub("10.0.5", good_argv, "a" * 64, 222))
    if other_version["comparable"] or other_version["delta"] is not None:
        problems.append("two kicad-cli versions compared instead of refused")
    if "ENGINE_VERSION_MISMATCH" not in {b["code"]
                                         for b in other_version["blockers"]}:
        problems.append("version mismatch not named as the blocker")

    no_version = compare_runs(baseline, _stub("", good_argv, "a" * 64, 222))
    if no_version["comparable"]:
        problems.append("an artefact with no measured version was compared")

    thin_argv = [t for t in good_argv if t != "--all-track-errors"]
    other_flags = compare_runs(baseline,
                               _stub("10.0.4", thin_argv, "a" * 64, 184))
    if other_flags["comparable"]:
        problems.append("two different invocations compared instead of refused")

    # Raw kicad-cli JSON records the version but never the command line.
    raw_pair = compare_runs({"kicad_version": "10.0.4", "violations": [],
                             "unconnected_items": []},
                            {"kicad_version": "10.0.4", "violations": [],
                             "unconnected_items": []})
    if raw_pair["comparable"]:
        problems.append("raw kicad-cli reports compared with unknown flags")

    # Same tool, same flags, same board digest, different count = the
    # nondeterminism defect, not progress.
    wobble = compare_runs(baseline, _stub("10.0.4", good_argv, "a" * 64, 205))
    if not wobble["comparable"] or wobble["agreement"] is not False:
        problems.append("same board + same tool disagreeing was not flagged")
    if wobble["attribution"] != "none_expected":
        problems.append(f"wrong attribution: {wobble['attribution']}")

    moved = compare_runs(baseline, _stub("10.0.4", good_argv, "b" * 64, 12))
    if moved["attribution"] != "design" or moved["delta"]["violation_count"] != -179:
        problems.append(f"design-change delta wrong: {moved['delta']}")
    return problems


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("design", nargs="?",
                        help=".kicad_pcb (DRC) or .kicad_sch (ERC)")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="write the report to this path")
    parser.add_argument("--kicad-cli", dest="kicad_cli",
                        help="full path to kicad-cli; overrides $KICAD_CLI")
    parser.add_argument("--project", help="companion .kicad_pro carrying the "
                                          "design rules (auto-detected)")
    parser.add_argument("--schematic-parity", action="store_true",
                        help="DRC only: also report schematic/board parity")
    parser.add_argument("--keep-raw", type=Path,
                        help="also keep the untouched kicad-cli JSON here")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="CHECK=REASON",
                        help="put one check deliberately out of scope, e.g. "
                             "--exclude schematic_parity='no schematic in this "
                             "scratch copy'. The reason is recorded in the "
                             "report. Only an unmeasured check can be "
                             "excluded; a measured FAIL still fails the gate. "
                             "Repeatable.")
    parser.add_argument("--compare", nargs=2, metavar=("LEFT", "RIGHT"),
                        type=Path,
                        help="compare two DRC/ERC reports instead of running "
                             "the engine. REFUSED (exit 2) unless both were "
                             "produced by the same measured kicad-cli version "
                             "with the same invocation flags.")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in parser self-test and exit")
    args = parser.parse_args(argv)
    exclusions = []
    for spec in args.exclude:
        check, sep, reason = spec.partition("=")
        if not sep or not reason.strip():
            parser.error(f"--exclude needs CHECK=REASON, got {spec!r}. An "
                         "exclusion with no recorded reason is indistinguish"
                         "able from a check that was never run.")
        exclusions.append({"check": check.strip(), "reason": reason.strip()})
    args.exclusions = exclusions
    if args.compare and args.design:
        parser.error("--compare reads two existing reports; it does not take "
                     "a DESIGN as well")
    if not args.self_test and not args.compare and not args.design:
        parser.error("DESIGN is required (a .kicad_pcb or .kicad_sch path)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        emit({"schema_version": SCHEMA_VERSION, "self_test": "PASS" if not
              problems else "FAIL", "problems": problems})
        return EXIT_OK if not problems else EXIT_FAIL

    if args.compare:
        comparison = compare_files(*args.compare)
        write_report(comparison, args.json_path)
        emit(comparison)
        diag(f"COMPARISON {comparison['verdict']}: {comparison['reason']}")
        if not comparison["comparable"]:
            # Exit 2, not 1: this is not "the two runs disagree", it is "no
            # comparison was possible", and the 0/1/2 contract reserves 2 for
            # a tool that could not produce an answer.
            return EXIT_ERROR
        if comparison["agreement"] is False:
            return EXIT_FAIL
        return EXIT_OK

    report = gate(Path(args.design), kicad_cli=args.kicad_cli,
                  project=args.project,
                  schematic_parity=args.schematic_parity,
                  keep_raw=args.keep_raw,
                  exclusions=args.exclusions)
    write_report(report, args.json_path)
    emit(report)
    diag(f"{report['mode'].upper()} {report['status']}: "
         f"{report['metrics']['violation_count']} violation(s), "
         f"scanned={report['coverage']['scanned']}")
    return EXIT_OK if report["status"] == PASS else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
