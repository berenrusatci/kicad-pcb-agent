#!/usr/bin/env python3
"""DC current-density heat map for one net's copper, from a real board.

    kicad .kicad_pcb  ->  net's track bounding box (see ACCURACY BOUNDARY)
    ->  rasterised resistive mesh (Kirchhoff current conservation)
    ->  per-cell current density (A/m), optionally rendered to PNG

NOT a full EM simulation - this does not replace ``scripts/sim/openems.py``'s
FDTD/S-parameter solve. It is the DC IR-drop current distribution spread over
geometry: a static resistive-mesh solve that flags hot/narrow copper without
needing openEMS installed.

OPTIONAL DEPENDENCY TIER
-------------------------
The mesh solver needs ``numpy`` and ``scipy``; the optional heat-map PNG also
needs ``matplotlib``. All three are imported INSIDE the functions that need
them, never at module level, so:

  * importing this module is stdlib-only,
  * the whole test suite collects and runs without them,
  * when they are missing the tool reports NO_COVERAGE with one clear,
    actionable message - not an ImportError from the middle of a solve.

Install with: ``pip install numpy scipy`` (add ``matplotlib`` for the PNG).
Every other tool in this bundle stays stdlib-only; this mirrors
``scripts/report/cross_section.py``'s optional tier.

ACCURACY BOUNDARY - READ BEFORE USING A NUMBER FROM THIS
----------------------------------------------------------
``current_density_for_net()`` uses the requested net's TRACK BOUNDING BOX
(a rectangle), not the real zone/copper-pour polygon (``ZONE.Outline()``).
For a narrow trace this is a fair approximation; for a wide power-plane pour
with cutouts it is not - use ``solve_polygon()`` directly with the zone's
real outline points for that case. ``solve_polygon()`` and ``solve_strip()``
have no bounding-box limitation; only the pcbnew-bridged
``current_density_for_net()`` does.

Usage:
    current_density.py <board.kicad_pcb> --net NET_NAME --current-a 2.0
                       [--json out.json] [--out heatmap.png]
                       [--copper-oz 1.0] [--cell-mm 0.5]
    current_density.py --self-test

Exit codes: 0 = solved, 1 = NO_COVERAGE (net has no copper, or the optional
tier is missing), 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.boardgeom import point_in_polygon as _point_in_polygon  # noqa: E402
from common.findings import board_exit_code, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.units import mm, xy  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "current_density"

OPTIONAL_PACKAGES = ("numpy", "scipy")

# Copper resistivity at room temperature (ohm*m) - a literature constant,
# not a second source of truth: common/stackup.py's IPC-2221 tables answer a
# different question (trace width/clearance), this answers current
# DISTRIBUTION within a given copper shape.
COPPER_RESISTIVITY_OHM_M = 1.68e-8
OZ_THICKNESS_M = 35e-6  # 1oz copper ~35 micron


class OptionalDependencyMissing(RuntimeError):
    """numpy/scipy are not installed. Actionable by design."""


def optional_tier_status() -> dict:
    """What is installed, without importing anything heavy."""
    import importlib.util
    present = {}
    for name in OPTIONAL_PACKAGES:
        try:
            present[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            present[name] = False
    return {"packages": present, "complete": all(present.values())}


def _require_optional_tier():
    """Import numpy/scipy or raise ONE clear, actionable error."""
    status = optional_tier_status()
    missing = [name for name, ok in status["packages"].items() if not ok]
    if missing:
        raise OptionalDependencyMissing(
            "current_density needs the optional mesh-solver tier, which is "
            f"not installed. Missing: {', '.join(missing)}. Install with: "
            "pip install numpy scipy. Every other tool in this bundle is "
            "stdlib-only and is unaffected.")
    import numpy as np
    from scipy.sparse import lil_matrix
    from scipy.sparse.linalg import spsolve
    return np, lil_matrix, spsolve


@dataclass
class MeshResult:
    n_rows: int
    n_cols: int
    node_voltage_v: "object"          # (n_rows, n_cols) ndarray, NaN = no copper
    current_density_a_m: "object"     # (n_rows, n_cols-1) ndarray, NaN = no copper
    total_current_a: float
    max_current_density_a_m: float



def _solve_mesh(mask, length_mm, width_mm, current_a, copper_oz,
                in_col, out_col) -> MeshResult:
    np, lil_matrix, spsolve = _require_optional_tier()

    n_rows, n_cols = mask.shape
    if in_col == out_col:
        raise ValueError("in_col and out_col must differ - current has nowhere to flow")

    dx_m = (length_mm / 1000.0) / n_cols
    dy_m = (width_mm / 1000.0) / n_rows
    thickness_m = copper_oz * OZ_THICKNESS_M
    sheet_r_ohm_sq = COPPER_RESISTIVITY_OHM_M / thickness_m

    idx = -np.ones((n_rows, n_cols), dtype=int)
    n = 0
    for i in range(n_rows):
        for j in range(n_cols):
            if mask[i, j]:
                idx[i, j] = n
                n += 1
    if n == 0:
        raise ValueError("mask has no conductive cell - polygon/resolution mismatch")

    def conductance(horizontal: bool) -> float:
        r = (sheet_r_ohm_sq * dx_m / dy_m) if horizontal else (sheet_r_ohm_sq * dy_m / dx_m)
        return 1.0 / r

    g_h, g_v = conductance(True), conductance(False)

    G = lil_matrix((n, n))
    for i in range(n_rows):
        for j in range(n_cols):
            if not mask[i, j]:
                continue
            a = idx[i, j]
            if j + 1 < n_cols and mask[i, j + 1]:
                b = idx[i, j + 1]
                G[a, a] += g_h
                G[b, b] += g_h
                G[a, b] -= g_h
                G[b, a] -= g_h
            if i + 1 < n_rows and mask[i + 1, j]:
                b = idx[i + 1, j]
                G[a, a] += g_v
                G[b, b] += g_v
                G[a, b] -= g_v
                G[b, a] -= g_v

    in_nodes = [idx[i, in_col] for i in range(n_rows) if mask[i, in_col]]
    out_nodes = [idx[i, out_col] for i in range(n_rows) if mask[i, out_col]]
    if not in_nodes:
        raise ValueError(f"in_col={in_col} has no conductive cell")
    if not out_nodes:
        raise ValueError(f"out_col={out_col} has no conductive cell")

    I = np.zeros(n)
    for a in in_nodes:
        I[a] += current_a / len(in_nodes)
    for a in out_nodes:
        I[a] -= current_a / len(out_nodes)

    ref = out_nodes[0]
    G[ref, :] = 0.0
    G[ref, ref] = 1.0
    I[ref] = 0.0

    V = spsolve(G.tocsr(), I)

    node_v = np.full((n_rows, n_cols), np.nan)
    for i in range(n_rows):
        for j in range(n_cols):
            if mask[i, j]:
                node_v[i, j] = V[idx[i, j]]

    density = np.full((n_rows, n_cols - 1), np.nan)
    for i in range(n_rows):
        for j in range(n_cols - 1):
            if mask[i, j] and mask[i, j + 1]:
                branch_a = g_h * (node_v[i, j] - node_v[i, j + 1])
                density[i, j] = abs(branch_a) / dy_m

    valid = density[~np.isnan(density)]
    max_density = float(np.max(valid)) if valid.size else 0.0

    return MeshResult(n_rows, n_cols, node_v, density, current_a, max_density)


def solve_strip(length_mm: float, width_mm: float, current_a: float,
                copper_oz: float = 1.0, n_cols: int = 40, n_rows: int = 10) -> MeshResult:
    """Rectangular copper strip - current enters the left edge, exits the
    right. The special case with a known analytic answer (uniform
    ``current_a / width_mm`` distribution), used to prove the solver."""
    np, _lil, _spsolve = _require_optional_tier()
    mask = np.ones((n_rows, n_cols), dtype=bool)
    return _solve_mesh(mask, length_mm, width_mm, current_a, copper_oz,
                       in_col=0, out_col=n_cols - 1)


def solve_polygon(polygon_mm: Sequence[Tuple[float, float]], current_a: float,
                  copper_oz: float = 1.0, resolution_mm: float = 0.5,
                  in_x_mm: Optional[float] = None,
                  out_x_mm: Optional[float] = None) -> MeshResult:
    """General case: rasterises an arbitrary (convex or concave) copper
    polygon at ``resolution_mm`` and solves it. Uses the polygon's REAL
    corners, not a bounding-box approximation - narrow necks and holes are
    correctly marked non-conductive in the grid."""
    np, _lil, _spsolve = _require_optional_tier()
    xs = [p[0] for p in polygon_mm]
    ys = [p[1] for p in polygon_mm]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    length_mm = x_max - x_min
    width_mm = y_max - y_min
    if length_mm <= 0 or width_mm <= 0:
        raise ValueError("degenerate polygon (zero area)")

    n_cols = max(2, int(round(length_mm / resolution_mm)))
    n_rows = max(2, int(round(width_mm / resolution_mm)))
    dx = length_mm / n_cols
    dy = width_mm / n_rows

    mask = np.zeros((n_rows, n_cols), dtype=bool)
    for i in range(n_rows):
        y = y_min + (i + 0.5) * dy
        for j in range(n_cols):
            x = x_min + (j + 0.5) * dx
            mask[i, j] = _point_in_polygon(x, y, polygon_mm)

    in_x = in_x_mm if in_x_mm is not None else x_min
    out_x = out_x_mm if out_x_mm is not None else x_max
    in_col = int(np.clip(round((in_x - x_min) / dx), 0, n_cols - 1))
    out_col = int(np.clip(round((out_x - x_min) / dx), 0, n_cols - 1))
    if in_col == out_col:
        out_col = n_cols - 1 if in_col == 0 else 0

    return _solve_mesh(mask, length_mm, width_mm, current_a, copper_oz, in_col, out_col)


def save_heatmap(result: MeshResult, png_path: Path,
                 title: str = "Current density (A/m)") -> Optional[Path]:
    """Returns None (no PNG written, no error raised) if matplotlib is not
    installed - the caller must report that, not silently ignore it."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    fig, ax = plt.subplots()
    im = ax.imshow(result.current_density_a_m, origin="upper", aspect="auto", cmap="inferno")
    fig.colorbar(im, ax=ax, label="A/m")
    ax.set_title(title)
    ax.set_xlabel("length (cell)")
    ax.set_ylabel("width (cell)")
    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path)
    plt.close(fig)
    return png_path


