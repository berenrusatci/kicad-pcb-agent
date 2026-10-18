---
name: dogrulama-matrisi
description: Her kuralın test edilebilirlik seviyesi (A/B/C/D), hangi araçla ölçüldüğü, hangi döngüde (L0..L3) koştuğu ve başarısızlıkta ne olacağı. "Yazılımla kanıtlanamaz" olanların açık listesi.
version: 1.1.0
last-reviewed: 2026-08-10
phase: "Faz hepsi — denetim ve ölçüm otoritesi"
assurance_loop: [L0, L1, L2, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: board, schema: null, required: true}
  - {artifact: netlist.xml, schema: null, required: true}
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: stackup.json, schema: ../empedans-stackup/references/stackup.schema.json, required: true}
  - {artifact: connector_spec.json, schema: ../konnektor/references/connector_spec.schema.json, required: true}
outputs:
  - {artifact: checks.json, schema: ../../tools/schemas/metric_coverage.schema.json}
  - {artifact: acik_maddeler.md, schema: null}
  - {artifact: "evidence/", schema: null}
  - {artifact: l3_rule_universe.json, schema: references/l3_rule_universe.schema.json}
writes_board: false
tools: [gate-receipt, dfm-emc-check, mask-dam, stitch-density, via-stub, zdiff-solver, assert-matrix, benchmark, verify-manifest]
---

# SKILL: Doğrulama Matrisi — test, döngü, araç

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] (seviye/döngü sözlükleri) ·
Ölçüm hatası sınıfları: [[references/measurement-error-classes.md]] ·
Araç envanteri: [[../../docs/tool-inventory.md]]

Bu repodaki kuralların çoğu **iddia** olarak yazılabilir ama hepsi aynı
şekilde **kanıtlanamaz**. Bu dosya her kuralı dört seviyeden birine oturtur ve
karşılığına aracı + döngüyü koyar. **Seviyesi yazılmamış kural, kural değildir;
"uygulandı" denemez.**

## 0. Ne zaman çağrılır

- Her faz sınırında (L1), kapı açılmadan önce.
- Bir kural/eşik eklendiğinde: seviyesi (A/B/C/D) ve aracı bu matrise yazılmadan
  skill'e girmez.
- Bir ölçüm şüpheliyse: §3 hata sınıflarına karşı denetlenir.
- Araştırma bulgusu bir skill'e terfi edilecekse
  ([[../pcb-orchestrator/SKILL|orchestrator]] §Terfi) seviye ataması burada yapılır.

## 1. Seviye taksonomisi

| Seviye | Anlamı | Kanıt biçimi |
|---|---|---|
| **A** | Board/şematik **dosyasından** ölçülebilir | script çıktısı (JSON), tekrar üretilebilir |
| **B** | KiCad'in kendi denetleyicisi (DRC/ERC/parity) | `drc.json` / `erc.json`, bayraklarıyla |
| **C** | **Belge kapısı** — bilgi dosyada yok, kararı biz veriyoruz | imzalı satır: `karar + kaynak + tarih` |
| **D** | **Fiziksel ölçüm** — kart/lab olmadan bilinemez | ölçüm kaydı (kurulum + baseline + fark) |

> C'yi A gibi göstermek bu repodaki en tehlikeli hata: "kontrol edildi ✓"
> yazan ama hiçbir şey ölçmeyen satır. C maddesi **kaynak (datasheet/standart)
> ve tarih** olmadan kapanmaz.

### Seviye tek eksen değildir (2026-08-10)

A/B/C/D yalnız **"kanıt nasıl elde edildi"** sorusunu cevaplar. Matris zaman
içinde iki soruyu daha aynı hücreye sıkıştırdı ve ikisi de seviye değildi:

| Yazılan | Aslında söylediği | Doğru eksen |
|---|---|---|
| `A(kısmi)` | araç kuralın bir **parçasını** ölçüyor | `coverage: partial` |
| `A(screen)` | araç ölçüyor ama **imzalamaya yetkili değil** | `authority: screen_only` |

Bu yüzden `tools/common/matrix.py` ikisini de çözmeyi reddediyordu
(`PARTIAL_LEVEL`) ve bu satırlar aylarca `UNASSERTED_RULE` kaldı: söylemek
istedikleri şeyin assertion dilinde karşılığı yoktu. Artık var.

* **`level`** — A/B/C/D. Kanıt nasıl elde edildi.
* **`coverage`** — `full` / `partial`. Kuralın ne kadarına ulaşıyor.
  `partial` ise **`residual_evidence` zorunludur**: neyin kapsanmadığı
  yazılmadan "kısmi" demek, bir şey söylemeyen alçakgönüllülüktür.
* **`authority`** — `signoff` / `screen_only` / `record_only` /
  `physical_acceptance`. Temiz bir sonucun neyi kapatmaya yetkili olduğu.
  **A/B için varsayılanı yoktur**: kapalı-form bir tarama ile field solver
  ikisi de sayı üretir, yalnız biri imza kapısını açabilir.
