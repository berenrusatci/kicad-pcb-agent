#!/usr/bin/env python3
"""Yönlendirme muhafızı: BEYAN edilen boşluklar bakırda GERÇEKTEN var mı?

Bu araç yönlendiricinin **kendi engel haritasına sormadığı** üç soruyu sorar.
Üçü de "beyan var, uygulama yok" örüntüsündedir:

  1. **Via bakır açıklığı** — via, iz DEĞİLDİR. 0.4 mm izin yarıçapı 0.20,
     0.45 mm via'nınki 0.225'tir. İz genişliğine göre kurulmuş tek bir engel
     haritası via'yı 0.025 mm dar geçirir ve kimse itiraz etmez.
  2. **Via delik açıklığı** — deliğin bakırdan AYRI bir kuralı vardır
     (`min_hole_clearance`), netclass açıklığından türetilmez. Matkap
     katmanları delip geçer: yalnız via'nın kendi katmanlarına değil, HER
     bakır katmana bakar.
  3. **Kart kenarı halkası** — `board_outline.json` bir halka BEYAN eder
     (`edge_clearance_ring_mm`); yönlendirici o dosyayı hiç açmıyorsa halka
     bir temennidir.

Ayrıca **delik-delik** (`min_hole_to_hole`) mesafesi ölçülür: iki via'nın
bakırı yeterince uzakken matkapları birbirine girebilir; bu, bakır kuralından
bağımsız bir FABRİKASYON sınırıdır.

NEDEN VAR — ölçülmüş bir olay (2026-08-10, cam-w-og05b10):
maze yönlendirici tek bir engel haritası kuruyordu (`clearance + width/2`) ve
yol katman değiştirdiğinde o hücreye via bırakıyordu. Via daha geniştir ve
deliğinin kendi kuralı vardır; ikisi de sınanmıyordu. Aynı gün KiCad DRC o
karttan 63 `hole_clearance`, 3 `hole_to_hole` ve 21 `copper_edge_clearance`
ihlali çıkardı — hepsi maze ile yönlendirilmiş netlerin via'larından.
`board_outline.json` ise 0.5 mm'lik bir kenar halkası beyan ediyordu ve
yönlendiricinin kaynak ağacında o anahtar hiç okunmuyordu.

TASARIM: pcbnew İSTEMEZ. Board METİN olarak okunur (`common/sexpr`).
`verify_placement.py` ile aynı gerekçe: pcbnew yalnız KiCad'in kendi
yorumlayıcısında vardır, paket araçları başka bir Python'da koşar, ve o
ayrımın yanlış tarafında kalan muhafız hiç koşmaz.

KURAL KAYNAĞI UYDURULMAZ. Halka önce `board_outline.json`
(`outline.edge_clearance_ring_mm`), sonra `.kicad_pro`
(`min_copper_edge_clearance`) içinde aranır; ikisi de yoksa sonuç
NO_COVERAGE'dır ve NO_COVERAGE bir PASS DEĞİLDİR (çıkış 1).

Çıkış: 0 GREEN · 1 ihlal veya NO_COVERAGE (RED) · 2 girdi okunamadı.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.sexpr import parse_sexpr, unquote  # noqa: E402


class RouteAuditInputOptional(TypedDict, total=False):
    """Istege bagli girdiler (argparse, `main()` icindeki tanimlardan)."""
    project: str          # --project       : .kicad_pro (varsayilan: board'un kardesi)
    outline: str          # --outline       : board_outline.json (kenar halkasi beyani)
    netclasses: str       # --netclasses    : layout/netclasses.json (capraz kontrol)
    edge_ring_mm: float   # --edge-ring-mm  : halkayi ELLE ez
    check: bool           # --check         : kapi modu (varsayilan davranisla ayni)
    report_only: bool     # --report-only   : ihlalde bile 0 don - KAPI DEGILDIR
    max_list: int         # --max-list      : ekrana yazilan ihlal sayisi (12)
    json: str             # --json          : makine raporunun yazilacagi dosya


class RouteAuditInput(RouteAuditInputOptional):
    """`route_audit.py` GIRDI sozlesmesi (kaynak: `main()` argparse blogu).

    DIKKAT - `report_only` KAPIYI SUSTURUR: ihlal bulunsa bile 0 doner.
    Boru hattindan cagiran hicbir yer bunu GECIRMEMELIDIR (`verify_pcb.py`
    baglayicisi bilerek gecirmez).

    Cikis kodlari: 0 GREEN · 1 ihlal VEYA NO_COVERAGE (RED) · 2 girdi okunamadi.
    "Olcemedim" bir PASS DEGILDIR: kural kaynagi bulunamazsa da 1 doner.
    """
    board: str            # --board (ZORUNLU) : .kicad_pcb


class RouteAuditViolation(TypedDict):
    """Tek bir ihlal kaydi — `--json` raporundaki `violations[]` ogesi."""
    rule: str             # via_copper_clearance | hole_clearance | hole_to_hole
                          # | copper_edge_clearance
    at: list              # [x_mm, y_mm]
    net: str
    against: str          # karsi bakirin turu ("iz iz B.Cu", "ped C3001.1", ...)
    against_net: str
    uuid: str
    against_uuid: str


class RouteAuditOutput(TypedDict):
    """CIKTI sozlesmesi — `--json` ile yazilan raporun ust duzey anahtarlari
    (kaynak: `main()` icindeki `Path(a.json).write_text(json.dumps({...}))`)."""
    schema_version: str   # "route-audit/v1"
    board: str
    scanned: dict         # {via, iz, ped, zone_fill, delik}
    rules: dict           # edge_ring_mm + kaynagi + design_settings kurallari
    uncovered: list       # olculemeyen kontrol adlari -> NO_COVERAGE
    counts: dict          # kural -> ihlal sayisi
    violations: list      # RouteAuditViolation[]
    status: str           # "GREEN" | "RED"

# KiCad'in DRC'si delik-delik kuralını `m_drcEpsilon` kadar gevşetir. Sayı
# UYDURULMADI: cam-w'nin 2026-08-10 DRC raporunda `min_hole_to_hole` 0.25 mm
# iken ihlal metni "min 0.2495 mm" diyor -> epsilon = 0.0005 mm. Yalnız
# delik-delik sağlayıcısında görülüyor; bakır ve kenar kuralları tam değerle
# raporlanıyor, o yüzden epsilon yalnız oraya uygulanır.
DRC_EPSILON_MM = 0.0005

# KiCad koordinatlari DOSYADA nanometre cozunurlugundadir. 1e-6 mm'den kucuk
# bir fark kartta TEMSIL EDILEMEZ; onu ihlal saymak kayan nokta gurultusunu
# rapor etmektir. Olculdu (2026-08-10, cam-w): kenar halkasi 0.3 mm iken uc iz
# tam 0.3 mm'de duruyordu ve mesafe 0.29999999999999993 hesaplaniyordu - KiCad
# uc temiz derken bu arac uc ihlal diyordu. TOLERANS bir kural GEVSETMESI
# DEGILDIR: kuralin kendisi degismez, yalnizca sayinin cozunurlugu kabul edilir.
TOL_MM = 1e-6

ARC_STEP_MM = 0.1          # Edge.Cuts yayları bu adımla poligona indirgenir
GRID_MM = 1.0              # kenar hattı uzamsal indeks hücresi


# --------------------------------------------------------------------------- #
# S-expression yardımcıları
# --------------------------------------------------------------------------- #
def kids(node, tag) -> list:
    return [n for n in node if isinstance(n, list) and n and n[0] == tag]


def first(node, tag):
    k = kids(node, tag)
    return k[0] if k else None


def fnum(node, tag, index: int = 1, default=None):
    k = first(node, tag)
    if k is None or len(k) <= index:
        return default
    try:
        return float(k[index])
    except (TypeError, ValueError):
        return default


def fstr(node, tag, index: int = 1, default="") -> str:
    k = first(node, tag)
    if k is None or len(k) <= index:
        return default
    return unquote(str(k[index]))


# --------------------------------------------------------------------------- #
# 2-B geometri. Her şekil iki soruyu cevaplar: bir NOKTAYA ve bir DOĞRU
# PARÇASINA olan (katı gövde dahil) mesafesi. İkisi de yeterlidir:
# via/delik bir noktadır, kart kenarı doğru parçalarından oluşur.
# --------------------------------------------------------------------------- #
def seg_point_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-18:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _ccw(a, b, c) -> float:
    return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])


def seg_seg_dist(p1, p2, p3, p4) -> float:
    d1, d2 = _ccw(p3, p4, p1), _ccw(p3, p4, p2)
    d3, d4 = _ccw(p1, p2, p3), _ccw(p1, p2, p4)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return 0.0
    return min(seg_point_dist(p1, p3, p4), seg_point_dist(p2, p3, p4),
               seg_point_dist(p3, p1, p2), seg_point_dist(p4, p1, p2))


def point_in_polygon(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-15) + x1:
            inside = not inside
    return inside


class Circle:
    __slots__ = ("cx", "cy", "r")

    def __init__(self, cx, cy, r):
        self.cx, self.cy, self.r = cx, cy, r

    def bbox(self):
        return (self.cx - self.r, self.cy - self.r,
                self.cx + self.r, self.cy + self.r)

    def dist_point(self, p) -> float:
        return math.hypot(p[0] - self.cx, p[1] - self.cy) - self.r

    def dist_seg(self, a, b) -> float:
        return seg_point_dist((self.cx, self.cy), a, b) - self.r


class Capsule:
    """İz: kalınlığı olan doğru parçası (uçlar yuvarlak - KiCad varsayılanı)."""
    __slots__ = ("a", "b", "r")

    def __init__(self, a, b, r):
        self.a, self.b, self.r = a, b, r

    def bbox(self):
        return (min(self.a[0], self.b[0]) - self.r, min(self.a[1], self.b[1]) - self.r,
                max(self.a[0], self.b[0]) + self.r, max(self.a[1], self.b[1]) + self.r)

    def dist_point(self, p) -> float:
        return seg_point_dist(p, self.a, self.b) - self.r

    def dist_seg(self, a, b) -> float:
        return seg_seg_dist(self.a, self.b, a, b) - self.r


class RRect:
    """Yuvarlatılmış köşeli dikdörtgen: rect/roundrect/oval/circle pedlerin
    hepsini tek gövdeyle taşır (r=0 dikdörtgen, r=hw=hh daire)."""
    __slots__ = ("cx", "cy", "hw", "hh", "r", "cos", "sin")

    def __init__(self, cx, cy, w, h, r, ang_deg):
        self.cx, self.cy = cx, cy
        self.r = min(r, w / 2.0, h / 2.0)
        self.hw = max(0.0, w / 2.0 - self.r)
        self.hh = max(0.0, h / 2.0 - self.r)
        a = math.radians(ang_deg)
        self.cos, self.sin = math.cos(a), math.sin(a)

    def _local(self, p):
        """Dünya -> ped çerçevesi. `_rotate`'in TERSİ olmak ZORUNDA: ileri
        dönüşümü buraya da yazmak, kare olmayan bir pedi -2a kadar döndürür
        ve mesafeyi sessizce yanlış ölçer."""
        dx, dy = p[0] - self.cx, p[1] - self.cy
        return (dx * self.cos - dy * self.sin, dx * self.sin + dy * self.cos)

    def bbox(self):
        ext = math.hypot(self.hw + self.r, self.hh + self.r)
        return (self.cx - ext, self.cy - ext, self.cx + ext, self.cy + ext)

    def dist_point(self, p) -> float:
        x, y = self._local(p)
        dx = max(0.0, abs(x) - self.hw)
        dy = max(0.0, abs(y) - self.hh)
        return math.hypot(dx, dy) - self.r

    def dist_seg(self, a, b) -> float:
        la, lb = self._local(a), self._local(b)
        if (abs(la[0]) <= self.hw and abs(la[1]) <= self.hh) or \
           (abs(lb[0]) <= self.hw and abs(lb[1]) <= self.hh):
            return -self.r
        hw, hh = self.hw, self.hh
        corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
        best = min(seg_seg_dist(la, lb, corners[i], corners[(i + 1) % 4])
                   for i in range(4))
        return best - self.r


class Poly:
    """Zone dolgusu (filled_polygon)."""
    __slots__ = ("pts", "_bbox")

    def __init__(self, pts):
        self.pts = pts
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self._bbox = (min(xs), min(ys), max(xs), max(ys))

    def bbox(self):
        return self._bbox

    def dist_point(self, p) -> float:
        x0, y0, x1, y1 = self._bbox
        if point_in_polygon(p[0], p[1], self.pts):
            return 0.0
        if p[0] < x0 or p[0] > x1 or p[1] < y0 or p[1] > y1:
            # kutu dışındaysa kutuya olan mesafe alt sınırdır; yine de kesin
            # cevabı kenarlardan alıyoruz (poligon sayısı azdır)
            pass
        n = len(self.pts)
        return min(seg_point_dist(p, self.pts[i], self.pts[(i + 1) % n])
                   for i in range(n))

    def dist_seg(self, a, b) -> float:
        if point_in_polygon(a[0], a[1], self.pts) or \
           point_in_polygon(b[0], b[1], self.pts):
            return 0.0
        n = len(self.pts)
        return min(seg_seg_dist(a, b, self.pts[i], self.pts[(i + 1) % n])
                   for i in range(n))


class SegmentIndex:
    """Kart kenarı doğru parçaları üzerinde tekdüze ızgara.

    Zone dolgusunun on binlerce köşesini kenar hattına karşı ölçmek indekssiz
    kaba kuvvette dakikalar sürüyordu; ızgara sorguyu O(1)'e indirir ve
    CEVABI DEĞİŞTİRMEZ (aday süzgeci, yaklaşıklama değil)."""

    def __init__(self, segments, cell: float = GRID_MM):
        self.segments = segments
        self.cell = cell
        self.grid: dict[tuple[int, int], list[int]] = {}
        for idx, (a, b) in enumerate(segments):
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for gx in range(int(math.floor(x0 / cell)), int(math.floor(x1 / cell)) + 1):
                for gy in range(int(math.floor(y0 / cell)),
                                int(math.floor(y1 / cell)) + 1):
                    self.grid.setdefault((gx, gy), []).append(idx)

    def _candidates(self, x0, y0, x1, y1, reach):
        cell = self.cell
        out: set[int] = set()
        gx0 = int(math.floor((x0 - reach) / cell))
        gx1 = int(math.floor((x1 + reach) / cell))
        gy0 = int(math.floor((y0 - reach) / cell))
        gy1 = int(math.floor((y1 + reach) / cell))
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                bucket = self.grid.get((gx, gy))
                if bucket:
                    out.update(bucket)
        return out

    def dist_to_shape(self, shape, reach: float) -> float:
        """`reach`'ten uzaksa `inf` döner - o hâlde cevabın kesin değerine
        ihtiyaç yoktur, yalnız 'ihlal değil' bilgisi kullanılır."""
        if isinstance(shape, Poly):
            return self._dist_to_poly(shape, reach)
        x0, y0, x1, y1 = shape.bbox()
        best = float("inf")
        for idx in self._candidates(x0, y0, x1, y1, reach):
            a, b = self.segments[idx]
            d = shape.dist_seg(a, b)
            if d < best:
                best = d
        return best

    def _dist_to_poly(self, poly: "Poly", reach: float) -> float:
        """Zone dolgusu KARTIN TAMAMINI kaplar: sınır kutusu hiçbir şey elemez.
        O yüzden sorgu ters çevrilir - poligonun HER KENARI tek tek ızgaraya
        sorulur ve kenar hattından uzak olanlar hiç ölçülmez."""
        pts = poly.pts
        n = len(pts)
        best = float("inf")
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for idx in self._candidates(x0, y0, x1, y1, reach):
                c, d = self.segments[idx]
                v = seg_seg_dist(a, b, c, d)
                if v < best:
                    best = v
        return best


# --------------------------------------------------------------------------- #
# Kart okuma
# --------------------------------------------------------------------------- #
class Item:
    """Bir bakır öge: şekil + net + üzerinde bulunduğu bakır katmanlar."""
    __slots__ = ("kind", "label", "net", "layers", "shape", "clearance_override",
                 "owner", "bbox", "uuid")

    def __init__(self, kind, label, net, layers, shape,
                 clearance_override=None, owner=None, uuid=""):
        self.kind = kind
        self.label = label
        self.net = net
        self.layers = layers
        self.shape = shape
        self.clearance_override = clearance_override
        self.owner = owner
        self.bbox = shape.bbox()
        # KiCad'in DRC raporu her ogeye uuid yazar. Ayni alani biz de tasirsak
        # iki rapor KONUM YAKINLIGIYLA degil KIMLIKLE karsilastirilabilir -
        # "yakin sayilar" ile dogrulama yapmanin sonu yoktur.
        self.uuid = uuid


class Hole:
    __slots__ = ("kind", "label", "net", "cx", "cy", "r", "owner", "uuid")

    def __init__(self, kind, label, net, cx, cy, r, owner=None, uuid=""):
        self.kind, self.label, self.net = kind, label, net
        self.cx, self.cy, self.r = cx, cy, r
        self.owner = owner
        self.uuid = uuid


def bbox_point_gap(bbox, px: float, py: float) -> float:
    """Noktanın sınır kutusuna uzaklığı = gerçek mesafenin ALT SINIRI.

    Ucuz bir eleme: bu alt sınır zaten kuraldan büyükse şeklin kendisine hiç
    bakılmaz. Cevabı DEĞİŞTİRMEZ, yalnız hızlandırır."""
    x0, y0, x1, y1 = bbox
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


def _copper_stack(tree) -> list[str]:
    """Bakır katmanlar, FİZİKSEL sırayla (F.Cu, In1.Cu, ..., B.Cu).

    KiCad'in katman NUMARALARI fiziksel sıra değildir (B.Cu = 2, In1.Cu = 4);
    numaraya göre sıralamak kör/gömülü via'nın kapsadığı katmanları yanlış
    hesaplatır."""
    block = first(tree, "layers")
    names: list[str] = []
    if block:
        for entry in block:
            if isinstance(entry, list) and len(entry) >= 2:
                name = unquote(str(entry[1]))
                if name.endswith(".Cu"):
                    names.append(name)
    if not names:
        names = ["F.Cu", "B.Cu"]

    def order(name: str) -> tuple[int, int]:
        if name == "F.Cu":
            return (0, 0)
        if name == "B.Cu":
            return (2, 0)
        digits = "".join(ch for ch in name if ch.isdigit())
        return (1, int(digits) if digits else 0)

    return sorted(names, key=order)


def _expand_layers(tokens, stack: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for tok in tokens:
        name = unquote(str(tok))
        if name in ("*.Cu", "*"):
            out.update(stack)
        elif name == "F&B.Cu":
            out.update({"F.Cu", "B.Cu"} & set(stack))
        elif name in stack:
            out.add(name)
    return frozenset(out)


def _rotate(px, py, ang_deg):
    """KiCad RotatePoint: x' = x cos + y sin ; y' = y cos - x sin."""
    a = math.radians(ang_deg)
    c, s = math.cos(a), math.sin(a)
    return (px * c + py * s, py * c - px * s)


def _pad_shape(pad, fx, fy, frot):
    shape_kind = str(pad[3]) if len(pad) > 3 else "rect"
    at = first(pad, "at")
    px = float(at[1]) if at and len(at) > 1 else 0.0
    py = float(at[2]) if at and len(at) > 2 else 0.0
    prot = float(at[3]) if at and len(at) > 3 else 0.0
    rx, ry = _rotate(px, py, frot)
    cx, cy = fx + rx, fy + ry
    size = first(pad, "size")
    w = float(size[1]) if size and len(size) > 1 else 0.0
    h = float(size[2]) if size and len(size) > 2 else w
    if shape_kind == "circle":
        radius = min(w, h) / 2.0
    elif shape_kind == "oval":
        radius = min(w, h) / 2.0
    elif shape_kind == "roundrect":
        ratio = fnum(pad, "roundrect_rratio", 1, 0.25) or 0.0
        radius = ratio * min(w, h)
    else:                                   # rect / trapezoid / custom
        radius = 0.0
    return RRect(cx, cy, w, h, radius, prot), (cx, cy), shape_kind


def _arc_points(start, mid, end, step=ARC_STEP_MM):
    """Üç noktalı yayı poligona indirger. Çember merkezi üç noktadan ÇÖZÜLÜR;
    tanımsızsa (eşdoğrusal) kiriş döner."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1)
          + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3)
          + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)
    # a1 -> a3, a2 üzerinden geçen yön
    def norm(a):
        while a <= -math.pi:
            a += 2 * math.pi
        while a > math.pi:
            a -= 2 * math.pi
        return a
    sweep = norm(a3 - a1)
    if norm(a2 - a1) * sweep < 0 or abs(norm(a2 - a1)) > abs(sweep):
        sweep = sweep - math.copysign(2 * math.pi, sweep)
    steps = max(2, int(math.ceil(abs(sweep) * r / max(step, 1e-6))))
    return [(ux + r * math.cos(a1 + sweep * k / steps),
             uy + r * math.sin(a1 + sweep * k / steps)) for k in range(steps + 1)]


