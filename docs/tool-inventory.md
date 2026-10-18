# Araç envanteri

> **Bu dosya iki eski envanterin birleşimidir** (konsolidasyon M2):
> `SKILL-pcb-tool-v2-kopruleri.md` + `SKILL-dogrulama-matrisi.md §Araç envanteri`.
> Hiçbiri *skill* değildi (karar prosedürü ve tetikleyicisi yoktu); ikisi de
> hash-bağlı **kayıt**tı ve aynı dosya adları için **çelişen** doğrulama
> verdict'leri veriyordu.

Kanonik makine-okur kayıt: [`../config/tool_registry.json`](../config/tool_registry.json)

**Registry elle bakılmaz.** `tools/scripts/sync_tool_registry.py` ağacı tarar,
SHA-256'ları hesaplar ve `config/tool_registry.json`'u yeniden üretir:

```bash
python3 tools/scripts/sync_tool_registry.py --check    # ağaçla uyuşmuyorsa exit ≠ 0
python3 tools/scripts/sync_tool_registry.py --write    # yeniden üretir
```

Bu dosya registry'nin **insan tarafını** taşır: hangi araç ne zaman çağrılır,
sınırı nedir, hangi skill'e aittir.

---

## 0. Çağrı disiplini

1. Her çağrıda **scratch kopya** kullan; kanonik KiCad dosyasına doğrudan yazma.
2. Bir testin *bulunması* teknik PASS değildir; uygun fixture ve güncel proje
   girdisiyle koş.
3. Script çıktısı JSON olsa bile `scanned=0`, eksik model, eksik kaynak, dış
   araç hatası veya coverage boşluğunu **PASS'a dönüştürme**.
4. Kodda görülen IPC/EMC/empedans sayısını, standardın sürüm/locator'ı ve proje
   profili olmadan skill'e **normatif eşik olarak kopyalama**.
5. Ağ, KiCad, ngspice veya CAD indirme çağrılarının dış etkisini önce
   onayla/capability receipt'e bağla.

## 1. CLI sözleşmesi (tüm araçlar)

| Bayrak | Anlamı |
|---|---|
| `--json <dosya>` | **YAZAR** — rapor çıktısı. Girdi okumaz. |
| `--input-json <dosya>` | Girdileri JSON'dan okur (yalnız `crosstalk_thinning.py`) |
| `--self-test` | Kanonik doğrulama bayrağı; fault-injection içerir |
| `--postflight` | Bildirilen argv'yi **çalıştırır** (yalnız `context_pack.py`) |
| `--exclude CHECK=REASON` | Bir kontrolü **gerekçesiyle birlikte** kapsam dışı bırakır (`production/outputs.py`, `drc_gate.py`). Yalnız **ölçülmemiş** bir kontrol dışarı alınabilir; ölçülmüş bir FAIL alınamaz. Gerekçesiz exclusion CLI tarafından reddedilir. Tekrarlanabilir. |

### Çıkış kodu sözleşmesi (2026-08-10'da sıkılaştırıldı)

**`NO_COVERAGE` çıkış kodu 0 ile taşınamaz.** Ölçülmemiş bir kontrol, ölçülüp
temiz çıkmış bir kontrolla aynı sinyali veremez. Şu araçlar bu turda
`1 if summary[FAIL] else 0` ifadesinden — `common/findings.board_exit_code()`'un
muafiyetini devralmayan elle yazılmış kopyalardan — kurtarıldı:

`production/outputs.py` · `board/measure_board.py` · `independent_verify.py` ·
`pmic_allocation.py` · `board/reference_plane.py` · `production/assembly.py` ·
`board/thermal_bridge.py` · `sim/ngspice.py` · `sim/parasitics.py` ·
`stackup_plan.py` · `benchmark.py` · `report/routing_table.py`

**Tek istisna, bilerek dondurulmuş:** `common/findings.board_exit_code()`
`NO_COVERAGE` için 0 döndürmeye devam eder. Docstring'i bunu `via_stub`'ın
yerleşik kardeş-araç sözleşmesi olarak kaydeder ve 12 aracı ilgilendiren bir
politika kararıdır — **açık madde**. Bu davranışı isteyen araç onu *çağırmak*
zorundadır; yeniden yazmak `test_coverage_monotonicity.py`'deki AST
denetleyicisi tarafından reddedilir.

---

## 2. Araç yönlendirme tablosu

### Yürütme, bağlam ve kapı

| Araç | Ne yapar | Sahip skill |
|---|---|---|
| `tools/scripts/context_pack.py` | `pcb-context-pack/v1` manifestini doğrular: minimum karar girdileri, SHA-256 tazeliği, path sınırı, blocking soru, file/byte bütçesi, typed evidence provenance, dar tool yetkisi, claim→evidence sözleşmesi. Gate validator gerçek argv entrypoint'i değilse **bloklar** | [decompose-pcb-decisions](../skills/decompose-pcb-decisions/SKILL.md) · [pcb-orchestrator](../skills/pcb-orchestrator/SKILL.md) |
| `tools/scripts/gate_receipt.py` | Kalite makbuzunu şema+sürüm, input/tool/config SHA-256, exact argv/exit, metric semantiği+kapsam, child gate indirgeme, promotion ve retention açısından doğrular; **yalnız etkin GREEN için exit 0** | [pcb-orchestrator](../skills/pcb-orchestrator/SKILL.md) · [dogrulama-matrisi](../skills/dogrulama-matrisi/SKILL.md) |
| `tools/scripts/research_promotion_gate.py` | `_research` claim tablolarını salt-okunur tarar; `KABUL` claim'inde HTTPS otorite ön-eleği, belge/revizyon+locator+erişim tarihi, A/B/C/D+L0..L3, path+SHA-256 bağlı validation fixture'ı, karar/kanıt izi ve hedef skill'i zorunlu kılar | [pcb-orchestrator](../skills/pcb-orchestrator/SKILL.md) |
| `tools/scripts/sync_tool_registry.py` | `config/tool_registry.json`'u ağaçtan yeniden üretir (locator + SHA-256) | [pcb-orchestrator](../skills/pcb-orchestrator/SKILL.md) |

### Sinyal bütünlüğü modelleri (ikisi de `signoff: false`)

| Araç | Ne yapar | Sahip skill |
|---|---|---|
| `tools/scripts/crosstalk_thinning.py` | Paralel iz çiftinde NEXT taraması + güvenli inceltme kararı: `kx=1/(1+(D/H)²)`, `Kb=0.25·kx`, doyum uzunluğu ölçeklemesi. `SAFE_TO_THIN` / `CROSSTALK_RISK` / `THINNING_BLOCKED` / `NO_COVERAGE`; inceltilmiş genişliği `D'=S+W'` ile **yeniden** tarar | [crosstalk-thinning](../skills/crosstalk-thinning/SKILL.md) |
| `tools/scripts/zdiff_solver.py` | IPC-2141/Wadell diferansiyel mikroşerit/stripline; hedef Z için (W,S) ve stackup (h) taraması | [empedans-stackup](../skills/empedans-stackup/SKILL.md) |

### Board ölçümü (hepsi salt-okunur, `pcbnew` gerektirir)