* **`coverage_selector`** — hangi alan gerçekten bir şeye bakıldığını
  kanıtlıyor. Bunsuz "ihlal yok" ile "hiçbir şeye bakılmadı" aynı sayıdır:
  `metrics.<check>` ihlal **sayısı** taşır ve boş bir kontrol sıfır ihlal
  bildirir. Bu yüzden `by_check.<check>.scanned` bağlanır.

Bir satır ancak niteleyicisinin adlandırdığı ekseni beyan eden bir assertion
tarafından sahiplenilebilir; düz `A` yazmak hâlâ **tam olarak kimsenin
otomatikleştirmediği kısmı** iddia eder ve reddedilir.

### Ölçüm semantiği kapısı

Bir metrik ancak `metric_definition`, units, distance semantics, region,
sampling step, geometry model ve tool/input hash alanları tam ise A-seviyesi
kanıt olabilir. Centerline↔edge-gap veya arc↔chord karışırsa `INVALID_METRIC`.

Positive + negative fault-injection olmadan hiçbir checker "canlı" sayılmaz.

## 2. Matris

### Faz 1 — güç, ısıl, mekanik, konnektör, BOM
| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| Rail worst-case akım + IR drop | A | `calculate_power_budget` (`rail_tree.json`) | L1 | rail yeniden boyutlandır |
| IPC-2221/2152 iz genişliği + hangi standart | A | aynı; rapora `standard:` alanı | L1 | 2152 ile yeniden bak, sonra katman/bakır |
| **MLCC DC-bias → efektif kapasite** | **C** | üretici DC-bias eğrisi; `c_eff_uf` alanı | L1 + L2→BOM | eğri yoksa `TBD`, parça değiştir |
| θ_ja / ψ_jt kaynağı (JEDEC kartı) | C | datasheet tablosu | L1 | marj yaz, `CONFIRM` |
| Konnektör mating cycle + kaplama | C | konnektör datasheet + fab süreç notu | L1 | parça/kaplama değiştir, maliyet güncelle |
| **Konnektör akımı (derating sonrası)** | C→A | datasheet derating eğrisi → `connector_spec.json`, `rail_tree` ile çapraz | L1 + L2→güç | kontak sayısı artır / konnektör değiştir |
| MSL / paketleme / sıcaklık sınıfı | C | distribütör + datasheet | L1 | alternatif parça (L2→şematik) |
| Lifecycle / stok / alternatif | A | Nexar GraphQL (ağ ana ajanda) | L1, periyodik | alternatif ara, footprint feedback |
| Keepout hacimleri (gövde+eşleşme+aktüatör) | A | `keepout_zones.json` + placement Z kontrolü | L1 → bariyer | yerleşimi değiştir |

### Faz 1 — arayüz eşleşmesi (fiziksel ara-bağlantı zinciri)

Sahip: [[../arayuz-eslesme-kapisi/SKILL|arayuz-eslesme-kapisi]] ·
Araç: `interconnect_mating.py` · Kapı `sematik-erc`'yi **bloklar**.

**Hiçbir satır düz `A` değil** (R11/R12/R13 hariç — onlar pin haritasından
ölçülür): `A`'nın tanımı *"board/şematik dosyasından ölçülebilir"*dir ve **iki
belgenin karşılaştırması board'dan ölçülmez**. Emsal: yukarıdaki *"Konnektör
akımı (derating sonrası)"* satırı da `C→A`.

**Kapsam paydası bu satırların hepsinde `interface_contract.json`'ın gerektirdiği
bağlantı kümesinden türer**, beyan edilmiş zincir listesinden değil
(`docs/BULGU-bos-kontrol-temiz-gorunur.md:79-85` — yanlış populasyondan gelen
payda gerçek bir muhafızla aynı yeşili verir).

| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| **R1 Zincirde `medium` mevcut (eksik halka)** | C→A | `interconnect_mating.py` → `NO_MATED_PART` / `MISSING_TRANSITION` | L1 + L2→BOM | kablo/adaptör kalemini tanımla, BOM'a yaz |
| R2 Arayüz sınıfı uyumu | C→A | aynı; `INTERFACE_CLASS_MISMATCH` | L1 | konnektör ailesini ya da medium tipini değiştir |
| R3 Pitch eşitliği | C→A | aynı; `PITCH_MISMATCH` | L1 | konnektör ya da kablo değiştir |
| **R4 Pozisyon sayısı eşitliği — `authority: screen_only`, imza AÇMAZ** | C→A | aynı; `POSITION_COUNT_MISMATCH`. Temiz sonuç imzalamaz; imzayı R10+R11 taşır | L1 | kontak sayısını eşitle; PASS'ı kanıt sayma |
| R5 Cinsiyet (receptacle/plug) | C→A | aynı; `GENDER_CONFLICT` | L1 | cinsiyeti düzelt |
| R6 Kontak yüzü (stiffener karşı yüzde) — `authority: record_only` | C | aynı; `CONTACT_FACE_MISMATCH` | L1 | kayda geç, `CONFIRM`; kararı `konnektor` verir |
| R7 Kablo tipi tutarlılığı (`contact_orientation` ↔ stiffener çifti) | C→A | aynı; `CABLE_TYPE_MISMATCH`. Satıcı harfi (Molex A/D, Würth 1/2) anahtar DEĞİL | L1 | normalize enum'u düzelt, satıcı harfini provenansta bırak |
| R8 Kuyruk kalınlığı yuvanın bandında — `authority: record_only` | C | aynı; `TAIL_THICKNESS_OUT_OF_RANGE` | L1 | kayda geç, `CONFIRM` |
| R9 Kuyruk genişliği & margin geometrik tutarlılığı | C→A | aynı; `TAIL_GEOMETRY_MISMATCH` | L1 | kablo geometrisini düzelt |
| **R10 `pin_to_pin[]` çizelgesi zorunlu** | C→A | aynı; `PINMAP_UNDECLARED` / `MAPPING_UNDECLARED`. İmzanın taşıyıcısı | L1 → Faz 2 bloklu | çizelgeyi yaz; geometri eşitliği yerine geçmez |
| **R11 Net kimliği (birebir) + sıra** | A | aynı; `NET_NAME_MISMATCH` / `PIN_ORDER_MISMATCH`. Alias tablosu YASAK | L1 | sözleşmeyi ya da kartı düzelt (normalleştirme YOK) → **L2 kenarı sözleşmeye** |
| **R12 Güç/veri çarpışması — KALICI HASAR** | A | aynı; `POWER_ON_SIGNAL_PIN` | L1 | pin haritasını düzelt; **bring-up öncesi bloklayıcı**, R6 ile aynı önceliğe koyma |
| R13 Diferansiyel çift bütünlüğü | A | aynı; `DIFFPAIR_SWAP` / `DIFFPAIR_SPLIT` | L1 | polarite/çift atamasını düzelt |
| R14 Lane sayısı (uçtan uca) | C→A | aynı; `LANE_COUNT_MISMATCH`. Fiziksel kapı geçilse bile mimari geçersiz | L1 | host/sensör lane sayısını uzlaştır |
| R15 Mezzanine stack height | C→A | aynı; `STACK_HEIGHT_MISMATCH` | L1 → mekanik | stack height'ı uzlaştır |
| R16 Mating half MPN beyan listede — `authority: record_only` | C | aynı; `NOT_A_VALID_MATING_PART`. Üretici verisi makine-okunur değil | L1 | insan girişi + kaynak URL |
| R17 Provenans zorunluluğu (alan başına 5 alan + `basis`) | C | aynı; `UNVERIFIED_ATTRIBUTE` → **cap**, en fazla `CONFIRM` | L1 | kaynağı tamamla; `derived`/`vendor_page` imza açmaz |
| R18 Parça belirlenmişlik (MPN+üretici+revizyon) | C→A | aynı; `BLOCKED_UNSPECIFIED`. NDA'lı pinout burada kalır | L1 + L2→BOM | parçayı belirle; "geçti" DİYEMEZ |
| R19 Kaplama uyumu — `authority: record_only` | C | aynı; `PLATING_MISMATCH` | L1 | kayda geç; kaplama kararı `konnektor`'ün |
| R20 Mating cycle bütçesi — `authority: record_only` | C | aynı; `MATING_CYCLE_BUDGET` (`--bringup-mate-cycles`, varsayılanı YOK) | L1 | bütçeyi ya da parçayı değiştir |
| Fiziksel eşleşme fixture'ı (kuvvet, hizalama, aktüatör kilidi) | **D** | `l3_rule_universe.json` satırı → [[../l3-bringup-karakterizasyon/SKILL\|l3-bringup-karakterizasyon]]; limitin sahibi eşleşme kapısı | L3 | fixture ölçümü; yazılımla kapanmaz |
| Pin-1 köşesi uzaysal ilişkisi (mirroring paritesi) | **C** | hiçbir datasheet alanı taşımaz; `pin_to_pin[]`'i insan imzalar | L1 + L3 | insan imzası ya da L3 süreklilik ölçümü |