# --------------------------------------------------------------------------- #
# pcbnew bridge
# --------------------------------------------------------------------------- #
def _net_track_bounds(board, net_name: str):
    """(x_min, y_min, x_max, y_max) mm of every track/via on ``net_name``, or
    None if the net has no copper. Track-bbox only - see ACCURACY BOUNDARY."""
    points = []
    for t in board.GetTracks():
        if t.GetNetname() != net_name:
            continue
        points.append(xy(t.GetStart()))
        points.append(xy(t.GetEnd()))
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def current_density_for_net(board_path: str, net_name: str, current_a: float,
                            workdir: str, copper_oz: float = 1.0,
                            n_cols: int = 60, n_rows: int = 20):
    """Loads ``board_path``, solves ``net_name``'s track bounding box as a
    resistive-mesh strip, writes ``workdir/current_density_<net>.png``.

    Returns ``(Finding, MeshResult | None)``. NO_COVERAGE (not a silent
    PASS) when: pcbnew is unavailable, the net has no copper, the bbox is
    degenerate, or the optional numpy/scipy tier is missing.
    """
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    bounds = _net_track_bounds(board, net_name)
    if bounds is None:
        return result("current_density", 0, [],
                      f"net '{net_name}' has no tracks - nothing to solve "
                      "(NO_COVERAGE, not a pass)."), None

    x_min, y_min, x_max, y_max = bounds
    length_mm, width_mm = x_max - x_min, y_max - y_min
    if length_mm <= 0 or width_mm <= 0:
        return result("current_density", 0, [],
                      f"net '{net_name}' track bounding box is degenerate "
                      f"({length_mm:.3f}x{width_mm:.3f}mm)."), None

    try:
        mesh = solve_strip(length_mm, width_mm, current_a, copper_oz,
                           n_cols=n_cols, n_rows=n_rows)
    except OptionalDependencyMissing as exc:
        return result("current_density", 0, [], str(exc)), None

    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    png_path = save_heatmap(
        mesh, work / f"current_density_{net_name}.png",
        title=f"{net_name} - {current_a}A (track bounding-box approximation)")

    detail = (f"net={net_name}, bbox={length_mm:.2f}x{width_mm:.2f}mm, "
             f"max_current_density_a_m={mesh.max_current_density_a_m:.2f}, "
             f"png={'not written (matplotlib missing)' if png_path is None else str(png_path)}. "
             "BOUNDARY: track bounding-box approximation, not the real zone "
             "polygon (see module docstring).")
    return result("current_density", 1, [], detail), mesh


