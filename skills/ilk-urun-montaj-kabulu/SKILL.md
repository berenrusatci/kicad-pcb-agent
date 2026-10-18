---
name: ilk-urun-montaj-kabulu
description: Released Gerber/BOM/CPL paketinden üretilen ilk kartın as-built konformansı; montajcı AOI/X-ray/MSL/depanel kaydını released paketle uzlaştırır. Seri montaj kararının tek sahibi — GREEN olmadan seri üretim açılmaz.
version: 1.0.0
last-reviewed: 2026-08-10
phase: "Faz 5A — üretim dönüş kapısı (4c'den sonra, D1 L3 bring-up döngüsünden önce)"
assurance_loop: [L2, L3]
evidence_origin: [PHYSICAL_MEASUREMENT, DESIGN_ARTIFACT, TOOL_REPORT]
signoff: false
inputs:
  - {artifact: fai_evidence.json, schema: references/fai_evidence.schema.json, required: true}
  - {artifact: gerbers.zip, schema: null, required: true}
  - {artifact: cpl.csv, schema: ../cpl-assembly/references/cpl-columns.md, required: true}
  - {artifact: bom.csv, schema: null, required: true}
  - {artifact: fab_receipt.json, schema: null, required: true}
  - {artifact: rotation_map.json, schema: ../cpl-assembly/references/rotation_map.schema.json, required: true}
  - {artifact: lifecycle_report.json, schema: ../bom-lifecycle/references/lifecycle_report.schema.json, required: true}
outputs:
  - {artifact: first_article_report.json, schema: ../../tools/schemas/first_article_report.schema.json}
  - {artifact: assembly_acceptance_receipt.json, schema: ../../tools/schemas/gate_receipt.schema.json}
  - {artifact: manufacturing_deviation_log.json, schema: null}
writes_board: false
tools: [first-article, production-outputs, production-contracts, gerber-dfm, bom-lifecycle, gate-receipt]
---

# SKILL: İlk Ürün / Montaj Kabulü (As-Built Kapısı)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

Bu skill **released pakete karşı üretilmiş ilk kartın as-built
konformansının** ve buna bağlı **seri montaj açma/bloke etme** kararının tek
sahibidir. Sorusu "dosyalar üretildi mi?" değildir —
[[../cpl-assembly/SKILL|cpl-assembly]] onu zaten cevaplıyor — sorusu
**"gerçekten monte edilen kart, released paketle uyuşuyor mu?"**dur.

## 0. Ne zaman çağrılır

- **Faz 5A** — kart fabdan/montajcıdan döndükten sonra, ilk üretilen birim
  üzerinde. Faz 4c ([[../cpl-assembly/SKILL|cpl-assembly]]) çıkış kapısı
  GREEN olmadan bu skill'in girdisi (released paket) mevcut değildir.
- **D1'in L3 bring-up döngüsünden ÖNCE**
  ([[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]]):
  elektriksel karakterizasyon, önce **doğru monte edilmiş** bir birime ihtiyaç
  duyar. Ters takılmış bir regülatörden alınan ripple ölçümü tasarımı
  değil montajı ölçer ve bunu söylemez.
