#!/usr/bin/env python3
"""Which `.kicad_dru` rule wins when two rules match the same object?

This is not a style question. Get it backwards and every specific rule you
inject is silently overridden by the general one, while DRC still reports
"clean" — a false PASS on the gate that is supposed to catch fabrication
defects.

Two sources disagreed:
  * ProjectE-1 skills said "the LATER rule wins, so write general first,
    specific last" (but also, self-contradictorily, "first match applies").
  * projectE-2archive/pcb-tool-v2 DOCS/03_Design_Rules.md:38-46 and
    HAFIZA/Hafiza_Defteri.md:123-144 recorded the OPPOSITE as a verified
    correction: "FIRST MATCHING RULE WINS, write specific before general."

Measured on KiCad 10.0.5: the LATER matching rule wins. The archive's
"correction" is wrong, and adopting it would have disabled every specific
rule in the bundle.

The test builds a board with a known-bad 0.15 mm edge-to-edge gap and two
competing clearance rules, runs DRC in both file orders, and asserts the
outcome flips with the order — with the LAST rule deciding each time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402

sys.path.insert(0, str(TOOLS))
from common.kicad_cli import _require_cli  # noqa: E402

GAP_MM = 0.15
GENERAL_MM = 0.25   # would flag the gap
SPECIFIC_MM = 0.10  # would allow the gap

GENERAL_RULE = """(rule "GENEL_gevsek"
  (constraint clearance (min %smm)))""" % GENERAL_MM

SPECIFIC_RULE = """(rule "SPESIFIK_siki"
  (condition "A.NetName == 'NET_A'")
  (constraint clearance (min %smm)))""" % SPECIFIC_MM


def _build_board(path: Path) -> None:
    import pcbnew
    board = pcbnew.BOARD()
    nets = {}
    for name in ("NET_A", "NET_B"):
        info = pcbnew.NETINFO_ITEM(board, name)
        board.Add(info)
        nets[name] = info

    def track(net, y_mm):
        item = pcbnew.PCB_TRACK(board)
        item.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(y_mm)))
        item.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(30), pcbnew.FromMM(y_mm)))
        item.SetWidth(pcbnew.FromMM(0.2))
        item.SetLayer(pcbnew.F_Cu)
        item.SetNet(net)
        board.Add(item)

    # centre spacing 0.35 mm - width 0.2 mm => 0.15 mm edge to edge
    track(nets["NET_A"], 20.0)
    track(nets["NET_B"], 20.0 + 0.2 + GAP_MM)

    for x1, y1, x2, y2 in ((5, 5, 40, 5), (40, 5, 40, 30),
                           (40, 30, 5, 30), (5, 30, 5, 5)):
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(pcbnew.FromMM(0.1))
        board.Add(shape)

    board.Save(str(path))


def _clearance_violations(board: Path, rules_text: str) -> list[dict]:
    (board.with_suffix(".kicad_dru")).write_text(
        "(version 1)\n" + rules_text + "\n", encoding="utf-8")
    report = board.parent / "drc.json"
    subprocess.run([_require_cli(), "pcb", "drc", "--severity-all",
                    "--format", "json", "-o", str(report), str(board)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace")
    data = json.loads(report.read_text(encoding="utf-8"))
    return [v for v in data.get("violations", [])
            if "clearance" in v.get("type", "")]


@unittest.skipIf(shutil.which("kicad-cli") is None, "kicad-cli not installed")
class DruRulePrecedence(unittest.TestCase):
    def setUp(self):
        try:
            import pcbnew  # noqa: F401
        except ImportError:
            self.skipTest("pcbnew not importable")
        self._tmp = tempfile.TemporaryDirectory()
        self.board = Path(self._tmp.name) / "t.kicad_pcb"
        _build_board(self.board)

    def tearDown(self):
        self._tmp.cleanup()

    def test_general_first_then_specific_lets_the_specific_rule_win(self):
        violations = _clearance_violations(
            self.board, GENERAL_RULE + "\n" + SPECIFIC_RULE)
        self.assertEqual(
            [], violations,
            "specific rule written LAST must win and allow the 0.15 mm gap; "
            "a violation here would mean first-match-wins")

    def test_specific_first_then_general_lets_the_general_rule_win(self):
        violations = _clearance_violations(
            self.board, SPECIFIC_RULE + "\n" + GENERAL_RULE)
        self.assertEqual(
            1, len(violations),
            "general rule written LAST must win and flag the 0.15 mm gap")
        self.assertIn(
            "GENEL_gevsek", violations[0].get("description", ""),
            "the violation must be attributed to the LAST rule")


if __name__ == "__main__":
    unittest.main()
