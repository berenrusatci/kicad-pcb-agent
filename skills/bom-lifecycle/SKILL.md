---
name: bom-lifecycle
description: Nexar/Octopart GraphQL ile stok + fiyat + lifecycle taraması; NRND/EOL veya risk eşiğinde pin-compatible alternatif üretimi ve provenance zinciri. Ağ erişimi ana ajanda kalır.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 1 — Temeller (paralel); geri besleme kenarı: 6 -> 1/2"
assurance_loop: [L1, L2]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: bom.json, schema: null, required: true}
  - {artifact: thermal_report.json, schema: ../guc-isil/references/thermal_report.schema.json, required: false}
outputs:
  - {artifact: lifecycle_report.json, schema: references/lifecycle_report.schema.json}
  - {artifact: alternates.json, schema: references/lifecycle_report.schema.json}
writes_board: false
tools: [bom-lifecycle, jlc-library, cad-assets]
---

# SKILL: BOM & Lifecycle

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

## 0. Ne zaman çağrılır

- Faz 1'de, güç ve mekanik düğümleriyle **paralel**; `bom.json` Faz 0a
  baseline'ından gelir.
- Her faz sınırında **tazelik** kontrolü için yeniden koşar (`supply_checked_at`).
- Bir alternatif kabul edildiğinde bu skill doğrudan tek adıma yama yapmaz:
  `parts.json`/`bom.json` baseline'ı yeni revizyonla değişir ve orkestratör
  etkilenen descendant'ları yeniden açar
  ([[../pcb-orchestrator/SKILL|orchestrator]] §Lifecycle feedback).

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| `refdes`, `MPN` | `bom.json` | satır taranamaz → `excluded` + gerekçe |
| lifecycle / stok / fiyat / offers | Nexar (Octopart) GraphQL | `TBD`/`CONFIRM`, **hayali MPN yok** |
| `msl_level`, `packaging`, `moq` | distribütör + üretici | `CONFIRM`; montaj kapısı açılmaz |
| sıcaklık sınıfı | datasheet | `thermal_report.json` ile çapraz doğrulanamazsa `CONFIRM` |
| DC-bias eğrisi | üretici | `TBD` → [[../guc-isil/SKILL|guc-isil]] §3b |
| `pcn_policy_source`, `last_time_buy`, `last_ship` | üretici PCN/PDN | PDN/EOL varken yoksa **`RED`** |

Çıktı: [`references/lifecycle_report.schema.json`](references/lifecycle_report.schema.json)

## 2. Adımlar

1. Her satır: `validate_bom_lifecycle()` → Nexar (Octopart) GraphQL: lifecycle,
   stok, fiyat, offers.
2. Risk skoru: lifecycle (Active=0 / NRND=0.6 / EOL=1) + stok + single-source +
   lead-time.
3. `NRND`/`EOL` **veya** risk>0.5 → `find_pin_compatible()`: **paket + pinout +
   elektriksel param** eşleşmeli (aynı paket ≠ aynı pinout!).
4. Alternatif footprint değiştiriyorsa → adım 1/2'ye feedback aç.
5. Kritik parçada ≥2 kaynak tut.
6. Alternatif sözleşmesinde pin işlevi/sırası, land pattern, abs-max/çalışma
   aralığı, güç kaybı ve termal koşulu ayrı alanlarla karşılaştır. Aynı paket
   adı veya "pin-compatible" pazarlama metni tek başına kabul değildir.
   Üretici açık pin-to-pin uyumu göstermiyorsa; land pattern/maske-pasta notu,
   thermal-pad/ısı yolu, junction limiti, MSL veya paketleme biçimi değişiyorsa
   otomatik eşdeğerlik verme, feedback/validasyon kapısı aç.
7. Her adayın kanıt zincirini provenance alanlarıyla bağla: `source_url`,
   `manufacturer_document_id`, `datasheet_revision`, `package_code`,
   `package_document_revision`, `land_pattern_source`, `land_pattern_revision`,
   `density_level_or_mfg_recommended`, `zero_rotation_checked`. Kaynak veya
   revizyon bilinmiyorsa üretime `PASS` verme; `CONFIRM` bırak. Land pattern/pad
   geometrisi değişmişse `RED` ve footprint/DFM geri beslemesi aç.
8. Lifecycle kaydında `pcn_policy_source`, `pcn_id_or_none`, `pdn_id_or_none`,
   `last_time_buy` ve `last_ship` tut. PDN/EOL varken son sipariş/sevkiyat
   tarihleri yoksa `RED`; PCN fit/form/function, paket, finish veya termal yolu
   etkiliyorsa alternatifi yeniden doğrula.

## 3. Nüanslar — "stokta var" ≠ monte edilebilir

Lifecycle/stok temiz görünen bir satır hâlâ montajı durdurabilir.

### (a) MSL (nem hassasiyeti) + floor life
> **Bu bölüm MSL kuralının kanonik sahibidir.**
> [[../cpl-assembly/SKILL|cpl-assembly]] buraya bağ verir.

BGA/QFN/plastik paketler MSL 3–5 olabilir; kuru paket açıldıktan sonra sınırlı
saat içinde reflow edilmeli, aşılırsa **bake** gerekir (popcorn çatlağı).
Prototipte "birkaç ay çekmecede bekleyen BGA" doğrudan hurdadır.
→ `msl_level` alanını yaz ve **montaj paketine iliştir**; BGA/QFN'de floor life
aşımı = bake veya hurda.

