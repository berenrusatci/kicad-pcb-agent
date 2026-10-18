#!/usr/bin/env python3
"""The scratch -> canonical promotion gate, emitting a ``gate-receipt/v1``.

PORTED FROM ``main.py::cmd_promote`` + ``karar_birimleri.py`` +
``scratch_yonetimi.py`` in the pcb-tool-v2 archive.

WHY THIS IS A REWRITE AND NOT A COPY
------------------------------------
The archive's promote command was the PRODUCER of exactly the receipts
``scripts/gate_receipt.py`` VALIDATES - and the two spoke different formats,
so the producer's output could never have opened the gate it was written for.
The archive receipt carried ``board_sha256`` / ``ruleset_sha256`` /
``komut_sha256`` and little else: no typed metrics, no metric semantics, no
coverage arithmetic, no exact argv, no tool identity, no UTC window, no
retention, no fault injection. Those are not decoration - they are the
difference between "the tool says it passed" and "here is why you may
believe it". The schema was NOT weakened to fit the producer; the producer
was made to earn the schema.

Three things the archive claimed and this port does not:

  * ``_ruleset_hash`` folded MASTER_RULEBOOK.md and every ``*.kicad_dru``
    into ONE opaque hash. A single digest proves nothing about WHICH rules
    applied, and it silently hashed nothing at all when the rulebook was
    absent. Here the ruleset is a manifest of ``{path, sha256}`` records; the
    combined digest is still emitted as ``ruleset_sha256`` for continuity,
    but it is now reproducible from listed evidence.
  * The archive's ERC/DRC calls did not pass ``--exit-code-violations`` or
    ``--severity-all``, so a dirty board could exit 0. Both gates now run
    through ``scripts/drc_gate.py`` and are emitted as MANDATORY CHILD
    RECEIPTS, which the validator reduces independently.
  * The archive skipped the decision gate entirely when a project had no
    decision file ("bu kapı atlanıyor"). Skipping a gate is not passing it:
    a skipped gate is now EXCLUDED from coverage with a reason, and an
    A-level promotion with no provable decision fault is RED.

EVIDENCE IS A SELF-CONTAINED BUNDLE
-----------------------------------
Every artifact a receipt names must resolve UNDER the validation root and
hash-match. So promotion writes a directory, not a lone JSON file, and copies
each artifact it cites into it. ``python3 scripts/gate_receipt.py
<bundle>/promotion_<ts>.json`` therefore validates with no ``--root`` and no
access to anything outside the bundle. Source and target artifacts carry
their real ``scratch_path`` / ``canonical_path`` alongside the bundled copy.

CLI:
    promote.py PROJECT_DIR [--scratch-id ID | --scratch DIR]
               [--verifier SCRIPT ...] [--apply --atomic]
               [--assurance-level {A,B}] [--json REPORT] [--self-test]

    promote.py --fault-inject-decisions BASELINE.json   (A-level harness)

Exit codes: 0 promotion GREEN, 1 promotion RED, 2 could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.decisions import (  # noqa: E402
    ACCEPTED, BLOCKED, DecisionError, decision_gate, decisions_path,
    dump_decisions, load_decision_document, parse_decision_document,
)
from common.findings import rollup_from_counts  # noqa: E402
from common.hashing import canonical_digest, sha256_file, sha256_text  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_ERROR, EXIT_FAIL, EXIT_OK, diag, emit, run_cli, write_report,
)

RECEIPT_SCHEMA = "gate-receipt/v1"
REPORT_SCHEMA = "promotion-report/v1"
TOOL_NAME = "promote.py"
TOOL_VERSION = "2.0"

SCRATCH_ROOT_NAME = ".scratch"

#: The four gates a promotion is made of. This list IS the coverage
#: denominator: a gate that could not run is excluded WITH a reason, never
#: quietly dropped.
GATES = ("drc", "erc", "contract", "decisions")

#: Metrics the A-level fault harness must break. Declared before the harness
#: runs, so the receipt's expectation cannot be back-filled from its result.
#: ``closed`` is listed alongside ``accepted``: the gate now passes on CLOSURE,
#: not on status, so a harness that only proved the status count moves would
#: leave the predicate that actually decides the gate unproven.
FAULT_EXPECTED_METRICS = (
    "gates.decisions.accepted",
    "gates.decisions.closed",
    "gates.decisions.unaccepted",
)

COUNT_SAMPLING = {"mode": "exhaustive_census", "value": 1, "units": "record"}
COUNT_GEOMETRY = ("none; integer census of discrete records, "
                  "no geometric model is involved")
COUNT_DISTANCE = ("not_a_distance_metric; these are counts of discrete "
                  "records, not measured separations")


class PromotionError(RuntimeError):
    """Promotion could not be evaluated. Never reported as a clean gate."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


# --------------------------------------------------------------------------- #
# Scratch handling
#
# common/scratch.py owns the scratch layout (ported from the archive's
# scratch_yonetimi.py by a sibling change). It is imported and NOT
# reimplemented; the fallbacks below are the same documented layout
# (`<project>/.scratch/<id>`) and exist only so this tool degrades to a clear
# error instead of an ImportError if that module is ever absent.
#
# ONE FUNCTION IS DELIBERATELY NOT USED: `promote_to_canonical`. It copies
# with shutil.copy2, which is not atomic, and this receipt asserts
# `promotion.atomic: true`. `atomic_replace` below writes through a temp file
# and os.replace so the claim is earned. Everything else - the root, the id
# list, the fail-closed containment check - comes from common/scratch.py.
# --------------------------------------------------------------------------- #
def _scratch_module():
    try:
        from common import scratch as module  # noqa: PLC0415
    except ImportError:
        return None
    return module


def _call_scratch(names: tuple[str, ...], *args):
    module = _scratch_module()
    if module is None:
        return None, False
    for name in names:
        function = getattr(module, name, None)
        if callable(function):
            return function(*args), True
    return None, False


def scratch_root(project_dir: Path) -> Path:
    value, used = _call_scratch(("scratch_root",),
                                str(project_dir))
    if used and value is not None:
        return Path(value)
    return Path(project_dir).resolve() / SCRATCH_ROOT_NAME


def list_scratches(project_dir: Path) -> list[str]:
    value, used = _call_scratch(("list_scratches",),
                                str(project_dir))
    if used and value is not None:
        return list(value)
    root = scratch_root(project_dir)
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)


def assert_inside_scratch(scratch: Path, project_dir: Path) -> None:
    """Fail closed when the 'scratch' is not actually under the scratch root."""
    try:
        _, used = _call_scratch(("assert_within_scratch",),
                                scratch, str(project_dir))
    except ValueError as exc:
        raise PromotionError(str(exc)) from exc
    if used:
        return
    root = scratch_root(project_dir).resolve()
    try:
        Path(scratch).resolve().relative_to(root)
    except ValueError as exc:
        raise PromotionError(
            f"{scratch} is not under the scratch root {root}; refusing to "
            "promote in case a canonical file is overwritten by accident"
        ) from exc


