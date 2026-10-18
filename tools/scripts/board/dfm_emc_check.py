#!/usr/bin/env python
"""DFM/EMC nüans denetleyicisi — board dosyasından ÖLÇER, iddiaya bakmaz.

SKILL-dfm / SKILL-emc-esd / SKILL-konnektor içindeki kuralların
**bugün pcbnew 10 API'siyle otomatikleştirilebilen** alt kümesini uygular.
Her kontrol bir `Finding` üretir; hiçbir kontrol "sessizce geçmez": kapsam
sayısı (kaç öğe bakıldı) her zaman raporlanır, 0 kapsam = FAIL.

Otomatikleştirilemeyenler bilerek yoktur (bkz. SKILL-dogrulama-matrisi):
kaplama/mating-cycle, MSL, DC-bias, ESD clamp seçimi, EMC ölçümü — bunlar
belge/lab kapılarıdır, board dosyasında bilgi olarak bulunmazlar.

Kullanım:
    python dfm_emc_check.py <board.kicad_pcb> [--json out.json]
                            [--hs-net-regex '(?i)(usb|mipi|d_p|d_n|rgmii|tx|rx)']
                            [--edge-keepout-mm 2.0] [--stitch-max-mm 2.5]
                            [--paste-ratio-max 0.80] [--epad-area-mm2 4.0]
                            [--annular-min-mm 0.15]

Çıkış kodu: 0 = tüm kontroller PASS, 1 = en az bir FAIL, 2 = çalıştırma hatası.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pcbnew  # noqa: E402

from common.boardgeom import (  # noqa: E402
    dist_point_to_points,
    edge_cuts_points,
    vias,
)
from common.findings import (  # noqa: E402
    Finding,
    board_exit_code,
    board_report,
    result,
)
from common.report_io import emit, run_cli, write_report  # noqa: E402
from common.units import MM, mm, xy  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "dfm_emc_check"


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — bir ajanın bu aracı çağırabilmesi için girdi/çıktı yapısı.
# Alanların hiçbiri uydurma değildir: girdi tarafı `main()` içindeki argparse
# bloğundan, çıktı tarafı `board_report()`'un GERÇEKTEN yazdığı anahtarlardan
# türetilmiştir. Bir alanın kaynağı yoksa burada da yoktur.
# --------------------------------------------------------------------------- #
class DfmEmcCheckInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argümanlar. Kaynak: `main()` argparse bloğu (s.339-350).

    Yorumdaki sayılar argparse VARSAYILANLARIDIR; alan verilmezse araç o
    değerle ölçer — kontrolü sessizce atlamaz. Eşikleri değiştiren her alan
    rapordaki `params` içinde aynen geri döner, yani ölçümün hangi sınırla
    yapıldığı raporun kendisinden okunabilir.
    """

    json: str              # --json: raporun yazılacağı dosya (verilmezse yalnız stdout)
    hs_net_regex: str      # --hs-net-regex, vars. (?i)(usb|mipi|csi|rgmii|_d_?[pn]\b|_tx|_rx|clk)
    edge_keepout_mm: float  # --edge-keepout-mm, vars. 2.0
    stitch_max_mm: float   # --stitch-max-mm, vars. 2.5
    paste_ratio_max: float  # --paste-ratio-max, vars. 0.80
    epad_area_mm2: float   # --epad-area-mm2, vars. 4.0
    annular_min_mm: float  # --annular-min-mm, vars. 0.15


class DfmEmcCheckInput(DfmEmcCheckInputOptional):
    """Aracın tam girdisi. Kaynak: `main()` argparse bloğu (s.339-350).

    `board` konumsaldır ve ZORUNLUDUR: araç iddiaya değil dosyaya bakar, o
    yüzden ölçülecek kart olmadan çalışacak bir kip yoktur.
    """

    board: str             # konumsal: ölçülecek .kicad_pcb yolu


class DfmEmcCheckOutput(TypedDict):
    """`--json` ile yazılan raporun üst düzey anahtarları.

    Kaynak: `common.findings.board_report()` (tools/common/findings.py
    s.352-372) + schemas/board_check_report.schema.json — o şemanın
    `required` listesi bu dokuz alanın hepsini içerir, yani sınıf ile şema
    ÇELİŞMEZ.

    ÇIKIŞ KODLARI (dosya başı notu): 0 = tüm kontroller PASS, 1 = en az bir
    FAIL, 2 = çalıştırma hatası. 0'ı "temiz" diye okumak YETMEZ:
    `board_exit_code()` NO_COVERAGE'ı da 0 ile döndürür, o yüzden çağıran
    `summary["NO_COVERAGE"]` ya da `by_check.<ad>.status` alanına bakmalıdır.
    """

    schema_version: str    # "board-check-report/v1"
    tool: str              # "dfm_emc_check"
    board: str             # ölçülen kartın yolu (girdideki `board`)
    design: dict           # {"path": str, "sha256": str | None} — rapor ölçtüğü karta bağlanır
    params: dict           # girdideki 6 eşik + hs_net_regex, aynen
    metrics: dict          # kontrol adı -> ihlal SAYISI; NO_COVERAGE ise None (0 değil)
    by_check: dict         # kontrol adı -> {"status", "scanned", "violations"}
    checks: list           # Finding listesi: check/status/scanned/violations/detail
    summary: dict          # {"PASS": n, "FAIL": n, "NO_COVERAGE": n}


