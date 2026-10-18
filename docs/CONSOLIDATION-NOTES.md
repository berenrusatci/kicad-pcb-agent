# Konsolidasyon notları

Kaynak: `ProjectE-1/Otonom-PCB-Ajani/Skills/` (17 × `SKILL-*.md`,
`MOCs/MOC-Skills.md`, `Config/tool_registry.json`) — **değiştirilmedi**.
Hedef: `ProjectE-2/pcb-agent/`.

Tarih: 2026-08-05

---

## 1. Sonuç

| | Önce | Sonra |
|---|---|---|
| Skill dosyası | 17 | **16** |
| Skill olmayan ama skill klasöründe duran kayıt | 1 | 0 (→ `docs/`) |
| Coverage-gate paragrafının kopyası | 16 | **1** (`_shared/coverage-gate.md`) |
| Artifact şeması | 4 | **17** |
| `- [ ]` kabul kriteri taşıyan skill | 6 / 17 | **16 / 16** |
| Frontmatter'da `name` + `description` | 16 / 17 | **16 / 16** |
| Çalıştırılabilir doğrulama bölümü | 4 / 17 | **16 / 16** |

Doğrulama: `python3 -m unittest discover -s tools/tests -t .` → **217 test, OK**.

---

## 2. Birleştirmeler, bölünmeler, silmeler

### M1 — `highspeed-length-match` + `pcb-highspeed-escape` → `highspeed-routing`

**Gerekçe:** ikisi tek bir fiziksel etkinliği keyfî bir sınırdan bölüyordu
("ilk 1–3 mm" vs "gövde") ve sonuç olarak skew formülünü (167.6 mm/ns + aynı
1.47 mm → 8.8 ps örneği), 90° köşe kuralını, teardrop ödününü ve kuplaj
metriğini **ikisinde birden** yazıyordu. Kuplaj hedefi de tam bu sınırda
çelişiyordu (→ C2).

Escape geometrisi SKILL.md §2'de kaldı (yük taşıyan içerik); ölçüm şemaları
`references/`e taşındı.

### M2 — `pcb-tool-v2-kopruleri` + `dogrulama-matrisi §Araç envanteri` → `docs/tool-inventory.md`

**Gerekçe:** ikisi de *skill* değildi — karar prosedürü, tetikleyici ve kabul
kriteri yoktu; ikisi de hash-bağlı **kayıt**tı. Üstelik aynı dosya adları için
**çelişen** doğrulama verdict'leri veriyorlardı (→ C3).
`pcb-tool-v2-kopruleri` aynı zamanda `name`+`description` frontmatter'ı olmayan
tek dosyaydı; taşınması bu istisnayı ortadan kaldırdı.

Kanonik makine-okur kayıt `config/tool_registry.json`'dır ve artık
`tools/scripts/sync_tool_registry.py` tarafından **üretilir** (elle bakılmaz).

### S1 — `SKILL-orchestrator.md:234-330` → `claim-audit`

**Gerekçe:** dosyada ikinci bir `# H1` vardı (`MOD: İddia denetimi`) ve ~100
satırlık tamamen farklı bir iş akışı taşıyordu: mevcut bir karta yöneltilmiş dış
iddiaları denetleme. Tetikleyicisi bir faz sınırı değil, bir dış iddiadır; DAG
akışında hiç koşmaz. Orkestratörün kendi §15 kuralı da "**Tüm kuralları
yükleme**" diyordu — bölünme onu küçülttü.

### Silinen tekrarlar (D1–D18)

| # | İçerik | Kanonik sahip | Ne yapıldı |
|---|---|---|---|
| D1 | Coverage kapısı paragrafı (16 kopya) | `_shared/coverage-gate.md` | Gövdelerden silindi; frontmatter'da `assurance_loop` + `evidence_origin` alanına indirgendi |
| D2 | `100 nF` GND→PWR SCREEN_ONLY (3 kopya) | empedans-stackup §Referans düzlemi | Diğer ikisi wikilink |
| D3 | "Fab profili yoksa LOCAL_POLICY" (2 kopya) | Her iki skill'de bağlam farklı → **ikisi de kaldı**, metin kısaltıldı ve şema linki tekilleştirildi |
| D4 | Skew ps formülü + 1.47 mm/8.8 ps örneği (2 kopya) | highspeed-routing §3.3 | M1 ile tekilleşti |
| D5 | `IsTeardropArea()` sayım idiomu (2 kopya) | highspeed-routing §5c | dfm §3 wikilink |
| D6 | `ZONE.GetLayer()` çok-katman tuzağı (2 kopya) | dogrulama-matrisi/references/measurement-error-classes.md §6 | highspeed-routing §6 wikilink |
| D7 | Teardrop yüksek hızda ödün (4 kopya) | dfm §3 (karar) + highspeed-routing §5c (API sınırı) | İkisi farklı konu; diğer ikisi wikilink |
| D8 | `--schematic-parity` uyarısı (5 kopya) | sematik-erc §4 | Diğerleri wikilink / matris satırı |
| D9 | Fault-injection "1.27 mm kaydır" (2 kopya) | sematik-erc §8 | claim-audit §7 wikilink |
| D10 | IPC-4761 Type VII (4 kopya) | dfm §4a | emc-esd'deki **çatışma çözümü** ayrı içerik → `_shared/rule-conflicts.md` §1 |
| D11 | MLCC flex crack (4 kopya) | dfm §4f | cpl/konnektor/bom wikilink |
| D12 | DNP BOM∧CPL parity (5 kopya) | cpl-assembly §2.3 | Diğerleri wikilink; `cpl-columns.md`'de DNP grup tablosu |
| D13 | MSL / floor life (3 kopya) | bom-lifecycle §3a | cpl wikilink |
| D14 | Konnektör 3D keepout üçlüsü (2 kopya) | mekanik-dxf (şema sahibi) | konnektor §3c wikilink + üç `kind` kaydı |
| D15 | 90° köşe yasağı (5 kopya) | highspeed-routing §3.5 | M1 ile tekilleşti; diğerleri kapı satırı |
| D16 | `≤1.5 mm` decoupling SCREEN_ONLY (3 kopya) | `_shared/rule-conflicts.md` §2 | sematik-erc'nin **kapsam istisnası** argümanı korundu (benzersizdi) |
| D17 | MLCC DC-bias (5 kopya) | guc-isil §3b | Zaten cross-ref'ti; korundu |
| D18 | Maske barajı formülü (3 kopya) | highspeed-routing §2a | dfm §2.5 wikilink |

> **Hiçbir benzersiz cümle düşürülmedi.** Tekrarın fazladan bilgi taşıdığı her
> yerde (D10'daki ESD çatışması, D16'daki ESD dizisi kapsam istisnası,
> D3'teki iki farklı fallback bağlamı, D7'deki API sınırı) o içerik önce
> kanonik sahibe **katlandı**, sonra kopya silindi.

---

## 3. Çakışma çözümleri (C1–C8)

### ⚠️ C1 — `f_knee` iki farklı büyüklüğü adlandırıyordu → **ÇÖZÜLDÜ**

| Eski | Yeni ad | Tanım | Neyi belirler |
|---|---|---|---|
| `f_knee` (emc-esd:163) | **`f_knee_edge`** | `0.35 / t_r` | Işıma, dikiş aralığı, guard via λ/20, filtre bandı |
| `f_knee` (dogrulama:221) | **`f_nyquist`** | `veri_hızı / 2` (NRZ) | Via stub rezonans marjı, kanal BW, S-parametre |

**Gerekçe:** ikisi de doğru fizik ama **farklı büyüklük**, ve ikisi de aynı CLI
bayrağını (`--f-knee-ghz`) besliyordu. 2.5 Gb/s / 150 ps kenarlı bir linkte
`f_knee_edge` = 2.33 GHz, `f_nyquist` = 1.25 GHz → λ/20 dikiş aralığı **~1.9×**
değişir.

**Uygulama:** ayrım `_shared/glossary.md` §1'de tanımlandı ve emc-esd §4/§5,
highspeed-routing §5a, crosstalk-thinning §3, `phy_contract.schema.json`,
`return_via_rules.schema.json` ve dogrulama-matrisi matrisinde **her geçtiği
yerde** kullanıldı. `return_via_rules.schema.json` içindeki
`frequency_basis` alanı `f_knee_edge` dışındaki bir temeli `INVALID_METRIC`
sayar. Araç bayrağı hâlâ `--f-knee-ghz` olduğu için çağıran skill besleneni
`metric_semantics`'e yazmak zorunda.

### ⚠️ C2 — `parallelism_coverage ≥ %95` hedefi → **KALDIRILDI** (güvenli okuma)

`highspeed-length-match:60` sabit `≥ %95` hedefi koyuyordu; `pcb-highspeed-escape:141-152`
ise **aynı kartın** her belgelenmiş toleransta bu hedefin altında kaldığını
ölçmüştü (tol 0.70 mm → %64.1; tol 1.00 mm → %88.6) ve yüzdenin toleranssız
anlamsız olduğunu yazıyordu.

**Karar:** sabit hedef kaldırıldı. Metrik artık bir kapı değil, **tolerans +
örnekleme adımıyla raporlanan bir ön-elektir** ve hiçbir zaman `impedance_pass`
üretmez (en fazla `GEOMETRY_PRECHECK_PASS`). Ad da `parallelism_coverage`
olarak sabitlendi (elektromanyetik kuplaj değildir).
**→ Open decisions #1.**

### ✅ C3 — `dfm_emc_check.py`: "kanıtlandı" vs `NO_COVERAGE` → **ÇÖZÜLDÜ**

Eskiden aynı ada sahip **iki farklı SHA-256'lı** kopya vardı
(`Skills/scripts/` = `73680fdd…`, pcb-tool-v2 registry = `e888e059…`) ve iki
envanter çelişiyordu.

**Çözüm:** yeni ağaçta tek kanonik kopya (`tools/scripts/board/dfm_emc_check.py`,
registry'de hash'li) ve gerçek validator (`tools/tests/test_board_checks.py`)
vardır → araç `NO_COVERAGE` **değildir**. Kalan kısıtlar araç kapsamı
boşluklarıdır (`epad_paste_coverage` windowpane, `edge_keepout_ceramics` yön) ve
bu satırlar `A(kısmi)` olarak işaretlendi. Aynı çözüm `via_stub.py` için de
geçerli. Ayrıntı: `docs/tool-inventory.md` §3.

### ⚠️ C4 — `testpoint_map.json` vs `testpoint_plan.json` → **`testpoint_plan.json`**

Aynı artifact iki adla dolaşıyordu ve tüketiciler birbirini bulamıyordu
(`dft-testpoints:15,75` + `dogrulama:77` = `_map`; `dft-testpoints:28,41` +
`orchestrator:76` = `_plan`).

**Karar:** kanonik ad **`testpoint_plan.json`** (üreticinin yazdığı ad; DFT
skill'inin frontmatter'ı ve orkestratör tablosu bunu kullanıyordu).
`testpoint_map.json` **kullanımdan kaldırıldı**; hiçbir üretici veya tüketici
kullanmıyor. Geç faz kapsama makbuzu ayrı bir artifact olarak korundu:
`dft_coverage_receipt.json`. `_shared/glossary.md` §7'de kayıtlı.

### ⚠️ C5 — Maske barajı `0.20–0.25` vs `0.20` → **karar bandı**

Aynı dosyada iki değer vardı (`dfm:61-62` = 0.20–0.25; `dfm:175-176` = 0.20) ve
çalışılmış örnek (`0.15 → 0.21 mm`) yalnız 0.20 okumasıyla geçiyordu.

**Karar** (`_shared/rule-conflicts.md` §5):

| Baraj | Verdict |
|---|---|
| `< 0.20 mm` | **RED** |
| `0.20 ≤ baraj < 0.25` | **CONFIRM** (fab profili olmadan PASS değil) |
| `≥ 0.25 mm` | `SCREEN_ONLY` PASS |

Fab profili (`soldermask.min_bridge_mm`) yerel bandı **ezer**. Bu, ölçülmüş
0.21 mm örneğini "geçti" saymak yerine dürüstçe `CONFIRM` bandına koyar.

### ⚠️ C6 — Aynı kural, farklı `assurance_loop` → **L1+L3**

`100 nF` SCREEN_ONLY kuralı üç dosyada iki farklı etiketle yazılıydı
(`empedans:59` ve `emc-esd:119` = L1+L3; `highspeed:76` = L0+L3).

