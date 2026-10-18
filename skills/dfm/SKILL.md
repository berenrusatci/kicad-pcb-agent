---
name: dfm
description: Fab yeteneklerini DRC'ye enjekte et (önleyici) + yerleşimi 3D keepout'a göre yap + gerber öncesi doğrula. Teardrop, via-in-pad, NSMD/SMD, EPAD pasta ve MLCC flex crack kararlarının sahibi.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 3 — BARİYER (önleyici DRC + yerleşim, seri) + Faz 4 (geç: son DRC doğrulaması)"
assurance_loop: [L0, L1, L2]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: fab_capabilities.json, schema: ../../tools/schemas/fab_capabilities.schema.json, required: false}
  - {artifact: stackup.json, schema: ../empedans-stackup/references/stackup.schema.json, required: true}
  - {artifact: keepout_zones.json, schema: ../mekanik-dxf/references/keepout_zones.schema.json, required: true}
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
outputs:
  - {artifact: dfm_report.json, schema: null}
  - {artifact: drc.json, schema: null}
  - {artifact: placement.json, schema: null}
  - {artifact: placed board, schema: null}
writes_board: true
tools: [dfm-emc-check, mask-dam, kicad-cli, drc-gate, gerber-dfm, ipc-fab-class, ipc-dfa, dru-emit, jlcpcb-dfm, footprint-factory]
---

# SKILL: DFM / Üretilebilirlik

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Kural çatışmaları: [[../_shared/rule-conflicts.md]] §1, §3, §5

Fab profili yoksa mevcut sayısal fallback'lerle iş akışını sürdür fakat
hepsini `LOCAL_POLICY/SCREEN_ONLY` say; bunlar otorite değeri değildir ve DFM
PASS üretmez. Yalnız scope-doğru, kaynak/hash bağlı
[`tools/schemas/fab_capabilities.schema.json`](../../tools/schemas/fab_capabilities.schema.json)
profili sonucu PASS'e yükseltebilir. `evidence_origin=LOCAL_POLICY`,
`assurance_loop=L1+L2`, `status=SCREEN_ONLY` (profil yoksa).

**İki fazda koşar:** Faz 3'te *önleyici* (kural enjeksiyonu + yerleşim, bariyer),
Faz 4'te *doğrulayıcı* (`run_dfm_check`, son DRC taraması).

## 0. Ne zaman çağrılır

- **Faz 3 bariyerinin ilk adımı.** Buradan sonrası seri; board tek sahiplidir.
  `keepout_zones.json`, `stackup.json` ve netlist hazır olmadan açılmaz.
- **Faz 4'te yeniden**, salt-okuma doğrulayıcı olarak; gerber üretiminden önce.
- Board'a yazan her adımdan sonra türetilmiş çıktılar (teardrop, zone fill,
  uzunluk raporu) **bayatlar** ve yeniden üretilir.
- Empedans izi fab minimumunun altına düşerse L2 kenarıyla
  [[../empedans-stackup/SKILL|empedans-stackup]] yeniden açılır (routing zorlanmaz).

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| min iz/space/via/annular/aspect/silk/comp-to-comp/edge | `fab_capabilities.json` | `LOCAL_POLICY/SCREEN_ONLY`, PASS yok |
| `profile_hash`, kaynak URL, erişim tarihi, profil sürümü | fab profili | receipt `STALE` |
| `soldermask.min_bridge_mm` | fab profili | yerel karar bandı ([[../_shared/rule-conflicts.md]] §5) |
| rail başına `final_width_mm` + `min_via_count` | `rail_tree.json` | güç net-class'ı yazılamaz |
| bölge başına `max_allowed_height_mm` | `keepout_zones.json` | Z ihlali ölçülemez → `NO_COVERAGE` |
| paket üreticisi land-pattern notu | datasheet | NSMD/SMD kararı `CONFIRM` |

## 2. Adımlar

1. `fab_capabilities.json` — **tarihli + doğrulanmış** (üretici sayfası değişir).
2. `apply_dfm_rules()` → fab profilini KiCad DRC custom rule/net-class'a çevir
   (ihlal en baştan engellenir).
   - **Güç net-class'ları** `rail_tree.json`'dan: her rail için `final_width_mm` +
     `min_via_count` DRC'ye yaz → güç yolu inceyse/tek via ise ihlal.
   - **Z/placement kısıtı** `keepout_zones.json`'dan: parça body-height >
     bölge `max_allowed_height_mm` → placement ihlali.
