#!/usr/bin/env python3
"""Name every vendor model a simulation rests on, and gate its use.

WHY THIS EXISTS
---------------
A simulation result inherits the authority of its model. This bundle reads
Touchstone files, SPICE subcircuits and digitised datasheet curves, and
records nothing about where any of them came from. Two consequences, both
silent:

  * a result nobody can trace to a vendor document is not reproducible;
  * a model swapped under the same filename keeps the authority of the one it
    replaced, and the report still reads green.

``docs/PROVENANCE.json`` already applies this discipline to imported CODE.
This is the same thing for the DATA.

IT DOES NOT JUDGE ACCURACY
--------------------------
Whether a model is RIGHT is ``sim/correlate.py``'s question and it needs a
measurement to answer. This answers a different one: WHICH model, and may it
be used HERE - at this frequency, for this kind of claim.

FOUR GATES, IN THIS ORDER
-------------------------
  1. integrity   is the file on disk the one that was registered
  2. supersession has a newer revision replaced it
  3. envelope    is the query inside the range the model was characterised over
  4. authority   is a vendor-published model required for what is being asked

Out-of-envelope is NO_COVERAGE, never a clamped number. Every reader in this
bundle will interpolate off the end of its data without raising, so the only
place that can refuse is here.

Usage:
    model_registry.py --registry models.json
    model_registry.py --registry models.json --query queries.json
    model_registry.py --registry models.json --check-envelope --root .
    model_registry.py --self-test

Registry file (``vendor-model-registry/v1``):
    {"schema_version": "vendor-model-registry/v1",
     "models": [{"id": "...", "kind": "touchstone", "path": "...",
                 "authority": "vendor_published",
                 "provenance": {"vendor": "...", "document": "...",
                                "revision": "...", "accessed": "..."},
                 "valid_range": [{"quantity": "frequency", "unit": "GHz",
                                  "min": 0.01, "max": 20.0}]}]}

Exit codes: 0 = GREEN, 1 = RED / NO_COVERAGE / SCREEN_ONLY, 2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (FAIL, Finding, NO_COVERAGE, PASS,  # noqa: E402
                             result, summarise)
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)
from common.vendormodel import (GRANT_SCREEN_ONLY, GRANT_SIGNOFF,  # noqa: E402
                                GREEN, RED, SCREEN_ONLY, ModelSyntaxError,
                                answer, check_envelope, check_integrity,
                                grant_summary, load_query, load_registry,
                                rollup)

SCHEMA_VERSION = "vendor-model-report/v1"
TOOL_NAME = "model_registry"
REGISTRY_SCHEMA_VERSION = "vendor-model-registry/v1"
QUERY_SCHEMA_VERSION = "vendor-model-query/v1"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def read_registry(path: Path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: registry must be a JSON object")
    declared = doc.get("schema_version")
    if declared != REGISTRY_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be "
                         f"{REGISTRY_SCHEMA_VERSION!r}, got {declared!r}")
    entries = doc.get("models")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'models' must be a non-empty array")
    return load_registry(entries)


def read_queries(path: Path) -> Tuple[List[Any], List[dict]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: query file must be a JSON object")
    declared = doc.get("schema_version")
    if declared != QUERY_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be "
                         f"{QUERY_SCHEMA_VERSION!r}, got {declared!r}")
    entries = doc.get("queries")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'queries' must be a non-empty array")
    queries, errors = [], []
    for entry in entries:
        try:
            queries.append(load_query(entry))
        except ModelSyntaxError as exc:
            errors.append(exc.to_dict())
    return queries, errors


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def loadability_finding(total: int, errors: List[dict]) -> Finding:
    return result("models_loadable", total, errors,
                  f"{total} registry entr(ies); every refusal is at LOAD time, "
                  "so an unaccountable model cannot lie dormant until the day "
                  "somebody queries it")


def integrity_finding(states) -> Finding:
    if not states:
        return Finding("models_intact", NO_COVERAGE, 0, [],
                       "no model was checked against disk")
    violations = [s.to_dict() for s in states if s.status == FAIL]
    unverified = [s for s in states if s.status == NO_COVERAGE]
    if violations:
        return result("models_intact", len(states), violations,
                      f"{len(violations)} model(s) are missing or no longer "
                      "byte-identical to what was registered")
    if unverified:
        return Finding("models_intact", NO_COVERAGE, len(states) - len(unverified),
                       [],
                       f"{len(unverified)} model(s) carry no registered digest; "
                       "a file that cannot be tied to the vetted one is "
                       "unverified, not verified")
    return result("models_intact", len(states), [],
                  f"all {len(states)} model file(s) match their registered digest")


def envelope_finding(checks) -> Finding:
    """An overclaimed envelope is a FAIL; an unverifiable one is NO_COVERAGE.

    The two must not merge. An overclaim is a defect somebody can fix by
    correcting the entry; an unchecked declaration is a limit of this tool,
    and reporting it as PASS is how a registry stops being evidence.
    """
    if not checks:
        return Finding("envelope_supported", NO_COVERAGE, 0, [],
                       "no envelope was cross-checked")
    violations = [c.to_dict() for c in checks if c.status == FAIL]
    checked = [c for c in checks if c.status != NO_COVERAGE]
    if violations:
        return result("envelope_supported", len(checked) or len(checks),
                      violations,
                      f"{len(violations)} declared range(s) exceed what the "
                      "file itself contains")
    if not checked:
        return Finding("envelope_supported", NO_COVERAGE, 0, [],
                       f"none of the {len(checks)} declaration(s) could be "
                       "checked against a file; all stand unverified")
    return result("envelope_supported", len(checked), [],
                  f"{len(checked)} declared range(s) lie inside the data the "
                  "file actually holds")


def usage_finding(answers) -> Finding:
    if not answers:
        return Finding("model_usage_permitted", NO_COVERAGE, 0, [],
                       "no query was asked")
    refused = [a.to_dict() for a in answers if a.status == FAIL]
    uncovered = [a for a in answers if a.status == NO_COVERAGE]
    cleared = [a for a in answers if a.status != NO_COVERAGE]
    if refused:
        return result("model_usage_permitted", len(cleared) or len(answers),
                      refused, f"{len(refused)} quer(ies) name a model that may "
                               "not be used the way they ask")
    if not cleared:
        return Finding("model_usage_permitted", NO_COVERAGE, 0, [],
                       f"all {len(uncovered)} quer(ies) fell outside a declared "
                       "envelope; nothing was established")
    if uncovered:
        return Finding("model_usage_permitted", NO_COVERAGE, len(cleared), [],
                       f"{len(cleared)} quer(ies) cleared, {len(uncovered)} fell "
                       "outside an envelope - a partial clearance is not a "
                       "clearance")
    return result("model_usage_permitted", len(cleared), [],
                  f"all {len(cleared)} quer(ies) are inside a declared envelope "
                  "with sufficient authority")


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

def overall(findings: List[Finding], answers) -> Tuple[str, List[str]]:
    counts = summarise(findings)
    if counts[FAIL]:
        return RED, sorted({f["check"].upper() for f in findings
                            if f["status"] == FAIL})
    status, reasons = rollup(answers)
    if counts[NO_COVERAGE] and status in (GREEN, SCREEN_ONLY):
        return NO_COVERAGE, sorted({f["check"].upper() for f in findings
                                    if f["status"] == NO_COVERAGE})
    return status, reasons


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register vendor models and gate their use.")
    parser.add_argument("--registry", type=Path,
                        help=f"model registry ({REGISTRY_SCHEMA_VERSION})")
    parser.add_argument("--query", type=Path,
                        help=f"queries to settle ({QUERY_SCHEMA_VERSION})")
    parser.add_argument("--root", type=Path, default=Path("."),
                        help="base for relative model paths")
    parser.add_argument("--check-envelope", action="store_true",
                        help="compare each declared range against the file")
    parser.add_argument("--skip-integrity", action="store_true",
                        help="do not hash the model files (paths only)")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    if not args.registry:
        raise ValueError("--registry is required (or use --self-test)")

    registry = read_registry(args.registry)
    queries, query_errors = ([], [])
    if args.query:
        queries, query_errors = read_queries(args.query)

    integrity = {}
    if not args.skip_integrity:
        integrity = {model.id: check_integrity(model, args.root)
                     for model in registry.models.values()}

    envelope_checks = []
    if args.check_envelope:
        for model in registry.models.values():
            envelope_checks.extend(check_envelope(model, args.root))

    answers = [answer(q, registry, integrity) for q in queries]

    findings = [loadability_finding(len(registry.models) + len(registry.errors),
                                    registry.errors + query_errors)]
    if integrity:
        findings.append(integrity_finding(list(integrity.values())))
    if args.check_envelope:
        findings.append(envelope_finding(envelope_checks))
    findings.append(usage_finding(answers))

    status, reason_codes = overall(findings, answers)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "verification",
        "judges_accuracy": False,
        "registry": str(args.registry),
        "models": [m.to_dict() for m in registry.models.values()],
        "load_errors": registry.errors + query_errors,
        "integrity": [s.to_dict() for s in integrity.values()],
        "envelope_checks": [c.to_dict() for c in envelope_checks],
        "answers": [a.to_dict() for a in answers],
        "grants": grant_summary(answers),
        "findings": findings,
        "coverage": summarise(findings),
    }

    _diagnostics(report, registry, integrity, answers, status)
    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


def _diagnostics(report, registry, integrity, answers, status) -> None:
    diag("")
    diag(f"model_registry: {len(registry.models)} model(s)")
    marks = {PASS: "ok  ", FAIL: "RED ", NO_COVERAGE: "?   "}
    for state in integrity.values():
        diag(f"  {marks[state.status]} {state.model.id:28s} "
             f"[{state.model.authority}]")
        if state.status != PASS:
            diag(f"       {state.detail}")
    for item in answers:
        diag(f"  {marks[item.status]} query {item.query.model_id:22s} "
             f"{item.query.use:9s} grant={item.grant}")
        if item.status != PASS:
            diag(f"       {item.detail}")
    grants = report["grants"]
    diag(f"  granted: {grants[GRANT_SIGNOFF]} sign-off, "
         f"{grants[GRANT_SCREEN_ONLY]} screening-only")
    diag(f"  status: {status}")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    """Drives the registry into the corners where a permissive one says yes."""
    import tempfile
    from common.vendormodel import (AUTHORITY_INSUFFICIENT, OUT_OF_ENVELOPE,
                                    Query, STALE_MODEL, SUPERSEDED,
                                    load_model)

    base = {"id": "ds90ub953-s4p", "kind": "touchstone",
            "path": "models/ub953.s4p", "authority": "vendor_published",
            "provenance": {"vendor": "TI", "document": "SNLS551",
                           "revision": "C", "accessed": "2026-08-08"},
            "valid_range": [{"quantity": "frequency", "unit": "GHz",
                             "min": 0.01, "max": 10.0}]}

    def refuses(code, **overrides):
        entry = {k: v for k, v in {**base, **overrides}.items() if v is not None}
        try:
            load_model(entry)
        except ModelSyntaxError as exc:
            assert exc.code == code, f"expected {code}, got {exc.code}"
            return
        raise AssertionError(f"{code} was accepted")

    refuses("AUTHORITY_UNDECLARED", authority=None)
    refuses("ENVELOPE_UNDECLARED", valid_range=None)
    refuses("PROVENANCE_INCOMPLETE",
            provenance={"vendor": "TI", "document": "SNLS551"})
    refuses("UNIT_UNDECLARED",
            valid_range=[{"quantity": "frequency", "min": 0, "max": 10}])
    refuses("BAD_ENVELOPE",
            valid_range=[{"quantity": "frequency", "unit": "GHz",
                          "min": 10.0, "max": 0.01}])
    refuses("BAD_KIND", kind="spreadsheet")

    registry = load_registry([base])
    assert not registry.errors

    # Inside the envelope, published authority: sign-off is granted.
    good = answer(Query("ds90ub953-s4p", "signoff", {"frequency": 5.0}), registry)
    assert good.status == PASS and good.grant == GRANT_SIGNOFF, good

    # Outside it: no number, and specifically NOT a failure of the design.
    outside = answer(Query("ds90ub953-s4p", "signoff", {"frequency": 12.0}),
                     registry)
    assert outside.status == NO_COVERAGE, outside
    assert outside.reason == OUT_OF_ENVELOPE, outside

    # An axis nobody declared is also NO_COVERAGE, not an implicit pass.
    silent = answer(Query("ds90ub953-s4p", "screening", {"temperature": 85.0}),
                    registry)
    assert silent.reason == "AXIS_UNDECLARED", silent

    # A derived model produces results and cannot close a gate.
    derived = dict(base, id="ub953-fit", authority="vendor_derived")
    reg2 = load_registry([base, derived])
    screen = answer(Query("ub953-fit", "screening", {"frequency": 5.0}), reg2)
    assert screen.status == PASS and screen.grant == GRANT_SCREEN_ONLY
    blocked = answer(Query("ub953-fit", "signoff", {"frequency": 5.0}), reg2)
    assert blocked.status == FAIL and blocked.reason == AUTHORITY_INSUFFICIENT

    # A superseded model is refused rather than annotated.
    newer = dict(base, id="ds90ub953-s4p-revD", supersedes=["ds90ub953-s4p"])
    reg3 = load_registry([base, newer])
    stale = answer(Query("ds90ub953-s4p", "screening", {"frequency": 5.0}), reg3)
    assert stale.status == FAIL and stale.reason == SUPERSEDED, stale

    # A file whose bytes moved keeps its id and loses its authority.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "models" / "ub953.s4p"
        target.parent.mkdir(parents=True)
        target.write_text("! placeholder\n", encoding="utf-8")
        from common.hashing import sha256_file
        pinned = dict(base, sha256=sha256_file(target))
        reg4 = load_registry([pinned])
        model = reg4.get("ds90ub953-s4p")
        assert check_integrity(model, root).status == PASS
        target.write_text("! edited\n", encoding="utf-8")
        moved = check_integrity(model, root)
        assert moved.status == FAIL and moved.reason == STALE_MODEL, moved
        integrity = {model.id: moved}
        assert answer(Query("ds90ub953-s4p", "screening", {"frequency": 5.0}),
                      reg4, integrity).status == FAIL

    # A clean screening-only run is SCREEN_ONLY, never GREEN.
    only_derived = load_registry([dict(base, authority="vendor_derived")])
    verdict, reasons = rollup([answer(Query("ds90ub953-s4p", "screening",
                                            {"frequency": 5.0}), only_derived)])
    assert verdict == SCREEN_ONLY and reasons == ["NO_SIGNOFF_GRADE_MODEL"]

    diag("SELF-TEST PASS: six load-time refusals, out-of-envelope is "
         "NO_COVERAGE rather than an extrapolated number, an undeclared axis "
         "is not an implicit pass, a derived model screens but cannot sign "
         "off, a superseded model is refused, a moved digest revokes the "
         "entry, and a screening-only run cannot be GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
