#!/usr/bin/env python3
"""Tests for scripts/board/route_audit.py — yonlendirme muhafizi.

Bu muhafiz uretim boru hattinda SERT KAPI olarak kosuyor ve 2026-08-11'e kadar
HIC testi yoktu (envanterle olculdu). Kapiyi test etmemek, kapinin acilip
acilmadigini hic denememektir.

Ayni disiplin test_verify_placement.py'den devralindi: her kural ONCE TEMIZ
halde gosterilir, SONRA kusuru enjekte edilir ve muhafizin dusmesi zorunlu
kilinir. Yalniz gecebilen bir test hicbir sey kanitlamaz.

Fikstürler SENTETIKTIR: depodaki hicbir dosya okunmaz, hicbir dosya
degistirilmez. Kart METIN olarak yazilir cunku muhafizin kendisi de pcbnew
istemez (bkz. route_audit.py TASARIM notu).

Sozlesme (route_audit.py docstring, son satir):
    0 GREEN · 1 ihlal veya NO_COVERAGE (RED) · 2 girdi okunamadi.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS  # noqa: E402

GUARD = BOARD_SCRIPTS / "route_audit.py"

# --------------------------------------------------------------------------- #
# Sentetik kart: 30 x 20 mm dikdortgen, tek iz, tek via.
# Her oge kurallardan UZAKTIR; ihlal ancak enjeksiyonla dogar.
BOARD_TPL = """(kicad_pcb (version 20240108)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(gr_line (start 0 0) (end 30 0) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 30 0) (end 30 20) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 30 20) (end 0 20) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(gr_line (start 0 20) (end 0 0) (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
\t(segment (start {tx0} {ty0}) (end {tx1} {ty1}) (width 0.25) (layer "F.Cu") (net "SIG") (uuid "t1"))
\t(via (at {vx} {vy}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "GND") (uuid "v1"))
)
"""

CLEAN_BOARD = {"tx0": 20.0, "ty0": 5.0, "tx1": 20.0, "ty1": 15.0,
               "vx": 10.0, "vy": 10.0}

# Kural kaynagi: muhafiz hicbir sayiyi kendi tasimaz, .kicad_pro'dan okur.
PROJECT = {
    "board": {"design_settings": {"rules": {
        "min_clearance": 0.15,
        "min_hole_clearance": 0.25,
        "min_hole_to_hole": 0.25,
        "min_copper_edge_clearance": 0.3,
    }}},
    "net_settings": {"classes": [{"name": "Default", "clearance": 0.15}]},
}


class RouteAuditBase(unittest.TestCase):
    """Fikstürleri gecici dizine yazar ve muhafizi alt surec olarak kosar."""

    def run_guard(self, board: dict | None = None, project: dict | None = None,
                  outline: dict | None = None, extra: list[str] | None = None):
        """(cikis kodu, stdout). `board`/`project` verilmezse TEMIZ hal kosar."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            board_path = d / "b.kicad_pcb"
            board_path.write_text(
                BOARD_TPL.format(**{**CLEAN_BOARD, **(board or {})}),
                encoding="utf-8")
            project_path = d / "b.kicad_pro"
            project_path.write_text(
                json.dumps(PROJECT if project is None else project),
                encoding="utf-8")
            argv = [sys.executable, str(GUARD),
                    "--board", str(board_path),
                    "--project", str(project_path)]
            if outline is not None:
                outline_path = d / "board_outline.json"
                outline_path.write_text(json.dumps(outline), encoding="utf-8")
                argv += ["--outline", str(outline_path)]
            argv += extra or []
            return _run(argv)

    @staticmethod
    def project_without_edge_ring() -> dict:
        """Kenar halkasi kuralini TASIMAYAN proje dosyasi (derin kopya)."""
        project = json.loads(json.dumps(PROJECT))
        del project["board"]["design_settings"]["rules"]["min_copper_edge_clearance"]
        return project


def _run(argv: list[str]):
    """Alt surec + deterministik metin kodlamasi.

    PYTHONIOENCODING ACIKCA verilir: muhafizin --help ciktisi Turkce karakter
    tasir ve cocugun yerel kod sayfasi onu kodlayamazsa arac YAZAMADIGI ICIN
    duser - test o zaman muhafizi degil, konsolu olcmus olurdu.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    return r.returncode, r.stdout


class TestCliContract(RouteAuditBase):
    def test_help_exits_zero(self):
        rc, out = _run([sys.executable, str(GUARD), "--help"])
        self.assertEqual(rc, 0, f"--help calismiyor:\n{out}")
        self.assertIn("--board", out)
        self.assertIn("--edge-ring-mm", out)

    def test_unreadable_input_returns_two_not_one(self):
        """'Dosyayi acamadim' ile 'ihlal buldum' AYNI SEY DEGILDIR.

        Ikisi de 1 donseydi, boru hattinda yanlis yazilmis bir yol meşru bir
        DRC bulgusu gibi gorunurdu ve kimse dosyayi aramazdi."""
        rc, out = _run([sys.executable, str(GUARD),
                        "--board", "yok-boyle-bir-kart.kicad_pcb"])
        self.assertEqual(rc, 2, f"okunamayan girdi 2 disinda bir kod verdi:\n{out}")
        self.assertIn("girdi okunamadi", out)


class TestCleanBaseline(RouteAuditBase):
    """Muhafiz ACILABILIYOR mu? Asla acilmayan bir kapi kapi degildir."""

    def test_clean_board_is_green(self):
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, f"temiz fikstur GREEN vermedi:\n{out}")
        self.assertIn("YONLENDIRME MUHAFIZI: GREEN", out)

    def test_clean_board_actually_scanned_something(self):
        """Sifir oge tarayan bir GREEN, GREEN degildir - bos bir kosudur."""
        rc, out = self.run_guard()
        self.assertEqual(rc, 0)
        self.assertIn("1 via - 1 iz", out)
        self.assertIn("4 dogru parcasi", out)


class TestEdgeRingCoverage(RouteAuditBase):
    """Kenar halkasi KURAL KAYNAGI: yoksa olculemez, olculemeyen PASS degildir."""

    def test_missing_edge_ring_source_is_no_coverage_not_pass(self):
        rc, out = self.run_guard(project=self.project_without_edge_ring())
        self.assertEqual(rc, 1, f"halka kaynagi yokken muhafiz gecti:\n{out}")
        self.assertIn("YONLENDIRME MUHAFIZI: NO_COVERAGE", out)
        self.assertIn("copper_edge_clearance", out)
        self.assertIn("olcemedim PASS DEGILDIR", out)

    def test_outline_declaration_restores_coverage(self):
        """AYRIMCILIK: halka board_outline.json'dan GERCEKTEN okunuyor mu?

        Bir onceki test tek basina 'muhafiz her seye NO_COVERAGE diyor'
        hipoteziyle de gecerdi. Ayni kart + ayni proje, yalniz beyan eklenince
        GREEN'e donuyorsa beyan okunmus demektir."""
        project = self.project_without_edge_ring()
        rc, out = self.run_guard(project=project,
                                 outline={"outline": {"edge_clearance_ring_mm": 0.3}})
        self.assertEqual(rc, 0, f"beyan verildi ama hala NO_COVERAGE:\n{out}")
        self.assertIn("YONLENDIRME MUHAFIZI: GREEN", out)
        self.assertIn("board_outline.json:outline.edge_clearance_ring_mm", out)


class TestFaultInjection(RouteAuditBase):
    """Kusur enjeksiyonu: muhafiz DUSURULEBILIYOR mu?"""

    def test_track_inside_edge_ring_is_caught(self):
        rc0, _ = self.run_guard()
        self.assertEqual(rc0, 0, "temiz hal once GREEN olmali")

        # Izi kartin sol kenarina 0.1 mm'ye yaklastir: halka 0.3 mm.
        rc, out = self.run_guard(board={"tx0": 0.1, "ty0": 10.0,
                                        "tx1": 5.0, "ty1": 10.0})
        self.assertEqual(rc, 1, f"kenar halkasi delindi ama muhafiz gecti:\n{out}")
        self.assertIn("YONLENDIRME MUHAFIZI: RED", out)
        self.assertIn("copper_edge_clearance", out)

    def test_via_too_close_to_foreign_net_copper_is_caught(self):
        """Via bir IZ DEGILDIR: kendi yaricapiyla sinanmali (route_audit'in
        var olma sebeplerinden birincisi)."""
        rc, out = self.run_guard(board={"vx": 20.2, "vy": 10.0})
        self.assertEqual(rc, 1, f"via yabanci bakira girdi ama gecti:\n{out}")
        self.assertIn("YONLENDIRME MUHAFIZI: RED", out)
        self.assertIn("via_copper_clearance", out)
        self.assertIn("SIG", out, "hangi yabanci net oldugu raporlanmali")

    def test_report_only_reports_but_does_not_gate(self):
        """--report-only KAPI DEGILDIR: ihlali bulur, ama 0 doner.

        Bu ayrim olculmeye deger, cunku kapiyi kapatan sey cikis kodudur;
        bayragin yanlislikla varsayilan olmasi muhafizi sessizce susturur."""
        rc, out = self.run_guard(board={"tx0": 0.1, "ty0": 10.0,
                                        "tx1": 5.0, "ty1": 10.0},
                                 extra=["--report-only"])
        self.assertEqual(rc, 0, "--report-only kapi gibi davrandi")
        self.assertIn("YONLENDIRME MUHAFIZI: RED", out,
                      "kapi degil ama RAPOR: ihlal yine de gorunmeli")


if __name__ == "__main__":
    unittest.main()