### Faz 2 — şematik, stackup, DFT
| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| ERC error = 0 (bayraklarıyla) | B | `kicad-cli sch erc --severity-error --severity-warning --exit-code-violations` | L0 | düzelt, PWR_FLAG ile susturma |
| Netlist ↔ şematik parity | B | `kicad-cli pcb drc --schematic-parity --severity-all` | L0 | düzelt |
| Wire sayısı > 0 / uçan net yok | A | `sch_wire.py` + `(wire` sayımı | L0 | yeniden yönlendir |
| **Floating giriş / strap / kutup / kristal CL** | **C** | `sematik_riskler.md` satır satır | L1 | satır kapanmadan Faz 3 açılmaz |
| **I²C pull-up hesabı** | C→A | `i2c_pullup.py` (`Rmin=(VDD−VOL)/IOL`, `Rmax=tr/(k·C_bus)`, k=ln((1−f_lo)/(1−f_hi))≈0.85); paralel eşdeğer netlist'ten. **A olması için** VDD/VOL/IOL/C_bus/tr'nin kaynaklı (doküman+revizyon) girilmesi şart — aksi halde NO_COVERAGE | L1 | direnç değeri değiştir |
| Empedans W/S çözümü | A | `zdiff_solver.py` + `impedance_rules.json` | L1 | stackup revizyonu (routing değil) |
| **Maske etkisi / etch factor / preslenmiş H** | D | fab stackup (pre-fab **önkoşul**, L1) + **impedance coupon ölçümü** (kapatan kanıt) | L3 | fab raporuyla hesabı güncelle |
| Cam dokuma (fiber weave) riski | C | veri hızı × uzunluk eşiği + malzeme seçimi | L1 | malzeme/açı kararı, routing'e not |
| Netclass↔net **ileri yön** (kısıtlı her net var mı) | A | `dfm_emc_check.py::hs_netclass_assigned` | L0 | pattern düzelt |
| Netclass↔net **ters yön** (kısıt gereken her net kısıtlı mı) | **C** | elle gözden geçirme; yürütülebilir predicate **yok** | L1 | net adını/kısıtı ekle → [[../sematik-erc/SKILL\|sematik-erc]] §10 |
| Netclass↔net **aşırı yakalama** (regex kısıt gerekmeyen neti çekiyor mu) | **C** | elle; `hs_regex` evreninin kendisi test edilmedi | L1 | pattern daralt |
| TP kapsamı (güç+debug %100) | A | `testpoint_plan.json` ↔ `rail_tree.json` | L1 | TP ekle |

> **2026-08-10 — netclass satırı üçe bölündü ve iki parçası A'dan C'ye indi.**
> Tek satır hâlinde `A` diyordu ama parantez içinde "ters yön elle" yazıyordu:
> yürütülebilir predicate'i olmayan bir yön `A` olamaz — bu, seviye tanımının
> kendi kuralıdır (§Seviye taksonomisi) ve bu paketin tekrar tekrar bulduğu
> sahte-PASS sınıfının ta kendisidir. Ölçüm: `assert_matrix.py` bu satır için
> hiçbir assertion tutmuyordu (`UNASSERTED_RULE`), yani `A` iddiası hiçbir
> yerde sınanmıyordu. Ayrıca ileri yönün `eligible` evreni bir **ad regex'i**
> ile tanımlıdır; CAM-W'de adsız kalan CSI netleri evrene hiç girmediği için
> kontrol kalan netlerde PASS veriyordu (`../../docs/BULGU-isimsiz-diferansiyel-netler.md`).
> Kısıtlanmış evren, boş evrenden daha sessizdir. Kaynak sözleşmesi ve kapanış
> yolu: [[../sematik-erc/SKILL|sematik-erc]] §10.

