---
name: arayuz-eslesme-kapisi
description: İki fiziksel bağlantı elemanının birbirine takılabilirliğinin ve uçtan uca pin örtüşmesinin tek otoritesi. Zincirdeki her halka bir kayıt; eksik halka bir ihlaldir, sessizlik değil. Sayı eşitliği kimlik değildir — kablo↔konnektör ekseninde.
version: 1.0.0
last-reviewed: 2026-08-12
phase: "Faz 1 — konnektor connector_spec.json'ı yayımladıktan SONRA, sematik-erc pinout'u dondurmadan ÖNCE zorunlu kapı (+ Faz 3: adaptör gövdesi keepout doğrulaması)"
assurance_loop: [L1, L2]
evidence_origin: [AUTHORITATIVE_SOURCE, DESIGN_ARTIFACT]
signoff: false
inputs:
  - {artifact: interface_contract.json, schema: ../sematik-erc/references/interface_contract.schema.json, required: true}
  - {artifact: interconnect_chain.json, schema: references/interconnect_chain.schema.json, required: true}
  - {artifact: connector_spec.json, schema: ../konnektor/references/connector_spec.schema.json, required: false}
  - {artifact: lifecycle_report.json, schema: ../bom-lifecycle/references/lifecycle_report.schema.json, required: false}
outputs:
  - {artifact: interconnect_mating_report.json, schema: ../../tools/schemas/interconnect_mating_report.schema.json}
  - {artifact: interconnect_gate_receipt.json, schema: ../../tools/schemas/gate_receipt.schema.json}
  - {artifact: l3_rule_universe.json, schema: ../dogrulama-matrisi/references/l3_rule_universe.schema.json, role: contributor}
writes_board: false
tools: [interconnect-mating]
---

# SKILL: Arayüz Eşleşme Kapısı (fiziksel ara-bağlantı zinciri)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

Bu skill **tek bir soruya** cevap verir: **A'nın fiziksel çıkışı B'nin fiziksel
girişine takılır mı, ve pinler örtüşür mü?** Parçayı **seçmez**, net
**bağlamaz**, hacim çakışması **ölçmez**, akım **hesaplamaz**.

> **Boşluk K1/K2.** Bu sorunun repoda sahibi yoktu ve bu bir kural boşluğu
> değil, **evren** boşluğuydu. Netlist **elektrikseldir**: iki pin aynı nete
> bağlıysa bağlıdır, o pinlerin fişleri birbirine giriyor mu diye sormaz.
> Footprint **tek parçadır**: bir pad dizisinin kendi içinde tutarlı olduğunu
> söyler, karşısına ne takıldığını bilmez. Keepout **hacimdir**: iki gövdenin
> çakışmadığını söyler — ve çakışmamak takılmak değildir.
> [[../konnektor/SKILL|konnektor]]'ün evreni yazılı olarak *"netlist'teki
> konnektör sınıfı refdes"*tir; hangi alanı eklerseniz ekleyin, **refdes'i
> olmayan bir kablo o paydaya giremez.**

## 0. Ne zaman çağrılır

- **Faz 1**, [[../konnektor/SKILL|konnektor]] `connector_spec.json`'ı
  yayımladıktan **sonra**; [[../sematik-erc/SKILL|sematik-erc]] pinout'u
  dondurmadan **önce** — zorunlu kapı.
- [[../ecad-varlik-kalifikasyonu/SKILL|ecad-varlik-kalifikasyonu]]'ndan da
  **önce**: ECAD kapısı *yanlış MPN'i mükemmel şekilde kalifiye eder.* Eşleşme
  kapısı MPN seçiminin kendisini sorgular.
- **Faz 3**: adaptör/kablo **gövdesi** keepout doğrulamasına `role: contributor`
  olarak katılır — hacim şemasının sahibi [[../mekanik-dxf/SKILL|mekanik-dxf]]'tir.
- **L2:** `interface_contract.json` veya kablo/adaptör MPN'i değişirse yeniden
  koşar. Yeni MPN = yeni eşleşme sorusu.
- `writes_board: false` — **board kilidi almaz**. Aynı anda board yazan bir
  skill ile serileştirilmesi gerekmez.
- Girdi hash'i kapıdan sonra değişirse çıktı **bayatlar** (`STALE`).

