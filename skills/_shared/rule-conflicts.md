# Kural çatışmaları — hangi kural hangisini yener

> İki doğru kuralın aynı geometride çeliştiği yerler. Bunlar "birini seç"
> değil, **sıralı önceliktir**; çatışma her seferinde ölçülmüş gerekçesiyle
> `acik_maddeler.md`'ye yazılır.

---

## 1. ESD dönüş yolu vs via-in-pad DFM kuralı

- **DFM kuralı:** pad içinde dolgusuz via olmasın (reflow'da lehim bareline
  emilir → zayıf/boş joint).
- **EMC kuralı:** TVS GND'si düzleme en kısa yoldan insin (uzun GND izi
  30 A / 1 ns'de yüzlerce volt üretir → koruma çalışmaz).

**Karar: ESD dönüş yolu kazanır.** Via yerinde kalır; fab notuna
**IPC-4761 Type VII** (dolgu + kapak) yazılır. Via'yı pad dışına taşımak clamp
dönüş endüktansını artırır — yani DFM'i düzeltirken korumayı bozar.

Ölçülmüş örnek: USBLC6 GND pini (U1.2), 0.95 mm pitch + iki yanda veri çifti.
Via 0.4 mm'ye küçültülse bile 0.2 mm clearance ile pad dışına çıkacak yer yok.

Sahipler: [[../dfm/SKILL|dfm]] §Via-in-pad · [[../emc-esd/SKILL|emc-esd]] §6

---

## 2. Decoupling mesafesi vs referans düzlemi bütünlüğü

`≤1.5 mm` decoupling mesafesi bir **placement ön-eleğidir** (`SCREEN_ONLY`),
elektriksel PASS değildir. Üstün kural: pad→via→plane **döngü alanı/endüktansı**
ve rail `ZPDN(f)`.

Mesafeyi sağlamak için diferansiyel çiftin üzerinden geçmek veya referans
düzlemini yarmak gerekiyorsa **kural uygulanmaz**; kapsam dışı olduğu ölçümle
yazılır.

Ölçülmüş örnek: U1 (USBLC6 **ESD dizisi**) VBUS pini 4.307 mm "ihlal" diye açık
madde yazılmıştı. ESD dizisinin VBUS pini bir besleme değil **clamp
referansıdır**; orada önemli olan mesafe değil GND dönüş endüktansıdır.
Courtyard kısıtıyla ulaşılabilecek en kısa mesafe 2.11 mm ölçüldü ve oraya
taşımak daha ağır bir kuralı çiğniyordu.

Sahip: [[../sematik-erc/SKILL|sematik-erc]] §Kural kapsamını sorgula

---

## 3. Teardrop: üretim payı vs yüksek hız empedansı

- **Üretim kuralı:** dar annular ring / ince iz / flex bükülme bölgesinde
  teardrop **gerekir** (drill wander + registration toleransı).
- **SI kuralı:** teardrop via girişinde empedansı **düşürür** (bakır artışı +
  çiftte filetoların yakınsaması → coupled taper), dönüş kaybı büyür.

