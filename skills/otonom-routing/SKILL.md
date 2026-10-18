---
name: otonom-routing
description: Sınırlı (bounded) otomatik yönlendirme — kritik netler kilitlendikten sonra kalan düşük hızlı netleri topolojik merdiven / grid A* / FreeRouting ile route eder; router çıktısını asla verdict saymaz, her koşuyu scratch kopyada, bütçeli ve DRC ile kapatılmış bir makbuza bağlar.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 3 — Seri (highspeed-routing sonrası; board tek sahipli)"
assurance_loop: [L0, L2]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: placed board, schema: null, required: true}
  - {artifact: routed board (kritik netler), schema: null, required: true}
  - {artifact: impedance_rules.json, schema: ../empedans-stackup/references/stackup.schema.json, required: true}
  - {artifact: fab_capabilities.json, schema: ../../tools/schemas/fab_capabilities.schema.json, required: false}
  - {artifact: route_request.json, schema: null, required: true}
outputs:
  - {artifact: routed board (kalan netler), schema: null}
  - {artifact: autoroute_receipt.json, schema: references/autoroute_receipt.schema.json}
  - {artifact: route_result.json, schema: ../../tools/schemas/route_result.schema.json}
  - {artifact: topological_route.json, schema: ../../tools/schemas/topological.schema.json}
  - {artifact: freerouting_report.json, schema: ../../tools/schemas/production_gate.schema.json}
writes_board: true
tools: [route-diff-pair, route-topological, route-grid-astar, production-freerouting, drc-gate, kicad-cli, place-force-directed, route-safe-write, routing-table, recovery, verify-placement, verify-zones, route-audit]
---

# SKILL: Otonom Routing (sınırlı otomatik yönlendirme)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Metrik adları: [[../_shared/glossary.md]] ·
Board yazımı kilidi: [[../pcb-orchestrator/references/board-write-lock.md]]

> **Bu skill'in tek varlık nedeni:** ağaçta üç çalışan router var
> (`route/topological.py`, `route/grid_astar.py`, `production/freerouting.py`) ve
> **yönetilmeyen bir router, çıktısı hiçbir kapı tarafından denetlenmeyen bir
> board mutasyonudur.** Skill, o mutasyonu bütçeye, kilide ve makbuza bağlar.
> Kararın gerekçesi: [[../../docs/CONSOLIDATION-NOTES.md]] §Otoruter kararı.

**Bu skill'in kanonik sahibi olduğu kural:** *router çıktısı verdict değildir* —
routing'in "bitti" demesi ile board'un doğru olması ayrı iki olaydır ve ikincisi
yalnızca import sonrası koşan DRC + kilitli net karşılaştırmasıyla kanıtlanır.

## 0. Ne zaman çağrılır

- Faz 3'te, **yalnızca** şu ikisi bittikten sonra:
  1. [[../dfm/SKILL|dfm]] yerleşimi kapandı (`placed board`),
  2. [[../highspeed-routing/SKILL|highspeed-routing]] kritik netleri route etti
     **ve kilitledi**.
- Kalan iş "düşük hızlı gövde" ise çağrılır: güç olmayan sinyal netleri, I²C/SPI/
  GPIO/strap, düşük hızlı kontrol hatları.
- **Çağrılmadığı yerler:** diferansiyel çiftler, length-match'li gruplar, MIPI/
  USB/DDR sınıfı hatlar, güç dağıtımı (bunlar polygon/plane işidir, iz değil),
  ve kritik netlerin re-route'u. Üç motorun **hiçbirinde** diff-pair veya
  net-class semantiği yoktur — bu netler otorutere *ayar* ile değil **kilit** ile
  korunur.
- Board tek sahiplidir; bu adım [[../dfm/SKILL|dfm]] ve
  [[../highspeed-routing/SKILL|highspeed-routing]] ile **serileşir**, paralel
  koşmaz.

## 1. Girdi/çıktı sözleşmesi

### Adım 0 — kilit kanıtı (routing'den ÖNCE)

`autoroute_receipt.json` (`references/autoroute_receipt.schema.json`) şu üçünü
routing başlamadan doldurur:

| Alan | Nereden | Yoksa |
|---|---|---|
| `board_before_sha256` | scratch kopyanın hash'i | adım açılmaz |
| `locked_nets[]` | `highspeed-routing` çıktısı + netclass taraması | adım açılmaz |
| `candidate_nets[]` | netlist − `locked_nets` − güç netleri | `NO_COVERAGE` |
| `placement_guard` | **`verify-placement`** çıktısı (RED ise adım açılmaz) | adım açılmaz |

