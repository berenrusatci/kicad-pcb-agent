---
name: pcb-orchestrator
description: Otonom PCB akışını Faz 0→4 design-to-release DAG'ı + Faz 5A/5B fiziksel kapanış olarak yürütür; adımları tetikler, artifact doğrular, board-yazımı kilidini tutar, kalite kapılarını ve release-health'i gate_receipt ile kapatır. Bağlamı adım ajanlarına dağıtır ve ham çıktı değil özet alır.
version: 1.1.0
last-reviewed: 2026-08-10
phase: "Tüm fazlar (0→4 + 5A/5B) — üst seviye yürütücü"
assurance_loop: [L0, L1, L2, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: design_intent.json, schema: ../decompose-pcb-decisions/references/decision_units.schema.json, required: true}
  - {artifact: decision_units.json, schema: ../decompose-pcb-decisions/references/decision_units.schema.json, required: true}
  - {artifact: config/tool_registry.json, schema: null, required: true}
outputs:
  - {artifact: gate_receipt.json, schema: references/gate_receipt.schema.json}
  - {artifact: promotion_receipt.json, schema: references/gate_receipt.schema.json}
writes_board: false
tools: [context-pack, gate-receipt, research-promotion-gate, sync-tool-registry, sync-skills, promote, error-memory, priors, capability-inventory, pipeline-adapters]
---

# SKILL: PCB Orkestratör

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Faz DAG'ı ve artifact akışı: [[references/phase-dag.md]] ·
Board yazımı kilidi ve transaction: [[references/board-write-lock.md]]

Sen üst-seviye orkestratörsün. **Tüm kuralları yükleme**; DAG + artifact
özetleri tut. Her adımı **kendi skill'i + yalnızca girdi artifact'leriyle** ayrı
ajana ver; ana bağlama ham çıktı değil özet al.

## 0. Ne zaman çağrılır

- Bir PCB tasarım akışı başlarken veya bir fazın kapısı açılacakken.
- Bir alt-ajan tamamlandığında (postflight + receipt doğrulaması için).
- Bir upstream artifact değiştiğinde (stale yayılımı ve descendant yeniden açma).
- **Dış inceleme geldiğinde bu skill değil** [[../claim-audit/SKILL|claim-audit]]
  çalışır.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| `design_intent.json`, `decision_units.json` | [[../decompose-pcb-decisions/SKILL|decompose]] | DAG düğümü açılmaz |
| `context_manifest.json` (worker başına) | decompose §2.3 | worker açılmaz |
| tool adı + hash + `available` | `config/tool_registry.json` | kayıt dışı tool → worker açılmaz |
| child `gate_receipt.json` | adım skill'leri | parent GREEN olamaz |
| board hash | kanonik dosya | stale kontrolü yapılamaz |

Kapı makbuzu şeması:
[`references/gate_receipt.schema.json`](references/gate_receipt.schema.json)

## 2. Fazlar

Tam DAG, adım tabloları, artifact akışı ve geri besleme kenarları:
[[references/phase-dag.md]]

| Faz | Ne olur | Bariyer mi |
|---|---|---|
| **0** | Karar ve context kapısı: `design_intent`, `decision_units`, worker preflight/postflight | ✅ |
| **1** | Temeller & tedarik (güç, mekanik, BOM → sonra konnektör) — **paralel** | ➖ |
| **2** | Elektriksel & fiziksel altyapı (stackup, koruma topolojisi, şematik, DFT planı) | ➖ |
| **3** | Seri kilit & fiziksel yönlendirme (DFM+yerleşim → routing → EMC uygulaması) | ✅ **BARİYER** |
| **4** | Doğrulama, paketleme, çıkış kapısı (4a paralel → 4b birleşik gate → 4c CPL) | ✅ |
| **5A** | Üretim dönüşü: first-article / as-built kabulü — seri montaj açılır ya da bloke edilir | ✅ |
| **5B** | Fiziksel L3 döngüsü: bring-up + karakterizasyon, D satırlarının kanıt kapanışı | ✅ |

