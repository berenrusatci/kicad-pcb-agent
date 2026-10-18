---
name: konnektor
description: Konnektör seçimi ve yerleşimi — mating cycle/kaplama, akım derating, mekanik yük ve strain relief, FPC/ZIF landing, shell/şase topraklama, hot-plug sırası. Faz 1'de seçim, Faz 3'te keepout uygulaması.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 1 (seçim: mekanik + BOM ile birlikte) + Faz 3 (yerleşim/keepout uygulaması)"
assurance_loop: [L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: interface_contract.json, schema: ../sematik-erc/references/interface_contract.schema.json, required: true}
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: case.dxf, schema: null, required: false}
outputs:
  - {artifact: connector_spec.json, schema: references/connector_spec.schema.json}
  - {artifact: keepout_zones.json, schema: ../mekanik-dxf/references/keepout_zones.schema.json, role: contributor}
  - {artifact: esd_chain.json, schema: ../emc-esd/references/esd_chain.schema.json, role: contributor}
writes_board: false
tools: [ipc-clearance, current-density, ipc-dfa, connector-current]
---

# SKILL: Konnektörler

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

> Konnektör, kartın **elektriksel değil mekanik olarak** en çok arızalanan
> parçasıdır ve sahadaki arızaların çoğu şematikte görünmez. Seçim Faz 1'de
> yapılır: paket/keepout mekaniği, akım derating güç bütçesini, kaplama fab
> sürecini ve maliyeti değiştirir.

## 0. Ne zaman çağrılır

- Faz 1'de, `rail_tree.json` + mekanik datum + `interface_contract.json`
  hazır olduğunda. Şematikten **önce**: koruma zinciri ve pinout kararı buna bağlı.
- Faz 3'te yeniden: eşleşme koridoru + aktüatör hacmi keepout'a uygulanır ve
  kenar/depanel yakınlığı kontrol edilir.
- Konnektör akım derating'i rayın worst-case akımının altına düşerse
  L2 geri besleme kenarıyla [[../guc-isil/SKILL|guc-isil]] yeniden açılır.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| `role` / `direction` | `interface_contract.json` | Rol açık değilse koruma MPN/limit seçme, `CONFIRM` |
| `datasheet_cycles` | konnektör datasheet | `CONFIRM` |
| derating eğrisi | konnektör datasheet | oda sıcaklığı ratingi **kullanılamaz** → `CONFIRM` |
| ortam sıcaklığı | `thermal_report.json` | `CONFIRM` |
| `fpc_thickness_mm` | konnektör datasheet | `TBD` — "0.30 mm hep doğrudur" varsayımı yanlış |
| landing pattern | datasheet | **türetme** — `NO_COVERAGE` |
| `actuator_*_envelope_mm` | datasheet + STEP | `CONFIRM`; keepout eksik kalır |

## 2. Adımlar

1. Ürün rolünü `interface_contract.json`'dan dondur (yalnız ölçüm / güç girişi /
   güç çıkışı / çift yönlü). Akım-inrush-Vdrop ve reverse/OVP hedefleri
   kapanmadan TVS/PTC/eFuse seçme.
2. Kaplama ve mating cycle kararını ver (§3a) → `connector_spec.json`.
3. Akımı **derating sonrası** kontrol et (§3b) ve `rail_tree.json` ile çapraz doğrula.
4. Mekanik yük, ankraj ve strain relief kararını ver (§3c).
5. FPC/ZIF landing pattern'i datasheet'ten **kopyala** (§3d).
6. Shell/ekran topraklama için üç seçenekli ayak izi bırak (§3e).
7. Hot-plug sırası ve inrush çözümünü kaydet (§3f).
8. Keepout eklerini `keepout_zones.json`'a **üç ayrı `kind`** ile yaz:
   `connector_body`, `connector_mating_corridor`, `connector_actuator_sweep`
   ([[../mekanik-dxf/SKILL|mekanik-dxf]] şemanın sahibidir).

## 3. Nüanslar — bilinen tuzaklar

### (a) Kaplama ve takma-çıkarma ömrü (mating cycles)
- Datasheet'teki **mating cycle** sayısı bir spesifikasyondur; kullanım senaryonu
  (üretim testi + servis + kullanıcı) toplayıp karşılaştır.