3. **Yerleşim (BARİYER — buradan sonrası seri, board tek sahipli):** parçaları
   `keepout_zones.json` 3D yükseklik haritasını **ihlal etmeden** yerleştir.
   Sıra: konnektör/mekanik-sabit parçalar → ana IC'ler → decoupling (≤1.5 mm) →
   osilatör (≤5 mm) → gerisi. Bu iki sayı iş akışını ilerleten
   `LOCAL_POLICY/SCREEN_ONLY` yerleşim ön-eleğidir; otorite veya elektriksel
   PASS değeri değildir. Üstün kural ve kapsam istisnası:
   [[../_shared/rule-conflicts.md]] §2.
   `evidence_origin=LOCAL_POLICY`, `assurance_loop=L0+L3`, `status=SCREEN_ONLY`.
   Çıktı `placed board` + `placement.json`; routing ve CPL bunu tüketir.
4. `run_dfm_check()` → DRC **repo rule set'iyle** (`drc.sh` wrapper, çıplak DRC
   değil). Tipe göre sınıflandır: clearance/width/annular/silk.
5. **Soldermask barajı (mask dam)** — clearance DRC'sinden AYRI kontrol.
   Formül, karar bandı ve pin-arası kanal hesabı
   [[../highspeed-routing/SKILL|highspeed-routing]] §Escape'te; ölçüm
   `tools/scripts/board/mask_dam.py`. DRC "clearance" barajı **yakalamaz**.
6. Assembly DFM ayrı: courtyard çakışması, comp-to-edge, nozzle erişimi.
7. Empedans iz < fab min → stackup'a geri sinyal (adım 2), routing'i zorlama.

### DRC kural enjeksiyonu (`.kicad_dru`) — sessiz hata kaynakları
> **Bu bölüm DRC kural enjeksiyonunun kanonik sahibidir.**

- **Dosyada SONRA gelen eşleşen kural kazanır** → **genel kuralları başa,
  spesifik kuralları SONA** yaz. Ters sıralarsan spesifik kuralın genel kural
  tarafından ezilir ve DRC yine "temiz" der.
  **Ölçüldü (KiCad 10.0.5, fault-injection):** 0.15 mm kenar-kenar boşluklu iki
  iz; genel `clearance ≥0.25 mm` + `NET_A` için spesifik `≥0.10 mm`.
  Genel→spesifik sırasıyla **0 ihlal** (spesifik kazandı); spesifik→genel
  sırasıyla **1 ihlal, gerekçe `GENEL_gevsek`** (genel kazandı). Her iki koşuda
  da kazanan **sonuncu** kuraldır. Regresyon:
  `tools/tests/test_dru_precedence.py`.
  > ⚠️ Bunun tersini ("ilk eşleşen kazanır, spesifik kurallar genelden ÖNCE
  > yazılmalı") söyleyen kaynaklara uyma — `projectE-2archive/pcb-tool-v2`
  > `DOCS/03_Design_Rules.md:38-46` ve `HAFIZA/Hafiza_Defteri.md:123-144` bunu
  > "doğrulanmış düzeltme" olarak kaydetmiş, **ölçüm bunu çürütüyor**. O sıraya
  > geçilirse her spesifik kural sessizce ezilir.
- Dosya `(version 1)` ile başlar.
- **Canlı projeyi doğrudan düzenleme.** KiCad harici metin düzenlemeyi önermiyor:
  scratch kopyaya deterministik üret → `kicad-cli pcb drc --format json
  --severity-all --exit-code-violations` ile *parse edildiğinin ve
  uygulandığının* kanıtını al → sonra atomik taşı. Üretilen kuralın koştuğunu
  kanıtlamadan "kural enjekte edildi" yazma (fault-injection: kuralı ihlal eden
  bir geometri koy, DRC kırılmalı).
- `min_clearance = 0.0` **kontrolün kapalı olması** demektir, "sınır yok" değil.
  Değeri `.kicad_dru`'daki en gevşek muafiyetin **altına** koy, üstüne değil —
  global taban muafiyeti ezer ve konnektörün kendi pad geometrisi hata verir.
- DRC **empedans doğrulamaz**, geometriyi korur →
  [[../empedans-stackup/SKILL|empedans-stackup]] Adım 4.

## 3. Teardrop (gözyaşı damlası)

> **Bu bölüm teardrop kararının kanonik sahibidir.** Yüksek hız tarafındaki ödün
> ve `IsTeardropArea()` ölçümü [[../highspeed-routing/SKILL|highspeed-routing]]
> §Teardrop'ta; ikisi birbirini tekrar etmez.

İz↔pad/via birleşimine eklenen bakır fileto. **Elektriksel bir iyileştirme
değil, üretim/mekanik payı**: matkap kayması (drill wander) ve tabaka kaydırma
(registration) toleransı yüzünden delik pad merkezinden kaçtığında iletken-land
birleşiminin kopmasını önler; ayrıca dar izin geniş pada bağlandığı yerdeki
gerilme yığılmasını (termal çevrim, titreşim çatlağı) dağıtır.

### Nerede **gerekir**
- **Annular ring dar olan PTH/via'lar**: dış katman halkası Class 2 için
  < ~0.15 mm, Class 3 için < ~0.20 mm kalıyorsa. IPC-6012 Class 2, land-iletken
  birleşiminde fileto varsa 90° breakout'a izin verir; Class 3'te breakout
  yasak ama **teğetlik (tangency) teardrop şartıyla** kabul görür. Yani teardrop
  burada annular ring'i iz yönünde etkin olarak büyütür.
- **İz padden/via'dan belirgin ince** ise — pratik eşik: iz ≤ ~0.13 mm (5 mil)
  ve via'ya giriyor.
- **Flex / rigid-flex** (IPC-2223): pad-iz geçişi bükülme bölgesinin yakınındaysa
  fileto neredeyse zorunlu; düz konik değil **eğri (curved)** fileto kullan,
  birleşimde genişlik ≥ 1.2–1.5 × iz genişliği. Bükülme bölgesine via koyma;
  mecbursan teardrop + ek kaplama.
- Yüksek termal çevrim / titreşim / press-fit yükü olan kartlar; genel olarak
  IPC Class 3 hedefleyen tasarımlar.
- Fab'ın DFM raporunda breakout/annular uyarısı verdiği yerler (fab profiline
  yaz, tek tek elle düzeltme).