def read_board(path: Path) -> dict:
    tree = parse_sexpr(path.read_text(encoding="utf-8"))
    if not tree:
        raise ValueError("board bos veya ayristirilamadi")
    stack = _copper_stack(tree)
    all_cu = frozenset(stack)

    # KiCad 10 net'i ADIYLA yazar - `(net "GND")`. Daha eski dosyalar SIRA
    # NUMARASI yazar ve adı üstteki `(net 3 "GND")` bildirimlerinde tutar.
    # İkisini de çözmezsek eski bir kartta her net "3", "4" gibi görünür ve
    # farklı netler AYNI sanılıp denetim sessizce boşalır.
    net_names: dict[str, str] = {}
    for decl in kids(tree, "net"):
        if len(decl) >= 3:
            net_names[str(decl[1])] = unquote(str(decl[2]))

    def netof(node, tag="net") -> str:
        k = first(node, tag)
        if not k or len(k) < 2:
            return ""
        if len(k) >= 3:
            return unquote(str(k[2]))
        token = str(k[1])
        if token.startswith('"'):
            return unquote(token)
        return net_names.get(token, token)

    items: list[Item] = []
    holes: list[Hole] = []

    # --- izler ------------------------------------------------------------- #
    for seg in kids(tree, "segment") + kids(tree, "arc"):
        start = first(seg, "start")
        end = first(seg, "end")
        if not (start and end):
            continue
        a = (float(start[1]), float(start[2]))
        b = (float(end[1]), float(end[2]))
        width = fnum(seg, "width", 1, 0.0) or 0.0
        layer = fstr(seg, "layer")
        items.append(Item("iz", f"iz {layer}", netof(seg),
                          frozenset({layer}) & all_cu, Capsule(a, b, width / 2.0),
                          uuid=fstr(seg, "uuid")))

    # --- via'lar ----------------------------------------------------------- #
    vias: list[Item] = []
    for via in kids(tree, "via"):
        at = first(via, "at")
        if not at:
            continue
        cx, cy = float(at[1]), float(at[2])
        size = fnum(via, "size", 1, 0.0) or 0.0
        drill = fnum(via, "drill", 1, 0.0) or 0.0
        lay = first(via, "layers")
        tokens = [unquote(str(x)) for x in (lay[1:] if lay else [])]
        spans = all_cu
        if len(tokens) >= 2 and tokens[0] in stack and tokens[-1] in stack:
            i, j = sorted((stack.index(tokens[0]), stack.index(tokens[-1])))
            spans = frozenset(stack[i:j + 1])
        net = netof(via)
        uid = fstr(via, "uuid")
        item = Item("via", "via", net, spans, Circle(cx, cy, size / 2.0), uuid=uid)
        vias.append(item)
        items.append(item)
        if drill > 0:
            holes.append(Hole("via", "via", net, cx, cy, drill / 2.0,
                              owner=item, uuid=uid))

    # --- pedler ------------------------------------------------------------ #
    for fp in kids(tree, "footprint"):
        at = first(fp, "at")
        fx = float(at[1]) if at and len(at) > 1 else 0.0
        fy = float(at[2]) if at and len(at) > 2 else 0.0
        frot = float(at[3]) if at and len(at) > 3 else 0.0
        ref = ""
        for prop in kids(fp, "property"):
            if len(prop) > 2 and unquote(str(prop[1])) == "Reference":
                ref = unquote(str(prop[2]))
        for pad in kids(fp, "pad"):
            number = unquote(str(pad[1])) if len(pad) > 1 else "?"
            lay = first(pad, "layers")
            layers = _expand_layers(lay[1:] if lay else [], stack)
            shape, centre, _kind = _pad_shape(pad, fx, fy, frot)
            net = netof(pad)
            local = fnum(pad, "clearance", 1, None)
            label = f"{ref}.{number}"
            uid = fstr(pad, "uuid")
            if layers:
                items.append(Item("ped", label, net, layers, shape,
                                  clearance_override=local, owner=ref, uuid=uid))
            drill = first(pad, "drill")
            if drill is not None:
                try:
                    dia = (float(drill[2]) if str(drill[1]) == "oval"
                           else float(drill[1]))
                except (IndexError, ValueError):
                    dia = 0.0
                if dia > 0:
                    holes.append(Hole("ped", label, net, centre[0], centre[1],
                                      dia / 2.0, owner=ref, uuid=uid))

    # --- zone dolguları ---------------------------------------------------- #
    zone_polys = 0
    for zone in kids(tree, "zone"):
        net = netof(zone) or fstr(zone, "net_name")
        for fill in kids(zone, "filled_polygon"):
            layer = fstr(fill, "layer")
            pts_node = first(fill, "pts")
            if not pts_node:
                continue
            pts = [(float(p[1]), float(p[2])) for p in kids(pts_node, "xy")]
            if len(pts) < 3:
                continue
            zone_polys += 1
            items.append(Item("zone", f"zone {layer}", net,
                              frozenset({layer}) & all_cu, Poly(pts),
                              uuid=fstr(zone, "uuid")))

    # --- kart kenarı ------------------------------------------------------- #
    edge: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for node in tree:
        if not (isinstance(node, list) and node and
                str(node[0]).startswith("gr_")):
            continue
        if fstr(node, "layer") != "Edge.Cuts":
            continue
        tag = node[0]
        if tag == "gr_line":
            s, e = first(node, "start"), first(node, "end")
            if s and e:
                edge.append(((float(s[1]), float(s[2])),
                             (float(e[1]), float(e[2]))))
        elif tag == "gr_arc":
            s, m, e = first(node, "start"), first(node, "mid"), first(node, "end")
            if s and m and e:
                pts = _arc_points((float(s[1]), float(s[2])),
                                  (float(m[1]), float(m[2])),
                                  (float(e[1]), float(e[2])))
                edge.extend(zip(pts, pts[1:]))
        elif tag == "gr_circle":
            c, e = first(node, "center"), first(node, "end")
            if c and e:
                cx, cy = float(c[1]), float(c[2])
                r = math.hypot(float(e[1]) - cx, float(e[2]) - cy)
                steps = max(16, int(2 * math.pi * r / ARC_STEP_MM))
                pts = [(cx + r * math.cos(2 * math.pi * k / steps),
                        cy + r * math.sin(2 * math.pi * k / steps))
                       for k in range(steps + 1)]
                edge.extend(zip(pts, pts[1:]))
        elif tag == "gr_rect":
            s, e = first(node, "start"), first(node, "end")
            if s and e:
                x0, y0 = float(s[1]), float(s[2])
                x1, y1 = float(e[1]), float(e[2])
                corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                edge.extend((corners[i], corners[(i + 1) % 4]) for i in range(4))
        elif tag == "gr_poly":
            pts_node = first(node, "pts")
            if pts_node:
                pts = [(float(p[1]), float(p[2])) for p in kids(pts_node, "xy")]
                if len(pts) >= 2:
                    edge.extend(zip(pts, pts[1:] + pts[:1]))

    return {
        "stack": stack,
        "items": items,
        "vias": vias,
        "holes": holes,
        "edge": edge,
        "zone_polys": zone_polys,
    }