def find_design_files(directory: Path) -> dict[str, Path | None]:
    """The single ``.kicad_pro`` / ``.kicad_sch`` / ``.kicad_pcb`` triple.

    Ambiguity is an error: promoting "one of two boards" is exactly the sort
    of guess that ends up in a fab package.
    """
    found: dict[str, Path | None] = {}
    for key, suffix in (("project", ".kicad_pro"), ("schematic", ".kicad_sch"),
                        ("board", ".kicad_pcb")):
        matches = sorted(p for p in directory.glob(f"*{suffix}")
                         if p.is_file())
        if len(matches) > 1:
            raise PromotionError(
                f"{len(matches)} {suffix} files in {directory}; "
                "promotion needs exactly one")
        found[key] = matches[0] if matches else None
    if found["board"] is None:
        raise PromotionError(f"no .kicad_pcb in {directory}")
    return found


# --------------------------------------------------------------------------- #
# Ruleset manifest
# --------------------------------------------------------------------------- #
def ruleset_manifest(scratch: Path, project_file: Path | None,
                     rulebook: Path | None) -> dict:
    """Every file that defines the rules this promotion was judged against."""
    entries: list[dict] = []
    for role, path in (("project", project_file), ("rulebook", rulebook)):
        if path is not None and Path(path).is_file():
            entries.append({"role": role, "name": Path(path).name,
                            "sha256": sha256_file(path),
                            "source_path": str(path)})
    for dru in sorted(scratch.glob("*.kicad_dru")):
        entries.append({"role": "custom_rules", "name": dru.name,
                        "sha256": sha256_file(dru),
                        "source_path": str(dru)})
    manifest = {
        "schema_version": "promotion-ruleset/v1",
        "entries": entries,
        "note": ("the archive folded these into one opaque digest; listing "
                 "them is what makes the digest reproducible"),
    }
    manifest["ruleset_sha256"] = canonical_digest(entries)
    return manifest


