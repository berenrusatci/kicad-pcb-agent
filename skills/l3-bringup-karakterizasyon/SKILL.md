---
name: l3-bringup-karakterizasyon
description: Fiziksel bring-up ve karakterizasyon döngüsü (L3). Kabul edilmiş ilk üründe alınan ölçüm kayıtlarını board hash + kalibrasyon + kuruluma bağlar, matris D satırlarına iliştirir ve fiziksel kanıt yeterliliği kapısını kapatır. Limit tanımlamaz; limit girdi olarak otoritesiyle gelir.
version: 1.0.0
last-reviewed: 2026-08-10
phase: "Faz 5B — sürüm sonrası fiziksel L3 döngüsü (Faz 4c ve D2 ilk-ürün kabulünden sonra)"
assurance_loop: [L2, L3]
evidence_origin: [PHYSICAL_MEASUREMENT, TOOL_REPORT, DESIGN_ARTIFACT, AUTHORITATIVE_SOURCE]
signoff: false
inputs:
  - {artifact: first_article_report.json, schema: null, required: true}
  - {artifact: l3_rule_universe.json, schema: ../dogrulama-matrisi/references/l3_rule_universe.schema.json, required: true}
  - {artifact: measurement_records.json, schema: references/measurement_record.schema.json, required: true}
  - {artifact: bringup_checklist.md, schema: null, required: true}
  - {artifact: testpoint_plan.json, schema: ../dft-testpoints/references/testpoint_plan.schema.json, required: true}
  - {artifact: dft_coverage_receipt.json, schema: null, required: true}
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: stackup.json, schema: null, required: true}
  - {artifact: impedance_rules.json, schema: null, required: true}
  - {artifact: emc_plan.md, schema: null, required: true}
  - {artifact: esd_test_profile.json, schema: ../../tools/schemas/esd_test_profile.schema.json, required: false}
  - {artifact: length_report.json, schema: ../highspeed-routing/references/length_report.schema.json, required: false}
outputs:
  - {artifact: physical_measurement_manifest.json, schema: ../../tools/schemas/physical_measurement_manifest.schema.json}
  - {artifact: l3_gate_receipt.json, schema: ../../tools/schemas/l3_gate_receipt.schema.json}
  - {artifact: l3_feedback_actions.json, schema: null}
  - {artifact: raw/, schema: null}
writes_board: false
tools: [physical-measurement, correlate, coupon-plan, boot-observability, shunt-plan, skew-timing, sparam, gate-receipt, assert-matrix]
---

# SKILL: L3 Bring-up ve Karakterizasyon (Fiziksel Kanıt Kapısı)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

Bu paketin **ilk `PHYSICAL_MEASUREMENT` üreticisi**. Ölçüm (2026-08-10): on
skill frontmatter'ında `L3` listeliyordu ve **18/18'i** `TOOL_REPORT`
yayımlıyordu — yani D satırlarını herkes tanıyordu, kimse sahiplenmiyordu.
Ayrımın kuralı: [[../_shared/coverage-gate.md]]
§"`assurance_loop` sahiplik taşımaz — participates-in ≠ produces-evidence-for".
Bu skill o tablodaki **produces-evidence-for L3** örneğidir.

**Sahiplendiği tek soru:** *Bu D satırı için, bu kabul edilmiş birim üzerinde
geçerli fiziksel kanıt var mı, ve sonuç limitin hangi tarafında?*
Limitin kendisi bu skill'e ait **değildir**.

## 0. Ne zaman çağrılır

- **Faz 5B.** Faz 4c (`cpl-assembly`) çıktı verdikten **ve**
  [[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]] (D2) birimi kabul
  ettikten sonra. FAI GREEN değilse elektriksel hata avı **açılmaz**:
  montajı kabul edilmemiş bir birimde bulunan arıza tasarıma yazılır
  ([[../_shared/rule-conflicts.md]] §9). Araç bu durumda `RED` verir,
  `CONFIRM` değil.
- Board yazılmaz (`writes_board: false`), board-write kilidi alınmaz. Kart
  yalnızca **referans ve hash kaynağıdır**.