- **ENIG kart-kenarı (gold finger) kontağı için uygun değildir**: altın katmanı
  0.05–0.13 µm mertebesinde, tipik **500–1000 çevrimde** aşınır, sonra korozyon.
- Kart-kenarı kontak → **sert altın (hard gold)**, ~0.76–1.27 µm (30–50 µin)
  altın + 1.27–3.81 µm nikel; ~100k çevrim mertebesi. Bu **ayrı bir fab süreç
  kalemidir** (selective plating), gerber'e ve fab notuna yazılır, maliyeti artırır.
- Gereken çevrim datasheet sınırını aşarsa `RED`.

### (b) Akım: datasheet değeri "tek kontak, boş kart" değeridir
Konnektör akım ratingi, tipik olarak **tek kontak**, **30 °C sıcaklık artışı**,
20–25 °C ortam ve standart test kartında ölçülür. Gerçek kartta:
- **Komşu kontaklar ısıyı toplar.** Bitişik 2 taşıyıcı kontak ≈ **%7**, bitişik
  3 kontak ≈ **%16** derating (ayrık yerleşimde ≈%9). Ortadaki kontak en sıcaktır.
- **Paralel pinler akımı eşit paylaşmaz**: ısınan orta pinin direnci artar,
  akım dış pinlere kayar → `N × I_rated` **yanlış** bir üst sınırdır. Paralel
  güç pininde belirgin marj bırak ve GND dönüşünü de aynı sayıda pinle taşı.
- Ortam sıcaklığı `thermal_report.json`'daki değerdir (85 °C senaryosu) →
  derating eğrisinden oku, oda sıcaklığı ratingini kullanma.
- Sonuç `rail_tree.json` ile **çapraz doğrulanır**: konnektörün geçirebildiği
  akım, rayın worst-case akımından küçükse kısıt konnektördür, izde değil.

### (c) Mekanik yük gerçek yüktür
- Takma/çıkarma kuvvetini **lehim tabları / through-hole ayaklar** taşır, sinyal
  pinleri değil. Bu ayakları büyük bakıra bağla, thermal relief'i abartma
  (lehimlenmezse tab işe yaramaz).
- SMT konnektörde pad kalkması (pad lift) tipik arıza → mümkünse en az iki
  mekanik ankraj veya through-hole varyant.
- **Keepout üç boyutludur:** gövde + **eşleşme koridoru** + **kilit kolu /
  aktüatör süpürme hacmi**. Şema ve `max_allowed_height_mm` kuralı
  [[../mekanik-dxf/SKILL|mekanik-dxf]]'te; buradaki katkı üç `kind` kaydıdır.
- Konnektör kart kenarındadır → kenar **bükülme bölgesidir**: çevresindeki
  seramikler flex crack adayıdır ve depanel hattına yakınsa iki stres üst üste
  biner ([[../dfm/SKILL|dfm]] §MLCC flex crack).
- Kablo tarafında **strain relief** kartın sorumluluğu: ZIF/FPC kilidinin çekme
  dayanımı yoktur; kablo ayrıca sabitlenmeli.

### (d) FPC / ZIF landing
- **Pad pitch kablonunkiyle bire bir aynı olmalı.** 0.05 mm'lik bir hata 60
  pozisyonluk konnektörde uzak uçta birikerek hizayı kaçırır — üreteç script'inde
  pitch'i sabitten değil konnektör datasheet'inden al.
- Pad genişliği tipik olarak pitch'in **%60–70**'i (0.5 mm pitch → ~0.3 mm pad);
  pad **uzunluğu** konnektöre özgüdür → datasheet landing pattern'i kopyala,
  türetme.
- **Kontak yüzü (top/bottom contact) FPC'nin hangi yüzünde iz olduğunu belirler.**
  Yanlış seçim = kablo ters takılır veya hiç çalışmaz; footprint'e ve FPC
  çizimine aynı anda yaz.
- FPC kuyruk kalınlığı konnektöre özgüdür — "**0.30 mm hep doğrudur**" varsayımı
  yanlış; datasheet'ten doğrula, yoksa `TBD`.
