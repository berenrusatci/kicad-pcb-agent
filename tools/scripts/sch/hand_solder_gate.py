#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EL-LEHIM TEL KAPISI — "etiket, telin YERINE gecemez".

KURAL (el-lehim / perfboard / prototip sematikleri icin)
    Her sinyal, kaynagindan hedefine kadar KESINTISIZ, GORUNUR bir telle
    cizilmelidir. Etiket telin YANINDA aciklama olarak durabilir, YERINE
    gecemez. Istisna: cok noktali ortak dugumler (GND, VCC gibi) guc sembolu
    + kisa telle baglanabilir.

    Bu kapi FAB icin cizilen hiyerarsik/net-label'li sematiklere UYGULANMAZ.

IKI SEVIYE OLCUM — birincisi tek basina YETMEZ
    L1  Her etiketin koordinatinda gercek bir tel var mi?
        (etiket bir telin ucunda ya da uzerinde mi duruyor)

    L2  Her netin butun pinleri TEK bir tel-bagli bilesende mi?

    L1'i tek basina kapi yapmak bir tuzaktir: sayfanin iki ucuna, her biri
    kendi 2.54 mm'lik stub telinin ucunda duran IKI ayri "FSYNC" etiketi
    koyarsaniz L1 ikisini de gecirir - ama aralarinda tel YOKTUR. Netlist
    onlari ayni net sayar, KiCad memnundur, delikli karti lehimleyen kisi
    ise ekranda birlestirici hicbir sey gormez. Uctugu yer tam da burasi;
    o yuzden hukum L2'dedir.

BILESEN NASIL HESAPLANIR (KiCad baglanti kurallari)
    - Bir tel parcasinin iki ucu baglidir.
    - Bir ucun BASKA bir telin govdesine denk gelmesi baglar (T birlesimi).
    - Iki telin ORTADAN kesismesi (X) junction YOKSA BAGLAMAZ.
    - Bir pin, uzerinde durdugu telin govdesine baglidir.

CIKIS
    0 = GREEN    ihlal yok  (ya da kart el-lehim degil -> SKIPPED)
    2 = RED      en az bir ihlal
    3 = girdi eksik/okunamadi

Kullanim:
    python hand_solder_gate.py --path <kart_klasoru> [--json out.json]
                               [--power-nets GND,VCC] [--force] [--strict]

    --force   klasor adi el-lehim'e benzemese de kapiyi kosar
    --strict  guc-net istisnasini KAPATIR (raylar da tel ister)
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
AGENT = HERE.parents[2]                     # pcb-agent/tools
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(HERE.parent))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from common.netlist import read_nets  # noqa: E402
from common.sexpr import parse_sexpr, unquote  # noqa: E402
from sch_wire import Placed, load_lib_symbols  # noqa: E402

EPS = 0.01
HAND_SOLDER_HINTS = ("prototip", "prototype", "perfboard", "hand-solder",
                     "handsolder", "delikli")
LABEL_KINDS = ("label", "global_label", "hierarchical_label")
DEFAULT_POWER_NETS = ("GND", "GNDA", "AGND", "DGND", "VCC", "VDD", "+3V3",
                      "+5V", "+1V8")


# --------------------------------------------------------------------------- #
# Sematik okuma                                                                #
# --------------------------------------------------------------------------- #
def _f(v) -> float:
    return round(float(v), 2)


