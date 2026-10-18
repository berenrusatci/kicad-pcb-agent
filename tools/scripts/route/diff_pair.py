#!/usr/bin/env python3
"""Locks SIMPLE differential pairs with a real, direct pcbnew track - the
missing "lock" half of the otonom-routing skill's own stated architecture.

WHY THIS EXISTS: ``pcbnew.ExportSpecctraDSN()`` does not carry differential-
pair matching/coupling information into the DSN at all (confirmed by
inspecting a real .dsn export from a differential-pair board). A high-speed
pair must therefore be resolved with REAL pcbnew geometry BEFORE any DSN-
based router (FreeRouting) ever sees the board. This module draws that pair
as locked (``SetLocked(True)``) tracks; a router that runs afterwards
(``scripts/route/grid_astar.py``, FreeRouting) treats a locked track as an
obstacle, never as something to move.

skills/otonom-routing's own SKILL.md already states the intended division of
labour: "üç motorun hiçbirinde diferansiyel çift ... semantiği yoktur ...
çiftler otorutere ayarla değil kilitle korunur" (none of the three router
engines understand diff pairs; pairs are protected from the autorouter by
being LOCKED, not tuned by it). This tool is that locking step - it does not
compete with or change that skill's routers, it produces their protected
input.

SCOPE - ONLY SIMPLE PAIRS, SAID PLAINLY
-----------------------------------------
This is not a general differential-pair router. It resolves only a pair
whose two ends can be joined by ONE straight segment each, where the P and N
connection vectors are close enough to parallel (within ``--tolerance-deg``).
A large P/N angle mismatch - the same geometry class as a "pair twist"
problem at a connector - is refused outright, with a reason, rather than
drawing a wrong/short-risk track. Also: only exactly-2-pad P/N net pairs;
a branching (3+ pad) net must be split into 2-pad segments upstream before
reaching this tool.

Usage:
    diff_pair.py <board.kicad_pcb> --pairs pairs.json [--json out.json]
                 [--tolerance-deg 15.0]
    diff_pair.py --self-test

``pairs.json``: ``[{"p_net": "...", "n_net": "...", "width_mm": 0.2,
"layer": "F.Cu"}, ...]``

Exit codes: 0 = every pair locked, 1 = at least one pair refused or
NO_COVERAGE, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import board_exit_code, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "diff_pair"

DEFAULT_TOLERANCE_DEG = 15.0
DEFAULT_WIDTH_MM = 0.2
DEFAULT_LAYER = "F.Cu"

Point = Tuple[float, float]


@dataclass
class DiffPairRequest:
    p_net: str
    n_net: str
    width_mm: float = DEFAULT_WIDTH_MM
    layer: str = DEFAULT_LAYER


@dataclass
class DiffPairOutcome:
    """Carries a reason on BOTH success and refusal - no silent "tried and
    it didn't work"."""

    p_net: str
    n_net: str
    locked: bool
    detail: str
    p_length_mm: Optional[float] = None
    n_length_mm: Optional[float] = None
    skew_mm: Optional[float] = None


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def net_pad_positions(board, net_name: str) -> List[Tuple[str, str, float, float]]:
    """All pad positions on ``net_name`` as ``(refdes, pad_no, x_mm, y_mm)``.

    Takes an already-loaded ``board`` - does not import pcbnew itself, so it
    stays testable with a duck-typed fake board.
    """
    NM_PER_MM = 1_000_000
    out: List[Tuple[str, str, float, float]] = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() == net_name:
                pos = pad.GetPosition()
                out.append((fp.GetReference(), pad.GetNumber(),
                           pos.x / NM_PER_MM, pos.y / NM_PER_MM))
    return out