- `writes_board: false` — board yazma kilidi almaz, board'a dokunmaz.
- **Kapı varsayılan olarak zorunludur ve bloke edicidir.** FAI'nin
  yapılmamış olması `NO_COVERAGE`'dır; sessiz geçiş değildir (§Açık soru).

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| `board_revision` / `hash`, `lot` / `work_order`, `unit_serial` | iş emri + montajcı | kimliksiz kanıt → `NO_COVERAGE` |
| `released_package_hash` | Faz 4c çıktısı (`gerbers.zip` + `cpl.csv` + `bom.csv`) | `UNBOUND` → `CONFIRM` |
| montajcının **hangi paketten** ürettiği digest | montajcı kaydı | eşleşmezse `RED`, seri bloke |
| refdes evreni (`eligible`) | released CPL/BOM | evren yoksa kapı koşamaz → `NO_COVERAGE` |
| `presence` / `value` / `orientation` / `package` | AOI + montajcı kaydı | kayıtsız refdes `excluded`'a katlanır **ve** ihlal olarak yükselir (§3f) |
| AOI/X-ray/manual kayıt locator'ı | evidence ID + tür + hash + zaman | kanıt bloğu eksikse o satır taranmamış sayılır |
| gizli lehim (BGA/QFN/LGA) sonucu | X-ray | evren boş değilse `NO_COVERAGE`/`CONFIRM` |
| MSL / floor-life handling kaydı | montajcı | `msl_level > 1` satırlarda `NO_COVERAGE` |
| depanel denemesi | montajcı | `panelised: true` iken kayıt yoksa `NO_COVERAGE` |
| fab DFM kabulü | fab resmî raporu | "siparişi aldılar" kabul değildir → `NO_COVERAGE` |
| deviation / disposition / approver | üretim sapma kaydı | onaylayanı olmayan disposition disposition değildir → `RED` |
| parent receipt hash'leri | Faz 4b/4c receipt'leri | zincir kopuksa `child_receipts` boş, `CONFIRM` |

Girdi sözleşmesi: [`references/fai_evidence.schema.json`](references/fai_evidence.schema.json)
Çıktı sözleşmesi: [`../../tools/schemas/first_article_report.schema.json`](../../tools/schemas/first_article_report.schema.json)

## 2. Adımlar

1. **Kimlik bağla.** `identity.released_package_sha256` (montajcının ürettiğini
   söylediği paket) ↔ `released_package.sha256` (gerçekten yayımlanan paket).
   - Revizyon **adı** farklıysa → `STALE`. Kanıt iyi olabilir; **başka bir
     kartın** kanıtıdır.
   - Revizyon aynı, **bayt** farklıysa → `RED`. Aynı ad altında sessiz ikame
     yapılmış demektir ve aşağıda hiçbir kontrol bunu görmez.
   - `--verify-package` ile diskteki paket yeniden hash'lenir.
2. **Evreni böl.** Released CPL refdes kümesi `eligible`'dır. Her satır ya
   *taranmış* (kullanılabilir bir evidence bloğu var) ya *gerekçeli excluded*
   olmalıdır. Üçüncü bir kategori yoktur (§3f).
3. **As-built konformansı:** `presence` (DNP dahil), `value`, `package`,
   `orientation`. Rotasyon karşılaştırması released CPL açısına karşıdır —
   yani fab rotasyon ofseti **zaten uygulanmış** açıya karşı; ofsetin sahibi
   [[../cpl-assembly/SKILL|cpl-assembly]]'dir ve burada yeniden hesaplanmaz.
4. **Gizli lehim evreni:** `hidden_joint: true` satırlar için X-ray. Optik
   kanıt bu bağlantılara **fiziksel olarak ulaşmaz**; bir BGA'da AOI PASS,
   altındaki toplar hakkında hiçbir şey söylemez.
5. **MSL handling:** montajcının *gerçek* kaydı, `lifecycle_report.json`'daki
   `msl_level`'a **karşı** doğrulanır. Seviyeyi bu skill tanımlamaz
   ([[../bom-lifecycle/SKILL|bom-lifecycle]] §3a kanonik sahiptir); floor life
   aşılmış ve bake kaydı yoksa `RED`.
6. **Depanel denemesi** ve **fab DFM kabulü** kayıtları.
7. **Deviation log:** her sapma `disposition` + `approver` + `resolved`
   taşımalı; taşımıyorsa açık sapmadır.
8. **Verdict + receipt:** `first_article_report.json`,
   `assembly_acceptance_receipt.json` (gate-receipt/v1 **fragment**) ve
   `manufacturing_deviation_log.json` yazılır. Kapıyı kapatan **araç değil
   validator**dür: `tools/scripts/gate_receipt.py`.
9. **RED/STALE'de L2 geri beslemesi**, sorun sınıfına göre yönlendirilir:
   oryantasyon/DNP → `cpl-assembly`, süreç/panel/void kuralı → `dfm`,
   MSL/lifecycle → `bom-lifecycle`, footprint/land pattern → `cad-assets`.
   Rapordaki `feedback_edges` bu yönlendirmedir; bu skill upstream artifact'ı
   **düzeltmez**, yalnız adresler.