**Karar: net sınıfına göre ayrış.** Çok Gb/s diferansiyel çiftlerde varsayılan
**kapalı**; üretim payı gereken yerlerde (dar annular ring, ince iz, flex)
açık. "Class 3 ⇒ teardrop zorunlu" **yanlıştır** (yüksek güvenilirlikli OpenVPX
backplane'ler teardrop şart koşmaz).

Sahip: [[../dfm/SKILL|dfm]] §Teardrop

---

## 4. Maske barajı vs akım kapasitesi

Pin-arası kanaldan geçen izde genişliği **akım değil maske barajı** belirler.
Önce barajı çöz, sonra IPC-2221 ile akımın hâlâ yettiğini doğrula. Yetmiyorsa
değişen şey iz değil **paket/footprint**'tir.

Sahip: [[../highspeed-routing/SKILL|highspeed-routing]] §Escape geometrisi

---

## 5. Maske barajı fab minimumu — karar bandı (çakışma C5)

Konsolidasyon öncesi aynı dosyada iki değer vardı ("tipik 0.20–0.25 mm" ve
"tipik 0.20 mm") ve çalışılmış örnek yalnız 0.20 okumasıyla geçiyordu.

**Karar:**

| Baraj | Verdict |
|---|---|
| `< 0.20 mm` | **RED** — hiçbir fab profilinde kabul edilmez |
| `0.20 mm ≤ baraj < 0.25 mm` | **CONFIRM** — fab profili olmadan PASS değil |
| `≥ 0.25 mm` | `SCREEN_ONLY` PASS (fab profili varsa profil değeri kazanır) |

Fab profili (`fab_capabilities.json → soldermask.min_bridge_mm`) her zaman bu
yerel banda **üstün gelir**. Yerel bant `evidence_origin=LOCAL_POLICY`.

Sahip: [[../highspeed-routing/SKILL|highspeed-routing]] · ölçüm: `tools/scripts/board/mask_dam.py`

---

## 6. `100 nF` referans geçişi (çakışma C6)

GND→PWR geçişinde bitişik `100 nF` **yalnız yaygın yerleşim pratiğidir** ve
`SCREEN_ONLY`'dir; dönüş-yolu PASS kriteri **değildir**.

Sıra: (1) referans değişimini mümkünse kaldır; (2) zorunluysa kapasitör
MPN / mounting ESL / yerleşim ve gerekli frekans bandını **modelle**.

**Tek kanonik etiket:** `evidence_origin=LOCAL_POLICY`,
`assurance_loop=L1+L3`, `status=SCREEN_ONLY`.
(Konsolidasyon öncesi aynı kural üç dosyada iki farklı `assurance_loop` ile
yazılıydı — L1+L3 ve L0+L3. L1+L3 seçildi: bu bir topoloji/stackup kararıdır,
board yazımı sonrası otomatik ölçülen bir L0 metriği değildir.)

Sahip: [[../empedans-stackup/SKILL|empedans-stackup]] §Referans düzlemi

---

## 7. Test noktası vs yüksek hız stub'ı

Yüksek hızlı hatta TP stub'ı **konulmaz** (empedans süreksizliği). Çare mikro-TP
veya prob-pad'dir; kapsama hedefi bu netlerde düşer ve `uncovered_reason` ile
yazılır. "Güç + debug %100" hedefi **tüm kart için %100 DFT coverage** diye
genellenemez.

Sahipler: [[../dft-testpoints/SKILL|dft-testpoints]] · [[../highspeed-routing/SKILL|highspeed-routing]]

---

## 8. `.kicad_dru` kural önceliği — iki kaynak birbirinin tersini söylüyordu

Aynı nesneye iki kural uyduğunda hangisi geçerlidir?

- **ProjectE-1 skill metni:** "sonra gelen kural kazanır" **ve** "ilk eşleşme
  uygulanır" — cümle kendi içinde çelişiyordu.
- **`projectE-2archive/pcb-tool-v2`** (`DOCS/03_Design_Rules.md:38-46`,
  `HAFIZA/Hafiza_Defteri.md:123-144`): "**İLK EŞLEŞEN KURAL KAZANIR**, spesifik
  kurallar genelden ÖNCE yazılmalı" — üstelik bunu, bizim ifademizin
  *doğrulanmış düzeltmesi* olarak kaydetmiş.

**Karar: son eşleşen kural kazanır. Genel kurallar başa, spesifik kurallar
sona.** Doküman kararı değil, ölçüm:

| `.kicad_dru` sırası | Sonuç | Kazanan |
|---|---|---|
| genel (`≥0.25 mm`) → spesifik (`NET_A ≥0.10 mm`) | 0 ihlal | spesifik = **sonuncu** |
| spesifik → genel | 1 ihlal, gerekçe `GENEL_gevsek` | genel = **sonuncu** |

Kart: 0.15 mm kenar-kenar boşluklu iki iz, KiCad 10.0.5. İlk-eşleşme hipotezi
çürüktür — geçerli olsaydı ilk satır ihlal verirdi.

Arşivin "düzeltmesi" **yanlıştır** ve tehlikelidir: o sıraya geçilirse enjekte
edilen her spesifik kural genel kural tarafından sessizce ezilir, DRC yine
"temiz" der. Bu, kapının sahte PASS verdiği tam da o sınıftır.

Regresyon: `tools/tests/test_dru_precedence.py` (iki sırayı da koşar).

Sahip: [[../dfm/SKILL|dfm]] §DRC kural enjeksiyonu

---

## 9. Fiziksel/as-built kapanış vs tasarım-zamanı limit sahipliği

2026-08-10'da eklenen üç skill (`ecad-varlik-kalifikasyonu`,
`ilk-urun-montaj-kabulu`, `l3-bringup-karakterizasyon`) ve proje
katmanındaki `sistem-arayuz-sozlesmesi` mevcut sahiplerin **üstüne** kapı
koyar; onların kararını **devralmaz**. Sınır tek cümlelik:

| Üretici | Ürettiği karar | Tüketici | İhlal sayılan davranış |
|---|---|---|---|
| [[../bom-lifecycle/SKILL\|bom-lifecycle]] | seçilmiş MPN + lifecycle | `ecad-varlik-kalifikasyonu` | ECAD skill'inin **parça seçmesi** |
| `ecad-varlik-kalifikasyonu` | nitelenmiş symbol/footprint receipt'i | [[../sematik-erc/SKILL\|sematik-erc]], sonra [[../dfm/SKILL\|dfm]] | ECAD skill'inin **net/wire bağlaması** veya **pad/paste geometrisi kuralı yazması** |
| `sistem-arayuz-sozlesmesi` | cross-board kanonik contract + board projeksiyonu | [[../decompose-pcb-decisions/SKILL\|decompose-pcb-decisions]], [[../konnektor/SKILL\|konnektor]], [[../sematik-erc/SKILL\|sematik-erc]] | board-local `interface_contract.json` seçimine **ikinci sahip** olması |
| [[../konnektor/SKILL\|konnektor]] | seçilmiş konnektör + derating + mating cycle + landing pattern + keepout üç `kind` | `arayuz-eslesme-kapisi` | Eşleşme kapısının **akım/derating/mating-cycle bütçesi hesaplaması** ya da **parça seçmesi** |
| `arayuz-eslesme-kapisi` | seçilmiş **iki** parçanın birbirine takılabilirliği + uçtan uca pin örtüşmesi receipt'i | `ecad-varlik-kalifikasyonu`, sonra [[../sematik-erc/SKILL\|sematik-erc]] | Eşleşme kapısının **net bağlaması**, **şematik açması** ya da **hacim çakışması ölçmesi** |
| `arayuz-eslesme-kapisi` | fiziksel eşleşme **limiti** (`l3_rule_universe.json` satırı) | `l3-bringup-karakterizasyon` | L3 skill'inin bu limiti **kendi yazması** (`SELF_AUTHORED_LIMIT`) |
| [[../cpl-assembly/SKILL\|cpl-assembly]] | released Gerber/BOM/CPL | `ilk-urun-montaj-kabulu` | FAI skill'inin **CPL'i düzeltmesi** |
| `ilk-urun-montaj-kabulu` | kabul edilmiş as-built birim + FAI receipt'i | `l3-bringup-karakterizasyon` | FAI GREEN olmadan L3 elektriksel hata avı açılması |
| domain skill'leri + [[../dogrulama-matrisi/SKILL\|dogrulama-matrisi]] | D-kuralı limitleri/profilleri | `l3-bringup-karakterizasyon` | L3 skill'inin **eşik/limit yazması** |
| `l3-bringup-karakterizasyon` | fiziksel kanıt + RED/GREEN kapanışı | ilgili upstream skill + orkestratör | Board'a **doğrudan yama** yapması (yalnız L2 kenarı açar) |

**Karar: yukarı akış limitin sahibi, aşağı akış kanıtın sahibidir.**
Bir skill hem limiti hem kanıtı sahiplenirse kendi ölçtüğü değeri kendi
yazdığı eşikle karşılaştırır ve kapı bir tautoloji olur — bu paketin
tekrar tekrar bulduğu sahte-PASS sınıfı.

Bunun `assurance_loop`/`evidence_origin` tarafındaki karşılığı:
[[coverage-gate.md]] §"participates-in ≠ produces-evidence-for".

### 9a. `arayuz-eslesme-kapisi` ile üç komşusu arasındaki sınır — ölçülmüş

Bu üç ayrım *"kim kararı verir"* sorusunun cevabıdır; ikisi de doğru olan
kuralların çakıştığı yer değil, **evrenlerin** çakıştığı yerdir.

| Soru | Sahibi | Niçin öteki değil |
|---|---|---|
| Bu konnektörün kontak yüzü nedir, kaç çevrim dayanır, kaç amper taşır? | [[../konnektor/SKILL\|konnektor]] | Tek taraflıdır ve öyle kalması gerekir |
| Bu iki parça birbirine takılır mı, pin 1 karşı uçta nereye düşer? | `arayuz-eslesme-kapisi` | `konnektor` **tek taraflıdır**: kendi konnektörünün kontak yüzünü bilir, karşı tarafınkini bilemez ve şeması (`additionalProperties: false`) karşı tarafı hiç tanımaz. **Aynalanma iki taraflı bir ilişkidir** ve tek taraflı kayıtta ifade edilemez |
| Bu tek MPN'in sembol+footprint'i belgesine uyuyor mu? | [[../ecad-varlik-kalifikasyonu/SKILL\|ecad-varlik-kalifikasyonu]] | Gücü **tek soru sormasından** gelir; ikinci ekseni oraya sokmak kendi varlık iddiasını sulandırır |
| İki parçanın **kümesi** eşit mi, **sırası** doğru mu? | küme: `ecad-varlik-kalifikasyonu` · **sıra**: `arayuz-eslesme-kapisi` | *"Sayı eşitliği kimlik eşitliği değildir"* ilkesinin kanonik sahibi ECAD kapısıdır; aynı ilkenin **parça↔parça** eksenine uygulanması ve **permütasyon** predicate'i eşleşme kapısınındır |
| Kablo/adaptör hangi MPN olacak, stokta var mı? | [[../bom-lifecycle/SKILL\|bom-lifecycle]] | Eşleşme kapısı yalnız *"seçilen zincirde duruyor mu"* diye sorar; BOM'da yoksa `CONFIRM` (RED **değil**) + L2 kenarı |

**Yeni bir evren gerekti, yeni bir kural değil.** `konnektor`'ün evreni yazılı
olarak *"netlist'teki konnektör sınıfı refdes"*tir — hangi alanı eklerseniz
ekleyin, **refdes'i olmayan bir kablo o paydaya giremez**, ve zincirdeki eksik
halka tam olarak odur. [[../design-checker/SKILL|design-checker]] bu iş için
**genişletilmedi**: `pcb-orchestrator:124-128` benzer bir öneriyi *"ikinci bir
yürütücü sessiz otorite çatışması yaratır"* diye reddetmişti.