# --------------------------------------------------------------------------- #
# Kural okuma - hiçbir sayı bu dosyada YAZILI DEĞİLDİR
# --------------------------------------------------------------------------- #
class Rules:
    def __init__(self):
        self.design: dict[str, float] = {}
        self.classes: dict[str, float] = {}
        self.patterns: list[tuple[str, str]] = []
        self.sources: dict[str, str] = {}
        self.edge_ring: float | None = None
        self.edge_ring_src: str = ""
        self.netclass_src: str = ""

    def netclass_of(self, net: str) -> str:
        if not net:
            return "Default"
        for pattern, name in self.patterns:
            if pattern == net or fnmatch.fnmatchcase(net, pattern):
                return name
        return "Default"

    def clearance_of(self, net: str) -> float | None:
        return self.classes.get(self.netclass_of(net))

    def pair_clearance(self, a: Item, b: Item) -> float | None:
        values = []
        for item in (a, b):
            if item.clearance_override is not None:
                values.append(item.clearance_override)
            else:
                c = self.clearance_of(item.net)
                if c is None:
                    return None
                values.append(c)
        floor = self.design.get("min_clearance")
        if floor is not None:
            values.append(floor)
        return max(values)


def load_rules(project: Path | None, outline: Path | None,
               netclasses: Path | None) -> tuple[Rules, list[str]]:
    rules = Rules()
    notes: list[str] = []

    if project and project.is_file():
        data = json.loads(project.read_text(encoding="utf-8"))
        design = (data.get("board", {}).get("design_settings", {})
                  .get("rules", {}) or {})
        for key in ("min_clearance", "min_hole_clearance", "min_hole_to_hole",
                    "min_copper_edge_clearance"):
            if isinstance(design.get(key), (int, float)):
                rules.design[key] = float(design[key])
                rules.sources[key] = f"{project.name}:design_settings.rules"
        nets = data.get("net_settings", {}) or {}
        for cls in nets.get("classes", []) or []:
            if isinstance(cls.get("clearance"), (int, float)):
                rules.classes[cls["name"]] = float(cls["clearance"])
        for pat in nets.get("netclass_patterns", []) or []:
            if pat.get("pattern") and pat.get("netclass"):
                rules.patterns.append((pat["pattern"], pat["netclass"]))
        for net, name in (nets.get("netclass_assignments") or {}).items():
            rules.patterns.insert(0, (net, name))
        if rules.classes:
            rules.netclass_src = f"{project.name}:net_settings"
    else:
        notes.append("KAPSAM DISI: .kicad_pro okunamadi - netclass ve tasarim "
                     "kurallari YOK")

    # netclasses.json yalnız ÇAPRAZ KONTROL içindir: KiCad'in uyguladığı
    # değerler .kicad_pro'dadır ve iki dosyanın ayrışması sessiz kalmamalıdır.
    if netclasses and netclasses.is_file():
        data = json.loads(netclasses.read_text(encoding="utf-8"))
        declared = {c["name"]: c.get("clearance_mm")
                    for c in data.get("classes", []) if c.get("name")}
        if not rules.classes:
            for name, value in declared.items():
                if isinstance(value, (int, float)):
                    rules.classes[name] = float(value)
            for cls in data.get("classes", []):
                for pattern in cls.get("net_patterns", []) or []:
                    rules.patterns.append((pattern, cls["name"]))
            rules.netclass_src = netclasses.name
        else:
            drift = sorted(
                name for name, value in declared.items()
                if isinstance(value, (int, float))
                and name in rules.classes
                and abs(rules.classes[name] - float(value)) > 1e-9)
            if drift:
                notes.append(
                    f"AYRISMA: {netclasses.name} ile .kicad_pro netclass "
                    f"clearance'i uyusmuyor: {', '.join(drift)}")

    if outline and outline.is_file():
        data = json.loads(outline.read_text(encoding="utf-8"))
        ring = (data.get("outline", {}) or {}).get("edge_clearance_ring_mm")
        if isinstance(ring, (int, float)):
            rules.edge_ring = float(ring)
            rules.edge_ring_src = f"{outline.name}:outline.edge_clearance_ring_mm"
            # Beyan ile UYGULAMA ayrisabilir ve sessiz kalmamalidir. Netclass
            # icin zaten bagiriliyor; halka icin bagirilmiyordu ve ayrisma
            # OLCULDU (2026-08-10, cam-w): board_outline.json 0.5 mm beyan
            # ederken .kicad_pro 0.3 mm uyguluyordu - KiCad 21 ihlal, bu arac
            # 35. Iki sayi da dogruydu; FARKLI KURALLARI olcuyorlardi. Beyan
            # siki oldugu icin denetim ona gore yapilir (o beyanin AMACI
            # budur), ama farkin kendisi rapor edilir: KiCad'in DRC'si o
            # bakiri gecirecektir.
            enforced = rules.design.get("min_copper_edge_clearance")
            if enforced is not None and abs(enforced - rules.edge_ring) > 1e-9:
                notes.append(
                    f"AYRISMA: {outline.name} {rules.edge_ring} mm kenar "
                    f"halkasi BEYAN ediyor, .kicad_pro {enforced} mm UYGULUYOR "
                    f"- denetim beyana gore yapildi; KiCad DRC'si aradaki "
                    f"bakiri gecirir")
    if rules.edge_ring is None and "min_copper_edge_clearance" in rules.design:
        rules.edge_ring = rules.design["min_copper_edge_clearance"]
        rules.edge_ring_src = rules.sources["min_copper_edge_clearance"]
    return rules, notes


