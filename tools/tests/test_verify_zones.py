#!/usr/bin/env python3
"""Tests for scripts/board/verify_zones.py — bolge (zone) muhafizi.

Bu muhafiz uretim boru hattinda SERT KAPI olarak kosuyor ve 2026-08-11'e kadar
HIC testi yoktu (envanterle olculdu). Ustelik muhafizin KENDI var olma sebebi
"etiket var, karsilik yok" oruntusudur: dogrulanmayan bir sema, sema degil
temennidir. Dogrulanmayan bir DOGRULAYICI da oyle.

Ayni disiplin test_verify_placement.py'den devralindi: her kural ONCE TEMIZ
halde gosterilir, SONRA kusuru enjekte edilir ve muhafizin dusmesi zorunlu
kilinir.

Fikstürler SENTETIKTIR: depodaki hicbir dosya okunmaz, hicbir dosya
degistirilmez. Sozlesme dosyasi (schemas/zones.schema.json) ise GERCEKTIR -
test edilen sey tam olarak o sozlesmenin uygulanip uygulanmadigidir.

Sozlesme (verify_zones.py docstring, son satir):
    0 GREEN · 1 ihlal var (RED) · 2 girdi okunamadi.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS  # noqa: E402

GUARD = BOARD_SCRIPTS / "verify_zones.py"

# --------------------------------------------------------------------------- #
# Sozlesmeye UYAN en kucuk belge. Her alan bir sebeple burada:
#   * id/kind/rule/src  -> semanin `required` listesi
#   * rect_mm           -> oneOf'un tam BIR dali (circle_mm ile birlikte olamaz)
#   * exclude_layers    -> escape_layer'in dependentRequired'i
#   * allowed_refs(_why)-> tutarlilik kuralinin (alt kume) temiz hali
CLEAN_DOC = {
    "$schema": "test/board_outline/v1",
    "zones": [
        {
            "id": "j1_escape",
            "kind": "escape_fanout",
            "rect_mm": {"x_mm": 3.0, "y_mm": 2.0, "w_mm": 4.0, "h_mm": 4.0},
            "exclude_layers": ["B.Cu"],
            "escape_layer": "B.Cu",
            "allowed_refs": ["J1"],
            "allowed_refs_why": {"J1": "konnektorun kendisi bandin icindedir"},
            "rule": "Bu alanda B.Cu yuzunde bilesen duramaz; kacis izleri orada kosar.",
            "src": "SENTETIK FIKSTUR - tests/test_verify_zones.py (gercek proje degil)",
        }
    ],
}


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


class VerifyZonesBase(unittest.TestCase):
    def run_guard(self, doc, extra: list[str] | None = None):
        """(cikis kodu, stdout). `doc` gecici bir dizine yazilir."""
        with tempfile.TemporaryDirectory() as td:
            outline = Path(td) / "board_outline.json"
            outline.write_text(json.dumps(doc, ensure_ascii=False),
                               encoding="utf-8")
            return _run([sys.executable, str(GUARD),
                         "--outline", str(outline)] + (extra or []))

    @staticmethod
    def mutate(**kw) -> dict:
        """Temiz belgenin ILK zone'unu degistirilmis bir KOPYASINI dondurur.

        Kopya sart: sozluk paylasilsa bir testin enjekte ettigi kusur bir
        sonrakine sizar ve hangi testin neyi kanitladigi belirsizlesir."""
        doc = copy.deepcopy(CLEAN_DOC)
        doc["zones"][0].update(kw)
        return doc


class TestCliContract(VerifyZonesBase):
    def test_help_exits_zero(self):
        rc, out = _run([sys.executable, str(GUARD), "--help"])
        self.assertEqual(rc, 0, f"--help calismiyor:\n{out}")
        self.assertIn("--outline", out)
        self.assertIn("--no-jsonschema", out)

    def test_missing_outline_returns_two_not_one(self):
        """'Dosyayi acamadim' ile 'ihlal buldum' AYNI SEY DEGILDIR."""
        rc, out = _run([sys.executable, str(GUARD),
                        "--outline", "yok-boyle-bir-dosya.json"])
        self.assertEqual(rc, 2, f"okunamayan girdi 2 disinda bir kod verdi:\n{out}")
        self.assertIn("girdi okunamadi", out)

    def test_missing_schema_file_returns_two(self):
        """Sozlesme okunamadiginda da 'dogrulanamadi' -> 2, sessiz PASS degil."""
        rc, out = self.run_guard(CLEAN_DOC,
                                 extra=["--schema", "yok-boyle-bir-sema.json"])
        self.assertEqual(rc, 2, f"eksik sema 2 disinda bir kod verdi:\n{out}")
        self.assertIn("girdi okunamadi", out)


class TestCleanBaseline(VerifyZonesBase):
    """Muhafiz ACILABILIYOR mu? Asla acilmayan bir kapi kapi degildir."""

    def test_clean_document_is_green(self):
        rc, out = self.run_guard(CLEAN_DOC)
        self.assertEqual(rc, 0, f"temiz fikstur RED verdi:\n{out}")
        self.assertIn("ZONE MUHAFIZI: GREEN", out)
        self.assertIn("yapi ihlali            : 0", out)
        self.assertIn("tutarlilik ihlali      : 0", out)

    def test_clean_document_actually_saw_the_zone(self):
        """Sifir zone okuyan bir GREEN, bos bir kosudur."""
        rc, out = self.run_guard(CLEAN_DOC)
        self.assertEqual(rc, 0)
        self.assertIn("zone sayisi            : 1", out)


class TestStructuralFaultInjection(VerifyZonesBase):
    """YAPI: schemas/zones.schema.json gercekten UYGULANIYOR mu?"""

    def test_kind_outside_the_closed_enum_is_caught(self):
        """`kind` KAPALI bir listedir. Listede olmayan bir tur yazilirsa zone
        tuketicilerde SESSIZCE gorevsizlesir - hicbir kontrol ona bakmaz."""
        rc0, _ = self.run_guard(CLEAN_DOC)
        self.assertEqual(rc0, 0, "temiz hal once GREEN olmali")

        rc, out = self.run_guard(self.mutate(kind="keepout_serbest_metin"))
        self.assertEqual(rc, 1, f"semada olmayan kind kabul edildi:\n{out}")
        self.assertIn("ZONE MUHAFIZI: RED", out)
        self.assertIn("$.zones[0].kind", out)

    def test_both_rect_and_circle_is_caught(self):
        """oneOf: geometri TAM OLARAK bir tane olmali. Ikisi birden yazilirsa
        hangi sinirin gecerli oldugu tuketiciye kalir."""
        rc, out = self.run_guard(
            self.mutate(circle_mm={"x_mm": 5.0, "y_mm": 5.0, "r_mm": 1.5}))
        self.assertEqual(rc, 1, f"rect_mm + circle_mm birlikte gecti:\n{out}")
        self.assertIn("ZONE MUHAFIZI: RED", out)

    def test_missing_required_src_is_caught(self):
        """Kaynaksiz limit = uydurma limit (deponun kaynak disiplini)."""
        doc = copy.deepcopy(CLEAN_DOC)
        del doc["zones"][0]["src"]
        rc, out = self.run_guard(doc)
        self.assertEqual(rc, 1, f"'src' olmadan gecti:\n{out}")
        self.assertIn("ZONE MUHAFIZI: RED", out)
        self.assertIn("src", out)

    def test_fallback_engine_catches_the_same_defect(self):
        """--no-jsonschema yedek motoru zorlar. Yedegin ayni kusuru yakalamasi
        gerekir; yakalamiyorsa jsonschema kurulu OLMAYAN her makinede bu kapi
        sessizce acik demektir."""
        rc, out = self.run_guard(self.mutate(kind="keepout_serbest_metin"),
                                 extra=["--no-jsonschema"])
        self.assertEqual(rc, 1, f"yedek motor kusuru kacirdi:\n{out}")
        self.assertIn("mini (yedek)", out, "gercekten yedek motor mu kostu?")
        self.assertIn("ZONE MUHAFIZI: RED", out)

    def test_fallback_engine_passes_the_clean_document(self):
        """AYRIMCILIK: yedek motor 'her seye hayir' diyen bir motor degil."""
        rc, out = self.run_guard(CLEAN_DOC, extra=["--no-jsonschema"])
        self.assertEqual(rc, 0, f"yedek motor temiz belgeyi reddetti:\n{out}")
        self.assertIn("mini (yedek)", out)
        self.assertIn("ZONE MUHAFIZI: GREEN", out)


class TestSemanticFaultInjection(VerifyZonesBase):
    """TUTARLILIK: JSON Schema'nin IFADE EDEMEDIGI, alanlar arasi kurallar."""

    def test_escape_layer_outside_exclude_layers_is_caught(self):
        """Kacis izi o katmanda kosuyorsa o katmanda bilesen de duramaz;
        aksi halde keepout OLU kalir."""
        rc, out = self.run_guard(self.mutate(escape_layer="F.Cu"))
        self.assertEqual(rc, 1, f"escape_layer exclude disinda kaldi ama gecti:\n{out}")
        self.assertIn("ZONE MUHAFIZI: RED", out)
        self.assertIn("tutarlilik ihlali      : 1", out)
        self.assertIn("keepout OLU kalir", out)

    def test_duplicate_zone_id_is_caught(self):
        """Iki zone ayni id'yi tasirsa ihlal mesajinin hangisine ait oldugu
        ayirt edilemez. Benzersizlik JSON Schema ile ifade EDILEMEZ."""
        doc = copy.deepcopy(CLEAN_DOC)
        doc["zones"].append(copy.deepcopy(doc["zones"][0]))
        rc, out = self.run_guard(doc)
        self.assertEqual(rc, 1, f"tekrar eden id gecti:\n{out}")
        self.assertIn("ZONE MUHAFIZI: RED", out)
        self.assertIn("j1_escape", out)

    def test_allowed_refs_why_for_an_absent_ref_is_caught(self):
        """Var olmayan bir izni savunan gerekce, izin ZANNI yaratir."""
        rc, out = self.run_guard(
            self.mutate(allowed_refs_why={"J1": "konnektor",
                                          "U9": "var olmayan bir izin"}))
        self.assertEqual(rc, 1, f"listede olmayan ref'e gerekce yazildi ama gecti:\n{out}")
        self.assertIn("ZONE MUHAFIZI: RED", out)
        self.assertIn("U9", out)


if __name__ == "__main__":
    unittest.main()
