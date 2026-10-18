#!/usr/bin/env python3
"""Parametrik kilif fabrikasi: uretici land pattern'indan .kicad_mod uretir.

NEDEN VAR: KiCad'in jenerik kutuphane ayak izleri paket ANAHATI standartlarindan
(JEDEC MO-178, IPC-7351) turetilmistir, ureticinin ONERDIGI land pattern'dan
degil. Ikisi ayni sey degildir ve olculdugunde ayrisiyorlar. 2026-08-10'da
cam-w'nin uc entegresi olculdu; ucunde de sapma bulundu. En keskin ornek
TPD4E02B04: TI, pin 3 ve 8'i (ikisi de GND, ESD desarj akiminin dondugu pedler)
iki kat genis cizmis; jenerik kutuphane tum pedleri esit yapiyor. Jenerik
kutuphane bu bilgiyi TASIYAMAZ, cunku o bilgi pakette degil PARCADA'dir.

MIMARI (deponun dort katmanina oturur):
  Katman 2  vendor_land_patterns.json   BEYAN - her sayi bir `src` tasir
  Katman 1  bu dosya                    URETEC - sayi uretmez, beyani cevirir
  Katman 3  --check                     MUHAFIZ - uretilen ile diskteki ayrisirsa RED
  Katman 4  tests/test_footprint_factory.py

BU MODUL SAYI URETMEZ. Icinde tek bir land pattern degeri yoktur; hepsi
JSON'dan gelir. Bir sayiyi buraya yazmak, onu kaynagindan koparmak olurdu -
duzeltilmek uzere oldugumuz hatanin ta kendisi.

    python footprint_factory.py --dict D.json --out DIR          # uret
    python footprint_factory.py --dict D.json --out DIR --check  # denetle

Cikis: 0 = GREEN, 1 = ayrisma/ihlal, 2 = girdi yok.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict

SCHEMA_VERSION = "footprint-factory/v1"

# KiCad 10 `(footprint ...)` bekler. Eski `(module ...)` + `(tedit ...)` KiCad 5
# bicimidir; okunabilir ama uretmek geriye dogru bir adim olurdu.
GENERATOR = "footprint_factory.py"


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — ajanın aracı çağırabilmesi için girdi/çıktı yapısı.
#
# NEDEN BURADA `...Output` YOK: bu araç JSON RAPOR YAZMAZ. Kardeş kart
# denetleyicilerinin aksine ne `--json` argümanı ne de `write_report()` çağrısı
# vardır; ürünü diske yazılan `.kicad_mod` DOSYALARIDIR (`generate()` ->
# dosya_adı -> dosya metni) ve `--check` kipinde stdout'a basılan GREEN/RED
# hükmüdür. Olmayan bir JSON'un anahtarlarını uydurmak yerine, modülün gerçekten
# ürettiği yapı — ped kaydı — aşağıda belgelenmiştir.
# --------------------------------------------------------------------------- #
class FootprintFactoryInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argüman. Kaynak: `main()` argparse bloğu (s.268-274)."""

    check: bool          # --check (store_true, vars. False): yazma; diskteki ile üretileni karşılaştır


class FootprintFactoryInput(FootprintFactoryInputOptional):
    """Aracın tam girdisi. Kaynak: `main()` argparse bloğu (s.268-274).

    İkisi de `required=True`: bu modül SAYI ÜRETMEZ, o yüzden sayıların geldiği
    sözlük (`dict`) olmadan çalışacak bir kip yoktur; `out` ise hem yazma hem
    `--check` kipinde karşılaştırma hedefidir.
    """

    dict: str            # --dict (zorunlu): vendor_land_patterns.json yolu
    out: str             # --out  (zorunlu): .pretty dizini


