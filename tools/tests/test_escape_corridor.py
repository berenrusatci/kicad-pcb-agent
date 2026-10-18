"""connector_escape_y_mm — kusur enjeksiyonu.

Bu fonksiyon kaynak koda gomulu iki sayinin (ESCAPE_Y = {J101: 9.0,
J201: 23.0}) yerine geldi. Yerine gectigi seyi BIREBIR yeniden urettigini
kanitlamadan degisiklik guvenilir degil; ayrica turetmenin ne zaman
REDDETTIGI de kanitlanmali - hicbir sey reddetmeyen bir turetme, sabitten
daha kotudur, cunku dogru gorunur.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from common.escape_geom import ConnectorBand, connector_escape_y_mm  # noqa: E402

REPO = TOOLS.parent.parent
CAM_W_OUTLINE = REPO / "cam-w-og05b10" / "layout" / "board_outline.json"

# pcb_route.py'de 2026-08-10 oncesi yazan sabitler. Bu satir bir TARIH
# KAYDIDIR: turetme bunlardan saparsa sapmanin bilerek yapilmis olmasi gerekir.
HISTORICAL_ESCAPE_Y = {"J101": 9.0, "J201": 23.0}
BOARD_H = 32.0


class EscapeYDerivation(unittest.TestCase):

    def test_j101_reproduces_the_old_constant(self):
        # Bant alt kenara (y=0) dayaniyor -> kacis +Y yonunde
        band = ConnectorBand("J101", 0.0, 8.6)
        y, why = connector_escape_y_mm(band, BOARD_H, 0.4)
        self.assertAlmostEqual(y, 9.0, places=9)
        self.assertIn("+Y", why)

    def test_j201_reproduces_the_old_constant(self):
        # Bant ust kenara (y=32) dayaniyor -> kacis -Y yonunde
        band = ConnectorBand("J201", 23.4, 32.0)
        y, why = connector_escape_y_mm(band, BOARD_H, 0.4)
        self.assertAlmostEqual(y, 23.0, places=9)
        self.assertIn("-Y", why)

    def test_derivation_string_carries_its_own_numbers(self):
        """Kaynagi basilamayan sayiyi kimse denetleyemez."""
        _y, why = connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6),
                                        BOARD_H, 0.4)
        for token in ("J101", "8.6", "0.4", "9.0"):
            self.assertIn(token, why)

    # ---- pay degisince sonuc DEGISMELI (yoksa pay okunmuyor demektir) ---- #

    def test_margin_actually_moves_the_corridor(self):
        a, _ = connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), BOARD_H, 0.4)
        b, _ = connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), BOARD_H, 0.9)
        self.assertNotAlmostEqual(a, b)
        self.assertAlmostEqual(b - a, 0.5, places=9)

    def test_band_change_actually_moves_the_corridor(self):
        a, _ = connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), BOARD_H, 0.4)
        b, _ = connector_escape_y_mm(ConnectorBand("J101", 0.0, 9.6), BOARD_H, 0.4)
        self.assertAlmostEqual(b - a, 1.0, places=9)

    def test_flipped_band_bounds_are_normalised(self):
        """lo/hi ters verilse de ayni sonuc; sessizce yanlis yon secilmemeli."""
        a, _ = connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), BOARD_H, 0.4)
        b, _ = connector_escape_y_mm(ConnectorBand("J101", 8.6, 0.0), BOARD_H, 0.4)
        self.assertAlmostEqual(a, b, places=9)

    # ---- REDDETMESI gereken durumlar ------------------------------------- #

    def test_band_floating_in_the_middle_is_refused(self):
        """Hicbir kenara dayanmayan bant konnektor bandi degildir."""
        with self.assertRaises(ValueError) as cm:
            connector_escape_y_mm(ConnectorBand("JX", 10.0, 18.0), BOARD_H, 0.4)
        self.assertIn("abuts neither", str(cm.exception))

    def test_zero_margin_is_refused(self):
        with self.assertRaises(ValueError):
            connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), BOARD_H, 0.0)

    def test_negative_margin_is_refused(self):
        with self.assertRaises(ValueError):
            connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), BOARD_H, -0.4)

    def test_degenerate_band_is_refused(self):
        with self.assertRaises(ValueError):
            connector_escape_y_mm(ConnectorBand("J101", 8.6, 8.6), BOARD_H, 0.4)

    def test_zero_board_height_is_refused(self):
        with self.assertRaises(ValueError):
            connector_escape_y_mm(ConnectorBand("J101", 0.0, 8.6), 0.0, 0.4)

    def test_tolerance_boundary(self):
        """0.5 mm tolerans icinde kabul, disinda RED - sinir olculur."""
        y, _ = connector_escape_y_mm(ConnectorBand("J", 0.5, 8.6), BOARD_H, 0.4)
        self.assertAlmostEqual(y, 9.0, places=9)
        with self.assertRaises(ValueError):
            connector_escape_y_mm(ConnectorBand("J", 0.6, 8.6), BOARD_H, 0.4)


class CamWDeclarationStillReproducesTheConstants(unittest.TestCase):
    """Beyan ile tarih arasindaki bag.

    Genel fonksiyonun dogru olmasi yetmez: cam-w'nin JSON'una yazilan bant ve
    pay degerlerinin de eski sabitleri uretmesi gerekir. Biri digerinden
    habersiz degistirilirse bu test dusler.
    """

    def setUp(self):
        if not CAM_W_OUTLINE.is_file():
            self.skipTest("cam-w board_outline.json yok")
        self.bo = json.loads(CAM_W_OUTLINE.read_text(encoding="utf-8"))

    def _bands(self):
        return [z for z in self.bo.get("zones", [])
                if z.get("kind") == "connector_band"]

    def test_every_connector_band_declares_ref_and_margin(self):
        for z in self._bands():
            self.assertIn("connector_ref", z, f"{z['id']}: connector_ref yok")
            self.assertIn("escape_margin_mm", z, f"{z['id']}: pay beyan edilmemis")
            self.assertIn("escape_margin_src", z,
                          f"{z['id']}: payin kaynagi beyan edilmemis")

    def test_declaration_reproduces_historical_constants(self):
        height = self.bo["outline"]["height_y_mm"]
        seen = {}
        for z in self._bands():
            r = z["rect_mm"]
            band = ConnectorBand(z["connector_ref"], r["y_mm"],
                                 r["y_mm"] + r["h_mm"])
            y, _ = connector_escape_y_mm(band, height, z["escape_margin_mm"])
            seen[z["connector_ref"]] = y
        self.assertEqual(set(seen), set(HISTORICAL_ESCAPE_Y))
        for ref, expected in HISTORICAL_ESCAPE_Y.items():
            self.assertAlmostEqual(seen[ref], expected, places=9,
                                   msg=f"{ref}: {seen[ref]} != {expected}")


if __name__ == "__main__":
    unittest.main()
