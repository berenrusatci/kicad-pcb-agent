#!/usr/bin/env python3
"""Tests for scripts/board/footprint_factory.py — kılıf fabrikası muhafızı.

Her kural ÖNCE TEMİZ hâlde gösterilir, SONRA kusuru enjekte edilir ve
üretecin REDDETMESİ zorunlu kılınır. Yalnız geçebilen bir test hiçbir şey
kanıtlamaz: "muhafız çalışıyor" iddiası ancak muhafızı kırıp düştüğünü
görerek kurulabilir.

Bu dosyanın var olma sebebi iki ölçülmüş olaydır (2026-08-10, cam-w-og05b10):

  1. Jenerik KiCad ayak izleri paket ANAHATINDAN türetilmiş, üreticinin
     ÖNERDİĞİ land pattern'dan değil. Üç entegrenin üçünde de ayrışma
     bulundu. En keskini TPD4E02B04: TI pin 3 ve 8'i (ikisi de GND, ESD
     deşarj akımının döndüğü pedler) bilerek iki kat geniş çizmiş.
     `pad_overrides` mekanizması tam olarak bunun için vardır; çalıştığı
     kanıtlanmazsa o iki ped sessizce dar kalır.

  2. Courtyard'ın ilk taslağı ELLE veriliyordu ve pedlerin İÇİNDE kalıyordu
     (courtyard ±1.65, pedler ±1.95). Hiçbir şey itiraz etmemişti. Bugün
     courtyard türetiliyor; bu dosya onun pedleri her koşulda KAPSADIĞINI
     sınar — testlerin en önemlisi budur.

Fikstürlerin ezici çoğunluğu SENTETİKTİR: gerçek sözlük okunmaz, gerçek
`vendor.pretty/` dizinine dokunulmaz. Yalnızca sondaki KİLİT testleri gerçek
`vendor_land_patterns.json`'a bakar ve dosya yoksa `skipTest` eder.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import BOARD_SCRIPTS, REPO  # noqa: E402

import footprint_factory as ff  # noqa: E402

FACTORY = BOARD_SCRIPTS / "footprint_factory.py"
CAM_W_DICT = REPO.parent / "cam-w-og05b10" / "footprints" / "vendor_land_patterns.json"


# --------------------------------------------------------------------------- #
# Üretilen metni geri okuyan ayrıştırıcılar.
#
# Testler üretecin İÇ değişkenlerine değil, ÜRETTİĞİ DOSYAYA bakar. İç
# değişkene bakan bir test, dosyaya yanlış yazılsa bile geçerdi.
PAD_RE = re.compile(
    r'\(pad "([^"]+)" smd roundrect\s*\n'
    r'\s*\(at (-?[\d.]+) (-?[\d.]+)\)\s*\n'
    r'\s*\(size (-?[\d.]+) (-?[\d.]+)\)')

CRTYD_RE = re.compile(
    r'\(fp_line \(start (-?[\d.]+) (-?[\d.]+)\) \(end (-?[\d.]+) (-?[\d.]+)\)\s*\n'
    r'\s*\(stroke[^\n]*\(layer "F\.CrtYd"\)\)')


def parse_pads(text: str) -> dict[str, dict]:
    """`.kicad_mod` metninden ped numarası -> {x, y, sx, sy}."""
    out = {}
    for num, x, y, sx, sy in PAD_RE.findall(text):
        out[num] = {"x": float(x), "y": float(y),
                    "sx": float(sx), "sy": float(sy)}
    return out


def parse_courtyard(text: str) -> tuple[float, float]:
    """Çizilmiş F.CrtYd çizgilerinden courtyard yarı-boyutları."""
    xs, ys = [], []
    for x1, y1, x2, y2 in CRTYD_RE.findall(text):
        xs += [abs(float(x1)), abs(float(x2))]
        ys += [abs(float(y1)), abs(float(y2))]
    if not xs:
        raise AssertionError("uretilen kilifta hic F.CrtYd cizgisi yok")
    return max(xs), max(ys)


def pad_extent(pads: dict[str, dict]) -> tuple[float, float]:
    """Pedlerin bakır kapsamının gerçek yarı-boyutu (merkez + yarım ped)."""
    return (max(abs(p["x"]) + p["sx"] / 2.0 for p in pads.values()),
            max(abs(p["y"]) + p["sy"] / 2.0 for p in pads.values()))


def derived_pitch(pads: dict[str, dict]) -> float:
    """Ped y'lerinden geri hesaplanan adım (en küçük pozitif fark)."""
    ys = sorted({round(p["y"], 6) for p in pads.values()})
    return min(b - a for a, b in zip(ys, ys[1:]))


def derived_cc(pads: dict[str, dict]) -> float:
    """Ped x'lerinden geri hesaplanan sıra merkez-merkez açıklığı."""
    xs = sorted({round(p["x"], 6) for p in pads.values()})
    return xs[-1] - xs[0]