def board_outline_segments(board):
    """Edge.Cuts üzerindeki çizim öğelerinden örneklenmiş nokta listesi."""
    return edge_cuts_points(board)


def pad_area_mm2(pad, layer):
    """Bakır pad alanı mm² (efektif poligon; oval/custom şekiller dahil)."""
    return pad.GetEffectivePolygon(layer).Area() / (MM * MM)


def is_ceramic_two_terminal(fp):
    """C*/R*/L* referanslı, 2 padli, SMD parça — flex crack adayı."""
    ref = fp.GetReference()
    if not re.match(r"^[CRL]\d", ref):
        return False
    return len(list(fp.Pads())) == 2


# --------------------------------------------------------------------------- #
# 1) Teardrop sayımı (SKILL-dfm §Teardrop)
# --------------------------------------------------------------------------- #
def check_teardrops(board):
    """Teardrop zone SAYIMI. Ölçülen sayı `scanned` alanındadır, düz metinde değil.

    ÖNCEDEN: Finding elle kuruluyordu; `scanned` = kartın TÜM zone sayısı,
    `violations` = koşulsuz `[]`, ölçülen teardrop sayısı ise yalnızca serbest
    metin `detail` içinde geçiyordu. Aynı anda iki sessiz hata:

      * `metrics.teardrop_count` = len(violations) = HER kartta 0. Yani
        `metrics.teardrop_count at_most 0` iddiası yalnız boş kartta değil,
        gerçekte kaç teardrop olursa olsun HER kartta geçiyordu.
      * 12 zone'u olup tek teardrop'u olmayan kart `scanned = 12` ile
        NO_COVERAGE raporluyordu; yani `by_check.teardrop_count.scanned >= 1`
        kapsam kapısı SIFIR teardrop'lu kartta da açılıyordu.

    ŞİMDİ: incelenen popülasyon teardrop zone'larının kendisidir → `scanned`
    doğrudan ölçülen teardrop sayısıdır ve kalanını `result()` kuralı yapar:
    0 teardrop → NO_COVERAGE (raporda AÇIK madde), ≥1 → PASS.

    İHLAL LİSTESİ BİLEREK BOŞTUR: "kaç teardrop olmalı" diye kaynaklı bir sınır
    YOKTUR (SKILL-dfm §3: Class 3 ≠ teardrop zorunlu; çok Gb/s çiftlerinde
    varsayılan KAPALI). Bu bir sayımdır, bir eşik değil. Sayıya bakacak kural
    `by_check.teardrop_count.scanned`'e bağlanmalıdır — `metrics.teardrop_count`
    ihlal sayısıdır ve bu kontrolde her zaman 0'dır.
    """
    zones = list(board.Zones())
    td = [z for z in zones if z.IsTeardropArea()]
    return result("teardrop_count", len(td), [],
                  f"{len(td)} teardrop zone / {len(zones)} zone. "
                  "0 ise raporda AÇIK madde (pcbnew SWIG üretemez, GUI adımı). "
                  "Sayı `scanned` alanındadır; `metrics.teardrop_count` ihlal "
                  "sayısıdır (kaynaklı sınır yok → her zaman 0).")


# --------------------------------------------------------------------------- #
# 2) Via-in-pad → IPC-4761 Type VII gerektirir (SKILL-dfm)
# --------------------------------------------------------------------------- #
def check_via_in_pad(board):
    viz = vias(board)
    pads = [(f, p) for f in board.GetFootprints() for p in f.Pads()
            if p.GetAttribute() in (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_CONN)]
    hits = []
    for v in viz:
        vp = v.GetPosition()
        for f, p in pads:
            if not p.IsOnLayer(v.TopLayer()) and not p.IsOnLayer(v.BottomLayer()):
                continue
            layer = v.TopLayer() if p.IsOnLayer(v.TopLayer()) else v.BottomLayer()
            if p.GetEffectivePolygon(layer).Collide(vp):
                hits.append({
                    "via_mm": xy(vp),
                    "pad": f"{f.GetReference()}.{p.GetNumber()}",
                    "net": v.GetNetname(),
                })
                break
    return result("via_in_pad", len(viz), hits,
                  "Bulunan her via-in-pad, fab notunda IPC-4761 Type VII "
                  "(dolgu+kapak, dimple ≤25 µm) olarak belirtilmelidir.")


