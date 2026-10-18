#!/usr/bin/env python3
"""Manifest muhafızı: tasarım SSOT'u geçerli mi ve türevleri ondan mı geliyor?

`design_manifest.json` bir projenin donanım kurallarının TEK KAYNAĞIDIR. Bu araç
üç soruyu sorar ve üçüne de "evet" gerekir:

  1. **YAPI** — `schemas/design_manifest.schema.json`'a uyuyor mu? (biçim,
     zorunlu alanlar, her kural bloğunda `src`)
  2. **TUTARLILIK** — JSON Schema'nın İFADE EDEMEDİĞİ, alanlar arası kurallar:
     W + S = pad pitch, CSI2 sınıfının geometrisi empedans bölümüyle aynı mı,
     her sınıf `design_policy` minimumlarının üstünde mi, bir net iki sınıfta mı.
  3. **AYRIŞMA** — türetilmiş dosyalar (`netclasses.json`) ve komşu beyanlar
     (`board_outline.json`) manifestle hâlâ aynı şeyi mi söylüyor?

ÜÇÜNCÜSÜ NEDEN VAR — ölçülmüş bir olay (2026-08-11, cam-w-og05b10):
`layout/netclasses.json` üretilmiş bir dosyaydı ama elle düzeltilmişti — CLK
sınıfına `XVCLK_OSC` eklenmişti (W12 kararı). Üreteç o adı bilmiyordu; bir
sonraki koşuda **sessizce siliyordu**. Kimse fark etmedi çünkü hiçbir şey
"türev ile kaynak aynı şeyi söylüyor mu" diye sormuyordu. Bir SSOT'un tek
kaynak olduğunu, türevinin ondan ayrışamadığını görmeden iddia etme.

Aynı şey `edge_clearance_ring_mm` için de geçerli: kenar halkasını üç tüketici
okur (yerleştirme, bakır denetimi, mekanik beyan) ve iki ayrı dosyada yazılıdır.
İkisi ayrışırsa hangisinin doğru olduğu bilinemez — bu araç ayrışmayı RED sayar.

TASARIM: proje-agnostiktir. Yol bilmez, argümanla alır. `verify_zones.py`'nin
doğrulama motorunu YENİDEN KULLANIR (kopyalamaz): iki motor zamanla ayrışırdı.

Çıkış: 0 GREEN · 1 ihlal var (RED) · 2 girdi okunamadı.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_zones import structural_errors  # noqa: E402

DEFAULT_SCHEMA = (Path(__file__).resolve().parents[2] / "schemas"
                  / "design_manifest.schema.json")
EPS = 1e-9


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# --------------------------------------------------------------------------- #
def semantic_errors(m: dict) -> list[str]:
    """Şemanın ifade edemediği, alanlar ARASI kurallar."""
    bad: list[str] = []
    imp = m.get("impedance", {})
    nc = m.get("netclass_policy") or {}
    classes = nc.get("classes") or []
    pol = nc.get("design_policy") or {}

    # --- empedans geometrisi kendi gerekcesiyle tutarli mi ------------------ #
    w, s, pitch = imp.get("trace_width_mm"), imp.get("gap_mm"), imp.get("pair_pitch_mm")
    if all(_num(v) for v in (w, s, pitch)) and abs((w + s) - pitch) > 1e-6:
        bad.append(
            f"empedans: trace_width + gap = {w + s:.4f} mm, pair_pitch = {pitch} mm. "
            "Manifest bu esitligi GEREKCE olarak beyan ediyor (iz konnektor "
            "pedinden cikarken geometri degistirmesin); esitlik bozulmussa "
            "gerekce artik dogru degildir.")

    # --- sinif tablosu ------------------------------------------------------ #
    names = [c.get("name") for c in classes]
    for n in sorted({x for x in names if names.count(x) > 1}):
        bad.append(f"netclass: '{n}' adi birden fazla sinifta")
    prios = [c.get("priority") for c in classes]
    for p in sorted({x for x in prios if prios.count(x) > 1}, key=str):
        bad.append(f"netclass: priority {p} birden fazla sinifta - hangi kuralin "
                   "kazanacagi belirsiz")

    seen: dict[str, str] = {}
    for c in classes:
        for pat in c.get("net_patterns") or []:
            if pat in seen:
                bad.append(f"netclass: '{pat}' hem '{seen[pat]}' hem '{c.get('name')}' "
                           "sinifinda - bir net tek sinifa aittir")
            seen[pat] = c.get("name")

    # design_policy MINIMUMDUR: hicbir sinif altina inemez.
    checks = (("track_width_mm", "min_track_mm"),
              ("clearance_mm", "min_clearance_mm"),
              ("via_diameter_mm", "min_via_diameter_mm"),
              ("via_drill_mm", "min_via_drill_mm"))
    for c in classes:
        for field, minf in checks:
            v, lo = c.get(field), pol.get(minf)
            if _num(v) and _num(lo) and v + EPS < lo:
                bad.append(f"netclass '{c.get('name')}': {field}={v} mm, "
                           f"design_policy.{minf}={lo} mm ALTINDA")

    # CSI2 sinifi empedans bolumunden TURETILIR; ikisi ayrisirsa hangi genislikle
    # uretildigi belirsizlesir (biri .kicad_pro'ya, oteki rapora gider).
    for c in classes:
        if (c.get("net_patterns_from") == "interface_contract"
                and all(_num(v) for v in (w, s))):
            if _num(c.get("track_width_mm")) and abs(c["track_width_mm"] - w) > 1e-9:
                bad.append(f"netclass '{c.get('name')}': track_width_mm="
                           f"{c['track_width_mm']} != impedance.trace_width_mm={w}")
            if _num(c.get("diff_pair_gap_mm")) and abs(c["diff_pair_gap_mm"] - s) > 1e-9:
                bad.append(f"netclass '{c.get('name')}': diff_pair_gap_mm="
                           f"{c['diff_pair_gap_mm']} != impedance.gap_mm={s}")

    # --- guc ---------------------------------------------------------------- #
    rails = {r.get("net") for r in (m.get("power", {}).get("rails") or [])}
    probe = (m.get("power") or {}).get("ir_drop_probe")
    if probe and probe.get("net") not in rails:
        bad.append(f"power.ir_drop_probe.net='{probe.get('net')}' rails[] icinde YOK")

    # --- stackup ------------------------------------------------------------ #
    ply = (m.get("stackup") or {}).get("ply_mil") or {}
    for p in (m.get("stackup") or {}).get("outer_dielectric_plies") or []:
        if p not in ply:
            bad.append(f"stackup: outer_dielectric_plies '{p}' ply_mil icinde tanimsiz")

    # --- turetilmis dosyalar beyan edilmis mi -------------------------------- #
    if not m.get("derived_files"):
        bad.append("derived_files bos - turevi olmayan bir SSOT dekorasyondur")
    return bad


# --------------------------------------------------------------------------- #
def drift_errors(m: dict, outline: dict | None, netclasses: dict | None) -> list[str]:
    """Manifest ile komsu beyanlar / turevler AYRISMIS mi?"""
    bad: list[str] = []

    if outline is not None:
        mech = m.get("mechanical", {})
        o = outline.get("outline", {})
        for mk, ok in (("board_width_x_mm", "width_x_mm"),
                       ("board_height_y_mm", "height_y_mm"),
                       ("corner_radius_mm", "corner_radius_mm"),
                       ("edge_clearance_ring_mm", "edge_clearance_ring_mm")):
            a, b = mech.get(mk), o.get(ok)
            if _num(a) and _num(b) and abs(a - b) > 1e-9:
                bad.append(f"AYRISMA: manifest.mechanical.{mk}={a} != "
                           f"board_outline.outline.{ok}={b} - ayni kural iki "
                           "dosyada FARKLI yaziyor")

    if netclasses is not None:
        want = {c["name"]: c for c in (m.get("netclass_policy") or {}).get("classes") or []}
        have = {c["name"]: c for c in netclasses.get("classes") or []}
        for miss in sorted(set(want) - set(have)):
            bad.append(f"AYRISMA: '{miss}' sinifi manifestte var, turetilmis "
                       "netclasses.json'da YOK")
        for extra in sorted(set(have) - set(want)):
            bad.append(f"AYRISMA: '{extra}' sinifi netclasses.json'da var, "
                       "manifestte YOK - elle eklenmis ve ilk uretimde SILINIR")
        geo = ("track_width_mm", "clearance_mm", "via_diameter_mm", "via_drill_mm",
               "diff_pair_width_mm", "diff_pair_gap_mm", "priority")
        for name in sorted(set(want) & set(have)):
            for f in geo:
                a, b = want[name].get(f), have[name].get(f)
                if _num(a) and _num(b) and abs(a - b) > 1e-9:
                    bad.append(f"AYRISMA: '{name}'.{f} manifest={a} turev={b}")
            # `net_patterns_from` tasiyan sinifin adlari BASKA bir sozlesmeden
            # cozulur; burada karsilastirilamaz (ve karsilastirilmamali).
            if "net_patterns" in want[name]:
                wa = list(want[name]["net_patterns"])
                ha = list(have[name].get("net_patterns") or [])
                if wa != ha:
                    bad.append(f"AYRISMA: '{name}'.net_patterns manifest={wa} "
                               f"turev={ha} - turevdeki elle duzeltme bir sonraki "
                               "uretimde SESSIZCE silinir")
    return bad


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="design_manifest.json")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    ap.add_argument("--outline", help="board_outline.json (ayrisma kontrolu)")
    ap.add_argument("--netclasses", help="turetilmis netclasses.json (ayrisma kontrolu)")
    ap.add_argument("--no-jsonschema", action="store_true",
                    help="jsonschema kurulu olsa bile yedek motoru zorla")
    ap.add_argument("--max-print", type=int, default=25)
    a = ap.parse_args(argv)

    try:
        m = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
        schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        print(f"[manifest] girdi okunamadi: {exc!r}")
        return 2

    def _opt(p: str | None) -> dict | None:
        if not p:
            return None
        try:
            return json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as exc:                               # noqa: BLE001
            print(f"[manifest] '{p}' okunamadi: {exc!r}")
            raise SystemExit(2) from exc

    outline, netclasses = _opt(a.outline), _opt(a.netclasses)

    try:
        struct, engine = structural_errors(m, schema, not a.no_jsonschema)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[manifest] sozlesme yorumlanamadi: {exc!r}")
        return 2

    sem = semantic_errors(m)
    drift = drift_errors(m, outline, netclasses)

    n_rail = len((m.get("power") or {}).get("rails") or [])
    n_cls = len((m.get("netclass_policy") or {}).get("classes") or [])
    print(f"[0] sozlesme               : {Path(a.schema).name}  (motor: {engine})")
    print(f"[1] manifest               : {a.manifest}")
    print(f"[2] kapsam                 : {n_rail} guc rayi, {n_cls} netclass, "
          f"{len(m.get('derived_files') or [])} turetilmis dosya, "
          f"{len(m.get('consumers') or [])} tuketici")
    print(f"[3] yapi ihlali            : {len(struct)}")
    for x in struct[:a.max_print]:
        print("      ", x)
    print(f"[4] tutarlilik ihlali      : {len(sem)}")
    for x in sem[:a.max_print]:
        print("      ", x)
    print(f"[5] AYRISMA (turev/komsu)  : {len(drift)}"
          + ("" if (outline or netclasses) else "   [kaynak verilmedi - kontrol YAPILMADI]"))
    for x in drift[:a.max_print]:
        print("      ", x)

    if struct or sem or drift:
        print("\nMANIFEST MUHAFIZI: RED")
        return 1
    print("\nMANIFEST MUHAFIZI: GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
