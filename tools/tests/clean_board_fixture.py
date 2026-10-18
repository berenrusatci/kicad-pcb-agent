"""A genuinely DRC-clean board, built with pcbnew.

``tests/board_fixture.py`` builds boards for the DFM/EMC checkers; every one
of them raises kicad-cli DRC violations (dangling vias, silk over copper,
hole clearance), which is fine for those tests and useless for a promotion
gate test that needs a GREEN run.

This board is the minimum that is BOTH non-empty (so coverage is not zero)
and clean at ``--severity-all``: an Edge.Cuts rectangle, two SMD footprints
with their reference and value text hidden, and one track joining a pad of
each on a shared net. Nothing here is arbitrary - each element is present
because removing it either empties the census or raises a violation.
"""

from __future__ import annotations

from pathlib import Path

EDGE = [(0, 0, 60, 0), (60, 0, 60, 40), (60, 40, 0, 40), (0, 40, 0, 0)]


def _mm(value: float) -> int:
    import pcbnew
    return pcbnew.FromMM(value)


def _vec(x: float, y: float):
    import pcbnew
    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _outline(board) -> None:
    import pcbnew
    for x0, y0, x1, y1 in EDGE:
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(_vec(x0, y0))
        shape.SetEnd(_vec(x1, y1))
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(_mm(0.1))
        board.Add(shape)


def _footprint(board, ref: str, x: float, y: float, pitch: float = 1.6):
    import pcbnew
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference(ref)
    footprint.SetPosition(_vec(x, y))
    for index, dx in enumerate((-pitch / 2.0, pitch / 2.0)):
        pad = pcbnew.PAD(footprint)
        pad.SetNumber(str(index + 1))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(_mm(0.9), _mm(1.2)))
        pad.SetPosition(_vec(x + dx, y))
        pad.SetLayerSet(pad.SMDMask())
        footprint.Add(pad)
    board.Add(footprint)
    # Silk text over the pads is a real DRC violation; the fixture is about
    # the gate, not about silkscreen, so the text is hidden rather than moved.
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    return footprint


def clean_routed_board(path: Path) -> Path:
    """Outline, two footprints, one routed net. Zero DRC violations."""
    import pcbnew
    board = pcbnew.NewBoard(str(path))
    _outline(board)
    net = pcbnew.NETINFO_ITEM(board, "NET1")
    board.Add(net)
    left = _footprint(board, "C1", 20.0, 20.0)
    right = _footprint(board, "C2", 30.0, 20.0)
    pad_a = next(p for p in left.Pads() if p.GetNumber() == "2")
    pad_b = next(p for p in right.Pads() if p.GetNumber() == "1")
    pad_a.SetNet(net)
    pad_b.SetNet(net)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pad_a.GetPosition())
    track.SetEnd(pad_b.GetPosition())
    track.SetWidth(_mm(0.25))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    board.Add(track)
    pcbnew.SaveBoard(str(path), board)
    return path


def dirty_board(path: Path) -> Path:
    """The clean board with its silkscreen reference text put back over copper.

    Three faults were tried before this one and each was a FALSE NEGATIVE on
    KiCad 10, which is worth recording because a fixture that cannot fail is
    worse than no fixture:
      * a netless dangling via - not flagged;
      * a 0.05 mm clearance infringement - not flagged, because a board made
        by ``NewBoard`` carries a smaller default netclass clearance;
      * a second net's track laid on top of the first - the SWIG net object
        did not take, and the track saved with the ORIGINAL net, so there was
        no short at all.
    Visible reference text sitting over the pads is reported as
    ``silk_over_copper`` every time, which is what this fixture needs.
    """
    import pcbnew
    clean_routed_board(path)
    board = pcbnew.LoadBoard(str(path))
    for footprint in board.GetFootprints():
        footprint.Reference().SetVisible(True)
        footprint.Reference().SetPosition(footprint.GetPosition())
    pcbnew.SaveBoard(str(path), board)
    return path
