"""Generate .kicad_pcb fixtures for the board checkers with pcbnew.

The board checkers previously had NO tests at all, because the bundle had no
committed board fixtures. Rather than commit opaque binaries, the fixtures
are BUILT here from the pcbnew API, so the fault being injected is visible in
the source instead of hidden in a board file.

Every builder returns a path to a saved board. ``clean_board`` is the
baseline; each ``with_*`` variant injects exactly one defect so a test can
assert that the corresponding check flips PASS -> FAIL. That is the same
fault-injection discipline the rest of the bundle uses: a check that cannot
be made to fail is not evidence.
"""

from __future__ import annotations

from pathlib import Path

import pcbnew

EDGE = [(0, 0, 60, 0), (60, 0, 60, 40), (60, 40, 0, 40), (0, 40, 0, 0)]


def _mm(value: float) -> int:
    return pcbnew.FromMM(value)


def _vec(x: float, y: float):
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _add_outline(board) -> None:
    for x0, y0, x1, y1 in EDGE:
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_vec(x0, y0))
        seg.SetEnd(_vec(x1, y1))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(_mm(0.1))
        board.Add(seg)


def _add_via(board, x: float, y: float, *, pad_mm: float = 0.6,
             drill_mm: float = 0.3, net=None):
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(_vec(x, y))
    via.SetWidth(_mm(pad_mm))
    via.SetDrill(_mm(drill_mm))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    if net is not None:
        via.SetNet(net)
    board.Add(via)
    return via


def _add_net(board, name: str):
    net = pcbnew.NETINFO_ITEM(board, name)
    board.Add(net)
    return net


def _add_smd_footprint(board, ref: str, x: float, y: float,
                       *, pitch_mm: float = 1.6, pad_w: float = 0.9,
                       pad_h: float = 1.2):
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference(ref)
    footprint.SetPosition(_vec(x, y))
    for index, dx in enumerate((-pitch_mm / 2.0, pitch_mm / 2.0)):
        pad = pcbnew.PAD(footprint)
        pad.SetNumber(str(index + 1))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(_mm(pad_w), _mm(pad_h)))
        pad.SetPosition(_vec(x + dx, y))
        pad.SetLayerSet(pad.SMDMask())
        footprint.Add(pad)
    board.Add(footprint)
    return footprint


def _save(board, path: Path) -> Path:
    pcbnew.SaveBoard(str(path), board)
    return path


def clean_board(path: Path) -> Path:
    """Baseline: outline, a GND stitching grid, and two ceramics well inboard."""
    board = pcbnew.NewBoard(str(path))
    _add_outline(board)
    gnd = _add_net(board, "GND")
    # Dense stitching so the lambda/20 edge check has coverage and passes.
    for ix in range(0, 13):
        for iy in range(0, 9):
            _add_via(board, 2.5 + ix * 4.5, 2.5 + iy * 4.5, net=gnd)
    _add_smd_footprint(board, "C1", 25.0, 20.0)
    _add_smd_footprint(board, "C2", 35.0, 20.0)
    return _save(board, path)


def board_with_small_annular_ring(path: Path) -> Path:
    """Inject a via whose annular ring is below the 0.15 mm minimum.

    (0.30 pad - 0.25 drill) / 2 = 0.025 mm ring.
    """
    board = pcbnew.NewBoard(str(path))
    _add_outline(board)
    gnd = _add_net(board, "GND")
    for ix in range(0, 13):
        for iy in range(0, 9):
            _add_via(board, 2.5 + ix * 4.5, 2.5 + iy * 4.5, net=gnd)
    _add_via(board, 30.0, 20.0, pad_mm=0.30, drill_mm=0.25, net=gnd)
    _add_smd_footprint(board, "C1", 25.0, 20.0)
    return _save(board, path)


def board_with_edge_ceramic(path: Path) -> Path:
    """Inject a two-terminal ceramic closer to the edge than the keepout."""
    board = pcbnew.NewBoard(str(path))
    _add_outline(board)
    gnd = _add_net(board, "GND")
    for ix in range(0, 13):
        for iy in range(0, 9):
            _add_via(board, 2.5 + ix * 4.5, 2.5 + iy * 4.5, net=gnd)
    _add_smd_footprint(board, "C1", 25.0, 20.0)
    _add_smd_footprint(board, "C9", 0.9, 20.0)      # 0.9 mm from the left edge
    return _save(board, path)


def board_without_stitching(path: Path) -> Path:
    """No GND vias at all: the stitching checks must report NO_COVERAGE."""
    board = pcbnew.NewBoard(str(path))
    _add_outline(board)
    _add_smd_footprint(board, "C1", 25.0, 20.0)
    return _save(board, path)


def board_with_sparse_stitching(path: Path) -> Path:
    """GND vias present but far apart: the edge stitching check must FAIL."""
    board = pcbnew.NewBoard(str(path))
    _add_outline(board)
    gnd = _add_net(board, "GND")
    for ix in range(0, 3):
        _add_via(board, 5.0 + ix * 25.0, 20.0, net=gnd)
    _add_smd_footprint(board, "C1", 25.0, 20.0)
    return _save(board, path)
