#!/usr/bin/env python
"""GND stitching via YOĞUNLUĞU denetleyicisi — board dosyasından ÖLÇER.

Hedef via aralığı, lambda/20 kuralından hesaplanır:

    v          = c / sqrt(er_eff)
    lambda     = v / f_knee
    hedef_mm   = lambda / 20

Ölçülen iki şey:
  1) Kart kenarı (Edge.Cuts) boyunca örneklenen noktalarda en yakın GND
     via mesafesi (RF sızıntı/return-path süreksizliği riski).
  2) Her via'nın (katman geçişi) etrafındaki en yakın GND via mesafesi
     (return-via kuralı — sinyal via'sı dönüş akımını görmeli).

Aynı `Finding` sözleşmesi (bkz. dfm_emc_check.py): check/status/scanned/
violations/detail; scanned=0 → NO_COVERAGE. GND via YOKSA her iki kontrol
de NO_COVERAGE döner (ölçüm için referans nokta yok).

SINIR (bilerek yapılmayanlar):
- er_eff sabit bir kullanıcı girdisidir; gerçek katman dielektriğine, iz
  genişliğine ya da mikrostrip/stripline farkına göre otomatik hesaplanmaz.
- Kenar örneklemesi yalnızca DÜZ (S_SEGMENT) Edge.Cuts çizgilerinde tam
  çözünürlükte çalışır; ark/daire kenarlarda kaba poligon köşe örneklemesine
  düşer (daha az nokta → daha az hassas).
- "Katman geçişi" ölçümü sadece TopLayer()!=BottomLayer() olan mikro-via'lar
  için filtre yapmaz; 2 katmanlı bir kartta HER via bir F.Cu<->B.Cu geçişidir
  ve bu triviali kontrol de yansıtır (gerçek çok katmanlı kartta anlamı
  artar).
- Zon (GND pour) bağlantısının kendisi/temas kalitesi ölçülmez, yalnızca
  fiziksel via-via mesafesi.

Kullanım:
    python stitch_density.py <board.kicad_pcb> [--json out.json]
                              [--f-knee-ghz 5.0] [--er 4.5]
                              [--gnd-net-regex '^/?(GND|AGND|DGND|VSS)']
                              [--edge-step-mm 1.0]
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
from common.si_models import knee_wavelength_spacing_mm  # noqa: E402
from common.units import C_MPS, MM, mm, xy  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "stitch_density"


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — ajanın aracı çağırabilmesi için girdi/çıktı yapısı. Girdi
# argparse bloğundan, çıktı `board_report()`'un yazdığı anahtarlardan türetildi.
# --------------------------------------------------------------------------- #
class StitchDensityInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argümanlar. Kaynak: `main()` argparse bloğu (s.201-209).

    `f_knee_ghz` ve `er` hedef aralığı BELİRLEYEN iki sayıdır (hedef =
    lambda/20); ikisi de kullanıcı beyanıdır, karttan ölçülmez (bkz. dosya
    başı SINIR notu — er_eff otomatik hesaplanmaz). Hesaplanan hedef raporda
    `params.target_spacing_mm` olarak durur, yani hangi eşiğe göre ölçüldüğü
    rapordan okunur.
    """

    json: str            # --json: raporun yazılacağı dosya (verilmezse yalnız stdout)
    f_knee_ghz: float    # --f-knee-ghz, vars. 5.0 — diz frekansı
    er: float            # --er, vars. 4.5 — efektif dielektrik sabiti (er_eff)
    gnd_net_regex: str   # --gnd-net-regex, vars. ^/?(GND|AGND|DGND|VSS)
    edge_step_mm: float  # --edge-step-mm, vars. 1.0 — kenar örnekleme adımı


class StitchDensityInput(StitchDensityInputOptional):
    """Aracın tam girdisi. Kaynak: `main()` argparse bloğu (s.201-209).

    `board` konumsaldır ve ZORUNLUDUR — via yoğunluğu dosyadan ölçülür.
    """

    board: str           # konumsal: ölçülecek .kicad_pcb yolu


class StitchDensityOutput(TypedDict):
    """`--json` ile yazılan raporun üst düzey anahtarları.

    Kaynak: `common.findings.board_report()` (tools/common/findings.py
    s.352-372) + schemas/board_check_report.schema.json (`required` = aynı
    dokuz alan). İki kontrol vardır: `edge_stitch_pitch`, `via_return_stitch`.

    ÇIKIŞ KODLARI (kardeş araçlarla aynı sözleşme, bkz. dosya başı): 0 = PASS,
    1 = FAIL, 2 = çalıştırma hatası. GND via YOKSA her iki kontrol de
    NO_COVERAGE döner ve çıkış kodu yine 0'dır — bu yüzden "ölçüm yapılmadı"
    hâli SADECE `by_check.<ad>.status` / `summary` üzerinden görülür.
    """

    schema_version: str  # "board-check-report/v1"
    tool: str            # "stitch_density"
    board: str           # ölçülen kartın yolu (girdideki `board`)
    design: dict         # {"path": str, "sha256": str | None}
    params: dict         # {"target_spacing_mm", "f_knee_ghz", "er_eff", "gnd_net_regex", "edge_step_mm"}
    metrics: dict        # kontrol adı -> ihlal SAYISI; NO_COVERAGE ise None (0 değil)
    by_check: dict       # kontrol adı -> {"status", "scanned", "violations"}
    checks: list         # Finding listesi: check/status/scanned/violations/detail
    summary: dict        # {"PASS": n, "FAIL": n, "NO_COVERAGE": n}


