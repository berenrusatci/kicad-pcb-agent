# Faz DAG'ı — adımlar, artifact akışı, geri besleme kenarları

Bu dosya [[../SKILL|pcb-orchestrator]]'ün adım tablolarını taşır. Orkestratör
ana bağlamda **yalnız DAG + artifact özetlerini** tutar; kuralların tamamını
yüklemez.

---

## Faz 0S — sistem contract kapısı (yalnız çok-kartlı projelerde)

Board-local Faz 0'ın **üstünde** duran proje-seviyesi kapıdır ve bu depoda
generic `pcb-agent` skill'i değil, proje katmanındaki
`system-planning/skills/sistem-arayuz-sozlesmesi` sahibidir.

- Her board'un `interface_contract.json` baseline'ı, kanonik system contract'ın
  kendine ait **projeksiyonunu** tüketir; ikisi aynı artifact değildir
  (`system-planning/README.md:21`).
- Connector/net/power/VC-ID değişince **L2** ile etkilenen tüm board
  projeksiyonları bayatlar.
- `writes_board: false`; hiçbir board lock'u almaz.
- Faz 4 sonrası device-tree/software handoff yeniden doğrulanır; gerçek hardware
  enumeration sonucu Faz 5B'ye devredilir.

Tek-kartlı bir projede bu düğüm `excluded` + gerekçe ile kapatılır; **atlanmış
sayılmaz**.

---

## Faz 0 — karar ve context kapısı

DAG düğümü açmadan önce [[../../decompose-pcb-decisions/SKILL|decompose-pcb-decisions]]
ile kullanıcı niyetini `design_intent.json`, mimari soruları
`decision_units.json` olarak ayır.

**0a — dondurulmuş giriş baseline'ı**
Kullanıcı hedefi ve doğrulanmış kaynaklardan `interface_contract.json`,
`parts.json`, `bom.json`, `fab_capabilities.json` ve `tool_registry.json` üret.
Bu düğüm PCB kararı vermez; downstream'in tükettiği ilk hash'li girdilerin tek
üreticisidir. Rol/yön/sahne hareketi gibi türetilemeyen niyet açık değilse
blocking soru bırak. **`parts.json`/`bom.json` yokken güç, lifecycle veya
şematik worker'ı açma.**

**0b — worker preflight**
Her worker için hash-bağlı `context_manifest.json` üret ve `context_pack.py` ile
doğrula. Yalnız `READY` worker başlatır. Blocking soru, stale hash, kayıt dışı
tool veya eksik claim/evidence sözleşmesi varken alt ajan açma. Worker tüm
vault'u değil yalnız manifestteki doğrudan girdileri yükler.

**0c — worker postflight**
Worker çıktısından sonra aynı manifesti `context_pack.py --postflight` ile
çalıştır. Yalnız exit 0 + `ACCEPTED`, claim kanıtlarını ve exact argv gate
sonuçlarını downstream'e açar. `READY`, tasarım kararının kabul edildiği
anlamına gelmez.

Mimari birleşim yeni elektriksel gerçek icat etmez; yalnız GREEN child
receipt'lerden seçenek/gerekçe/blocker sentezler. Bir girdi değişince bağlamı
ekleme yaparak büyütme, küçük paketi yeniden üret ve eskisini stale say.

---

## Faz 1 — Temeller & Tedarik (kök düğümler, PARALEL)

Birbirinden bağımsız; eşzamanlı koşar. Downstream'in tükettiği limitleri üretirler.

| Adım | Skill | Yayınladığı artifact |
|---|---|---|
| Güç bütçesi & ısıl | [[../../guc-isil/SKILL\|guc-isil]] | `rail_tree.json` (`i_max_a`, `ipc2221_screen_width_mm`, `final_width_mm`, `standard`, `min_via_count`, `max_ir_drop_mv`, `c_eff_uf`), `thermal_report.json` |
| Mekanik / DXF | [[../../mekanik-dxf/SKILL\|mekanik-dxf]] | `board_outline.json`, `keepout_zones.json` (+`max_allowed_height_mm`), `mounting_holes.json` |
| Erken BOM & lifecycle | [[../../bom-lifecycle/SKILL\|bom-lifecycle]] | `lifecycle_report.json`, `alternates.json` |

Bu üç düğüm Faz 0 baseline'ını salt-okur. **Ardından** konnektör
([[../../konnektor/SKILL|konnektor]]) `rail_tree.json` + mekanik datum +
`interface_contract.json` ile `connector_spec.json` ve mating/actuator keepout
eklerini üretir.