- Aktüatör (flip-lock) için önünde/üstünde temiz alan; FPC'nin **bükülmeden**
  düz girebileceği koridor.
- Yüksek hız FPC üzerinden gidiyorsa (MIPI): konnektör geçişi bir empedans
  süreksizliğidir; çift, konnektör pininde ayrılmamalı, GND pinleri çiftin
  **iki yanında** olacak şekilde pinout seç. Kanal bütçesi:
  [[../highspeed-routing/SKILL|highspeed-routing]].

### (e) Shell / kablo ekranı topraklama — üç okul, tek dürüst cevap
Ekranı (a) doğrudan GND, (b) 1 nF–0.47 µF kapasitör (+ ~1 MΩ paralel),
(c) ferrit ile bağlamak: **TI, Intel, FTDI ve Cypress birbirleriyle çelişiyor**;
doğru seçim kasa, kablo ve ürünün topraklama mimarisine bağlı — masabaşında
kesin karar verilemez.
- **Uygulanabilir karar:** yerleşimde **üçünü de** destekleyen bir ayak izi bırak
  (aynı pad çiftine 0 Ω / kondansatör / ferrit takılabilsin), varsayılan olarak
  birini monte et, kalanları DNP. EMC ölçümünde tek değişkenle karşılaştır
  ([[../emc-esd/SKILL|emc-esd]] §Ön-uyum ölçümü).
- Shell pini konnektörün altında **kesintisiz GND**'ye birden fazla via ile
  gitmeli; tek ince iz, ekranı anten yapar.
- Şase toprağı ile sinyal toprağı ayrıysa birleşme noktası **tek** ve belgeli olsun.

### (f) Hot-plug ve sıra
- Takılabilir arayüzde kontak uzunlukları eşit değildir: **GND önce girer, en son
  çıkar (mate-first / break-last)**, sonra güç, en son sinyal. Konnektör bunu
  desteklemiyorsa akış girişi (inrush) ve sinyal-önce-güç durumunu devrede çöz.
- Inrush: bulk kapasiteye anlık akım, konnektör kontağını aşındırır (arc) →
  soft-start / NTC / e-fuse. Bu `rail_tree.json`'daki *ortalama* akımda görünmez.
- Ters/yanlış takma: keying, asimetrik pinout, pin-1 işareti ipek baskıda
  **kart üstünde** olsun (konnektörün altında kalan işaret montajda okunmaz).
- Kullanıcının erişebildiği her konnektör = ESD giriş noktası → koruma zinciri
  ([[../emc-esd/SKILL|emc-esd]] §ESD).

### (g) DFT ile kesişim
Konnektör pinleri doğal test erişimidir; ama ölçüm için ayrı TP koy — kontak
yüzeyine prob değdirmek kaplamayı bozar ([[../dft-testpoints/SKILL|dft-testpoints]]).

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Mating cycle ömrü | C | Datasheet spesifikasyonu; aşınma sahada görülür |
| Kaplama aşınması / korozyon | C→D | Çevrim testi gerekir |
| Gerçek mating kuvveti / hizalama | D | Fiziksel fixture; STEP nominal ön-eleme |
| FPC/konnektör launch discontinuity | C→D | S-parametre / TDR gerekir |
| Kontak akım paylaşımı | C→D | Elektro-termal ölçüm |

## 5. Doğrulama

```bash
python3 tools/scripts/gate_receipt.py gate_receipt.json

# Akım çapraz doğrulaması (§3b). Üç girdi de zorunlu: kapasite ve talep ayrı
# belgelerden gelmezse çapraz doğrulama, belgenin kendisiyle karşılaştırılması olur.
python3 tools/scripts/connector_current.py \
  --connector-spec connector_spec.json \
  --rail-tree rail_tree.json \
  --map system-planning/connector_rail_map.json
```

Fault-injection: `cycles_required` değerini `datasheet_cycles`'ın üstüne çek →
verdict **`RED`** olmalı. Olmuyorsa mating kapısı boştur.

Fault-injection (akım): `derated_a_per_contact`'i `null` yap ama
`rated_a_per_contact`'i bırak → `connector_current.py` **`NO_COVERAGE`** ve
`ROOM_TEMPERATURE_RATING_NOT_SUBSTITUTED` vermeli. Oda sıcaklığı ratingine
düşüyorsa kapı boştur.

