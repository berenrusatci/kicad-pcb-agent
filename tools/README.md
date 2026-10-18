---
tags: [scripts, tools, verification]
durum: aktif
guncelleme: 2026-08-05
ust-harita: "[[skills/README]]"
---

# pcb-agent/tools

Skill'lerin başvurduğu **çalışan** referans kodu. Doküman değil, kanıtlanmış araç.

## Yerleşim

```
tools/
├── common/          paylaşılan modüller (hashing, units, findings, si_models,
│                    sexpr, paths, contract, report_io, kicad_cli, boardgeom, ids)
├── scripts/         saf-stdlib validator ve çözücüler
│   ├── board/       pcbnew gerektirenler (import hatası bu dizine hapsedilir)
│   └── sch/         kicad-cli + S-Expr şematik zinciri
├── servers/         MCP sunucuları
├── schemas/         JSON Schema'lar
├── tests/           tek discovery kökü
└── fixtures/        test verisi
```

Ortak kural (hepsi `common/report_io.py` üzerinden zorlanır): **zorunlu girdi
positional**, `--json` **daima yazar**, `--self-test` opt-in, stdout yalnız
makine-okunur JSON / stderr tüm teşhis, exit `0 = ok · 1 = fail · 2 = hata`.

| Dosya | Ne yapar | Skill |
|---|---|---|
| `scripts/sch/sch_wire.py` | KiCad 10 şematiğine gerçek `(wire)` / `(junction)` / label / power sembolü üretir; pin ankraj geometrisini (rotasyon+mirror+Y-flip) çözer; netlist ile bağlantı doğrular | [[skills/sematik-erc]] |
| `scripts/sch/sch_route.py` | **Pin→pin ortogonal router** (A*): sembol gövdelerinden kaçar, aynı net'i MST ile bağlar, yabancı net'e değmeyi yasaklar | aynı |
| `scripts/sch/rewire.py` | Bir şematiği baştan telle: mevcut netlist'i koruyarak **veya** hedef net tablosuna göre. Güç sembolü, PWR_FLAG, `no_connect` üretir; sonuçta netlist'i kanıtlar, tutmazsa `.bak`'tan geri alır | aynı |
| `scripts/sch/wireify.py` | Hafif yol: pin üstündeki label'ı dışarı taşıyıp araya tel koyar (netlist değişmez) | aynı |
| `tests/test_sch_wire.py` | Kanıt testi — netlist üzerinden doğrular, fault-injection ile testin boş olmadığını gösterir | aynı |
| `scripts/board/dfm_emc_check.py` | Board dosyasından **DFM/EMC nüans ölçümü**: teardrop sayısı, via-in-pad, annular ring, EPAD pasta kapsaması, kenar/depanel seramik keepout, hızlı via'nın dönüş via'sı, hızlı netlerin netclass'ı. Her bulguda `scanned` (kapsam) raporlar; `scanned=0` → `NO_COVERAGE`, sessiz PASS yok | [[skills/dfm]] · [[skills/emc-esd]] · [[skills/dogrulama-matrisi]] |
| `scripts/board/mask_dam.py` | Pin-arası kanalda maske barajı: `baraj = bakır_boşluk − expansion(a) − expansion(b)`; fab minimumunun altını ihlal sayar | [[skills/dfm]] · [[skills/highspeed-routing]] |
| `scripts/board/stitch_density.py` | `f_knee`'den λ/20 hedefi türetip kart kenarı ve via dönüş dikişi aralığını ölçer | [[skills/emc-esd]] |
| `scripts/zdiff_solver.py` | IPC-2141/Wadell diferansiyel mikroşerit/stripline; hedef Z için (W,S) ve stackup (h) taraması; self-test + fault-injection içerir. **`model_class: exploratory_closed_form`, `signoff: false`** — aşağıdaki uyarıyı oku | [[skills/empedans-stackup]] |
| `scripts/gate_receipt.py` | Kalite makbuzunu şema+sürüm, input/tool/config SHA-256, exact argv/exit, metric semantiği+kapsam, child gate indirgeme, promotion ve retention açısından doğrular; yalnız etkin `GREEN` için exit 0 verir | [[skills/pcb-orchestrator]] · [[skills/dogrulama-matrisi]] |
| `scripts/context_pack.py` | `pcb-context-pack/v1` manifestini doğrular: minimum karar girdileri, SHA-256 freshness, path sınırı, blocking soru, file/byte bütçesi, typed evidence provenance, dar tool yetkisi ve claim→evidence çıktı sözleşmesi; gate validator gerçek argv entrypoint'i değilse bloklar | [[skills/decompose-pcb-decisions]] · [[skills/pcb-orchestrator]] |
| `tests/test_context_pack.py` | 21-test preflight/postflight fault suite: blocker downgrade, duplicate core role, decision cycle/evidence, unbound evidence, tool registry, gate-entrypoint bypass, UTF-8, tipler, path/hash, claim/evidence ve gerçek gate exit | [[skills/decompose-pcb-decisions]] |
| `scripts/board/via_stub.py` | Hızlı-sinyal via'sının kullanılmayan barel/stub boyunu ve çeyrek-dalga rezonans ön-elemesini ölçer; kapsam yoksa PASS vermez | [[skills/highspeed-routing]] · [[skills/dogrulama-matrisi]] |
| `scripts/board/route_audit.py` | **Beyan edilen açıklık bakırda gerçekten var mı**: via bakır açıklığı (via iz DEĞİLDİR — geniştir), via/ped **delik** açıklığı (deliğin bakırdan ayrı kuralı vardır ve her katmanı deler), delik-delik, kart kenarı halkası. Kural kaynağı uydurulmaz: halka `board_outline.json` → `.kicad_pro`, ikisi de yoksa `NO_COVERAGE` ve o bir PASS **değildir**. İki dosya farklı halka beyan ederse `AYRISMA` basar ve **beyana göre** denetler. pcbnew istemez. **Kapsam dışı: iz-iz ve ped-ped bakır açıklığı** — buradaki GREEN temiz DRC demek değildir | [[skills/otonom-routing]] · [[skills/dfm]] |
| `scripts/crosstalk_thinning.py` | Paralel iz çiftinde **NEXT taraması + güvenli inceltme kararı**: `kx=1/(1+(D/H)²)`, `Kb=0.25·kx`, doyum uzunluğu ölçeklemesi. `SAFE_TO_THIN` / `CROSSTALK_RISK` / `THINNING_BLOCKED` / `NO_COVERAGE` döndürür; inceltilmiş genişliği `D'=S+W'` ile **yeniden** tarar. **`model_class: exploratory_closed_form`, `signoff: false`** — FEXT modellenmez | [[skills/crosstalk-thinning]] · [[skills/empedans-stackup]] · [[skills/emc-esd]] |
| `servers/crosstalk_mcp_server.py` | Yukarıdaki motoru `analyze_crosstalk_and_thinning` MCP tool'u olarak yayınlar. Saf stdlib stdio JSON-RPC (MCP 2024-11-05); `mcp` paketi gerekmez. Taşıma katmanı — hiçbir mühendislik kararı vermez | aynı |
| `tests/test_crosstalk_mcp.py` | 14 test: protokol (initialize/tools/list/notification/bozuk JSON) + sözleşme (risk asla inceltmez, fab tabanı, NO_COVERAGE, empedans bloğu, `signoff:false`, bilinmeyen parametre reddi). Şema anahtarlarını motorun imzasıyla birebir karşılaştırır | aynı |
| `scripts/research_promotion_gate.py` | `_research` claim tablolarını salt-okunur tarar; `KABUL` claim'inde HTTPS otorite ön-eleği, belge/revizyon+locator+erişim-tokenına bağlı tarih, A/B/C/D+L0..L3, path+SHA-256 bağlı validation fixture'ı, exact claim kolonlu karar/kanıt izi ve hedef skill'i zorunlu kılar. v2 receipt exact argv, tool/registry/input hash'lerini taşır; aday sorunları advisory, yaş eşiği yalnız açık CLI politikasıdır | [[skills/pcb-orchestrator]] · [[skills/dogrulama-matrisi]] |

