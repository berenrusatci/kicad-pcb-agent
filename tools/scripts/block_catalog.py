#!/usr/bin/env python3
"""Instantiate parametric circuit blocks from an architecture decision, and
check the design against them.

WHY THIS EXISTS
--------------------------------------------------------------------------
Architecture decisions were already being produced and nothing read them.
An unread decision drifts away from the design silently, because no check
was ever pointed at it: the record says "no serialiser, frame sync on a
connector contact", the board grows a serialiser, and every gate stays
green because none of them had been told what to expect.

This tool is the reader. It takes a decision document, instantiates the
blocks that decision implies, and - given a netlist - reports whether the
design actually contains them. Emission and verification come from the same
record, which is the whole point; two documents that must be kept in sync
by hand are two documents that eventually are not.

WHAT IT DOES NOT DO
--------------------------------------------------------------------------
It emits no schematic and no part numbers. A block declares component
ROLES with constraints; choosing an MPN needs stock, lifecycle and package
inputs that are not visible here. Schematic emission belongs to the board's
own generator - schematics in this repo are generated from pin tables, and
an edit made behind the generator is erased by the next build.

VERIFICATION IS THREE-VALUED ON PURPOSE
--------------------------------------------------------------------------
present / absent / unverifiable. The third is not a rounding of the other
two: a template with no verification method, or a run with no netlist, has
established nothing, and folding that into "present" would turn the absence
of a check into evidence of correctness. Unverifiable blocks make the whole
run NO_COVERAGE rather than GREEN.

Usage:
    block_catalog.py --list
    block_catalog.py --decision architecture.json [--netlist build/netlist.net]
                     [--json block_plan.json]
    block_catalog.py --block decoupling_shell --param rail=VDD_1V2D
                     --param decades=4 --param bulk_farads=1e-5
    block_catalog.py --self-test

Decision document (``pcb-block-plan-input/v1``):
    {"blocks": [{"block": "link_architecture",
                 "params": {"serdes_required": false,
                            "fsync_route": "connector_pin",
                            "fsync_net": "FSYNC"}}]}

Exit codes: 0 = GREEN (every block verified present), 1 = RED or
NO_COVERAGE, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.blocks import (ABSENT, CATALOG, PRESENT, UNVERIFIABLE,  # noqa: E402
                           BlockRefusal, instantiate)
from common.findings import Finding, NO_COVERAGE, result, summarise  # noqa: E402
from common.netlist import read_nets  # noqa: E402
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "block-plan/v1"
TOOL_NAME = "block_catalog"
INPUT_SCHEMA_VERSION = "pcb-block-plan-input/v1"


def _coerce(value: str) -> Any:
    """``--param k=v`` values arrive as text. JSON first so that ``false``
    and ``4`` keep their types; a bare word stays a string."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def parse_params(items: List[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param must be NAME=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        if not key.strip():
            raise ValueError(f"--param has an empty name: {item!r}")
        params[key.strip()] = _coerce(value.strip())
    return params


