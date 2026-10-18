---
name: guc-isil
description: Rail-bazlı güç bütçesi + regülatör junction sıcaklığı + LDO/SMPS kararı ve güç izi/via boyutlandırma. Şematikten önce koşar; rail_tree.json ve thermal_report.json üretir.
version: 1.1.0
last-reviewed: 2026-08-10
phase: "Faz 1 — Temeller (paralel kök düğüm; 2, 5, 8 ve konnektör bunu tüketir)"
assurance_loop: [L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: parts.json, schema: null, required: true}
outputs:
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json}
  - {artifact: thermal_report.json, schema: references/thermal_report.schema.json}
  - {artifact: zpdn_model.json, schema: references/zpdn_model.schema.json}
writes_board: false
tools: [rail-budget, pmic-allocation, current-density, thermal-bridge, electrothermal, ipc-trace-width, ipc-via-current, loop-stability, ngspice-bridge, model-registry, correlate]
---

# SKILL: Güç Bütçesi & Isıl

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Adı çakışan büyüklükler: [[../_shared/glossary.md]] §3, §4

## 0. Ne zaman çağrılır

- Şematik açılmadan **önce**, Faz 1'in kök düğümü olarak. `parts.json` yoksa
  koşma; orkestratör Faz 0a baseline'ını üretmeden bu skill açılmaz.
- Bir parça/rail değiştiğinde (lifecycle alternatifi, konnektör derating
  sonucu, DC-bias bulgusu) **yeniden** koşar; `rail_tree.json` yeniden yayınlanır.
- Konnektör akım derating'i rail'in worst-case akımının altına düşerse bu skill
  L2 geri besleme kenarıyla tekrar açılır.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| `i_max` (parça başına) | datasheet abs-max / uygulama notu | `TBD` + spec, `CONFIRM` |
| `θ_ja` | datasheet (JEDEC kartı belirtilerek) | `SCREEN_ONLY`, marj yaz |
| `vin` / `vout` | güç ağacı | `TBD` |
| ortam sıcaklığı | ürün senaryosu (bu projede 85 °C) | `CONFIRM` |
| bitmiş bakır kalınlığı | fab profili | IPC-2221 ekranı yalnız eleme |
| min bitmiş delik + duvar kaplaması | fab profili | via kabulü `CONFIRM` |

Çıktı `rail_tree.json`
([`tools/schemas/rail_tree.schema.json`](../../tools/schemas/rail_tree.schema.json))
**yedi sabit alanı** taşır ve producer/consumer parity fixture'ı GREEN olmadan
yayınlanmaz: `i_max_a`, `ipc2221_screen_width_mm`, `final_width_mm`, `standard`,
`min_via_count`, `max_ir_drop_mv`, `c_eff_uf`.
`evidence_origin=DESIGN_ARTIFACT`, `assurance_loop=L0`, `status=CONFIRM`
(schema/fixture uyuşmazlığında).

## 2. Adımlar

1. `calculate_power_budget(parts, ambient_c=85)` → her rail için worst-case
   akım/güç. `util=1.0` (6× kamera FSIN ile eşzamanlı).
2. Her regülatör: `check_regulator_thermal()` → `Tj = Ta + Ploss·θ_ja`,
   `Ploss=(Vin−Vout)·Iout`. Bu verdict **`SCREEN_ONLY`** etiketi taşır (§3c).
3. Karar: LDO ve (`Ploss>1W` **veya** `Tj>100°C` **veya** `Vout/Vin<0.6`) →
   **`SWITCH_TO_SMPS`**.
   - Örnek: 5V→1.2V@1A → Ploss=3.8W → SMPS zorunlu.