### Faz 3 — yerleşim ve routing
| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| DRC 0 ihlal (repo rule set) | B | `drc.sh` ⚠️ dış araç → §5 | L0 | düzelt |
| Unrouted 0 / short 0 | A | `measure.py` ⚠️ dış araç → §5 | L0 | route |
| Intra/inter-pair skew (**ps**) | A | `measure.py` ⚠️ (ark dahil `GetLength()`) | L0 | bütçenin altındaysa **meander ekleme** |
| `parallelism_coverage` (tanımlı metrik) | A | `coupling.py` ⚠️ (tolerans + örnekleme adımı yazılı) | L0 | yeniden route; **`impedance_pass` üretmez** |
| Referans düzlemi sürekliliği | A | `check_reference_plane_continuity()` | L0 | hattı taşı / düzlemi kesme |
| 90° köşe sayısı = 0 | A | `measure.py` açı taraması | L0 | köşeleri 45°/yay yap |
| Maske barajı ≥ karar bandı | A | `mask_dam.py` + `solder_mask_bridge` DRC | L0 | negatif mask margin veya iz genişliği |
| **Annular ring → teardrop adayı** | A | `dfm_emc_check.py::annular_ring` | L0 | teardrop (GUI) veya via büyüt |
| **Via-in-pad tespiti** | A | `dfm_emc_check.py::via_in_pad` | L0 | IPC-4761 Type VII fab notu + maliyet |
| **EPAD pasta kapsaması** | A(kısmi) | `dfm_emc_check.py::epad_paste_coverage` | L0 | negatif margin/windowpane; çok-açıklıklı desende elle |
| **Kenar/depanel seramik keepout** | A(kısmi) | `dfm_emc_check.py::edge_keepout_ceramics` | L0 | parçayı taşı; **yön** kontrolü elle |
| **Hızlı via'nın dönüş via'sı** | A | `dfm_emc_check.py::return_via_near_hs_via` | L0 | GND via ekle |
| Teardrop sayısı | A | `dfm_emc_check.py::teardrop_count` (`ZONE.IsTeardropArea`) | L0 + **stale döngüsü** | 0 ise AÇIK madde; GUI adımı |
| Via stub rezonansı vs kanal BW (`f_nyquist`) | A | `via_stub.py` — **kapsamı dar, §4'ü oku** | L0 | katman geçişini değiştir / backdrill |
| Stitching aralığı (`f_knee_edge` → λ/20) | A | `stitch_density.py --f-knee-ghz` | L0 | kenar/geçiş via'sı ekle |
| Crosstalk NEXT taraması | A(screen) | `crosstalk_thinning.py` — `signoff:false` | L0 | boşluk aç / H küçült / uzunluk kısalt |
| Otoruter kritik nete dokunmadı | A | kilitli netlerin geometrisi before/after karşılaştırması (`autoroute_receipt.locked_nets_untouched`) | L0 | board'u geri al; kilidi düzelt, yeniden koş |
| Otoruter arama bütçesi yazılı ve aşılmadı | A | `route/grid_astar.py --max-nodes` · `production/freerouting.py --timeout-s` | L0 | yerleşime feedback (L2); bütçeyi büyütmeden önce nedeni bul |
| Otoruter sonrası board gerçekten değişti | A | `autoroute_receipt.tracks_before/after` (importer dönüş değeri **değil**) | L0 | padstack uyuşmazlığı; `RED` |
| Hızlı nette otoruter kaynaklı via yok | A | `topological.py::high_speed_via_ban` | L0 | via ile "çözme" — yerleşim problemi |
| Otoruter sonrası DRC | B | `drc_gate.py --schematic-parity` (routing sonucu **verdict değildir**) | L0 | düzelt; DRC koşmadıysa `NO_COVERAGE` |

### Faz 4 — çıkış
| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| Gerber/drill/CPL üretimi | B | `kicad-cli pcb export gerbers/drill/pos` | L1 | yeniden üret |
| Rotasyon-map + oryantasyon | A | `check_orientation()` + versiyonlu map | L1 | map düzelt |
| DNP tutarlılığı (BOM ∧ CPL) | A | iki dosyanın karşılaştırması | L1 | düzelt |
| **EMC ön-uyum (yakın alan)** | **D** | prob + spektrum analizörü; baseline + tek değişken | L3 | tasarım dönüşü (pahalı!) |
| **Radyasyonlu/iletilen emisyon uyumu** | **D** | akredite lab | L3 | — |
| **ESD IEC 61000-4-2 dayanımı** | **D** | ESD tabancası; 4/8 kV kontakt yalnız `LOCAL_POLICY`, gerçek severity ürün standardı + `esd_test_profile` | L3 | Profil/lab yoksa `SCREEN_ONLY/CONFIRM`, D/L3 PASS yok |
| **Empedans doğrulaması** | **D** | fab coupon + TDR raporu | L3 | stackup revizyonu |
| Isıl doğrulama | D | termal kamera / termokupl, gerçek yükte | L3 | bakır/via/regülatör revizyonu |

ESD satırı için `evidence_origin=LOCAL_POLICY+PHYSICAL_MEASUREMENT`,
`assurance_loop=L1+L3`, `status=LOCAL_POLICY` uygula.

### Faz 5A — üretim dönüşü (as-built kabul)
| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| Released paket ↔ as-built kimlik bağı (hash) | A | `first_article.py::released_package_binding` | L3 | aynı revizyon farklı bayt → `RED`; farklı revizyon → `STALE` |
| **Gerçek montaj oryantasyonu (kutuplu/BGA/QFN)** | **D** | montajcı AOI/foto **yapısal** kaydı + released CPL açısı | L3 | `RED`, seri üretim bloke → [[../cpl-assembly/SKILL\|cpl-assembly]] |
| As-built presence / value / package | A | `first_article.py` (released CPL refdes evreni) | L3 | `RED`; refdes ne kayıtlı ne gerekçeli dışlanmış → `UNACCOUNTED_REFDES` |
| **Gizli lehim (BGA/QFN) boşluk oranı** | **D** | X-ray; **vendor verdict'i yeniden yargılanmaz**, eşik uydurulmaz | L3 | X-ray yoksa `NO_COVERAGE`/`CONFIRM`, asla GREEN |
| **MSL floor-life fiilî uyumu** | **D** | montajcı handling kaydı ↔ [[../bom-lifecycle/SKILL\|bom-lifecycle]] `msl_level` | L3 | aşım + bake yok → `RED` |
| **Depanel stresi** | **D** | depanel denemesi kaydı (`panelised: null` "panel yok" değildir) | L3 | `RED`/`NO_COVERAGE` → yerleşime L2 |
| Sapma dispozisyonu onaylı | A | `first_article.py::deviation_disposition` | L3 | onaylayanı olmayan dispozisyon dispozisyon değildir |

