#!/usr/bin/env python
"""Pin-arası kanal SOLDER MASK BARAJI denetleyicisi — board dosyasından ÖLÇER.

Komşu pad çiftleri arasından geçen izlerde kalan solder mask barajını hesaplar:

    baraj_mm = bakır_boşluk_mm - mask_expansion(pad_a) - mask_expansion(pad_b)

mask_expansion, `pad.GetSolderMaskExpansion(layer)` üzerinden okunur; bu API
pad'in kendi `GetLocalSolderMaskMargin()` override'ı varsa onu, yoksa
`board.GetDesignSettings().m_SolderMaskExpansion` (global) değerini otomatik
seçer — pcbnew'in kendi çözümleme sırasıdır, burada yeniden icat edilmez.

Aynı `Finding` sözleşmesi (bkz. dfm_emc_check.py): her kontrol
check/status/scanned/violations/detail döndürür; scanned=0 → NO_COVERAGE,
sessiz PASS yok. Çıkış kodu: 0=PASS, 1=FAIL, 2=çalıştırma hatası.

SINIR (bilerek yapılmayanlar):
- Pad "yarı-genişlik" izdüşümü eksen-hizalı (axis-aligned) yaklaşımdır;
  döndürülmüş (rotated) padler için kanal geometrisi yaklaşıktır (dikdörtgen
  köşe etkisi hesaba katılmaz). Gerçek poligon-poligon mesafesi değil.
- Sadece PCB_TRACK (düz iz) kontrol edilir; ark (arc) izler ve zone dolgusu
  (kanaldaki bakır dökümü) bu taramaya dahil değildir.
- Tenting/via-in-channel, solder paste, ve izin KENDİ solder mask açıklığının
  barajı ayrıca daraltması modellenmez — sadece iki PAD'in maskesi arasındaki
  baraj hesaplanır.
- Sadece pad merkezleri arası hattın ortasından geçen kanal (e1-e2) segmentine
  iz_genişliği/2 + tolerans içinde yaklaşan izler "kanaldan geçiyor" sayılır;
  kanalı sadece kısmen kesen çapraz izler yanlış negatif verebilir.

Kullanım:
    python mask_dam.py <board.kicad_pcb> [--json out.json]
                        [--min-dam-mm 0.20] [--search-mm 3.0]
                        [--track-tol-mm 0.05]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pcbnew  # noqa: E402

from common.boardgeom import (  # noqa: E402
    ccw,
    seg_point_dist,
    seg_seg_min_dist,
    segments_intersect,
)
from common.escape_geom import FAB_MIN_MASK_DAM_MM  # noqa: E402
from common.findings import (  # noqa: E402
    Finding,
    board_exit_code,
    board_report,
    result,
)
from common.report_io import emit, run_cli, write_report  # noqa: E402
from common.units import MM, mm, xy  # noqa: E402

SCHEMA_VERSION = "board-check-report/v1"
TOOL_NAME = "mask_dam"


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — ajanın aracı çağırabilmesi için girdi/çıktı yapısı. Girdi
# argparse bloğundan, çıktı `board_report()`'un yazdığı anahtarlardan türetildi.
# --------------------------------------------------------------------------- #
class MaskDamInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argümanlar. Kaynak: `main()` argparse bloğu (s.241-250).

    `min_dam_mm` varsayılanı bu modülde SABİT DEĞİLDİR: `common.escape_geom.
    FAB_MIN_MASK_DAM_MM` üzerinden gelir, yani fab tabanı tek yerden değişir.
    Arama/tolerans alanları taramanın KAPSAMINI belirler; büyütmek daha çok
    (pad çifti, iz) kombinasyonu tarar, `scanned` bu yüzden raporda durur.
    """

    json: str            # --json: raporun yazılacağı dosya (verilmezse yalnız stdout)
    min_dam_mm: float    # --min-dam-mm, vars. FAB_MIN_MASK_DAM_MM (escape_geom'dan)
    search_mm: float     # --search-mm, vars. 3.0 — komşu pad adayı azami bakır boşluğu
    track_tol_mm: float  # --track-tol-mm, vars. 0.05 — izin kanal hattına yaklaşma toleransı