class FootprintFactoryPad(TypedDict):
    """`pads_from_declaration()`'ın ürettiği ped kaydı — modülün ÜRETTİĞİ tek
    makinece okunabilir yapı (kaynak: bu dosyada s.156-162, `build_footprint()`
    s.227-235 tarafından tüketilir).

    Konumlar TABLODA YAZMAZ, TÜRETİLİR: `x` sütundan (`col` L/R ile
    `row_center_to_center_x_mm`), `y` sıradan (`row` ile `pitch_y_mm`) çıkar.
    `sy` ped başına istisna (`pad_overrides`) varsa onu, yoksa `pad_y_mm`
    varsayılanını taşır — TPD4E02B04'ün iki kat geniş GND pedlerinin
    yaşayabildiği tek alan budur.

    ÇIKIŞ KODLARI (dosya başı notu): 0 = GREEN, 1 = ayrışma/ihlal
    (`--check` kipinde EKSIK/AYRISMA/BEYANSIZ ya da beyan hatası),
    2 = girdi yok (sözlük dosyası bulunamadı).
    """

    num: str             # pin numarası (beyandaki `pins[].num`, dize olarak)
    x: float             # mm — sütun merkezinden türetilmiş yatay konum
    y: float             # mm — sıra/adımdan türetilmiş dikey konum
    sx: float            # mm — ped genişliği (geometry.pad_x_mm)
    sy: float            # mm — ped yüksekliği (pad_overrides varsa o, yoksa geometry.pad_y_mm)


class FootprintBuildError(ValueError):
    """Beyan tutarsiz. Uretmeyi REDDEDER - yaklasik bir kilif uretmez."""


def _need(d: dict, key: str, where: str):
    if key not in d:
        raise FootprintBuildError(f"{where}: '{key}' beyan edilmemis")
    return d[key]


def pads_from_declaration(fp: dict) -> list[dict]:
    """Beyandan ped listesi. Konumlar TURETILIR, tabloda yazmaz.

    row/col disi hicbir sey kabul edilmez: bir pedin nerede oldugu, kac sira
    oldugu ve adimin ne oldugu bilgisinden CIKAR. Boylece adim degistiginde
    tum pedler birlikte kayar ve elle guncellenmesi gereken ikinci bir liste
    olusmaz.
    """
    name = fp.get("name", "?")
    g = _need(fp, "geometry", name)
    rows = int(_need(fp, "rows", name))
    pitch = float(_need(g, "pitch_y_mm", name))
    cc = float(_need(g, "row_center_to_center_x_mm", name))
    px = float(_need(g, "pad_x_mm", name))
    py_default = float(_need(g, "pad_y_mm", name))

    # Tek siralı bir parcada (iki uclu pasif) ADIM KAVRAMI YOKTUR. Alan yine de
    # zorunludur - eksik birakmak "unutuldu mu, yok mu" ayrimini siler - ama
    # degeri 0 OLMAK ZORUNDADIR. Aksi halde anlamsiz bir sayi dosyada sessizce
    # durur ve ileride rows buyudugunde yanlis geometri uretir.
    if rows == 1 and pitch != 0:
        raise FootprintBuildError(
            f"{name}: rows=1 iken pitch_y_mm 0 olmali (verilen {pitch}); "
            f"tek sirada adim tanimsizdir")
    if rows > 1 and pitch <= 0:
        raise FootprintBuildError(
            f"{name}: rows={rows} iken pitch_y_mm > 0 olmali (verilen {pitch})")

    # Ped basina istisnalar (orn. TPD4E02B04'te pin 3 ve 8 = GND, iki kat genis)
    override: dict[str, float] = {}
    for ov in fp.get("pad_overrides", []):
        if "pad_y_mm" not in ov:
            raise FootprintBuildError(f"{name}: pad_overrides girdisi pad_y_mm tasimiyor")
        if not ov.get("src"):
            raise FootprintBuildError(
                f"{name}: pad_overrides girdisi `src` tasimiyor - kaynaksiz sayi giremez")
        for pin in ov["pins"]:
            override[str(pin)] = float(ov["pad_y_mm"])

    # Sira 0 en USTTE. Ortalanmis dizilim: rows tek ise ortadaki y=0.
    y0 = -pitch * (rows - 1) / 2.0
    out = []
    seen = set()
    for p in _need(fp, "pins", name):
        num, col, row = str(p["num"]), p["col"], int(p["row"])
        if num in seen:
            raise FootprintBuildError(f"{name}: pin '{num}' iki kez beyan edilmis")
        seen.add(num)
        if row >= rows:
            raise FootprintBuildError(
                f"{name}: pin {num} row={row}, ama rows={rows} beyan edilmis")
        if col not in ("L", "R"):
            raise FootprintBuildError(f"{name}: pin {num} col='{col}' (L ya da R olmali)")
        out.append({
            "num": num,
            "x": (-cc / 2.0) if col == "L" else (cc / 2.0),
            "y": y0 + row * pitch,
            "sx": px,
            "sy": override.get(num, py_default),
        })
    return out