| Araç | Ne ölçer | Sahip skill |
|---|---|---|
| `tools/scripts/board/dfm_emc_check.py` | teardrop sayısı, via-in-pad, annular ring, EPAD pasta kapsaması, kenar/depanel seramik keepout, hızlı via'nın dönüş via'sı, hızlı netlerin netclass'ı | [dfm](../skills/dfm/SKILL.md) · [emc-esd](../skills/emc-esd/SKILL.md) |
| `tools/scripts/board/mask_dam.py` | Pin-arası kanalda `baraj = bakır_boşluk − expansion(a) − expansion(b)` | [dfm](../skills/dfm/SKILL.md) · [highspeed-routing](../skills/highspeed-routing/SKILL.md) |
| `tools/scripts/board/stitch_density.py` | `f_knee_edge` → λ/20 hedefiyle kart kenarı + via dönüş dikişi aralığı | [emc-esd](../skills/emc-esd/SKILL.md) |
| `tools/scripts/board/via_stub.py` | Hızlı via'nın kullanılmayan barel boyu + çeyrek-dalga rezonans ön-elemesi | [highspeed-routing](../skills/highspeed-routing/SKILL.md) |
| `tools/scripts/board/route_audit.py` | Via bakır açıklığı · via/ped **delik** açıklığı · delik-delik · kart kenarı halkası. Yönlendiricinin tek engel haritasına sormadığı sorular: via iz değildir (0.45 mm via 0.4 mm izden geniştir), deliğin bakırdan **ayrı** kuralı vardır ve her katmanı deler, kenar halkası `board_outline.json`'da **beyan** edilir. Beyan ile `.kicad_pro` ayrışırsa `AYRISMA` basar ve beyana göre denetler. KiCad DRC'ye karşı UUID düzeyinde çapraz kontrol edildi (cam-w, 2026-08-10): 49/49 · 3/3 · 21/21 birebir, 0 yanlış pozitif. **İz-iz ve ped-ped açıklığı kapsam dışı** | [otonom-routing](../skills/otonom-routing/SKILL.md) · [dfm](../skills/dfm/SKILL.md) |
| `tools/scripts/board/decoupling_coverage.py` | IC güç pinlerini board'DAN KEŞFEDER ve her biri için en yakın uygun (47-220nF) dekuplaj kapasitörünün mesafesini ölçer — `measure_board.py`'nin elle `REF.PAD:REF.PAD` isimlendirme kısıtını kapatır (unutulan bir pin sessizce kapsam dışı kalmaz). İki ayrı ihlal: `capacitor_too_far` / `no_capacitor` | [dfm](../skills/dfm/SKILL.md) · [guc-isil](../skills/guc-isil/SKILL.md) |

### Yönlendirme motorları (üçü de [otonom-routing](../skills/otonom-routing/SKILL.md)'e ait)

| Araç | Ne yapar | Board yazar mı |
|---|---|---|
| `tools/scripts/route/topological.py` | `DIRECT → L_BEND → U_DETOUR → LAYER_CHANGE → NOT_FOUND` merdiveni + 45° pah + tek adımlık shove önerisi; üç kapı: `route_found`, `high_speed_via_ban`, `45_degree_geometry` | **Hayır** — `write_tracks()` var ama CLI çağırmaz |
| `tools/scripts/route/grid_astar.py` | Tek net için 8-yönlü ızgara A*, iki fazlı çarpışma (box AABB + segment narrow-phase); tek bütçe `--max-nodes` | Hayır — yalnız polyline üretir |
| `tools/scripts/production/freerouting.py` | DSN/SES köprüsü: `pcbnew.ExportSpecctraDSN` → `java -jar freerouting.jar` → `ImportSpecctraSES`; Java exception'ında süreci anında öldürür | **Evet** — başarılı SES import'ta `board.Save()`; yalnız scratch kopyada koş |

> Üçünde de **diff-pair ve net-class semantiği yoktur.** Kritik netler otorutere
> ayarla değil **kilitle** korunur; ayrıntı ilgili skill'de.

### Şematik

| Araç | Ne yapar | Sahip skill |
|---|---|---|
| `tools/scripts/sch/sch_wire.py` | Gerçek `(wire)` / `(junction)` / label / power sembolü üretir; pin ankraj geometrisini (rotasyon+mirror+Y-flip) çözer | [sematik-erc](../skills/sematik-erc/SKILL.md) |
| `tools/scripts/sch/sch_route.py` | Pin→pin ortogonal router (A*): gövdelerden kaçar, MST ile bağlar, yabancı net'e değmeyi yasaklar | aynı |
| `tools/scripts/sch/rewire.py` | Şematiği baştan teller; netlist'i kanıtlar, tutmazsa `.bak`'tan geri alır | aynı |
| `tools/scripts/sch/wireify.py` | Pin üstündeki label'ı dışarı taşıyıp araya tel koyar (netlist değişmez) | aynı |
| `tools/scripts/sch/i2c_pullup.py` | I²C pull-up penceresi: netlist'ten gerçek pull-up ağını (ve **paralel eşdeğerini**) çözer, `Rmin=(VDD−VOL)/IOL` ve `Rmax≈tr/(k·C_bus)` ile karşılaştırır. **Hiçbir I²C sabiti kodda değil** — spec/revizyon, VOL/IOL, C_bus, tr kaynaklı girdi olarak gelir; biri eksik ya da kaynaksızsa sonuç `NO_COVERAGE`, tahmin değil | aynı |

### MCP sunucuları (taşıma katmanı — mühendislik kararı vermez)

| Sunucu | Yayımladığı | Not |
|---|---|---|
| `tools/servers/crosstalk_mcp_server.py` | `analyze_crosstalk_and_thinning` | stdlib-only stdio JSON-RPC (MCP 2024-11-05) |
| `tools/servers/board_mcp_server.py` | Dört board checker'ı | `pcbnew` gerektirir; ayrı process. `board` argümanı `PCB_AGENT_BOARD_ROOT` içine hapsedilir ve `.kicad_pcb` olmalıdır |