> **Faz 0→4 bir design-to-manufacturing-release DAG'ıdır; L3 onun içinde
> değildir.** Ortak sözlük L3'ü açıkça "kart geldikten sonra" diye tanımlıyor
> ([[../_shared/coverage-gate.md]]), Faz 4 ise final read-only check + birleşik
> gate + Gerber/CPL ile bitiyor. L3'ü Faz 4'ün içine sıkıştırmak, henüz fiziksel
> kart yokken fiziksel kabul varmış gibi görünmesine yol açar. Bu yüzden 5A ve
> 5B **ayrı fazlardır** ve `writes_board: false`'tur.

## 3. Kalite kapıları (geçilmeden sonraki faz açılmaz)

- **Faz 0 → 1:** `context_pack.py` exit 0 + `READY`; blocking soru yok; tüm
  zorunlu girdilerin SHA-256'sı güncel; tool'lar registry'de kayıtlı.
- **Faz 2 → 3:** `erc.json` error = 0 **ve** `verify_nets()` farkı 0 **ve**
  `unconnected-*` = 0 **ve** şematikte `(wire` sayısı > 0 (uçan-net şematik
  kabul edilmez) **ve** `sematik_riskler.md` her satırı kapalı.
- **Faz 3 → 4:** DRC 0 ihlal (repo rule set), unrouted 0, yüksek hız netlerinde
  90° köşe = 0, pin-arası izlerde maske barajı karar bandında, skew **ps
  cinsinden** bütçe altında, `parallelism_coverage` **tolerans + örnekleme
  adımıyla** raporlanmış.
- **Faz 4 çıkış:** fab rotasyon-map'i versiyonlu, TP kapsamı güç+debug %100,
  F.Paste açıklıkları EPAD'de indirgenmiş, DNP listesi BOM+CPL'de tutarlı.
- **Her fazda:** lifecycle riski taze (≤ eşik).

**DRC/unconnected/parity sıfırını tek başına sign-off sayma.** Uygulanabilir her
board için receipt ayrıca empedans geometrisi, katman dağılımı, referans-düzlem
kapsamı, `parallelism_coverage`, mask dam ve açık blocker listesini taşısın.
Bir alt-kapı FAIL ise birleşik verdict RED olur.

### "Temiz araç çıktısı" kapı değildir
ERC/DRC'nin yapı olarak göremediği kararlar her fazda ayrı bir dosyada kapanır,
aksi halde faz açılmaz:

- **Faz 1:** kapasite değerleri **DC-bias sonrası efektif**
  ([[../guc-isil/SKILL|guc-isil]] §3b); akım hesabının hangi standarda
  (2221/2152) göre olduğu etiketli.
- **Faz 1:** her konnektör için mating-cycle/kaplama ve **derating sonrası** akım
  kontrolü yapılmış; eşleşme koridoru + aktüatör hacmi keepout'a girmiş
  ([[../konnektor/SKILL|konnektor]]).
- **Faz 2:** `sematik_riskler.md` (floating giriş, strap, I²C pull-up hesabı,
  kutup, kristal CL — [[../sematik-erc/SKILL|sematik-erc]] §5) her satır kapatılmış.
- **Faz 2:** hedef empedans fab'a **Z + tolerans + referans katman** olarak
  verilmiş, coupon istenmiş ([[../empedans-stackup/SKILL|empedans-stackup]] §3a).
- **Faz 2→3:** kart-dışına çıkan her arayüzde ESD zinciri (`esd_chain.json`,
  `V_clamp@I_PP` sayısıyla) ve filtre/choke yeri **fiziksel olarak ayrılmış**;
  EMC hesapları **`f_knee_edge`**'den türetilmiş ([[../emc-esd/SKILL|emc-esd]]).
- **Faz 3:** via-in-pad varsa IPC-4761 Type VII fab notu yazılı; depanel/kenar
  keepout'u seramiklere uygulanmış ([[../dfm/SKILL|dfm]]).
- **Faz 3:** her paralel segment için crosstalk taraması koşulmuş; taranmamış
  segment `excluded` + gerekçeli ([[../crosstalk-thinning/SKILL|crosstalk-thinning]]).
