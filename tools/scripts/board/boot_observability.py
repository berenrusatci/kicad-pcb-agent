#!/usr/bin/env python3
"""If the board does not boot, can the bench tell WHICH rail failed?

THE GAP
-------
``board/rail_budget.py`` knows the enable graph - which rail gates which,
and the tiers that forces. ``testpoint_plan.py`` knows which nets get a test
point, and takes the power-up order as a comma-separated string somebody
typed. Neither one crosses the two, so nothing in this bundle answers the
question a bring-up bench asks first.

The cost of the gap is asymmetric. Adding a test point before the Gerbers go
out is free. Discovering on the bench that two rails fail identically costs a
fab cycle, and it is discovered at exactly the moment the schedule has no
room for one.

WHAT IT COUNTS
--------------
Not "rails with test points" - that metric reads well and hides the problem.
A plan can cover every rail and still leave three failures indistinguishable.

The unit is the DISTINGUISHABLE CLASS: rails whose failures produce the same
observable state. Two rails in one class are two faults nobody can tell
apart. "5 rails, 5 classes" and "5 rails, 2 classes" are very different
boards.

Rails in DIFFERENT tiers are already separated in time - the later tier
never starts - so ambiguity is only ever inside a tier, which is exactly
where the enable graph refuses to impose an order.

PG IS NOT A RAIL MEASUREMENT
----------------------------
A regulator can assert power-good into a rail that is shorted downstream and
current-limiting: PG high, rail low, bench sees nothing wrong. A rail whose
only observation is PG is ``PG_ONLY`` - real information, and not the rail.

WHAT IT DOES NOT DO
-------------------
It does not decide whether a test point is physically probeable. There is no
placement here, and ``testpoint_plan.py`` already draws that line
(``phase: early_schematic``, ``physical: null``). This checks the SCOPE of
the plan, never the reachability of a pad.

Usage:
    boot_observability.py --rails rails.json --testpoint-plan plan.json
    boot_observability.py --rails rails.json --observation VDD_3V3=rail_voltage
    boot_observability.py --self-test

``rails.json`` (the shape rail_budget.py already reads):
    {"rails": [{"name": "3V3", "voltage_v": 3.3},
               {"name": "1V2", "voltage_v": 1.2, "enable_source": "3V3"}],
     "resets": [{"net": "nRST", "waits_for": ["1V2"]}]}

Exit codes: 0 = every failure is distinguishable, 1 = something is invisible
or ambiguous, 2 = bad input.
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
from common.observability import (GREEN, INDISTINGUISHABLE,  # noqa: E402
                                  NO_RAILS, OBS_KINDS, PG_ONLY, RED,
                                  RESET_UNOBSERVABLE, UNOBSERVED_RAIL,
                                  Observation, ObservabilityError,
                                  check_resets, diagnosability,
                                  failure_classes, load_observation,
                                  observations_from_testpoint_plan,
                                  rail_observability, rollup,
                                  unobserved_enable_edges)
from common.railbudget import ALWAYS_ON, Rail, enable_order  # noqa: E402
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "boot-observability-report/v1"
TOOL_NAME = "boot_observability"


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

def read_rails(path: Path) -> Tuple[List[Rail], List[dict]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: rails file must be a JSON object")
    entries = doc.get("rails")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 'rails' must be a non-empty array")
    rails = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each rail must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path}: a rail needs a non-empty 'name'")
        voltage = entry.get("voltage_v")
        if isinstance(voltage, bool) or not isinstance(voltage, (int, float)):
            raise ValueError(f"{path}: rail {name!r} needs a numeric "
                             "'voltage_v'")
        rails.append(Rail(name=name.strip(), voltage_v=float(voltage),
                          enable_source=str(entry.get("enable_source",
                                                      ALWAYS_ON)),
                          en_threshold_v=entry.get("en_threshold_v")))
    resets = doc.get("resets", []) or []
    if not isinstance(resets, list):
        raise ValueError(f"{path}: 'resets' must be an array")
    return rails, resets


def read_observations(testpoint_plan: Path | None,
                      pairs: List[str]) -> List[Observation]:
    """Test-point plan plus any ``NET=kind`` given on the command line."""
    out: List[Observation] = []
    if testpoint_plan is not None:
        plan = json.loads(Path(testpoint_plan).read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise ValueError(f"{testpoint_plan}: plan must be a JSON object")
        out.extend(observations_from_testpoint_plan(plan))
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--observation must be NET=KIND, got {pair!r}; "
                             f"KIND is one of {list(OBS_KINDS)}")
        net, _, kind = pair.partition("=")
        out.append(load_observation({"net": net.strip(), "kind": kind.strip(),
                                     "source": "cli"}))
    return out


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

def rail_finding(states) -> Finding:
    if not states:
        return Finding("rails_observable", NO_COVERAGE, 0, [],
                       "no rail was declared")
    violations = [s.to_dict() for s in states if s.status == FAIL]
    soft = [s for s in states if s.status == NO_COVERAGE]
    if violations:
        return result("rails_observable", len(states), violations,
                      f"{len(violations)} rail(s) cannot be looked at at all")
    if soft:
        return Finding("rails_observable", NO_COVERAGE, len(states) - len(soft),
                       [],
                       f"{len(soft)} rail(s) are covered only by power-good; "
                       "PG can be high while the rail is held down by a "
                       "downstream short")
    return result("rails_observable", len(states), [],
                  f"all {len(states)} rail voltage(s) can be measured")


def distinguishability_finding(classes, states) -> Finding:
    """The finding this tool exists for.

    Separate from ``rails_observable`` because a board can pass that one and
    fail this one: every rail having SOMETHING to look at does not mean two
    of them cannot fail identically.
    """
    if not classes:
        return Finding("failures_distinguishable", NO_COVERAGE, 0, [],
                       "nothing to distinguish")
    ambiguous = [c.to_dict() for c in classes if c.ambiguous]
    return result("failures_distinguishable", len(classes), ambiguous,
                  f"{len(states)} rail(s) resolve into {len(classes)} "
                  f"distinguishable failure class(es); "
                  f"{len(ambiguous)} class(es) hold more than one rail")


def reset_finding(resets) -> Finding:
    if not resets:
        return Finding("reset_sequence_observable", NO_COVERAGE, 0, [],
                       "no reset was declared; a board with no declared reset "
                       "is not a board with no reset")
    violations = [r.to_dict() for r in resets if r.status == FAIL]
    soft = [r for r in resets if r.status == NO_COVERAGE]
    if violations:
        return result("reset_sequence_observable", len(resets), violations,
                      f"{len(violations)} reset edge(s) cannot be seen")
    if soft:
        return Finding("reset_sequence_observable", NO_COVERAGE,
                       len(resets) - len(soft), [],
                       f"{len(soft)} reset(s) are observable but the rails "
                       "they wait for are not - only half of the comparison "
                       "exists")
    return result("reset_sequence_observable", len(resets), [],
                  "every reset edge and the rails it waits for can be measured")


def enable_finding(edges, rails) -> Finding:
    gated = [r for r in rails if r.enable_source != ALWAYS_ON]
    if not gated:
        return Finding("enable_edges_observable", NO_COVERAGE, 0, [],
                       "no rail is gated by another")
    if edges:
        return Finding("enable_edges_observable", NO_COVERAGE,
                       len(gated) - len(edges), [],
                       f"{len(edges)} enable source(s) are invisible; a gate "
                       "that never opened looks exactly like a regulator that "
                       "failed")
    return result("enable_edges_observable", len(gated), [],
                  "every enable source can be observed, directly or through "
                  "the rail driving it")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Can a bench tell which rail failed to come up?")
    parser.add_argument("--rails", type=Path,
                        help="rail tree with enable sources and resets")
    parser.add_argument("--testpoint-plan", type=Path,
                        help="testpoint_plan.py artifact to read observations "
                             "from")
    parser.add_argument("--observation", action="append", default=[],
                        metavar="NET=KIND",
                        help=f"an extra observation; KIND in {list(OBS_KINDS)}")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    if not args.rails:
        raise ValueError("--rails is required (or use --self-test)")

    rails, reset_entries = read_rails(args.rails)
    observations = read_observations(args.testpoint_plan, args.observation)

    try:
        tiers = enable_order(rails)
    except ValueError as exc:
        # An enable cycle is not a sequence; refusing here beats reporting
        # observability against an order that does not exist.
        raise ValueError(f"{args.rails}: {exc}") from exc

    states = rail_observability(rails, observations, tiers)
    classes = failure_classes(rails, states, tiers)
    resets = check_resets(reset_entries, rails, observations)
    edges = unobserved_enable_edges(rails, observations)

    findings = [rail_finding(states),
                distinguishability_finding(classes, states),
                reset_finding(resets),
                enable_finding(edges, rails)]

    status, reason_codes = rollup(states, classes, resets)
    counts = summarise(findings)
    if counts[FAIL] and status == GREEN:
        status = RED
    elif counts[NO_COVERAGE] and status == GREEN:
        # The FAIL half of this guard was here; the NO_COVERAGE half was not.
        # common/observability.rollup() has no term for unobserved enable
        # edges and no term for "no reset declared", so it returns GREEN in
        # exactly the cases enable_finding()/reset_finding() report as
        # NO_COVERAGE - and the findings were then summarised and thrown away.
        # sim/model_registry.overall() is the sibling that already gets this
        # right; this is the same two lines.
        status = NO_COVERAGE
        reason_codes = sorted(set(reason_codes) | {
            f["check"].upper() for f in findings
            if f["status"] == NO_COVERAGE})

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "early_schematic",
        "signoff": False,
        "checks_physical_access": False,
        "power_up_tiers": [list(group) for group in tiers],
        "observations": [o.to_dict() for o in observations],
        "rails": [s.to_dict() for s in states],
        "failure_classes": [c.to_dict() for c in classes],
        "resets": [r.to_dict() for r in resets],
        "enable_edges": edges,
        "diagnosability": diagnosability(states, classes),
        "findings": findings,
        "coverage": summarise(findings),
    }

    _diagnostics(report, states, classes, resets, status)
    write_report(report, args.json)
    emit(report)
    return status_exit_code(status)


def _diagnostics(report, states, classes, resets, status) -> None:
    marks = {PASS: "ok  ", FAIL: "RED ", NO_COVERAGE: "?   "}
    diag("")
    diag(f"boot_observability: {len(states)} rail(s) in "
         f"{len(report['power_up_tiers'])} tier(s)")
    for state in states:
        diag(f"  {marks[state.status]} T{state.tier} {state.rail:16s} "
             f"{','.join(state.kinds) or '(nothing)'}")
        if state.status != PASS:
            diag(f"       {state.detail}")
    for group in classes:
        if group.ambiguous:
            diag(f"  RED  tier {group.tier}: {list(group.rails)} fail "
                 f"identically")
    for reset in resets:
        diag(f"  {marks[reset.status]} reset {reset.net}")
        if reset.status != PASS:
            diag(f"       {reset.detail}")
    d = report["diagnosability"]
    diag(f"  {d['rails']} rail(s) -> {d['distinguishable_classes']} "
         f"distinguishable class(es), {d['ambiguous_classes']} ambiguous")
    diag(f"  status: {status}")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    from common.observability import OBS_PG, OBS_RAIL, OBS_RESET

    rails = [Rail("5V", 5.0),
             Rail("3V3", 3.3, enable_source="5V"),
             Rail("1V8", 1.8, enable_source="3V3"),
             Rail("1V2", 1.2, enable_source="3V3")]
    tiers = enable_order(rails)
    assert tiers == [["5V"], ["3V3"], ["1V2", "1V8"]], tiers

    def run(observations, resets=()):
        states = rail_observability(rails, observations, tiers)
        classes = failure_classes(rails, states, tiers)
        checks = check_resets(list(resets), rails, observations)
        return states, classes, checks, rollup(states, classes, checks)

    # Nothing observable: every rail is a failure AND the last tier is
    # ambiguous - two different defects, reported separately.
    states, classes, _, (status, reasons) = run([])
    assert status == RED
    assert UNOBSERVED_RAIL in reasons and INDISTINGUISHABLE in reasons

    # The trap this tool exists for: FULL rail coverage by count, and two
    # failures still indistinguishable.
    partial = [Observation("5V", OBS_RAIL), Observation("3V3", OBS_RAIL),
               Observation("1V8", OBS_PG), Observation("1V2", OBS_PG)]
    states, classes, _, (status, reasons) = run(partial)
    assert all(s.kinds for s in states), "every rail has SOMETHING"
    assert status == RED and INDISTINGUISHABLE in reasons
    ambiguous = [c for c in classes if c.ambiguous]
    assert len(ambiguous) == 1 and set(ambiguous[0].rails) == {"1V2", "1V8"}

    # One real measurement in that tier separates BOTH: the other rail is now
    # distinguishable by elimination.
    better = partial[:2] + [Observation("1V8", OBS_RAIL),
                            Observation("1V2", OBS_PG)]
    states, classes, _, (status, _) = run(better)
    assert not any(c.ambiguous for c in classes)
    # ...but 1V2 is still PG-only, so the run is not green either.
    assert status == NO_COVERAGE, status

    # Everything measured: green.
    full = [Observation(r.name, OBS_RAIL) for r in rails]
    assert run(full)[3][0] == GREEN

    # A reset nobody can see is a failure of its own kind.
    resets = [{"net": "nRST", "waits_for": ["1V2"]}]
    _, _, checks, (status, reasons) = run(full, resets)
    assert status == RED and RESET_UNOBSERVABLE in reasons, reasons

    # Observable reset + observable rails: the ORDER is checkable.
    _, _, checks, (status, _) = run(full + [Observation("nRST", OBS_RESET)],
                                    resets)
    assert status == GREEN and checks[0].status == PASS

    # A reset waiting on a rail nobody declared cannot be checked at all.
    _, _, checks, _ = run(full + [Observation("nRST", OBS_RESET)],
                          [{"net": "nRST", "waits_for": ["0V9"]}])
    assert checks[0].reason == "RESET_RAIL_NOT_A_DECLARED_RAIL"

    # An unlabelled observation kind does not load - PG counted as a rail
    # measurement is the error the distinction exists to prevent.
    for bad in (None, "testpoint", "yes"):
        try:
            load_observation({"net": "3V3", "kind": bad})
        except ObservabilityError:
            pass
        else:
            raise AssertionError(f"kind {bad!r} was accepted")

    # An enable cycle is refused rather than reported against a fake order.
    try:
        enable_order([Rail("A", 1.0, enable_source="B"),
                      Rail("B", 1.0, enable_source="A")])
    except ValueError:
        pass
    else:
        raise AssertionError("an enable cycle was accepted")

    diag("SELF-TEST PASS: a plan covering every rail by COUNT is still caught "
         "when two failures are indistinguishable, PG never counts as a rail "
         "measurement, one real measurement separates a tier by elimination, "
         "an unseeable reset is its own failure, a reset waiting on an "
         "undeclared rail is refused, an unlabelled observation kind does not "
         "load, and an enable cycle is not reported against a fake order")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