## 3. Nüanslar — bilinen tuzaklar

### (a) Bu kapı görüntüye bakmaz — kural, tercih değil
Araç **yapılandırılmış** bir sonuç (AOI/X-ray/insan kararı) + hash'li bir
evidence locator tüketir. Locator'ı **açmaz**, piksel yorumlamaz. "Model AOI
görüntüsüne baktı, iyi görünüyordu" tam olarak bu kapının kapattığı uydurma
kanıt yoludur: kimsenin yeniden türetemediği bir kabul, kabul değildir.
`test_first_article.py::test_no_image_is_ever_opened` bunu docstring'e değil
**kaynağa** karşı doğrular.

### (b) Fotoğraf bir kabul kriteri değildir
`PHOTO`/`MANUAL` kaydı `signoff_standard` taşımıyorsa sonuç `SCREEN_ONLY`'dir:
birisi baktı, kimse "iyi"nin ne olduğunu söylemedi. `SCREEN_ONLY` seri üretimi
açmaz. Standart adı yazıldığı anda (ör. `IPC-A-610 Class 2`) aynı kayıt kabul
üretebilir.

### (c) Boş evren ≠ kapsam boşluğu
"Dört BGA'ya karşı sıfır X-ray kaydı" bir boşluktur. "Sıfır BGA'ya karşı sıfır
X-ray kaydı" released paketin **bir gerçeğidir**. İkincisini `NO_COVERAGE`
saymak, BGA'sız her kartta kapıyı kalıcı olarak kapatır ve **hiç açılmayan bir
kapı insanlara atlamayı öğretir.** Ayrım yalnız şu yüzden güvenlidir: boş evren
her zaman **released paketten** (`refdes_universe`, `panelised`) türetilir,
kanıttan değil. Kanıt kendi paydasını küçültemez. Her bu tür dışlama
`coverage_ledger.excluded_checks` içinde gerekçesiyle yazılır.

### (d) `panelised: null` "panel yok" demek değildir
Kimse söylememiştir; depanel sınıfı **cevapsızdır** ve kapı `CONFIRM`'de
kalır. Panelsizlik bir beyandır, bir varsayım değil. Panel/depanel kararının
kendisi bir **yerleşim kısıtıdır** ve Faz 3'e aittir
([[../cpl-assembly/SKILL|cpl-assembly]] §3b, [[../dfm/SKILL|dfm]] §MLCC flex
crack).

### (e) Tek bir eski AOI dosyası bütün kabulü yanlış karta bağlar
Refdes'ler hâlâ durduğu için hiçbir şey fark etmez. `evidence.board_revision`
alanı birimin revizyonundan farklıysa kapı `STALE`'dir — kanıt kalitesi iyi
olsa bile.

### (f) Kayıtsız refdes: katlanır **ve** yükselir, asla yalnızca katlanır
`scanned + excluded == eligible` zorunlu olduğu için, ne kaydı ne dışlaması
olan bir refdes kaybolamaz. Araç onu *"no inspection record and no exclusion
was supplied"* gerekçesiyle `excluded`'a katlar, `coverage_ledger`'da adıyla
listeler **ve** `as_built_presence` içinde `UNACCOUNTED_REFDES` ihlali olarak
yükseltir. Katlama aritmetiği dürüst tutar; eşzamanlı ihlal katlamanın bir
*karar* gibi okunmasını engeller. Biri diğeri olmadan sessiz geçiştir.

### (g) `violations` yalnız **taranmış** satırları sayar
`violations <= scanned` kuralı bunu gerektirir. Kayıtsız refdes bir
`blocker`'dır; ölçülmüş bir şeyin ihlali değildir. Sayıyı şişirmek raporu
`INVALID_COVERAGE` yapardı.