- **Faz 5A:** released paketten üretilen ilk kartın as-built konformansı
  kapanmış ([[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]]);
  seri üretim bundan önce açılmaz.
- **Faz 5B:** zorunlu D satırlarının fiziksel kanıtı toplanmış ve limite göre
  karara bağlanmış
  ([[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]]).

### Release-health: üretilmiş dosya ve sayaç drift'i kapıya bağlanır

Bu, **yeni bir "repo-health" skill'i değildir** ve olmamalıdır: orkestratör
zaten preflight/test/registry yürütme otoritesidir; ikinci bir yürütücü sessiz
otorite çatışması yaratır ([[../_shared/rule-conflicts.md]] §9'daki aynı
gerekçe). Eksik olan sahip değil, **mevcut testlerin zorunlu release kapısı
olarak uygulanması**.

Ölçülmüş arıza sınıfı (`../../docs/tool-inventory.md:311-325,339-366`): üretilmiş
dosyalar (`config/tool_registry.json`, `docs/YETENEK_ENVANTERI.csv`/`.xlsx`) ve
anlatı içindeki sayaçlar birbirinden bağımsız bayatlıyor; hiçbiri kapıya bağlı
olmadığı için kimse kırmızı görmüyor. Bir tur "16 skill" derken başka bir dosya
"18 skill" diyordu; envanter "83 registry / 78 anılan" derken doğrudan ölçüm
86/81'di.

**Kapı — dördü de koşmadan release receipt'i GREEN olmaz:**

| # | Kontrol | Komut | RED anlamı |
|---|---|---|---|
| 1 | registry ↔ disk parity | `sync_tool_registry.py --check` | kayıtlı araç bayat/eksik |
| 2 | dört yön (A/B/C/D) | `python3 -m unittest tests.test_tool_registry` | diskte olup kayıtsız araç, skill'in adlandırdığı olmayan araç, **hiçbir skill'in anmadığı araç** |
| 3 | tam paket | `python3 -m unittest discover -s tools/tests -t .` | herhangi bir regresyon |
| 4 | sayaç tazeliği | ölçüm komutları aşağıda | anlatı sayısı ≠ ölçülen sayı |

```bash
# 4 — sayaçlar elle kopyalanmaz, ölçülür
ls pcb-agent/skills/*/SKILL.md | wc -l           # skill sayısı
ls pcb-agent/tools/tests/test_*.py | wc -l       # test dosyası sayısı
python3 - <<'PY'                                  # registry / anılan / sahipsiz
import json, re, pathlib
reg = json.load(open("pcb-agent/config/tool_registry.json"))
names = {t["name"] for t in reg["tools"]}
cited = set()
for p in pathlib.Path("pcb-agent/skills").glob("*/SKILL.md"):
    m = re.search(r"^tools:\s*\[(.*?)\]", p.read_text(encoding="utf-8"), re.M | re.S)
    if m:
        cited |= {x.strip() for x in m.group(1).split(",") if x.strip()}
print(len(names), len(cited & names), sorted(names - cited))
PY
```

Üç sert kural:

1. **`sync_tool_registry.py --check` GREEN, paket GREEN demek DEĞİLDİR.**
   `--check` yalnız *kayıtlı* araçları ağaca karşı doğrular; diskte olup
   registry'de olmayanı göremez. Ters yönü `test_every_entrypoint_script_is_registered`
   kapatır. 2026-08-07'de bu fark depoyu bir gün kırmızı bıraktı.
   **Bir tur tam paket koşmadan kapatılmaz.**
2. **Üretilmiş dosya elle düzenlenmez.** `.gitattributes` bu dosyaları
   `-merge` ile işaretler; çözüm satır birleştirmek değil **yeniden
   üretmektir** (`sync_tool_registry.py --write`). Regresyon:
   `test_tool_registry.py::GeneratedFilesAreNotLineMergedTests`.