class MaskDamInput(MaskDamInputOptional):
    """Aracın tam girdisi. Kaynak: `main()` argparse bloğu (s.241-250).

    `board` konumsaldır ve ZORUNLUDUR — baraj beyandan değil dosyadan ölçülür.
    """

    board: str           # konumsal: ölçülecek .kicad_pcb yolu


class MaskDamOutput(TypedDict):
    """`--json` ile yazılan raporun üst düzey anahtarları.

    Kaynak: `common.findings.board_report()` (tools/common/findings.py
    s.352-372) + schemas/board_check_report.schema.json (`required` = aynı
    dokuz alan). Tek kontrol vardır: `mask_dam_channel`.

    ÇIKIŞ KODLARI (dosya başı notu): 0 = PASS, 1 = FAIL, 2 = çalıştırma
    hatası. NO_COVERAGE de 0 döndürür (`board_exit_code()`), o yüzden "iz
    bulunamadı" ile "baraj temiz" ayrımı YALNIZ `by_check.mask_dam_channel.
    status` / `summary` okunarak yapılır.
    """

    schema_version: str  # "board-check-report/v1"
    tool: str            # "mask_dam"
    board: str           # ölçülen kartın yolu (girdideki `board`)
    design: dict         # {"path": str, "sha256": str | None}
    params: dict         # {"min_dam_mm", "search_mm", "track_tol_mm"} — kullanılan eşikler
    metrics: dict        # {"mask_dam_channel": ihlal sayısı | None}
    by_check: dict       # {"mask_dam_channel": {"status", "scanned", "violations"}}
    checks: list         # Finding listesi: check/status/scanned/violations/detail
    summary: dict        # {"PASS": n, "FAIL": n, "NO_COVERAGE": n}


def half_extent_along(pad, dirx, diry):
    """Eksen-hizalı yaklaşık yarı-genişlik izdüşümü (bkz. SINIR notu)."""
    hx, hy = mm(pad.GetSizeX()) / 2.0, mm(pad.GetSizeY()) / 2.0
    return abs(hx * dirx) + abs(hy * diry)


def pad_layers(pad):
    layers = []
    if pad.IsOnLayer(pcbnew.F_Cu):
        layers.append(pcbnew.F_Cu)
    if pad.IsOnLayer(pcbnew.B_Cu):
        layers.append(pcbnew.B_Cu)
    return layers


def all_copper_pads(board):
    out = []
    for f in board.GetFootprints():
        for p in f.Pads():
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue
            out.append((f, p))
    return out


# --------------------------------------------------------------------------- #
def find_pad_pairs(board, search_mm):
    """Bakır boşluğu (0, search_mm] arasında kalan komşu pad çiftleri."""
    pads = all_copper_pads(board)
    pairs = []
    n = len(pads)
    for i in range(n):
        f1, p1 = pads[i]
        c1 = xy(p1.GetPosition())
        for j in range(i + 1, n):
            f2, p2 = pads[j]
            if f1 is f2 and p1.GetNumber() == p2.GetNumber():
                continue
            c2 = xy(p2.GetPosition())
            dist = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
            if dist <= 1e-9:
                continue
            dirx, diry = (c2[0] - c1[0]) / dist, (c2[1] - c1[1]) / dist
            h1 = half_extent_along(p1, dirx, diry)
            h2 = half_extent_along(p2, dirx, diry)
            gap = dist - h1 - h2
            if gap <= 0 or gap > search_mm:
                continue
            common_layers = set(pad_layers(p1)) & set(pad_layers(p2))
            if not common_layers:
                continue
            e1 = (c1[0] + dirx * h1, c1[1] + diry * h1)
            e2 = (c2[0] - dirx * h2, c2[1] - diry * h2)
            pairs.append({
                "f1": f1, "p1": p1, "f2": f2, "p2": p2,
                "gap_mm": gap, "e1": e1, "e2": e2, "layers": common_layers,
            })
    return pairs


