---
name: highspeed-routing
description: Yüksek hızlı diferansiyel routing — pad'den çıkış (escape) geometrisi, pin-arası kanal ve maske barajı, 45° köşe, UI'dan türetilmiş skew bütçesi ve ps cinsinden length-match, kuplaj/paralellik ölçümü, referans düzlemi sürekliliği.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 3 — Seri (placement sonrası; board tek sahipli)"
assurance_loop: [L0, L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: impedance_rules.json, schema: ../empedans-stackup/references/stackup.schema.json, required: true}
  - {artifact: stackup.json, schema: ../empedans-stackup/references/stackup.schema.json, required: true}
  - {artifact: fab_capabilities.json, schema: ../../tools/schemas/fab_capabilities.schema.json, required: false}
  - {artifact: placed board, schema: null, required: true}
  - {artifact: phy_contract.json, schema: references/phy_contract.schema.json, required: true}
outputs:
  - {artifact: routed board, schema: null}
  - {artifact: length_report.json, schema: references/length_report.schema.json}
  - {artifact: coupling_report.json, schema: references/length_report.schema.json}
  - {artifact: reference_report.json, schema: references/length_report.schema.json}
  - {artifact: escape_report.json, schema: references/length_report.schema.json}
writes_board: true
tools: [via-stub, measure-board, skew-timing, pair-coupling, kicad-cli, sparam, eye, openems-bridge, correlate, ai-feedback-loop, reference-plane, model-registry, parasitics]
---

# SKILL: Yüksek Hız Routing (Escape + Length-Match)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Metrik adları: [[../_shared/glossary.md]] §1, §2 ·
Kural çatışmaları: [[../_shared/rule-conflicts.md]] §3, §4, §7

> **Bu skill iki eski skill'in birleşimidir** (`highspeed-length-match` +
> `pcb-highspeed-escape`). Ayrık oldukları sürece skew formülü, 90° köşe kuralı,
> teardrop ödünü ve kuplaj metriği ikisinde birden yazılıydı ve kuplaj hedefi
> çelişiyordu. Hattın **pad'den çıktığı ilk 1–3 mm**'si ile **gövdesi** aynı
> fiziktir ve aynı bütçeyi paylaşır.

## 0. Ne zaman çağrılır

- Faz 3'te, **placement bariyeri kapandıktan sonra**. Board tek sahiplidir;
  yazım seridir.
- Her yüksek hızlı çift için: önce escape (§2), sonra gövde (§3).
- Paralel segment planlıyorsan veya iz inceltmek istiyorsan geometriyi yazmadan
  **ÖNCE** [[../crosstalk-thinning/SKILL|crosstalk-thinning]] çağrılır.
- Katman geçişi, referans değişimi veya fab min ihlali çıkarsa
  [[../empedans-stackup/SKILL|empedans-stackup]]'a geri sinyal verilir.

## 1. Girdi/çıktı sözleşmesi

### Adım 0 — bütçeyi mm'den değil **UI**'dan türet (`phy_contract`)

Skew bütçesi bir sabit değil, link parametresidir. Route etmeden önce her link için
[`references/phy_contract.schema.json`](references/phy_contract.schema.json):

```
phy_contract: { D-PHY revizyonu, lane_rate_Gbps, UI_ps = 1000/lane_rate_Gbps,
                intra_skew_ps, inter_skew_ps, kaynak_doküman }
```

- Uç-cihaz (OV9281 ↔ soft-D-PHY, hardened-D-PHY ↔ AM5706) limiti **yoksa
  routing'i sign-off etme.** Bu kartta iki ayrı bütçe var, tek bütçe değil.
- Ön-tahmin olarak (sadece yön bulmak için): lane-to-lane/clock skew `≤0.1 UI`,
  çift-içi için `UI/50`. 2.5 Gb/s'te UI=400 ps → sırasıyla ~40 ps ve ~8 ps.
  **Bunlar DRC limiti değildir** — uç-cihaz dokümanlarına doğrulanmadan
  `CONFIRM` kalır. Başka bir ailenin (ör. CX3) mm cinsinden kuralını buraya taşıma.