- Kampanya kendi içinde **SERİdir** ve sırası güvenlik gerekçesidir:
  `power_safe` (güvenli enerjilendirme) → `functional` (gözlemlenebilirlik) →
  `characterisation` (ısıl / SI / EMC). Aynı aşama içindeki domain ölçümleri
  ekipman izin veriyorsa paralelleştirilebilir. Rayları kanıtlanmamış bir
  kartta fonksiyonel düğüm dürtmek, elde olan tek birimi öldürmenin yoludur;
  araç sıra ihlalini `CAMPAIGN_ORDER_VIOLATION` ile yazar.
- `RED`'de board'a **yama yapılmaz**: ilgili üreticiye L2 kenarı açılır
  (`l3_feedback_actions.json`), sürüm bloklanır.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| kabul edilmiş birim (rev/hash/seri/lot) | `first_article_report.json` (D2) | bağlanacak birim yok → araç koşmaz (exit 2) |
| birimin kabul verdict'i | `first_article_report.json` | kabul yoksa **`RED`** (önkoşul kırık) |
| D satırı evreni + **limit + otorite** | [[../dogrulama-matrisi/SKILL\|dogrulama-matrisi]] | limit yoksa satır **`NO_COVERAGE`** — asla uydurulmaz |
| satırın sahibi (`owner`) | kural evreni | sahipsiz kural `NO_COVERAGE`: `RED`'in geri besleyeceği yer yok |
| ölçüm kayıtları | lab / cihaz → [`references/measurement_record.schema.json`](references/measurement_record.schema.json) | kayıt yoksa `eligible>0 ∧ scanned=0` → `NO_COVERAGE` |
| prob/fixture erişimi planı | `testpoint_plan.json` + `dft_coverage_receipt.json` ([[../dft-testpoints/SKILL\|dft-testpoints]]) | ölçülemeyen düğüm `NO_COVERAGE`; **bu skill TP eklemez** |
| bring-up sırası | `bringup_checklist.md` ([[../dft-testpoints/SKILL\|dft-testpoints]]) | kampanya sırası doğrulanamaz |
| ray ağacı / shunt planı | `rail_tree.json` + `shunt_plan` | ray akımı `NO_COVERAGE` |
| stackup + empedans kuralı | `stackup.json`, `impedance_rules.json` ([[../empedans-stackup/SKILL\|empedans-stackup]]) | TDR satırı limitsiz → `NO_COVERAGE` |
| EMC/ESD profili | `emc_plan.md`, `esd_test_profile.json` ([[../emc-esd/SKILL\|emc-esd]]) | profil yoksa `SCREEN_ONLY`/`CONFIRM` |
| ham veri (Touchstone/TDR/termal/scope) | cihaz dosyası + `raw_data_sha256` | digest tutmuyorsa kayıt ile yakalama **aynı ölçüm değildir** |

Çıktılar: [`physical_measurement_manifest.json`](../../tools/schemas/physical_measurement_manifest.schema.json)
· [`l3_gate_receipt.json`](../../tools/schemas/l3_gate_receipt.schema.json)
· `l3_feedback_actions.json` · `raw/` (ham veri referansları).

Birim bağı `first-article-report/v1`'in `identity` (`board_revision`,
`board_hash`, `lot`, `unit_serial`, `package_binding`) ve `gate`
(`verdict`, `serial_release`) bloklarından okunur. `gate.serial_release=false`
alan adı tanınmasa bile **veto**dur: o kararın sahibi FAI kapısıdır, burada
yeniden tartışılmaz. `identity.board_hash` **null ise kampanya başlamaz**
(exit 2) — digest'siz bir birim, tasarıma bağlanamayan bir birimdir.