def courtyard_from(pads: list[dict], g: dict, clearance: float) -> tuple[float, float]:
    """Courtyard yari-boyutlari. ELLE VERILMEZ, TURETILIR.

    Elle verilen bir courtyard pedlerden KUCUK olabilir ve hicbir sey itiraz
    etmez; kart uzerinde bileseni pedlerinin ustune oturtan bir yerlesim
    dogar. Burada pedlerin gercek kapsami ile govde kapsaminin BUYUGU alinir.
    """
    pad_x = max(abs(p["x"]) + p["sx"] / 2.0 for p in pads)
    pad_y = max(abs(p["y"]) + p["sy"] / 2.0 for p in pads)
    body_x = float(g.get("body_x_mm", 0.0)) / 2.0
    body_y = float(g.get("body_y_mm", 0.0)) / 2.0
    return max(pad_x, body_x) + clearance, max(pad_y, body_y) + clearance


def build_footprint(fp: dict, common: dict) -> str:
    name = _need(fp, "name", "footprints[]")
    pads = pads_from_declaration(fp)
    g = fp["geometry"]
    clr = float(_need(common, "courtyard_clearance_mm", "common"))
    cx, cy = courtyard_from(pads, g, clr)
    rr = float(common.get("roundrect_rratio", 0.25))
    mask = common.get("mask_expansion_mm")

    # Ayak izinin KENDI icinde nereden geldigi yazili olmali: kutuphaneden tek
    # basina kopyalanip baska bir projeye tasindiginda kaynagi kaybolmasin.
    descr = (f"{fp.get('for_mpn', '')} — {fp.get('package', '')}. "
             f"URETICI land pattern: {fp.get('datasheet', '')} "
             f"cizim {fp.get('land_pattern_drawing', '')} "
             f"s.{fp.get('land_pattern_page', '?')}. "
             f"URETILDI: {GENERATOR} (elle duzenlemeyin). "
             f"Jenerik karsiligi: {fp.get('replaces_generic', '-')}")

    L: list[str] = []
    L.append(f'(footprint "{name}"')
    L.append('  (version 20240108)')
    L.append(f'  (generator "{GENERATOR}")')
    L.append('  (layer "F.Cu")')
    L.append(f'  (descr {json.dumps(descr, ensure_ascii=False)})')
    L.append(f'  (tags {json.dumps(fp.get("package", ""), ensure_ascii=False)})')
    L.append('  (attr smd)')
    L.append(f'  (property "Reference" "REF**" (at 0 {-(cy + 0.6):.3f} 0)')
    L.append('    (layer "F.SilkS")')
    L.append('    (effects (font (size 0.8 0.8) (thickness 0.15)))')
    L.append('  )')
    L.append(f'  (property "Value" "{name}" (at 0 {cy + 0.6:.3f} 0)')
    L.append('    (layer "F.Fab")')
    L.append('    (effects (font (size 0.8 0.8) (thickness 0.15)))')
    L.append('  )')

    ring = [(-cx, -cy), (cx, -cy), (cx, cy), (-cx, cy), (-cx, -cy)]
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        L.append(f'  (fp_line (start {x1:.3f} {y1:.3f}) (end {x2:.3f} {y2:.3f})')
        L.append('    (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))')

    bx, by = float(g.get("body_x_mm", 0)) / 2, float(g.get("body_y_mm", 0)) / 2
    if bx and by:
        body = [(-bx, -by), (bx, -by), (bx, by), (-bx, by), (-bx, -by)]
        for (x1, y1), (x2, y2) in zip(body, body[1:]):
            L.append(f'  (fp_line (start {x1:.3f} {y1:.3f}) (end {x2:.3f} {y2:.3f})')
            L.append('    (stroke (width 0.10) (type solid)) (layer "F.Fab"))')

    for p in pads:
        L.append(f'  (pad "{p["num"]}" smd roundrect')
        L.append(f'    (at {p["x"]:.4f} {p["y"]:.4f})')
        L.append(f'    (size {p["sx"]:.4f} {p["sy"]:.4f})')
        L.append('    (layers "F.Cu" "F.Paste" "F.Mask")')
        L.append(f'    (roundrect_rratio {rr})')
        if mask is not None:
            L.append(f'    (solder_mask_margin {float(mask):.3f})')
        L.append('  )')
    L.append(')')
    return "\n".join(L) + "\n"