# --------------------------------------------------------------------------- #
# Denetimler
# --------------------------------------------------------------------------- #
def check_via_copper(board: dict, rules: Rules) -> tuple[list[dict], bool]:
    """Via bakırı ile FARKLI NETTEKİ bakır arasındaki açıklık."""
    if not rules.classes:
        return [], False
    bad: list[dict] = []
    for via in board["vias"]:
        cx, cy, radius = via.shape.cx, via.shape.cy, via.shape.r
        for other in board["items"]:
            if other is via or other.net == via.net or not other.layers:
                continue
            if not (other.layers & via.layers):
                continue
            need = rules.pair_clearance(via, other)
            if need is None:
                continue
            if bbox_point_gap(other.bbox, cx, cy) - radius >= need - TOL_MM:
                continue
            gap = other.shape.dist_point((cx, cy)) - radius
            if gap < need - TOL_MM:
                bad.append({
                    "rule": "via_copper_clearance",
                    "at": [round(cx, 4), round(cy, 4)],
                    "net": via.net, "against": f"{other.kind} {other.label}",
                    "against_net": other.net, "uuid": via.uuid,
                    "against_uuid": other.uuid,
                    "measured_mm": round(gap, 4), "required_mm": round(need, 4),
                })
    return bad, True


def check_hole_clearance(board: dict, rules: Rules) -> tuple[list[dict], bool]:
    """Delik kenarı ile FARKLI NETTEKİ bakır arasındaki açıklık.

    Matkap her katmanı deler: katman kesişimi ARANMAZ. Netclass açıklığı da
    aranmaz - bu ayrı ve tek bir kuraldır."""
    need = rules.design.get("min_hole_clearance")
    if need is None:
        return [], False
    bad: list[dict] = []
    for hole in board["holes"]:
        centre = (hole.cx, hole.cy)
        for other in board["items"]:
            if other is hole.owner or other.net == hole.net or not other.layers:
                continue
            if other.kind == "ped" and other.label == hole.label:
                continue                     # deliğin KENDİ halkası
            if bbox_point_gap(other.bbox, hole.cx, hole.cy) - hole.r >= need - TOL_MM:
                continue
            gap = other.shape.dist_point(centre) - hole.r
            if gap < need - TOL_MM:
                bad.append({
                    "rule": "hole_clearance",
                    "at": [round(hole.cx, 4), round(hole.cy, 4)],
                    "net": hole.net, "hole": hole.kind,
                    "against": f"{other.kind} {other.label}",
                    "against_net": other.net, "uuid": hole.uuid,
                    "against_uuid": other.uuid,
                    "measured_mm": round(gap, 4), "required_mm": round(need, 4),
                })
    return bad, True