# --------------------------------------------------------------------------- #
# Ana kontrol
# --------------------------------------------------------------------------- #
def check_mask_dam(board, min_dam_mm, search_mm, track_tol_mm):
    pairs = find_pad_pairs(board, search_mm)
    # PCB_TRACK filtresini GEVŞETME: aşağıdaki t.GetWidth() argümansız çağrılır
    # ve bu yalnız düz iz için doğrudur. PCB_VIA sızarsa KiCad 10'da
    # "GetWidth called without a layer argument" assert'i tetiklenir
    # (bkz. scripts/board/dfm_emc_check.py:124).
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    scanned, viol = 0, []
    seen = set()
    for pair in pairs:
        for t in tracks:
            layer = t.GetLayer()
            if layer not in pair["layers"]:
                continue
            s, e = xy(t.GetStart()), xy(t.GetEnd())
            half_w = mm(t.GetWidth()) / 2.0
            d = seg_seg_min_dist(s, e, pair["e1"], pair["e2"])
            if d > half_w + track_tol_mm:
                continue
            key = (pair["f1"].GetReference(), pair["p1"].GetNumber(),
                   pair["f2"].GetReference(), pair["p2"].GetNumber(),
                   t.m_Uuid.AsString())
            if key in seen:
                continue
            seen.add(key)
            scanned += 1
            # Karsilastirma TAM SAYI nm ile: float mm'de tam sinirdaki deger
            # (0.15 - (-0.025) - (-0.025) = 0.2) esigin altinda gorunup yanlis
            # ihlal uretiyordu. Ayni sinif hata dfm_emc_check.annular_ring'de
            # de yasandi -> olcum birimi arac ici birim (nm) olmali.
            m1_nm = pair["p1"].GetSolderMaskExpansion(layer)
            m2_nm = pair["p2"].GetSolderMaskExpansion(layer)
            gap_nm = int(round(pair["gap_mm"] * MM))
            dam_nm = gap_nm - m1_nm - m2_nm
            m1, m2, dam = mm(m1_nm), mm(m2_nm), mm(dam_nm)
            entry = {
                "pad_a": f"{pair['f1'].GetReference()}.{pair['p1'].GetNumber()}",
                "pad_b": f"{pair['f2'].GetReference()}.{pair['p2'].GetNumber()}",
                "track_net": t.GetNetname(),
                "layer": t.GetLayerName(),
                "copper_gap_mm": round(pair["gap_mm"], 4),
                "mask_expansion_a_mm": round(m1, 4),
                "mask_expansion_b_mm": round(m2, 4),
                "dam_mm": round(dam, 4),
            }
            if dam_nm < int(round(min_dam_mm * MM)):
                viol.append(entry)
    return result(
        "mask_dam_channel", scanned, viol,
        f"baraj = bakır_boşluk - mask_expansion(a) - mask_expansion(b); "
        f"eşik ≥{min_dam_mm} mm. Aday kanal arama yarıçapı={search_mm} mm, "
        f"iz-kanal toleransı={track_tol_mm} mm. scanned=bulunan (pad_çifti, iz) "
        "kombinasyonu sayısı; via/zone/ark izler kapsam dışı (bkz. SINIR).")


CHECKS = ("mask_dam_channel",)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--json")
    ap.add_argument("--min-dam-mm", type=float, default=FAB_MIN_MASK_DAM_MM)
    ap.add_argument("--search-mm", type=float, default=3.0,
                     help="Komşu pad adayı aranacak azami bakır boşluğu (mm)")
    ap.add_argument("--track-tol-mm", type=float, default=0.05,
                     help="İzin kanal merkez hattına yaklaşma toleransı (mm)")
    a = ap.parse_args(argv)
    return run_analysis(a)


def run_analysis(a) -> int:
    board = pcbnew.LoadBoard(a.board)
    findings = [check_mask_dam(board, a.min_dam_mm, a.search_mm, a.track_tol_mm)]
    report = board_report(TOOL_NAME, a.board, findings, params={
        "min_dam_mm": a.min_dam_mm,
        "search_mm": a.search_mm,
        "track_tol_mm": a.track_tol_mm,
    })
    write_report(report, a.json)
    emit(report)
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