- **`verify-placement`** — **Routing'e girmeden önce koş.** İki şeyi kanıtlar:
  (1) `locked: true` beyan edilen her bileşen kartta gerçekten o konumdadır,
  (2) hiçbir bileşen bir keepout/koridor zone'unu ihlal etmez (katman-farkında).
  Neden burada: yönlendirici, yerleşimi **veri** kabul eder. Yerleşim beyan
  edilenden kaymışsa router doğru kurallarla yanlış bir karta çalışır ve bunu
  hiçbir DRC söylemez — DRC bakırı denetler, *niyeti* değil. Ölçülmüş olay
  (2026-08-10): `locked` bayrağı üretiliyor, beyan ediliyor, hatta karta
  yazılıyordu ama **hiçbir şey doğrulamıyordu**; kilit bir temenniydi.
  `pcbnew` gerektirmez — board'u metin okur, her yorumlayıcıda koşar.

- **`verify-zones`** — **`verify-placement`'ten ÖNCE koş.** O, bileşenlerin
  zone'ları ihlal edip etmediğini ölçer; bu, **zone beyanının kendisinin**
  ayakta olup olmadığını ölçer: `kind` kapalı listede mi, geometri tek biçimde
  mi verilmiş (`rect_mm` **ya da** `circle_mm`, ikisi birden değil), her zone
  bir `src` taşıyor mu, `escape_layer` gerçekten `exclude_layers` içinde mi.
  Neden ayrı: bozuk bir zone beyanı `verify-placement`'i **kırmaz**, sessizce
  etkisizleştirir — `exclude_layers: ["her iki yuz"]` yazarsanız karşılaştırma
  hiçbir footprint katmanıyla tutmaz ve keepout ölü kalır, ama kapı GREEN
  verir. Ölçülmüş olay (2026-08-10): sözleşme yazılınca cam-w'nin 7 zone'unun
  **5'inin `src` alanı olmadığı** ortaya çıktı; kaynaksız beyanlar aylardır
  oradaydı ve hiçbir şey sormuyordu.

`candidate_nets` boşsa **router çalıştırma**: yapılacak iş yoktur, `NO_COVERAGE`
yaz ve çık. "Router koştu, hiçbir şey değişmedi" ile "route edilecek net yoktu"
aynı satırda görünmemelidir.

### Girdi dosyaları (motorların beklediği biçim)

`grid_astar.py` (problem dosyası) — `params` anahtarları bayrakla ezilebilir
(öncelik: **bayrak > dosya > varsayılan**):

```json
{"cell_mm": 0.1, "clearance_mm": 0.2, "max_nodes": 200000,
 "nets": [{"net": "SDA", "start": [0,0], "end": [10,0]}],
 "obstacles": [{"type": "box", "x_min": 4, "y_min": -1, "x_max": 6, "y_max": 1},
               {"type": "segment", "x1": 0, "y1": 5, "x2": 9, "y2": 5,
                "width_mm": 0.25}]}
```

`topological.py` (request dosyası):

```json
{"requests": [{"net": "SDA", "start": [0,0], "end": [10,0], "width_mm": 0.2,
               "clearance_mm": 0.2, "layer": "F.Cu", "high_speed": false}],
 "obstacles": [{"name": "U1", "x_min": 4, "y_min": -1, "x_max": 6, "y_max": 1,
                "clearance_mm": 0.2}],
 "lower_layer": "B.Cu",
 "lower_layer_obstacles": [ ... ]}
```

> **`lower_layer_obstacles` alanını "bilmiyorum" diye boş bırakma.** Alan yoksa
> araç alt katmanı **boş varsaymaz**; katman değişimi önermez ve `NEEDS_HUMAN`
> yazar. Boş liste (`[]`) yazmak ise "alt katman temiz" **beyanıdır** ve
> yanlışsa via'yı bir izin üstüne koyar. İkisi aynı şey değildir.

### Çıktılar

| Artifact | Üreten | Şema |
|---|---|---|
| `topological_route.json` | `route/topological.py` | `topological-route-report/v1` |
| `route_result.json` | `route/grid_astar.py` | `route-result/v1` |
| `freerouting_report.json` | `production/freerouting.py` | `production-gate-report/v1` |
| `autoroute_receipt.json` | bu skill | `autoroute-receipt/v1` |

## 2. Merdiven — hangi motor, hangi sırayla

Sıra ucuzdan pahalıya ve **şeffaftan opağa** doğrudur. Bir üst basamak
yetiyorsa alt basamağa inme.