**Karar:** **L1+L3**. Bu bir topoloji/stackup kararıdır (kapı döngüsünde kapanır
ve fiziksel ölçümle doğrulanır), board yazımı sonrası otomatik koşan bir L0
metriği değildir. Tek kaynak: `_shared/rule-conflicts.md` §6.

### ✅ C7 — Stale `aiplanlama/` path prefix'i → **ÇÖZÜLDÜ**

Eski `Config/tool_registry.json`'daki **her** `locator` ve
`decompose-pcb-decisions:54,65` var olmayan bir `aiplanlama/…` kökünü işaret
ediyordu (hash'ler doğruydu, yollar yanlıştı) — yani her "tool available"
preflight'ı açılamayan bir dosyayı doğruluyordu.

**Çözüm:** tüm locator'lar `pcb-agent/` köküne göre yeniden yazıldı
(`tools/scripts/…`) ve hash'ler yeniden üretildi. Registry artık
`tools/scripts/sync_tool_registry.py` ile **üretiliyor**, elle bakılmıyor.
Skill'lerdeki her komut satırı da yeni yollara güncellendi.

### ⚠️ C8 — Hash bağı olmayan dış araçlar → **`SCREEN_ONLY`**

`drc.sh` (`tools/krt/`), `measure.py` (`usb-hs-breakout/evidence/`) ve
`coupling.py` (`~/krt12_out/`) bu ağaçta yok ve registry'de kaydı yok — ama L0
iç döngüsünün ve Faz 3→4 kapısının çekirdek araçları olarak matriste duruyorlar.

**Karar:** vendorlanana kadar ürettikleri sonuç `SCREEN_ONLY`'dir ve tek başına
Faz 3→4 kapısını **açmaz**. Matris satırları ⚠️ ile işaretlendi.
`kicad-cli pcb drc` (B seviyesi) hash-bağlıdır ve kapı için kullanılabilir;
eksik olan **repo rule set sarmalayıcısı** ile uzunluk/açı/paralellik
ölçümleridir. **→ Open decisions #2.**

---

## 4. Yeni yazılan şemalar

Eskiden 4 şema vardı (`rail_tree`, `fab_capabilities`, `esd_test_profile`,
`metric_coverage`) ve ~25 artifact adı şemasızdı. Eklenen 13:

| Şema | Sahip skill |
|---|---|
| `thermal_report.schema.json` | guc-isil |
| `keepout_zones.schema.json` | mekanik-dxf |
| `lifecycle_report.schema.json` | bom-lifecycle |
| `connector_spec.schema.json` | konnektor |
| `stackup.schema.json` | empedans-stackup |
| `interface_contract.schema.json` | sematik-erc |
| `phy_contract.schema.json` | highspeed-routing |
| `length_report.schema.json` | highspeed-routing |
| `crosstalk_scan.schema.json` | crosstalk-thinning |
| `esd_chain.schema.json` | emc-esd |
| `return_via_rules.schema.json` | emc-esd |
| `testpoint_plan.schema.json` | dft-testpoints |
| `rotation_map.schema.json` | cpl-assembly |
| `decision_units.schema.json` | decompose-pcb-decisions |
| `gate_receipt.schema.json` | pcb-orchestrator |

Şemalar **stub değildir**: her biri `$comment` alanlarında kuralın *neden* öyle
olduğunu taşır (ör. `land_pattern: different` → RED + DFM feedback;
`budget_origin: estimate_ui_fraction` → sign-off yok; `signoff: const false`).

---

## 5. Open decisions — kullanıcı kararı gerekiyor

### #1 — `parallelism_coverage` için proje hedefi var mı? (C2)

Sabit `≥ %95` kapısı kaldırıldı çünkü referans kart onu hiçbir toleransta
sağlamıyordu ve metrik toleranssız anlamsızdı.

**Karar gerekiyor:** projeye özgü bir paralellik hedefi **isteniyor mu**, ve
isteniyorsa hangi toleransta tanımlanacak? Seçenekler:

- **(a)** Hedef yok — metrik yalnız raporlanır (şu anki güvenli durum).
- **(b)** Net-class başına hedef, `impedance_rules.json` içinde
  `tolerance_mm` ile birlikte tanımlanır.
- **(c)** Tek global hedef + sabit tolerans (önerilmez: eski çelişkiyi geri getirir).

İşaretlendiği yer: `skills/highspeed-routing/SKILL.md` §4 `⚠️ KARAR GEREKLİ`.

### #2 — `drc.sh` / `measure.py` / `coupling.py` vendorlanacak mı? (C8)

> ## ✅ KAPANDI — **(a) şıkkı uygulandı**, 2026-08-08'de doğrulandı
>
> Üçü de vendorlandı, registry'ye girdi, hash bağlandı, teste bağlandı:
> `drc.sh` → `tools/scripts/drc_gate.py` ·
> `measure.py` → `tools/scripts/board/measure_board.py` ·
> `coupling.py` → `tools/scripts/board/pair_coupling.py`.
>
> **Karar uygulandı ama kapatılmadı** ve bedeli vardı: `tool-inventory.md` §4
> ile `dogrulama-matrisi/SKILL.md` §5, engel kalktıktan sonra da aylarca
> *"vendorlanana kadar `SCREEN_ONLY`, Faz 3→4 kapısını açmaz"* dedi. Bir
> kararın **uygulanması** onu kapatmıyor; **işlenmesi** kapatıyor.
>
> Ayrıntılı eşleme tablosu ve kalan iki kısmi kapsam:
> [tool-inventory.md §C8](tool-inventory.md). Kalan karar: **D6**.

Bu üçü L0 iç döngüsünün çekirdeğinde ama ağaçta yok ve hash bağı yok.

**Karar gerekiyor:**

- **(a)** `tools/scripts/board/` altına vendorla + test yaz → A/B seviyesine
  yükselir, Faz 3→4 kapısını açabilir. (Önerilen.)
- **(b)** Dış araç olarak kalsın → matris satırları kalıcı `SCREEN_ONLY`,
  Faz 3→4 kapısı `kicad-cli` + mevcut board checker'larıyla yeniden tanımlanmalı.
- **(c)** İşlevlerini mevcut araçlara devret (uzunluk/açı ölçümü
  `tools/scripts/board/` altında yeni bir checker olarak yazılır).

İşaretlendiği yer: `skills/dogrulama-matrisi/SKILL.md` §5,
`docs/tool-inventory.md` §4.

### #3 — `zdiff_solver.py` self-test referansının provenance'ı

Self-test'teki `measured_ohm = 184.0` referansının **kaynağı yok** (hangi kart /
kupon / ölçüm / tarih). ±%10 testi geçmek üretim doğruluğunu kanıtlamaz.

**Karar gerekiyor:** ya provenance eklenir, ya yayımlanmış referans vektörleri +
beklenen hata payıyla değiştirilir. Bu, eski `Skills/scripts/README.md`'de zaten
`AÇIK MADDE` olarak işaretliydi ve konsolidasyonda çözülmedi (araç tarafı).

### #4 — `tools/README.md` eski adlara atıf yapıyor

Araç ağacındaki `tools/README.md` hâlâ `Skills/scripts` başlığını, eski
`cd Skills/scripts` komutlarını ve `[[SKILL-pcb-highspeed-escape]]` /
`[[SKILL-highspeed-length-match]]` gibi **artık var olmayan** skill adlarını
kullanıyor. Ayrıca `--json` girdi okuyormuş gibi örnekler veriyor (yeni
sözleşmede `--json` yazar).

Bu dosya araç ağacına ait olduğu için **dokunulmadı**. Güncellenmesi gerekiyor;
doğru adlar `skills/README.md`'de, doğru CLI sözleşmesi
`docs/tool-inventory.md` §1'de.

---

## 6. Kaynak ağaç

`ProjectE-1/` **hiç değiştirilmedi** — okundu, kopyalanmadı,
silinmedi. Yeni ağaç bağımsızdır; eski dosyalara hiçbir yolla atıf yapmaz.

---

# Tur 2 — `projectE-2archive` birleştirmesi (2026-08-05)

`~/projectE-2archive/`'in vault kısmı (`Skills/`, `_research/`, `Adimlar/`,
`Mimari/`, `MOCs/`, `Config/`) ProjectE-1'inkiyle **byte-byte özdeşti** —
`diff -rq` sıfır fark. Tur 1'de zaten konsolide edilmişti, tekrar işlem yok.

Yeni olan tek şey `pcb-tool-v2/`: kendi git'i, `pyproject.toml`/`uv.lock`'u,
47 aracı, 47 testi ve 5 Claude skill'i olan bağımsız bir proje.

## Ölçümle çözülen çelişki — `.kicad_dru` kural önceliği

İki kaynak birbirinin tam tersini söylüyordu ve bu bir doküman tercihi değil,
ölçülebilir bir olguydu. KiCad 10.0.5 üzerinde fault-injection:

| `.kicad_dru` sırası | Sonuç | Kazanan |
|---|---|---|
| genel (`≥0.25 mm`) → spesifik (`NET_A ≥0.10 mm`) | 0 ihlal | spesifik = **sonuncu** |
| spesifik → genel | 1 ihlal, gerekçe `GENEL_gevsek` | genel = **sonuncu** |

**Son eşleşen kural kazanır.** Arşivin "ilk eşleşen kazanır, spesifik önce
yazılmalı" düzeltmesi yanlıştır; o sıraya geçilseydi enjekte edilen her spesifik
kural sessizce ezilir, DRC yine "temiz" derdi. Bizim metindeki "ilk eşleşme
uygulanır" ifadesi de kendi içinde çelişkiliydi, kaldırıldı.
Regresyon: `tools/tests/test_dru_precedence.py`. Ayrıntı:
`skills/_shared/rule-conflicts.md` §8.

## Bizim ağacımızda bulunan ve düzeltilen gerilemeler

- `dfm_emc_check.py`: `PCB_VIA.GetWidth()` katman argümansız çağrılıyordu.
  KiCad 10'da per-layer via yığını var; argümansız overload
  `pcb_track.cpp(387)` assert'ini tetikler ve debug/GUI derlemesinde modal
  diyalog açıp headless koşuyu **askıda bırakır**. Doğrulandı (pcbnew 10.0.5),
  düzeltildi, kaynak-düzeyinde teste bağlandı.
- `common/kicad_cli.py`: `kicad-cli` yalnız PATH'ten aranıyordu — `$KICAD_CLI`
  override'ı ve Windows kurulum dizini eklendi. Windows'ta `pgrep` yok, koşulsuz
  çağrı yakalanmamış `FileNotFoundError` ile tüm yazımı kesiyordu; `tasklist`
  dalı eklendi. Bizim per-isim `-x` eşleşmemiz korundu (arşivin `-f`
  alternasyonu, ilgisiz bir MCP sürecine takılıp kendi testlerinde yanlış
  pozitif veriyordu).
- `common/findings.py`: `scanned=0` iken PASS artık `Finding.__init__`'te
  reddediliyor — sadece `result()` içinde değil.
- `common/stackup.py` → `scripts/zdiff_solver.py` katman inversiyonu:
  çözücü çekirdeği (`solve_for_target`, `sweep_stackups`) `common/si_models.py`
  içine indirildi. `common/`'dan `scripts/`'e uzanan import kalmadı; CLI ile
  kütüphane artık sayısal olarak ayrışamaz.
- `research_promotion_gate.py`: `--vault` varsayılanı `parents[2]` idi, yani
  script nereye taşınırsa orayı vault sanıyordu ve taşıma sonrası yanlış ağacı
  gösteriyordu. Artık **zorunlu**.

### ✅ C9 — Seviye tek eksendi: `A(kısmi)` / `A(screen)` (2026-08-10)

**Çakışma.** Matris üç satırda seviye sütununa seviye olmayan bir şey yazmıştı:
`A(kısmi)` (EPAD pasta, kenar keepout) ve `A(screen)` (crosstalk NEXT).
`common/matrix.py` ikisini de çözmeyi **reddediyordu** (`PARTIAL_LEVEL`) ve
haklıydı — düz `A` yazmak tam olarak kimsenin otomatikleştirmediği kısmı iddia
eder. Ama reddin gidecek bir yeri yoktu: bu satırlar assert edilemez olduğu
için aylarca `UNASSERTED_RULE` kaldı.