4. **İz/via boyutlandırma — iki aşamalı, tek sayı değil.**
   `size_power_traces_and_vias()` iki ayrı alan yayınlar; ikisini karıştırma
   ([[../_shared/glossary.md]] §3):
   - `ipc2221_screen_width_mm` — hızlı, muhafazakâr **tarama**
     (`A=(I/(k·ΔT^0.44))^(1/0.725)`, k=0.048 dış / 0.024 iç). Yalnız eleme içindir.
   - `final_width_mm` — IPC-2152 baseline + stackup/düzlem yakınlığı/bakır
     kalınlığı/ortam düzeltmeleriyle. Tasarımı dondurmadan önce **bitmiş** bakır
     kalınlığını ve fab'ın minimum imal edilebilir iz genişliğini kullan.
   - `max_ir_drop_mv` **ayrı** hesaplanır (§3a: asıl kısıt genelde IR drop'tur).
   Rapor `standard`, ΔT, varsayımlar ve kullanılan düzeltme eğrilerini yazmadan
   kapanmaz. "IPC-2221 hesaplandı" tek başına kabul kriteri değildir.
5. **Via sayısı: sabit A/via katsayısı KULLANMA.** `via_ampacity=0.7` gibi bir
   sabit (ve "0.3 mm via ≈ 0.5–1 A" folkloru) taşınabilir bir sayı değildir —
   kaplama kalınlığı, bitmiş delik, via boyu, bağlı bakır ve darboğaz akım
   yoğunluğu belirtilmeden anlamsızdır. Ayrıca **via sıcaklığı bağlı iz/düzlemden
   bağımsız tahmin edilemez** (ölçüm–simülasyon çalışmalarının ana bulgusu).
   Yerine: fab'dan minimum bitmiş delik + minimum duvar kaplaması al; via
   direncini, paralel via'lar arasında akım paylaşımını, toplam IR drop'u ve
   pad/düzlem neck-down akım yoğunluğunu raporla. Termal kabulü via'yı tek başına
   değil **bağlı iz + düzlemle birlikte** elektro-termal model ya da kupon
   ölçümüyle ver. Veri yoksa `CONFIRM` — uydurma katsayı yazma.
6. `rail_tree.json` + `thermal_report.json` yayınla (adım 2, 5, 8 ve konnektör
   tüketir).

## 3. Nüanslar — bilinen tuzaklar

### (a) IPC-2221 muhafazakârdır — ama iç katman sayısı *uydurma*
IPC-2221'in iç katman eğrisi (k=0.024), dış eğrinin **deneysel dayanağı olmadan
yarıya bölünmesiyle** üretilmiştir. IPC-2152 (2009) ölçümle gösterdi ki gerçek
kartta iç izler dış izlere **çok daha yakın** akım taşır; aynı ΔT'de 2152 tipik
olarak 2221'in **1.5–2×** akımına izin verir (düzlem yakınlığı, malzeme, kart
kalınlığı, hava akışı parametre).
- **Varsayılan:** 2221 ile boyutlandır (muhafazakâr, hızlı) ama raporda
  `standard: IPC-2221` etiketi koy.
- **2221'i "olmaz" gerekçesi yapma.** Bir iz 2221'e göre sığmıyorsa tasarımı
  bloke etmeden önce 2152 ile (düzlem bağlamıyla) yeniden bak — yoksa gereksiz
  yere katman/bakır ağırlığı ekletirsin.
- Güç izinde asıl kısıt genelde ΔT değil **IR drop**'tur; ikisini ayrı raporla.

### (b) MLCC DC-bias: nominal kapasite bir yalandır
> **Bu bölüm bu kuralın kanonik sahibidir.** BOM, şematik, EMC ve doğrulama
> matrisi buraya bağ verir, içeriği tekrar etmez.

Yüksek-CV MLCC'de kapasite uygulanan DC gerilimle çöker. Örnek: **0402 X5R
10 µF/6.3 V @5 V ≈ 1.5 µF (−%85)**. Aynı değer 16 V/0805'te çok daha az düşer.
- PDN/bulk/LDO stabilite hesabında **derated (efektif) kapasiteyi** kullan,
  BOM'daki nominali değil. `rail_tree.json`'a `c_eff_uf` alanı yaz.
- Kural: `V_rated ≥ 2 × V_rail`, mümkünse **kılıfı büyüt** (V/mm alanı düşer),
  X7R > X5R, **Y5V yasak**. Sıcaklık katsayısı + yaşlanma ayrı kayıplar,
  DC-bias ile toplanır.
- **Alternatif önerirken dielektrik + `V_rated` + kılıf üçünü birden eşle.**
  Aynı değer + aynı kılıf farklı dielektrik (X7R↔Y5V), farklı gerilim, farklı
  DC-bias eğrisi demek olabilir; "eşdeğer" yoktur.
- Üretici DC-bias eğrisi yoksa değer `TBD` — "%20 derating" varsayımı
  yüksek-CV parçalarda 4× hata verir.
- LDO/SMPS çıkış kapasitesi derating sonrası stabilite penceresinin altına
  düşerse regülatör salınır: bu ERC/DRC'nin **hiç** yakalamadığı bir hatadır.