def load_decision(path: Path) -> List[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: decision document must be an object")
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError(f"{path}: 'blocks' must be a non-empty array")
    for entry in blocks:
        if not isinstance(entry, dict) or "block" not in entry:
            raise ValueError(f"{path}: every entry needs a 'block' key")
        if not isinstance(entry.get("params", {}), dict):
            raise ValueError(f"{path}: 'params' must be an object")
    return blocks


def build_instances(entries: List[dict], nets: Optional[dict]):
    """Instantiate each entry. A refusal is recorded, never raised past
    here - one unbuildable block must not hide the state of the others."""
    instances = []
    refusals = []
    for entry in entries:
        key = entry["block"]
        try:
            instances.append(instantiate(key, dict(entry.get("params", {})), nets))
        except BlockRefusal as refusal:
            refusals.append(refusal.to_dict())
    return instances, refusals


def instantiation_finding(entries: List[dict], refusals: List[dict]) -> Finding:
    return result("blocks_instantiable", len(entries), refusals,
                  f"{len(entries)} block(s) from the decision document")


def verification_finding(instances, nets: Optional[dict]) -> Finding:
    check = "design_matches_decision"
    if nets is None:
        return Finding(check, NO_COVERAGE, 0, [],
                       "no netlist supplied; blocks were emitted but the "
                       "design was not checked against them")
    absent = [{"block": i.key, "issue": "BLOCK_ABSENT",
               "params": i.params, "detail": i.verification.get("detail", "")}
              for i in instances if i.verification["outcome"] == ABSENT]
    unverifiable = [i for i in instances
                    if i.verification["outcome"] == UNVERIFIABLE]
    checked = [i for i in instances if i.verification["outcome"] != UNVERIFIABLE]
    if absent:
        return result(check, len(checked) or len(instances), absent,
                      f"{len(absent)} block(s) the decision declares are not "
                      "in the design")
    if not checked:
        return Finding(check, NO_COVERAGE, 0, [],
                       f"{len(unverifiable)} block(s) had no applicable "
                       "verification; nothing was established")
    if unverifiable:
        return Finding(check, NO_COVERAGE, len(checked), [],
                       f"{len(checked)} block(s) confirmed present, "
                       f"{len(unverifiable)} unverifiable - a partial check is "
                       "not a passing one")
    return result(check, len(checked), [],
                  f"all {len(checked)} block(s) confirmed present in the netlist")


def rollup(findings) -> tuple[str, List[str]]:
    counts = summarise(findings)
    if counts.get("FAIL"):
        return "RED", [f["check"].upper() for f in findings
                       if f["status"] == "FAIL"]
    if counts.get(NO_COVERAGE):
        return "NO_COVERAGE", [f["check"].upper() for f in findings
                               if f["status"] == NO_COVERAGE]
    return "GREEN", []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Instantiate circuit blocks from an architecture decision "
                    "and check the design against them.")
    parser.add_argument("--list", action="store_true",
                        help="print the catalogue and exit")
    parser.add_argument("--decision", type=Path,
                        help=f"decision document ({INPUT_SCHEMA_VERSION})")
    parser.add_argument("--block", help="instantiate a single block by key")
    parser.add_argument("--param", action="append", default=[],
                        metavar="NAME=VALUE", help="parameter for --block")
    parser.add_argument("--netlist", type=Path,
                        help="without it, verification is NO_COVERAGE")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    if args.list:
        catalogue = {"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
                     "catalog": [t.to_dict() for t in
                                 sorted(CATALOG.values(), key=lambda t: t.key)]}
        emit(catalogue)
        return 0

    if bool(args.decision) == bool(args.block):
        raise ValueError("give exactly one of --decision or --block "
                         "(or --list)")

    if args.decision:
        entries = load_decision(args.decision)
        source = str(args.decision)
    else:
        entries = [{"block": args.block, "params": parse_params(args.param)}]
        source = "command line"

    nets = read_nets(args.netlist) if args.netlist else None
    instances, refusals = build_instances(entries, nets)

    findings = [instantiation_finding(entries, refusals),
                verification_finding(instances, nets)]
    status, reason_codes = rollup(findings)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "early_schematic",
        "emits_schematic": False,
        "decision_source": source,
        "netlist": str(args.netlist) if args.netlist else None,
        "instances": [i.to_dict() for i in instances],
        "refusals": refusals,
        "findings": findings,
        "coverage": summarise(findings),
    }

    diag("")
    diag(f"block catalog: {len(entries)} declared, {len(instances)} instantiated")
    for instance in instances:
        outcome = instance.verification["outcome"]
        mark = {PRESENT: "ok  ", ABSENT: "RED ", UNVERIFIABLE: "?   "}[outcome]
        diag(f"  {mark} {instance.key:24s} {outcome}")
        if outcome != PRESENT:
            diag(f"       {instance.verification.get('detail', '')}")
    for refusal in refusals:
        diag(f"  RED  {refusal['block']:24s} missing {refusal['missing']}")
    diag(f"  status: {status}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


def _self_test() -> int:
    """Drives the catalogue into the corners where a permissive tool would
    quietly say yes."""
    # A missing required parameter is refused, and names itself.
    try:
        instantiate("decoupling_shell", {"rail": "VDD"})
    except BlockRefusal as refusal:
        assert "decades" in refusal.missing and "bulk_farads" in refusal.missing
    else:  # pragma: no cover - the assert above is the test
        raise AssertionError("a missing required parameter was defaulted")

    # An unknown block is refused rather than skipped.
    try:
        instantiate("no_such_block", {})
    except BlockRefusal:
        pass
    else:  # pragma: no cover
        raise AssertionError("an unknown block was accepted")

    nets = {"VDD": [("U1", "5"), ("C1", "1"), ("C2", "1"), ("C3", "1")],
            "P": [("U1", "1")], "N": [("U1", "2")]}

    # Present when the count is met, absent when it is not.
    ok = instantiate("decoupling_shell",
                     {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5}, nets)
    assert ok.verification["outcome"] == PRESENT, ok.verification
    short = instantiate("decoupling_shell",
                        {"rail": "VDD", "decades": 5, "bulk_farads": 1e-5}, nets)
    assert short.verification["outcome"] == ABSENT, short.verification

    # The bank really is decade-spaced, and shrinking.
    values = [c["nominal_farads"] for c in ok.emitted["components"]]
    assert len(values) == 3
    for lower, higher in zip(values[1:], values):
        assert abs(higher / lower - 10.0) < 1e-9, values

    # back_channel: the ABSENCE of a frame-sync net is the correct design.
    back = instantiate("link_architecture",
                       {"serdes_required": True, "fsync_route": "back_channel",
                        "fsync_net": "FSYNC"}, nets)
    assert back.verification["outcome"] == PRESENT, back.verification
    # ...and its PRESENCE is the defect.
    contradicted = instantiate(
        "link_architecture",
        {"serdes_required": True, "fsync_route": "back_channel",
         "fsync_net": "P"}, nets)
    assert contradicted.verification["outcome"] == ABSENT, contradicted.verification

    # connector_pin with no net to look for is unverifiable, not present.
    vague = instantiate("link_architecture",
                        {"serdes_required": False,
                         "fsync_route": "connector_pin"}, nets)
    assert vague.verification["outcome"] == UNVERIFIABLE, vague.verification

    # No netlist establishes nothing.
    blind = instantiate("decoupling_shell",
                        {"rail": "VDD", "decades": 3, "bulk_farads": 1e-5})
    assert blind.verification["outcome"] == UNVERIFIABLE, blind.verification

    diag("SELF-TEST PASS: missing parameter refused, unknown block refused, "
         "decade spacing verified, back-channel frame sync correct in both "
         "directions, unverifiable never folded into present")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