def read_schematic(path: Path) -> dict:
    """Tel / junction / etiket / pin koordinatlarini cikarir.

    Pin geometrisi YENIDEN YAZILMAZ: deponun kendi `Placed`/`load_lib_symbols`
    kodu kullanilir. Rotasyon+mirror sirasi burada tekrar cozulseydi, ayni
    hatanin iki ayri kopyasi olurdu ve biri duzeltilince oburu sessizce
    yanlis kalirdi.
    """
    text = path.read_text(encoding="utf-8")
    tree = parse_sexpr(text)
    libs = load_lib_symbols(str(path))          # gomulu lib_symbols

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    junctions: list[tuple[float, float]] = []
    labels: list[dict] = []
    pins: dict[tuple[str, str], tuple[float, float]] = {}
    symbols: list[dict] = []

    for node in tree:
        if not (isinstance(node, list) and node):
            continue
        head = node[0]

        if head == "wire":
            pts = []
            for child in node[1:]:
                if isinstance(child, list) and child and child[0] == "pts":
                    for xy in child[1:]:
                        if isinstance(xy, list) and xy and xy[0] == "xy":
                            pts.append((_f(xy[1]), _f(xy[2])))
            for a, b in zip(pts, pts[1:]):
                if a != b:
                    segments.append((a, b))

        elif head == "junction":
            for child in node[1:]:
                if isinstance(child, list) and child and child[0] == "at":
                    junctions.append((_f(child[1]), _f(child[2])))

        elif head in LABEL_KINDS:
            name = unquote(node[1]) if len(node) > 1 else "?"
            for child in node[2:]:
                if isinstance(child, list) and child and child[0] == "at":
                    labels.append({"kind": head, "text": name,
                                   "at": (_f(child[1]), _f(child[2]))})
                    break

        elif head == "symbol":
            lib_id = ref = None
            at = (0.0, 0.0, 0.0)
            mirror, unit = "", 1
            for child in node[1:]:
                if not isinstance(child, list) or not child:
                    continue
                if child[0] == "lib_id":
                    lib_id = unquote(child[1])
                elif child[0] == "at":
                    at = (float(child[1]), float(child[2]),
                          float(child[3]) if len(child) > 3 else 0.0)
                elif child[0] == "mirror":
                    mirror = str(child[1])
                elif child[0] == "unit":
                    unit = int(child[1])
                elif child[0] == "property" and len(child) > 2 \
                        and unquote(child[1]) == "Reference":
                    ref = unquote(child[2])
            if not (lib_id and ref and lib_id in libs):
                continue
            placed = Placed(ref, lib_id, at[0], at[1], at[2], mirror, unit,
                            libs[lib_id])
            symbols.append({"ref": ref, "lib_id": lib_id})
            for p in libs[lib_id].pins:
                if p.unit not in (0, unit):
                    continue
                pr = placed.pin(p.number)
                pins[(ref, p.number)] = (_f(pr.x), _f(pr.y))

    return {"segments": segments, "junctions": junctions, "labels": labels,
            "pins": pins, "symbols": symbols}


# --------------------------------------------------------------------------- #
# Tel grafi                                                                    #
# --------------------------------------------------------------------------- #
def _on_segment(p, a, b) -> bool:
    """p, a-b parcasinin UZERINDE mi (uclar dahil)?"""
    (px, py), (ax, ay), (bx, by) = p, a, b
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > EPS * max(1.0, math.hypot(bx - ax, by - ay)):
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -EPS:
        return False
    return dot <= (bx - ax) ** 2 + (by - ay) ** 2 + EPS


class Union:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def wire_components(sch: dict, extra_points: list) -> tuple[Union, set]:
    """Tel parcalarindan bilesen kumesi. Nokta -> kok temsilci."""
    u = Union()
    segs = sch["segments"]
    for a, b in segs:
        u.union(a, b)

    # Endpoint / pin / junction bir telin GOVDESINE denk geliyorsa baglanir.
    # (X kesismesi junction'siz baglanmaz - bu yuzden yalniz BU noktalar
    # taranir, segmentlerin birbiriyle kesisimi degil.)
    points = set()
    for a, b in segs:
        points.add(a)
        points.add(b)
    points.update(sch["junctions"])
    points.update(extra_points)

    for p in points:
        for a, b in segs:
            if p in (a, b):
                continue
            if _on_segment(p, a, b):
                u.union(p, a)
    return u, points


# --------------------------------------------------------------------------- #
# Kapi                                                                         #
# --------------------------------------------------------------------------- #
def evaluate(sch: dict, nets: dict, power_nets: set, strict: bool) -> dict:
    pin_at = sch["pins"]
    extra = list(pin_at.values()) + [l["at"] for l in sch["labels"]]
    u, _ = wire_components(sch, extra)

    def comp(p):
        return u.find(p) if p in u.parent else None

    # ---- L1: etiketin altinda tel var mi -------------------------------- #
    l1: list[dict] = []
    segs = sch["segments"]
    for lab in sch["labels"]:
        p = lab["at"]
        touching = any(p in (a, b) or _on_segment(p, a, b) for a, b in segs)
        if not touching:
            l1.append({"text": lab["text"], "kind": lab["kind"],
                       "at": list(p),
                       "why": "etiket havada: koordinatinda tel YOK"})

    # ---- L2: net tek bilesende mi --------------------------------------- #
    l2: list[dict] = []
    skipped: list[str] = []
    checked = 0
    for raw_name, nodes in sorted(nets.items()):
        name = raw_name.lstrip("/")
        if name.startswith("unconnected-"):
            continue
        if len(nodes) < 2:
            continue
        if not strict and name in power_nets:
            # ISTISNA: cok noktali ortak dugum. Kabul edilir ama SAYILIR -
            # sessiz bir muafiyet, muafiyet olmaktan cikip aliskanlik olur.
            skipped.append(f"{name} ({len(nodes)} dugum)")
            continue

        coords, missing = {}, []
        for ref, pin in nodes:
            p = pin_at.get((ref, pin))
            if p is None:
                missing.append(f"{ref}-{pin}")
                continue
            coords[f"{ref}-{pin}"] = p

        groups: dict = {}
        for node, p in coords.items():
            groups.setdefault(comp(p), []).append(node)

        checked += 1
        if len(groups) > 1:
            islands = [sorted(v) for v in groups.values()]
            islands.sort(key=lambda g: (-len(g), g[0]))
            l2.append({
                "net": name,
                "islands": len(groups),
                "detail": islands,
                "why": f"'{name}' {len(groups)} ayri tel adasina bolunmus; "
                       f"adalari birlestiren TEL YOK - yalnizca ortak etiket "
                       f"adi var. El-lehimde bu bir baglanti DEGILDIR.",
                "missing_pins": missing,
            })

    verdict = "RED" if (l1 or l2) else "GREEN"
    return {"verdict": verdict, "L1_floating_labels": l1,
            "L2_label_only_nets": l2, "nets_checked": checked,
            "power_nets_skipped": skipped,
            "counts": {"labels": len(sch["labels"]),
                       "wire_segments": len(sch["segments"]),
                       "junctions": len(sch["junctions"]),
                       "L1_violations": len(l1), "L2_violations": len(l2)}}