### (c) θ_ja bir cihaz özelliği değildir
Datasheet θ_ja belirli bir **JEDEC test kartına** (1s0p / 2s2p) aittir; 2 düzlem
eklemek θ_ja'yı %20–50 düşürür. Gerçek kartta:
- Sıcaklık tahmini için θ_ja yerine **ψ_jt** (kapak ölçümü) veya **θ_jb** kullan;
  θ_ja'yı sadece kaba eleme için kullan. Adım 2'nin verdict'i bu yüzden
  **`SCREEN_ONLY`** etiketi taşır — tek başına PASS vermez.
- Prototipte kararlı durumda paket **üst** sıcaklığı ölçülürse:
  `Tj_est = T_top + ψ_jt · P`. ψ_jt bir termal direnç **değildir**, bir ölçüm
  katsayısıdır; ölçüm noktasını ve koşulunu (akış, ortam, yük) kaydetmeden
  sayı taşınmaz.
- AM5706 gibi yüksek güçlü SoC'lerde enclosure + hava akışı + komşu ısı
  kaynaklarını içeren bir termal model (CFD) ve termokupl/IR korelasyonu
  olmadan **PASS verme** — headset kapalı hacminde bu kritik.
- Isı yayılımını **bakır alanı değil, düzlem sürekliliği** belirler; iç katmandaki
  bağlantısız bakır bile θ_ja'yı belirgin düşürür. Alan büyüdükçe kazanç doyuma girer.
- EPAD altındaki termal via dizisi hesabın parçası; via yoksa datasheet θ_ja
  geçersizdir.

### (d) Termal via dizisi ve bakır alanı — karar sırası
1. **Önce parça üreticisinin land-pattern/termal notunu uygula.** Genel kural
   parçaya özel nota üstün gelmez.
2. Genel bir QFN başlangıcı gerekiyorsa: delik **≤0.30 mm**, solid düzleme
   bağlantı, pasta için **windowpane** stencil deseni
   ([[../dfm/SKILL|dfm]] §QFN/EPAD sahibidir).
3. Açık / tented / plugged / capped seçimini fab ile kapat — ölçüt lehim
   kaçması (solder wicking), void ve maliyet. Bu bir DFM sorusudur, ısıl değil.
4. Bakır alanını tek bir eşikle değil **`Tj` vs bakır kaplama alanı eğrisiyle**
   büyüt; eğri platoya girene kadar üretici hesaplayıcısı/simülasyon koştur.
   "Alan büyüdükçe doyum" nicel gösterilmeden kabul kriteri olamaz.

`CONFIRM: via adımı, bitmiş delik, dolgu tipi, pasta kapsaması ve X-ray void limiti.`

### (e) Sıcaklık sınıfı BOM'u bağlar
Commercial (0–70 °C) parça 85 °C ortam hesabıyla çelişir; AEC-Q / industrial
gerekiyorsa MPN soneki değişir ve **stok bambaşkadır**. `thermal_report.json`
ortam sıcaklığı ile BOM sıcaklık aralığı **çapraz doğrulanır**
([[../bom-lifecycle/SKILL|bom-lifecycle]]).

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| DC-bias eğrisi | C | Üretici eğrisi olmadan hesaplanamaz; board dosyasında yok |
| Gerçek `θ_ja` | C→D | JEDEC kartı ≠ bu kart; gerçek değer termokupl/IR ister |
| Via akım/sıcaklık kabulü | D | Elektro-termal model veya kupon ölçümü gerekir |
| `ZPDN(f)` anti-rezonans | C→D | Kart-özel model + ölçüm korelasyonu ister; model/ölçüm sözleşmesi **§7** |
| Isıl doğrulama | D | Termal kamera / termokupl, gerçek yükte (L3) |

Bu tablodaki **D** satırlarının kapanışı bu skill'in elinde değildir: bu skill
`L3`'e **participates-in**'dir, `PHYSICAL_MEASUREMENT` üretmez (§7c).

Bunlar `acik_maddeler.md`'ye düşer: **ne / neden kanıtlanamadı / kim, hangi
ölçümle kapatacak.**

## 5. Doğrulama

