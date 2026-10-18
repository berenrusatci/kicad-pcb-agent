---
name: cpl-assembly
description: Gerber + BOM + CPL üretimi; centroid + fab rotasyon düzeltmesi + oryantasyon kontrolü + DNP parity + panelizasyon. Faz 4 çıkış kapısı — birleşik gate GREEN olmadan koşmaz.
version: 1.1.0
last-reviewed: 2026-08-10
phase: "Faz 4 — Çıkış kapısı (4c; birleşik gate 4b'den sonra)"
assurance_loop: [L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: routed.kicad_pcb, schema: null, required: true}
  - {artifact: bom.json, schema: null, required: true}
  - {artifact: lifecycle_report.json, schema: ../bom-lifecycle/references/lifecycle_report.schema.json, required: true}
  - {artifact: rotation_map.json, schema: references/rotation_map.schema.json, required: true}
outputs:
  - {artifact: gerbers.zip, schema: null}
  - {artifact: cpl.csv, schema: references/cpl-columns.md}
  - {artifact: bom.csv, schema: null}
  - {artifact: fab_receipt.json, schema: null}
writes_board: false
tools: [kicad-cli, production-outputs, production-contracts, cad-assets, jlc-library]
---

# SKILL: Assembly / CPL (Çıkış Kapısı)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

## 0. Ne zaman çağrılır

- **Faz 4c — en son.** Birleşik gate (4b) GREEN olmadan **koşmaz**:
  `final_dfm_receipt.json` + `dft_coverage_receipt.json` +
  `emc_geometry_receipt.json` üçü de GREEN ve güncel board hash'inde olmalı.
- Board değişirse tüm Gerber/CPL/zone fill/length çıktıları **bayatlar** ve son
  board hash'inden yeniden üretilir.
- Panel/depanel kararı bir **yerleşim kısıtıdır** ve Faz 3'e geri beslenir —
  Faz 4'te öğrenilmez (§3b).

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| gövde/courtyard centroid | footprint | pick-point yanlış → tüm parti kayık |
| fab rotasyon ofseti | `rotation_map.json` (**versiyonlu**) | tüm parti ters lehim → `RED` |
| DNP listesi | `bom.json` ∧ CPL | tek taraflıysa parça takılır → `RED` |
| `msl_level` | `lifecycle_report.json` | montaj paketi eksik → `CONFIRM` |
| panel/fiducial planı | panel çizimi | panelsiz üretimde `excluded` |
| son DRC + parity | Faz 4b receipt | gerber üretilmez |

Çıktı kolon sözleşmesi: [`references/cpl-columns.md`](references/cpl-columns.md)

## 2. Adımlar