### (a) `topological` — plan, geometri değil

```bash
python3 tools/scripts/route/topological.py route_request.json \
        --json topological_route.json --chamfer-mm 0.25
```

`DIRECT → L_BEND → U_DETOUR → LAYER_CHANGE → NOT_FOUND` merdivenini yürür,
45° pah uygular ve üç kapı koşar: `route_found`, `high_speed_via_ban`,
`45_degree_geometry`. Çıktısı **plan**dır: `main()` board'a yazmaz
(`write_tracks()` var ama CLI çağırmaz). Plan burada okunur, tartışılır ve
gerekirse yerleşime geri sinyal verilir — geometri yazılmadan önce.

- `high_speed: true` olan istekte **katman değişimi basamağı hiç denenmez**;
  çözüm yoksa `NOT_FOUND` döner. Bu bir routing başarısızlığı değil, bir
  **yerleşim** problemidir → [[../dfm/SKILL|dfm]]'e feedback (L2).
- `--no-chamfer` yalnız fault-injection içindir: 45° kapısının gerçekten
  ısırdığını kanıtlar (§5).
- Boş request seti `route_found = NO_COVERAGE` üretir ve **exit 1** verir.

### (b) `grid_astar` — son çare tekil net çözücü

```bash
python3 tools/scripts/route/grid_astar.py route_problem.json \
        --json route_result.json --cell-mm 0.1 --max-nodes 200000
```

Tıkanan **tek** bir net için 8-yönlü ızgara A*. Board'a yazmaz; polyline üretir.

- **`cell_mm` seçimi kapının kendisidir:** ızgara sürekli geometriyi yuvarlar,
  `cell_mm`'den dar bir koridor **kaçırılır**. Kural:
  `cell_mm < (iz_genişliği + clearance) / 2`. 0.2–0.25 mm iz için varsayılan
  0.1 mm güvenlidir; daha yoğun bölgede küçült — arama süresi karesel büyür,
  `max_nodes` bu yüzden **zorunlu** üst sınırdır.
- Tek bütçe `max_nodes`'tur. **Zaman ve bbox bütçesi kodda yoktur**; registry
  ön koşulları bu doğrultuda düzeltildi. Bütçe patlarsa
  `found=false, reason="node budget (N) exceeded"` — sessiz kısmi yol yok.
- Hiç net olmayan problem dosyası `NO_COVERAGE` + **exit 1**.

### (c) `freerouting` — DSN/SES köprüsü (opak, en son)

```bash
python3 tools/scripts/production/freerouting.py scratch/board.kicad_pcb \
        --jar /opt/freerouting/freerouting.jar --workdir scratch/fr \
        --timeout-s 240 --json freerouting_report.json
```

**Bu basamak board'u yazar** (`ImportSpecctraSES` başarılıysa `board.Save`).
Bu yüzden yalnız scratch kopyada koşar ve kanonik dosyaya promote
[[../pcb-orchestrator/references/board-write-lock.md]] §2 transaction'ı ile
yapılır.

Zincir: `pcbnew.ExportSpecctraDSN` → `java -jar freerouting.jar` →
`pcbnew.ImportSpecctraSES`. `kicad-cli`'nin DSN/SES desteği **yoktur**; köprü
gömülü pcbnew script'iyle çalışır.

Zorunlu ön adımlar:
1. **Diff çiftler ve length-match'li netler LOCK'lu.** DSN'e çift semantiği
   taşınmaz; kilitlenmemiş bir çifti FreeRouting tek tek iki iz gibi yeniden
   yönlendirir ve length-match sessizce ölür.
2. `java` PATH'te ve `.jar` mevcut. Yoksa `FreeroutingUnavailable` → makbuz
   `NO_COVERAGE`, **exit 1** — "router çalışmadı" asla "board route edildi"
   diye okunmaz.
3. `--dsn-only` ile önce sadece DSN üret ve gözle bak; ilk koşuda tam zinciri
   açma.

## 3. Nüanslar — bilinen tuzaklar

### (a) `ImportSpecctraSES`'in dönüş değeri tek başına yalan söyler
Gerçek route içeren bir `.ses` padstack uyuşmazlığında `False` dönebilir; ya da
`True` dönüp **sıfır iz** ekleyebilir. Kural: `ok` **ve** `tracks_changed`
birlikte doğru olmadan "route edildi" yazma. Makbuzda
`tracks_after == tracks_before` → `RED`, `PASS` değil.

