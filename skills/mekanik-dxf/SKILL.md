---
name: mekanik-dxf
description: DXF/STEP'ten board outline + montaj deliği + 3D keepout hacmi üret; IPD tolerans zinciri ve mekanik datum kaydı. Faz 1'de paralel koşar, bariyerde placement'e girer.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 1 — Temeller (paralel kök düğüm; bariyerde placement'e girer)"
assurance_loop: [L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: case.dxf, schema: null, required: true}
  - {artifact: enclosure.step, schema: null, required: false}
outputs:
  - {artifact: board_outline.json, schema: null}
  - {artifact: keepout_zones.json, schema: references/keepout_zones.schema.json}
  - {artifact: mounting_holes.json, schema: null}
writes_board: false
tools: [mechanical-dxf, mcad-collision, cross-section]
---

# SKILL: Mekanik / DXF

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

> `keepout_zones.json` bu skill'in **sahip olduğu** artifact'tir ve akıştaki en
> çok tüketilen dosyadır ([[../dfm/SKILL|dfm]] placement,
> [[../konnektor/SKILL|konnektor]] mating/aktüatör ekleri,
> [[../pcb-orchestrator/SKILL|orchestrator]] bariyeri). Şeması burada durur.

## 0. Ne zaman çağrılır

- Faz 1'de, güç ve BOM düğümleriyle **paralel**. Adım 1/2'den bağımsızdır.
- Enclosure, konnektör seçimi veya optik eksen değiştiğinde yeniden koşar.
- Placement (Faz 3 bariyeri) açılmadan önce `keepout_zones.json` yayınlanmış
  olmalıdır; yoksa bariyer açılmaz.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| Edge.Cuts poligonu | `case.dxf` | Açık poligon → board dolmaz, `CONFIRM` |
| birim + orijin | DXF başlığı + bilinen delik koordinatı | çapa kontrolü yapılamazsa `NO_COVERAGE` |
| tavan haritası (bölge-bazlı Z) | `enclosure.step` | `global_max_height_mm` tek değerle `SCREEN_ONLY` |
| optik merkez ofseti | sensör datasheet'i | `TBD` — footprint origin'i optik merkez sanma |
| `datum_tolerance` | mekanik çizim | enclosure nominalinden **türetip PASS verme**, `CONFIRM` |
| konnektör mating/aktüatör hacmi | [[../konnektor/SKILL|konnektor]] §3 | keepout eksik → kart monte edilemez |

## 2. Adımlar

1. `import_board_outline(path)` → kapalı Edge.Cuts poligonu + delikler.
   **Birim (mm/mil) ve orijin** doğrula (bilinen delik koordinatıyla çapa kontrolü).
2. `derive_keepouts(holes, step_model, ring_mm=3.0)` → montaj deliği + konnektör +
   optik eksen keepout. Her bölgeye **`max_allowed_height_mm`**
   (3D keepout hacmi = 2D poligon × [0, tavan]). Kutu tavanı düz değilse STEP'ten
   **tavan haritası** türet (bölge-bazlı Z).
3. Stereo/optik: IPD tolerans zinciri
   `IPD = nominal ± (t_yerleşim + t_footprint + t_montaj)`.
   **Optik merkez ≠ footprint origin** ofsetini datasheet'ten al.
4. Z kontrolü: her parçanın **body-height ≤ bölgenin `max_allowed_height_mm`**
   (top + bottom ayrı). CLEARANCE payı (0.2–0.5 mm) bırak.
5. `keepout_zones.json` yayınla → placement'in sabit kısıtı.
6. Mekanik referansı açıkça kaydet: `datum_type` (`edge|hole|mounting_fixture`),
   `datum_feature_ids`, `connector_offset_from_datum` ve `datum_tolerance`.
   Konnektöre özgü tolerans bilinmiyorsa enclosure nominal clearance'ından
   türetip `PASS` verme; `CONFIRM` bırak.
7. Her STEP girdisi için `step_source`, `step_revision`, `step_origin`,
   `step_units`, `body_only_or_mated_state` ve `step_tolerance_assumption`
   alanlarını receipt'e bağla. Bounding box'ı üretici 2D çizimiyle çapraz
   doğrula; revizyon/orijin/birim belirsizse sonuç yalnız ön elemedir.