- `UI/50` geometrik bir tahmindir ve **mode-conversion (Sdc/Scd) şartının yerine
  geçmez**; ikisi ayrı kapıdır.

| Alan | Kaynak | Yoksa |
|---|---|---|
| `intra_skew_ps`, `inter_skew_ps` | uç-cihaz dokümanı | sign-off **yok**, `CONFIRM` |
| `rise_time_ps` | sürücü datasheet'i (**min**, tipik değil) | `CONFIRM` |
| W/S + net-class | `impedance_rules.json` | routing açılmaz |
| `min_bridge_mm` | `fab_capabilities.json` | yerel karar bandı ([[../_shared/rule-conflicts.md]] §5) |
| pad geometrisi (kanal) | footprint / paket datasheet | `NO_COVERAGE` |

## 2. Escape — hattın pad'den çıktığı ilk 1–3 mm

Gerçek kartlarda hataların çoğu burada doğar ve **DRC'yi geçtiği için sessiz kalır**.

### (a) Pin-arası kanaldan geçen iz → maske barajını hesapla, iz genişliğini ONA göre seç

> **Bu bölüm maske barajı hesabının kanonik sahibidir.**
> [[../dfm/SKILL|dfm]] buraya bağ verir.

ESD dizileri, USB switch'ler, konnektörler: güç/GND pini çoğu zaman iki veri
pininin **tam ortasındadır** (ör. USBLC6-2SC6: 1=I/O1, 2=GND, 3=I/O2 | 6=I/O1,
5=VBUS, 4=I/O2). O pine ulaşmak için iz zorunlu olarak pad sütunları arasındaki
kanaldan geçer.

```
kanal        = pad_sütun_aralığı − pad_uzunluğu      # SOT-23-6: 2.274 − 1.32 = 0.954 mm
boşluk       = (kanal − iz_genişliği) / 2
maske_barajı = boşluk − 2 × mask_expansion           # tipik expansion 0.05 mm
```

| iz | bakır boşluk | **maske barajı** | verdict | IPC-2221 akım (35 µm, ΔT=10 °C) |
|---|---|---|---|---|
| 0.40 mm | 0.277 | **0.177 mm** | **RED** (<0.20) | 1.23 A |
| 0.30 mm | 0.327 | 0.227 mm | `CONFIRM` (0.20–0.25) | 1.00 A |
| **0.25 mm** | 0.352 | **0.252 mm** | `SCREEN_ONLY` PASS (≥0.25) | 0.88 A |

- **Kural:** maske barajı fab minimumunun altına düşerse maske basılmaz → iki pin
  arasında **lehim köprüsü** → 5 V veri hattına biner, host portu yanar.
  DRC "clearance" bunu YAKALAMAZ; ayrı bir `solder_mask_bridge` kontrolü ve
  yukarıdaki hesap gerekir. Karar bandı: [[../_shared/rule-conflicts.md]] §5.
  Fab profili varsa profil değeri yerel bandı **ezer**.
- İz genişliğini akım değil **maske barajı** belirler; sonra IPC-2221 ile
  akımın hâlâ yettiğini doğrula (yetmiyorsa iz değil **paket/footprint** değişir).
- Kanal izini kanalın **tam ortasına** koy (simetrik boşluk).
- Alternatifler: kısa pad'li footprint (kanalı genişletir), farklı pinout'lu
  parça, ya da 4 katman + güç düzlemine via (2 katmanda via kanala sığmaz).

### (b) Çift, pad sütununa girmeden AÇILMALI

Ara pin (GND/VBUS) pad **boyu**, çift adımından büyükse çift olduğu gibi
giremez — pad'e değer (short).

```
if pad_boyu >= çift_adımı:
    çift, x_pad_sütunu − Δ noktasında ±(pin_aralığı/2)'ye 45° ile açılır
```
Örnek: SOT-23-6 pad boyu 0.60 mm, çift adımı 0.62 mm → çift pad sütunundan
**1.4 mm önce** ±0.95 mm'ye açılır. Açılmazsa DRC "shorting_items" verir
(yakalanır) — ama açılma çok geç yapılırsa maske barajı ihlali kalır (yakalanmaz).

### (c) Referans düzlemi: alt katman izlerini çift koridorunun DIŞINDA tut