> **MCP'ye yayımlanmayanlar:** `rewire.py`, `wireify.py` (şematik mutasyonu) ve
> `context_pack.py --postflight` (bildirilen argv'yi çalıştırır).

### Tur 3 eklentileri (2026-08-06, `pcb-tool-v2` drift senkronizasyonu)

Aşağıdaki 11 araç bu turda `pcb-tool-v2`'den **ilk kez** ported edildi
(önceden envanterde/registry'de hiç yoktu). Ayrıntı ve kaynak eşlemesi:
[CONSOLIDATION-NOTES.md §Tur 3](CONSOLIDATION-NOTES.md#tur-3--pcb-tool-v2-drift-senkronizasyonu-2026-08-06).

| Araç | Ne yapar |
|---|---|
| `tools/scripts/board/mechanical_dxf.py` | Mekanik DXF anahatını (board outline) gerçek `ezdxf.readfile()` ile okur, `keepout_zones.json` üretir (montaj deliği/komponent-yüksekliği/IPD tolerans zinciri) |
| `tools/scripts/board/current_density.py` | DC akım yoğunluğu için direnç-ağı çözücü, ısı haritası üretir |
| `tools/scripts/sim/openems.py` | Diferansiyel çift geometrisi çıkarımı + Touchstone (.sNp) değerlendirmesi (FDTD motoru YOK — yalnız girdi hazırlama/çıktı okuma) |
| `tools/scripts/route/diff_pair.py` | En yakın-uç eşleştirme, paralellik açısı, pcbnew üzerinden diferansiyel çift kilitleme |
| `tools/scripts/route/safe_write.py` | Netclass genişlik ihlali kontrolü + yaz-doğrula-yoksa-geri-al (DRC/unconnected regresyonunda rollback) |
| `tools/scripts/report/routing_table.py` | Net bazlı routing karar tablosu (CSV/XLSX), diferansiyel çift eşleşmesi, arayüz türü tahmini, katman zinciri metni |
| `tools/scripts/pmic_allocation.py` | PMIC ray/kanal tahsisi (greedy) + ek dağıtım gücü hesabı |
| `tools/scripts/report/capability_inventory.py` | Git geçmişi + test modülü eşleşmesi + kategori haritası + 4 AST-tabanlı sütun (Security Status, Internal/External Dependencies, AI Contract). `test_status` gerçekten `pytest --junit-xml` ile ölçülür ve ölçülemediğinde bunu **söyler** (`test_measurement.warning`); `real_or_mock` çıplak kelime değil **itiraf cümlesi** arar, elenen vuruşlar gerekçesiyle `mock_screen`'e yazılır |
| `tools/scripts/board/reference_plane.py` | İz segmentinin referans düzlem sürekliliğini kontrol eder (split/kesik referans düzlemi tespiti) |
| `tools/scripts/testpoint_plan.py` | Zorunlu test noktası kapsamını + bring-up checklist'ini üretir (`testpoint_plan.schema.json`'a göre doğrulanmış) |
| `tools/scripts/production/contracts.py` | `drc.json`/`parts.json` kontrat kapıları (her zaman DİSKTEN okunur, bellekteki değer güvenilmez) — önceden vardı, bu turda provenance kaydı eklendi |

Mevcut araçlara bu turda eklenen kütüphane-seviyesi yetenekler (kendi CLI'larına
sarılmış, ayrıntı CONSOLIDATION-NOTES §Tur 3): `common/stackup.py` →
`PhysicalVia`/`select_via_type_for_bga` (via tipi-farkında aspect-ratio),
`route/grid_astar.py` → `layered_astar_search`/`coupled_astar_search`/
`via_placement_valid` (katman-farkında + diferansiyel-çift A*, via yerleşim
geçerliliği), `common/decisions.py` → kritik-pin datasheet-teyit kapısı,
`common/ipc7351.py` → gullwing/QFN/BGA land pattern, `board/thermal_bridge.py`
→ junction-sıcaklığı ilk-yaklaşımı, `common/escape_geom.py` → çoklu-kart skew
bütçesi, `sim/ngspice.py` → transient analiz + dekuplaj önerisi.

### Kapılar ve doğrulama

| Araç | Ne yapar | Sahip skill |
|---|---|---|
| `tools/scripts/drc_gate.py` | `kicad-cli` etrafında **hash-bağlanabilir** DRC/ERC kapısı; `drc.sh`'ın yerini alır (o script hash bağı taşımıyordu). ERC census'ü **tüm sayfa kümesini** okur (kök-sayfa-tek census hiyerarşik tasarımda paydayı 83 kat küçük gösteriyordu); `parity_difference_count` ölçülmediğinde **`null`**, 0 değil; DRC'de **`--all-track-errors`** zorunlu — onsuz iz başına tek hata raporlanıyor ve kazananı bir yarış seçiyordu (D7); her rapor `provenance.engine_version`'ı (`kicad-cli version` ile **ölçülür**) ve yol içermeyen `argv_signature`'ı taşır, `--compare A.json B.json` sürüm/bayrak farklı ya da bilinmiyorsa karşılaştırmayı **REDDEDER** (çıkış 2) — commit'li 191 ihlallik taban çizgisi 10.0.5 ile, buradaki koşu 10.0.4 ile üretilmişti ve fark tasarıma yazılıyordu (D8) | [design-checker](../skills/design-checker/SKILL.md) · [dogrulama-matrisi](../skills/dogrulama-matrisi/SKILL.md) · [otonom-routing](../skills/otonom-routing/SKILL.md) |
| `tools/scripts/independent_verify.py` | KiCad DRC/ERC'den **tamamen bağımsız** ikinci doğrulama katmanı; KiCad yalnız jenerik kuralları bilir | [design-checker](../skills/design-checker/SKILL.md) |
| `tools/scripts/assert_matrix.py` | Doğrulama matrisini **prose değil kod** olarak koşar: her kurala assertion bağlar, `--prove` ile bildirilen kusuru enjekte edip yakalanmasını zorunlu kılar. **Hiçbir şey ölçmez** — başka bir aracın raporundaki bir konuma iddia bağlar. `--artifact NAME=PATH` ile tek bir set proje-bağımsız kalıp gerçek karta bağlanır (her kart için setin kopyası ayrışırdı) | [dogrulama-matrisi](../skills/dogrulama-matrisi/SKILL.md) *(SKILL.md henüz adını anmıyor)* |
| `tools/scripts/promote.py` | scratch → kanonik terfi kapısı, `gate-receipt/v1` üretir | [pcb-orchestrator](../skills/pcb-orchestrator/SKILL.md) *(anılmıyor)* |
| `tools/scripts/benchmark.py` | Sabit problem setini N kez koşup **ölçülen geçme oranı** üretir. Test paketi "her parça yazarının istediği gibi davranıyor" der; bu "akış ne sıklıkla başarıyor" der | — |
| `tools/scripts/pipeline_adapters.py` | Arşivin `main.py`'sinin tek monolitik `cmd_run`'a gömdüğü fazlar üzerine ince, tek tek çağrılabilir fonksiyonlar. **Sıralama kararı çağırana ait** | — |

### Empedans, alan ve tolerans

| Araç | Ne yapar | Not |
|---|---|---|
| `tools/scripts/field_solver.py` | 2-D kesit **sayısal** alan çözücü — kapalı formun yerine gerçek elektrostatik problemi çözer. İki bağımsız çözücü (scipy sparse + saf-numpy SOR) 1.3e-14'e kadar uyuşur. **Esnek/esnek-sert:** `flex-microstrip` ve `coupled-flex-microstrip` geometrileri — coverlay yapıştırıcısı ve filmi **ayrı** dielektrikler; yığın `--flex-stackup` JSON'undan gelir ve her katman `source` taşımak zorundadır (`--pi-er` diye bir bayrak kasten yok). Yapıştırıcılı taban laminat kapsam dışı, reddedilir  | Ölçülen kapalı-form sapması: mikroşerit −%10.2, diferansiyel +%8.0, şerit hat +%3.6 |
| `tools/scripts/monte_carlo.py` | Tolerans analizi: `circuit` (ngspice) ve `impedance` (field2d duyarlılıkları) | Doğrusallaştırma tam çözümlere karşı yeniden ölçülür; eşik aşılırsa `NO_COVERAGE`. Verimler Wilson %95 aralığıyla |
| `tools/scripts/stackup_plan.py` | Tasarım-niyet JSON'undan katman sayısı / stackup planı | — |
| `tools/scripts/coupon_plan.py` | Empedans test kuponu planı; **gerber çıktısından ÖNCE** koşar | `--tdr-rise-time-ps` **zorunlu, asla varsayılan değil**: 20 ps ile 200 ps kafa arasında 10× fark var ve kısa kupon *sessizce* başarısız olur |
| `tools/scripts/board/skew_timing.py` | Ham `skew_mm` → **ps** türetilmiş metrik. `measure_board.py` raporunu + kaynaklı `er_eff`'i tüketir; `er_eff` ya `--er-eff` ile beyan edilir ya `--solve` ile deklare edilmiş kesitten `common.field2d` 2-B çözümüyle gelir. `pcbnew` **gerektirmez** (ölçmez, dönüştürür) | `er_eff` yoksa dönüşüm **çalışmaz** — `coupon_plan.py`'nin tahmini reddedişinin aynısı. `escape_geom.py`'deki tipik FR-4 sabiti bu yolda erişilebilir değil. `length-report/v1` şemasının **ilk gerçek üreticisi** |

### Sinyal bütünlüğü ve simülasyon

| Araç | Ne yapar | Not |
|---|---|---|
| `tools/scripts/sim/sparam.py` | Touchstone **yazma**, mixed-mode dönüşüm, kanal kaskadı | — |
| `tools/scripts/sim/eye.py` | İstatistiksel göz diyagramı | — |
| `tools/scripts/sim/correlate.py` | Ölçülen Touchstone'u simüle edilene karşı korele eder — **paketteki modelin yanlış olduğunu gösterebilen ilk sayı** | Ölçüm yoksa `MEASUREMENT_ABSENT` → NO_COVERAGE. Ara değerleme **yok**; de-embedding **beyan edilir, çıkarılmaz** |
| `tools/scripts/sim/loop_stability.py` | Döngü-kazanç yanıtından kararlılık marjları | Hiçbir kompanzasyon **modellemez**; başkasının ürettiği taramayı okur. Kabul eşikleri **girdi** |
| `tools/scripts/sim/ngspice.py` | ngspice köprüsü (transient + dekuplaj önerisi) | **Asla** MIPI CSI-2 SI kanıtı olarak kullanılmaz |
| `tools/scripts/sim/openems.py` | Diferansiyel çift geometri çıkarımı + Touchstone değerlendirmesi | **FDTD motoru YOK** — yalnız girdi hazırlama / çıktı okuma |
| `tools/scripts/sim/ai_feedback_loop.py` | Simülatörün diferansiyel çift verdict'ini okur, hedef empedans tutmadıysa geometri düzeltmesi hesaplar | `pcbnew` gerektirir; **yalnız iz genişliği** uygulanır (KiCad'de net-to-net gap API'si yok) |
| `tools/scripts/sim/model_registry.py` | Her simülasyonun dayandığı **satıcı modelini** adlandırır ve kullanımını kapılar: bütünlük → yerine geçme → zarf → otorite | **Doğruluğu yargılamaz** (o `correlate.py`'nin işi). Zarf dışı sorgu `OUT_OF_ENVELOPE` → NO_COVERAGE: kırpılmış sayı değil, **sayı yok**. Beyan edilen aralık dosyanın kendisiyle karşılaştırılır; aşan beyan FAIL |

### Güç, termal ve akım

| Araç | Ne yapar | Not |
|---|---|---|
| `tools/scripts/board/electrothermal.py` | İz için **bağlaşık** elektrotermal çalışma noktası + **kaçış tespiti** | `G = I²·R20·α·Rth ≥ 1` iken sıcaklık söylemeyi **reddeder**; red kapalı-form kazançtan gelir, iterasyonun oturmamasından değil |
| `tools/scripts/board/rail_budget.py` | Güç açılış sıralaması + ray başına gürültü bütçesi | `V_EN(HI)` datasheet girdisi, asla varsayılan değil. PSRR bir eğridir: başka frekanstaki değer **reddedilir**, ara değerlenmez |
| `tools/scripts/shunt_plan.py` | Ray başına **tahribatsız** akım ölçümü için seri şönt + prob test noktası planı | Regülatörün **beyan edilen** çıkışından boyutlanır, beklenen çekimden değil — bilinmeyenden boyutlamak döngüsel olurdu |
| `tools/scripts/board/current_density.py` | DC akım yoğunluğu için direnç-ağı çözücü + ısı haritası | — |
| `tools/scripts/board/thermal_bridge.py` | Junction sıcaklığı ilk-yaklaşımı | — |
| `tools/scripts/pmic_allocation.py` | PMIC ray/kanal tahsisi (greedy) + dağıtım gücü | — |
| `tools/scripts/connector_current.py` | `connector_spec` ↔ `rail_tree` **bağımsız** çaprazlama: kontak kapasitesi ile rayın worst-case talebini iki ayrı kaynaklı artifact'ten okur, açık rail→kontak-grubu eşlemesiyle karşılaştırır | **Kapasite ≠ talep**, yapısal olarak zorlanır: `connector_spec`'in içine önceden yazılmış `rail_cross_check` talep olarak **kullanılmaz**, yalnız ray ağacına karşı *doğrulanır*; çelişirse FAIL. `N × rated` reddedilir (merkez kontak akımı dışa atar). Derating eğrisi/ortam sıcaklığı yoksa oda sıcaklığı ratingi **yerine konmaz** → `NO_COVERAGE` |

### IPC hesaplayıcıları ve kural emisyonu

| Araç | Ne hesaplar | Not |
|---|---|---|
| `tools/scripts/ipc/trace_width.py` | IPC-2152 temelli asgari iz genişliği | Fab limitleri `common.stackup.fab_profiles()`'dan gelir, **hardcode edilmez** |
| `tools/scripts/ipc/via_current.py` | IPC-2221 temelli via matkap/pad çapı | aynı |
| `tools/scripts/ipc/clearance.py` | Gerilim/irtifa açıklık hesabı | — |
| `tools/scripts/ipc/fab_class.py` | IPC-6012 Class 2/3 limit denetimi — **verilen ölçümleri** denetler | Ölçüm yoksa kapı açılmaz |
| `tools/scripts/ipc/dfa.py` | IPC-A-610 montaj açıklığı motoru | — |
| `tools/scripts/dru_emit.py` | `scripts/ipc/*` sayılarını KiCad 10 `.kicad_dru` özel kural dosyasına çevirir | Öncelik sırası `test_dru_precedence.py`'de bağlı |
| `tools/scripts/emc_rules.py` | 3W, 20H ve λ/20 via-dikiş aralığı kural motoru | Üçü de yaygın alıntılanan yüksek-hız kuralları; **normatif eşik değil** (bkz. §5) |

### Kart ölçümü (ek — hepsi `pcbnew` gerektirir)

| Araç | Ne ölçer | Sahip skill |
|---|---|---|
| `tools/scripts/board/measure_board.py` | Elle `REF.PAD:REF.PAD` isimlendirmesiyle mesafe ölçümü | — |
| `tools/scripts/board/reference_plane.py` | İz segmentinin referans düzlem sürekliliği (split/kesik referans) | — |
| `tools/scripts/board/pair_coupling.py` | Diferansiyel çiftin ne kadarının birlikte koştuğu — kapsama taraması | — |
| `tools/scripts/board/collision_radar.py` | Yerleşim çakışma radarı — resme bakmak yerine **ölçer** | [design-checker](../skills/design-checker/SKILL.md) |
| `tools/scripts/board/mcad_collision.py` | ECAD/MCAD 3-B çakışma: gövdeler ↔ muhafaza engelleri | [mekanik-dxf](../skills/mekanik-dxf/SKILL.md) |
| `tools/scripts/board/gerber_dfm.py` | **Gerçek export edilmiş RS-274X gerber'lardan** DFM kanıtı — DRC'den bağımsız | — |
| `tools/scripts/board/boot_observability.py` | *"Kart açılmıyor, hangi ray gelmedi?"* — `rail_budget`'ın enable grafiğini `testpoint_plan`'in kapsamıyla çaprazlar | **"Test noktası olan ray" saymaz**, ayırt edilebilir **arıza sınıfı** sayar: her rayı kapsayan bir plan bile iki arızayı ayırt edilemez bırakabilir. PG asla ray ölçümü değildir. Fiziksel prob erişimine **girmez** |
| `tools/scripts/report/cross_section.py` | Kartın milimetrelik bir bölgesini ajanın **görebileceği** PNG'ye basar | — |

### Üretim ve tedarik

| Araç | Ne yapar | Not |
|---|---|---|
| `tools/scripts/production/outputs.py` | Fabrikasyon çıktıları için tek komut — **doğrulama kapısı önde** | [design-checker](../skills/design-checker/SKILL.md) |
| `tools/scripts/production/assembly.py` | CPL/montaj: pick point, fab rotasyon haritası, oryantasyon, panelizasyon | — |
| `tools/scripts/production/bom_lifecycle.py` | BOM yaşam döngüsü / tedarik riski kapısı — **tahmin etmeyi reddeder** | `risk_score` şemanın 1.0 tavanına kırpılır; ham toplam gerekçede saklanır |
| `tools/scripts/production/cad_assets.py` | İndirilen sembol/footprint **pin sayısı datasheet'e karşı kanıtlanana kadar güvenilmez** | **Ağ çağrısı YAPMAZ** — yalnız URL'yi yargılar; transferi ajan yapar. **Sayı kapısıdır**, semantik kapı değil → `cad_asset_qualification.py` |
| `tools/scripts/production/cad_asset_qualification.py` | Sembol+footprint'in otoriter pin/paket tanımına **pin-pin, pad-pad, paket-varyantı** düzeyinde uyup uymadığı; kayıt öncesi serbest bırakma kapısı | Sayı eşitliği kimlik eşitliği değildir: eşit sayıda fakat **fonksiyonu takas edilmiş** pin, count kapısında PASS'tir. Ad normalleştirmesi **yok** (case folding, `~{}`, alias yok) — normalleştirici, uyuşmazlığın sessizce öldüğü yerdir. Datasheet **çıkarmaz**: otoriter kayıt beyan edilmiş bir girdidir |
| `tools/scripts/production/first_article.py` | Released paketten üretilen ilk kartın as-built konformansı; montajcı AOI/X-ray/MSL/depanel kaydını released paketle uzlaştırır | **Görüntüye bakmaz** — yapılandırılmış sonuç + hash'li locator tüketir, locator'ı açmaz. Aynı revizyon farklı bayt = `RED` (sessiz ikame), farklı revizyon = `STALE` (başka kartın kanıtı) |
| `tools/scripts/production/jlc_library.py` | LCSC parça numarasından KiCad sembol/footprint/3B modeli (JLC2KiCadLib köprüsü) | — |
| `tools/scripts/production/jlcpcb_dfm.py` | JLCPCB erken-DFM API köprüsü | ⚠️ **İSKELET — gerçek bir JLCPCB hesabına karşı doğrulanmadı** (#43, kullanıcı bloklu). Endpoint ve anahtar yalnız ortam değişkeninden |
| `tools/scripts/testpoint_plan.py` | Zorunlu test noktası kapsamı + bring-up checklist | `method` alanı DFT kararına kadar geçici varsayılanda |

### Yerleşim, hafıza ve raporlama

| Araç | Ne yapar | Not |
|---|---|---|
| `tools/scripts/place/force_directed.py` | Netlist'in sürdüğü kuvvet-yönelimli yerleşim — zevkin değil | — |
| `tools/scripts/block_catalog.py` | Mimari kararından **parametrik devre bloğu** örnekler ve tasarımı ona karşı denetler | Şematik **üretmez** (üretilmiş şematik, arkasından yapılan düzenlemeleri siler). MPN değil **rol** yayar |
| `tools/scripts/memory/error_memory.py` | Kalıcı ERC/DRC/DFM hata hafızası: ne denendi, neye mal oldu | — |
| `tools/scripts/memory/priors.py` | Hatırlanan düzeltmeleri **gerçekten işe yaramışlığa** göre sıralar ve sıralamayı ölçer | **Wilson ALT SINIRINA** göre sıralar; `--min-attempts` altı `INSUFFICIENT_EVIDENCE` ve hiç sıralanmaz. Kalibrasyon leave-one-out ölçülür |
| `tools/scripts/memory/recovery.py` | İzole yürütme + koşarken ölen router için geri çekilme merdiveni | — |
| `tools/scripts/report/capability_inventory.py` | Yetenek envanteri üreteci (§3'e bakınız) | `test_status` gerçekten `pytest --junit-xml` ile ölçülür ve **ölçülemediğinde bunu söyler** |
| `tools/scripts/topology_advise.py` | Başka bir aracın ölçtüğü eksikliğe **topoloji değişikliği** önerir (ek dekuplaj kademesi, LC son-filtre, dikiş via sırası, guard iz, feed-forward sıfır…) | **Hipotez üretir, çözüm değil.** Yön söyler, **büyüklük asla** — birim içeren katalog girişi yüklenmez. Her giriş doğrulayıcı araç + çürütücü gözlem + bedel + en az bir eleme koşulu taşımak zorunda. Kartı **ölçmez**, RED vermez |

### ✅ Yönlendirme boşluğu — KAPANDI (2026-08-09)

**Ölçüm (2026-08-08, düzeltilmiş):** registry'de **82 araç**, skill'lerin
`tools:` alanında anılan **30**, hiçbir skill'in anmadığı **52**.

> **2026-08-09 kapanışı:** registry **83 araç** (`parasitics` eklendi),
> anılan **78**, sahipsiz **5** — hepsi gerekçeli muaf. Yön D testi yeni
> aracı skill'siz bırakmayı engelledi ve Paket 6'da gerçekten kırmızıya
> döndü.
>
> **2026-08-10 ölçümü:** registry **89 araç**, anılan **84**, sahipsiz
> **5** — muaf küme değişmedi. Artan 6'nın 3'ü bu turun yeni araçları
> (`cad-asset-qualification`, `first-article`, `physical-measurement`),
> kalanı 08-09'dan sonra eklenenler. Yön D testi üçünü de skill'siz
> bırakmayı reddetti: her biri kendi skill'inin `tools:` alanında.
>
> Yukarıdaki 82/83/78 sayıları **tarihsel kayıttır**, kaynak değil. Anlık
> değer için §Sayaç drift'i'ndeki komutu koş.

> *Önceki kayıt "55" diyordu ve bu bölüm iki paragrafta iki farklı sayı
> (55 ve 52) taşıyordu.* İkisi farklı ölçümdü: 55, tüm skill `.md`
> metinlerinde `.py` **dosya adı** arayan kaba bir aramadan; 52 ise ajanın
> gerçekten okuduğu **`tools:` frontmatter** alanından. Tetiklenebilirlik
> için bağlayıcı olan ikincisi.

**Yapılan:** 52 aracın **47'si** işlevine göre sahibi olan skill'e bağlandı
(15 skill, 59 bağ — bazı araçlar birden çok skill'e ait, örn.
`reference-plane` hem `highspeed-routing` hem `emc-esd`). `guc-isil` ve
`konnektor` `tools: []` ile duruyordu, artık değil.

**Ad listesine eklemek tek başına yetmez:** ajan aracı `tools:`'tan *görür*,
ama **ne zaman** çağıracağını gövdeden öğrenir. Bu yüzden her skill'e
**"Araç kancaları"** bölümü eklendi — eklenen her araç için bir satır: hangi
anda çağrılır, çıktısı ne işe yarar, ve neyi *söylemez*
(örn. `topology-advise` "yön söyler, büyüklük asla";
`thermal-bridge` "elemek için, imzalamak için değil").

**Kalan 5 araç kasten bağlanmadı** — sahibi olmayan bir araca sahip
uydurmak, sayacı yeşile çeviren boş bir hamle olurdu:

| Araç | Neden skill'e ait değil |
|---|---|
| `exec` · `web` | harness yerleşiği, depo aracı değil |
| `ngspice` | harici ikili; depo tarafındaki sarmalayıcı `ngspice-bridge` ve **o** bağlı |
| `kicad-ipc` | MCP sunucusu; skill'ler KiCad'e `kicad-cli`/pcbnew akışıyla gidiyor |
| `board-checks-mcp` | MCP sunucusu; sardığı denetleyiciler ayrı ayrı anılı |

**Test:** `test_every_registered_tool_is_reachable_from_some_skill`
(Yön D) + `test_the_exemption_list_does_not_rot`. Muafiyet listesi kodda ve
her kaydın gerekçesi zorunlu; **anılan bir aracı muaf tutmak da hata** —
bayat mazeret, listenin ağacı tarif etmeyi bırakma yoludur.

*Testin ayırt edici olduğu kanıtlandı:* `rail-budget` `guc-isil`'den
geçici olarak çıkarıldı, test **düştü**; geri konunca geçti.

*Emsali:* Tur 8'de "18 skill'in 15'i **var olmayan** araç adlarına işaret
ediyordu" bulunmuştu (Yön C). O, yanlış yöne bakan işaretti; bu, **hiç
olmayan** işaret. Artık ikisi de aynı dosyadaki testlerin kapsamında.

### Fiziksel kapanış (Faz 5 — kart geldikten sonra)

| Araç | Ne yapar | Not |
|---|---|---|
| `tools/scripts/lab/physical_measurement.py` | Bir ölçüm kaydının, adlandırılmış bir kural için adlandırılmış bir birim üzerinde **kabul edilebilir kanıt** olup olmadığına karar verir; L3 manifesti + gate receipt üretir | Deponun **ilk** `PHYSICAL_MEASUREMENT` üreticisi. **Hiçbir cihaza bağlanmaz, hiçbir ölçüm almaz.** Eşik uydurmaz: limit `owner_skill`+`document_id`+`locator` ile gelir yoksa satır `NO_COVERAGE`; limitin otoritesi bu skill'i gösterirse **reddedilir** (`SELF_AUTHORED_LIMIT`). Değer `değer ± (belirsizlik + guard band)` **aralığı** olarak karşılaştırılır — sınıra değen sonuç `INCONCLUSIVE`, asla PASS |

Bu iki araç ([`first_article.py`](#) üstteki üretim tablosunda) **birleştirilmedi**:
as-built release ≠ elektriksel karakterizasyon; girdileri, kapıları ve karar
otoriteleri farklıdır. Faz 5A GREEN, 5B'nin **ön koşuludur** — yanlış monte
edilmiş bir karttan alınan ölçüm tasarımı değil montajı ölçer ve bunu söylemez.

### Proje-seviyesi sistem araçları (`system-planning/`)

Bunlar **kasten** `pcb-agent/tools/` dışındadır: sabit kamera topolojisi,
belirli bir adres çevirme şeması ve RK3588'e özgü mantık taşırlar
(`system-planning/README.md`). Generic registry'ye alınmadılar.

| Araç | Ne yapar |
|---|---|
| `system-planning/camera_hub_contract_check.py` | YAML sözleşmesini iki gerçek `.kicad_pcb` projesine karşı çapraz doğrular |
| `system-planning/vc_id_assignment.py` | N kamera kartı için VC-ID + deserializer I²C adres çeviri planı |
| `system-planning/device_tree_gen.py` | RK3588 DT overlay fragmenti; Ambarella kalıcı `NO_COVERAGE` (NDA — sözdizimi uydurulmaz) |
| `system-planning/system_contract_gate.py` | Yukarıdaki üçünü tek bir `eligible/scanned/excluded/violations` raporuna ve gate receipt'e **normalize eden** proje-seviyesi kapı. Overlay'i yalnız sistem verdict'i GREEN iken yazar |

### Sistem araçları

| Araç | Ön koşul |
|---|---|
| `kicad-cli` 10.0.5 | ERC/DRC kapılarında `--exit-code-violations`; sign-off'ta `--severity-all`; parity için `--schematic-parity` |
| `ngspice` 46 | Güç/analog analizi için; **asla** MIPI CSI-2 SI kanıtı olarak değil |
| `kicad-ipc` (MCP) | Yalnız açık GUI oturumu; IPC çağrılarını paralelleştirme |

---

## 3. Doğrulama durumu

```bash
python3 -m unittest discover -s tools/tests -t .
# 2026-08-08: 2829 test, OK (72 skip)
# 2026-08-10 (registry düzeltilmeden ÖNCE): 2935 test — 164 fail/error.
#   154'ü test_tool_registry: committed registry Windows `\` locator'ları
#   taşıyordu, Linux'ta hiçbiri çözülmüyordu (LOCATOR_DRIFT). Araç
#   değişikliklerinden SONRA `sync_tool_registry.py --write` koşulmalı.
# 2026-08-10 (issue-04 dalı, taban): 3162 test — 2 fail + 4 error, 125 skip.
#   Registry kırıkları GİTTİ (sync koşuldu). Kalan 6'sı ortama bağlı ve bu
#   dalın öncesine ait: pcbnew sistemde KURULU olduğu için "pcbnew yok"
#   varsayan üç test (test_diff_pair, test_mechanical_dxf,
#   test_ai_feedback_loop), field2d çözücü uyuşması + sınır düğümü,
#   test_common'ın Windows/POSIX ayırıcı testi.
# 2026-08-10 (issue-04 dalı, kapanış): 3412 test — AYNI 6 taban kırığı,
#   sıfır yeni. Eklenen 250 testin tamamı geçiyor.
# system-planning ayrı köktür: `cd system-planning && python3 -m unittest
#   discover -s tests -t .` -> 144 test, OK (115 taban + 29 yeni).
```

> **`sync_tool_registry.py --check` GREEN, paket GREEN demek DEĞİLDİR.**
> `--check` yalnız **kayıtlı** araçları ağaca karşı doğrular; diskte olup
> registry'de olmayanı göremez. Ters yönü `test_tool_registry.py`'deki
> `test_every_entrypoint_script_is_registered` kontrol eder. 2026-08-07'de
> bu fark depoyu bir gün kırmızı bıraktı: `assert_matrix.py` commit edildi,
> registry'ye kasten alınmadı, `--check` GREEN dedi ve tam paket koşulmadı.
> **Bir tur tam paket koşmadan kapatılmaz.**

Aşağıdaki tablo yalnız *hangi validator neyi kapsıyor* sorusunun cevabı olan
çekirdek satırları taşır.

### Sayaç drift'i — buradaki sayılar elle kopyalanmaz

Bu satırda daha önce "72 test dosyası" yazıyordu; 2026-08-10'da gerçek sayı
79'du (bugün **86**). Aynı kayma `KALDIGIMIZ-YER.md`'de registry için de var
(82 kayıtlı, gerçekte 83, bugün **89**). Üçüncüsü daha kötüydü: kök
`README.md` **16 skill** derken `skills/README.md` **18** diyordu — aynı
sayacın iki dosyada iki farklı değeri, ikisi de kimseye kırmızı vermeden.

**Anlık değer ölçülür, hatırlanmaz:**

```bash
ls pcb-agent/skills/*/SKILL.md   | wc -l    # generic skill sayısı  (2026-08-10: 21)
ls system-planning/skills/*/SKILL.md | wc -l # proje-seviyesi skill  (2026-08-10: 1)
ls pcb-agent/tools/tests/test_*.py | wc -l  # test dosyası          (2026-08-10: 86)
python3 pcb-agent/tools/scripts/sync_tool_registry.py --check  # registry (2026-08-10: 89)
```

Anlatı metnine yazılan sayı **yazıldığı gün doğrudur ve ertesi gün kaynak
değildir**. Bu, C8 vakasının (karar uygulandı, belge aylarca "açık" dedi)
küçük kardeşidir. Sayacı kapıya bağlayan yer:
[`../skills/pcb-orchestrator/SKILL.md`](../skills/pcb-orchestrator/SKILL.md)
§3 "Release-health".

> **`pcb-agent/skills.md` diye bir depo artifact'i YOKTUR.** 2026-08-10
> anlık görüntüsünde 0 byte'lık böyle bir dosya görüldü; ölçüldüğünde
> `git ls-files` onu **takip etmiyordu** — çalışma ağacında kalmış izlenmeyen
> bir artıktı, bayat bir depo dosyası değil. Kanonik skill dizini
> [`../skills/README.md`](../skills/README.md)'dir. Boş dosyayı "düzeltmek"
> için içerik yazmak, olmayan bir ikinci indeks yaratıp aynı drift sınıfını
> baştan kurmak olurdu.

> **ÜRETİLMİŞ İKİ DOSYA BU MAKİNEDE TAZELENEMEDİ (2026-08-10).** İkisi de
> elle düzenlenmez; ikisi de doğru araçla, doğru ortamda yeniden üretilir:
>
> - `config/tool_registry.json` — committed hâli Windows `\` locator'ları
>   taşıyor, Linux'ta hiçbiri çözülmüyor. `test_tool_registry.py`'nin
>   **154 kırığının tamamı** budur ve bu turdan öncesine aittir. Araçların
>   sha256'ları da bu turda değişti. Ağaç durulunca
>   `python tools/scripts/sync_tool_registry.py --write` koşulmalı.
> - `docs/YETENEK_ENVANTERI.csv` / `.xlsx` — "Test Durumu" sütunu
>   `pytest --junit-xml` ile ÖLÇÜLÜR. Bu makinede `pytest` kurulu değil,
>   dolayısıyla `--skip-tests` dışında üretilemiyor; öyle üretilince 84
>   modülün ölçülmüş `gecti` değeri `olculemedi`'ye düşüyordu. **Bilerek
>   yeniden üretilmedi:** ölçülmüş veriyi ölçülmemişle değiştirmek bu turun
>   kapattığı arıza sınıfının ta kendisidir. `pytest`'in bulunduğu ortamda
>   `capability_inventory.py . --out docs/YETENEK_ENVANTERI.xlsx` koşulmalı
>   (o koşu locator'ları da POSIX'e çevirecektir).

| Validator | Kapsadığı |
|---|---|
| `tools/tests/test_assertions.py` | assertion dili + matris ayrıştırıcı + `assert_matrix` CLI (**137 test**): altı yükleme-zamanı reddi, 0.15 mm float sınırı, `PATH_ABSENT`→NO_COVERAGE, bayat kanıt, `--prove` vacuous yakalama, `ARTIFACT_MISSING`→FAIL, **ve sevkiyattaki setin her seçicisinin üretici aracın gerçekten yazdığı bir yolu adlandırması** (rapor şekli `drc_gate.py`'nin kendisinden üretilir, kopyalanmaz) |
| `tools/tests/test_field2d.py` | 2-D alan çözücü: iki bağımsız çözücünün uyuşması, koaksiyele karşı analitik doğrulama, Richardson dış-değerlemesi |
| `tools/tests/test_monte_carlo.py` | tolerans: her ngspice denemesi elle hesaplanan bölücüyle, dağılım genişliği analitik varyans yayılımıyla |
| `tools/tests/test_electrothermal.py` | bağlaşık çözüm ↔ kapalı-form kazanç uyuşması, `G ≥ 1`'de reddetme |
| `tools/tests/test_benchmark.py` | kıyaslama harness'ı: sonuç / determinizm / kapsam üç ekseni ayrı |
| `tools/tests/test_priors.py` | Wilson alt sınırı sıralaması, soğuk başlangıç reddi, leave-one-out kalibrasyon |
| `tools/tests/test_vendor_models.py` | satıcı model kayıt defteri (75 test): beyansız zarf/otorite/provenance reddi, zarf dışı sorgunun sayı döndürmemesi, dosyasını aşan beyanın FAIL olması, bayat digest'in kaydı iptal etmesi |
| `tools/tests/test_topology.py` | topoloji önerisi (72 test): birim içeren katalog girişinin reddi, elenemeyen dönüşümün reddi, elemelerin gerçekten ateşlemesi, "zaten sahipli" eksikliğin susarak geçilmemesi |
| `tools/tests/test_boot_observability.py` | boot gözlemlenebilirliği (50 test): **her rayı kapsayan planın hâlâ yakalanması**, PG'nin ray sayılmaması, tek ölçümün katmanı elemeyle çözmesi, reset'in ayrı arıza türü olması, enable döngüsünün reddi |
| `tools/tests/test_board_checks.py` | dfm_emc_check, mask_dam, stitch_density, via_stub |
| `tools/tests/test_crosstalk_thinning.py` | crosstalk motoru (sözleşme + fault-injection) |
| `tools/tests/test_crosstalk_mcp.py` | MCP protokol + sözleşme |
| `tools/tests/test_zdiff_solver.py` | monotonluk + katsayı fault-injection |
| `tools/tests/test_gate_receipt.py` | receipt sözleşmesi |
| `tools/tests/test_context_pack.py` | preflight/postflight fault suite |
| `tools/tests/test_research_promotion_gate.py` | terfi kapısı |
| `tools/tests/test_sch_wire.py` | 12 rotasyon×mirror kombinasyonu, netlist üzerinden |
| `tools/tests/test_tool_registry.py` | registry ↔ ağaç tutarlılığı |
| `tools/tests/test_grid_astar.py` | ızgara A*: bütçe, iki fazlı çarpışma, CLI sözleşmesi, NO_COVERAGE → exit 1 |
| `tools/tests/test_topological.py` | merdiven, 45° pah, shove'un kaskad yapmaması, beyan edilmemiş alt katmanın boş sayılmaması, boş request → exit 1 |
| `tools/tests/test_production.py` | FreeRouting **köprü mantığı** (JVM bayrakları, exception'da anında kill, değişmeyen track sayısı ≠ routed, jar yokken NO_COVERAGE) — gerçek java/jar koşturulmaz |
| `tools/tests/test_common.py`, `test_contract_fixtures.py` | ortak katman + sözleşme fixture'ları |

### ✅ C3 çelişkisi çözüldü

Konsolidasyon öncesi `dfm_emc_check.py` **iki ayrı SHA-256'lı kopya** hâlinde
vardı ve iki envanter çelişiyordu:

| Eski kaynak | İddia |
|---|---|
| `SKILL-dogrulama-matrisi.md:190-206` | "bu turda yazıldı ve **kanıtlandı**", 3 enjekte kusur yakalandı |
| `SKILL-pcb-tool-v2-kopruleri.md:71,113` | validator **`NO_COVERAGE`** — eşleşen test yok |

**Çözüm:** yeni ağaçta tek kanonik kopya (`tools/scripts/board/dfm_emc_check.py`,
registry'de hash'li) ve gerçek bir validator (`tools/tests/test_board_checks.py`)
vardır. Araç `NO_COVERAGE` **değildir**. Aynı durum `via_stub.py` için de
geçerlidir.

---

## 4. Bilinen sınırlar

| Araç | Sınır |
|---|---|
| `zdiff_solver.py` | Kapalı-form hatası **sistematik**: yayımlanmış Polar karşılaştırmasında −%29.94…+%2.41. "Hedefe yakın çıktı" doğruluk kanıtı değildir. Kontrollü empedansta 2D field solver **zorunlu kapı**. Self-test'teki `measured_ohm = 184.0` referansının provenance'ı **AÇIK MADDE**. |
| `crosstalk_thinning.py` | FEXT modellenmez (mikroşeritte daima `CONFIRM`); iki-iz modeli — N-agresör toplamı hesaplanmaz; `signoff: false` |
| `dfm_emc_check.epad_paste_coverage` | Tek-pad + margin modeli; ayrı çizilmiş çok açıklıklı (windowpane) deseni **tanımaz** → `A(kısmi)` |
| `dfm_emc_check.edge_keepout_ceramics` | Mesafeyi ölçer, **yönü ölçmez**; V-score hattını bilmez (panel verisi board'da yok) → `A(kısmi)` |
| `dfm_emc_check.return_via_near_hs_via` | Net **adı** desenine dayanır; isimlendirme değişirse kapsam sessizce düşer → `scanned`'ı her koşuda oku |
| `stitch_density.py` | `er_eff` kullanıcı girdisidir, stackup'tan türetilmez. `NO_COVERAGE` **çıkış koduna yansımaz** → `summary.NO_COVERAGE` alanına bak |
| `mask_dam.py` | Yalnız düz `PCB_TRACK`; ark ve zone fill kapsam dışı |
| `via_stub.py` | Ön-eleme; sign-off 3B EM. **Bu repodaki kartlarda kapsam 0** (katman çıkarımı dar + kartta stackup yok) |
| Board checker'ları | `pcbnew` (KiCad 10 SWIG) gerektirir; 10.0.x'e pinlenmeli |
| `route/grid_astar.py` | Izgara sürekli geometriyi `cell_mm`'e yuvarlar: `cell_mm`'den dar koridor **kaçırılır** (`cell_mm < (W + clearance)/2` zorunlu). Tek bütçe `max_nodes`; **zaman/bbox bütçesi yok**. Diff-pair/net-class yok |
| `route/topological.py` | "Push & shove" **tek adımlıktır**; KiCad'in PNS router'ı ne SWIG'e ne IPC'ye açıktır. Kaskad itme yok — çarpışma varsa öneri geri çekilir. `lower_layer_obstacles` verilmezse alt katman **boş varsayılmaz** (via önerilmez) |
| `production/freerouting.py` | **CANLI DOĞRULANDI (2026-08-06)** — gerçek java 25 + gerçek v2.2.4 jar (GitHub'dan indirildi, 57,766,203 byte) + gerçek pcbnew 10.0.4 DSN export'u ile uçtan uca koşuldu. İndirme yolu (`download_jar`/`find_jar_download_url`) gerçek GitHub API'sine karşı çalıştı; checksum pinlenmez (GitHub release asset'i için yayımlamıyor). Eksik jar hâlâ **varsayılan fail-closed** — `--allow-jar-download` açıkça verilmeden ağa çıkılmaz. **Canlı testte bulunan P0 düzeltildi:** timeout hiç uygulanmıyordu (bloklayan `readline()`; 240s bütçe gerçekte 1682s koştu) — okuma ayrı thread + queue'ya alındı, regresyon testi `test_production.FreeroutingBridge.test_regression_a_silent_child_is_still_killed_at_the_deadline`. Bilinen kozmetik açık: v2.2.4 headless çalışsa da "set gui.enabled to false" uyarısı basıyor; `gui.enabled` bir `-D` özelliği veya CLI seçeneği DEĞİL (`-help`'te yok, `-Dgui.enabled=false` denendi, susturmadı) → bayrak **bilerek gönderilmiyor**. `ImportSpecctraSES` `True` dönüp sıfır iz ekleyebilir → `tracks_changed` ölçülmeden "routed" denmez. DRC koşmaz: routing sonucu verdict değildir |

### C8 KAPANDI — üçü de vendorlandı (2026-08-08'de doğrulandı)

**Bu bölüm 2026-08-08'e kadar yanlıştı ve yanlışlığı ucuz değildi.** Metin
`drc.sh` / `measure.py` / `coupling.py` için *"bu ağaçta yoktur, registry'de
kaydı yoktur, vendorlanana kadar `SCREEN_ONLY` ve tek başına Faz 3→4 kapısını
açmaz"* diyordu. C8 kararının **(a) şıkkı uygulanmış** — üçü de vendorlandı,
registry'ye girdi, hash bağlandı ve teste bağlandı — ama karar
*"açık"* olarak kaldı. Sonuç: okuyan herkes Faz 3→4 kapısının kalıcı olarak
açılamaz olduğunu sanıyordu, oysa engel kalkmıştı.

| Matris satırı | Eski dış araç | Bugünkü karşılığı | Kapsam |
|---|---|---|---|
| DRC 0 ihlal | `drc.sh` | `tools/scripts/drc_gate.py` → `metrics.violation_count` · `test_drc_gate.py` | **tam** — hash-bağlanabilir · ölçüm **belirlenimli** (5 koşu bit-bit aynı, D7 kapandı) · sayı **araç sürümüne bağlanır**: iki rapor ancak aynı ölçülmüş `kicad-cli` sürümü ve aynı bayraklarla üretilmişse karşılaştırılır (D8) |
| 90° köşe sayısı = 0 | `measure.py` açı taraması | `board/measure_board.py` → `measurements.right_angle_corners` · `test_vendored_board_tools.py` | **tam** — yalnız `--net` ile verilen netler |
| Unrouted 0 / short 0 | `measure.py` | `independent_verify.py` + `production/contracts.py`; sayı `drc_gate.py` → `metrics.unconnected_count` | **tam** — ama `measure_board.py` değil, **başka** araç |
| Intra/inter-pair skew (**ps**) | `measure.py` | `board/measure_board.py` → `skew_mm` | ⚠️ **KISMİ** |
| `parallelism_coverage` | `coupling.py` | `board/pair_coupling.py` → `coupled_fraction` / `coverage` | ⚠️ **KISMİ** |

> **2026-08-09:** yukarıdaki üç "tam" satır, o güne kadar **koşulmamıştı.**
> `fixtures/assertions/matrix_core.json` bu araçların hiçbirinin yazmadığı
> `summary.*` yollarına bakıyordu; gerçek bir raporla ilk temasta üçü de
> NO_COVERAGE döndü. Seçiciler artık araçların bastığı yollara bağlı ve
> canlı `cam-w-og05b10` kartı üzerinde koşuldu. **"Tam kapsam" ile "bağlı ve
> koşulmuş" ayrı iki şey**; bu tablo birincisini söyler. Ayrıntı ve bağlarken
> çıkan üç kusur: [KALDIGIMIZ-YER.md](KALDIGIMIZ-YER.md) Tur 16.

**Kalan iki gerçek kusur — karar gerekiyor, kendiliğinden kapanmaz:**

1. **Birim uyuşmazlığı.** Matris skew'i **ps** cinsinden istiyor;
   `measure_board.py` **mm** raporluyor (`skew_mm`, `--max-skew-mm`).
   Dönüşüm `er_eff` ister ve ps bütçesi başka yerde duruyor
   (`common/escape_geom.py`, `length_report.schema.json`). Etiketli bir eşiğin
   etiketsiz bir sayıyla karşılaştırılması — `assertions.py`'nin
   `UNITS_UNBOUND` diye reddettiği hata sınıfının ta kendisi.
2. **Hiçbir aracın üretmediği metrik adı.** Matris `parallelism_coverage`
   diyor; `pair_coupling.py` `coupled_fraction` ve `coverage` yayıyor. Kural,
   kimsenin basmadığı bir alana nişan almış durumda — `assert_matrix.py`'nin
   `PATH_ABSENT` → NO_COVERAGE dediği durum.

**Bu iki satır neden hâlâ ayakta:** ikisi de matrisin 42 `UNASSERTED_RULE`
satırından. Assertion yazılmış olsaydı `assert_matrix.py` ikisini de ilk
koşuda yakalardı. Boşluğu bulan şey aracın kendisi değil, aracın **eksikliği**
oldu.

*Matris satırlarının araç sütunu bu turda DEĞİŞTİRİLMEDİ:* iki satırın kapsamı
kısmi ve kısmi bir aracı tam diye göstermek, bu bölümün az önce düzeltilen
hatasının ters yönüdür. Karar
[KALDIGIMIZ-YER.md](KALDIGIMIZ-YER.md) **D6**'da.

### 2026-08-10 — yukarıdaki iki kusurun durumu

**1. Birim uyuşmazlığı — kapandı, mekanizma olarak.** `skew_mm` alanına `ps`
etiketi yapıştırılmadı; bu `UNITS_UNBOUND` guard'ının amacını bozardı.
Bunun yerine `board/skew_timing.py` eklendi: ham mm raporunu + **kaynaklı**
`er_eff` (ya `--er-eff` ile beyan, ya `--solve` ile deklare edilmiş
kesitten `common.field2d` 2B çözümü) tüketip gerçek bir `units: "ps"` alanı
taşıyan türetilmiş metrik üretir. `er_eff` yoksa dönüşüm **çalışmaz** —
`coupon_plan.py`'nin tahmin etmeyi reddedişinin aynısı. `escape_geom.py`
içindeki tipik FR-4 sabiti sign-off yolunda erişilebilir değildir.
Yan kazanç: bu araç `length-report/v1` şemasının **ilk gerçek üreticisi**;
şema aylardır sıfır üreticiyle duruyordu.

**2. Metrik adı — bağlandı.** `parallelism_coverage` gerçekten hiçbir aracın
basmadığı bir addı (depoda 26 geçiş, hepsi doküman/şema, sıfır üretici), ama
**ölçüm eksik değildi — bağ yanlıştı.** `pair_coupling.py` metriği zaten
hesaplıyor. `matrix_core.json`'daki assertion artık pozisyonel `pairs[i]`
yerine toplam `coverage` bloğuna bağlanıyor (pozisyonel indeks çift listesi
değişince sessizce başka bir çifti gösterir) ve `coverage.scanned`'ı ayrıca
kapsam paydası olarak bağlıyor.

Bağlarken çıkan üçüncü kusur: `pair_coupling.py` kendi şeması `design` ve
`metrics`'i **zorunlu** saymasına rağmen ikisini de yazmıyordu — yani her
raporu hash'e bağlanamazdı ve üzerine yazılacak her assertion sonsuza kadar
`SCREEN_ONLY` kalırdı. Hiçbir tüketici şemaya karşı doğrulamadığı için
fark edilmemişti.

**Her ikisinin de `authority`si bugün `screen_only`,** çünkü karşılaştırılan
eşikler (spacing toleransı, coupled hedef, örnekleme adımı, NEXT bütçesi)
hâlâ kod içi CLI varsayılanları — kaynaklı proje profili gelmedi. Profil
geldiğinde `signoff`a geçiş tek alanlık bir değişiklik ve burada kayıtlı.

---

## 5. Terfi kuralı

Bir aracın kodunda görülen sayı, **otomatik olarak** bir mühendislik eşiği
değildir. Normatif eşik olabilmesi için:

1. birincil kaynak (standart sürümü + locator + erişim tarihi),
2. proje profiline bağ (`fab_capabilities.json` vb.),
3. hash-bağlı gate receipt

gerekir. Üçü yoksa değer `LOCAL_POLICY` / `CONFIRM` kalır.
