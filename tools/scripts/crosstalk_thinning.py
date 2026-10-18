#!/usr/bin/env python3
"""Paralel iz çifti için crosstalk (NEXT) taraması + güvenli iz inceltme kararı.

backend: file/pure-calc  ·  model_class: exploratory_closed_form  ·  signoff: false

NE YAPAR
--------
İki paralel iz için geri-yönlü çapraz karışmayı (NEXT, near-end crosstalk)
kapalı-form geometri modeliyle TARAR ve tek bir aksiyon döndürür:

    SAFE_TO_THIN      -> gürültü bütçesi altında; `suggested_width_mm` ile incelt
    CROSSTALK_RISK    -> ASLA inceltme; `suggested_spacing_mm` kadar ayır
    THINNING_BLOCKED  -> boşluk yeterli ama inceltme yasak (empedans/akım/fab tabanı)
    NO_COVERAGE       -> EM kararı için girdi yok; sessiz PASS yok

MODEL
-----
Geri-yönlü (backward) kuplaj katsayısı, Johnson & Graham'ın yaygın kapalı-form
geometri yaklaşımıdır:

    kx   = 1 / (1 + (D/H)^2)          D = merkez-merkez = S + W, H = en yakın
                                       referans düzlemine dielektrik yükseklik
    Kb   = KB_MAX * kx                KB_MAX = 0.25 (sıkı kuplajda Kb doyumu)
    NEXT = Kb * min(1, L / L_sat)     L_sat = tr * v / 2  (doyum uzunluğu)

`L_sat` altındaki kuplaj uzunluğunda NEXT doğrusal ölçeklenir: kısa paralellik
tam genlikli NEXT üretmez. `v = c / sqrt(er_eff)`; mikroşeritte
`er_eff = (er+1)/2 + (er-1)/2 * (1+12H/W)^-0.5`, striplinede `er_eff = er`.

İNCELTME MANTIĞI (asıl nokta)
-----------------------------
İzi inceltmek TEK BAŞINA crosstalk'ı azaltmaz. Alan kazancı ancak boşluk
korunup izler yaklaştırılırsa gerçekleşir; bu da merkez-merkez mesafeyi (D)
küçültür ve kuplajı ARTIRIR. Bu yüzden araç, inceltilmiş W' için NEXT'i
`D' = S + W'` ile YENİDEN hesaplar ve bütçe hâlâ tutuyorsa SAFE_TO_THIN der.
Ayrıca inceltme Z0'ı yükseltir; kontrollü empedans netlerinde inceltme
kategorik olarak yasaktır.

BİLEREK YAPILMAYANLAR (güven sınırı)
------------------------------------
- Bu bir 2D/3D field solver DEĞİLDİR. Kapalı-form kuplaj modelinin hatası
  geometriye göre sistematik değişir; çıktı `signoff: false` taşır ve
  kontrollü empedans/hızlı arayüz imzası için field solver zorunludur.
- FEXT (uzak-uç) MODELLENMEZ. Mikroşerit inhomojen ortamdır, FEXT sıfır
  değildir ve uzun kuplajda NEXT'i geçebilir. Mikroşerit girdisinde araç
  bunu `CONFIRM` olarak yayınlar, PASS saymaz.
- Referans düzlemi sürekliliği ölçülmez; `reference_plane_continuous=false`
  verilirse model geçersizdir ve sonuç NO_COVERAGE'a düşer (yarık üzerinden
  dönüş yolu kuplajı bu denklemin kapsamı dışıdır).
- Akım/genişlik ön-elemesi IPC-2221 formudur, IPC-2152 DEĞİL. Yalnız
  `SCREEN_ONLY`; imza için pcb-tool-v2/`ipc2152_hesaplayici.py` kullanılır.
- Stripline'da `b = 2H` (simetrik) varsayılır; asimetrik stripline CONFIRM'dür.

Kullanım:
    python3 crosstalk_thinning.py --trace-width-mm 0.20 --spacing-mm 0.20 \
        --dielectric-height-mm 0.20 --coupled-length-mm 40 --rise-time-ps 300
    python3 crosstalk_thinning.py --input-json girdi.json
    python3 crosstalk_thinning.py --trace-width-mm 0.2 --spacing-mm 0.2 --json rapor.json
    python3 crosstalk_thinning.py --self-test

CLI SÖZLEŞMESİ (bundle geneli)
------------------------------
``--json`` YAZAR. Girdiyi dosyadan okumak için ``--input-json`` kullan.
(Bu araç eskiden ``--json`` ile OKUYOR, ``--out`` ile yazıyordu — kardeş
araçların tam tersi. ``--out`` gizli/deprecated alias olarak kabul edilir.)

Çıkış kodu: 0 = crosstalk sorunu yok (SAFE_TO_THIN veya THINNING_BLOCKED),
1 = CROSSTALK_RISK / NO_COVERAGE, 2 = girdi veya çalıştırma hatası.
"Inceltebilir miyim?" sorusunun cevabı exit code değil, ``action`` alanıdır.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.report_io import emit, run_cli, write_report  # noqa: E402
from common.si_models import (  # noqa: E402
    ModelDomainError,
    coupling_ratio,
    er_eff,
    ipc2221_min_width_mm,
    prop_velocity_mm_ps,
    z0_with_notes,
)
from common.units import C_MM_PS, MM_TO_MIL, OZ_TO_MM  # noqa: E402

SCHEMA_VERSION = "crosstalk-thinning/v1"

# What the caller should DO. Kept separate from ``status`` and from the exit
# code, because the exit code answers "is there a crosstalk problem?" while
# this answers "may I thin this trace?".
ACTION_BY_STATUS = {
    "SAFE_TO_THIN": "THIN_TO_SUGGESTED_WIDTH",
    "THINNING_BLOCKED": "KEEP_WIDTH",
    "CROSSTALK_RISK": "INCREASE_SPACING",
    "NO_COVERAGE": "GATHER_EM_INPUTS",
}

KB_MAX = 0.25                  # doymuş geri-yönlü kuplaj katsayısı tavanı

# Fab tabanları yalnız BAŞLANGIÇ politikasıdır; gerçek değer fab_capabilities.json'dan gelir.
DEFAULT_FAB_MIN_WIDTH_MM = 0.15        # 6 mil
DEFAULT_FAB_MIN_CLEARANCE_MM = 0.15    # 6 mil
DEFAULT_NOISE_BUDGET = 0.05            # sinyal salınımının %5'i
THIN_STEP_MM = 0.005


class InputError(ValueError):
    """Girdi sözleşmesi ihlali."""


# ---------------------------------------------------------------- yardımcılar

def _pos(name: str, value, required: bool = True):
    if value is None:
        if required:
            raise InputError(f"{name} zorunlu")
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise InputError(f"{name} sayı olmalı, gelen: {value!r}")
    if not math.isfinite(v) or v <= 0:
        raise InputError(f"{name} sonlu ve pozitif olmalı, gelen: {v}")
    return v


def next_ratio(w_mm: float, s_mm: float, h_mm: float, er: float,
               layer_type: str, coupled_len_mm: float, rise_ps: float) -> dict:
    """NEXT genliğini saldırgan salınımına oran olarak döndürür."""
    d_mm = s_mm + w_mm
    kx = coupling_ratio(d_mm, h_mm)
    kb = KB_MAX * kx
    ere = er_eff(er, h_mm, w_mm, layer_type)
    v = prop_velocity_mm_ps(ere)
    l_sat = rise_ps * v / 2.0
    length_factor = min(1.0, coupled_len_mm / l_sat) if l_sat > 0 else 1.0
    return {
        "center_to_center_mm": round(d_mm, 4),
        "d_over_h": round(d_mm / h_mm, 3),
        "coupling_ratio_kx": round(kx, 5),
        "kb_saturated": round(kb, 5),
        "er_eff": round(ere, 3),
        "prop_velocity_mm_per_ps": round(v, 5),
        "saturation_length_mm": round(l_sat, 3),
        "length_factor": round(length_factor, 4),
        "next_ratio": kb * length_factor,
    }


def impedance_z0_ohm(w_mm: float, h_mm: float, er: float, t_mm: float,
                     layer_type: str) -> dict:
    """IPC-2141 kapalı-form Z0. Yalnız yön duygusu; sign-off değeri değildir.

    Now delegates to ``common.si_models.z0_with_notes``, which is the single
    IPC-2141 implementation shared with zdiff_solver. Behaviour change: an
    out-of-domain geometry now yields ``z0_ohm: None`` plus a NO_COVERAGE
    note instead of a silently NEGATIVE impedance.
    """
    return z0_with_notes(w_mm, h_mm, er, t_mm, layer_type)


def solve_spacing_for_budget(w_mm: float, h_mm: float, er: float, layer_type: str,
                             coupled_len_mm: float, rise_ps: float,
                             budget: float) -> float | None:
    """Bütçeyi sağlayan minimum kenar-kenar boşluğu ters-çözer (None = çözümsüz)."""
    ere = er_eff(er, h_mm, w_mm, layer_type)
    v = prop_velocity_mm_ps(ere)
    l_sat = rise_ps * v / 2.0
    length_factor = min(1.0, coupled_len_mm / l_sat) if l_sat > 0 else 1.0
    if length_factor <= 0:
        return None
    # KB_MAX * length_factor / (1 + (D/H)^2) = budget
    rhs = KB_MAX * length_factor / budget - 1.0
    if rhs <= 0:
        return None  # doyumda bile bütçe altında -> ayırmaya gerek yok
    d_mm = h_mm * math.sqrt(rhs)
    return max(0.0, d_mm - w_mm)


# ---------------------------------------------------------------- ana analiz

def analyze(
    trace_width_mm,
    spacing_mm,
    dielectric_height_mm=None,
    layer_type="microstrip",
    coupled_length_mm=None,
    rise_time_ps=None,
    er=4.3,
    copper_weight_oz=1.0,
    reference_plane_continuous=None,
    impedance_controlled=False,
    noise_budget_ratio=DEFAULT_NOISE_BUDGET,
    current_a=None,
    delta_t_c=10.0,
    fab_min_width_mm=DEFAULT_FAB_MIN_WIDTH_MM,
    fab_min_clearance_mm=DEFAULT_FAB_MIN_CLEARANCE_MM,
    net_class=None,
) -> dict:
    """Tek paralel-çift için crosstalk taraması + inceltme kararı."""
    w = _pos("trace_width_mm", trace_width_mm)
    s = _pos("spacing_mm", spacing_mm)
    er_v = _pos("er", er)
    budget = _pos("noise_budget_ratio", noise_budget_ratio)
    if not 0 < budget < 1:
        raise InputError(f"noise_budget_ratio 0..1 aralığında olmalı, gelen: {budget}")
    if layer_type not in ("microstrip", "stripline"):
        raise InputError(f"layer_type 'microstrip'|'stripline' olmalı, gelen: {layer_type!r}")
    h = _pos("dielectric_height_mm", dielectric_height_mm, required=False)
    length = _pos("coupled_length_mm", coupled_length_mm, required=False)
    rise = _pos("rise_time_ps", rise_time_ps, required=False)
    cur = _pos("current_a", current_a, required=False)
    fab_w = _pos("fab_min_width_mm", fab_min_width_mm)
    fab_c = _pos("fab_min_clearance_mm", fab_min_clearance_mm)
    t_mm = _pos("copper_weight_oz", copper_weight_oz) * OZ_TO_MM

    blockers: list[str] = []
    confirm: list[str] = []
    rationale: list[str] = []

    # --- inceltme tabanı: fab + akım (crosstalk'tan bağımsız sert sınırlar)
    floor_terms = {"fab_min_width_mm": fab_w}
    if cur is not None:
        ipc_w = ipc2221_min_width_mm(cur, delta_t_c, copper_weight_oz, layer_type)
        floor_terms["ipc2221_min_width_mm"] = round(ipc_w, 4)
        confirm.append(
            "Akım genişliği IPC-2221 ön-elemesidir; imza için IPC-2152 "
            "(pcb-tool-v2/ipc2152_hesaplayici.py) ile doğrula."
        )
    else:
        confirm.append("current_a verilmedi — akım taşıma tabanı hesaplanmadı.")
    width_floor = max(floor_terms.values())

    # --- 3W geometrik ön-eleme (EM girdisi olmadan da anlamlı)
    three_w_gap_mm = 2.0 * w  # merkez-merkez 3W  <=>  kenar-kenar 2W
    geometry = {
        "three_w_gap_mm": round(three_w_gap_mm, 4),
        "meets_3w": s >= three_w_gap_mm,
        "width_floor_mm": round(width_floor, 4),
        "width_floor_terms": floor_terms,
    }

    # --- kapsam kapısı: EM kararı için H + L + tr zorunlu
    missing = [n for n, v in (("dielectric_height_mm", h),
                              ("coupled_length_mm", length),
                              ("rise_time_ps", rise)) if v is None]
    if reference_plane_continuous is False:
        missing.append("reference_plane_continuous=false")
        blockers.append(
            "Referans düzlemi kesintili: yarık üzerinden dönüş yolu kuplajı bu "
            "modelin kapsamı dışıdır. Önce düzlemi sürekli hale getir."
        )
    elif reference_plane_continuous is None:
        confirm.append(
            "reference_plane_continuous belirtilmedi — model kesintisiz düzlem varsayar."
        )

    if missing:
        suggested_gap = max(three_w_gap_mm, fab_c)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "NO_COVERAGE",
        "action": ACTION_BY_STATUS["NO_COVERAGE"],
            "suggested_width_mm": round(max(w, width_floor), 4),
            "suggested_spacing_mm": round(suggested_gap, 4),
            "input": {"trace_width_mm": w, "spacing_mm": s, "layer_type": layer_type,
                      "impedance_controlled": bool(impedance_controlled),
                      "net_class": net_class},
            "metrics": {"geometry": geometry},
            "coverage": {"eligible": 1, "scanned": 0, "excluded": 1, "violations": 0},
            "blockers": blockers or [f"eksik girdi: {', '.join(missing)}"],
            "confirm": confirm + [
                f"NEXT hesaplanamadı, eksik: {', '.join(missing)}. "
                "Yalnız 3W geometrik ön-elemesi uygulandı — bu bir EM kanıtı değildir."
            ],
            "rationale": [
                "EM girdisi eksik olduğu için inceltme İZNİ VERİLMEDİ; "
                "önerilen boşluk 3W kuralının geometrik tabanıdır."
            ],
            "model_class": "exploratory_closed_form",
            "signoff": False,
            "evidence_origin": "LOCAL_POLICY",
            "assurance_loop": "L1",
            "status_reason": "MISSING_EM_INPUTS",
        }

    # --- mevcut geometride NEXT
    base = next_ratio(w, s, h, er_v, layer_type, length, rise)
    base_next = base["next_ratio"]
    z0_base = impedance_z0_ohm(w, h, er_v, t_mm, layer_type)
    for n in z0_base["notes"]:
        confirm.append(f"Z0 modeli: {n}")
    if layer_type == "microstrip":
        confirm.append(
            "Mikroşerit inhomojendir: FEXT sıfır değildir ve bu araçta "
            "MODELLENMEZ. Uzun kuplajda FEXT'i field solver ile doğrula."
        )

    metrics = {
        "geometry": geometry,
        "baseline": {**{k: v for k, v in base.items() if k != "next_ratio"},
                     "next_ratio": round(base_next, 5),
                     "next_percent": round(base_next * 100.0, 2),
                     "z0_ohm": z0_base["z0_ohm"]},
        "noise_budget_ratio": budget,
    }

    # --- KARAR 1: bütçe aşıldı mı?
    if base_next > budget:
        solved_gap = solve_spacing_for_budget(w, h, er_v, layer_type, length, rise, budget)
        candidates = {"fab_min_clearance_mm": fab_c, "three_w_gap_mm": three_w_gap_mm}
        if solved_gap is not None:
            candidates["budget_solved_gap_mm"] = round(solved_gap, 4)
        suggested_gap = max(candidates.values())
        after = next_ratio(w, suggested_gap, h, er_v, layer_type, length, rise)
        metrics["suggested"] = {
            "spacing_terms": candidates,
            "next_ratio": round(after["next_ratio"], 5),
            "next_percent": round(after["next_ratio"] * 100.0, 2),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "CROSSTALK_RISK",
        "action": ACTION_BY_STATUS["CROSSTALK_RISK"],
            "suggested_width_mm": round(w, 4),      # DEĞİŞTİRME
            "suggested_spacing_mm": round(suggested_gap, 4),
            "input": {"trace_width_mm": w, "spacing_mm": s, "layer_type": layer_type,
                      "impedance_controlled": bool(impedance_controlled),
                      "net_class": net_class},
            "metrics": metrics,
            "coverage": {"eligible": 1, "scanned": 1, "excluded": 0, "violations": 1},
            "blockers": blockers + [
                f"NEXT {base_next * 100:.2f}% > bütçe {budget * 100:.2f}% — inceltme yasak."
            ],
            "confirm": confirm,
            "rationale": [
                f"D/H = {base['d_over_h']}, kx = {base['coupling_ratio_kx']}; "
                f"kuplaj uzunluğu {length} mm, doyum uzunluğu "
                f"{base['saturation_length_mm']} mm (length_factor "
                f"{base['length_factor']}).",
                f"Boşluğu {s} mm -> {suggested_gap:.4f} mm yap; iz genişliği {w} mm "
                "sabit kalır. İnceltme merkez-merkez mesafeyi küçültüp kuplajı artırır.",
                "Boşluk artırılamıyorsa alternatifler: kuplaj uzunluğunu kısalt, "
                "araya GND guard + stitching via koy, çiftleri farklı katmanlara "
                "ortogonal ayır, ya da stackup'ta H'ı küçült (D/H büyür).",
            ],
            "model_class": "exploratory_closed_form",
            "signoff": False,
            "evidence_origin": "LOCAL_POLICY",
            "assurance_loop": "L1",
            "status_reason": "NEXT_OVER_BUDGET",
        }

    # --- KARAR 2: bütçe tutuyor; inceltme mümkün mü?
    if impedance_controlled:
        blockers.append(
            "Net kontrollü empedanslıdır: inceltme Z0'ı değiştirir. Genişlik "
            "empedans çözücüsünün çıktısıdır, alan optimizasyonunun değil."
        )
    undersized = w < width_floor - 1e-9
    if undersized:
        blockers.append(
            f"İz mevcut haliyle ZATEN TABANIN ALTINDA: {w} mm < "
            f"{width_floor:.4f} mm ({', '.join(f'{k}={v}' for k, v in floor_terms.items())}). "
            "İnceltme değil, GENİŞLETME gerekiyor."
        )
    elif w <= width_floor + 1e-9:
        blockers.append(
            f"İz zaten taban genişlikte ({w} mm <= {width_floor:.4f} mm); "
            "inceltilecek pay yok."
        )

    if blockers:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "THINNING_BLOCKED",
        "action": ACTION_BY_STATUS["THINNING_BLOCKED"],
            "suggested_width_mm": round(max(w, width_floor), 4),
            "suggested_spacing_mm": round(max(s, fab_c), 4),
            "input": {"trace_width_mm": w, "spacing_mm": s, "layer_type": layer_type,
                      "impedance_controlled": bool(impedance_controlled),
                      "net_class": net_class},
            "metrics": metrics,
            "coverage": {"eligible": 1, "scanned": 1, "excluded": 0,
                         "violations": 1 if undersized else 0},
            "blockers": blockers,
            "confirm": confirm,
            "rationale": [
                f"NEXT {base_next * 100:.2f}% <= bütçe {budget * 100:.2f}% — "
                "boşluk yeterli, sinyal bütünlüğü tarafında aksiyon yok.",
                (f"İz akım/fab tabanının altında; {width_floor:.4f} mm'ye genişlet "
                 "ve genişleyen bakırın boşluğu yemediğini yeniden tara."
                 if undersized else
                 "İnceltme sinyal bütünlüğü dışındaki sert bir sınıra takıldı; "
                 "geometriyi olduğu gibi bırak."),
            ],
            "model_class": "exploratory_closed_form",
            "signoff": False,
            "evidence_origin": "LOCAL_POLICY",
            "assurance_loop": "L1",
            "status_reason": "THINNING_NOT_PERMITTED",
        }

    # İnceltme taraması: boşluk KORUNUR, izler yaklaşır -> D küçülür, kuplaj artar.
    best_w = w
    best = base
    steps = int(math.floor((w - width_floor) / THIN_STEP_MM))
    for i in range(1, steps + 1):
        cand_w = w - i * THIN_STEP_MM
        if cand_w < width_floor:
            break
        cand = next_ratio(cand_w, s, h, er_v, layer_type, length, rise)
        if cand["next_ratio"] <= budget:
            best_w, best = cand_w, cand
        else:
            break
    if best_w > width_floor and (w - best_w) >= THIN_STEP_MM:
        cand = next_ratio(width_floor, s, h, er_v, layer_type, length, rise)
        if cand["next_ratio"] <= budget:
            best_w, best = width_floor, cand

    z0_new = impedance_z0_ohm(best_w, h, er_v, t_mm, layer_type)
    # Either impedance may be None when the geometry leaves the IPC-2141
    # domain. Report the delta as unavailable rather than crashing or
    # inventing a percentage.
    if z0_new["z0_ohm"] is None or not z0_base["z0_ohm"]:
        z0_delta_pct = None
        confirm.append(
            "Z0 deltası hesaplanamadı: geometri IPC-2141 kapalı-form "
            "geçerlilik alanı dışında (z0 NO_COVERAGE).")
    else:
        z0_delta_pct = (z0_new["z0_ohm"] - z0_base["z0_ohm"]) / z0_base["z0_ohm"] * 100.0
    area_gain_pct = (w - best_w) / w * 100.0
    metrics["suggested"] = {
        "width_mm": round(best_w, 4),
        "next_ratio": round(best["next_ratio"], 5),
        "next_percent": round(best["next_ratio"] * 100.0, 2),
        "z0_ohm": z0_new["z0_ohm"],
        "z0_delta_percent": (None if z0_delta_pct is None
                             else round(z0_delta_pct, 2)),
        "width_reduction_percent": round(area_gain_pct, 2),
    }
    if z0_delta_pct is not None and abs(z0_delta_pct) > 15.0:
        confirm.append(
            f"İnceltme Z0'ı %{z0_delta_pct:.1f} değiştiriyor "
            f"({z0_base['z0_ohm']} -> {z0_new['z0_ohm']} Ω). Net kontrollü "
            "empedanslı olmasa bile kaynak/yük uyumunu ve DC direnç/IR düşümünü kontrol et."
        )

    if best_w >= w - 1e-9:
        status = "THINNING_BLOCKED"
        reason = "THINNING_WOULD_EXCEED_BUDGET"
        blockers.append(
            "Boşluk korunarak yapılan her inceltme adımı merkez-merkez mesafeyi "
            "küçültüp NEXT'i bütçenin üstüne çıkarıyor."
        )
        rationale.append(
            "Mevcut geometri bütçeyi sağlıyor ama inceltme payı yok; alan kazanmak "
            "için önce H'ı küçült (stackup) ya da kuplaj uzunluğunu kısalt."
        )
    else:
        status = "SAFE_TO_THIN"
        reason = "NEXT_UNDER_BUDGET"
        rationale.append(
            f"NEXT {base_next * 100:.2f}% <= bütçe {budget * 100:.2f}%; "
            f"{best_w:.3f} mm genişlikte bile NEXT "
            f"{best['next_ratio'] * 100:.2f}% ile bütçe altında kalıyor."
        )
        rationale.append(
            f"Genişlik {w} -> {best_w:.3f} mm (%{area_gain_pct:.1f} kazanç); "
            f"kenar-kenar boşluk {s} mm KORUNUR, merkez-merkez "
            f"{base['center_to_center_mm']} -> {best['center_to_center_mm']} mm'ye iner."
        )
        rationale.append(
            f"Taban: {width_floor:.4f} mm ({', '.join(f'{k}={v}' for k, v in floor_terms.items())})."
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "action": ACTION_BY_STATUS.get(status, "REVIEW"),
        "suggested_width_mm": round(best_w, 4),
        "suggested_spacing_mm": round(max(s, fab_c), 4),
        "input": {"trace_width_mm": w, "spacing_mm": s, "layer_type": layer_type,
                  "impedance_controlled": bool(impedance_controlled),
                  "net_class": net_class},
        "metrics": metrics,
        "coverage": {"eligible": 1, "scanned": 1, "excluded": 0, "violations": 0},
        "blockers": blockers,
        "confirm": confirm,
        "rationale": rationale,
        "model_class": "exploratory_closed_form",
        "signoff": False,
        "evidence_origin": "LOCAL_POLICY",
        "assurance_loop": "L1",
        "status_reason": reason,
    }


# ---------------------------------------------------------------- self-test

def _self_test() -> int:
    fails = []

    def check(label, cond, detail=""):
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label} {detail}")
            fails.append(label)

    print("[1] Sıkı kuplaj -> CROSSTALK_RISK ve inceltme yasak")
    r = analyze(trace_width_mm=0.20, spacing_mm=0.10, dielectric_height_mm=0.20,
                coupled_length_mm=60, rise_time_ps=200)
    check("status == CROSSTALK_RISK", r["status"] == "CROSSTALK_RISK", r["status"])
    check("genişlik değişmedi", r["suggested_width_mm"] == 0.20, r["suggested_width_mm"])
    check("boşluk büyütüldü", r["suggested_spacing_mm"] > 0.10, r["suggested_spacing_mm"])
    check("önerilen boşlukta NEXT bütçe altında",
          r["metrics"]["suggested"]["next_ratio"] <= 0.05 + 1e-6)

    print("[2] Geniş boşluk -> SAFE_TO_THIN, fab tabanı ihlal edilmez")
    r = analyze(trace_width_mm=0.40, spacing_mm=1.60, dielectric_height_mm=0.10,
                coupled_length_mm=20, rise_time_ps=500)
    check("status == SAFE_TO_THIN", r["status"] == "SAFE_TO_THIN", r["status"])
    check("öneri < mevcut", r["suggested_width_mm"] < 0.40, r["suggested_width_mm"])
    check("öneri >= fab tabanı", r["suggested_width_mm"] >= DEFAULT_FAB_MIN_WIDTH_MM,
          r["suggested_width_mm"])
    check("önerilen genişlikte NEXT bütçe altında",
          r["metrics"]["suggested"]["next_ratio"] <= 0.05 + 1e-6)

    print("[3] Kontrollü empedans -> asla inceltme")
    r = analyze(trace_width_mm=0.40, spacing_mm=1.60, dielectric_height_mm=0.10,
                coupled_length_mm=20, rise_time_ps=500, impedance_controlled=True)
    check("status == THINNING_BLOCKED", r["status"] == "THINNING_BLOCKED", r["status"])
    check("genişlik korundu", r["suggested_width_mm"] == 0.40, r["suggested_width_mm"])

    print("[4] Akım tabanı fab tabanını ezer; dar iz genişletme olarak raporlanır")
    r = analyze(trace_width_mm=1.20, spacing_mm=3.00, dielectric_height_mm=0.10,
                coupled_length_mm=20, rise_time_ps=1000, current_a=3.0)
    floor = r["metrics"]["geometry"]["width_floor_mm"]
    check("IPC tabanı > fab tabanı", floor > DEFAULT_FAB_MIN_WIDTH_MM, floor)
    check("status == THINNING_BLOCKED", r["status"] == "THINNING_BLOCKED", r["status"])
    check("öneri >= akım tabanı", r["suggested_width_mm"] >= floor - 1e-9,
          (r["suggested_width_mm"], floor))
    check("ihlal sayıldı", r["coverage"]["violations"] == 1)

    print("[4b] Akım tabanı üstündeki geniş iz tabana kadar incelir")
    r = analyze(trace_width_mm=2.00, spacing_mm=4.00, dielectric_height_mm=0.10,
                coupled_length_mm=20, rise_time_ps=1000, current_a=1.0)
    floor = r["metrics"]["geometry"]["width_floor_mm"]
    check("status == SAFE_TO_THIN", r["status"] == "SAFE_TO_THIN", r["status"])
    check("öneri >= akım tabanı", r["suggested_width_mm"] >= floor - 1e-9,
          (r["suggested_width_mm"], floor))
    check("öneri < mevcut", r["suggested_width_mm"] < 2.00, r["suggested_width_mm"])

    print("[5] Eksik EM girdisi -> NO_COVERAGE, sessiz PASS yok")
    r = analyze(trace_width_mm=0.20, spacing_mm=0.60)
    check("status == NO_COVERAGE", r["status"] == "NO_COVERAGE", r["status"])
    check("scanned == 0", r["coverage"]["scanned"] == 0)
    check("SAFE_TO_THIN dönmedi", r["status"] != "SAFE_TO_THIN")

    print("[6] Kesintili referans düzlemi -> NO_COVERAGE")
    r = analyze(trace_width_mm=0.20, spacing_mm=0.60, dielectric_height_mm=0.20,
                coupled_length_mm=20, rise_time_ps=500,
                reference_plane_continuous=False)
    check("status == NO_COVERAGE", r["status"] == "NO_COVERAGE", r["status"])

    print("[7] Monotonluk: boşluk arttıkça NEXT azalır")
    prev = 1.0
    mono = True
    for gap in (0.10, 0.20, 0.40, 0.80, 1.60):
        n = next_ratio(0.20, gap, 0.20, 4.3, "microstrip", 100, 200)["next_ratio"]
        mono = mono and n < prev
        prev = n
    check("NEXT(boşluk) kesin azalan", mono)

    print("[8] Kuplaj uzunluğu doyum altında -> NEXT ölçekleniyor")
    short = analyze(trace_width_mm=0.20, spacing_mm=0.10, dielectric_height_mm=0.20,
                    coupled_length_mm=1.0, rise_time_ps=200)
    long_ = analyze(trace_width_mm=0.20, spacing_mm=0.10, dielectric_height_mm=0.20,
                    coupled_length_mm=60, rise_time_ps=200)
    check("kısa kuplaj NEXT < uzun kuplaj NEXT",
          short["metrics"]["baseline"]["next_ratio"] < long_["metrics"]["baseline"]["next_ratio"])
    check("length_factor < 1", short["metrics"]["baseline"]["length_factor"] < 1.0)

    print("[9] Fault-injection: model bozulursa test kırmızı olmalı")
    global KB_MAX
    saved = KB_MAX
    try:
        KB_MAX = 0.0   # kuplajı yok say -> her şey 'güvenli' görünür
        r = analyze(trace_width_mm=0.20, spacing_mm=0.10, dielectric_height_mm=0.20,
                    coupled_length_mm=60, rise_time_ps=200)
        check("enjekte edilen kusur yakalandı (risk artık gizleniyor)",
              r["status"] != "CROSSTALK_RISK", r["status"])
    finally:
        KB_MAX = saved

    print("[10] Girdi sözleşmesi: negatif/0 genişlik reddedilir")
    for bad in (0, -0.1, "abc", None):
        try:
            analyze(trace_width_mm=bad, spacing_mm=0.2)
            check(f"reddedildi: {bad!r}", False, "istisna atılmadı")
        except InputError:
            check(f"reddedildi: {bad!r}", True)

    print()
    if fails:
        print(f"SELF-TEST FAIL — {len(fails)} kontrol: {fails}")
        return 1
    print("SELF-TEST PASS")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--trace-width-mm", type=float)
    p.add_argument("--spacing-mm", type=float, help="kenar-kenar boşluk")
    p.add_argument("--dielectric-height-mm", type=float,
                   help="en yakın referans düzlemine yükseklik (H)")
    p.add_argument("--layer-type", default="microstrip",
                   choices=["microstrip", "stripline"])
    p.add_argument("--coupled-length-mm", type=float)
    p.add_argument("--rise-time-ps", type=float)
    p.add_argument("--er", type=float, default=4.3)
    p.add_argument("--copper-weight-oz", type=float, default=1.0)
    p.add_argument("--impedance-controlled", action="store_true")
    p.add_argument("--reference-plane-continuous", dest="ref_plane",
                   choices=["true", "false"], default=None)
    p.add_argument("--noise-budget-ratio", type=float, default=DEFAULT_NOISE_BUDGET)
    p.add_argument("--current-a", type=float)
    p.add_argument("--delta-t-c", type=float, default=10.0)
    p.add_argument("--fab-min-width-mm", type=float, default=DEFAULT_FAB_MIN_WIDTH_MM)
    p.add_argument("--fab-min-clearance-mm", type=float, default=DEFAULT_FAB_MIN_CLEARANCE_MM)
    p.add_argument("--net-class")
    p.add_argument("--input-json", dest="json_in", type=Path,
                   help="girdileri JSON dosyasından oku "
                        "(eski adı --json; --json artık YAZAR)")
    p.add_argument("--json", dest="out", type=Path,
                   help="sonucu JSON dosyasına yaz")
    p.add_argument("--out", dest="out_legacy", type=Path,
                   help=argparse.SUPPRESS)   # deprecated alias for --json
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in fault-injection suite and exit")
    a = p.parse_args(argv)

    if a.out_legacy is not None:
        if a.out is not None:
            p.error("--out is a deprecated alias for --json; pass only one")
        a.out = a.out_legacy

    if a.self_test:
        return _self_test()

    if a.json_in:
        kwargs = json.loads(Path(a.json_in).read_text(encoding="utf-8"))
    else:
        if a.trace_width_mm is None or a.spacing_mm is None:
            p.error("--trace-width-mm ve --spacing-mm zorunlu "
                    "(veya --input-json ile dosyadan ver)")
        kwargs = {
            "trace_width_mm": a.trace_width_mm,
            "spacing_mm": a.spacing_mm,
            "dielectric_height_mm": a.dielectric_height_mm,
            "layer_type": a.layer_type,
            "coupled_length_mm": a.coupled_length_mm,
            "rise_time_ps": a.rise_time_ps,
            "er": a.er,
            "copper_weight_oz": a.copper_weight_oz,
            "impedance_controlled": a.impedance_controlled,
            "reference_plane_continuous": (None if a.ref_plane is None
                                           else a.ref_plane == "true"),
            "noise_budget_ratio": a.noise_budget_ratio,
            "current_a": a.current_a,
            "delta_t_c": a.delta_t_c,
            "fab_min_width_mm": a.fab_min_width_mm,
            "fab_min_clearance_mm": a.fab_min_clearance_mm,
            "net_class": a.net_class,
        }

    try:
        result = analyze(**kwargs)
    except (InputError, ModelDomainError) as exc:
        emit({"schema_version": SCHEMA_VERSION, "status": "ERROR",
              "error": str(exc)})
        return 2

    write_report(result, a.out)
    emit(result)
    return status_exit_code(result["status"])


# --------------------------------------------------------------------------- #
# Exit-code contract
# --------------------------------------------------------------------------- #
# The exit code answers "is there a crosstalk problem?", NOT "did I thin the
# trace?". Both SAFE_TO_THIN and THINNING_BLOCKED mean the pair's NEXT is
# inside budget, so both are 0.
#
# An earlier review called this an inversion. Re-checking the semantics, it
# is not: THINNING_BLOCKED is a clean gate result. What WAS genuinely
# ambiguous is that a caller asking "can I thin?" could not tell the two
# apart from the exit code alone, so ``action`` is now an explicit top-level
# field and callers should branch on that, not on the exit status.
GATE_PASS_STATUSES = ("SAFE_TO_THIN", "THINNING_BLOCKED")
GATE_FAIL_STATUSES = ("CROSSTALK_RISK", "NO_COVERAGE")


def status_exit_code(status: str) -> int:
    """0 = no crosstalk problem, 1 = risk or no coverage, 2 = error."""
    if status in GATE_PASS_STATUSES:
        return 0
    if status == "ERROR":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