2 katmanlı kartta her alt katman izi düzlemde yarıktır. Kaçınılmaz olanlar
(USB-C flip pad'leri A6-B6/A7-B7 X kesişmesi; konnektörün iki yanındaki VBUS
pad gruplarının birleştirilmesi) **konnektör pad alanının altına** alınır.
Kabul kriteri: çiftin iletim hattı bölümünün altındaki B.Cu **kesintisiz**;
ölçerek kanıtla (tüm B.Cu iz koordinatları koridorun dışında mı?).

### (d) Koplanar GND dolgusu: clearance ≥ ~1.25 × iz genişliği

F.Cu GND dolgusu dikiş via'larına üst katman bağlantısı verir (yoksa
`via_dangling`) ve koridoru kalkanlar; ama dolgu çifte çok yaklaşırsa her iz
**birbirine değil düzleme** kuple olur → diferansiyel empedans ve gürültü
bağışıklığı düşer. Dolgu local clearance'ını ayrı ayarla (ör. iz 0.4 → dolgu 0.5).

## 3. Gövde — routing ve length-match

1. Diff çiftleri kuralına göre route et (kuplajlı router; kaba bölge için
   autorouter alt-ajan).
2. `check_trace_lengths(board, pairs, tol)` → P/N skew. Uzunluk **elektriksel
   (gecikme)**; via z-katkısı + yay dahil.
   - Önce **intra-pair** (çift-içi, ~ps sıkı), sonra **inter-pair**
     (grup, D-PHY bütçesi).
   - Karışık microstrip/stripline veya anlamlı via gecikmesi varsa zaman-alanı
     tuning zorunlu: KiCad 10 **tuning profile** katman başına width/gap/unit
     delay ve via layer-pair delay tanımlar → `stackup.json`'dan üret. Bağımsız
     `measure.py` yine **nihai kapıdır**, profil değil.
     `CONFIRM: tuning profile'ın dosya/API ile güvenli üretim yolu.`
3. **Önce ps'e çevir, sonra karar ver:**
   ```
   v = c / √εeff          (FR4 mikroşerit εeff ≈ 3.2 → ≈ 167.6 mm/ns)
   skew_ps = skew_mm / v × 1000
   ```
   | skew | ps | USB 2.0 HS bütçesi ~100 ps |
   |---|---|---|
   | 1.47 mm | 8.8 ps | %9 → **telafi gereksiz** |
   | 0.15 mm | 0.9 ps | %1 |

   Yukarıdaki **167.6 mm/ns yalnızca büyüklük sezgisi içindir; karar sayısı
   değildir** (tek bir tipik FR4 mikroşerit değeri; gerçek hız stackup ve
   frekansa bağlı). Rapora girecek ps değerini elle çarpma —
   **`skew-timing`** (`tools/scripts/board/skew_timing.py`) `measure-board`
   raporunun mm kanıtını `length-report/v1` olarak ps'e çevirir ve dönüşüm
   girdisini kaynağıyla birlikte yazar:

   ```
   python3 tools/scripts/board/skew_timing.py measure_board.json \
       --solve --structure coupled-microstrip \
       --w-mm 0.09 --s-mm 0.15 --t-mm 0.035 --h-mm 0.10 --er 4.3 \
       --budget-ps 100 --budget-source "USB 2.0 spec 7.1.x, rev 2.0" \
       --json length_report.json
   ```

   `--solve` yerine `--er-eff X --er-eff-source "..."` de kabul edilir; ikisi
   de yoksa araç **çevirmez** (exit 2). Bütçe kaynağıyla verilmediyse metrik
   `INCONCLUSIVE` yazılır — ölçüldü ama yargılanmadı, ve bu bir PASS değildir.
   Bakırı olmayan çift `NO_COVERAGE`'dır; `measure-board`'un o çift için
   yazdığı `skew_mm = 0.0` "mükemmel eşleşme" değil, **route yok** demektir.

   Fark **bütçenin altındaysa MEANDER EKLEME** — meander çifti ayırır, kuplajı ve
   empedansı bozar, ekstra köşe getirir. Konnektör pad ofseti (ör. USB-C A6/A7
   arası 0.85 mm) + çift adımı doğal ve kaçınılmaz bir skew üretir; önce ps'e
   çevir, sonra karar ver.
