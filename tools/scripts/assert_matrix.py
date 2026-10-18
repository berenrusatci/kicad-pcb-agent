#!/usr/bin/env python3
"""Run the verification matrix as code instead of reading it as prose.

WHY THIS EXISTS
--------------------------------------------------------------------------
``skills/dogrulama-matrisi`` already maps every rule to a level (A/B/C/D),
a tool, a loop (L0..L3) and an action on failure. That mapping is only as
live as the last person who read it. This tool reads it instead: it checks
that every matrix row has an assertion, evaluates each assertion against a
report some tool already produced, and - with ``--prove`` - injects the
declared fault to demonstrate the assertion can actually fail.

IT MEASURES NOTHING
--------------------------------------------------------------------------
No check here re-derives an engineering quantity. An assertion binds a
claim to a LOCATION in another tool's report; the tool stays the single
source of the measurement. Re-computing "is the annular ring wide enough"
in this file would give the repo two answers to one question, which is what
#51 and #54 were both about.

Usage:
    assert_matrix.py --list-operators
    assert_matrix.py --set assertions.json --matrix SKILL.md
    assert_matrix.py --set assertions.json --evidence drc=build/drc.json
    assert_matrix.py --set assertions.json --evidence drc=build/drc.json --prove
    assert_matrix.py --set assertions.json --evidence drc=build/drc_gate.json \
        --root proj/ --artifact drc=proj.kicad_pcb
    assert_matrix.py --self-test

Assertion set (``assertion-set/v1``):
    {"schema_version": "assertion-set/v1",
     "evidence": {"drc": {"artifact": "board.kicad_pcb",
                          "hash_selector": "design.sha256"}},
     "assertions": [{...}]}

The ``evidence`` block is where an assertion gets tied to the artifact it
describes. Declaring it is optional and NOT free: without it a passing run
is SCREEN_ONLY, never GREEN - the same call the matrix already made in
section 5 for the tools it cannot hash-bind.

A ``--evidence`` report is only as good as the selector that reads it. The
paths in the set must be the paths the producing tool WRITES: an assertion
whose selector names a field no tool emits is NO_COVERAGE forever, and it
looks identical to an assertion nobody has run yet. ``--artifact`` exists so
a set can stay project-independent while still binding to a real board -
the alternative is one copy of the set per project, and two copies drift.

Exit codes: 0 = GREEN, 1 = RED / NO_COVERAGE / SCREEN_ONLY, 2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.assertions import (EVIDENCE_UNBOUND, GREEN, OPERATORS,  # noqa: E402
                               RED, SCREEN_ONLY, UNASSERTED_RULE,
                               AssertionSyntaxError, Evidence,
                               authority_summary, establishment_summary,
                               evaluate, load_assertion, prove, rollup)
from common.findings import (FAIL, Finding, NO_COVERAGE, PASS,  # noqa: E402
                             result, summarise)
from common.hashing import sha256_file  # noqa: E402
from common.matrix import cross_reference, read_matrix  # noqa: E402
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "assertion-report/v1"
TOOL_NAME = "assert_matrix"
INPUT_SCHEMA_VERSION = "assertion-set/v1"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def load_set(path: Path) -> Tuple[List[Any], List[dict], Dict[str, dict]]:
    """Return (assertions, syntax_errors, evidence_declarations).

    A syntax error is COLLECTED, not raised: one malformed assertion must
    not hide the state of the other forty. It still makes the run RED - a
    rule that does not load is a rule nothing enforces.
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: assertion set must be a JSON object")
    declared = doc.get("schema_version")
    if declared != INPUT_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be "
                         f"{INPUT_SCHEMA_VERSION!r}, got {declared!r}")
    entries = doc.get("assertions")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'assertions' must be a non-empty array")
    evidence_decl = doc.get("evidence", {})
    if not isinstance(evidence_decl, dict):
        raise ValueError(f"{path}: 'evidence' must be an object")

    assertions, errors, seen = [], [], set()
    for entry in entries:
        try:
            assertion = load_assertion(entry)
        except AssertionSyntaxError as exc:
            errors.append(exc.to_dict())
            continue
        if assertion.id in seen:
            errors.append({"assertion": assertion.id, "issue": "DUPLICATE_ID",
                           "detail": "two assertions share one id; a report "
                                     "keyed by id would silently drop one"})
            continue
        seen.add(assertion.id)
        assertions.append(assertion)
    return assertions, errors, evidence_decl