### Faz 5B — L3 fiziksel bring-up ve karakterizasyon
| Kural | Sev | Araç | Döngü | FAIL'de |
|---|---|---|---|---|
| Ölçüm ↔ kabul edilmiş birim bağı (board hash/serial) | A | `physical_measurement.py` | L3 | `STALE` |
| Kalibrasyon kimliği/tarihi var | A | `physical_measurement.py` | L3 | kalibrasyonsuz ölçüm ölçüm değildir → `NO_COVERAGE` |
| Belirsizlik + guard band karar sınırını kapatıyor | A | `physical_measurement.py` | L3 | `INCONCLUSIVE` |
| S-parametre metadata tam (port_map/Zref/de-embed) | A | `physical_measurement.py` + `sparam.py` | L3 | `INVALID_SPARAM` |
| Zorunlu D satırı için geçerli fiziksel kayıt var | A | `physical_measurement.py` (`eligible>0, scanned=0`) | L3 | `NO_COVERAGE` — release bloke |

> Bu iki tablo **yeni limit tanımlamaz.** Yukarıdaki D satırlarının eşikleri
> hâlâ domain skill'lerinin (`guc-isil`, `empedans-stackup`, `emc-esd`,
> `highspeed-routing`) ve bu matrisin malıdır; 5A/5B satırları yalnız
> **kanıtın yeterliliğini** ve **as-built kimliği** ölçer.

### D/L3 satırlarını kim kapatır — matris ölçmez

Bu matris **limit/authority eşlemesinin** sahibidir: hangi kuralın hangi
seviyede olduğunu, hangi araçla ölçüldüğünü ve FAIL'de ne olacağını burası
söyler. **Ölçümü yapmaz.** Yukarıdaki `D`/`L3` satırlarının fiziksel kanıtını
üretip yeterliliğine karar veren
[[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]]'dur;
as-built birimin kabulü ise
[[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]]'nda kapanır.

Devir makine-okunur bir artifact üzerinden yapılır:
[`references/l3_rule_universe.schema.json`](references/l3_rule_universe.schema.json).
Her satır `rule_id` + `owner` + `measurement_class` taşır; kapanabilmesi için
ayrıca `limit{units, min|max, authority{owner_skill, document_id, locator}}`
gerekir. **Limitsiz satır `NO_COVERAGE`'dır** — L3 aracının
(`tools/scripts/lab/physical_measurement.py::rule_limit`) hiçbir fallback dalı
yoktur, dolayısıyla eksik girdiden sayı türeyemez. `owner_skill` olarak
`l3-bringup-karakterizasyon` yazılırsa limit **reddedilir**
(`SELF_AUTHORED_LIMIT`).

Sınır tek cümle: **limit buranın, kanıt oranın.** L3 skill'i eşik yazarsa
kendi ölçtüğü değeri kendi yazdığı eşikle karşılaştırmış olur ve kapı
tautolojiye döner ([[../_shared/rule-conflicts.md]] §9). Ters yönde, bu
matrisin `assurance_loop: L3` taşıması onu ölçüm üreticisi yapmaz —
`evidence_origin` hâlâ `TOOL_REPORT`'tur; ayrım
[[../_shared/coverage-gate.md]] §"participates-in ≠ produces-evidence-for"da.

## 3. Ölçüm hatası sınıfları

Yanlış ölçüm, ölçmemekten kötüdür: sahte ihlal gerçek işi bloke eder, sahte
PASS gerçek kusuru geçirir. Bu repoda gerçekten yaşanmış **yedi sınıf**
(float sınır hatası, boş karşılaştırma, araç birimi yalanı, arc'ı atlayan
`GetClass()`, ipek metnini içeren `GetBoundingBox()`, yalan söyleyen
`ZONE.GetLayerName()`, yakınlık yarıçapı):
→ [[references/measurement-error-classes.md]] **(kanonik sahip)**

## 4. Döngüler

- **L0 — iç döngü (her board/şematik yazımından sonra, otomatik).**
  Tek komutla koşmalı ve **çıkış kodu** vermeli; "baktım iyi görünüyor" L0
  değildir. Girdi değişmeden çıktı değişiyorsa ölçüm bozuktur.
- **L1 — kapı döngüsü (faz sınırı).** [[../pcb-orchestrator/SKILL|orchestrator]]
  kalite kapıları. C seviyesi maddeler burada kapanır; kapanmamış C maddesi bir
  sonraki fazı **açmaz** (açılırsa geri dönüş maliyeti 10×).
