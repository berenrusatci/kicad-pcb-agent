#!/usr/bin/env python3
"""Board measurements read from the board file - never from a document.

VENDORED FROM ``ProjectE/aiplanlama/usb-hs-breakout/evidence/measure.py``.

WHICH COPY, AND WHY
-------------------
Three files named ``measure.py`` were in play. Two of them
(``sync_out/measure.py`` and ``PRJ-AM5706-.../tools/sync/measure.py``) are
byte-identical eight-line scripts that FILL ZONES and re-save a board - they
measure nothing at all and share only the filename. The third, the 222-line
``usb-hs-breakout/evidence`` copy, is the actual measurement tool, and it is
the one vendored here. Nothing was merged from the other two: a zone filler
is a mutating tool and belongs nowhere near a read-only evidence path.

WHAT CHANGED IN THE PORT
------------------------
  * The original hardcoded ONE board's net names, refdes and pad numbers
    (``/USB_C_D_P``, ``C1``, ``U1.5``, ...). Every population is now a CLI
    argument, so the tool is evidence for any board instead of exactly one.
  * The original printed a bag of numbers with no verdict. Numbers with no
    pass/fail rule cannot gate anything, and an empty population printed
    zeros that read like clean results. Every measurement now becomes a
    ``common.findings`` record, so ``scanned == 0`` is ``NO_COVERAGE`` and
    never a silent PASS.
  * Dead code removed rather than left in place: the original computed
    ``teardrop_sayisi`` with ``... and False``, then popped the key.

WHAT WAS DELIBERATELY KEPT
--------------------------
  * ``GetLength()`` for track length, which returns ARC LENGTH, not the
    chord. The original's comment is right and the distinction is the whole
    point of a skew number.
  * ``PCB_ARC`` included in the track filter. Filtering on ``"PCB_TRACK"``
    alone silently drops hand-drawn arc corners.
  * The sampled coupling definition, verbatim in ``coupling_note``: coupling
    is a SAMPLED CHORD measurement, not a field solution, and the report says
    so instead of implying an electromagnetic result.

CLI:
    measure_board.py BOARD [--net NET ...] [--pair P:N ...]
                     [--coupling-tolerance-mm MM ...] [--step-mm MM]
                     [--decoupling REF.PAD:REF.PAD ...] [--decoupling-max-mm MM]
                     [--gnd-via-of REF.PAD ...] [--gnd-via-radius-mm MM]
                     [--max-skew-mm MM] [--corner-tolerance-deg DEG]
                     [--json REPORT] [--self-test]

Exit codes: 0 every measurement ran and passed, 1 a measurement FAILed
or was never taken (NO_COVERAGE), 2 could not run. An unmeasured net is
not a clean net, and it must not leave through the exit code either.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL, PASS, by_check_map, result, rollup_status, summarise,
)
from common.hashing import sha256_file  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_FAIL, EXIT_OK, diag, emit, run_cli, write_report,
)
from common.tuning import decoupling_limits  # noqa: E402

SCHEMA_VERSION = "measure-board-report/v1"
MM = 1e6

COUPLING_NOTE = (
    "coupling is measured by splitting the first net's copper into --step-mm "
    "samples and asking whether the sample midpoint lies within the tolerance "
    "of any segment of the second net. It is a SAMPLED CHORD DISTANCE, not a "
    "field solution, and it does not prove differential impedance."
)


class MeasureError(RuntimeError):
    """The measurement could not be taken."""


# --------------------------------------------------------------------------- #
# Pure geometry - importable and testable without pcbnew
# --------------------------------------------------------------------------- #
def seg_dist(point, a, b) -> float:
    """Distance from ``point`` to segment ``a``-``b``."""
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return math.dist(point, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def coupled_fraction(segments_a, segments_b, tolerance: float,
                     step: float = 0.05) -> tuple[float, float, float]:
    """(coupled mm, total mm, percent) of A that runs within ``tolerance`` of B."""
    total = coupled = 0.0
    for a, b in segments_a:
        length = math.dist(a, b)
        if length < 1e-9:
            continue
        count = max(1, int(math.ceil(length / step)))
        piece = length / count
        for index in range(count):
            f = (index + 0.5) / count
            mid = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            total += piece
            if segments_b and min(seg_dist(mid, p, q)
                                  for p, q in segments_b) <= tolerance:
                coupled += piece
    return coupled, total, (100.0 * coupled / total if total else 0.0)


def corner_angles(segments_by_net: dict, eps: float = 1e-6) -> list[dict]:
    """Turn angle at every point where two same-layer segments of a net meet."""
    found: list[dict] = []
    for net, segments in segments_by_net.items():
        for i, (a1, b1, layer1) in enumerate(segments):
            for j, (a2, b2, layer2) in enumerate(segments):
                if j <= i or layer1 != layer2:
                    continue
                shared = None
                for p in (a1, b1):
                    for q in (a2, b2):
                        if math.dist(p, q) < eps:
                            shared = p
                if shared is None:
                    continue
                o1 = b1 if math.dist(a1, shared) < eps else a1
                o2 = b2 if math.dist(a2, shared) < eps else a2
                v1 = (o1[0] - shared[0], o1[1] - shared[1])
                v2 = (o2[0] - shared[0], o2[1] - shared[1])
                n1, n2 = math.hypot(*v1), math.hypot(*v2)
                if n1 < eps or n2 < eps:
                    continue
                cosine = max(-1.0, min(1.0,
                                       (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
                interior = math.degrees(math.acos(cosine))
                found.append({
                    "net": net,
                    "layer": layer1,
                    "at": [round(shared[0], 3), round(shared[1], 3)],
                    "interior_deg": round(interior, 1),
                    "turn_deg": round(180.0 - interior, 1),
                })
    return found


# --------------------------------------------------------------------------- #
# pcbnew readers
# --------------------------------------------------------------------------- #
def _xy(vector) -> tuple[float, float]:
    return (vector.x / MM, vector.y / MM)


def load_board(path: Path):
    try:
        import pcbnew  # noqa: PLC0415
    except ImportError as exc:                    # pragma: no cover
        raise MeasureError(
            "pcbnew is required for measure_board.py; run it with the "
            "python that KiCad's pcbnew module is installed for") from exc
    return pcbnew.LoadBoard(str(path))


def tracks_of(board, net: str) -> list:
    """Straight tracks AND arcs. Arcs are PCB_TRACK subclasses whose
    GetClass() is 'PCB_ARC'; filtering on 'PCB_TRACK' loses them."""
    return [t for t in board.GetTracks()
            if t.GetNetname() == net and t.GetClass() in ("PCB_TRACK", "PCB_ARC")]


def net_length_mm(board, net: str) -> float:
    """Copper length of a net. ``GetLength()`` returns arc length for arcs."""
    return sum(t.GetLength() / MM for t in tracks_of(board, net))


def segments_of(board, nets) -> list:
    return [(_xy(t.GetStart()), _xy(t.GetEnd()))
            for net in nets for t in tracks_of(board, net)]


def pad_positions(board) -> dict:
    out = {}
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            out[(footprint.GetReference(), pad.GetNumber())] = \
                _xy(pad.GetPosition())
    return out


def parse_pad(token: str) -> tuple[str, str]:
    if "." not in token:
        raise MeasureError(f"expected REF.PAD, got {token!r}")
    ref, pad = token.rsplit(".", 1)
    return ref, pad


# --------------------------------------------------------------------------- #
def measure(board_path: Path, *, nets: list[str], pairs: list[str],
            tolerances: list[float], step_mm: float,
            decoupling: list[str], decoupling_max_mm: float,
            gnd_via_of: list[str], gnd_via_radius_mm: float,
            max_skew_mm: float, corner_tolerance_deg: float) -> dict:
    board = load_board(board_path)
    pads = pad_positions(board)
    findings = []
    measurements: dict = {"board": str(board_path)}

    measurements["copper_layers"] = board.GetCopperLayerCount()
    bbox = board.GetBoardEdgesBoundingBox()
    measurements["board_size_mm"] = [round(bbox.GetWidth() / MM, 3),
                                     round(bbox.GetHeight() / MM, 3)]

    # ------------------------------------------------------------- lengths
    lengths = {net: round(net_length_mm(board, net), 4) for net in nets}
    measurements["net_length_mm"] = lengths
    measurements["arc_count"] = {
        net: sum(1 for t in tracks_of(board, net)
                 if t.GetClass() == "PCB_ARC")
        for net in nets
    }
    routed = [net for net, length in lengths.items() if length > 0]
    findings.append(result(
        "nets_routed", len(nets),
        [f"{net}: no copper" for net in nets if lengths[net] <= 0],
        f"{len(routed)}/{len(nets)} requested net(s) carry copper"))

    # --------------------------------------------------------------- skew
    skews = {}
    skew_violations = []
    for spec in pairs:
        if ":" not in spec:
            raise MeasureError(f"--pair expects P:N, got {spec!r}")
        p_net, n_net = spec.split(":", 1)
        p_len = net_length_mm(board, p_net)
        n_len = net_length_mm(board, n_net)
        delta = round(abs(p_len - n_len), 4)
        skews[spec] = {"p_mm": round(p_len, 4), "n_mm": round(n_len, 4),
                       "skew_mm": delta}
        if p_len <= 0 or n_len <= 0:
            continue
        if delta > max_skew_mm:
            skew_violations.append(
                f"{spec}: skew {delta} mm > {max_skew_mm} mm")
    measurements["pair_skew_mm"] = skews
    measurable_pairs = sum(1 for v in skews.values()
                           if v["p_mm"] > 0 and v["n_mm"] > 0)
    findings.append(result(
        "pair_skew", measurable_pairs, skew_violations,
        f"intra-pair length skew against {max_skew_mm} mm"))

    # ----------------------------------------------------------- coupling
    coupling: dict = {}
    coupled_samples = 0
    for spec in pairs:
        p_net, n_net = spec.split(":", 1)
        segs_p = segments_of(board, [p_net])
        segs_n = segments_of(board, [n_net])
        for tolerance in tolerances:
            coupled, total, percent = coupled_fraction(
                segs_p, segs_n, tolerance, step_mm)
            coupling[f"{spec}@{tolerance}mm"] = {
                "coupled_mm": round(coupled, 4),
                "total_mm": round(total, 4),
                "coupled_percent": round(percent, 3),
            }
            if total > 0:
                coupled_samples += 1
    measurements["coupling"] = coupling
    measurements["coupling_note"] = COUPLING_NOTE
    findings.append(result(
        "pair_coupling_measured", coupled_samples, [],
        "sampled coupling ratios; see coupling_note - this is not a field "
        "solution and no threshold is asserted here"))

    # --------------------------------------------------------- decoupling
    distances: dict = {}
    decoupling_violations = []
    for spec in decoupling:
        if ":" not in spec:
            raise MeasureError(f"--decoupling expects REF.PAD:REF.PAD, got {spec!r}")
        left, right = spec.split(":", 1)
        a = pads.get(parse_pad(left))
        b = pads.get(parse_pad(right))
        if a is None or b is None:
            continue
        distance = round(math.dist(a, b), 4)
        distances[spec] = distance
        if distance > decoupling_max_mm:
            decoupling_violations.append(
                f"{spec}: {distance} mm > {decoupling_max_mm} mm")
    measurements["decoupling_pad_distance_mm"] = distances
    findings.append(result(
        "decoupling_distance", len(distances), decoupling_violations,
        f"pad-centre to pad-centre against {decoupling_max_mm} mm"))

    # ------------------------------------------------------------ gnd via
    via_positions = [(_xy(t.GetPosition()), t.GetNetname())
                     for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    measurements["via_count"] = len(via_positions)
    near: dict = {}
    via_violations = []
    for token in gnd_via_of:
        position = pads.get(parse_pad(token))
        if position is None:
            continue
        found = sorted(round(math.dist(vp, position), 4)
                       for vp, name in via_positions
                       if name == "GND"
                       and math.dist(vp, position) <= gnd_via_radius_mm)
        near[token] = {"count": len(found), "distances_mm": found}
        if not found:
            via_violations.append(
                f"{token}: no GND via within {gnd_via_radius_mm} mm")
    measurements["gnd_vias_near_pad"] = near
    findings.append(result(
        "gnd_via_near_pad", len(near), via_violations,
        f"GND vias within {gnd_via_radius_mm} mm of the named pads"))

    # ------------------------------------------------------------ corners
    by_net = {net: [(_xy(t.GetStart()), _xy(t.GetEnd()), t.GetLayerName())
                    for t in tracks_of(board, net)] for net in nets}
    angles = corner_angles(by_net)
    right_angles = [a for a in angles
                    if abs(a["turn_deg"] - 90.0) <= corner_tolerance_deg]
    measurements["corner_count"] = len(angles)
    measurements["turn_angles_deg"] = sorted({a["turn_deg"] for a in angles})
    measurements["right_angle_corners"] = right_angles
    findings.append(result(
        "no_right_angle_corners", len(angles),
        [f"{a['net']} {a['layer']} at {a['at']}: turn {a['turn_deg']} deg"
         for a in right_angles],
        f"90 +/- {corner_tolerance_deg} deg turns on the measured nets"))

    # ------------------------------------------------------------- census
    references = [f.GetReference() for f in board.GetFootprints()]
    measurements["footprint_refs"] = sorted(references)
    measurements["test_point_count"] = sum(
        1 for r in references if r.upper().startswith("TP"))
    measurements["fiducial_count"] = sum(
        1 for r in references if r.upper().startswith("FID"))
    try:
        measurements["teardrop_zone_count"] = sum(
            1 for zone in board.Zones() if zone.IsTeardropArea())
    except (AttributeError, RuntimeError) as exc:
        measurements["teardrop_zone_count"] = None
        measurements["teardrop_zone_error"] = str(exc)

    summary = summarise(findings)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "measure_board.py",
        "board": str(board_path),
        # The digest is what lets an assertion tie this report to the board it
        # measured. Without it the report is unbindable and every assertion
        # resting on it is SCREEN_ONLY forever - which is what it silently was
        # until now. ``drc_gate.py`` has carried the same block since it was
        # written; this one was simply missing, and nothing noticed because no
        # assertion had ever been pointed at a real measure_board report.
        "design": {
            "path": str(board_path),
            "sha256": sha256_file(board_path),
        },
        "params": {
            "nets": nets, "pairs": pairs, "tolerances_mm": tolerances,
            "step_mm": step_mm, "decoupling": decoupling,
            "decoupling_max_mm": decoupling_max_mm,
            "gnd_via_of": gnd_via_of,
            "gnd_via_radius_mm": gnd_via_radius_mm,
            "max_skew_mm": max_skew_mm,
            "corner_tolerance_deg": corner_tolerance_deg,
        },
        "measurements": measurements,
        # Same name-keyed coverage map board_report() emits, for the same
        # reason: an assertion can reach `measurements.right_angle_corners` but
        # had no way to ask whether the corner check examined any corners at
        # all. This envelope is hand-built rather than routed through
        # board_report(), so it has to opt in explicitly.
        "by_check": by_check_map(findings),
        "checks": findings,
        "summary": summary,
        # The rollup used to demote to NO_COVERAGE only when EVERY check was
        # NO_COVERAGE, so one PASS beside five unmeasured checks reported a
        # top-level PASS. ``rollup_status`` is the shared monotone rule: an
        # unmeasured check can never be outvoted by a measured one.
        "status": rollup_status(findings),
    }


# --------------------------------------------------------------------------- #
def self_test() -> list[str]:
    problems: list[str] = []

    if abs(seg_dist((0.0, 1.0), (-5.0, 0.0), (5.0, 0.0)) - 1.0) > 1e-9:
        problems.append("seg_dist perpendicular case wrong")
    if abs(seg_dist((10.0, 0.0), (-5.0, 0.0), (5.0, 0.0)) - 5.0) > 1e-9:
        problems.append("seg_dist beyond-endpoint case wrong")

    a = [((0.0, 0.0), (10.0, 0.0))]
    b = [((0.0, 0.2), (10.0, 0.2))]
    _c, total, percent = coupled_fraction(a, b, 0.3, 0.05)
    if abs(total - 10.0) > 1e-6 or percent < 99.9:
        problems.append(f"parallel pair not fully coupled: {percent}")
    _c, _t, percent_far = coupled_fraction(a, b, 0.1, 0.05)
    if percent_far > 0.0:
        problems.append("a pair beyond tolerance was counted as coupled")

    # FAULT INJECTION: an empty opposite net must never read as coupled.
    _c, _t, percent_empty = coupled_fraction(a, [], 10.0, 0.05)
    if percent_empty != 0.0:
        problems.append("coupling reported against an EMPTY net")

    corners = corner_angles({"N": [
        ((0.0, 0.0), (5.0, 0.0), "F.Cu"),
        ((5.0, 0.0), (5.0, 5.0), "F.Cu"),
    ]})
    if len(corners) != 1 or abs(corners[0]["turn_deg"] - 90.0) > 0.1:
        problems.append(f"90 degree corner not detected: {corners}")
    if corner_angles({"N": [
        ((0.0, 0.0), (5.0, 0.0), "F.Cu"),
        ((5.0, 0.0), (5.0, 5.0), "B.Cu"),
    ]}):
        problems.append("corner detected between segments on DIFFERENT layers")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", nargs="?", help="path to a .kicad_pcb")
    parser.add_argument("--net", action="append", default=[],
                        help="net to measure; repeatable")
    parser.add_argument("--pair", action="append", default=[],
                        help="differential pair as P_NET:N_NET; repeatable")
    parser.add_argument("--coupling-tolerance-mm", action="append",
                        type=float, default=[], dest="tolerances")
    parser.add_argument("--step-mm", type=float, default=0.05)
    parser.add_argument("--decoupling", action="append", default=[],
                        help="pad pair as REF.PAD:REF.PAD; repeatable")
    # No literal default. This 1.5 was one of THREE copies of the same
    # rule (decoupling_coverage.py had 3.0), so two tools in this bundle
    # could reach opposite verdicts about one capacitor. The project rule
    # now lives in fixtures/references/tuning_defaults.json.
    parser.add_argument("--decoupling-max-mm", type=float, default=None)
    parser.add_argument("--gnd-via-of", action="append", default=[],
                        help="pad as REF.PAD that needs a nearby GND via")
    parser.add_argument("--gnd-via-radius-mm", type=float, default=1.5)
    parser.add_argument("--max-skew-mm", type=float, default=15.0,
                        help="intra-pair length skew budget")
    parser.add_argument("--corner-tolerance-deg", type=float, default=5.0)
    parser.add_argument("--json", dest="json_path", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_test and not args.board:
        parser.error("BOARD is required (a .kicad_pcb path)")
    if not args.tolerances:
        args.tolerances = [0.70]
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
        raise MeasureError(f"board not found: {board_path}")
    report = measure(
        board_path, nets=args.net, pairs=args.pair,
        tolerances=args.tolerances, step_mm=args.step_mm,
        decoupling=args.decoupling,
        decoupling_max_mm=(args.decoupling_max_mm if args.decoupling_max_mm
                           is not None else decoupling_limits()[0]),
        gnd_via_of=args.gnd_via_of,
        gnd_via_radius_mm=args.gnd_via_radius_mm,
        max_skew_mm=args.max_skew_mm,
        corner_tolerance_deg=args.corner_tolerance_deg)
    write_report(report, args.json_path)
    emit(report)
    diag(f"measure_board {report['status']}: {report['summary']}")
    # Derived from the report's own status rather than from the FAIL count.
    # Once the rollup above became monotone, `1 if summary[FAIL] else 0` made
    # the tool contradict itself in a single run: the report said
    # NO_COVERAGE and the exit code said clean. This is a hand-rolled copy of
    # board_exit_code() and never inherited that function's documented,
    # deliberately frozen NO_COVERAGE-exits-0 exemption.
    return EXIT_OK if report["status"] == PASS else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
