#!/usr/bin/env python3
"""Tests for scripts/board/verify_placement.py — yerleşim kilidi muhafızı.

Her kural ÖNCE TEMİZ hâlde gösterilir, SONRA kusuru enjekte edilir ve muhafızın
düşmesi zorunlu kılınır. Yalnız geçebilen bir test hiçbir şey kanıtlamaz.

Bu dosyanın var olma sebebi ölçülmüş bir olaydır (2026-08-10, cam-w-og05b10):
`locked: true` üretiliyordu, beyan ediliyordu, hatta karta `fp.SetLocked(True)`
olarak yazılıyordu — ama **hiçbir şey doğrulamıyordu**. Kilit bir temenniydi.
Aşağıdaki senaryolar o gün elle koşturulup yakalandı; burada kalıcı hâle
getiriliyorlar ki bir daha sessizce kaybolmasınlar.

Fikstürler SENTETİKTİR: hiçbir projeye ait dosya okunmaz. Muhafız genel
amaçlıdır, testi de öyle olmalıdır.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS  # noqa: E402

GUARD = BOARD_SCRIPTS / "verify_placement.py"

# --------------------------------------------------------------------------- #
# Sentetik kart: 10 x 10 mm, cerceve sol-ALT (0,0), KiCad'de (5,25)..(15,15).
# Yani donusum: board_x = 5 + frame_x ; board_y = 25 - frame_y
BOARD_TPL = """(kicad_pcb (version 20240108)
\t(gr_line (start 5 15) (end 15 15) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 15 15) (end 15 25) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 15 25) (end 5 25) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 5 25) (end 5 15) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
{FOOTPRINTS}
)
"""

FP_TPL = """\t(footprint "lib:{fp}"
\t\t(layer "{layer}")
\t\t(at {bx} {by})
\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))
\t)"""


def to_board(fx: float, fy: float) -> tuple[float, float]:
    return (5.0 + fx, 25.0 - fy)


def make_board(components: dict) -> str:
    fps = []
    for ref, c in components.items():
        bx, by = to_board(c["x_mm"], c["y_mm"])
        fps.append(FP_TPL.format(fp="X", layer=c["layer"], bx=bx, by=by, ref=ref))
    return BOARD_TPL.format(FOOTPRINTS="\n".join(fps))


BASE_COMPONENTS = {
    # koridor DISINDA, kilitli
    "J1":  {"x_mm": 5.0, "y_mm": 1.0, "layer": "B.Cu", "locked": True},
    # koridor ICINDE, karsi yuzde, BEYAN EDILMIS
    "U1":  {"x_mm": 4.0, "y_mm": 4.0, "layer": "F.Cu", "locked": True},
    # koridor disinda, kilitsiz
    "C1":  {"x_mm": 8.5, "y_mm": 8.0, "layer": "F.Cu", "locked": False},
}

OUTLINE = {
    "frame": {"origin": "sol-alt kose (0,0)", "y_axis": "YUKARI"},
    "zones": [
        {
            "kind": "escape_fanout",
            "id": "j1_escape",
            "rect_mm": {"x_mm": 3.0, "y_mm": 2.0, "w_mm": 4.0, "h_mm": 4.0},
            "escape_layer": "B.Cu",
            "exclude_layers": ["B.Cu"],
            "allowed_refs": ["J1", "U1"],
        }
    ],
}

BODIES = {"J1": [1.0, 0.6], "U1": [1.0, 0.6], "C1": [1.0, 0.6]}


class VerifyPlacementBase(unittest.TestCase):
    """Fikstürleri geçici dizine yazar ve muhafızı alt süreç olarak koşar."""

    def run_guard(self, declared: dict, outline: dict | None = None,
                  board_components: dict | None = None):
        """`declared` = placement.json (BEYAN). `board_components` = karta
        gercekten cizilen (GERCEKLIK); verilmezse beyanla ayni kabul edilir.

        Bu ayrim testin ta kendisidir: kilit muhafizi ancak ikisi AYRISTIGINDA
        bir sey soyleyebilir. Ilk yazimimda karti da beyandan uretiyordum, bu
        yuzden ayrisma hic olusmuyor ve muhafiz hakli olarak GREEN veriyordu."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            board = d / "b.kicad_pcb"
            board.write_text(make_board(board_components if board_components
                                        is not None else declared),
                             encoding="utf-8")
            (d / "placement.json").write_text(
                json.dumps({"components": declared}), encoding="utf-8")
            (d / "outline.json").write_text(
                json.dumps(outline or OUTLINE), encoding="utf-8")
            (d / "bodies.json").write_text(json.dumps(BODIES), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(GUARD),
                 "--board", str(board),
                 "--placement", str(d / "placement.json"),
                 "--outline", str(d / "outline.json"),
                 "--bodies", str(d / "bodies.json")],
                capture_output=True, text=True)
            return r.returncode, r.stdout

    def move_declared(self, components: dict, ref: str, **kw) -> dict:
        """placement.json'u degistirir ama BOARD'u DEGISTIRMEZ -> beyan/gerceklik
        ayrisir. Kilit muhafizinin yakalamasi gereken tam olarak budur."""
        out = {k: dict(v) for k, v in components.items()}
        out[ref].update(kw)
        return out