def check_hole_to_hole(board: dict, rules: Rules) -> tuple[list[dict], bool]:
    need = rules.design.get("min_hole_to_hole")
    if need is None:
        return [], False
    limit = need - DRC_EPSILON_MM
    holes = board["holes"]
    bad: list[dict] = []
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            a, b = holes[i], holes[j]
            if a.kind == "ped" and b.kind == "ped" and a.owner == b.owner:
                continue                    # aynı ayak izinin delikleri
            gap = math.hypot(a.cx - b.cx, a.cy - b.cy) - a.r - b.r
            if gap < limit - TOL_MM:
                bad.append({
                    "rule": "hole_to_hole",
                    "at": [round(a.cx, 4), round(a.cy, 4)],
                    "net": a.net,
                    "against": f"{b.kind} {b.label} @ "
                               f"({round(b.cx, 3)}, {round(b.cy, 3)})",
                    "against_net": b.net, "uuid": a.uuid, "against_uuid": b.uuid,
                    "measured_mm": round(gap, 4), "required_mm": round(limit, 4),
                })
    return bad, True


def check_edge(board: dict, rules: Rules) -> tuple[list[dict], bool]:
    ring = rules.edge_ring
    if ring is None or not board["edge"]:
        return [], False
    index = SegmentIndex(board["edge"])
    bad: list[dict] = []
    for item in board["items"]:
        if not item.layers:
            continue
        gap = index.dist_to_shape(item.shape, ring)
        if gap < ring - TOL_MM:
            x0, y0, x1, y1 = item.shape.bbox()
            bad.append({
                "rule": "copper_edge_clearance",
                "at": [round((x0 + x1) / 2, 4), round((y0 + y1) / 2, 4)],
                "net": item.net, "kind": item.kind, "label": item.label,
                "uuid": item.uuid,
                "measured_mm": round(gap, 4), "required_mm": round(ring, 4),
            })
    return bad, True