def closest_pairing(p1: Point, p2: Point, n1: Point, n2: Point
                    ) -> Tuple[Point, Point, Point, Point]:
    """Matches (p1,p2) against (n1,n2) by PHYSICAL PROXIMITY, not
    positional order - a real differential pair's P/N pads at the same
    connector/IC end sit close together, so the closer-total-distance
    pairing is the "same end" pairing.

    Returns ``(p_start, n_start, p_end, n_end)`` - the start/end labels are
    arbitrary (just distinguishing the two ends), but ``p_start`` is ALWAYS
    the physical partner of ``n_start``.
    """
    same_order = _distance(p1, n1) + _distance(p2, n2)
    crossed = _distance(p1, n2) + _distance(p2, n1)
    if same_order <= crossed:
        return p1, n1, p2, n2
    return p1, n2, p2, n1


def parallelism_angle_deg(p_start: Point, p_end: Point,
                          n_start: Point, n_end: Point) -> float:
    """Angle (0-180 deg) between the P connection vector and the N
    connection vector. A zero-length vector (start==end, e.g. two pads at
    the same point) has an UNDEFINED angle - returns 180.0 (the least safe
    value), never silently 0."""
    vp = (p_end[0] - p_start[0], p_end[1] - p_start[1])
    vn = (n_end[0] - n_start[0], n_end[1] - n_start[1])
    lp, ln = math.hypot(*vp), math.hypot(*vn)
    if lp < 1e-9 or ln < 1e-9:
        return 180.0
    cosine = max(-1.0, min(1.0, (vp[0] * vn[0] + vp[1] * vn[1]) / (lp * ln)))
    return math.degrees(math.acos(cosine))


def _draw_locked_track(pcbnew, board, start_mm, end_mm, net, layer_id: int,
                       width_mm: float):
    """One straight ``pcbnew.PCB_TRACK`` segment, added and IMMEDIATELY
    locked - an unlocked track drawn here could be moved by a router that
    runs afterwards, defeating the whole point of this tool."""
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start_mm[0]), pcbnew.FromMM(start_mm[1])))
    track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end_mm[0]), pcbnew.FromMM(end_mm[1])))
    track.SetWidth(pcbnew.FromMM(width_mm))
    track.SetLayer(layer_id)
    if net is not None:
        track.SetNet(net)
    track.SetLocked(True)
    board.Add(track)
    return track


def process_pair(pcbnew, board, pair: DiffPairRequest,
                 tolerance_deg: float) -> DiffPairOutcome:
    """One pair: find pads, match ends, check parallelism, draw+lock on
    pass; write NOTHING to the board on refusal."""
    p_pads = net_pad_positions(board, pair.p_net)
    n_pads = net_pad_positions(board, pair.n_net)

    if len(p_pads) != 2 or len(n_pads) != 2:
        return DiffPairOutcome(
            pair.p_net, pair.n_net, False,
            f"both P/N nets must have EXACTLY 2 pads (P={len(p_pads)}, "
            f"N={len(n_pads)}) - a branching (3+ pad) net must be split into "
            "2-pad segments upstream before reaching this tool.")

    p1, p2 = (p_pads[0][2], p_pads[0][3]), (p_pads[1][2], p_pads[1][3])
    n1, n2 = (n_pads[0][2], n_pads[0][3]), (n_pads[1][2], n_pads[1][3])

    p_start, n_start, p_end, n_end = closest_pairing(p1, p2, n1, n2)
    angle = parallelism_angle_deg(p_start, p_end, n_start, n_end)

    if angle > tolerance_deg:
        return DiffPairOutcome(
            pair.p_net, pair.n_net, False,
            f"P/N connection vectors are {angle:.1f} deg apart > tolerance "
            f"{tolerance_deg:.1f} deg - same geometry class as a connector "
            "pair-twist problem. Refusing rather than drawing a wrong/short-"
            "risk track; needs a coupled router or a human.")

    p_net = board.FindNet(pair.p_net)
    n_net = board.FindNet(pair.n_net)
    if p_net is None or n_net is None:
        return DiffPairOutcome(
            pair.p_net, pair.n_net, False,
            f"board.FindNet() could not find the net (P found={p_net is not None}, "
            f"N found={n_net is not None}) - a pad can carry a net name the "
            "board itself has no net record for.")

    layer_id = board.GetLayerID(pair.layer)
    _draw_locked_track(pcbnew, board, p_start, p_end, p_net, layer_id, pair.width_mm)
    _draw_locked_track(pcbnew, board, n_start, n_end, n_net, layer_id, pair.width_mm)

    p_len, n_len = _distance(p_start, p_end), _distance(n_start, n_end)
    return DiffPairOutcome(
        pair.p_net, pair.n_net, True,
        f"routed as a direct locked segment (angle={angle:.1f} deg).",
        p_length_mm=round(p_len, 4), n_length_mm=round(n_len, 4),
        skew_mm=round(abs(p_len - n_len), 4))