3. **Ölçülmüş veriyi ölçülmemişle değiştirme.** `YETENEK_ENVANTERI` "Test
   Durumu" sütunu `pytest --junit-xml` ile ÖLÇÜLÜR; `pytest` yokken
   `--skip-tests` ile yeniden üretmek ölçülmüş `gecti` değerlerini
   `olculemedi`'ye düşürür. O ortamda dosya **bilerek yeniden üretilmez** ve
   durum açık madde olarak kaydedilir.

> **Açık soru / varsayım.** Release-health receipt'i şu an ayrı bir
> `gate_kind` olarak şemalanmadı; 4 kontrol Faz 4b birleşik gate'inin
> zorunlu child'ı olarak koşulur. Kendi `gate_kind`'ını hak edip etmediği,
> "release" kelimesinin kanonik anlamı (Gerber release / kabul edilmiş
> first-article / ürün release) kararlaştırılmadan çözülemez — bkz.
> [[references/phase-dag.md]] §Faz 5.

## 4. Kapı = `gate_receipt.json`, LLM'in "PASS" demesi değil

Bir metin çıktısı karar verisi değildir. Her kalite kapısı makine-okunur bir
makbuz üretmeden kapanmaz. Şema:
[`references/gate_receipt.schema.json`](references/gate_receipt.schema.json)