> **Açık soru / varsayım (2026-08-10, kapatılmadı).**
> **Ürün seviyesindeki L3 kabul profilleri nerede yaşıyor?** EMC standardı ve
> severity'si, ısıl yük noktası, TDR toleransı, ray sıralama ve fonksiyonel
> kabul kriterleri için bu repoda kanonik bir artefakt **yok**. Alınan
> muhafazakâr yol: **limitler girdidir** ve kendi otorite/provenance bağlarını
> (`owner_skill` + `document_id` + `locator`) taşıyarak gelir. Limiti gelmemiş
> bir D satırı `NO_COVERAGE`'dır — asla `GREEN`. Kanonik bir kabul-profili
> artefaktı kararlaştırılana kadar bu skill'in `GREEN`'i **yalnızca otorite
> bağıyla limiti gelmiş satırları** kapsar. Uydurulmuş bir `GREEN` yolu
> açılmadı ve açılmayacak; `physical_measurement.py` içinde limit üreten tek
> bir dal yoktur ve otoritesi bu skill'i gösteren bir limit
> `SELF_AUTHORED_LIMIT` ile reddedilir.

## 2. Adımlar

1. **Birimi bağla.** `first_article_report.json`'dan `board_revision`,
   `board_hash`, `serial`, `lot` ve kabul verdict'ini oku. Dördü birden yoksa
   kampanya **hiçbir kartla ilgili değildir**; araç exit 2 ile durur. Kabul
   yoksa `RED`.
2. **Kural evrenini yükle.** Her satır `rule_id` + `owner` + `limit`
   (`units`, `min`/`max`, `authority`) taşımalı. Eksik olan satır
   `NO_COVERAGE` işaretlenir; **eksik limit yerine varsayılan konmaz**.
3. **`power_safe` aşaması.** Akım sınırlı besleme, `shunt-plan` penceresi,
   `boot-observability` ile hangi ray arızasının hangi belirtiyi ürettiği.
   Ray/enable sırası ölçülür; PG **ray ölçümü değildir**
   ([[../dft-testpoints/SKILL|dft-testpoints]], `boot-observability`).
4. **`functional` aşaması.** Reset/strap/debug erişimi, arayüz enumerasyonu,
   `bringup_checklist.md` sırası. Ayırt edilemeyen arıza sınıfı kaldıysa satır
   `NO_COVERAGE`.
5. **`characterisation` aşaması.** Gerçek yükte ısıl, kupon/TDR empedansı,
   kanal S-parametresi, EMC ön-uyum / ESD. Her ölçüm kaydı §3(a)'daki metadata
   setini taşır.
6. **Kayıtları iliştir ve yargıla.** `physical-measurement` her kaydı sırayla
   süzer: köken → birim bağı → kalibrasyon → tekrarlanabilir kurulum →
   (TDR/VNA ise) S-parametre metadata → ham veri digest'i → birim uyumu →
   uyum sınıfı → **aralık karşılaştırması**.
7. **Korelasyon (varsa).** Ölçülen Touchstone ↔ simüle Touchstone:
   `correlate` (de-embed **beyan edilir**, çıkarsanmaz). Kupon geometrisi ve
   hız girdisi: `coupon-plan`, `skew-timing`, `sparam`.
8. **Kapıyı yaz.** `l3_gate_receipt.json` + `physical_measurement_manifest.json`.
   `RED`/`STALE`/`INVALID_*` satırların her biri için `l3_feedback_actions.json`
   içine satırın **beyan edilmiş sahibini** adlandıran bir L2 kenarı açılır.
9. **Matrise bağla.** `assert-matrix` ile D satırlarının kapanışını doğrula;
   `gate-receipt` ile üst kapıya iliştir.

## 3. Nüanslar — bilinen tuzaklar

### (a) Kalibrasyonsuz okuma, ölçüm değildir
`calibration_id` **ve** `calibration_date` yoksa sonuç "biraz daha kötü bir
sayı" değil, `NO_COVERAGE`'dır. Aynı şey yazılmamış kurulum için de geçerli:
`method` / `setup` / `environment` / `load_state` eksikse ölçüm
tekrarlanamaz, tekrarlanamayan ölçüm kanıt değildir
([[../emc-esd/SKILL|emc-esd]] §8 bunu ön-uyum için söylüyor; burada her satır
için geçerli).

### (b) TDR/VNA kanıtı: kural yeni değil, **üreticisi** yeni
`reference_plane`, `measurement_zone`, `port_map`, `reference_impedance`,
`fixture`, `deembed_method` zorunludur; eksikse `NO_COVERAGE` veya
`INVALID_SPARAM`. Bu kuralın kanonik metni
[[../highspeed-routing/SKILL|highspeed-routing]] §6'dadır (satır 349-351) —
burada **yeniden tanımlanmaz**, uygulanır. De-embed beyanı çıkarsanmaz; bunu
`sim/correlate.py` zaten böyle yapıyor.