# --------------------------------------------------------------------------- #
# Sentetik sözlük. GERÇEK dosyadan hiçbir sayı kopyalanmadı; buradaki değerler
# uydurmadır ve bilerek yuvarlaktır ki aritmetik elle kontrol edilebilsin.
#   rows=3, pitch 1.00 -> y = -1.00, 0.00, +1.00
#   cc 3.00             -> x = -1.50 / +1.50
SYNTH = {
    "footprints": [
        {
            "name": "SENTETIK_6",
            "for_mpn": "YOK",
            "package": "TEST-6",
            "geometry": {
                "pitch_y_mm": 1.00,
                "row_center_to_center_x_mm": 3.00,
                "pad_x_mm": 1.20,
                "pad_y_mm": 0.60,
                "body_x_mm": 2.00,
                "body_y_mm": 2.00,
            },
            "pins": [
                {"num": "1", "col": "L", "row": 0},
                {"num": "2", "col": "L", "row": 1},
                {"num": "3", "col": "L", "row": 2},
                {"num": "4", "col": "R", "row": 2},
                {"num": "5", "col": "R", "row": 1},
                {"num": "6", "col": "R", "row": 0},
            ],
            "rows": 3,
        }
    ],
    "common": {"courtyard_clearance_mm": 0.25, "mask_expansion_mm": 0.07},
}

# İki sıralı ikinci fikstür: rows tek olduğunda ORTA sıra y=0'da kalır ve adım
# değişince kımıldamaz. Bu fikstürde her pedin y'si adımla birlikte oynamak
# ZORUNDA; "hepsi kaydı mı" sorusu ancak burada dürüstçe sorulabilir.
SYNTH2 = {
    "footprints": [
        {
            "name": "SENTETIK_4",
            "geometry": {
                "pitch_y_mm": 1.00,
                "row_center_to_center_x_mm": 2.00,
                "pad_x_mm": 0.80,
                "pad_y_mm": 0.40,
            },
            "pins": [
                {"num": "1", "col": "L", "row": 0},
                {"num": "2", "col": "L", "row": 1},
                {"num": "3", "col": "R", "row": 1},
                {"num": "4", "col": "R", "row": 0},
            ],
            "rows": 2,
        }
    ],
    "common": {"courtyard_clearance_mm": 0.25},
}


def doc(base: dict = SYNTH) -> dict:
    """Fikstürün derin kopyası. Testler birbirinin sözlüğünü kirletemez."""
    return json.loads(json.dumps(base))


class FactoryBase(unittest.TestCase):
    """Sözlükten tek ayak izi üretip metnini döndüren yardımcılar."""

    def build(self, d: dict) -> str:
        out = ff.generate(d)
        self.assertEqual(len(out), 1, "fikstur tek ayak izi uretmeli")
        return next(iter(out.values()))

    def pads_of(self, d: dict) -> dict[str, dict]:
        return parse_pads(self.build(d))

    def assertRefused(self, d: dict, needle: str | None = None):
        """Üretecin REDDETMESİ zorunlu. Sessizce yaklaşık kılıf üretemez."""
        with self.assertRaises(ff.FootprintBuildError) as cm:
            ff.generate(d)
        if needle:
            self.assertIn(needle, str(cm.exception))
        return str(cm.exception)


# --------------------------------------------------------------------------- #
class TestCleanBaseline(FactoryBase):
    """Üreteç AÇILABİLİYOR mu? Asla açılmayan bir kapı kapı değildir."""

    def test_clean_dictionary_builds(self):
        text = self.build(doc())
        pads = parse_pads(text)
        self.assertEqual(len(pads), 6, "6 pin beyan edildi, 6 ped bekleniyor")
        self.assertEqual(sorted(pads), ["1", "2", "3", "4", "5", "6"])

    def test_clean_dictionary_reproduces_hand_computed_geometry(self):
        """Aritmetik elle yapılabilmeli; üreteç sihir yapmıyor.

        rows=3, pitch=1.00 -> y0 = -1.00 ; cc=3.00 -> x = +-1.50."""
        pads = self.pads_of(doc())
        self.assertAlmostEqual(pads["1"]["x"], -1.50, places=4)
        self.assertAlmostEqual(pads["1"]["y"], -1.00, places=4)
        self.assertAlmostEqual(pads["2"]["y"], 0.00, places=4)
        self.assertAlmostEqual(pads["3"]["y"], +1.00, places=4)
        self.assertAlmostEqual(pads["6"]["x"], +1.50, places=4)
        self.assertAlmostEqual(pads["6"]["y"], -1.00, places=4)
        for p in pads.values():
            self.assertAlmostEqual(p["sx"], 1.20, places=4)
            self.assertAlmostEqual(p["sy"], 0.60, places=4)

    def test_footprint_names_its_own_source(self):
        """Kütüphaneden tek başına kopyalanan bir kılıf kaynağını taşımalı."""
        d = doc()
        d["footprints"][0]["datasheet"] = "SENTETIK DS-000"
        d["footprints"][0]["land_pattern_drawing"] = "CIZIM-000"
        text = self.build(d)
        self.assertIn("SENTETIK DS-000", text)
        self.assertIn("CIZIM-000", text)
        self.assertIn("footprint_factory.py", text)