1. `generate_cpl_file(board, rotation_map)` → Designator, MidX, MidY, Layer, Rotation.
   - Pick-point = **gövde/courtyard centroid**, footprint origin DEĞİL.
   - Rotasyon = footprint açısı + **fab-özel ofset** (JLC rotasyonu KiCad'den farklı).
2. `check_orientation()` → kutuplu parça (diyot/IC pin-1/LED/elektrolitik) yönü ↔
   CPL açısı çapraz kontrol.
   - Populated BOM refdes kümesi CPL refdes kümesine **eşit**; DNP yok; X/Y board
     bounding box içinde; side yalnız top/bottom; rotation sayısal olmalı.
   - Sıfır-yön düzeltmesini **tüm paketlere uygulama.** Yalnız polar sınıflar için
     versioned whitelist ve `expected_pin1_or_cathode_side` kullan.
3. **DNP parity** (bu skill'in sahibi): DNP listesi BOM **ve** CPL'de aynı anda
   işaretli olmalı. Tek taraflıysa montajcı parçayı yine takar. İzolasyon
   0 Ω/DNP jumper'ları ([[../dft-testpoints/SKILL|dft-testpoints]] §3d) ve
   shell/ekran seçenek ayak izleri ([[../konnektor/SKILL|konnektor]] §3e) bu
   listeye dahildir.
4. `generate_fab_package()`: `kicad-cli pcb export gerbers` + `drill` + BOM + CPL → zip.
5. Panel varsa: ≥3 global fiducial (+BGA local), ≥5 mm rail, de-panel stresi
   hassas parçadan uzak.

## 3. Nüanslar — bilinen tuzaklar

### (a) Pasta (F.Paste) katmanı ayrı bir çıktıdır
Gerber setinde gider. EPAD'li parçalarda **tam açıklık bırakma** (windowpane
%50–75) — kuralın kanonik sahibi [[../dfm/SKILL|dfm]] §QFN/EPAD. Stencil dosyası
hatalıysa DRC temiz, kart ölü.

### (b) Panel + depanel kararı bir yerleşim kısıtıdır
Paketleme detayı değil: V-score/mouse-bite hattına yakın seramik parçalar
kırılırken çatlar. Fizik ve mesafe kuralı: [[../dfm/SKILL|dfm]] §MLCC flex crack
(kanonik sahip). Panel şemasını **Faz 3 yerleşimine geri besle**, Faz 4'te öğrenme.

### (c) MSL / kuru paket bilgisi montaj paketine iliştirilir
BGA/QFN'de floor life aşımı = bake veya hurda. Kural ve alan tanımı:
[[../bom-lifecycle/SKILL|bom-lifecycle]] §3a (kanonik sahip).

### (d) Bottom-side dikkat
X mirror + rotasyon işareti. Bottom parçalarda rotasyon ofseti top ile aynı
değildir; `rotation_map.json` `side` bazlı olmalıdır.

### (e) "zip aldım" doğrulama değildir
Gerber üretiminden önce **son DRC + son netlist parity** koşulmuş olmalı
(`--schematic-parity --severity-all --exit-code-violations` →
[[../sematik-erc/SKILL|sematik-erc]] §4).

### (f) Courtyard çakışması burada patlarsa geç
Yerleşim adımında (Faz 3) yakalanmalıydı → [[../dfm/SKILL|dfm]] §Yerleşim.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek montaj oryantasyonu | D | First-article + AOI/foto kanıtı gerekir |
| Lehim boşluk oranı | D | X-ray |
| Fab'ın paketi kabul etmesi | C | Fab DFM raporu |
| MSL floor life uyumu | C | Montajcı kaydı |
| Panel stres dağılımı | C→D | Depanel denemesi |

> CPL rotasyonunu **yalnız dosya formatıyla doğrulanmış sayma**: ilk üretimde
> kutuplu/BGA/QFN örnekleri için montaj öncesi yön görseli ve first-article
> kontrolü iste. Kanıt kaydı evidence ID, board revizyonu, foto/AOI veya test
> sonucu ve onaylayanı taşır; seri üretim bundan önce açılmaz. Ayrı bir
> dedicated fixture'ı varsayılan zorunluluk yapma.

**Bu tablonun tüketicisi vardır (2026-08-10).** Yukarıdaki beş satırı kapatan
kanıtı toplayıp "seri montajı aç / bloke et" kararını veren
[[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]]'dur (Faz 5A). Bu
skill **kontrolün istenmesini** kabul kriteri yapar; kabul **artifact'ini** o
skill yayımlar (`first_article_report.json`,
`assembly_acceptance_receipt.json`). Sınır tek cümle: released paketin ve
DNP/rotasyon parity'sinin sahibi burasıdır; **as-built konformans ve release
kararı** oranın. FAI skill'i CPL'i düzeltirse tek-otorite kuralı ihlal edilir
([[../_shared/rule-conflicts.md]] §9).

## 5. Doğrulama

```bash
# ÖNCE: son DRC + parity (bayraklar zorunlu)
kicad-cli pcb drc --schematic-parity --format json --severity-all \
  --exit-code-violations -o drc.json routed.kicad_pcb

# SONRA: fab paketi
kicad-cli pcb export gerbers -o gerbers/ routed.kicad_pcb
kicad-cli pcb export drill   -o gerbers/ routed.kicad_pcb
kicad-cli pcb export pos     -o cpl.csv  routed.kicad_pcb

python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: `rotation_map.json`'dan bir polar sınıfın ofsetini değiştir →
`check_orientation()` **FAIL vermeli**. BOM'dan bir DNP işaretini sil →
DNP parity **FAIL vermeli**.

## 6. Kabul kriterleri

- [ ] Birleşik gate (4b) GREEN ve üç zorunlu child receipt güncel board hash'inde;
      CPL 4b'den önce koşmadı.
- [ ] Gerber üretiminden önce **son DRC + son netlist parity** koşuldu
      (`--schematic-parity --severity-all --exit-code-violations`).
- [ ] Pick-point **gövde/courtyard centroid**; footprint origin kullanılmadı.
- [ ] Fab rotasyon-map'i **versiyonlu** ve `side` bazlı; bottom-side X mirror +
      rotasyon işareti kontrol edildi.
- [ ] Sıfır-yön düzeltmesi yalnız polar sınıflara, versioned whitelist ve
      `expected_pin1_or_cathode_side` ile uygulandı; tüm paketlere uygulanmadı.
- [ ] Populated BOM refdes kümesi = CPL refdes kümesi; DNP yok; X/Y board
      bounding box içinde; side ∈ {top, bottom}; rotation sayısal.
- [ ] **DNP listesi BOM ve CPL'de aynı anda** işaretli (parity kontrolü koştu).
- [ ] F.Paste katmanı gerber setinde; EPAD'lerde tam açıklık kalmadı
      (windowpane %50–75).
- [ ] Panel varsa ≥3 global fiducial (+BGA local), ≥5 mm rail; de-panel stresi
      hassas parçadan uzak ve panel şeması Faz 3'e geri beslendi.
- [ ] `msl_level` montaj paketine iliştirildi.
- [ ] İlk üretimde kutuplu/BGA/QFN için yön görseli + first-article kontrolü
      istendi; evidence ID + board revizyonu + onaylayan kaydedildi.
- [ ] Board tek-sahipli, KiCad kapalı.
- [ ] Vendor eklentisi zorunlu kılınmadı; kabul kapısı export aracının adı değil
      **CPL içeriği, board/commit hash'i ve fabın kabul ettiği paket formatı**.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../dfm/SKILL|dfm]] · [[../bom-lifecycle/SKILL|bom-lifecycle]] ·
[[../dft-testpoints/SKILL|dft-testpoints]] · [[../konnektor/SKILL|konnektor]] ·
[[../sematik-erc/SKILL|sematik-erc]] · [[../pcb-orchestrator/SKILL|orchestrator]] ·
[[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]] (Faz 5A tüketicisi)

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`production-contracts`** — CPL/montaj: pick point, fab rotasyon haritası, oryantasyon, panelizasyon.
- **`cad-assets`** — İndirilen sembol/footprint, pin sayısı datasheet'e karşı **kanıtlanana kadar** güvenilmez.
- **`jlc-library`** — LCSC parça numarasından KiCad sembol/footprint/3B modeli (JLC2KiCadLib köprüsü).