CHECKS = ("current_density",)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?")
    ap.add_argument("--net")
    ap.add_argument("--current-a", type=float)
    ap.add_argument("--json")
    ap.add_argument("--out", help="working directory for the heat-map PNG")
    ap.add_argument("--copper-oz", type=float, default=1.0)
    ap.add_argument("--cell-mm", type=float, default=0.5)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()

    if not a.board or not a.net or a.current_a is None:
        ap.error("board, --net and --current-a are required unless --self-test is given")

    tier = optional_tier_status()
    workdir = a.out or str(Path(a.board).resolve().parent)
    finding, _mesh = current_density_for_net(
        a.board, a.net, a.current_a, workdir, copper_oz=a.copper_oz)
    report = board_report(TOOL_NAME, a.board, [finding], params={
        "net": a.net, "current_a": a.current_a, "copper_oz": a.copper_oz,
        "cell_mm": a.cell_mm, "optional_tier": tier,
    })
    write_report(report, a.json)
    emit(report)
    return board_exit_code(report)


def _self_test() -> int:
    """Runs the pure-geometry solver path (no board, no PNG). If numpy/scipy
    are absent this reports that clearly and exits 0 - a self-test is a
    smoke check, not a hard requirement that the optional tier be present."""
    status = optional_tier_status()
    if not status["complete"]:
        diag(f"SELF-TEST SKIP: optional tier not installed ({status})")
        return 0

    # Analytic check: a uniform strip must show a UNIFORM current density
    # away from injection-edge effects (current_a / width_mm).
    mesh = solve_strip(length_mm=20.0, width_mm=10.0, current_a=5.0,
                       copper_oz=1.0, n_cols=40, n_rows=10)
    expected_a_m = 5.0 / (10.0 / 1000.0)
    middle_row = mesh.current_density_a_m[:, mesh.n_cols // 2 - 1]
    import numpy as np
    observed = float(np.nanmean(middle_row))
    if abs(observed - expected_a_m) / expected_a_m > 0.05:
        diag(f"SELF-TEST FAIL: mid-strip density {observed:.1f} A/m, "
             f"expected ~{expected_a_m:.1f} A/m")
        return 1
    diag(f"SELF-TEST PASS: uniform strip density {observed:.1f} A/m "
        f"(expected ~{expected_a_m:.1f} A/m)")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