### (c) Marj, nokta tahmini değil **aralıktır**
Karşılaştırma `değer ± (uncertainty + guard_band)` aralığıyla yapılır.
Tamamen içeride `GREEN`, tamamen dışarıda `RED`, sınıra değiyorsa
`INCONCLUSIVE`. [[../highspeed-routing/SKILL|highspeed-routing]] §6:
"marj `uncertainty + guard_band` içinde kalıyorsa **PASS verme**".
`uncertainty: 0` meşru bir değerdir ama **yazılmalıdır**; yokluk sıfır değildir.

### (d) Ön-uyum asla `GREEN`'e yükseltilmez
Yakın alan probu sayısı laboratuvar limitiyle **karşılaştırılamaz**; anlamlı
olan aynı kurulumdaki baseline farkıdır ([[../emc-esd/SKILL|emc-esd]] §8).
`compliance_class: pre_compliance` kaydı formal limitle karşılaştırılmaz,
`SCREEN_ONLY` verir ve zorunlu satırı **kapatmaz**. `baseline_id` yoksa
`NO_COVERAGE` — baseline'sız mutlak sayı bir tarama bile değildir.
Akredite uyum yazılımda kapanmaz: sınıf beyan etmek belgeyi elde tutmakla
aynı şey değildir → `lab_artifact` yoksa `CONFIRM`.

### (e) Başka bir kartın ölçümü bu kart hakkında **zayıf kanıt değil**, kanıt değildir
`board_revision`/`board_hash`/`serial`/`lot` dördü birden kabul edilmiş
birimle eşleşmezse `STALE`. Aynı disiplin `board/skew_timing.py`'da da var:
"`board_sha256` ebeveyn raporun `design.sha256`'sından kopyalanır; taşımayan
ebeveyn reddedilir".

### (f) Ham veri digest'i kaydın parçasıdır
`raw_data_sha256` yoksa/uyuşmuyorsa kayıt ile yakalama **aynı ölçüm
değildir** → `INVALID_METRIC`. `--raw-root` verilmediyse doğrulama
yapılmadığı `RAW_DATA_NOT_VERIFIED` ile açıkça yazılır; sessizce geçilmez.

### (g) Kapı verdict'i ile domain state'i karıştırma
Kampanya aşaması (`POWER_SAFE` / `FUNCTIONAL` / `CHARACTERISATION` /
`CAMPAIGN_COMPLETE`) `domain_state` alanında yayımlanır, `gate.verdict`
alanında **değil**. Kapalı verdict sözlüğü yalnız kapı kararı içindir ve
çakışmada bağlayıcı olan `gate.verdict`'tir:
[[../_shared/coverage-gate.md]] §"Verdict isim alanı — `gate.verdict` ≠
domain/workflow state".

### (h) Bu skill limit yazarsa tek-sahiplik kuralı çöker
Kendi ölçtüğü değeri kendi yazdığı eşikle karşılaştıran bir kapı bir
totolojidir. Otoritesi `l3-bringup-karakterizasyon` olan bir limit
`SELF_AUTHORED_LIMIT` ile reddedilir. Sahiplik tablosu:
[[../_shared/rule-conflicts.md]] §9 — *yukarı akış limitin sahibi, aşağı akış
kanıtın sahibidir*.