def target_spacing_mm(f_knee_ghz, er_eff):
    """lambda/20 stitching target at the knee frequency, in millimetres."""
    return knee_wavelength_spacing_mm(f_knee_ghz, er_eff, divisor=20.0)


def gnd_vias(board, gnd_regex):
    rx = re.compile(gnd_regex)
    return [v for v in vias(board) if rx.match(v.GetNetname() or "")]


def sample_edge_points(board, step_mm):
    """Edge.Cuts boyunca sabit adımlı örnekleme.

    Now delegates to ``common.boardgeom.edge_cuts_points``, which is the same
    sampler dfm_emc_check uses. Düz (S_SEGMENT) çizgilerde tam adımda
    örnekler; ark/daire/poligonda kaba poligon köşelerine düşer (bkz. SINIR).
    """
    return edge_cuts_points(board, step_mm=step_mm, approx_mm=0.05)


# --------------------------------------------------------------------------- #
# 1) Kenar boyunca GND stitching aralığı
# --------------------------------------------------------------------------- #
def check_edge_stitching(board, gnd, target_mm, edge_step_mm):
    edge_pts = sample_edge_points(board, edge_step_mm)
    if not edge_pts:
        return Finding("edge_stitch_pitch", "NO_COVERAGE", 0, [],
                        "Edge.Cuts bulunamadı")
    if not gnd:
        return Finding("edge_stitch_pitch", "NO_COVERAGE", 0, [],
                        "GND via bulunamadı — ölçüm referansı yok")
    gxy = [xy(v.GetPosition()) for v in gnd]
    scanned, viol = 0, []
    for pt in edge_pts:
        scanned += 1
        d = dist_point_to_points(pt, gxy)
        if d > target_mm:
            viol.append({"edge_pt_mm": (round(pt[0], 3), round(pt[1], 3)),
                         "nearest_gnd_via_mm": round(d, 3)})
    return result("edge_stitch_pitch", scanned, viol,
                  f"Hedef aralık (lambda/20) = {target_mm:.3f} mm. "
                  f"GND via sayısı={len(gnd)}, kenar örnek adımı={edge_step_mm} mm.")


# --------------------------------------------------------------------------- #
# 2) Katman geçişi (via) çevresinde en yakın GND via
# --------------------------------------------------------------------------- #
def check_via_return_stitching(board, gnd, target_mm):
    all_v = vias(board)
    if not gnd:
        return Finding("via_return_stitch", "NO_COVERAGE", 0, [],
                        "GND via bulunamadı — ölçüm referansı yok")
    scanned, viol = 0, []
    gnd_ids = {v.m_Uuid.AsString() for v in gnd}
    for v in all_v:
        if v.TopLayer() == v.BottomLayer():
            continue  # katman geçişi yok (garip ama teorik olarak mümkün)
        scanned += 1
        vp = xy(v.GetPosition())
        others = [xy(g.GetPosition()) for g in gnd
                  if g.m_Uuid.AsString() != v.m_Uuid.AsString()]
        d = dist_point_to_points(vp, others)
        if d > target_mm:
            viol.append({"via_net": v.GetNetname(), "pos_mm": vp,
                         "nearest_other_gnd_via_mm":
                             None if math.isinf(d) else round(d, 3)})
    return result("via_return_stitch", scanned, viol,
                  f"Hedef aralık (lambda/20) = {target_mm:.3f} mm. "
                  f"Toplam via={len(all_v)}, GND via={len(gnd)}. SINIR: 2 katmanlı "
                  "kartta her via bir F.Cu<->B.Cu geçişi sayılır (bkz. dosya başı).")


CHECKS = ("edge_stitch_pitch", "via_return_stitch")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--json")
    ap.add_argument("--f-knee-ghz", type=float, default=5.0)
    ap.add_argument("--er", type=float, default=4.5)
    ap.add_argument("--gnd-net-regex", default=r"^/?(GND|AGND|DGND|VSS)")
    ap.add_argument("--edge-step-mm", type=float, default=1.0)
    a = ap.parse_args(argv)
    return run_analysis(a)


def run_analysis(a) -> int:
    board = pcbnew.LoadBoard(a.board)
    target_mm = target_spacing_mm(a.f_knee_ghz, a.er)
    gnd = gnd_vias(board, a.gnd_net_regex)
    findings = [
        check_edge_stitching(board, gnd, target_mm, a.edge_step_mm),
        check_via_return_stitching(board, gnd, target_mm),
    ]
    report = board_report(TOOL_NAME, a.board, findings, params={
        "target_spacing_mm": round(target_mm, 4),
        "f_knee_ghz": a.f_knee_ghz,
        "er_eff": a.er,
        "gnd_net_regex": a.gnd_net_regex,
        "edge_step_mm": a.edge_step_mm,
    })
    write_report(report, a.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