def parse_overrides(pairs: List[str]) -> Dict[str, str]:
    """``NAME=PATH`` for --artifact. An override that names nothing is a
    refusal at parse time rather than a silently ignored flag: a typo here
    would otherwise leave the run bound to the placeholder in the set."""
    overrides: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--artifact must be NAME=PATH, got {pair!r}")
        name, _, path = pair.partition("=")
        name, path = name.strip(), path.strip()
        if not name or not path:
            raise ValueError(f"--artifact has an empty name or path: {pair!r}")
        overrides[name] = path
    return overrides


def load_evidence(pairs: List[str], declarations: Dict[str, dict],
                  root: Path,
                  artifacts: Optional[Dict[str, str]] = None,
                  ) -> Tuple[Dict[str, Evidence], List[dict]]:
    """Read each ``NAME=path.json`` and tie it to its artifact when declared.

    ``artifacts`` replaces the artifact PATH an evidence source declares,
    never its ``hash_selector``: which file a report measured is a per-project
    fact, where the digest sits inside the report is a property of the tool.
    An override is not a way around the binding - an overridden path that is
    not on disk still lands in ARTIFACT_MISSING.
    """
    evidence: Dict[str, Evidence] = {}
    problems: List[dict] = []
    overrides = artifacts or {}
    for name in sorted(set(overrides) - set(declarations)):
        problems.append({
            "evidence": name, "issue": "ARTIFACT_OVERRIDE_UNKNOWN",
            "detail": f"--artifact {name}=... names an evidence source the "
                      "set does not declare, so it binds nothing"})
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--evidence must be NAME=PATH, got {pair!r}")
        name, _, raw_path = pair.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"--evidence has an empty name: {pair!r}")
        document = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{raw_path}: an evidence report must be an object")

        source = Evidence(name=name, document=document, path=raw_path)
        binding = declarations.get(name)
        if isinstance(binding, dict):
            artifact = overrides.get(name, binding.get("artifact"))
            selector = binding.get("hash_selector")
            if artifact and selector:
                from common.assertions import parse_selector
                artifact_path = (root / artifact) if not Path(artifact).is_absolute() \
                    else Path(artifact)
                if artifact_path.exists():
                    source.artifact = str(artifact)
                    source.artifact_digest = sha256_file(artifact_path)
                    source.hash_selector = selector
                    source.hash_path = parse_selector(selector, f"evidence:{name}")
                else:
                    problems.append({
                        "evidence": name, "issue": "ARTIFACT_MISSING",
                        "detail": f"{artifact!r} is declared as the artifact "
                                  "this report measured and is not on disk, so "
                                  "the report cannot be tied to anything"})
        evidence[name] = source
    return evidence, problems


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def loadability_finding(total: int, errors: List[dict]) -> Finding:
    return result("assertions_loadable", total, errors,
                  f"{total} assertion(s) in the set; every refusal is at LOAD "
                  "time, so a broken rule cannot lie dormant until the day its "
                  "evidence happens to show up")


def binding_finding(evidence: Dict[str, Evidence],
                    problems: List[dict]) -> Finding:
    """A declared binding that names a file which is not there is a FAILURE,
    not a quiet fallback to unbound.

    The distinction matters because the fallback is the attacker-shaped
    mistake: if a typo in an artifact path silently downgraded the run to
    "no binding declared", then the cheapest way to stop a staleness check
    from ever firing would be to misspell the path it watches.
    """
    if not evidence:
        return Finding("evidence_bindable", NO_COVERAGE, 0, [],
                       "no evidence reports were supplied")
    bound = sum(1 for source in evidence.values() if source.bound)
    return result("evidence_bindable", len(evidence), problems,
                  f"{bound}/{len(evidence)} evidence report(s) are tied by "
                  "digest to the artifact they measured; the rest can produce "
                  "a real result but never a GREEN one")


