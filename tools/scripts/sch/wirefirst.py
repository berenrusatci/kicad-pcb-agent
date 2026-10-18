"""wirefirst — `sch_wire` üzerine **TEL-ÖNCE** yönlendirme katmanı.

Neden var
---------
`sch_wire.SchBuilder` gerçek `(wire ...)` üretebiliyor; eksik olan şey
**varsayılan davranış**tı. Ajan script'lerinin elindeki en kısa yol

    b.label(u1.pin("5"), "CAM_CLK", "global_label")
    b.label(u2.pin("8"), "CAM_CLK", "global_label")

olduğu için iki komşu bileşen arasındaki bağlantı bile **ada** bırakılıyordu.
Netlist doğru çıkıyor, şema yalan söylüyor: aradaki seri direnç başka bir
köşede duruyor, IC'nin decoupling'i başka bir rayın üstünde asılı kalıyor ve
devrenin çalışma mantığı sayfadan **okunamıyor**.

Bu modül o kısa yolu kapatır. Sunduğu ilkeller bağlantıyı **çizgiyle** kurar;
etiket ancak `label()`'a bir GEREKÇE verilerek konabilir ve gerekçe sabit bir
listeden seçilmek zorundadır. "Kısa yol olsun diye" gerekçesi listede yoktur.

    ESKİ:  connect(a, b) -> create_label(a, net); create_label(b, net)
    YENİ:  connect(a, b) -> wire_segments(route(a, b))

Kanıt disiplini
---------------
Bu modülün çizdiği hiçbir şey "bağlandı" iddiasının kanıtı değildir. İki
bağımsız tanık gerekir:

  1. `kicad-cli sch export netlist`  -> elektriksel bağlantı GERÇEKTEN var mı
  2. `wire_audit.py` tel grafı       -> bağlantı TELLE mi kuruldu, adla mı

İkisi de aynı yönü göstermeden bir iş "tamam" sayılmaz.

Kullanım
--------
    wf = WireFirst(builder)
    v3 = wf.trunk("VIN_3V3", y=360.0, span=(120.0, 690.0))   # omurga tel
    wf.tap(v3, u302.pin("1"))                                # omurga -> pin
    wf.link(u401.pin("3"), j101.pin("17"), net="XVCLK")      # pin -> pin
    wf.series(mcu.pin("TX"), [r12], cam.pin("RX"))           # araya PASİF
    wf.decouple(u402.pin("6"), [c5003], gnd=sheet.gnd)       # pinin DİBİNDE
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sch_wire import PinRef, Placed, SchBuilder, route  # noqa: E402

GRID = 2.54
EPS = 5e-3


# --------------------------------------------------------------------------- #
# Etiket politikası                                                            #
# --------------------------------------------------------------------------- #
#: Bir etiketin meşru olabileceği YEGANE gerekçeler. Liste bilerek kısadır:
#: buraya bir gerekçe eklemek, "şu durumda tel çizmiyoruz" diye bir karar
#: vermektir ve o kararın yazılı bir sebebi olmalıdır.
LABEL_REASONS: dict[str, str] = {
    # Bağlantıyı TEL yapar; etiket yalnız ada verir. Netclass, .kicad_dru
    # kuralı ve arayüz sözleşmesi net'i ADIYLA adresler; ad olmazsa KiCad
    # `Net-(D101-IO1)` üretir ve o ad diyot yeniden numaralanınca değişir.
    "naming-on-wire": "Sürekli telin üzerinde ad — bağlantıyı tel kurar",
    # Güç sembolü: hem net adı hem ERC'nin 'power input not driven' tanığı.
    "power-rail": "Güç rayı sembolü (ad + ERC sürücüsü)",
    # Yalnız çok sayfalı tasarımda. Tek sayfada bu gerekçe KULLANILAMAZ.
    "sheet-to-sheet": "Sayfa sınırını geçen bağlantı",
    # Gerçekten kart genelinde olan sinyal (reset ağacı, tek telli sistem
    # senkronu). Yakın iki bileşen arası bağlantı BU DEĞİLDİR.
    "global-system": "Gerçek global sistem sinyali",
}


class LabelPolicyError(ValueError):
    """Gerekçesiz ya da tek sayfada geçersiz gerekçeli etiket denemesi."""


@dataclass
class LabelUse:
    """Konan her etiketin kaydı — `wire_audit` bunu denetler."""

    text: str
    at: tuple[float, float]
    kind: str
    reason: str
    note: str = ""


# --------------------------------------------------------------------------- #
# Omurga (trunk) — çizilmiş güç/sinyal hattı                                   #
# --------------------------------------------------------------------------- #
@dataclass
class Trunk:
    """Sayfada GERÇEKTEN çizilmiş yatay ya da dikey omurga tel.

    `bypass()` tarzı "kondansatörü rayın adına bağla" yaklaşımının yerine
    geçer: ray artık bir ad değil, üzerine dokunulabilen bir çizgidir.
    """

    net: str
    horizontal: bool
    coord: float                      # yatay ise y, dikey ise x
    lo: float
    hi: float
    taps: list[tuple[float, float]] = field(default_factory=list)

    def point(self, along: float) -> tuple[float, float]:
        """Omurga üzerinde bir nokta. `along` yatayda x, dikeyde y'dir."""
        if not (self.lo - EPS <= along <= self.hi + EPS):
            raise ValueError(
                f"{self.net}: dokunma noktasi {along} omurga disinda "
                f"[{self.lo}, {self.hi}] — omurgayi uzat ya da baska yerden dokun")
        return ((round(along, 2), self.coord) if self.horizontal
                else (self.coord, round(along, 2)))

    def nearest(self, xy: tuple[float, float]) -> tuple[float, float]:
        """Verilen noktanın omurgaya dik izdüşümü (aralığa kırpılmış)."""
        along = xy[0] if self.horizontal else xy[1]
        return self.point(min(max(along, self.lo), self.hi))


