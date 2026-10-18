#!/usr/bin/env python3
"""openEMS/CSXCAD bridge for MIPI-class differential-pair S-parameter checks.

    kicad .kicad_pcb  ->  differential-pair geometry (tracks/vias, real
    coordinates)  ->  .s4p/.s2p Touchstone evaluation (single-ended Sii,
    via skrf if the result already exists)

WHY THIS EXISTS: ``scripts/sim/ngspice.py`` does circuit-level (DC/AC/
transient) simulation but cannot see transmission-line/EM behaviour
(impedance discontinuities, return loss). ``common/stackup.py``'s IPC-2141
closed-form formulas are a first estimate; a full 3D field solve is the
stronger check when they disagree.

WHAT THIS PORT DELIBERATELY DOES NOT INCLUDE
----------------------------------------------
The source project's FDTD engine itself (setting up openEMS/CSXCAD mesh,
ports and running the solve) was written but per its own header **never run
against real openEMS on any machine** - openEMS/CSXCAD are not simple `pip
install` packages (they need a compiled FDTD backend), and the author's own
validation notes say the run chain "must not be used for a production
decision without independent verification against a known-impedance
reference." That is the same "never actually run" category
docs/CONSOLIDATION-NOTES.md already rejects other tools for (cad_api bridge,
JLCPCB DFM stub, the kipy/IPC block) - so the FDTD setup/run function and its
CSXCAD-polygon geometry helper are NOT ported here. What IS ported below are
the parts that were independently verified without needing openEMS itself:
geometry extraction from a real board, differential-pair auto-matching, the
stackup Z-lookup, and Touchstone-file evaluation (which only needs an
existing .sNp file, produced by whatever means).

If a real FDTD run chain is wanted later, port `fdtd_kur_ve_calistir`/
`csxcad_kutu_olustur` from pcb-tool-v2 THEN, on a machine that can actually
install and run openEMS - not before.

OPTIONAL DEPENDENCY TIER
-------------------------
``openems_installed()`` probes for ``CSXCAD``/``openEMS`` without importing
them (neither is used by anything in this file - kept only so a future FDTD
port has a working probe to build on). ``evaluate_touchstone()`` needs
``skrf``, imported inside the function; if absent it reports NO_COVERAGE
with an actionable message, never a crash.

Usage:
    openems.py --pair-geometry BOARD.kicad_pcb --net-pos D0_P --net-neg D0_N
               [--json out.json]
    openems.py --evaluate-touchstone result.s4p --target-ohm 100
               [--min-return-loss-db -10] [--json out.json]
    openems.py --self-test

Exit codes: 0 = PASS/no FAIL, 1 = FAIL or NO_COVERAGE-as-exit-1 per sibling
board tools, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import board_exit_code, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.stackup import LAYER_THICKNESS_ASSUMPTION_MM  # noqa: E402
from common.units import mm  # noqa: E402

SCHEMA_VERSION = "openems-bridge-report/v1"
TOOL_NAME = "openems"


def openems_installed() -> bool:
    """Is the (currently unused-by-this-file) openEMS Python API importable?

    Kept as a working probe for a future FDTD port; nothing here calls it to
    gate a code path, since nothing here needs CSXCAD/openEMS.
    """
    import importlib.util
    try:
        return (importlib.util.find_spec("CSXCAD") is not None
                and importlib.util.find_spec("openEMS") is not None)
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Differential-pair geometry extraction (pcbnew-bridged)
# --------------------------------------------------------------------------- #
@dataclass
class DiffPairGeometry:
    net_name_pos: str
    net_name_neg: str
    track_segments_pos: List[dict] = field(default_factory=list)
    track_segments_neg: List[dict] = field(default_factory=list)
    vias_pos: List[dict] = field(default_factory=list)
    vias_neg: List[dict] = field(default_factory=list)


def _net_geometry(board, net_name: str) -> Tuple[List[dict], List[dict]]:
    """Tracks (PCB_TRACK, straight segments only - see module boundary
    below) and vias for one net, in mm."""
    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetNetname() != net_name:
            continue
        if t.GetClass() == "PCB_TRACK":
            tracks.append({
                "start_mm": xy_of(t.GetStart()), "end_mm": xy_of(t.GetEnd()),
                "width_mm": mm(t.GetWidth()), "layer": board.GetLayerName(t.GetLayer()),
            })
        elif t.GetClass() == "PCB_VIA":
            vias.append({"pos_mm": xy_of(t.GetPosition()), "drill_mm": mm(t.GetDrillValue())})
    return tracks, vias


def xy_of(point) -> Tuple[float, float]:
    return (mm(point.x), mm(point.y))


def extract_pair_geometry(board_path: str, net_name_pos: str, net_name_neg: str
                          ) -> Tuple["object", Optional["object"]]:
    """Loads ``board_path``, collects both nets' track/via geometry.

    Returns ``(Finding, DiffPairGeometry | None)``. NO_COVERAGE (not a
    silent pass) when pcbnew is unavailable or neither net has any copper.

    BOUNDARY: PCB_ARC segments are not collected (straight PCB_TRACK only) -
    differential-pair escape/critical regions are almost always straight
    segments in practice; a geometry that relies on arcs there needs a
    richer extractor.
    """
    kontrol = "openems_pair_geometry"
    try:
        import pcbnew
    except ImportError as exc:
        return result(kontrol, 0, [],
                     f"pcbnew module not importable ({exc}) - must run inside "
                     "KiCad's bundled Python."), None

    board = pcbnew.LoadBoard(board_path)
    tracks_pos, vias_pos = _net_geometry(board, net_name_pos)
    tracks_neg, vias_neg = _net_geometry(board, net_name_neg)

    if not tracks_pos and not tracks_neg:
        return result(kontrol, 0, [],
                     f"no tracks found on '{net_name_pos}'/'{net_name_neg}' - "
                     "geometry could not be extracted."), None

    geometry = DiffPairGeometry(net_name_pos, net_name_neg,
                                tracks_pos, tracks_neg, vias_pos, vias_neg)
    return result(kontrol, 1, [],
                 f"{net_name_pos}: {len(tracks_pos)} tracks/{len(vias_pos)} vias; "
                 f"{net_name_neg}: {len(tracks_neg)} tracks/{len(vias_neg)} vias."), geometry


# --------------------------------------------------------------------------- #
# Differential-pair auto-matching (suffix-based, not substring search)
# --------------------------------------------------------------------------- #
def match_differential_pairs(board, suffix_pos: str = "_P",
                             suffix_neg: str = "_N") -> List[Tuple[str, str]]:
    """Matches net names by SUFFIX (not substring search).

    "MIPI_D0_P"/"MIPI_D0_N" matches; unrelated nets like "LED0" or
    "GPIO_D0_EN" are excluded because their suffix does not match. Walks a
    sorted net-name list for deterministic, testable ordering.
    """
    all_names = sorted(n for n in board.GetNetsByName().keys() if n)
    name_set = set(all_names)
    pairs: List[Tuple[str, str]] = []
    for name in all_names:
        if not name.endswith(suffix_pos):
            continue
        stem = name[: -len(suffix_pos)]
        match = stem + suffix_neg
        if match in name_set:
            pairs.append((name, match))
    return pairs


# --------------------------------------------------------------------------- #
# Stackup Z-lookup - single source of truth is common/stackup.py's output
# --------------------------------------------------------------------------- #
def layer_z_position(stackup_result: Dict[str, str], layer_name: str,
                     layer_thickness_mm: Optional[float] = None
                     ) -> Optional[Tuple[float, float]]:
    """``(z_top_mm, z_bottom_mm)`` for ``layer_name`` within a
    ``common.stackup`` plan result.

    ``plan_stackup()``/``stackup_as_list()`` do not return a separate
    dataclass - they return a plain ``{"L1": "...", "L2": "...", ...}``
    dict whose keys are ALREADY in physical order (L1 = outer top layer);
    Python dicts preserve insertion order, so that ordering is a reliable Z
    source without inventing a second one here.

    Uses ``common.stackup.LAYER_THICKNESS_ASSUMPTION_MM`` as the default per
    layer - the SAME constant ``scripts/board/via_stub.py``'s stub-length
    model reads from the board's own real stackup instead; here (no real
    board stackup input) the assumption stands in unless overridden.

    Returns ``None`` (never a made-up Z) if ``layer_name`` is not in the
    stackup.
    """
    thickness = (layer_thickness_mm if layer_thickness_mm is not None
                else LAYER_THICKNESS_ASSUMPTION_MM)
    layer_names = list(stackup_result.keys())
    if layer_name not in layer_names:
        return None
    idx = layer_names.index(layer_name)
    return idx * thickness, (idx + 1) * thickness


# --------------------------------------------------------------------------- #
# Touchstone (.sNp) evaluation - single-ended Sii only, see boundary note
# --------------------------------------------------------------------------- #
def evaluate_touchstone(touchstone_path: str, target_impedance_ohm: float,
                        min_return_loss_db: float = -10.0):
    """Reads a Touchstone (``.s2p``/``.s4p``) file with ``skrf`` and checks
    each port's SINGLE-ENDED return loss (``Sii``).

    Missing file is a FAIL (the simulation ran but produced no result), not
    NO_COVERAGE - NO_COVERAGE is reserved for ``skrf`` itself being absent.

    BOUNDARY (deliberate): this does NOT compute the DIFFERENTIAL
    (``Sdd11``/``Sdd21``, mixed-mode) result - ``skrf.Network.se2gmm()``'s
    port-pairing convention (which physical port maps to which mode index)
    could not be confirmed without ``skrf`` installed in the source
    environment, and a wrong guess reported as "Sdd11" would be worse than
    not reporting it (the no-invented-numbers rule this whole bundle
    follows). Only single-ended Sii is reported here.
    """
    kontrol = "openems_touchstone_evaluation"
    path = Path(touchstone_path)
    if not path.exists():
        return result(kontrol, 1, [{
            "reason": "result file not found", "expected_path": str(path),
        }], "the simulation ran but no .sNp was produced - this is FAIL, not NO_COVERAGE.")

    try:
        import skrf as rf
    except ImportError:
        return result(kontrol, 0, [], "skrf library not installed - evaluation did not run.")

    network = rf.Network(str(path))
    scanned = 0
    violations: List[dict] = []
    for port_no in range(network.nports):
        scanned += 1
        sii_db = network.s_db[:, port_no, port_no]
        max_sii_db = float(sii_db.max())
        if max_sii_db > min_return_loss_db:
            violations.append({
                "port": port_no + 1, "max_return_loss_db": round(max_sii_db, 3),
                "limit_db": min_return_loss_db, "issue": "single_ended_return_loss_exceeded",
            })

    z0 = getattr(network, "z0", None)
    return result(kontrol, scanned, violations,
                 f"{network.nports}-port network, SINGLE-ENDED Sii evaluated (not Sdd - "
                 "see function docstring). target_impedance_ohm="
                 f"{target_impedance_ohm} (port reference impedance should be "
                 f"{z0[0].real if z0 is not None else '?'} in the Touchstone file, not "
                 "independently verified here).")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class _FakeBoard:
    def __init__(self, net_names):
        self._names = net_names

    def GetNetsByName(self):
        return {n: None for n in self._names}


def _self_test() -> int:
    stackup = {"L1": "SIGNAL", "L2": "GND", "L3": "PWR", "L4": "SIGNAL"}
    z = layer_z_position(stackup, "L2")
    assert z == (LAYER_THICKNESS_ASSUMPTION_MM, 2 * LAYER_THICKNESS_ASSUMPTION_MM), \
        f"layer_z_position gave {z}, expected insertion-order Z"
    assert layer_z_position(stackup, "NOPE") is None, \
        "unknown layer must return None, never a made-up Z"
    diag("SELF-TEST PASS: layer Z lookup (insertion-order + unknown-layer guard)")

    board = _FakeBoard(["MIPI_D0_P", "MIPI_D0_N", "LED0", "GPIO_D0_EN", ""])
    pairs = match_differential_pairs(board)
    assert pairs == [("MIPI_D0_P", "MIPI_D0_N")], \
        f"suffix matching gave {pairs} - unrelated nets must not match"
    diag("SELF-TEST PASS: differential-pair suffix matching rejects unrelated nets")

    diag(f"SELF-TEST PASS: openems_installed() -> {openems_installed()} "
        "(informational only - nothing in this file requires it)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pair-geometry", metavar="BOARD",
                    help="extract differential-pair geometry from this board")
    ap.add_argument("--net-pos")
    ap.add_argument("--net-neg")
    ap.add_argument("--evaluate-touchstone", metavar="FILE",
                    help="evaluate an existing .s2p/.s4p Touchstone file")
    ap.add_argument("--target-ohm", type=float, default=100.0)
    ap.add_argument("--min-return-loss-db", type=float, default=-10.0)
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()

    if a.pair_geometry:
        if not a.net_pos or not a.net_neg:
            ap.error("--pair-geometry requires --net-pos and --net-neg")
        finding, geometry = extract_pair_geometry(a.pair_geometry, a.net_pos, a.net_neg)
        report = board_report(TOOL_NAME, a.pair_geometry, [finding], params={
            "net_pos": a.net_pos, "net_neg": a.net_neg,
        }, schema_version=SCHEMA_VERSION)
        write_report(report, a.json)
        emit(report)
        return board_exit_code(report)

    if a.evaluate_touchstone:
        finding = evaluate_touchstone(a.evaluate_touchstone, a.target_ohm,
                                      a.min_return_loss_db)
        report = board_report(TOOL_NAME, a.evaluate_touchstone, [finding], params={
            "target_ohm": a.target_ohm, "min_return_loss_db": a.min_return_loss_db,
        }, schema_version=SCHEMA_VERSION)
        write_report(report, a.json)
        emit(report)
        return board_exit_code(report)

    ap.error("one of --pair-geometry, --evaluate-touchstone or --self-test is required")
    return 2


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