# --------------------------------------------------------------------------- #
class TestPositionsAreDerivedNotCopied(FactoryBase):
    """Ped konumları gerçekten TÜRETİLİYOR mu?

    Bir parametre değiştiğinde çıktı değişmiyorsa, o parametre OKUNMUYOR
    demektir; tablodaki sayı dekoratif hâle gelir ve kimse fark etmez.
    """

    def test_pitch_change_moves_every_pad_in_y(self):
        a = self.pads_of(doc(SYNTH2))
        d = doc(SYNTH2)
        d["footprints"][0]["geometry"]["pitch_y_mm"] = 1.40
        b = self.pads_of(d)
        for num in a:
            self.assertNotAlmostEqual(
                a[num]["y"], b[num]["y"], places=4,
                msg=f"pin {num}: adim degisti ama y kimildamadi")
        # rows=2 -> aralik dogrudan adimin kendisi
        self.assertAlmostEqual(derived_pitch(b), 1.40, places=4)

    def test_pitch_change_scales_the_whole_column(self):
        """rows=3'te orta sıra y=0'da kalır; SPAN adımla ölçeklenmeli."""
        d = doc()
        d["footprints"][0]["geometry"]["pitch_y_mm"] = 0.50
        pads = self.pads_of(d)
        ys = sorted(p["y"] for p in pads.values())
        self.assertAlmostEqual(ys[-1] - ys[0], 1.00, places=4)  # 0.50 * (3-1)
        self.assertAlmostEqual(derived_pitch(pads), 0.50, places=4)

    def test_pitch_does_not_leak_into_x(self):
        """Eksenler karışmamalı: adım Y'nin işidir, X'e dokunmamalı."""
        a = self.pads_of(doc())
        d = doc()
        d["footprints"][0]["geometry"]["pitch_y_mm"] = 2.00
        b = self.pads_of(d)
        for num in a:
            self.assertAlmostEqual(a[num]["x"], b[num]["x"], places=4)

    def test_row_center_to_center_change_moves_every_pad_in_x(self):
        a = self.pads_of(doc())
        d = doc()
        d["footprints"][0]["geometry"]["row_center_to_center_x_mm"] = 4.00
        b = self.pads_of(d)
        for num in a:
            self.assertNotAlmostEqual(
                a[num]["x"], b[num]["x"], places=4,
                msg=f"pin {num}: c-c degisti ama x kimildamadi")
        self.assertAlmostEqual(derived_cc(b), 4.00, places=4)
        self.assertAlmostEqual(abs(b["1"]["x"]), 2.00, places=4)

    def test_row_center_to_center_does_not_leak_into_y(self):
        a = self.pads_of(doc())
        d = doc()
        d["footprints"][0]["geometry"]["row_center_to_center_x_mm"] = 5.00
        b = self.pads_of(d)
        for num in a:
            self.assertAlmostEqual(a[num]["y"], b[num]["y"], places=4)

    def test_columns_land_on_opposite_signs(self):
        """L solda (-x), R sağda (+x). Sütun bilgisi gerçekten kullanılmalı."""
        pads = self.pads_of(doc())
        self.assertLess(pads["1"]["x"], 0.0)   # col L
        self.assertGreater(pads["6"]["x"], 0.0)  # col R

    def test_pad_size_is_read_from_geometry(self):
        d = doc()
        d["footprints"][0]["geometry"]["pad_x_mm"] = 0.90
        d["footprints"][0]["geometry"]["pad_y_mm"] = 0.35
        for p in self.pads_of(d).values():
            self.assertAlmostEqual(p["sx"], 0.90, places=4)
            self.assertAlmostEqual(p["sy"], 0.35, places=4)


# --------------------------------------------------------------------------- #
class TestPadOverrides(FactoryBase):
    """`pad_overrides` — bu üretecin var olma sebebi.

    TPD4E02B04'te pin 3 ve 8 (ikisi de GND) iki kat geniştir. Bu mekanizma
    sessizce uygulanmazsa jenerik kütüphaneden farkımız kalmaz.
    """

    def _with_override(self, pins, pad_y=1.20, src="SENTETIK cizim"):
        d = doc()
        ov = {"pins": pins, "pad_y_mm": pad_y}
        if src is not None:
            ov["src"] = src
        d["footprints"][0]["pad_overrides"] = [ov]
        return d

    def test_override_widens_only_the_named_pins(self):
        base = self.pads_of(doc())
        pads = self.pads_of(self._with_override(["2", "5"]))
        self.assertAlmostEqual(pads["2"]["sy"], 1.20, places=4)
        self.assertAlmostEqual(pads["5"]["sy"], 1.20, places=4)
        for num in ("1", "3", "4", "6"):
            self.assertAlmostEqual(
                pads[num]["sy"], base[num]["sy"], places=4,
                msg=f"pin {num} adi gecmiyordu ama pedi degisti")

    def test_override_does_not_touch_pad_x_or_position(self):
        """İstisna yalnız `pad_y_mm`'dir; ped kaymamalı, X'i değişmemeli."""
        base = self.pads_of(doc())
        pads = self.pads_of(self._with_override(["2"]))
        self.assertAlmostEqual(pads["2"]["sx"], base["2"]["sx"], places=4)
        self.assertAlmostEqual(pads["2"]["x"], base["2"]["x"], places=4)
        self.assertAlmostEqual(pads["2"]["y"], base["2"]["y"], places=4)

    def test_override_without_src_is_refused(self):
        """Kaynaksız sayı giremez. Bir istisna, gerekçesiyle birlikte gelir."""
        self.assertRefused(self._with_override(["2"], src=None), "src")

    def test_override_with_empty_src_is_refused(self):
        """Boş bir `src` kaynak DEĞİLDİR; kuralı boşluğa kaçarak geçemez."""
        self.assertRefused(self._with_override(["2"], src=""), "src")

    def test_override_without_pad_y_is_refused(self):
        d = self._with_override(["2"])
        del d["footprints"][0]["pad_overrides"][0]["pad_y_mm"]
        self.assertRefused(d, "pad_y_mm")