### (i) Araç hiçbir cihazla konuşmaz
`physical-measurement` ölçüm **almaz**. Kendi ürettiği bir sayı, tezgâhın
zaten cevapladığı sorunun ikinci cevabı olurdu ve raporda ikisi ayırt
edilemezdi. Cihazın dosyası kanıttır; araç onun etrafındaki zincirdir.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Akredite lab uyumu (radyasyonlu/iletilen emisyon) | D | Doğru test profili/lab artefaktı olmadan yazılımda kapanmaz → `NO_COVERAGE`/`CONFIRM` |
| EMC ön-uyum (yakın alan) | D | Yalnız `SCREEN_ONLY`; resmî sertifikasyon **değildir** |
| Ürün seviyesi kabul profili (severity, yük noktası, TDR toleransı) | — | Kanonik artefakt yok → §1 açık soru; limitsiz satır `NO_COVERAGE` |
| Tekrarlanamayan kurulum | — | `NO_COVERAGE` veya `INCONCLUSIVE` |
| Limitin kendisinin doğruluğu | — | Limit sahibi domain skill'idir; bu skill yalnız hangi tarafta olduğunu söyler |
| Numune yeterliliği / istatistiksel dağılım | D | Tek birimin ölçümü partiyi temsil etmez; lot kaydı taşınır, çıkarım yapılmaz |
| Ölçülen değerin kök nedeni | — | `RED` bir gözlemdir; nedeni upstream üreticinin L2 turudur |
| Board'un düzeltilmesi | — | `writes_board: false`; yalnız L2 kenarı açılır |

## 5. Doğrulama

```bash
# aracın kendi iç tutarlılığı (cihaz gerektirmez)
python3 tools/scripts/lab/physical_measurement.py --self-test

# gerçek kampanya
python3 tools/scripts/lab/physical_measurement.py \
  --accepted-unit build/first_article_report.json \
  --rules       build/l3_rule_universe.json \
  --records     build/measurement_records.json \
  --raw-root    build/raw \
  --manifest    build/physical_measurement_manifest.json \
  --receipt     build/l3_gate_receipt.json \
  --feedback    build/l3_feedback_actions.json \
  --json        build/physical_measurement_manifest.json

# ölçülen ↔ simüle edilen Touchstone (de-embed BEYAN edilir)
python3 tools/scripts/sim/correlate.py --measured raw/coupon.s2p \
  --simulated build/coupon_sim.s2p --band-start-hz 1e9 --band-stop-hz 1e10 \
  --de-embedded --json build/correlation.json

# matris + üst kapı
python3 tools/scripts/assert_matrix.py --json build/assert.json
python3 tools/scripts/gate_receipt.py build/l3_gate_receipt.json

# validator (fault-injection dahil)
python3 -m unittest -v tools.tests.test_physical_measurement
```

Fault-injection: manifest'te `GREEN` olan bir satırın ölçülen değerini
limitin dışına taşı → verdict **`RED` olmalı** ve `l3_feedback_actions.json`
o satırın **sahibini** adlandıran bir L2 kenarı içermeli. Kayıttan
`calibration_id`'yi sil → `NO_COVERAGE` olmalı, `GREEN` kalmamalı.
Kural evreninden `limit`'i sil → `NO_COVERAGE` olmalı; hiçbir eşik
türetilmemeli.

## 6. Kabul kriterleri

- [ ] D2 ilk-ürün kabulü (`first_article_report.json`) **GREEN** ve kampanya
      onun `board_revision`/`board_hash`/`serial`/`lot`'una bağlandı.
- [ ] Her ölçüm kaydı `evidence_origin: PHYSICAL_MEASUREMENT` taşıyor;
      `TOOL_REPORT` bir kaydı fiziksel yapmadı.
- [ ] Her kayıt cihaz üçlüsü (`make`/`model`/`serial`) + `calibration_id` +
      `calibration_date` taşıyor; kalibrasyonsuz kayıt `NO_COVERAGE` sayıldı.
- [ ] Her kayıt `method` / `setup` / `environment` / `load_state` ile
      **tekrarlanabilir**; yazılmamış kurulum kanıt sayılmadı.
- [ ] TDR/VNA kanıtında `reference_plane`, `measurement_zone`, `port_map`,
      `reference_impedance`, `fixture`, `deembed_method` tam; eksikse
      `INVALID_SPARAM` verildi ([[../highspeed-routing/SKILL|highspeed-routing]] §6).
- [ ] Her sayısal satır `value` + `units` + `uncertainty` + `guard_band`
      taşıyor ve karşılaştırma **aralıkla** yapıldı; sınıra değen sonuç
      `INCONCLUSIVE` yazıldı, `PASS` değil.
- [ ] Her limit **girdi** olarak geldi ve `owner_skill` + `document_id` +
      `locator` otorite bağı taşıyor; bu skill hiçbir eşik tanımlamadı.
