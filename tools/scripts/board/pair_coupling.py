#!/usr/bin/env python3
"""Differential-pair coupling screen: how much of a pair runs together.

VENDORED FROM an earlier standalone ``coupling.py`` script.

WHAT THE ORIGINAL DID AND WHAT IT COST
--------------------------------------
54 lines of top-level script that loaded a board, discovered ``*_P``/``*_N``
pairs, measured the fraction of each member that runs within a spacing
threshold of its partner, printed a table and stopped. Everything that made
it unusable as gate evidence is fixed here:

  * ``pairs = [p for p in pairs if p.startswith("CAM") or p.startswith("CSI")]``
    - one project's net prefixes, welded into the tool. Now ``--net-prefix``,
    and with no prefix every discovered pair is measured.
  * ``SPACING_MAX`` and ``TH`` were module constants with the derivation in a
    comment. Now CLI parameters, echoed into ``params`` so the report says
    which thresholds produced the verdict.
  * ``print("COUPLED %d/%d")`` on a board with NO pairs printed ``COUPLED
    0/0`` and exited 0 - a clean-looking pass for a board nobody measured.
    Zero eligible pairs is now ``NO_COVERAGE``.
  * ``type(t).__name__ != "PCB_TRACK"`` skipped ARCS, so a pair routed with
    arc corners lost that copper from both the coupled and the total length.
    ``GetClass()`` is used and ``PCB_ARC`` is included.
  * No schema, no report file, no exit-code contract.
  * The report carried no ``design`` block and no ``metrics`` map even though
    its own schema listed both as REQUIRED, so it could not be tied to the
    board it measured (SCREEN_ONLY forever) and no assertion could point at
    one of its checks. Nothing caught this because no consumer validated the
    output against the schema; ``tests/test_vendored_board_tools.py`` now
    does.

WHAT THIS IS NOT
----------------
A sampled centre-to-centre distance screen. It does not compute impedance,
it does not model FEXT/NEXT, and running "coupled" is necessary but not
sufficient for a controlled-impedance pair. ``model_class`` and
``signoff: false`` are in every report for that reason; use
``scripts/crosstalk_thinning.py`` for the coupling-coefficient question and a
2D field solver for impedance.

CLI:
    pair_coupling.py BOARD [--net-prefix PREFIX ...] [--pair P:N ...]
                     [--spacing-max-mm MM] [--coupled-threshold F]
                     [--step-mm MM] [--json REPORT] [--self-test]

Exit codes: 0 every pair coupled, 1 an uncoupled pair or NO_COVERAGE, 2 error.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL, NO_COVERAGE, by_check_map, result, summarise,
)
from common.hashing import sha256_file  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_FAIL, EXIT_OK, diag, emit, run_cli, write_report,
)

SCHEMA_VERSION = "pair-coupling-report/v1"
MM = 1e6

MODEL_CLASS = "sampled_centreline_proximity_screen"


class CouplingError(RuntimeError):
    """The screen could not be run."""


# --------------------------------------------------------------------------- #
# Pure geometry - no pcbnew
# --------------------------------------------------------------------------- #
def point_segment_distance(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def coupled_length(segments_a, segments_b, spacing_max: float,
                   step: float = 0.05) -> tuple[float, float]:
    """(coupled mm, total mm) of A that runs within ``spacing_max`` of B.

    LAYER-AWARE: a segment on B.Cu is not "next to" a segment on F.Cu just
    because it is close in X/Y. Segments carry their layer id and only
    same-layer partners count, exactly as the original did.
    """
    total = coupled = 0.0
    for layer, x1, y1, x2, y2 in segments_a:
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0.0:
            continue
        total += length
        count = max(2, int(math.ceil(length / step)))
        hits = 0
        partners = [s for s in segments_b if s[0] == layer]
        for index in range(count):
            f = (index + 0.5) / count
            px, py = x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
            for _layer, ax, ay, bx, by in partners:
                if point_segment_distance(px, py, ax, ay, bx, by) <= spacing_max:
                    hits += 1
                    break
        coupled += length * hits / count
    return coupled, total


def discover_pairs(net_names, prefixes=()) -> list[tuple[str, str]]:
    """``(P net, N net)`` for every ``*_P``/``*_N`` pair present on the board."""
    names = set(net_names)
    bases = sorted({
        name[:-2] for name in names
        if name.endswith("_P") and name[:-2] + "_N" in names and name[:-2]
    })
    if prefixes:
        bases = [b for b in bases
                 if any(b.lstrip("/").startswith(p) or b.startswith(p)
                        for p in prefixes)]
    return [(base + "_P", base + "_N") for base in bases]


# --------------------------------------------------------------------------- #
def read_segments(board) -> dict:
    """``{net: [(layer, x1, y1, x2, y2), ...]}`` in millimetres.

    Arcs are included: the original filtered them out and silently lost the
    copper in every arc corner.
    """
    segments = defaultdict(list)
    for track in board.GetTracks():
        if track.GetClass() not in ("PCB_TRACK", "PCB_ARC"):
            continue
        start, end = track.GetStart(), track.GetEnd()
        segments[track.GetNetname()].append(
            (track.GetLayer(), start.x / MM, start.y / MM,
             end.x / MM, end.y / MM))
    return dict(segments)


def design_block(board_path: Path) -> dict:
    """``{path, sha256}`` - the board this report measured, with its digest.

    ``pair_coupling_report.schema.json`` has listed ``design`` as REQUIRED
    since it was written; the producer never emitted it, and nothing caught
    that because no consumer validated the report against its own schema.
    Without the digest the report cannot be tied to the board it measured, so
    every assertion resting on it is SCREEN_ONLY forever - the same hole
    ``measure_board.py`` had, for the same reason.

    ``sha256`` is null when the path cannot be read: LOUD, not absent. A
    reader must be able to see that the binding is missing instead of
    inferring it from a field that is not there. Same behaviour as
    ``common.findings.board_report``.
    """
    path = Path(board_path)
    return {"path": str(path),
            "sha256": sha256_file(path) if path.is_file() else None}


def load_board(path: Path):
    try:
        import pcbnew  # noqa: PLC0415
    except ImportError as exc:                    # pragma: no cover
        raise CouplingError(
            "pcbnew is required for pair_coupling.py") from exc
    return pcbnew.LoadBoard(str(path))


def screen(board_path: Path, *, prefixes: list[str], explicit_pairs: list[str],
           spacing_max_mm: float, threshold: float, step_mm: float) -> dict:
    board = load_board(board_path)
    segments = read_segments(board)

    if explicit_pairs:
        pairs = []
        for spec in explicit_pairs:
            if ":" not in spec:
                raise CouplingError(f"--pair expects P:N, got {spec!r}")
            p_net, n_net = spec.split(":", 1)
            pairs.append((p_net, n_net))
    else:
        pairs = discover_pairs(segments.keys(), tuple(prefixes))

    rows = []
    violations = []
    measured = 0
    for p_net, n_net in pairs:
        segs_p = segments.get(p_net, [])
        segs_n = segments.get(n_net, [])
        coupled_p, total_p = coupled_length(segs_p, segs_n, spacing_max_mm, step_mm)
        coupled_n, total_n = coupled_length(segs_n, segs_p, spacing_max_mm, step_mm)
        # The WEAKER direction decides: a long P hugging a short N is not a
        # coupled pair, and averaging would hide exactly that case.
        fraction_p = coupled_p / total_p if total_p else 0.0
        fraction_n = coupled_n / total_n if total_n else 0.0
        fraction = min(fraction_p, fraction_n)
        has_copper = total_p > 0.0 and total_n > 0.0
        if has_copper:
            measured += 1
        row = {
            "pair": f"{p_net}:{n_net}",
            "p_total_mm": round(total_p, 4),
            "n_total_mm": round(total_n, 4),
            "p_coupled_mm": round(coupled_p, 4),
            "n_coupled_mm": round(coupled_n, 4),
            "coupled_fraction": round(fraction, 6),
            "coupled_percent": round(100.0 * fraction, 3),
            "measurable": has_copper,
            "coupled": bool(has_copper and fraction >= threshold),
        }
        rows.append(row)
        if not has_copper:
            continue
        if fraction < threshold:
            violations.append(
                f"{row['pair']}: {row['coupled_percent']}% coupled "
                f"< {100.0 * threshold:.1f}% required")

    findings = [
        result("pair_coupling", measured, violations,
               f"fraction of each member running within {spacing_max_mm} mm "
               f"of its partner ON THE SAME LAYER, sampled at {step_mm} mm"),
        result("pairs_discovered", len(pairs),
               [row["pair"] for row in rows if not row["measurable"]],
               "pairs whose members both carry copper"),
    ]
    summary = summarise(findings)
    status = (NO_COVERAGE if measured == 0 else
              FAIL if summary[FAIL] else "PASS")
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "pair_coupling.py",
        "board": str(board_path),
        "design": design_block(board_path),
        "model_class": MODEL_CLASS,
        "signoff": False,
        "params": {
            "net_prefixes": prefixes,
            "explicit_pairs": explicit_pairs,
            "spacing_max_mm": spacing_max_mm,
            "coupled_threshold": threshold,
            "step_mm": step_mm,
        },
        "pairs": rows,
        "coverage": {
            "eligible": len(pairs),
            "scanned": measured,
            "excluded": len(pairs) - measured,
            "violations": len(violations),
            "reason": ("differential pairs discovered on the board; excluded "
                       "pairs have no copper on one or both members"),
        },
        # ``checks`` is a LIST and the assertion selector grammar addresses
        # ``a.b`` paths and numeric indices only, so a rule could name this
        # tool and still have no way to point at the check it cares about.
        # ``metrics`` is the name-keyed door the schema requires: check name ->
        # VIOLATION COUNT. It cannot express coverage - a count of 0 with
        # ``scanned == 0`` is not a pass - so ``by_check`` carries the status
        # and scanned count next to it, from the same shared helper the board
        # checkers use.
        "metrics": {f["check"]: len(f["violations"]) for f in findings},
        "by_check": by_check_map(findings),
        "checks": findings,
        "summary": summary,
        "status": status,
        "limits": ("centreline proximity only: no impedance, no FEXT/NEXT "
                   "model, no stackup. signoff=false."),
    }


# --------------------------------------------------------------------------- #
def self_test() -> list[str]:
    problems: list[str] = []

    parallel_a = [(0, 0.0, 0.0, 10.0, 0.0)]
    parallel_b = [(0, 0.0, 0.35, 10.0, 0.35)]
    coupled, total = coupled_length(parallel_a, parallel_b, 0.60)
    if abs(total - 10.0) > 1e-6 or coupled / total < 0.99:
        problems.append(f"parallel pair not coupled: {coupled}/{total}")

    far = coupled_length(parallel_a, [(0, 0.0, 2.0, 10.0, 2.0)], 0.60)[0]
    if far > 1e-9:
        problems.append("a pair 2 mm apart was counted as coupled")

    # The layer rule: same X/Y, different layer, must NOT couple.
    other_layer = coupled_length(parallel_a, [(31, 0.0, 0.35, 10.0, 0.35)], 0.60)[0]
    if other_layer > 1e-9:
        problems.append("segments on DIFFERENT layers were counted as coupled")

    # FAULT INJECTION: an empty partner net cannot be coupled to anything.
    if coupled_length(parallel_a, [], 100.0)[0] > 1e-9:
        problems.append("coupling reported against an EMPTY net")

    pairs = discover_pairs(["/CAM0_P", "/CAM0_N", "/CSI_P", "/CSI_N",
                            "GND", "/LONE_P"])
    if pairs != [("/CAM0_P", "/CAM0_N"), ("/CSI_P", "/CSI_N")]:
        problems.append(f"pair discovery wrong: {pairs}")
    if discover_pairs(["/CAM0_P", "/CAM0_N", "/CSI_P", "/CSI_N"],
                      ("CAM",)) != [("/CAM0_P", "/CAM0_N")]:
        problems.append("net-prefix filter did not apply")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", nargs="?", help="path to a .kicad_pcb")
    parser.add_argument("--net-prefix", action="append", default=[],
                        dest="prefixes",
                        help="only pairs whose base name starts with this; "
                             "repeatable. Default: every discovered pair")
    parser.add_argument("--pair", action="append", default=[],
                        dest="explicit_pairs",
                        help="explicit P_NET:N_NET, bypassing discovery")
    parser.add_argument("--spacing-max-mm", type=float, default=0.60,
                        help="centre-to-centre spacing that counts as coupled")
    parser.add_argument("--coupled-threshold", type=float, default=0.50,
                        help="fraction of the weaker member that must be "
                             "coupled (0..1)")
    parser.add_argument("--step-mm", type=float, default=0.05)
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_test and not args.board:
        parser.error("BOARD is required (a .kicad_pcb path)")
    if not 0.0 < args.coupled_threshold <= 1.0:
        parser.error("--coupled-threshold must be in (0, 1]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        emit({"schema_version": SCHEMA_VERSION,
              "self_test": "PASS" if not problems else "FAIL",
              "problems": problems})
        return EXIT_OK if not problems else EXIT_FAIL

    board_path = Path(args.board).expanduser().resolve()
    if not board_path.is_file():
        raise CouplingError(f"board not found: {board_path}")
    report = screen(board_path, prefixes=args.prefixes,
                    explicit_pairs=args.explicit_pairs,
                    spacing_max_mm=args.spacing_max_mm,
                    threshold=args.coupled_threshold, step_mm=args.step_mm)
    write_report(report, args.json_path)
    emit(report)
    diag(f"pair_coupling {report['status']}: "
         f"{report['coverage']['scanned']}/{report['coverage']['eligible']} "
         "pair(s) measurable")
    return EXIT_OK if report["status"] == "PASS" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
