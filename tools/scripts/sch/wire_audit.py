#!/usr/bin/env python3
"""wire_audit — bağlantı TELLE mi kuruldu, ADLA mı? Şematiğin geometrisini okur.

NEDEN VAR
--------------------------------------------------------------------------
Bir üreteç, elektriksel olarak kusursuz bir netlist üretirken sayfaya
neredeyse hiçbir şey çizmeyebilir: iki pinin arasına tel çekmek yerine
ikisinin yanına da aynı net etiketini koymak yeter. `kicad-cli sch export
netlist` bu iki durumu AYIRT EDEMEZ — ikisinde de tek bir net görür. Netlist
"bağlantı var mı" sorusunu yanıtlar, "bağlantı nasıl kuruldu" sorusunu değil.

Farkı gören tek tanık geometridir, ve bu modül onu okur:

    pin ankrajı  ->  tel parçaları  ->  birleşim-bul  ->  aynı iletken mi?

Fark neden önemli: LDO'nun Cin/Cout kondansatörü doğru rayın ADINDA ama
sayfanın 60 mm ötesinde duruyorsa netlist yeşildir, devre değildir —
bypass'ın anlamı ray adı değil, besleme pinine olan İLMEK ALANIdır. Aynı
şekilde seri sonlandırma direnci, sinyal yolunun üzerinde durmadığı sürece
layout'ta izin nereden geçeceğini kimseye söylemez.

`cam-w-og05b10/scripts/sch_graph.py` bu ölçümün dar bir sürümüdür (yalnız
rot=0, aynasız) ve birleşim-bul iskeleti oradan alınmıştır. Bu modül ek
olarak rotasyon + mirror + çok birimli sembolleri çözer, bulguyu netlistle
çapraz doğrular ve bağlanma kuralını o modülden DEĞİL, KiCad'in kendisinden
alır (aşağıdaki tabloya bakınız — ikisi aynı şeyi söylemiyor).

KİCAD BAĞLANMA KURALI (varsayılmadı — ÖLÇÜLDÜ)
--------------------------------------------------------------------------
Kural, KiCad 10.0.5 ile sentetik sayfalar üretip `kicad-cli sch export
netlist` çıktısına bakarak doğrulandı. Sonuç, bu depodaki iki modülün
yazdığından FARKLI çıktı, o yüzden ölçüm burada tablo hâlinde durur:

    iki telin UCU aynı noktada .................... BAĞLANIR (junction'sız)
    tel UCU  -> başka telin ORTASI (T) ............ BAĞLANMAZ *
    tel ORTASI -> tel ORTASI (X kesişimi) ......... BAĞLANMAZ *
    pin -> telin UCU .............................. BAĞLANIR
    pin -> telin ORTASI ........................... BAĞLANMAZ *
    (*) o noktada `(junction ...)` varsa BAĞLANIR

Yani tek kural şudur: bir nokta, YALNIZCA ucu olduğu tellere bağlanır;
`junction` ise üzerinde bulunduğu HER teli (ortası dahil) birleştirir.

`cam-w-og05b10/scripts/sch_graph.py` "telin ucu başka bir tele (ortasına
dahil) değiyorsa bağlanır" der; ölçüm bunu doğrulamıyor — o model FAZLA
bağlar ve tam da aranan kusuru (junction'ı unutulmuş T) "bağlı" diye
raporlar. `sch_wire.auto_junctions()` ise doğru tarafta: her T'ye nokta
koyar, çünkü noktasız T bağlantı değildir. Eeschema çizim sırasında bu
noktayı kendisi ekler; dosyayı üreten bir script eklemezse bağlantı yoktur.

KANIT DİSİPLİNİ
--------------------------------------------------------------------------
Bu modülün ölçtüğü şey "çizildi mi", netlistin ölçtüğü şey "bağlandı mı".
İkisi birbirinin yerine GEÇMEZ; G kontrolü ikisini karşı karşıya getirir ve
anlaşamadıkları anda yüksek sesle durur. Anlaşmazlık ya geometri modelinin
ya da çizimin yanlış olduğu anlamına gelir — hangisi olduğuna bakmadan
birine güvenmek, sessizce yanlış bir rapora imza atmaktır.

KAPSAM DIŞI (bilinen körlükler)
--------------------------------------------------------------------------
  * Bus / bus_entry modellenmez. Bus kullanan bir sayfa `0-input` altında
    bir `info` bulgusuyla işaretlenir — o sayfadaki A/D bulguları yanlış
    pozitif olabilir, sessizce doğru sayılmaz.
  * Hiyerarşik sayfa pinleri yalnız D kontrolünde "geçerli iniş noktası"
    olarak kullanılır; sayfalar arası süreklilik ölçülmez (tek sayfa okur).
  * Netlist tüm projeyi kapsayabilir; bu sayfada bulunmayan pinler kusur
    değil, kapsam dışıdır ve `info` olarak raporlanır.

KULLANIM
--------------------------------------------------------------------------
    wire_audit.py <sheet.kicad_sch> [--netlist X.net] [--rules rules.json]
                                    [--json out.json]

`rules.json` (hepsi isteğe bağlı):
    {"power_lib_prefixes": ["power:", "cam-w:V"],
     "name_stitched_ok":   ["GND", "VDD_*", "VIN_*"],
     "required": [
       {"kind": "decoupling", "pin": "U402.6", "parts": ["C5003"],
        "max_mm": 40.0, "why": "VCC ilmek alanı"},
       {"kind": "pullup", "net": "SCCB_SCL", "part": "R5001",
        "to": "VDD_1V8IO", "why": "I2C çekme"}]}

`name_stitched_ok` VARSAYILAN OLARAK BOŞTUR. "GND zaten hep adla birleşir"
diye önceden affetmek, tam olarak aranan kusuru (ray adına asılmış
kondansatör) affetmek olurdu; istisna, kararı veren tarafından YAZILI olarak
verilmelidir.

Çıkış kodları: 0 = temiz, 1 = en az bir `error` bulgusu, 2 = araç çalışamadı.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.kicad_cli import KicadCliError, erc_violations, run_erc  # noqa: E402
from common.netlist import read_nets  # noqa: E402
from common.report_io import (diag, emit, run_cli, write_report,  # noqa: E402
                              EXIT_FAIL, EXIT_OK)
from common.sexpr import (block_at, find, find_all, parse_sexpr,  # noqa: E402
                          unquote)

from sch_wire import Pin, Placed, _xform, load_lib_symbols  # noqa: E402

SCHEMA_VERSION = "wire-audit/v1"

#: Çakışma toleransı (mm). `sch_wire._on_segment` ile aynı 0.005: pin
#: ankrajları 2, tel uçları 3 ondalığa yuvarlanır; bundan ince bir eşik
#: yuvarlama gürültüsünü "kopukluk" diye raporlardı.
EPS = 5e-3

#: "Yakın ıska" bandının üst sınırı (mm). Bu aralıktaki bir tel ucu KiCad'de
#: BAĞLI DEĞİLDİR ama göz kontrolünde bağlı görünür — sessiz kopukluğun
#: klasik hâli. Uyarı değil, hata.
NEAR_MISS_MM = 1.0

DEFAULT_RULES: dict = {
    "power_lib_prefixes": ["power:"],
    "name_stitched_ok": [],
    "required": [],
}

Point = tuple[float, float]
Segment = tuple[Point, Point]


# --------------------------------------------------------------------------- #
# Geometri yardımcıları                                                        #
# --------------------------------------------------------------------------- #
def _pt(x, y) -> Point:
    return (round(float(x), 3), round(float(y), 3))


def _same(p: Point, q: Point) -> bool:
    return math.dist(p, q) <= EPS


def _on_segment(p: Point, a: Point, b: Point) -> bool:
    """`p` `a`-`b` parçasının üzerinde mi (uçlar dahil)?

    `sch_wire._on_segment` yeniden kullanılmadı ve bu bilerek yapıldı: o
    sürüm yalnız eksen hizalı parçaları tanır (çapraz için koşulsuz False
    döner), çünkü `sch_wire` zaten yalnız ortogonal tel ÇİZER. Denetçi ise
    elle çizilmiş çapraz teli de okumak zorundadır; onu görmezden gelmek
    "bağlı olan pini bağsız raporlamak" demektir.

    Dikey uzaklık NORMALİZE edilir. Ham çapraz çarpım uzunlukla ölçeklenir:
    325 mm'lik bir telde 0.003 mm'lik sapma çarpımı ~1'e çıkarır ve sabit
    eşikli bir karşılaştırma o teli "üzerinde değil" sayar — tolerans sessizce
    telin boyuna bağlı hâle gelir.
    """
    (px, py), (ax, ay), (bx, by) = p, a, b
    length = math.dist(a, b)
    if length <= EPS:                              # dejenere parça = nokta
        return _same(p, a)
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) / length > EPS:
        return False
    return (min(ax, bx) - EPS <= px <= max(ax, bx) + EPS
            and min(ay, by) - EPS <= py <= max(ay, by) + EPS)


def _interior(p: Point, a: Point, b: Point) -> bool:
    """`p` parçanın ÜZERİNDE ama UCUNDA değil — yani gerçek bir T teması."""
    return _on_segment(p, a, b) and not _same(p, a) and not _same(p, b)


def _anchor(placed: Placed, pin: Pin) -> Point:
    """Belirli bir `Pin` nesnesinin sayfa ankrajı.

    `Placed.pin(number)` numaradan ARAR; çok birimli bir sembolde aynı
    numara iki birimde geçerse yanlış birimin pinini döndürür. Burada pin
    nesnesi zaten elimizdedir, o yüzden dönüşüm doğrudan uygulanır.

    Dönüşüm `sch_wire._xform`'dur, kopyası değil: iki ayrı rotasyon+mirror
    uygulaması zamanla ayrışır ve ayrışma SESSİZDİR (netlist yanlış olur,
    şema göz kontrolünde doğru görünür). Bu yeniden kullanımın bir hatayı
    gizlemesine karşı G kontrolü vardır — geometri ile netlist anlaşmazsa
    rapor durur.
    """
    ax, ay = _xform(pin.x, pin.y, placed.rot, placed.mirror)
    return (round(placed.x + ax, 2), round(placed.y + ay, 2))


# --------------------------------------------------------------------------- #
# Tel grafı                                                                    #
# --------------------------------------------------------------------------- #
class WireGraph:
    """Tel parçalarının birleşim-bul yapısı. Soru: iki nokta aynı iletkende mi?

    Modül başlığındaki ÖLÇÜLMÜŞ kural birebir uygulanır:

      * bir nokta yalnızca UCU olduğu parçalara katılır,
      * `junction` ise üzerinde bulunduğu HER parçayı (ortası dahil) katar.

    Düğümler kurulumda toplu verilir (tel uçları + junction + pin ankrajı);
    sorgu anında tembel eklemek, aynı dosyaya sorulan aynı sorunun sorgu
    sırasına göre farklı yanıt vermesine yol açardı.
    """

    def __init__(self, segs, *, junctions=(), anchors=()) -> None:
        self._segs: list[Segment] = list(segs)
        self._junctions: list[Point] = [_pt(*p) for p in junctions]
        self._parent: dict[Point, Point] = {}
        # 1) Aynı noktada biten iki tel bağlıdır — junction gerekmez.
        for a, b in self._segs:
            self._union(a, b)
        # 2) Junction, ÜZERİNDE olduğu her teli birleştirir. T bağlantısını
        #    ve X kesişimini bağlantıya çeviren tek yapı budur.
        for j in self._junctions:
            for a, b in self._segs:
                if _on_segment(j, a, b):
                    self._union(j, a)
        # 3) Pin ankrajları: tolerans içinde bir tel UCUna oturuyorsa aynı
        #    düğümdür. Ankraj 2, tel ucu 3 ondalığa yuvarlanmış gelir;
        #    eşitlik yerine EPS ile bakılmasının tek sebebi budur.
        for p in sorted({_pt(*p) for p in anchors}):
            for a, b in self._segs:
                if _same(p, a):
                    self._union(p, a)
                elif _same(p, b):
                    self._union(p, b)

    def _find(self, p: Point) -> Point:
        self._parent.setdefault(p, p)
        while self._parent[p] != p:
            self._parent[p] = self._parent[self._parent[p]]
            p = self._parent[p]
        return p

    def _union(self, a: Point, b: Point) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[ra] = rb

    def touches_wire(self, p: Point) -> bool:
        """`p` bir telin ÜZERİNDE mi? — GEOMETRİK soru, elektriksel değil.

        Bir telin ortasından geçmek bağlanmak DEĞİLDİR (bkz. başlıktaki
        ölçüm). Bu ayrım `component_of`'un işidir; bu yordam yalnızca "kablo
        oradan geçiyor ama bağlamıyor" bulgusunu yazabilmek için vardır.
        """
        return any(_on_segment(p, a, b) for a, b in self._segs)

    def component_of(self, p: Point):
        """`p`'nin ELEKTRİKSEL olarak bulunduğu iletkenin kimliği; yoksa None.

        Kimlik birleşim-bul kökü olarak döner: aynı iletkendeki iki nokta
        aynı kökü verir, karşılaştırılabilir ve hash'lenebilir.
        """
        p = _pt(*p)
        root = None
        for a, b in self._segs:
            if _same(p, a) or _same(p, b):
                self._union(p, a if _same(p, a) else b)
                root = self._find(p)
        if any(_same(p, j) for j in self._junctions):
            for a, b in self._segs:
                if _on_segment(p, a, b):
                    self._union(p, a)
                    root = self._find(p)
        return root

    def connected(self, p: Point, q: Point) -> bool:
        """`p` ile `q` KESİNTİSİZ tel yoluyla mı bağlı?"""
        rp, rq = self.component_of(p), self.component_of(q)
        return rp is not None and rp == rq


# --------------------------------------------------------------------------- #
# Sayfa modeli                                                                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Placement:
    """Sayfaya YERLEŞTİRİLMİŞ bir sembol örneği (kütüphane girdisi değil)."""

    ref: str
    lib_id: str
    value: str
    unit: int
    placed: Placed

    @property
    def is_power(self) -> bool:
        """Referansı `#` ile başlayan sembol netliste DÜĞÜM OLARAK GEÇMEZ.

        Bu bir politika değil, KiCad'in davranışıdır: `#PWR`/`#FLG` sembolleri
        net'e ADINI verir ama `(node ...)` üretmez. B/G kontrolleri bu yüzden
        onları netlistte aramaz — arasalar her güç sembolü "netlistte yok"
        diye raporlanırdı.
        """
        return self.ref.startswith("#")


@dataclass
class Sheet:
    """Bir `.kicad_sch` sayfasının denetime konu olan tüm geometrisi."""

    path: Path
    placements: list[Placement] = field(default_factory=list)
    anchors: dict[str, Point] = field(default_factory=dict)
    owner: dict[str, Placement] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    junctions: list[Point] = field(default_factory=list)
    no_connects: list[Point] = field(default_factory=list)
    labels: list[tuple[str, Point, str]] = field(default_factory=list)
    sheet_pins: list[Point] = field(default_factory=list)
    notices: list[dict] = field(default_factory=list)


def _finding(check: str, severity: str, where: str, message: str) -> dict:
    return {"check": check, "severity": severity, "where": where,
            "message": message}


def _has_common_pins(symbol_text: str) -> bool:
    """Sembolün `NAME_0_<style>` (tüm birimlerde ortak) bloğunda pin var mı?

    Blok sınırı `block_at` ile bulunur, girintiye ya da bir sonraki
    `(symbol` satırına bakarak değil: üretecin sarmalama seviyesi değişince
    metin tabanlı bir sınır sessizce kayar.
    """
    for match in re.finditer(r'\(symbol "[^"]*_0_\d+"', symbol_text):
        body = block_at(symbol_text, match.start())
        if body and re.search(r"\(pin\s+\w+", body):
            return True
    return False


def read_sheet(sch: Path) -> Sheet:
    """`.kicad_sch` dosyasını tek geçişte okur.

    Dosya iki kez ayrıştırılır ve bu bilinçlidir: `load_lib_symbols`
    `lib_symbols` bloğunu kendi ham-metin tarayıcısıyla okur (pin gövdesini
    de saklar), geri kalan her şey ağaç üzerinden gezilir. Alternatif,
    `lib_symbols` için İKİNCİ bir pin okuyucu yazmaktı — bu modülün
    varlık sebebi tam olarak "aynı şeyin iki uygulaması sessizce ayrışır".
    """
    sch = Path(sch)
    text = sch.read_text(encoding="utf-8")
    libs = load_lib_symbols(str(sch))
    root = parse_sexpr(text)
    if not root or root[0] != "kicad_sch":
        raise ValueError(f"{sch.name}: kicad_sch olarak ayrıştırılamadı")

    sheet = Sheet(path=sch)

    for node in find_all(root, "symbol"):
        lib_node = find(node, "lib_id")
        at = find(node, "at")
        if lib_node is None or at is None:
            continue                                # yerleşim değil
        lib_id = unquote(lib_node[1])
        props = {unquote(p[1]): unquote(p[2])
                 for p in find_all(node, "property") if len(p) > 2}
        ref = props.get("Reference", "")
        unit_node = find(node, "unit")
        unit = int(unit_node[1]) if unit_node else 1
        mirror_node = find(node, "mirror")
        mirror = mirror_node[1] if mirror_node else ""
        sym = libs.get(lib_id)
        if sym is None:
            # Gömülü sembol yoksa pin koordinatı UYDURULAMAZ. Sessizce
            # atlamak bu sembolün tüm pinlerini denetimden çıkarırdı.
            sheet.notices.append(_finding(
                "0-input", "error", ref or lib_id,
                f"{lib_id} lib_symbols icinde yok — bu sembolun pinleri "
                "denetlenemedi"))
            continue
        placed = Placed(ref=ref, lib_id=lib_id, x=float(at[1]), y=float(at[2]),
                        rot=float(at[3]) if len(at) > 3 else 0.0,
                        mirror=mirror, unit=unit, sym=sym)
        entry = Placement(ref=ref, lib_id=lib_id, value=props.get("Value", ""),
                          unit=unit, placed=placed)
        sheet.placements.append(entry)
        if unit != 1 and _has_common_pins(sym.text):
            # `sch_wire.load_lib_symbols` alt birim adından birimi
            # `int(...) or 1` ile okur, yani ORTAK blok (`NAME_0_1`) birim
            # 1'e düşer. Birim 2+ yerleşimlerinde o ortak pinler bu sayfada
            # hiç ankraj almaz — kusur değil, KÖRLÜK olur: eksik pin sessizce
            # denetim dışı kalır. Modül tek dosya olduğu için düzeltme orada
            # yapılamaz; körlük en azından raporlanır.
            sheet.notices.append(_finding(
                "0-input", "warning", f"{ref} (unit {unit})",
                f"{lib_id} ortak (_0_) blokta pin tasiyor; yukleyici ortak "
                "pinleri birim 1'e atadigi icin bu yerlesimin ortak pinleri "
                "DENETLENMEDI"))
        # Çok birimli sembolde AYNI ref birden çok kez yerleştirilir; her
        # yerleşim yalnız kendi biriminin pinlerini taşır (unit 0 = ortak).
        for pin in sym.pins:
            if pin.unit not in (0, unit):
                continue
            key = f"{ref}.{pin.number}"
            if key in sheet.anchors:
                sheet.notices.append(_finding(
                    "0-input", "warning", key,
                    "ayni ref.pin iki kez yerlestirilmis — ilk yerlesim "
                    "esas alindi"))
                continue
            sheet.anchors[key] = _anchor(placed, pin)
            sheet.owner[key] = entry

    for node in find_all(root, "wire"):
        pts_node = find(node, "pts")
        if not pts_node:
            continue
        pts = [_pt(xy[1], xy[2]) for xy in find_all(pts_node, "xy")]
        sheet.segments += list(zip(pts, pts[1:]))

    for node in find_all(root, "junction"):
        at = find(node, "at")
        if at:
            sheet.junctions.append(_pt(at[1], at[2]))

    for node in find_all(root, "no_connect"):
        at = find(node, "at")
        if at:
            sheet.no_connects.append(_pt(at[1], at[2]))

    for kind in ("label", "global_label", "hierarchical_label"):
        for node in find_all(root, kind):
            at = find(node, "at")
            if at:
                sheet.labels.append((unquote(node[1]), _pt(at[1], at[2]), kind))

    # Hiyerarşik sayfa kutularının pinleri: tel BURADA da bitebilir. D
    # kontrolü onları tanımazsa, üst sayfadaki her tel "havada" raporlanır.
    for node in find_all(root, "sheet"):
        for pin in find_all(node, "pin"):
            at = find(pin, "at")
            if at:
                sheet.sheet_pins.append(_pt(at[1], at[2]))

    if re.search(r"\(bus\b|\(bus_entry\b", text):
        sheet.notices.append(_finding(
            "0-input", "info", sheet.path.name,
            "sayfada bus/bus_entry var — bu modul bus'lari MODELLEMEZ; "
            "bus'a baglanan pinler 'aski' (A) ve tel uclari 'oturmamis' (D) "
            "goruunebilir"))
    return sheet


# --------------------------------------------------------------------------- #
# Yeniden kullanılabilir sorgular (modülün dışa açık küçük yüzü)               #
# --------------------------------------------------------------------------- #
def pin_anchors(sch: Path) -> dict[str, Point]:
    """`{"REF.PIN": (x, y)}` — her pinin sayfa üzerindeki elektriksel ankrajı."""
    return read_sheet(sch).anchors


def segments(sch: Path) -> list[Segment]:
    """Sayfadaki tüm tel parçaları `[(p, q), ...]`. Bus/etiket okunmaz."""
    return read_sheet(sch).segments


# --------------------------------------------------------------------------- #
# Denetim bağlamı                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class Model:
    """Kontrollerin ortak girdisi: geometri + netlist + ikisinin eşlemesi."""

    sheet: Sheet
    graph: WireGraph
    rules: dict
    nets: dict[str, list[tuple[str, str]]]
    net_of: dict[str, str]                 # "REF.PIN" -> net adı
    comp_of: dict[str, object]             # "REF.PIN" -> iletken kimliği
    names_of: dict[object, set]            # iletken kimliği -> net adları

    def is_power_pin(self, key: str) -> bool:
        owner = self.sheet.owner.get(key)
        if owner is None:
            return False
        return owner.lib_id.startswith(tuple(self.rules["power_lib_prefixes"]))


def _build_model(sheet: Sheet, rules: dict, nets) -> Model:
    graph = WireGraph(sheet.segments, junctions=sheet.junctions,
                      anchors=sheet.anchors.values())
    net_of: dict[str, str] = {}
    for net, nodes in nets.items():
        for ref, pin in nodes:
            net_of.setdefault(f"{ref}.{pin}", net)

    comp_of = {key: graph.component_of(xy) for key, xy in sheet.anchors.items()}
    names_of: dict[object, set] = {}
    for key, root in comp_of.items():
        if root is None:
            continue
        bucket = names_of.setdefault(root, set())
        if key in net_of:
            bucket.add(net_of[key])
        owner = sheet.owner[key]
        # Güç sembolü netliste düğüm üretmez ama net'e ADINI verir; bir
        # çekme direncinin ray ucu neredeyse her zaman böyle adlanır.
        if owner.is_power and owner.value:
            bucket.add(owner.value)
    return Model(sheet=sheet, graph=graph, rules=rules, nets=nets,
                 net_of=net_of, comp_of=comp_of, names_of=names_of)


def _matches(name: str, patterns) -> bool:
    """Net adı listedeki bir ada/glob'a uyuyor mu?

    Yerel etiketli net'ler netlistte sayfa yolu önekiyle görünür
    (`/Guc/VIN_3V3`); global etiket ve güç sembolü adları öneksizdir. Aynı
    kural iki farklı biçimde yazılmasın diye ikisi de denenir.
    """
    tail = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(candidate, pattern)
               for pattern in patterns for candidate in (name, tail))


def _reaches(target: str, names) -> bool:
    """Bir iletken `target` adını taşıyor mu?

    `_matches`'tan ayrı: orada desen kuraldan gelir, burada iki tarafta da
    GERÇEK net adları vardır ve sayfa yolu öneki her iki tarafta da olabilir
    (kural `SCCB_SCL` yazar, netlist `/Saat ve kimlik/SCCB_SCL` der).
    """
    tail = target.rsplit("/", 1)[-1]
    return any(name == target or name.rsplit("/", 1)[-1] == tail
               for name in names)


# --------------------------------------------------------------------------- #
# A — askıda pin                                                               #
# --------------------------------------------------------------------------- #
def check_dangling(model: Model) -> list[dict]:
    """Her pin ya bir tele BAĞLI olmalı ya da `no_connect` taşımalı."""
    out = []
    for key, xy in sorted(model.sheet.anchors.items()):
        if model.is_power_pin(key):
            continue
        if model.comp_of[key] is not None:
            continue
        if any(_same(xy, nc) for nc in model.sheet.no_connects):
            continue
        # Pinin dibindeki etiket KiCad'de bağlar ama TEL değildir; bunu
        # mesajda söylemek, düzeltmeyi yapacak kişiye "burada zaten bir ad
        # var, eksik olan çizgi" bilgisini verir.
        at_pin = [text for text, pos, _ in model.sheet.labels if _same(xy, pos)]
        detail = (f" Pin uzerinde etiket var ({', '.join(sorted(at_pin))}): "
                  "ad bagliyor, tel baglamiyor." if at_pin else "")
        if model.graph.touches_wire(xy):
            # En sinsi hâli: tel pinin ÜSTÜNDEN geçiyor, çizim bağlı görünüyor,
            # KiCad bağlamıyor. Junction konmadıkça pin havadadır.
            out.append(_finding(
                "A-dangling", "error", key,
                f"pin ({xy[0]}, {xy[1]}) bir telin ORTASINDA kaliyor ama o "
                "noktada junction yok — KiCad bunu BAGLI SAYMAZ; cizim bagli "
                f"gorunur, netlist pini ayirir.{detail}"))
            continue
        nearest = min((math.dist(xy, p) for seg in model.sheet.segments
                       for p in seg), default=float("inf"))
        out.append(_finding(
            "A-dangling", "error", key,
            f"pin ({xy[0]}, {xy[1]}) hicbir tel ucuna oturmuyor ve "
            f"no_connect da yok; en yakin tel ucu {nearest:.2f} mm.{detail}"))
    return out


# --------------------------------------------------------------------------- #
# B — bağlantı adla mı kuruldu (başlık kontrolü)                               #
# --------------------------------------------------------------------------- #
def check_label_only(model: Model) -> tuple[list[dict], dict]:
    """Netlistteki her net'in bu sayfadaki pinlerini TEL bileşenlerine böler.

    Bir net birden çok bileşene düşüyorsa, bileşenler arası bağlantı tel ile
    değil ADLA (etiket ya da güç sembolü) kurulmuştur. Netlist bunu göremez;
    tek fark burada, geometridedir.
    """
    findings: list[dict] = []
    report: dict[str, dict] = {}
    if not model.nets:
        return findings, report

    off_sheet_total = 0
    for net, nodes in sorted(model.nets.items()):
        keys = sorted({f"{ref}.{pin}" for ref, pin in nodes})
        on = [k for k in keys if k in model.sheet.anchors]
        off = [k for k in keys if k not in model.sheet.anchors]
        off_sheet_total += len(off)
        if not on:
            continue

        groups: dict[object, list[str]] = {}
        for key in on:
            root = model.comp_of[key]
            # Hiçbir tele değmeyen pin kendi başına bir adadır: onu diğer
            # telsiz pinlerle aynı kovaya koymak, olmayan bir bağlantı
            # uydurmak olurdu.
            groups.setdefault(root if root is not None else ("free", key),
                              []).append(key)
        components = sorted((sorted(g) for g in groups.values()),
                            key=lambda g: g[0])

        report[net] = {
            "pins": on,
            "components": components,
            # "wired" = bu sayfadaki her uye TELIN uzerinde ve hepsi TEK
            # iletkende. Yalnizca "tek bilesen" demek yetmez: hicbir tele
            # degmeyen tek bir pin de tek bilesen sayilir ve o net "telle
            # kuruldu" diye raporlanirdi (no_connect'li pinlerde gorulen
            # tam olarak buydu).
            "wired": (len(components) == 1
                      and all(model.comp_of[k] is not None for k in on)),
            "stitched_by_name": len(components) > 1,
        }
        if len(components) < 2:
            continue

        severity = ("warning" if _matches(net, model.rules["name_stitched_ok"])
                    else "error")
        islands = "; ".join(f"ada{i + 1}={{{', '.join(g)}}}"
                            for i, g in enumerate(components))
        extra = (f" Bu net'in {len(off)} uyesi baska sayfada — adalar orada "
                 "birlesiyor olabilir, ama BU sayfada aralarinda tel yok."
                 if off else "")
        note = (" (bu ad, rules['name_stitched_ok'] ile BELGELENMIS bir "
                "ray istisnasi)" if severity == "warning" else "")
        findings.append(_finding(
            "B-label-only", severity, net,
            f"{len(on)} pin {len(components)} ayri tel adasinda: {islands}. "
            f"Adalar arasi baglanti TEL degil AD ile kuruldu "
            f"(etiket/guc sembolu); netlist bu farki goremez.{extra}{note}"))

    if off_sheet_total:
        findings.append(_finding(
            "B-label-only", "info", model.sheet.path.name,
            f"netlistin {off_sheet_total} pin'i bu sayfada degil (netlist tum "
            "projeyi kapsiyor); o pinler geometrik olarak degerlendirilmedi"))
    return findings, report


# --------------------------------------------------------------------------- #
# C — olması gereken pasif                                                     #
# --------------------------------------------------------------------------- #
def check_required(model: Model) -> list[dict]:
    """`rules["required"]` girdilerini geometri üzerinde doğrular."""
    out: list[dict] = []
    for index, entry in enumerate(model.rules.get("required") or []):
        if not isinstance(entry, dict):
            out.append(_finding("C-missing-passive", "error",
                                f"required[{index}]",
                                "girdi bir nesne degil"))
            continue
        severity = entry.get("severity", "error")
        kind = entry.get("kind")
        if kind == "decoupling":
            out += _check_decoupling(model, entry, severity, index)
        elif kind == "pullup":
            out += _check_pullup(model, entry, severity, index)
        else:
            out.append(_finding("C-missing-passive", "error",
                                f"required[{index}]",
                                f"bilinmeyen kind={kind!r} — 'decoupling' ya "
                                "da 'pullup' bekleniyor"))
    return out


def _why(entry: dict) -> str:
    return f" GEREKCE: {entry['why']}" if entry.get("why") else ""


def _check_decoupling(model: Model, entry: dict, severity: str,
                      index: int) -> list[dict]:
    """Kondansatör var mı, TELLE mi bağlı ve pinin KAÇ mm dibinde?

    Üçü de aynı anda doğru olmadan bypass yoktur. Doğru net'te ama sayfanın
    öbür ucundaki bir kondansatör netlistte kusursuz görünür; ilmek alanı
    ise kondansatörün pine olan FİZİKSEL yakınlığıdır, net adı değil.
    """
    pin_key = str(entry.get("pin", ""))
    where = pin_key or f"required[{index}]"
    max_mm = float(entry.get("max_mm", 0) or 0)
    parts = entry.get("parts") or []
    if not pin_key or not parts:
        return [_finding("C-missing-passive", "error", where,
                         "decoupling girdisi 'pin' ve 'parts' ister")]

    pin_xy = model.sheet.anchors.get(pin_key)
    if pin_xy is None:
        return [_finding("C-missing-passive", "info", where,
                         f"{pin_key} bu sayfada yok — kural degerlendirilemedi"
                         f"{_why(entry)}")]

    out: list[dict] = []
    for part in parts:
        part_key = f"{part}.1"
        part_xy = model.sheet.anchors.get(part_key)
        if part_xy is None:
            out.append(_finding(
                "C-missing-passive", severity, where,
                f"{part} bu sayfada YOK; {pin_key} icin decoupling "
                f"dogrulanamaz — kondansator, besledigi pinle ayni sayfada "
                f"ve dibinde olmalidir{_why(entry)}"))
            continue
        if not model.graph.connected(pin_xy, part_xy):
            names = model.names_of.get(model.comp_of.get(part_key), set())
            out.append(_finding(
                "C-missing-passive", severity, where,
                f"{part}.1 ile {pin_key} arasinda TEL YOK "
                f"({part} su iletkende: {sorted(names) or 'telsiz'}); "
                f"ayni net adini tasimak bypass DEGILDIR{_why(entry)}"))
            continue
        distance = math.dist(pin_xy, part_xy)
        if max_mm and distance > max_mm:
            out.append(_finding(
                "C-missing-passive", severity, where,
                f"{part}.1 dogru iletkende ama {pin_key}'e {distance:.1f} mm "
                f"uzakta (sinir {max_mm:g} mm); ilmek alani mesafeyle buyur, "
                f"net adiyla degil{_why(entry)}"))
    return out


def _check_pullup(model: Model, entry: dict, severity: str,
                  index: int) -> list[dict]:
    """Direncin bir ucu sinyalde, öbür ucu rayda — ikisi de TELLE."""
    net = str(entry.get("net", ""))
    part = str(entry.get("part", ""))
    rail = str(entry.get("to", ""))
    where = net or part or f"required[{index}]"
    if not (net and part and rail):
        return [_finding("C-missing-passive", "error", where,
                         "pullup girdisi 'net', 'part' ve 'to' ister")]
    if not model.nets:
        return [_finding("C-missing-passive", "info", where,
                         "netlist verilmedi — pullup topolojisi "
                         f"dogrulanamadi{_why(entry)}")]

    ends = [f"{part}.1", f"{part}.2"]
    missing = [k for k in ends if k not in model.sheet.anchors]
    if missing:
        return [_finding("C-missing-passive", severity, where,
                         f"{part} bu sayfada yok ya da iki uclu degil "
                         f"(eksik: {missing}){_why(entry)}")]

    # Ray ucu neredeyse her zaman bir güç sembolüne gider; o sembol netliste
    # düğüm üretmediği için `names_of` net adlarını pin'lerden VE güç
    # sembolünün Value'sundan toplar. Direncin hangi ucunun sinyalde olduğu
    # yerleşime göre değişir, o yüzden iki yön de denenir.
    reach = [model.names_of.get(model.comp_of[k], set()) for k in ends]
    forward = _reaches(net, reach[0]) and _reaches(rail, reach[1])
    reverse = _reaches(net, reach[1]) and _reaches(rail, reach[0])
    if forward or reverse:
        return []
    return [_finding(
        "C-missing-passive", severity, where,
        f"{part} bir ucuyla {net}'e, obur ucuyla {rail}'e TELLE baglanmis "
        f"degil; uclarin ulastigi iletkenler: {ends[0]}={sorted(reach[0])}, "
        f"{ends[1]}={sorted(reach[1])}{_why(entry)}")]


# --------------------------------------------------------------------------- #
# D — tel ucu oturmuş mu                                                       #
# --------------------------------------------------------------------------- #
def check_snap(model: Model) -> list[dict]:
    """Her tel ucu bir şeye OTURMALI; 1 mm'lik ıska bağlantı değildir."""
    sheet = model.sheet
    landing = (list(sheet.anchors.values()) + sheet.junctions
               + [pos for _, pos, _ in sheet.labels] + sheet.sheet_pins
               + sheet.no_connects)
    out: list[dict] = []
    seen: set[Point] = set()
    for index, (a, b) in enumerate(sheet.segments):
        for end in (a, b):
            if end in seen:
                continue
            seen.add(end)

            if not any(_same(end, target) for target in landing):
                other = any(
                    _on_segment(end, s, t) or _same(end, s) or _same(end, t)
                    for j, (s, t) in enumerate(sheet.segments) if j != index)
                if not other:
                    out.append(_finding(
                        "D-snap", "error", f"({end[0]}, {end[1]})",
                        "tel ucu hicbir seye oturmuyor: pin, junction, "
                        "etiket, sayfa pini ya da baska bir tel yok"))

            # "Yaklaşık doğru" koordinat, KiCad'de bağlantı DEĞİLDİR: sayfa
            # doğru görünür, netlist net'i ikiye böler. Uyarı olarak
            # raporlanan bir sessiz kopukluk, raporlanmamış demektir.
            near = sorted(
                ((math.dist(end, xy), key)
                 for key, xy in sheet.anchors.items()
                 if math.dist(end, xy) <= NEAR_MISS_MM),
                key=lambda item: item[0])
            if near and near[0][0] > EPS:
                distance, key = near[0]
                out.append(_finding(
                    "D-snap", "error", f"({end[0]}, {end[1]})",
                    f"tel ucu {key} pinine {distance:.3f} mm mesafede — "
                    "ustunde DEGIL. Bu 'yeterince yakin' koordinat KiCad'de "
                    "bagli sayilmaz; goz kontrolu bagli gorur"))
    return out