### (b) FreeRouting yavaşlamaz, **ölür**
Gerçek kartta `PolylineTrace.combine()` içinde sonsuz özyinelemeye girip
`StackOverflowError` atabilir. Üç önlem birden gerekir ve üçü de kodda vardır:
`-Xss8m` (tek başına yetmez — gerçek sonsuz özyinelemeyi durdurmaz),
`-Djava.awt.headless=true` (crash handler pencere açıp süreci sonsuza kadar
bekletmesin) ve stdout'un **satır satır** okunup ilk Java exception'ında
sürecin **anında öldürülmesi** (timeout beklenmez).

Varsayılan timeout 240 s'dir, yarım saat değil: ölçülmüş gerçek kartlar (14 ve
174 net) 120–240 s içinde bitiremedi. Otonom bir zincirin tek routing adımında
yarım saat kilitlenmesi kabul edilebilir değildir — **çağıran bekler değil,
geri düşer**.

### (c) "Push & shove" burada KiCad'in PNS'i değildir
KiCad'in etkileşimli PNS router'ı C++'ta yaşar ve **ne SWIG `pcbnew` modülüne ne
de IPC API'ye** açılmıştır. `topological.suggest_shove()` yalnızca **tek** bir
engelleyen izi kendi dikine kaydırır: kaskad yok, zincirleme itme yok. Kaydırılan
iz başka bir şeye çarpacaksa öneri **geri çekilir**, tahmin edilmez. Kilitli izi
asla taşımaz.

### (d) Otoruter netclass'ı okumaz — kilit okur
Üç motorun hiçbirinde net-class kavramı yok. `grid_astar` yalnız `clearance_mm`
+ `cell_mm` bilir; `topological` istek başına `width_mm`/`clearance_mm` ve
`high_speed` bayrağı bilir; FreeRouting'e DSN üzerinden giden kural seti
KiCad'in kendi netclass'larıdır ama **çift eşleştirmesi taşınmaz**. Netclass↔net
eşleşmesinin iki yönlü testi [[../highspeed-routing/SKILL|highspeed-routing]]
§5f'de; otoruter çağrılmadan önce o test **geçmiş olmalı**.

### (e) Router çıktısı ile DRC arasına hiçbir şey girmez
`route_chain()` DRC koşmaz — bilinçli. Import'tan sonra kapı:

```bash
python3 tools/scripts/drc_gate.py scratch/board.kicad_pcb \
        --schematic-parity --json drc_after.json
```

DRC koşmadıysa makbuz `NO_COVERAGE`'dır. "Router temiz bitirdi" cümlesi bu
repoda tek başına hiçbir kapıyı açmaz.

### (f) Türetilmiş artifact'ler bayatlar
Otoruter board'a yazdığı anda zone fill, teardrop sayısı, uzunluk/skew raporu,
`escape_report.json`, `emc_geometry_receipt.json` ve gerber/CPL çıktıları
**stale** olur. Hepsi yeni board hash'inden yeniden üretilir:
[[../pcb-orchestrator/references/board-write-lock.md]] §3.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Diferansiyel çift routing kalitesi | — | Hiçbir motorda çift semantiği yok; kapsam dışı, kilitli |
| Length-match / skew | — | Otoruter uzunluk bilmez; [[../highspeed-routing/SKILL\|highspeed-routing]] |
| Empedans | D | Geometri ≠ empedans; coupon + TDR |
| Crosstalk | C | Paralel segment kararı [[../crosstalk-thinning/SKILL\|crosstalk-thinning]]'de |
| Referans düzlemi sürekliliği | C | Otoruter katman değiştirirken yarık görmez; ayrı kapı |
| KiCad PNS davranışı | — | Python'a açık değil (§3c) |
| FreeRouting sonucunun tekrarlanabilirliği | `NO_COVERAGE` | Bu ortamda **hiç koşturulmadı**: java + `freerouting.jar` yok |

> **AÇIK MADDE — FreeRouting bu ağaçta hiç çalıştırılmadı.** `test_production.py`
> köprü mantığını kanıtlar (JVM bayrakları, exception'da anında kill, değişmeyen
> track sayısının "routed" sayılmaması, jar yokken `NO_COVERAGE`) ama gerçek
> java/jar ile uçtan uca bir koşu **yoktur**. İlk gerçek koşuya kadar bu
> basamağın çıktısı `CONFIRM`'dür ve tek başına Faz 3→4 kapısını açmaz.

## 5. Doğrulama