| Adım | Skill | Yayınladığı artifact |
|---|---|---|
| **Arayüz eşleşmesi** (zorunlu child) | [[../../arayuz-eslesme-kapisi/SKILL\|arayuz-eslesme-kapisi]] | `interconnect_mating_report.json`, `interconnect_gate_receipt.json` (+ `l3_rule_universe.json` ve `keepout_zones.json`'a `role: contributor`) |

**Eşleşme kapısı `konnektor`'den SONRA, `ecad-varlik-kalifikasyonu`'ndan ÖNCE
koşar ve Faz 2'yi BLOKLAR.** Sıra keyfî değil:

- Girdisi (`connector_spec.json`) Faz 1'de doğar; daha erken koşamaz.
- `konnektor/SKILL.md:33-35`: *"Şematikten **önce**: koruma zinciri ve pinout
  kararı buna bağlı."* Faz 2'de bulunursa şematik **ve** netlist yeniden yazılır.
- `ecad-varlik-kalifikasyonu`'ndan önce, çünkü ECAD kapısı **yanlış MPN'i
  mükemmel şekilde kalifiye eder**; eşleşme kapısı MPN seçiminin kendisini
  sorgular.
- Faz 3'te yakalanırsa **10× geri dönüş maliyeti**
  ([[../../dogrulama-matrisi/SKILL|dogrulama-matrisi]]:246-248).
- Faz 5A'da **hiç görünmez**: FAI evreni *"released CPL refdes evreni"*dir ve
  kart **dışındaki** kablo orada yoktur.

Bu düğüm **zorunlu child**'dır ve bu bir üslup tercihi değil: `NO_COVERAGE →
GREEN` yolunu kapatan üst kural (bu dosyanın sahibi olan SKILL.md:217-219,
*"`coverage.scanned=0` zorunlu kapıda `NO_COVERAGE`; advisory olarak açıkça
işaretlenmedikçe birleşik kapıyı RED yapar"*) yalnız **DAG'a zorunlu child
olarak bağlanmış** bir kapı için tetiklenir. `coverage-gate.md:18` bir *sözlük*
kuralıdır, tetikleme kuralı değil — tetikleyen bağ **burada** olmak zorundadır.
Zincir hiç beyan edilmezse kapı `NO_COVERAGE` verir ve birleşik kapı `RED` olur;
"beyan etmemek" bir kaçış yolu değildir.

`writes_board: false` — board kilidi almaz, `sematik-erc` ile serileştirilmesi
gerekmez; **bloklaması artifact bağımlılığıdır, kilit değil.**

`rail_tree.json` producer ve consumer'ı
[`tools/schemas/rail_tree.schema.json`](../../../tools/schemas/rail_tree.schema.json)
ile aynı alan setini kullanır; parity fixture fark bulursa Faz 2 açılmaz.
`evidence_origin=DESIGN_ARTIFACT`, `assurance_loop=L0`, `status=CONFIRM`
(schema/fixture uyuşmazlığında).

**Lifecycle feedback'i:** BOM düğümü doğrudan tek bir adıma yama yapmaz. Kabul
edilen alternatif `parts.json`/`bom.json` baseline'ını **yeni revizyonla
değiştirir**; pinout/rail/footprint/package delta'sına göre güç, konnektör,
şematik, stackup, placement/DFM ve parity child receipt'leri stale olur.
Orkestratör etkilenen descendant'ları yeniden açar.

---

## Faz 2 — Elektriksel & Fiziksel Altyapı

| Adım | Skill | Çıktı |
|---|---|---|
| Stackup & empedans | [[../../empedans-stackup/SKILL\|empedans-stackup]] | `stackup.json`, `impedance_rules.json` |
| Koruma topolojisi | [[../../emc-esd/SKILL\|emc-esd]] | `esd_chain.json`, `return_via_rules.json`, interface başına filtre/CMC/DNP gereksinimi |
| Şematik & ERC | [[../../sematik-erc/SKILL\|sematik-erc]] | `*.kicad_sch` (**wire'lı**), netlist, `erc.json`, `sematik_riskler.md` |
| DFT şematik planı | [[../../dft-testpoints/SKILL\|dft-testpoints]] | TP sembolleri + `testpoint_plan.json` |

- Stackup adımı rail/fab/PHY girdileriyle koşar.
- **Koruma topolojisi şematikten önce donar; şematik o tamamlanmadan açılmaz.**
- Şematik → DFT **seridir** çünkü ikisi de aynı `.kicad_sch`'e yazar.
- DFT planı **placement tüketmez**; yalnız netlist + rail + debug gereksinimiyle
  TP'leri şematiğe doğurur.

---

## Faz 3 — Seri Kilit & Fiziksel Yönlendirme (BARİYER)

`board_outline.json` + connector keepout + `stackup.json` + netlist +
`esd_chain.json` burada birleşir. **Bu noktadan sonra tasarım dosyasının tek
sahibi vardır; işlemler seridir.**

1. **Önleyici DFM + yerleşim** — [[../../dfm/SKILL|dfm]]: fab yeteneklerini
   DRC'ye enjekte et, sonra 3D keepout/yükseklik haritasını ihlal etmeden
   yerleştir. → `placed board`, `placement.json`
2. **Yüksek hız routing** — [[../../highspeed-routing/SKILL|highspeed-routing]]:
   escape geometrisi (pin-arası kanal, maske barajı, 45° köşe), diff çift
   routing, ps cinsinden gecikme eşitleme, referans düzlemi sürekliliği.
   → `routed board`, `length_report.json`, `coupling_report.json`,
   `reference_report.json`, `escape_report.json`
   - Her paralel segment için, geometri yazılmadan **önce**
     [[../../crosstalk-thinning/SKILL|crosstalk-thinning]] → `crosstalk_scan.json`
3. **Kalan netlerin otonom yönlendirmesi (opsiyonel)** —
   [[../../otonom-routing/SKILL|otonom-routing]]: kritik netler **kilitlendikten
   sonra** kalan düşük hızlı netler bütçeli bir router'la (topological →
   grid A* → FreeRouting) scratch kopyada yönlendirilir; import sonrası DRC ve
   kilitli net karşılaştırması kapıyı kapatır.
   → `routed board`, `autoroute_receipt.json`
   - Router çıktısı **verdict değildir**: `engine=none`, `tracks_after ==
     tracks_before` veya DRC koşmamışsa sonuç `NO_COVERAGE`/`RED`'dir.
   - `high_speed` netinde `NOT_FOUND` çıkarsa bu bir routing değil **yerleşim**
     problemidir → [[../../dfm/SKILL|dfm]]'e L2 feedback.
4. **Fiziksel EMC/ESD uygulaması** — [[../../emc-esd/SKILL|emc-esd]]: konnektör
   tarafı koruma sırası, return path, stitching ve DNP filtre alanını routed
   board üzerinde ölç. → `emc_plan.md` + `emc_geometry_receipt.json`

---

## Faz 4 — Doğrulama, Paketleme, Çıkış Kapısı

| Sıra | Adım | Çıktı |
|---|---|---|
| **4a** (paralel, salt-okuma) | son DFM + DFT erişim + EMC geometry | `final_dfm_receipt.json`, `dft_coverage_receipt.json`, `emc_geometry_receipt.json` |
| **4b** (bariyer) | birleşik gate | üç zorunlu child receipt GREEN **ve** güncel board hash'i |
| **4c** (en son) | CPL/assembly [[../../cpl-assembly/SKILL\|cpl-assembly]] | `gerbers.zip`, `cpl.csv`, `bom.csv`, fab receipt |

**CPL, 4b'den önce koşmaz.** Board değişirse final DFM/DFT/EMC receipt'leri ve
Gerber/CPL/zone fill/length çıktıları stale olur; hepsi son board hash'inden
yeniden üretilir. DFT erişim FAIL'i placement'a, EMC geometry FAIL'i
stackup/placement/routing'e feedback açar; kanonik board'u **sessizce yama
yapmaz**.

---

## Faz 5 — fiziksel kapanış (Faz 4 çıktısı bir *dosya paketidir*, kart değil)

Faz 0→4 bir **design-to-manufacturing-release** DAG'ıdır: 4c'nin çıktısı
`gerbers.zip` + `cpl.csv`'dir, monte edilmiş bir kart değil. Ortak sözlük ise
L3'ü açıkça "kart geldikten sonra" diye tanımlar
([[../../_shared/coverage-gate.md]]). Bu iki model aynı yaşam döngüsünü
anlatmıyordu; Faz 5 o boşluğu **iki ayrı düğüm** olarak kapatır. Tek bir
"physical validation" düğümünde birleştirilmezler: as-built release ≠ elektriksel
karakterizasyon, girdileri, kapıları ve karar otoriteleri farklıdır.

### Faz 5A — first-article / as-built üretim kabulü

| Adım | Skill | Yayınladığı artifact |
|---|---|---|
| Üretim dönüşü kabulü | [[../../ilk-urun-montaj-kabulu/SKILL\|ilk-urun-montaj-kabulu]] | `first_article_report.json`, `assembly_acceptance_receipt.json`, `manufacturing_deviation_log.json` |

- Girdi: released Gerber/BOM/CPL paketi + `rotation_map.json` + lifecycle/MSL
  metadata + lot/work-order kimliği + montajcı AOI/foto/X-ray/depanel kaydı.
- Kapı: "dosya üretildi mi?" değil, **"üretilen ilk kart released package'a
  fiilen uyuyor mu?"**
- Otorite: **seri montajı açma/bloke etme**. `cpl-assembly` paketi üretir,
  `dfm` üretilebilirliği değerlendirir; ikisi de as-built release otoritesi
  değildir.
- `writes_board: false`; board lock yok. RED → issue sınıfına göre
  `cpl-assembly` / `dfm` / `bom-lifecycle` / asset owner'a L2, seri build bloke.

### Faz 5B — L3 fiziksel bring-up ve karakterizasyon

| Adım | Skill | Yayınladığı artifact |
|---|---|---|
| Fiziksel ölçüm kampanyası | [[../../l3-bringup-karakterizasyon/SKILL\|l3-bringup-karakterizasyon]] | `physical_measurement_manifest.json`, `l3_gate_receipt.json`, `l3_feedback_actions.json` |

- **5A'dan sonra gelir:** elektriksel karakterizasyon için önce "doğru assemble
  edilmiş birim" şarttır; yanlış monte edilmiş kartı "elektriksel arıza" diye
  debug etmek, bu sıralamanın olmadığı yerde standart sonuçtur.
- Kampanya güvenlik nedeniyle **kendi içinde seridir**: power-safe bring-up →
  functional observability → thermal/SI/EMC ölçümleri. Domain ölçümleri ekipman
  uygunsa paralellenebilir.
- `writes_board: false`; board yalnız referans/hash kaynağıdır. RED'de board'a
  yama yapılmaz, doğru producer'a L2 kenarı açılır.
- Bu depoda **ilk ve tek** `evidence_origin=PHYSICAL_MEASUREMENT` üreticisidir;
  limit sahibi olmaya çalışmaz ([[../../_shared/rule-conflicts.md]] §9).

> **Açık soru / varsayım.** "Release" kelimesinin kanonik anlamı (Gerber
> release / kabul edilmiş first-article / ürün release) henüz
> kararlaştırılmadı. Bu dosya muhafazakâr varsayımı uygular: **Faz 4c
> "Gerber release", Faz 5A "accepted first-article release"dır ve 5A GREEN
> olmadan seri üretim açılmaz.** Parent-child receipt ilişkisi bu varsayıma
> göre kurulur; karar değişirse 5A/5B receipt'lerinin parent'ı değişir.

---

## Geri besleme kenarları (L2) — DAG'ın çevrimleri

Baştan planlanmazsa Faz 3'te tıkanır:

| Kenar | Tetikleyici |
|---|---|
| `annular/DFM → stackup` | Dar annular ring / aspect ratio |
| `empedans W < fab min → stackup` | Solver çıktısı fab minimumunun altında |
| `crosstalk H → stackup` | NEXT bütçesi ince prepreg gerektiriyor |
| `konnektör akım derating → güç bütçesi` | Derating sonrası akım rayın altında |
| `DC-bias → BOM/parça` | Efektif kapasite stabilite penceresinin altında |
| `NRND/EOL → şematik + footprint` | Alternatif footprint değiştiriyor |
| `panel/depanel → yerleşim` | V-score hattı hassas seramiğe yakın |
| `keepout Z → yerleşim` | Parça body-height bölge tavanını aşıyor |
| `DFT erişim FAIL → yerleşim` | Prob açısı/courtyard engeli |
| `EMC geometry FAIL → stackup/yerleşim/routing` | Referans yarığı, dikiş aralığı |
| `otoruter NOT_FOUND / bütçe aşımı → yerleşim` | Via yasaklı hızlı net çözülemedi ya da `max_nodes` patladı |

Feedback kenarlarını **say**, döngü sınırı koy (ör. 3); aşılırsa `NEEDS_HUMAN`.

---

## Stale (bayatlama) yayılımı

Bazı çıktılar üretildikleri anki board'a bağlıdır ve iz düzenlemesiyle sessizce
geçersizleşir:

- **teardrop** · zone fill · uzunluk/skew raporu · `parallelism_coverage` ·
  gerber/CPL · `escape_report.json` · `emc_geometry_receipt.json`

**Kural:** board'a yazan her adımdan sonra bunlar yeniden üretilir ve yeniden
ölçülür — yoksa rapor doğru, dosya yanlış olur.