### Nerede **kullanma / dikkat**
- **Çok Gb/s diferansiyel çiftler** → varsayılan **kapalı**. Gerekçe ve S-parametre
  koşulu: [[../_shared/rule-conflicts.md]] §3. (Yüksek güvenilirlikli OpenVPX
  backplane'ler teardrop şart koşmaz — Class 3 ≠ teardrop zorunlu.)
- **İnce pitch BGA/QFN escape**: fileto komşu pad/iz clearance'ını ve
  **maske barajını** yer. Teardrop sonrası baraj hesabını (adım 5) tekrar koştur.
- **Zone içindeki padler**: `td_on_pad_in_zone` varsayılan `false` — açık
  bırakma, thermal relief geometrisini bozar.
- İz genişliği pad boyutuna yakınsa teardrop anlamsız; KiCad zaten
  `td_width_to_size_filter_ratio` (0.9) ile atlar.

### KiCad'de somut
- Ayarlar `.kicad_pro → board.design_settings.teardrop_options / _parameters`.
  Repo varsayılanları: `td_onvia/td_onpthpad/td_onsmdpad = true`,
  `td_ontrackend = false`, `td_length_ratio 0.5` / `td_maxlen 1.0 mm`,
  `td_height_ratio 1.0` / `td_maxheight 2.0 mm`, `td_curve_segcount 0` (düz
  konik; **flex'te ≥ 5 yapıp eğri kullan**), `td_allow_use_two_tracks true`.
- **Üretim API'si yok** (pcbnew 10.0.5 SWIG): İnsan adımı **GUI → Tools →
  Generate Teardrops**. API sınırı ve doğrulama:
  [[../highspeed-routing/SKILL|highspeed-routing]] §Teardrop.

### Sıra ve doğrulama
1. Teardrop **en son**: routing dondurulduktan sonra, gerber'den hemen önce.
2. Ekledikten sonra **DRC + maske barajı yeniden koş** (fileto clearance/mask
   ihlali doğurabilir).
3. Herhangi bir iz düzenlemesinden sonra teardrop'lar bayatlar → sil + yeniden
   üret (`BulkRemoveStaleTeardrops` / GUI).
4. Rapora: teardrop zone sayısı, hangi netlerde **kapalı** bırakıldığı
   (yüksek-hız çiftleri) ve gerekçesi.

## 4. Pad & via nüansları (DRC'nin görmediği üretim kararları)

### (0) Land pattern: kütüphanenin verdiği ≠ üreticinin istediği — **`footprint-factory`**

Bu, bu bölümdeki diğer maddelerin hepsinden **önce** gelir: pad nüanslarını
tartışmadan önce pad'lerin doğru yerde ve doğru boyutta olduğunu bilmek gerekir.

KiCad'in jenerik kütüphane ayak izleri **paket anahatı** standartlarından
(JEDEC MO-178, IPC-7351) türetilmiştir. Üreticinin datasheet'indeki
**"Recommended Land Pattern"** ayrı bir belgedir ve ikisi aynı değildir.
DRC bu farkı **göremez**: DRC bakırı kendi kurallarına göre denetler,
pad'lerin üreticinin istediği yerde olup olmadığını değil.

**Ölçülmüş olay (cam-w, 2026-08-10):** dört parçanın land pattern'ı üretici
çizimine karşı ölçüldü, **üçü ayrıştı**:

| parça | ayrışma |
|---|---|
| `TPD4E02B04` USON-10 | TI, **pin 3 ve 8'i** (ikisi de GND, ESD deşarj dönüş pedleri) 0.40 mm çiziyor; jenerik kütüphane onları da 0.20 → **%25 dar toprak dönüşü**, diğer 8 pedi %50 geniş |
| `24AA025E48` SOT-23-6 | iç boşluk 0.95 vs Microchip **min 1.70** — pad'ler gövde altına üreticinin öngördüğünden fazla giriyor |
| `TPS7A20` SOT-23-5 | sıra merkez-merkez 2.275 vs TI 2.60; asıl sapma toe'da değil **heel**'de |
| `SiT8924B` 2520 | **0.00 mm — tam uyuyor.** Ayrışmanın kural olmadığının kanıtı |

**Kullanımı:** `vendor_land_patterns.json` beyanı yaz (her sayı bir `src`
taşır), `footprint_factory.py` ile üret, `--check` ile kapıya bağla.

**Bilinmesi gerekenler:**
- Üreteç **hiçbir land pattern sayısı içermez**; hepsi beyandan gelir. Bir
  sayıyı koda yazmak onu kaynağından koparmaktır.
- Courtyard **elle verilmez**, pad'lerin gerçek kapsamından türetilir. Elle
  verilen bir courtyard pad'lerin içinde kalabilir ve hiçbir şey itiraz etmez.
- Kayıtlar **parça başınadır, paket başına değil**: Microchip aynı SOT-23 için
  c-c 2.80, TI 2.60 istiyor. Tek bir paylaşılan "SOT-23" tanımı ikisinden
  birini sessizce yanlış basardı.
- `--check` yalnız *üretilen ile diskteki* uyumu denetler. Sayıların
  datasheet'e uyduğunu **kanıtlamaz** — o bir insan okumasıdır ve `src`
  alanlarında yaşar.
- Ped numaralandırması çoğu land pattern çiziminde **yoktur**. Geometri
  üreticiden gelse bile "hangi ped pin 1" ayrı bir iddiadır; beyan edilmezse
  parça 180° dönük dizilebilir ve hiçbir ölçüm itiraz etmez.

### (a) Via-in-pad: "çizimde çalışıyor" ≠ lehimlenebilir
Pad'in içindeki açık via, reflow'da lehimi bareline emer (**solder wicking**) →
zayıf/boş joint, BGA'da açık devre. Via-in-pad kullanacaksan **IPC-4761 Type VII
(filled and capped)** şart. Tented/plugged (Type IV/V) **yeterli değildir**.

> **Type VII "iletken dolgu" demek DEĞİLDİR.** Type VII, Type V (filled) via'nın
> üzerine **ikincil metalize kaplama** (cap) gelmesini tanımlar — dolgu
> malzemesinin cinsini belirtmez. Yaygın via-in-pad süreci **iletken olmayan
> epoksi** dolgu + bakır kapaktır. Dolayısıyla fab çizimine tipi yazmak yetmez;
> ayrıca **dolgu malzemesi (iletken / iletken değil)**, kapak kaplaması, izin
> verilen çukur/çıkıntı (dimple/bump) ve kabul yöntemi belirtilmelidir.
> Isıl/elektriksel gerekçeyle bakır-dolgu istiyorsan bunu **ayrıca** iste.

Planarlık toleransı ve maliyet artışı fab teklifinden gelmeden sayı yazma —
`CONFIRM: dimple/bump limiti ve HDI/VIPPO fiyat farkı (RFQ).`
Karar: BGA fanout'ta gerekiyorsa kullan ve *bütçele*; gerekmiyorsa via'yı pad
dışına al (dog-bone). **ESD/TVS GND padinde bu kural tersine döner** →
[[../_shared/rule-conflicts.md]] §1.

### (b) NSMD mi SMD pad mi?
- **NSMD (mask pad'den geniş)** varsayılan: pad boyutu bakır etch toleransıyla
  belirlenir (maske hizalamasından sıkı), yan duvar ıslanması termal çevrimde
  daha dayanıklı, reflow gazı kaçabildiği için daha az boşluk.
- **SMD (mask pad kenarına biner)** çok ince pitch'te (~≤0.4 mm) köprülenmeyi
  sınırlamak için tercih edilebilir — ama bu **evrensel bir eşik değildir**.
- **Sıra: paket üreticisinin uygulama notu > genel pitch kuralı.**
  Bu kartta AM5706'nın **0.65 mm** BGA'sı için TI'nin kendi paket notu (SPRACA4,
  §BGA PCB Feature Definitions) **NSMD** önerir → footprint kaynağı odur.
  `≤0.4 mm → SMD` yalnız ilgili paket notu **ve** montajcının deneyimi
  destekliyorsa uygulanır.
- Karar footprint kütüphanesinde verilir ve **belgelenir**; aynı kartta sessizce
  karışması kalite sorunudur. Mask açıklığı/pad ilişkisini footprint üretiminde
  ölç, göz kararı bırakma.

### (c) QFN/EPAD: stencil açıklığı ≠ pad
> **Bu bölüm EPAD pasta kuralının kanonik sahibidir.**
> [[../cpl-assembly/SKILL|cpl-assembly]] buraya bağ verir.

Termal pad'e **tam açıklık verme** — parça reflow'da yüzer/eğilir ve boşluk
oranı patlar. Windowpane/ızgara desen, **%50–75 pasta kapsaması**; IPC-A-610
boşluk sınırı termal pad alanının **%25**'i. İnce pitch'te stencil 0.10–0.12 mm.
Bu bir **F.Paste katmanı** kararıdır; footprint'ten gelen tam pasta açıklığını
düzeltmeden gerber üretme.

### (d) Maske barajını bakıra dokunmadan genişletme
Baraj fab minimumunun altındaysa ve bakır geometrisi konnektörün kendi
footprint'inden geliyorsa (değiştirilemez), çözüm **negatif local solder
mask margin**: pad başına −0.03 mm → baraj 0.15 → 0.21 mm. Maske açıklığı
küçülür, **bakır aynı kalır**, lehimlenebilirlik pratikte etkilenmez.
Ölçülmüş örnek: USB-C J1'de 20 pad, `mask_dam` ihlali 10 → 0, DRC 0'da kaldı.
Bu sonuç **0.20 mm karar bandına göre** geçerlidir; 0.21 mm hâlâ `CONFIRM`
bandındadır ve fab profili olmadan PASS sayılmaz
([[../_shared/rule-conflicts.md]] §5).
> `m_SolderMaskMinWidth`'i fab minimumuna çekmek **ters etki yapar**: KiCad o
> genişliğin altındaki maske kılçıklarını *siler* ve açıklıkları birleştirir.

### (e) Yerleşim uygunluğu: pad clearance yetmez, **courtyard** şart
Bir parçaya yer ararken yalnız pad/iz clearance'ına bakan arama, parçayı komşu
IC'nin **gövdesinin altına** koyar ve bunu "temiz" raporlar. Bu kartta C1'e yer
ararken ilk tarama 14 "geçerli" aday buldu; **hepsi U1'in (SOT-23-6) gövdesinin
altındaydı**. Courtyard çakışması eklenince aday sayısı 0'a düştü.
- Kural: yerleşim aramasında engel kümesi = pad + iz + via + **courtyard**.
- Aynı hata via/iz eklerken de olur: via'nın konumu temiz olabilir ama
  **onu bağlayan kısa izin yolu** değildir. via-in-pad düzeltmesinin ilk sürümü
  yalnız via'yı kontrol etti, eklediği 0.6 mm'lik iz U1.3 padine 0.093 mm
  yaklaşıp DRC patlattı. Yol da örneklenerek kontrol edilmeli.

### (f) MLCC flex crack: yerleşim kararı, montaj sorunu değil
> **Bu bölüm MLCC flex crack kuralının kanonik sahibidir.**
> [[../cpl-assembly/SKILL|cpl-assembly]], [[../konnektor/SKILL|konnektor]] ve
> [[../bom-lifecycle/SKILL|bom-lifecycle]] buraya bağ verir.

Panel kırma (depanel), montaj vidası ve konnektör basıncı kartı büker; seramik
çatlar (45° chevron çatlak, terminasyon dibinden). Genelde **test edilmez,
sahada patlar**.
- V-score'dan **≥2 mm** (temkinli: 3–5 mm), routed/mouse-bite kenardan ≥1 mm.
  Montaj deliği ve konnektör çevresi de bükülme bölgesidir.
- **Yön:** kart, kırma çizgisi etrafında döner → yüzey gerinimi çizgiye **dik**
  yöndedir. Bu yüzden kondansatörün uzun ekseni (terminasyondan terminasyona)
  kırma çizgisine **paralel** olmalı; dik yerleşirse gerinimin tamamı iki lehim
  arasına biner. (İnternette bunun tersini yazan kaynaklar var — gerinim yönü
  argümanıyla doğrula, ezber alma.)
- Kenara yakın zorunlu parçalarda **soft/flexible terminasyon** (standart MLCC
  ~3 mm bükülmede çatlar, soft-term ~5–6 mm dayanır) → BOM'a kısıt olarak iner
  ([[../bom-lifecycle/SKILL|bom-lifecycle]] §3e).
- Kenar bakır boşluğu + kart kenarına parça mesafesi ayrı kısıt; keepout'a ekle.

## 5. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Lehim boşluk oranı (void) | D | X-ray gerekir |
| Gerçek planarlık / dimple-bump | C→D | Fab teklifi + kesit |
| `epad_paste_coverage` çok-açıklıklı desen | A(kısmi) | Araç tek-pad + margin modelini ölçer; windowpane desenini **tanımaz** → elle doğrula |
| `edge_keepout_ceramics` **yön** | A(kısmi) | Mesafeyi ölçer, uzun eksen yönünü ölçmez; V-score hattı board'da yok |
| Teardrop **üretimi** | `API_YOK` | pcbnew 10.0.5 SWIG'de üretim API'si yok; GUI adımı |
| Fab süreç kabulü | C | Fab DFM raporu |

> **`dfm_emc_check.py` doğrulama durumu — ÇÖZÜLDÜ (C3).**
> Konsolidasyon öncesi bu araç iki farklı SHA-256'lı kopya hâlinde dolaşıyordu ve
> iki envanter çelişiyordu ("fault-injection kanıtlandı" vs `NO_COVERAGE`).
> Yeni ağaçta **tek kanonik kopya** vardır (`tools/scripts/board/dfm_emc_check.py`,
> registry'de hash'li) ve validator'ı `tools/tests/test_board_checks.py`'dir.
> Araç artık `NO_COVERAGE` değildir; A-seviyesi kanıt üretebilir — fakat
> §5'teki kapsam boşlukları (`epad_paste_coverage` windowpane,
> `edge_keepout_ceramics` yön) hâlâ geçerlidir ve o satırlar `A(kısmi)`'dir.

## 6. Doğrulama

```bash
# DRC (repo rule set ile, çıplak DRC değil)
kicad-cli pcb drc --format json --severity-all --exit-code-violations \
  -o drc.json board.kicad_pcb

# DFM/EMC nüans ölçümü   (--json YAZAR)
python3 tools/scripts/board/dfm_emc_check.py board.kicad_pcb --json checks.json

# maske barajı
python3 tools/scripts/board/mask_dam.py board.kicad_pcb --json mask_dam.json

# validator (fault-injection dahil)
python3 -m unittest -v tools.tests.test_board_checks

python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection (zorunlu): kuralı ihlal eden bir geometri enjekte et → DRC
**kırılmalı**. Ölçülmüş referans: U3.1 pad merkezine via (`via_in_pad` 0→1),
0.30/0.25 mm via (`annular_ring` 0→2), C5 kart kenarına 0.87 mm
(`edge_keepout_ceramics` 0→1) — üçü de yakalandı.
DRC "temiz" görünürse fault-injection ile testin boş olmadığını kanıtla.

## 7. Kabul kriterleri

- [ ] `fab_capabilities.json` fab adı, kaynak URL, erişim tarihi, profil sürümü
      ve `profile_hash` taşıyor; profil 30 günü aşınca `STALE`, üretim release'i
      öncesi her durumda yeniden doğrulandı.
- [ ] Fab profili yoksa tüm sayısal sonuçlar `LOCAL_POLICY/SCREEN_ONLY`
      etiketli; DFM PASS iddiası yok.
- [ ] `.kicad_dru` kural sırası **genel → spesifik**; üretilen kuralın koştuğu
      fault-injection ile kanıtlandı.
- [ ] `min_clearance = 0.0` hiçbir yerde "sınır yok" anlamında kullanılmadı.
- [ ] Güç net-class'ları `rail_tree.json`'dan (`final_width_mm`, `min_via_count`)
      DRC'ye yazıldı.
- [ ] Yerleşim `keepout_zones.json` 3D yükseklik haritasını ihlal etmiyor
      (top + bottom ayrı).
- [ ] Yerleşim arama engel kümesi pad + iz + via + **courtyard** içeriyor;
      via/iz eklerken **bağlayan yol da** örneklenerek kontrol edildi.
- [ ] Aspect ratio = kalınlık/delik ≤ ~10:1; aşan yerlerde via büyütüldü / HDI.
- [ ] Via-in-pad varsa IPC-4761 Type VII fab notu yazılı **ve** dolgu malzemesi,
      kapak kaplaması, dimple/bump limiti, kabul yöntemi belirtildi.
- [ ] NSMD/SMD kararı paket üreticisi notuna dayanıyor ve belgelendi; aynı kartta
      sessizce karışmıyor.
- [ ] EPAD pasta kapsaması %50–75 windowpane; tam açıklık kalmadı.
- [ ] Maske barajı karar bandına göre değerlendirildi
      ([[../_shared/rule-conflicts.md]] §5); `< 0.20 mm` kalan yer yok.
- [ ] Teardrop **en son** eklendi; sonrasında DRC + maske barajı yeniden koşuldu;
      yüksek-hız çiftlerinde bilinçli kapalı ve gerekçesi yazıldı.
- [ ] Depanel/kenar keepout'u seramiklere uygulandı; **yön** (uzun eksen kırma
      hattına paralel) elle doğrulandı.
- [ ] Empedans izi fab min'in altındaysa stackup'a geri sinyal açıldı, routing
      zorlanmadı.
- [ ] Gerber+drill üretim tabanı; IPC-2581/ODB++ yalnız yardımcı provenance
      olarak (board hash + commit ile) saklandı, tek zorunlu format yapılmadı.
- [ ] Her kontrol `scanned` raporluyor; `scanned=0` PASS sayılmadı.
- [ ] Board yazımı transaction sırasına uydu (scratch → L0 receipt → atomik promote).
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../empedans-stackup/SKILL|empedans-stackup]] ·
[[../highspeed-routing/SKILL|highspeed-routing]] ·
[[../crosstalk-thinning/SKILL|crosstalk-thinning]] ·
[[../emc-esd/SKILL|emc-esd]] · [[../cpl-assembly/SKILL|cpl-assembly]] ·
[[../konnektor/SKILL|konnektor]] · [[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`gerber-dfm`** — Gerçek export edilmiş RS-274X gerber'lardan DFM kanıtı — DRC'den **bağımsız** ikinci okuma.
- **`ipc-fab-class`** — IPC-6012 Class 2/3 limit denetimi; **verilen** ölçümleri denetler, kendisi ölçmez.
- **`ipc-dfa`** — IPC-A-610 montaj açıklığı; yerleşim sıkıştığında bileşen arası mesafeyi karara bağlar.
- **`dru-emit`** — `scripts/ipc/*` sayılarını KiCad 10 `.kicad_dru` dosyasına çevirir — önleyici DRC böyle enjekte edilir.
- **`jlcpcb-dfm`** — JLCPCB erken-DFM API köprüsü. **İskelet:** gerçek bir hesaba karşı doğrulanmadı, çıktısı tek başına kapı açmaz.