# --------------------------------------------------------------------------- #
# 3) Annular ring → teardrop/DFM adayları (SKILL-dfm §Teardrop nerede gerekir)
# --------------------------------------------------------------------------- #
def check_annular(board, min_mm):
    """Halka ölçümü TAM SAYI nm ile yapılır.

    mm float'ında (0.7-0.4)/2 = 0.14999999999999997 çıkar ve tam sınırdaki
    (0.15 mm) her pad yanlışlıkla ihlal olur — USB-C J1'in 16 padinde bu
    yaşandı. Ölçüm birimini aracın iç birimi (nm) yapmak sınır hatasını
    tamamen kaldırır.
    """
    limit_nm = int(round(min_mm * MM))
    items, viol = 0, []
    for v in vias(board):
        items += 1
        # GetWidth() MUST be given a layer. KiCad 10 supports per-layer via
        # stacks, so the no-arg overload is a debug assert:
        #   pcb_track.cpp(387): "PCB_VIA::GetWidth called without a layer
        #   argument"
        # It still returns a value, so it looks harmless from Python — but on a
        # debug/GUI build the assert raises a modal dialog, which hangs a
        # headless gate run with no output. Verified against pcbnew 10.0.5.
        ring_nm = (v.GetWidth(v.TopLayer()) - v.GetDrill()) // 2
        if ring_nm < limit_nm:
            viol.append({"type": "via", "pos_mm": xy(v.GetPosition()),
                         "net": v.GetNetname(), "annular_mm": round(ring_nm / MM, 4)})
    for f in board.GetFootprints():
        for p in f.Pads():
            d = p.GetDrillSize()
            if d.x <= 0:
                continue
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue  # mekanik delik: annular ring kavramı yok
            items += 1
            ring_nm = (min(p.GetSizeX(), p.GetSizeY()) - d.x) // 2
            if ring_nm < limit_nm:
                viol.append({"type": "pth", "pad": f"{f.GetReference()}.{p.GetNumber()}",
                             "annular_mm": round(ring_nm / MM, 4)})
    return result("annular_ring", items, viol,
                  f"Halka < {min_mm} mm olan delikler teardrop adayıdır "
                  "(Class 2 ~0.15 / Class 3 ~0.20 mm).")


# --------------------------------------------------------------------------- #
# 4) EPAD pasta kapsaması (SKILL-dfm §QFN/EPAD)
# --------------------------------------------------------------------------- #
def check_paste_coverage(board, epad_area_mm2, ratio_max):
    scanned, viol = 0, []
    for f in board.GetFootprints():
        for p in f.Pads():
            if not p.IsOnLayer(pcbnew.F_Paste) and not p.IsOnLayer(pcbnew.B_Paste):
                continue
            cu = pcbnew.F_Cu if p.IsOnLayer(pcbnew.F_Paste) else pcbnew.B_Cu
            area = pad_area_mm2(p, cu)
            if area < epad_area_mm2:
                continue
            scanned += 1
            mv = p.GetSolderPasteMargin(cu)
            margin = mm(mv[0] if isinstance(mv, (tuple, list)) else mv.x)
            # Oran, aynı dikdörtgen modeli üzerinden hesaplanır (poligon köşe
            # yuvarlaması iki tarafta da olduğu için sadeleşir).
            w0, h0 = mm(p.GetSizeX()), mm(p.GetSizeY())
            ratio = (max(w0 + 2 * margin, 0) * max(h0 + 2 * margin, 0)) / (w0 * h0)
            if ratio > ratio_max:
                viol.append({"pad": f"{f.GetReference()}.{p.GetNumber()}",
                             "copper_mm2": round(area, 3),
                             "paste_margin_mm": round(margin, 4),
                             "paste_ratio": round(ratio, 3)})
    return result("epad_paste_coverage", scanned, viol,
                  f"≥{epad_area_mm2} mm² pad'lerde pasta oranı ≤{ratio_max} olmalı "
                  "(windowpane). SINIR: tek pad + negatif margin modelini ölçer; "
                  "ayrı ayrı çizilmiş çok-açıklıklı pasta desenini tanımaz → "
                  "o durumda elle doğrula.")


