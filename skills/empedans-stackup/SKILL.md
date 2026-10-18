---
name: empedans-stackup
description: Katman sayısı/sırası seçimi + IPC-2141/Wadell ile diferansiyel iz W/S çözümü + referans düzlemi ataması; sonucu DRC net-class'a geometri olarak yazar. Empedansın kendisini DRC doğrulamaz.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 2 — Altyapı (Adım 1'den sonra; şematik ile paralel)"
assurance_loop: [L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: fab_capabilities.json, schema: ../../tools/schemas/fab_capabilities.schema.json, required: false}
outputs:
  - {artifact: stackup.json, schema: references/stackup.schema.json}
  - {artifact: impedance_rules.json, schema: references/stackup.schema.json}
writes_board: false
tools: [zdiff-solver, stackup-plan, field-solver, cross-section, coupon-plan, monte-carlo]
---

# SKILL: Stackup & Empedans

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Kural çatışmaları: [[../_shared/rule-conflicts.md]] §6

Fab profili yoksa mevcut W/S/tolerans fallback'leriyle keşfi sürdür fakat
sonucu yalnız `LOCAL_POLICY/SCREEN_ONLY` say; empedans PASS verme. Yalnız
scope-doğru, kaynak/hash bağlı
[`tools/schemas/fab_capabilities.schema.json`](../../tools/schemas/fab_capabilities.schema.json)
profili ve gereken solver/coupon kanıtı verdict'i yükseltebilir.
`evidence_origin=LOCAL_POLICY`, `assurance_loop=L1+L3`, `status=SCREEN_ONLY`.

## 0. Ne zaman çağrılır

- Faz 2'de, `rail_tree.json` (güç düzlemi sayısı) ve hızlı-çift sayısı
  bilindiğinde. Şematik ile paralel koşabilir.
- **Routing'den önce.** Hesaplanan W fab minimumunun altındaysa çözüm
  routing'de değil buradadır (§2 adım 3).
- Crosstalk taraması `H`'ı küçültmeyi öneriyorsa
  ([[../crosstalk-thinning/SKILL|crosstalk-thinning]] §EM görüş) bu bir
  **stackup kararıdır** ve buraya geri döner.
- Fab preslenmiş stackup'ı geldiğinde Z hesabı **tekrarlanır**.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| güç düzlemi sayısı | `rail_tree.json` | `CONFIRM` |
| min iz / boşluk / katman limitleri | `fab_capabilities.json` | `LOCAL_POLICY/SCREEN_ONLY` |
| `Zdiff_target`, `Zse_target`, `mode_conversion_limit` | uç-cihaz dokümanı | **her birinin `source`'u zorunlu**; yoksa `CONFIRM` |
| maske kalınlığı / Dk | fab | nominal Z sign-off edilemez (§3b) |
| preslenmiş H | fab stackup | nominal ile hesap `SCREEN_ONLY` |
| `glass_style`, `panel_orientation` | fab | cam dokuma riski `CONFIRM` (§3e) |

## 2. Adımlar

1. `select_stackup()` → katman sırası; her hızlı katmanın **kesintisiz referans
   düzlemi** olmalı.