### (b) Paketleme biçimi
Cut-tape / reel / tray / bulk. Pick&place makinesi **bulk ve kesik banda**
basmaz; MOQ genelde paketleme birimine bağlıdır (tek parça fiyatı ucuz ama
minimum 1 reel = 4000 adet olabilir). → `packaging`, `moq`,
gerçek maliyet = `moq × birim`.

### (c) Sıcaklık sınıfı ve kalite seviyesi
Commercial (0–70 °C) parça 85 °C ortam hesabıyla çelişir; AEC-Q / industrial
gerekiyorsa MPN soneki değişir ve **stok bambaşkadır**. Sıcaklık aralığını
`thermal_report.json`'daki ortamla **çapraz doğrula**
([[../guc-isil/SKILL|guc-isil]] §3e).

### (d) Kapasitör/direnç satırlarında "eşdeğer" yoktur
Aynı değer + kılıf farklı dielektrik (X7R↔Y5V), farklı gerilim, farklı DC-bias
eğrisi demek olabilir. Dielektrik + `V_rated` + kılıf üçünü birden eşle.
Gerekçe ve derating kuralı: [[../guc-isil/SKILL|guc-isil]] §3b.

### (e) Layout kararları BOM'a kısıt olarak iner
**Soft-termination gerekliliği** (kart kenarı / depanel yakını MLCC'ler) bir BOM
kısıtıdır: standart MLCC ~3 mm bükülmede çatlar, soft-term ~5–6 mm dayanır.
Yani yerleşim kararı BOM'u değiştirir. Fiziği ve yerleşim kuralı:
[[../dfm/SKILL|dfm]] §MLCC flex crack.

### (f) DNP satırları
DNP hem BOM'da hem CPL'de işaretli olmalıdır; sadece birinde işaretliyse
montajcı yine takar. Kuralın sahibi ve parity kontrolü:
[[../cpl-assembly/SKILL|cpl-assembly]] §DNP parity.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek stok (anlık) | A ama **bayat** | Stok anlıktır → safety margin + LTB stratejisi |
| MSL / floor life uyumu | C | Distribütör + montajcı kaydı; board dosyasında yok |
| Pin-to-pin gerçek eşdeğerlik | C | Üretici açık beyanı olmadan kanıtlanamaz |
| Land pattern uyumu | C→A | Footprint üretiminde ölçülür, burada değil |
| PCN'in fit/form/function etkisi | C | Üretici belgesi |

## 5. Doğrulama

```bash
# lifecycle_report.json + alternates.json şema/receipt doğrulaması
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: bir satırın `datasheet_revision` alanını sil → alternatif
`CONFIRM`'e düşmeli, `PASS` kalmamalı. Kalıyorsa provenance kapısı boştur.

Ağ erişimi olmayan sandbox'ta koşarsan **sessizce uydurur**: API dönmezse
`TBD`/`CONFIRM` yaz, asla MPN üretme.

## 6. Kabul kriterleri

- [ ] Ağ çağrısı **ana ajanda** koştu; API dönmediğinde satır `TBD`/`CONFIRM`,
      hayali MPN yok.
- [ ] `supply_checked_at` tazeliği: üretim quote'u öncesi ≤7 gün, tasarım
      review'unda ≤30 gün; aşarsa `STALE`/`RED`.
- [ ] Her `NRND`/`EOL`/risk>0.5 satırı için alternatif arandı ve sonucu yazıldı.
- [ ] Her alternatif adayında pin işlevi/sırası, land pattern, abs-max, güç kaybı
      ve termal koşul **ayrı alanlarla** karşılaştırıldı; pazarlama metni kanıt sayılmadı.
- [ ] Her aday provenance zincirini taşıyor (`source_url`,
      `manufacturer_document_id`, `datasheet_revision`, `package_code`,
      `land_pattern_source`, `land_pattern_revision`, `zero_rotation_checked`).
- [ ] PDN/EOL satırlarında `last_time_buy` + `last_ship` var; yoksa `RED`.
- [ ] Kritik parçalarda ≥2 kaynak var.
- [ ] `msl_level`, `packaging`, `moq` kritik satırlarda dolu; gerçek maliyet
      `moq × birim` olarak yazıldı.
- [ ] Sıcaklık sınıfı `thermal_report.json` ortamıyla çapraz doğrulandı.
- [ ] Land pattern/pad geometrisi değişen alternatifler `RED` ve DFM feedback açtı.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## Kurallar (kısa)

- Provider arayüzünü **soyut tut**; ücretli SiliconExpert veya tek distributor
  bağımlılığını zorunlu yapma.
- "Equivalent" marketing'e güvenme; pinout'u datasheet'ten doğrula.
- JLCPCB Basic/Extended sınıfı assembly maliyeti + stok riskini değiştirir.
- Her parça için ağır bir PLM akışı kurma. Üretici belge/revizyonu ve varsa
  PCN/PDN kimliği bu skill için yeterli minimumdur.

## İlgili

[[../guc-isil/SKILL|guc-isil]] §3b · [[../cpl-assembly/SKILL|cpl-assembly]] ·
[[../dfm/SKILL|dfm]] · [[../konnektor/SKILL|konnektor]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`jlc-library`** — LCSC parça numarasından KiCad sembol/footprint/3B modeli (JLC2KiCadLib köprüsü).
- **`cad-assets`** — İndirilen sembol/footprint, pin sayısı datasheet'e karşı **kanıtlanana kadar** güvenilmez.