> ### `zdiff_solver.py` sign-off aracı DEĞİLDİR
> Kapalı-form IPC-2141 denkleminin hatası geometriye göre **sistematik olarak**
> değişir; Polar'ın yayımladığı karşılaştırmada aynı denklem
> **−%29.94 ile +%2.41** arasında sapıyor (30.09 Ω yapıda −%29.94, 50.63 Ω'da
> −%2.31, 89.63 Ω'da +%2.41). Yani "hedefe yakın çıktı" bir doğruluk kanıtı değil.
> - Çıktıya `model_class: exploratory_closed_form` + `signoff: false` yaz.
> - Kontrollü empedansta **gerçek fab stackup'ı ile 2D field solver zorunlu kapı**.
> - Self-test'teki `measured_ohm = 184.0` referansının **kaynağı yok**
>   (`AÇIK MADDE`): ya provenance ekle (hangi kart/kupon/ölçüm, tarih) ya da
>   yayımlanmış referans vektörleriyle ve **beklenen hata payıyla** değiştir.
>   ±%10 testi geçmek üretim doğruluğunu kanıtlamaz.

## Çalıştırma

Testler tek komut. **pytest gerekmez** — takım `unittest` tabanlıdır
(`make test-pytest` opsiyonel bağımlılıkla aynı takımı koşar).

```bash
cd pcb-agent/tools
make test              # tüm takım
make selftest          # araçların gömülü self-test'leri
make registry-check    # config/tool_registry.json ↔ disk (drift'te exit 1)
make registry          # registry'yi yeniden üret + fixture hash'lerini yeniden çak
```

Tek tek araçlar (kökten, `cd pcb-agent`):

```bash
python3 tools/scripts/sch/rewire.py board.kicad_sch                    # dry-run rapor
python3 tools/scripts/sch/rewire.py board.kicad_sch --write            # netlist'i koruyarak telle
python3 tools/scripts/sch/rewire.py board.kicad_sch --nets hedef.json --write

python3 tools/scripts/gate_receipt.py gate_receipt.json                # rapor stdout'a
python3 tools/scripts/gate_receipt.py gate_receipt.json --json verify.json

python3 tools/scripts/context_pack.py context_manifest.json --root <project-root>
python3 tools/scripts/context_pack.py context_manifest.json --root <project-root> --postflight

python3 tools/scripts/crosstalk_thinning.py --trace-width-mm 0.20 --spacing-mm 0.10 \
    --dielectric-height-mm 0.20 --coupled-length-mm 60 --rise-time-ps 200
python3 tools/scripts/crosstalk_thinning.py --input-json girdi.json --json sonuc.json
python3 tools/scripts/crosstalk_thinning.py --self-test

python3 tools/scripts/zdiff_solver.py 90 --er 4.3 --json zdiff.json     # hedef Z positional
python3 tools/scripts/zdiff_solver.py --self-test

python3 tools/scripts/board/dfm_emc_check.py board.kicad_pcb --json checks.json
python3 tools/scripts/board/mask_dam.py board.kicad_pcb --json dam.json
python3 tools/scripts/board/stitch_density.py board.kicad_pcb --f-knee-ghz 2.33 --json stitch.json
python3 tools/scripts/board/via_stub.py board.kicad_pcb --data-rate-gbps 2.5 --json stub.json

python3 tools/scripts/research_promotion_gate.py --vault <vault-root> --json <vault>/_inbox/rapor.json
python3 tools/scripts/research_promotion_gate.py --vault <vault-root> --verify-report <vault>/_inbox/rapor.json
```

### Taşınırken değişen CLI sözleşmesi

| Önce | Şimdi | Neden |
|---|---|---|
| `crosstalk_thinning.py --json X` girdiyi **okurdu** | `--input-json X` okur, `--json X` **yazar** | 9 araç yazıyordu, 1 okuyordu; sessiz veri kaybı riski |
| `zdiff_solver.py --selftest` | `--self-test` (tüm araçlarda tek isim) | iki farklı yazım vardı |
| `zdiff_solver.py` hedefsiz çağrıldığında **exit 0** | hedef yoksa `parser.error` → **exit 2** | hiç iş yapmadan başarı bildiriyordu |
| `crosstalk_thinning`: `CROSSTALK_RISK`→1, `THINNING_BLOCKED`→0 | ikisi de non-zero | bloke edilmiş inceltme başarı değildir |
| `research_promotion_gate.py --vault` varsayılanı `parents[2]` | `--vault` **zorunlu** | varsayılan script'in yerini takip ediyordu; taşımadan sonra yanlış ağacı gösterdi |
| board araçları `open(p,"w")` (encoding yok) | `encoding="utf-8"`, `allow_nan=False` | UTF-8 dışı locale'de Türkçe bulgu metni patlıyordu; `NaN` geçersiz JSON |
| `stackup_plan.py`: ölçülmemiş kontrol varken **GREEN / exit 0** | ölçülmemiş kontrol varsa **NO_COVERAGE / exit 1** | tepe karar yalnızca `summary["FAIL"]`e bakıyordu; `summary["NO_COVERAGE"]` hiç okunmuyordu. Niyet dosyasında diff pair / anten / kristal / RF / termal bölümü yoksa o kontroller `scanned=0` gelir — tipik koşuda 9/10 kontrol ölçülmemişken rapor GREEN diyordu. Kural artık `common/findings.rollup_status()`. Ayrıca `design_rules` eksikken atlanan fab DFM kontrolü `checks`'e hiç eklenmiyordu (sayılamıyordu bile); artık açık bir NO_COVERAGE bulgusu olarak ekleniyor |
| `benchmark.py`: sadece **hiçbir** problem kapsanmadıysa NO_COVERAGE | **herhangi** bir problem kapsanmadıysa NO_COVERAGE / exit 1 | `if summary["covered_problems"] == 0` ya hep ya hiç bir kuraldı; 20 araçtan 19'u varken 1'i eksikse GREEN çıkıyordu. `problem_pass_rate` paydası KAPSANAN problemler olduğu için `--min-problem-pass-rate` de bunu yakalayamıyordu (oranın tanımı bilerek değiştirilmedi). Yeni sebep kodu: `PROBLEM_UNCOVERED` (hiçbir şey kapsanmadıysa yine `NOTHING_COVERED`) |
| `report/routing_table.py`: pcbnew varsa **GREEN / exit 0** | ölçülmeyen eksen varsa **NO_COVERAGE / exit 1** | verdict `outcome["success"]`ten türetiliyordu, o da yalnızca pcbnew import edilemediğinde False olur: hiç iz olmayan kart, routing'i yapılmamış netler ve `--drc-json` verilmediği için her satırı "N/A (no DRC report given)" olan DRC sütunu GREEN sayılıyordu. Ölçülen eksenler artık bulgu olarak yayımlanıyor (`outcome.checks`) ve karar `rollup_status()`ten geliyor; RED verilmiyor — routing/DRC kapısı `drc-gate` ve router'ın işidir |
| `independent_verify.py`, `pmic_allocation.py`, `board/reference_plane.py`, `production/assembly.py`, `board/thermal_bridge.py`, `sim/ngspice.py`: NO_COVERAGE → **exit 0** | altısında da NO_COVERAGE → **exit 1** | bu altı araç çıkış ifadesini `common/findings.board_exit_code()`'u çağırmak yerine elle kopyalamıştı, dolayısıyla o fonksiyonun belgelenmiş NO_COVERAGE muafiyetini devralmadılar; üçü ayrıca kendi docstring'iyle çelişiyordu. `board_exit_code()` ve onu çağıran araçlar bilinçli olarak DEĞİŞMEDİ (açık madde olarak izleniyor). |

### MCP sunucuları

`servers/crosstalk_mcp_server.py` (saf stdlib) ve `servers/board_mcp_server.py`
(dört salt-okunur board kontrolü, `board` argümanı path-sınırlı). `.mcp.json`:

```json
{
  "mcpServers": {
    "pcb-crosstalk": {
      "command": "python3",
      "args": ["pcb-agent/tools/servers/crosstalk_mcp_server.py"]
    },
    "pcb-board": {
      "command": "python3",
      "args": ["pcb-agent/tools/servers/board_mcp_server.py"]
    }
  }
}
```

Bilerek **yayımlanmayanlar**: `context_pack.py --postflight` (JSON'dan gelen
argv'yi `subprocess` ile çalıştırır — MCP'ye açmak uzaktan kod çalıştırmadır),
`rewire`/`wireify`/`sch_wire` (şematiği mutasyona uğratır) ve
`research_promotion_gate` (vault'a yazar, `KABUL` kararı insana aittir).

### `research_promotion_gate.py` CLI ve güven sınırı

```text
research_promotion_gate.py [--vault DIR] [--json REPORT]
                           [--verify-report STORED_REPORT]
                           [--check-live] [--timeout SECONDS]
                           [--max-source-age-days N]
exit 0 = terfi izi GREEN
exit 1 = accepted claim RED/STALE
exit 2 = giriş/çalıştırma hatası
```

Araç bir elektriksel iddianın doğru olduğuna karar vermez ve hiçbir
`SKILL-*.md`, board veya şematik dosyasına yazmaz. Domain allowlist yalnız
otorite ön-eleğidir; belge kapsamı ve `KABUL` kararı insana aittir. Kaynak TTL'i
varsayılmaz: `--max-source-age-days` verilirse bu değer proje politikasıdır,
vendor/standart gereksinimi değildir. `--check-live` yalnız URL erişilebilirlik
sinyali üretir; canlı URL içeriğin doğru veya değişmemiş olduğunu kanıtlamaz.
Vault içindeki `--json` hedefi yalnız `_inbox/*.json` olabilir; diğer yollar ve
`.json` dışı uzantılar fail-closed reddedilir. Rapor geçici dosya, `fsync` ve
atomik `os.replace` ile yazılır.

`pcb-research-promotion-report/v2`, exact argv ile çalışan tool SHA-256'sını,
registry dosyası+exact tool kaydını, karar günlüğü/kanıt haritası ve tüm
araştırma notlarının SHA-256'larını receipt'e bağlar. `--verify-report`, saklı
raporun kendi payload digest'ini ve güncel tool/registry/input/policy binding'ini
yeniden hesaplar; elle değiştirilmiş rapor `STORED_REPORT_TAMPERED`, bayat girdi
zinciri `STORED_REPORT_INPUT_STALE` ile exit 1 verir.

Karar ve kanıt tablolarında eşleşme yalnız exact `claim_id`/`claim` kolonundan
yapılır; başka hücrede geçen claim metni kanıt sayılmaz. Erişim tarihi yalnız
`erişim|accessed` token'ından sonra gelen ISO tarihtir. Validation hücresi
`TBD`, `N/A`, `manual`, `-`, `?`, `CONFIRM` veya `NO_COVERAGE` olamaz; vault
içinde çözülen fixture/validator yolu ile o dosyanın exact SHA-256'sını taşır.

Fault-injection regresyonu kabul edilmiş claim'den source locator, karar
günlüğü, kanıt haritası veya seviye/döngüyü tek tek kaldırır; araç her kusurda
`RED/exit 1` üretmelidir. Candidate notlarda aynı kusurlar promotion'ı bloke
etmez, görünür `ADVISORY` olarak kalır.

Commitli uçtan uca fixture:

```bash
python3 tools/scripts/context_pack.py \
  tools/fixtures/context_pack/projecte_ready/context_manifest.json \
  --root . --postflight
```

Komutu `pcb-agent/` kökünden çalıştır. Beklenen sonuç: preflight `READY`,
postflight `ACCEPTED` ve `FIXTURE-G1 exit_code=0`.
`--force` yalnızca **scratch kopya** içindir: KiCad-açık kontrolünü atlar.

### `gate_receipt.py` CLI ve sözleşme

```text
gate_receipt.py RECEIPT [--root DIR] [--json REPORT]
                           [--require-fault-injection]
exit 0 = güncel ve etkin GREEN
exit 1 = RED/FAIL, NO_COVERAGE, INVALID_METRIC veya STALE
exit 2 = bozuk JSON, desteklenmeyen şema veya sözleşme/çalıştırma hatası
```

Kanonik `schema_version` değeri `gate-receipt/v1`'dir. Eski üst-alan adı
`schema` salt-okunur alias olarak normalize edilir ve uyarı üretir; iki alan
çelişirse `ERROR`. Eksik alan **asla çıkarımla doldurulmaz**.

Zorunlu kanıt: `gate_id`, `gate_kind|profile`, `verdict|status`, tool
`name/version/path/sha256`, exact `argv`, UTC başlangıç/bitiş, `exit_code`,
role+path+SHA-256 bağlı `inputs`, path+SHA-256 bağlı `config|rules`, typed
`parsed_metrics`, her metrik için `definition`, `units`, `distance_semantics`,
`region`, `sampling_step`, `geometry_model`; ayrıca
`coverage.scanned/eligible/excluded`, blockers,
child receipt kimliği, retention ve A-seviyesinde fault-injection sonucu.
Her `parsed_metrics` leaf yolu tam **bir** semantic kaydına bağlanır; eksik,
fazla veya inline+ref çakışması `INVALID_METRIC` olur. Dış semantic yalnız
`{metric_id,path,sha256}` ref nesnesiyle kabul edilir. Hash-bağlı JSON aynı
`metric_id` ile `semantics` nesnesini ve altı zorunlu alanı taşımalıdır; düz
string ref kabul edilmez.
Metric leaf yalnız sonlu JSON number'dır; `bool`, `null`, string, list ve
nesne-değerli metric kabul edilmez. `coverage` aritmetiği zorunludur:
`scanned <= eligible` ve `scanned + excluded == eligible`; aksi RED
`INVALID_COVERAGE`. Opsiyonel `coverage.per_metric` verilirse tüm metric leaf
ID'lerini birebir kapsar ve her satır aynı aritmetiğe uyar.

A-seviyesi `fault_injection`, hash-bağlı `fixture`, receipt input hash'ine
bağlı `baseline_artifact`, farklı hash'li `injected_artifact`, exact
`command.argv/exit_code`, `expected_failed_metrics` ve `observed_failures`
taşır. Failure ID'lerinin tamamı mevcut parsed metric leaf'leri olmalı;
uydurma metin PASS olamaz.

Receipt, child, tool, input, config, semantic ref, promotion ve fault artifact
yolları receipt dosyasının dizinine göre çözülür ve `--root` (verilmezse ana
receipt dizini) altında kalır.
Mutlak veya `../` ile root dışına çıkış `PATH_OUTSIDE_ROOT/ERROR`; external
allowlist bu sürümde bilerek yoktur. `--json` raporu stdout GREEN basılmadan
önce yazılır; yazma hatası structured `REPORT_WRITE_ERROR`, exit 2 üretir.

İndirgeme deterministiktir: zorunlu child `FAIL`, `NO_COVERAGE`,
`INVALID_METRIC`, `STALE` veya `ERROR`; eksik child veya hash uyumsuzluğu
birleşik kapıyı RED yapar.
Advisory child RED görünür uyarı olarak kalır ama tek başına kapıyı kapatmaz.
DRC/ERC'de `--exit-code-violations` ve `--severity-all` yoksa veya parse edilen
ihlal sayısı sıfırdan büyükse tool exit 0 olsa bile RED'dir. GREEN promotion,
scratch/source/target hash'leri, tam komut+exit code ve `atomic: true` ya da
exact `--atomic` komut kanıtı ister. RED promotion gerekçe kodu ister.
RED promotion ayrıca her canonical target için
`before_sha256 == after_sha256 == sha256(current)` zincirini kanıtlar; fark,
kanonik dosyaya dokunulduğunu gösteren `RED_PROMOTION_MODIFIED_CANONICAL` olur.

Fixture ve regresyon:

```bash
python3 -m unittest -v tests.test_gate_receipt   # veya: make test
```

`tests/fixtures/gate_receipt/`, gerçek `usb-hs-breakout/evidence/22..25`
şeklinin minimal, hassas-olmayan staging kopyasıdır. `provenance.json` kaynak
göreli yollarını ve kaynak SHA-256'ları taşır; `synthetic: false`. Testler bütün
fault'ları yalnız geçici staging kopyalarına enjekte eder; canlı vault'a yazmaz.

## Son doğrulanmış koşular (2026-07-27, kicad-cli 10.0.5)

`test_sch_wire.py`:
```
[1] üretilen wire sayısı: 35
[2] netlist doğrulaması: 26 net bekleniyor, 0 problem
[3] unconnected-* net sayısı: 0
[4] fault-injection: 1 problem (>0 OLMALI)
[5] ERC: 0 error, 24 toplam (0 beklenmeyen uyarı)   # 24'ü de isolated_pin_label (fixture gereği)
PASS
```
Kapsam: 4 rotasyon × 3 mirror = 12 sembol yönelimi, pin→pin ortogonal wire,
T-junction'lı güç rayları, `power:GND` + `power:PWR_FLAG`, `no_connect`.

`rewire.py` + `usb-hs-breakout/nets_fixed.json` (scratch kopyada):
```
nets: 8 · routed_nets: 7 (GND güç sembolüyle) · wire_segments: 41
unroutable: 0 · netlist_identical: True (hedef tabloya göre)
ERC: 0 error, 1 warning (lib_symbol_mismatch — karta ait, önceden var)
```

`dfm_emc_check.py` (2026-07-29, RGMII-USB-Breakout, KiCad 10.0.5):
```
temiz kart:  PASS 3 / FAIL 2 / NO_COVERAGE 2
  FAIL epad_paste_coverage : U3.2, U4.2 (SOT-223 tab, pasta margin 0.0 → oran 1.0)
  FAIL hs_netclass_assigned: 17 hızlı net Default netclass'ta
fault-injection (scratch kopya): 3 kusur enjekte → 3'ü de yakalandı
  U3.1 pad merkezine via  → via_in_pad            0 → 1
  0.30/0.25 mm via        → annular_ring          0 → 2
  C5 kenardan 0.87 mm     → edge_keepout_ceramics 0 → 1
```

`gate_receipt.py` (2026-07-29, stdlib Python):
```text
42 test: OK
pozitif gerçek-şekilli USB receipt → GREEN / exit 0
fault'lar → STALE, NO_COVERAGE, INVALID_METRIC veya RED / exit 1
bilinmeyen/çelişkili şema → ERROR / exit 2
```

### Açık sertleştirme adayları — bu sürümün sözleşmesi değil

- Receipt/digest için RFC 8785 JCS veya açık eşdeğer byte canonicalization;
  duplicate JSON key ve tüm receipt boyunca non-I-JSON sayı reddi; key-order ve
  whitespace bağımsız digest fixture'ı.
- Secret dump etmeden dar environment/materials provenance: Python, KiCad CLI,
  pcbnew, git commit, cwd, env allowlist ve `materials[]` hash'leri.
- Legacy migration receipt'i: source/target schema, migration-tool SHA ve
  before/after canonical SHA.

Tam Sigstore/Rekor/SLSA L2–3 ve günlük gate imzası kapsam dışıdır. Receipt ile
artifact birlikte kötü niyetli değiştirilirse, imzalı/harici trusted anchor
olmadan tarihsel özgünlük kanıtlanamaz; mevcut validator'ın güven sınırı budur.

## pcb-tool-v2'den gelen araçlar

`projectE-2archive/pcb-tool-v2` birleştirmesiyle eklendi. Her biri bu paketin
sözleşmesine **uydurularak** alındı (positional zorunlu girdi, `--json` yazar,
`--self-test` opt-in, stdout=JSON, exit 0/1/2, `schema_version`+şema). Kaynak
dosya ve hash'leri: `docs/PROVENANCE.json`.

### IPC hesaplayıcıları ve kural motorları — saf stdlib
| Araç | Ne yapar |
|---|---|
| `scripts/ipc/trace_width.py` | IPC-2152 akım→iz genişliği, iç katman deratingi |
| `scripts/ipc/clearance.py` | IPC-2221 gerilim→clearance, kaplamalı/kaplamasız |
| `scripts/ipc/fab_class.py` | IPC-6012 Sınıf 1/2/3 üretim limitleri |
| `scripts/ipc/dfa.py` | IPC-A-610 montaj/DFA açıklıkları |
| `scripts/ipc/via_current.py` | IPC-2221 via delik/pad çapı; **sabit A/via katsayısı yok** |
| `scripts/dru_emit.py` | Yukarıdakileri uygulanabilir `.kicad_dru`'ya çevirir |
| `scripts/emc_rules.py` | Knee frekansı / iz-anten EMI kuralları |
| `scripts/stackup_plan.py` | Katman sayısı kararı + dizilim + PI/crosstalk taraması |

> `dru_emit.py` **saf stdlib'dir** (pcbnew istemez), o yüzden `scripts/board/`
> değil `scripts/` altındadır. Yalnız `track_width`/`clearance`/`annular_width`
> kısıtları doğrulanmıştır; diğerleri `verified_constraint: false` taşır ve
> üretilen dosyanın kendi yorumuna da yazılır.

### Board ölçerler — pcbnew veya kicad-cli
| Araç | Ne yapar |
|---|---|
| `scripts/board/gerber_dfm.py` | Gerçek RS-274X gerber'lerden DFM — sembolik DRC'den **bağımsız** kanıt |
| `scripts/board/collision_radar.py` | Yerleşim çarpışma radarı (JSON); yerleşim doğruluğu görselden değil buradan |
| `scripts/board/mcad_collision.py` | STEP export ile 3D gövde/kasa çarpışması; gövdesi olmayan parça "test edilmedi" sayılır |
| `scripts/board/thermal_bridge.py` | Isı emici temas yüzeyi, B.Mask açıklığı, termal bariyer yarığı — **board'a YAZAR** |
| `scripts/board/measure_board.py` | Vendor'lanan L0 ölçüm aracı (uzunluk/çift/pad) |
| `scripts/board/pair_coupling.py` | Vendor'lanan kuplaj ölçümü; `0/0` → NO_COVERAGE |
| `scripts/drc_gate.py` | Vendor'lanan DRC kapısı — `drc.sh`'ten Python'a taşındı. Her rapor `provenance.engine_version`'ı (`kicad-cli version` ile **ölçülür**, koda gömülmez) ve yol içermeyen `argv_signature`'ı taşır; `--compare A.json B.json` iki raporu ancak aynı sürüm **ve** aynı bayraklarla üretilmişlerse karşılaştırır, aksi hâlde REDDEDER (çıkış 2) |
| `scripts/independent_verify.py` | DRC'den bağımsız ikinci doğrulayıcı |

### Yerleşim / yönlendirme / üretim / simülasyon
| Araç | Ne yapar | Ek bağımlılık |
|---|---|---|
| `scripts/place/force_directed.py` | Ratsnest ağırlıklı kuvvet-yönelimli yerleşim önerisi | — |
| `scripts/route/grid_astar.py` | Son çare saf-Python A*; arama düğüm/süre/bbox ile **sınırlı** | — |
| `scripts/route/topological.py` | Topolojik yol bulma + tek adım shove | pcbnew (lazy) |
| `scripts/production/freerouting.py` | FreeRouting zinciri (DSN→SES) | **java + freerouting.jar** |
| `scripts/production/outputs.py` | Fab çıktıları; öncesinde DRC/ERC tekrar koşar | **KiBot** |
| `scripts/production/assembly.py` | Üretim sözleşmesi kapısı | — |
| `scripts/sim/ngspice.py` | SPICE netlist → DC/AC → IR-drop PASS/FAIL | **ngspice** |
| `scripts/report/cross_section.py` | Board bölgesi görsel kesiti | **svglib+reportlab+Pillow+poppler** |

> `production/*` ve `sim/ngspice.py` testlerinde java/freerouting/KiBot **gerçekten
> çalıştırılmadı** — bu testler `*_binary_not_exercised` adını taşır, mock'lanmış
> bir PASS köprünün çalıştığının kanıtı sayılmaz. `ngspice` ise gerçek ikiliyle
> (ngspice-46) doğrulandı.
>
> `cross_section.py` bu pakette **stdlib-only özelliğini kıran tek modüldür**;
> bağımlılıkları yalnız render fonksiyonunun içinde lazy import edilir, eksikse
> traceback değil tek satırlık eylem önerisi ve geçerli bir JSON rapor verir.
> Çıktısı `visual_guidance_only: true` / `measurement_admissible: false` taşır —
> **hiçbir kapı buna dayanamaz**.

### Terfi kapısı ve hafıza
| Araç | Ne yapar |
|---|---|
| `scripts/promote.py` | scratch→kanonik terfi; **`gate_receipt.py`'nin GREEN doğruladığı** makbuzu üretir |
| `scripts/memory/error_memory.py` | Kalıcı ERC/DRC/DFM hata hafızası + geri getirme |
| `scripts/memory/recovery.py` | pcbnew/MCP ölürse katmanlı kurtarma merdiveni |

`promote.py` üreticidir, `gate_receipt.py` doğrulayıcı — ikisi aynı sözleşmede
buluşur ve `promote.py` kendi çıktısını doğrulayıcıdan geçirip verdikti raporlar,
böylece üretici/doğrulayıcı ayrışması çalışma anında gürültülü şekilde patlar.
Hafıza dosyaları `pcb-agent/var/memory/` altına yazılır; `.obsidian` ağacı içine
düşen her yol **fail-closed** reddedilir (vault'ta tasarım dosyası tutulmaz).

## Bağımlılık

| Katman | Gereksinim |
|---|---|
| `common/`, `scripts/*.py`, `servers/crosstalk_mcp_server.py` | yalnız stdlib |
| `scripts/sch/*` | stdlib + `kicad-cli` (saf S-Expr, `pcbnew` gerekmez) |
| `scripts/board/*`, `servers/board_mcp_server.py` | **`pcbnew`** (KiCad 10.0.5) |
| `scripts/ipc/*`, `scripts/emc_rules.py`, `scripts/dru_emit.py`, `scripts/place/*`, `scripts/route/grid_astar.py` | yalnız stdlib |
| `scripts/sim/ngspice.py` | **ngspice** ikilisi |
| `scripts/production/freerouting.py` | **java** + `freerouting.jar` |
| `scripts/production/outputs.py` | **KiBot** + pcbnew |
| `scripts/report/cross_section.py` | **svglib, reportlab, Pillow, poppler** (lazy; tek stdlib kırıcı) |
| test takımı | stdlib `unittest`; `pytest` opsiyonel |

`pcbnew` sistem `python3`'ünden içe aktarılabiliyor (10.0.5 ile doğrulandı) —
eski README'nin dayattığı ayrı venv yorumlayıcısı **artık
gerekli değil**. Sembol kaynağı: `/usr/share/kicad/symbols/*.kicad_sym`.

## KiCad 10 otomasyon yüzeyi — üç ayrı yol, karıştırma

Her script'e `backend:` etiketi koy. Hangi yüzeyin ne yapabildiği farklı:

| backend | Ne için | Sınırı |
|---|---|---|
| `cli` | **CI, ERC/DRC, bütün export'lar** | Varsayılan tercih. Headless çalışır. |
| `file` | Saf S-Expr okuma/yazma (`sch_*`, `rewire`) | KiCad'in dosya formatına bağımlı |
| `ipc` | **Çalışan pcbnew GUI** oturumunu canlı mutasyon (`kicad-python`/kipy) | Açık GUI + API server **zorunlu**; KiCad 9/10'da headless yükleme/export **yok** (KiCad 11 hedefi) |
| `swig` | Headless nesne-düzeyi board mutasyonu (`pcbnew` modülü) | **Teknik borç**: KiCad 9'dan beri deprecated, kaldırma planı **KiCad 11** |

- SWIG script'leri KiCad **10.0.x'e pinle**, üzerine yeni özellik ekleme.
  IPC eşdeğeri olmayan headless işler için açık madde:
  `CONFIRM: KiCad 11 headless IPC kapsamı ve kullanılan nesneler.`
- Geçiş testi aynı board'da **item/net/DRC metriklerini birebir** karşılaştırmalı.
- Yeni API adı yazacaksan **yalnız resmî `kicad-python` dokümanından** al;
  uydurma API ismi bu projedeki en büyük hasar kaynağı.

### `kicad-cli` kapı sözleşmesi
- ERC/DRC'de **`--exit-code-violations` olmadan ihlal exit code'a yansımaz** →
  kapı sahte PASS verir. Her kapıda kullan.
- DRC'de gerektiğinde: `--schematic-parity --severity-all --refill-zones`.
  (Bayrak yoksa parity listesi **hep boş** gelir — [[skills/pcb-orchestrator]] §7.)
- `pcb export` yüzeyi geniş: `drill, dxf, gerbers, glb, ipc2581, ipcd356, odb,
  pdf, pos, step, stl, svg, ...`; `sch export`: `bom, netlist, pdf, svg, ...`.
  HPGL komutları KiCad 10'da uyumluluk için görünür ama **işlevsizdir**.
- `jobset`: standart fab paketini tek hedefte üretme adayı — KiBot ile örtüşür,
  çekirdeği değiştirmeden önce ikisinin çıktısını hash/semantik farkla ölç.