**Kök neden.** A/B/C/D yalnız *"kanıt nasıl elde edildi"* sorusunu cevaplıyor.
Niteleyiciler başka iki soruyu aynı hücreye sıkıştırmıştı:

| Yazılan | Aslında sorulan | Eksen |
|---|---|---|
| `A(kısmi)` | kuralın ne kadarı kapsanıyor? | **coverage** |
| `A(screen)` | temiz sonuç neyi kapatabilir? | **authority** |

**Çözüm — (a) şıkkı: eksenler ayrıldı, beşinci seviye eklenmedi.**
`level` değişmedi. `coverage` (`full`/`partial`; partial ise
`residual_evidence` **zorunlu**), `authority` (`signoff`/`screen_only`/
`record_only`/`physical_acceptance`; A/B için **varsayılan yok**) ve
`coverage_selector` ayrı alanlar oldu. Yetki merdiveni sıfırdan tasarlanmadı —
`common/vendormodel.py` içindeki `GRANT_SIGNOFF`/`GRANT_SCREEN_ONLY` modeli
ve onun "en zayıf kademeye varsayılan vermek güvenli *görünür*" notu
kopyalandı.

Bir matris satırı artık ancak niteleyicisinin adlandırdığı ekseni beyan eden
bir assertion tarafından sahiplenilebilir; düz `A` hâlâ reddediliyor.

**Bu turda bulunan ve düzeltilen dört gerçek kusur** (hiçbiri hipotez değil,
hepsi ölçülerek doğrulandı):

1. `metrics.<check> at_most 0` **boş kartta geçiyordu** — boş bir kontrol sıfır
   ihlal bildirir, çünkü ihlal listesi boştur. Rapor zarfına ada göre
   erişilebilen `by_check` haritası eklendi; `Finding` zaten `scanned == 0`
   ile PASS kurmayı reddettiği için `by_check.X.status == PASS` tek başına
   "koştu + baktı + temiz" birleşimini taşıyor.
2. `teardrop_count` **her kartta** vacuous idi: `violations` koşulsuz `[]`, ve
   `scanned` **toplam zone sayısı** — yani sıfır teardrop'lu kartta bile
   kapsam muhafızını açardı. Muhafız ancak üreticinin `scanned`'ı doğru
   popülasyona koyması kadar iyidir; bu ders `findings.by_check_map`
   docstring'ine yazıldı.