def evaluation_finding(outcomes) -> Finding:
    if not outcomes:
        return Finding("assertions_hold", NO_COVERAGE, 0, [],
                       "no assertion was evaluated")
    failures = [o.to_dict() for o in outcomes if o.status == FAIL]
    uncovered = [o for o in outcomes if o.status == NO_COVERAGE]
    checked = [o for o in outcomes if o.status != NO_COVERAGE]
    if failures:
        return result("assertions_hold", len(checked) or len(outcomes), failures,
                      f"{len(failures)} assertion(s) the design does not satisfy")
    if not checked:
        return Finding("assertions_hold", NO_COVERAGE, 0, [],
                       f"all {len(uncovered)} assertion(s) were uncoverable; "
                       "nothing was established")
    if uncovered:
        return Finding("assertions_hold", NO_COVERAGE, len(checked), [],
                       f"{len(checked)} assertion(s) hold, {len(uncovered)} "
                       "uncoverable - a partial check is not a passing one")
    return result("assertions_hold", len(checked), [],
                  f"all {len(checked)} assertion(s) hold")


def proof_finding(proofs) -> Finding:
    """An assertion nothing can falsify is not a check.

    This is the matrix's own acceptance criterion, made measurable: no
    checker counts as live without positive AND negative fault injection.
    """
    if not proofs:
        return Finding("assertions_falsifiable", NO_COVERAGE, 0, [],
                       "no assertion carried a falsifier to apply")
    unproven = [p.to_dict() for p in proofs if not p.proven]
    return result("assertions_falsifiable", len(proofs), unproven,
                  f"{len(proofs) - len(unproven)}/{len(proofs)} assertion(s) "
                  "demonstrably break when the fault they name is injected")


def matrix_findings(coverage) -> List[Finding]:
    """Two findings, because an unasserted rule and a misbound assertion are
    different kinds of wrong.

    A rule nobody asserted is ABSENCE OF COVERAGE: nothing was established
    about it, and calling that a failure would report a violation where
    there is only silence. An assertion bound to a rule that does not exist,
    or claiming a level the matrix disagrees with, IS a defect - it passes
    forever and reads as coverage while enforcing nothing.
    """
    if not coverage.rows:
        return [Finding("rules_asserted", NO_COVERAGE, 0, [],
                        "the matrix parsed to zero rules; an empty comparison "
                        "is not agreement, it is absence of coverage")]
    asserted = len(coverage.rows) - len(coverage.unasserted)
    if coverage.unasserted:
        gap = Finding("rules_asserted", NO_COVERAGE, asserted, [],
                      f"{asserted}/{len(coverage.rows)} matrix rule(s) carry an "
                      f"assertion; the other {len(coverage.unasserted)} are "
                      "enforced only as prose")
        gap["unasserted"] = [{"rule": row.rule, "issue": UNASSERTED_RULE,
                              "level": row.level_raw, "section": row.section}
                             for row in coverage.unasserted]
    else:
        gap = result("rules_asserted", len(coverage.rows), [],
                     f"every one of {len(coverage.rows)} matrix rule(s) is "
                     "asserted")
    misbound = list(coverage.orphans)
    misbound += [entry for entry in coverage.matched if "issue" in entry]
    binding = result("assertion_rule_binding", max(asserted, 1), misbound,
                     f"{len(misbound)} assertion(s) bound to a rule that does "
                     "not exist or to a level the matrix disagrees with")
    return [gap, binding]


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