class TestCleanBaseline(VerifyPlacementBase):
    """Muhafiz ACILABILIYOR mu? Asla acilmayan bir kapi kapi degildir."""

    def test_clean_is_green(self):
        rc, out = self.run_guard(BASE_COMPONENTS)
        self.assertEqual(rc, 0, f"temiz fikstur RED verdi:\n{out}")
        self.assertIn("GREEN", out)
        self.assertIn("kilit ihlali           : 0", out)
        self.assertIn("zone ihlali            : 0", out)

    def test_frame_transform_measured_from_edge_cuts(self):
        """Donusum board_outline'dan DEGIL, cizilmis Edge.Cuts'tan olculmeli."""
        rc, out = self.run_guard(BASE_COMPONENTS)
        self.assertEqual(rc, 0)
        self.assertIn("board = (5.000 + x, 25.000 - y)", out)


class TestLockFaultInjection(VerifyPlacementBase):
    """Kilit gercekten kilit mi?"""

    def test_moved_locked_component_is_caught(self):
        rc0, _ = self.run_guard(BASE_COMPONENTS)
        self.assertEqual(rc0, 0, "temiz hal once GREEN olmali")

        bad = self.move_declared(BASE_COMPONENTS, "U1", x_mm=4.5)
        rc, out = self.run_guard(bad, board_components=BASE_COMPONENTS)
        self.assertEqual(rc, 1, f"kilitli bilesen oynatildi ama muhafiz gecti:\n{out}")
        self.assertIn("KILIT KIRILDI", out)
        self.assertIn("U1", out)
        self.assertIn("0.500", out, "sapma miktari raporlanmali")

    def test_layer_change_on_locked_component_is_caught(self):
        bad = self.move_declared(BASE_COMPONENTS, "J1", layer="F.Cu")
        rc, out = self.run_guard(bad, board_components=BASE_COMPONENTS)
        self.assertEqual(rc, 1)
        self.assertIn("katman", out)
        self.assertIn("J1", out)

    def test_locked_component_missing_from_board_is_caught(self):
        decl = {k: dict(v) for k, v in BASE_COMPONENTS.items()}
        decl["U9"] = {"x_mm": 6.0, "y_mm": 6.0, "layer": "F.Cu", "locked": True}
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            board = d / "b.kicad_pcb"
            board.write_text(make_board(BASE_COMPONENTS), encoding="utf-8")  # U9 YOK
            (d / "placement.json").write_text(
                json.dumps({"components": decl}), encoding="utf-8")
            (d / "outline.json").write_text(json.dumps(OUTLINE), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(GUARD), "--board", str(board),
                 "--placement", str(d / "placement.json"),
                 "--outline", str(d / "outline.json")],
                capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("KARTTA YOK", r.stdout)

    def test_unlocked_component_may_move_freely(self):
        """Muhafiz AYRIMCI olmali: kilitsiz bir bilesenin oynamasi ihlal DEGIL."""
        bad = self.move_declared(BASE_COMPONENTS, "C1", x_mm=9.5)
        rc, out = self.run_guard(bad, board_components=BASE_COMPONENTS)
        self.assertEqual(rc, 0, f"kilitsiz bilesen oynadi diye RED verdi:\n{out}")