def load_dictionary(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    d = json.loads(path.read_text(encoding="utf-8"))
    if not d.get("footprints"):
        raise FootprintBuildError(f"{path}: `footprints` bos ya da yok")
    return d


def generate(doc: dict) -> dict[str, str]:
    common = doc.get("common", {})
    out: dict[str, str] = {}
    for fp in doc["footprints"]:
        name = _need(fp, "name", "footprints[]")
        # `out` DOSYA ADIYLA anahtarlanir. Bu kontrol eskiden ciplak `name` ile
        # bakiyordu; anahtarlar hicbir zaman esleşmedigi icin koruma ATESLENMIYOR
        # ve ikinci kayit birincinin uzerine SESSIZCE yaziyordu (2026-08-10,
        # kusur enjeksiyon testi yakaladi). Ayni paketin iki ureticisi oldugu bir
        # sozlukte cift kayit BEKLENEN hata turudur; ustelik --check bunu GREEN
        # gorurdu, cunku diskteki dosya da ayni yanlis dosyadir.
        fn = f"{name}.kicad_mod"
        if fn in out:
            raise FootprintBuildError(f"'{name}' iki kez beyan edilmis")
        out[fn] = build_footprint(fp, common)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dict", required=True, help="vendor_land_patterns.json")
    ap.add_argument("--out", required=True, help=".pretty dizini")
    ap.add_argument("--check", action="store_true",
                    help="yazma; diskteki ile uretileni karsilastir, ayrisirsa 1 don")
    a = ap.parse_args(argv)

    try:
        doc = load_dictionary(Path(a.dict))
    except FileNotFoundError as e:
        print(f"sozluk yok: {e}", file=sys.stderr)
        return 2
    try:
        built = generate(doc)
    except FootprintBuildError as e:
        print(f"BEYAN HATASI: {e}", file=sys.stderr)
        return 1

    outdir = Path(a.out)
    print(f"[0] sozluk        : {a.dict}")
    print(f"[1] ayak izi      : {len(built)}")

    if a.check:
        problems: list[str] = []
        for fn, text in sorted(built.items()):
            p = outdir / fn
            if not p.is_file():
                problems.append(f"EKSIK:{fn}")
            elif p.read_text(encoding="utf-8") != text:
                problems.append(f"AYRISMA:{fn}")
        # Sozlukte olmayan ama dizinde duran dosya da bir sorundur: kimsenin
        # beyan etmedigi bir ayak izi karta girebilir.
        for p in sorted(outdir.glob("*.kicad_mod")):
            if p.name not in built:
                problems.append(f"BEYANSIZ:{p.name}")
        print(f"[2] sorun         : {len(problems)}")
        for x in problems:
            print(f"      {x}")
        print()
        print("KILIF FABRIKASI: " + ("GREEN" if not problems else "RED"))
        return 1 if problems else 0

    outdir.mkdir(parents=True, exist_ok=True)
    for fn, text in sorted(built.items()):
        (outdir / fn).write_text(text, encoding="utf-8", newline="\n")
        print(f"      yazildi {fn}")
    print()
    print("KILIF FABRIKASI: yazildi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