```bash
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

- **Yalnız exit 0 + effective `GREEN`** kapıyı açar. Exit 1 =
  RED/NO_COVERAGE/INVALID_METRIC/STALE; exit 2 = sözleşme veya çalışma hatası.
  Salt tool exit code'una bakma.
- Kanonik alan `schema_version: gate-receipt/v1`. Eski `schema` alanı yalnız
  okuma alias'ıdır; eksik alan çıkarımla doldurulmaz, çelişki `ERROR` olur.
- `metric_semantics` her ölçüm için definition, units, distance semantics,
  region, sampling step ve geometry model taşır. Centerline'ı edge-gap, chord'u
  arc-aware, `parallelism_coverage`'ı empedans/SI gibi sunmak `INVALID_METRIC`.
- Her `parsed_metrics` leaf yolu tam bir semantic entry'ye bağlıdır. Eksik,
  fazla veya inline+ref çakışan mapping `INVALID_METRIC`. Dış semantic ref yalnız
  `{metric_id, path, sha256}` nesnesidir.
- Metric leaf yalnız sonlu JSON number'dır (bool hariç). `coverage` için
  `scanned <= eligible` ve `scanned + excluded == eligible`; tutarsızlık
  `INVALID_COVERAGE/RED`. `coverage.per_metric` varsa bütün metric leaf'lerini
  birebir kapsar.
- A-seviyesi fault proof; hash-bağlı fixture, receipt input'una bağlı baseline,
  farklı injected hash, exact command/exit ve parsed metric leaf'lerine bağlı
  expected/observed failure ID'leri olmadan PASS değildir.
- Tüm receipt/artifact yollarını `--root` içinde tut. `../` ve mutlak path escape
  `ERROR`; external allowlist bu sürümde yoktur.
- `coverage.scanned=0` zorunlu kapıda `NO_COVERAGE`; advisory olarak açıkça
  işaretlenmedikçe birleşik kapıyı RED yapar. Zorunlu child hash/verdict'i
  güncel değilse veya child RED/INVALID/STALE ise parent GREEN olamaz.
- **Sonraki faz, makbuzdaki input hash'i güncel artifact hash'iyle eşleşmeden
  açılmaz.** Bu, "kapı geçildi ama sonra dosya değişti" sessiz hatasını kapatır.
- Araç çıkış kodunu **yakala**: `kicad-cli` için ERC/DRC'de
  `--exit-code-violations` olmadan ihlal exit code'a yansımaz ve kapı sahte
  PASS verir.
- LLM yargısı yalnız deterministik grader'ın **üretemeyeceği** C-seviyesi
  maddelerde (okunabilirlik, gerekçe kalitesi) kullanılır; DRC/ERC'nin yerine
  asla geçmez.

## 5. Doğrulama disiplini

Alt-ajan self-report'una güvenme. Kritik metriği (wire/net sayısı, ERC, unrouted,
DRC-tip, `parallelism_coverage`) **ana ajan yeniden ölçer**. Test yazdıysan
fault-injection ile boş olmadığını kanıtla.

### Alt-ajan kapanış ve transcript recovery
Alt-ajan sayısını ana ajan aynı tool + input hash'iyle yeniden ölçer. Ajan mesaj
vermeden kapanırsa transkriptini kurtar; rapor bulunursa provenance ile kaydet,
yoksa görevi yeniden aç. Mesajsız kapanışı "başarısız" veya self-report'u
"kanıt" diye sınıflandırma.

### İterasyon
Feedback kenarlarını say, döngü sınırı koy (ör. 3); aşılırsa `NEEDS_HUMAN`.

### Bilinmeyen
Uydurma yok. Eksik veri → `TBD`(+spec) / `CONFIRM`, insana bırak.

## 6. Araştırma bulgusunu skill'e terfi etme

Araştırmacı ajanları yalnız açık konular listesinden seçilmiş **dar** başlıklara
ata. Her ajandan iddia, birincil kaynak, tarih, geçerlilik kapsamı, güven ve
doğrulama yolu iste. Ham raporu doğrudan bir skill'e kopyalama.

```bash
python3 tools/scripts/research_promotion_gate.py --vault . --json promotion_report.json
```

1. Kaynak ve kapsam eksikse `RED`; araştırma notunda bırak.
2. Çelişkili mevcut kural varsa kararı insana aç; sessizce ezme.
3. Kabul edilen bulguyu karar günlüğüne kaydet.
4. İlgili skill'e yalnız **kısa, imperatif koşum kuralını** ekle; seviye ataması
   [[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]'nde yapılır.
5. Fixture/test/ölçüm koş; kapsam yoksa `NO_COVERAGE`, asla sessiz `PASS`.
6. Skill index'ini ve kanıt izini güncelle.

Genel artifact standardı dayatma. Yalnız kalite kapılarında sürümlü receipt
şeması zorunlu tut.

## 7. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Elektriksel gerçek | — | Orkestratör **süreç** kapısıdır; ölçümü adım skill'leri yapar |
| `pgrep` ile kilit tespiti | C | Yanlış pozitif verir → dört kanıtlı paket gerekir ([[references/board-write-lock.md]]) |
| Dış servis yan etkileri | — | Fab RFQ / API sorgusu **geri alınamaz**; dosya rollback'i bunları geri almaz |
| Alt-ajanın gerçekten ölçtüğü | — | Ana ajan yeniden ölçer |
| KiCad 11 uyumu | C | Sürüm numarasına bakıp otomatik uyum varsayma; fixture bazında yeniden doğrula |

### KiCad API sürüm kapısı
- KiCad 9/10 IPC'yi yalnız **açık GUI oturumu** için kullan; CI/ERC/DRC/export
  işini `kicad-cli` ile ayır. IPC'yi headless dosya kütüphanesi sayma.
- KiCad 11'e geçişte SWIG/IPC/CLI yolunu fixture bazında yeniden doğrula.

## 8. Doğrulama

```bash
# worker preflight / postflight
python3 tools/scripts/context_pack.py context_manifest.json --root . --json context_check.json
python3 tools/scripts/context_pack.py context_manifest.json --root . --postflight --json postflight_check.json

# kapı makbuzu
python3 tools/scripts/gate_receipt.py gate_receipt.json

# tool registry tazeliği (hash'leri yeniden üretir)
python3 tools/scripts/sync_tool_registry.py --check   # drift varsa exit 1
python3 tools/scripts/sync_tool_registry.py --write   # düzeltme: yeniden üret