4. Gerekiyorsa kısa bacağa `add_meander(delta)` → aralık ≥ 3·W (self-coupling
   bozmasın), **45°/yay köşe**, hattın kritik olmayan bölümünde.
   - Sıra önemli: **önce** doğal/asimetrik dönüşleri azalt, tuning'i skew'in
     *doğduğu katmanda ve kaynağına yakın* yap. Serpantin son çaredir.
   - `3·W` bir **başlangıç noktasıdır, kabul kriteri değil**. Serpantin
     eklediysen amplitüd, komşu bacak aralığı, H, W/S ve eklenen gecikmeyi
     raporla; komşu bacaklar arası self-coupling oluşmadığını ve lokal
     Zdiff/mode-conversion'ı simülasyonla göster. Evrensel bir `3W`/`3H`/`4H`
     sayısı üretici dokümanlarında yok.
5. **90° köşe yok.** Köşede efektif iz genişliği ~√2 kat artar → yerel empedans
   düşüşü; ayrıca dar açı asit tuzağıdır. **45° pah veya yay** kullan.
   Doğrulama: her net için ardışık segment çiftlerinin dönüş açısını ölç,
   **85°–95° arası sıfır olmalı**.
6. `parallelism_coverage` ölçümü — **kendi geometrik ölçümünle**, araç konsol
   etiketine güvenmeden. Tanım ve zorunlu alanlar: §4.
7. **`check_reference_plane_continuity()`** (zorunlu, routing sonrası): hiçbir
   yüksek hızlı hat referans düzlemi yarığı/void üzerinden geçmemeli (empedans
   fırlar + EMI). Katman geçişinde return-stitch şart: GND→GND yakın stitching
   via. GND→PWR geçişi ve `100 nF` kuralı:
   [[../empedans-stackup/SKILL|empedans-stackup]] §Referans düzlemi (kanonik sahip).
   → `reference_report.json`. Length-match temiz olsa bile bu ihlal SI'yı bozar.

## 4. `parallelism_coverage`: tanımsız metrik = doğrulanamaz metrik

> **Bu bölüm metriğin kanonik sahibidir.** Ad ayrımı:
> [[../_shared/glossary.md]] §2.

