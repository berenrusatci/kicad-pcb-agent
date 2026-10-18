#!/usr/bin/env python3
"""The ONE reusable "write, verify, roll back on regression" wrapper every
routing tool that mutates a board should sit on - plus a pre-flight
netclass/board track-width consistency check.

WHY A SEPARATE FILE (not folded into a router): ``scripts/route/grid_astar.py``
and ``scripts/route/topological.py`` are deliberately pcbnew-free (pure
geometry/search, see their own module docstrings). Both functions here need
a REAL board plus a real ``kicad-cli`` (to read netclasses and to run DRC) -
keeping that dependency in one place means the pure-logic routers keep their
"no pcbnew" claim true.

SAFE-WRITE DISCIPLINE
------------------------
``write_verify_or_rollback()``: back up the board -> apply the caller's
change -> refill zones -> run ``kicad-cli pcb drc --format json
--severity-all`` -> compare BOTH the ``violations`` count AND the
``unconnected_items`` count against the pre-change run -> if EITHER got
worse, restore the backup. Seeing "0 violations" alone does not mean a board
is done - this function tracks the two counts separately so a regression in
one is caught even if the other stays flat.

NOT PORTED (said plainly): the source's optional error-memory integration
(look up whether a similar strategy failed before, auto-record the outcome)
is not wired in here. pcb-agent already has an error-memory system
(``scripts/memory/error_memory.py``), but its ``ErrorMemory``/``Record``
API (markdown-backed, similarity-scored) is a different shape from the
source's ``HataHafizasi``/``HataKaydi`` dataclass model, and mapping one
onto the other correctly is real adaptation work, not a rename - doing it
without a caller that actually needs it would be the kind of unverified
integration this bundle refuses to carry. The core safe-write behaviour
(backup / verify / rollback) does not depend on it and is fully ported.

Usage:
    safe_write.py --netclass-check BOARD.kicad_pcb [--json out.json]
    safe_write.py --self-test

``write_verify_or_rollback()`` takes a Python callback and is meant to be
imported by another routing tool, not driven from this CLI directly (there
is no generic "apply this JSON-described change" mutation format to drive
it from the command line).

Exit codes: 0 = PASS, 1 = FAIL/violation found, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    NO_COVERAGE, Finding, board_exit_code, board_report, result,
)
from common.kicad_cli import _require_cli  # noqa: E402
from common.report_io import emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "safe_write"

NM_PER_MM = 1_000_000


# --------------------------------------------------------------------------- #
# 1. Netclass width validator (pure logic, no pcbnew needed)
# --------------------------------------------------------------------------- #
def netclass_width_violations(netclass_widths_mm: Dict[str, float],
                              board_min_track_width_mm: float) -> List[dict]:
    """Checks every netclass's ``track_width`` against the board's
    ``min_track_width`` BEFORE routing starts.

    A netclass that specifies a narrower ``track_width`` than the board's
    minimum will have EVERY track assigned to it fail DRC with "track width
    below minimum" - almost always a configuration mistake (netclass typo,
    or the board setting was tightened after the netclass was written), and
    should be caught here, explicitly, before routing silently produces
    violating tracks.
    """
    violations = []
    for name, width_mm in netclass_widths_mm.items():
        if width_mm < board_min_track_width_mm:
            violations.append({
                "netclass": name, "defined_track_width_mm": width_mm,
                "board_min_track_width_mm": board_min_track_width_mm,
            })
    return violations


def check_netclass_widths(netclass_widths_mm: Dict[str, float],
                          board_min_track_width_mm: float):
    """``netclass_width_violations()`` wrapped as a Finding - ``scanned=0``
    (no netclasses at all) is NO_COVERAGE, never a silent PASS."""
    violations = netclass_width_violations(netclass_widths_mm, board_min_track_width_mm)
    return result("netclass_width_check", len(netclass_widths_mm), violations,
                 f"board_min_track_width_mm={board_min_track_width_mm}, "
                 f"{len(netclass_widths_mm)} netclass(es) checked.")


def check_netclass_widths_from_board(board_path: str):
    """Real-``.kicad_pcb``-reading wrapper: reads netclass widths and the
    board's own minimum track width from ``pcbnew.BOARD.GetDesignSettings()``.
    """
    kontrol = "netclass_width_check"
    try:
        import pcbnew
    except ImportError as exc:
        return result(kontrol, 0, [],
                     f"pcbnew module not importable ({exc}) - not running "
                     "inside KiCad's Python environment.")

    board = pcbnew.LoadBoard(board_path)
    settings = board.GetDesignSettings()
    board_min_mm = settings.m_TrackMinWidth / NM_PER_MM

    # Netclasses live on BOARD_DESIGN_SETTINGS ("settings"), not on the
    # board object itself.
    netclass_widths: Dict[str, float] = {}
    netclasses = settings.GetNetClasses() if hasattr(settings, "GetNetClasses") else {}
    for name, nc in netclasses.items():
        netclass_widths[str(name)] = nc.GetTrackWidth() / NM_PER_MM
    # The default netclass is not always included in GetNetClasses() on
    # every KiCad version; add it only if a separate accessor exists, never
    # invent a "Default" entry that was not actually read.
    if hasattr(settings, "GetDefault"):
        default = settings.GetDefault()
        netclass_widths.setdefault(default.GetName(), default.GetTrackWidth() / NM_PER_MM)

    return check_netclass_widths(netclass_widths, board_min_mm)


# --------------------------------------------------------------------------- #
# 2. Safe write + verify (backup -> write -> refill -> DRC -> rollback if worse)
# --------------------------------------------------------------------------- #
def _run_drc_severity_all(board_path: str, kicad_cli: Optional[str] = None,
                          timeout_s: int = 180) -> Dict[str, Any]:
    """``--severity-all`` is not optional here: the default severity filter
    can leave some violation classes (e.g. warning-level only) out of the
    report, which is exactly the risk "0 violations does not mean done"
    warns about."""
    cli = _require_cli(kicad_cli)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report_path = tmp.name
    # --all-track-errors is load-bearing HERE in a way it is nowhere else:
    # this function is called twice and the two counts are SUBTRACTED to
    # decide whether to keep or roll back a board write. Without the flag
    # kicad-cli reports only the first error per track, and which one that is
    # depends on whichever DRC worker finishes first - drc_gate.py measured
    # 189..205 across five runs of one unchanged board. Differencing two
    # draws from that distribution makes the rollback decision a coin flip in
    # both directions: a clean change can be reverted, and a real regression
    # can be kept. drc_gate.py::build_argv carries the measurement; the flag
    # never reached this copy of the invocation.
    cmd = [cli, "pcb", "drc", "--output", report_path, "--format", "json",
          "--severity-all", "--all-track-errors", board_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout_s)
    if proc.returncode not in (0, 1):  # 1 = violations found, not a tool error
        raise RuntimeError(f"kicad-cli DRC failed (returncode={proc.returncode}): {proc.stderr}")
    with open(report_path, encoding="utf-8") as fh:
        return json.load(fh)


def write_verify_or_rollback(board_path: str, apply_change: Callable[[Any], Any],
                             kicad_cli: Optional[str] = None,
                             refill_zones: bool = True):
    """Backs up the board to ``<board_path>.safe_write_backup``, calls
    ``apply_change(board)`` on a LOADED ``pcbnew.BOARD``, saves, optionally
    refills zones, runs DRC before and after, and restores the backup if
    EITHER the violation count or the unconnected-items count got worse.

    ``apply_change`` must NOT call ``board.Save()`` itself - this function
    is the single save point, to avoid double-saves / inconsistent state.

    If the BEFORE state could not be measured (e.g. the board was already
    broken, or kicad-cli itself errored), the change is still applied, but
    reported with ``regression_measured=False`` rather than silently
    assuming "no regression".
    """
    kontrol = "safe_write_verify"
    try:
        import pcbnew
    except ImportError as exc:
        return result(kontrol, 0, [],
                     f"pcbnew module not importable ({exc}) - not running "
                     "inside KiCad's Python environment.")

    path = Path(board_path)
    if not path.is_file():
        return result(kontrol, 0, [], f"{board_path} not found.")

    backup_path = str(path) + ".safe_write_backup"
    shutil.copy2(board_path, backup_path)

    before_violations: Optional[int] = None
    before_unconnected: Optional[int] = None
    try:
        before = _run_drc_severity_all(board_path, kicad_cli=kicad_cli)
        before_violations = len(before.get("violations", []))
        before_unconnected = len(before.get("unconnected_items", []))
    except Exception:
        pass  # before-state could not be measured; flagged below, not assumed clean

    board = pcbnew.LoadBoard(board_path)
    apply_change(board)
    board.Save(board_path)

    refill_error: Optional[str] = None
    if refill_zones:
        try:
            reloaded = pcbnew.LoadBoard(board_path)
            filler = pcbnew.ZONE_FILLER(reloaded)
            filler.Fill(reloaded.Zones())
            reloaded.Save(board_path)
        except Exception as exc:
            refill_error = str(exc)  # a refill failure does not block DRC, only gets reported

    try:
        after = _run_drc_severity_all(board_path, kicad_cli=kicad_cli)
    except Exception as exc:
        shutil.copy2(backup_path, board_path)
        return result(kontrol, 1, [{
            "reason": f"DRC run failed, change was ROLLED BACK: {exc}",
        }], f"board restored from {backup_path}.")

    after_violations = len(after.get("violations", []))
    after_unconnected = len(after.get("unconnected_items", []))

    if before_violations is None:
        regressed, regression_measured = False, False
    else:
        regressed = (after_violations > before_violations
                    or after_unconnected > before_unconnected)
        regression_measured = True

    if regressed:
        shutil.copy2(backup_path, board_path)
        return result(kontrol, 1, [{
            "reason": "DRC regression (violations and/or unconnected_items increased) "
                     "- change ROLLED BACK",
            "before_violations": before_violations, "after_violations": after_violations,
            "before_unconnected": before_unconnected, "after_unconnected": after_unconnected,
        }], f"board restored from {backup_path}.")

    detail = (f"change applied and KEPT. regression_measured={regression_measured}, "
             f"violations: {before_violations}->{after_violations}, "
             f"unconnected_items: {before_unconnected}->{after_unconnected}.")
    if refill_error:
        detail += f" WARNING: zone refill failed ({refill_error}) - DRC still ran."

    if not regression_measured:
        # The docstring above promises this case is "reported with
        # regression_measured=False rather than silently assuming 'no
        # regression'" - but it used to be reported ONLY in the free-text
        # detail, while the machine-readable status said PASS. summarise(),
        # board_exit_code() and every assertion selector read the status, not
        # the prose. A board write kept without a before/after comparison is
        # exactly NO_COVERAGE: the change is applied, and nothing verified it.
        return Finding(kontrol, NO_COVERAGE, 1, [],
                       "NOT VERIFIED: the before-DRC could not be measured, so "
                       "no regression comparison exists for this write. " + detail)
    return result(kontrol, 1, [], detail)


# --------------------------------------------------------------------------- #
# CLI - exposes the netclass check only; write_verify_or_rollback takes a
# Python callback and is meant to be imported, not driven generically here.
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    clean = netclass_width_violations({"Default": 0.2, "HighSpeed": 0.15}, 0.1)
    assert clean == [], f"widths above the board minimum should not violate: {clean}"

    broken = netclass_width_violations({"Default": 0.2, "TooNarrow": 0.05}, 0.1)
    assert len(broken) == 1 and broken[0]["netclass"] == "TooNarrow", \
        f"injected width violation was not caught: {broken}"
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netclass-check", metavar="BOARD",
                    help="check this board's netclass widths against its own minimum")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.netclass_check:
        ap.error("--netclass-check BOARD is required unless --self-test is given")

    finding = check_netclass_widths_from_board(a.netclass_check)
    report = board_report(TOOL_NAME, a.netclass_check, [finding], params={})
    write_report(report, a.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