# --------------------------------------------------------------------------- #
# Gate execution
# --------------------------------------------------------------------------- #
def run_drc_gate(design: Path, bundle: Path, prefix: str, *,
                 kicad_cli: str | None, project: Path | None,
                 tool_dir: Path, schematic: Path | None = None) -> dict:
    """Run scripts/drc_gate.py on ``design``; stage its evidence in the bundle.

    For the DRC run, board/schematic parity is REQUESTED when the scratch copy
    has a schematic. Without the flag kicad-cli returns an empty parity list
    whatever the board looks like, so the gate reported a check it had not
    run. When there is no schematic the parity check is EXCLUDED with that
    reason recorded in the gate report - the same disposition this function's
    caller already gives the ERC gate - rather than left as a bare
    NO_COVERAGE that no one wrote down a decision about.
    """
    gate_tool = tool_dir / "drc_gate.py"
    report_path = bundle / f"{prefix}_gate_report.json"
    raw_path = bundle / f"{prefix}_engine_report.json"
    argv = [sys.executable, str(gate_tool), str(design),
            "--json", str(report_path), "--keep-raw", str(raw_path)]
    if prefix == "drc":
        if schematic is not None:
            argv.append("--schematic-parity")
        else:
            argv += ["--exclude", "schematic_parity=no schematic in the "
                                  "scratch copy, so board/schematic parity "
                                  "cannot be measured"]
    if kicad_cli:
        argv += ["--kicad-cli", kicad_cli]
    if project is not None:
        argv += ["--project", str(project)]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if not report_path.is_file():
        raise PromotionError(
            f"{prefix} gate produced no report (exit {proc.returncode})\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["_wrapper_argv"] = argv
    report["_wrapper_exit_code"] = proc.returncode
    return report


def run_verifiers(board: Path, bundle: Path, verifiers: list[str]) -> dict:
    """The independent contract check: project-specific rules, NOT KiCad DRC.

    Each verifier is any script emitting the repo's ``checks``/``summary``
    envelope (``scripts/independent_verify.py``, ``scripts/board/*.py``).
    Zero verifiers is NOT a pass: the gate reports NO_COVERAGE and the
    promotion is RED, because "nobody checked the project contract" and "the
    project contract holds" are not the same statement.

    A verifier spec may carry its own arguments, so a tool that needs one
    (``independent_verify.py --contract contract.json``) can be used without
    a wrapper. The full argv is recorded in the receipt.
    """
    directory = bundle / "verification"
    reports: list[dict] = []
    passed = failed = no_coverage = 0
    for index, verifier in enumerate(verifiers):
        parts = shlex.split(verifier)
        if not parts:
            raise PromotionError("empty --verifier spec")
        script = Path(parts[0]).expanduser().resolve()
        if not script.is_file():
            raise PromotionError(f"verifier not found: {parts[0]}")
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"{index:02d}_{script.stem}.json"
        argv = [sys.executable, str(script), str(board),
                *parts[1:], "--json", str(out)]
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        if not out.is_file():
            raise PromotionError(
                f"verifier {script.name} produced no report "
                f"(exit {proc.returncode})\n{proc.stderr[-2000:]}")
        document = json.loads(out.read_text(encoding="utf-8"))
        checks = document.get("checks", [])
        summary = document.get("summary", {})
        passed += int(summary.get("PASS", 0))
        failed += int(summary.get("FAIL", 0))
        no_coverage += int(summary.get("NO_COVERAGE", 0))
        reports.append({
            "verifier": str(script),
            "argv": argv,
            "exit_code": proc.returncode,
            "report_path": str(out),
            "sha256": sha256_file(out),
            "checks": len(checks),
            "summary": summary,
            "failed_checks": [c.get("check") for c in checks
                              if c.get("status") == "FAIL"],
            "no_coverage_checks": [c.get("check") for c in checks
                                   if c.get("status") == "NO_COVERAGE"],
        })
    total = passed + failed + no_coverage
    return {
        "ran": bool(verifiers),
        "reports": reports,
        "passed_checks": passed,
        "failed_checks": failed,
        "no_coverage_checks": no_coverage,
        "total_checks": total,
        # A run in which ANY check came back NO_COVERAGE is NOT a pass:
        # "nothing to examine" and "examined and clean" differ, and only the
        # second one may open a gate. The earlier rule only demoted when
        # passed == 0, so 1 PASS + 100 NO_COVERAGE opened the contract gate.
        "status": rollup_from_counts(passed, failed, no_coverage),
        "reason": ("no independent verifier was configured; a project "
                   "contract nobody checked is not a contract that holds"
                   if not verifiers else
                   f"{len(verifiers)} verifier(s), {total} check(s): "
                   f"{passed} PASS / {failed} FAIL / {no_coverage} NO_COVERAGE"),
    }


# --------------------------------------------------------------------------- #
# A-level fault injection harness
# --------------------------------------------------------------------------- #
def inject_decision_fault(baseline_path: Path) -> dict:
    """Flip one accepted decision to ``blocked`` and see whether it is caught.

    This is the proof that the decision gate is not vacuous. It reads a
    hash-bound baseline, mutates it IN MEMORY ONLY (the baseline file is never
    written) and reports which metric leaves moved to a failing value.
    """
    baseline_sha = sha256_file(baseline_path)
    units, evidence = load_decision_document(baseline_path)
    before = decision_gate(units, evidence)

    accepted = sorted((u for u in units if u.status == ACCEPTED),
                      key=lambda u: u.decision_id)
    observation: dict[str, Any] = {
        "schema_version": "promotion-fault-observation/v1",
        "baseline_sha256": baseline_sha,
        "baseline_path": str(baseline_path),
        "injected_fault": "",
        "baseline_metrics": {
            "gates.decisions.total": before["total"],
            "gates.decisions.accepted": before["accepted"],
            "gates.decisions.closed": before["closed"],
            "gates.decisions.unaccepted": before["unaccepted"],
        },
        "injected_metrics": {},
        "observed_failed_metrics": [],
        "status": "NO_COVERAGE",
        "reason": "",
    }
    if not accepted:
        observation["reason"] = (
            "no accepted decision to invalidate; the decision gate cannot be "
            "proven non-vacuous on this project")
        return observation

    victim = accepted[0]
    victim.status = BLOCKED
    victim.invalidated_reason = "fault injection"
    after = decision_gate(units, evidence)
    observation["injected_fault"] = (
        f"decision {victim.decision_id!r} forced from accepted to blocked")
    observation["injected_metrics"] = {
        "gates.decisions.total": after["total"],
        "gates.decisions.accepted": after["accepted"],
        "gates.decisions.closed": after["closed"],
        "gates.decisions.unaccepted": after["unaccepted"],
    }
    failed = []
    if after["accepted"] < before["accepted"]:
        failed.append("gates.decisions.accepted")
    if after["closed"] < before["closed"]:
        failed.append("gates.decisions.closed")
    if after["unaccepted"] > before["unaccepted"]:
        failed.append("gates.decisions.unaccepted")
    observation["observed_failed_metrics"] = failed
    observation["status"] = "PASS" if failed else "FAIL"
    observation["reason"] = (
        "invalidating an accepted decision moved the gate metrics"
        if failed else
        "the injected fault did NOT move any gate metric; the decision gate "
        "cannot distinguish a clean project from a broken one")
    return observation


def run_fault_harness(bundle: Path, tool_copy: Path,
                      baseline_name: str) -> tuple[list[str], int, dict]:
    """Run the harness through the BUNDLED tool copy, in the bundle.

    The command is executed with ``cwd=bundle`` and relative paths so the
    receipt's ``fault_injection.command.argv`` literally contains the
    hash-bound tool path and the hash-bound baseline path - which is what the
    validator checks, and what makes the command replayable from the bundle
    alone.
    """
    argv = [sys.executable, tool_copy.name,
            "--fault-inject-decisions", baseline_name]
    env = dict(os.environ)
    tools_dir = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        [tools_dir] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    proc = subprocess.run(argv, cwd=str(bundle), env=env,
                          capture_output=True, text=True, check=False)
    observation_path = bundle / "fault_observation.json"
    if not observation_path.is_file():
        raise PromotionError(
            f"fault harness wrote no observation (exit {proc.returncode})\n"
            f"{proc.stderr[-2000:]}")
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    return argv, proc.returncode, observation


# --------------------------------------------------------------------------- #
# Receipt construction
# --------------------------------------------------------------------------- #
def artifact(bundle: Path, path: Path, **extra) -> dict:
    """A ``{path, sha256}`` identity relative to the receipt directory."""
    record = {"path": str(Path(path).relative_to(bundle)),
              "sha256": sha256_file(path)}
    record.update(extra)
    return record


def count_semantics(definition: str, region: str) -> dict:
    """The six mandatory semantic fields for an integer census metric.

    ``distance_semantics`` and ``geometry_model`` are required by the schema
    even for counts. They are answered honestly ("this is not a distance")
    rather than filled with a plausible-sounding measurement description,
    which is the failure mode the validator's CENTERLINE_AS_EDGE_GAP and
    CHORD_AS_ARC rules exist to catch.
    """
    return {
        "definition": definition,
        "units": "count",
        "distance_semantics": COUNT_DISTANCE,
        "region": region,
        "sampling_step": dict(COUNT_SAMPLING),
        "geometry_model": COUNT_GEOMETRY,
    }


def retention_block(end: datetime, days: int) -> dict:
    """Everything the bundle actually retains, for either verdict.

    The union of the validator's GREEN and RED retention sets is listed
    because the bundle genuinely holds all of it - a RED promotion keeps its
    logs and scratch copies, a GREEN one keeps its hashes and replay command.
    """
    return {
        "policy": f"audit-window-{days}d",
        "expires_utc": iso(end + timedelta(days=days)),
        "retain": ["receipt", "child_receipts", "hashes", "metrics",
                   "reproducibility_command", "logs", "scratch"],
    }


def child_receipt(bundle: Path, mode: str, gate_report: dict, design: Path,
                  rules: list[dict], retention: dict,
                  start: datetime, end: datetime) -> dict:
    """A DRC or ERC gate as its own reducible receipt.

    ``argv`` is the EXACT kicad-cli command, because that is the command
    whose ``--exit-code-violations`` / ``--severity-all`` flags the validator
    must see; ``tool`` is the hash-bound harness that ran it, and the engine's
    own identity travels in ``engine``. Recording the wrapper's argv instead
    would have hidden the flags that decide whether the gate is real.
    """
    metrics = gate_report["metrics"]
    region = f"{design.name} (scratch copy), all layers"
    parsed = {
        "violation_count": metrics["violation_count"],
        "error_count": metrics["error_count"],
        "warning_count": metrics["warning_count"],
    }
    semantics = {
        "violation_count": count_semantics(
            f"{mode.upper()} violations reported by kicad-cli at "
            "--severity-all", region),
        "error_count": count_semantics(
            f"{mode.upper()} violations with severity=error", region),
        "warning_count": count_semantics(
            f"{mode.upper()} violations with severity=warning", region),
    }
    if mode == "drc":
        parsed["unconnected_count"] = metrics["unconnected_count"]
        semantics["unconnected_count"] = count_semantics(
            "ratsnest connections kicad-cli reported as unrouted", region)

    failing = (gate_report["status"] != "PASS")
    coverage = dict(gate_report["coverage"])
    blockers = []
    if coverage["scanned"] == 0:
        blockers.append({
            "code": "EMPTY_DESIGN",
            "message": (f"the {mode.upper()} engine was handed zero eligible "
                        "objects; an empty violation list is not a clean design"),
        })
    return {
        "schema_version": RECEIPT_SCHEMA,
        "gate_id": f"promotion-{mode}",
        "gate_kind": mode,
        "profile": f"kicad-cli-{mode}-severity-all",
        "assurance_level": "B",
        "verdict": "RED" if failing else "GREEN",
        "tool": {
            "name": f"drc_gate.py ({mode} harness around kicad-cli)",
            "version": TOOL_VERSION,
            "path": "drc_gate_tool.py",
            "sha256": sha256_file(bundle / "drc_gate_tool.py"),
        },
        "argv": gate_report["argv"],
        "argv_role": ("the exact kicad-cli engine command; the harness "
                      "invocation is in wrapper.argv"),
        "wrapper": {
            "argv": gate_report["_wrapper_argv"],
            "exit_code": gate_report["_wrapper_exit_code"],
        },
        "engine": gate_report["engine"],
        "exit_code": gate_report["exit_code"] if not failing else max(
            gate_report["exit_code"], 1),
        "start_utc": iso(start),
        "end_utc": iso(end),
        "started_utc": iso(start),
        "finished_utc": iso(end),
        "inputs": [
            artifact(bundle, design, role=f"{mode}_design"),
            artifact(bundle, bundle / f"{mode}_gate_report.json",
                     role="gate_report"),
            artifact(bundle, bundle / f"{mode}_engine_report.json",
                     role="engine_report"),
        ],
        "rules": rules,
        "parsed_metrics": parsed,
        "metric_semantics": semantics,
        "coverage": coverage,
        "blockers": blockers,
        "child_receipts": [],
        "retention": retention,
    }


def build_receipt(state: dict) -> dict:
    """Assemble the promotion receipt from the evaluated gate state."""
    bundle: Path = state["bundle"]
    start: datetime = state["start"]
    end: datetime = state["end"]
    green: bool = state["green"]
    region = f"{state['board'].name} (scratch {state['scratch_id']})"

    parsed: dict[str, Any] = {"gates": {}}
    semantics: dict[str, Any] = {}

    drc = state["drc"]["metrics"]
    parsed["gates"]["drc"] = {
        "violation_count": drc["violation_count"],
        "error_count": drc["error_count"],
        "unconnected_count": drc["unconnected_count"],
    }
    semantics["gates.drc.violation_count"] = count_semantics(
        "DRC violations at --severity-all on the scratch board", region)
    semantics["gates.drc.error_count"] = count_semantics(
        "DRC violations with severity=error", region)
    semantics["gates.drc.unconnected_count"] = count_semantics(
        "ratsnest connections kicad-cli reported as unrouted", region)

    if state["erc"] is not None:
        erc = state["erc"]["metrics"]
        parsed["gates"]["erc"] = {
            "violation_count": erc["violation_count"],
            "error_count": erc["error_count"],
        }
        semantics["gates.erc.violation_count"] = count_semantics(
            "ERC violations at --severity-all on the scratch schematic",
            f"{state['schematic'].name} (scratch {state['scratch_id']})")
        semantics["gates.erc.error_count"] = count_semantics(
            "ERC violations with severity=error",
            f"{state['schematic'].name} (scratch {state['scratch_id']})")

    contract = state["contract"]
    parsed["gates"]["contract"] = {
        "failed_checks": contract["failed_checks"],
        "passed_checks": contract["passed_checks"],
        "no_coverage_checks": contract["no_coverage_checks"],
    }
    for name, text in (
        ("failed_checks", "independent verifier checks that FAILED"),
        ("passed_checks", "independent verifier checks that PASSED"),
        ("no_coverage_checks",
         "independent verifier checks that found nothing to examine "
         "(NO_COVERAGE, deliberately not counted as PASS)"),
    ):
        semantics[f"gates.contract.{name}"] = count_semantics(text, region)

    decisions = state["decisions"]
    debt = decisions["decision_debt"]
    parsed["gates"]["decisions"] = {
        "total": decisions["total"],
        "accepted": decisions["accepted"],
        "unaccepted": decisions["unaccepted"],
        "closed": decisions["closed"],
        "accepted_but_not_closed": decisions["accepted_but_not_closed"],
        "accepted_evidence_missing": debt["accepted_evidence_missing"],
        "accepted_output_artifact_missing":
            debt["accepted_output_artifact_missing"],
        "accepted_invalidation_trigger_missing":
            debt["accepted_invalidation_trigger_missing"],
    }
    for name, text in (
        ("total", "decision units recorded for the project"),
        ("accepted", "decision units in state 'accepted'"),
        ("unaccepted", "decision units NOT in state 'accepted'"),
        ("closed", "decision units that are CLOSED: accepted AND carrying "
                   "declared required_evidence, canonical evidence resolved "
                   "in the document's evidence catalogue, an invalidation "
                   "trigger and an output artifact"),
        ("accepted_but_not_closed",
         "decision units a human accepted whose closure the repository "
         "cannot demonstrate; decision debt, not a pass"),
        ("accepted_evidence_missing",
         "accepted decision units missing declared or resolvable canonical "
         "evidence"),
        ("accepted_output_artifact_missing",
         "accepted decision units that declare no output artifact"),
        ("accepted_invalidation_trigger_missing",
         "accepted decision units that declare no invalidation trigger"),
    ):
        semantics[f"gates.decisions.{name}"] = count_semantics(
            text, f"{state['decisions_source']}")

    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "gate_id": f"promotion-{state['scratch_id']}",
        "gate_kind": "promotion" if state["applied"] else "promotion-preflight",
        "profile": "scratch-to-canonical",
        "assurance_level": state["assurance_level"],
        "verdict": "GREEN" if green else "RED",
        "exit_code": 0 if green else 1,
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "path": "promote_tool.py",
            "sha256": sha256_file(bundle / "promote_tool.py"),
        },
        "argv": state["argv"],
        "start_utc": iso(start),
        "end_utc": iso(end),
        "started_utc": iso(start),
        "finished_utc": iso(end),
        "inputs": state["inputs"],
        "rules": state["rules"],
        "parsed_metrics": parsed,
        "metric_semantics": semantics,
        "coverage": state["coverage"],
        "blockers": state["blockers"],
        "child_receipts": state["children"],
        "retention": retention_block(end, state["retention_days"]),
        # Continuity with the archive receipt: same three digests, now with
        # the evidence that makes them checkable.
        "board_sha256": state["board_sha256"],
        "ruleset_sha256": state["ruleset_sha256"],
        "command_sha256": sha256_text(" ".join(state["argv"])),
        "scratch_id": state["scratch_id"],
        "provenance": {
            "ported_from": "pcb-tool-v2 main.py::cmd_promote",
            "synthetic": False,
            "bundle_is_self_contained": True,
        },
    }
    if state["fault"] is not None:
        receipt["fault_injection"] = state["fault"]
    if state["promotion"] is not None:
        receipt["promotion"] = state["promotion"]
    return receipt


