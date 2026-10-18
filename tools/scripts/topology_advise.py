#!/usr/bin/env python3
"""Propose topology changes for shortfalls another tool already measured.

THE GAP
-------
Every solver here optimises inside a topology somebody else chose: sweep W
and S, allocate rails that exist, route around obstacles that exist. None of
them can say "add a part". A whole class of fixes lives outside the parameter
space they search.

WHAT COMES OUT
--------------
Hypotheses, not fixes. Each proposal carries:

    what it ADDS
    which metric it moves and in WHICH DIRECTION - never how far
    the MECHANISM, argued from the topology rather than measured
    the TOOL that would verify it
    the OBSERVATION that would refute it
    its COST

No component value is chosen here and no simulation is run. A magnitude in
this report would be read as a result, and this tool has no solver behind it.
That is not a limitation to work around - it is the boundary that keeps the
proposals reviewable.

THE EXCLUSIONS ARE THE USEFUL PART
----------------------------------
A transformation that fits every shortfall fits none, so every catalogue
entry carries conditions that rule it out, supplied by the caller through
``--context``. The most informative line this tool prints is usually a
REFUSED proposal: "a capacitor that exists and sits too far away is a
placement problem; a second part does not shorten the first one's loop".

Facts are supplied, never inferred. Whether the reference plane is split
under a via row is something ``reference_plane.py`` measures, and guessing it
here would be a second measurement of the same thing.

Usage:
    topology_advise.py --report drc=build/decoupling.json
    topology_advise.py --report r=build/rail.json --context dominant_noise_term=random
    topology_advise.py --list-catalogue
    topology_advise.py --self-test

Exit codes: 0 = a checkable proposal was offered, 1 = nothing to offer or a
shortfall the catalogue does not know, 2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (FAIL, Finding, NO_COVERAGE, PASS,  # noqa: E402
                             result, summarise)
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)
from common.topology import (ALREADY_OWNED, EXCLUDED, GREEN,  # noqa: E402
                             NO_TRANSFORM_KNOWN, TransformSyntaxError,
                             default_catalogue, load_transform,
                             observations_from_report, propose, rollup,
                             uncovered)

SCHEMA_VERSION = "topology-proposal-report/v1"
TOOL_NAME = "topology_advise"
CATALOGUE_SCHEMA_VERSION = "topology-catalogue/v1"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def read_catalogue(path: Path | None):
    """The built-in catalogue unless a file replaces it.

    A user catalogue goes through the same load-time refusals as the shipped
    one; a proposal nobody can check is not more acceptable for having been
    written locally.
    """
    if path is None:
        return default_catalogue(), []
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: catalogue must be a JSON object")
    declared = doc.get("schema_version")
    if declared != CATALOGUE_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be "
                         f"{CATALOGUE_SCHEMA_VERSION!r}, got {declared!r}")
    entries = doc.get("transforms")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'transforms' must be a non-empty array")
    catalogue, errors = [], []
    for entry in entries:
        try:
            catalogue.append(load_transform(entry))
        except TransformSyntaxError as exc:
            errors.append(exc.to_dict())
    return catalogue, errors


def read_reports(pairs: List[str]) -> Tuple[List[Any], List[dict]]:
    """Each ``NAME=path.json`` is a report some tool already produced."""
    observations, problems = [], []
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--report must be NAME=PATH, got {pair!r}")
        name, _, raw_path = pair.partition("=")
        document = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{raw_path}: a report must be a JSON object")
        if not str(document.get("tool", "")).strip():
            problems.append({
                "report": name.strip(), "issue": "TOOL_UNNAMED",
                "detail": "the report declares no 'tool', so no trigger can "
                          "match it. Triggers key on the producing tool "
                          "because the same check name in two tools is not "
                          "the same shortfall"})
            continue
        observations.extend(observations_from_report(document))
    return observations, problems


def parse_context(pairs: List[str]) -> Dict[str, Any]:
    """``key=value``, with JSON literals so true/false/numbers stay typed.

    An exclusion compares for equality, so ``guard_can_be_stitched=false``
    arriving as the STRING "false" would silently fail to exclude - and a
    guard that cannot be stitched is the case that matters most.
    """
    context: Dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--context must be KEY=VALUE, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--context has an empty key: {pair!r}")
        try:
            context[key] = json.loads(raw)
        except json.JSONDecodeError:
            context[key] = raw
    return context


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def catalogue_finding(total: int, errors: List[dict]) -> Finding:
    return result("catalogue_loadable", total, errors,
                  f"{total} transformation(s); each has to name a verifier, a "
                  "refuting observation, a cost and at least one condition "
                  "that rules it out")


def input_finding(observations, problems: List[dict]) -> Finding:
    if problems:
        return result("reports_readable", len(problems), problems,
                      "a report that does not name its tool cannot trigger "
                      "anything")
    if not observations:
        return Finding("reports_readable", NO_COVERAGE, 0, [],
                       "no FAIL or NO_COVERAGE finding was supplied. A PASS "
                       "is not a shortfall, and proposing against one is how "
                       "a tool starts recommending work for its own sake")
    return result("reports_readable", len(observations), [],
                  f"{len(observations)} shortfall(s) read from the supplied "
                  "reports")


def coverage_finding(observations, gaps) -> Finding:
    """A shortfall the catalogue cannot address is NO_COVERAGE, never a pass.

    Silently omitting it would make the proposal list look like a complete
    answer to the report it read.
    """
    if not observations:
        return Finding("shortfalls_addressed", NO_COVERAGE, 0, [],
                       "nothing to address")
    if gaps:
        finding = Finding("shortfalls_addressed", NO_COVERAGE,
                          len(observations) - len(gaps), [],
                          f"{len(observations) - len(gaps)}/"
                          f"{len(observations)} shortfall(s) have a "
                          f"transformation; {len(gaps)} do not")
        finding["uncovered"] = [{"tool": o.tool, "check": o.check,
                                 "issue": NO_TRANSFORM_KNOWN}
                                for o in gaps]
        return finding
    return result("shortfalls_addressed", len(observations), [],
                  "every shortfall read has at least one catalogue entry")


def proposal_finding(proposals) -> Finding:
    """Never FAIL. This module measures nothing, so it cannot find a defect.

    An excluded proposal is a correct answer, not a violation - which is why
    the count of offers, not the count of exclusions, decides the status.
    """
    if not proposals:
        return Finding("proposals_offered", NO_COVERAGE, 0, [],
                       "no transformation matched")
    offered = [p for p in proposals if p.offered]
    if not offered:
        return Finding("proposals_offered", NO_COVERAGE, len(proposals), [],
                       f"all {len(proposals)} candidate(s) were ruled out by "
                       "the supplied context; the exclusions say why, and "
                       "that is an answer rather than a gap in the catalogue")
    return result("proposals_offered", len(proposals), [],
                  f"{len(offered)}/{len(proposals)} candidate(s) survive the "
                  "context and name a tool that would verify them")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Propose topology changes for measured shortfalls.")
    parser.add_argument("--report", action="append", default=[],
                        metavar="NAME=PATH",
                        help="a report some tool already produced")
    parser.add_argument("--context", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="facts that rule transformations out; JSON "
                             "literals keep true/false typed")
    parser.add_argument("--catalogue", type=Path,
                        help=f"replace the built-in catalogue "
                             f"({CATALOGUE_SCHEMA_VERSION})")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--list-catalogue", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    catalogue, catalogue_errors = read_catalogue(args.catalogue)

    if args.list_catalogue:
        emit({"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
              "catalogue": [t.to_dict() for t in catalogue],
              "load_errors": catalogue_errors})
        return 0 if not catalogue_errors else 1

    if not args.report:
        raise ValueError("--report is required (or use --list-catalogue)")

    context = parse_context(args.context)
    observations, input_problems = read_reports(args.report)
    proposals = propose(observations, catalogue, context)
    gaps = uncovered(observations, proposals)

    findings = [catalogue_finding(len(catalogue) + len(catalogue_errors),
                                  catalogue_errors),
                input_finding(observations, input_problems),
                coverage_finding(observations, gaps),
                proposal_finding(proposals)]

    counts = summarise(findings)
    if counts[FAIL]:
        status, reason_codes = "RED", sorted({f["check"].upper()
                                              for f in findings
                                              if f["status"] == FAIL})
    else:
        status, reason_codes = rollup(proposals, gaps)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "improvement",
        "signoff": False,
        "measures_nothing": True,
        "runs_no_solver": True,
        "context": context,
        "observations": [o.to_dict() for o in observations],
        "proposals": [p.to_dict() for p in proposals],
        "uncovered": [{"tool": o.tool, "check": o.check} for o in gaps],
        "load_errors": catalogue_errors + input_problems,
        "findings": findings,
        "coverage": summarise(findings),
    }

    _diagnostics(report, proposals, gaps, status)
    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


def _diagnostics(report, proposals, gaps, status) -> None:
    diag("")
    diag(f"topology_advise: {len(proposals)} candidate(s)")
    for proposal in proposals:
        mark = "ok  " if proposal.offered else "--  "
        diag(f"  {mark} {proposal.transform.id:32s} "
             f"{'OFFERED' if proposal.offered else proposal.reason}")
        if proposal.offered:
            diag(f"       adds: {proposal.transform.adds}")
            diag(f"       {proposal.transform.direction} "
                 f"{proposal.transform.metric} "
                 f"(magnitude unknown - run {proposal.transform.verified_by})")
        else:
            diag(f"       {proposal.detail}")
    for gap in gaps:
        diag(f"  ?    {gap.tool}.{gap.check}: no transformation known")
    diag(f"  status: {status}")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    from common.topology import Observation

    catalogue = default_catalogue()

    def refuses(code, **overrides):
        entry = {"id": "t", "title": "t", "adds": "a part",
                 "metric": "some quantity", "direction": "decrease",
                 "mechanism": "because of the topology",
                 "triggers": [{"tool": "x", "check": "c"}],
                 "verified_by": "x", "refuted_by": "y", "cost": ["a part"],
                 "excluded_when": [{"fact": "f", "equals": True,
                                    "because": "reason"}]}
        entry = {k: v for k, v in {**entry, **overrides}.items() if v is not None}
        try:
            load_transform(entry)
        except TransformSyntaxError as exc:
            assert exc.code == code, f"expected {code}, got {exc.code}"
            return
        raise AssertionError(f"{code} was accepted")

    refuses("UNVERIFIABLE_PROPOSAL", verified_by=None)
    refuses("UNFALSIFIABLE_PROPOSAL", refuted_by=None)
    refuses("COST_UNDECLARED", cost=None)
    refuses("UNCONDITIONAL_TRANSFORM", excluded_when=None)
    refuses("MAGNITUDE_CLAIMED", metric="return loss below -16 dB")
    refuses("MISSING_TRIGGER", triggers=None)
    refuses("BAD_TRIGGER", triggers=[{"tool": "x"}])

    # A shortfall with a matching transformation gets a proposal.
    shortfall = Observation(tool="decoupling_coverage",
                            check="decoupling_coverage", status=FAIL,
                            problems=("no_capacitor",))
    offered = [p for p in propose([shortfall], catalogue) if p.offered]
    assert len(offered) == 1 and offered[0].transform.id == "add-decoupling-stage"

    # No proposal carries a magnitude, ever.
    for proposal in offered:
        assert proposal.to_dict()["predicted"]["magnitude"] is None

    # The exclusion that matters: a capacitor that exists but sits too far
    # away is a PLACEMENT problem, and another part does not fix it.
    ruled_out = propose([shortfall], catalogue,
                        {"shortfall": "capacitor_too_far"})
    assert ruled_out and not any(p.offered for p in ruled_out)
    assert ruled_out[0].reason == EXCLUDED

    # Two entries trigger on the same rail shortfall and the context picks.
    rail = Observation(tool="rail_budget", check="noise_within_budget",
                       status=FAIL,
                       reason_codes=("NOISE_BUDGET_EXCEEDED",))
    tonal = {p.transform.id for p in propose([rail], catalogue,
                                             {"dominant_noise_term":
                                              "deterministic"}) if p.offered}
    assert tonal == {"add-lc-post-filter"}, tonal
    broadband = {p.transform.id for p in propose([rail], catalogue,
                                                 {"dominant_noise_term":
                                                  "random"}) if p.offered}
    assert broadband == {"add-bulk-stage"}, broadband

    # A parameter change an existing tool owns is reported, not proposed.
    spacing = Observation(tool="emc_rules", check="emc_3w_crosstalk",
                          status=FAIL)
    owned = propose([spacing], catalogue)
    assert owned and not owned[0].offered and owned[0].reason == ALREADY_OWNED

    # A shortfall nothing addresses is reported rather than omitted.
    unknown = Observation(tool="nobody", check="whatever", status=FAIL)
    assert uncovered([unknown], propose([unknown], catalogue)) == [unknown]

    # A passing finding is not a shortfall.
    report = {"tool": "decoupling_coverage",
              "findings": [{"check": "decoupling_coverage", "status": PASS,
                            "violations": [], "detail": ""}]}
    assert observations_from_report(report) == []

    # This module never returns RED: it measures nothing.
    assert rollup(propose([shortfall], catalogue), [])[0] == GREEN
    assert rollup([], [unknown])[0] == NO_COVERAGE

    diag("SELF-TEST PASS: seven load-time refusals including a claimed "
         "magnitude and a transformation nothing can rule out, a placement "
         "shortfall is excluded rather than answered with another part, the "
         "noise term picks between two rail entries, a parameter change is "
         "reported as already owned, an unaddressable shortfall is not "
         "omitted, and no proposal carries a magnitude")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