# --------------------------------------------------------------------------- #
# 5) Kenar/depanel — seramik flex crack riski (SKILL-dfm §MLCC)
# --------------------------------------------------------------------------- #
def check_edge_keepout(board, keepout_mm):
    edge = board_outline_segments(board)
    if not edge:
        return Finding("edge_keepout_ceramics", "NO_COVERAGE", 0, [],
                       "Edge.Cuts bulunamadı")
    scanned, viol = 0, []
    for f in board.GetFootprints():
        if not is_ceramic_two_terminal(f):
            continue
        scanned += 1
        pos = xy(f.GetPosition())
        d = dist_point_to_points(pos, edge)
        if d < keepout_mm:
            viol.append({"ref": f.GetReference(), "pos_mm": pos,
                         "edge_dist_mm": round(d, 3),
                         "orientation_deg": round(f.GetOrientationDegrees() % 180, 1)})
    return result("edge_keepout_ceramics", scanned, viol,
                  f"2 uçlu seramikler kart kenarından ≥{keepout_mm} mm. "
                  "İhlal listesindeki `orientation_deg` ile parçanın uzun ekseni "
                  "kırma hattına PARALEL mi elle doğrula (SKILL-dfm §MLCC).")


# --------------------------------------------------------------------------- #
# 6) Dönüş via'sı — her hızlı sinyal via'sının yanında GND via'sı (SKILL-emc-esd §3)
# --------------------------------------------------------------------------- #
def check_return_vias(board, hs_regex, max_mm):
    rx = re.compile(hs_regex)
    viz = vias(board)
    gnd = [v for v in viz if re.match(r"^/?(GND|AGND|DGND|VSS)", v.GetNetname() or "")]
    hs = [v for v in viz if v.GetNetname() and rx.search(v.GetNetname())]
    viol = []
    for v in hs:
        p = xy(v.GetPosition())
        d = min((math.hypot(p[0] - g[0], p[1] - g[1])
                 for g in (xy(x.GetPosition()) for x in gnd)), default=float("inf"))
        if d > max_mm:
            viol.append({"net": v.GetNetname(), "pos_mm": p,
                         "nearest_gnd_via_mm": None if math.isinf(d) else round(d, 3)})
    return result("return_via_near_hs_via", len(hs), viol,
                  f"Her hızlı sinyal via'sının ≤{max_mm} mm yakınında GND via'sı "
                  f"olmalı. GND via sayısı={len(gnd)}.")


# --------------------------------------------------------------------------- #
# 7) Netclass pattern iki yönlü kapsama (SKILL-pcb-highspeed-escape §9)
# --------------------------------------------------------------------------- #
def check_netclass_coverage(board, hs_regex):
    rx = re.compile(hs_regex)
    rows, miss = 0, []
    for netname, net in board.GetNetsByName().items():
        name = str(netname)
        if not name or not rx.search(name):
            continue
        rows += 1
        nc = net.GetNetClassName()
        if nc in ("Default", "", None):
            miss.append({"net": name, "netclass": nc})
    return result("hs_netclass_assigned", rows, miss,
                  "Hızlı desene uyan her net Default DIŞINDA bir netclass'ta "
                  "olmalı; ters yön (güç/CC netlerinin yanlışlıkla kapsanması) "
                  "elle gözden geçirilir.")


# --------------------------------------------------------------------------- #
CHECKS = ("teardrop_count", "via_in_pad", "annular_ring", "epad_paste_coverage",
          "edge_keepout_ceramics", "return_via_near_hs_via", "hs_netclass_assigned")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--json")
    ap.add_argument("--hs-net-regex",
                    default=r"(?i)(usb|mipi|csi|rgmii|_d_?[pn]\b|_tx|_rx|clk)")
    ap.add_argument("--edge-keepout-mm", type=float, default=2.0)
    ap.add_argument("--stitch-max-mm", type=float, default=2.5)
    ap.add_argument("--paste-ratio-max", type=float, default=0.80)
    ap.add_argument("--epad-area-mm2", type=float, default=4.0)
    ap.add_argument("--annular-min-mm", type=float, default=0.15)
    a = ap.parse_args(argv)
    return run_analysis(a)


def run_analysis(a) -> int:
    board = pcbnew.LoadBoard(a.board)
    findings = [
        check_teardrops(board),
        check_via_in_pad(board),
        check_annular(board, a.annular_min_mm),
        check_paste_coverage(board, a.epad_area_mm2, a.paste_ratio_max),
        check_edge_keepout(board, a.edge_keepout_mm),
        check_return_vias(board, a.hs_net_regex, a.stitch_max_mm),
        check_netclass_coverage(board, a.hs_net_regex),
    ]
    report = board_report(TOOL_NAME, a.board, findings, params={
        "hs_net_regex": a.hs_net_regex,
        "edge_keepout_mm": a.edge_keepout_mm,
        "stitch_max_mm": a.stitch_max_mm,
        "paste_ratio_max": a.paste_ratio_max,
        "epad_area_mm2": a.epad_area_mm2,
        "annular_min_mm": a.annular_min_mm,
    })
    write_report(report, a.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
