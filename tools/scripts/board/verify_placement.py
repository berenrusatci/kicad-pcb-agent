#!/usr/bin/env python3
"""Yerleşim muhafızı: BEYAN ile GERÇEKLİK aynı mı?

Bu araç iki soruyu sorar ve ikisine de "evet" gerekir:

  1. `locked: true` beyan edilen her bileşen, karttaki konumunda **duruyor mu**?
  2. Hiçbir bileşen bir **zone**'u ihlal ediyor mu (keepout / koridor / keep-in)?

NEDEN VAR — ölçülmüş bir olay (2026-08-10, cam-w-og05b10):
`locked` bayrağı üretiliyordu (`gen_placement`), beyan ediliyordu
(`board_outline.json`), hatta karta da yazılıyordu (`fp.SetLocked(True)`) —
ama **hiçbir şey doğrulamıyordu**. `verify_pcb.py` içinde "locked" kelimesi
sıfır kez geçiyordu. Board her koşuda sıfırdan kurulduğu için `SetLocked`
yalnız KiCad GUI'sinde elle sürüklemeyi engelliyor; boru hattının kendisini
engellemiyor. Yani kilit bir **temenni**ydi.

Bir kilidin işe yaradığını, onu kırıp düşmesini görmeden iddia etme.

TASARIM: pcbnew İSTEMEZ. Board'u metin olarak okur (`common/sexpr` yoksa
düzenli ifadeyle). Sebep: pcbnew yalnız KiCad'in kendi yorumlayıcısında var
ve paket araçları başka bir Python'da koşuyor — muhafız o ayrımın yanlış
tarafında kalırsa hiç koşmaz.

Çıkış: 0 GREEN · 1 ihlal var (RED) · 2 girdi okunamadı.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import TypedDict

TOL_MM = 0.001          # beyan ile kart arasi kabul edilen sapma


class VerifyPlacementInputOptional(TypedDict, total=False):
    """Istege bagli girdiler (argparse, `main()` icindeki tanimlardan)."""
    bodies: str          # --bodies : {ref: [w_mm, h_mm]} courtyard kutulari


class VerifyPlacementInput(VerifyPlacementInputOptional):
    """`verify_placement.py` GIRDI sozlesmesi.

    Kaynak: bu dosyanin `main()` icindeki argparse blogu (uc zorunlu argüman).
    `bodies` VERILMEZSE `check_zones` her govdeyi 1.0 x 0.6 mm varsayar ve
    gercek govde buyukse zone ihlali KACAR - bu yuzden opsiyonel ama
    ONERILEN alandir (cam-w'de 3 ihlal tam bu yuzden gorunmuyordu).

    Cikis kodlari: 0 GREEN · 1 ihlal var (RED) · 2 girdi okunamadi.
    """
    board: str           # --board     : .kicad_pcb
    placement: str       # --placement : placement.json
    outline: str         # --outline   : board_outline.json (zones)


class VerifyPlacementOutput(TypedDict):
    """CIKTI sozlesmesi — bu arac JSON YAZMAZ, hukmunu stdout + cikis koduyla
    verir. Alanlar `main()`'in bastigi satirlarin makine karsiligidir; cagiran
    (`verify_pcb.run_guard`) hukmu CIKIS KODUNDAN okur, metinden degil."""
    locked_components: int    # "[1] kilitli bilesen"
    lock_violations: int      # "[2] kilit ihlali"
    zone_violations: int      # "[3] zone ihlali"
    verdict: str              # "YERLESIM MUHAFIZI: GREEN" | "... RED"


# --------------------------------------------------------------------------- #
def load_board_footprints(board_path: Path) -> dict[str, dict]:
    """{ref: {x, y, layer, locked}} — board dosyasindan, pcbnew olmadan."""
    text = board_path.read_text(encoding="utf-8")
    out: dict[str, dict] = {}
    starts = [m.start() for m in re.finditer(r'\(footprint "', text)] + [len(text)]
    for a, b in zip(starts, starts[1:]):
        blk = text[a:b]
        ref = re.search(r'\(property "Reference" "([^"]+)"', blk)
        at = re.search(r'\(at ([\d.\-]+) ([\d.\-]+)(?: ([\d.\-]+))?\)', blk)
        lay = re.search(r'\(layer "([^"]+)"\)', blk)
        if not (ref and at):
            continue
        out[ref.group(1)] = {
            "x": float(at.group(1)), "y": float(at.group(2)),
            "rot": float(at.group(3) or 0.0),
            "layer": lay.group(1) if lay else "?",
            "locked": "(locked yes)" in blk or "\n\t\t(locked)" in blk,
        }
    return out


def frame_transform(board_path: Path) -> tuple[float | None, float | None]:
    """(ox, cy) — cerceve koordinatini board koordinatina baglayan donusum.

    KARTIN KENDISINDEN olculur, tahmin edilmez: `Edge.Cuts` sinir kutusu.
    Cerceve sol-ALT koseden baslar ve +Y YUKARI'dir; KiCad +Y ASAGI'dir. Yani
      board_x = ox + frame_x
      board_y = cy - frame_y      (cy = Edge.Cuts'in EN BUYUK y'si)

    Neden dosyadan degil karttan: `board_outline.json`'daki `frame` blogu bugun
    yalnız METIN aciklama tasiyor ("origin": "sol-alt kose (0,0)"), sayisal
    ofset yok. Sayiyi uydurmaktansa gercekten cizilmis bakiri olcuyoruz.
    """
    text = board_path.read_text(encoding="utf-8")
    xs: list[float] = []
    ys: list[float] = []
    for m in re.finditer(
            r'\(gr_(?:line|rect|arc)\b[\s\S]{0,400}?\(layer "Edge\.Cuts"\)', text):
        for pm in re.finditer(r'\((?:start|end|mid|center) ([\d.\-]+) ([\d.\-]+)\)',
                              m.group(0)):
            xs.append(float(pm.group(1)))
            ys.append(float(pm.group(2)))
    if not xs:
        return (None, None)
    return (min(xs), max(ys))


def to_frame(x: float, y: float, ox: float, cy: float) -> tuple[float, float]:
    return (x - ox, cy - y)


# --------------------------------------------------------------------------- #
def check_locks(placement: dict, board: dict, ox, cy) -> list[str]:
    bad: list[str] = []
    for ref, c in sorted(placement.items()):
        if not c.get("locked"):
            continue
        fp = board.get(ref)
        if fp is None:
            bad.append(f"KILIT: {ref} 'locked' beyan edilmis ama KARTTA YOK")
            continue
        if ox is None:
            bad.append(f"KILIT: {ref} dogrulanamadi - cerceve donusumu okunamadi "
                       "(board_outline.frame.origin_x_mm/origin_y_mm/height_mm)")
            continue
        fx, fy = to_frame(fp["x"], fp["y"], ox, cy)
        dx, dy = fx - float(c["x_mm"]), fy - float(c["y_mm"])
        if math.hypot(dx, dy) > TOL_MM:
            bad.append(f"KILIT KIRILDI: {ref} beyan ({c['x_mm']}, {c['y_mm']}) "
                       f"kart ({fx:.3f}, {fy:.3f}) sapma {math.hypot(dx, dy):.3f} mm")
        if c.get("layer") and fp["layer"] != c["layer"]:
            bad.append(f"KILIT KIRILDI: {ref} katman beyan {c['layer']} "
                       f"kart {fp['layer']}")
    return bad


def check_zones(placement: dict, outline: dict, bodies: dict) -> list[str]:
    """Zone ihlalleri. Katman-farkinda: bir zone 'exclude_layers' verirse o
    yuzde istisna YOKTUR; karsi yuzde yalniz 'allowed_refs' durabilir."""
    bad: list[str] = []
    for z in outline.get("zones", []):
        rect = z.get("rect_mm")
        if not rect:
            continue                       # daire (montaj deligi) ayri kontrol
        x0, y0 = float(rect["x_mm"]), float(rect["y_mm"])
        x1, y1 = x0 + float(rect["w_mm"]), y0 + float(rect["h_mm"])
        allowed = set(z.get("allowed_refs", []))
        excl = set(z.get("exclude_layers", []))
        strict = z.get("kind") in ("high_speed_corridor", "escape_fanout")
        if not strict and not excl:
            continue
        for ref, c in sorted(placement.items()):
            w, h = bodies.get(ref, (1.0, 0.6))
            cx, cy_ = float(c["x_mm"]), float(c["y_mm"])
            if not (cx + w / 2 > x0 and cx - w / 2 < x1
                    and cy_ + h / 2 > y0 and cy_ - h / 2 < y1):
                continue
            layer = c.get("layer")
            if layer in excl:
                bad.append(f"ZONE: {ref} ({layer}) '{z.get('id')}' bolgesinin "
                           f"KACIS YUZUNDE - istisna yok")
            elif ref not in allowed:
                bad.append(f"ZONE: {ref} ({layer}) '{z.get('id')}' icinde ve "
                           f"allowed_refs'te BEYAN EDILMEMIS")
    return bad


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", required=True, help=".kicad_pcb")
    ap.add_argument("--placement", required=True, help="placement.json")
    ap.add_argument("--outline", required=True, help="board_outline.json (zones)")
    ap.add_argument("--bodies", help="{ref: [w_mm, h_mm]} JSON (courtyard kutulari)")
    a = ap.parse_args(argv)

    try:
        board = load_board_footprints(Path(a.board))
        placement = json.loads(Path(a.placement).read_text(encoding="utf-8"))["components"]
        outline = json.loads(Path(a.outline).read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        print(f"[yerlesim] girdi okunamadi: {exc!r}")
        return 2
    bodies = json.loads(Path(a.bodies).read_text(encoding="utf-8")) if a.bodies else {}
    bodies = {k: tuple(v) for k, v in bodies.items()}

    ox, cy = frame_transform(Path(a.board))
    if ox is None:
        print("[yerlesim] Edge.Cuts bulunamadi - cerceve donusumu olculemedi")
        return 2
    print(f"[0] cerceve donusumu (Edge.Cuts'tan): board = ({ox:.3f} + x, {cy:.3f} - y)")
    locks = check_locks(placement, board, ox, cy)
    zones = check_zones(placement, outline, bodies)

    n_locked = sum(1 for c in placement.values() if c.get("locked"))
    print(f"[1] kilitli bilesen        : {n_locked} / {len(placement)}")
    print(f"[2] kilit ihlali           : {len(locks)}")
    for b in locks[:12]:
        print("      ", b)
    print(f"[3] zone ihlali            : {len(zones)}")
    for b in zones[:12]:
        print("      ", b)

    if locks or zones:
        print("\nYERLESIM MUHAFIZI: RED")
        return 1
    print("\nYERLESIM MUHAFIZI: GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
