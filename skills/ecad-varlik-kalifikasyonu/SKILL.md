---
name: ecad-varlik-kalifikasyonu
description: Seçilmiş MPN/paket için aday KiCad sembol+footprint çiftinin, otoriter pin/paket tanımına pin-pin, pad-pad ve paket-varyantı düzeyinde uyup uymadığına karar veren tek otorite. Sayı eşitliği kimlik eşitliği değildir; kapı kayıt (registration) ÖNCESİ kapanır.
version: 1.0.0
last-reviewed: 2026-08-10
phase: "Faz 1 — Temeller; BOM MPN/paket adayını seçtikten SONRA, sematik-erc o varlığı ilk kullanmadan ÖNCE zorunlu kapı"
assurance_loop: [L1, L2]
evidence_origin: [AUTHORITATIVE_SOURCE, DESIGN_ARTIFACT, TOOL_REPORT]
signoff: false
inputs:
  - {artifact: parts.json, schema: null, required: false}
  - {artifact: lifecycle_report.json, schema: ../bom-lifecycle/references/lifecycle_report.schema.json, required: true}
  - {artifact: pin_package_source.json, schema: references/pin_package_source.schema.json, required: true}
  - {artifact: "*.kicad_sym", schema: null, required: true}
  - {artifact: "*.kicad_mod", schema: null, required: true}
outputs:
  - {artifact: cad_asset_qualification.json, schema: ../../tools/schemas/cad_asset_qualification.schema.json}
  - {artifact: cad_asset_gate_receipt.json, schema: ../../tools/schemas/gate_receipt.schema.json}
writes_board: false
tools: [cad-asset-qualification, cad-assets, bom-lifecycle, jlc-library]
---

# SKILL: ECAD Varlık Kalifikasyonu (Sembol + Footprint Serbest Bırakma Kapısı)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

Bu skill **tek bir soruya** cevap verir: eldeki `.kicad_sym` + `.kicad_mod` çifti,
seçilmiş MPN/paket için üreticinin belgelediği **cihazı ve paketi** temsil ediyor mu?
Parçayı **seçmez**, net **bağlamaz**, üretilebilirlik **kararı vermez**.

> **Boşluk G03.** `tools/scripts/production/cad_assets.py` kendi başlığında şunu
> yazıyor: bir varyanttan gelen sembol ile başka varyanttan gelen footprint
> "ERC ve DRC'yi temiz geçer ve ancak ilk kart lehimlendiğinde bulunur". Ölçüldü
> (2026-08-10): o dosyanın kapısı (`cad_assets.py:177-190`) **iki sayım
> karşılaştırması** yapıyor — sembol pin **sayısı** == datasheet pin sayısı ve
> footprint pad **sayısı** == sembol pin sayısı. Sayı eşitliği kimlik eşitliği
> değildir. Bu skill o kapının **üstündeki semantik katmandır**, yerine geçen
> değil: aracı `check_pin_count`'u içeriden çağırır.

## 0. Ne zaman çağrılır

- **Faz 1**, [[../bom-lifecycle/SKILL|bom-lifecycle]] bir MPN/paket adayı
  seçtikten **sonra**; [[../sematik-erc/SKILL|sematik-erc]] o varlığı ilk kez
  kullanmadan **önce** — zorunlu kapı.
- Kütüphaneye **kayıt (registration) öncesi**. Kayıt yalnız araç üzerinden
  yapılır; sembol/footprint dosyasını elle yamamak bu skill'in önerdiği bir iş
  değildir.
- **L2:** alternatif MPN kabul edildiğinde ([[../bom-lifecycle/SKILL|bom-lifecycle]]
  §4) yeniden koşar. Yeni MPN = yeni otoriter kaynak = yeni kalifikasyon.
- `writes_board: false` — **board kilidi almaz**, şematik/board açmaz. Aynı anda
  board yazan bir skill ile serileştirilmesi gerekmez.