**Niçin Faz 1, `sematik-erc`'yi bloklayarak:** girdileri daha erken hazır değil
(`connector_spec.json` Faz 1'de doğar) · `konnektor/SKILL.md:33-35` *"Şematikten
**önce**: koruma zinciri ve pinout kararı buna bağlı"* · Faz 2'de bulunursa
şematik + netlist yeniden yazılır · Faz 3'te yakalanırsa **10× geri dönüş
maliyeti** ([[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]:246-248) · Faz 5A'da
**hiç görünmez**, çünkü FAI evreni "released CPL refdes evreni"dir ve kart
*dışındaki* kablo orada yoktur.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| Zorunlu bağlantı kümesi (**PAYDA**) | `interface_contract.json` | koşu **reddedilir** (exit 2): paydasız bir kapsam yoktur |
| Zincir kayıtları (**PAY**) | `interconnect_chain.json` ([`şema`](references/interconnect_chain.schema.json)) | `chains: []` geçerlidir; sonuç `NO_COVERAGE` |
| Konnektör datasheet nitelikleri | `connector_spec.json` (opsiyonel, çapraz okuma) | zincir kaydındaki alan kullanılır, provenansıyla |
| Kablo/adaptör lifecycle | `lifecycle_report.json` (opsiyonel) | BOM satırı yoksa `CONFIRM` + L2 kenarı |
| **Çıktı** | `interconnect_mating_report.json` ([`şema`](../../tools/schemas/interconnect_mating_report.schema.json)) | — |

İki dosya **ayrı** olmak zorundadır ve araç tek dosyayla koşmayı reddeder:
payda ile pay aynı belgeden gelseydi kapı kendi beyanını kendi beyanıyla
karşılaştırır ve bir **tautolojiye** dönerdi.

### Veri modeli — düğüm bir PARÇA değil, iki YÜZ taşır

Her düğüm `faces[]` taşır; her yüzün kendi `interface_type`, `interface_class`,
`pin_map`, `pin1_reference`, `numbering_direction`, `contact_face` alanları var.
Gerekçe ölçülmüştür: CAM-W'nin J101 ve J201'i aynı aile, aynı kart, ama **farklı
pin ataması** — J101 pin 17 = `XVCLK`, J201 pin 17 = `FSYNC`; J101 pin 22 =
`SCL`, J201 pin 22 = `XSHUTDOWN`. Kart tek yüz sayılsaydı bu iç geçiş **hiç
ölçülmezdi.** Kablo için daha kritik: iki ucundaki sıranın düz mü ters mi olduğu
tek yüzlü modelde **görünmez.**

**İki kenar sınıfı, İKİSİ DE `eligible`'a girer:**

| Kenar | Tanım | Ölçtüğü |
|---|---|---|
| **`mating`** (dış) | düğüm yüzü ↔ komşu düğüm yüzü | fiziksel takılabilirlik |
| **`internal`** (iç) | aynı düğümün iki yüzü · aynı kablonun iki ucu | kart netlist sürekliliği · kablonun düz/ters eşlemesi |

Kablonun iç kenarı `mapping: straight \| reversed \| custom` beyan etmezse
`NO_COVERAGE`. `custom` beyan edip `pin_to_pin[]` boş bırakmak `RED`'dir:
*"custom" tek başına bir bilgi değil, bilginin yerinde durduğunun beyanıdır.*

### Zincirin sınırı — objektif kriter: **AYRILABİLİRLİK**

> Bir geçiş, ancak **alet gerektirmeden ayrılıp yeniden takılabiliyorsa** düğüm
> sınırıdır. İşlemsel tanım: **`mating_cycles` (connector_spec'in
> `datasheet_cycles`'ı) alanı anlamlı olan her geçiş bir yüzdür.**

Lehimli/kalıcı geçiş (BGA, SMT pad) **yüz değildir** — o
[[../ecad-varlik-kalifikasyonu/SKILL|ecad-varlik-kalifikasyonu]]'nun alanıdır.
Mevcut ayrımlarla çakışmaz: RK3588 BGA → düğüm değil ✔ · OG05B10 silikonu →
düğüm değil (NDA, kapsam dışı) ✔ · DF40 mezzanine → düğüm **evet** (lehimli ama
çift takılıp çıkar, `datasheet_cycles: 30`) · FFC ucu, FAKRA, klemens → **evet**.

### `medium` üçüncü bir birimdir ve **nullable değildir**

FFC/FPC ZIF yuvasının karşı tarafı bir konnektör **değil**, kablonun
kendisidir. Digi-Key kategori ağacı bunu ayrı kategorilerle kodlar (housings
390 ≠ assemblies 399 ≠ flat flex cables). Bir DIP pin çıkışı ile FFC yuvası
arasındaki mesafeyi kapatan tek şey bir **cable assembly**'dir ve o **ayrı bir
BOM kalemidir.** `medium`'un zorunlu olması, eksik halkayı *"hiç var olmayan bir
şey"*ten **zorunlu bir alanın boşluğuna** çevirir: artık şema düzeyinde görünür.

Kenar aritmetiği: iki uçlu bir `medium` (`FFC_CABLE`/`FPC`/`ADAPTER_BOARD`/
`DISCRETE_HARNESS`/`COAX_ASSEMBLY`) **iki eşleşme + bir iç kenar** üretir;
`DIRECT_MATE` **bir** eşleşme üretir, iç kenarı yoktur.

## 2. Adımlar

1. **Sözleşmeden paydayı türet.** `interface_contract.json`'ın **gerektirdiği**
   bağlantı kümesi okunur. İki biçim de okunur ve **fark rapora yazılır**:
   kanonik `interface-contract/v1` (`interfaces[]`) ve depoda gerçekte bulunan
   kart-yerel biçim (`$schema: pcb-agent/interface_contract/v1`, adlandırılmış
   bölümler). Birini okuyup diğerini reddetmek kapıyı deponun **kendi verisine**
   kapatırdı; ikisini okuyup farkı susmak iki sözleşme biçiminin varlığını
   gizlerdi.
2. **Zincirleri sözleşmeye bağla.** `contract_ref` (`<yol>#/<pointer>`)
   çözülmeyen zincir `CONTRACT_REF_UNRESOLVED` → `RED` ve kenarları evrene
   **girmez**. Zorunlu bir bağa **iki** zincir bağlanması da ihlaldir: hangisinin
   ölçüldüğü sıraya bağlı olurdu.
3. **Kenar evrenini kur.**
   `eligible = Σ_düğüm (yüz_sayısı − 1) + dış_kenar_sayısı`. Düz zincirde bu tam
   olarak *"düğüm − 1"*e indirgenir. Aynı kart iki zincirde geçse bile **iç
   kenarı bir kez** sayılır: J101 ↔ J201 sürekliliği tek bir fakttır. Bir
   arayüzün zinciri **yoksa ya da hiç eşleşme kenarı üretmiyorsa** sözleşme
   asgarisi olarak **1 kenar** evrende durur ve `MISSING_TRANSITION` ile ihlal
   sayılır.
4. **Her kenarı R1–R20 ile değerlendir** (§4 tablosu). Ölçülmemiş bir alan
   üzerinden yapılan karşılaştırma `PASS` **değil**, `NO_COVERAGE`'dır:
   `null == null` eşitlik değildir.
5. **Kenar durumunu belirle.** `signoff` veya `screen_only` bir kuralın FAIL'i
   kenarı `FAIL` yapar · `record_only` bir FAIL kenarı **FAIL yapmaz**, bir
   `CONFIRM` cap ekler · FAIL yokken bir `signoff` kural `NO_COVERAGE` ise kenar
   `NO_COVERAGE`'dır · `PASS` yalnız her `signoff` kural PASS iken.
6. **Kapsamı yayımla.** `eligible / scanned / excluded / violations` — üç blok:
   `coverage` (kenar), `contract_coverage` (arayüz), `provenance_coverage`
   (karşılaştırılan alan). `scanned` **gerçekten incelenen** nüfustur,
   `eligible − excluded` **değil**.
7. **Verdict'i tek yerde ver.** `INVALID_COVERAGE` → `RED` → `STALE` →
   `NO_COVERAGE` → en güçlü cap (`CONFIRM` > `LOCAL_POLICY` > `SCREEN_ONLY`) →
   `GREEN`. `GREEN` yalnız her evren tarandı, hiçbir şey uyuşmadı ve **hiçbir cap
   uygulanmıyorken** erişilebilir.
8. **Domain durumunu AYRI alana yaz.** `gate.verdict` kapalı sözlüktedir;
   `domain_state` (`CHAIN_COMPLETE`/`CHAIN_BROKEN`/`MEDIUM_UNSPECIFIED`/
   `PINMAP_UNVERIFIED`/`DECLARED_NONMATCHING`) kendi sözlüğündedir. Aynı raporda
   ikisi varsa **bağlayıcı olan `gate.verdict`**'tir.
9. **L3 satırlarını yaz.** Kanıtlanamayan her şey
   `l3_rule_universe.json`'a satır olarak devredilir (§4b).

## 3. Nüanslar — bilinen tuzaklar

### (a) `eligible` beyan listesinden TÜREMEZ — kapının hayat memat meselesi

Ölçülmüş sebep, deponun kendi belgesindedir
(`docs/BULGU-bos-kontrol-temiz-gorunur.md:79-85`): iki halka beyan edilip
üçüncüsü unutulduğunda `eligible=2, scanned=2, violations=0 → GREEN` olur ve
eksik halka evrene **hiç girmez**. Teardrop vakasının birebir aynısı —
> *"Yanlış populasyondan gelen bir payda, gerçek bir muhafızla aynı yeşili
> verir."*

Bu yüzden zincir dosyası `eligible` / `coverage` / `coverage_hint` /
`edge_count` / `declared_eligible` alanlarından birini taşırsa koşu
**reddedilir** (`ELIGIBLE_FROM_CHAIN_REFUSED`, exit 2) — yoksayılmaz: yoksaymak
okuyucuyu sayının onurlandırıldığına inandırırdı.

### (b) Pin haritası hizalama — **numara ekseni yoktur**

Karşılaştırma **fiziksel kontak konumu** üzerinden yapılır, pin numarası
üzerinden **değil**: iki üreticinin numaralandırma yönü farklı olabilir. Her yüz
üç alan taşır — `pin1_reference`, `numbering_direction`, `contact_face`.
Ölçüldü: **bu üç alan hiçbir kart dosyasında yok** → geriye dönük doldurma işi;
doldurulamayanda `NO_COVERAGE`, **uydurma yok**.

**Alias tablosu yasaktır** (`ecad-varlik-kalifikasyonu:130-133`: *"Normalleştirici,
yanlış pinin sessizce doğru pine dönüştüğü yerdir."*). Repoda bugün mevcut örnek:
`camera_hub_interface_wearable.yaml:73` `MIPI_D0_P` diyor, CAM-W netlisti
`CSI_D0_P` diyor. Aynı iletken, iki ad — bu bir **ihlaldir**, çözümü
normalleştirme değil, **sözleşmenin veya kartın düzeltilmesidir**.

Küme eşitliği ile **sıra** eşitliği ayrı kodlardır: net kümeleri aynı ama sıra
yanlışsa `PIN_ORDER_MISMATCH`. Küme eşitliği
[[../ecad-varlik-kalifikasyonu/SKILL|ecad-varlik-kalifikasyonu]]'nun, **sıra** bu
kapının predicate'idir.

### (c) Satıcı harfi ASLA anahtar olmaz

Kablo kontak-yüzü tipini adlandıran **evrensel standart yoktur**: Molex `A`/`D`,
Würth `1`/`2`, Farnell `same-sided`/`opposite-sided`, piyasa jargonu `A`/`B` — ve
**Molex'te "B" diye bir tip yoktur.** Satıcı harfi anahtar yapılırsa Molex
parçasında **sessizce yanlış** çalışır. Şema yalnız `SAME_SIDE`/`OPPOSITE_SIDE`
normalize enum'unu taşır; satıcı harfi `provenance.vendor_contact_letter` içinde
ham veri olarak yaşar ve **hiçbir kararı beslemez** (regresyon testi bunu beş
harfle koşturur).

### (d) `excluded`, "bakılmadı"nın kibar hâli değildir

`sistem-arayuz-sozlesmesi:150-158`'den miras: gerekçe taşımayan birim
`unjustified_exclusions`'a düşer, **taranan nüfusta kalır** ve verdict `RED`
olur. Aritmetik her blokta korunur; değişen şey, bir birimi `excluded`'a
**kimin** koyabildiğidir. Evrende **olmayan** bir kenarı dışlamak paydayı elle
küçültme girişimidir: sayılır, aritmetiği bozar ve `INVALID_COVERAGE` (exit 2)
üretir.

Kenar kimlikleri `:` içerir (`mate:ic_1:a`) — ilk iki nokta üzerinden bölmek
`mate:ic_1`'i *"mate adlı kenar, gerekçe 'ic_1'"* diye okurdu ve **gerekçesiz bir
dışlama sessizce gerekçeli hâle gelirdi**. Ayrıştırıcı bilinen kimliklerin en
uzun önekini arar.

### (e) `R4` imza AÇMAZ, ama bir FAIL'i kapıyı kapatır

`authority: screen_only`, *"temiz bir sonuç tek başına imzalamaz"* demektir —
*"kirli bir sonuç bir şey söylemez"* demek **değil**: 30 kontaklı bir kablo 22
kontaklı bir yuvaya girmez. `record_only` (R6/R8/R16/R19/R20) ise iki yönde de
kapı açmaz/kapatmaz: bir FAIL kayda geçer ve `CONFIRM` cap ekler. Kaplama
uyuşmazlığını ölçülmüş bir takılamazlıkla eşitlemek, kararı
[[../konnektor/SKILL|konnektor]]'den çalmak olurdu.

### (f) `R12` öldürücüdür — `R6` ile aynı önceliğe konmaz

Yanlış pin haritası MIPI'de *"çalışmaz"* değildir: 3V3 rayının bir D-PHY lane'ine
binmesi sensör veya ISP girişinde **kalıcı hasardır**. `R6` sessiz **açık devre**
üretir; `R12` sessiz **hasar**.

### (g) Provenans eksikliği bir cap'tir, bir ihlal değil

Bir alan **karşılaştırıldıysa** provenansı da gelmek zorundadır ve `revision`
listede: revizyonsuz bir belge bir **belge ailesini** adlandırır, okunan nüsha
sonradan geri getirilemez. Ama kaynaksızlığı `FAIL` saymak onu **yanlış bir
pinle** eşitlerdi (`RED`), `NO_COVERAGE` saymak **kimse bakmadı** derdi —
ikisi de yanlış. Doğru cevap `CONFIRM`'dür ve onu bir **cap** taşır; sayı
`provenance_coverage` bloğunda assert edilebilir kalır. `basis: derived` /
`vendor_page` → `SCREEN_ONLY`; `local_policy` → `LOCAL_POLICY`. Hiçbiri `GREEN`
değildir.

### (h) `DECLARED_NONMATCHING` gerçek bir vakadır

`proc-a-cv72s/pins/som_cv72s_if.json:9` — taraf **bakmış**, yüz **var**, ve
*"gercek SoM pinlerine KARSILIK GELMEZ"* diye **açıkça yanlış olduğunu beyan
etmiş**. Bu `NO_COVERAGE` değil (bakıldı), bir geometri uyuşmazlığı değil (tip
bilinmiyor), `PASS` hiç değil. **Kapalı sözlüğe yeni verdict eklenmez:**
`gate.verdict = RED` + `domain_state = DECLARED_NONMATCHING`.

### (i) Aynalanma otoritesi burada, `konnektor`'de değil

[[../konnektor/SKILL|konnektor]] **tek taraflıdır**: kendi konnektörünün kontak
yüzünü bilir, karşı tarafınkini bilemez ve şeması (`additionalProperties: false`)
karşı tarafı hiç tanımaz. Aynalanma **iki taraflı bir ilişkidir** ve tek taraflı
kayıtta ifade edilemez. Bu kapı böylece `contact_face` ve
`fpc_insert_direction`'ın **ilk gerçek tüketicisi** olur — yeni alan icat etmez,
ölü alanları diriltir.

### (j) Kenar durumunu bir bulguya bağlamayı unutmak sessiz GREEN üretir

Bulgular kuralları **gruplara** yönlendirir ve bir grubu güncellemeyi unutmak, o
kuralın FAIL'ini rollup'a hiç sokmaz: `coverage.violations` 2 derken her bulgu
`PASS` kalır ve kapı `GREEN` verir. **Ölçüldü** (R0 beyan-edilmiş-uyuşmazlık
kuralı geliştirme sırasında tam olarak bunu yaptı). Bu yüzden
`her_kenarin_durumu_bir_bulguya_bagli` bulgusu her kenarın durumunu **doğrudan**
sayar ve yönlendirilmeyi unutulan bir kural sessiz kalamaz.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden | Devir |
|---|---|---|---|
| Gerçek mating kuvveti / hizalama / aktüatör kilidi | D | fiziksel fixture gerekir | `l3_rule_universe.json` satırı → [[../l3-bringup-karakterizasyon/SKILL\|l3-bringup-karakterizasyon]] |
| Pin-1 köşelerinin uzaysal ilişkisi (mirroring paritesi) | C | **hiçbir datasheet alanı taşımaz**; mekanik yerleşimden türer | `pin_to_pin[]` çizelgesini **insan imzalar** |
| *"Mates with"* ilişkisinin doğruluğu | C | üreticiler makine-okunur yayımlamıyor; ölçülmüş vakada **Molex'in kendi datasheet'i kendi ordering table'ıyla çelişiyor** | insan girişi + kaynak URL; R16 `record_only` |
| Zincirin **aradaki** bir düğümünün hiç yazılmadığı | C | düğüm sayısı bir **beyandır** | kanıtlayabildiği: her zorunlu arayüzün bir zinciri var ve beyan edilen hiçbir halka medium'suz değil |
| NDA'lı pinout (OG05B10 89-pin CSP) | — | kapalı | `BLOCKED_UNSPECIFIED` — **"geçti" diyemez** |
| Datasheet'siz COTS modülün pin sırası | C→D | satıcı web sayfası otorite değil | `basis: vendor_page` → en fazla `SCREEN_ONLY`, L3 ölçümüyle kapanır |
| Kablonun gerçek bükülme/yönlendirme yolu, uzunluk | D | IPC-D-620A §8.2 clocking | [[../mekanik-dxf/SKILL\|mekanik-dxf]] + L3 |
| Kontak akım paylaşımı, derating, launch discontinuity | C→D | elektro-termal / TDR | [[../konnektor/SKILL\|konnektor]] + L3 |
| Hacim çakışması | — | çakışmamak takılmak değildir | [[../mekanik-dxf/SKILL\|mekanik-dxf]] |
| Kablo/adaptör MPN'inin doğru seçimi | — | stok/lifecycle/alternatif | [[../bom-lifecycle/SKILL\|bom-lifecycle]]; BOM satırı yoksa `CONFIRM` + L2 |
| **Adaptör/kablo gövdesi keepout hacmi** | — | **Faz 3'e ertelendi ve bu kapı YAZMAZ** | açık madde; sahibi [[../mekanik-dxf/SKILL\|mekanik-dxf]] |

> **`keepout_zones.json` bilerek `outputs`'tan DÜŞÜRÜLDÜ.** Kodun hiçbir zaman
> yazmadığı bir çıktıyı beyan etmek, bu araştırmanın adını koyduğu kusur
> sınıfının ta kendisidir: **beyanın kapı sanılması** (*declared-but-ungated*).
> Emsal repoda ölçülmüştür — `contact_face` şemada `required` ama tüketicisi
> **sıfır**; tam bu yüzden hiçbir şeyi kapatmıyordu. Adaptör gövdesi keepout
> katkısı **ayrı bir Faz 3 ticket'ıdır** ve o ticket kapanana kadar bu satır
> burada, "kanıtlayamadıkları" tablosunda durur. `l3_rule_universe.json`
> katkısı ise **gerçekten yazılıyor** (`report.l3_handoff`) ve `outputs`'ta
> kalır.

### 4a. Kural tablosu R1–R20

`level` sınıflandırması [[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]'ne göre.
**Hiçbiri düz `A` değil** (R11/R12/R13 hariç — onlar netlist/pin haritasından
ölçülür): `A`'nın tanımı *"board/şematik dosyasından ölçülebilir"*dir ve iki
belgenin karşılaştırması board'dan ölçülmez. Emsal: `dogrulama-matrisi:107`
(konnektör akımı da `C→A`).

| ID | Kural | Girdi | Verdict kodu | level | authority |
|---|---|---|---|---|---|
| **R1** | `medium` mevcut | `interconnect.medium` | `NO_MATED_PART` / `MISSING_TRANSITION` | C→A | signoff |
| **R2** | Arayüz sınıfı uyumu | iki `interface_class` + `medium.kind` | `INTERFACE_CLASS_MISMATCH` | C→A | signoff |
| **R3** | Pitch eşitliği | `pitch_mm` × 3 | `PITCH_MISMATCH` | C→A | signoff |
| **R4** | Pozisyon sayısı eşitliği | `positions` vs `conductors` | `POSITION_COUNT_MISMATCH` | C→A | **screen_only** |
| **R5** | Cinsiyet | `gender[0..1]` | `GENDER_CONFLICT` | C→A | signoff |
| **R6** | Kontak yüzü (M1) | `contact_face` vs `stiffener_face` | `CONTACT_FACE_MISMATCH` | C | record_only |
| **R7** | Kablo tipi tutarlılığı (M3) | `contact_orientation` vs `stiffener_face` çifti | `CABLE_TYPE_MISMATCH` | C→A | signoff |
| **R8** | Kuyruk kalınlığı | `accepts_thickness_mm` vs `thickness_mm` | `TAIL_THICKNESS_OUT_OF_RANGE` | C | record_only |
| **R9** | Genişlik & margin | `width_mm` ≥ (`conductors`−1)·`pitch` + 2·`margin` | `TAIL_GEOMETRY_MISMATCH` | C→A | signoff |
| **R10** | Pin-to-pin çizelgesi zorunlu | `pin_to_pin[]`, `mapping` | `PINMAP_UNDECLARED` / `MAPPING_UNDECLARED` | C→A | signoff |
| **R11** | Net kimliği (+ sıra) | çizelge üzerinden net adları | `NET_NAME_MISMATCH` / `PIN_ORDER_MISMATCH` | A | signoff |
| **R12** | Güç/veri çarpışması | pin `class` alanları | `POWER_ON_SIGNAL_PIN` | A | signoff |
| **R13** | Diferansiyel çift bütünlüğü | pin `class` + `pair` | `DIFFPAIR_SWAP` / `DIFFPAIR_SPLIT` | A | signoff |
| **R14** | Lane sayısı (uçtan uca) | `lane_count` × 2 | `LANE_COUNT_MISMATCH` | C→A | signoff |
| **R15** | Mezzanine stack height | `stack_height_mm` × 2 | `STACK_HEIGHT_MISMATCH` | C→A | signoff |
| **R16** | Mating half MPN | `valid_mating_mpns` | `NOT_A_VALID_MATING_PART` | C | record_only |
| **R17** | Provenans zorunluluğu | alan başına 5 alan + `basis` | `UNVERIFIED_ATTRIBUTE` (cap) | C | signoff (cap) |
| **R18** | Parça belirlenmişlik | `mpn` + `manufacturer` + `revision` | `BLOCKED_UNSPECIFIED` | C→A | signoff |
| **R19** | Kaplama uyumu | `contact_finish` × 2 | `PLATING_MISMATCH` | C | record_only |
| **R20** | Mating cycle bütçesi | `mating_cycles` vs `--bringup-mate-cycles` | `MATING_CYCLE_BUDGET` | C | record_only |

**R4 neden `screen_only`:** bu,
[[../ecad-varlik-kalifikasyonu/SKILL|ecad-varlik-kalifikasyonu]]'nun tüm varlık
sebebinin **zincire** uygulanmasıdır. 20 pozisyonlu breakout + 20 pozisyonlu FPC
"20=20" geçer, ama pin 1 karşı uçta pin 20'ye düşüyorsa zincir yanlıştır. İmzayı
**R10 + R11** taşır.

> **Kanonik kanıt (birincil kaynak):** Raspberry Pi 5'te CSI ve DSI portları
> **fiziksel olarak özdeştir** — 22-pin, 0.5 mm pitch, 11.5 mm genişlik — ama
> kablolar birbirinin yerine geçmez ve RPi bunları **ayrı SKU** olarak satar.
> Aynı konnektör, aynı pitch, aynı pozisyon sayısı, **farklı kablo.**

### 4b. L3 devir kuralı — limitin sahibi BU kapıdır

Kapı `l3_rule_universe.json`'a satır yazar ve **limitin sahibi olur**;
`owner_skill` olarak `l3-bringup-karakterizasyon` yazılırsa limit
`SELF_AUTHORED_LIMIT` ile **reddedilir** — bir skill hem limiti hem kanıtı
sahiplenirse kendi ölçtüğü değeri kendi yazdığı eşikle karşılaştırır ve kapı bir
**tautoloji** olur ([[../_shared/rule-conflicts.md]] §9). Limitsiz satır
`NO_COVERAGE`'dır: `physical_measurement.py::rule_limit`'in hiçbir fallback dalı
yoktur.

Fixture alan adları `konnektor/SKILL.md:200-203`'teki **mevcut** alanlardan
yeniden kullanılır, yeni ad **icat edilmez** — `actual_connector_mpn`,
`actual_fpc_thickness`, `datum_fixture_used`, `insert_remove_without_force`,
`actuator_full_lock_visible`, `post_mate_clearance_margin`. Bir büyüklük iki adla
yaşarsa iki farklı değer taşır.

`assurance_loop`'ta **`L3` yoktur**: `PHYSICAL_MEASUREMENT` yayımlamayan bir
skill `L3` listelese bile o kuralı **kapatamaz** (`coverage-gate.md:75-77`).
Emsal: `sistem-arayuz-sozlesmesi` bunu bilinçli olarak yapmış.

## 5. Doğrulama

```bash
# araç iç tutarlılığı (F1-F10 + negatif taraf, hepsi burada koşar)
python3 tools/scripts/interconnect_mating.py --self-test

# gerçek CAM-W zinciri — BEKLENEN sonuç RED, exit 1
python3 tools/scripts/interconnect_mating.py \
    --contract ../cam-w-og05b10/docs/interface_contract.json \
    --chain ../system-planning/interconnect_chain.json \
    --json interconnect_mating_report.json \
    --receipt interconnect_gate_receipt.json --now <ISO-8601>

# regresyon paketi (F1-F10 ayrı ayrı adlandırılmış testler olarak)
python3 -m unittest tests.test_interconnect_mating -v

# sözleşme + kayıt muhafızları
python3 tools/scripts/sync_tool_registry.py --check
make -C tools contracts

# makbuz sözleşmesi — --root hem bu aracı hem artefaktları KAPSAMALI
python3 tools/scripts/gate_receipt.py interconnect_gate_receipt.json --root <ortak-kök>
```

### Zorunlu fault-injection F1–F10

`--self-test` **ve** `tools/tests/test_interconnect_mating.py` içinde koşar.

| # | Enjeksiyon | Beklenen | Neyi kanıtlar |
|---|---|---|---|
| **F1** | Zincirden bir halkayı sil (`medium` → null) | `RED` + `NO_MATED_PART`, halka **adıyla** raporda | Kapının var olmayan bir şeyin yokluğunu gördüğünü |
| **F2** | Halkayı sil **VE** paydayı küçültmeyi dene (üç yol: zincirde `eligible` beyanı · halkayı silme · zinciri tümden kaldırma) | hepsinde `RED` — `NO_COVERAGE` **değil**, `GREEN` **hiç değil** | Evrenin `interface_contract`'tan türediğini. **F1 tek başına yetersizdir** |
| **F3** | Hiç zincir + hiç zorunlu bağ | `NO_COVERAGE` + `coverage.reason` dolu | `scanned=0` kuralı |
| **F4** | Gerekçesiz `excluded` | `RED` + `UNJUSTIFIED_EXCLUSION`, `excluded` **artmaz** | `excluded`'ın "bakılmadı" olmadığını |
| **F5** | Pitch 0.5→0.4, **pozisyon sabit** | `RED` + `PITCH_MISMATCH`; **R4 aynı koşuda `PASS` kalır** | Sayı eşitliğinin kimlik olmadığını — PASS kalması kusur değil **kanıttır** |
| **F6** | Pin sırasını permüte et, pitch + sayı sabit | `RED` + `PIN_ORDER_MISMATCH` + `POWER_ON_SIGNAL_PIN`; **R3 ve R4 `PASS` kalır** | K2'nin kapatıldığını ve R12'nin canlı olduğunu |
| **F7** | Pitch'i datasheet yerine `basis: derived`'dan ver | `SCREEN_ONLY` / `LOCAL_POLICY`, asla `GREEN` | Türetilmiş verinin imza açmadığını |
| **F8** | Provenanstan `revision` düşür | en fazla `CONFIRM` | Kaynaksız karşılaştırmanın GREEN üretemeyeceğini |
| **F9** | Kapı koştuktan sonra segment kaydını değiştir | `STALE` | Hash bağının canlı olduğunu |
| **F10** | Evrende olmayan bir kenarı `excluded` yap | `INVALID_COVERAGE`, **exit 2** | Aritmetik muhafızının canlı olduğunu; `scanned`'in türetilmediğini |
| **NEG** | Bozulmamış fixture | `GREEN`, `domain_state = CHAIN_COMPLETE` | Her şeye RED diyen bir kapının bu testi geçemeyeceğini |

**F1+F2 çifti pazarlık konusu değildir.** F1 tek başına, `BULGU`nun uyardığı
yanılsamayı üretir: kapı *"eksik halkayı yakalıyorum"* der, ama yakaladığı şey
**beyan listesindeki** bir boşluktur, **gerçeklikteki** değil.

**F5/F6'nın pozitif tarafı da zorunludur.** Bir kuralın `PASS` **kalması**
kusur değil, bu kapının varlık sebebinin kanıtıdır. Emsal:
`ecad-varlik-kalifikasyonu:278-282`.

## 6. Kabul kriterleri

- [ ] Kapı, [[../konnektor/SKILL|konnektor]] `connector_spec.json`'ı
      yayımladıktan **sonra** ve [[../sematik-erc/SKILL|sematik-erc]] pinout'u
      dondurmadan **önce** koştu.
- [ ] `coverage.eligible` **`interface_contract.json`'dan** türedi;
      `eligible_derivation.required_links` her zorunlu bağı ve kenar kotasını
      adıyla taşıyor.
- [ ] `scanned + excluded == eligible`, `violations <= scanned`, `scanned > 0`;
      üç blokta birden (`coverage`, `contract_coverage`, `provenance_coverage`).
- [ ] Zincirdeki **her** halkanın `medium`'u var; hiçbiri `null` değil.
- [ ] Her `mating` kenarı için `pin_to_pin[]` **beyan edilmiş** (R10) ve net
      adları **birebir** örtüşüyor (R11) — alias tablosu **yok**.
- [ ] Her `internal` kenar `straight` / `reversed` / `custom` **beyan etmiş**;
      `custom` olanlar boş olmayan çizelge taşıyor.
- [ ] R12 **hiçbir** kenarda ray/sinyal çarpışması bulmadı **ve** `scanned > 0`
      ile buldu — pin sınıfları beyan edilmediği için "temiz" **sayılmadı**.
- [ ] Karşılaştırılan her alanın provenansı **tam** (`document_id` + `revision`
      + `locator` + `accessed` + `snapshot_sha256`); eksikse verdict `GREEN`
      **değil**.
- [ ] `basis: derived` / `local_policy` / `vendor_page` taşıyan hiçbir alan
      imza açmadı.
- [ ] Her `excluded` kenar **açık gerekçe** taşıyor; `unjustified_exclusions`
      ve `unknown_exclusions` **boş**.
- [ ] `gate.verdict` kapalı sözlükten; `domain_state` **ayrı alanda** ve
      `gate.verdict`'e sızmamış.
- [ ] `writes_board: false`, `signoff: false`, `assurance_loop: [L1, L2]` —
      `L3` **listelenmemiş**.
- [ ] Kanıtlanamayan her satır `l3_rule_universe.json`'a **limitiyle** yazıldı
      ve `owner_skill` **bu skill**; `l3-bringup-karakterizasyon` **değil**.
- [ ] F1–F10 **ve** negatif taraf koştu; `--self-test` exit 0.

## İlgili

[[../konnektor/SKILL|konnektor]] ·
[[../ecad-varlik-kalifikasyonu/SKILL|ecad-varlik-kalifikasyonu]] ·
[[../sematik-erc/SKILL|sematik-erc]] ·
[[../mekanik-dxf/SKILL|mekanik-dxf]] ·
[[../bom-lifecycle/SKILL|bom-lifecycle]] ·
[[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../pcb-orchestrator/SKILL|orchestrator]] ·
[[../_shared/coverage-gate.md]] · [[../_shared/rule-conflicts.md]]

## Araç kancaları (2026-08-12)

- **`interconnect-mating`** — Bu skill'in kendi aracı
  (`tools/scripts/interconnect_mating.py`). Paydayı `interface_contract.json`'dan,
  payı `interconnect_chain.json`'dan okur; iki dosya **ayrı** olmadan koşmaz.
  `--exclude-link <id>:<gerekçe>` açık dışlama, `--bringup-mate-cycles` R20'nin
  bütçesi (**varsayılanı yoktur**), `--prior` bayatlama karşılaştırması,
  `--input-json` girdiyi JSON'dan okuma, `--json` **her zaman yazar**,
  `--receipt` + `--now` makbuz (tekrar üretilebilir).
- **Makbuz sözleşmesi — çelişki ÇÖZÜLMEDİ, KAYDEDİLDİ.** Depoda iki
  gate-receipt sözleşmesi var ve **çelişiyorlar** (ölçüldü 2026-08-10):
  `pcb-orchestrator/references/gate_receipt.schema.json` `metric_definition`
  isteyip `additionalProperties: false` derken, `tools/scripts/gate_receipt.py`
  `definition` + `sampling_step` istiyor. İkisi tek belgede sağlanamaz. Bu kapı
  [[../ecad-varlik-kalifikasyonu/SKILL|ecad-varlik-kalifikasyonu]] ile **aynı
  seçimi** yapar ve **doğrulayıcıyı** izler: doğrulayıcının reddettiği bir
  makbuz hiçbir kapıyı açamaz. Ölçülmüş bir incelik: `parsed_metrics` ile
  `coverage.per_metric` **anahtarları** paylaşır, **değer şekilleri farklıdır** —
  ilki bir sayı, ikincisi tam bir `eligible/scanned/excluded/violations`
  nesnesidir; sayı yazmak doğrulayıcıda `INVALID_COVERAGE` üretir. Regresyon
  bunu **gerçek doğrulayıcıyı koşturarak** kanıtlar
  (`GateReceiptTest.test_the_real_validator_accepts_a_green_receipt`).
- **`konnektor`** — Seçilmiş konnektörün datasheet nitelikleri (`pitch`,
  `positions`, `datasheet_cycles`, `contact_finish`) `connector_spec.json`'dan
  gelir ve **yalnız okunur**. Derating, akım ve landing pattern kararı onda kalır.
- **`cad-asset-qualification`** — **Kardeş kapı**, üstü değil: o tek MPN'in
  sembol+footprint ↔ belge kimliğini ölçer, bu kapı **parça ↔ parça** eksenini.
  Bu kapı ondan **önce** koşar, çünkü ECAD kapısı yanlış MPN'i mükemmel şekilde
  kalifiye eder.
- **`bom-lifecycle`** — Kablo/adaptör MPN'i, stok ve lifecycle onun; bu kapı
  yalnız *"seçilen zincirde duruyor mu"* diye sorar. BOM satırı yoksa `CONFIRM`
  ve L2 kenarı.
- **`gate-receipt`** — `interconnect_gate_receipt.json`'ı doğrular ve kapıyı
  kapatır. `assurance_level == "A"` olan satırlarda fault-injection makbuzu
  **zorunludur** (`gate_receipt.py:502-642`).