## 3. Nüanslar — bilinen tuzaklar

### (a) Konnektör keepout'u gövdeden ibaret değildir
Bölgeye **eşleşme koridoru** (fiş/kablo düz girebilmeli) + **kilit kolu /
aktüatör süpürme hacmi** de girer. Gövdeye göre çizilen keepout kartı monte
edilemez yapar. Bu üçlü, `keepout_zones.json` şemasında ayrı `kind` değerleri
olarak tutulur; konnektör tarafındaki gerekçe
[[../konnektor/SKILL|konnektor]] §3'tedir.

### (b) STEP nihai mating kanıtı değildir
Gerçek mate parçası/FPC, flatness toleransı, insertion yönü ve aktüatör süpürme
hacmini fiziksel fixture ile kontrol et; STEP yalnız **nominal çarpışma ön
elemesidir**. Eksik 3D model varsa sessizce atlama, `NO_COVERAGE` üret.

### (c) Footprint'teki `PCB Edge` çizgisi gerçek Edge.Cuts değildir
Footprint'in yardımcı `PCB Edge` çizgisini gerçek kart kenarı sanma; koordinat
farkını **ölç** ve datasheet board-edge/mating envelope ile doğrula. Sapma varsa
routing başlamadan blocker aç.

### (d) MCAD çarpışma fixture'ı sabitlenmelidir
Kart + enclosure export'unu sabit fixture olarak sakla; orijin, birim, model
sürümü ve clearance eşiğini receipt'e yaz. Aksi halde iki koşu arasında sonuç
sessizce değişir.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek mating kuvveti / yön | D | Fiziksel fixture gerekir; STEP nominal |
| Board flatness (reflow sonrası warpage) | D | Ölçüm ister; stackup simetrisi yalnız ön-elek |
| FPC kuyruk kalınlığı toleransı | C | Konnektör datasheet'i; türetilemez |
| Kutu içi termal etkileşim | C→D | [[../guc-isil/SKILL|guc-isil]] §3c CFD/korelasyon |

## 5. Doğrulama

```bash
# keepout_zones.json şema doğrulaması
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: bir bölgenin `max_allowed_height_mm` değerini komşu parçanın
body-height'ının altına çek → Z kontrolü **FAIL vermeli**. Vermiyorsa placement
kapısı boştur.

## 6. Kabul kriterleri

- [ ] Edge.Cuts poligonu **kapalı**; açıksa toleransla kapatıldı veya `CONFIRM` yazıldı.
- [ ] Birim ve orijin bilinen bir delik koordinatıyla çapa kontrolünden geçti.
- [ ] Her keepout bölgesinde `max_allowed_height_mm` var; `global_max_height_mm` yayınlandı.
- [ ] Konnektör bölgeleri **gövde + eşleşme koridoru + aktüatör süpürme hacmi**
      olarak üç ayrı `kind` ile girildi.
- [ ] Girişim/keepout için mekanik tolerans (±0.1–0.2 mm) eklendi.
- [ ] `datum_type` / `datum_feature_ids` / `datum_tolerance` kaydedildi;
      türetilmiş tolerans `PASS` sayılmadı.
- [ ] Her STEP girdisi için kaynak/revizyon/orijin/birim receipt'e bağlandı.
- [ ] Footprint `PCB Edge` çizgisi gerçek Edge.Cuts ile **sayısal** karşılaştırıldı.
- [ ] Eksik 3D model `NO_COVERAGE` olarak yazıldı, sessizce atlanmadı.
- [ ] Board-yazımı gerekiyorsa **KiCad kapalı** + `.bak` (kilit kuralı →
      [[../pcb-orchestrator/SKILL|orchestrator]]).
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../konnektor/SKILL|konnektor]] §3 · [[../dfm/SKILL|dfm]] §Yerleşim ·
[[../pcb-orchestrator/SKILL|orchestrator]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`cross-section`** — Kartın bir bölgesini ajanın **görebileceği** PNG'ye basar; bir ölçüm tartışmalı olduğunda bakılacak şey.