def overall(findings: List[Finding], outcomes) -> Tuple[str, List[str]]:
    """A finding-level FAIL is RED; otherwise the assertion roll-up decides.

    SCREEN_ONLY survives only when nothing else lowered the verdict, which
    is the point: an unbound-evidence run that also has a real failure is
    RED, not screening.
    """
    counts = summarise(findings)
    if counts[FAIL]:
        return RED, sorted({f["check"].upper() for f in findings
                            if f["status"] == FAIL})
    status, reasons = rollup(outcomes)
    if counts[NO_COVERAGE] and status == GREEN:
        return NO_COVERAGE, sorted({f["check"].upper() for f in findings
                                    if f["status"] == NO_COVERAGE})
    if counts[NO_COVERAGE] and status == SCREEN_ONLY:
        return NO_COVERAGE, sorted({f["check"].upper() for f in findings
                                    if f["status"] == NO_COVERAGE})
    return status, reasons


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the verification matrix as executable assertions.")
    parser.add_argument("--set", dest="assertion_set", type=Path,
                        help=f"assertion set ({INPUT_SCHEMA_VERSION})")
    parser.add_argument("--evidence", action="append", default=[],
                        metavar="NAME=PATH",
                        help="a report some tool already produced")
    parser.add_argument("--matrix", type=Path,
                        help="dogrulama-matrisi SKILL.md; cross-references "
                             "rules against assertions. READ ONLY")
    parser.add_argument("--root", type=Path, default=Path("."),
                        help="base for artifact paths in the evidence block")
    parser.add_argument("--artifact", action="append", default=[],
                        metavar="NAME=PATH",
                        help="point an evidence source at the real file it "
                             "measured, overriding the name declared in the "
                             "set. The set stays project-independent; a second "
                             "copy of it per board is how the two copies drift")
    parser.add_argument("--prove", action="store_true",
                        help="inject each declared fault and require the "
                             "assertion to catch it")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--list-operators", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    if args.list_operators:
        emit({"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
              "operators": [{"name": op.name, "numeric": op.numeric,
                             "reads_length": op.wants_sequence,
                             "describes": op.describe}
                            for op in sorted(OPERATORS.values(),
                                             key=lambda o: o.name)]})
        return 0

    if not args.assertion_set:
        raise ValueError("--set is required (or use --list-operators)")

    assertions, syntax_errors, evidence_decl = load_set(args.assertion_set)
    evidence, evidence_problems = load_evidence(args.evidence, evidence_decl,
                                                args.root,
                                                parse_overrides(args.artifact))

    outcomes = [evaluate(a, evidence) for a in assertions]
    proofs = [p for p in (prove(a, evidence) for a in assertions)
              if p is not None] if args.prove else []

    findings = [loadability_finding(len(assertions) + len(syntax_errors),
                                    syntax_errors),
                binding_finding(evidence, evidence_problems),
                evaluation_finding(outcomes)]
    coverage = None
    if args.matrix:
        rows, parse_problems = read_matrix(args.matrix)
        coverage = cross_reference(rows, assertions)
        coverage.problems = parse_problems
        findings.extend(matrix_findings(coverage))
    if args.prove:
        findings.append(proof_finding(proofs))

    status, reason_codes = overall(findings, outcomes)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "verification",
        "measures_nothing": True,
        "assertion_set": str(args.assertion_set),
        "assertions": [a.to_dict() for a in assertions],
        "syntax_errors": syntax_errors,
        "evidence": sorted(evidence),
        "outcomes": [o.to_dict() for o in outcomes],
        "proofs": [p.to_dict() for p in proofs],
        "establishes": establishment_summary(outcomes),
        # What the run is ENTITLED TO, alongside how it was known. Six green
        # sign-off measurements and six green screens are the same number
        # under `establishes`, and they are not the same run.
        "authority": authority_summary(outcomes),
        "matrix_coverage": coverage.to_dict() if coverage else None,
        "findings": findings,
        "coverage": summarise(findings),
    }

    _diagnostics(report, outcomes, proofs, coverage, status)
    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