```bash
# rail_tree producer/consumer parity fixture'ı
python3 tools/scripts/context_pack.py context_manifest.json --root . --json context_check.json

# rail_tree.json şema doğrulaması (gate receipt üzerinden)
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: `rail_tree.json`'dan yedi zorunlu alandan birini sil →
parity fixture **FAIL vermeli**. Vermiyorsa kontrol boştur.

## 6. Kabul kriterleri

- [ ] Her rail'de worst-case (max) akım kullanıldı, tipik değil; peak/inrush ayrı tutuldu.
- [ ] `ipc2221_screen_width_mm` ve `final_width_mm` **ayrı** alanlar olarak yayınlandı;
      `standard` etiketi yazılı.
- [ ] `max_ir_drop_mv` ΔT hesabından **ayrı** raporlandı.
- [ ] Hiçbir sabit katsayı (`via_ampacity`, "%20 derating", "0.3 mm via = 1 A")
      kaynaksız kullanılmadı; her katsayının ya kaynağı ya `CONFIRM`'ü var.
- [ ] Kapasite değerleri **nominal + efektif (`c_eff_uf`)** çift yazıldı.
- [ ] Regülatör `Tj` verdict'i `SCREEN_ONLY` etiketiyle yayınlandı; PASS iddiası yok.
- [ ] Hızlı akım geçişli her kritik ray için `Ztarget` ve frekans aralığı yazıldı;
      `ZPDN(f)` tepe noktaları raporlandı. Anti-rezonansı görmeden
      "kapasitans yeterli" PASS'i verilmedi.
- [ ] Sıcaklık sınıfı `thermal_report.json` ortamıyla çapraz doğrulandı.
- [ ] `rail_tree.json` şemaya uyuyor ve parity fixture GREEN.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.
- [ ] `gate_receipt.json` GREEN (`python3 tools/scripts/gate_receipt.py <file>` exit 0).
- [ ] §7a: kart-özel ZPDN modeli **beyanıyla** yayınlandı — ray, bant,
      `z_target_ohm` + kaynağı, modelde OLAN **ve OLMAYAN** parazitikler
      (mounting ESL, düzlem yayılımı, via endüktansı, DC-bias derating, ESR(f)),
      `valid_range`. Beyanı eksik model koşulmuş olsa bile `CONFIRM`.
- [ ] §7b: ölçüm varsa `correlate` beyan edilmiş bantta koşuldu; de-embedding
      **beyan edildi**; artık eşiği verildi. Ölçüm yoksa `NO_COVERAGE` yazıldı,
      `SIM_ONLY` PASS'e yükseltilmedi.
- [ ] §7c: `assurance_loop: L3` **participates-in** olarak okundu; bu skill
      `PHYSICAL_MEASUREMENT` yayımlamadı ve D satırları D olarak bırakıldı.

## 7. Kart-özel ZPDN modeli ve ölçüm korelasyonu

> **Bu skill `Ztarget`/`ZPDN` limitinin kanonik sahibidir.** Ayrı bir
> `power-integrity` skill'i aynı kararı ikiye bölerdi. Eksik olan bir sahip
> değildi — **modelin ve ölçümün derinliğiydi**: §4 "`ZPDN(f)` anti-rezonans |
> C→D | Kart-özel model + ölçüm korelasyonu ister" diyor, "Kurallar (kısa)"
> "kart-özel ZPDN(f) modeli ve ölçüm korelasyonu yoksa verdict en fazla
> `SIM_ONLY` olsun" diyor, ama *modelin ne beyan edeceği* ve *korelasyonun
> nasıl koşulacağı* hiçbir yerde yazılı değildi. Bu bölüm o iki sözleşmedir.

### (a) Modelin beyanı — bunlar yoksa model kullanılamaz

Makine-okur iskelet:
[`references/zpdn_model.schema.json`](references/zpdn_model.schema.json)
(`zpdn-model/v1`), çıktı `zpdn_model.json`.

| Beyan | Neden zorunlu |
|---|---|
| `rail` | `rail_tree.json`'daki kanonik ad ile **birebir**; iki lehçe iki modeldir |
| `band_hz` (`f_min`–`f_max`) | `Ztarget` bantsız bir sayı değildir. "Kurallar (kısa)"daki AM570x satırı bandı zaten taşıyor: VDD_CORE `≤20 MHz`, VDDS_DDR1 `≤100 MHz` |
| `z_target_ohm` + `source` | `AUTHORITATIVE_SOURCE` beşlisi ([[../_shared/coverage-gate.md]]); kaynaksız hedef `LOCAL_POLICY`, PASS değil |
| `i_step_a` / `di_dt` | Hedefin türetildiği yük geçişi; yoksa `z_target_ohm` nereden geldiği bilinmeyen bir sayıdır |

Ve parazitikler — her biri **var/yok olarak ayrı ayrı**, boş bırakılmadan:

| Parazitik | Modelde yoksa ne olur |
|---|---|
| **Mounting ESL** (pad + fanout via + iz döngüsü) | Kapasitörün kendi ESL'i **değildir**; yerleşimle değişir ve yüksek frekans ucunu o belirler |
| **Düzlem yayılımı** (spreading L/R) | Yükten kapasitöre giden yol modelde sıfır uzunluk olur; anti-rezonans tepesi olduğundan küçük çıkar |
| **Via endüktansı** (besleme **ve dönüş** via'sı çifti) | Tek via endüktansı yazmak döngünün yarısını modellemektir |
| **MLCC DC-bias derating** | §3b: 0402 X5R 10 µF/6.3 V @5 V ≈ **1.5 µF (−%85)**. Nominal kapasiteyle kurulmuş bir ZPDN modeli sayı üretir ve o sayı yanlıştır |
| **ESR(f)** | Tek nokta ESR bir eğri değildir; anti-rezonansın sönümünü ESR'in frekans davranışı belirler |
| **Yükün on-die kapasitesi** | Bandın üst ucu bu terim beyansızken tanımsızdır |

**Modelde olmayan her parazitik satır satır yazılır.** "Modellenmedi" yazılmış
bir parazitik dürüsttür; yazılmamış olan sessizdir ve modeli olduğundan iyi
gösterir. Beyanı eksik model **koşulmuş olsa bile `CONFIRM`**.

`valid_range` her eksende **birim ile** zorunludur ve bu kural repo'da zaten
uygulanıyor: **`model-registry`** (`tools/scripts/sim/model_registry.py`) zarfı
beyan edilmemiş modeli reddeder, zarf dışı sorguyu `OUT_OF_ENVELOPE →
NO_COVERAGE` yapar (kırpılmış/ekstrapole sayı değil), kaydedilmiş dosyanın
baytları oynamışsa `STALE_MODEL → FAIL` der. §Araç kancaları zaten "**her
simülasyondan önce**" diyor — ZPDN bunun istisnası değildir. Aracın kendi
uyarısı burada özellikle önemlidir: **zarf dışı olmak bir tasarım hatası
değildir** — kart yanlış değil, model yetişmiyor; ikisini karıştırmak birini
olmayan bir kusuru aramaya gönderir.

Anti-rezonans tepesi (bulk↔seramik, seramik↔düzlem kapasitansı) **bandın
içinde** aranır. Tepe beyan edilen bandın dışına düşüyorsa sonuç "tepe yok"
değil **"bakılmadı"**dır; bant genişletilir ya da satır `NO_COVERAGE` yazılır.

Kapsam aritmetiği ([[../_shared/coverage-gate.md]]): **`eligible`** = hızlı akım
geçişi olan **her ray** — `rail_tree.json`'dan alınır, "modeli olan raylar"dan
değil; evreni modelin varlığından türetmek kapsamı kendi kendine tanımlar.
**`scanned`** = §7a'nın beyanları tam olan ray. **`excluded`** yalnız
gerekçeli (ör. sabit yüklü, geçişsiz ray). `scanned + excluded == eligible`;
`scanned = 0` → **`NO_COVERAGE`**.

### (b) Ölçüm korelasyonu — `correlate`

Bu bölüm **yeni bir araç tanımlamaz**. Disiplini repo'da zaten `correlate`
(`tools/scripts/sim/correlate.py`, `correlation-report/v1`) uyguluyor:

- **Ne karşılaştırılır:** enstrümanın yazdığı Touchstone ↔ modelden üretilen
  Touchstone. Metrik sözlüğü kapalı: `magnitude_db`, `phase_deg`,
  `group_delay_ps`.
- **Hangi bant:** `--band-start-hz` / `--band-stop-hz` **beyan edilir**.
  Enstrümanın hiç süpürmediği bir yerde alıntılanan artık, model hakkında bir
  cümle değildir. Bandın §7a'daki `band_hz` ile örtüşmesi zorunludur.
- **İnterpolasyon yok:** yalnız **her iki süpürmede de var olan** frekanslar
  karşılaştırılır; ortak nokta sayısı `MIN_SHARED_POINTS = 8`'in altındaysa araç
  ortalama almaz, **reddeder** ("gürültünün ortalamasını almak" yerine).
- **De-embedding beyandır, çıkarım değil:** `--de-embedded` çağıranın ölçüm
  hakkındaki ifadesidir. Beyan edilmemiş fikstür, artık "fikstür bulaşmış" diye
  işaretlenerek raporlanır — sessizce model hatası sayılmaz.
- **Anlaşma eşiği:** `--max-magnitude-residual-db` / `--max-delay-residual-ps`.
  Eşik **çağıran tarafından** verilir ve ray/bant başına **bu skill'in**
  sahipliğindedir; verilmezse artık hesaplanır ama **yargılanmaz**.
- **Ölçüm yoksa `NO_COVERAGE`** — aracın kendi ifadesiyle model
  *yanlışlanmamıştır, doğrulanmamıştır*. `SIM_ONLY` bu boşluğu doldurmaz.
- **Ve asıl olan:** küçük bir artık, modelin ve ölçümün **birbiriyle
  uyuştuğunu** söyler; ikisinden birinin **doğru** olduğunu değil. Yanlış
  referans düzlemine de-embed edilmiş bir launch ikisini birden aynı yöne
  kaydırabilir. Bu yüzden korelasyon GREEN'i bir **`Ztarget` PASS'i değildir**:
  yalnız modelin o bantta kullanılabilir olduğunu söyler. `Ztarget` kararı
  korelasyondan **sonra**, modelin kendi çıktısıyla ve §7a'daki eşikle verilir.

> **Açık soru / varsayım** — `correlate`'in metrik sözlüğünde `impedance_ohm`
> **yoktur**; karşılaştırma S-parametre alanında yapılır. PDN ölçümü pratikte
> 2-port shunt-through'dur ve ohm alanına dönüşüm aracın **dışında** kalır.
> Bu bölüm, ZPDN korelasyonunun enstrümanın gerçekten yazdığı Touchstone
> üzerinde koşulacağını varsayar. Muhafazakâr seçim: **dB alanındaki anlaşma,
> ohm alanında bir PASS diye raporlanmaz**; ohm alanındaki karşılaştırma bugün
> araçsızdır ve `CONFIRM` kalır. Bu satır kapanana kadar `zpdn_model.json`
> hem hedefi hem artığı **hangi alanda ölçüldüğüyle birlikte** yazar.

### (c) Sınır — limit burada, ölçüm orada

`guc-isil` **limitin** sahibidir: hangi ray, hangi bant, kaç ohm, hangi
kaynak, hangi artık eşiği. Fiziksel ölçümü — kurulum, baseline, fark kaydı,
süpürme — üreten **`l3-bringup-karakterizasyon`**'dur
([[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]]).

Bu ayrım [[../_shared/rule-conflicts.md]] §9'da kayıtlı: *"yukarı akış limitin
sahibi, aşağı akış kanıtın sahibidir"*. Bir skill hem limiti hem kanıtı
sahiplenirse kendi ölçtüğü değeri kendi yazdığı eşikle karşılaştırır ve kapı bir
**tautoloji** olur — bu paketin tekrar tekrar bulduğu sahte-PASS sınıfı. Aynı
tabloda ters yön de yasak: L3 üreticisi **eşik/limit yazamaz**.

Metadata karşılığı: bu skill'in frontmatter'ı `assurance_loop: [L1, L3]` ve
`evidence_origin: TOOL_REPORT` taşır. [[../_shared/coverage-gate.md]]
§"participates-in ≠ produces-evidence-for" bunu **participates-in L3** diye
okur: bu skill ısıl/PDN doğrulamasını **ister, üretmez**. `evidence_origin`'i
`PHYSICAL_MEASUREMENT` yapmak tam da düzeltilen karışıklığı geri getirir —
`L3` üreticisi olmak için gereken köken odur ve bu skill onu yayımlamaz.
Sonuç bağlayıcı: §4'teki D satırları burada **D olarak kalır**; kapanışları L3
üreticisine devredilir, `acik_maddeler.md`'ye "kim, hangi ölçümle" satırıyla
yazılır.

### (d) Bu bölümün kapatamadıkları

| Konu | Seviye | Neden |
|---|---|---|
| Ölçülen `ZPDN(f)` | D | VNA + shunt-through fikstürü; bu skill ölçüm üretmez → `l3-bringup-karakterizasyon` |
| "Model mi ölçüm mü doğru" | — | `correlate` bunu **yapı olarak** cevaplayamaz; küçük artık yalnız *anlaşma*dır |
| Ohm alanında `Ztarget` PASS'i | C | `correlate` metriği S-parametre; ohm dönüşümü araçsız (§7b açık soru) |
| Yükün di/dt profili | C→D | Silikon içi; üretici vermiyorsa modelde **varsayım** kalır ve öyle etiketlenir |
| On-die kapasitans | C | Üretici modeli yoksa bandın üst ucu beyansızdır |
| Fikstür/de-embedding kalitesi | D | Ölçüm kurulumunun kaydı L3 üreticisinindir; `--de-embedded` yalnız **beyanı** taşır |

## Kurallar (kısa)

- Bilinmeyen akım/θ_ja → `TBD`(+spec), uydurma yok.
- θ_ja datasheet değeri gerçek bakırla 1.5–2× sapabilir → marj bırak.
- **Yalnız AM570x için** TI SPRAC76H Rev.H (Oct-2025), §7 Table 7-1,
  s.15 hedeflerini kullan: VDD_CORE `18 mΩ / 2 nH / 57 mΩ / ≤20 MHz`;
  VDD_DSP `22 mΩ / 2.5 nH / 54 mΩ / ≤20 MHz`; VDDS_DDR1
  `10 mΩ / 2.5 nH / 200 mΩ / ≤100 MHz` (erişim 2026-08-03).
  Bu değerleri başka family veya rail'e taşıma. Kapasitör BOM'unu PASS
  sayma; kart-özel ZPDN(f) modeli ve ölçüm korelasyonu yoksa verdict en
  fazla `SIM_ONLY` olsun. Modelin ne beyan edeceği ve korelasyonun nasıl
  koşulacağı: **§7**.
  `evidence_origin=AUTHORITATIVE_SOURCE`, `assurance_loop=L1+L3`,
  `status=CONFIRM` (kart-özel model/ölçüm eksikse).

## İlgili

[[../empedans-stackup/SKILL|empedans-stackup]] · [[../konnektor/SKILL|konnektor]] ·
[[../bom-lifecycle/SKILL|bom-lifecycle]] · [[../emc-esd/SKILL|emc-esd]] ·
[[../dfm/SKILL|dfm]] · [[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] (sınır: bu dosyanın §7c'si)

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`rail-budget`** — Ray ağacı kurulduktan sonra, şematiğe girmeden önce: güç açılış sırası + ray başına gürültü bütçesi.
- **`pmic-allocation`** — PMIC seçildiğinde ray/kanal tahsisi ve ek dağıtım gücü; kanal yetmezse burada görülür.
- **`current-density`** — Bakır döküldükten sonra DC akım yoğunluğu (direnç-ağı çözücü) + ısı haritası; dar boyun bu haritada çıkar.
- **`thermal-bridge`** — Junction sıcaklığı ilk-yaklaşımı; regülatör seçimini **elemek** için, imzalamak için değil.
- **`electrothermal`** — İz sıcaklığı ve ısıl kaçış tespiti. Kazanç ≥ 1 iken sıcaklık **söylemez** — reddi kapalı formdan gelir, iterasyonun oturmamasından değil.
- **`ipc-trace-width`** — IPC-2152 asgari iz genişliği; güç izlerini boyutlandırırken.
- **`ipc-via-current`** — IPC-2221 via matkap/pad çapı; güç viası boyutlandırırken.
- **`loop-stability`** — Döngü-kazanç yanıtından kararlılık marjları; SMPS/LDO kompanzasyonu tartışılırken.
- **`ngspice-bridge`** — Transient koşu + dekuplaj önerisi; sayısal bir cevap gerektiğinde ve model zarf içindeyse.
- **`model-registry`** — **Her simülasyondan önce:** kullanılan satıcı modelini adlandırır ve karakterizasyon zarfı dışında sayı döndürmeyi reddeder.
- **`correlate`** *(2026-08-10 eklendi)* — Ölçüm dosyası geldiğinde, **§7b**: modelden üretilen Touchstone ile enstrümanınkini beyan edilmiş bantta karşılaştırır. Ölçüm yokken çağrılırsa cevabı `NO_COVERAGE`'dır ve **bu doğru cevaptır**.