# --------------------------------------------------------------------------- #
class TestRequiredFieldsAreRefusedWhenMissing(FactoryBase):
    """Zorunlu alan eksikse üreteç YAKLAŞIK bir kılıf uydurmamalı.

    Varsayılana düşen bir üreteç en tehlikelisidir: çıktı makul görünür,
    kimse bakmaz, kart yanlış basılır. Her alan için AYRI test var ki
    hangisinin korumasız kaldığı tek bakışta görülsün.
    """

    def _drop_geo(self, key):
        d = doc()
        del d["footprints"][0]["geometry"][key]
        return d

    def _drop_fp(self, key):
        d = doc()
        del d["footprints"][0][key]
        return d

    def test_missing_geometry_is_refused(self):
        self.assertRefused(self._drop_fp("geometry"), "geometry")

    def test_missing_pitch_y_is_refused(self):
        self.assertRefused(self._drop_geo("pitch_y_mm"), "pitch_y_mm")

    def test_missing_row_center_to_center_is_refused(self):
        self.assertRefused(self._drop_geo("row_center_to_center_x_mm"),
                           "row_center_to_center_x_mm")

    def test_missing_pad_x_is_refused(self):
        self.assertRefused(self._drop_geo("pad_x_mm"), "pad_x_mm")

    def test_missing_pad_y_is_refused(self):
        self.assertRefused(self._drop_geo("pad_y_mm"), "pad_y_mm")

    def test_missing_rows_is_refused(self):
        self.assertRefused(self._drop_fp("rows"), "rows")

    def test_missing_pins_is_refused(self):
        self.assertRefused(self._drop_fp("pins"), "pins")

    def test_missing_name_is_refused(self):
        self.assertRefused(self._drop_fp("name"), "name")

    def test_missing_courtyard_clearance_is_refused(self):
        """Boşluk payı da bir sayıdır; varsayılana düşmemeli."""
        d = doc()
        del d["common"]["courtyard_clearance_mm"]
        self.assertRefused(d, "courtyard_clearance_mm")


# --------------------------------------------------------------------------- #
class TestDeclarationConsistency(FactoryBase):
    """Tablonun kendi içinde çelişmesi sessizce geçmemeli."""

    def test_row_beyond_declared_rows_is_refused(self):
        """`rows` beyanı ile pin tablosu çelişiyorsa hangisi doğru?

        İkisi de olabilir; üreteç seçim yapmamalı, DURMALI. Aksi hâlde ped
        courtyard'ın dışına taşar ve merkezleme de bozulur."""
        d = doc()
        d["footprints"][0]["pins"][0]["row"] = 3  # rows = 3, gecerli 0..2
        msg = self.assertRefused(d, "rows=3")
        self.assertIn("row=3", msg)

    def test_row_within_declared_rows_is_accepted(self):
        """Ayrımcılık: sınırın içindeki satır reddedilmemeli."""
        d = doc()
        d["footprints"][0]["pins"][0]["row"] = 2
        self.assertEqual(len(self.pads_of(d)), 6)

    def test_unknown_column_is_refused(self):
        """L/R dışında bir sütunun X'i türetilemez; sessiz varsayılan yok."""
        d = doc()
        d["footprints"][0]["pins"][0]["col"] = "M"
        self.assertRefused(d, "col='M'")

    def test_lowercase_column_is_refused(self):
        """'l' ile 'L' farkı: tolere edilirse tablo iki dilli olur."""
        d = doc()
        d["footprints"][0]["pins"][0]["col"] = "l"
        self.assertRefused(d, "col='l'")

    def test_duplicate_pin_number_is_refused(self):
        """Aynı pin iki kez: biri sessizce diğerinin üstüne yazılırdı."""
        d = doc()
        d["footprints"][0]["pins"][1]["num"] = "1"
        self.assertRefused(d, "iki kez")

    def test_duplicate_footprint_name_is_refused(self):
        """Aynı ad iki kez beyan edilirse üreteç DURMALI.

        !!! BU TEST BUGÜN KIRMIZI. Kusur TESTTE DEĞİL, ÜRETEÇTE. !!!

        `generate()` içindeki nöbetçi `if name in out:` diye bakıyor, ama
        sözlüğe `out[f"{name}.kicad_mod"]` diye yazıyor. Anahtarlar hiçbir
        zaman aynı biçimde olmadığı için koşul ASLA sağlanamaz; nöbetçi
        ateşleyemez. 2026-08-10'da ölçülen davranış: iki kayıt da üretilir,
        İKİNCİSİ birincinin üzerine sessizce yazar, çıktı tek dosyadır ve
        hiçbir uyarı basılmaz.

        Sonucu somut: bir SOT-23-6 sözlükte iki kez beyan edilirse (ki aynı
        paketin iki üreticisi olduğu için bu tam da olması beklenen hata
        türüdür) kart yanlış ped boyuyla basılır — ve `--check` bunu GREEN
        görür, çünkü diskteki dosya da aynı yanlış dosyadır. Muhafız burada
        körleşiyor.

        Düzeltme tek satır: `if f"{name}.kicad_mod" in out:`. Bu test o satır
        düzeltilene kadar kırmızı kalmalıdır; yeşile boyamak açığı kapatmak
        değil gizlemek olurdu.
        """
        d = doc()
        dup = json.loads(json.dumps(d["footprints"][0]))
        dup["geometry"]["pad_x_mm"] = 9.99   # birinciden FARKLI bir kilif
        d["footprints"].append(dup)
        try:
            built = ff.generate(d)
        except ff.FootprintBuildError as e:
            self.assertIn("SENTETIK_6", str(e))
            self.assertIn("iki kez", str(e))
            return
        sizes = sorted({p["sx"] for p in
                        parse_pads(built["SENTETIK_6.kicad_mod"]).values()})
        self.fail(
            "CIFTE AD REDDEDILMEDI. generate() tek dosya uretti "
            f"({sorted(built)}) ve IKINCI beyan birincinin uzerine yazdi: "
            f"ped x = {sizes} (birinci beyan 1.20 diyordu). Kok neden: "
            "generate() `name in out` diye bakiyor, `out` anahtarlari ise "
            "`<name>.kicad_mod`. Nobetci hicbir zaman atesleyemez.")

    def test_two_distinct_names_are_accepted(self):
        """Ayrımcılık: farklı adlı iki kayıt sorun değil."""
        d = doc()
        second = json.loads(json.dumps(d["footprints"][0]))
        second["name"] = "SENTETIK_6B"
        d["footprints"].append(second)
        out = ff.generate(d)
        self.assertEqual(sorted(out),
                         ["SENTETIK_6.kicad_mod", "SENTETIK_6B.kicad_mod"])

    def test_empty_dictionary_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.json"
            p.write_text(json.dumps({"footprints": []}), encoding="utf-8")
            with self.assertRaises(ff.FootprintBuildError):
                ff.load_dictionary(p)