- **L2 — geri besleme kenarları (ileri değil, geri akan bilgi).** Bunlar
  DAG'ın çevrimleri; baştan planlanmazsa Faz 3'te tıkanır:
  `annular/DFM → stackup` · `empedans W < fab min → stackup` ·
  `konnektör akım derating → güç bütçesi` · `DC-bias → BOM/parça` ·
  `NRND/EOL → şematik+footprint` · `panel/depanel → yerleşim` ·
  `keepout Z → yerleşim` · `crosstalk H → stackup`.
- **L3 — fiziksel döngü (kart geldikten sonra).** D seviyesi maddeler. Bu
  döngünün turu **haftalar** sürer: bu yüzden L3'te çıkacak her karar için
  tasarımda **ayak izi bırakılır** (filtre/choke/shield seçenekleri DNP,
  0 Ω köprüler, TP'ler). Ayak izi bırakılmamış bir L3 kararı = kart dönüşü.
- **Stale (bayatlama) döngüsü.** Bazı çıktılar üretildikleri anki board'a
  bağlıdır ve iz düzenlemesiyle sessizce geçersizleşir: **teardrop**,
  zone fill, uzunluk/skew raporu, gerber/CPL. Kural: board'a yazan her adımdan
  sonra bunlar **yeniden üretilir**, yeniden ölçülür — yoksa rapor doğru,
  dosya yanlış olur.

## 5. Bilinen sınırlar

### Yazılımla **kanıtlanamayacak** olanlar (dürüst liste)
DC-bias eğrisi, MSL/floor life, konnektör mating cycle ve kaplama aşınması,
ESD clamp davranışı, EMC emisyonu, gerçek empedans, gerçek θ_ja, cam dokuma
skew'i, lehim boşluk oranı, FEXT. Bunlar **C veya D** seviyesidir; board
dosyasında bilgi olarak bulunmazlar. Bunlar için tek doğru çıktı
`acik_maddeler.md`: her satır **ne / neden kanıtlanamadı / kim, hangi ölçümle
kapatacak**.

### Araç kapsam boşlukları

| Araç | Boşluk |
|---|---|
| `epad_paste_coverage` | Tek-pad + margin modelini ölçer; ayrı çizilmiş çok açıklıklı (windowpane) pasta desenini **tanımaz** → elle doğrula |
| `edge_keepout_ceramics` | Mesafeyi ölçer, **yönü ölçmez** (uzun eksen kırma hattına paralel mi) ve V-score hattını bilmez — panel verisi board'da yok |
| `annular_ring` | NPTH'leri hariç tutar (ilk koşuda montaj delikleri yanlış pozitif vermişti; düzeltildi) |
| `return_via_near_hs_via` | Net **adı** desenine dayanır; isimlendirme değişirse kapsam sessizce düşer → `scanned` sayısını her koşuda oku |
| `via_stub.py` | **Bu repodaki hiçbir kartta henüz ölçüm yapamıyor** → [[../highspeed-routing/SKILL|highspeed-routing]] §7 AÇIK MADDE |
| `stitch_density.py` | `NO_COVERAGE` **çıkış koduna yansımaz**; çağıran `summary.NO_COVERAGE` alanına bakmalı |
| `zdiff_solver.py` | Kapalı-form; `signoff:false`. Yayımlanmış karşılaştırmada −%29.94…+%2.41 sapma |
| `crosstalk_thinning.py` | FEXT modellenmez; `signoff:false`; iki-iz modeli (N-agresör toplamı yok) |

### Dış araçlar — hash bağı (C8) — KAPANDI 2026-08-08, metin 2026-08-10'da düzeltildi

**Bu bölüm 2026-08-08 ile 2026-08-10 arasında yanlıştı ve yanlışlığı ucuza
gelmedi.** C8 kararı uygulandı — `drc.sh` / `measure.py` / `coupling.py`
`tools/scripts/drc_gate.py`, `tools/scripts/board/measure_board.py` ve
`tools/scripts/board/pair_coupling.py` olarak vendorlandı, registry'ye girdi ve
hash bağı kuruldu — fakat **bu satırlar güncellenmedi**, yani karar uygulandı
ama işlenmedi. Okuyan herkes Faz 3→4 kapısının kalıcı olarak kapalı olduğunu
sanmaya devam etti. Bir kararın *uygulanması* onu kapatmaz; *işlenmesi*
kapatır. Aynı kusur `docs/tool-inventory.md` §4'te de kayıtlıdır.

**Bugünkü durum:**

| Araç | Hash bağı | Not |
|---|---|---|
| `drc_gate.py` | ✅ `design.sha256` | ERC ve DRC modunda aynı zarf |
| `measure_board.py` | ✅ `design.sha256` | digest 2026-08-09'da eklendi; öncesinde yoktu |
| `pair_coupling.py` | ✅ `design.sha256` | **2026-08-10'da eklendi.** Kendi şeması `design` ve `metrics`'i zorunlu sayıyordu, üretici ikisini de yazmıyordu — yani raporları bağlanamazdı ve hiçbir tüketici şemaya karşı doğrulamadığı için fark edilmemişti |
| `crosstalk_thinning.py` | ❌ **yapısal olarak bağlanamaz** | girdileri CLI geometri sayıları, hash'lenecek tasarım dosyası yok. Bu bir eksiklik değil, aracın doğası; assertion setinde `unbound_reason` ile açıkça beyan edilir ve üzerine oturan her koşu `SCREEN_ONLY` ile sınırlanır |

Ayrıntı: [[../../docs/CONSOLIDATION-NOTES.md]] §C8.

### `dfm_emc_check.py` doğrulama durumu — ÇÖZÜLDÜ (C3)
Yeni ağaçta tek kanonik kopya + validator (`tools/tests/test_board_checks.py`)
vardır; araç `NO_COVERAGE` değildir. Kalan kısıtlar yukarıdaki kapsam boşlukları
tablosundadır. Ayrıntı: [[../dfm/SKILL|dfm]] §5.

## 6. Doğrulama

```bash
# gate receipt doğrulaması — yalnız exit 0 + effective GREEN kapıyı açar
python3 tools/scripts/gate_receipt.py gate_receipt.json

# board ölçümleri  (--json her zaman YAZAR)
python3 tools/scripts/board/dfm_emc_check.py board.kicad_pcb --json checks.json
python3 tools/scripts/board/mask_dam.py board.kicad_pcb --json mask_dam.json
python3 tools/scripts/board/stitch_density.py board.kicad_pcb --f-knee-ghz 0.7 --json stitch.json
python3 tools/scripts/board/via_stub.py board.kicad_pcb --json via_stub.json

# tüm doğrulama paketi (1244 test)
python3 -m unittest discover -s tools/tests -t .
```

## 7. Kabul kriterleri

- [ ] Her kuralın seviyesi (A/B/C/D) yazılı; seviyesiz kural yok.
- [ ] L0 tek komutla koşuyor ve **çıkış kodu** veriyor.
- [ ] Her A-seviyesi kontrol için **fault-injection kanıtı** var (kusur enjekte
      edildi → kontrol FAIL verdi); kanıtı olmayan kontrol "koşuyor" sayılmaz.
- [ ] Positive fixture yanında **negative** fault-injection da koşuldu.
- [ ] Her kontrol `scanned` (kapsam) raporluyor; `scanned=0` PASS değil.
      `scanned <= eligible` ve `scanned + excluded == eligible`; bozuk kapsam
      aritmetiği `INVALID_COVERAGE/RED`.
- [ ] Aracın **bilinen sınırları** yazılı (§5).
- [ ] C maddeleri kaynak + tarih ile kapanmış; kapanmamışsa faz açılmamış.
- [ ] D maddeleri için tasarımda ayak izi bırakılmış (DNP filtre, 0 Ω, TP).
- [ ] Stale çıktılar (teardrop, zone fill, uzunluk raporu, gerber) son board'dan
      yeniden üretilmiş.
- [ ] Gate receipt şema sürümü doğrulandı; input/tool hash, argv, exit code,
      coverage ve verdict eksikse kapı açılmadı. Genel artifact'lere gereksiz
      şema dayatılmadı; bu zorunluluk yalnız kalite makbuzlarına uygulandı.
- [ ] A-seviyesi fault proof fixture/baseline/injected hash'leri, exact komut ve
      parsed metric ID'lerine bağlı expected/observed failure listeleri taşıyor.
- [ ] DRC/unconnected/parity yanında uygulanabilir geometry alt-kapıları
      (impedance, layer distribution, reference coverage, `parallelism_coverage`,
      mask dam) ve açık blocker'lar birleşik receipt içinde kapandı.
- [ ] Hash bağı olmayan dış araçların (§5) sonucu `SCREEN_ONLY` sayıldı.
- [ ] `acik_maddeler.md` her açık madde için **ne / neden / kim, hangi ölçümle**
      taşıyor.

## İlgili

[[../pcb-orchestrator/SKILL|orchestrator]] · [[../claim-audit/SKILL|claim-audit]] ·
[[../dfm/SKILL|dfm]] · [[../emc-esd/SKILL|emc-esd]] ·
[[../konnektor/SKILL|konnektor]] · [[../guc-isil/SKILL|guc-isil]] ·
[[../empedans-stackup/SKILL|empedans-stackup]] ·
[[../highspeed-routing/SKILL|highspeed-routing]] ·
[[../crosstalk-thinning/SKILL|crosstalk-thinning]] ·
[[../sematik-erc/SKILL|sematik-erc]] · [[../../docs/tool-inventory.md]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`assert-matrix`** — Bu matrisi prose değil **kod** olarak koşar; `--prove` ile her iddiaya kendi kusurunu enjekte eder. Seçiciler, araçların gerçekten bastığı yolları adlandırmalı (bkz. Tur 16).
- **`benchmark`** — Sabit problem setini N kez koşup ölçülen geçme oranı üretir; test paketi “davranıyor” der, bu “çözüyor mu” diye sorar.
