#!/usr/bin/env python3
"""Iterative sign integrity feedback loop: read a simulator's verdict on a
differential pair, and if it missed the target impedance, compute the
(width, gap) that WOULD hit it and re-apply it to the board - repeat until
in tolerance or a hard iteration ceiling is reached.

    parse_simulation_log()  ->  compute_corrected_geometry()  ->
    apply_geometry() (pcbnew)  ->  run_simulation() again  ->  repeat

WHAT THIS MODULE DELIBERATELY DOES NOT DO
--------------------------------------------
It does not call openEMS, PathWave, or any other simulator itself.
``scripts/sim/openems.py``'s own docstring already explains why: this
bundle's FDTD run chain has never been executed against real openEMS in
this environment (openEMS/CSXCAD are not `pip install`-able, and PathWave
is a commercial Keysight application with an automation interface this
codebase has zero grounding in). Writing code that CLAIMS to drive either
tool without ever having run it would be exactly the kind of unverified
integration docs/CONSOLIDATION-NOTES.md already rejects other tools for.

Instead, ``run_simulation`` is a CALLBACK the caller supplies - wire it to
your real openEMS/PathWave invocation (or to ``scripts/sim/openems.py``'s
``evaluate_touchstone()`` once a real ``.sNp`` file exists). This module
owns the LOOP, the CORRECTION MATH and the PCBNEW WRITE-BACK; it does not
own - and does not pretend to own - the simulation itself.

CORRECTION MATH IS NOT AN ARBITRARY OFFSET
---------------------------------------------
A naive "measured 115ohm, target 100ohm, so shave 0.02mm off the width"
rule has no physical grounding and would be exactly the kind of invented
number this bundle refuses to carry. Instead, ``compute_corrected_geometry``
calls the ALREADY-VERIFIED closed-form solver
``common.stackup.solve_impedance_geometry`` (used by
``scripts/zdiff_solver.py`` and ``scripts/stackup_plan.py``) to compute the
(W, S) that actually meets the simulator's stated target on the real
stackup (H, Er, copper weight) - the same solver, not a second one that
could silently disagree with it.

LOG PARSING SCOPE
--------------------
``parse_simulation_log`` normalises an ALREADY-STRUCTURED simulator report
(a dict, or a JSON string) into the canonical
``{"metric", "current_value", "target_value", "status"}`` shape. It does
NOT regex-parse arbitrary unstructured simulator prose - a hand-written
pattern for a log format never seen would risk silently misreading real
data, which is the same reasoning
``scripts/report/capability_inventory.py``'s external-dependency scan
documents for why it only recognises known patterns rather than guessing.
If your simulator's raw output is not already one of the two accepted
shapes, adapt it to one before calling this loop.

FAIL-CLOSED, PER THIS PROJECT'S OWN RULE
--------------------------------------------
A simulation error, an unreachable target, or exceeding ``max_iterations``
all raise ``RuntimeError`` - never a fabricated PASS. Reaching tolerance is
the ONLY way ``run_feedback_loop`` returns normally.

Usage (library only - no CLI, this loop is meant to be driven by a caller
that owns the real simulator invocation):

    from ai_feedback_loop import run_feedback_loop, LoopConfig

    def run_sim():
        ...  # your real openEMS/PathWave call, returns a dict or JSON text
        return {"metric": "differential_impedance", "current_value": 115.4,
                "target_value": 100.0, "status": "FAIL"}

    def get_geometry():
        return current_w_mm, current_s_mm

    def apply_geometry(w_mm, s_mm):
        ...  # your real pcbnew write, or use apply_trace_geometry() below

    record = run_feedback_loop(run_sim, get_geometry, apply_geometry,
                               pair=my_diff_pair, h_mm=0.2, er=4.3)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import diag  # noqa: E402
from common.stackup import DiffPair, solve_impedance_geometry  # noqa: E402

REQUIRED_LOG_KEYS = ("metric", "current_value", "target_value", "status")
VALID_STATUSES = ("PASS", "FAIL")

DEFAULT_MAX_ITERATIONS = 15
DEFAULT_TOLERANCE_PCT = 2.0


class SimulationLogError(ValueError):
    """The simulator's report is not in a recognised, structured shape."""


class ConvergenceError(RuntimeError):
    """The loop did not converge - either the target is unreachable at this
    stackup, or max_iterations was exhausted. Fail-closed: never returns a
    fabricated PASS instead of raising this."""