# --------------------------------------------------------------------------- #
class TestCourtyardCoversPads(FactoryBase):
    """EN ÖNEMLİ SINAV.

    İlk taslakta courtyard elle veriliyordu ve pedlerin İÇİNDE kalıyordu
    (courtyard ±1.65, pedler ±1.95). Hiçbir şey itiraz etmedi; kart üstünde
    komşu bileşeni pedlerin üstüne oturtan bir yerleşim doğardı. Aşağıdaki
    testler o hatanın bir daha KURULAMAZ olduğunu gösterir.
    """

    def test_courtyard_encloses_pads_in_the_clean_case(self):
        text = self.build(doc())
        cx, cy = parse_courtyard(text)
        ex, ey = pad_extent(parse_pads(text))
        self.assertGreater(cx, ex, "courtyard pedlerin X kapsamindan kucuk")
        self.assertGreater(cy, ey, "courtyard pedlerin Y kapsamindan kucuk")
        self.assertAlmostEqual(cx, ex + 0.25, places=4)

    def test_courtyard_encloses_pads_when_body_is_declared_absurdly_small(self):
        """Gövde 0.10 mm beyan edilse bile courtyard pedlerin DIŞINDA kalmalı.

        Gövdeye güvenen bir türetme burada düşerdi: pedler (X'te 2.10) gövdeden
        (0.05) çok daha geniş."""
        d = doc()
        d["footprints"][0]["geometry"]["body_x_mm"] = 0.10
        d["footprints"][0]["geometry"]["body_y_mm"] = 0.10
        text = self.build(d)
        cx, cy = parse_courtyard(text)
        ex, ey = pad_extent(parse_pads(text))
        self.assertGreater(cx, ex)
        self.assertGreater(cy, ey)
        self.assertAlmostEqual(cx, 2.10 + 0.25, places=4)  # 1.50 + 1.20/2

    def test_courtyard_survives_a_missing_body(self):
        """Gövde hiç beyan edilmese bile courtyard pedleri kapsamalı."""
        d = doc()
        del d["footprints"][0]["geometry"]["body_x_mm"]
        del d["footprints"][0]["geometry"]["body_y_mm"]
        text = self.build(d)
        cx, cy = parse_courtyard(text)
        ex, ey = pad_extent(parse_pads(text))
        self.assertGreater(cx, ex)
        self.assertGreater(cy, ey)

    def test_courtyard_grows_with_the_pads(self):
        """Ped büyütülünce courtyard ONUNLA BİRLİKTE büyümeli.

        Ayrışmanın klasik yolu budur: biri elle güncellenir, diğeri unutulur."""
        a = self.build(doc())
        d = doc()
        d["footprints"][0]["geometry"]["pad_x_mm"] = 3.00
        d["footprints"][0]["geometry"]["pad_y_mm"] = 2.00
        b = self.build(d)
        ax, ay = parse_courtyard(a)
        bx, by = parse_courtyard(b)
        self.assertGreater(bx, ax, "ped buyudu, courtyard buyumedi")
        self.assertGreater(by, ay, "ped buyudu, courtyard buyumedi")
        ex, ey = pad_extent(parse_pads(b))
        self.assertGreater(bx, ex)
        self.assertGreater(by, ey)

    def test_courtyard_grows_when_pads_move_apart(self):
        """Sıralar ayrılınca da courtyard takip etmeli, ped konumu da kapsamda."""
        ax, _ = parse_courtyard(self.build(doc()))
        d = doc()
        d["footprints"][0]["geometry"]["row_center_to_center_x_mm"] = 6.00
        text = self.build(d)
        bx, _ = parse_courtyard(text)
        self.assertGreater(bx, ax)
        self.assertGreater(bx, pad_extent(parse_pads(text))[0])

    def test_courtyard_covers_an_overridden_oversize_pad(self):
        """İstisna ile büyütülen ped courtyard'ın dışında kalmamalı.

        TPD4E02B04'te tam bu olur: pin 3 ve 8 iki kat geniştir. Courtyard
        yalnız varsayılan ped boyuna bakıyor olsaydı burada düşerdi."""
        d = doc()
        d["footprints"][0]["pad_overrides"] = [
            {"pins": ["2"], "pad_y_mm": 4.00, "src": "SENTETIK"}]
        text = self.build(d)
        cx, cy = parse_courtyard(text)
        ex, ey = pad_extent(parse_pads(text))
        self.assertGreater(cy, ey, "genisletilmis ped courtyard'i tasiyor")
        self.assertAlmostEqual(cy, 2.00 + 0.25, places=4)  # y=0, sy/2 = 2.00
        self.assertGreater(cx, ex)

    def test_courtyard_follows_the_body_when_the_body_is_the_larger(self):
        """Ayrımcılık: gövde pedlerden büyükse courtyard GÖVDEYİ kapsamalı."""
        d = doc()
        d["footprints"][0]["geometry"]["body_x_mm"] = 12.00
        text = self.build(d)
        cx, _ = parse_courtyard(text)
        self.assertAlmostEqual(cx, 6.00 + 0.25, places=4)
        self.assertGreater(cx, pad_extent(parse_pads(text))[0])

    def test_clearance_actually_moves_the_courtyard(self):
        """Boşluk payı okunmuyorsa değişince çıktı da değişmez."""
        ax, ay = parse_courtyard(self.build(doc()))
        d = doc()
        d["common"]["courtyard_clearance_mm"] = 0.75
        bx, by = parse_courtyard(self.build(d))
        self.assertAlmostEqual(bx - ax, 0.50, places=4)
        self.assertAlmostEqual(by - ay, 0.50, places=4)

    def test_there_is_no_api_to_hand_feed_a_courtyard(self):
        """Elle courtyard verilebilen bir yol KALMAMIŞ olmalı.

        Kural ancak ihlal edilemiyorsa kuraldır. `courtyard_from` yalnız
        pedleri, gövdeyi ve payı alır; dördüncü bir 'şunu yaz' parametresi
        yoktur."""
        params = list(inspect.signature(ff.courtyard_from).parameters)
        self.assertEqual(params, ["pads", "g", "clearance"])

    def test_hand_written_courtyard_keys_are_ignored(self):
        """Sözlüğe courtyard yazılmaya çalışılırsa çıktı DEĞİŞMEMELİ.

        Değişseydi, elle verilen (ve pedlerin içinde kalabilen) bir courtyard
        arka kapıdan geri gelmiş olurdu."""
        a = self.build(doc())
        d = doc()
        d["footprints"][0]["geometry"].update(
            {"courtyard_x_mm": 0.10, "courtyard_y_mm": 0.10})
        d["footprints"][0]["courtyard_mm"] = {"x": 0.10, "y": 0.10}
        d["common"]["courtyard_x_mm"] = 0.10
        self.assertEqual(self.build(d), a,
                         "elle courtyard beyani cikti_yi etkiledi")