```bash
# motorların kendi self-test'leri (opt-in, board gerekmez)
python3 tools/scripts/route/topological.py --self-test
python3 tools/scripts/route/grid_astar.py --self-test
python3 tools/scripts/production/freerouting.py --self-test

# regresyon (82 test: 30 grid A* + 51 topological + 11 freerouting köprüsü)
python3 -m unittest tools.tests.test_grid_astar tools.tests.test_topological \
                    tools.tests.test_production

# registry hash'leri ağaçla uyuşuyor mu
python3 tools/scripts/sync_tool_registry.py --check
```

**Fault-injection (üçü de kanıtlanmış davranış):**

| Enjeksiyon | Beklenen |
|---|---|
| `--no-chamfer` ile koş | `45_degree_geometry` **FAIL** vermeli |
| Boş `requests` / boş `nets` | `NO_COVERAGE` + **exit 1** (exit 0 değil) |
| `--max-nodes 50` ile geniş problem | `found=false`, `reason="node budget (50) exceeded"` |
| `high_speed: true` + tam bariyer | `NOT_FOUND`; via ile **çözülmemeli** |
| `lower_layer_obstacles` alanını sil | katman değişimi **önerilmemeli**, `NEEDS_HUMAN` |
| `--jar yok.jar` | `NO_COVERAGE` + exit 1; java **çalıştırılmamalı** |
| SES import'u track sayısını değiştirmesin | makbuz `RED`, "routed" **yazılmamalı** |

## 6. Kabul kriterleri

- [ ] Kritik netlerin tamamı (`locked_nets[]`) routing'den **önce** kilitlendi;
      kilit, listeyle değil **geometri karşılaştırmasıyla** kanıtlandı
      (`locked_nets_untouched: true`).
- [ ] Router **scratch kopyada** koştu; kanonik board'a yalnız GREEN makbuzla
      atomik promote yapıldı ([[../pcb-orchestrator/references/board-write-lock.md]] §2).
- [ ] `candidate_nets` boş değil; boşsa router **hiç çalıştırılmadı** ve
      `NO_COVERAGE` yazıldı.
- [ ] Arama bütçesi makbuzda yazılı (`budget.kind`/`value`) ve bütçesiz koşu yok;
      bütçe patladıysa `tripped: true` ve etkilenen netler `unrouted_nets`'te.
- [ ] `cell_mm < (iz_genişliği + clearance) / 2` doğrulandı (grid A* koştuysa).
- [ ] `tracks_after > tracks_before` **ölçüldü**; importer'ın dönüş değerine
      güvenilmedi.
- [ ] Import sonrası DRC koştu (`--schematic-parity --severity-all`) ve
      0 ihlal / 0 unconnected verdi ([[../sematik-erc/SKILL|sematik-erc]] §4).
- [ ] `high_speed` netlerde otoruter kaynaklı **via yok**; `NOT_FOUND`
      çıktıysa yerleşime feedback açıldı, via ile "çözülmedi".
- [ ] Otoruter kaynaklı 90°±5 köşe sayısı = **0** (chamfer kapısı PASS).
- [ ] Board'a yazıldıysa türetilmiş artifact'ler (zone fill, teardrop, uzunluk
      raporu, gerber/CPL) **stale** işaretlendi ve yeniden üretildi.
- [ ] FreeRouting basamağı koştuysa `java_present` + `pcbnew_importable`
      makbuzda `true`; biri false ise sonuç `NO_COVERAGE` sayıldı.
- [ ] Feedback kenarı sayıldı; döngü sınırı (ör. 3) aşıldıysa `NEEDS_HUMAN`.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.
- [ ] Sonuç [[../design-checker/SKILL|design-checker]]'a **rapor olarak değil,
      board olarak** verildi; checker kendi ölçümünü yaptı.

## İlgili

[[../highspeed-routing/SKILL|highspeed-routing]] · [[../dfm/SKILL|dfm]] ·
[[../crosstalk-thinning/SKILL|crosstalk-thinning]] ·
[[../emc-esd/SKILL|emc-esd]] · [[../design-checker/SKILL|design-checker]] ·
[[../pcb-orchestrator/SKILL|pcb-orchestrator]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`place-force-directed`** — Netlist'in sürdüğü kuvvet-yönelimli yerleşim — zevkin değil; kritik netler kilitlendikten sonra.
- **`route-safe-write`** — Netclass genişlik ihlali kontrolü + yaz-doğrula-yoksa-geri-al; DRC/unconnected regresyonunda rollback.
- **`routing-table`** — Net bazlı routing karar tablosu (CSV/XLSX); hangi netin **neden** o katmanda olduğunu yazılı bırakır.
- **`recovery`** — İzole yürütme + koşarken ölen router için geri çekilme merdiveni.