# --------------------------------------------------------------------------- #
# Tel-önce yönlendirici                                                        #
# --------------------------------------------------------------------------- #
class WireFirst:
    """`SchBuilder`'ı tel-önce ilkellerle sarar.

    Her ilkel, ürettiği geometriyi `self.drawn` altında net adına göre
    kaydeder; denetleyici "bu net telle mi kuruldu" sorusunu üretecin
    iddiasıyla değil, dosyadaki geometriyle karşılaştırarak yanıtlar.
    """

    def __init__(self, b: SchBuilder, *, single_sheet: bool = True) -> None:
        self.b = b
        self.single_sheet = single_sheet
        self.labels: list[LabelUse] = []
        self.drawn: dict[str, list[tuple[float, float]]] = {}
        self.trunks: dict[str, Trunk] = {}

    # -- kayıt ------------------------------------------------------------- #
    def _touch(self, net: str | None, *pts: tuple[float, float]) -> None:
        if not net:
            return
        self.drawn.setdefault(net, []).extend((round(x, 2), round(y, 2))
                                              for x, y in pts)

    # -- omurga ------------------------------------------------------------- #
    def trunk(self, net: str, *, span: tuple[float, float],
              y: float | None = None, x: float | None = None) -> Trunk:
        """Yatay (`y=`) ya da dikey (`x=`) omurga teli çizer ve döndürür.

        Rayı ÇİZGİ yapan şey budur: sonraki her tüketici bu çizgiye dokunur,
        rayın adına değil.
        """
        if (y is None) == (x is None):
            raise ValueError("trunk(): y= ya da x= — tam olarak biri")
        lo, hi = round(min(span), 2), round(max(span), 2)
        t = Trunk(net=net, horizontal=y is not None,
                  coord=round(y if y is not None else x, 2), lo=lo, hi=hi)
        a, b = t.point(lo), t.point(hi)
        self.b.wire([a, b], key=f"trunk/{net}/{t.coord}")
        self._touch(net, a, b)
        self.trunks[net] = t
        return t

    # -- omurga <-> pin ----------------------------------------------------- #
    def tap(self, t: Trunk, pin: PinRef, *, along: float | None = None,
            net: str | None = None) -> tuple[float, float]:
        """Omurgadan pine ortogonal iniş. Dönüş: omurgadaki dokunma noktası.

        Dokunma noktası omurganın ORTASINA düşerse `auto_junctions()` oraya
        junction koyar — junction'sız bir T, KiCad'de bağlantı DEĞİLDİR.
        """
        if along is None:
            along = pin.x if t.horizontal else pin.y
        p = t.point(round(along, 2))
        pts = route(p, pin.xy, (0.0, 1.0) if t.horizontal else (1.0, 0.0))
        self.b.wire(pts, key=f"tap/{t.net}/{pin.ref}.{pin.pin.number}")
        t.taps.append(p)
        self._touch(net or t.net, *pts)
        return p

    def tap_point(self, t: Trunk, along: float, to: tuple[float, float],
                  *, net: str | None = None) -> tuple[float, float]:
        """Omurgadan serbest bir noktaya iniş (pasif eleman ucu için)."""
        p = t.point(round(along, 2))
        pts = route(p, (round(to[0], 2), round(to[1], 2)),
                    (0.0, 1.0) if t.horizontal else (1.0, 0.0))
        self.b.wire(pts, key=f"tap/{t.net}/{p[0]},{p[1]}")
        t.taps.append(p)
        self._touch(net or t.net, *pts)
        return p

    # -- pin <-> pin --------------------------------------------------------- #
    def link(self, a: PinRef | tuple[float, float], b: PinRef | tuple[float, float],
             *, net: str | None = None, jog: float | None = None,
             key: str | None = None) -> list[tuple[float, float]]:
        """İki uç arasında GERÇEK tel. `jog` verilirse Z kırığı o eksende yapılır.

        `jog`: iki pin de aynı yöne bakıyorsa (ör. ikisi de sağa çıkıyorsa) düz
        L yolu gövdenin üstünden geçer; ara bir x/y vererek yol dışarıdan
        dolaştırılır. Çapraz tel ÜRETİLMEZ — 90° kırıklar okunur, çapraz değil.
        """
        pa = a.xy if isinstance(a, PinRef) else (round(a[0], 2), round(a[1], 2))
        pb = b.xy if isinstance(b, PinRef) else (round(b[0], 2), round(b[1], 2))
        a_dir = (a.dx, a.dy) if isinstance(a, PinRef) else None

        if jog is None:
            pts = route(pa, pb, a_dir)
        elif a_dir is not None and abs(a_dir[0]) > abs(a_dir[1]):
            pts = [pa, (jog, pa[1]), (jog, pb[1]), pb]        # dikey Z
        else:
            pts = [pa, (pa[0], jog), (pb[0], jog), pb]        # yatay Z
        pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        k = key or (f"link/{_tag(a)}-{_tag(b)}")
        self.b.wire(pts, key=k)
        self._touch(net, *pts)
        return pts

    # -- seri pasif (§5) ------------------------------------------------------ #
    def series(self, a, parts: list[Placed], b, *, net: str | None = None,
               jogs: list[float] | None = None) -> None:
        """`a ── P1 ── P2 ── b`: zincirin HER halkası gerçek tel.

        Seri direnç/ferrit/termination sinyal yolunun ÜZERİNDE durmalıdır.
        `MCU_TX ─ R12 ─ CAM_RX` ile "R12 köşede, bağlantı etiketle" arasındaki
        fark, layout'ta izin nereden geçtiğidir; şema ikincisini gizler.

        Elemanın hangi ucunun giriş olduğu YÖNELİM PARAMETRESİNDEN değil,
        gelinen noktaya olan uzaklıktan bulunur: rot/mirror birleşimlerinde
        pin 1 ile pin 2 yer değiştirir ve o hata netlistte sessiz kalır.
        """
        if not parts:
            raise ValueError("series(): pasif listesi bos — link() kullan")
        cur = a
        jogs = list(jogs or [])
        for i, part in enumerate(parts):
            p_in, p_out = _ends_near(part, _pt(cur))
            self.link(cur, p_in, net=net,
                      jog=jogs[i] if i < len(jogs) else None,
                      key=f"series/{_tag(cur)}-{part.ref}")
            cur = p_out
        self.link(cur, b, net=net,
                  jog=jogs[len(parts)] if len(parts) < len(jogs) else None,
                  key=f"series/{_tag(cur)}-{_tag(b)}")

    # -- şönt pasif (decoupling / pull-up / termination) ---------------------- #
    def shunt(self, at, part: Placed, sink, *, net: str | None = None,
              sink_kw: dict | None = None) -> None:
        """Düğüm ── PASİF ── (GND/ray).

        `sink` bir çağrılabilir (ör. `Sheet.gnd`), bir `PinRef` ya da nokta
        olabilir. Elemanın yakın ucu `at`'a uzaklıktan seçilir.
        """
        near, far = _ends_near(part, _pt(at))
        self.link(at, near, net=net, key=f"shunt/{_tag(at)}-{part.ref}")
        if callable(sink):
            sink(far, **(sink_kw or {}))
        else:
            self.link(far, sink, key=f"shunt/{part.ref}-{_tag(sink)}")

    def decouple(self, pin: PinRef, caps: list[Placed], gnd, *,
                 net: str | None = None, drop: float = GRID * 2) -> tuple[float, float]:
        """IC besleme pininin DİBİNE decoupling. Dönüş: pin dalının ucu.

        `IC_VDD -> 3V3` + "kondansatör başka yerde, rayın üstünde asılı" kabul
        edilmez: bypass'ın anlamı IC besleme pinine olan İLMEK ALANIdır, net
        adı değil. Kondansatörler pinden çıkan dalın üzerine dizilir.
        """
        stem = pin.out(drop)
        self.link(pin, stem, net=net, key=f"dec/{pin.ref}.{pin.pin.number}/stem")
        for c in caps:
            near, far = _ends_near(c, stem)
            self.link(stem, near, net=net, key=f"dec/{pin.ref}/{c.ref}")
            gnd(far)
        return stem

    # -- etiket (gerekçe ZORUNLU) --------------------------------------------- #
    def label(self, at, text: str, reason: str, *, kind: str = "global_label",
              note: str = "") -> None:
        """Etiket koyar — ancak listeden bir GEREKÇE ile.

        Etiket, component-to-component wiring'in yerine geçen bir kısayol
        değildir. `naming-on-wire` gerekçesi telin varlığını VARSAYAR; denetim
        o telin gerçekten sürekli olduğunu ayrıca ölçer.
        """
        if reason not in LABEL_REASONS:
            raise LabelPolicyError(
                f"'{text}': gecersiz etiket gerekcesi {reason!r}. "
                f"Gecerli olanlar: {sorted(LABEL_REASONS)}")
        if self.single_sheet and reason == "sheet-to-sheet":
            raise LabelPolicyError(
                f"'{text}': tek sayfali tasarimda 'sheet-to-sheet' gerekcesi "
                "gecersiz — sayfa siniri yok, tel cizilebilir")
        xy = at.out() if isinstance(at, PinRef) else (round(at[0], 2), round(at[1], 2))
        self.b.label(at, text, kind)
        self.labels.append(LabelUse(text=text, at=xy, kind=kind, reason=reason,
                                    note=note))

    # -- pin ortada kalan dallar ---------------------------------------------- #
    def close_pin_taps(self, pins: list[PinRef]) -> list[dict]:
        """Bir telin ORTASINA denk gelen pin ankrajlarına junction koyar.

        KiCad'in bağlanma kuralı burada asimetriktir ve sessizce ısırır:

            tel UCU  başka telin ortasına değerse   -> BAĞLANIR
            PIN      bir telin ortasına denk gelirse -> BAĞLANMAZ (junction şart)

        `auto_junctions()` yalnız tel uçlarına bakar, pinleri hiç görmez; bir
        dalın üzerine dizilen üç kondansatörden sonuncusu (dalın ucundaki)
        bağlanır, ortadakiler bağlanmaz. Netlist bunu `unconnected-(C5001-Pad1)`
        diye gösterir ama şema göz kontrolünde KUSURSUZ görünür — çizgi
        gerçekten pinin üstünden geçmektedir.

        Eklenen her junction rapora yazılır: sessizce bağlanan bir şey,
        sessizce yanlış bağlanmış olabilir.
        """
        segs = [(a, b) for pts in self.b._wire_pts for a, b in zip(pts, pts[1:])]
        ends = {p for pts in self.b._wire_pts for p in pts}
        added: list[dict] = []
        for pr in pins:
            p = (round(pr.x, 2), round(pr.y, 2))
            if p in ends:
                continue                      # zaten tel UCU -> KiCad bağlar
            if any(_interior(p, a, b) for a, b in segs):
                self.b.junction(p)
                added.append({"ref": pr.ref, "pin": pr.pin.number,
                              "name": pr.pin.name, "at": list(p)})
        self.pin_taps = added
        return added

    # -- rapor ---------------------------------------------------------------- #
    def label_report(self) -> list[dict]:
        return [{"net": l.text, "at": list(l.at), "kind": l.kind,
                 "reason": l.reason, "why": LABEL_REASONS[l.reason],
                 "note": l.note} for l in self.labels]