- Kaynak veya varlık hash'i kalifikasyondan sonra değişirse çıktı **bayatlar**
  (`STALE`) ve yeniden koşulur.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| `mpn`, `package_code` | `pin_package_source.json` (BOM'un seçtiği MPN) | `CONFIRM` — kimliksiz varlık serbest bırakılmaz |
| pin tablosu (`number`+`name`+`function`+`electrical_role`) | üretici belgesi, **elle/vendor tarafından yazılmış** kayıt | tablo boşsa `NO_COVERAGE` (`eligible>0, scanned=0`) — kitlesel `RED` değil |
| pad tablosu (`number`+`type`) | aynı kayıt | aynı |
| exposed pad (`present`/`pad_number`/`symbol_pin_number`) | paket tablosu | `present: true` iken footprint'te yoksa **`RED`** |
| land pattern (`source_type`, `pad_span_*`, `tolerance_mm`) | **öncelik: üretici land pattern'i** | üretici yoksa `LOCAL_POLICY`/`SCREEN_ONLY`; hiç yoksa `CONFIRM` |
| kaynak provenansı | `document_id`+`revision`+`locator`+`accessed`+`snapshot_sha256` | biri bile eksikse en fazla `CONFIRM`, asla `GREEN` |
| aday `.kicad_sym` / `.kicad_mod` | [[../bom-lifecycle/SKILL\|bom-lifecycle]] / `jlc-library` / `cad-assets` | okunamıyorsa `NO_COVERAGE` |
| lifecycle durumu | `lifecycle_report.json` (**yalnız okunur**) | `CONFIRM`; engelli durumda **`RED`** |

Girdi sözleşmesi: [`references/pin_package_source.schema.json`](references/pin_package_source.schema.json)
Çıktı sözleşmesi: [`../../tools/schemas/cad_asset_qualification.schema.json`](../../tools/schemas/cad_asset_qualification.schema.json)

> **Açık soru / varsayım (2026-08-10).**
> (a) **ECAD kalifikasyonu için kanonik, repo genelinde geçerli otoriter
> pin/paket kaynak artefaktının hangisi olduğu HENÜZ KARARLAŞTIRILMADI.**
> `references/pin_package_source.schema.json` bu kararı vermiyor ve kendini o
> artefakt ilan etmiyor; **açıkça beyan edilmiş, insan veya vendor tarafından
> yazılmış** bir kayıt sözleşmesidir. Arkasında PDF ayrıştırıcı, OCR veya
> sezgisel tablo kazıyıcı **yoktur** — bir datasheet-çıkarım formatı uydurup
> onun çıktısını ground truth saymak, bu paketin karşı kurulduğu uydurma-kaynak
> hatasının ta kendisidir.
> (b) **Kanonik artefakt seçilene kadar**, provenansı tam olmayan bir kaynakla
> koşan hiçbir çalışma `CONFIRM`'ün ötesine geçemez; `GREEN` yolu yoktur.
> (c) `--source-snapshot` verilmediğinde `provenance.snapshot_sha256` **beyan
> edilmiş ama doğrulanmamıştır** (`source.snapshot.verified = null`) ve rapor
> bunu böyle yazar. Bunu tek başına bir cap saymamak **bilinçli bir seçimdir**,
> kanıtlanmış bir karar değil: snapshot belgesi elde varsa verin, uyuşmazlık
> `STALE` üretir.
> (d) `expected_footprint_name` kontrolü **etiketi** doğrular, geometriyi değil.
> Bir dosyaya istenen ad verilebilir. Ölçülen kanıt `pad_span_*`'tir; ikisinin
> **çelişmesi** sinyaldir, uyuşması tek başına delil değildir.
> (e) `package_dimensions` (body/pitch) **kaydedilir, doğrulanmaz.** Body
> konturu fab/silk katmanı çizim konvansiyonudur, pad merkezlerinden pitch
> türetmek satır yapısını tahmin etmektir. Doğrulanmamış bir sayıyı ölçülmüş
> gibi yayımlamak yasaktır.
> (f) **Repoda İKİ gate-receipt sözleşmesi var ve ÇELİŞİYORLAR** (ölçüldü,
> 2026-08-10). `skills/pcb-orchestrator/references/gate_receipt.schema.json`
> `metric_semantics.<id>.metric_definition` istiyor ve
> `additionalProperties: false`; `tools/scripts/gate_receipt.py`
> (`SEMANTIC_FIELDS`, satır 56) ise `definition` **ve** `sampling_step` şart
> koşuyor. İkisi tek belgede sağlanamaz. Bu skill **doğrulayıcıyı** izliyor
> (`tools/schemas/gate_receipt.schema.json` kendi açıklamasında "the Python
> validator remains authoritative" diyor ve `promote.py` ile
> `tools/fixtures/gate_receipt/usb_pair_positive.json` zaten o biçimi
> yayımlıyor). **Hangisinin kanonik olduğu bu skill'in kararı değildir**;
> çelişki burada kaydedilir, üstü örtülmez.
> (g) Doğrulayıcının `GREEN_WORDS`/`RED_WORDS` kümelerinde `CONFIRM`,
> `LOCAL_POLICY` ve `SCREEN_ONLY` **yok**; `effective_status` onları `RED`'e
> düşürür. Bir serbest bırakma kapısı için bu **doğru** okumadır (üçü de kapı
> açmaz), ama makbuzdaki nüans kaybolur.

## 2. Adımlar

1. **Otoriter kaydı oku ve provenansı denetle.** Beş alanın (`document_id`,
   `revision`, `locator`, `accessed`, `snapshot_sha256`) hepsi dolu mu?
   Eksik olan varsa çalışma en fazla `CONFIRM`'dir — bu kontrol sonda değil
   **başta** yapılır, çünkü kaynaksız bir karşılaştırma sonucu ne olursa olsun
   `GREEN` üretemez.
2. **Sembol ve footprint'i yapısal oku** (`common.sexpr`; ham metin üzerinde
   regex **yok** — gerekçe: `cad_assets.py` `_numbers_under` docstring'i).
   Sembolden her pin için `number` + `name` + elektriksel tip; footprint'ten her
   pad için `number` + tip + şekil + merkez.
   - Çok-birimli / De Morgan tekrarları **daraltılır**; ama tekrar pin adı veya
     elektriksel tipi konusunda **çelişiyorsa** bu bir tekrar değil, bozuk
     semboldür → ihlal.
   - **Numarasız** (`""`) montaj/mekanik pad'ler ayrı sayılır, gerekçesiyle
     `excluded` edilir; pin tablosuyla karşılaştırılmaz.
3. **Pin kimliği** (`pin_kimlik_eslemesi`): evren = otoriter numaralar ∪ sembol
   numaraları. Her numara için ad(**işlev**) ve elektriksel rol karşılaştırılır.
   İki yön de bakılır: tabloda olup sembolde olmayan da, sembolde olup tabloda
   olmayan da ihlaldir.
   - Ad karşılaştırması **birebirdir** (yalnız baştaki/sondaki boşluk atılır).
     Küçük/büyük harf katlama yok, `~{X}` üst-çizgi yeniden yazımı yok, alias
     tablosu yok. **Normalleştirici, yanlış pinin sessizce doğru pine
     dönüştüğü yerdir.**
4. **Pad kimliği** (`pad_kimlik_eslemesi`): pad numarası + tipi; **EPAD ayrıca
   ve açıkça**. `exposed_pad.present: true` iken footprint'te o pad yoksa
   `RED` (yanlış paket varyantı). Footprint'te EPAD varken **sembolde karşılık
   pin yoksa** da `RED`: GND'ye bağlanamaz (MASTER_RULEBOOK Faz 1) ve ERC
   şikâyet etmez, çünkü şikâyet edecek bir şey yoktur.
5. **Sembol ↔ footprint pad haritası** (`sembol_footprint_padmap`): **küme
   eşitliği**, sayı eşitliği değil. 1..5 numaralı beş pad ile A1..A5 numaralı
   beş pin, iki tane "beş" ve sıfır tane eşleşmedir.
6. **Paket varyant kimliği** (`paket_kimligi`): `expected_footprint_name`
   belirteci + ölçülen `pad_span_x_mm` / `pad_span_y_mm` (pad **merkez**
   aralığı, EPAD hariç) `tolerance_mm` içinde. Kaynağın bildirmediği öznitelik
   **`excluded`**'dır — **varsayılan üretilmez** ve her `excluded` öznitelik
   çalışmayı `CONFIRM`'e sabitler.
7. **Taban sayım kapısı** (`taban_pin_sayisi`): `cad_assets.check_pin_count`
   içeriden çağrılır (kopyalanmaz). Datasheet pin sayısı **otoriter tablodan**
   türetilir, CLI'dan girilen bir sayıdan değil.
8. **Lifecycle reddi** (`lifecycle_serbest_birakma`): `cad_assets.lifecycle_gate`
   çağrılır. Bu bir **seçim değil reddir** — durum `lifecycle_report.json`'dan
   okunur, burada asla yazılmaz/yükseltilmez.
9. **Hash bağla ve bayatlığı ölç.** Kaynak dosyası, sembol, footprint ve (varsa)
   snapshot özetleri rapora yazılır; `--prior` ile bir önceki kalifikasyona göre
   değişen varsa `STALE`.
10. **Verdict → makbuz → (yalnız `GREEN` ise) kayıt.**
    `cad_asset_gate_receipt.json` yazılır; `--register` verilmişse
    `cad_assets.register_assets` `pin_check_passed = (verdict == GREEN)` ile
    çağrılır — kapının etrafından dolaşılabilen bir kapı, kapı değildir.

Verdict sırası (bu sıra bağlayıcıdır):
`INVALID_COVERAGE` → `RED` → `STALE` → `NO_COVERAGE` → en güçlü cap
(`CONFIRM` > `LOCAL_POLICY` > `SCREEN_ONLY`) → `GREEN`.
**Ölçülmüş bir uyuşmazlığı hiçbir cap yumuşatmaz**; eksik bir provenans alanı,
yanlış bir pin işlevini daha az yanlış yapmaz.

## 3. Nüanslar — bilinen tuzaklar

### (a) Sayı eşitliği kimlik eşitliği değildir — bu skill'in tamamı budur
Aşağıdakilerin **hepsi** `cad_assets.check_pin_count`'u temiz geçer:
- pin 1'i VIN, pin 3'ü GND olan bir sembol ile pin 1'i GND, pin 3'ü VIN olan bir
  parça — **aynı altı numara, takas edilmiş işlevler**, ilk enerjilendirmede
  regülatör üstünden kısa devre;
- pin 4'ü `EN`/`input` yazan bir sembol ile pin 4'ü `NC` olan varyant;
- aynı pad sayılı, farklı pitch'li (0,5 mm ↔ 0,4 mm) başka paket ailesinden bir
  QFN-40;
- datasheet'in hiç atamadığı bir numara altında duran, sayıyı denkleştiren ama
  gerçek termal pad'in yerini tutmayan bir `EP`.

### (b) `cad_assets` numarasız mekanik pad'i pad sayar — ölçüldü
`cad_assets.footprint_pad_numbers` `sorted(set(pads))` döndürür ve KiCad montaj
deliğini `(pad "" np_thru_hole ...)` diye yazar; boş dize kümede kalır. Ölçüm
(2026-08-10): tek montaj deliği olan **doğru** bir footprint'te taban kapısı
`footprint_vs_sembol: 6 pad / 5 pin; sembolde olmayan pad=['']` üretiyor.
Bu bir **kardinalite kapısı artefaktıdır**, varlık hakkında bir iddia değil.
Bu skill onu tespit eder (boş numaralı pad'ler atıldığında sayılar uyuşuyor mu?),
gerekçesiyle `excluded` eder ve **aynı iddiayı** `sembol_footprint_padmap` içinde
mekanik pad'leri dışlayarak **küme eşitliği** olarak ölçer. `cad_assets.py`
başka bir sahibin dosyasıdır ve buradan **düzenlenmez**; kurt masalı anlatan bir
kapı, kapatılan kapıdır.

### (c) Otoriter kaynak boşsa bu `RED` değil `NO_COVERAGE`'dır
Pin tablosu boş bir kayıtla adayın 40 pini karşılaştırılırsa 40 ihlal çıkar ve
rapor, hiç ölçmediği bir şey hakkında kesin konuşmuş olur. Boş tablo =
"pin evreni okunamadı" → `eligible>0, scanned=0`, `NO_COVERAGE`. Aynı sebeple
`datasheet_pins = 0` ile taban kapısı da koşturulmaz.

### (d) Sembol ile footprint'in birbirine uyması delil değildir
Kaynak yokken pad haritası teknik olarak hesaplanabilir; **kanıt değildir.**
İki dosyanın birbiriyle uyuşması, ikisinin de doğru parça olduğunu göstermez.
Bu yüzden kaynak okunamadığında pad haritası evreni de `excluded` edilir —
aksi hâlde rapor, yalnız kendine bakabildiği bir durumda `scanned > 0` derdi.

### (e) Kanıtsız istisna istisna değildir
Bir istisna öğeyi **taranan** nüfustan çıkarır; kapıyı formaliteye çeviren hamle
tam olarak budur. Kabul için gerekçe **ve** kendi beş alanlı provenansı şarttır.
Reddedilen istisna silinmez: öğe taranmaya devam eder, red rapora yazılır ve bir
`CONFIRM` cap ekler — **gerekçesiz istisna yazmak raporu sessizleştirmez,
gürültülendirir.**

### (f) Üretici land pattern'i varsa o kullanılır; yoksa türetme **yazılır**
Öncelik sırası: üretici land pattern > jenerik footprint kuralı. Üretici
uygulama notu jenerik kuralla çelişiyorsa **otoriter kaynak kazanır** ve uzlaşma
geometrisi **uydurulmaz**. Üretici yoksa kullanılan teknik (`derivation`,
ör. "IPC-7351B nominal, density level M") kaydedilir ve sonuç açıkça
**imza değildir**: `LOCAL_POLICY`. Teknik de kaydedilmemişse `SCREEN_ONLY`.

### (g) Sınır: bu skill pad'in **üretilebilirliğine** karışmaz
NSMD/SMD seçimi, EPAD pasta kapsaması, maske barajı ve fab geometrisinin
kanonik sahibi [[../dfm/SKILL|dfm]] §(b)–§(d) (`dfm/SKILL.md:197-233`).
Buradaki pad karşılaştırması bir **kimlik** iddiasıdır: "doğru cinsten land mı?"
Bir footprint bu skill'de tam kalifiye olup DFM'de reddedilebilir; **bu doğru
iş bölümüdür**, çelişki değil.

### (h) Sınır: MPN'i BOM seçer, neti sematik-erc bağlar
[[../bom-lifecycle/SKILL|bom-lifecycle]] MPN/lifecycle/alternatif sahibidir; bu
skill **parça seçmez**. [[../sematik-erc/SKILL|sematik-erc]] tel/net topolojisi
sahibidir; bu skill **net bağlamaz**, şematik açmaz.
[[../cpl-assembly/SKILL|cpl-assembly]] gerçek montaj oryantasyonunun sahibi
**değildir** — o yalnız paket export'u ve parity yapar; oryantasyon kanıtı
first-article'dır.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Datasheet pin işlevini/paket tablosunu net söylemiyorsa doğru pinout | C | Yazılım belirsizliği `GREEN`'e yükseltemez → `CONFIRM`, belgeyi insan okur |
| Land pattern lehimlenebilirliği ve montaj verimi | D | Dosya karşılaştırmasıyla ölçülmez; first-article + AOI/X-ray (L3) |
| Gerçek montaj oryantasyonu / pin-1 yönü | D | Fiziksel kanıt gerekir; kapanışı L3 üreticisine devredilir |
| Body ölçüsü / pitch | C | `.kicad_mod`'dan türetilmez (fab/silk çizim konvansiyonu, satır yapısı tahmini) → kaydedilir, doğrulanmaz |
| Pad'in üretilebilirliği (NSMD/SMD, pasta, maske) | — | Sahibi [[../dfm/SKILL\|dfm]]; kapsam dışı, `excluded` değil **başka sahibin** |
| Parçanın doğru seçilmiş olması | — | Sahibi [[../bom-lifecycle/SKILL\|bom-lifecycle]] |
| 3B model / mekanik çakışma | C | Sahibi [[../mekanik-dxf/SKILL\|mekanik-dxf]] |

> Bu skill `PHYSICAL_MEASUREMENT` yayımlamaz. Frontmatter'da `L3` **yoktur** ve
> yukarıdaki D satırları burada kapanmaz — [[../_shared/coverage-gate.md]]
> "participates-in ≠ produces-evidence-for" kuralı gereği kapanışları L3
> üreticisine devredilir. `GREEN` bir kalifikasyon, "bu kart lehimlenir"
> demek **değildir**; "bu dosya çifti, belgelenen cihazı ve paketi temsil ediyor"
> demektir.

## 5. Doğrulama

```bash
cd pcb-agent/tools

# kapı
python3 scripts/production/cad_asset_qualification.py \
  --source   pin_package_source.json \
  --symbol   part.kicad_sym \
  --footprint part.kicad_mod \
  --lifecycle lifecycle_report.json \
  --source-snapshot datasheet.pdf \
  --json    cad_asset_qualification.json \
  --receipt cad_asset_gate_receipt.json
# exit 0 = GREEN; 1 = RED/CONFIRM/STALE/LOCAL_POLICY/SCREEN_ONLY/NO_COVERAGE

# makbuz sözleşmesi — --root hem bu aracı hem artefaktları KAPSAMALI,
# çünkü resolve_path göreli yolu makbuzun dizinine göre çözer; makbuzdaki
# yollar bu yüzden mutlaktır.
python3 scripts/gate_receipt.py cad_asset_gate_receipt.json --root <ortak-kok>

# araç iç tutarlılığı + test paketi
python3 scripts/production/cad_asset_qualification.py --self-test
python3 -m unittest tests.test_cad_asset_qualification -v
```

Fault-injection (zorunlu, `test_cad_asset_qualification.py` içinde koşuyor):
otoriter kaynakta **tek bir pinin işlevini/adını değiştir** — pin **sayısı
değişmeden** verdict `GREEN` → `RED` olmalı ve taban sayım kapısı aynı koşuda
`PASS` kalmalıdır. Taban kapısının `PASS` kalması bir kusur değil, **bu skill'in
varlık sebebinin kanıtıdır**. İkinci enjeksiyon: footprint'ten `EP` pad'ini sil
→ `RED`. Üçüncü: sembol dosyasını kalifikasyondan sonra değiştir → `STALE`.

## 6. Kabul kriterleri

- [ ] Kapı, varlık kütüphaneye **kaydedilmeden ÖNCE** ve
      [[../sematik-erc/SKILL|sematik-erc]] o varlığı ilk kullanmadan önce koştu.
- [ ] Otoriter kaynak provenansı **tam**: `document_id` + `revision` + `locator`
      + `accessed` + `snapshot_sha256`. Eksikse verdict `GREEN` **değil**.
- [ ] Sembol/footprint `common.sexpr` ile **yapısal** okundu; ham metin regex'i
      kullanılmadı.
- [ ] Her pin için `number` **ve** `name`(işlev) **ve** `electrical_role`
      otoriter tabloya bağlandı; karşılaştırma iki yönlü (eksik + fazla).
- [ ] Ad karşılaştırmasında normalleştirme (case folding / overbar / alias)
      **uygulanmadı**.
- [ ] Pad kimliği + tipi karşılaştırıldı; numarasız mekanik pad'ler gerekçesiyle
      `excluded` edildi.
- [ ] `exposed_pad.present: true` ise EPAD hem **footprint'te** hem **sembolde**
      var; yoksa `RED`.
- [ ] Sembol pin kümesi ile footprint pad kümesi **küme olarak** eşit
      (sayı eşitliği yeterli sayılmadı).
- [ ] Paket varyant kimliği ölçüldü; kaynağın bildirmediği her öznitelik
      **gerekçesiyle** `excluded` ve çalışma `CONFIRM`'e sabitlendi —
      hiçbir varsayılan ölçü/tolerans üretilmedi.
- [ ] Land pattern **üretici** kaynaklıysa `document_id`+`revision` var; değilse
      `derivation` yazıldı ve sonuç `LOCAL_POLICY`/`SCREEN_ONLY` olarak
      **imza değildir** diye işaretlendi.
- [ ] Lifecycle `lifecycle_report.json`'dan **yalnız okundu**; engelli durumda
      varlık serbest bırakılmadı.
- [ ] Kaynak/sembol/footprint sha256'ları makbuza bağlandı; değişmişse `STALE`
      ve yeniden koşuldu.
- [ ] Her istisna gerekçe **ve** kendi provenansını taşıyor; kanıtsız istisna
      reddedildi ve öğe taranmaya devam etti.
- [ ] Fault-injection koştu: tek pin işlevi çevrildi → `RED`; taban sayım kapısı
      aynı koşuda `PASS` kaldı.
- [ ] `cad_asset_gate_receipt.json` üretildi ve **`gate_receipt.py` ile
      doğrulandı** (şeklen benzemek yetmez); `parsed_metrics` ile
      `coverage.per_metric` anahtar kümeleri **aynı**, `blockers` `{code,
      message}` nesneleri, `rules` = otoriter kaynak kaydı (path+sha256).
- [ ] Kayıt yalnız araç üzerinden yapıldı (`--register`), elle lib-table yaması
      önerilmedi; `GREEN` olmayan bir çalışmada kayıt **reddedildi**.
- [ ] Yeni verdict **üretilmedi**; hepsi [[../_shared/coverage-gate.md]]
      sözlüğünden.
- [ ] coverage: her metrikte ve toplamda `scanned + excluded == eligible`,
      `violations <= scanned`; `scanned = 0` → `NO_COVERAGE`.

## İlgili

[[../bom-lifecycle/SKILL|bom-lifecycle]] · [[../sematik-erc/SKILL|sematik-erc]] ·
[[../dfm/SKILL|dfm]] · [[../cpl-assembly/SKILL|cpl-assembly]] ·
[[../mekanik-dxf/SKILL|mekanik-dxf]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../pcb-orchestrator/SKILL|orchestrator]] ·
[[../_shared/rule-conflicts.md]]

## Araç kancaları (2026-08-10)

- **`cad-asset-qualification`** — Bu skill'in kendi aracı. Aday sembol+footprint
  çifti otoriter pin/paket kaydına **pin-pin, pad-pad, paket-varyantı** düzeyinde
  bağlanana kadar serbest bırakılmaz; sayı eşitliğini semantik eşitlik saymaz.
  Kayıt öncesi çağrılır, `--register` ile kayıt yalnız `GREEN`'de açılır.
- **`cad-assets`** — Taban katman: lifecycle kapısı (indirme öncesi), pin/pad
  **sayısı** kapısı ve kütüphane kaydı. Bu skill onun üstünde durur, yerine
  geçmez; `check_pin_count`, `lifecycle_gate` ve `register_assets` içeriden
  çağrılır.
- **`bom-lifecycle`** — MPN/lifecycle/alternatif sahibi. `lifecycle_report.json`
  buradan gelir ve **yalnız okunur**; alternatif kabulü bu skill'i L2'den
  yeniden açar.
- **`jlc-library`** — LCSC parça numarasından KiCad sembol/footprint/3B modeli.
  Aday varlık köprüsü: indirir, **kalifiye etmez**.
