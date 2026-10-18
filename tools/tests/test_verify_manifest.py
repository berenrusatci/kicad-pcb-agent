#!/usr/bin/env python3
"""Tests for scripts/board/verify_manifest.py — manifest (SSOT) muhafizi.

Bu muhafiz uretim boru hattinda SERT KAPI olarak kosuyor ve 2026-08-11'e kadar
HIC testi yoktu (envanterle olculdu). Muhafizin kendi var olma sebebi olculmus
bir olaydir: turetilmis `netclasses.json` elle duzeltilmisti (CLK sinifina
XVCLK_OSC eklenmisti) ve bir sonraki uretimde SESSIZCE siliniyordu. Bir SSOT'un
tek kaynak oldugunu, turevinin ondan ayrismadigini GORMEDEN iddia etme - ayni
sey bu muhafiz icin de gecerlidir.

Ayni disiplin test_verify_placement.py'den devralindi: her kural ONCE TEMIZ
halde gosterilir, SONRA kusuru enjekte edilir ve muhafizin dusmesi zorunlu
kilinir.

Fikstürler SENTETIKTIR: depodaki hicbir dosya okunmaz, hicbir dosya
degistirilmez. Sozlesme dosyasi (schemas/design_manifest.schema.json) GERCEKTIR.

Sozlesme (verify_manifest.py docstring, son satir):
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

GUARD = BOARD_SCRIPTS / "verify_manifest.py"

_SRC = "SENTETIK FIKSTUR - tests/test_verify_manifest.py (gercek kart degil)"

# --------------------------------------------------------------------------- #
# Sozlesmeye UYAN en kucuk manifest. Ucu de kasitli:
#   * impedance.trace_width + gap == pair_pitch  -> tutarlilik kurali TEMIZ
#   * CSI2 sinifi impedance'tan TURETILMIS degerleri tasir -> ayrisma yok
#   * her sinif design_policy minimumlarinin USTUNDE
CLEAN_MANIFEST = {
    "$schema": "pcb-agent/design-manifest/v1",
    "project": "sentetik-fikstur",
    "date": "2026-08-11",
    "purpose": ("SENTETIK FIKSTUR. Muhafizin sozlesmesini sinamak icin "
                "uretildi; hicbir gercek karti tanimlamaz."),
    "consumers": ["tests/test_verify_manifest.py"],
    "derived_files": ["layout/netclasses.json"],
    "mechanical": {
        "board_width_x_mm": 30.0,
        "board_height_y_mm": 32.0,
        "corner_radius_mm": 1.0,
        "edge_clearance_ring_mm": 0.5,
        "courtyard_gap_mm": 0.15,
        "src": _SRC,
    },
    "stackup": {
        "board_thickness_mm": 1.6,
        "layer_count": 4,
        "soldermask_mil": 1.1,
        "copper_mil": 1.2,
        "ply_mil": {"1080": 2.7},
        "mil_to_mm": 0.0254,
        "outer_dielectric_plies": ["1080"],
        "prepreg_er": 4.0,
        "prepreg_df": 0.019,
        "core_er": 4.2,
        "core_df": 0.015,
        "src": _SRC,
    },
    "impedance": {
        "target_ohm": 100.0,
        "tolerance_pct": 10.0,
        "spec_frequency_hz": 1e9,
        "pair_pitch_mm": 0.5,
        "trace_width_mm": 0.235,
        "gap_mm": 0.265,
        "src": _SRC,
    },
    "power": {
        "delta_t_c": 10.0,
        "rho_cu_ohm_m": 1.72e-08,
        "ir_drop_probe": {"net": "VIN_3V3", "track_width_mm": 0.4,
                          "length_mm": 15.0},
        "rails": [{"net": "VIN_3V3", "current_a": 0.2, "src": _SRC}],
        "src": _SRC,
    },
    "netclass_policy": {
        "design_policy": {
            "min_track_mm": 0.1,
            "min_clearance_mm": 0.1,
            "min_via_diameter_mm": 0.45,
            "min_via_drill_mm": 0.25,
            "src": _SRC,
        },
        "classes": [
            {"name": "Default", "priority": 100, "track_width_mm": 0.2,
             "clearance_mm": 0.15, "via_diameter_mm": 0.45, "via_drill_mm": 0.25,
             "diff_pair_width_mm": 0.2, "diff_pair_gap_mm": 0.2,
             "net_patterns": []},
            # net_patterns_from: adlar BASKA bir sozlesmeden cozulur; genislik ve
            # bosluk ise impedance bolumunden TURETILIR (muhafiz esitligi sinar).
            {"name": "CSI2", "priority": 10, "track_width_mm": 0.235,
             "clearance_mm": 0.15, "via_diameter_mm": 0.45, "via_drill_mm": 0.25,
             "diff_pair_width_mm": 0.235, "diff_pair_gap_mm": 0.265,
             "net_patterns_from": "interface_contract"},
            {"name": "POWER", "priority": 30, "track_width_mm": 0.4,
             "clearance_mm": 0.2, "via_diameter_mm": 0.6, "via_drill_mm": 0.3,
             "diff_pair_width_mm": 0.4, "diff_pair_gap_mm": 0.2,
             "net_patterns": ["VIN_3V3"]},
        ],
    },
}

# Manifestle AYNI seyi soyleyen turev. `net_patterns_from` tasiyan sinifin
# adlari burada COZULMUS gorunur; muhafiz o alani bilerek karsilastirmaz.
CLEAN_NETCLASSES = {
    "classes": [
        {"name": "Default", "priority": 100, "track_width_mm": 0.2,
         "clearance_mm": 0.15, "via_diameter_mm": 0.45, "via_drill_mm": 0.25,
         "diff_pair_width_mm": 0.2, "diff_pair_gap_mm": 0.2, "net_patterns": []},
        {"name": "CSI2", "priority": 10, "track_width_mm": 0.235,
         "clearance_mm": 0.15, "via_diameter_mm": 0.45, "via_drill_mm": 0.25,
         "diff_pair_width_mm": 0.235, "diff_pair_gap_mm": 0.265,
         "net_patterns": ["CSI_D0_P", "CSI_D0_N"]},
        {"name": "POWER", "priority": 30, "track_width_mm": 0.4,
         "clearance_mm": 0.2, "via_diameter_mm": 0.6, "via_drill_mm": 0.3,
         "diff_pair_width_mm": 0.4, "diff_pair_gap_mm": 0.2,
         "net_patterns": ["VIN_3V3"]},
    ]
}

# Komsu beyan: ayni mekanik sayilari IKINCI bir dosyada tasir.
CLEAN_OUTLINE = {
    "outline": {"width_x_mm": 30.0, "height_y_mm": 32.0,
                "corner_radius_mm": 1.0, "edge_clearance_ring_mm": 0.5},
    "zones": [],
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


class VerifyManifestBase(unittest.TestCase):
    def run_guard(self, manifest=None, outline=None, netclasses=None,
                  extra: list[str] | None = None):
        """(cikis kodu, stdout). Verilmeyen yan dosya HIC gecilmez - muhafizin
        'kaynak verilmedi' dalini de olcebilmek icin."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            path = d / "design_manifest.json"
            path.write_text(json.dumps(CLEAN_MANIFEST if manifest is None
                                       else manifest, ensure_ascii=False),
                            encoding="utf-8")
            argv = [sys.executable, str(GUARD), "--manifest", str(path)]
            if outline is not None:
                p = d / "board_outline.json"
                p.write_text(json.dumps(outline), encoding="utf-8")
                argv += ["--outline", str(p)]
            if netclasses is not None:
                p = d / "netclasses.json"
                p.write_text(json.dumps(netclasses), encoding="utf-8")
                argv += ["--netclasses", str(p)]
            return _run(argv + (extra or []))

    @staticmethod
    def manifest_with(section: str, **kw) -> dict:
        """Temiz manifestin bir bolumunu degistirilmis KOPYASINI dondurur."""
        doc = copy.deepcopy(CLEAN_MANIFEST)
        doc[section].update(kw)
        return doc

    @staticmethod
    def netclasses_copy() -> dict:
        return copy.deepcopy(CLEAN_NETCLASSES)