# release-health (§3) — dördü de zorunlu
python3 -m unittest tests.test_tool_registry           # yön A/B/C/D
python3 -m unittest discover -s tools/tests -t .       # tam paket
```

> `--json` her zaman **yazar**. `context_pack.py --postflight` bildirilen argv'yi
> çalıştırdığı için **MCP üzerinden yayımlanmaz**.

## 9. Kabul kriterleri

- [ ] Faz 0 koşmadan hiçbir DAG düğümü açılmadı; her worker için hash-bağlı
      `context_manifest.json` üretildi ve `READY` alındı.
- [ ] Worker tamamlandıktan sonra `--postflight` koşuldu; yalnız exit 0 +
      `ACCEPTED` downstream'i açtı.
- [ ] Her kalite kapısı `gate_receipt.json` üretti; yalnız exit 0 + effective
      `GREEN` kapıyı açtı.
- [ ] Sonraki faz, makbuzdaki input hash'i güncel artifact hash'iyle **eşleştikten
      sonra** açıldı.
- [ ] Zorunlu child receipt'ler GREEN ve güncel board hash'inde; hiçbir parent
      RED/INVALID/STALE child üzerine GREEN olmadı.
- [ ] `coverage.scanned=0` hiçbir zorunlu kapıda PASS sayılmadı.
- [ ] `--exit-code-violations` (ve parity için `--schematic-parity
      --severity-all`) her ERC/DRC kapısında kullanıldı; exact argv receipt'te.
- [ ] Board yazan her adım transaction sırasına uydu ve kilit kanıt paketi
      toplandı ([[references/board-write-lock.md]]).
- [ ] Türetilmiş artifact'ler (zone fill, teardrop, uzunluk raporu, gerber/CPL)
      promote sonrası **yeniden üretildi**.
- [ ] Ana ajan kritik metrikleri alt-ajan self-report'una güvenmeden yeniden ölçtü.
- [ ] Feedback döngü sayacı sınır altında; aşıldıysa `NEEDS_HUMAN` verildi.
- [ ] Eksik veri `TBD`/`CONFIRM` olarak bırakıldı; uydurma yok.
- [ ] Araştırma bulguları terfi kapısından geçmeden skill'e girmedi.
- [ ] `config/tool_registry.json` `sync_tool_registry.py` ile güncel; kayıt dışı
      tool varsayılmadı.
- [ ] **Release-health dördü de koştu** (§3): `sync_tool_registry.py --check`
      GREEN, `test_tool_registry` (A/B/C/D) GREEN, tam paket koşuldu, sayaçlar
      ölçülerek tazelendi — anlatıdan kopyalanmadı.
- [ ] Hiçbir registered tool sahipsiz kalmadı; kalanlar `SKILL_EXEMPT`'te
      **gerekçesiyle** duruyor ve gerekçe bayat değil.
- [ ] Üretilmiş dosya elle düzenlenmedi; çakışma satır birleştirmeyle değil
      yeniden üretimle çözüldü.
- [ ] Ölçülmüş envanter verisi, ölçüm aracı yokken üretilmiş "ölçülemedi"
      değeriyle **değiştirilmedi**; durum açık madde olarak kaydedildi.
- [ ] Faz 5A first-article kabulü GREEN olmadan seri montaj açılmadı;
      Faz 5B L3 kanıtı olmadan hiçbir D satırı kapalı sayılmadı.

## İlgili

[[../decompose-pcb-decisions/SKILL|decompose-pcb-decisions]] ·
[[../claim-audit/SKILL|claim-audit]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../ilk-urun-montaj-kabulu/SKILL|ilk-urun-montaj-kabulu]] (Faz 5A) ·
[[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] (Faz 5B) ·
[[references/phase-dag.md]] · [[references/board-write-lock.md]] ·
[[../../docs/tool-inventory.md]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`promote`** — scratch → kanonik terfi kapısı; `gate-receipt/v1` üretir.
- **`error-memory`** — Kalıcı ERC/DRC/DFM hata hafızası: ne denendi, neye mal oldu.
- **`priors`** — Hatırlanan düzeltmeleri gerçekten işe yaramışlığa göre sıralar (Wilson alt sınırı). **Öğrenilmiş model değildir.**
- **`capability-inventory`** — Depo öz-envanteri: modül/test/kategori haritası; bir tur kapanırken sayılar tazelenir.
- **`pipeline-adapters`** — Arşivin monolitik `cmd_run`'ına gömülü fazlar üzerine tek tek çağrılabilir fonksiyonlar.