# --------------------------------------------------------------------------- #
class TestOutputFormat(FactoryBase):
    """Üretilen metnin biçimi — sessizce geriye adım atılmasın."""

    def test_output_is_kicad_footprint_not_kicad5_module(self):
        """`(module ...)` KiCad 5 biçimidir; üretmek geriye adım olurdu."""
        text = self.build(doc())
        self.assertTrue(text.startswith("(footprint "),
                        f"ilk 40 karakter: {text[:40]!r}")
        self.assertNotIn("(module ", text)
        self.assertNotIn("(tedit ", text)

    def test_output_has_lf_line_endings_in_memory(self):
        """CRLF, Windows'ta üretilen dosyayı Linux'takinden AYIRIRDI ve
        `--check` her platformda farklı sonuç verirdi."""
        text = self.build(doc())
        self.assertNotIn("\r", text)
        self.assertTrue(text.endswith("\n"))

    def test_written_file_has_lf_line_endings_on_disk(self):
        """Bellek LF olsa bile Python metin modu diske CRLF yazabilir."""
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, doc())
            self.assertEqual(rc, 0, out)
            raw = (Path(td) / "out" / "SENTETIK_6.kicad_mod").read_bytes()
        self.assertNotIn(b"\r\n", raw, "diske CRLF yazildi")
        self.assertNotIn(b"\r", raw)

    def test_pad_layers_are_smd(self):
        text = self.build(doc())
        self.assertIn('(attr smd)', text)
        self.assertIn('(layers "F.Cu" "F.Paste" "F.Mask")', text)

    def test_mask_expansion_is_emitted_only_when_declared(self):
        self.assertIn("solder_mask_margin", self.build(doc()))
        d = doc()
        del d["common"]["mask_expansion_mm"]
        self.assertNotIn("solder_mask_margin", self.build(d))