# --------------------------------------------------------------------------- #
def _fmt(v: dict) -> str:
    return (f"{v['rule']}: {v.get('kind', v.get('hole', 'via'))} "
            f"[{v.get('net') or '-'}] ({v['at'][0]}, {v['at'][1]}) "
            f"olculen {v['measured_mm']:.4f} mm < gereken "
            f"{v['required_mm']:.4f} mm"
            + (f" -- {v['against']} [{v.get('against_net') or '-'}]"
               if v.get("against") else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", required=True, help=".kicad_pcb")
    ap.add_argument("--project", help=".kicad_pro (varsayilan: board'un kardesi)")
    ap.add_argument("--outline", help="board_outline.json (kenar halkasi beyani)")
    ap.add_argument("--netclasses", help="layout/netclasses.json (capraz kontrol)")
    ap.add_argument("--edge-ring-mm", type=float, default=None,
                    help="halkayi ELLE ez (beyani gormezden gelir)")
    ap.add_argument("--check", action="store_true",
                    help="kapi modu (varsayilan davranisla ayni: ihlal -> 1)")
    ap.add_argument("--report-only", action="store_true",
                    help="ihlal bulsa bile 0 don - KAPI DEGILDIR")
    ap.add_argument("--max-list", type=int, default=12,
                    help="ekrana yazilan ihlal sayisi (varsayilan 12)")
    ap.add_argument("--json", help="makine raporunu bu dosyaya yaz")
    a = ap.parse_args(argv)

    board_path = Path(a.board)
    project = Path(a.project) if a.project else board_path.with_suffix(".kicad_pro")
    try:
        board = read_board(board_path)
        rules, notes = load_rules(
            project,
            Path(a.outline) if a.outline else None,
            Path(a.netclasses) if a.netclasses else None)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[yonlendirme] girdi okunamadi: {exc!r}")
        return 2

    if a.edge_ring_mm is not None:
        rules.edge_ring = a.edge_ring_mm
        rules.edge_ring_src = "--edge-ring-mm (ELLE)"

    n_iz = sum(1 for i in board["items"] if i.kind == "iz")
    n_ped = sum(1 for i in board["items"] if i.kind == "ped")
    print(f"[0] kart              : {board_path.name}")
    print(f"[1] bakir katman      : {len(board['stack'])} "
          f"({', '.join(board['stack'])})")
    print(f"[2] taranan bakir     : {len(board['vias'])} via - {n_iz} iz - "
          f"{n_ped} ped - {board['zone_polys']} zone dolgusu - "
          f"{len(board['holes'])} delik")
    print(f"[3] kart kenari       : {len(board['edge'])} dogru parcasi "
          f"(yaylar {ARC_STEP_MM} mm adimla indirgendi)")

    copper, cov_copper = check_via_copper(board, rules)
    hole, cov_hole = check_hole_clearance(board, rules)
    h2h, cov_h2h = check_hole_to_hole(board, rules)
    edge, cov_edge = check_edge(board, rules)

    def line(idx: int, title: str, values: list[dict], covered: bool,
             detail: str) -> None:
        if not covered:
            print(f"[{idx}] {title:<34}: NO_COVERAGE ({detail})")
            return
        print(f"[{idx}] {title:<34}: {len(values)} ihlal   [{detail}]")
        for v in values[:a.max_list]:
            print("       ", _fmt(v))
        if len(values) > a.max_list:
            print(f"        ... +{len(values) - a.max_list} tane daha")

    line(4, "via bakir acikligi (netclass)", copper, cov_copper,
         rules.netclass_src or "netclass kaynagi yok")
    line(5, "via/ped delik acikligi", hole, cov_hole,
         f"min_hole_clearance = {rules.design.get('min_hole_clearance')} mm - "
         f"{rules.sources.get('min_hole_clearance', '?')}")
    line(6, "delik-delik mesafesi", h2h, cov_h2h,
         f"min_hole_to_hole = {rules.design.get('min_hole_to_hole')} mm "
         f"(- {DRC_EPSILON_MM} mm DRC epsilon)")
    line(7, "kart kenari halkasi", edge, cov_edge,
         f"{rules.edge_ring} mm - {rules.edge_ring_src or 'BEYAN YOK'}")

    for note in notes:
        print(f"[8] {note}")

    uncovered = [name for name, ok in (
        ("via_copper_clearance", cov_copper), ("hole_clearance", cov_hole),
        ("hole_to_hole", cov_h2h), ("copper_edge_clearance", cov_edge))
        if not ok]
    violations = copper + hole + h2h + edge

    print("[9] KAPSAM DISI (bu arac olcmez): egri iz (arc) genisligi yaklasik, "
          "custom ped govdesi dikdortgene indirgenir, .kicad_dru ozel "
          "kurallari okunmaz, iz-iz ve ped-ped bakir acikligi (KiCad "
          "'clearance') kapsam disi, lehim maskesi ve baglanti kontrolu yok")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "schema_version": "route-audit/v1",
            "board": board_path.name,
            "scanned": {"via": len(board["vias"]), "iz": n_iz, "ped": n_ped,
                        "zone_fill": board["zone_polys"],
                        "delik": len(board["holes"])},
            "rules": {"edge_ring_mm": rules.edge_ring,
                      "edge_ring_src": rules.edge_ring_src,
                      **{k: v for k, v in rules.design.items()}},
            "uncovered": uncovered,
            "counts": {"via_copper_clearance": len(copper),
                       "hole_clearance": len(hole),
                       "hole_to_hole": len(h2h),
                       "copper_edge_clearance": len(edge)},
            "violations": violations,
            "status": "GREEN" if not (violations or uncovered) else "RED",
        }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")

    if uncovered:
        print(f"\nYONLENDIRME MUHAFIZI: NO_COVERAGE ({', '.join(uncovered)}) "
              f"- olcemedim PASS DEGILDIR")
        return 0 if a.report_only else 1
    if violations:
        print(f"\nYONLENDIRME MUHAFIZI: RED - {len(violations)} ihlal")
        return 0 if a.report_only else 1
    print("\nYONLENDIRME MUHAFIZI: GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