2. Hedef **tek sayı değil, üç alandır**: `Zdiff_target`, `Zse_target`,
   `mode_conversion_limit` (+ her birinin `source`'u).
   `calc_diff_pair(target, H, T, eps_r)` ile ters-çöz → (W, S).
   - `Z0 = 87/√(εr+1.41)·ln(5.98H/(0.8W+T))`, `Zodd = Z0(1−0.347·e^(−2.9S/H))`,
     `Zdiff = 2·Zodd`.
   - MIPI'de **tek-uçlu empedans da bir gereksinimdir** (LP modu tek-uçlu çalışır);
     yalnız Zdiff çözmek eksiktir. Başlangıç 100 Ω diff olabilir ama kabul
     aralığı uç-cihaz dokümanından gelir. Ölçek fikri için: TI'nin AM62x CSI
     örneği 85–115 Ω diff / 50 Ω tek-uçlu ister ve P/N skew'i Sdc/Scd
     mode-conversion terimlerine bağlar — bunu AM5706/CrossLink'e **otomatik
     taşıma**, `CONFIRM` olarak tut.
3. Hesaplanan W < fab min iz? → **stackup'ı revize et** (ince prepreg),
   routing'i değil.
4. Net-class/DRC custom rule yaz (S-Expr `(rule ... track_width ...)` veya kipy
   net-class). `impedance_rules.json` yayınla.
   > **DRC empedansı doğrulamaz.** `.kicad_dru` diline "Zdiff" diye bir kavram
   > yoktur; yazabileceğin şey geometridir (`track_width`, `diff_pair_gap`,
   > gerekirse `length`, `skew`, `diff_pair_uncoupled`, `via_count`). DRC yalnız
   > *solver'ın verdiği geometrinin korunduğunu* kanıtlar; Zdiff'in kaynağı
   > solver + fab'dır. İkisini rapor dilinde karıştırma.
   > Diferansiyel çift dış boşluğu için resmî desen:
   > `A.inDiffPair('*') && !AB.isCoupledDiffPair()`. Seri terminasyonda net
   > değiştiği için her `fromTo()` segmentini ayrı tanımla.
   > Kural sırası **ters önceliklidir** → [[../dfm/SKILL|dfm]] §DRC kural enjeksiyonu.

## Referans düzlemi (return path)

> **Bu bölüm referans düzlemi kuralının kanonik sahibidir.**
> [[../highspeed-routing/SKILL|highspeed-routing]] ve
> [[../emc-esd/SKILL|emc-esd]] buraya bağ verir.

- Her sinyal katmanına `stackup.json`'da `reference_net` yaz; her hızlı katmanın
  **kesintisiz** referans düzlemi olmalı.
- Kural: yüksek hızlı hat **split/void üzerinden geçemez** →
  `check_reference_plane_continuity()` bunu routing sonrası zorlar.
- Katman geçişi: GND→GND yakın stitching via.
- **GND→PWR geçişinde bitişik `100 nF`** yalnız yaygın yerleşim pratiği ve
  `SCREEN_ONLY`'dir; dönüş-yolu PASS kriteri değildir. Referans değişimini
  mümkünse kaldır; zorunluysa kapasitör MPN / mounting ESL / yerleşim ve gerekli
  frekans bandını modelle.
  **Tek kanonik etiket:** `evidence_origin=LOCAL_POLICY`,
  `assurance_loop=L1+L3`, `status=SCREEN_ONLY` ([[../_shared/rule-conflicts.md]] §6).

## 3. Nüanslar — hesabın ile üretilenin farkı

### (a) W/S senin değil, fab'ın çıktısıdır
"Controlled impedance" sipariş edildiğinde fab, kendi malzeme/etch verisiyle
**iz genişliğini değiştirir**. Bu yüzden fab'a `W=0.12` değil **hedef Z + katman
niyeti** ver (`impedance_rules.json` → fab notu: hangi katman çifti, hedef Z,
tolerans, referans katman). Karşılığında **impedance test coupon + rapor** iste.
Aksi halde senin W'un aynen basılır ve Z tutmaz.

**Teslimat paketi (yoksa "coupon istedik" bir şey ifade etmez):**
- Onaylı **preslenmiş** stackup + malzeme lotu.
- Her kontrollü yapı için final `W_top / W_bottom / S / T / H`.
- Kupon: IPC-2221B Ek A **Z-coupon** (veya fab eşdeğeri) — katman başına
  tek-uçlu ve/veya diferansiyel yapı, ≥114.30 mm test iletkeni.
- Yöntem: **IPC-TM-650 2.5.5.7A** (TDR).
- Rapor: hedef/tolerans + ölçülen ortalama/min/max + panel & kupon kimliği.
- Kupon **aynı** katmanı, bakır ağırlığını, W/S'yi, referans düzlemlerini ve
  soldermask koşulunu temsil etmeli — etmiyorsa temsil ettiği şey senin hattın değil.

> Kupon TDR'ı **üretim yapısını** doğrular, routed kanalı değil: via, launch,
> bend ve süreksizlikler kuponda yoktur. "TDR geçti" bir MIPI kanalı S-parametre
> sign-off'u değildir.

### (b) Solder mask mikroşerit empedansını düşürür
Maske, mikroşeridin efektif εr'sini yükseltir → Z düşer. Wadell/IPC-2141 formülü
**maskesizdir** → maske altındaki çiftte hesabın sistematik olarak yüksek çıkar.
**Stripline (iç katman) etkilenmez** — bu yüzden kritik çiftleri iç katmana
almak toleransı da iyileştirir.
- Kaba yön: çıplak mikroşerit hesabını ×0.94–0.96; dar/sıkı kuplajlı çiftlerde
  fark daha büyük. **Bu sabit çarpanı sign-off'ta kullanma** — geometriye,
  maske kalınlığına ve maske Dk'sına bağlıdır, tek bir yüzde değildir.
- Doğru yol: dış katman MIPI için **coated (kaplamalı) edge-coupled microstrip**
  modeli çöz; maske kalınlığı/Dk fab'dan gelmeden nominal Z'yi sign-off etme.
  Raporda **maskeli ve maskesiz iki sonucu birlikte** ver.

### (c) İz dikdörtgen değil, yamuk
Etch sonrası kesit trapez (üst dar). Etch factor Z'yi yükseltir ve fab'a göre
değişir. 1 oz bakırda etkisi küçük, ince iz + kalın bakırda (≥2 oz) belirgin.
Bakır pürüzlülüğü (roughness) ayrıca kaybı ve εeff'i artırır — GHz'de kayıp
bütçesini pürüzlülük belirler, formül değil.

### (d) Nominal prepreg kalınlığı ≠ preslenmiş kalınlık
Reçine akışı ve bakır doluluğu presten sonra H'yi değiştirir. `stackup.json`'a
**nominal** yaz ama fab'ın **preslenmiş** stackup'ını dosyaya al; Z hesabını o
değerle tekrarla. Ayrıca stackup **simetrik/dengeli** olmalı (bakır dağılımı
katman çiftlerinde eşit) — değilse kart reflow'da kamburlaşır (warpage).

### (e) Cam dokuma (fiber weave) skew — çift-içi skew'in görünmeyen kaynağı
> **Bu bölüm cam dokuma kuralının kanonik sahibidir.**
> [[../highspeed-routing/SKILL|highspeed-routing]] buraya bağ verir.

Cam elyaf ile reçinenin εr'si farklıdır. Bir çiftin bir bacağı elyaf demeti
üstünde, diğeri reçine üstünde giderse **aynı uzunlukta farklı gecikme** olur;
length-match temiz görünürken skew vardır ve *ölçtüğün geometri bunu göstermez*.
- Riski niteliksel kapıyla değerlendir: lane UI, toplam paralel uzunluk,
  pair pitch, glass style ve panel yönünü birlikte kaydet. Sabit açı veya
  ps/uzunluk sayısı uydurma.
- Azaltmayı fab ile seç: glass style/multi-ply, panel orientation veya açılı
  routing. Kararı `stackup.json` içinde en az `glass_style` ve
  `panel_orientation` alanlarıyla yayınla.
- Routing adımı bunu düzeltemez; ≳5 Gb/s + uzun çiftte açılı yönlendirme/malzeme
  kararı stackup'a **geri sinyaldir**.
- `evidence_origin=AUTHORITATIVE_SOURCE`, `assurance_loop=L1`, `status=CONFIRM`
  (proje stackup/fab kapsamı kapanmadıysa).

### (f) `zdiff_solver.py` bir sign-off aracı DEĞİLDİR
Kapalı-form IPC-2141 denkleminin hatası geometriye göre **sistematik olarak**
değişir; yayımlanmış karşılaştırmada aynı denklem **−%29.94 ile +%2.41**
arasında sapıyor. "Hedefe yakın çıktı" bir doğruluk kanıtı değildir.
- Çıktıya `model_class: exploratory_closed_form` + `signoff: false` yaz.
- Kontrollü empedansta **gerçek fab stackup'ı ile 2D field solver zorunlu kapı**.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek empedans | D | Fab coupon + TDR raporu (L3) |
| Maske etkisi / etch factor / preslenmiş H | C→D | Fab stackup + impedance coupon |
| Cam dokuma skew'i | C | Malzeme/panel yönü; geometriyle ölçülemez |
| Kanal S-parametreleri (via, launch, bend) | D | Kuponda yoktur; routed kanal ölçümü ister |
| Bakır pürüzlülüğü kayıp bütçesi | C | Malzeme verisi |

## 5. Doğrulama

```bash
# (W,S) taraması + self-test (fault-injection dahil)
python3 tools/scripts/zdiff_solver.py --self-test

# receipt doğrulaması
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: solver katsayısını boz → self-test **FAIL vermeli**.
Ayrıca `stackup.json`'dan `reference_net` alanını sil → hızlı katman kapısı
`NO_COVERAGE` üretmeli.

## 6. Kabul kriterleri

- [ ] Her hızlı katmanın **kesintisiz referans düzlemi** var ve `reference_net`
      `stackup.json`'da yazılı.
- [ ] Hedef üç alanla verildi (`Zdiff_target`, `Zse_target`,
      `mode_conversion_limit`) ve her birinin `source`'u var.
- [ ] MIPI/LP gibi tek-uçlu gereksinimi olan arayüzlerde `Zse_target` boş değil.
- [ ] Hesaplanan W < fab min ise **stackup revize edildi**, routing zorlanmadı.
- [ ] `impedance_rules.json` DRC'ye **geometri** olarak yazıldı; raporda
      "DRC empedansı doğruladı" ifadesi yok.
- [ ] Fab'a **hedef Z + tolerans + referans katman** verildi (W değil) ve
      impedance coupon + IPC-TM-650 2.5.5.7A raporu **istendi**.
- [ ] Maskeli ve maskesiz Z sonucu **birlikte** raporlandı; sabit ×0.94–0.96
      çarpanı sign-off'ta kullanılmadı.
- [ ] Preslenmiş stackup geldiğinde Z hesabı tekrarlandı; stackup simetrik/dengeli.
- [ ] Cam dokuma riski için `glass_style` + `panel_orientation` yayınlandı.
- [ ] Toleransı üretici ±%10 (kontrollü ±%5–8) **merkezine** oturt, kenara değil.
- [ ] Solver çıktısı `model_class: exploratory_closed_form` + `signoff: false`
      taşıyor; kontrollü empedansta field solver kapısı açık.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## Kurallar (kısa)

- Diff çift katman değiştirirse → GND stitching via çifti (referans dönüşü).
- εr frekansta düşer; MIPI GHz için yüksek-hız malzeme ya da frekans-düzeltmeli εr.

## İlgili

[[../highspeed-routing/SKILL|highspeed-routing]] ·
[[../crosstalk-thinning/SKILL|crosstalk-thinning]] · [[../dfm/SKILL|dfm]] ·
[[../emc-esd/SKILL|emc-esd]] · [[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`stackup-plan`** — Tasarım-niyet JSON'undan katman sayısı / stackup planı; empedans çözümünden önce.
- **`field-solver`** — Kontrollü empedansta **zorunlu kapı**: kapalı form yerine gerçek elektrostatik problemi çözer (`zdiff-solver` `signoff: false`).
- **`cross-section`** — Kartın bir bölgesini ajanın **görebileceği** PNG'ye basar; bir ölçüm tartışmalı olduğunda bakılacak şey.
- **`coupon-plan`** — Empedans test kuponu planı — gerber çıkarılmadan **önce** koşar, sonra geç kalınır.
- **`monte-carlo`** — Tolerans analizi (`circuit` + `impedance`). Verimler Wilson %95 aralığıyla gelir; 100/100 kesinlik demek değildir.