# --------------------------------------------------------------------------- #
def run_factory(td: str, d: dict, check: bool = False):
    """Fabrikayı ALT SÜREÇ olarak koşar (gerçek CLI yolu, import değil)."""
    root = Path(td)
    dictp = root / "dict.json"
    if not dictp.is_file():
        dictp.write_text(json.dumps(d), encoding="utf-8")
    argv = [sys.executable, str(FACTORY), "--dict", str(dictp),
            "--out", str(root / "out")]
    if check:
        argv.append("--check")
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


class TestCheckGuard(unittest.TestCase):
    """`--check` — MUHAFIZ katmanı.

    Üretilenle diskteki ayrışırsa RED. Bu kapı yoksa `.kicad_mod` elle
    düzenlenebilir ve sözlük yalancı bir kaynak hâline gelir.
    """

    def test_check_is_green_right_after_generation(self):
        """Muhafız AÇILABİLİYOR mu?"""
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, doc())
            self.assertEqual(rc, 0, out)
            rc, out = run_factory(td, doc(), check=True)
        self.assertEqual(rc, 0, f"taze uretimden hemen sonra RED:\n{out}")
        self.assertIn("GREEN", out)
        self.assertIn("sorun         : 0", out)

    def test_hand_edited_footprint_is_caught_as_AYRISMA(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, doc())
            self.assertEqual(rc, 0, out)
            f = Path(td) / "out" / "SENTETIK_6.kicad_mod"
            # Tek bir pedin boyu elle buyutulur - "kucuk bir duzeltme".
            f.write_text(f.read_text(encoding="utf-8")
                         .replace("(size 1.2000 0.6000)",
                                  "(size 1.2000 0.9000)", 1),
                         encoding="utf-8", newline="\n")
            rc, out = run_factory(td, doc(), check=True)
        self.assertEqual(rc, 1, f"elle duzenlenen kilif gecti:\n{out}")
        self.assertIn("AYRISMA", out)
        self.assertIn("SENTETIK_6.kicad_mod", out)
        self.assertIn("RED", out)

    def test_deleted_footprint_is_caught_as_EKSIK(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, doc())
            self.assertEqual(rc, 0, out)
            (Path(td) / "out" / "SENTETIK_6.kicad_mod").unlink()
            rc, out = run_factory(td, doc(), check=True)
        self.assertEqual(rc, 1, f"silinen kilif gecti:\n{out}")
        self.assertIn("EKSIK", out)

    def test_undeclared_extra_footprint_is_caught_as_BEYANSIZ(self):
        """Kimsenin beyan etmediği bir ayak izi karta girebilir. RED."""
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, doc())
            self.assertEqual(rc, 0, out)
            (Path(td) / "out" / "KACAK.kicad_mod").write_text(
                '(footprint "KACAK")\n', encoding="utf-8", newline="\n")
            rc, out = run_factory(td, doc(), check=True)
        self.assertEqual(rc, 1, f"beyansiz kilif gecti:\n{out}")
        self.assertIn("BEYANSIZ", out)
        self.assertIn("KACAK.kicad_mod", out)

    def test_dictionary_change_alone_makes_check_red(self):
        """Sözlük değişip üretim koşulmazsa muhafız düşmeli.

        Kilidin yönü ikili: yalnız diski değil, BEYANI da izler."""
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, doc())
            self.assertEqual(rc, 0, out)
            d = doc()
            d["footprints"][0]["geometry"]["pitch_y_mm"] = 1.10
            (Path(td) / "dict.json").write_text(json.dumps(d), encoding="utf-8")
            rc, out = run_factory(td, d, check=True)
        self.assertEqual(rc, 1, f"sozluk degisti ama muhafiz gecti:\n{out}")
        self.assertIn("AYRISMA", out)

    def test_bad_declaration_returns_one_not_zero(self):
        """Üretilemeyen bir sözlük 'sorun yok' DEĞİLDİR."""
        d = doc()
        d["footprints"][0]["pins"][0]["col"] = "M"
        with tempfile.TemporaryDirectory() as td:
            rc, out = run_factory(td, d)
        self.assertEqual(rc, 1)
        self.assertIn("BEYAN HATASI", out)

    def test_missing_dictionary_returns_two(self):
        r = subprocess.run(
            [sys.executable, str(FACTORY), "--dict", "yok.json", "--out", "yok"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


# --------------------------------------------------------------------------- #
class CamWDictionaryReproducesDatasheetValues(unittest.TestCase):
    """KİLİT TESTİ — gerçek sözlük, datasheet sayıları.

    Genel üretecin doğru olması yetmez: cam-w'nin sözlüğüne yazılan sayıların
    da datasheet'te okunan land pattern'ı üretmesi gerekir. Biri diğerinden
    habersiz değiştirilirse bu test düşer.

    Buradaki sayılar üretecin çıktısından KOPYALANMADI; datasheet çizimlerinden
    (C04-2028A, 4214839/K, 4220328/A) gelir. Aksi hâlde test kendini onaylardı.
    """

    @classmethod
    def setUpClass(cls):
        if not CAM_W_DICT.is_file():
            raise unittest.SkipTest("cam-w vendor_land_patterns.json yok")
        cls.built = ff.generate(json.loads(CAM_W_DICT.read_text(encoding="utf-8")))

    def pads(self, name: str) -> dict[str, dict]:
        key = f"{name}.kicad_mod"
        self.assertIn(key, self.built, f"{name} sozlukte yok")
        return parse_pads(self.built[key])

    def test_microchip_sot23_6_matches_C04_2028A(self):
        pads = self.pads("Microchip_SOT-23-6_OT")
        self.assertEqual(len(pads), 6)
        self.assertAlmostEqual(derived_cc(pads), 2.80, places=4)
        self.assertAlmostEqual(derived_pitch(pads), 0.95, places=4)
        for num, p in pads.items():
            self.assertAlmostEqual(p["sx"], 1.10, places=4, msg=f"pin {num}")
            self.assertAlmostEqual(p["sy"], 0.60, places=4, msg=f"pin {num}")

    def test_ti_sot23_5_matches_4214839K(self):
        """SOT-23-5, Microchip'in SOT-23-6'sıyla AYNI paket standardına dayanır
        ama c-c 2.60'tır. İkisi paylaşılan tek bir tanıma indirgenemez."""
        pads = self.pads("TI_SOT-23-5_DBV")
        self.assertEqual(len(pads), 5)
        self.assertAlmostEqual(derived_cc(pads), 2.60, places=4)
        self.assertAlmostEqual(derived_pitch(pads), 0.95, places=4)
        for num, p in pads.items():
            self.assertAlmostEqual(p["sx"], 1.10, places=4, msg=f"pin {num}")
            self.assertAlmostEqual(p["sy"], 0.60, places=4, msg=f"pin {num}")

    def test_ti_sot23_5_right_column_middle_seat_is_empty(self):
        """SOT-23-5'te 5 pin vardır, 6 değil: sağ sütunun ortası BOŞ."""
        pads = self.pads("TI_SOT-23-5_DBV")
        right = sorted(round(p["y"], 4) for p in pads.values() if p["x"] > 0)
        self.assertEqual(right, [-0.95, 0.95])

    def test_ti_uson10_matches_4220328A_including_the_wide_gnd_pads(self):
        """Bu üretecin var olma sebebinin TEK CÜMLELİK kanıtı.

        Pin 3 ve pin 8 ikisi de GND'dir; TI onları bilerek iki kat geniş
        çizmiş (0.40 vs 0.20). Jenerik kütüphane hepsini eşit yapar."""
        pads = self.pads("TI_USON-10_DQA")
        self.assertEqual(len(pads), 10)
        self.assertAlmostEqual(derived_cc(pads), 0.835, places=4)
        self.assertAlmostEqual(derived_pitch(pads), 0.50, places=4)
        for num, p in pads.items():
            self.assertAlmostEqual(p["sx"], 0.565, places=4, msg=f"pin {num}")
        for num, p in pads.items():
            expect = 0.40 if num in ("3", "8") else 0.20
            self.assertAlmostEqual(
                p["sy"], expect, places=4,
                msg=f"pin {num}: sy={p['sy']} beklenen {expect}")

    def test_ti_uson10_wide_pads_are_exactly_the_two_gnd_pins(self):
        """'İki kat geniş' istisnası TAM OLARAK iki pede uygulanmalı.

        Fazlası da eksiği kadar kötüdür: istisna sızarsa ped komşusuna
        yaklaşır."""
        pads = self.pads("TI_USON-10_DQA")
        wide = {n for n, p in pads.items() if p["sy"] > 0.30}
        self.assertEqual(wide, {"3", "8"})

    def test_every_real_courtyard_encloses_its_pads(self):
        """Gerçek üç kılıfın üçünde de courtyard pedlerin DIŞINDA olmalı."""
        for fn, text in self.built.items():
            with self.subTest(fn=fn):
                cx, cy = parse_courtyard(text)
                ex, ey = pad_extent(parse_pads(text))
                self.assertGreater(cx, ex, f"{fn}: courtyard X pedleri kesiyor")
                self.assertGreater(cy, ey, f"{fn}: courtyard Y pedleri kesiyor")

    def test_every_real_footprint_is_kicad_format_and_lf(self):
        for fn, text in self.built.items():
            with self.subTest(fn=fn):
                self.assertTrue(text.startswith("(footprint "))
                self.assertNotIn("(module ", text)
                self.assertNotIn("\r", text)

    def test_disk_matches_the_declaration(self):
        """`vendor.pretty/` diskte sözlükle AYNI mı? (salt okuma; hiçbir şey
        yazılmaz — ayrışma varsa bu bir bulgudur, testin kusuru değil.)"""
        pretty = CAM_W_DICT.parent / "vendor.pretty"
        if not pretty.is_dir():
            self.skipTest("vendor.pretty yok")
        for fn, text in self.built.items():
            with self.subTest(fn=fn):
                p = pretty / fn
                self.assertTrue(p.is_file(), f"{fn} diskte yok")
                self.assertEqual(p.read_text(encoding="utf-8"), text,
                                 f"{fn}: diskteki ile uretilen AYRISIYOR")


if __name__ == "__main__":
    unittest.main()