class TestZoneFaultInjection(VerifyPlacementBase):
    """Koridor kurallari katman-farkinda mi?"""

    def test_undeclared_component_in_corridor_is_caught(self):
        bad = self.move_declared(BASE_COMPONENTS, "C1", x_mm=4.0, y_mm=3.0)
        # board da tasinir: burada test edilen ZONE kurali, kilit degil
        rc, out = self.run_guard(bad, board_components=bad)
        self.assertEqual(rc, 1, f"beyansiz bilesen koridora girdi ama gecti:\n{out}")
        self.assertIn("BEYAN EDILMEMIS", out)
        self.assertIn("C1", out)

    def test_component_on_escape_face_is_caught_even_if_declared(self):
        """allowed_refs KACIS YUZUNDE istisna SAGLAMAZ - orada iz kosuyor."""
        bad = self.move_declared(BASE_COMPONENTS, "U1", layer="B.Cu")
        rc, out = self.run_guard(bad, board_components=bad)
        self.assertEqual(rc, 1)
        self.assertIn("KACIS YUZUNDE", out)
        self.assertIn("istisna yok", out)

    def test_declared_component_on_opposite_face_is_allowed(self):
        """Ayrimcilik: beyan edilmis bilesen KARSI yuzde durabilmeli.

        Aksi halde kural 'her seyi reddet'e donerdi ve XO kumesi gibi konumu
        SI gerekcesiyle zorunlu olan bileşenler koridordan atilirdi."""
        rc, out = self.run_guard(BASE_COMPONENTS)
        self.assertEqual(rc, 0)
        self.assertIn("U1", json.dumps(OUTLINE["zones"][0]["allowed_refs"]))

    def test_zone_without_exclude_layers_still_guards_corridor_kind(self):
        """exclude_layers verilmese bile escape_fanout/high_speed_corridor
        turleri beyan zorunlulugunu surdurmeli."""
        outline = json.loads(json.dumps(OUTLINE))
        del outline["zones"][0]["exclude_layers"]
        bad = self.move_declared(BASE_COMPONENTS, "C1", x_mm=4.0, y_mm=3.0)
        rc, out = self.run_guard(bad, outline, board_components=bad)
        self.assertEqual(rc, 1, f"tur bazli koruma dustu:\n{out}")
        self.assertIn("BEYAN EDILMEMIS", out)


class TestGuardRobustness(VerifyPlacementBase):
    def test_missing_edge_cuts_is_reported_not_silently_passed(self):
        """Donusum olculemezse muhafiz GECMEZ - 'olcemedim' PASS degildir."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            board = d / "b.kicad_pcb"
            board.write_text("(kicad_pcb (version 20240108)\n)\n", encoding="utf-8")
            (d / "placement.json").write_text(
                json.dumps({"components": BASE_COMPONENTS}), encoding="utf-8")
            (d / "outline.json").write_text(json.dumps(OUTLINE), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(GUARD), "--board", str(board),
                 "--placement", str(d / "placement.json"),
                 "--outline", str(d / "outline.json")],
                capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "olculemeyen donusum GREEN verdi")
        self.assertIn("olculemedi", r.stdout)

    def test_unreadable_input_returns_two_not_zero(self):
        r = subprocess.run(
            [sys.executable, str(GUARD), "--board", "yok.kicad_pcb",
             "--placement", "yok.json", "--outline", "yok.json"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