def _diagnostics(report, outcomes, proofs, coverage, status) -> None:
    diag("")
    diag(f"assert_matrix: {len(outcomes)} assertion(s)")
    marks = {PASS: "ok  ", FAIL: "RED ", NO_COVERAGE: "?   "}
    for outcome in outcomes:
        diag(f"  {marks[outcome.status]} [{outcome.assertion.level}] "
             f"{outcome.assertion.id:32s} {outcome.status}")
        if outcome.status != PASS:
            diag(f"       {outcome.detail}")
    established = report["establishes"]
    diag(f"  established: {established['measurement']} measured, "
         f"{established['record_only']} record-only, "
         f"{established['physical_measurement']} physical")
    granted = report["authority"]
    diag(f"  authority:   {granted['by_authority']['signoff']} sign-off, "
         f"{granted['by_authority']['screen_only']} screen-only, "
         f"{granted['by_coverage']['partial']} partial-coverage")
    for item in granted["residual_evidence"]:
        # Printed, not buried in the JSON: this is the work a green run still
        # leaves on the table, and its whole job is to be noticed.
        diag(f"  RESIDUAL {item}")
    for proof in proofs:
        if not proof.proven:
            diag(f"  VACUOUS {proof.assertion.id}: {proof.detail}")
    if coverage:
        diag(f"  matrix: {len(coverage.rows)} rule(s), "
             f"{len(coverage.unasserted)} unasserted, "
             f"{len(coverage.orphans)} orphan assertion(s)")
    diag(f"  status: {status}")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    """Drives the language into the corners where a permissive runner would
    quietly say yes."""
    from common.assertions import MISSING, load_assertion as _load, select

    base = {"id": "a1", "rule": "DRC 0 ihlal (repo rule set)", "level": "B",
            "loop": "L0", "on_fail": "duzelt", "source": "SKILL.md 2",
            "evidence": "drc", "selector": "summary.FAIL",
            "operator": "at_most", "expected": 0, "tolerance": 0,
            "falsifier": {"operation": "set", "path": "summary.FAIL",
                          "value": 3},
            "authority": "signoff",
            "coverage_waiver": "self-test document carries no scanned count"}

    def refuses(code, **overrides):
        entry = {k: v for k, v in {**base, **overrides}.items() if v is not None}
        try:
            _load(entry)
        except AssertionSyntaxError as exc:
            assert exc.code == code, f"expected {code}, got {exc.code}"
            return
        raise AssertionError(f"{code} was accepted")

    refuses("MISSING_TOLERANCE", tolerance=None)
    refuses("MISSING_FALSIFIER", falsifier=None)
    refuses("UNKNOWN_OPERATOR", operator="eval")
    refuses("UNITS_UNBOUND", units="mm")
    refuses("BAD_SELECTOR", selector="checks[*].status")
    refuses("UNBACKED_CLAIM", level="A", operator=None, expected=None,
            tolerance=None, falsifier=None)

    # The coverage/authority axes, driven into the same corners.
    #
    # An A/B rule may not stay silent about what a PASS is allowed to close:
    # a closed-form screen and a field solver both emit a number.
    refuses("BAD_AUTHORITY", authority=None)
    # ...nor may a file-measurable rule claim to accept hardware.
    refuses("BAD_AUTHORITY", authority="physical_acceptance")
    # "Partial" without naming the gap is humility that conveys nothing.
    refuses("RESIDUAL_UNDECLARED", coverage="partial")
    refuses("BAD_COVERAGE", residual_evidence=["something"])
    # Neither binding nor waiving the denominator is the EPAD vacuity.
    refuses("COVERAGE_UNBOUND", coverage_waiver=None)
    refuses("COVERAGE_UNBOUND", coverage_selector="coverage.scanned")
    refuses("COVERAGE_UNBOUND", coverage_waiver=None,
            coverage_selector="coverage.scanned", coverage_min=0)
    # The key that used to vanish without a word. Writing it was the only way
    # anyone had to say "measures, but must not sign off"; it was read by
    # nothing, so the set file lied and the run went on claiming a full
    # measurement.
    refuses("UNKNOWN_FIELD", establishes="record_only")

    good = _load(base)
    report = {"summary": {"FAIL": 0}}
    ev = {"drc": Evidence(name="drc", document=report)}
    assert evaluate(good, ev).status == PASS

    # A missing path is coverage loss, never a pass and never a failure.
    blind = evaluate(_load({**base, "selector": "summary.NOPE"}), ev)
    assert blind.status == NO_COVERAGE and blind.reason == "PATH_ABSENT", blind

    # Unbound evidence cannot produce GREEN.
    assert rollup([evaluate(good, ev)])[0] == SCREEN_ONLY

    # A check that examined nothing is not a check that found nothing. This is
    # the EPAD case: violations == 0 because the violation list is empty,
    # because no eligible pad was ever looked at.
    counted_base = {k: v for k, v in base.items() if k != "coverage_waiver"}
    guarded = _load({**counted_base, "coverage_selector": "coverage.scanned"})
    empty = {"drc": Evidence(name="drc",
                             document={"summary": {"FAIL": 0},
                                       "coverage": {"scanned": 0}})}
    swept = evaluate(guarded, empty)
    assert swept.status == NO_COVERAGE and swept.reason == "NOTHING_SCANNED", swept
    # ...and the same assertion passes once something was actually examined.
    seen = {"drc": Evidence(name="drc",
                            document={"summary": {"FAIL": 0},
                                      "coverage": {"scanned": 12}})}
    assert evaluate(guarded, seen).status == PASS

    # A screening tool's clean result is worth having and is not a sign-off.
    screen = _load({**base, "authority": "screen_only"})
    verdict, reasons = rollup([evaluate(screen, ev)])
    assert verdict == SCREEN_ONLY and "SCREEN_ONLY_AUTHORITY" in reasons, reasons

    # The falsifier must actually break it...
    assert prove(good, ev).proven
    # ...and one that cannot is reported as decoration, not as a pass.
    vacuous = _load({**base, "falsifier": {"operation": "set",
                                           "path": "summary.FAIL",
                                           "value": 0}})
    assert prove(vacuous, ev).reason == "VACUOUS_ASSERTION"
    # Deleting the evidence proves nothing: the check went blind.
    blinded = _load({**base, "falsifier": {"operation": "remove",
                                           "path": "summary.FAIL"}})
    assert prove(blinded, ev).reason == "FALSIFIER_NO_COVERAGE"

    # A level C pass is filing, and says so.
    filing = _load({"id": "c1", "rule": "MSL / paketleme", "level": "C",
                    "loop": "L1", "on_fail": "alternatif parca",
                    "source": "SKILL.md 2",
                    "record": {"decision": "MSL3", "source": "datasheet p4",
                               "date": "2026-08-07"}})
    out = evaluate(filing, ev)
    assert out.status == PASS and out.establishes == "record_only", out
    assert establishment_summary([out])["measurement"] == 0

    # A level D item with no measurement is not a pass and not a failure.
    physical = _load({"id": "d1", "rule": "Isil dogrulama", "level": "D",
                      "loop": "L3", "on_fail": "bakir revizyonu",
                      "source": "SKILL.md 2"})
    assert evaluate(physical, ev).reason == "MEASUREMENT_ABSENT"

    # The float boundary that bit this repo twice, in both directions.
    boundary = {"ring": (0.7 - 0.4) / 2, "units": "mm"}
    strict = _load({**base, "id": "b1", "selector": "ring",
                    "operator": "at_least", "expected": 0.15, "tolerance": 0,
                    "falsifier": {"operation": "set", "path": "ring",
                                  "value": 0.0}})
    tolerant = _load({**base, "id": "b2", "selector": "ring",
                      "operator": "at_least", "expected": 0.15,
                      "tolerance": 1e-9,
                      "falsifier": {"operation": "set", "path": "ring",
                                    "value": 0.0}})
    bev = {"drc": Evidence(name="drc", document=boundary)}
    assert evaluate(strict, bev).status == FAIL, "0.1499999... under 0.15"
    assert evaluate(tolerant, bev).status == PASS
    assert select(boundary, ("nope",)) is MISSING

    diag("SELF-TEST PASS: thirteen load-time refusals, missing path is "
         "NO_COVERAGE, unbound evidence cannot be GREEN, a check that scanned "
         "nothing is NOTHING_SCANNED rather than a clean pass, a screen_only "
         "PASS cannot reach GREEN, 'establishes' is refused instead of "
         "silently dropped, a vacuous falsifier is caught, level C passes as "
         "record_only, level D without a measurement is NO_COVERAGE, and the "
         "0.15 mm boundary fails strict / passes tolerant")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