def lock_diff_pairs(board_path: str, pairs: List[DiffPairRequest],
                    tolerance_deg: float = DEFAULT_TOLERANCE_DEG
                    ) -> Tuple["object", List[DiffPairOutcome]]:
    """Entry point. NO_COVERAGE (not a crash) when pcbnew is unavailable.

    A ``.bak`` backup is taken BEFORE saving, only if at least one pair
    actually locked - if nothing changed, backing up an unchanged file is
    pointless and is skipped.
    """
    kontrol = "diff_pair_routing"
    try:
        import pcbnew
    except ImportError as exc:
        return result(kontrol, 0, [],
                     f"pcbnew module not found ({exc}) - must run inside "
                     "KiCad's bundled Python."), []

    board = pcbnew.LoadBoard(board_path)
    outcomes = [process_pair(pcbnew, board, p, tolerance_deg) for p in pairs]

    if any(o.locked for o in outcomes):
        shutil.copy(board_path, board_path + ".bak")
        board.Save(board_path)

    violations = [{"p_net": o.p_net, "n_net": o.n_net, "detail": o.detail}
                 for o in outcomes if not o.locked]
    locked_count = sum(1 for o in outcomes if o.locked)
    finding = result(kontrol, len(outcomes), violations,
                     f"{locked_count}/{len(outcomes)} pairs locked as tracks.")
    return finding, outcomes


def _self_test() -> int:
    """Pure-geometry checks - no pcbnew needed for these three functions."""
    p_start, n_start, p_end, n_end = closest_pairing(
        (0, 0), (10, 0), (10, 1), (0, 1))
    assert (p_start, n_start) == ((0, 0), (0, 1)), \
        f"closest_pairing matched the wrong ends: {p_start}, {n_start}"

    parallel = parallelism_angle_deg((0, 0), (10, 0), (0, 1), (10, 1))
    assert parallel < 1.0, f"parallel vectors measured {parallel} deg apart"

    twisted = parallelism_angle_deg((0, 0), (10, 0), (0, 1), (0, 11))
    assert abs(twisted - 90.0) < 1.0, f"perpendicular vectors measured {twisted} deg"

    degenerate = parallelism_angle_deg((5, 5), (5, 5), (0, 1), (10, 1))
    assert degenerate == 180.0, "a zero-length vector must report 180, never 0"

    diag("SELF-TEST PASS: closest-end pairing, parallel/twisted/degenerate angles verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?")
    ap.add_argument("--pairs", help="JSON file: [{p_net, n_net, width_mm, layer}, ...]")
    ap.add_argument("--json")
    ap.add_argument("--tolerance-deg", type=float, default=DEFAULT_TOLERANCE_DEG)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.board or not a.pairs:
        ap.error("board and --pairs are required unless --self-test is given")

    raw_pairs = json.loads(Path(a.pairs).read_text(encoding="utf-8"))
    pairs = [DiffPairRequest(
        p_net=p["p_net"], n_net=p["n_net"],
        width_mm=p.get("width_mm", DEFAULT_WIDTH_MM),
        layer=p.get("layer", DEFAULT_LAYER),
    ) for p in raw_pairs]

    finding, outcomes = lock_diff_pairs(a.board, pairs, a.tolerance_deg)
    report = board_report(TOOL_NAME, a.board, [finding], params={
        "pairs_requested": len(pairs), "tolerance_deg": a.tolerance_deg,
        "outcomes": [o.__dict__ for o in outcomes],
    })
    write_report(report, a.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