class TestCliContract(VerifyManifestBase):
    def test_help_exits_zero(self):
        rc, out = _run([sys.executable, str(GUARD), "--help"])
        self.assertEqual(rc, 0, f"--help calismiyor:\n{out}")
        self.assertIn("--manifest", out)
        self.assertIn("--netclasses", out)

    def test_missing_manifest_returns_two_not_one(self):
        """'Dosyayi acamadim' ile 'ihlal buldum' AYNI SEY DEGILDIR."""
        rc, out = _run([sys.executable, str(GUARD),
                        "--manifest", "yok-boyle-bir-manifest.json"])
        self.assertEqual(rc, 2, f"okunamayan girdi 2 disinda bir kod verdi:\n{out}")
        self.assertIn("girdi okunamadi", out)

    def test_missing_side_file_returns_two_not_silent_skip(self):
        """Verilen ama acilamayan bir turev dosyasi ATLANMAZ.

        Atlansaydi, yolu yanlis yazilmis bir --netclasses cagrisi ayrisma
        kontrolunu sessizce KAPATIR ve kosu yine GREEN gorunurdu."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "design_manifest.json"
            path.write_text(json.dumps(CLEAN_MANIFEST), encoding="utf-8")
            rc, out = _run([sys.executable, str(GUARD),
                            "--manifest", str(path),
                            "--netclasses", str(Path(td) / "yok.json")])
        self.assertEqual(rc, 2, f"acilamayan turev 2 disinda bir kod verdi:\n{out}")
        self.assertIn("okunamadi", out)


class TestCleanBaseline(VerifyManifestBase):
    """Muhafiz ACILABILIYOR mu? Asla acilmayan bir kapi kapi degildir."""

    def test_clean_manifest_alone_is_green(self):
        rc, out = self.run_guard()
        self.assertEqual(rc, 0, f"temiz fikstur RED verdi:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: GREEN", out)
        self.assertIn("yapi ihlali            : 0", out)
        self.assertIn("tutarlilik ihlali      : 0", out)

    def test_clean_manifest_with_derivatives_is_green(self):
        rc, out = self.run_guard(outline=CLEAN_OUTLINE,
                                 netclasses=CLEAN_NETCLASSES)
        self.assertEqual(rc, 0, f"ayrismayan turevler RED verdi:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: GREEN", out)
        self.assertIn("AYRISMA (turev/komsu)  : 0", out)
        self.assertNotIn("kontrol YAPILMADI", out)

    def test_absent_derivatives_are_declared_not_claimed_as_passed(self):
        """Yapilmamis bir kontrol PASS diye SAYILMAZ: --outline/--netclasses
        verilmeden kosuldugunda muhafiz bunu ACIKCA yazar."""
        rc, out = self.run_guard()
        self.assertEqual(rc, 0)
        self.assertIn("kaynak verilmedi - kontrol YAPILMADI", out)


class TestStructuralFaultInjection(VerifyManifestBase):
    """YAPI: schemas/design_manifest.schema.json gercekten UYGULANIYOR mu?"""

    def test_rule_block_without_src_is_caught(self):
        """Kaynaksiz bir sayi bu depoda kural degil, tahmindir."""
        rc0, _ = self.run_guard()
        self.assertEqual(rc0, 0, "temiz hal once GREEN olmali")

        doc = copy.deepcopy(CLEAN_MANIFEST)
        del doc["mechanical"]["src"]
        rc, out = self.run_guard(doc)
        self.assertEqual(rc, 1, f"'src' olmadan gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("$.mechanical", out)

    def test_unknown_top_level_key_is_caught(self):
        """Kok nesne KAPALIDIR: yazim hatasi sessizce yok sayilmamali - yoksa
        'mechanicals' diye yazilmis bir blok hicbir yerde okunmaz."""
        doc = copy.deepcopy(CLEAN_MANIFEST)
        doc["mechanicals"] = doc["mechanical"]
        rc, out = self.run_guard(doc)
        self.assertEqual(rc, 1, f"sozlesmede olmayan kok alan kabul edildi:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)


class TestSemanticFaultInjection(VerifyManifestBase):
    """TUTARLILIK: JSON Schema'nin IFADE EDEMEDIGI, alanlar arasi kurallar."""

    def test_broken_width_gap_pitch_identity_is_caught(self):
        """Manifest W + S = pair_pitch esitligini GEREKCE olarak beyan eder
        (iz konnektor pedinden cikarken geometri degistirmesin). Esitlik
        bozulursa gerekce artik dogru degildir."""
        rc, out = self.run_guard(self.manifest_with("impedance",
                                                    pair_pitch_mm=0.6))
        self.assertEqual(rc, 1, f"W+S != pitch gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("empedans", out)

    def test_class_below_design_policy_minimum_is_caught(self):
        """design_policy MINIMUMDUR: hicbir sinif altina inemez."""
        doc = copy.deepcopy(CLEAN_MANIFEST)
        doc["netclass_policy"]["classes"][0]["track_width_mm"] = 0.05
        rc, out = self.run_guard(doc)
        self.assertEqual(rc, 1, f"minimumun altindaki sinif gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("design_policy.min_track_mm", out)

    def test_csi2_geometry_drifting_from_impedance_is_caught(self):
        """CSI2 sinifi empedans bolumunden TURETILIR; ayrisirsa hangi
        genislikle uretildigi belirsizlesir (biri .kicad_pro'ya, oteki rapora)."""
        doc = copy.deepcopy(CLEAN_MANIFEST)
        doc["netclass_policy"]["classes"][1]["track_width_mm"] = 0.30
        rc, out = self.run_guard(doc)
        self.assertEqual(rc, 1, f"CSI2 empedanstan ayristi ama gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("impedance.trace_width_mm", out)


class TestDriftFaultInjection(VerifyManifestBase):
    """AYRISMA: turev ve komsu beyanlar manifestle HALA ayni seyi mi soyluyor?"""

    def test_hand_edited_net_pattern_in_derived_file_is_caught(self):
        """Muhafizin var olma sebebi olan ta o olay: turetilmis dosyaya elle
        eklenen bir net adi, bir sonraki uretimde SESSIZCE silinir."""
        rc0, _ = self.run_guard(netclasses=CLEAN_NETCLASSES)
        self.assertEqual(rc0, 0, "ayrismayan turev once GREEN olmali")

        derived = self.netclasses_copy()
        derived["classes"][2]["net_patterns"].append("VIN_RAW_ELLE_EKLENDI")
        rc, out = self.run_guard(netclasses=derived)
        self.assertEqual(rc, 1, f"elle eklenmis net deseni gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("AYRISMA", out)
        self.assertIn("VIN_RAW_ELLE_EKLENDI", out)

    def test_class_missing_from_derived_file_is_caught(self):
        derived = self.netclasses_copy()
        derived["classes"] = [c for c in derived["classes"]
                              if c["name"] != "POWER"]
        rc, out = self.run_guard(netclasses=derived)
        self.assertEqual(rc, 1, f"turevden dusen sinif gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("'POWER'", out)

    def test_geometry_drift_in_derived_file_is_caught(self):
        derived = self.netclasses_copy()
        derived["classes"][2]["track_width_mm"] = 0.35
        rc, out = self.run_guard(netclasses=derived)
        self.assertEqual(rc, 1, f"turevde degismis genislik gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("track_width_mm manifest=0.4 turev=0.35", out)

    def test_edge_ring_drift_against_outline_is_caught(self):
        """Kenar halkasini uc tuketici okur ve iki ayri dosyada yazilidir;
        ikisi ayrisirsa hangisinin dogru oldugu BILINEMEZ."""
        outline = copy.deepcopy(CLEAN_OUTLINE)
        outline["outline"]["edge_clearance_ring_mm"] = 0.3
        rc, out = self.run_guard(outline=outline)
        self.assertEqual(rc, 1, f"kenar halkasi ayrismasi gecti:\n{out}")
        self.assertIn("MANIFEST MUHAFIZI: RED", out)
        self.assertIn("edge_clearance_ring_mm", out)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# IKI MOTOR, TEK SOZLESME (2026-08-11)
#
# `verify_manifest` dogrulamayi `verify_zones`'un motorundan alir ve o motor
# CIFTTIR: `jsonschema` kuruluysa o, degilse dosyadaki mini yorumlayici. Ikisi
# AYNI .json'u okur, dolayisiyla AYNI hukmu vermek ZORUNDADIR.
#
# NEDEN TEST GEREKIYOR - olculdu 2026-08-11: `design_manifest.schema.json`
# `minProperties` kullaniyordu; mini motor o anahtari TANIMIYORDU ve
# "YEDEK MOTOR EKSIK" diyerek HER manifesti reddediyordu. Yani `jsonschema`
# kurulu OLMAYAN bir makinede manifest kapisi HIC ACILAMIYORDU. Davranis
# fail-closed oldugu icin sessiz degildi, ama kapiyi kullanilamaz kiliyordu.
#
# Bu test o ayrismanin geri gelmesini engeller: iki motor hem TEMIZ hem
# BOZUK girdide ayni yone hukmetmeli.
# --------------------------------------------------------------------------- #
class BothEnginesAgree(VerifyManifestBase):

    def _both(self, manifest=None):
        rc_js, out_js = self.run_guard(manifest=manifest)
        rc_mini, out_mini = self.run_guard(manifest=manifest,
                                           extra=["--no-jsonschema"])
        self.assertIn("jsonschema", out_js,
                      "bu makinede jsonschema kurulu degil; karsilastirma anlamsiz")
        self.assertIn("mini", out_mini, "--no-jsonschema yedek motoru zorlamadi")
        return (rc_js, out_js), (rc_mini, out_mini)

    def test_clean_manifest_is_green_in_both_engines(self):
        (rc_js, _), (rc_mini, out_mini) = self._both()
        self.assertEqual(rc_js, 0, "jsonschema motoru temiz manifesti reddetti")
        self.assertEqual(
            rc_mini, 0,
            "YEDEK motor temiz manifesti REDDETTI - sema, mini motorun "
            "bilmedigi bir anahtar kullaniyor olabilir.\n" + out_mini)

    def test_broken_manifest_is_red_in_both_engines(self):
        doc = copy.deepcopy(CLEAN_MANIFEST)
        del doc["impedance"]["target_ohm"]          # zorunlu alan silindi
        (rc_js, _), (rc_mini, out_mini) = self._both(manifest=doc)
        self.assertEqual(rc_js, 1)
        self.assertEqual(rc_mini, 1,
                         "YEDEK motor bozuk manifesti KACIRDI:\n" + out_mini)