- [ ] Otoritesi bu skill olan limit `SELF_AUTHORED_LIMIT` ile reddedildi.
- [ ] Limiti gelmemiş zorunlu satır `NO_COVERAGE`; hiçbir yerde uydurulmuş
      eşik yok.
- [ ] Kaydı olmayan zorunlu satır `NO_COVERAGE`; `eligible>0 ∧ scanned=0`
      asla `GREEN` olmadı.
- [ ] `board_hash` kabul edilen birimle eşleşmeyen kayıt `STALE`.
- [ ] `raw_data_sha256` beyan edildi; `--raw-root` verildiyse dosyayla
      doğrulandı, verilmediyse `RAW_DATA_NOT_VERIFIED` raporlandı.
- [ ] Ön-uyum kaydı `SCREEN_ONLY`; resmî sertifikasyon olarak etiketlenmedi ve
      `GREEN`'e yükseltilmedi ([[../emc-esd/SKILL|emc-esd]] §9).
- [ ] Akredite satır için `lab_artifact` yoksa `CONFIRM` verildi.
- [ ] Kampanya sırası (`power_safe` → `functional` → `characterisation`)
      korundu; ihlal `CAMPAIGN_ORDER_VIOLATION` ile yazıldı.
- [ ] `RED`/`STALE`/`INVALID_*` her satır için `l3_feedback_actions.json`'da
      **sahibini adlandıran** bir L2 kenarı var; board'a yama yapılmadı
      (`writes_board: false`).
- [ ] `gate.verdict` kapalı sözlükten; kampanya aşaması `domain_state`'e
      yazıldı ([[../_shared/coverage-gate.md]] §"Verdict isim alanı").
- [ ] `signoff: false` — `GREEN` kanıt-yeterliliği kapanışıdır, akreditasyon
      değildir.
- [ ] coverage: `scanned + excluded == eligible`, `violations <= scanned`,
      `scanned > 0`.

## İlgili

[[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../dft-testpoints/SKILL|dft-testpoints]] ·
[[../guc-isil/SKILL|guc-isil]] ·
[[../empedans-stackup/SKILL|empedans-stackup]] ·
[[../emc-esd/SKILL|emc-esd]] ·
[[../highspeed-routing/SKILL|highspeed-routing]] ·
[[../cpl-assembly/SKILL|cpl-assembly]] ·
[[../pcb-orchestrator/SKILL|orchestrator]] ·
[[../_shared/rule-conflicts.md]] §9

## Araç kancaları (2026-08-10)

- **`physical-measurement`** — Kampanyanın kapısı. Ölçüm kaydını board hash +
  kalibrasyon + kurulum + kapsama bağlar, bayat/geçersiz kanıtı reddeder ve
  matris `rule_id`'sine iliştirir. **Cihazla konuşmaz, ölçüm almaz.**
- **`boot-observability`** — `power_safe` aşamasından ÖNCE: hangi ray
  arızasının ayırt edilebilir bir belirti ürettiğini planlar. Plan üretir,
  ölçüm değil.
- **`shunt-plan`** — Ray akımı ölçüm penceresi (droop bütçesi ↔ metre tabanı).
  Ölçümün nasıl alınacağını söyler; akımı tahmin etmez.
- **`coupon-plan`** — TDR/empedans kuponunun geometrisi ve ölçüm bölgesi;
  `characterisation` aşamasının girdisi.
- **`correlate`** — Ölçülen Touchstone ↔ simüle Touchstone artığı. De-embed
  **beyan edilir**; beyansız artık fixture içerir.
- **`sparam`** — Touchstone okuma/karma-mod dönüşümü; port eşlemesi
  **açıkça** verilir.
- **`skew-timing`** — mm → ps dönüşümü; hız girdisi (`--er-eff` +
  `--er-eff-source` veya `--solve`) olmadan koşmaz. Ölçülen kupon er_eff'i
  buraya provenance olarak girer.
- **`gate-receipt`** — L3 receipt'ini üst kapıya iliştirir.
- **`assert-matrix`** — D satırlarının gerçekten kapandığını (yalnız
  "çalıştı" değil) satır bazında doğrular.