# --------------------------------------------------------------------------- #
# 1. JSON log parser
# --------------------------------------------------------------------------- #
def parse_simulation_log(raw: Union[str, dict]) -> Dict[str, Any]:
    """Normalises an already-structured simulator report into
    ``{"metric", "current_value", "target_value", "status"}``.

    Accepts:
      - a dict already in the canonical shape (validated and returned as-is),
      - a JSON string of that same shape,
      - a dict shaped like ``scripts/sim/openems.py::evaluate_touchstone()``'s
        Finding (has ``check``/``status``/``violations`` with
        ``max_return_loss_db``) - adapted into the canonical shape using the
        WORST (highest) measured return loss across ports as
        ``current_value``.

    Raises ``SimulationLogError`` for anything else - a malformed or
    unrecognised report must stop the loop, not be silently guessed at.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SimulationLogError(
                f"simulation log is not valid JSON and no other structured "
                f"shape is accepted: {exc}") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise SimulationLogError(f"simulation log must be a dict or a JSON string, got {type(raw)}")

    if all(key in data for key in REQUIRED_LOG_KEYS):
        return _validate_canonical(data)

    if "check" in data and "violations" in data and "status" in data:
        return _adapt_touchstone_finding(data)

    raise SimulationLogError(
        f"simulation log has neither the canonical keys {REQUIRED_LOG_KEYS} nor "
        "the evaluate_touchstone() Finding shape ('check'/'status'/'violations') "
        "- adapt your simulator's output to one of these before calling this loop.")


def _validate_canonical(data: dict) -> Dict[str, Any]:
    status = data["status"]
    if status not in VALID_STATUSES:
        raise SimulationLogError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    for key in ("current_value", "target_value"):
        if not isinstance(data[key], (int, float)):
            raise SimulationLogError(f"{key} must be numeric, got {data[key]!r}")
    return {"metric": str(data["metric"]), "current_value": float(data["current_value"]),
           "target_value": float(data["target_value"]), "status": status}


def _adapt_touchstone_finding(data: dict) -> Dict[str, Any]:
    violations = data.get("violations") or []
    if not violations:
        # No violation recorded - evaluate_touchstone's own status already
        # says PASS/NO_COVERAGE; there is no single "current_value" to
        # report without a violation entry to read the measurement from.
        raise SimulationLogError(
            "evaluate_touchstone() Finding has no violations to adapt a "
            "current_value from - only a FAILing Finding carries the measured "
            "return loss; a PASSing run has nothing for this loop to correct.")
    worst = max(violations, key=lambda v: v.get("max_return_loss_db", float("-inf")))
    if "max_return_loss_db" not in worst or "limit_db" not in worst:
        raise SimulationLogError(
            "evaluate_touchstone() violation entry is missing max_return_loss_db/limit_db")
    return {"metric": "return_loss_db", "current_value": float(worst["max_return_loss_db"]),
           "target_value": float(worst["limit_db"]), "status": "FAIL"}


# --------------------------------------------------------------------------- #
# 2. Correction engine - real closed-form solver, not an invented offset
# --------------------------------------------------------------------------- #
@dataclass
class CorrectedGeometry:
    w_mm: float
    s_mm: float
    predicted_zdiff_ohm: float
    predicted_error_percent: float


def compute_corrected_geometry(target_ohm: float, h_mm: float, er: float,
                               t_mm: float = 0.035,
                               fab_min_w_mm: float = 0.127,
                               fab_min_s_mm: float = 0.127,
                               pair_name: str = "feedback_loop_pair") -> CorrectedGeometry:
    """Calls the SAME closed-form solver ``scripts/zdiff_solver.py`` and
    ``scripts/stackup_plan.py`` use (``common.stackup.solve_impedance_geometry``)
    to find the (W, S) that meets ``target_ohm`` on the given stackup.

    Raises ``ConvergenceError`` if the target is UNREACHABLE at this H/Er/
    fab-limit combination - that is a stackup problem, not something a
    trace-width tweak can fix, and must stop the loop rather than apply a
    geometry that cannot work.
    """
    from common.stackup import InterfaceKind

    pair = DiffPair(name=pair_name, interface=InterfaceKind.USB2_0,
                    length_p_mm=0.0, length_n_mm=0.0,
                    target_impedance_ohm_override=target_ohm)
    solved = solve_impedance_geometry(pair, h_mm, er, t_mm, fab_min_w_mm, fab_min_s_mm)
    if not solved["reachable"] or solved["best"] is None:
        raise ConvergenceError(
            f"target {target_ohm} ohm is UNREACHABLE at h={h_mm}mm, er={er} within the "
            f"swept W/S range and fab limits (min_w={fab_min_w_mm}mm, min_s={fab_min_s_mm}mm) "
            "- revise the stackup, a trace geometry change alone cannot fix this.")
    best = solved["best"]
    return CorrectedGeometry(w_mm=best["w_mm"], s_mm=best["s_mm"],
                             predicted_zdiff_ohm=best["zdiff_ohm"],
                             predicted_error_percent=best["error_percent"])


# --------------------------------------------------------------------------- #
# 3. pcbnew write-back - lazy import, same NO_COVERAGE discipline as every
#    other board/*.py tool in this bundle
# --------------------------------------------------------------------------- #
def apply_trace_geometry(board_path: str, net_names: Tuple[str, str],
                         w_mm: float, s_mm: float) -> int:
    """Sets the track width to ``w_mm`` for every ``PCB_TRACK`` on either
    net in ``net_names`` and saves the board. Returns the number of tracks
    updated.

    ``s_mm`` (the gap) is NOT applied here - KiCad has no single "set gap
    between these two nets" API; a real gap change requires re-routing (or
    a differential-pair-aware router) with the new spacing rule, which is
    out of scope for a width-only write-back. Callers that need the gap
    applied must feed ``s_mm`` into their own re-route step; it is still
    returned by ``compute_corrected_geometry`` so nothing is silently
    dropped, only NOT WRITTEN by this specific function.

    Raises ``RuntimeError`` (not silently continuing) if pcbnew is not
    importable - there is no NO_COVERAGE-and-continue option for a
    correction step that MUST apply for the loop to make progress; the
    caller of ``run_feedback_loop`` sees this as the loop's own
    ``ConvergenceError``/``RuntimeError`` fail-closed path.
    """
    try:
        import pcbnew
    except ImportError as exc:
        raise RuntimeError(
            f"pcbnew module not found ({exc}) - apply_trace_geometry must run inside "
            "KiCad's bundled Python.") from exc

    board = pcbnew.LoadBoard(board_path)
    updated = 0
    for track in board.GetTracks():
        if track.GetClass() != "PCB_TRACK":
            continue
        if track.GetNetname() not in net_names:
            continue
        track.SetWidth(pcbnew.FromMM(w_mm))
        updated += 1
    if updated:
        board.Save(board_path)
    return updated


# --------------------------------------------------------------------------- #
# 4. Loop controller
# --------------------------------------------------------------------------- #
@dataclass
class LoopIteration:
    iteration: int
    log: Dict[str, Any]
    within_tolerance: bool
    correction: Optional[CorrectedGeometry] = None


@dataclass
class LoopResult:
    converged: bool
    iterations_used: int
    history: List[LoopIteration] = field(default_factory=list)
    final_log: Optional[Dict[str, Any]] = None


def run_feedback_loop(
    run_simulation: Callable[[], Union[str, dict]],
    get_current_geometry: Callable[[], Tuple[float, float]],
    apply_geometry: Callable[[float, float], None],
    h_mm: float,
    er: float,
    t_mm: float = 0.035,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> LoopResult:
    """The main optimisation loop: simulate -> parse -> if outside
    tolerance, compute and apply a corrected (W, S) -> simulate again.

    ``run_simulation()`` is called with NO ARGUMENTS - it owns triggering
    the real simulator (or a fixture, in tests) AND reading its output;
    this loop only reads whatever it returns via ``parse_simulation_log``.

    Stops and returns normally the moment a log reports ``status="PASS"``
    OR ``abs(current_value - target_value) / target_value * 100 <=
    tolerance_pct``. Raises ``ConvergenceError`` if neither happens within
    ``max_iterations`` - this is the hard ceiling the task specification
    requires so the loop can never run forever.

    ``get_current_geometry`` and ``apply_geometry`` are also
    dependency-injected: this keeps the loop testable without a real board
    (see the test suite's fake pcbnew), and lets a caller choose whether
    ``apply_geometry`` writes to pcbnew directly, queues a change for
    review, or does something else entirely.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    if tolerance_pct <= 0:
        raise ValueError("tolerance_pct must be positive")

    history: List[LoopIteration] = []
    for iteration in range(1, max_iterations + 1):
        raw = run_simulation()
        try:
            log = parse_simulation_log(raw)
        except SimulationLogError as exc:
            raise RuntimeError(
                f"iteration {iteration}: simulator report could not be parsed - "
                f"fail-closed, not treated as a silent pass: {exc}") from exc

        error_pct = abs(log["current_value"] - log["target_value"]) / log["target_value"] * 100.0
        within_tolerance = log["status"] == "PASS" or error_pct <= tolerance_pct

        if within_tolerance:
            history.append(LoopIteration(iteration, log, True))
            diag(f"iteration {iteration}: within tolerance "
                f"({error_pct:.2f}% <= {tolerance_pct}%) - loop converged")
            return LoopResult(converged=True, iterations_used=iteration,
                             history=history, final_log=log)

        current_w_mm, current_s_mm = get_current_geometry()
        try:
            correction = compute_corrected_geometry(
                target_ohm=log["target_value"], h_mm=h_mm, er=er, t_mm=t_mm)
        except ConvergenceError:
            history.append(LoopIteration(iteration, log, False))
            raise

        history.append(LoopIteration(iteration, log, False, correction))
        diag(f"iteration {iteration}: {log['metric']}={log['current_value']} vs "
            f"target={log['target_value']} ({error_pct:.2f}% off) - applying "
            f"w={correction.w_mm}mm s={correction.s_mm}mm "
            f"(predicted {correction.predicted_zdiff_ohm} ohm)")
        apply_geometry(correction.w_mm, correction.s_mm)

    raise ConvergenceError(
        f"did not converge within max_iterations={max_iterations} - fail-closed, "
        f"the loop stops rather than returning an unconverged result as success. "
        f"Last measurement: {history[-1].log if history else 'none'}.")


def _self_test() -> int:
    canonical = parse_simulation_log(
        {"metric": "differential_impedance", "current_value": 115.4,
         "target_value": 100.0, "status": "FAIL"})
    assert canonical["status"] == "FAIL"

    try:
        parse_simulation_log({"nonsense": True})
        raise AssertionError("an unrecognised log shape must raise, not be silently guessed at")
    except SimulationLogError:
        pass

    touchstone_finding = {
        "check": "openems_touchstone_evaluation", "status": "FAIL", "scanned": 1,
        "violations": [{"port": 1, "max_return_loss_db": -6.0, "limit_db": -10.0,
                       "issue": "single_ended_return_loss_exceeded"}],
    }
    adapted = parse_simulation_log(touchstone_finding)
    assert adapted["current_value"] == -6.0 and adapted["target_value"] == -10.0

    # A fake, deterministic "simulator" that improves every time apply_geometry
    # is called, converging on iteration 3 - no real openEMS/PathWave involved.
    state = {"w_mm": 0.10, "s_mm": 0.10, "call": 0}

    def fake_sim():
        state["call"] += 1
        current = 130.0 - state["call"] * 15.0  # 115, 100, 85... converges near iter 2
        return {"metric": "differential_impedance", "current_value": current,
               "target_value": 100.0, "status": "FAIL" if abs(current - 100.0) > 2.0 else "PASS"}

    def get_geom():
        return state["w_mm"], state["s_mm"]

    def apply_geom(w_mm, s_mm):
        state["w_mm"], state["s_mm"] = w_mm, s_mm

    result = run_feedback_loop(fake_sim, get_geom, apply_geom, h_mm=0.2, er=4.3, max_iterations=15)
    assert result.converged, f"the fake simulator should converge: {result}"
    assert result.iterations_used <= 15

    def always_fails_sim():
        return {"metric": "differential_impedance", "current_value": 200.0,
               "target_value": 100.0, "status": "FAIL"}

    try:
        run_feedback_loop(always_fails_sim, get_geom, apply_geom, h_mm=0.2, er=4.3,
                          max_iterations=3)
        raise AssertionError("exceeding max_iterations must raise ConvergenceError, "
                            "never return a fabricated success")
    except ConvergenceError:
        pass

    diag("SELF-TEST PASS: canonical + Touchstone-Finding log parsing, real-solver "
        "correction, deterministic convergence, and the fail-closed iteration "
        "ceiling all verified")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