# --------------------------------------------------------------------------- #
# Yardımcılar                                                                  #
# --------------------------------------------------------------------------- #
def _ends_near(part: Placed, ref: tuple[float, float]) -> tuple[PinRef, PinRef]:
    """İki uçlu elemanın (yakın uç, uzak uç) pinleri — `ref` noktasına göre.

    Uçlar pin KOORDİNATINDAN seçilir, `rot` parametresinden değil: rotasyon +
    mirror birleşimlerinde pin 1 ile pin 2 yer değiştirebilir ve bu hata
    netlistte sessiz kalır (şema göz kontrolünde doğru görünür).
    """
    a, b = part.pin("1"), part.pin("2")
    da = (a.x - ref[0]) ** 2 + (a.y - ref[1]) ** 2
    db = (b.x - ref[0]) ** 2 + (b.y - ref[1]) ** 2
    return (a, b) if da <= db else (b, a)


def _pt(x) -> tuple[float, float]:
    return x.xy if isinstance(x, PinRef) else (float(x[0]), float(x[1]))


def _interior(p, a, b) -> bool:
    """`p`, `a`-`b` parçasının UÇLARI HARİÇ üzerinde mi?

    Uçları dışlamak şart: uçta zaten bağlantı vardır ve oraya konan bir
    junction, olmayan bir üçüncü iletken varmış izlenimi verir.
    """
    if abs(a[0] - b[0]) < EPS:                                  # dikey
        return (abs(p[0] - a[0]) < EPS
                and min(a[1], b[1]) + EPS < p[1] < max(a[1], b[1]) - EPS)
    if abs(a[1] - b[1]) < EPS:                                  # yatay
        return (abs(p[1] - a[1]) < EPS
                and min(a[0], b[0]) + EPS < p[0] < max(a[0], b[0]) - EPS)
    return False


def _tag(x) -> str:
    if isinstance(x, PinRef):
        return f"{x.ref}.{x.pin.number}"
    if isinstance(x, tuple):
        return f"{x[0]},{x[1]}"
    return str(getattr(x, "ref", x))