3. `pair_coupling.py` kendi şeması `design`+`metrics`'i **zorunlu** sayarken
   ikisini de yazmıyordu → her raporu hash'e bağlanamazdı. Hiçbir tüketici
   şemaya karşı doğrulamadığı için görülmemişti (C8'in aynadaki hâli).
4. Assertion loader **bilinmeyen alanı sessizce düşürüyordu**. `establishes:
   record_only` yazmak etkisiz olmakla kalmıyor, set dosyası uygulanmış gibi
   okunuyordu. Artık reddediliyor ve "muhtemelen `authority` demek istediniz"
   diyor.

Ayrıntı: [tool-inventory.md](tool-inventory.md) §2026-08-10 ·
[KALDIGIMIZ-YER.md](KALDIGIMIZ-YER.md) §D4/D6 ·
`skills/dogrulama-matrisi/SKILL.md` §1.

---

## Açık maddeler — karar bekleyen

1. **`design-checker` ayrı skill mi, `claim-audit`'e mod mu?** İkisi de "rapora
   güvenme, ölç" diyor; ama `claim-audit` dışarıdan gelen bir iddiayla tetiklenir,
   `design-checker` DAG içinde zorunlu bir kapıdır ve maker/checker bağımsızlık
   savı taşır. Birleştirmek ucuz, ayrı tutmak bağımsızlık iddiasını korur.
2. **Otoruter isteniyor mu?** `production/freerouting.py`, `route/*` ve
   bounded-search disiplinleri ancak router varsa anlam taşır. Arşivin kendi
   kaydı FreeRouting'i hem "ölü" hem "canlandırıldı" diye işaretliyor.
3. **Context pack bayt bütçesi.** `config/tool_registry.json` 21→48 araçla
   24 kB'a çıktı ve commitli fixture'ın 25 kB'lık paketinin **%96'sı**. Bütçe
   40 kB'a çıkarıldı ama asıl soru mimari: her context pack tüm registry'yi
   taşımalı mı, yoksa yalnız ilgili karar biriminin yetkilendirdiği araçları mı?
4. **`gate_receipt.schema.json` alan adı ayrışması:** şema
   `started_utc`/`finished_utc` istiyor, otoriter Python doğrulayıcı
   `start_utc`/`end_utc` okuyor. `promote.py` şimdilik **ikisini birden** aynı
   değerle yazıyor. Biri kanonik seçilmeli.
5. **`metric_coverage.schema.json`** `additionalProperties: false` ve
   `violations` zorunlu; ama `gate_receipt.schema.json` bunu `reason`/`per_metric`
   taşıyan bir `coverage` bloğu için `$ref`'liyor. Hiçbir yerde `jsonschema`
   koşmadığı için gizli kalmış tutarsızlık.
6. **`promote.py` sertliği:** `--severity-all` altındaki *her* ihlali (uyarılar
   dahil) RED sayıyor. Doğrulayıcının mevcut kuralına uyuyor ama gerçek kartlar
   açık DRC istisnaları gerektirecek — politika kararı.
7. **`parallelism_coverage` hedefi** (Tur 1'den devam): `≥%95` küresel kapı
   kaldırıldı, metrik yalnız raporlanıyor. Net sınıfı başına tolerans+hedef
   tanımlanacak mı?
   → **2026-08-10: kısmen kapandı.** Metrik *bağlandı* (aşağıdaki C9), fakat
   **hedef hâlâ açık** ve bilerek öyle bırakıldı: `coupled_threshold` ile
   `spacing_max_mm` kod içi CLI varsayılanı, kaynaklı profil değil. Bu yüzden
   assertion `authority: screen_only` taşıyor — ölçüm gerçek, eşik kaynaksız.
   Kaynaklı net-sınıfı profili geldiğinde `signoff`a geçiş tek alanlık
   değişikliktir. Karar hâlâ **sizin**; kod artık kararı bekleyebilir durumda.

## Alınmayanlar ve gerekçesi

`empedans_cozucu.py` (bizim `zdiff_solver`'ın alt kümesi, üstelik
`signoff:false` etiketi yok), `sematik_wire_motoru_old.py` (kendi dokümanında
ölü), `bom_lifecycle_koprusu.py` + `cad_api_koprusu.py` (ağ bağımlı, hiç
çalıştırılmamış), `mekanik_dxf_koprusu.py` (kendi beyanıyla stub),
`degisiklik_gunlugu_uret.py` (repoya özgü), `ortam_on_kontrol.py` (ince sarmalayıcı,
testi olmayan tek modül), JLCPCB DFM stub'ı (`NotImplementedError`, endpoint
doğrulanmamış), `kipy`/IPC bloğu (kendi dokümanına göre hiç çalıştırılmamış;
KiCad 9/10 headless IPC export yapamıyor).

`pyproject.toml`/`uv.lock` alınmadı: `circuit-synth` üzerinden **130 paket** ve
366 MB'lık bir venv çekiyordu, ama hiçbir modül `circuit_synth`, `numpy`,
`scipy`, `matplotlib` veya `PySpice` import etmiyordu — tamamen ölü ağırlıktı.

## Git geçmişi

Arşivin 6 commit'inin ikisi çöp (`Create aa`/`Delete aa`), biri her şeyi tek
blob'da döken initial commit; bisect edilebilir bir tarih yok. `git subtree` ile
dolamak yerine `pcb-tool-v2-archive.bundle` (714 KB, tam geri yüklenebilir)
alındı ve dosya bazında köken `docs/PROVENANCE.json`'a hash'leriyle yazıldı.

## Kapanan iki karar (2026-08-05, kullanıcı)

**1. `design-checker` ayrı skill oldu.** `claim-audit`'e mod olarak
katılmadı. Gerekçe: ikisi de "rapora güvenme, ölç" der ama `claim-audit`
faz-dışıdır ve dış bir iddiayla tetiklenir; `design-checker` DAG içinde
**zorunlu bir kapıdır** (Faz 4a) ve maker/checker bağımsızlık savı taşır.
Yöntem tekrarlanmadı: numaralı evidence klasörü, yan etki ölçümü, bisect ve
push öncesi tarama için `claim-audit` §2–§8'e wikilink verildi.

Kaynak skill'in özel `PASS / CONDITIONAL PASS / FAIL` sözlüğü **paralel sözlük
açmadan** kanonik sözlüğe eşlendi: `GREEN` / `CONFIRM` / `RED`, artı kaynağın
hiç ifade edemediği ve bu yüzden sessizce PASS'a çevirdiği iki durum —
`NO_COVERAGE` (kontrol hiç koşmadı) ve `STALE` (rapor alındıktan sonra karta
dokunulmuş). Şema: `skills/design-checker/references/checker_report.schema.json`.

Skill sayısı 16 → **17**.

> ⚠️ **Aşağıdaki 2. karar aynı gün içinde geri alındı — güncel durum için
> §Otoruter kararı (revizyon) bölümüne bakın.** Metin, kararın neden önce böyle
> verildiğini göstermek için silinmedi.

**2. Otoruter istenmiyor.** `otonom-routing` skill'i **yazılmadı**; bounded
search / DSN diff-pair sınırı / MST segmentasyonu disiplinleri bir skill olarak
paketlenmedi.

Araçlar silinmedi — kullanıcı önceki turda "hepsini al" demişti ve bunlar
test edilmiş, çalışan kod. Ama artık **hiçbir skill tarafından yönetilmiyorlar**
ve tasarım akışının parçası değiller:

| Araç | Durum |
|---|---|
| `scripts/route/grid_astar.py` | akış dışı — deneysel/manuel kullanım |
| `scripts/route/topological.py` | akış dışı — deneysel/manuel kullanım |
| `scripts/production/freerouting.py` | akış dışı; java+freerouting.jar zaten bu ortamda hiç çalıştırılmadı |

Yönlendirme kararları [[../skills/highspeed-routing/SKILL]] altında **insan
tarafından** verilir. Bu araçlardan biri bir kapıya girdirilecekse önce onu
yöneten bir skill yazılmalıdır: yönetilmeyen bir router, çıktısı hiçbir kapı
tarafından denetlenmeyen bir board mutasyonu demektir.

> Bu araçların tamamen kaldırılması isteniyorsa: `tools/scripts/route/`,
> `tools/scripts/production/freerouting.py`, ilgili testler ve registry
> kayıtları silinir — tek komutluk iş, ama geri alınması port'u tekrarlamak
> demek olduğu için sorulmadan yapılmadı.

---

# Otoruter kararı (revizyon) — 2026-08-05, kullanıcı

**Karar geri alındı: otoruter isteniyor.** `otonom-routing` skill'i yazıldı
(`skills/otonom-routing/`), üç router akışa **yönetilerek** alındı. Skill sayısı
17 → **18**.

Kapsam bilinçli olarak dar tutuldu: otoruter **kritik netlere dokunmaz**. Üç
motorun hiçbirinde diferansiyel çift veya net-class semantiği yoktur (kodda
`diff`/`net_class` geçen tek satır yok), dolayısıyla çiftler otorutere *ayarla*
değil **kilitle** korunur ve otoruter yalnız kalan düşük hızlı netleri sürer.

Skill'in taşıdığı kanonik kural: **router çıktısı verdict değildir.**
`route_chain()` DRC koşmaz; "router bitirdi" ile "board doğru" ayrı iki olaydır.
`autoroute_receipt.json` (`skills/otonom-routing/references/autoroute_receipt.schema.json`)
ikisini ayırır: board hash'i, kilitli net listesi, arama bütçesi,
`tracks_before/after` ve **import sonrası** DRC sonucu aynı makbuzda durur;
`engine=none`, `tracks_after == tracks_before` veya DRC koşmamışsa GREEN
üretilemez.

## Bu turda ölçülüp düzeltilen iki gerileme

**1. `topological.py` NO_COVERAGE'da exit 0 veriyordu.** Çıkış kodu
`return 1 if summary[FAIL] else 0` idi; yani **hiç net içermeyen** bir request
dosyası — hiçbir şeyin route edilmediği koşu — 0 dönüyordu ve bir zincir bunu
"her şey route edildi" diye okuyabilirdi. `grid_astar.py` aynı durumda zaten 1
dönüyordu ve `topological.schema.json`'ın kendi `$comment`'i
"`scanned == 0` MUST yield NO_COVERAGE" diyordu.

Düzeltme kaba bir "her NO_COVERAGE = exit 1" değildir: `high_speed_via_ban`
kapısı, kartta hızlı net yoksa **meşru olarak** taranmaz ve bu eksik bir routing
sonucu değildir. Kapı yalnız `route_found` üzerinden kurulur. Regresyon:
`test_topological.CliContract.test_an_empty_request_set_exits_1_not_0`.

**2. Registry ön koşulu koda uymuyordu.** `route-grid-astar` kaydı "search is
bounded by node/time/bbox limits and marks UNROUTED on trip" diyordu; kodda
**yalnız `max_nodes`** var, zaman ve bbox bütçesi yok ve "UNROUTED" diye bir
literal hiç geçmiyor (`found=false` + `reason`). Bir preflight'ın var olmayan
bütçelere güvenmesi, sınırsız aramayı sınırlı sanmak demekti. Üç kaydın
preconditions listesi de ölçülen davranışla yeniden yazıldı: board yazan tek
motorun FreeRouting olduğu, diff-pair/net-class desteğinin bulunmadığı ve
`--dsn-only` öncesi kilidin şart olduğu artık makine-okur kayıtta yazılı.

## Hâlâ açık

`production/freerouting.py` bu ortamda **gerçek java + `freerouting.jar` ile hiç
çalıştırılmadı**. `test_production.py` köprü mantığını kanıtlar (JVM bayrakları,
Java exception'ında anında kill, değişmeyen track sayısının "routed"
sayılmaması, jar yokken `NO_COVERAGE`) ama uçtan uca koşu yoktur → bu basamağın
çıktısı ilk gerçek koşuya kadar `CONFIRM`'dür ve tek başına Faz 3→4 kapısını
açmaz.

---

# Tur 3 — `pcb-tool-v2` drift senkronizasyonu (2026-08-06)

Tur 1/2'den bu yana `pcb-tool-v2` tarafında bağımsız geliştirme devam etmişti:
16 tamamen yeni dosya, provenance'ta zaten kayıtlı 11 dosyanın kaynağı
değişmiş (12 hedefi etkiliyor). `archive_head_commit` bu turda
`e30bab29a2166be313b649f00e2398889f62d684`'e güncellendi
(`docs/PROVENANCE.json`, artık 47 kayıt — 12 yeni, 10 hash tazelendi).
pcb-tool-v2'ye dokunulmadı (kullanıcı kendi reposunda commit etti).

## A — Yeni jenerik araçlar (12)

`tools/scripts/board/mechanical_dxf.py` (ezdxf_okuyucu+mekanik_dxf_koprusu —
önceki "stub" reddi geçersizdi, ikisi birlikte tam pipeline),
`board/current_density.py`, `sim/openems.py`, `route/diff_pair.py`,
`route/safe_write.py`, `report/routing_table.py`, `pmic_allocation.py`
(gerçek bir bug bulundu ve düzeltildi — bkz. aşağı), `report/
capability_inventory.py` (+ 4 yeni AST-tabanlı sütun: Security Status,
Internal/External Dependencies, AI Contract — mevcut işlevler bozulmadı),
`board/reference_plane.py`, `testpoint_plan.py` (ikisi de `kicad_koprusu.py`'nin
daha önce hiç işaretlenmemiş 2 fonksiyonundan), `board/via_stub.py` ile
`via_siniflandirma.py` çakışması (gerçek çakışma değil — farklı dosyalar,
eksik provenance kaydıydı, çözüldü).

**`pmic_allocation.py`'de gerçek bug:** greedy tahsis döngüsü her `RailNeed`
için `source` alanını koşulsuz eziyordu, bu yüzden `output_overloaded`
kontrolü (dışarıdan önceden doldurulmuş `source` değerleri için tasarlanmıştı)
hiçbir zaman tetiklenemiyordu — ölü kod. Greedy döngüden ÖNCE geçerli bir
`source` adı taşıyan kayıtlar için kapasite ayıran bir ön-geçiş eklendi.

## B — Proje-özel modüller

`modular-camera-pcb-agent/system-planning/` (pcb-agent/tools DIŞINDA, kullanıcı
kararı): `camera_hub_interface.yaml` + `camera_hub_contract_check.py`
(arayuz_sozlesmesi.yaml/coklu_kart_sozlesme_kontrolu.py'den — ayrı bir
kavram olduğu doğrulandı, `interface_contract.json`'ı EZMEZ), `vc_id_
assignment.py` (sistem_orkestratoru.py), `device_tree_gen.py`
(device_tree_uretici.py, hiçbir zaman gerçek `.dts` üretmez).

## C — Değişmiş 12 hedef, dosya dosya

Yöntem: kaynağı ve hedefi TAM oku, def/class adlarını eşleştir, davranış
farkını çıkar, "zaten daha kapsamlı ported" ile "gerçek yeni yetenek" ile
"mimari-uyumsuz eski orkestrasyon" arasında ayır.

- **`kuvvet_yonelimli_yerlesim.py` → `place/force_directed.py`.** `angle_offset_rad`
  parametresi + `PlacementParameterSet`/`PlacementScore`/`calibrate_score_weights`
  (numpy/scipy opsiyonel katman) + `try_multiple_placements` eklendi.
- **`pcb_stackup_planner.py` → `common/stackup.py`.** 46 kaynak fonksiyonun
  45'i zaten birebir karşılığıyla vardı; tek gerçek fark
  `via_tipi_aspect_ratio_kontrolu()` — `via_siniflandirma.py`'deki (kendisi de
  hiç ported edilmemiş, task A#8) `ViaTipiDetay`/`select_via_type_for_bga()`'ya
  bağımlıydı. İkisi birlikte `PhysicalVia`/`ViaTypeDetail`/`MAX_ASPECT_RATIO_
  BY_VIA_TYPE`/`select_via_type_for_bga` olarak `common/stackup.py`'ye
  taşındı (kaynaktaki döngüsel-import-kaçınma gerekçesi burada geçersiz —
  ikisi zaten aynı modülde). `scripts/stackup_plan.py`'ye `physical_vias`
  intent alanı + `via_type_aspect_ratio_check` eklendi.
- **`uretim_zinciri_koprusu.py` → `production/{assembly,freerouting,contracts}.py`.**
  CPL/rotasyon/oryantasyon/panelizasyon, freerouting köprüsü (JVM bayrakları,
  fail-fast satır taraması, stub-değil-gerçek-routed kontrolü) ve DRC/parts
  kontrat kapıları **zaten birebir veya daha sıkı** ported'dı (doğrudan
  karşılaştırıldı). Tek gerçek boşluk: `_drc_unconnected_net_gruplari()` —
  `common/drc_summary.py::unconnected_net_positions()` olarak eklendi, ve
  `main.py`'nin `_drc_baglantisiz_netler()`'inden gelen gerçek kanıtla
  (gerçek kicad-cli çıktısında net adı köşeli parantez içinde item
  description'ında geçiyor, üst-seviye alan DEĞİL) net-adı çıkarma mantığı
  düzeltildi. Reddedilenler: JLCPCB DFM API (kaynağın kendi ifadesiyle "bilerek
  bir İSKELET"), JLC2KiCadLib (ağ-bağımlı-test-edilmemiş), otonom-fallback-
  merdiveni (pcb-agent'ta olmayan `otonom_kurtarma_motoru.py`'ye bağımlı,
  skill-seviyesi orkestrasyonla ÇAKIŞIR).
- **`main.py` → `promote.py`.** `promote.py` zaten sadece `cmd_promote`'un
  BİLİNÇLİ yeniden yazımıydı (kendi docstring'i bunu söylüyor). Geri kalan
  `faz_*`/`cmd_run` monolitik "tek CLI her şeyi yapar" akışıdır — pcb-agent'ın
  skill-tabanlı ajan mimarisinin YERİNE koyduğu tam o desendir (kaynağın
  KENDİ docstring'i bile routing/production'ı "insan onayı veya ayrı skill
  gerektiriyor" diye AYNI komuta dahil etmemişti). `cmd_via_capi`/`cmd_coklu_
  kart_dogrula`/`cmd_envanter_guncelle`/`cmd_sistem_atama_plani_uret`/
  `cmd_device_tree_uret` zaten bağımsız var olan araçların ince sarmalayıcıları.
- **`otonom_python_router.py` → `route/grid_astar.py`.** Gerçek, doğrulanmış
  yeni içerik: via-yerleşim geçerliliği (`via_placement_valid` — GERÇEK bir
  221-DRC-ihlali olayından sonra eklendi, annular-ring+hole-to-hole), 3W-kuralı
  SI maliyet fonksiyonu, empedans-sapmasıyla ölçeklenen via maliyeti, katman/via-
  farkında 3D A* (`layered_astar_search`), coupled diferansiyel-çift A*
  (`coupled_astar_search`). `grid_astar_search`'e `extra_cost_fn` parametresi
  eklendi (temel arama değişmedi, sadece hook). Bulunan tutarsızlık: kaynağın
  varsayılan via çapı/delik/min-annular-ring üçlüsü kendi varsayılan sınırını
  SAĞLAMIYOR (0.5/0.3mm → 0.1mm ring < 0.15mm varsayılan minimum) — düzeltilmedi
  (uydurma sayı riski), docstring'de AÇIKÇA işaretlendi.
- **`karar_birimleri.py` → `common/decisions.py`.** Kritik-pin (boot-strap/
  mode-select/NDA) datasheet-teyit kapısı eklendi (`critical_pin_category`,
  `detect_and_save_critical_pin_decisions`) — CV72S/CV5S NDA pin endişesiyle
  doğrudan ilgili.
- **`ipc7351_footprint.py` → `common/ipc7351.py`.** Modülün kendi "TBD: QFN/
  BGA" notu doldu: `gullwing_land_pattern`/`qfn_land_pattern`/`bga_land_pattern`
  eklendi (BGA tamamen farklı formül ailesi — ball çapının oranı, toe/heel/side
  DEĞİL). RK3588 (1088 ball) ve CV72S gibi BGA parçaları için doğrudan gerekli.
- **`ecad_mcad_termal_kopru.py` → `board/thermal_bridge.py`.** Junction-sıcaklığı
  ilk-yaklaşım hesabı (`JunctionTempInput`/`check_junction_temperature`,
  Tj=Tref+P×Rθ, kasa-temas yolu RθJA/ortama tercih edilir) + termal ped BOM
  notu eklendi.
- **`pcb_highspeed_escape.py` → `common/escape_geom.py`.** Çoklu-kart (host→N
  kamera) skew bütçesi eklendi (`multi_board_skew_budget`) — bu projenin
  gerçek çoklu-kamera mimarisine doğrudan uygulanabilir. Reddedilmedi ama
  ERTELENDİ: DF40-tipi yüksek-yoğunluklu konnektör escape planı ve via-pair
  gerekliliği (Liang-Barsky kesişim testi) — gerçek/referans-board-ölçümlü
  içerik ama şu an tüketicisi yok, spekülatif eklenmedi.
- **`uretim_ciktilari_cli.py` → `production/outputs.py`.** Doğrulama-kapısı
  mantığı (şema-tanınmadı fail-closed, pcbnew-yok fail-closed varsayılan)
  zaten birebir + daha sıkı ported'dı. `--routing-tablosu` bayrağı bağımsız
  var olan `routing_table.py`'nin ince sarmalayıcısıydı, taşınmadı (agent
  zaten ayrı ayrı çağırabilir).
- **`dfm_emc_check.py` → `board/dfm_emc_check.py`.** Doğrudan karşılaştırıldı
  (annular ring, paste coverage, regex varsayılanları) — birebir/daha iyi
  (paylaşılan `common.boardgeom`/`common.findings`/`common.units` kullanıyor).
  Tek eksik provenance kaydıydı, bu turda eklendi.

## Alınmayanlar ve gerekçesi (Tur 3)

| Kaynak | Gerekçe |
|---|---|
| JLCPCB DFM API (uretim_zinciri_koprusu.py) | Kaynağın kendi ifadesiyle iskelet/yer tutucu, doğrulanmamış endpoint |
| JLC2KiCadLib (uretim_zinciri_koprusu.py) | Ağ-bağımlı, bu ortamda hiç çalıştırılmadı, kaynak da "DOĞRULANMADI" diyor |
| Otonom-fallback-merdiveni (uretim_zinciri_koprusu.py) | pcb-agent'ta olmayan orkestrasyon modüllerine bağımlı, skill mimarisiyle çakışır |
| `main.py`'nin `faz_*`/`cmd_run` | Skill-tabanlı ajan orkestrasyonunun YERİNE koyduğu eski monolitik desen |
| `main.py`'nin `cmd_*` alt-komutları | Zaten bağımsız var olan araçların ince CLI sarmalayıcıları |
| DF40 konnektör escape planı (pcb_highspeed_escape.py) | Gerçek içerik ama tüketicisi yok, spekülatif eklenmedi (ertelendi) |
| Outline-değişince-yeniden-yerleştir (main.py) | Bu projede henüz yerleşim-iterasyon aşamasına gelinmedi (şematik aşaması) |
| `hdi_stackup_genisletme.py` | Kaynağın kendi ifadesiyle "SADECE VERİ MODELİ, gerçek mantık yok" iskelet |

## Regresyon testleri

Tur 3'ün her eklentisi fault-injection'lı testlerle geldi (~350+ yeni test).
`python -m unittest discover -s tools/tests -t .` Tur 3 sonunda temiz;
tek beklenen fark registry SHA256 tazeliğiydi, `sync_tool_registry.py --write`
ile kapatıldı (48 araç, GREEN).

---

# Tur 4 — daha önce reddedilen/ertelenen 4 madde, kullanıcı kararıyla aktif
edildi (2026-08-06)

Tur 3'te reddedilen/ertelenen dört madde ("ağ-bağımlı", "iskelet",
"test edilmemiş", "eski monolitik desen") kullanıcının açık isteğiyle bu
turda gerçek/üretilebilir hale getirildi. Registry 59→**62 araç**
(`jlcpcb-dfm`, `jlc-library`, `pipeline-adapters`).

## 1 — JLCPCB DFM API + JLC2KiCadLib

**`tools/scripts/production/jlcpcb_dfm.py` (yeni, CONFIRM/iskelet — kullanıcı
kararı):** kimlik doğrulama (`JLCPCB_API_KEY`) VE endpoint
(`JLCPCB_DFM_ENDPOINT`) ortam değişkeninden okunur, İKİSİ de UYDURULMAZ.
`--allow-network` açıkça verilmeden hiçbir ağ çağrısı yapılmaz. Request/
response şeması hâlâ DOĞRULANAMADI (JLCPCB hesabına erişim yok) — modülün
kendi "STATUS: CONFIRM" notu bunu açıkça işaretliyor; `parse_dfm_response()`
tek başına izole, gerçek şema geldiğinde SADECE o fonksiyon değişecek.

**`tools/scripts/production/jlc_library.py` (yeni, GERÇEK/doğrulanmış):**
`uretim_zinciri_koprusu.py::jlc_parcasi_indir()`'in reddedilme gerekçesi
("ağ-bağımlı-test-edilmemiş") bu turda gerçek PyPI verisiyle kapatıldı —
`JLC2KiCadLib` paketinin GERÇEK `--help` çıktısı PyPI'den çekilip
doğrulandı (`-dir`/`-symbol_lib`/`--skip_existing`/`--version` bayrakları
UYDURULMADI, kaynağı gösterildi). Offline önbellek `var/memory/
jlc_parts_cache/` altında (aynı vault-guard'lı `scripts/memory/store.py`
ağacı) `(lcsc_kodu, symbol_lib, output_dir)` üçlüsüne göre başarı-işareti
tutar — dosya kopyalamaz, sadece tekrar ağ çağrısını atlar.

## 2 — main.py'nin fazları: Command Adapter deseni

**`tools/scripts/pipeline_adapters.py` (yeni, jenerik):**
`check_environment`/`check_schematic_erc`/`check_thermal`/`plan_placement`/
`check_drc`/`produce_outputs`/`recommend_via_size` — her biri ZATEN var olan
bir araca (`common.toolpaths.check_all_tools`, `common.kicad_cli.run_erc`,
`thermal_bridge.check_thermal_contact`, `force_directed.solve_placement`,
`drc_gate.gate`, `outputs.py`'nin kapı fonksiyonları,
`via_current.recommend_via`) ince bir sarmalayıcı. **Hiçbiri diğerini
çağırmaz** — sıralama kararı skill/agent'ta kalır, eski `cmd_run`'ın
sabit-sıra deseni GERİ GETİRİLMEDİ. `tests/test_pipeline_adapters.py`'de
bunu AST ile kanıtlayan bir test var (`NoHardcodedSequence`).

**`system-planning/command_adapters.py` (yeni, proje-özel):**
`generate_vc_id_plan` = `build_assignment_plan` + `validate_plan` +
(SADECE geçerliyse) `merge_into_contract` — arşivin `cmd_sistem_atama_
plani_uret`'inin birleşik davranışı birebir. `generate_device_tree` zaten
tek fonksiyon olduğu için sadece yeniden dışa aktarıldı. `pcb-agent/tools`
DIŞINDA tutuldu (proje-özel/jenerik ayrımı bozulmadı).

## 3 — DF40 konnektör escape'i

`common/escape_geom.py`'ye `HighDensityConnectorEscape`/
`pair_to_pair_min_distance_mm`/`build_escape_plan`/`via_pair_required`
eklendi — referans board'dan (CM4 IO Board TRD escape'i) ÖLÇÜLMÜŞ sayılar
(0.55/2.1/1.6mm, 0.47/0.127 oranı) birebir taşındı, UYDURULMADI.
`via_pair_required` gerçek Liang-Barsky segment/AABB kesişim testi kullanır.

**Düzeltme (kullanıcıya bildirilmesi gereken):** görev başlangıçta "otonom-
routing skill'ine bağla" diye yazılmıştı, ama mimari inceleme gösterdi ki
`otonom-routing` SADECE sınırlı otomatik router'ı (topological/grid_astar/
freerouting) yönetiyor — escape geometrisi placement-sonrası/routing-öncesi
bir kavram ve zaten `highspeed-routing` skill'inin (`pcb-highspeed-escape` +
`highspeed-length-match` birleşimi) sahip olduğu alan. Kural
`highspeed-routing/SKILL.md` §2(h)'ye eklendi, `otonom-routing`'e DEĞİL.

## 4 — HDI stackup: blind/buried/microvia katman-aralığı

`common/stackup.py`'ye eklendi (`hdi_stackup_genisletme.py`'nin BOŞ veri
modelinin — `MikroviaKatmani`/`BlindBuriedViaBolgesi` — kendi docstring'inin
işaret ettiği bağlantı noktasını gerçekleştirir):

- `via_layer_span_valid(via, layer_count)`: bir via'nın (start_layer,
  end_layer) çifti `kind`'ına (THROUGH_HOLE/BLIND/BURIED/MICROVIA) göre
  IPC-2226 tanımına uyuyor mu? (through-hole iki dış katmana da ulaşmalı,
  buried hiçbirine değmemeli, blind/microvia tam bir ucu dış katman olmalı).
  **Bulunan gerçek tasarım hatası (kendi kodumda):** ilk yazımda MICROVIA'ya
  da BLIND'ın "tam bir ucu dış katman" kuralı uygulanmıştı — bu, stacked
  mikrovia dizisinin ARA elemanlarını (iki iç katman arası tek adım) yanlış
  reddediyordu. Test yazarken (3 test FAIL) yakalandı, düzeltildi: microvia
  kuralı sadece `layer_span == 1`, dış/iç katman kısıtı YOK.
- `stacked_microvia_sequence_valid(vias, layer_count)`: ardışık
  mikrovia'ların boşluksuz/çakışmasız zincir oluşturup oluşturmadığı
  (`select_via_type_for_bga`'nın kendi belgelediği sınırın devamı).
- `resolve_stackup_layer_index`/`microvia_span_valid_for_stackup`/
  `blind_buried_region_valid_for_stackup`: arşivin `ust_katman_adi`/
  `alt_katman_adi`/`baslangic_katman_adi`/`bitis_katman_adi` (katman
  İSİMLERİYLE tanımlama) alan şeklini GERÇEK bir `build_stackup()`/
  `plan_stackup()` sözlüğüne karşı doğrular — var olmayan bir katman adı
  `StackupDataError` fırlatır, sessizce UYDURULMAZ.

`hedef_delik_capi_mm`/`aspect_orani_siniri`/`fabrika_destekliyor_mu`
(arşivin kendi "TBD, UYDURULMADI" alanları) hâlâ eklenmedi — gerçek
aspect-ratio sayıları zaten `MAX_ASPECT_RATIO_BY_VIA_TYPE`'da (Tur 3).

## Bookkeeping

`docs/PROVENANCE.json`: `jlc_library.py` yeni kayıt (kaynak:
`uretim_zinciri_koprusu.py`), `stackup.py`/`escape_geom.py` hash tazelendi.
`jlcpcb_dfm.py`/`pipeline_adapters.py`/`system-planning/command_adapters.py`
kayıt DIŞI bırakıldı — arşivden kopyalanan içerik değil, orijinal kod.
Yeni testler (~120+) hepsi fault-injection'lı geldi; tam suite Tur 4
sonunda `python -m unittest discover -s tools/tests -t .` ile doğrulandı.

---

# Tur 5 — freerouting canlı doğrulaması ve bulduğu P0 (2026-08-06)

Tur 2'den beri açık duran **"`production/freerouting.py` bu ortamda gerçek
java + jar ile hiç çalıştırılmadı"** maddesi kapatıldı. Yapılanlar ve
ölçülenler:

1. Gerçek GitHub releases API'sinden v2.2.4 bulundu; `find_jar_download_url`
   4 asset (`.zip`/`.dmg`/`.msi`/`.jar`) arasından doğru `.jar`'ı seçti.
2. Gerçek indirme: **57,766,203 byte**, GitHub'ın `Content-Length`'iyle
   birebir; `PK\x03\x04` zip magic doğrulandı; `.part` → atomik rename
   çalıştı.
3. `java -jar` ile headless çalıştı (`Freerouting v2.2.4`), argümansız
   çağrıda exit=1.
4. Gerçek pcbnew 10.0.4 DSN export'u (`cm4-io-test/eth_only.dsn`, 104 KB,
   174 net sınıfı bir board) ile `run_freerouting()` uçtan uca koşuldu.

## P0 — timeout hiç uygulanmıyordu

**Ölçüm:** `timeout_s=240` verildi, süreç **1682.7 saniye (28 dakika)**
çalıştı ve yalnızca elle öldürüldüğü için durdu. Fonksiyon buna rağmen
`timed_out=True` + *"FreeRouting did not finish within 240s"* raporladı —
yani **uygulamadığı bir bütçeyi uygulamış gibi gösterdi.**

**Kök neden:** `proc.stdout.readline()` bloklayan bir çağrıdır. FreeRouting
dört satır basıp tüm routing hesabı boyunca susuyor; okuma bloklanınca
döngü, başındaki deadline kontrolüne bir daha dönemiyor. Modülün kendi
docstring'i tam bu senaryoyu ("an autonomous chain locking up for half an
hour on one routing step is not acceptable") önlemek için yazılmıştı.

**Neden birim testler yakalayamadı:** mevcut testler `FakeStdout.readline()`
kullanıyordu ve o anında dönüyordu — susan bir çocuk süreç hiç modellenmemişti.
Bu, "mock'lanmış test yeşil olabilir ama gerçek davranışı kanıtlamaz"
ilkesinin somut bir örneği.

**Düzeltme:** okuma ayrı bir daemon thread'e alındı, satırlar bir `queue`'ya
itiliyor, ana döngü kuyruğu kalan süreyle poll ediyor (Windows'ta pipe
üzerinde `select()` yok, taşınabilir çözüm thread). Gerçek zamanlı
java-exception taraması ve fail-fast kill davranışı korundu.

**Kanıt (canlı, düzeltmeden sonra):** 60 s bütçe → **60.2 s**'de durdu,
`timed_out=True`. Regresyon testi gerçek bir çocuk süreç kullanıyor
(bir satır basıp 120 s uyuyor); eski kodda 120 s sürer ve `timed_out=False`
dönerdi: `test_production.FreeroutingBridge.
test_regression_a_silent_child_is_still_killed_at_the_deadline`.

## Kozmetik açık — bilerek düzeltilmedi

v2.2.4, `-Djava.awt.headless=true` ile **gerçekten headless çalışıyor**
(routing job başlıyor ve ilerliyor) ama yine de bir uyarı basıyor:
*"Couldn't get screen resolution ... disable the GUI by setting gui.enabled
to false"*. `gui.enabled` bir JVM sistem özelliği veya CLI seçeneği
**değil** — `java -jar freerouting.jar -help` çıktısında yok ve
`-Dgui.enabled=false` denendi, uyarıyı **susturmadı**. Ölçülebilir biçimde
hiçbir şey yapmayan bir bayrağı göndermektense boşluğu belgelemek tercih
edildi; `test_no_unverified_gui_flag_is_shipped_binary_not_exercised` bu
kararı kilitliyor.

## Ayrıca — envanterin test-eşleştirme ölçüm hatası

`capability_inventory.py` 94 modülün 56'sını "test yok" raporluyordu.
AST + gerçek import taramasıyla doğrulandı: 6'sı ürün modülü bile değil
(fixture/test helper), 12'si boş `__init__.py`, **35'i farklı adlı bir test
dosyasında gerçekten kapsanıyor** (`test_board_checks.py` tek başına 4,
`test_common.py` 11, `test_production.py` 4 modül kapsıyor), **gerçekten
testsiz olan yalnızca 3**: `sch/rewire.py`, `sch/sch_route.py`,
`sch/wireify.py` — ikisi şematik dosyası YAZIYOR, bu yüzden yüksek
öncelikli. Envanterin 1:1 dosya-adı eşleşmesi bu projenin bilinçli test
gruplama düzeniyle uyumsuz; düzeltme ayrı bir madde olarak sırada.

---

# Tur 6 — bulguların kapatılması ve iki yeni bug (2026-08-06)

Tur 5'te açılan maddeler kapatıldı ve kapatma sırasında iki gerçek bug daha
çıktı. Ortak örüntü, Tur 5'inkiyle aynı: **"testler yeşil" ile "gerçekten
doğru" arasındaki fark.**

## BUG — `det_uuid` basename çakışması (kullanıcı tespiti)

`common/ids.py::det_uuid` anahtarı `os.path.basename(path)` idi. Aynı adlı
İKİ FARKLI şematik birebir aynı UUID üretiyordu ve KiCad duplicate UUID'yi
AYNI NESNE sayar. Kanıt:

    det_uuid('rewire','proc-a-cv72s/.../mipi.kicad_sch','U1-1')
    det_uuid('rewire','proc-r-rk3588/.../mipi.kicad_sch','U1-1')
    -> ikisi de 3d74994a-8677-5550-842b-63a35d9b8573

Tetikleyici bu ağaçta HAZIR: 71 şematik içinde `mipi`, `power`, `PCIe`,
`hdmi`, `RTC`, `PSUs`, `USB2-HUB` adları 3'er kez geçiyor.

**Neden yakalanmamıştı:** yanlış davranış TESTİN KENDİSİNE gömülüydü —
`test_common.py` *"only the basename should matter"* diye assert ediyordu.
Docstring'in verdiği gerekçe ("re-running on an unchanged file produces an
unchanged diff") basename GEREKTİRMİYOR; tam yol ile de sağlanır.

**Neden tam yol da çözüm değil:** her otonom koşu `.scratch/<timestamp>/`
kopyasında çalışıp kanoniğe terfi ediyor (`common/scratch.py`), tam yol
anahtarı her koşuda tüm UUID'leri churn ederdi.

**Çözüm:** `scratch_independent_key()` — `.scratch/<id>/` segmenti soyulmuş,
ayraçları normalize edilmiş yol. Aynı dosya scratch↔kanonik arasında
kimliğini korur, farklı kartların aynı adlı dosyaları ayrışır.

**Migrasyon riski ÖLÇÜLDÜ, sıfır çıktı:** 71 şematiğin tamamı tarandı,
`wireify`/`rewire` üretimi UUID içeren dosya YOK. Dedektörün kendisi pozitif
kontrolle doğrulandı (bilerek enjekte edilen bir UUID yakalandı) — hiçbir şey
bulamayan bir dedektör bozuk da olabilirdi.

## RİSK — rollback yolunda kapanmamış dosya tanıtıcısı

`rewire.py`, `wireify.py`, `sch_wire.py` toplam 5 yerde `open(...).read()` /
`open(...).write(...)` kullanıyordu, CPython refcount'una güvenerek.
Kozmetik DEĞİL: yazma hemen ardından AYNI dosyada `shutil.copyfile` ile
rollback geliyor ve Windows'ta açık bir yazma tanıtıcısı bunu
`PermissionError` ile düşürebilir — yani kurtarma yolu tam ihtiyaç
duyulduğu anda kırılabilirdi. Hepsi `Path.read_text`/`write_text`'e
çevrildi. Regresyon koruması iki taraflı: statik desen taraması +
`ResourceWarning` yakalayan dinamik test.

## Envanterin test-eşleştirme hatası kapatıldı

`capability_inventory.py` yalnız `test_<stem>.py` arıyordu; bu proje ise
testleri BİLİNÇLİ gruplandırıyor (`test_board_checks.py` dört board
modülünü, `test_common.py` on bir `common/` modülünü kapsıyor). 97 modülün
55'i "testsiz" raporlanıyordu.

Düzeltme üç kademeli: (1) doğrudan ad eşleşmesi, (2) `tests/` altında ad
eşleşmesi, (3) **gerçek import** veya **script yolu literali** (CLI araçları
subprocess ile koşuluyor, import edilmiyor). Kelime eşleşmesi KASITLI olarak
sayılmıyor — `det_uuid("rewire", ...)` çağrısı araç adını argüman olarak
geçiriyor ama `rewire.py`'yi test etmiyor.

`__init__.py` dosyaları aramadan tamamen çıkarıldı: hepsi `__init__` stem'ini
paylaştığı için tek bir test onları toptan "test edilmiş" gösterirdi — ilk
denemede tam bu ters yanlış-pozitif oluştu ve düzeltildi.

**Sonuç:** 55 "testsiz" → **15**, bunun 13'ü paket işaretçisi, 1'i fixture ve
**1'i gerçek**: `common/contract.py`. O da bu turda test edildi (35 test) —
doğrulama ilkelleri modülü olduğu için önemli: `require_int` bool'u
reddetmezse (Python'da bool bir int'tir) veya `require_number` NaN/inf'i
geçirirse (`json.loads` bunları üretir), uzaktaki bir kapı reddetmesi gereken
veriyle PASS verirdi.

## Durum

Tam suite **1775+ test, 0 hata**. Registry 63 araç GREEN. Envanterde
gerçekten testsiz modül kalmadı.

---

# Tur 7 — envanterin kendi ölçümü ve mock taramasının kalibrasyonu (2026-08-07)

Sıradaki madde #38 ("envanteri yeniden üret") rutin bir yeniden üretim
sanılıyordu. Çıktının **bayat** olmadığı, bir kolonunun hiç ölçülmediği
anlaşıldı. Tur 5 ve 6'nın örüntüsü üçüncü kez: **"testler yeşil" ile
"gerçekten doğru" arasındaki fark.**

## BUG — `test_status` hiçbir zaman ölçülmüyordu

`capability_inventory.py`'nin kendi namus sınırı şunu yazıyor: *"test_status
is the ONE exception — it is REALLY measured with `pytest --junit-xml`, never
a made-up 'all passed' default."* Ölçüm şöyle anahtarlanıyordu:

    classname = "tests.test_ai_feedback_loop.ParseSimulationLogTests"
    file_name = classname.split(".")[-1]     # -> "ParseSimulationLogTests"

`unittest.TestCase` süitlerinde pytest, junit `classname`'inin sonuna **test
sınıfını** ekler. Anahtar test DOSYASI değil test SINIFI oluyordu; arama
hiçbir zaman eşleşmiyor, 84 modülün tamamı sessizce `olculemedi`ye
düşüyordu. Süit her koşuda gerçekten 224 saniye çalışıyor, sonuçlar hiçbir
satıra ulaşmıyordu.

**Neden görünmüyordu:** `--skip-tests` ile bilerek atlanan koşu ile ölçüm
borusu kırık koşu **aynı hücreyi** üretiyordu. Tabloyu okuyan biri ikisini
ayırt edemezdi. Bu, junit hatasının kendisinden daha önemli kusurdu.

**Çözüm:**
- `junit_test_module()` — son `test_` önekli bileşeni alır (hem sınıflı hem
  fonksiyon tarzı testte doğru), eşleşme yoksa anahtar uydurmaz.
- `measurement_status()` — koşu kendi eşleşme oranını rapor eder; gerçek bir
  pytest koşusu hiçbir modülle eşleşmezse `warning` alanı dolar ve stderr'e
  uyarı düşer. Çıkış kodu kırmızıya çevrilmedi: diğer kolonlar geçerli, bir
  raporlama aracını kırmızıya boyamak onları da gizlerdi.
- Regresyon koruması iki katmanlı: junit classname biçimlerinin birim
  testleri **artı uçtan uca gerçek bir pytest koşusu** (kurulu pytest'e
  biçimi doğrudan sorar). Yalnız birim testi yazılsaydı, biçimi değiştiren
  bir pytest sürümü kolonu yine sessizce bozardı ve testler yeşil kalırdı.

**Sonuç:** 84 modülün 84'ü ölçüldü (`gecti`), `olculemedi` sıfır.

## #45 — mock taraması: 38 bayrak → 2

Bulgu ailesinin son üyesi. Ölçüm: 35 dosyada 199 anahtar-kelime vuruşu,
98 modülün 38'i bayraklı. Elle doğrulandı: **gerçek olan bir taneydi.**
%97 yanlış-pozitif oranı, hiç bayrak kaldırmamakla aynı şeydir.

Aileler ve kapatılışları:

| Aile | Örnek | Kapatma |
|---|---|---|
| Alan terimi | `via_stub`, `stub_length_mm`, `sch_wire.STUB` (117/199 vuruş) | Tarama yalnız **prose**'a bakar: tanımlayıcıdaki kelime bir **ad**dır, iddia değil |
| Alt-dize | `grid_astar`'ın "far more **stub**born"u | Çekim ekleriyle sınırlı tam kelime |
| Olumsuzlama | "there is deliberately NO hardcoded fallback", "NEITHER is hardcoded" | Olumsuzlayıcı kelimeyi **yönetmeli** (bitişik pencere) |
| Başka artefakt | "the archive's `main.py` hardcoded...", "VENDORED FROM `drc.sh`" | Paragraf kapsamı + madde listesinin giriş cümlesini miras alması |
| Alıntı | `"is a stub" confesses` | Bir ifadeyi **anmak** onu iddia etmek değildir |
| Self-test | `_FakeBoard`, `fake_sim` | `test_*.py`'ı dışlamakla aynı gerekçe: bunlar test ikizi |
| Sabit-dönüş | Dosya uzantısı→komut, durum→çıkış kodu, net adı→net sınıfı (5 modül) | Uydurma sabit yalnız **dışarıdan** gelmesi gereken yanıt için şüpheli: fonksiyon dışa dönük görünmeli **ve** hiçbir dış çağrı yapmamalı |

Olumsuzlama kuralında ilk deneme cümle geneline bakıyordu ve **yanlıştı**:
`jlcpcb_dfm`'in gerçek itirafını yutuyordu ("GUESSED SHAPE, **not** verified:
... a placeholder for however the real API wants the file transferred") —
tüm taramanın var olma sebebi olan tek bulguyu. Test olarak sabitlendi.

Kendine-referans tuzağına bu dosya **üçüncü kez** düştü: taradığı ifadeleri
adlandırmak zorunda olan dosya, adlandırdığı için mahkûm oluyordu. Muafiyet
değil, genel kural yazıldı (alıntı = anma).

**Elenen vuruşlar sessizce atılmıyor:** 30 eleme gerekçesiyle koşu raporunun
`mock_screen` bloğuna yazılıyor (14 alıntı, 11 olumsuzlama, 5 başka
artefakt). Fazla daralmış bir filtre, tablodan bakınca temiz bir depoyla
birebir aynı görünür — junit hatasını gizleyen mekanizmanın aynısı.

Kalan 2 bayrak: `jlcpcb_dfm.py` (bilinen iskelet, #43 — kullanıcı bloklu) ve
`testpoint_plan.py` (`method` alanı DFT stratejisi seçilene kadar geçici
varsayılanda; gerçek bir "sonra düzeltilecek" kaydı).

`determine_security_status` de aynı taramayı kullanır oldu — daha önce ham
alt-dize taramasıyla "NO hardcoded fallback" cümlesi + tek satırlık bir
erişimci bir modülü "Riskli Mock" saymaya yetiyordu.

## Envanterin bilinen tek tahmin hatası (kayıt için)

`Durum` kolonu `common/ids.py`'yi **İskelet** gösteriyor; değil — 2 fonksiyon
+ 80 satır altı eşiğine takılan bir `otomatik_tahmin`. Kolonun kaynağı zaten
`status_source` ile açıkça işaretli. Gerçek iskelet `jlcpcb_dfm.py`'dir ve
onu artık `real_or_mock` yakalıyor.

## Durum

Tam suite **1850 test OK (72 skip)**. Registry **GREEN, 63 araç**
(`capability_inventory.py` değiştiği için hash yeniden senkronlandı).
Envanter: 98 modül, `test_status` 84/84 ölçüldü ve **geçti**, mock bayrağı 2,
gerçekten testsiz 0.

---

# Tur 8 — #46: iki reddin yeniden değerlendirmesi (2026-08-07)

Emsal doğru çıktı. `mekanik_dxf_koprusu` reddi nasıl geçersizse,
`bom_lifecycle_koprusu` ve `cad_api_koprusu` reddi de öyle — **ikisi de aynı
hatayla reddedilmişti: modül, ağa bakan ince yüzeyine göre yargılanmış,
özündeki çevrimdışı doğrulama görülmemişti.**

Ret gerekçesi: *"ağ bağımlı, hiç çalıştırılmamış"*. Ölçüm:

| Modül | Ağ yapan kısım | Özü (çevrimdışı) |
|---|---|---|
| `bom_lifecycle_koprusu` | 1 fonksiyon (`nexar_sorgula`), anahtarsız zaten `TBD`'ye düşüyor | lifecycle kapısı, risk skoru, pin-uyumlu alternatif kabulü, rapor |
| `cad_api_koprusu` | 1 fonksiyon (`varlik_sorgula`) + indirme | **pin sayısı kapısı** (sembol↔datasheet↔footprint), lifecycle kapısı, host beyaz listesi, idempotent lib-table kaydı |

Üstelik `bom-lifecycle` skill'inin **kendi şeması** ret gerekçesini
çürütüyor: `provider.queried_from` alanının yorumu *"Network calls belong to
the main agent. A sandboxed subagent silently fabricates."* Yani sözleşme,
sorguyu aracın yapmamasını **şart koşuyor**. Reddedilen şey, sözleşmenin
zaten yasakladığı kısımdı.

## Yan bulgu — skill'ler var olmayan araçlara işaret ediyordu

`bom-lifecycle/SKILL.md` `tools: [bom_lifecycle_koprusu]` diyordu; böyle bir
araç bu pakette **yoktu**. Tarama yapıldı: **18 skill'in 15'i** registry'de
karşılığı olmayan en az bir araç adı taşıyordu. İki sınıf:

- **yazım sapması** — registry tireli (`dfm-emc-check`), skill'ler modül adını
  alt çizgiyle yazmış;
- **hiç taşınmamış arşiv adları** — `uretim_ciktilari_cli` (→
  `production-outputs`), `mcad_carpisma_koprusu` (→ `mcad-collision`),
  `crosstalk_mcp_server` (→ `crosstalk-mcp`), `measure` (→ `measure-board`),
  `coupling` (→ `pair-coupling`), `drc.sh` (→ `drc-gate`; kabuk betiği zaten
  `drc_gate.py`'ye vendor edilmişti).

Skill'i okuyan bir ajan bulamayacağı bir aracı arıyordu — bu paketin
kapatmak için var olduğu iddia/gerçek boşluğunun ta kendisi. 15 skill
düzeltildi ve **tek seferlik düzeltme yerine test** yazıldı
(`test_tool_registry.py::test_every_skill_tool_reference_resolves`).

## Yeni araç 1 — `production/bom_lifecycle.py`

Ağ **yok**: tedarik verisi `--supply` ile girdi olarak gelir (sorguyu ana
ajan yapar). Kapılar:

- NRND/EOL/Obsolete → **RED**, prototip istisnası yok (Bölüm 0).
- Tedarik verisi yok → **CONFIRM**, asla sessiz "Active"; ölçülmemiş risk
  `null`, **0 değil**.
- PCN: yayınlanmış PDN, veya EOL olup `last_time_buy`/`last_ship` bilinmiyorsa
  RED (şemanın kendi `$comment`'i).
- Alternatif: 9 alanlı `provenance` bloğunda tek bir `null` bile onu
  CONFIRM'e sabitler; `land_pattern=different` → RED + footprint geri
  besleme kenarı.
- Tazelik: teklif öncesi 7 gün, tasarım gözden geçirmesinde 30 gün; zaman
  damgası yok/okunamıyorsa **bayat sayılır, taze değil**.

**Çözülen çelişki (sessizce seçilmedi):** arşiv skoru ağırlıkların 1.0'ı
aşmasına bilerek izin veriyordu, şema ise `risk_score`'u `maximum: 1` ile
sınırlıyor. Yayınlanan sayı 1.0'a kırpıldı (sözleşme otorite) ama ham toplam
gerekçe metninde birebir korundu.

**Şema uyumu ölçüldü, okunmadı:** elle yazılan port iki yerde şema-DIŞI
çıktı — (1) filtrelenmemiş distribütör yükü `assembly_gates`'e fazladan
anahtar sızdırıyordu (`additionalProperties: false`), (2) tedarik verisi
olmayan koşuda `supply_checked_at: null` şemada karşılığı yoktu. İkincisi
**şemanın kendi iç tutarsızlığı**: `provider.queried_from` zaten
`"unavailable"` durumunu tanıyor ama zaman damgasının null seçeneği yoktu.
Uydurma zaman damgası yazmak, yapılmamış bir sorguyu yapılmış göstermek
olurdu — şema nullable yapıldı ve gerekçesi `$comment`'e yazıldı.

## Yeni araç 2 — `production/cad_assets.py`

LCSC'de olmayan ama DigiKey/Mouser'da stokta olan parça için bu pakette
hiçbir yol yoktu (`jlc_library` yalnız LCSC kataloğunu kapsıyor).

- **Pin sayısı kapısı** — sembol↔datasheet ve footprint↔sembol. İkincisi
  pratikte atlanan ve karta mal olan karşılaştırma: exposed pad'li varyantın
  sembolü + pad'siz varyantın footprint'i ERC'den de DRC'den de temiz geçer.
- Pin numaraları **yapısal** okunuyor (`common.sexpr` ağacı), arşivdeki gibi
  ham metinde `(number "..")` aranarak değil — regex belirtecin nerede
  durduğunu bilemez.
- Çok-üniteli/De Morgan tekrarları teke indiriliyor (ham `(pin` saymak 3 pinli
  parçayı 4 gösterip sahte FAIL üretiyordu); exposed pad **asla**
  filtrelenmiyor.
- `register_assets`, pin kapısı geçilmeden `PermissionError` fırlatır —
  atlanabilen kapı, kapı değildir. lib-table kaydı idempotent (yinelenen
  nickname'de KiCad projeyi hiç açmaz).
- İndirme **yapılmıyor**: `download_allowed()` yalnız URL'yi yargılar (https +
  dar host beyaz listesi), aktarımı ajan yapar. Bir test bunu dosyanın
  kendisinde doğruluyor (hiçbir HTTP istemcisi import edilmiyor) — "ağ
  bağımlı" reddi bu kez gerçekten geçersiz.

## Durum

57 yeni test (35 + 22), registry **65 araç**. Kendi kapsamımda hata yok.

**Bu turda kapsanmayan:** `common/field2d.py`, `scripts/field_solver.py`,
`schemas/field_solver_report.schema.json`, `tests/test_field2d.py` —
kullanıcının paralel yürüyen, henüz commit edilmemiş çalışması. Tam süitte
3 başarısızlık bunlardan geliyor (`test_field2d`'de 2 assertion +
`field_solver.py` registry'de kayıtlı değil). Dosyalara dokunulmadı,
commit'e alınmadı.

---

# Tur 9 — #47: kamera sayısı tek kaynağa bağlandı (2026-08-07)

Madde "kamera sayısını parametrik yap" diye yazılmıştı. Ölçüm başka bir şey
gösterdi: **`camera_count` zaten parametrikti** (`vc_id_assignment.py` onu
pozisyonel argüman olarak alıyor). Gerçek kusur, aynı sayının **üç ayrı
yerde** yazılı olmasıydı:

1. `camera_hub_interface.yaml` → `power_budget.board_count: 6`
2. aynı dosyada `vc_id.assignment`'ın uzunluğu (6 girdi)
3. Python imzalarında `connector_count: int = 6` /
   `deserializer_max_channels: int = 6` varsayılanları

Bir sayının üç kopyası, er geç üçünün ayrışması demektir — ve bu ayrışma
**sessizdir**: güç bütçesi altı karta göre sigorta seçerken hub denetimi dört
konnektör arar, ikisi de PASS raporlar. Kullanıcı kararı: varsayılanlar
kaldırılsın, konfigürasyon mevcut sözleşme dosyasında yaşasın.

## Yapılan

`camera_hub_interface.yaml`'a `system:` bloğu eklendi — `camera_count`
(2/4/6/12) ve `deserializer_max_channels`. İkisi bilerek **ayrı**: kanal
sayısı seçilen parçanın özelliği, kamera sayısı sistemin; `camera_count`'un
onu aşması `validate_plan`'ın raporladığı hatanın ta kendisi ve ayrı
tutulmaları o kontrolü anlamlı kılan şey.

- `power_budget.board_count` dosyadan **silindi**, `system.camera_count`'tan
  türetiliyor. Okunabilirlik için geri eklenirse `load_contract` yalnız
  **aynı değerde** kabul eder, farklıysa `ContractInconsistency` fırlatır.
- Yükleme anında tutarlılık doğrulaması: kanal aşımı, `vc_id.assignment`
  uzunluğu, `vc_id.range` genişliği, `camera_count`'un pozitif tamsayı
  olması. **Hiçbiri sessizce düzeltilmiyor** — dört kameralık bir VC-ID planı
  altı kameralık sisteme sessizce doldurulsaydı, kimsenin seçmediği üç strap
  ayarı uydurulmuş olurdu.
- `system:` yazılıp altı boş bırakılırsa (gerçekçi bir YAML yazım hatası)
  ne dosyayı ne bloğu adlandıran bir `TypeError` yerine açık mesaj.
- `check_hub_board(connector_count=6)` → varsayılan yok, sözleşmeden geliyor.
  Açık argüman **override olarak korundu** (yarı-dolu bring-up kartı gibi bir
  varsayımı denemek için) ve bunun kaldırılmadığı teste bağlandı.
- `generate_vc_id_plan` ve `vc_id_assignment.py` CLI'sinde
  `deserializer_max_channels` **zorunlu**. Eski varsayılan, sistemi kimsenin
  seçmediği bir sayıyla karşılaştırıyordu: 4 kanallı bir parça üzerindeki
  8 kameralık plan, bayrak unutulduğu sürece PASS veriyordu.

`device_tree_gen.py` zaten sözleşmeden okuyor, değişiklik gerekmedi.
Kalan `6` sabitleri yalnız `_self_test()` fixture'larında — veri, varsayılan
değil.

## Doğrulama

`system-planning`: **63 test OK**. Aralarında 2/4/6/12'nin dördünün de
yüklendiği, `board_count` çelişkisinin yakalandığı, ve iki kameralık bir
sistemde `check_hub_board`'un altı değil **iki** konnektör aradığı
regresyonlar var (eski varsayılanla o kart doğru olduğu halde FAIL alırdı).

Bu madde kıyaslama boşluğu **B2**'ye (artımlı yeniden koşum / tasarım yeniden
kullanımı) karşılık geliyor: aynı tasarım, tek bir sayı değiştirilerek farklı
kamera sayısında yeniden koşulabilir hale geldi.

**Kapsam dışı:** `pcb-agent/` tarafındaki eşzamanlı çalışma (field solver,
registry senkronu, yol haritası dosyası) bu commit'e alınmadı.

---

# Tur 10 — #50: Device Tree üreteci kısmi çıktıyı PASS sayıyordu (2026-08-07)

Madde "üreteci genişlet" diye yazılmıştı; kaydedilmiş tek gerekçe
"#47'den sonra" idi. İpucu oradaydı: üreteç artık **kaç kamera olması
gerektiğini** biliyor.

## BUG — altı kameranın ikisini açan overlay "geçti" veriyordu

Ölçüldü (uydurulmadı): sözleşmede `camera_count: 6`, ama yalnız iki kart
için `i2c_address_translation` girdisi var:

    6 kamera beyan, 2 girdi -> PASS | scanned: 2

`build_dts_entries_from_contract`, `i2c_address_translation` veya
`--i2c-bus-map` içinde bulamadığı kartı **sessizce atlıyordu** — docstring
bunu açıkça "silently SKIPPED (not an error)" diye yazıyordu bile. #47'ye
kadar bu savunulabilirdi: paydanın ne olduğunu kimse bilmiyordu. Artık
`system.camera_count` tek kaynak, yani eksik kapsam **ölçülebilir**.

Sonuç: sistemin üçte birini ayağa kaldıran bir overlay diske yazılıyor ve
tamam sayılıyordu.

## Kapatma

- Atlanan her kart **gerekçesiyle** dönüyor (`DtsBuildResult.skipped`):
  adres çevirisi yok / bus map'te yok / `system.camera_count` beyan ediyor
  ama `vc_id.assignment`'ta yok.
- `camera_count`'tan az düğüm üretilmişse **FAIL**, ve **fragment diske
  yazılmıyor** — yarım bir overlay diskte, tam olanından ayırt edilemez.
- `(bus, adres)` çakışması: `vc_id_assignment` adresleri sistem genelinde
  benzersiz tutuyor, ama çağıranın bus map'i ile adres planı ayrışırsa tek
  `&i2c3` altında iki `camera@40` düğümü çıkar — gerçeği tarif etmeyen bir
  device tree. Başka hiçbir yerin bakmadığı bileşim buydu.
- `data-lanes` artık **uydurulmuyor**. Dataclass'taki `= 2` varsayılanı
  kaldırıldı; değer `system.mipi_lanes_per_camera`'dan geliyor, yoksa
  `data-lanes` hiç yazılmıyor, yerine TODO düşüyor ve bulgu üretiliyor.
  Tahmin edilmiş bir lane listesi derlenir, uygulanır ve **görüntü
  vermez** — sessiz başarısızlığın ders kitabı hali. Gerçek değeri
  bilmediğim için `camera_hub_interface.yaml`'a **yazmadım**; araç şu an
  o boşluğu rapor ediyor (kısıt #3).
- `screen_dts_syntax()`: dengesiz parantez / eksik `};` taraması. Adı
  **bilerek** "screen" — `dtc` derlemesi değil, bu ortamda `dtc` yok.
  Bağlamaların doğruluğu, düğüm adlarının hedefte var olup olmadığı
  hakkında hiçbir şey söylemez.

Ambarella'nın kalıcı NO_COVERAGE garantisi ve "asla uydurma .dts yazma"
kuralı olduğu gibi korundu.

## Doğrulama

`system-planning`: **73 test OK** (63 → 73). Aralarında altı beyan/iki
tarif edilen fragment'ın FAIL verdiği ve **diske yazılmadığı**,
yapılandırılmamış lane sayısının yakalandığı, aynı adresin farklı
buslarda **meşru** sayıldığı ve yorum içindeki parantezin sayılmadığı
regresyonlar var.

---

# Tur 11 — #51: bir kural, üç kopya, iki farklı sayı (2026-08-07)

Madde "Faz 4 sabitlerini konfigürasyona taşı" diye yazılmıştı ve dekuplaj
mesafesindeki **çelişki** ayrıca işaretlenmişti. Ölçüm, çelişkinin
sanılandan bir kopya daha büyük olduğunu gösterdi:

| Dosya | Sabit | Değer |
|---|---|---|
| `scripts/board/decoupling_coverage.py` | `DEFAULT_MAX_DISTANCE_MM` | **3.0 mm** |
| `scripts/board/measure_board.py` | `--decoupling-max-mm` varsayılanı | **1.5 mm** |
| `scripts/sim/ngspice.py` | `DEFAULT_MAX_DECOUPLING_DISTANCE_MM` | **1.5 mm** |

Üçüncüsünün üstündeki yorum aynen şunu diyordu: *"kept as one source rather
than a second, independently-drifting constant"* — yani **tek kaynak
olduğunu iddia eden sabit, üçüncü kopyaydı** ve 3.0'la çelişiyordu.
`decoupling_coverage.py`'nin yorumu ise ikisinin "NOT reconciled"
olduğunu açıkça kabul ediyordu.

Pratik sonucu: bu paketin iki aracı **aynı kondansatör hakkında zıt karar**
verebiliyordu ve hangisine inanacağın hangisini koştuğuna bağlıydı.

## Çözüm — çelişki değil, iki farklı iş

İki sayı da gerçekti, farklı şeyleri kastediyorlardı; kodun kendi notları
bunu zaten yazıyordu: 3.0 mm *"a datasheet-typical upper bound"*, 1.5 mm
*"a tighter placement target"*. Bir **üst sınır** ile bir **hedef**
karıştırıldığı için çelişki gibi görünüyordu.

    decoupling.max_distance_mm    = 3.0   -> ihlal EŞİĞİ
    decoupling.target_distance_mm = 1.5   -> yerleşim HEDEFİ (raporlanır, ihlal değil)

**Davranış değişikliği, açıkça:** `measure_board` ve `ngspice` için hata
eşiği 1.5 → 3.0'a gevşedi, çünkü kaydedilen gerekçe 3.0'ı üst sınır
sayıyor. Proje 1.5'i zorunlu kılmak isterse artık **tek dosyada tek sayı**
değişecek; üç dosyada üç sabit aramak gerekmeyecek.

## `common/tuning.py` + `fixtures/references/tuning_defaults.json`

Emsal `fab_profiles()`: sayı bir veri dosyasında yaşar, çağrı anında
okunur, **gömülü yedeği yoktur** — sessizce ikame edilen bir eşik, bu
paketin var olma sebebi olan görünmez yanlış cevabın ta kendisi.

Taşınanlar: `rule_multiplier=3.0`, `violation_weight=5.0`, `via_cost=5.0`
(`grid_astar`), `ambient_temp_c=25.0` (`thermal_bridge`),
`_DROP_PCT_TIERS` (`ngspice`), dekuplaj limiti/hedefi.

Her girdi **gerekçesini taşıyor** ve gerekçe kodda yoksa bunu söylüyor:
`violation_weight` ve `via_cost` için *"no rationale was stated in the
code; recorded here as unexplained rather than given an invented one"*.
`ambient_temp_c` için de 25 °C'nin datasheet referans koşulu olduğu, bu
ürünün muhafaza ortamı **olmadığı** yazılı.

Yükleyici hiçbir eksikliği yutmuyor: tablo yoksa/şema sürümü yanlışsa,
anahtar yoksa, girdi gerekçesiz çıplak bir sayıysa, hedef limitten
gevşekse, ya da tier'lar sıralı değilse (arama İLK kapsayan tier'ı alır —
sırasız tablo sessizce yanlış kondansatör önerir) hata fırlatıyor.

## Doğrulama

11 yeni test (`test_tuning.py`) + `test_ngspice.py`'de değiştirilen
regresyon. İki **statik nöbetçi** eklendi: hiçbir modül kendi dekuplaj
mesafesi sabitini geri koyamaz, hiçbir imza `rule_multiplier`/
`violation_weight`/`via_cost` için literal varsayılan taşıyamaz — sapma,
birinin "kolaylık olsun" diye varsayılanı geri koyduğu an geri döner.

Silinen test: `test_default_distance_matches_measure_board`. Adı tam da
yanlış olan iddiaydı; yerine üç tüketicinin **aynı** sayıyı kullandığını
doğrulayan test geldi.

---

# Tur 12 — #54: reddedilmiş bir hesap, artık ölçülebilir (2026-08-07)

`sim/openems.py` Touchstone okuyabiliyordu ama **diferansiyel sonucu
bilerek raporlamıyordu**. Kendi docstring'i gerekçeyi yazıyordu:

> *"skrf.Network.se2gmm()'s port-pairing convention (which physical port
> maps to which mode index) could not be confirmed without skrf installed
> in the source environment, and a wrong guess reported as Sdd11 would be
> worse than not reporting it"*

**O ret doğruydu. Artık gereksiz:** `skrf` 2.0.1 kurulu, yani kural
tahmin edilmek yerine **ölçüldü**.

## Kural nasıl ölçüldü

İçinde iki ideal hat dışında hiçbir şey olmayan bir 4-port. `se2gmm`'in
AYNI çifte koyduğu iki portu birleştiren bir hat, çift 1 ile çift 2
arasında hiçbir şey taşıyamaz → `Sdd21` çöker; farklı çiftlere giden bir
hat sonlu bırakır:

    hatlar 1-2 ve 3-4:  Sdd21 = -600 dB   (ikisi de çift İÇİNDE)
    hatlar 1-3 ve 2-4:  Sdd21 =    0 dB   (ikisi de çiftler ARASI)
    hatlar 1-4 ve 2-3:  Sdd21 =    0 dB   (ikisi de çiftler ARASI)

Üçünü birden sağlayan tek eşleştirme: **portlar (1,2) çift 1, (3,4) çift
2** — yani `2k-1` ve `2k` portları `k`. Üçüncü satır `{1,4},{2,3}`
alternatifini eliyor.

İkinci doğrulama, kaydedilmiş bir çıktıya değil **kapalı forma** karşı:
Δ derecelik skew'de `|Sdc21| = |sin(Δ/2)|`, `|Sdd21| = |cos(Δ/2)|`.
Δ=20° için -15.206 / -0.133 dB; kütüphane 1e-3 dB içinde uyuyor. Yanlış
eşleştirme skew'e **kör** kalıyor (-600 dB'de duruyor) — bu yüzden test
bir duman testi değil, ayırt edici.

## Kayda değer: ilk okuma YANLIŞTI

Probe'un ilk etiketleri **hatların nerede olduğunu** anlatıyordu, hangi
**portların eşleştiğini** değil — ve ikisi birbirinin tersi. Modül o
yanlış iddiayla yazıldı; self-test saniyeler içinde yakaladı, çünkü
kütüphanenin döndürdüğüne değil **kapalı forma** bakıyor. Kütüphane
çıktısını kaydeden bir test hatayı dondururdu.

## `scripts/sim/sparam.py`

- `to_mixed_mode()` — eşleştirme **açıkça** verilir, portları kendisi
  permüte eder. Gerçek `.s4p` dosyaları numaralandırmada anlaşmıyor:
  bazıları UÇ'a göre (1,2 = bir uçtaki P/N — `se2gmm`'in istediği),
  bazıları İLETKEN'e göre (1,2 = P hattının iki ucu — sessizce çöken).
  İkincisi permüte edilmeden verilirse **kusursuz bir kanal ölü
  raporlanır**; bu tam olarak eski kodun raporlamayı reddetme sebebiydi.
- `write_touchstone()` — uzantı port sayısından **türetilir**; skrf
  formatı dosya adından seçtiği için `.s2p`'ye yazılan 4-port bir sonraki
  okumada sessizce yanlış ayrıştırılır.
- `cascade()` — yalnız 2-port kabul eder ve frekans ekseni uyuşmazlığını
  reddeder (skrf sessizce interpolasyon yapar, cevabı değiştirir).
- `check_differential_channel()` — Sdd21/Sdd11/**Sdc21**. Sonuncusu
  tek-uçlu denetleyicinin göremediği terim: pay içi skew ve asimetrik
  referans buradan görünür.
- **Kendi maskesi yok:** IL/RL/mod-dönüşüm limitleri çağıranın girdisi,
  çünkü kanal bütçesi arayüz standardının işi, genel bir aracın değil.
  (#53'ün TI SNLA224B maskeleri bu araca girdi olarak bağlanacak.)

## Doğrulama

19 test. Kritik olanlar kapalı forma karşı: skew↔mod dönüşümü üç farklı
açıda, kaskad kaybının dB'de toplanması, ve **fault injection** olarak
iletken-numaralı bir s4p'nin permüte edilmeden çöktüğü.

Kıyaslama boşluğu **A3** (S-parametre üretimi) kapandı; **#62** (göz
diyagramı) buna bağlıydı.