def is_hand_solder(board: Path) -> bool:
    low = str(board).lower()
    return any(h in low for h in HAND_SOLDER_HINTS)


def find_inputs(board: Path) -> tuple[list[Path], Path | None, list[Path]]:
    """Sematikler + OLCULECEK netlist + degerlendirilmeyen netlist adaylari.

    🔴 BURADA BIR KUSUR VARDI - 2026-08-12'de olculdu ve giderildi.
        Eski satir `nets[0] if nets else None` idi: `sorted()` ALFABETIK
        siralar, dolayisiyla secilen dosya "en yeni" degil "adi en onde olan"
        oluyordu. `tek-kamerali-prototip` klasorunde uc aday vardi:

            _netlist.net       11 Agu 15:43   (v1, silinmis tasarim)
            proto_netlist.net  11 Agu 15:45   (v1)
            v2_netlist.net     12 Agu 07:47   (GUNCEL)

        Alt cizgi harflerden once siralandigi icin kapi BIR GUN ONCEKI, artik
        var olmayan bir tasarimi olcup RED verdi - ihlal listesinde guncel
        sematikte HIC BULUNMAYAN 'VIN_RAW' ve 'VBUS_5V' netleri vardi.
        Diger bes kartta aday sayisi 1 oldugu icin hata orada isirmiyordu:
        yani kusur, GOZLE GORULMEDEN dogru cevap veren turden.

        Bir kapinin yanlis RED vermesi, GREEN vermesi kadar zararlidir: ikisi
        de hukmun olculen seyle iliskisini koparir.

    Artik en YENI netlist secilir ve gerisi cagirana bildirilir (bayatlik
    denetimi icin).
    """
    schs = sorted(p for p in board.glob("*.kicad_sch")
                  if "block_diagram" not in p.name)
    nets = sorted(board.glob("build/*netlist.net"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return schs, (nets[0] if nets else None), nets[1:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True, help="kart klasoru")
    ap.add_argument("--json", help="kanit dosyasi (varsayilan build/…)")
    ap.add_argument("--power-nets", default=",".join(DEFAULT_POWER_NETS))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="guc-net istisnasini kapat")
    args = ap.parse_args(argv)

    board = Path(args.path).resolve()
    if not board.is_dir():
        print(f"HATA: klasor yok: {board}", file=sys.stderr)
        return 3

    if not (args.force or is_hand_solder(board)):
        print(f"[el-lehim kapisi] SKIPPED - '{board.name}' el-lehim kartina "
              f"benzemiyor (fab sematiklerinde etiket mesrudur). "
              f"Zorlamak icin --force.")
        return 0

    schs, netlist, other_nets = find_inputs(board)
    if not schs:
        print(f"HATA: {board} icinde .kicad_sch yok", file=sys.stderr)
        return 3
    if netlist is None:
        print(f"HATA: {board}/build altinda *netlist.net yok "
              f"(once build_all.py kos)", file=sys.stderr)
        return 3

    # ---- BAYATLIK DENETIMI: olculen sey guncel mi ------------------------- #
    # Netlist sematikten eskiyse verilecek her hukum - GREEN de RED de - baska
    # bir tasarima aittir. Boyle bir durumda hukum vermek, olcmemekten daha
    # kotudur: cunku olculmus gibi gorunur. Kapi burada KOSULAMADI der (exit 3).
    newest_sch = max(schs, key=lambda p: p.stat().st_mtime)
    if netlist.stat().st_mtime < newest_sch.stat().st_mtime:
        print(f"HATA: netlist BAYAT - '{netlist.name}' "
              f"'{newest_sch.name}' dosyasindan eski.\n"
              f"      Kapi bayat bir netlist uzerinde hukum VERMEZ. "
              f"Once build_all.py kosun.", file=sys.stderr)
        return 3
    if other_nets:
        print(f"[el-lehim kapisi] not: build/ altinda {len(other_nets) + 1} "
              f"netlist var; en YENI olan olculuyor -> {netlist.name} "
              f"(digerleri: {', '.join(p.name for p in other_nets)})")

    # ---- SEMATIK <-> NETLIST ESLESMESI ------------------------------------ #
    # 🔴 IKINCI KUSUR (2026-08-12): kapi klasordeki BUTUN .kicad_sch'leri tek
    #    netlist'e karsi olcuyordu. `tek-kamerali-prototip` icinde v1 (artik
    #    superseded, tarihsel kayit olarak saklanan) sematik de duruyor ve o,
    #    v2'nin netlist'iyle olculunce anlamsiz ihlaller uretiyordu
    #    ("sematikte bulunamayan pin: TP7-1") - ve bunlar HUKME giriyordu.
    #    Bir sematigi BASKA bir tasarimin netlist'iyle olcmek, olcmemekten
    #    kotudur.
    # KiCad netlist'i kendi kaynagini yaziyor: (design (source "....kicad_sch")).
    # Yani eslesme TAHMIN EDILMEZ, OKUNUR.
    src = re.search(r'\(source\s+"([^"]+)"', netlist.read_text(
        encoding="utf-8", errors="replace"))
    if src:
        want = Path(src.group(1).replace("\\\\", "\\")).name
        paired = [p for p in schs if p.name == want]
        if paired:
            skipped = [p.name for p in schs if p.name != want]
            if skipped:
                print(f"[el-lehim kapisi] netlist kaynagi '{want}' -> yalniz o "
                      f"sematik olculuyor; ATLANAN: {', '.join(skipped)} "
                      f"(baska bir tasarima ait, bu netlist'le olculemez)")
            schs = paired
        else:
            print(f"[el-lehim kapisi] UYARI: netlist kaynagi '{want}' klasorde "
                  f"bulunamadi; butun sematikler olculuyor (hukum supheli).")

    power_nets = {n.strip() for n in args.power_nets.split(",") if n.strip()}
    nets = read_nets(netlist)

    reports = []
    worst = 0
    for sch_path in schs:
        sch = read_schematic(sch_path)
        rep = evaluate(sch, nets, power_nets, args.strict)
        rep["schematic"] = sch_path.name
        reports.append(rep)
        if rep["verdict"] == "RED":
            worst = 2

    out = Path(args.json) if args.json else board / "build" / "hand_solder_gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "$schema": "kaitek/hand-solder-gate/v1",
        "board": board.name,
        "rule": "El-lehim sematiginde etiket telin YERINE gecemez; her sinyal "
                "kaynagindan hedefine KESINTISIZ gorunur telle cizilir.",
        "strict": args.strict,
        "power_nets_exempt": sorted(power_nets) if not args.strict else [],
        "netlist": str(netlist.relative_to(board)),
        # Hangi adaylarin ELENDIGI de kanittir: "en yeniyi sectim" iddiasi,
        # neyin arasindan secildigi yazilmadan dogrulanamaz.
        "netlist_candidates_rejected": [str(p.relative_to(board))
                                        for p in other_nets],
        "verdict": "RED" if worst else "GREEN",
        "sheets": reports,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- rapor
    print("=" * 78)
    print(f"EL-LEHIM TEL KAPISI - {board.name}")
    print("  kural: etiket telin YERINE gecemez")
    print("=" * 78)
    for rep in reports:
        c = rep["counts"]
        print(f"  {rep['schematic']}: tel={c['wire_segments']} "
              f"junction={c['junctions']} etiket={c['labels']}  "
              f"net_denetlendi={rep['nets_checked']}")
        for v in rep["L1_floating_labels"]:
            print(f"    [L1] HAVADA ETIKET '{v['text']}' @ {v['at']}")
        for v in rep["L2_label_only_nets"]:
            print(f"    [L2] '{v['net']}' -> {v['islands']} ayri tel adasi, "
                  f"aralarinda TEL YOK")
            for isl in v["detail"]:
                print(f"           ada: {', '.join(isl)}")
            if v["missing_pins"]:
                print(f"           (sematikte bulunamayan pin: "
                      f"{', '.join(v['missing_pins'])})")
        if rep["power_nets_skipped"]:
            print(f"    istisna uygulandi (guc sembollu ortak dugum): "
                  f"{', '.join(rep['power_nets_skipped'])}")
        print(f"    -> {rep['verdict']}")
    print("-" * 78)
    print(f"  VERDICT: {'RED' if worst else 'GREEN'}   kanit -> {out}")
    print("=" * 78)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