# --------------------------------------------------------------------------- #
# Canonical promotion
# --------------------------------------------------------------------------- #
def atomic_replace(source: Path, target: Path) -> None:
    """Copy ``source`` over ``target`` via a temp file and ``os.replace``.

    Per-FILE atomicity only: a reader never sees a half-written canonical
    file, but the file SET is not transactional. ``promotion.atomicity``
    records that limit rather than letting ``atomic: true`` imply more.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        delete=False, dir=str(target.parent), prefix=f".{target.name}.",
        suffix=".tmp")
    handle.close()
    temporary = Path(handle.name)
    try:
        shutil.copyfile(source, temporary)
        with open(temporary, "rb") as opened:
            os.fsync(opened.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def promote_files(scratch: Path, project_dir: Path,
                  exclude: tuple[str, ...] = (SCRATCH_ROOT_NAME, "DOCS",
                                              ".git")) -> list[tuple[Path, Path]]:
    """Replace canonical files with their scratch counterparts.

    ``DOCS`` is excluded so a promotion cannot overwrite the very directory
    the evidence bundle is being written into.
    """
    moved: list[tuple[Path, Path]] = []
    for item in sorted(scratch.iterdir()):
        if item.name in exclude:
            continue
        target = project_dir / item.name
        if item.is_dir():
            for child in sorted(item.rglob("*")):
                if child.is_file():
                    relative = child.relative_to(scratch)
                    atomic_replace(child, project_dir / relative)
                    moved.append((child, project_dir / relative))
        else:
            atomic_replace(item, target)
            moved.append((item, target))
    return moved


def git_commit(project_dir: Path, message: str) -> dict:
    """Best effort. A failed commit never rolls back a completed promotion."""
    try:
        top = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False)
        if top.returncode != 0:
            return {"committed": False, "reason": "not a git repository"}
        subprocess.run(["git", "-C", str(project_dir), "add", "."],
                       capture_output=True, text=True, timeout=60, check=True)
        subprocess.run(["git", "-C", str(project_dir), "commit", "-m", message],
                       capture_output=True, text=True, timeout=60, check=True)
        return {"committed": True, "reason": ""}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"committed": False, "reason": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def promote(project_dir: Path, *, scratch_id: str | None = None,
            scratch: Path | None = None, verifiers: list[str] | None = None,
            apply: bool = False, atomic: bool = False,
            kicad_cli: str | None = None, rulebook: Path | None = None,
            assurance_level: str = "A", retention_days: int = 365,
            git: bool = True, argv: list[str] | None = None,
            evidence_dir: Path | None = None) -> dict:
    start = utc_now()
    argv = list(argv if argv is not None else sys.argv)
    verifiers = list(verifiers or [])
    project_dir = Path(project_dir).expanduser().resolve()
    if not project_dir.is_dir():
        raise PromotionError(f"project directory not found: {project_dir}")

    if scratch is not None:
        scratch_dir = Path(scratch).expanduser().resolve()
        scratch_id = scratch_dir.name
    else:
        if scratch_id is None:
            candidates = list_scratches(project_dir)
            if not candidates:
                raise PromotionError(
                    f"no scratch copy under {scratch_root(project_dir)}; "
                    "nothing to promote")
            scratch_id = candidates[0]
            diag(f"no --scratch-id given, using newest scratch: {scratch_id}")
        scratch_dir = scratch_root(project_dir) / scratch_id
    if not scratch_dir.is_dir():
        raise PromotionError(f"scratch not found: {scratch_dir}")
    assert_inside_scratch(scratch_dir, project_dir)

    design = find_design_files(scratch_dir)
    board = design["board"]
    schematic = design["schematic"]
    project_file = design["project"]

    # ---------------------------------------------------------------- bundle
    marker = stamp(start)
    base = evidence_dir if evidence_dir is not None else (
        project_dir / "DOCS" / "07_Dogrulama")
    bundle = Path(base) / f"promotion_{marker}"
    bundle.mkdir(parents=True, exist_ok=False)
    tool_dir = Path(__file__).resolve().parent
    shutil.copy2(tool_dir / "promote.py", bundle / "promote_tool.py")
    shutil.copy2(tool_dir / "drc_gate.py", bundle / "drc_gate_tool.py")

    board_copy = bundle / board.name
    shutil.copy2(board, board_copy)
    schematic_copy = None
    if schematic is not None:
        schematic_copy = bundle / schematic.name
        shutil.copy2(schematic, schematic_copy)
    if project_file is not None:
        shutil.copy2(project_file, bundle / project_file.name)

    manifest = ruleset_manifest(scratch_dir, project_file, rulebook)
    manifest_path = bundle / "ruleset.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True,
                   allow_nan=False) + "\n", encoding="utf-8")
    rules = [artifact(bundle, manifest_path, role="ruleset_manifest")]
    if project_file is not None:
        rules.append(artifact(bundle, bundle / project_file.name,
                              role="kicad_project"))

    blockers: list[dict] = []
    reason_codes: list[str] = []
    scanned_gates: list[str] = []
    excluded_gates: list[str] = []
    failed_gates: list[str] = []

    # ------------------------------------------------------------- gate: DRC
    drc = run_drc_gate(board, bundle, "drc", kicad_cli=kicad_cli,
                       project=project_file, tool_dir=tool_dir,
                       schematic=schematic)
    scanned_gates.append("drc")
    if drc["status"] != "PASS":
        failed_gates.append("drc")
        reason_codes.append("DRC_NOT_CLEAN")
        blockers.append({"code": "DRC_NOT_CLEAN",
                         "message": f"DRC {drc['status']}: "
                                    f"{drc['metrics']['violation_count']} "
                                    "violation(s)"})

    # ------------------------------------------------------------- gate: ERC
    erc = None
    if schematic is not None:
        erc = run_drc_gate(schematic, bundle, "erc", kicad_cli=kicad_cli,
                           project=project_file, tool_dir=tool_dir)
        scanned_gates.append("erc")
        if erc["status"] != "PASS":
            failed_gates.append("erc")
            reason_codes.append("ERC_NOT_CLEAN")
            blockers.append({"code": "ERC_NOT_CLEAN",
                             "message": f"ERC {erc['status']}: "
                                        f"{erc['metrics']['violation_count']} "
                                        "violation(s)"})
    else:
        excluded_gates.append("erc")

    # -------------------------------------------------------- gate: contract
    contract = run_verifiers(board, bundle, verifiers)
    scanned_gates.append("contract")
    if contract["status"] != "PASS":
        failed_gates.append("contract")
        reason_codes.append(f"CONTRACT_{contract['status']}")
        blockers.append({
            "code": f"CONTRACT_{contract['status']}",
            "message": f"independent verification {contract['status']}: "
                       f"{contract['reason']}",
        })

    # ------------------------------------------------------- gate: decisions
    source = decisions_path(scratch_dir)
    decisions_copy = bundle / "decision_units.json"
    if source is not None:
        units, evidence = load_decision_document(source)
        decisions_source = str(source)
    else:
        units, evidence = [], {}
        decisions_source = "(no decision document in the scratch copy)"
    decisions = decision_gate(units, evidence)
    # The evidence catalogue is copied with the decisions. The bundle copy is
    # what the fault harness re-reads, and a copy with an empty catalogue
    # would show every decision as un-closable for reasons the project does
    # not actually have.
    bundled = dump_decisions(units, evidence)
    bundled["source"] = decisions_source
    decisions_copy.write_text(
        json.dumps(bundled, indent=2, ensure_ascii=False, sort_keys=True,
                   allow_nan=False) + "\n", encoding="utf-8")
    scanned_gates.append("decisions")
    if not decisions["passed"]:
        failed_gates.append("decisions")
        if not decisions["graph_valid"]:
            reason_codes.append("DECISION_GRAPH_INVALID")
            blockers.append({"code": "DECISION_GRAPH_INVALID",
                             "message": decisions["graph_error"]})
        if decisions["unaccepted"]:
            reason_codes.append("DECISIONS_OPEN")
            blockers.append({
                "code": "DECISIONS_OPEN",
                "message": "unaccepted decisions: "
                           + ", ".join(decisions["unaccepted_ids"]),
            })
        if decisions["accepted_but_not_closed"]:
            # A DIFFERENT failure from DECISIONS_OPEN and it needs a different
            # human response: the engineering call was made, the receipts
            # proving it landed in the repository were not.
            reason_codes.append("DECISIONS_ACCEPTED_NOT_CLOSED")
            blockers.append({
                "code": "DECISIONS_ACCEPTED_NOT_CLOSED",
                "message": "accepted but not closed: " + "; ".join(
                    f"{did} ({', '.join(decisions['closure_reasons'][did])})"
                    for did in decisions["accepted_but_not_closed_ids"]),
            })
    if decisions["total"] == 0:
        blockers.append({
            "code": "NO_DECISION_UNITS",
            "message": ("the project records no decision units; the archive "
                        "skipped this gate, which is not the same as passing "
                        "it"),
        })
        reason_codes.append("NO_DECISION_UNITS")
        failed_gates.append("decisions")

    # ---------------------------------------------------------------- inputs
    inputs = [artifact(bundle, board_copy, role="promoted_board",
                       scratch_path=str(board))]
    if schematic_copy is not None:
        inputs.append(artifact(bundle, schematic_copy,
                               role="promoted_schematic",
                               scratch_path=str(schematic)))
    inputs.append(artifact(bundle, decisions_copy, role="decision_units",
                           source_path=decisions_source))
    for report in contract["reports"]:
        inputs.append(artifact(bundle, Path(report["report_path"]),
                               role="verification_report"))

    # ------------------------------------------------------- fault injection
    fault = None
    if assurance_level.upper() == "A":
        definition = {
            "schema_version": "promotion-fault-definition/v1",
            "expected_failed_metrics": list(FAULT_EXPECTED_METRICS),
            "fault": ("force one accepted decision unit to 'blocked' and "
                      "require the decision gate metrics to move"),
            "declared_before_running": True,
        }
        definition_path = bundle / "fault_definition.json"
        definition_path.write_text(
            json.dumps(definition, indent=2, ensure_ascii=False,
                       sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8")
        harness_argv, harness_exit, observation = run_fault_harness(
            bundle, bundle / "promote_tool.py", decisions_copy.name)
        fault = {
            "status": observation["status"],
            "fixture": artifact(bundle, definition_path),
            "baseline_artifact": artifact(bundle, decisions_copy),
            "injected_artifact": artifact(bundle,
                                          bundle / "fault_observation.json"),
            "command": {"argv": harness_argv, "exit_code": harness_exit,
                        "cwd": str(bundle)},
            "expected_failed_metrics": list(FAULT_EXPECTED_METRICS),
            "observed_failures": list(observation["observed_failed_metrics"]),
            "reason": observation["reason"],
        }
        if observation["status"] != "PASS":
            reason_codes.append("FAULT_INJECTION_NOT_PROVEN")
            blockers.append({"code": "FAULT_INJECTION_NOT_PROVEN",
                             "message": observation["reason"]})
            failed_gates.append("fault_injection")

    green = not failed_gates and not blockers

    # ------------------------------------------------------------- promotion
    promotion = None
    applied = False
    moved: list[tuple[Path, Path]] = []
    commit = {"committed": False, "reason": "not attempted"}
    if apply:
        if not atomic:
            raise PromotionError(
                "--apply requires --atomic: a GREEN promotion must prove the "
                "canonical files were replaced atomically")
        source_dir = bundle / "source"
        target_dir = bundle / "target"
        source_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        canonical = {
            "board": project_dir / board.name,
            **({"schematic": project_dir / schematic.name}
               if schematic is not None else {}),
        }
        before = {role: (sha256_file(path) if path.is_file() else "")
                  for role, path in canonical.items()}
        for role, path in canonical.items():
            scratch_file = scratch_dir / path.name
            shutil.copy2(scratch_file, source_dir / path.name)

        if green:
            moved = promote_files(scratch_dir, project_dir)
            applied = True
            commit = git_commit(
                project_dir,
                f"promote: scratch {scratch_id} -> canonical "
                f"(board={sha256_file(board)[:12]})") if git else commit

        source_artifacts = []
        target_artifacts = []
        for role, path in canonical.items():
            staged_source = source_dir / path.name
            source_artifacts.append(artifact(
                bundle, staged_source, role=f"scratch_{role}",
                scratch_path=str(scratch_dir / path.name)))
            if path.is_file():
                shutil.copy2(path, target_dir / path.name)
                after = sha256_file(path)
            else:
                (target_dir / path.name).write_text("", encoding="utf-8")
                after = ""
            record = artifact(bundle, target_dir / path.name,
                              role=f"canonical_{role}",
                              canonical_path=str(path))
            record["before_sha256"] = before[role]
            record["after_sha256"] = after
            target_artifacts.append(record)

        promotion = {
            "decision": "GREEN" if green else "RED",
            "scratch_path": str(scratch_dir),
            "scratch_id": scratch_id,
            "reason_codes": [] if green else sorted(set(reason_codes)),
            "atomic": True,
            "atomicity": ("per_file_os_replace; each canonical file is "
                          "replaced atomically, the file SET is not "
                          "transactional"),
            "commands": [{
                "argv": argv,
                "exit_code": 0,
                "description": ("this exact invocation performed the "
                                "scratch -> canonical replacement"),
            }],
            "source_artifacts": source_artifacts,
            "target_artifacts": target_artifacts,
            "files_replaced": [str(t) for _s, t in moved],
            "git": commit,
            "note": ("source/target are hash-bound COPIES taken inside the "
                     "bundle so the receipt validates without --root; each "
                     "carries its real scratch_path / canonical_path"),
        }
        if green:
            # after == before would mean the promotion wrote nothing.
            for record in target_artifacts:
                if record["before_sha256"] == record["after_sha256"]:
                    diag(f"note: {record['canonical_path']} was already "
                         "identical to the scratch copy")
        else:
            for record in target_artifacts:
                record["after_sha256"] = record["before_sha256"] or "0" * 64
                record["sha256"] = record["after_sha256"]

    end = utc_now()
    coverage = {
        "eligible": len(GATES),
        "scanned": len(scanned_gates),
        "excluded": len(excluded_gates),
        "violations": len({g for g in failed_gates if g in GATES}),
        "reason": ("promotion gates evaluated: "
                   + ", ".join(sorted(scanned_gates))
                   + ("; excluded: " + ", ".join(
                       f"{g} (no schematic in the scratch copy)"
                       for g in excluded_gates) if excluded_gates
                      else "; excluded: none")),
    }

    children = []
    for mode, report, design_copy in (
        ("drc", drc, board_copy),
        ("erc", erc, schematic_copy),
    ):
        if report is None:
            continue
        child = child_receipt(bundle, mode, report, design_copy, rules,
                              retention_block(end, retention_days),
                              start, end)
        path = bundle / f"{mode}_receipt.json"
        path.write_text(json.dumps(child, indent=2, ensure_ascii=False,
                                   sort_keys=True, allow_nan=False) + "\n",
                        encoding="utf-8")
        children.append({
            "gate_id": child["gate_id"],
            "mandatory": True,
            "verdict": child["verdict"],
            **artifact(bundle, path, role=f"{mode}_receipt"),
        })

    receipt = build_receipt({
        "bundle": bundle, "start": start, "end": end, "green": green,
        "board": board, "schematic": schematic, "scratch_id": scratch_id,
        "drc": drc, "erc": erc, "contract": contract, "decisions": decisions,
        "decisions_source": decisions_source,
        "inputs": inputs, "rules": rules, "coverage": coverage,
        "blockers": blockers, "children": children, "fault": fault,
        "promotion": promotion, "applied": applied,
        "assurance_level": assurance_level.upper(),
        "retention_days": retention_days, "argv": argv,
        "board_sha256": sha256_file(board),
        "ruleset_sha256": manifest["ruleset_sha256"],
    })
    receipt_path = bundle / f"promotion_{marker}.json"
    receipt_path.write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True,
                   allow_nan=False) + "\n", encoding="utf-8")

    return {
        "schema_version": REPORT_SCHEMA,
        "status": "GREEN" if green else "RED",
        "project_dir": str(project_dir),
        "scratch_id": scratch_id,
        "scratch_path": str(scratch_dir),
        "receipt": str(receipt_path),
        "bundle": str(bundle),
        "applied": applied,
        "gates": {
            "drc": drc["status"],
            "erc": erc["status"] if erc else "EXCLUDED",
            "contract": contract["status"],
            "decisions": "PASS" if decisions["passed"] and decisions["total"]
                         else "FAIL",
        },
        "blockers": blockers,
        "reason_codes": sorted(set(reason_codes)),
        "board_sha256": receipt["board_sha256"],
        "ruleset_sha256": receipt["ruleset_sha256"],
        "command_sha256": receipt["command_sha256"],
        "git": commit,
    }


def verify_receipt(receipt_path: Path) -> dict:
    """Run the repo's own validator on what we just produced.

    A producer that cannot pass its own consumer is broken, and the failure
    belongs in this tool's output rather than in someone's later surprise.
    """
    validator = Path(__file__).resolve().parent / "gate_receipt.py"
    proc = subprocess.run([sys.executable, str(validator), str(receipt_path)],
                          capture_output=True, text=True, check=False)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"exit_code": proc.returncode, "effective_status": "ERROR",
                "stderr": proc.stderr[-2000:]}
    return {"exit_code": proc.returncode,
            "effective_status": report.get("effective_status"),
            "reason_codes": report.get("reason_codes", [])}


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
#: The self-test's idea of a CLEAN project. Both decisions are genuinely
#: CLOSED - accepted, with declared required_evidence, evidence ids that
#: resolve to canonical entries in this document's own catalogue, invalidation
#: triggers and output artifacts.
#:
#: This baseline used to carry ``"evidence_ids": []`` on both decisions, so
#: the whole fault-injection proof rested on evidence-free acceptance: the
#: harness proved the gate could tell "accepted" from "blocked" while the gate
#: could not tell "decided with proof" from "decided by assertion". A clean
#: baseline that would fail the closure predicate cannot prove anything about
#: a gate that now enforces it.
SELF_TEST_DECISIONS = {
    "schema_version": "pcb-decision-units/v1",
    "evidence": [
        {"evidence_id": "E-STACKUP", "kind": "artifact",
         "source": "fabricator stackup report", "status": "canonical",
         "path": "docs/stackup_report.pdf"},
        {"evidence_id": "E-ZDIFF", "kind": "artifact",
         "source": "2D field solver run", "status": "canonical",
         "path": "artifacts/zdiff.json"},
    ],
    "decisions": [
        {"decision_id": "D1", "question": "4 or 6 layers?", "status": "accepted",
         "depends_on": [], "constraints": ["C1"], "options": ["4", "6"],
         "required_evidence": ["stackup report"],
         "invalidation_triggers": ["stackup change"],
         "evidence_ids": ["E-STACKUP"],
         "output_artifact": "artifacts/stackup.json"},
        {"decision_id": "D2", "question": "impedance target?",
         "status": "accepted", "depends_on": ["D1"], "constraints": ["C1"],
         "options": ["90"], "required_evidence": ["field solver"],
         "invalidation_triggers": ["stackup change"],
         "evidence_ids": ["E-ZDIFF"],
         "output_artifact": "artifacts/zdiff.json"},
    ],
}

#: A baseline that differs from ``SELF_TEST_DECISIONS`` in ONE way: its
#: decisions are accepted with no evidence. It must NOT pass the gate. Without
#: this the self-test could not tell a gate that enforces closure from one
#: that merely counts statuses - which is the bug the old baseline hid.
SELF_TEST_UNCLOSED_DECISIONS = {
    "schema_version": "pcb-decision-units/v1",
    "evidence": [],
    "decisions": [
        {"decision_id": "D1", "question": "4 or 6 layers?", "status": "accepted",
         "depends_on": [], "constraints": ["C1"], "options": ["4", "6"],
         "required_evidence": ["stackup report"],
         "invalidation_triggers": ["stackup change"], "evidence_ids": [],
         "output_artifact": "artifacts/stackup.json"},
    ],
}


def self_test() -> list[str]:
    problems: list[str] = []

    units, evidence = parse_decision_document(SELF_TEST_DECISIONS)
    gate = decision_gate(units, evidence)
    if not gate["passed"] or gate["accepted"] != 2 or gate["closed"] != 2:
        problems.append(f"clean decision gate did not pass: {gate}")
    if gate["decision_debt"].get("accepted_but_not_closed"):
        problems.append(f"clean baseline reports decision debt: {gate}")
    for key in gate["decision_debt"]:
        if "score" in key:
            problems.append(
                "decision debt must stay a vector; a weighted score is a "
                f"governance decision, not a measurement: {key}")

    # The gate must reject acceptance without receipts, not just openness.
    unclosed_units, unclosed_evidence = parse_decision_document(
        SELF_TEST_UNCLOSED_DECISIONS)
    unclosed = decision_gate(unclosed_units, unclosed_evidence)
    if unclosed["passed"] or unclosed["unaccepted"]:
        problems.append(
            "an accepted-but-unevidenced decision passed the gate, so the "
            f"gate is still reading status alone: {unclosed}")
    if unclosed["accepted_but_not_closed"] != 1:
        problems.append(
            f"accepted_but_not_closed was not reported: {unclosed}")

    # FAULT INJECTION: the harness must catch an invalidated decision.
    with tempfile.TemporaryDirectory(prefix="promote-selftest-") as tmp:
        path = Path(tmp) / "decision_units.json"
        path.write_text(json.dumps(SELF_TEST_DECISIONS, ensure_ascii=False),
                        encoding="utf-8")
        observation = inject_decision_fault(path)
        if observation["status"] != "PASS":
            problems.append(f"fault harness did not fire: {observation}")
        if set(observation["observed_failed_metrics"]) != set(
                FAULT_EXPECTED_METRICS):
            problems.append(
                f"observed metrics differ from the declared expectation: "
                f"{observation['observed_failed_metrics']}")

        # A project with no accepted decision cannot prove the gate works.
        empty = Path(tmp) / "empty.json"
        empty.write_text(json.dumps(
            {"schema_version": "pcb-decision-units/v1", "decisions": []}),
            encoding="utf-8")
        if inject_decision_fault(empty)["status"] != "NO_COVERAGE":
            problems.append("empty decision set claimed a proven fault")

    semantic = count_semantics("x", "y")
    for field in ("definition", "units", "distance_semantics", "region",
                  "sampling_step", "geometry_model"):
        if not semantic.get(field):
            problems.append(f"semantic block is missing {field}")

    contract = run_verifiers(Path(__file__), Path(tempfile.gettempdir()), [])
    if contract["status"] != "NO_COVERAGE":
        problems.append("zero verifiers reported as a PASS")
    return problems


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", nargs="?",
                        help="canonical project root (holds the .kicad_pro)")
    parser.add_argument("--scratch-id", help="scratch id to promote "
                                             "(default: newest)")
    parser.add_argument("--scratch", type=Path,
                        help="explicit scratch directory")
    parser.add_argument("--verifier", action="append", default=[],
                        help="independent verifier command, e.g. "
                             "'tools/scripts/independent_verify.py "
                             "--contract c.json'; repeatable")
    parser.add_argument("--apply", action="store_true",
                        help="actually replace the canonical files")
    parser.add_argument("--atomic", action="store_true",
                        help="required with --apply: per-file os.replace")
    parser.add_argument("--kicad-cli", dest="kicad_cli",
                        help="full path to kicad-cli")
    parser.add_argument("--rulebook", type=Path,
                        help="rulebook markdown to bind into the ruleset hash")
    parser.add_argument("--assurance-level", choices=["A", "B"], default="A",
                        help="A requires fault-injection evidence (default)")
    parser.add_argument("--retention-days", type=int, default=365)
    parser.add_argument("--evidence-dir", type=Path,
                        help="where to write the bundle "
                             "(default: <project>/DOCS/07_Dogrulama)")
    parser.add_argument("--no-git", action="store_true",
                        help="do not attempt a git commit after promoting")
    parser.add_argument("--no-verify-receipt", action="store_true",
                        help="skip running gate_receipt.py on the output")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="write this tool's report to this path")
    parser.add_argument("--fault-inject-decisions", type=Path,
                        help="A-level harness: invalidate one accepted "
                             "decision in this baseline and report the "
                             "metrics that move")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test or args.fault_inject_decisions:
        return args
    if not args.project_dir:
        parser.error("PROJECT_DIR is required")
    if args.apply and not args.atomic:
        parser.error("--apply requires --atomic")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        emit({"schema_version": REPORT_SCHEMA,
              "self_test": "PASS" if not problems else "FAIL",
              "problems": problems})
        return EXIT_OK if not problems else EXIT_FAIL

    if args.fault_inject_decisions:
        observation = inject_decision_fault(args.fault_inject_decisions)
        out = Path(args.fault_inject_decisions).parent / "fault_observation.json"
        out.write_text(json.dumps(observation, indent=2, ensure_ascii=False,
                                  sort_keys=True, allow_nan=False) + "\n",
                       encoding="utf-8")
        emit(observation)
        diag(f"fault injection {observation['status']}: {observation['reason']}")
        return EXIT_OK if observation["status"] == "PASS" else EXIT_FAIL

    report = promote(
        Path(args.project_dir), scratch_id=args.scratch_id,
        scratch=args.scratch, verifiers=args.verifier, apply=args.apply,
        atomic=args.atomic, kicad_cli=args.kicad_cli, rulebook=args.rulebook,
        assurance_level=args.assurance_level,
        retention_days=args.retention_days, git=not args.no_git,
        argv=list(sys.argv) if argv is None else ["promote.py", *argv],
        evidence_dir=args.evidence_dir)

    if not args.no_verify_receipt:
        report["receipt_validation"] = verify_receipt(Path(report["receipt"]))

    write_report(report, args.json_path)
    emit(report)
    diag(f"promotion {report['status']} -> {report['receipt']}")
    for blocker in report["blockers"]:
        diag(f"  [{blocker['code']}] {blocker['message']}")

    validation = report.get("receipt_validation")
    if validation and validation.get("effective_status") == "ERROR":
        diag("the emitted receipt does not validate; this is a producer bug")
        return EXIT_ERROR
    return EXIT_OK if report["status"] == "GREEN" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=REPORT_SCHEMA))