"%75 kuple" tek başına anlamsızdır. Raporlarken **daima** şunları yaz:
tolerans (kaç mm'ye kadar "paralel" sayılıyor), örnekleme adımı, ve hangi
bölüm (segment) ölçüldü. Yöntem: bir bacağı `step` (ör. 0.05 mm) örneklere böl,
her örneğin orta noktasından **karşı bacağın tüm segmentlerine dik mesafe**
hesapla, min ≤ tol ise o örnek paralel sayılır.

Tolerans seçimi çift adımına bağlanmalı (adım + ~%10–15 pay). usb-hs-breakout
0.62 mm adımda, tol 0.70 mm: J1→U1 **%57.6**, U1→J2 **%74.5**, toplam **%64.1**.
Aynı kartta tol 1.00 mm toplamı **%88.6**'ya çıkarıyor — yani toleransı
yazmadan verilen yüzde, istenen sonucu üretecek şekilde seçilebilir.

- Bu metrik **elektromanyetik kuplaj değildir**. %100 paralellik ne sıkı
  kuplajın ne de doğru empedansın kanıtıdır — Zdiff/Zse kapısı ayrıdır.
- MIPI'de **LP modu tek-uçludur**: çift bilinçli olarak *loosely coupled*
  tasarlanabilir; W/S'yi paralellik yüzdesini büyütmek için değil field solver +
  LP/HS gereksinimiyle seç.
- `parallelism_coverage` hiçbir zaman `impedance_pass` üretmez; field-solver/lab
  kanıtı yoksa sonuç en fazla `GEOMETRY_PRECHECK_PASS`.

> ⚠️ **KARAR GEREKLİ — proje `parallelism_coverage` hedefi (C2).**
> Konsolidasyon öncesi bir tarafta sabit `≥ %95` hedefi, diğer tarafta aynı
> kartın her toleransta bu hedefin altında kaldığı ölçümü vardı. Sabit hedef
> **kaldırıldı** (güvenli okuma): metrik artık kapı değil, raporlanan bir
> ön-elektir. Projeye özgü bir paralellik hedefi istenip istenmediği ve
> hangi toleransta tanımlanacağı kullanıcı kararıdır.
> Ayrıntı: [[../../docs/CONSOLIDATION-NOTES.md]] §Open decisions.

## 5. Nüanslar — bilinen tuzaklar

### (a) Via stub — via'nın *kullanılmayan* kısmı
Through-via ile üst katmandan iç katmana geçtiğinde barelin alt kalanı bir
**stub** olur ve çeyrek-dalga rezonatörü gibi davranır; rezonans bandın içine
düşerse hat orada ölür. Uzunluk ölçen hiçbir metrik bunu göstermez.
- Kaba eşik (Bogatin): `stub[inch] < 0.3 / veri_hızı[Gb/s]` — 10 Gb/s'te ~0.76 mm;
  FR4'te rezonans `f[GHz] ≈ 1.5 / L[inch]`. **Bunlar eleme heuristiğidir,
  sign-off kriteri değil.**
- Sign-off yolu: her geçiş için kalan stub boyunu **ölç**, via çevresinin
  etkin Dk'sıyla çeyrek-dalga rezonansını hesapla ve rezonansın kanalın gerekli
  elektriksel bant genişliğinin **>2×** üstünde kaldığını simülasyonla göster.
  Karar veri hızına değil **kanal bant genişliğine** bağlıdır → bu hesapta
  **`f_nyquist`** kullanılır, `f_knee_edge` değil ([[../_shared/glossary.md]] §1).
- Çözüm sırası: (1) katman geçişini **en dipteki** katman çiftine al (stub kısalır),
  (2) blind/buried via, (3) backdrill — kalan stub değerini fab'ın **garanti
  ettiği** residual-stub rakamından al, ezberden değil.
- Bu karttaki hızlar (USB2 HS, MIPI D-PHY ≤2.5 Gb/s) stub'ı genelde zararsız
  kılar, ama **otomatik muafiyet yazma**: kararı ölç, sayıyla yaz, sessizce geçme.

### (b) Cam dokuma skew: geometriyle ölçülemez
Çift-içi skew'in bir kısmı bakır uzunluğundan değil malzemeden gelir.
Length-match `0 ps` iken gerçek skew sıfır olmayabilir. Routing adımı bunu
düzeltemez; ≳5 Gb/s + uzun çiftte açılı yönlendirme/malzeme kararı stackup'a
geri sinyaldir. Kanonik sahip: [[../empedans-stackup/SKILL|empedans-stackup]] §3e.

### (c) Teardrop yüksek hızda ödün — ve pcbnew'de üretilemez
Via girişinde empedansı düşürür; çiftlerde varsayılan **kapalı**. Nerede
gerekli/nerede zararlı: [[../dfm/SKILL|dfm]] §Teardrop (kanonik sahip).

**API sınırı (bu skill'in sahip olduğu kısım):** `dir(pcbnew)` içinde teardrop
ile ilgili hiçbir isim yok. `BOARD` yalnız `BulkRemoveStaleTeardrops` /
`LegacyTeardrops` (silme/okuma), `ZONE` yalnız `Get/SetTeardrop*`
**parametreleri** (geometri üretmez), `BOARD_DESIGN_SETTINGS.m_TeardropParamsList`
opak `SwigPyObject`. → **Açık madde olarak yaz**, "eklendi" deme. İnsan adımı:
KiCad GUI → *Tools → Generate Teardrops*. Bir düzeltmeyi API desteklemiyorsa
fonksiyon `API_YOK: <sebep>` döndürsün; sessizce başarılı görünmesin.

Üretilemese de **doğrulanabilir**: teardrop'lar ZONE'dur →
`sum(z.IsTeardropArea() for z in board.Zones())`. İddiayı bu sayıyla kapat;
sayı 0 ise raporda **AÇIK** yaz.

KiCad 10.x SWIG ile geometri üretirken abstract `BoardShape` instantiate etme;
somut `BoardSegment` veya `BoardRectangle` kullan. SWIG yolu yalnız 10.x için
geçerlidir; başka major sürümde `CONFIRM` bırak.
`evidence_origin=AUTHORITATIVE_SOURCE+TOOL_REPORT`, `assurance_loop=L0`,
`status=CONFIRM` (major sürüm 10.x değilse).

### (d) AC kuplaj kapasitörü bir süreksizliktir
Seri kapasitör pad'i çiftte lokal empedans çukuru yaratır. Kural: pad'i küçük
tut (0402 → 0201 tercih), pad **altındaki referans düzlemini kesme**, iki
kapasitör **simetrik ve yan yana** (biri önde biri arkada olmasın — mode
conversion), stub bırakma.

### (e) Diferansiyel via geçişi ölçülebilir bir geometridir
"Yakında stitching via var" bir kabul kriteri değil. Her diferansiyel geçiş için
raporla: P/N via boyu, drill/pad/**antipad**, çift adımı, en yakın iki dönüş
via'sının uzaklığı ve **P/N'ye göre simetrisi**. Hedef **GSSG** benzeri simetrik
geometri; kritik geçişte 3B field-solver/S-parametre doğrulaması.
MIPI'yi mümkünse baştan sona **GND referanslı** tut.

### (f) Netclass pattern'ini gerçek net adlarıyla TEST ET
Netclass tanımlamak, net'in o sınıfa **atandığı anlamına gelmez**. Pattern
gerçek net adına uymuyorsa sınıf sessizce boş kalır ve kart Default'la
yönlendirilir. usb-hs-breakout'ta `USB_D+`/`USB_D-` pattern'leri, gerçek adlar
`/USB_C_D_P`, `/USB_C_D_N`, `/USB_D_P`, `/USB_D_N` iken **hiçbir nete**
uymuyordu; 0.40 mm genişlik netclass'tan değil script sabitinden geliyordu.

Kapı: `fnmatch` ile pattern↔net eşleşmesini yaz ve **iki yönlü** doğrula —
(1) hedef netlerin tamamı kapsandı mı, (2) güç/CC netleri **yanlışlıkla**
kapsanmadı mı. Ayrıca `board.FindNet(n).GetNetClassName()` ile karttan da oku.

### (g) Konnektör/FPC şeffaf tel değildir
Launch/pad/temas geometrisi, FPC kalınlığı ve GND-contact dağılımı kanal
bütçesine girer; veri yoksa `CONFIRM`. Flip-pad/PTH konnektör geometrisinin
zorladığı layer crossover'ı routing detayı sayma; placement/connector seçimi
kapısına geri besle. Main/launch/pad bölgelerini **ayrı ölç** ve her birinin
metric semantiğini receipt'e yaz ([[../konnektor/SKILL|konnektor]]).

### (h) DF40-sınıfı yüksek-yoğunluklu konnektör escape'i (hub↔kamera B2B)
Bu projenin hub↔kamera kartı arayüzü 0.4mm pitch'li DF40-sınıfı bir B2B
konnektör kullanıyor — `common/escape_geom.py::HighDensityConnectorEscape`/
`pair_to_pair_min_distance_mm`/`build_escape_plan` referans board'dan
(CM4 IO Board TRD escape'i) ÖLÇÜLMÜŞ ilk-düz-segment/commit-mesafesi/
max-trim-segment değerlerini taşır — UYDURULMADI. Tek 45°/135° diyagonalin
bir engelle kesişip kesişmediğini (`via_pair_gerekli_mi` → via-pair/İn2.Cu
tünel gerekip gerekmediği) `via_pair_required()` Liang-Barsky testiyle
kontrol eder; `True` dönerse ÖNCE hedef komponentin taşınabilirliği
değerlendirilmeli, otomatik via-pair kararı VERİLMEZ.

## 6. Ölçüm disiplini

Bu skill'in ürettiği her sayısal SI metriği:
`limit`, `measured`, `units`, `uncertainty`, `guard_band`, `calibration_id` ve
tool/instrument hash'i taşır. Marj `uncertainty + guard_band` içinde kalıyorsa
**`PASS` verme**.

Örneklemeli geometri metriği `sampling_step_mm`, `phase_offset`,
`min_feature_mm` ve `convergence_delta` taşır. Aynı ölçümü step ve step/2 ile
koş; verdict değişirse veya delta toleransın dörtte birini aşarsa
`INCONCLUSIVE`. Arc için `true-arc` kernel yoksa chord sonucu kanonik değildir;
yalnız `approximation=true` ve `max_chord_error_mm` ile ön bilgi olabilir.
Arc-aware ve review-compatibility chord metriklerini aynı kimlikte
**birleştirme**; semantik uyuşmazlığında `INVALID_METRIC` üret.

TDR/VNA/S-parametre kanıtında reference plane, measurement zone, calibration,
fixture/de-embed yöntemi, port map ve reference impedance zorunludur; eksikse
`NO_COVERAGE` veya `INVALID_SPARAM`.

**pcbnew ölçüm tuzakları** (arc'ı atlayan `GetClass()`, ipek metnini içeren
`GetBoundingBox()`, yalan söyleyen `ZONE.GetLayerName()`, yakınlık yarıçapı):
[[../dogrulama-matrisi/references/measurement-error-classes.md]] — kanonik sahip.

## 7. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek empedans | D | Fab coupon + TDR; `parallelism_coverage` bunu üretmez |
| Cam dokuma skew'i | C | Malzeme kaynaklı; geometri göstermez |
| Kanal S-parametreleri | D | Routed kanal ölçümü / 3B solver |
| Via stub (bu repoda) | `NO_COVERAGE` | Katman çıkarımı dar + kartta stackup yok — aşağı bak |
| Mode conversion (Sdc/Scd) | C→D | Simülasyon veya ölçüm |
| FEXT | — | [[../crosstalk-thinning/SKILL|crosstalk-thinning]] modellemez; mikroşeritte `CONFIRM` |

> **AÇIK MADDE — `via_stub.py` bu repodaki hiçbir kartta henüz ölçüm yapamıyor.**
> İki ayrı sebep, ikisi de ölçüldü:
> 1. **Katman çıkarımı dar:** via merkezine ≥2 katmanda iz ucu değmesi gerekiyor.
>    RGMII kartında 231 via'nın **hiçbiri** bu koşulu sağlamıyor (izler pad/zone
>    üzerinden bağlanıyor) → `ölçülebilen=0, kapsam dışı=14`.
> 2. **Ana kartta stackup yok:** AM5706'da 53 hızlı via adayı var ama
>    `(setup (stackup ...))` kalınlık verisi ne `.kicad_pcb`'de ne `.kicad_pro`'da
>    mevcut → `NO_COVERAGE`.
>
> Kapatmak için ikisi de gerekli: (1) pad/zone bağlantısını da sayan katman
> çıkarımı, (2) karta fiziksel stackup yazılması **veya** araca `stackup.json`
> girdisi. Kapanana kadar bu satır A değil `NO_COVERAGE` üretir — ve
> `scanned=0` **PASS değildir**.

## 8. Doğrulama

```bash
# via stub taraması (kapsam yoksa PASS vermez)
python3 tools/scripts/board/via_stub.py board.kicad_pcb --json via_stub.json

# receipt doğrulaması — birleşik kapıya vermeden ÖNCE
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: bir çiftin bir bacağına 90° köşe ekle → açı taraması
**FAIL vermeli**. Bir netclass pattern'ini bozarak hiçbir nete uymaz hale getir
→ iki yönlü kapsama kontrolü **FAIL vermeli**.

## 9. Kabul kriterleri

### Escape
- [ ] Pin-arası kanal izlerinde maske barajı karar bandına uyuyor
      (`< 0.20 mm` yok); hesap **ve** `solder_mask_bridge` = 0.
- [ ] İz genişliği maske barajından türetildi, akımdan değil; IPC-2221 ile
      akımın yettiği ayrıca doğrulandı.
- [ ] Çift, pad sütununa girmeden 45° ile açıldı; `shorting_items` = 0.
- [ ] Çift koridorunun altında referans düzlemi kesintisiz (koordinatla kanıt).
- [ ] Koplanar GND dolgusu clearance'ı ≥ ~1.25 × iz genişliği; `via_dangling` yok.

### Gövde
- [ ] `phy_contract.json` her link için dolu ve `kaynak_doküman` var; limitsiz
      link sign-off edilmedi.
- [ ] Skew **ps cinsinden** bütçenin altında; bütçe altındaki farka meander
      **eklenmedi**; eklenen her meander gerekçelendirildi.
- [ ] Uzunluk/skew ölçümü **yayları içeriyor** (`PCB_ARC` + `GetLength()`).
- [ ] Yüksek hız netlerinde 90°±5 köşe sayısı = **0**.
- [ ] `parallelism_coverage` **toleransı ve örnekleme adımıyla birlikte**
      raporlandı; `impedance_pass` iddiası yok.
- [ ] `check_reference_plane_continuity()` koştu; hiçbir hızlı hat yarık/void
      üzerinden geçmiyor.
- [ ] P ve N aynı katmanda; via'da simetrik geçiş + GND return via var.
- [ ] Diff çiftte via sayısı = 0 (aksi belgelenmeli); her diferansiyel geçiş
      için P/N via boyu, antipad, dönüş via mesafesi ve simetrisi raporlandı.
- [ ] Netclass↔net eşleşmesi **iki yönlü** test edildi (kapsam + yanlış kapsam).
- [ ] Yüksek hızlı hatta TP stub'ı yok ([[../_shared/rule-conflicts.md]] §7).
- [ ] Yüksek-hız çiftlerinde teardrop bilinçli **kapalı** (veya simetrik +
      gerekçeli); teardrop eklendiyse DRC + maske barajı **yeniden** koşuldu;
      teardrop sayısı `IsTeardropArea()` ile ölçüldü.
- [ ] Uygulanamayan her düzeltme **açık madde** olarak yazıldı, "yapıldı" diye
      geçilmedi (`API_YOK: <sebep>`).
- [ ] DRC 0 ihlal, 0 unconnected, 0 schematic-parity —
      **`--schematic-parity --severity-all` bayraklarıyla**
      ([[../sematik-erc/SKILL|sematik-erc]] §4 kanonik uyarı).
- [ ] Her SI metriği `uncertainty` + `guard_band` taşıyor; marj bunların içinde
      kalıyorsa PASS verilmedi.
- [ ] Örneklemeli metrikler step ve step/2 ile koşuldu; verdict değişmedi.
- [ ] Board tek-sahipli, seri yazım; **kendi ölçümün otoritedir** — alt-ajan
      self-report'una güvenilmedi.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../empedans-stackup/SKILL|empedans-stackup]] ·
[[../crosstalk-thinning/SKILL|crosstalk-thinning]] · [[../dfm/SKILL|dfm]] ·
[[../emc-esd/SKILL|emc-esd]] · [[../konnektor/SKILL|konnektor]] ·
[[../dft-testpoints/SKILL|dft-testpoints]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`sparam`** — Touchstone yazma, karma-mod dönüşüm, kanal kaskadı; kanal bütçesi çıkarılırken.
- **`eye`** — İstatistiksel göz diyagramı; skew ve kayıp bütçesi kapandıktan sonra.
- **`openems-bridge`** — Diferansiyel çift geometri çıkarımı + Touchstone okuma. **FDTD motoru yok** — girdi hazırlar, çözmez.
- **`correlate`** — Ölçülen Touchstone'u simüle edilene karşı korele eder — paketteki modelin **yanlış** olduğunu gösterebilen ilk sayı.
- **`ai-feedback-loop`** — Simülatörün diferansiyel çift verdict'i hedefi tutmadıysa geometri düzeltmesi hesaplar.
- **`reference-plane`** — İz segmentinin referans düzlem sürekliliği; split/kesik düzlem dönüş yolunu kırar.
- **`model-registry`** — **Her simülasyondan önce:** kullanılan satıcı modelini adlandırır ve karakterizasyon zarfı dışında sayı döndürmeyi reddeder.
- **`parasitics`** — Yönlendirme bittikten sonra: routed geometriden R/L/C çıkarır ve SPICE `.subckt` olarak `ngspice`'a geri besler. **Via endüktansı yok, segmentler arası bağlaşım yok, DC direnç** — layout'un nerede acıttığını daraltır, acıtmadığını kanıtlamaz.