### (h) Kanıt kaydı eksikse `NO_COVERAGE`, yorum gerektiriyorsa `CONFIRM`
İkisi de bloke eder, ama aynı şey değildir. Hiç kayıt yok → ölçülmedi
(`NO_COVERAGE`). Kayıt var, kabul standardı adlandırılmamış veya sonuç
`REQUIRES_INTERPRETATION` → `CONFIRM`. Rapor ikisini birden yaşadığında
öncelik `gate_receipt.effective_status` sırasından gelir
(`STALE > NO_COVERAGE > RED > CONFIRM > SCREEN_ONLY`) ve **sıra yalnız hangi
gerekçenin önce yazıldığını** belirler; beş verdict de seri üretimi eşit
şekilde bloke eder.

### (i) Bu skill'in `authority` ekseni `physical_acceptance`, `signoff` değil
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] §"Seviye tek eksen değildir"
taksonomisinde: bu kapı bir **build**'i kabul eder, bir **tasarım kuralını**
imzalamaz. Frontmatter'daki `signoff: false` bunu söyler; §4'teki tablo neyi
kapatamadığını sayar.

### (j) `identity.board_hash` boş bırakılırsa Faz 5B hiç açılamaz

Şema `board_hash`'i nullable tutar (montajcı kaydı onu taşımayabilir), fakat
[[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] her ölçüm
kaydını **dört alanla birden** (`board_revision` + `board_hash` + `serial` +
`lot`) kabul edilmiş birime bağlar; `board_hash` null gelirse L3 kampanyası
"hiçbir belirli kart hakkında kanıt" diyerek **exit 2** ile durur.

Bu bir hata değil, sözleşmedir: kartın kimliği yoksa o kart hakkında ölçüm de
yoktur. Pratik sonuç — **FAI kanıtını yazan taraf `board_hash`'i doldurmak
zorundadır**; kaynağı montajcı değil, released paketin bağlı olduğu kanonik
board dosyasının digest'idir ([[../cpl-assembly/SKILL|cpl-assembly]] 4c
çıktısı).