# --------------------------------------------------------------------------- #
# E — KiCad ERC                                                                #
# --------------------------------------------------------------------------- #
def check_erc(sheet: Sheet) -> list[dict]:
    """ERC'yi çalıştırır ve ihlalleri bulgulara katar.

    ERC çalıştırılamadıysa TEK bir `info` üretilir. Sessizce atlamak,
    raporun "ERC temiz" iddiasını hiç ölçmeden taşıması demek olurdu.

    Çıktı geçici dizine yazılır: denetleyicinin, denetlediği ağacın içine
    dosya bırakması kabul edilemez.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="wire_audit_erc_") as tmp:
            report = run_erc(str(sheet.path), str(Path(tmp) / "erc.json"))
    except (KicadCliError, OSError, ValueError, json.JSONDecodeError) as exc:
        return [_finding("E-erc", "info", sheet.path.name,
                         f"ERC CALISTIRILAMADI ({type(exc).__name__}: {exc}) — "
                         "bu raporda ERC hakkinda hicbir iddia YOKTUR")]

    out: list[dict] = []
    for violation in erc_violations(report):
        items = violation.get("items") or []
        where = (items[0].get("description") if items else None) or "?"
        severity = violation.get("severity", "warning")
        if severity not in ("error", "warning"):
            severity = "info"
        out.append(_finding(
            "E-erc", severity, where,
            f"{violation.get('type', '?')}: {violation.get('description', '')}"))
    if not out:
        out.append(_finding("E-erc", "info", sheet.path.name,
                            "ERC calisti, ihlal bildirmedi (sayfa TEK BASINA "
                            "acildi: baska sayfadan beslenen raylar burada "
                            "'driven' gorunmeyebilir)"))
    return out


# --------------------------------------------------------------------------- #
# F — junction                                                                 #
# --------------------------------------------------------------------------- #
def check_junction(model: Model) -> list[dict]:
    """Eksik nokta ve fazla nokta — ikisi de çizimi yalancı yapar."""
    sheet = model.sheet
    out: list[dict] = []

    endpoints = sorted({p for seg in sheet.segments for p in seg})
    for point in endpoints:
        touched = [(a, b) for a, b in sheet.segments if _interior(point, a, b)]
        if not touched:
            continue
        if any(_same(point, j) for j in sheet.junctions):
            continue
        at_pin = [k for k, xy in sheet.anchors.items() if _same(xy, point)]
        # Bu bulgu bilerek `error`: istek "warning" diyordu, gerekçesi de
        # "KiCad burayı bağlar, eksik olan yalnızca noktanın görsel dürüstlüğü"
        # idi. KiCad 10.0.5 ile ölçüldü ve o gerekçe DOĞRU DEĞİL — noktasız T
        # hiç bağlanmıyor (bkz. modül başlığındaki tablo). Yani bu kozmetik bir
        # eksiklik değil, sessiz bir kopukluk; kozmetik diye raporlanırsa
        # raporlanmamış olur.
        out.append(_finding(
            "F-junction", "error", f"({point[0]}, {point[1]})",
            f"tel ucu {len(touched)} telin ORTASINA degiyor ama junction yok — "
            "KiCad bu T'yi BAGLAMAZ (olculdu: KiCad 10.0.5). Cizim bagli "
            "gorunur, netlist iki ayri net uretir"
            + (f"; bu noktada pin de var: {sorted(at_pin)}" if at_pin else "")))

    for point in sorted(set(sheet.junctions)):
        ends = sum(1 for seg in sheet.segments for p in seg if _same(p, point))
        # Ortasından geçilen bir parça iki yöne devam eder: KiCad'in "kaç
        # bağlantı ucu birleşiyor" sayımında 2 sayılır (bkz. sch_wire.
        # auto_junctions).
        mids = sum(1 for a, b in sheet.segments if _interior(point, a, b))
        # PİN de bir iletkendir ve burada sayılmak ZORUNDADIR: bir dalın
        # ortasına denk gelen kondansatör pini KiCad'de ancak junction ile
        # bağlanır (ölçüldü: junction'sız hâlde netlist `unconnected-(C5001-Pad1)`
        # veriyor). O noktayı "fazla junction" saymak, tek doğru düzeltmeyi
        # hata diye raporlamak olurdu.
        pins = [k for k, xy in sheet.anchors.items() if _same(xy, point)]
        if ends + 2 * mids + len(pins) < 3:
            out.append(_finding(
                "F-junction", "error", f"({point[0]}, {point[1]})",
                f"junction var ama burada yalnizca {ends} tel ucu, {mids} tel "
                f"ortasi ve {len(pins)} pin birlesiyor (<3); nokta, var olmayan "
                "bir baglantiyi iddia ediyor"))
    return out


# --------------------------------------------------------------------------- #
# G — geometri ile netlist aynı şeyi mi söylüyor                               #
# --------------------------------------------------------------------------- #
def check_geometry(model: Model) -> list[dict]:
    """Telle bağlı iki pin AYNI net'te olmak ZORUNDADIR.

    Bu, modelin kendi kendini denetlemesidir. `_xform` yeniden kullanıldığı
    için buradaki bir hata sessiz kalabilir: yanlış ankraj, olmayan bir
    kopukluk uydurur ya da gerçek olanı gizler. Netlist ile geometri
    anlaşmıyorsa hangisinin yanlış olduğunu bu modül BİLEMEZ — o yüzden
    ikisinden birine güvenmek yerine anlaşmazlığı bildirir.

    Ters yön (aynı net'teki pinler telle bağlı mı) B kontrolüdür; burada
    tekrarlanmaz.
    """
    if not model.nets:
        return []

    out: list[dict] = []
    by_component: dict[object, list[str]] = {}
    unknown: list[str] = []
    for key, root in sorted(model.comp_of.items()):
        owner = model.sheet.owner[key]
        if owner.is_power:
            continue                                # netlistte düğüm üretmez
        if key not in model.net_of:
            unknown.append(key)
            continue
        if root is not None:
            by_component.setdefault(root, []).append(key)

    for root, keys in sorted(by_component.items()):
        names = sorted({model.net_of[k] for k in keys})
        if len(names) > 1:
            detail = ", ".join(f"{k}->{model.net_of[k]}" for k in sorted(keys))
            out.append(_finding(
                "G-geometry-consistency", "error", " ".join(names),
                f"tek bir kesintisiz iletken uzerindeki pinler netlistte "
                f"{len(names)} FARKLI net'te: {detail}. Ya geometri modeli "
                "yanlis (pin ankraji/donusum), ya cizimde kisa devre var — "
                "iki taniktan birine guvenmeden once bu cozulmeli"))

    if unknown:
        out.append(_finding(
            "G-geometry-consistency", "info", model.sheet.path.name,
            f"{len(unknown)} pin sayfada var ama netlistte yok "
            f"({', '.join(unknown[:8])}"
            f"{'...' if len(unknown) > 8 else ''}); netlist bu sayfanin "
            "surumune ait olmayabilir"))
    return out


# --------------------------------------------------------------------------- #
# Denetim                                                                      #
# --------------------------------------------------------------------------- #
def audit(sch: Path, netlist: Path | None = None,
          rules: dict | None = None) -> dict:
    """Sayfayı denetler ve JSON'a çevrilebilir raporu döndürür."""
    merged = dict(DEFAULT_RULES)
    merged.update(rules or {})
    sheet = read_sheet(Path(sch))
    nets = read_nets(netlist) if netlist else {}
    model = _build_model(sheet, merged, nets)

    findings = list(sheet.notices)
    findings += check_dangling(model)
    label_findings, nets_report = check_label_only(model)
    findings += label_findings
    findings += check_required(model)
    findings += check_snap(model)
    findings += check_erc(sheet)
    findings += check_junction(model)
    findings += check_geometry(model)
    findings.sort(key=lambda f: (f["check"], f["severity"], f["where"],
                                 f["message"]))

    power_prefixes = tuple(merged["power_lib_prefixes"])
    return {
        "sheet": sheet.path.name,
        "counts": {
            "symbols": len(sheet.placements),
            "pins": len(sheet.anchors),
            "wire_segments": len(sheet.segments),
            "junctions": len(sheet.junctions),
            "labels": len(sheet.labels),
            # Güç sembolü = ilan edilmiş güç kütüphanesinden gelen VEYA
            # referansı `#` ile başlayan sembol; ikincisi KiCad'in kendi
            # kuralıdır ve önek listesi eksik verildiğinde de doğrudur.
            "power_symbols": sum(1 for p in sheet.placements
                                 if p.lib_id.startswith(power_prefixes)
                                 or p.is_power),
            "no_connects": len(sheet.no_connects),
        },
        "findings": findings,
        "nets": nets_report,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sheet", type=Path,
                        help="denetlenecek .kicad_sch sayfasi")
    parser.add_argument("--netlist", type=Path,
                        help="kicad-cli sch export netlist --format kicadsexpr "
                             "ciktisi; B ve G kontrolleri BUNSUZ CALISMAZ")
    parser.add_argument("--rules", type=Path,
                        help="power_lib_prefixes / name_stitched_ok / required")
    parser.add_argument("--json", type=Path, help="raporu buraya yaz")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rules = (json.loads(args.rules.read_text(encoding="utf-8"))
             if args.rules else None)
    report = audit(args.sheet, args.netlist, rules)

    counts = report["counts"]
    severities = [f["severity"] for f in report["findings"]]
    errors = severities.count("error")
    warnings = severities.count("warning")

    diag("")
    diag(f"wire_audit: {report['sheet']}")
    diag(f"  sembol {counts['symbols']}  pin {counts['pins']}  "
         f"tel {counts['wire_segments']}  junction {counts['junctions']}  "
         f"etiket {counts['labels']}  guc {counts['power_symbols']}  "
         f"NC {counts['no_connects']}")
    if not args.netlist:
        diag("  NOT: --netlist verilmedi; B (adla birlestirme) ve G "
             "(geometri/netlist tutarliligi) CALISTIRILMADI")
    for finding in report["findings"]:
        if finding["severity"] == "info":
            continue
        diag(f"  {finding['severity'].upper():7s} {finding['check']:24s} "
             f"{finding['where']}: {finding['message']}")
    stitched = sorted(n for n, d in report["nets"].items()
                      if d["stitched_by_name"])
    if stitched:
        diag(f"  ADLA birlestirilen net ({len(stitched)}): "
             f"{', '.join(stitched)}")
    diag(f"  sonuc: {errors} error / {warnings} warning / "
         f"{severities.count('info')} info")

    write_report(report, args.json)
    emit(report)
    return EXIT_FAIL if errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