## 6. Kabul kriterleri

- [ ] Konnektörün ürün rolü `interface_contract.json` içinde kesin: yalnız
      ölçüm / güç girişi / güç çıkışı / çift yönlü; akım-inrush-Vdrop ve
      reverse/OVP hedefleri kapanmadan TVS/PTC/eFuse seçilmedi.
- [ ] Her konnektör için `cycles_required` vs `datasheet_cycles` **yazılı**;
      kart-kenarı kontak varsa sert altın fab notu mevcut.
- [ ] Akım kontrolü **derating sonrası** yapıldı (komşu kontak + ortam sıcaklığı);
      paralel pin varsayımı `N×I` değil.
- [ ] Konnektör akımı `rail_tree.json` worst-case akımıyla çapraz doğrulandı.
- [ ] Eşleşme koridoru + aktüatör hacmi `keepout_zones.json`'da **üç ayrı `kind`**
      olarak var; komşu parça yüksekliği kontrol edildi.
- [ ] STEP kontrolü yalnız nominal ön eleme sayıldı; gerçek FPC kalınlığı,
      insertion yönü, board flatness ve aktüatör açık/kapalı durumları fiziksel
      mating fixture'ında doğrulandı veya açık `CONFIRM` bırakıldı.
- [ ] Fixture kaydı `actual_connector_mpn`, `actual_fpc_thickness`,
      `datum_fixture_used`, `insert_remove_without_force`,
      `actuator_full_lock_visible` ve `post_mate_clearance_margin` taşıyor;
      yarım kilit, hasar veya yön problemi `RED`.
- [ ] Footprint'teki `PCB Edge` yardımcı çizgisi gerçek Edge.Cuts ile sayısal
      karşılaştırıldı; konnektör pin geometrisinin zorladığı layer crossover
      routing başlamadan SI/mekanik kapıda değerlendirildi.
- [ ] FPC landing pattern datasheet'ten alındı (pitch/pad/uzunluk/kontak yüzü),
      türetilmedi; kuyruk kalınlığı doğrulandı veya `TBD`.
- [ ] Yüksek hızlı FPC/konnektörde launch+pad+temas discontinuity'si kanal
      bütçesine girdi; S-parametre/empedans verisi yoksa PASS yerine `CONFIRM` yazıldı.
- [ ] Ekran/shell için üç seçenekli ayak izi bırakıldı, varsayılan belgelendi.
- [ ] Hot-plug arayüzlerinde GND mate-first doğrulandı veya inrush çözümü var.
- [ ] Kenar/depanel yakınındaki seramikler flex crack açısından kontrol edildi.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../mekanik-dxf/SKILL|mekanik-dxf]] · [[../emc-esd/SKILL|emc-esd]] ·
[[../guc-isil/SKILL|guc-isil]] · [[../bom-lifecycle/SKILL|bom-lifecycle]] ·
[[../dfm/SKILL|dfm]] · [[../dft-testpoints/SKILL|dft-testpoints]] ·
[[../highspeed-routing/SKILL|highspeed-routing]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`connector-current`** — §3b'nin çapraz doğrulaması: `connector_spec.json`
  (kapasite) × `rail_tree.json` (talep) + ray→kontak-grubu eşlemesi. Adım 3'te,
  derating sonrası akım kararı verilirken çağrılır. Datasheet ratingi
  **kapasitedir, talep değildir**; `N × I_rated` yüklenmez, derating eğrisi
  yoksa oda sıcaklığı ratingi **yerine konmaz** (`NO_COVERAGE`). Eşleme dosyası
  `system-planning/` altında yaşar, araçta değil:
  `tools/schemas/connector_rail_map.schema.json`.
- **`ipc-clearance`** — Gerilim/irtifa açıklığı; konnektör pin aralığı ve hot-plug sırası tartışılırken.
- **`current-density`** — Bakır döküldükten sonra DC akım yoğunluğu (direnç-ağı çözücü) + ısı haritası; dar boyun bu haritada çıkar.
- **`ipc-dfa`** — IPC-A-610 montaj açıklığı; yerleşim sıkıştığında bileşen arası mesafeyi karara bağlar.
