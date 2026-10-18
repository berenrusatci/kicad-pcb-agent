"""sch_route — şematikte PIN'DEN PIN'E gerçek kablo çeken ortogonal router.

`sch_wire.py` tek tek wire yazar; bu modül **bağlantıyı planlar**: her net için
pinler arasında sembol gövdelerinden kaçan, 1.27 mm grid üzerinde ortogonal
yollar bulur (A*), aynı net'in dallarını paylaştırır (T + junction) ve yabancı
net'lerin teline değmeyi yasaklar.

Kural özeti (KiCad connectivity):
  * dik kesişme  -> bağlantı YOK (serbest, sadece maliyetli)
  * üst üste/paralel akış veya yabancı telin UCUNA değme -> SHORT (yasak)
  * aynı net'in teline değmek -> istenen (junction ile işaretlenir)
"""

from __future__ import annotations

import heapq
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.sexpr import GRID, find_all, parse_sexpr, snap  # noqa: E402
from sch_wire import PinRef, Placed  # noqa: E402

TURN_COST = 3.0
CROSS_COST = 12.0        # yabancı teli dik kesmek: serbest ama çirkin
SHARE_BONUS = 0.25       # aynı net'in telini paylaşmak ucuz (T oluşsun)


# --------------------------------------------------------------------------- #
# Sembol gövde kutusu                                                          #
# --------------------------------------------------------------------------- #
def body_bbox(pl: Placed, margin: float = 1.27) -> tuple[float, float, float, float]:
    """Yerleştirilmiş sembolün sheet koordinatlarında gövde sınır kutusu.
    Pin çizgileri dahil edilmez (pin ucuna ulaşabilmek gerekir)."""
    from sch_wire import _xform
    pts: list[tuple[float, float]] = []
    tree = parse_sexpr(pl.sym.text)
    for sub in find_all(tree, "symbol"):
        for r in find_all(sub, "rectangle"):
            for k in ("start", "end"):
                n = [c for c in r if isinstance(c, list) and c and c[0] == k]
                if n:
                    pts.append((float(n[0][1]), float(n[0][2])))
        for poly in find_all(sub, "polyline") + find_all(sub, "arc") + find_all(sub, "bezier"):
            for p in find_all(poly, "pts"):
                for xy in find_all(p, "xy"):
                    pts.append((float(xy[1]), float(xy[2])))
            for k in ("start", "mid", "end"):
                n = [c for c in poly if isinstance(c, list) and c and c[0] == k]
                if n:
                    pts.append((float(n[0][1]), float(n[0][2])))
        for c in find_all(sub, "circle"):
            ctr = [n for n in c if isinstance(n, list) and n and n[0] == "center"]
            rad = [n for n in c if isinstance(n, list) and n and n[0] == "radius"]
            if ctr and rad:
                cx, cy, rr = float(ctr[0][1]), float(ctr[0][2]), float(rad[0][1])
                pts += [(cx - rr, cy - rr), (cx + rr, cy + rr)]
    if not pts:
        return (pl.x, pl.y, pl.x, pl.y)
    xs, ys = [], []
    for px, py in pts:
        ax, ay = _xform(px, py, pl.rot, pl.mirror)
        xs.append(pl.x + ax)
        ys.append(pl.y + ay)
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