> **Açık soru / varsayım.** `board_hash`'i FAI akışında kimin doldurduğu
> (montajcı mı, released paketi üreten taraf mı) kararlaştırılmadı. Muhafazakâr
> varsayım: **released paketi üreten taraf doldurur**, çünkü montajcının
> gördüğü şey Gerber'dir, `.kicad_pcb` değil. Karar değişene kadar null bir
> `board_hash` GREEN üretebilir ama **5B'yi açamaz** — ve bu, sessizce
> geçmemesi için burada yazılıdır.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gizli lehim kusur evreni (X-ray yoksa) | D | Optik kanıt BGA/QFN topuna ulaşmaz → `NO_COVERAGE`/`CONFIRM` |
| Lehim metalurjisi / intermetalik büyüme | D | FAI'den türetilemez |
| Uzun dönem güvenilirlik, termal çevrim ömrü | D | Tek birimin ilk ürün kontrolü qualification değildir |
| Fotoğrafın tek başına kabul ettiği hiçbir şey | — | Signoff standardı yoksa `SCREEN_ONLY` |
| Elektriksel fonksiyon / karakterizasyon | D | [[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] |
| Void oranı eşiği / süreç kuralı | — | Eşiğin sahibi [[../dfm/SKILL|dfm]]; burada yalnız vendor verdict'i raporlanır |
| `msl_level` değerinin doğruluğu | C | Sahibi [[../bom-lifecycle/SKILL|bom-lifecycle]] §3a; burada *handling kaydı* ona karşı doğrulanır |
| Seri partinin tamamı | — | FAI **bir** birimdir; parti istatistiği bu kapının kanıtı değildir |

> Bu skill'in `GREEN`'i bir **as-built konformans** beyanıdır, bir
> **güvenilirlik qualification'ı değildir.** İkisini karıştırmak, kimsenin
> ölçmediği bir sertifikayı bir montaj kontrolünden üretmek olur.

### Çatışma sınırları (HARD RULE)

| Konu | Kanonik sahip | Bu skill ne yapar |
|---|---|---|
| Released paket, pick point, fab rotasyon haritası, DNP/rotasyon parity | [[../cpl-assembly/SKILL|cpl-assembly]] | CPL'yi **düzenlemez**; uyuşmazlığı RED + L2 kenarı olarak bildirir |
| Süreç/tasarım kuralı, void ve stres eşikleri | [[../dfm/SKILL|dfm]] | DFM eşiği **yazmaz**; vendor'ın hangi standarda göre karar verdiğini kaydeder |
| `msl_level`, floor life, lifecycle metadata | [[../bom-lifecycle/SKILL|bom-lifecycle]] | Seviyeyi tanımlamaz; montajcının **fiili handling kaydını** ona karşı doğrular |
| Fab öncesi tasarım-artifact kontrolü | [[../design-checker/SKILL|design-checker]] | Aynı refdes evreni, fabın **öteki tarafı**: bu skill as-built kontrolcüsüdür |

> **Açık soru / varsayım (2026-08-10, kapatılmadı)**
>
> **(1) Seri üretim gerçekten bu ajanın release hedefi mi, yoksa yalnız
> prototip/NPI mi?** Kaynak rapor bunu açık bıraktı ve cevap bilinmeden bu
> kapının "seri üretimi bloke et" yetkisinin **ZORUNLU mu OPSİYONEL mi**
> olduğu belirsizdir. Burada **muhafazakâr yol** seçildi: kapı
> **varsayılan olarak zorunlu ve bloke edicidir**; eksik bir FAI
> `NO_COVERAGE`'dır, asla sessiz geçiş değildir.
> Proje gerçekten yalnız prototip ise kapı kapsam dışı **ilan edilebilir** —
> ama bu ilan `first_article.py --declare-out-of-scope REASON` ile
> **açıkça kaydedilmiş bir `excluded`** olmak zorundadır (gerekçe raporda
> `coverage_ledger.excluded_checks` içine yazılır) ve **verdict yine
> `NO_COVERAGE` kalır.** Kapsam daraltarak `GREEN` üreten bir bayrak yoktur
> ve eklenmemelidir.
>
> **(2) "Release" hangi release?** Terim akışta en az üç şeye kayıyor:
> *Gerber release* (Faz 4c'de paketin yayımlanması), *accepted-first-article
> release* (bu kapı) ve *product release* (ürün onayı). Bu skill kendi
> anlamını **"accepted first article"** olarak sabitler ve raporda
> `gate.release_meaning` alanında bunu yazar. Diğer iki anlamı bu skill
> **kapatmaz**; bir okuyucunun kelimeyi ödünç alıp bu kapının hiç yapmadığı
> bir iddiaya taşımaması için alan makine-okur tutuldu.

## 5. Doğrulama

```bash
# FAI kanıt kaydından as-built kapısı + üç çıktı
python3 tools/scripts/production/first_article.py fai_evidence.json \
  --json first_article_report.json \
  --receipt-out assembly_acceptance_receipt.json \
  --deviation-log-out manufacturing_deviation_log.json \
  --verify-package production/gerbers.zip

# KAPIYI KAPATAN BU: aracın kendisi kendi receipt'ini doğrulamaz
python3 tools/scripts/gate_receipt.py assembly_acceptance_receipt.json --root .

# Araç iç tutarlılığı (opt-in, stdout'a hiçbir şey yazmaz)
python3 tools/scripts/production/first_article.py --self-test
python3 -m unittest tests.test_first_article -v   # cwd: pcb-agent/tools
```

Fault-injection (üçü de **koşulmuş olmalı**, testte ve gerçek kayıtta):

1. Tek bir refdes'in `rotation_deg` değerini 90° kaydır → verdict `GREEN` iken
   `RED` olmalı, `feedback_edges` `cpl-assembly`'yi göstermeli.
2. `identity.released_package_sha256`'yı değiştir → `RED`, seri bloke.
3. `identity.board_revision`'ı bir önceki spin'e çevir → `STALE` (RED değil).

Kapanmıyorsa kapı boştur: `by_check.<check>.status` **ve** `scanned` alanına
bak, yalnız `metrics.<check>` sayısına değil — boş bir kontrol sıfır ihlal
bildirir.

## 6. Kabul kriterleri

- [ ] Faz 4c GREEN ve released paket (`gerbers.zip` + `cpl.csv` + `bom.csv` +
      `fab_receipt.json`) mevcut; bu kapı ondan **sonra** koştu.
- [ ] `board_revision`/`hash`, `lot`/`work_order`, `unit_serial` raporda dolu.
- [ ] Montajcının ürettiğini beyan ettiği paket digest'i released paket
      digest'iyle **birebir** eşleşti (`package_binding: BOUND`).
- [ ] Refdes evreni released CPL'den alındı; kanıttan türetilmedi.
- [ ] `presence` / `value` / `package` / `orientation` sonuçları yazıldı;
      rotasyon released CPL açısına karşı karşılaştırıldı (ofset yeniden
      hesaplanmadı).
- [ ] Her `hidden_joint` refdes için X-ray verdict'i var; yoksa satır
      `NO_COVERAGE`/`CONFIRM` ve `GREEN` **verilmedi**.
- [ ] `msl_level > 1` her satır için montajcı handling kaydı var; floor life
      aşımı varsa bake kaydı var.
- [ ] `panelised` **beyan edildi**; `true` ise depanel denemesi kaydı var.
- [ ] Fab DFM kabulü resmî kayıttan okundu; "siparişi aldılar" kabul sayılmadı.
- [ ] Her deviation `disposition` + **adı yazılı** `approver` + `resolved`
      taşıyor; açık sapma yok.
- [ ] Her evidence kaydı ID + tür + hash + zaman taşıyor; locator **açılmadı**,
      hiçbir kabul görüntüden türetilmedi.
- [ ] `PHOTO`/`MANUAL` kaydı `signoff_standard` taşımıyorsa sonuç
      `SCREEN_ONLY` bırakıldı, `GREEN`'e yuvarlanmadı.
- [ ] Kayıtsız refdes hem `coverage_ledger`'a katlandı hem
      `UNACCOUNTED_REFDES` ihlali olarak yükseldi.
- [ ] Kapsam dışı ilan edildiyse gerekçesiyle kaydedildi ve verdict
      `NO_COVERAGE` kaldı.
- [ ] `assembly_acceptance_receipt.json` `gate_receipt.py` ile doğrulandı;
      araç kendi receipt'ini onaylamadı.
- [ ] RED/STALE'de L2 geri besleme kenarı doğru sahibe açıldı ve seri üretim
      **bloke** edildi.
- [ ] coverage: `scanned + excluded == eligible`, `violations <= scanned`;
      `scanned = 0` ise verdict `NO_COVERAGE`.

## İlgili

[[../cpl-assembly/SKILL|cpl-assembly]] §4 (bu skill onun "kanıtlayamaz"
tablosunun tüketicisidir) · [[../dfm/SKILL|dfm]] ·
[[../bom-lifecycle/SKILL|bom-lifecycle]] §3a ·
[[../design-checker/SKILL|design-checker]] ·
[[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../pcb-orchestrator/SKILL|orchestrator]]

## Araç kancaları

> Ad listesine eklemek tek başına tetiklenebilirlik sağlamaz; aşağıdaki her
> satır *ne zaman* çağrılacağını söyler.

- **`first-article`** — Kart montajdan döndüğünde, seri üretim kararından
  önce. Yapılandırılmış FAI kanıt kaydını released paketle uzlaştırır; üç
  çıktıyı yazar. Görüntü okumaz, güvenilirlik yargılamaz, CPL'ye dokunmaz.
- **`gate-receipt`** — `assembly_acceptance_receipt.json`'u kapatan tek şey.
  Aracın kendi verdict'i öneridir; kapıyı validator kapatır.
- **`production-outputs`** — Bu kapının girdisi olan released paketi üreten
  fail-closed kapı (Faz 4c). Buradaki `RED` çoğu zaman oraya geri besler.
- **`production-contracts`** — CPL/montaj sözleşmesi: pick point, fab rotasyon
  haritası, oryantasyon, panelizasyon. As-built uyuşmazlığında released
  tarafın ne dediğini okumak için.
- **`gerber-dfm`** — Fab uyuşmazlığı iddiasında, exported gerber'dan bağımsız
  kanıt; sembolik DRC'den farklı bir tanık.
- **`bom-lifecycle`** — MSL/floor-life metadata'sının kanonik kaynağı;
  montajcı kaydı buna **karşı** doğrulanır, bunun yerine geçmez.