# --------------------------------------------------------------------------- #
# Grid router                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class Router:
    origin: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] = (297.0, 210.0)      # A4
    grid: float = GRID
    blocked: set[tuple[int, int]] = field(default_factory=set)
    # hücre -> {net: {yön}} ; yön 0=yatay 1=dikey
    occupied: dict[tuple[int, int], dict[str, set[int]]] = field(default_factory=dict)
    endpoints: dict[tuple[int, int], set[str]] = field(default_factory=dict)

    # -- koordinat <-> hücre --------------------------------------------------
    def cell(self, p: tuple[float, float]) -> tuple[int, int]:
        return (int(round((p[0] - self.origin[0]) / self.grid)),
                int(round((p[1] - self.origin[1]) / self.grid)))

    def point(self, c: tuple[int, int]) -> tuple[float, float]:
        return (round(self.origin[0] + c[0] * self.grid, 2),
                round(self.origin[1] + c[1] * self.grid, 2))

    @property
    def nx(self) -> int:
        return int(self.size[0] / self.grid) + 1

    @property
    def ny(self) -> int:
        return int(self.size[1] / self.grid) + 1

    # -- engeller -------------------------------------------------------------
    def block_symbols(self, placements: list[Placed], keep: set[tuple[float, float]]) -> None:
        keep_cells = {self.cell(p) for p in keep}
        for pl in placements:
            x0, y0, x1, y1 = body_bbox(pl)
            c0, c1 = self.cell((x0, y0)), self.cell((x1, y1))
            for i in range(c0[0], c1[0] + 1):
                for j in range(c0[1], c1[1] + 1):
                    if (i, j) not in keep_cells:
                        self.blocked.add((i, j))

    def block_pins(self, anchors: dict[tuple[float, float], PinRef],
                   net_of: dict[str, str]) -> None:
        """Her pin ankrajı, KENDİ net'i dışındaki yollar için yasak."""
        self.pin_owner: dict[tuple[int, int], str] = {}
        for xy, pr in anchors.items():
            self.pin_owner[self.cell(xy)] = net_of.get(f"{pr.ref}-{pr.pin.number}", "?")

    # -- maliyet --------------------------------------------------------------
    def _passable(self, c: tuple[int, int], net: str, direction: int) -> float | None:
        if not (0 <= c[0] < self.nx and 0 <= c[1] < self.ny):
            return None
        if c in self.blocked:
            return None
        owner = getattr(self, "pin_owner", {}).get(c)
        if owner is not None and owner != net:
            return None                              # yabancı pin'e basma
        foreign_end = self.endpoints.get(c, set()) - {net}
        if foreign_end:
            return None                              # yabancı telin UCU = short
        cost = 1.0
        occ = self.occupied.get(c)
        if occ:
            for onet, dirs in occ.items():
                if onet == net:
                    cost -= SHARE_BONUS
                elif direction in dirs:
                    return None                      # paralel üst üste = short
                else:
                    cost += CROSS_COST
        return max(cost, 0.1)

    # -- A* -------------------------------------------------------------------
    def route(self, a: tuple[float, float], b: tuple[float, float],
              net: str) -> list[tuple[float, float]] | None:
        start, goal = self.cell(a), self.cell(b)
        if start == goal:
            return None
        def h(c):
            return abs(c[0] - goal[0]) + abs(c[1] - goal[1])
        # durum: (hücre, geliş yönü)
        openq = [(h(start), 0.0, start, -1)]
        best: dict[tuple[tuple[int, int], int], float] = {(start, -1): 0.0}
        prev: dict[tuple[tuple[int, int], int], tuple] = {}
        seen = set()
        while openq:
            _, g, c, d = heapq.heappop(openq)
            if (c, d) in seen:
                continue
            seen.add((c, d))
            if c == goal:
                return self._unwind(prev, (c, d), start)
            for nd, (dx, dy) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
                axis = 0 if dy == 0 else 1
                nc = (c[0] + dx, c[1] + dy)
                # Hedef hücre de denetlenir: yol yabancı bir telin ÜZERİNDE
                # bitemez — uç, telin ortasına değerse KiCad bunu T-bağlantı
                # sayar ve iki net sessizce kısa devre olur.
                step = self._passable(nc, net, axis)
                if step is None:
                    continue
                if nc == goal and any(on != net for on in self.occupied.get(nc, {})):
                    continue      # uçta yabancı tel varsa (dik kesse bile) short
                ng = g + step + (TURN_COST if d != -1 and d != axis else 0.0)
                key = (nc, axis)
                if ng < best.get(key, 1e18):
                    best[key] = ng
                    prev[key] = (c, d)
                    heapq.heappush(openq, (ng + h(nc), ng, nc, axis))
        return None

    def _unwind(self, prev, node, start) -> list[tuple[float, float]]:
        cells = []
        while True:
            cells.append(node[0])
            if node[0] == start:
                break
            node = prev[node]
        cells.reverse()
        # köşe noktalarını çıkar (düz parçaları birleştir)
        pts = [cells[0]]
        for i in range(1, len(cells) - 1):
            ax = (cells[i][0] - cells[i - 1][0], cells[i][1] - cells[i - 1][1])
            bx = (cells[i + 1][0] - cells[i][0], cells[i + 1][1] - cells[i][1])
            if ax != bx:
                pts.append(cells[i])
        pts.append(cells[-1])
        return [self.point(c) for c in pts]

    # -- kayıt ----------------------------------------------------------------
    def commit(self, pts: list[tuple[float, float]], net: str) -> None:
        for a, b in zip(pts, pts[1:]):
            ca, cb = self.cell(a), self.cell(b)
            axis = 0 if ca[1] == cb[1] else 1
            step = 1 if (cb[axis == 0] if False else 0) else 1
            lo, hi = sorted((ca[axis], cb[axis]))
            for k in range(lo, hi + 1):
                c = (k, ca[1]) if axis == 0 else (ca[0], k)
                self.occupied.setdefault(c, {}).setdefault(net, set()).add(axis)
        for p in (pts[0], pts[-1]):
            self.endpoints.setdefault(self.cell(p), set()).add(net)


# --------------------------------------------------------------------------- #
# Net planlama                                                                 #
# --------------------------------------------------------------------------- #
def mst_order(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Manhattan MST kenarları (Prim) — net'i en kısa ağaçla bağla."""
    n = len(points)
    if n < 2:
        return []
    inside = {0}
    edges = []
    while len(inside) < n:
        best = None
        for i in inside:
            for j in range(n):
                if j in inside:
                    continue
                d = abs(points[i][0] - points[j][0]) + abs(points[i][1] - points[j][1])
                if best is None or d < best[0]:
                    best = (d, i, j)
        edges.append((best[1], best[2]))
        inside.add(best[2])
    return edges


def commit_stubs(router: Router, nets: dict[str, list[PinRef]], stub: float = GRID
                 ) -> dict[str, list[list[tuple[float, float]]]]:
    """TÜM net'lerin pin çıkışlarını routing'den ÖNCE rezerve eder.

    Zorunlu: yoksa önce route edilen net, sonra route edilecek bir net'in pin
    çıkış noktasının üzerinden geçer; o nokta sonradan uç olunca iki net
    T-bağlantısıyla sessizce kısa devre olur (gerçekte yaşandı: USB_D+/USB_D-).
    """
    out: dict[str, list[list[tuple[float, float]]]] = {}
    for net, pins in nets.items():
        polys = []
        for pr in pins:
            seg = [pr.xy, pr.out(stub)]
            polys.append(seg)
            router.commit(seg, net)
        out[net] = polys
    return out


def route_net(router: Router, net: str, pins: list[PinRef], stub: float = GRID,
              pre_stubbed: bool = False
              ) -> tuple[list[list[tuple[float, float]]], list[str]]:
    """Bir net'in tüm pinlerini ağaç olarak bağla. Dönen: polyline listesi + hata listesi."""
    polys: list[list[tuple[float, float]]] = []
    errs: list[str] = []
    # 1) her pin'den gövdeden uzaklaşan kısa çıkış (pin ankrajı grid dışıysa da hizalar)
    exits: list[tuple[float, float]] = []
    for pr in pins:
        ex = pr.out(stub)
        if not pre_stubbed:
            polys.append([pr.xy, ex])
            router.commit([pr.xy, ex], net)
        exits.append(ex)
    # 2) çıkış noktalarını MST sırasıyla A* ile bağla
    for i, j in mst_order(exits):
        path = router.route(exits[i], exits[j], net)
        if path is None:
            errs.append(f"{net}: {pins[i].ref}-{pins[i].pin.number} -> "
                        f"{pins[j].ref}-{pins[j].pin.number} yol bulunamadı")
            continue
        polys.append(path)
        router.commit(path, net)
    return polys, errs


def junction_points(polys_by_net: dict[str, list[list[tuple[float, float]]]]
                    ) -> list[tuple[float, float]]:
    """Aynı net içinde 3+ tel ucunun/dalın buluştuğu noktalar."""
    from sch_wire import _on_segment
    out = []
    for net, polys in polys_by_net.items():
        segs = [(a, b) for pts in polys for a, b in zip(pts, pts[1:])]
        ends: dict[tuple[float, float], int] = {}
        for a, b in segs:
            for p in (a, b):
                ends[p] = ends.get(p, 0) + 1
        for p, n in ends.items():
            touching = n
            for a, b in segs:
                if p not in (a, b) and _on_segment(p, a, b):
                    touching += 2
            if touching >= 3:
                out.append(p)
    return sorted(set(out))
